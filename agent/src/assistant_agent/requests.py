from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from psycopg.types.json import Jsonb

from .database import Database, json_safe, sanitize_json_value


@dataclass
class IncomingRequest:
    channel: str
    external_id: str
    conversation_id: str
    sender_id: str
    thread_id: str
    body: str
    reply_route: dict[str, Any] = field(default_factory=dict)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    email_id: Optional[int] = None
    received_at: Optional[datetime] = None
    parent_workstream_id: Optional[int] = None


class TransactionDatabase(Database):
    def __init__(self, connection: Any):
        self.connection = connection

    @contextmanager
    def connect(self):
        yield self.connection


class RequestService:
    def __init__(self, db: Database):
        self.db = db

    def receive_request(self, request: IncomingRequest, *, queue: bool = True,
                        summary: Optional[str] = None, **queue_options: Any) -> dict[str, Any]:
        if not all((request.channel, request.external_id, request.sender_id, request.thread_id)):
            raise ValueError("request identity and task thread are required")
        with self.db.connect() as connection:
            database = TransactionDatabase(connection)
            if request.channel in {"telegram", "email"}:
                database.execute(
                    "SELECT set_config('assistant.workstream_channel', %s, true)",
                    (request.channel,),
                )
            if request.parent_workstream_id is not None:
                database.execute("SELECT set_config('assistant.parent_workstream_id', %s, true)",
                                 (str(request.parent_workstream_id),))
            if request.metadata.get("temporary_workstream") == "telegram":
                database.execute("SELECT set_config('assistant.telegram_new_workstream', 'yes', true)")
                database.execute(
                    "SELECT set_config('assistant.telegram_workstream_title', %s, true)",
                    (str(request.metadata.get("workstream_title") or "New workstream"),),
                )
            database.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("request:" + request.thread_id,))
            row = database.fetch_one(
                """INSERT INTO request_messages(channel, external_id, conversation_id, sender_id, thread_id,
                   email_id, body_text, reply_route, attachments, metadata, status, received_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (channel, external_id) DO UPDATE SET external_id = EXCLUDED.external_id
                   RETURNING *""",
                (request.channel, request.external_id, request.conversation_id, request.sender_id,
                 request.thread_id, request.email_id, sanitize_json_value(request.body), Jsonb(json_safe(request.reply_route)),
                 Jsonb(json_safe(request.attachments)), Jsonb(json_safe(request.metadata)),
                 "ready" if queue else "pending", request.received_at or datetime.now(timezone.utc)),
            )
            if queue and not (row.get("metadata") or {}).get("queued"):
                RequestService(database).queue_or_update_job(
                    row["thread_id"], sanitize_json_value(summary) if summary else row["body_text"][:160] or "Attachment request",
                    request.email_id, channel=request.channel, body=row["body_text"], **queue_options,
                )
                database.execute(
                    """UPDATE jobs SET trigger_request_id = COALESCE(trigger_request_id, %s),
                       metadata = metadata || %s
                       WHERE thread_id = %s AND status IN ('queued', 'running', 'waiting', 'needs_review')""",
                    (row["id"], Jsonb({"source_channel": request.channel, "reply_route": request.reply_route,
                                      "reply_request_id": row["id"],
                                      "auto_reply": bool(request.reply_route), "waiting_for_input": False,
                                      "pending_final_response": None}), row["thread_id"]),
                )
                database.execute("UPDATE request_messages SET status = 'ready' WHERE id = %s", (row["id"],))
                database.execute("UPDATE request_messages SET metadata = metadata || %s WHERE id = %s",
                                 (Jsonb({"queued": True}), row["id"]))
            return row

    def queue_or_update_job(
        self,
        thread_id: str,
        summary: str,
        trigger_email_id: Optional[int] = None,
        *,
        channel: str = "email",
        body: str = "",
        admin_authenticated: bool = False,
        resume_research: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
        review_override: Optional[Callable[..., None]] = None,
    ) -> None:
        resumed_research = (
            resume_research(thread_id) if resume_research
            else self.resume_waiting_research(thread_id)
        )
        if resumed_research:
            self.db.execute(
                """UPDATE jobs SET trigger_email_id = COALESCE(trigger_email_id, %s),
                   has_new_context = true, updated_at = now() WHERE id = %s""",
                (trigger_email_id, resumed_research["original_job_id"]),
            )
            self.db.log_event(
                resumed_research["original_job_id"], "status_change",
                output_data={"has_new_context": True,
                             "reason": "new %s arrived; resumed waiting deep research" % channel,
                             "deep_research_run_id": resumed_research["id"]},
            )
            return
        open_job = self.db.fetch_one(
            "SELECT id, status, metadata, last_error FROM jobs WHERE thread_id = %s AND status IN ('queued', 'running', 'waiting', 'needs_review')",
            (thread_id,),
        )
        if open_job:
            if (open_job.get("metadata") or {}).get("worker") == "deep-research-agent":
                self.db.execute("UPDATE jobs SET has_new_context = true WHERE id = %s", (open_job["id"],))
                return
            if open_job["status"] == "needs_review":
                if admin_authenticated and review_override:
                    review_override(open_job, trigger_email_id, body)
                return
            if open_job["status"] == "waiting":
                self.db.execute(
                    """
                    UPDATE jobs
                    SET status = 'queued',
                        trigger_email_id = COALESCE(trigger_email_id, %s),
                        run_at = now(),
                        has_new_context = true,
                        last_error = NULL,
                        locked_at = NULL,
                        locked_by = NULL,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (trigger_email_id, open_job["id"]),
                )
                self.db.log_event(open_job["id"], "status_change", output_data={
                    "status": "queued", "has_new_context": True, "reason": "new %s arrived" % channel,
                })
                return
            self.db.execute(
                """
                UPDATE jobs
                SET trigger_email_id = COALESCE(trigger_email_id, %s),
                    has_new_context = true,
                    updated_at = now()
                WHERE id = %s
                """,
                (trigger_email_id, open_job["id"]),
            )
            self.db.log_event(open_job["id"], "status_change", output_data={
                "has_new_context": True, "reason": "new %s arrived" % channel,
            })
            return
        self.db.execute(
            "INSERT INTO jobs(thread_id, task_summary, trigger_email_id) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (thread_id, summary, trigger_email_id),
        )

    def resume_waiting_research(self, thread_id: str) -> Optional[dict[str, Any]]:
        return self.db.fetch_one(
            """WITH candidates AS (
                 SELECT r.id, count(*) OVER () AS matches
                 FROM deep_research_runs r
                 LEFT JOIN jobs j ON j.id = r.execution_job_id
                 JOIN jobs source ON source.id = r.original_job_id
                 WHERE r.status = 'waiting_for_input'
                   AND source.status IN ('waiting', 'queued', 'running')
                   AND (j.id IS NULL OR j.status = 'waiting')
                   AND (r.original_thread_id = %s OR j.thread_id = %s)
               )
               UPDATE deep_research_runs SET status = 'queued', run_at = now(),
                 waiting_since = NULL, last_error = NULL, locked_at = NULL, locked_by = NULL
               WHERE id = (SELECT id FROM candidates WHERE matches = 1)
                 AND status = 'waiting_for_input'
               RETURNING id, original_job_id, execution_job_id""",
            (thread_id, thread_id),
        )