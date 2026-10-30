import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


LOGGER = logging.getLogger("assistant.database")
_active_connection: ContextVar[Optional[tuple[str, psycopg.Connection]]] = ContextVar(
    "assistant_active_database_connection", default=None
)

SCHEMA_BASELINE = "000_current_baseline"


class MigrationBlockedError(RuntimeError):
    pass

REQUEUEABLE_JOB_STATUSES = frozenset({"failed", "cancelled", "waiting", "needs_review", "completed"})

UPDATED_AT_SQL = """
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger AS $$
BEGIN
    IF to_jsonb(NEW) ? 'embedding'
         AND NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at
         AND (to_jsonb(NEW) - ARRAY['embedding', 'embedding_model', 'embedding_dimensions', 'embedding_updated_at', 'metadata'])
                 = (to_jsonb(OLD) - ARRAY['embedding', 'embedding_model', 'embedding_dimensions', 'embedding_updated_at', 'metadata'])
         AND (COALESCE(to_jsonb(NEW)->'metadata', '{}'::jsonb) - 'embedding_pending')
                 = (COALESCE(to_jsonb(OLD)->'metadata', '{}'::jsonb) - 'embedding_pending') THEN
        RETURN NEW;
    END IF;
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def json_safe(value: Any) -> Any:
    return sanitize_json_value(json.loads(json.dumps(value, default=str)))


def sanitize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "[NUL]")
    if isinstance(value, list):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, dict):
        return {sanitize_json_value(key): sanitize_json_value(item) for key, item in value.items()}
    return value


class Database:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def for_job(self, job_id: int) -> "Database":
        database = Database(self.database_url)
        database.job_id = job_id
        return database

    def for_workstream(self, workstream_id: int) -> "Database":
        database = Database(self.database_url)
        database.workstream_id = workstream_id
        return database

    @contextmanager
    def object_version_transaction(self, kind: str, key: str, version: int) -> Iterator[None]:
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            if getattr(self, "job_id", None) is not None:
                conn.execute("SELECT set_config('assistant.job_id', %s, true)", (str(self.job_id),))
            if getattr(self, "workstream_id", None) is not None:
                conn.execute("SELECT set_config('assistant.workstream_id', %s, true)", (str(self.workstream_id),))
            object_row = conn.execute(
                "SELECT version FROM workstream_objects WHERE kind = %s AND object_key = %s FOR UPDATE",
                (kind, key),
            ).fetchone()
            if object_row is None or object_row["version"] != version:
                raise ValueError("object changed since it was read; refresh it before updating")
            conn.execute("SELECT set_config('assistant.expected_object_version', %s, true)", (str(version),))
            conn.execute("SELECT set_config('assistant.expected_object_kind', %s, true)", (kind,))
            conn.execute("SELECT set_config('assistant.expected_object_key', %s, true)", (key,))
            conn.execute("SELECT set_config('assistant.expected_object_checked', 'yes', true)")
            token = _active_connection.set((self.database_url, conn))
            try:
                yield
            finally:
                _active_connection.reset(token)

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        active = _active_connection.get()
        if active and active[0] == self.database_url:
            yield active[1]
            return
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            if getattr(self, "job_id", None) is not None:
                conn.execute("SELECT set_config('assistant.job_id', %s, true)", (str(self.job_id),))
            if getattr(self, "workstream_id", None) is not None:
                conn.execute("SELECT set_config('assistant.workstream_id', %s, true)", (str(self.workstream_id),))
            yield conn

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                if params:
                    cur.execute(sql, params)
                else:
                    cur.execute(sql)
                return cur.fetchone()

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                if params:
                    cur.execute(sql, params)
                else:
                    cur.execute(sql)
                return list(cur.fetchall())

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                if params:
                    cur.execute(sql, params)
                else:
                    cur.execute(sql)

    def ensure_feature_schema(self) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                self._require_schema_baseline(cur)

    @staticmethod
    def _require_schema_baseline(cur: psycopg.Cursor) -> None:
        message = (
            "Database schema baseline is unsupported; no automatic conversion was attempted. "
            "Use the manual transition package at "
            "https://github.com/rb81/assistant/releases/tag/layout-transition before starting services."
        )
        cur.execute("SELECT to_regclass('schema_migrations') AS relation")
        if cur.fetchone()["relation"] is None:
            raise MigrationBlockedError(message)
        cur.execute("SELECT filename FROM schema_migrations WHERE filename = %s", (SCHEMA_BASELINE,))
        if cur.fetchone() is None:
            raise MigrationBlockedError(message)

    def run_migrations(self, migrations_dir: Optional[Path] = None) -> list[str]:
        """Apply any pending SQL migration files in order.

        Migrations are tracked in a ``schema_migrations`` table keyed by
        filename. Pending files are applied in one serialized transaction so
        concurrent workers cannot apply or partially observe the same upgrade.
        The default migrations directory is the ``migrations/``
        sub-package bundled alongside this module.
        """
        if migrations_dir is None:
            migrations_dir = Path(__file__).parent / "migrations"
            require_baseline = True
        else:
            require_baseline = False
        if not migrations_dir.is_dir():
            return []

        ran: list[str] = []
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('assistant_schema_upgrade'))")
                if require_baseline:
                    self._require_schema_baseline(cur)
                cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations (filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
                cur.execute("SELECT filename FROM schema_migrations ORDER BY filename")
                applied = {row["filename"] for row in cur.fetchall()}
                for path in sorted(migrations_dir.glob("*.sql")):
                    if path.name in applied:
                        continue
                    LOGGER.info("applying migration: %s", path.name)
                    cur.execute(path.read_text(encoding="utf-8"))
                    cur.execute(
                        "INSERT INTO schema_migrations(filename) VALUES (%s)",
                        (path.name,),
                    )
                    ran.append(path.name)
        for filename in ran:
            LOGGER.info("migration applied: %s", filename)

        return ran

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.fetch_one("SELECT value FROM runtime_state WHERE key = %s", (key,))
        if row is None:
            return default
        return row["value"]

    def set_state(self, key: str, value: Any) -> None:
        self.execute(
            """
            INSERT INTO runtime_state(key, value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key)
            DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (key, Jsonb(json_safe(value))),
        )

    def log_event(
        self,
        job_id: int,
        event_type: str,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
        tool_name: Optional[str] = None,
        tool_action: Optional[str] = None,
        tokens_used: Optional[dict[str, Any]] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("task-log:%s" % job_id,))
                cur.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM task_logs WHERE job_id = %s",
                    (job_id,),
                )
                sequence = cur.fetchone()["next_sequence"]
                cur.execute(
                    """
                    INSERT INTO task_logs(
                      job_id,
                      sequence,
                      event_type,
                      tool_name,
                      tool_action,
                      input_data,
                      output_data,
                      tokens_used,
                      duration_ms
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        job_id,
                        sequence,
                        event_type,
                        tool_name,
                        tool_action,
                        Jsonb(json_safe(input_data)) if input_data is not None else None,
                        Jsonb(json_safe(output_data)) if output_data is not None else None,
                        Jsonb(json_safe(tokens_used)) if tokens_used is not None else None,
                        duration_ms,
                    ),
                )

    def claim_job(self, locked_by: str) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH picked AS (
                      SELECT id
                      FROM jobs
                      WHERE status = 'queued'
                        AND COALESCE(metadata->>'worker', '') <> 'deep-research-agent'
                        AND run_at <= now()
                        AND EXISTS (SELECT 1 FROM workstreams w WHERE w.id = jobs.workstream_id AND w.status = 'active')
                      ORDER BY priority DESC, created_at ASC
                      FOR UPDATE SKIP LOCKED
                      LIMIT 1
                    )
                    UPDATE jobs
                    SET status = 'running',
                        locked_at = now(),
                        locked_by = %s,
                        attempts = attempts + 1,
                        has_new_context = false,
                        updated_at = now()
                    WHERE id IN (SELECT id FROM picked)
                    RETURNING *
                    """,
                    (locked_by,),
                )
                return cur.fetchone()

    def update_job_status(
        self,
        job_id: int,
        status: str,
        last_error: Optional[str] = None,
        task_summary: Optional[str] = None,
    ) -> None:
        completed = status in ("completed", "failed", "cancelled")
        clear_lock = status != "running"
        self.execute(
            """
            UPDATE jobs
            SET status = %s,
                last_error = %s,
                task_summary = COALESCE(%s, task_summary),
                completed_at = CASE WHEN %s THEN now() ELSE completed_at END,
                locked_at = CASE WHEN %s THEN NULL ELSE locked_at END,
                locked_by = CASE WHEN %s THEN NULL ELSE locked_by END,
                updated_at = now()
            WHERE id = %s
            """,
            (status, last_error, task_summary, completed, clear_lock, clear_lock, job_id),
        )
        self.log_event(
            job_id,
            "status_change",
            output_data={"status": status, "last_error": last_error, "task_summary": task_summary},
        )

    def requeue_job(self, job_id: int, reason: str, statuses: Iterable[str] = REQUEUEABLE_JOB_STATUSES) -> bool:
        row = self.fetch_one(
            """
            UPDATE jobs
            SET status = 'queued', run_at = now(), locked_at = NULL, locked_by = NULL, last_error = NULL, updated_at = now()
            WHERE id = %s AND status = ANY(%s)
            RETURNING id
            """,
            (job_id, sorted(statuses)),
        )
        if row:
            self.log_event(job_id, "status_change", output_data={"status": "queued", "reason": reason})
        return row is not None

    def add_job_instruction(self, job_id: int, instruction: str, created_by: str) -> str:
        """Record guidance; jobs waiting on a question, or already finished, are requeued to act on it.

        Returns created/requeued. Guidance never approves held actions or changes limits.
        """
        self.execute(
            "INSERT INTO supervisor_instructions(job_id, instruction, created_by) VALUES (%s, %s, %s)",
            (job_id, instruction, created_by),
        )
        self.log_event(job_id, "supervisor_note", output_data={"instruction": instruction, "created_by": created_by})
        resumed = self.fetch_one(
            """UPDATE jobs SET status = 'queued', run_at = now(), locked_at = NULL, locked_by = NULL,
               last_error = NULL, has_new_context = true, updated_at = now(),
               metadata = metadata - 'waiting_for_admin' - 'waiting_for_input'
               WHERE id = %s AND status = 'waiting'
               AND (metadata->>'waiting_for_admin' = 'true' OR metadata->>'waiting_for_input' = 'true')
               RETURNING id""",
            (job_id,),
        )
        if resumed:
            self.log_event(job_id, "status_change", output_data={"status": "queued", "reason": "admin guidance received"})
            return "requeued"
        if self.requeue_job(job_id, "requeued by admin instruction", ("completed", "failed", "cancelled")):
            return "requeued"
        self.execute("UPDATE jobs SET has_new_context = true WHERE id = %s AND status IN ('queued', 'running')", (job_id,))
        return "created"

    def create_manual_job(
        self,
        subject: str,
        body: str,
        from_address: str = "dashboard@local",
        agent_address: str = "agent@local",
        message_domain: str = "assistant.local",
        parent_workstream_id: Optional[int] = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        message_id = "<manual-%s@%s>" % (int(now.timestamp() * 1000000), message_domain)
        thread_id = message_id
        with self.connect() as conn:
            with conn.cursor() as cur:
                if parent_workstream_id is not None:
                    cur.execute("SELECT set_config('assistant.parent_workstream_id', %s, true)", (str(parent_workstream_id),))
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
                    (message_id, thread_id, from_address, [agent_address], subject, body, now),
                )
                email_row = cur.fetchone()
                cur.execute(
                    """
                    INSERT INTO jobs(thread_id, task_summary, trigger_email_id)
                    VALUES (%s, %s, %s)
                    RETURNING *
                    """,
                    (thread_id, subject, email_row["id"]),
                )
                job_row = cur.fetchone()
                cur.execute(
                    "INSERT INTO manual_events(event_type, payload) VALUES (%s, %s)",
                    ("manual_job_created", Jsonb(json_safe({"job_id": job_row["id"], "email_id": email_row["id"]}))),
                )
                return job_row

    @staticmethod
    def _thread_message_cursor(before: Any) -> Optional[dict[str, Any]]:
        if before is None:
            return None
        cursor = before
        if isinstance(before, str):
            try:
                cursor = json.loads(before)
            except json.JSONDecodeError:
                cursor = None
        if isinstance(cursor, dict):
            try:
                return {
                    "timestamp": cursor["timestamp"],
                    "id": int(cursor["id"]),
                    "thread_item_id": str(cursor["thread_item_id"]),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("invalid workstream message cursor") from exc
        return {"timestamp": before, "id": None, "thread_item_id": None}

    @staticmethod
    def _thread_cursor_filter(
        cursor: Optional[dict[str, Any]], timestamp_sql: str, id_sql: str, item_prefix: str,
    ) -> tuple[str, tuple[Any, ...]]:
        if cursor is None:
            return "", ()
        if cursor["id"] is None:
            return f"{timestamp_sql} <= %s", (cursor["timestamp"],)
        clause = (
            f"({timestamp_sql}, {id_sql}, %s || {id_sql}::text) "
            "< (%s::timestamptz, %s::bigint, %s::text)"
        )
        return clause, (item_prefix, cursor["timestamp"], cursor["id"], cursor["thread_item_id"])

    def latest_thread_emails(self, thread_id: str, limit: int = 10, through: Any = None) -> list[dict[str, Any]]:
        cursor = self._thread_message_cursor(through)
        cursor_filter, cursor_params = self._thread_cursor_filter(
            cursor, "COALESCE(received_at, created_at)", "id", "email:",
        )
        query = """
            SELECT *
            FROM emails
            WHERE thread_id = %s
        """
        params: tuple[Any, ...] = (thread_id,)
        if cursor_filter:
            query += " AND " + cursor_filter
            params += cursor_params
        query += """
            ORDER BY COALESCE(received_at, created_at) DESC, id DESC
            LIMIT %s
        """
        rows = self.fetch_all(query, (*params, limit))
        return list(reversed(rows))

    def latest_thread_messages(self, thread_id: str, limit: int = 10, through: Any = None) -> list[dict[str, Any]]:
        clean_limit = min(max(int(limit or 10), 1), 100)
        cursor = self._thread_message_cursor(through)
        emails = [self.thread_email_context_item(row) for row in self.latest_thread_emails(
            thread_id, limit=clean_limit, through=through,
        )]
        outbound_filter, outbound_cursor_params = self._thread_cursor_filter(
            cursor, "COALESCE(o.sent_at, o.created_at)", "o.id", "outbound:",
        )
        outbound_query = """
            SELECT o.*, j.thread_id
            FROM outbound_email_logs o
            JOIN jobs j ON j.id = o.job_id
            WHERE j.thread_id = %s AND o.status = 'sent'
        """
        outbound_params: tuple[Any, ...] = (thread_id,)
        if outbound_filter:
            outbound_query += " AND " + outbound_filter
            outbound_params += outbound_cursor_params
        outbound_query += """
            ORDER BY COALESCE(o.sent_at, o.created_at) DESC, o.id DESC
            LIMIT %s
        """
        outbound_rows = self.fetch_all(outbound_query, (*outbound_params, clean_limit))
        messages = emails + [self.outbound_email_context_item(row) for row in reversed(outbound_rows)]

        request_filter, request_cursor_params = self._thread_cursor_filter(
            cursor, "COALESCE(r.received_at, r.created_at)", "r.id", "request:",
        )
        request_query = """
            SELECT r.* FROM request_messages r
            WHERE r.thread_id = %s AND r.channel NOT IN ('email', 'local')
        """
        request_params: tuple[Any, ...] = (thread_id,)
        if request_filter:
            request_query += " AND " + request_filter
            request_params += request_cursor_params
        request_query += """
            ORDER BY COALESCE(r.received_at, r.created_at) DESC, r.id DESC
            LIMIT %s
        """
        requests = self.fetch_all(request_query, (*request_params, clean_limit))
        for request in requests:
            if request.get("channel"):
                messages.append({**request, "context_type": "request", "direction": "inbound",
                                 "thread_item_id": "request:%s" % request["id"]})

        delivery_filter, delivery_cursor_params = self._thread_cursor_filter(
            cursor, "COALESCE(d.sent_at, d.created_at)", "d.id", "telegram-delivery:",
        )
        delivery_query = """
            SELECT d.* FROM channel_deliveries d JOIN jobs j ON j.id = d.job_id
            WHERE j.thread_id = %s AND d.channel = 'telegram' AND d.status = 'sent'
        """
        delivery_params: tuple[Any, ...] = (thread_id,)
        if delivery_filter:
            delivery_query += " AND " + delivery_filter
            delivery_params += delivery_cursor_params
        delivery_query += """
            ORDER BY COALESCE(d.sent_at, d.created_at) DESC, d.id DESC
            LIMIT %s
        """
        deliveries = self.fetch_all(delivery_query, (*delivery_params, clean_limit))
        for delivery in deliveries:
            if delivery.get("channel") == "telegram":
                messages.append({**delivery, "context_type": "outbound_telegram", "direction": "outbound",
                                 "thread_item_id": "telegram-delivery:%s" % delivery["id"],
                                 "received_at": delivery.get("sent_at") or delivery["created_at"]})
        messages.sort(key=self.thread_message_sort_key)
        return messages[-clean_limit:]

    def thread_email_context_item(self, row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["context_type"] = "email"
        result["direction"] = "inbound"
        result["thread_item_id"] = "email:%s" % row.get("id")
        return result

    def outbound_email_context_item(self, row: dict[str, Any]) -> dict[str, Any]:
        sent_at = row.get("sent_at") or row.get("created_at")
        return {
            "id": row.get("id"),
            "outbound_log_id": row.get("id"),
            "context_type": "outbound_email",
            "direction": "outbound",
            "thread_item_id": "outbound:%s" % row.get("id"),
            "message_id": row.get("provider_message_id") or "",
            "in_reply_to": row.get("in_reply_to") or "",
            "thread_id": row.get("thread_id"),
            "from_address": "assistant@local",
            "to_addresses": row.get("to_addresses") or [],
            "cc_addresses": row.get("cc_addresses") or [],
            "subject": row.get("subject") or "",
            "body_text": row.get("body_text") or "",
            "body_html": "",
            "attachments": row.get("attachments") or [],
            "received_at": sent_at,
            "sent_at": sent_at,
            "created_at": row.get("created_at"),
            "is_actionable": False,
            "status": row.get("status"),
        }

    def thread_message_sort_key(self, row: dict[str, Any]) -> tuple[Any, int, str]:
        return (
            row.get("received_at") or row.get("sent_at") or row.get("created_at"),
            int(row.get("id") or 0),
            str(row.get("thread_item_id") or ""),
        )

    def processed_artifacts_for_thread(self, thread_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return self.fetch_all(
            """
            SELECT *
            FROM processed_artifacts
            WHERE thread_id = %s
            ORDER BY email_id ASC, id ASC
            LIMIT %s
            """,
            (thread_id, limit),
        )

    def processed_artifacts_for_email(self, email_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return self.fetch_all(
            """
            SELECT *
            FROM processed_artifacts
            WHERE email_id = %s
            ORDER BY id ASC
            LIMIT %s
            """,
            (email_id, limit),
        )

    def pending_supervisor_instructions(self, job_id: int) -> list[dict[str, Any]]:
        return self.fetch_all(
            """
            SELECT *
            FROM supervisor_instructions
            WHERE job_id = %s
              AND consumed_at IS NULL
            ORDER BY created_at ASC
            """,
            (job_id,),
        )

    def mark_instructions_consumed(self, job_id: int) -> None:
        self.execute(
            """
            UPDATE supervisor_instructions
            SET consumed_at = now()
            WHERE job_id = %s
              AND consumed_at IS NULL
            """,
            (job_id,),
        )

    def erase_job(self, job_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
                root_job = cur.fetchone()
                if root_job is None:
                    raise ValueError("job not found")
                if root_job.get("workstream_id") is not None:
                    raise ValueError("Use setup tools > Workstreams and data deletion; job erasure bypasses shared-object ownership")

                job_ids = self.associated_job_ids(cur, job_id)
                cur.execute("SELECT * FROM jobs WHERE id = ANY(%s::bigint[]) FOR UPDATE", (job_ids,))
                jobs = list(cur.fetchall())
                thread_ids = sorted({row["thread_id"] for row in jobs if row.get("thread_id")})

                project_ids = sorted({int(row["metadata"]["retired_project"]["id"]) for row in jobs
                                      if isinstance(row.get("metadata", {}).get("retired_project"), dict)
                                      and str(row["metadata"]["retired_project"].get("id", "")).isdigit()})
                project_task_ids = sorted({int(task["id"]) for row in jobs
                                           for task in row.get("metadata", {}).get("retired_project_tasks", [])
                                           if isinstance(task, dict) and str(task.get("id", "")).isdigit()})
                deep_research_run_ids = self.column_values(
                    cur,
                    "SELECT id FROM deep_research_runs WHERE original_job_id = ANY(%s::bigint[])",
                    (job_ids,),
                    "id",
                )
                reminder_ids = self.column_values(
                    cur,
                    """
                    SELECT id
                    FROM reminders
                    WHERE created_by_job_id = ANY(%s::bigint[])
                       OR job_id = ANY(%s::bigint[])
                    """,
                    (job_ids, job_ids),
                    "id",
                )
                memory_ids = self.column_values(
                    cur,
                    "SELECT id FROM agent_memories WHERE source_job_id = ANY(%s::bigint[])",
                    (job_ids,),
                    "id",
                )
                contact_ids = self.column_values(
                    cur,
                    """
                    SELECT DISTINCT (output_data->'contact'->>'id')::bigint AS id
                    FROM task_logs
                    WHERE job_id = ANY(%s::bigint[])
                      AND event_type = 'tool_result'
                      AND tool_name = 'contact_create'
                      AND output_data->'contact'->>'id' ~ '^[0-9]+$'
                    """,
                    (job_ids,),
                    "id",
                )
                created_calendar_ids = self.column_values(
                    cur,
                    "SELECT assistant_id FROM calendar_managed_events WHERE created_by_job_id = ANY(%s::bigint[])",
                    (job_ids,),
                    "assistant_id",
                )

                shared_thread_ids: set[str] = set()
                if thread_ids:
                    cur.execute(
                        """
                        SELECT DISTINCT thread_id
                        FROM jobs
                        WHERE thread_id = ANY(%s::text[])
                          AND NOT (id = ANY(%s::bigint[]))
                        """,
                        (thread_ids, job_ids),
                    )
                    shared_thread_ids = {row["thread_id"] for row in cur.fetchall()}
                erasable_thread_ids = [thread_id for thread_id in thread_ids if thread_id not in shared_thread_ids]

                email_ids: list[int] = []
                if erasable_thread_ids:
                    email_ids.extend(
                        self.column_values(
                            cur,
                            "SELECT id FROM emails WHERE thread_id = ANY(%s::text[])",
                            (erasable_thread_ids,),
                            "id",
                        )
                    )
                email_ids = sorted(set(email_ids))

                counts: dict[str, int] = {
                    "jobs": 0,
                    "emails": 0,
                    "thread_summaries": 0,
                    "processed_artifacts": 0,
                    "task_logs": 0,
                    "agent_checkpoints": 0,
                    "supervisor_instructions": 0,
                    "outbound_email_logs": 0,
                    "channel_deliveries": 0,
                    "request_messages": 0,
                    "agent_memories": 0,
                    "memory_events": 0,
                    "contacts": 0,
                    "reminders": 0,
                    "calendar_managed_events": 0,
                    "calendar_event_audit": 0,
                    "zoom_managed_meetings": 0,
                    "deep_research_runs": 0,
                    "deep_research_events": 0,
                    "manual_events": 0,
                }

                counts["manual_events"] += self.delete_manual_events_for_erasure(
                    cur,
                    job_ids=job_ids,
                    email_ids=email_ids,
                    project_ids=project_ids,
                    project_task_ids=project_task_ids,
                    deep_research_run_ids=deep_research_run_ids,
                    reminder_ids=reminder_ids,
                )
                counts["memory_events"] += self.delete_count(
                    cur,
                    """
                    DELETE FROM memory_events
                    WHERE job_id = ANY(%s::bigint[])
                       OR memory_id = ANY(%s::bigint[])
                    """,
                    (job_ids, memory_ids),
                )
                counts["agent_memories"] += self.delete_count(
                    cur,
                    "DELETE FROM agent_memories WHERE id = ANY(%s::bigint[])",
                    (memory_ids,),
                )
                counts["contacts"] += self.delete_count(
                    cur,
                    "DELETE FROM contacts WHERE id = ANY(%s::bigint[])",
                    (contact_ids,),
                )
                counts["reminders"] += self.delete_count(
                    cur,
                    "DELETE FROM reminders WHERE id = ANY(%s::bigint[])",
                    (reminder_ids,),
                )
                counts["calendar_event_audit"] += self.delete_count(
                    cur,
                    """
                    DELETE FROM calendar_event_audit
                    WHERE job_id = ANY(%s::bigint[])
                       OR assistant_id = ANY(%s::text[])
                    """,
                    (job_ids, created_calendar_ids),
                )
                counts["calendar_managed_events"] += self.delete_count(
                    cur,
                    "DELETE FROM calendar_managed_events WHERE assistant_id = ANY(%s::text[])",
                    (created_calendar_ids,),
                )
                cur.execute(
                    """
                    UPDATE calendar_managed_events
                    SET updated_by_job_id = NULL,
                        updated_at = now()
                    WHERE updated_by_job_id = ANY(%s::bigint[])
                    """,
                    (job_ids,),
                )
                counts["outbound_email_logs"] += self.delete_count(
                    cur,
                    "DELETE FROM outbound_email_logs WHERE job_id = ANY(%s::bigint[])",
                    (job_ids,),
                )
                counts["channel_deliveries"] += self.delete_count(
                    cur, "DELETE FROM channel_deliveries WHERE job_id = ANY(%s::bigint[])", (job_ids,),
                )
                counts["deep_research_events"] += self.delete_count(
                    cur,
                    "DELETE FROM deep_research_events WHERE run_id = ANY(%s::bigint[])",
                    (deep_research_run_ids,),
                )
                counts["deep_research_runs"] += self.delete_count(
                    cur,
                    "DELETE FROM deep_research_runs WHERE id = ANY(%s::bigint[])",
                    (deep_research_run_ids,),
                )
                counts["zoom_managed_meetings"] += self.delete_count(
                    cur,
                    "DELETE FROM zoom_managed_meetings WHERE created_by_job_id = ANY(%s::bigint[])",
                    (job_ids,),
                )
                counts["task_logs"] += self.delete_count(
                    cur,
                    "DELETE FROM task_logs WHERE job_id = ANY(%s::bigint[])",
                    (job_ids,),
                )
                counts["agent_checkpoints"] += self.delete_count(
                    cur,
                    "DELETE FROM agent_checkpoints WHERE job_id = ANY(%s::bigint[])",
                    (job_ids,),
                )
                counts["supervisor_instructions"] += self.delete_count(
                    cur,
                    "DELETE FROM supervisor_instructions WHERE job_id = ANY(%s::bigint[])",
                    (job_ids,),
                )
                counts["jobs"] += self.delete_count(
                    cur,
                    "DELETE FROM jobs WHERE id = ANY(%s::bigint[])",
                    (job_ids,),
                )
                if erasable_thread_ids:
                    counts["thread_summaries"] += self.delete_count(
                        cur,
                        "DELETE FROM thread_summaries WHERE thread_id = ANY(%s::text[])",
                        (erasable_thread_ids,),
                    )
                counts["processed_artifacts"] += self.delete_count(
                    cur,
                    "DELETE FROM processed_artifacts WHERE email_id = ANY(%s::bigint[])",
                    (email_ids,),
                )
                if erasable_thread_ids:
                    counts["processed_artifacts"] += self.delete_count(
                        cur, "DELETE FROM processed_artifacts WHERE email_id IS NULL AND thread_id = ANY(%s::text[])",
                        (erasable_thread_ids,),
                    )
                    counts["request_messages"] += self.delete_count(
                        cur, "DELETE FROM request_messages WHERE thread_id = ANY(%s::text[])", (erasable_thread_ids,),
                    )
                counts["emails"] += self.delete_count(
                    cur,
                    "DELETE FROM emails WHERE id = ANY(%s::bigint[])",
                    (email_ids,),
                )

                return {
                    "root_job_id": job_id,
                    "job_ids": job_ids,
                    "thread_ids": erasable_thread_ids,
                    "shared_thread_ids": sorted(shared_thread_ids),
                    "deleted": {key: value for key, value in counts.items() if value},
                }

    def associated_job_ids(self, cur, job_id: int) -> list[int]:
        seen = {int(job_id)}
        frontier = [int(job_id)]
        while frontier:
            cur.execute(
                """
                SELECT child.id
                FROM jobs child
                WHERE child.metadata->>'parent_job_id' ~ '^[0-9]+$'
                  AND (child.metadata->>'parent_job_id')::bigint = ANY(%s::bigint[])

                UNION

                                SELECT child.id
                                FROM jobs parent
                                CROSS JOIN LATERAL jsonb_array_elements(
                                        CASE WHEN jsonb_typeof(parent.metadata->'retired_project_tasks') = 'array'
                                                 THEN parent.metadata->'retired_project_tasks' ELSE '[]'::jsonb END
                                ) task
                                JOIN jobs child ON child.id::text = task->>'job_id'
                                WHERE parent.id = ANY(%s::bigint[])

                UNION

                SELECT r.job_id AS id
                FROM reminders r
                WHERE r.created_by_job_id = ANY(%s::bigint[])
                  AND r.job_id IS NOT NULL
                """,
                (frontier, frontier, frontier),
            )
            discovered = {int(row["id"]) for row in cur.fetchall()}
            frontier = sorted(discovered - seen)
            seen.update(frontier)
        return sorted(seen)

    def column_values(self, cur, sql: str, params: tuple[Any, ...], column: str) -> list[Any]:
        cur.execute(sql, params)
        return [row[column] for row in cur.fetchall()]

    def delete_count(self, cur, sql: str, params: tuple[Any, ...]) -> int:
        cur.execute(sql, params)
        return max(int(cur.rowcount or 0), 0)

    def delete_manual_events_for_erasure(
        self,
        cur,
        *,
        job_ids: list[int],
        email_ids: list[int],
        project_ids: list[int],
        project_task_ids: list[int],
        deep_research_run_ids: list[int],
        reminder_ids: list[int],
    ) -> int:
        job_id_texts = [str(value) for value in job_ids]
        email_id_texts = [str(value) for value in email_ids]
        project_id_texts = [str(value) for value in project_ids]
        project_task_id_texts = [str(value) for value in project_task_ids]
        run_id_texts = [str(value) for value in deep_research_run_ids]
        reminder_id_texts = [str(value) for value in reminder_ids]
        cur.execute(
            """
            DELETE FROM manual_events
            WHERE payload->>'job_id' = ANY(%s::text[])
               OR payload->>'original_job_id' = ANY(%s::text[])
               OR payload->>'parent_job_id' = ANY(%s::text[])
               OR payload->>'completed_job_id' = ANY(%s::text[])
               OR payload->>'email_id' = ANY(%s::text[])
               OR payload->>'project_id' = ANY(%s::text[])
               OR payload->>'project_task_id' = ANY(%s::text[])
               OR payload->>'run_id' = ANY(%s::text[])
               OR payload->>'deep_research_run_id' = ANY(%s::text[])
               OR payload->>'reminder_id' = ANY(%s::text[])
            """,
            (
                job_id_texts,
                job_id_texts,
                job_id_texts,
                job_id_texts,
                email_id_texts,
                project_id_texts,
                project_task_id_texts,
                run_id_texts,
                run_id_texts,
                reminder_id_texts,
            ),
        )
        return max(int(cur.rowcount or 0), 0)
