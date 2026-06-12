import logging
from datetime import timedelta
from typing import Any

from .config import AppConfig
from .database import Database
from .notifications import notify_admin_job_failure


LOGGER = logging.getLogger("assistant.supervisor")
NON_FAILURE_REVIEW_REASONS = {
    "admin input requested",
    "deep research created",
    "human input requested",
    "project created",
    "requester input requested",
}


class Supervisor:
    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config

    def run_forever(self, stop_requested) -> None:
        interval = self.config.get_int("agent.limits.poll_interval_seconds", 60)
        while not stop_requested():
            try:
                count = self.run_once()
                LOGGER.info("supervisor reviewed %s job(s)", count)
            except Exception:
                LOGGER.exception("supervisor loop failed")
            stop_requested(interval)

    def run_once(self) -> int:
        stall_minutes = self.config.get_int("agent.supervisor.stall_threshold_minutes", 15)
        max_minutes = self.config.get_int("agent.supervisor.max_task_duration_minutes", 60)
        rows = self.db.fetch_all(
            """
            SELECT j.*,
                   MAX(l.created_at) AS last_log_at,
                   COUNT(l.id) AS log_count
            FROM jobs j
            LEFT JOIN task_logs l ON l.job_id = j.id
            WHERE j.status IN ('running', 'waiting', 'needs_review')
            GROUP BY j.id
            ORDER BY j.updated_at ASC
            LIMIT 100
            """
        )
        for row in rows:
            self.review_job(row, stall_minutes, max_minutes)
        return len(rows)

    def review_job(self, job: dict[str, Any], stall_minutes: int, max_minutes: int) -> None:
        status = job["status"]
        if status == "waiting":
            return

        if status == "needs_review":
            if self.needs_review_is_failure(job):
                reason = job.get("last_error") or "job needs review"
                if self.admin_failure_already_notified(job["id"], reason):
                    return
                self.add_note_once(job["id"], "Job is waiting for human review: %s" % (job.get("last_error") or "no reason recorded"))
                notify_admin_job_failure(
                    self.db,
                    self.config,
                    job,
                    "needs_review",
                    reason,
                    "supervisor needs_review",
                )
            return

        if status != "running":
            return

        last_log_at = job.get("last_log_at") or job.get("locked_at")
        locked_at = job.get("locked_at")
        if locked_at and locked_at.tzinfo is None:
            locked_at = locked_at.replace(tzinfo=last_log_at.tzinfo if last_log_at else None)

        stall_threshold = timedelta(minutes=stall_minutes)
        max_duration = timedelta(minutes=max_minutes)
        now = self.db.fetch_one("SELECT now() AS now")["now"]

        if last_log_at and now - last_log_at > stall_threshold:
            self.flag(job["id"], "No task log progress for more than %s minutes" % stall_minutes)
            return

        if locked_at and now - locked_at > max_duration:
            self.flag(job["id"], "Task has been running for more than %s minutes" % max_minutes)
            return

        failure = self.repeated_failure(job["id"])
        if failure:
            self.flag(job["id"], failure)

    def repeated_failure(self, job_id: int) -> str:
        rows = self.db.fetch_all(
            """
            SELECT event_type, tool_name, output_data
            FROM task_logs
            WHERE job_id = %s
              AND event_type IN ('error', 'tool_result', 'status_change', 'supervisor_note')
            ORDER BY sequence DESC
            LIMIT 50
            """,
            (job_id,),
        )
        failure_tool = ""
        failure_count = 0
        for row in rows:
            if self.failure_resolution_event(row):
                return ""
            if row["event_type"] != "error":
                continue
            tool_name = row["tool_name"] or "unknown"
            if not failure_tool:
                failure_tool = tool_name
            if tool_name != failure_tool:
                return ""
            failure_count += 1
            if failure_count >= 3:
                return "Tool %s failed three times without recovery" % failure_tool
        return ""

    def failure_resolution_event(self, row: dict[str, Any]) -> bool:
        event_type = row.get("event_type")
        output = row.get("output_data") or {}
        if event_type == "tool_result":
            return True
        if event_type == "status_change":
            status = str(output.get("status") or "")
            reason = str(output.get("reason") or "")
            if status in {"waiting", "completed", "failed", "cancelled"}:
                return True
            if reason in NON_FAILURE_REVIEW_REASONS:
                return True
        if event_type == "supervisor_note":
            if output.get("notification") == "admin_failure_email" and output.get("sent") is True:
                return True
        return False

    def flag(self, job_id: int, reason: str) -> None:
        updated = self.db.fetch_one(
            """
            UPDATE jobs
            SET status = 'needs_review',
                last_error = %s,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = now()
            WHERE id = %s
              AND status = 'running'
            RETURNING *
            """,
            (reason, job_id),
        )
        if not updated:
            return
        self.db.log_event(job_id, "status_change", output_data={"status": "needs_review", "reason": reason, "source": "supervisor"})
        notify_admin_job_failure(self.db, self.config, updated, "needs_review", reason, "supervisor")

    def add_note_once(self, job_id: int, note: str) -> None:
        existing = self.db.fetch_one(
            """
            SELECT id
            FROM task_logs
            WHERE job_id = %s
              AND event_type = 'supervisor_note'
              AND output_data->>'reason' = %s
            """,
            (job_id, note),
        )
        if existing:
            return
        self.db.log_event(job_id, "supervisor_note", output_data={"reason": note})

    def admin_failure_already_notified(self, job_id: int, reason: str) -> bool:
        row = self.db.fetch_one(
            """
            SELECT id
            FROM task_logs
            WHERE job_id = %s
              AND event_type = 'supervisor_note'
              AND output_data->>'notification' = 'admin_failure_email'
              AND output_data->>'sent' = 'true'
              AND output_data->>'reason' = %s
            LIMIT 1
            """,
            (job_id, str(reason or "job needs review")),
        )
        return row is not None

    def needs_review_is_failure(self, job: dict[str, Any]) -> bool:
        status_reason = self.latest_status_reason(job["id"])
        if status_reason in NON_FAILURE_REVIEW_REASONS:
            return False
        last_error = str(job.get("last_error") or "").strip()
        waiting_prefixes = (
            "Project #",
            "Deep research run #",
            "Async request is running.",
        )
        return not any(last_error.startswith(prefix) for prefix in waiting_prefixes)

    def latest_status_reason(self, job_id: int) -> str:
        row = self.db.fetch_one(
            """
            SELECT output_data->>'reason' AS reason
            FROM task_logs
            WHERE job_id = %s
              AND event_type = 'status_change'
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (job_id,),
        )
        return str(row["reason"] or "") if row else ""
