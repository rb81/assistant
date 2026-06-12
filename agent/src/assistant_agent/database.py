import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


LOGGER = logging.getLogger("assistant.database")


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

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
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
        self.execute(FEATURE_SCHEMA_SQL)

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
                        AND run_at <= now()
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

    def create_manual_job(
        self,
        subject: str,
        body: str,
        from_address: str = "dashboard@local",
        agent_address: str = "agent@local",
        message_domain: str = "assistant.local",
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        message_id = "<manual-%s@%s>" % (int(now.timestamp() * 1000000), message_domain)
        thread_id = message_id
        with self.connect() as conn:
            with conn.cursor() as cur:
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

    def latest_thread_emails(self, thread_id: str, limit: int = 10, through: Any = None) -> list[dict[str, Any]]:
        if through is None:
            rows = self.fetch_all(
                """
                SELECT *
                FROM emails
                WHERE thread_id = %s
                ORDER BY received_at DESC, id DESC
                LIMIT %s
                """,
                (thread_id, limit),
            )
        else:
            rows = self.fetch_all(
                """
                SELECT *
                FROM emails
                WHERE thread_id = %s
                  AND created_at <= %s
                ORDER BY received_at DESC, id DESC
                LIMIT %s
                """,
                (thread_id, through, limit),
            )
        return list(reversed(rows))

    def latest_thread_messages(self, thread_id: str, limit: int = 10, through: Any = None) -> list[dict[str, Any]]:
        clean_limit = min(max(int(limit or 10), 1), 100)
        emails = [self.thread_email_context_item(row) for row in self.latest_thread_emails(thread_id, limit=clean_limit, through=through)]
        if through is None:
            outbound_rows = self.fetch_all(
                """
                SELECT o.*, j.thread_id
                FROM outbound_email_logs o
                JOIN jobs j ON j.id = o.job_id
                WHERE j.thread_id = %s
                  AND o.status = 'sent'
                ORDER BY COALESCE(o.sent_at, o.created_at) DESC, o.id DESC
                LIMIT %s
                """,
                (thread_id, clean_limit),
            )
        else:
            outbound_rows = self.fetch_all(
                """
                SELECT o.*, j.thread_id
                FROM outbound_email_logs o
                JOIN jobs j ON j.id = o.job_id
                WHERE j.thread_id = %s
                  AND o.status = 'sent'
                  AND COALESCE(o.sent_at, o.created_at) <= %s
                ORDER BY COALESCE(o.sent_at, o.created_at) DESC, o.id DESC
                LIMIT %s
                """,
                (thread_id, through, clean_limit),
            )
        messages = emails + [self.outbound_email_context_item(row) for row in reversed(outbound_rows)]
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

    def thread_message_sort_key(self, row: dict[str, Any]) -> tuple[Any, int]:
        return (row.get("received_at") or row.get("sent_at") or row.get("created_at"), int(row.get("id") or 0))

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

                job_ids = self.associated_job_ids(cur, job_id)
                cur.execute("SELECT * FROM jobs WHERE id = ANY(%s::bigint[]) FOR UPDATE", (job_ids,))
                jobs = list(cur.fetchall())
                thread_ids = sorted({row["thread_id"] for row in jobs if row.get("thread_id")})

                project_ids = self.column_values(
                    cur,
                    "SELECT id FROM projects WHERE original_job_id = ANY(%s::bigint[])",
                    (job_ids,),
                    "id",
                )
                project_task_ids = self.column_values(
                    cur,
                    "SELECT id FROM project_tasks WHERE project_id = ANY(%s::bigint[])",
                    (project_ids,),
                    "id",
                )
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
                    "agent_memories": 0,
                    "memory_events": 0,
                    "contacts": 0,
                    "reminders": 0,
                    "calendar_managed_events": 0,
                    "calendar_event_audit": 0,
                    "projects": 0,
                    "project_tasks": 0,
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
                counts["project_tasks"] += self.delete_count(
                    cur,
                    "DELETE FROM project_tasks WHERE id = ANY(%s::bigint[])",
                    (project_task_ids,),
                )
                counts["projects"] += self.delete_count(
                    cur,
                    "DELETE FROM projects WHERE id = ANY(%s::bigint[])",
                    (project_ids,),
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

                SELECT pt.job_id AS id
                FROM project_tasks pt
                JOIN projects p ON p.id = pt.project_id
                WHERE p.original_job_id = ANY(%s::bigint[])
                  AND pt.job_id IS NOT NULL

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

FEATURE_SCHEMA_SQL = """
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS trigger_email_id bigint;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'jobs_trigger_email_id_fkey'
      AND conrelid = 'jobs'::regclass
  ) THEN
    ALTER TABLE jobs
    ADD CONSTRAINT jobs_trigger_email_id_fkey
    FOREIGN KEY (trigger_email_id) REFERENCES emails(id) ON DELETE SET NULL;
  END IF;
END $$;
UPDATE jobs j
SET trigger_email_id = (
  SELECT e.id
  FROM emails e
  WHERE e.thread_id = j.thread_id
    AND e.created_at <= j.created_at
  ORDER BY e.created_at DESC, e.id DESC
  LIMIT 1
)
WHERE j.trigger_email_id IS NULL
  AND EXISTS (
    SELECT 1
    FROM emails e
    WHERE e.thread_id = j.thread_id
      AND e.created_at <= j.created_at
  );
CREATE INDEX IF NOT EXISTS jobs_trigger_email_idx ON jobs(trigger_email_id);
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_status_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_status_check CHECK (status IN ('queued', 'running', 'waiting', 'completed', 'failed', 'needs_review', 'cancelled'));
DROP INDEX IF EXISTS jobs_one_open_per_thread_idx;
CREATE UNIQUE INDEX jobs_one_open_per_thread_idx
  ON jobs(thread_id)
  WHERE status IN ('queued', 'running', 'waiting', 'needs_review');
UPDATE jobs
SET status = 'waiting', updated_at = now()
WHERE status = 'needs_review'
  AND (
    last_error LIKE 'Project #% is running; waiting for project completion.%'
    OR last_error LIKE 'Deep research run #% is running; waiting for research completion.%'
    OR last_error = 'Async request is running.'
  );

CREATE TABLE IF NOT EXISTS processed_artifacts (
  id bigserial PRIMARY KEY,
  email_id bigint NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
  thread_id text NOT NULL,
  source_type text NOT NULL,
  source_label text NOT NULL,
  source_uri text,
  original_filename text,
  content_type text,
  raw_path text,
  raw_sha256 text,
  raw_size_bytes bigint,
  scan_status text NOT NULL DEFAULT 'pending',
  scan_engine text,
  scan_result text,
  conversion_status text NOT NULL DEFAULT 'pending',
  markdown_path text,
  markdown_sha256 text,
  markdown_size_bytes bigint,
  error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE processed_artifacts DROP CONSTRAINT IF EXISTS processed_artifacts_source_type_check;
ALTER TABLE processed_artifacts ADD CONSTRAINT processed_artifacts_source_type_check CHECK (source_type IN ('attachment', 'youtube_url'));
ALTER TABLE processed_artifacts DROP CONSTRAINT IF EXISTS processed_artifacts_scan_status_check;
ALTER TABLE processed_artifacts ADD CONSTRAINT processed_artifacts_scan_status_check CHECK (scan_status IN ('pending', 'clean', 'infected', 'error', 'skipped', 'not_applicable'));
ALTER TABLE processed_artifacts DROP CONSTRAINT IF EXISTS processed_artifacts_conversion_status_check;
ALTER TABLE processed_artifacts ADD CONSTRAINT processed_artifacts_conversion_status_check CHECK (conversion_status IN ('pending', 'ready', 'unsupported', 'failed', 'skipped'));

CREATE INDEX IF NOT EXISTS processed_artifacts_email_idx ON processed_artifacts(email_id, id);
CREATE INDEX IF NOT EXISTS processed_artifacts_thread_idx ON processed_artifacts(thread_id, id);
CREATE INDEX IF NOT EXISTS processed_artifacts_status_idx ON processed_artifacts(conversion_status, scan_status);

CREATE TABLE IF NOT EXISTS agent_memories (
  id bigserial PRIMARY KEY,
  content text NOT NULL,
  tags text[] NOT NULL DEFAULT ARRAY[]::text[],
  scope text NOT NULL DEFAULT 'global',
  kind text NOT NULL DEFAULT 'fact',
  importance int NOT NULL DEFAULT 3,
  confidence double precision NOT NULL DEFAULT 0.7,
  expires_at timestamptz,
  pinned boolean NOT NULL DEFAULT false,
  embedding double precision[],
  embedding_model text,
  embedding_dimensions int,
  embedding_updated_at timestamptz,
  source_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_accessed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT 'global';
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'fact';
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS importance int NOT NULL DEFAULT 3;
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS confidence double precision NOT NULL DEFAULT 0.7;
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS expires_at timestamptz;
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT false;
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS embedding double precision[];
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS embedding_model text;
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS embedding_dimensions int;
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;

CREATE INDEX IF NOT EXISTS agent_memories_created_idx ON agent_memories(created_at DESC);
CREATE INDEX IF NOT EXISTS agent_memories_tags_idx ON agent_memories USING GIN(tags);
CREATE INDEX IF NOT EXISTS agent_memories_scope_kind_idx ON agent_memories(scope, kind);
CREATE INDEX IF NOT EXISTS agent_memories_pinned_idx ON agent_memories(pinned, importance DESC);

CREATE TABLE IF NOT EXISTS memory_events (
  id bigserial PRIMARY KEY,
  memory_id bigint,
  job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  actor text NOT NULL DEFAULT 'system',
  event_type text NOT NULL,
  input_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  output_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE memory_events DROP CONSTRAINT IF EXISTS memory_events_memory_id_fkey;

CREATE INDEX IF NOT EXISTS memory_events_memory_created_idx ON memory_events(memory_id, created_at DESC);
CREATE INDEX IF NOT EXISTS memory_events_job_created_idx ON memory_events(job_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_notes (
  id bigserial PRIMARY KEY,
  title text NOT NULL DEFAULT 'Untitled note',
  content text NOT NULL,
  tags text[] NOT NULL DEFAULT ARRAY[]::text[],
  embedding double precision[],
  embedding_model text,
  embedding_dimensions int,
  embedding_updated_at timestamptz,
  source_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_accessed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE agent_notes ADD COLUMN IF NOT EXISTS title text NOT NULL DEFAULT 'Untitled note';
ALTER TABLE agent_notes ADD COLUMN IF NOT EXISTS tags text[] NOT NULL DEFAULT ARRAY[]::text[];
ALTER TABLE agent_notes ADD COLUMN IF NOT EXISTS embedding double precision[];
ALTER TABLE agent_notes ADD COLUMN IF NOT EXISTS embedding_model text;
ALTER TABLE agent_notes ADD COLUMN IF NOT EXISTS embedding_dimensions int;
ALTER TABLE agent_notes ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;
ALTER TABLE agent_notes ADD COLUMN IF NOT EXISTS source_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL;
ALTER TABLE agent_notes ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE agent_notes ADD COLUMN IF NOT EXISTS last_accessed_at timestamptz;

CREATE INDEX IF NOT EXISTS agent_notes_created_idx ON agent_notes(created_at DESC);
CREATE INDEX IF NOT EXISTS agent_notes_tags_idx ON agent_notes USING GIN(tags);
CREATE INDEX IF NOT EXISTS agent_notes_updated_idx ON agent_notes(updated_at DESC);

CREATE TABLE IF NOT EXISTS note_events (
  id bigserial PRIMARY KEY,
  note_id bigint,
  job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  actor text NOT NULL DEFAULT 'system',
  event_type text NOT NULL,
  input_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  output_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE note_events DROP CONSTRAINT IF EXISTS note_events_note_id_fkey;

CREATE INDEX IF NOT EXISTS note_events_note_created_idx ON note_events(note_id, created_at DESC);
CREATE INDEX IF NOT EXISTS note_events_job_created_idx ON note_events(job_id, created_at DESC);

CREATE TABLE IF NOT EXISTS contacts (
  id bigserial PRIMARY KEY,
  first_name text NOT NULL DEFAULT '',
  last_name text NOT NULL DEFAULT '',
  email_address text NOT NULL DEFAULT '',
  company text NOT NULL DEFAULT '',
  title text NOT NULL DEFAULT '',
  notes text NOT NULL DEFAULT '',
  source text NOT NULL DEFAULT 'agent',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE contacts ADD COLUMN IF NOT EXISTS first_name text NOT NULL DEFAULT '';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS last_name text NOT NULL DEFAULT '';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS email_address text NOT NULL DEFAULT '';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS company text NOT NULL DEFAULT '';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS title text NOT NULL DEFAULT '';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS notes text NOT NULL DEFAULT '';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'agent';
ALTER TABLE contacts DROP CONSTRAINT IF EXISTS contacts_source_check;
ALTER TABLE contacts ADD CONSTRAINT contacts_source_check CHECK (source IN ('dashboard', 'agent'));
ALTER TABLE contacts DROP CONSTRAINT IF EXISTS contacts_not_blank_check;
ALTER TABLE contacts ADD CONSTRAINT contacts_not_blank_check CHECK (
  first_name <> ''
  OR last_name <> ''
  OR email_address <> ''
  OR company <> ''
  OR title <> ''
  OR notes <> ''
);

CREATE UNIQUE INDEX IF NOT EXISTS contacts_email_unique_idx
  ON contacts(lower(email_address))
  WHERE email_address <> '';
CREATE INDEX IF NOT EXISTS contacts_name_idx ON contacts(last_name, first_name);
CREATE INDEX IF NOT EXISTS contacts_company_idx ON contacts(company);
CREATE INDEX IF NOT EXISTS contacts_updated_idx ON contacts(updated_at DESC);

CREATE TABLE IF NOT EXISTS workspace_files (
  id bigserial PRIMARY KEY,
  relative_path text NOT NULL UNIQUE,
  size_bytes bigint NOT NULL DEFAULT 0,
  mtime_ns text NOT NULL DEFAULT '',
  content_sha256 text NOT NULL DEFAULT '',
  mime_type text,
  extension text NOT NULL DEFAULT '',
  index_status text NOT NULL DEFAULT 'pending',
  error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  indexed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS size_bytes bigint NOT NULL DEFAULT 0;
ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS mtime_ns text NOT NULL DEFAULT '';
ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS content_sha256 text NOT NULL DEFAULT '';
ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS mime_type text;
ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS extension text NOT NULL DEFAULT '';
ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS index_status text NOT NULL DEFAULT 'pending';
ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS error text;
ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS indexed_at timestamptz;
ALTER TABLE workspace_files DROP CONSTRAINT IF EXISTS workspace_files_index_status_check;
ALTER TABLE workspace_files ADD CONSTRAINT workspace_files_index_status_check CHECK (
  index_status IN ('pending', 'indexed', 'embedding_failed', 'unsupported', 'error', 'deleted', 'superseded')
);

CREATE INDEX IF NOT EXISTS workspace_files_status_idx ON workspace_files(index_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS workspace_files_extension_idx ON workspace_files(extension);

CREATE TABLE IF NOT EXISTS workspace_file_chunks (
  id bigserial PRIMARY KEY,
  file_id bigint NOT NULL REFERENCES workspace_files(id) ON DELETE CASCADE,
  chunk_index int NOT NULL,
  content text NOT NULL,
  start_line int,
  end_line int,
  content_sha256 text NOT NULL DEFAULT '',
  embedding double precision[],
  embedding_model text,
  embedding_dimensions int,
  embedding_updated_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(file_id, chunk_index)
);

ALTER TABLE workspace_file_chunks ADD COLUMN IF NOT EXISTS start_line int;
ALTER TABLE workspace_file_chunks ADD COLUMN IF NOT EXISTS end_line int;
ALTER TABLE workspace_file_chunks ADD COLUMN IF NOT EXISTS content_sha256 text NOT NULL DEFAULT '';
ALTER TABLE workspace_file_chunks ADD COLUMN IF NOT EXISTS embedding double precision[];
ALTER TABLE workspace_file_chunks ADD COLUMN IF NOT EXISTS embedding_model text;
ALTER TABLE workspace_file_chunks ADD COLUMN IF NOT EXISTS embedding_dimensions int;
ALTER TABLE workspace_file_chunks ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;

CREATE INDEX IF NOT EXISTS workspace_file_chunks_file_idx ON workspace_file_chunks(file_id, chunk_index);

CREATE TABLE IF NOT EXISTS workspace_document_conversions (
  id bigserial PRIMARY KEY,
  original_relative_path text NOT NULL,
  markdown_relative_path text,
  archived_relative_path text,
  original_sha256 text,
  markdown_sha256 text,
  status text NOT NULL DEFAULT 'pending',
  source text NOT NULL DEFAULT 'workspace',
  error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE workspace_document_conversions ADD COLUMN IF NOT EXISTS markdown_relative_path text;
ALTER TABLE workspace_document_conversions ADD COLUMN IF NOT EXISTS archived_relative_path text;
ALTER TABLE workspace_document_conversions ADD COLUMN IF NOT EXISTS original_sha256 text;
ALTER TABLE workspace_document_conversions ADD COLUMN IF NOT EXISTS markdown_sha256 text;
ALTER TABLE workspace_document_conversions ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'pending';
ALTER TABLE workspace_document_conversions ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'workspace';
ALTER TABLE workspace_document_conversions ADD COLUMN IF NOT EXISTS error text;
ALTER TABLE workspace_document_conversions ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE workspace_document_conversions DROP CONSTRAINT IF EXISTS workspace_document_conversions_status_check;
ALTER TABLE workspace_document_conversions ADD CONSTRAINT workspace_document_conversions_status_check CHECK (
  status IN ('pending', 'ready', 'failed', 'skipped')
);

CREATE INDEX IF NOT EXISTS workspace_document_conversions_original_idx ON workspace_document_conversions(original_relative_path, created_at DESC);
CREATE INDEX IF NOT EXISTS workspace_document_conversions_markdown_idx ON workspace_document_conversions(markdown_relative_path, created_at DESC);

CREATE TABLE IF NOT EXISTS reminders (
  id bigserial PRIMARY KEY,
  title text NOT NULL,
  task text NOT NULL,
  run_at timestamptz NOT NULL,
  status text NOT NULL DEFAULT 'scheduled'
    CHECK (status IN ('scheduled', 'queued', 'completed', 'failed', 'cancelled')),
  priority int NOT NULL DEFAULT 0,
  recurrence_unit text,
  recurrence_interval int,
  recurrence_anchor_day int,
  created_by text NOT NULL DEFAULT 'agent',
  created_by_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  queued_at timestamptz,
  completed_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE reminders ADD COLUMN IF NOT EXISTS recurrence_unit text;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS recurrence_interval int;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS recurrence_anchor_day int;
ALTER TABLE reminders DROP CONSTRAINT IF EXISTS reminders_recurrence_check;
ALTER TABLE reminders ADD CONSTRAINT reminders_recurrence_check CHECK (
  (recurrence_unit IS NULL AND recurrence_interval IS NULL AND recurrence_anchor_day IS NULL)
  OR (
    recurrence_unit IN ('hour', 'day', 'week', 'month')
    AND recurrence_interval IS NOT NULL
    AND recurrence_interval > 0
    AND (recurrence_anchor_day IS NULL OR recurrence_anchor_day BETWEEN 1 AND 31)
  )
);

CREATE INDEX IF NOT EXISTS reminders_status_run_at_idx ON reminders(status, run_at);
CREATE INDEX IF NOT EXISTS reminders_job_id_idx ON reminders(job_id);

CREATE TABLE IF NOT EXISTS calendar_managed_events (
  assistant_id text PRIMARY KEY,
  uid text NOT NULL UNIQUE,
  calendar_name text NOT NULL,
  relative_path text NOT NULL,
  summary text NOT NULL DEFAULT '',
  starts_at timestamptz NOT NULL,
  ends_at timestamptz NOT NULL,
  file_hash text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'active',
  created_by_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  updated_by_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  deleted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS calendar_name text NOT NULL DEFAULT '';
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS relative_path text NOT NULL DEFAULT '';
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS summary text NOT NULL DEFAULT '';
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS starts_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS ends_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS file_hash text NOT NULL DEFAULT '';
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active';
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS created_by_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL;
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS updated_by_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL;
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE calendar_managed_events DROP CONSTRAINT IF EXISTS calendar_managed_events_status_check;
ALTER TABLE calendar_managed_events ADD CONSTRAINT calendar_managed_events_status_check CHECK (status IN ('active', 'deleted'));

CREATE UNIQUE INDEX IF NOT EXISTS calendar_managed_events_uid_unique_idx ON calendar_managed_events(uid);
CREATE INDEX IF NOT EXISTS calendar_managed_events_status_starts_idx ON calendar_managed_events(status, starts_at);
CREATE INDEX IF NOT EXISTS calendar_managed_events_job_idx ON calendar_managed_events(created_by_job_id, updated_by_job_id);

CREATE TABLE IF NOT EXISTS calendar_event_audit (
  id bigserial PRIMARY KEY,
  assistant_id text NOT NULL REFERENCES calendar_managed_events(assistant_id) ON DELETE CASCADE,
  job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  action text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE calendar_event_audit DROP CONSTRAINT IF EXISTS calendar_event_audit_action_check;
ALTER TABLE calendar_event_audit ADD CONSTRAINT calendar_event_audit_action_check CHECK (action IN ('created', 'updated', 'deleted'));
CREATE INDEX IF NOT EXISTS calendar_event_audit_event_created_idx ON calendar_event_audit(assistant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS calendar_event_audit_job_created_idx ON calendar_event_audit(job_id, created_at DESC);

CREATE TABLE IF NOT EXISTS projects (
  id bigserial PRIMARY KEY,
  original_job_id bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  original_thread_id text NOT NULL,
  title text NOT NULL,
  status text NOT NULL DEFAULT 'queued',
  priority int NOT NULL DEFAULT 0,
  run_at timestamptz NOT NULL DEFAULT now(),
  locked_at timestamptz,
  locked_by text,
  result_summary text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  completed_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_status_check;
ALTER TABLE projects ADD CONSTRAINT projects_status_check CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled'));
ALTER TABLE projects ADD COLUMN IF NOT EXISTS priority int NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS run_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE projects ADD COLUMN IF NOT EXISTS locked_at timestamptz;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS locked_by text;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS result_summary text;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS completed_at timestamptz;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS last_error text;

CREATE INDEX IF NOT EXISTS projects_status_run_at_idx ON projects(status, run_at);
CREATE INDEX IF NOT EXISTS projects_original_job_idx ON projects(original_job_id);
CREATE INDEX IF NOT EXISTS outbound_email_logs_provider_message_idx ON outbound_email_logs(provider_message_id);

CREATE TABLE IF NOT EXISTS project_tasks (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  sequence int NOT NULL,
  title text NOT NULL,
  task text NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  priority int NOT NULL DEFAULT 0,
  result_summary text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  queued_at timestamptz,
  completed_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, sequence)
);

ALTER TABLE project_tasks DROP CONSTRAINT IF EXISTS project_tasks_status_check;
ALTER TABLE project_tasks ADD CONSTRAINT project_tasks_status_check CHECK (status IN ('pending', 'queued', 'running', 'completed', 'failed', 'cancelled'));
ALTER TABLE project_tasks ADD COLUMN IF NOT EXISTS job_id bigint REFERENCES jobs(id) ON DELETE SET NULL;
ALTER TABLE project_tasks ADD COLUMN IF NOT EXISTS priority int NOT NULL DEFAULT 0;
ALTER TABLE project_tasks ADD COLUMN IF NOT EXISTS result_summary text;
ALTER TABLE project_tasks ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE project_tasks ADD COLUMN IF NOT EXISTS queued_at timestamptz;
ALTER TABLE project_tasks ADD COLUMN IF NOT EXISTS completed_at timestamptz;
ALTER TABLE project_tasks ADD COLUMN IF NOT EXISTS last_error text;

CREATE INDEX IF NOT EXISTS project_tasks_project_sequence_idx ON project_tasks(project_id, sequence);
CREATE INDEX IF NOT EXISTS project_tasks_job_idx ON project_tasks(job_id);
CREATE INDEX IF NOT EXISTS project_tasks_status_idx ON project_tasks(status);

CREATE TABLE IF NOT EXISTS deep_research_runs (
  id bigserial PRIMARY KEY,
  original_job_id bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  original_thread_id text NOT NULL,
  title text NOT NULL,
  research_question text NOT NULL,
  instructions text,
  status text NOT NULL DEFAULT 'queued',
  priority int NOT NULL DEFAULT 0,
  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 3,
  run_at timestamptz NOT NULL DEFAULT now(),
  locked_at timestamptz,
  locked_by text,
  tool_call_count int NOT NULL DEFAULT 0,
  max_tool_calls int NOT NULL DEFAULT 40,
  waiting_since timestamptz,
  result_summary text,
  result_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  completed_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE deep_research_runs DROP CONSTRAINT IF EXISTS deep_research_runs_status_check;
ALTER TABLE deep_research_runs ADD CONSTRAINT deep_research_runs_status_check CHECK (status IN ('queued', 'running', 'waiting_for_input', 'completed', 'failed', 'cancelled'));
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS instructions text;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS priority int NOT NULL DEFAULT 0;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS attempts int NOT NULL DEFAULT 0;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS max_attempts int NOT NULL DEFAULT 3;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS run_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS locked_at timestamptz;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS locked_by text;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS tool_call_count int NOT NULL DEFAULT 0;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS max_tool_calls int NOT NULL DEFAULT 40;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS waiting_since timestamptz;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS result_summary text;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS result_data jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS completed_at timestamptz;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS last_error text;

CREATE INDEX IF NOT EXISTS deep_research_runs_status_run_at_idx ON deep_research_runs(status, run_at);
CREATE INDEX IF NOT EXISTS deep_research_runs_original_job_idx ON deep_research_runs(original_job_id);

CREATE TABLE IF NOT EXISTS deep_research_events (
  id bigserial PRIMARY KEY,
  run_id bigint NOT NULL REFERENCES deep_research_runs(id) ON DELETE CASCADE,
  sequence int NOT NULL,
  event_type text NOT NULL,
  tool_name text,
  input_data jsonb,
  output_data jsonb,
  tokens_used jsonb,
  duration_ms int,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (run_id, sequence)
);

ALTER TABLE deep_research_events DROP CONSTRAINT IF EXISTS deep_research_events_event_type_check;
ALTER TABLE deep_research_events ADD CONSTRAINT deep_research_events_event_type_check CHECK (
  event_type IN ('llm_request', 'llm_response', 'tool_call', 'tool_result', 'search_request', 'search_result', 'error', 'status_change')
);

CREATE INDEX IF NOT EXISTS deep_research_events_run_sequence_idx ON deep_research_events(run_id, sequence);
CREATE INDEX IF NOT EXISTS deep_research_events_run_created_idx ON deep_research_events(run_id, created_at);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'processed_artifacts_set_updated_at') THEN
    CREATE TRIGGER processed_artifacts_set_updated_at
    BEFORE UPDATE ON processed_artifacts
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'agent_memories_set_updated_at') THEN
    CREATE TRIGGER agent_memories_set_updated_at
    BEFORE UPDATE ON agent_memories
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'agent_notes_set_updated_at') THEN
    CREATE TRIGGER agent_notes_set_updated_at
    BEFORE UPDATE ON agent_notes
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'contacts_set_updated_at') THEN
    CREATE TRIGGER contacts_set_updated_at
    BEFORE UPDATE ON contacts
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'reminders_set_updated_at') THEN
    CREATE TRIGGER reminders_set_updated_at
    BEFORE UPDATE ON reminders
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'calendar_managed_events_set_updated_at') THEN
    CREATE TRIGGER calendar_managed_events_set_updated_at
    BEFORE UPDATE ON calendar_managed_events
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'projects_set_updated_at') THEN
    CREATE TRIGGER projects_set_updated_at
    BEFORE UPDATE ON projects
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'project_tasks_set_updated_at') THEN
    CREATE TRIGGER project_tasks_set_updated_at
    BEFORE UPDATE ON project_tasks
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'deep_research_runs_set_updated_at') THEN
    CREATE TRIGGER deep_research_runs_set_updated_at
    BEFORE UPDATE ON deep_research_runs
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'workspace_files_set_updated_at') THEN
    CREATE TRIGGER workspace_files_set_updated_at
    BEFORE UPDATE ON workspace_files
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'workspace_document_conversions_set_updated_at') THEN
    CREATE TRIGGER workspace_document_conversions_set_updated_at
    BEFORE UPDATE ON workspace_document_conversions
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;
END $$;

-- Context search embedding columns
ALTER TABLE jobs
  ADD COLUMN IF NOT EXISTS embedding double precision[],
  ADD COLUMN IF NOT EXISTS embedding_model text,
  ADD COLUMN IF NOT EXISTS embedding_dimensions int,
  ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;

ALTER TABLE reminders
  ADD COLUMN IF NOT EXISTS embedding double precision[],
  ADD COLUMN IF NOT EXISTS embedding_model text,
  ADD COLUMN IF NOT EXISTS embedding_dimensions int,
  ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;

ALTER TABLE outbound_email_logs
  ADD COLUMN IF NOT EXISTS embedding double precision[],
  ADD COLUMN IF NOT EXISTS embedding_model text,
  ADD COLUMN IF NOT EXISTS embedding_dimensions int,
  ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;

ALTER TABLE emails
  ADD COLUMN IF NOT EXISTS embedding double precision[],
  ADD COLUMN IF NOT EXISTS embedding_model text,
  ADD COLUMN IF NOT EXISTS embedding_dimensions int,
  ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;

ALTER TABLE contacts
  ADD COLUMN IF NOT EXISTS embedding double precision[],
  ADD COLUMN IF NOT EXISTS embedding_model text,
  ADD COLUMN IF NOT EXISTS embedding_dimensions int,
  ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;

ALTER TABLE projects
  ADD COLUMN IF NOT EXISTS embedding double precision[],
  ADD COLUMN IF NOT EXISTS embedding_model text,
  ADD COLUMN IF NOT EXISTS embedding_dimensions int,
  ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;
"""
