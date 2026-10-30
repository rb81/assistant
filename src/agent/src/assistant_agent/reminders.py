import logging
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb

from .config import AppConfig, agent_email, message_id_domain
from .database import Database, json_safe
from . import scheduled_scripts
from .polling import poll_interval_seconds, run_poll_loop
from .requests import IncomingRequest, RequestService, TransactionDatabase
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
        delivered = self.deliver_notifications()
        return completed + queued + delivered

    def deliver_notifications(self) -> int:
        from .replies import ReplyRouter
        from .task_agent import TaskAgent
        from .tools import ToolRuntime
        jobs = self.db.fetch_all(
            """SELECT j.*, r.task AS notification_text FROM jobs j JOIN reminders r ON r.job_id = j.id
               WHERE j.status = 'waiting' AND j.run_at <= now() AND r.status = 'queued'
               AND j.metadata->>'notification_only' = 'true' ORDER BY j.id LIMIT 25"""
        )
        delivered = 0
        for job in jobs:
            with self.db.connect() as connection:
                with connection.cursor() as cursor:
                    key = "reminder-delivery:%s" % job["id"]
                    cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS claimed", (key,))
                    if not cursor.fetchone()["claimed"]:
                        continue
                    try:
                        current = self.db.fetch_one("SELECT status FROM jobs WHERE id = %s", (job["id"],))
                        if not current or current["status"] != "waiting":
                            continue
                        result = ReplyRouter(ToolRuntime(self.db, self.config, job)).send(job["notification_text"], purpose="reminder")
                        if result.get("status") == "sent":
                            database = TransactionDatabase(connection)
                            completed = database.fetch_one(
                                """UPDATE jobs SET status = 'completed', completed_at = now()
                                   WHERE id = %s AND status = 'waiting' RETURNING id""", (job["id"],),
                            )
                            if completed:
                                TaskAgent(database, self.config).record_final_response(job["id"], job["notification_text"])
                                delivered += 1
                        elif result.get("status") == "uncertain":
                            self.db.update_job_status(job["id"], "needs_review", last_error="Reminder delivery uncertain; reconcile before retrying")
                        else:
                            delay = max(30, int(result.get("retry_after") or 0))
                            self.db.execute("""UPDATE jobs SET attempts = attempts + 1,
                                status = CASE WHEN attempts >= 2 THEN 'failed' ELSE 'waiting' END,
                                last_error = 'Reminder delivery failed', run_at = now() + (%s * interval '1 second')
                                WHERE id = %s AND status = 'waiting'""", (delay, job["id"]))
                    finally:
                        cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
        return delivered

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
        alerts = []
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM reminders
                    WHERE status = 'scheduled'
                      AND run_at <= now()
                      AND EXISTS (
                        SELECT 1 FROM workstream_objects o
                        JOIN workstream_attachments a ON a.object_id = o.id
                        JOIN workstreams w ON w.id = a.workstream_id
                        WHERE o.id = reminders.object_id AND NOT o.deleting AND w.status = 'active'
                      )
                    ORDER BY run_at ASC, id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                    """,
                    (limit,),
                )
                reminders = list(cur.fetchall())
                for reminder in reminders:
                    problem = scheduled_scripts.due_problem(TransactionDatabase(conn), self.config, reminder)
                    if problem:
                        alerts.append(self.skip_scheduled_script(cur, reminder, problem))
                        queued += 1
                        continue
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
        for reminder, problem, consequence in alerts:
            scheduled_scripts.alert(
                self.db, self.config, "Assistant: scheduled script skipped (reminder #%s)" % reminder["id"],
                scheduled_scripts.alert_body(reminder, problem, consequence),
            )
        return queued

    def skip_scheduled_script(self, cur, reminder: dict[str, Any], problem: str) -> tuple:
        """Never hand an unapproved scheduled script to the agent, which would request approval unattended."""
        error = "Scheduled script not run: %s" % problem
        if reminder.get("recurrence_unit"):
            next_run_at = next_recurring_run_at(
                reminder["run_at"], reminder["recurrence_unit"], int(reminder.get("recurrence_interval") or 1),
                self.config, reminder.get("recurrence_anchor_day"),
            )
            cur.execute("UPDATE reminders SET run_at = %s, last_error = %s, updated_at = now() WHERE id = %s",
                        (next_run_at, error, reminder["id"]))
            consequence = "This occurrence was skipped; the next one is due %s." % datetime_context_label(next_run_at, self.config)
        else:
            next_run_at = None
            cur.execute(
                """UPDATE reminders SET status = 'failed', completed_at = now(), last_error = %s, updated_at = now()
                   WHERE id = %s""", (error, reminder["id"]))
            consequence = "This one-time reminder did not run and is marked failed."
        cur.execute(
            "INSERT INTO manual_events(event_type, payload) VALUES (%s, %s)",
            ("reminder_script_skipped", Jsonb(json_safe({
                "reminder_id": reminder["id"], "problem": problem, "next_run_at": next_run_at,
            }))),
        )
        LOGGER.warning("reminder %s scheduled script skipped: %s", reminder["id"], problem)
        return reminder, problem, consequence

    def create_job_for_reminder(self, cur, reminder: dict[str, Any]) -> dict[str, Any]:
        if reminder.get("object_id"):
            cur.execute(
                """SELECT w.id FROM workstream_attachments a JOIN workstreams w ON w.id = a.workstream_id
                   JOIN workstream_objects o ON o.id = a.object_id
                   WHERE a.object_id = %s AND w.status = 'active' AND NOT o.deleting
                   ORDER BY (w.id = o.origin_workstream_id) DESC, w.id LIMIT 1""",
                (reminder["object_id"],),
            )
            parent = cur.fetchone()
            if parent is None:
                raise RuntimeError("reminder has no active workstream")
            cur.execute("SELECT set_config('assistant.parent_workstream_id', %s, true)", (str(parent["id"]),))
            cur.execute("SELECT set_config('assistant.thread_role', 'system', true)")
        policy = (reminder.get("metadata") or {}).get("delivery") or {}
        binding = (reminder.get("metadata") or {}).get("script")
        task_text = reminder["task"]
        job_metadata: dict[str, Any] = {}
        if binding:
            task_text += "\n\n" + scheduled_scripts.instructions(binding)
            job_metadata["scheduled_script"] = {**binding, "reminder_id": reminder["id"]}
        if policy.get("primary"):
            database = TransactionDatabase(cur.connection)
            identity = "reminder:%s:%s" % (reminder["id"], reminder["run_at"].isoformat())
            system_thread = "reminder:%s" % reminder["id"]
            RequestService(database).receive_request(IncomingRequest(
                "reminder", identity, "reminder:%s" % reminder["id"],
                "reminder:%s" % reminder["id"], system_thread, task_text,
                reply_route=policy["primary"], metadata={"reminder_id": reminder["id"]}),
                summary="Reminder: %s" % reminder["title"])
            database.execute("UPDATE jobs SET priority = %s, metadata = metadata || %s WHERE thread_id = %s AND status IN ('queued', 'running', 'waiting', 'needs_review')",
                             (int(reminder.get("priority") or 0), Jsonb({**job_metadata, "auto_reply": True,
                              "reply_fallback": policy.get("fallback") or {},
                             "notification_only": bool((reminder.get("metadata") or {}).get("notification_only"))}), system_thread))
            if (reminder.get("metadata") or {}).get("notification_only"):
                database.execute("UPDATE jobs SET status = 'waiting', last_error = 'Scheduled notification awaiting delivery' WHERE thread_id = %s AND status = 'queued'", (system_thread,))
            return database.fetch_one("SELECT * FROM jobs WHERE thread_id = %s ORDER BY id DESC LIMIT 1", (system_thread,))
        now = datetime.now(timezone.utc)
        message_id = "<reminder-%s-%s@%s>" % (
            reminder["id"],
            int(now.timestamp() * 1000000),
            message_id_domain(self.config),
        )
        subject = "Reminder: %s" % reminder["title"]
        system_thread = "reminder:%s" % reminder["id"]
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
        body_lines.extend(["", "Task:", task_text])
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
            (message_id, system_thread, "reminder@local", [agent_email(self.config)], subject, body, now),
        )
        email_row = cur.fetchone()
        cur.execute(
            """
            INSERT INTO jobs(thread_id, task_summary, priority, run_at, metadata)
            VALUES (%s, %s, %s, now(), %s)
            RETURNING *
            """,
            (
                system_thread,
                subject,
                int(reminder.get("priority") or 0),
                Jsonb(json_safe(job_metadata)),
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
