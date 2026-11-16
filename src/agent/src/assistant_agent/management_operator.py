"""Structured, local-only management transport. No terminal presentation."""
import base64
from datetime import datetime
import binascii
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile

import psycopg

from .admin_commands import AdminCommands, redact_secrets
from .config import database_url, load_config
from .database import Database, json_safe
from .email_quotes import strip_quoted_history
from .requests import IncomingRequest, RequestService, TransactionDatabase
from .script_permissions import PermissionRevocationIncomplete
from .management_errors import ApprovalApplicationIncomplete, PERMISSION_ACTIVATION_ERROR
from .tool_result_cache import ToolResultCache
from .workstream_files import WorkstreamFileTracker
from .workstreams import WorkstreamError, WorkstreamStore


FILE_PAGE_SIZE = 50
FILE_CHUNK_SIZE = 1024 * 1024
MAX_UPLOAD_BYTES = 1024 * 1024
COLLECTIONS = {
    "tasks": ("jobs", "id, thread_id, workstream_id, status, task_summary, last_error, "
              "metadata->>'final_response' AS result, metadata->>'source_channel' AS source_channel, "
              "priority, attempts, max_attempts, run_at, has_new_context, created_at, updated_at, completed_at, deleted_at",
              "task_summary"),
    "memories": ("agent_memories", "id, content, tags, scope, kind, importance, confidence, pinned, "
                 "metadata, expires_at, source_job_id, last_accessed_at, created_at, updated_at", "content"),
    "reminders": ("reminders", "id, title, task, run_at, status, priority, recurrence_unit, recurrence_interval, "
                  "recurrence_anchor_day, created_by, created_by_job_id, job_id, metadata, queued_at, completed_at, "
                  "last_error, created_at, updated_at", "title"),
    "projects": ("workstreams", "id, title, status, main_thread_id, parent_id, origin_channel, name_status, created_at", "title"),
    "research": ("deep_research_runs", "id, original_job_id, execution_job_id, title, research_question, "
                 "status, result_summary, last_error, created_at, updated_at", "research_question"),
}
MUTATIONS = {
    "cancel": "cancel", "retry": "requeue", "review": "review", "instruct": "instruct",
    "cancel-reminder": "reminder-cancel", "delete-memory": "memory delete",
    "update-memory": "memory update", "create-memory": "memory create",
    "pin-memory": "memory pin", "unpin-memory": "memory unpin",
    "approve-memory": "memory approve", "reject-memory": "memory reject", "expire-memory": "memory expire",
    "block-script": "permissions block-script", "unblock-script": "permissions unblock-script",
    "revoke-script": "permissions revoke-script",
}


def execute(db, config, request):
    kind = request.get("kind")
    if kind in {"domain.read", "domain.mutate"}:
        from .management_domains import execute as execute_domain
        if kind == "domain.mutate" and request.get("confirmed") is not True:
            raise ValueError("Explicit confirmation is required")
        return execute_domain(db, config, request, mutate=kind == "domain.mutate")
    if kind in {"client.register", "client.unregister", "client.delivery-resolve", "clients.read"}:
        from .client_channels import ClientChannels
        channels = ClientChannels(db, config)
        if kind == "client.register":
            import hashlib
            identifier = request.get("client_id") or hashlib.sha256(request["request_key"].encode()).hexdigest()[:32]
            return channels.register(identifier, request["name"], request["client_type"],
                                     list(dict.fromkeys(request["fallback_channels"])))
        if kind == "client.unregister":
            return channels.unregister(request["client_id"])
        if kind == "client.delivery-resolve":
            if request.get("confirmed") is not True:
                raise ValueError("Explicit confirmation is required")
            return channels.resolve(request["client_id"], request["delivery_id"], request["resolution"])
        action = request["action"]
        if action == "list":
            return channels.list()
        if action == "reconcile":
            return channels.reconcile()
        if action == "poll":
            return channels.poll(request["client_id"])
        if action == "acknowledge":
            return channels.acknowledge(request["client_id"], request["message_ids"])
        if action == "history":
            return channels.history(request["client_id"], request.get("before_id"))
        raise ValueError("Unsupported client channel request")
    page = request.get("page", 0)
    if type(page) is not int or not 0 <= page <= (2**63 - 1) // 8:
        raise ValueError("Invalid page")
    query = request.get("query", "")
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError("Search must be at most 500 characters")
    identifier = request.get("id")
    if identifier is not None and (type(identifier) is not int or not 0 < identifier < 2**63):
        raise ValueError("Invalid record identifier")
    if kind == "task.deletion-preview" and identifier:
        from .task_deletion import TaskDeletionService
        return TaskDeletionService(db).preview(identifier)
    if kind == "task.delete":
        from .task_deletion import TaskDeletionService
        if request.get("confirmed") is not True:
            raise ValueError("Explicit confirmation is required")
        return TaskDeletionService(db).delete(request.get("task_id"), request.get("preview_digest"))
    if kind == "approvals.list":
        from .management_approvals import list_approvals
        limit, offset = request.get("limit", 50), request.get("offset", 0)
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise ValueError("Invalid approval page")
        return {"approvals": list_approvals(db, config, limit=limit, offset=offset),
                "limit": limit, "offset": offset}
    if kind == "approval.decide":
        from .management_approvals import decide_approval
        if request.get("confirmed") is not True:
            raise ValueError("Explicit confirmation is required")
        result = decide_approval(db, config, request.get("approval_id"), request.get("version"),
                                 request.get("decision"), request.get("instruction", ""))
        if not isinstance(result, str) or not result:
            raise ValueError("Approval decision did not return a result")
        return {"result": result}
    if kind in {"task.start", "task.reply", "research.start"}:
        body = request.get("question") if kind == "research.start" else request.get("body")
        key = request.get("request_key")
        if (request.get("confirmed") is not True or not isinstance(body, str) or not body.strip()
                or len(body) > 100_000 or not isinstance(key, str)
                or not key or len(key) > 64 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in key)):
            raise ValueError("A task message and valid operation identifier are required")
        with db.connect() as connection:
            transaction = TransactionDatabase(connection)
            from .client_channels import ClientChannels
            client = ClientChannels(transaction, config).client(request["client_id"])
            thread = "client:" + key
            workstream_id = request.get("workstream_id")
            if kind == "task.reply":
                task_id = request.get("task_id")
                if type(task_id) is not int or task_id <= 0:
                    raise ValueError("Select an existing task")
                task = transaction.fetch_one(
                    """SELECT COALESCE(source.id, j.id) AS id, w.id AS workstream_id,
                              COALESCE(source.thread_id, j.thread_id) AS thread_id
                       FROM jobs j
                       LEFT JOIN deep_research_runs r ON r.execution_job_id = j.id
                       LEFT JOIN jobs source ON source.id = r.original_job_id
                       JOIN workstreams w ON w.id = COALESCE(source.workstream_id, j.workstream_id)
                       WHERE j.id = %s AND w.status = 'active'
                         AND (r.id IS NULL OR source.id IS NOT NULL)""", (task_id,),
                )
                if not task:
                    raise ValueError("Task is unavailable for management replies")
                thread = task["thread_id"]
                workstream_id = task["workstream_id"]
                reply_task_id = task["id"]
                from .task_deletion import TaskDeletionService
                TaskDeletionService(transaction).assert_operable(task_id, reply_task_id)
                transaction.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("request:" + thread,))
                task = transaction.fetch_one(
                    """SELECT j.id FROM jobs j JOIN workstreams w ON w.id = j.workstream_id
                       WHERE j.id = %s AND j.thread_id = %s AND w.status = 'active' FOR UPDATE OF j""",
                    (reply_task_id, thread),
                )
                if not task:
                    raise ValueError("Task is unavailable for management replies")
            elif workstream_id is not None:
                if type(workstream_id) is not int or workstream_id <= 0:
                    raise ValueError("Select an active workstream")
                transaction.execute("SELECT pg_advisory_xact_lock_shared(hashtext('workstream-maintenance'))")
                project = transaction.fetch_one(
                    "SELECT id FROM workstreams WHERE id = %s AND status = 'active' FOR SHARE", (workstream_id,),
                )
                if not project:
                    raise ValueError("Select an active workstream")
                transaction.execute(
                    """INSERT INTO workstream_threads(thread_id, workstream_id, role)
                       VALUES (%s, %s, 'main') ON CONFLICT (thread_id) DO NOTHING""",
                    (thread, workstream_id),
                )
                owner = transaction.fetch_one("SELECT workstream_id FROM workstream_threads WHERE thread_id = %s", (thread,))
                if owner["workstream_id"] != workstream_id:
                    raise ValueError("Task thread belongs to a different workstream")
            file_ids = request.get("attachment_file_ids") or []
            if (not isinstance(file_ids, list) or len(file_ids) > 20
                    or any(type(value) is not int or value <= 0 for value in file_ids)
                    or len(file_ids) != len(set(file_ids))):
                raise ValueError("Select up to 20 distinct project files")
            if file_ids and workstream_id is None:
                raise ValueError("Select a workstream before attaching files")
            attachments = []
            tracker = WorkstreamFileTracker(None, config, None)
            for file_id in file_ids:
                file = transaction.fetch_one(
                    """SELECT f.id, f.path, f.sha256 FROM workstream_files f
                       JOIN workstream_attachments a ON a.object_id = f.object_id
                       WHERE f.id = %s AND a.workstream_id = %s""", (file_id, workstream_id),
                )
                if not file:
                    raise ValueError("Attachment is not available in this workstream")
                path = _public_file_path(file["path"], tracker, config)
                if tracker.shared_root not in path.parents or not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError("Attachment must be an available workspace file")
                attachments.append({"file_id": file_id, "path": str(path.relative_to(tracker.shared_root)),
                                    "sha256": file["sha256"]})
            message = RequestService(transaction).receive_request(IncomingRequest(
                channel="client", external_id=key, conversation_id=thread,
                sender_id=client["id"], thread_id=thread, body=body,
                reply_route={"channel": "client", "client_id": client["id"]},
                attachments=attachments,
            ))
            job = transaction.fetch_one(
                """SELECT id, workstream_id, thread_id, status FROM jobs
                   WHERE thread_id = %s
                     AND (trigger_request_id = %s OR metadata->>'reply_request_id' = %s)
                   ORDER BY id DESC LIMIT 1""",
                (thread, message["id"], str(message["id"])),
            )
            if not job:
                raise ValueError("Task message could not be associated with a job")
            result = {"task_id": job["id"], "workstream_id": job["workstream_id"], "request_id": message["id"]}
            if kind == "research.start":
                from .tools import ToolError, create_deep_research
                existing = transaction.fetch_one(
                    """SELECT id FROM deep_research_runs
                       WHERE original_job_id = %s AND metadata->>'management_operation_id' = %s""",
                    (job["id"], key),
                )
                if existing:
                    result["research_id"] = existing["id"]
                    return result
                if job["status"] != "queued":
                    raise ValueError("Research task is no longer queued")
                title, instructions = request.get("title"), request.get("instructions")
                if (title is not None and (not isinstance(title, str) or len(title) > 500)
                        or instructions is not None and (not isinstance(instructions, str) or len(instructions) > 100_000)):
                    raise ValueError("Invalid research details")
                transaction.execute("SELECT set_config('assistant.job_id', %s, true)", (str(job["id"]),))
                try:
                    run = create_deep_research(
                        transaction, config, job, research_question=body, title=title, instructions=instructions,
                        metadata={"management_operation_id": key},
                    )["deep_research_run"]
                except ToolError as error:
                    raise ValueError(str(error)) from error
                updated = transaction.fetch_one(
                    """UPDATE jobs SET status = 'waiting', last_error = %s, updated_at = now()
                       WHERE id = %s AND status = 'queued' RETURNING id""",
                    (f"Deep research run #{run['id']} is running; waiting for research completion.", job["id"]),
                )
                if not updated:
                    raise ValueError("Research task changed while starting")
                transaction.log_event(job["id"], "status_change", output_data={
                    "status": "waiting", "reason": "deep research created", "deep_research_run_id": run["id"],
                })
                result["research_id"] = run["id"]
            return result
    if kind == "conversations":
        if page > 1_000_000:
            raise ValueError("Conversation page is out of range")
        channel, search = request.get("channel") or "", (request.get("query") or "")[:200]
        if channel not in {"", "client", "telegram", "email"}:
            raise ValueError("Unsupported conversation channel")
        # One row per human conversation thread; jobs from system sources never appear here.
        query = """
            WITH threads AS (
              SELECT thread_id, count(*) AS message_count, max(received_at) AS last_message_at,
                     (array_agg(channel ORDER BY received_at, id))[1] AS channel,
                     (array_agg(body_text ORDER BY received_at, id))[1] AS opening_message
              FROM request_messages
              WHERE (%s = '' OR channel = %s)
              GROUP BY thread_id
              HAVING %s = '' OR bool_or(body_text ILIKE %s)
            )
            SELECT t.thread_id, t.channel, t.message_count, t.opening_message,
                   GREATEST(t.last_message_at, latest.updated_at) AS last_activity_at,
                   latest.id AS latest_task_id, latest.status AS latest_task_status,
                   latest.task_summary AS latest_task_summary, w.title AS workstream_title,
                   w.name_status AS workstream_name_status
            FROM threads t
            JOIN LATERAL (
              SELECT j.id, j.status, j.task_summary, j.updated_at, j.workstream_id FROM jobs j
              WHERE j.thread_id = t.thread_id AND j.deleted_at IS NULL ORDER BY j.id DESC LIMIT 1
            ) latest ON true
            LEFT JOIN workstreams w ON w.id = latest.workstream_id
        """
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        parameters = (channel, channel, search, pattern)
        rows = db.fetch_all(query + " ORDER BY last_activity_at DESC, latest_task_id DESC LIMIT 25 OFFSET %s",
                            (*parameters, page * 25))
        total = db.fetch_one("SELECT count(*) AS total FROM (" + query + ") conversations", parameters)["total"]
        return {"rows": rows, "total": total}
    if kind == "task-messages" and identifier:
        if page > 1_000_000:
            raise ValueError("Message page is out of range")
        task = db.fetch_one(
            """SELECT COALESCE(r.original_thread_id, j.thread_id) AS thread_id
               FROM jobs j LEFT JOIN deep_research_runs r ON r.execution_job_id = j.id
               WHERE j.id = %s""", (identifier,),
        )
        if not task:
            raise ValueError("Task is unavailable")
        thread = task["thread_id"]
        query = """
            SELECT r.id, 'request' AS kind, r.channel, r.body_text AS body, r.received_at AS created_at,
                   r.attachments, r.workstream_id, ARRAY[]::text[] AS recipients, NULL::text AS subject
            FROM request_messages r WHERE r.thread_id = %s
            UNION ALL
            SELECT d.id, d.purpose AS kind, d.channel, d.body_text AS body, d.created_at,
                   d.attachments, d.workstream_id, ARRAY[]::text[] AS recipients, NULL::text AS subject
            FROM channel_deliveries d JOIN jobs j ON j.id = d.job_id
            WHERE (j.thread_id = %s OR EXISTS (
              SELECT 1 FROM deep_research_runs r WHERE r.execution_job_id = j.id AND r.original_thread_id = %s
            )) AND d.channel IN ('client', 'telegram')
               AND (d.status = 'sent' OR (d.channel = 'client' AND d.status = 'pending'))
            UNION ALL
            SELECT o.id, 'message' AS kind, 'email' AS channel, o.body_text AS body, o.created_at,
                   o.attachments, o.workstream_id, o.to_addresses || o.cc_addresses AS recipients, o.subject
            FROM outbound_email_logs o JOIN jobs j ON j.id = o.job_id
            WHERE (j.thread_id = %s OR EXISTS (
              SELECT 1 FROM deep_research_runs r WHERE r.execution_job_id = j.id AND r.original_thread_id = %s
            )) AND o.status = 'sent'
        """
        parameters = (thread, thread, thread, thread, thread)
        rows = db.fetch_all(
            "SELECT * FROM (" + query + ") messages ORDER BY created_at, kind, channel, id LIMIT 50 OFFSET %s",
            (*parameters, page * 50),
        )
        total = db.fetch_one("SELECT count(*) AS total FROM (" + query + ") messages", parameters)["total"]
        for row in rows:
            if row["channel"] == "email":
                row["body"] = strip_quoted_history(row["body"])
            admin = str(config.get("agent.admin.email") or "").strip().casefold()
            row["external"] = any(address.strip().casefold() != admin for address in row["recipients"])
            row["attachments"] = _message_attachments(db, config, row)
            row.pop("workstream_id", None)
        return {"rows": rows, "total": total}
    if kind == "task-activity" and identifier:
        return _task_activity(db, identifier, request.get("since"))
    if kind == "permissions.read":
        from .script_permissions import management
        view = request.get("view")
        if view not in {"hosts", "history"}:
            raise ValueError("Unsupported permission view")
        return {"data": management(db, config, [view])}
    if kind == "permission.control":
        from .script_permissions import management
        action, target = request.get("action"), request.get("target")
        if (request.get("confirmed") is not True
                or action not in {"block-host", "unblock-host", "revoke-grant", "remove-legacy"}
                or not isinstance(target, str) or not target or len(target) > 253):
            raise ValueError("A supported permission change and confirmation are required")
        if action in {"revoke-grant", "remove-legacy"} and (
                not target.isascii() or not target.isdigit() or not 0 < int(target) < 2**63):
            raise ValueError("Select an existing permission record")
        return management(db, config, [action, target], confirmed=True)
    if kind in COLLECTIONS:
        table, columns, search = COLLECTIONS[kind]
        where, values = (f"WHERE {search} ILIKE %s", ["%" + query + "%"]) if query else ("", [])
        if identifier is not None:
            where, values = "WHERE id = %s", [identifier]
        total = db.fetch_one(f"SELECT count(*) AS total FROM {table} {where}", tuple(values))["total"]
        rows = db.fetch_all(f"SELECT {columns} FROM {table} {where} ORDER BY id DESC LIMIT 8 OFFSET %s",
                            (*values, page * 8))
        return {"rows": rows, "total": total}
    if kind == "summary":
        return {"jobs": db.fetch_all("SELECT status, count(*) AS count FROM jobs GROUP BY status ORDER BY status")}
    if kind == "task-events" and identifier:
        rows = db.fetch_all(
            "SELECT sequence, created_at, event_type, tool_name, tool_action, input_data, output_data, tokens_used, duration_ms FROM task_logs WHERE job_id = %s ORDER BY sequence DESC LIMIT 8 OFFSET %s",
            (identifier, page * 8))
        total = db.fetch_one("SELECT count(*) AS total FROM task_logs WHERE job_id = %s", (identifier,))["total"]
        return {"rows": rows, "total": total}
    if kind == "research-events" and identifier:
        if db.fetch_one("SELECT id FROM deep_research_runs WHERE id = %s", (identifier,)) is None:
            raise ValueError("Research run is unavailable")
        rows = db.fetch_all(
            """SELECT id, sequence, created_at, event_type, tool_name, input_data, output_data
               FROM deep_research_events WHERE run_id = %s ORDER BY sequence DESC LIMIT 8 OFFSET %s""",
            (identifier, page * 8),
        )
        total = db.fetch_one(
            "SELECT count(*) AS total FROM deep_research_events WHERE run_id = %s", (identifier,),
        )["total"]
        return {"rows": rows, "total": total}
    if kind == "project-objects" and identifier:
        rows = WorkstreamStore(db, config).objects(identifier)
        return {"rows": rows[page * 8:(page + 1) * 8], "total": len(rows)}
    if kind == "project-files" and identifier:
        if page > 1_000_000:
            raise ValueError("File page is out of range")
        if db.fetch_one("SELECT id FROM workstreams WHERE id = %s AND status = 'active'", (identifier,)) is None:
            raise ValueError("Project is unavailable")
        total = db.fetch_one(
            """SELECT count(*) AS total FROM workstream_objects o
               JOIN workstream_attachments a ON a.object_id = o.id
               JOIN workstream_files f ON f.object_id = o.id
               WHERE a.workstream_id = %s AND o.kind = 'file'""",
            (identifier,),
        )["total"]
        rows = db.fetch_all(
            """SELECT f.id, f.path, f.sha256,
                      count(DISTINCT other.workstream_id) AS retaining_workstreams
               FROM workstream_objects o
               JOIN workstream_attachments a ON a.object_id = o.id
               JOIN workstream_files f ON f.object_id = o.id
               JOIN workstream_attachments other ON other.object_id = o.id
               WHERE a.workstream_id = %s AND o.kind = 'file'
               GROUP BY f.id, f.path, f.sha256
               ORDER BY f.id DESC LIMIT %s OFFSET %s""",
            (identifier, FILE_PAGE_SIZE, page * FILE_PAGE_SIZE),
        )
        tracker = WorkstreamFileTracker(None, config, None)
        files = []
        for row in rows:
            item = {"id": row["id"], "path": _file_label(row["path"], tracker, config),
                    "sha256": row["sha256"], "retaining_workstreams": row["retaining_workstreams"],
                    "available": False, "size_bytes": None}
            try:
                target = _public_file_path(row["path"], tracker, config)
                mode = target.lstat().st_mode
                if stat.S_ISREG(mode):
                    item.update(available=True, size_bytes=target.stat().st_size)
            except (OSError, ValueError):
                pass
            files.append(item)
        return {"files": files, "total": total}
    if kind == "project-file-content" and identifier:
        file_id = request.get("file_id")
        offset = request.get("offset", 0)
        limit = request.get("limit", FILE_CHUNK_SIZE)
        if (type(file_id) is not int or file_id <= 0 or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= FILE_CHUNK_SIZE):
            raise ValueError("Invalid file range")
        row = db.fetch_one(
            """SELECT f.path, f.sha256
               FROM workstream_objects o
               JOIN workstream_attachments a ON a.object_id = o.id
               JOIN workstream_files f ON f.object_id = o.id
               WHERE a.workstream_id = %s AND f.id = %s AND o.kind = 'file'""",
            (identifier, file_id),
        )
        if row is None:
            raise ValueError("Project file is unavailable")
        tracker = WorkstreamFileTracker(None, config, None)
        target = _public_file_path(row["path"], tracker, config)
        return {
            "id": file_id, "path": _file_label(row["path"], tracker, config),
            **read_file_chunk(target, offset, limit),
        }
    if kind in {"file.upload", "file.attach"}:
        if request.get("confirmed") is not True:
            raise ValueError("Explicit confirmation is required")
        workstream_id = request.get("workstream_id")
        if type(workstream_id) is not int or workstream_id <= 0:
            raise ValueError("Invalid project")
        store = WorkstreamStore(db, config)
        if kind == "file.attach":
            file_id = request.get("file_id")
            if type(file_id) is not int or file_id <= 0:
                raise ValueError("Invalid file identifier")
            file = db.fetch_one("SELECT path FROM workstream_files WHERE id = %s", (file_id,))
            if file is None:
                raise ValueError("File is unavailable")
            target = _public_file_path(file["path"], WorkstreamFileTracker(None, config, None), config)
            if not stat.S_ISREG(target.lstat().st_mode):
                raise ValueError("File is unavailable")
            result = store.attach(workstream_id, "file", str(file_id))
            return {"workstream_id": workstream_id, "file_id": file_id, **result}
        path = request.get("path")
        encoded = request.get("content_base64")
        if not isinstance(path, str) or not path or len(path) > 1024:
            raise ValueError("Invalid upload path")
        if not isinstance(encoded, str) or len(encoded) > ((MAX_UPLOAD_BYTES + 2) // 3) * 4:
            raise ValueError("Upload exceeds the 1 MiB limit")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("Upload content must be valid base64") from None
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError("Upload exceeds the 1 MiB limit")
        relative = PurePosixPath(path)
        if (relative.is_absolute() or "\\" in path or not relative.parts
                or any(part in {".", "..", ".assistant"} for part in relative.parts)):
            raise ValueError("Upload path must be a relative workspace path")
        root = Path(config.get("agent.filesystem.shared_root", "/data/share")).resolve()
        tracker = WorkstreamFileTracker(db, config, None)
        target = _public_file_path(root.joinpath(*relative.parts), tracker, config)
        if not target.parent.is_dir():
            raise ValueError("Upload directory must already exist")
        from .artifact_processor import ArtifactProcessor
        with tempfile.NamedTemporaryFile(prefix="assistant-management-upload-") as scan_file:
            scan_file.write(content)
            scan_file.flush()
            scan = ArtifactProcessor(db, config).scan_file(Path(scan_file.name))
        if scan.get("status") != "clean":
            raise ValueError("Upload requires a successful clean malware scan")
        upload_tracker = WorkstreamFileTracker(db.for_workstream(workstream_id), config, None)
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
            upload_tracker.track_file(target)
            result = store.attach(workstream_id, "file", str(target))
            file = db.fetch_one("SELECT id FROM workstream_files WHERE path = %s", (str(target),))
        except (OSError, ValueError, WorkstreamError, psycopg.Error):
            target.unlink(missing_ok=True)
            db.execute(
                """DELETE FROM workstream_files f
                   WHERE f.path = %s AND NOT EXISTS (
                     SELECT 1 FROM workstream_attachments a WHERE a.object_id = f.object_id
                   )""",
                (str(target),),
            )
            raise
        return {
            "workstream_id": workstream_id, "file_id": file["id"],
            "path": _file_label(str(target), tracker, config), **result,
        }
    if kind == "scripts":
        from .script_permissions import management
        return management(db, config, ["scripts"], page=page, query=query)
    if kind == "script" and identifier:
        from .script_permissions import management
        detail = management(db, config, ["show", str(identifier)])
        script = db.fetch_one("SELECT id, entry, blocked FROM sandbox_scripts WHERE id = %s",
                              (detail["version"]["script_id"],))
        if not script:
            raise ValueError("Script no longer exists")
        return {**detail, **script}
    if kind == "mutate":
        action = request.get("action")
        if action not in MUTATIONS or request.get("confirmed") is not True:
            raise ValueError("A supported action and explicit confirmation are required")
        if action != "create-memory" and identifier is None:
            raise ValueError("Select an existing record")
        body = request.get("body", "")
        if not isinstance(body, str) or len(body) > 100_000:
            raise ValueError("Invalid text")
        if action in {"create-memory", "update-memory", "instruct"} and not body.strip():
            raise ValueError("Text cannot be empty")
        subject = "/admin " + MUTATIONS[action] + (f" {identifier}" if identifier else "")
        if action in {"cancel", "retry", "review", "instruct"}:
            from .task_deletion import TaskDeletionService
            with db.connect() as connection:
                transaction = TransactionDatabase(connection)
                TaskDeletionService(transaction).assert_operable(identifier)
                result = json.loads(AdminCommands(transaction, config).execute(subject, body, confirmed=True))
        else:
            result = json.loads(AdminCommands(db, config).execute(subject, body, confirmed=True))
        if result is None or result is False:
            raise ValueError("The record was not changed; refresh its current state")
        return {"result": result}
    raise ValueError("Unsupported management request")


ACTIVITY_LIMIT = 200
ACTIVITY_PREVIEW_CHARACTERS = 4000
# Sending the reply is already visible as a message, so it is not repeated as a tool call.
ACTIVITY_HIDDEN_TOOLS = ("reply_send", "email_send")


def read_file_chunk(target, offset, limit):
    """Return a range and live digest from the same regular file, not stale tracking metadata."""
    descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("File is unavailable")
        digest = hashlib.sha256()
        for block in iter(lambda: handle.read(FILE_CHUNK_SIZE), b""):
            digest.update(block)
        position = min(offset, before.st_size)
        handle.seek(position)
        content = handle.read(limit)
        after = os.fstat(handle.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("File changed during download; refresh before retrying")
    next_offset = position + len(content)
    return {
        "sha256": digest.hexdigest(), "offset": position, "next_offset": next_offset,
        "size_bytes": before.st_size, "truncated": next_offset < before.st_size,
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def _message_attachments(db, config, message):
    """Expose contained download references, never arbitrary server paths or embedded content."""
    tracker = WorkstreamFileTracker(None, config, None)
    attachments = []
    for raw in message["attachments"]:
        if not isinstance(raw, dict):
            continue
        item = {key: raw[key] for key in ("filename", "content_type", "size_bytes", "voice_note") if key in raw}
        item["available"] = False
        path = raw.get("path")
        if path:
            try:
                target = _public_file_path(
                    str(tracker.shared_root / path) if not Path(path).is_absolute() else path, tracker, config,
                )
                if not target.is_file():
                    raise ValueError("Attachment file is unavailable")
                file = db.fetch_one(
                    """SELECT f.id FROM workstream_files f
                       JOIN workstream_attachments a ON a.object_id = f.object_id
                       WHERE a.workstream_id = %s AND f.path = %s LIMIT 1""",
                    (message["workstream_id"], str(target)),
                )
                if file:
                    item.update(file_id=file["id"], project_id=message["workstream_id"], available=True)
                elif tracker.shared_root in target.parents:
                    item.update(workspace_path=str(target.relative_to(tracker.shared_root)), available=True)
                item.setdefault("filename", target.name)
            except (OSError, ValueError):
                pass
        attachments.append(item)
    return attachments


def _activity_preview(value):
    if value in (None, {}, []):
        return None
    text = json.dumps(json_safe(value), ensure_ascii=False, default=str)
    return value if len(text) <= ACTIVITY_PREVIEW_CHARACTERS else text[:ACTIVITY_PREVIEW_CHARACTERS] + "…"


def _task_activity(db, identifier, since=None):
    """Tool calls across every task in the conversation, oldest first, each paired with its result."""
    if since is not None:
        if not isinstance(since, str) or len(since) > 40:
            raise ValueError("Activity start time is invalid")
        try:
            datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Activity start time is invalid") from None
    task = db.fetch_one(
        """SELECT COALESCE(r.original_thread_id, j.thread_id) AS thread_id
           FROM jobs j LEFT JOIN deep_research_runs r ON r.execution_job_id = j.id
           WHERE j.id = %s""", (identifier,),
    )
    if not task:
        raise ValueError("Task is unavailable")
    thread = task["thread_id"]
    events = db.fetch_all(
        """SELECT l.job_id, l.sequence, l.created_at, l.event_type, l.tool_name, l.tool_action,
                  l.input_data, l.output_data, l.duration_ms, j.status AS task_status
           FROM task_logs l JOIN jobs j ON j.id = l.job_id
           WHERE (j.thread_id = %s OR EXISTS (
             SELECT 1 FROM deep_research_runs r WHERE r.execution_job_id = j.id AND r.original_thread_id = %s
           )) AND l.tool_name IS NOT NULL AND l.event_type IN ('tool_call', 'tool_result', 'error')
             AND NOT (l.tool_name = ANY(%s)) AND (%s::timestamptz IS NULL OR l.created_at >= %s::timestamptz)
           ORDER BY l.created_at DESC, l.job_id DESC, l.sequence DESC LIMIT %s""",
        (thread, thread, list(ACTIVITY_HIDDEN_TOOLS), since, since, ACTIVITY_LIMIT * 2 + 1),
    )
    truncated = len(events) > ACTIVITY_LIMIT * 2
    calls, pending = [], {}
    for event in reversed(events[:ACTIVITY_LIMIT * 2]):
        key = (event["job_id"], event["tool_name"])
        if event["event_type"] == "tool_call":
            # Tools execute serially within a job. A new call proves the previous unpaired call stopped.
            for previous in [key for key in pending if key[0] == event["job_id"]]:
                pending.pop(previous)["status"] = "interrupted"
        if event["event_type"] != "tool_call" and pending.get(key):
            call = pending.pop(key)
        else:
            call = {
                "id": "%s:%s" % (event["job_id"], event["sequence"]), "task_id": event["job_id"],
                "created_at": event["created_at"], "tool_name": event["tool_name"],
                "tool_action": event["tool_action"],
                "status": "running" if event["task_status"] == "running" else "interrupted",
                "input": None, "output": None,
                "duration_ms": None,
            }
            calls.append(call)
        call["input"] = call["input"] or _activity_preview(event["input_data"])
        if event["event_type"] == "tool_call":
            pending[key] = call
        else:
            call["status"] = "failed" if event["event_type"] == "error" else "succeeded"
            call["output"] = _activity_preview(event["output_data"])
            call["duration_ms"] = event["duration_ms"]
    calls = calls[-ACTIVITY_LIMIT:]
    return {"rows": calls, "total": len(calls), "truncated": truncated}


def _file_label(path, tracker, config):
    try:
        target = _public_file_path(path, tracker, config)
    except (OSError, ValueError):
        return "unavailable"
    for label, root in (("workspace", tracker.shared_root), ("artifacts", tracker.raw_root)):
        if target != root and root in target.parents:
            return str(PurePosixPath(label, *target.relative_to(root).parts))
    return "unavailable"


def _public_file_path(path, tracker, config):
    target = tracker._safe_path(Path(path))
    if target != tracker.shared_root and tracker.shared_root in target.parents:
        relative = target.relative_to(tracker.shared_root)
        if ".assistant" in relative.parts:
            raise ValueError("internal workspace files are not available through management")
    archive = Path(str(config.get(
        "agent.workspace.source_archive_root", ".assistant/archive/source-documents",
    ) or ".assistant/archive/source-documents"))
    archive = archive if archive.is_absolute() else tracker.shared_root / archive
    cache = ToolResultCache(config).root
    trash = Path(str(config.get(
        "agent.filesystem.trash_directory", str(tracker.shared_root / ".trash"),
    ))).resolve()
    if any(target == root or root in target.parents for root in (archive.resolve(), cache, trash)):
        raise ValueError("internal workspace files are not available through management")
    return target


def main():
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError("Expected a management request")
        config = load_config()
        result = execute(Database(database_url(config)), config, request)
        print(json.dumps(redact_secrets(json_safe(result))))
    except PermissionRevocationIncomplete:
        print(json.dumps({"code": "permission_activation_incomplete", "error": PERMISSION_ACTIVATION_ERROR}))
        raise SystemExit(1) from None
    except ApprovalApplicationIncomplete:
        print(json.dumps({"code": "approval_application_incomplete", "error": ApprovalApplicationIncomplete.MESSAGE}))
        raise SystemExit(1) from None
    except (ValueError, TypeError, KeyError, RuntimeError, psycopg.Error, OSError):
        print(json.dumps({"error": "Management request failed. Check service health and migrations, "
                                   "then refresh the record before retrying. No success was confirmed."}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
