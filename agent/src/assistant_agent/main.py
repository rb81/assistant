import logging
import os
import signal
import time

import uvicorn

from .config import database_url, load_config
from .database import Database
from .deep_research import DeepResearchAgent
from .email_ingest import EmailDownloader
from .filesystem_permissions import apply_shared_file_umask
from .heartbeat import Heartbeat
from .prompt_context import ensure_prompt_files, validate_agent_prompt
from .projects import ProjectScheduler
from .reminders import ReminderScheduler
from .supervisor import Supervisor
from .task_agent import TaskAgent
from .tool_result_cache import ToolResultCache
from .validation import postgres_password_is_weak, validate_role_config
from .workspace_index import WorkspaceIndex


LOGGER = logging.getLogger("assistant.agent")
PROMPT_FILE_SEED_ROLES = {"api", "downloader", "task-agent", "deep-research-agent", "deep-research-cron", "all"}
PROMPT_REQUIRED_ROLES = {"task-agent", "all"}


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


class Stopper:
    def __init__(self) -> None:
        self.stop = False

    def request_stop(self, signum, frame) -> None:
        self.stop = True
        LOGGER.info("received shutdown signal", extra={"signal": signum})

    def __call__(self, sleep_seconds: float = 0) -> bool:
        if sleep_seconds <= 0:
            return self.stop
        deadline = time.monotonic() + sleep_seconds
        while not self.stop and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))
        return self.stop


def install_stopper() -> Stopper:
    stopper = Stopper()
    signal.signal(signal.SIGTERM, stopper.request_stop)
    signal.signal(signal.SIGINT, stopper.request_stop)
    return stopper


def run_role(role: str) -> None:
    config = load_config()
    apply_shared_file_umask(config)
    prompt_context_error = None
    if role in PROMPT_FILE_SEED_ROLES:
        try:
            ensure_prompt_files(config)
            if role in PROMPT_REQUIRED_ROLES:
                validate_agent_prompt(config)
        except RuntimeError as exc:
            prompt_context_error = str(exc)
    errors = validate_role_config(role, config)
    if prompt_context_error:
        errors.append(prompt_context_error)
    if errors:
        for error in errors:
            LOGGER.error("configuration error: %s", error)
        raise SystemExit(78)

    # Log warnings for degraded capabilities
    if role in ("task-agent", "all"):
        from .validation import admin_configured, smtp_configured
        if not smtp_configured(config):
            LOGGER.warning(
                "task-agent running without SMTP configuration: email_send, request_input email, "
                "and admin failure notifications will be unavailable. The agent can still function "
                "via the workspace and dashboard."
            )
        if not admin_configured(config):
            LOGGER.warning(
                "task-agent running without ADMIN_EMAIL configuration: admin notifications and "
                "request_input recipient='admin' will be unavailable."
            )

    if role == "api":
        port = config.get_int("agent.api.port", 8000)
        host = config.get("agent.api.bind_host", "0.0.0.0")
        if host == "0.0.0.0" and not config.get_bool("agent.api.allow_public_bind", False):
            LOGGER.error(
                "Refusing to bind the dashboard to 0.0.0.0 without an explicit override. "
                "This would expose all unauthenticated endpoints to every network interface. "
                "To allow this, set AGENT_API_ALLOW_PUBLIC_BIND=true (or agent.api.allow_public_bind: true "
                "in config/agent.yaml). Alternatively, set AGENT_API_BIND_HOST to 127.0.0.1 or a specific "
                "LAN IP. See docs/security.md for details."
            )
            raise SystemExit(78)
        uvicorn.run("assistant_agent.api:app", host=host, port=port)
        return

    db = Database(database_url(config))
    db.ensure_feature_schema()
    ran = db.run_migrations()
    if ran:
        LOGGER.info("applied %s migration(s): %s", len(ran), ", ".join(ran))
    stopper = install_stopper()

    if role == "downloader":
        EmailDownloader(db, config).run_forever(stopper)
    elif role == "reminder-scheduler":
        ReminderScheduler(db, config).run_forever(stopper)
    elif role == "reminder-cron":
        count = ReminderScheduler(db, config).run_once()
        LOGGER.info("reminder cron processed %s reminder(s)", count)
    elif role == "project-scheduler":
        ProjectScheduler(db, config).run_forever(stopper)
    elif role == "project-cron":
        count = ProjectScheduler(db, config).run_once()
        LOGGER.info("project cron processed %s item(s)", count)
    elif role == "deep-research-agent":
        DeepResearchAgent(db, config).run_forever(stopper)
    elif role == "deep-research-cron":
        worked = DeepResearchAgent(db, config).run_once()
        LOGGER.info("deep research cron processed %s run(s)", 1 if worked else 0)
    elif role == "task-agent":
        TaskAgent(db, config, role=role).run_forever(stopper)
    elif role == "supervisor":
        Supervisor(db, config).run_forever(stopper)
    elif role == "heartbeat":
        Heartbeat(db, config).run_forever(stopper)
    elif role == "heartbeat-cron":
        count = Heartbeat(db, config).run_once()
        LOGGER.info("heartbeat cron completed with %s action(s)", count)
    elif role == "workspace-indexer":
        WorkspaceIndex(db, config).run_forever(stopper)
    elif role == "workspace-index-cron":
        count = WorkspaceIndex(db, config).run_once()
        LOGGER.info("workspace index cron processed %s file(s)", count)
    elif role == "tool-cache-cleanup-cron":
        count = ToolResultCache(config).cleanup_expired()
        LOGGER.info("tool result cache cleanup removed %s file(s)", count)
    elif role == "all":
        LOGGER.info("role 'all' runs downloader, schedulers, agents, and supervisor sequentially")
        while not stopper():
            EmailDownloader(db, config).sync_once()
            ReminderScheduler(db, config).run_once()
            ProjectScheduler(db, config).run_once()
            DeepResearchAgent(db, config).run_once()
            WorkspaceIndex(db, config).run_once()
            TaskAgent(db, config, role=role).run_once()
            Supervisor(db, config).run_once()
            Heartbeat(db, config).run_once()
            stopper(config.get_int("agent.limits.poll_interval_seconds", 60))
    else:
        raise RuntimeError("unknown AGENT_ROLE: %s" % role)


def main() -> None:
    configure_logging()
    if postgres_password_is_weak():
        LOGGER.error(
            "SECURITY WARNING: POSTGRES_PASSWORD is not set or is using a known weak/placeholder "
            "value. Change it in your .env file before deploying. "
            "See docs/security.md for guidance."
        )
    role = os.getenv("AGENT_ROLE", "task-agent")
    run_role(role)


if __name__ == "__main__":
    main()
