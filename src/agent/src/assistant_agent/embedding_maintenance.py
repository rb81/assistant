import argparse
import json
import logging
import re
import time
import urllib.request
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from .config import database_url, load_config
from .context_store import (contact_embedding_text, inbound_email_embedding_text,
                            job_embedding_text, outbound_email_embedding_text,
                            reminder_embedding_text)
from .database import Database, UPDATED_AT_SQL
from .embedding_client import EmbeddingClient


LOGGER = logging.getLogger("assistant.embedding_maintenance")

SOURCES = (
    ("agent_memories", ("content",), lambda row: row["content"], "expires_at IS NULL OR expires_at > now()", "content"),
    ("workspace_file_chunks", ("content",), lambda row: row["content"],
     "EXISTS (SELECT 1 FROM workspace_files f WHERE f.id = record.file_id AND f.index_status IN ('indexed', 'embedding_failed'))", "content"),
    ("jobs", ("task_summary", "metadata"), job_embedding_text, "true", "concat_ws(' ', task_summary, metadata->>'final_response')"),
    ("reminders", ("title", "task"), reminder_embedding_text, "true", "concat_ws(' ', title, task)"),
    ("outbound_email_logs", ("to_addresses", "subject", "body_text"), outbound_email_embedding_text,
     "true", "concat_ws(' ', array_to_string(to_addresses, ' '), subject, body_text)"),
    ("emails", ("from_address", "subject", "body_text", "body_html"), inbound_email_embedding_text,
     "true", "concat_ws(' ', from_address, subject, body_text, body_html)"),
    ("contacts", ("first_name", "last_name", "email_address", "company", "title", "notes"), contact_embedding_text,
     "true", "concat_ws(' ', first_name, last_name, email_address, company, title, notes)"),
)


class EmbeddingMaintenance:
    def __init__(self, db, config):
        self.db = db
        self.client = EmbeddingClient(config)
        self.dimensions = self.client.dimensions
        if self.dimensions is None and self.client.model.split(":")[0] == "embeddinggemma":
            self.dimensions = 768
        self._last_ids: dict[str, int] = {}

    def predicate(self, active, text):
        return (f"({active}) AND btrim({text}, E' \\t\\r\\n') <> '' AND "
                "(embedding IS NULL OR embedding_model IS DISTINCT FROM %s "
                "OR embedding_dimensions IS NULL OR (%s::int IS NOT NULL AND embedding_dimensions IS DISTINCT FROM %s) "
                "OR cardinality(embedding) IS DISTINCT FROM embedding_dimensions)")

    def parameters(self):
        return (self.client.model, self.dimensions, self.dimensions)

    def status(self):
        counts = {}
        for table, fields, builder, active, text in SOURCES:
            row = self.db.fetch_one(f"SELECT count(*) AS count FROM {table} record WHERE {self.predicate(active, text)}", self.parameters())
            counts[table] = row["count"]
        return {"model": self.client.model, "dimensions": self.dimensions, "enabled": self.client.enabled,
                "pending": counts, "complete": not any(counts.values())}

    def probe(self):
        if not self.client.enabled:
            raise RuntimeError("Embeddings are disabled; enable them before probing or rebuilding")
        if self.client.model.split(":")[0] == "embeddinggemma":
            with urllib.request.urlopen(self.client.base_url + "/api/version", timeout=self.client.timeout_seconds) as response:
                version = str(json.loads(response.read())["version"])
            match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
            if not match or tuple(int(part) for part in match.groups()) < (0, 11, 10):
                raise RuntimeError("EmbeddingGemma requires Ollama 0.11.10 or newer")
        vector = self.client.embed("Embedding model connectivity verification.")
        if self.dimensions is not None and len(vector) != self.dimensions:
            raise RuntimeError("Embedding model returned unexpected dimensions")
        self.dimensions = len(vector)
        return {"model": self.client.model, "dimensions": self.dimensions, "status": "ok"}

    def refresh_row(self, table, fields, builder, active, row):
        content = builder(row).strip()
        if not content:
            raise RuntimeError(f"Empty embedding source in {table}, record {row['id']}; no data was changed")
        vector = self.client.embed(content)
        if self.dimensions is not None and len(vector) != self.dimensions:
            raise RuntimeError("Embedding dimensions changed during refresh; retry with a stable model")
        guarded_fields = fields + ("embedding_model", "embedding_dimensions", "embedding_updated_at")
        guards = " AND ".join(f"{field} IS NOT DISTINCT FROM %s" for field in guarded_fields)
        snapshot = tuple(Jsonb(row.get(field)) if field == "metadata" else row.get(field) for field in guarded_fields)
        pending = ", metadata = metadata - 'embedding_pending'" if table == "agent_memories" else ""
        return bool(self.db.fetch_one(
            f"UPDATE {table} record SET embedding = %s, embedding_model = %s, embedding_dimensions = %s, "
            f"embedding_updated_at = %s{pending} WHERE id = %s AND ({active}) AND {guards} RETURNING id",
            (vector, self.client.model, len(vector), datetime.now(timezone.utc), row["id"]) + snapshot,
        ))

    def refresh_pending(self, batch_size=10, *, deadline=None):
        if not 1 <= batch_size <= 1000:
            raise ValueError("Batch size must be between 1 and 1000")
        result = {"refreshed": 0, "conflicts": 0, "failed": 0}
        if not self.client.enabled:
            return result
        with self.db.connect() as lock_connection:
            lock_connection.autocommit = True
            locked = lock_connection.execute("SELECT pg_try_advisory_lock(hashtext('assistant_embedding_reindex')) AS locked").fetchone()
            if not locked["locked"]:
                return result
            try:
                for table, fields, builder, active, text in SOURCES:
                    last_id = self._last_ids.get(table, 0)
                    query = (f"SELECT * FROM {table} record WHERE {self.predicate(active, text)} "
                             "AND id > %s ORDER BY id LIMIT %s")
                    rows = self.db.fetch_all(query, self.parameters() + (last_id, batch_size))
                    if not rows and last_id:
                        self._last_ids[table] = 0
                        rows = self.db.fetch_all(query, self.parameters() + (0, batch_size))
                    for row in rows:
                        if deadline is not None and time.monotonic() >= deadline:
                            return result
                        # Advance past failures so one bad record cannot starve the rest.
                        self._last_ids[table] = row["id"]
                        timeout = self.client.timeout_seconds
                        try:
                            if deadline is not None:
                                self.client.timeout_seconds = min(timeout, max(0.01, deadline - time.monotonic()))
                            saved = self.refresh_row(table, fields, builder, active, row)
                        except TimeoutError:
                            result["failed"] += 1
                            LOGGER.warning("embedding request timed out for %s record %s; will retry", table, row["id"])
                        except Exception:
                            result["failed"] += 1
                            LOGGER.exception("automatic embedding refresh failed for %s record %s; will retry", table, row["id"])
                        else:
                            result["refreshed"] += saved
                            result["conflicts"] += not saved
                        finally:
                            self.client.timeout_seconds = timeout
            finally:
                lock_connection.execute("SELECT pg_advisory_unlock(hashtext('assistant_embedding_reindex'))")
        return result

    def reindex(self, batch_size=50):
        if not 1 <= batch_size <= 1000:
            raise ValueError("Batch size must be between 1 and 1000")
        refreshed = 0
        conflicts = 0
        with self.db.connect() as lock_connection:
            lock_connection.autocommit = True
            idle = lock_connection.execute(
                "SELECT pg_try_advisory_lock(hashtext('assistant_embedding_activity')) AS locked",
            ).fetchone()["locked"]
            if not idle:
                raise RuntimeError("Cannot rebuild embeddings while tasks are executing; retry when idle")
            locked = lock_connection.execute("SELECT pg_try_advisory_lock(hashtext('assistant_embedding_reindex')) AS locked").fetchone()
            if not locked["locked"]:
                lock_connection.execute("SELECT pg_advisory_unlock(hashtext('assistant_embedding_activity'))")
                raise RuntimeError("Another embedding rebuild is already running")
            try:
                self.probe()
                self.db.execute(UPDATED_AT_SQL)
                for table, fields, builder, active, text in SOURCES:
                    last_id = 0
                    while True:
                        rows = self.db.fetch_all(
                            f"SELECT * FROM {table} record WHERE {self.predicate(active, text)} AND id > %s ORDER BY id LIMIT %s",
                            self.parameters() + (last_id, batch_size),
                        )
                        if not rows:
                            break
                        for row in rows:
                            available = lock_connection.execute(
                                "SELECT pg_try_advisory_lock(hashtext('assistant_embedding_startup')) AS locked"
                            ).fetchone()["locked"]
                            if not available:
                                raise RuntimeError("Embedding rebuild yielded to queued agent work; rerun when idle to resume")
                            lock_connection.execute("SELECT pg_advisory_unlock(hashtext('assistant_embedding_startup'))")
                            saved = self.refresh_row(table, fields, builder, active, row)
                            refreshed += bool(saved)
                            conflicts += not bool(saved)
                            last_id = row["id"]
                return {**self.status(), "refreshed": refreshed, "conflicts": conflicts}
            finally:
                lock_connection.execute("SELECT pg_advisory_unlock(hashtext('assistant_embedding_reindex'))")
                lock_connection.execute("SELECT pg_advisory_unlock(hashtext('assistant_embedding_activity'))")


def main():
    parser = argparse.ArgumentParser(description="Inspect, verify, or resumably rebuild embeddings without deleting source data")
    parser.add_argument("action", choices=("status", "probe", "reindex"))
    parser.add_argument("--batch-size", type=int, default=50)
    options = parser.parse_args()
    try:
        config = load_config()
        service = EmbeddingMaintenance(Database(database_url(config)), config)
        result = service.reindex(options.batch_size) if options.action == "reindex" else service.probe() if options.action == "probe" else service.status()
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({"status": "incomplete", "error_type": type(error).__name__,
                          "detail": "Maintenance stopped. Source data and previously saved vectors are preserved; verify configuration and rerun to resume."}))
        raise SystemExit(1) from None
    print(json.dumps(result))
    if result.get("complete") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()