"""Channel-neutral provenance, retaining attachments and confirmed deletion."""
import hashlib
import json
import uuid
from contextlib import contextmanager
from pathlib import Path

from psycopg import sql
from psycopg.types.json import Jsonb

from .database import json_safe


OBJECT_TABLES = {
    "job": ("jobs", "id"),
    "memory": ("agent_memories", "id"),
    "contact": ("contacts", "id"),
    "reminder": ("reminders", "id"),
    "research": ("deep_research_runs", "id"),
    "calendar_event": ("calendar_managed_events", "assistant_id"),
    "zoom_meeting": ("zoom_managed_meetings", "id"),
    "dropbox_transfer": ("dropbox_transfers", "request_key"),
    "artifact": ("processed_artifacts", "id"),
    "file": ("workstream_files", "id"),
}
ATTACHABLE_KINDS = {"contact", "file"}
MODES = ("preserve_shared", "delete_objects", "connected")
OWNED_TABLES = ("emails", "request_messages", "outbound_email_logs", "channel_deliveries", "thread_summaries")
DERIVED_TABLES = {
    "task_logs": ("job_id", "job"),
    "agent_checkpoints": ("job_id", "job"),
    "supervisor_instructions": ("job_id", "job"),
    "deep_research_events": ("run_id", "research"),
    "memory_events": ("memory_id", "memory"),
    "calendar_event_audit": ("assistant_id", "calendar_event"),
}


class WorkstreamError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(json_safe(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def descendants(selected, parents):
    result = set(selected)
    while True:
        expanded = result | {identifier for identifier, parent in parents.items() if parent in result}
        if expanded == result:
            return result
        result = expanded


def deletion_scope(selected, mode, parents, attachments):
    if mode not in MODES or selected not in parents:
        raise WorkstreamError("unknown workstream or deletion mode")
    streams = descendants({selected}, parents)
    if mode == "connected":
        while True:
            linked = set().union(*(owners for owners in attachments.values() if owners & streams))
            expanded = descendants(streams | linked, parents)
            if expanded == streams:
                break
            streams = expanded
    candidates = {identifier for identifier, owners in attachments.items() if owners & streams}
    shared = {identifier for identifier in candidates if attachments[identifier] - streams}
    removed = candidates - shared if mode == "preserve_shared" else candidates
    affected = set().union(*(attachments[identifier] - streams for identifier in removed)) if removed else set()
    return streams, removed, shared, affected


class WorkstreamStore:
    def __init__(self, db, config=None):
        self.db = db
        self.config = config

    def list(self, before_id=None, limit=50):
        return self.db.fetch_all(
            """SELECT w.*, count(a.object_id) AS object_count FROM workstreams w
               LEFT JOIN workstream_attachments a ON a.workstream_id = w.id
               WHERE (%s::bigint IS NULL OR w.id < %s)
               GROUP BY w.id ORDER BY w.id DESC LIMIT %s""",
            (before_id, before_id, min(max(int(limit), 1), 100)),
        )

    def objects(self, workstream_id):
        return self.db.fetch_all(
            """SELECT o.*, f.path, count(other.workstream_id) AS retaining_workstreams
               FROM workstream_objects o JOIN workstream_attachments a ON a.object_id = o.id
               JOIN workstream_attachments other ON other.object_id = o.id
               LEFT JOIN workstream_files f ON f.object_id = o.id
               WHERE a.workstream_id = %s GROUP BY o.id, f.path ORDER BY o.kind, o.id""", (workstream_id,),
        )

    def attach(self, workstream_id, kind, key):
        if kind not in ATTACHABLE_KINDS:
            raise WorkstreamError("only contacts and files can be adopted into another workstream")
        if kind == "file" and not str(key).isdigit():
            if self.config is None:
                raise WorkstreamError("file paths require filesystem configuration")
            root = Path(self.config.get("agent.filesystem.shared_root", "/data/share")).resolve()
            path = Path(str(key))
            path = path if path.is_absolute() else root / path
            if path.resolve() != path or root not in path.parents or ".assistant" in path.relative_to(root).parts:
                raise WorkstreamError("attach requires a regular, non-internal workspace file")
            checksum = self._checksum(path)
            if checksum is None:
                raise WorkstreamError("file does not exist")
            # Unknown existing files have unknown origin, not fabricated task provenance.
            from .database import Database
            unscoped = Database(self.db.database_url)
            row = unscoped.fetch_one(
                """INSERT INTO workstream_files(path, sha256) VALUES (%s, %s)
                   ON CONFLICT(path) DO UPDATE SET sha256 = EXCLUDED.sha256 RETURNING id""", (str(path), checksum),
            )
            key = row["id"]
        with self.db.connect() as connection, connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock_shared(hashtext('workstream-maintenance'))")
            cur.execute("SELECT id FROM workstreams WHERE id = %s AND status = 'active'", (workstream_id,))
            if cur.fetchone() is None:
                raise WorkstreamError("workstream is unavailable")
            cur.execute("SELECT * FROM workstream_objects WHERE kind = %s AND object_key = %s", (kind, str(key)))
            obj = cur.fetchone()
            if obj is None:
                raise WorkstreamError("object not found")
            ids, frontier = set(), [obj["id"]]
            while frontier:
                identifier = frontier.pop()
                if identifier in ids:
                    continue
                ids.add(identifier)
                cur.execute(
                    """INSERT INTO workstream_attachments(workstream_id, object_id) VALUES (%s, %s)
                       ON CONFLICT DO NOTHING""", (workstream_id, identifier),
                )
                cur.execute(
                    """SELECT j.object_id FROM deep_research_runs r JOIN jobs j ON j.id = r.original_job_id
                       WHERE r.object_id = %s
                       UNION
                       SELECT f.object_id FROM dropbox_transfers d JOIN workstream_files f
                         ON f.path IN (d.local_path, d.markdown_path)
                         WHERE d.object_id = %s AND d.direction = 'import'
                       UNION
                       SELECT c.object_id FROM zoom_managed_meetings z JOIN calendar_managed_events c
                         ON c.assistant_id = z.calendar_event_id WHERE z.object_id = %s
                       UNION
                       SELECT z.object_id FROM calendar_managed_events c JOIN zoom_managed_meetings z
                         ON z.id::text = c.metadata->>'zoom_meeting_id' OR z.calendar_event_id = c.assistant_id
                         WHERE c.object_id = %s""", (identifier,) * 4,
                )
                frontier.extend(row["object_id"] for row in cur.fetchall())
                cur.execute("SELECT * FROM processed_artifacts WHERE object_id = %s", (identifier,))
                artifact = cur.fetchone()
                if artifact:
                    cur.execute("SELECT object_id FROM workstream_files WHERE path = ANY(%s)",
                                (list(self._row_paths("artifact", artifact)),))
                    frontier.extend(row["object_id"] for row in cur.fetchall())
            return {"workstream_id": workstream_id, "object_ids": sorted(ids), "origin_workstream_id": obj["origin_workstream_id"]}

    def read(self, workstream_id, *, thread_id=None, before=None, limit=25,
             include_system_threads=False, include_objects=False, include_execution_details=False,
             include_change_events=False):
        limit = min(max(int(limit or 25), 1), 100)
        workstream = self.db.fetch_one(
            "SELECT id, title, status, parent_id, main_thread_id, created_at FROM workstreams WHERE id = %s",
            (workstream_id,),
        )
        if workstream is None:
            raise WorkstreamError("workstream not found")
        if thread_id is None:
            thread_id = workstream["main_thread_id"]
        thread = self.db.fetch_one(
            "SELECT thread_id, role FROM workstream_threads WHERE thread_id = %s AND workstream_id = %s",
            (thread_id, workstream_id),
        )
        if thread is None:
            raise WorkstreamError("thread does not belong to this workstream")
        history = self.db.latest_thread_messages(thread_id, limit=limit, through=before)
        next_before = None
        if len(history) == limit:
            oldest = history[0]
            next_before = json.dumps({
                "timestamp": json_safe(
                    oldest.get("received_at") or oldest.get("sent_at") or oldest.get("created_at")
                ),
                "id": int(oldest.get("id") or 0),
                "thread_item_id": str(oldest.get("thread_item_id") or ""),
            }, separators=(",", ":"))
        result = {
            "workstream": json_safe(workstream),
            "thread": {**thread, "is_main": thread_id == workstream["main_thread_id"]},
            "messages": json_safe(history),
            "next_before": next_before,
        }
        if include_system_threads:
            threads = self.db.fetch_all(
                """SELECT thread_id FROM workstream_threads
                   WHERE workstream_id = %s AND role = 'system' ORDER BY thread_id""",
                (workstream_id,),
            )
            result["system_threads"] = [
                {"thread_id": item["thread_id"],
                 "messages": json_safe(self.db.latest_thread_messages(item["thread_id"], limit=limit))}
                for item in threads
            ]
            result["task_notifications"] = json_safe(self.db.fetch_all(
                """SELECT source_job_id, source_thread_id, notification_type, summary, content, created_at
                   FROM workstream_task_notifications
                   WHERE workstream_id = %s ORDER BY id DESC LIMIT %s""",
                (workstream_id, limit),
            ))
        if include_objects:
            result["objects"] = json_safe(self.objects(workstream_id))
        if include_change_events:
            result["change_events"] = json_safe(self.db.fetch_all(
                """SELECT e.id, e.object_id, e.object_kind, e.object_key, e.origin_workstream_id,
                          e.actor_job_id, e.actor_workstream_id, e.event_type, e.changes, e.reason, e.created_at
                   FROM workstream_context_events c
                   JOIN workstream_object_events e ON e.id = c.object_event_id
                   WHERE c.workstream_id = %s ORDER BY e.id DESC LIMIT %s""",
                (workstream_id, limit),
            ))
        if include_execution_details:
            jobs = self.db.fetch_all(
                """SELECT id, thread_id, task_summary, status, created_at, completed_at, last_error
                   FROM jobs WHERE workstream_id = %s
                   AND (%s::text IS NULL OR thread_id = %s)
                   ORDER BY id DESC LIMIT %s""",
                (workstream_id, thread_id, thread_id, limit),
            )
            for job in jobs:
                job["events"] = self.db.fetch_all(
                    """SELECT sequence, event_type, input_data, output_data, created_at
                       FROM task_logs WHERE job_id = %s ORDER BY sequence DESC LIMIT 10""",
                    (job["id"],),
                )
                job["events"].reverse()
            result["executions"] = json_safe(jobs)
        return result

    @contextmanager
    def maintenance(self):
        # A session lock spans filesystem/provider operations and their separate DB transactions.
        with self.db.connect() as connection:
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock(hashtext('workstream-maintenance')) AS acquired"
            ).fetchone()["acquired"]
            if not acquired:
                raise WorkstreamError("another operation is changing workstreams; retry")
            connection.commit()
            try:
                yield connection
            finally:
                connection.rollback()
                connection.execute("SELECT pg_advisory_unlock(hashtext('workstream-maintenance'))")

    def _inventory(self, connection, selected, mode):
        streams = list(connection.execute("SELECT * FROM workstreams ORDER BY id"))
        links = list(connection.execute("SELECT * FROM workstream_attachments ORDER BY workstream_id, object_id"))
        owners = {}
        for link in links:
            owners.setdefault(link["object_id"], set()).add(link["workstream_id"])
        scope, removed, shared, affected = deletion_scope(selected, mode, {w["id"]: w["parent_id"] for w in streams}, owners)
        if any(w["status"] != "active" for w in streams if w["id"] in scope):
            raise WorkstreamError("workstream deletion is pending; resume the existing deletion")
        records, objects = {}, {}
        for kind, (table, _) in OBJECT_TABLES.items():
            rows = list(connection.execute(sql.SQL("SELECT * FROM {} ORDER BY object_id").format(sql.Identifier(table))))
            objects[kind] = rows
            records[table] = [row for row in rows if row["object_id"] in removed]
        for table in OWNED_TABLES:
            records[table] = list(connection.execute(
                sql.SQL("SELECT * FROM {} WHERE workstream_id = ANY(%s) ORDER BY 1").format(sql.Identifier(table)),
                (sorted(scope),),
            ))
        for table, (column, kind) in DERIVED_TABLES.items():
            key = OBJECT_TABLES[kind][1]
            keys = [str(row[key]) for row in records[OBJECT_TABLES[kind][0]]]
            records[table] = list(connection.execute(
                sql.SQL("SELECT * FROM {} WHERE {}::text = ANY(%s) ORDER BY 1").format(
                    sql.Identifier(table), sql.Identifier(column)), (keys,),
            ))
        records["workstream_object_events"] = list(connection.execute(
            """SELECT * FROM workstream_object_events
               WHERE object_id = ANY(%s) OR actor_workstream_id = ANY(%s) ORDER BY id""",
            (sorted(removed), sorted(scope)),
        ))
        records["workstream_context_events"] = list(connection.execute(
            "SELECT * FROM workstream_context_events WHERE workstream_id = ANY(%s) ORDER BY id",
            (sorted(scope),),
        ))
        jobs = {str(j["id"]) for j in records["jobs"]}
        records["manual_events"] = [
            row for row in connection.execute("SELECT * FROM manual_events ORDER BY id")
            if any(str((row["payload"] or {}).get(key)) in jobs for key in
                   ("job_id", "original_job_id", "parent_job_id", "completed_job_id"))
        ]
        running = [j["id"] for j in objects["job"] if
                   (j["workstream_id"] in scope | affected or j["object_id"] in removed) and j["status"] == "running"]
        running_research = [r["id"] for r in objects["research"] if
                            (r["object_id"] in removed or str(r["original_job_id"]) in jobs) and r["status"] == "running"]
        blocked_reminders = [r["id"] for r in objects["reminder"] if
                             r["object_id"] not in removed and r["status"] == "queued" and str(r["job_id"]) in jobs]
        object_kinds = {row["object_id"]: kind for kind, rows in objects.items() for row in rows}
        kept_paths = set()
        for kind, rows in objects.items():
            for row in rows:
                if row["object_id"] not in removed:
                    kept_paths.update(self._row_paths(kind, row))
        cleanup = []
        for kind, rows in objects.items():
            for row in rows:
                if row["object_id"] not in removed:
                    continue
                for path, checksum in self._row_paths(kind, row).items():
                    if path not in kept_paths:
                        cleanup.append({"type": "file", "path": path, "sha256": checksum})
                if kind == "dropbox_transfer" and row["direction"] == "export" and row["status"] in {"ready", "uncertain", "pending"}:
                    cleanup.append({"type": "dropbox", "key": row["request_key"], "path": row["remote_path"],
                                    "revision": row["revision"], "status": row["status"]})
                if kind == "calendar_event" and row["status"] != "deleted":
                    cleanup.append({"type": "calendar", "key": row["assistant_id"], "provider": row["provider_name"],
                                    "calendar_id": row["calendar_id"], "provider_id": row["provider_event_id"]})
                if kind == "zoom_meeting" and row["status"] not in {"deleted", "failed"}:
                    cleanup.append({"type": "zoom", "key": row["id"], "provider_id": row["provider_meeting_id"]})
        # A failed conversion/download can leave an inbound file without an artifact row.
        for table in ("emails", "request_messages"):
            for message in records[table]:
                for attachment in message.get("attachments") or []:
                    path = attachment.get("path")
                    if path and path not in kept_paths:
                        cleanup.append({"type": "file", "path": path, "sha256": attachment.get("sha256")})
        if self.config:
            from .tool_result_cache import ToolResultCache
            cache = ToolResultCache(self.config).root
            for job in records["jobs"]:
                folder = cache / ("job-%s" % job["id"])
                if folder.is_symlink():
                    raise WorkstreamError("tool cache contains a symlink; resolve it before deletion")
                if folder.exists():
                    for path in sorted(folder.rglob("*")):
                        if path.is_file() or path.is_symlink():
                            cleanup.append({"type": "file", "path": str(path), "sha256": self._checksum(path)})
        # Derivative indexes must be previewed and removed, not silently rebuilt from deleted bytes.
        paths = {step["path"] for step in cleanup if step["type"] == "file"}
        records["workspace_files"], records["workspace_document_conversions"] = [], []
        if self.config:
            root = Path(self.config.get("agent.filesystem.shared_root", "/data/share")).resolve()
            for row in connection.execute("SELECT * FROM workspace_files ORDER BY id"):
                if str(root / row["relative_path"]) in paths:
                    records["workspace_files"].append(row)
            for row in connection.execute("SELECT * FROM workspace_document_conversions ORDER BY id"):
                row_paths = [str(root / row[key]) for key in
                             ("original_relative_path", "markdown_relative_path", "archived_relative_path") if row.get(key)]
                if row_paths and all(p in paths or not Path(p).exists() for p in row_paths):
                    records["workspace_document_conversions"].append(row)
        records["workspace_file_chunks"] = list(connection.execute(
            "SELECT * FROM workspace_file_chunks WHERE file_id = ANY(%s) ORDER BY id",
            ([row["id"] for row in records["workspace_files"]],),
        ))
        unique_cleanup = {digest(step): step for step in cleanup}
        cleanup = sorted(unique_cleanup.values(), key=lambda step: (step["type"], str(step.get("path", step.get("key")))))
        physical, blocked = [], []
        for step in cleanup:
            if step["type"] != "file":
                continue
            path = Path(step["path"])
            self._validate_cleanup_path(path)
            current = self._checksum(path)
            physical.append({"path": step["path"], "current_sha256": current})
            if current is not None and current != step["sha256"]:
                blocked.append({"path": step["path"], "reason": "missing or changed recorded digest"})
        counts = {table: len(rows) for table, rows in records.items() if rows}
        summary = {
            "workstream_id": selected, "mode": mode, "workstreams": [w for w in streams if w["id"] in scope],
            "deleted_counts": counts,
            "shared_objects": [{"id": identifier, "kind": object_kinds[identifier],
                                "retained_by": sorted(owners[identifier] - scope)} for identifier in sorted(shared)],
            "affected_workstreams": [w for w in streams if w["id"] in affected],
            "local_files": sum(step["type"] == "file" for step in cleanup),
            "remote_actions": [step for step in cleanup if step["type"] != "file"],
            "running_jobs": running, "running_research": running_research,
            "blocked_files": blocked,
            "blocked_reminders": blocked_reminders,
            "limitations": "Sent messages, downloaded provider inputs, backups and remote copies cannot be recalled. "
                          "Shared objects are live objects, not snapshots. This is deletion, not rollback.",
        }
        manifest = {"summary": summary, "workstream_ids": sorted(scope), "object_ids": sorted(removed),
                    "records": records, "cleanup": cleanup}
        fingerprint = digest({"manifest": manifest, "links": links, "physical": physical})
        return manifest, fingerprint

    def preview(self, selected, mode="preserve_shared"):
        with self.maintenance() as connection:
            manifest, fingerprint = self._inventory(connection, selected, mode)
            return json_safe({**manifest["summary"], "preview_digest": fingerprint})

    def begin_delete(self, selected, mode, preview_digest):
        with self.maintenance() as connection:
            manifest, fingerprint = self._inventory(connection, selected, mode)
            if fingerprint != preview_digest:
                raise WorkstreamError("scope changed; preview again before confirming")
            summary = manifest["summary"]
            if summary["running_jobs"] or summary["running_research"]:
                raise WorkstreamError("work is still running; let it finish before deletion")
            if summary["blocked_files"]:
                raise WorkstreamError("file provenance does not match; reconcile the listed files before deletion")
            if summary["blocked_reminders"]:
                raise WorkstreamError("shared reminders still have queued work in this tree; finish or cancel it before deletion")
            connection.execute("UPDATE workstreams SET status = 'deleting' WHERE id = ANY(%s)", (manifest["workstream_ids"],))
            connection.execute("UPDATE workstream_objects SET deleting = true WHERE id = ANY(%s)", (manifest["object_ids"],))
            row = connection.execute(
                """INSERT INTO workstream_deletions(workstream_ids, object_ids, mode, manifest)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (manifest["workstream_ids"], manifest["object_ids"], mode, Jsonb(json_safe(manifest))),
            ).fetchone()
            connection.commit()
            return row["id"]

    def _row_paths(self, kind, row):
        fields = {"file": (("path", "sha256"),), "artifact": (("raw_path", "raw_sha256"), ("markdown_path", "markdown_sha256"))}
        if kind == "dropbox_transfer" and row["direction"] == "import":
            fields[kind] = (("local_path", "local_sha256"), ("markdown_path", "markdown_sha256"))
        paths = {}
        for field, checksum in fields.get(kind, ()):
            if not row.get(field):
                continue
            path = Path(row[field])
            if kind == "artifact" and field == "markdown_path" and not path.is_absolute():
                if self.config is None:
                    raise WorkstreamError("filesystem configuration is required")
                if ".." in path.parts:
                    raise WorkstreamError("invalid cleanup path")
                root = Path(self.config.get("agent.filesystem.shared_root", "/data/share")).resolve()
                path = root / path
            expected = row.get(checksum)
            if (not expected and kind == "artifact" and field == "markdown_path"
                    and row.get("conversion_status") == "ready"
                    and Path(row.get("original_filename") or "").suffix.lower()
                    in {".ogg", ".opus", ".wav", ".mp3", ".m4a", ".flac"}):
                # Older voice artifacts omitted the digest but retained the exact transcript.
                transcript = (row.get("metadata") or {}).get("text")
                if isinstance(transcript, str):
                    expected = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
            paths[str(path)] = expected
        return paths

    @staticmethod
    def _checksum(path):
        if path.is_symlink() or path.resolve() != path.absolute():
            raise WorkstreamError("refusing a symlink in deletion inventory")
        if not path.exists():
            return None
        if not path.is_file():
            raise WorkstreamError("deletion target is not a regular file")
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()

    def _clean_step(self, step):
        if step["type"] == "file":
            self._delete_file(step)
        elif step["type"] == "dropbox":
            from .dropbox_provider import DropboxClient
            client = DropboxClient(self.config, self.db)
            if not step["revision"]:
                current = client.metadata(step["path"], "Outbox", missing_ok=True)
                if current is not None:
                    raise WorkstreamError("uncertain Dropbox upload must be reconciled before deletion")
            else:
                client.delete_file(step["path"], step["revision"], "Outbox")
        elif step["type"] == "calendar":
            from .calendar.router import CalendarRouter
            from .calendar.provider import ProviderEventNotFoundError
            router = CalendarRouter(self.db, self.config, {})
            try:
                router._get_provider(step["provider"]).delete_event(step["calendar_id"], step["provider_id"])
            except ProviderEventNotFoundError:
                pass
        elif step["type"] == "zoom":
            from .zoom_meetings import ZoomMeetings
            if not step["provider_id"]:
                raise WorkstreamError("uncertain Zoom meeting must be reconciled before deletion")
            ZoomMeetings(self.db, self.config, {}).request("DELETE", "meetings/" + str(int(step["provider_id"])))
        else:
            raise WorkstreamError("unsupported cleanup step")

    def _delete_file(self, step):
        path = Path(step["path"])
        self._validate_cleanup_path(path)
        current = self._checksum(path)
        if current is None:
            return
        if not step["sha256"] or current != step["sha256"]:
            raise WorkstreamError("file changed or has no verified digest; reconcile before retrying deletion")
        path.unlink()

    def _validate_cleanup_path(self, path):
        if self.config is None:
            raise WorkstreamError("filesystem configuration is required")
        roots = [Path(self.config.get("agent.filesystem.shared_root", "/data/share")).resolve(),
                 Path(self.config.get("agent.artifacts.raw_root", "/data/private/artifacts")).resolve()]
        absolute = path.absolute()
        if not path.is_absolute() or path != absolute or any(part in (".", "..") for part in path.parts):
            raise WorkstreamError("invalid cleanup path")
        root = next((r for r in roots if path != r and r in path.parents), None)
        if root is None or path.resolve() != path or (roots[0] / ".assistant/docs") in (path, *path.parents):
            raise WorkstreamError("cleanup path is outside managed storage or protected")

    def execute_delete(self, deletion_id):
        with self.maintenance() as connection:
            row = connection.execute("SELECT * FROM workstream_deletions WHERE id = %s", (deletion_id,)).fetchone()
            if row is None:
                raise WorkstreamError("deletion not found")
            if row["status"] == "completed":
                return {"id": deletion_id, "status": "completed"}
            manifest = row["manifest"]
            done = set(row["completed_steps"])
            try:
                for step in manifest["cleanup"]:
                    key = digest(step)
                    if key not in done:
                        self._clean_step(step)
                        done.add(key)
                        connection.execute("UPDATE workstream_deletions SET completed_steps = %s WHERE id = %s",
                                           (Jsonb(sorted(done)), deletion_id))
                        connection.commit()
                connection.execute("SELECT set_config('assistant.workstream_cleanup', 'yes', true)")
                self._delete_records(connection, manifest)
                # Only a content-free receipt remains after success.
                connection.execute(
                    """UPDATE workstream_deletions SET status = 'completed', error = NULL,
                       manifest = %s, completed_steps = '[]'::jsonb WHERE id = %s""",
                    (Jsonb({"counts": manifest["summary"]["deleted_counts"]}), deletion_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                connection.execute(
                    "UPDATE workstream_deletions SET status = 'failed', error = %s WHERE id = %s",
                    ("Cleanup incomplete. Original records retained; retry this deletion after resolving the error.", deletion_id),
                )
                connection.commit()
                raise
            return {"id": deletion_id, "status": "completed", "deleted_counts": manifest["summary"]["deleted_counts"]}

    def _delete_records(self, connection, manifest):
        scope, removed = manifest["workstream_ids"], manifest["object_ids"]
        connection.execute(
            """UPDATE telegram_workstream_selections
               SET unavailable_workstream_id = workstream_id, workstream_id = NULL, updated_at = now()
               WHERE workstream_id = ANY(%s)""", (scope,),
        )
        # Shared jobs retain their logs and can continue in a surviving workstream.
        jobs = list(connection.execute("SELECT * FROM jobs WHERE workstream_id = ANY(%s)", (scope,)))
        job_workstreams = {job["id"]: job["workstream_id"] for job in jobs}
        references = []
        for item in manifest["records"]["emails"]:
            value = item.get("message_id")
            if value:
                references.append((hashlib.sha256(str(value).encode()).hexdigest(), item["workstream_id"]))
        for item in manifest["records"]["outbound_email_logs"]:
            value = item.get("provider_message_id")
            workstream_id = item.get("workstream_id") or job_workstreams.get(item.get("job_id"))
            if value and workstream_id:
                references.append((hashlib.sha256(str(value).encode()).hexdigest(), workstream_id))
        for item in manifest["records"]["request_messages"]:
            value = item.get("external_id")
            if value:
                references.append((hashlib.sha256(str(value).encode()).hexdigest(), item["workstream_id"]))
        for item in manifest["records"]["channel_deliveries"]:
            workstream_id = item.get("workstream_id") or job_workstreams.get(item.get("job_id"))
            if item.get("channel") == "telegram" and workstream_id:
                for message_id in item.get("provider_message_ids") or []:
                    value = "telegram:%s:%s" % (item.get("destination"), message_id)
                    references.append((hashlib.sha256(value.encode()).hexdigest(), workstream_id))
        for reference_hash, workstream_id in references:
            connection.execute(
                """INSERT INTO deleted_thread_references(reference_hash, deleted_workstream_id)
                   VALUES (%s, %s) ON CONFLICT (reference_hash) DO NOTHING""",
                (reference_hash, workstream_id),
            )
        for job in jobs:
            if job["object_id"] in removed:
                continue
            owner = connection.execute(
                """SELECT a.workstream_id FROM workstream_attachments a JOIN workstreams w ON w.id = a.workstream_id
                   WHERE a.object_id = %s AND NOT (a.workstream_id = ANY(%s)) AND w.status = 'active'
                   ORDER BY a.workstream_id LIMIT 1""", (job["object_id"], scope),
            ).fetchone()
            if owner is None:
                raise WorkstreamError("a retained job has no surviving workstream")
            connection.execute("UPDATE jobs SET workstream_id = %s WHERE id = %s", (owner["workstream_id"], job["id"]))
            connection.execute("UPDATE workstream_threads SET workstream_id = %s WHERE thread_id = %s",
                               (owner["workstream_id"], job["thread_id"]))
        for table in ("manual_events", "workspace_files", "workspace_document_conversions"):
            ids = [r["id"] for r in manifest["records"][table]]
            connection.execute(sql.SQL("DELETE FROM {} WHERE id = ANY(%s)").format(sql.Identifier(table)), (ids,))
        for table in DERIVED_TABLES:
            ids = [r["id"] for r in manifest["records"][table]]
            connection.execute(sql.SQL("DELETE FROM {} WHERE id = ANY(%s)").format(sql.Identifier(table)), (ids,))
        connection.execute("DELETE FROM workstream_objects WHERE id = ANY(%s)", (removed,))
        connection.execute("DELETE FROM workstream_object_events WHERE object_id = ANY(%s)", (removed,))
        connection.execute(
            """UPDATE workstream_object_events SET actor_job_id = NULL, reason = NULL
               WHERE actor_workstream_id = ANY(%s)""", (scope,),
        )
        connection.execute("DELETE FROM workstreams WHERE id = ANY(%s)", (scope,))
        if manifest["records"]["agent_memories"]:
            connection.execute(
                """INSERT INTO runtime_state(key, value) VALUES ('memory_revision', %s)
                   ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                (Jsonb({"revision": str(uuid.uuid4()), "reason": "workstream deletion"}),),
            )
