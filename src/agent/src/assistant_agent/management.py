"""Host-side operations for the native management interface."""
import copy
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig
from .management_errors import ApprovalApplicationIncomplete, PERMISSION_ACTIVATION_ERROR
from .secret_store import SECRET_NAMES
from .setup import (
    ADVANCED_SETTINGS, AGENT_SERVICES, ENV_OVERRIDES, LONG_RUNNING_SERVICES, DockerCommandError,
    Installation, PermissionActivationError, PublicSetupError, SetupError,
    draft_problems, ensure_docker_access, read_regular, validate_secret,
)
DEFAULT_PATHS = {key: path for key, path, _ in ENV_OVERRIDES}
SERVICE_NAMES = {
    "task-agent": "Task worker", "postgres": "Database", "downloader": "Email intake",
    "workspace-indexer": "Workspace indexer", "reminder-scheduler": "Reminders",
    "deep-research-agent": "Research worker", "supervisor": "Health monitor",
    "heartbeat": "Heartbeat", "telegram-receiver": "Telegram",
    "request-preprocessor": "Request screening", "clamav": "Malware scanning",
    "ollama": "Local embeddings", "transcription": "Voice transcription",
    "sandbox": "Script runner",
    "management": "Management API", "management-docker": "Management Docker reader",
    "management-reader": "Conversation service",
}


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    default: str = ""
    secret: bool = False
    boolean: bool = False


HELPER_VERIFICATION_TTL_SECONDS = 300
_HELPER_VERIFIED: dict[str, float] = {}


def field(key, label, default=""):
    return Field(key, label, default, key in SECRET_NAMES, default in {"true", "false"})


GROUPS = {
    "identity": ("About your assistant", [
        field("AGENT_IDENTITY_NAME", "Assistant name", "Assistant"), field("AGENT_APP_TIMEZONE", "Timezone", "UTC"),
        field("AGENT_ADMIN_NAME", "Your name"), field("AGENT_ADMIN_EMAIL", "Approval email"),
        field("AGENT_ORG_NAME", "Organization"), field("AGENT_ORG_SECURITY_EMAIL", "Security contact"),
        field("AGENT_ORG_INTERNAL_EMAIL_DOMAINS", "Internal email domains (comma-separated)"),
    ]),
    "models": ("Models & access", [
        field("AGENT_LLM_BASE_URL", "Provider API URL", "https://openrouter.ai/api/v1"),
        field("AGENT_LLM_MODEL", "Primary model"), field("AGENT_LLM_FALLBACK_MODEL", "Fallback model"),
        field("AGENT_LLM_OPENROUTER_API_KEY", "OpenRouter API key"), field("AGENT_LLM_OPENAI_API_KEY", "OpenAI-compatible API key"),
        field("AGENT_DATABASE_PASSWORD", "Database password"),
    ]),
    "email": ("Email", [
        field("SETUP_EMAIL_ENABLED", "Enable email", "false"),
        *[field(key, label, default) for key, label, default in (
            ("AGENT_EMAIL_IMAP_HOST", "IMAP server", ""), ("AGENT_EMAIL_IMAP_PORT", "IMAP TLS port", "993"),
            ("AGENT_EMAIL_IMAP_USERNAME", "Mailbox login", ""), ("AGENT_EMAIL_IMAP_PASSWORD", "Mailbox password", ""),
            ("AGENT_EMAIL_IMAP_FOLDER", "Incoming folder", "INBOX"), ("AGENT_EMAIL_IMAP_ARCHIVE_FOLDER", "Processed folder", "Archive"),
            ("AGENT_EMAIL_IMAP_QUARANTINE_FOLDER", "Quarantine folder", "Quarantine"), ("AGENT_EMAIL_IMAP_SENT_FOLDER", "Sent folder", "Sent"),
            ("AGENT_EMAIL_SMTP_HOST", "SMTP server", ""), ("AGENT_EMAIL_SMTP_PORT", "SMTP TLS port", "587"),
            ("AGENT_EMAIL_SMTP_USERNAME", "SMTP login", ""), ("AGENT_EMAIL_SMTP_PASSWORD", "SMTP password", ""),
            ("AGENT_EMAIL_SMTP_FROM", "Sender email", ""), ("AGENT_IDENTITY_EMAIL", "Assistant email", ""),
            ("AGENT_EMAIL_ACTIONABLE_SENDERS", "Allowed senders (comma-separated)", ""),
            ("AGENT_EMAIL_ALLOWED_RECIPIENT_DOMAINS", "Allowed recipient domains (comma-separated)", ""),
        )],
    ]),
    "telegram": ("Telegram", [
        field("AGENT_TELEGRAM_ENABLED", "Enable Telegram", "false"), field("AGENT_TELEGRAM_BOT_TOKEN", "Bot token"),
        field("AGENT_TELEGRAM_ALLOWED_USER_IDS", "Allowed numeric user IDs (comma-separated)"),
        field("AGENT_TELEGRAM_RECIPIENTS", 'Verified recipients (JSON: {"you@example.com": "123456"})', "{}"),
    ]),
    "calendar": ("Calendar", [
        field("AGENT_CALENDAR_ENABLED", "Enable calendar", "false"),
        field("AGENT_CALENDAR_DEFAULT_PROVIDER", "Default provider (google, outlook, zoho)", "google"),
        *[field(f"AGENT_CALENDAR_{provider}_{key}", f"{provider.title()} {label}", default)
          for provider in ("GOOGLE", "OUTLOOK", "ZOHO")
          for key, label, default in (
              ("CLIENT_ID", "client ID", ""), ("CLIENT_SECRET", "client secret", ""),
              ("REFRESH_TOKEN", "refresh token", ""), ("CALENDAR_ID", "calendar ID", "primary" if provider == "GOOGLE" else "default"),
          )],
        field("AGENT_CALENDAR_OUTLOOK_TENANT_ID", "Outlook tenant", "common"),
        field("AGENT_CALENDAR_ZOHO_API_BASE", "Zoho regional API URL", "https://calendar.zoho.com/api/v1"),
    ]),
    "zoom": ("Zoom", [
        field("AGENT_ZOOM_ENABLED", "Enable Zoom", "false"),
        field("AGENT_ZOOM_AUTH_MODE", "Authorization mode (s2s or oauth)", "s2s"),
        *[field("AGENT_ZOOM_" + key, label) for key, label in (
            ("CLIENT_ID", "Client ID"), ("CLIENT_SECRET", "Client secret"),
            ("ACCOUNT_ID", "Account ID"), ("REFRESH_TOKEN", "Refresh token"),
        )],
    ]),
    "dropbox": ("Dropbox", [
        field("AGENT_DROPBOX_ENABLED", "Enable private App Folder exchange", "false"),
        field("AGENT_DROPBOX_ALLOW_EXPORTS", "Allow exports", "false"),
        field("AGENT_DROPBOX_MAX_FILE_BYTES", "Maximum file size (bytes)", "26214400"),
        field("AGENT_DROPBOX_CLIENT_ID", "App Folder client ID"), field("AGENT_DROPBOX_REFRESH_TOKEN", "Refresh token"),
    ]),
    "transcription": ("Voice messages", [
        field("AGENT_TRANSCRIPTION_ENABLED", "Enable local voice transcription", "false"),
        field("AGENT_TRANSCRIPTION_LANGUAGE", "Language (auto, en or ar)", "auto"),
    ]),
    "embeddings": ("Search & embeddings", [
        field("AGENT_EMBEDDINGS_ENABLED", "Enable semantic search", "true"),
        field("AGENT_EMBEDDINGS_MODEL", "Embedding model", "embeddinggemma"),
        field("AGENT_EMBEDDINGS_BASE_URL", "Embedding API URL", "http://ollama:11434"),
        field("AGENT_EMBEDDINGS_DIMENSIONS", "Dimensions (blank uses model default)"),
        field("AGENT_EMBEDDINGS_TIMEOUT_SECONDS", "Timeout (seconds)", "20"),
    ]),
    "security": ("Safety & screening", [
        field("AGENT_SECURITY_DEVELOPMENT_MODEL", "Development supervisor model ID (blank = agent model)"),
        field("AGENT_SECURITY_DEVELOPMENT_MAX_MINUTES", "Maximum development session duration (minutes)", "30"),
        field("AGENT_SECURITY_DEVELOPMENT_MAX_RUNS", "Maximum development session runs", "20"),
        field("AGENT_SECURITY_EMAIL_AUTHENTICATION_ENABLED", "Authenticate admin email (DKIM/SPF, recommended)", "true"),
        field("AGENT_SECURITY_CONFIRM_NEW_ADMIN_CONVERSATIONS", "Confirm new admin conversations", "true"),
        field("AGENT_SECURITY_DECISION_MODEL_ENABLED", "Enable content screening", "false"),
        field("AGENT_SECURITY_DECISION_MODEL_PROVIDER", "Screening provider", "openrouter"),
        field("AGENT_SECURITY_DECISION_MODEL_URL", "Screening API URL (blank uses provider default)"),
        field("AGENT_SECURITY_DECISION_MODEL_MODEL", "Screening model"),
        field("AGENT_SECURITY_DECISION_MODEL_ON_ERROR", "On screening error (review, reject or allow)", "review"),
        field("AGENT_SECURITY_DECISION_MODEL_SCREEN_OUTBOUND_EMAIL", "Screen outgoing email", "true"),
        field("AGENT_SECURITY_DECISION_MODEL_SCREEN_SCRIPTS", "Screen scripts", "true"),
        field("AGENT_SECURITY_SCRIPTS_ALLOWED_DOMAINS", "Allowed script source domains"),
        field("AGENT_SECURITY_DECISION_MODEL_API_KEY", "Screening API key"),
    ]),
}
for title, settings in ADVANCED_SETTINGS.items():
    GROUPS["advanced:" + title] = (title, [
        Field(key, label, ",".join(default) if isinstance(default, list) else str(default).lower()
              if isinstance(default, bool) else str(default), False, kind is bool)
        for key, _, label, default, kind, _ in settings
    ])


class Management:
    def __init__(self, root, confirm):
        self.root = Path(root)
        self.confirm = confirm
        self.installation = None
        self.values = {}
        self.credentials = {}
        self.instructions = None
        self.accepted = False
        self.helper_ready = False
        self.dirty = False
        self.progress = ""

    def load(self):
        self.progress = "Reading protected local configuration"
        previous = self.installation
        self.installation = Installation(self.root)
        if previous is not None:
            self.installation.sudo_consent = getattr(previous, "sudo_consent", False)
        self.values = copy.deepcopy(self.installation.values)
        self.credentials = dict(self.installation.secret_values)
        self.instructions = None
        self.dirty = False
        self.accepted = bool(self.installation.state.get("setup_complete"))
        return self.installation

    def value(self, spec):
        if spec.secret:
            return ""
        if spec.key in self.values:
            if spec.boolean:
                return str(AppConfig({"value": self.values[spec.key]}).get_bool("value")).lower()
            return self.values[spec.key]
        if spec.key == "SETUP_EMAIL_ENABLED":
            return str("email" in self.values.get("COMPOSE_PROFILES", "").split(",")).lower()
        path = DEFAULT_PATHS.get(spec.key)
        configured = self.installation.defaults.get(path) if path else None
        if configured is None:
            return spec.default
        if spec.boolean:
            return str(AppConfig({"value": configured}).get_bool("value")).lower()
        if isinstance(configured, list):
            return ",".join(map(str, configured))
        if isinstance(configured, dict):
            return json.dumps(configured)
        return str(configured).lower() if isinstance(configured, bool) else str(configured)

    def stage(self, group, values):
        self.stage_fields(GROUPS[group][1], values)

    def stage_fields(self, specs, values):
        changes = []
        for spec in specs:
            value = values[spec.key] if spec.secret else values[spec.key].strip()
            if spec.secret:
                if value:
                    validate_secret(spec.key, value)
                    if spec.key == "AGENT_DATABASE_PASSWORD" and self.installation.secret_values.get(spec.key):
                        raise PublicSetupError("Use Credentials > Rotate database password for an existing database.")
            if not spec.secret or value:
                if self.installation.state.get("setup_complete") and not spec.secret and value == self.value(spec):
                    continue
                changes.append((spec.secret, spec.key, value))
        for secret, key, value in changes:
            target = self.credentials if secret else self.values
            if target.get(key) != value:
                target[key] = value
                self.dirty = True

    def draft(self):
        instance = copy.deepcopy(self.installation)
        instance.values = dict(self.values)
        instance.secret_values = dict(self.credentials)
        profiles = [name.strip() for name in instance.values.get("COMPOSE_PROFILES", "").split(",")
                    if name.strip()]
        for key, profile in (
            ("SETUP_EMAIL_ENABLED", "email"), ("AGENT_TELEGRAM_ENABLED", "telegram"),
            ("AGENT_TRANSCRIPTION_ENABLED", "voice")):
            path = DEFAULT_PATHS.get(key)
            configured = instance.defaults.get(path) if path else None
            if key in instance.values or configured is not None:
                profiles = [name for name in profiles if name != profile]
                if AppConfig({"enabled": instance.values.get(key, configured)}).get_bool("enabled"):
                    profiles.append(profile)
        instance.values["COMPOSE_PROFILES"] = ",".join(profiles)
        instance.prompt_copy = self.instructions
        return instance

    def problems(self):
        instance = self.draft()
        errors = draft_problems(instance)
        instructions = instance.prompt_copy if instance.prompt_copy is not None else instance.prompt_original
        if not instructions or not instructions.strip():
            errors.append(("instructions", "Review and save nonempty assistant instructions before completing setup."))
        return errors

    def save(self):
        if not self.accepted:
            raise PublicSetupError("Acknowledge the first-run safety notice before saving.")
        instance = self.draft()
        errors = self.problems()
        if errors:
            raise PublicSetupError("\n".join(message for _, message in errors))
        instance.state["setup_complete"] = True
        instance.save()
        self.load()

    def access(self):
        self.progress = "Checking Docker access"
        ensure_docker_access(self.installation, self.confirm)

    def _service_catalog(self):
        output = self.installation.compose(["--profile", "*", "config", "--format", "json"])
        try:
            services = json.loads(output)["services"]
        except (json.JSONDecodeError, KeyError, TypeError):
            raise PublicSetupError("Docker returned invalid component configuration. Check Docker and refresh.") from None
        if not isinstance(services, dict) or any(
                not isinstance(name, str) or not isinstance(config, dict)
                or not isinstance(config.get("profiles", []), list)
                or any(not isinstance(profile, str) for profile in config.get("profiles", []))
                for name, config in services.items()):
            raise PublicSetupError("Docker returned invalid component configuration. Check Docker and refresh.")
        return services

    def _enabled_service(self, config):
        profiles = config.get("profiles", [])
        enabled = {value.strip() for value in self.installation.values.get("COMPOSE_PROFILES", "").split(",")
                   if value.strip()}
        return not profiles or bool(enabled.intersection(profiles))

    @staticmethod
    def _one_shot_service(config):
        return config.get("restart", "no") == "no"

    def services(self, *, include_uptime=False):
        self.access()
        self.progress = "Reading component health"
        catalog = self._service_catalog()
        names = set(catalog)
        output = self.installation.compose(["--profile", "*", "ps", "--all", "--format", "json"])
        try:
            parsed = json.loads(output) if output.strip() else []
            rows = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            try:
                rows = [json.loads(line) for line in output.splitlines() if line.strip()]
            except json.JSONDecodeError:
                raise PublicSetupError("Docker returned invalid component status. Check Docker and refresh.") from None
        if any(not isinstance(row, dict) or not isinstance(row.get("Service"), str)
               or not isinstance(row.get("State", "unknown"), str)
               or row.get("Health") is not None and not isinstance(row["Health"], str) for row in rows):
            raise PublicSetupError("Docker returned invalid component status. Check Docker and refresh.")
        by_name = {}
        for row in rows:
            by_name.setdefault(row["Service"], []).append(row)
        result = []
        for name in sorted(names | set(by_name)):
            config = catalog.get(name, {})
            if name in by_name:
                state = ", ".join(sorted({row.get("State", "unknown") for row in by_name[name]}))
                health_values = set()
                for row in by_name[name]:
                    if self._one_shot_service(config) and row.get("State") in {"exited", "dead"}:
                        exit_code = row.get("ExitCode")
                        if type(exit_code) is int:
                            health_values.add(
                                "completed successfully" if exit_code == 0 else f"failed (exit code {exit_code})"
                            )
                        else:
                            health_values.add("completed (exit status unavailable)")
                    else:
                        health = row.get("Health") or "no healthcheck"
                        if health == "healthy" and row.get("State") != "running":
                            health = "not ready (container is not running; previous probe was healthy)"
                        probe = (config.get("healthcheck") or {}).get("test") or []
                        if "assistant_agent.worker_health" in probe:
                            if health == "healthy":
                                health = "healthy (recent polling + database; job/integration outcomes unverified)"
                            elif health == "unhealthy":
                                health = "unhealthy (polling/database unverified; inspect probe output and worker logs)"
                        elif health == "healthy" and name == "clamav":
                            health = "healthy (daemon ping; scan outcomes/signature freshness unverified)"
                        elif health == "healthy" and name == "ollama":
                            health = "healthy (model inventory API; inference/model readiness unverified)"
                        health_values.add(health)
                health = ", ".join(sorted(health_values))
            elif config.get("profiles") and not self._enabled_service(config):
                state, health = "profile inactive", "not checked"
            else:
                state, health = "not created", "not checked"
            result.append({
                "id": name, "title": SERVICE_NAMES.get(name, name), "worker": name in LONG_RUNNING_SERVICES,
                "state": state, "health": health,
                **({"uptime": ", ".join(sorted({
                    row["RunningFor"] for row in by_name.get(name, [])
                    if isinstance(row.get("RunningFor"), str) and row["RunningFor"]
                })) or None} if include_uptime else {}),
            })
        return result

    def lifecycle(self, action, service=None, *, preserve_management=False):
        if action in {"start", "apply"} and not self.installation.state.get("setup_complete"):
            raise PublicSetupError("Complete and save setup before starting the application.")
        self.access()
        self.installation.assert_current()
        if action == "start" and not service:
            if self.dirty:
                raise PublicSetupError("Save or discard your draft before starting the application.")
            self.progress = "Building and starting the application"
            if preserve_management:
                self.installation.start_containers(preserve_management=True)
            else:
                self.installation.start_containers()
            return self.services()
        catalog = self._service_catalog()
        if service and service not in catalog:
            raise PublicSetupError("Service is no longer configured. Refresh the list.")
        if preserve_management and service in {"management", "management-docker"}:
            allowed = {"restart", "apply"} if service == "management" else {"restart"}
            if action not in allowed:
                raise PublicSetupError("This management control-plane action is not safe through its own API.")
            if service == "management-docker":
                if os.environ.get("ASSISTANT_MANAGEMENT_EXECUTOR") != "1":
                    raise PublicSetupError("Restarting the management executor requires its private executor channel.")
                self.progress = "Scheduling management executor restart"
                return {"_restart_management_docker": True}
        commands = {
            "start": ["up", "-d", "--wait", "--wait-timeout", "180"],
            "restart": ["restart"], "stop": ["stop"],
            "apply": ["up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "180"],
        }
        if action not in commands:
            raise PublicSetupError("Unsupported service action")
        if action != "stop":
            self.progress = "Refreshing credential grants"
            self.installation.reconcile_grants()
        self.progress = action.title() + " " + (service or "application")
        profile_args = ["--profile", "*"]
        protected = ({"management", "management-docker"}
                     if preserve_management or action == "stop" and not service else set())
        one_shot = {name for name, config in catalog.items() if self._one_shot_service(config)}
        if service and action in {"start", "restart", "apply"} and service in one_shot:
            self.installation.compose(
                profile_args + [
                    "up", "--pull", "never", "--no-build", "--no-deps", "--force-recreate", service,
                ],
                timeout=1800,
            )
            return self.services()
        if action == "stop" and not service:
            created = self.installation.compose(profile_args + ["ps", "--all", "--services"]).split()
            targets = [name for name in created if name not in protected]
        elif action == "restart" and not service:
            running = self.installation.compose(profile_args + ["ps", "--status", "running", "--services"]).split()
            targets = [name for name in running if name not in protected and name not in one_shot]
        elif action == "apply" and not service:
            targets = [
                name for name, config in catalog.items()
                if name not in protected and name not in one_shot and self._enabled_service(config)
            ]
        else:
            targets = [service] if service else []
        if service and action == "stop":
            created = set(self.installation.compose(profile_args + ["ps", "--all", "--services"]).split())
            if service not in created:
                return self.services()
        if service and action == "restart" and service not in one_shot:
            created = set(self.installation.compose(profile_args + ["ps", "--all", "--services"]).split())
            if service not in created:
                self.installation.compose(
                    profile_args + ["up", "-d", "--wait", "--wait-timeout", "180", service], timeout=1800,
                )
                return self.services()
        if not targets:
            return self.services()
        verify_management_restart = service == "management" and action == "restart"
        previous = self._management_container_state(timeout=15) if verify_management_restart else None
        self.installation.compose(
            profile_args + commands[action] + targets, timeout=180 if verify_management_restart else 1800,
        )
        if verify_management_restart:
            self._verify_management_restart(previous)
        return self.services()

    def _management_container_state(self, *, timeout):
        identifiers = self.installation.compose(
            ["--profile", "*", "ps", "--all", "--quiet", "management"], timeout=timeout,
        ).split()
        if len(identifiers) != 1 or not re.fullmatch(r"[a-f0-9]{12,64}", identifiers[0]):
            raise PublicSetupError("The management API container could not be identified for restart verification.")
        try:
            record = json.loads(self.installation.docker(
                ["inspect", "--format", "{{json .}}", identifiers[0]], timeout=timeout,
            ))
            labels = record["Config"]["Labels"]
            state = record["State"]
            if (labels.get("com.docker.compose.project") !=
                    self.installation.values.get("COMPOSE_PROJECT_NAME", "assistant")
                    or labels.get("com.docker.compose.service") != "management"
                    or not isinstance(state["StartedAt"], str)):
                raise ValueError
            return identifiers[0], state
        except (ValueError, KeyError, TypeError, AttributeError):
            raise PublicSetupError("The management API container could not be verified.") from None

    def _verify_management_restart(self, previous, *, wait_seconds=180):
        self.progress = "Verifying restarted management API health"
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            identifier, state = self._management_container_state(timeout=min(15, max(0.1, remaining / 2)))
            changed = identifier != previous[0] or state["StartedAt"] != previous[1]["StartedAt"]
            health = (state.get("Health") or {}).get("Status")
            if (changed and state.get("Running") is True and health == "healthy"
                    and time.monotonic() < deadline):
                return
            if changed and (health == "unhealthy" or state.get("Status") in {"exited", "dead"}):
                raise PublicSetupError("The restarted management API container failed its health check.")
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        raise PublicSetupError("The restarted management API container was not verified healthy before its deadline.")

    def logs(self, service):
        self.access()
        if service not in self._service_catalog():
            raise PublicSetupError("Select a configured service.")
        return self.installation.compose(
            ["--profile", "*", "logs", "--no-color", "--timestamps", "--tail", "150", service], timeout=30)

    def request(self, payload):
        if not self.installation.state.get("setup_complete"):
            raise PublicSetupError("Complete and save setup before managing application records.")
        self.access()
        self.prepare_management()
        self.progress = "Reading application records" if payload.get("kind") != "mutate" else "Applying the confirmed change"
        args = ["run", "--rm", "--no-deps", "--pull", "never", "-T", "--volume", self.management_source(),
                "--entrypoint", "python",
                "task-agent", "-m", "assistant_agent.management_operator"]
        try:
            output = self.installation.compose(args, input_text=json.dumps(payload))
        except DockerCommandError as error:
            try:
                failure = json.loads(error.stdout or "")
            except json.JSONDecodeError:
                self._forget_helper()
                raise error from None
            if isinstance(failure, dict) and isinstance(failure.get("error"), str):
                if failure.get("code") == "permission_activation_incomplete":
                    raise PermissionActivationError(PERMISSION_ACTIVATION_ERROR) from None
                if failure.get("code") == "approval_application_incomplete":
                    raise ApprovalApplicationIncomplete() from None
                raise PublicSetupError(failure["error"]) from None
            raise
        try:
            result = json.loads(output)
            if not isinstance(result, dict) or "error" in result:
                raise ValueError
            if payload.get("kind") in {
                "tasks", "memories", "reminders", "projects", "scripts", "task-events", "project-objects",
                "task-messages", "research", "research-events", "conversations", "task-activity",
            } and (not isinstance(result.get("rows"), list)
                   or any(not isinstance(row, dict) for row in result["rows"])
                   or type(result.get("total")) is not int or result["total"] < 0):
                raise ValueError
            kind = payload.get("kind")
            if kind == "summary" and (
                    not isinstance(result.get("jobs"), list)
                    or any(not isinstance(row, dict) or not isinstance(row.get("status"), str)
                           or type(row.get("count")) is not int or row["count"] < 0
                           for row in result["jobs"])):
                raise ValueError
            if kind in {"tasks", "memories", "reminders", "projects", "scripts", "project-objects", "research"}:
                if any(type(row.get("id")) is not int or row["id"] <= 0 for row in result["rows"]):
                    raise ValueError
            if kind == "memories" and any(not isinstance(row.get("content"), str) for row in result["rows"]):
                raise ValueError
            if kind == "script" and (type(result.get("id")) is not int
                                     or not isinstance(result.get("version"), dict)
                                     or result["version"].get("id") != payload.get("id")):
                raise ValueError
            if kind == "mutate" and result.get("result") in (None, False):
                raise ValueError
            if kind in {"task.start", "task.reply", "research.start"} and any(
                type(result.get(key)) is not int or result[key] <= 0
                for key in ("task_id", "request_id", "workstream_id")
            ):
                raise ValueError
            if kind == "research.start" and (type(result.get("research_id")) is not int
                                               or result["research_id"] <= 0):
                raise ValueError
            if kind == "task-messages" and any(
                type(row.get("id")) is not int or row["id"] <= 0
                or not isinstance(row.get("kind"), str) or not isinstance(row.get("body"), str)
                or not isinstance(row.get("channel"), str) or not isinstance(row.get("created_at"), str)
                for row in result["rows"]
            ):
                raise ValueError
            if kind == "task-activity" and (type(result.get("truncated")) is not bool or any(
                not isinstance(row.get("id"), str) or not isinstance(row.get("tool_name"), str)
                or not isinstance(row.get("created_at"), str)
                or row.get("status") not in {"running", "succeeded", "failed", "interrupted"}
                for row in result["rows"]
            )):
                raise ValueError
            if kind == "conversations" and any(
                not isinstance(row.get("thread_id"), str) or not isinstance(row.get("channel"), str)
                or type(row.get("latest_task_id")) is not int or row["latest_task_id"] <= 0
                or type(row.get("message_count")) is not int
                for row in result["rows"]
            ):
                raise ValueError
            if kind == "permission.control" and result.get("status") != "updated":
                raise ValueError
            if kind == "permissions.read" and not isinstance(result.get("data"), (dict, list)):
                raise ValueError
            if kind == "approvals.list" and (
                    not isinstance(result.get("approvals"), list)
                    or any(not isinstance(item, dict) for item in result["approvals"])
                    or type(result.get("limit")) is not int or result["limit"] != payload.get("limit", 50)
                    or type(result.get("offset")) is not int or result["offset"] != payload.get("offset", 0)):
                raise ValueError
            if kind == "approval.decide" and not isinstance(result.get("result"), str):
                raise ValueError
            if kind == "project-files" and (
                    not isinstance(result.get("files"), list)
                    or type(result.get("total")) is not int or result["total"] < 0
                    or any(not isinstance(item, dict) or type(item.get("id")) is not int
                           or item["id"] <= 0 or not isinstance(item.get("path"), str)
                           for item in result["files"])):
                raise ValueError
            if kind == "project-file-content" and (
                    type(result.get("id")) is not int or result["id"] != payload.get("file_id")
                    or not isinstance(result.get("content_base64"), str)
                    or type(result.get("next_offset")) is not int
                    or type(result.get("size_bytes")) is not int):
                raise ValueError
            if kind in {"file.upload", "file.attach"} and (
                    result.get("workstream_id") != payload.get("workstream_id")
                    or type(result.get("file_id")) is not int or result["file_id"] <= 0
                    or not isinstance(result.get("object_ids"), list)
                    or any(type(value) is not int or value <= 0 for value in result["object_ids"])):
                raise ValueError
            return result
        except (json.JSONDecodeError, ValueError):
            raise PublicSetupError("Management response was invalid. Rebuild the application image and refresh; no success was confirmed.") from None

    def mutate(self, action, identifier=None, body=""):
        return self.request({"kind": "mutate", "action": action, "id": identifier,
                             "body": body, "confirmed": True})

    def management_source(self):
        return str(self.root.resolve() / "src/agent/src") + ":/app/src:ro"

    def prepare_management(self):
        if self.helper_ready:
            return
        # Each API request builds a fresh backend; without this, every read paid for an extra
        # container start just to re-prove the helper imports.
        verified = _HELPER_VERIFIED.get(self._helper_cache_key())
        if verified is not None and time.monotonic() - verified < HELPER_VERIFICATION_TTL_SECONDS:
            self.helper_ready = True
            return
        self.access()
        self.progress = "Checking management runtime"
        output = self.installation.compose(["config", "--format", "json"])
        try:
            config = json.loads(output)
            service = config["services"]["task-agent"]
            image = service.get("image")
            if not image and service.get("build"):
                image = config["name"] + "-task-agent"
            if not isinstance(image, str) or not image.strip():
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise PublicSetupError("Could not identify the task-agent image from saved Compose configuration. "
                             "Check that task-agent has an image or build configuration and Compose reports "
                             "the project name.") from None
        try:
            self.installation.docker(["image", "inspect", image], timeout=30)
        except DockerCommandError as error:
            raise PublicSetupError("The configured task-agent image could not be inspected. "
                             "If it is missing, use Services > Start app to build it. "
                             "Otherwise check Docker access. " + str(error)) from None
        try:
            self.installation.compose([
                "run", "--rm", "--no-deps", "--pull", "never", "-T",
                "--volume", self.management_source(), "--entrypoint", "python",
                "task-agent", "-c", "import assistant_agent.management_operator, assistant_agent.management_domains",
            ], timeout=60)
        except DockerCommandError as error:
            raise PublicSetupError("The management helper failed its startup check. "
                             "This does not mean the application is stopped. If server source was updated, "
                             "rebuild the task-agent image so its runtime dependencies match. "
                             "Restarting alone does not install missing dependencies. "
                             "Check Docker Compose compatibility and the helper's source mount and dependencies, "
                             "then refresh. " + str(error)) from None
        self.helper_ready = True
        _HELPER_VERIFIED[self._helper_cache_key()] = time.monotonic()

    def _helper_cache_key(self):
        return str(self.root.resolve())

    def _forget_helper(self):
        self.helper_ready = False
        _HELPER_VERIFIED.pop(self._helper_cache_key(), None)

    def project_request(self, action, payload):
        if not self.installation.state.get("setup_complete"):
            raise PublicSetupError("Complete and save setup before managing projects.")
        self.access()
        self.prepare_management()
        mount = str(self.root.resolve() / "data/private/artifacts") + ":/data/private/artifacts"
        try:
            output = self.installation.compose([
                "run", "--rm", "--no-deps", "--pull", "never", "-T", "--volume", mount,
                "--volume", self.management_source(), "--entrypoint", "python",
                "task-agent", "-m", "assistant_agent.workstream_operator", action,
            ], input_text=json.dumps(payload), timeout=1800)
        except DockerCommandError as error:
            try:
                failure = json.loads(error.stdout or "")
            except json.JSONDecodeError:
                self._forget_helper()
                raise error from None
            if isinstance(failure, dict) and isinstance(failure.get("workstream_error"), str):
                raise PublicSetupError(failure["workstream_error"]) from None
            raise
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            raise PublicSetupError("Invalid project response; no change was confirmed.") from None
        if not isinstance(result, dict) or "workstream_error" in result:
            raise PublicSetupError("Invalid project response; no change was confirmed.")
        if action == "preview" and (not isinstance(result.get("preview_digest"), str)
                                    or not result["preview_digest"]):
            raise PublicSetupError("Invalid project inventory; no deletion can be confirmed.")
        return result

    def preview_project(self, identifier):
        return self.project_request("preview", {"workstream_id": identifier, "mode": "preserve_shared"})

    def delete_project(self, identifier, digest):
        self.access()
        running = sorted(set(self.installation.compose(["ps", "--status", "running", "--services"]).split())
                         & set(AGENT_SERVICES))
        if running:
            self.progress = "Pausing writers for a consistent project inventory"
            self.installation.compose(["stop", "--timeout", "120", *running], timeout=180)
        attempted = completed = False
        try:
            preview = self.preview_project(identifier)
            if any(preview.get(key) for key in ("running_jobs", "running_research", "blocked_files", "blocked_reminders")):
                raise PublicSetupError("Active work or unverified files block deletion. Resolve them and preview again. Nothing was deleted.")
            if preview.get("preview_digest") != digest:
                raise PublicSetupError("The project inventory changed. Preview and confirm the new scope. Nothing was deleted.")
            attempted = True
            result = self.project_request("delete", {
                "workstream_id": identifier, "mode": "preserve_shared", "preview_digest": digest,
                "confirmed": True, "writers_stopped": True,
            })
            completed = result.get("status") == "completed"
            if not completed:
                raise PublicSetupError("Project deletion is incomplete. Writer services remain stopped; inspect pending deletions.")
        finally:
            try:
                pending = self.pending_deletions()
            except (SetupError, OSError, ValueError) as error:
                raise PublicSetupError("Could not verify deletion state. Writer services remain stopped. "
                                 "Check pending deletions from Storage & recovery before restarting.") from error
            if pending or attempted and not completed:
                raise PublicSetupError("An incomplete deletion remains. Writer services stay stopped. Resume it from Storage & recovery.")
            if running:
                self.installation.compose(["start", *running])

    def pending_deletions(self):
        result = self.project_request("pending", {})
        if not isinstance(result.get("deletions"), list) or any(
                not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0
                or not isinstance(row.get("status"), str) for row in result.get("deletions", [])):
            raise PublicSetupError("Could not verify pending deletions. Writer services must remain stopped.")
        return result["deletions"]

    def resume_deletion(self, identifier):
        self.access()
        pending = self.pending_deletions()
        if identifier not in {row["id"] for row in pending}:
            raise PublicSetupError("Select an existing pending deletion.")
        running = sorted(set(self.installation.compose(["ps", "--status", "running", "--services"]).split())
                         & set(AGENT_SERVICES))
        if running:
            self.installation.compose(["stop", "--timeout", "120", *running], timeout=180)
        result = self.project_request("resume", {"deletion_id": identifier, "confirmed": True, "writers_stopped": True})
        if result.get("status") != "completed" or self.pending_deletions():
            raise PublicSetupError("Deletion remains incomplete. Writer services are intentionally stopped.")
        if running:
            self.installation.compose(["start", *running])

    def replace_credential(self, name, value):
        validate_secret(name, value)
        if self.dirty:
            raise PublicSetupError("Save or discard your draft before replacing a live credential.")
        self.access()
        if name == "AGENT_DATABASE_PASSWORD":
            self.installation.rotate_database(value)
        else:
            instance = copy.deepcopy(self.installation)
            instance.secret_values[name] = value
            instance.save()
        self.load()

    def apply_credentials(self, name=None):
        if self.dirty:
            raise PublicSetupError("Save or discard your draft before activating credentials.")
        pending = self.installation.state.get("pending", [])
        if name is not None and name not in pending:
            raise PublicSetupError("This credential has no pending activation.")
        self.access()
        self.installation.apply([name] if name is not None else pending)
        self.load()

    def reset(self, *, clean_install=False, keep_mailbox_position=True):
        from .setup_tools import run_factory_reset

        if self.dirty:
            raise PublicSetupError("Save or discard your draft before resetting the database.")
        self.access()

        class Report:
            def __init__(self):
                self.messages = []

            def print(self, value, **kwargs):
                self.messages.append(value)

            def run_command(self, instance, arguments):
                instance.compose(arguments, timeout=1800)
                return 0

        report = Report()
        run_factory_reset(self.installation, report, clean_install=clean_install,
                          keep_mailbox_position=keep_mailbox_position)
        return report.messages[-1]

    def recover(self):
        from .setup import recover_database
        if self.dirty:
            raise PublicSetupError("Save or discard your draft before recovering database credentials.")
        recover_database(self.root, prepare=lambda instance: ensure_docker_access(instance, self.confirm))
        return self.load()

    def embeddings(self, operation):
        if operation not in {"status", "probe", "reindex"}:
            raise PublicSetupError("Unsupported search operation")
        self.access()
        self.prepare_management()
        output = self.installation.compose([
            "run", "--rm", "--no-deps", "--pull", "never", "-T",
            "--volume", self.management_source(), "--entrypoint", "python",
            "task-agent", "-m", "assistant_agent.embedding_maintenance", operation,
        ], timeout=1800)
        try:
            result = json.loads(output)
            if not isinstance(result, dict) or "error" in result:
                raise ValueError
            return result
        except (json.JSONDecodeError, ValueError):
            raise PublicSetupError("Search maintenance returned an invalid response. No success was confirmed.") from None

    def storage(self, volume):
        if self.dirty:
            raise PublicSetupError("Save or discard your draft before changing credential storage.")
        self.access()
        instance = copy.deepcopy(self.installation)
        instance.use_credential_volume = volume
        instance.save()
        self.load()
        try:
            targets = self.installation.apply(sorted(self.installation.secret_values))
            if volume:
                self.installation.compose(
                    ["up", "-d", "--no-deps", "--force-recreate", "management"],
                )
            if not volume and self.installation.state.get("previous_credential_volume"):
                self.installation.retire_previous_volume()
        except SetupError as error:
            raise PublicSetupError("Credential storage was saved, but activation or retirement is incomplete. "
                             "Use Activate pending in Credentials or Finish credential storage change "
                             "in Storage & recovery, then check service health. " + str(error)) from None
        self.load()
        return targets

    def instruction_text(self):
        if self.instructions is not None:
            return self.instructions.decode()
        return (read_regular(self.root / "data/config/AGENT.md") or
                read_regular(self.root / "src/config/AGENT.md.example") or b"").decode()

    def generate_database_password(self):
        if self.installation.secret_values.get("AGENT_DATABASE_PASSWORD"):
            raise PublicSetupError("An existing database password must be rotated, not overwritten.")
        try:
            initialized = (self.root / "data/postgres/pgdata/PG_VERSION").exists()
        except PermissionError:
            raise PublicSetupError("Cannot inspect existing PostgreSQL data. Supply its current password; no replacement was generated.") from None
        if initialized:
            raise PublicSetupError("Existing PostgreSQL data detected. Supply its current password; no replacement was generated.")
        return secrets.token_urlsafe(36)

    def redact(self, value):
        secrets = set(self.credentials.values())
        if self.installation is not None:
            secrets.update(self.installation.secret_values.values())
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[hidden]")
        return value
