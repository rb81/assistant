import logging
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb

from .config import AppConfig, agent_email, message_id_domain
from .database import Database, json_safe
from .polling import poll_interval_seconds, run_poll_loop
from .time_utils import datetime_context_label, next_recurring_run_at


LOGGER = logging.getLogger("assistant.reminders")


class ReminderScheduler:
    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config

    def run_forever(self, stop_requested) -> None:
        interval = poll_interval_seconds(self.config, "agent.reminders.scheduler_poll_interval_seconds")
        run_poll_loop(
            stop_requested,
            self.run_once,
            interval,
            should_sleep=lambda count: not count,
            on_result=lambda count: LOGGER.info("reminder scheduler processed %s reminder(s)", count),
            logger=LOGGER,
            error_message="reminder scheduler loop failed",
        )

    def run_once(self) -> int:
        completed = self.sync_completed_reminders()
        queued = self.queue_due_reminders()
        return completed + queued

    def sync_completed_reminders(self) -> int:
        processed = 0
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT r.*, j.status AS job_status, j.last_error AS job_last_error
                    FROM reminders r
                    JOIN jobs j ON r.job_id = j.id
                    WHERE r.status = 'queued'
                      AND j.status IN ('completed', 'failed', 'cancelled')
                    ORDER BY r.id ASC
                    FOR UPDATE OF r SKIP LOCKED
                    """
                )
                rows = list(cur.fetchall())
                for reminder in rows:
                    if reminder["job_status"] == "completed" and reminder.get("recurrence_unit"):
                        next_run_at = next_recurring_run_at(
                            reminder["run_at"],
                            reminder["recurrence_unit"],
                            int(reminder.get("recurrence_interval") or 1),
                            self.config,
                            reminder.get("recurrence_anchor_day"),
                        )
                        cur.execute(
                            """
                            UPDATE reminders
                            SET status = 'scheduled',
                                run_at = %s,
                                job_id = NULL,
                                queued_at = NULL,
                                completed_at = now(),
                                last_error = NULL,
                                updated_at = now()
                            WHERE id = %s
                            """,
                            (next_run_at, reminder["id"]),
                        )
                        cur.execute(
                            "INSERT INTO manual_events(event_type, payload) VALUES (%s, %s)",
                            (
                                "reminder_rescheduled",
                                Jsonb(
                                    json_safe(
                                        {
                                            "reminder_id": reminder["id"],
                                            "completed_job_id": reminder["job_id"],
                                            "next_run_at": next_run_at,
                                        }
                                    )
                                ),
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE reminders
                            SET status = %s,
                                completed_at = now(),
                                last_error = %s,
                                updated_at = now()
                            WHERE id = %s
                            """,
                            (reminder["job_status"], reminder.get("job_last_error"), reminder["id"]),
                        )
                    processed += 1
        return processed

    def queue_due_reminders(self) -> int:
        limit = self.config.get_int("agent.reminders.max_due_per_tick", 25)
        queued = 0
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM reminders
                    WHERE status = 'scheduled'
                      AND run_at <= now()
                    ORDER BY run_at ASC, id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                    """,
                    (limit,),
                )
                reminders = list(cur.fetchall())
                for reminder in reminders:
                    job = self.create_job_for_reminder(cur, reminder)
                    cur.execute(
                        """
                        UPDATE reminders
                        SET status = 'queued',
                            job_id = %s,
                            queued_at = now(),
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (job["id"], reminder["id"]),
                    )
                    cur.execute(
                        "INSERT INTO manual_events(event_type, payload) VALUES (%s, %s)",
                        (
                            "reminder_queued",
                            Jsonb(json_safe({"reminder_id": reminder["id"], "job_id": job["id"]})),
                        ),
                    )
                    queued += 1
        return queued

    def create_job_for_reminder(self, cur, reminder: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        message_id = "<reminder-%s-%s@%s>" % (
            reminder["id"],
            int(now.timestamp() * 1000000),
            message_id_domain(self.config),
        )
        subject = "Reminder: %s" % reminder["title"]
        recurrence = ""
        if reminder.get("recurrence_unit"):
            recurrence = "Recurrence: every %s %s%s" % (
                reminder.get("recurrence_interval") or 1,
                reminder["recurrence_unit"],
                "" if int(reminder.get("recurrence_interval") or 1) == 1 else "s",
            )
        body_lines = [
            "Scheduled reminder task.",
            "",
            "Reminder ID: %s" % reminder["id"],
            "Title: %s" % reminder["title"],
            "Scheduled for: %s" % datetime_context_label(reminder["run_at"], self.config),
        ]
        if recurrence:
            body_lines.append(recurrence)
        body_lines.extend(["", "Task:", reminder["task"]])
        body = "\n".join(body_lines)
        cur.execute(
            """
            INSERT INTO emails(
              message_id,
              thread_id,
              from_address,
              to_addresses,
              subject,
              body_text,
              received_at,
              is_actionable
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, true)
            RETURNING *
            """,
            (message_id, message_id, "reminder@local", [agent_email(self.config)], subject, body, now),
        )
        email_row = cur.fetchone()
        cur.execute(
            """
            INSERT INTO jobs(thread_id, task_summary, priority, run_at)
            VALUES (%s, %s, %s, now())
            RETURNING *
            """,
            (
                message_id,
                subject,
                int(reminder.get("priority") or 0),
            ),
        )
        job_row = cur.fetchone()
        cur.execute(
            "INSERT INTO manual_events(event_type, payload) VALUES (%s, %s)",
            (
                "reminder_job_created",
                Jsonb(json_safe({"job_id": job_row["id"], "email_id": email_row["id"], "reminder_id": reminder["id"]})),
            ),
        )
        LOGGER.info("queued reminder %s as job %s", reminder["id"], job_row["id"])
        return job_row
