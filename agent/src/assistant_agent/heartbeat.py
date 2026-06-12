import logging
from datetime import timedelta
from typing import Any

from psycopg.types.json import Jsonb

from .config import AppConfig, worker_id
from .database import Database, json_safe
from .notifications import notify_admin_job_failure


LOGGER = logging.getLogger("assistant.heartbeat")


class Heartbeat:
    """Rule-based queue health monitor with no LLM calls."""

    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.locked_by = worker_id("heartbeat")

    def run_forever(self, stop_requested) -> None:
        interval = self.config.get_int(
            "agent.heartbeat.poll_interval_seconds",
            self.config.get_int("agent.limits.poll_interval_seconds", 60),
        )
        while not stop_requested():
            try:
                count = self.run_once()
                LOGGER.info("heartbeat completed with %s rule action(s)", count)
            except Exception:
                LOGGER.exception("heartbeat loop failed")
            stop_requested(interval)

    def run_once(self) -> int:
        if not self.config.get_bool("agent.heartbeat.enabled", True):
            return 0
        now = self.db.fetch_one("SELECT now() AS now")["now"]
        actions = 0
        actions += self.flag_stalled_jobs(now)
        actions += self.notify_failed_jobs()
        actions += self.flag_stalled_deep_research(now)
        actions += self.flag_stalled_projects(now)
        if actions == 0:
            self.log_manual_event("heartbeat_rule_check", {"result": "healthy", "now": now})
        return actions

    def flag_stalled_jobs(self, now) -> int:
        threshold = timedelta(minutes=self.config.get_int("agent.heartbeat.stale_threshold_minutes", 30))
        rows = self.db.fetch_all(
            """
            SELECT j.*, MAX(l.created_at) AS last_log_at
            FROM jobs j
            LEFT JOIN task_logs l ON l.job_id = j.id
            WHERE j.status = 'running'
            GROUP BY j.id
            ORDER BY j.updated_at ASC
            LIMIT 100
            """
        )
        actions = 0
        for job in rows:
            last_activity = job.get("last_log_at") or job.get("locked_at") or job.get("updated_at")
            if last_activity and last_activity.tzinfo is None:
                last_activity = last_activity.replace(tzinfo=now.tzinfo)
            if last_activity and now - last_activity > threshold:
                reason = "Heartbeat detected no job progress for more than %s minutes" % int(threshold.total_seconds() // 60)
                self.db.update_job_status(job["id"], "needs_review", last_error=reason)
                notify_admin_job_failure(self.db, self.config, job, "needs_review", reason, "heartbeat rule")
                actions += 1
        return actions

    def notify_failed_jobs(self) -> int:
        rows = self.db.fetch_all(
            """
            SELECT *
            FROM jobs
            WHERE status IN ('failed', 'needs_review')
              AND last_error IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT 100
            """
        )
        actions = 0
        for job in rows:
            if self.expected_wait_state(job):
                continue
            notify_admin_job_failure(
                self.db,
                self.config,
                job,
                job.get("status") or "needs_review",
                job.get("last_error") or "attention required",
                "heartbeat rule",
            )
            actions += 1
        return actions

    def expected_wait_state(self, job: dict[str, Any]) -> bool:
        if job.get("status") == "waiting":
            return True
        reason = str(job.get("last_error") or "")
        if reason.startswith("Project #") or reason.startswith("Deep research run #") or reason.startswith("Async request is running"):
            return True
        latest = self.db.fetch_one(
            """
            SELECT output_data->>'reason' AS reason
            FROM task_logs
            WHERE job_id = %s
              AND event_type = 'status_change'
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (job["id"],),
        )
        return (latest or {}).get("reason") in {
            "human input requested",
            "requester input requested",
            "admin input requested",
            "project created",
            "deep research created",
        }

    def flag_stalled_deep_research(self, now) -> int:
        threshold = timedelta(minutes=self.config.get_int("agent.heartbeat.deep_research_stale_minutes", 60))
        rows = self.db.fetch_all(
            """
            SELECT dr.*, j.id AS job_id, j.thread_id, j.task_summary, j.status AS job_status, j.last_error AS job_last_error
            FROM deep_research_runs dr
            JOIN jobs j ON j.id = dr.original_job_id
            WHERE dr.status = 'running'
            ORDER BY dr.updated_at ASC
            LIMIT 100
            """
        )
        actions = 0
        for run in rows:
            last_activity = run.get("updated_at") or run.get("locked_at")
            if last_activity and last_activity.tzinfo is None:
                last_activity = last_activity.replace(tzinfo=now.tzinfo)
            if last_activity and now - last_activity > threshold:
                reason = "Heartbeat detected deep research run #%s has been running without update for more than %s minutes" % (
                    run["id"],
                    int(threshold.total_seconds() // 60),
                )
                self.db.execute(
                    """
                    UPDATE deep_research_runs
                    SET status = 'failed', last_error = %s, locked_at = NULL, locked_by = NULL, updated_at = now()
                    WHERE id = %s
                    """,
                    (reason, run["id"]),
                )
                job = {"id": run["job_id"], "thread_id": run["thread_id"], "task_summary": run["task_summary"], "status": run["job_status"], "last_error": run["job_last_error"]}
                notify_admin_job_failure(self.db, self.config, job, "needs_review", reason, "heartbeat rule")
                actions += 1
        return actions

    def flag_stalled_projects(self, now) -> int:
        threshold = timedelta(minutes=self.config.get_int("agent.heartbeat.project_stale_minutes", 60))
        rows = self.db.fetch_all(
            """
            SELECT p.*, j.thread_id, j.task_summary, j.status AS job_status, j.last_error AS job_last_error
            FROM projects p
            JOIN jobs j ON j.id = p.original_job_id
            WHERE p.status IN ('queued', 'running')
            ORDER BY p.updated_at ASC
            LIMIT 100
            """
        )
        actions = 0
        for project in rows:
            updated_at = project.get("updated_at")
            if updated_at and updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=now.tzinfo)
            if updated_at and now - updated_at > threshold:
                reason = "Heartbeat detected project #%s has had no update for more than %s minutes" % (
                    project["id"],
                    int(threshold.total_seconds() // 60),
                )
                self.db.execute("UPDATE projects SET last_error = %s, updated_at = now() WHERE id = %s", (reason, project["id"]))
                job = {"id": project["original_job_id"], "thread_id": project["thread_id"], "task_summary": project["task_summary"], "status": project["job_status"], "last_error": project["job_last_error"]}
                notify_admin_job_failure(self.db, self.config, job, "needs_review", reason, "heartbeat rule")
                actions += 1
        return actions

    def log_manual_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO manual_events(event_type, payload) VALUES (%s, %s)",
            (event_type, Jsonb(json_safe(payload))),
        )
