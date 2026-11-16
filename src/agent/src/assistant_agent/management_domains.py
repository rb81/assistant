"""Explicit user-facing domain actions backed by existing platform services."""
import base64
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid
from typing import Any, Literal, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, create_model


READ_ACTIONS = {
    "contacts.search": "contact_search", "contacts.read": "contact_read",
    "reminders.list": "reminder_list",
    "calendar.calendars": "calendar_list_calendars", "calendar.events": "calendar_list_events",
    "calendar.busy": "calendar_list_busy",
    "email.search": "email_search", "email.read": "email_read",
    "memory.search": "memory_search",
    "workspace.search": "file_search", "workspace.semantic-search": "file_semantic_search",
    "zoom.list": "zoom_list_meetings", "zoom.read": "zoom_read_meeting",
    "dropbox.list": "dropbox_list", "context.search": "context_search",
    "capabilities.read": "runtime_capabilities",
}
WRITE_ACTIONS = {
    "contacts.create": "contact_create", "contacts.update": "contact_update", "contacts.delete": "contact_delete",
    "reminders.create": "reminder_create", "reminders.update": "reminder_update", "reminders.cancel": "reminder_cancel",
    "calendar.create": "calendar_create_event", "calendar.update": "calendar_update_event",
    "calendar.delete": "calendar_delete_event", "calendar.sync": "calendar_sync",
    "email.send": "email_send", "telegram.send": "telegram_send",
    "memory.remember": "memory_remember", "memory.update": "memory_update", "memory.forget": "memory_forget",
    "workspace.move": "file_move", "workspace.copy": "file_copy", "workspace.delete": "file_delete",
    "workspace.convert": "file_convert",
    "zoom.create": "zoom_create_meeting", "zoom.update": "zoom_update_meeting",
    "zoom.delete": "zoom_delete_meeting", "zoom.reconcile": "zoom_reconcile_meeting",
    "dropbox.import": "dropbox_import", "dropbox.export": "dropbox_export", "dropbox.delete": "dropbox_delete",
    "scripts.execute": "command_execute",
}
ReadAction = Literal[
    "catalog", "contacts.search", "contacts.read", "reminders.list",
    "calendar.calendars", "calendar.events", "calendar.busy", "email.search", "email.read",
    "memory.search", "workspace.search", "workspace.semantic-search", "workspace.list", "workspace.read",
    "zoom.list", "zoom.read", "dropbox.list", "context.search", "capabilities.read",
]
WriteAction = Literal[
    "contacts.create", "contacts.update", "contacts.delete",
    "reminders.create", "reminders.update", "reminders.cancel",
    "calendar.create", "calendar.update", "calendar.delete", "calendar.sync",
    "email.send", "telegram.send", "memory.remember", "memory.update", "memory.forget",
    "workspace.write", "workspace.mkdir", "workspace.move", "workspace.copy", "workspace.delete",
    "workspace.convert", "zoom.create", "zoom.update", "zoom.delete", "zoom.reconcile",
    "dropbox.import", "dropbox.export", "dropbox.delete", "scripts.execute",
    "projects.create", "projects.rename", "projects.archive", "projects.restore", "projects.attach",
]
VERSIONED = {
    "contacts.update", "contacts.delete", "reminders.update", "reminders.cancel",
    "calendar.update", "calendar.delete", "memory.update", "memory.forget",
    "zoom.update", "zoom.delete",
}
LOCAL_READ_ACTIONS = {"email.search", "email.read", "contacts.search", "contacts.read", "reminders.list"}

class ProjectReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    project_id: int = Field(gt=0)


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=500)


class ProjectRename(ProjectReference):
    title: str = Field(min_length=1, max_length=500)


class ProjectAttach(ProjectReference):
    kind: Literal["file", "contact"]
    key: str = Field(min_length=1, max_length=1024)


PROJECT_ARGUMENTS = {
    "projects.create": ProjectCreate, "projects.rename": ProjectRename,
    "projects.archive": ProjectReference, "projects.restore": ProjectReference,
    "projects.attach": ProjectAttach,
}


def argument_model(action, method):
    hints = get_type_hints(method)
    fields = {
        name: (hints.get(name, Any), parameter.default if parameter.default is not inspect.Parameter.empty else ...)
        for name, parameter in inspect.signature(method).parameters.items()
        if name != "self"
    }
    if action in VERSIONED:
        fields["expected_version"] = (int, Field(gt=0))
    return create_model(action.replace(".", "_"), __config__=ConfigDict(extra="forbid", strict=True), **fields)


class WorkspaceFiles:
    def __init__(self, runtime):
        self.runtime = runtime

    def path(self, value):
        from .tools import ToolError
        try:
            self.runtime.reject_symlink_path(value)
        except ToolError as error:
            raise ValueError(str(error)) from error
        path = self.runtime.resolve_path(value)
        if path != self.runtime.shared_root:
            from .management_operator import _public_file_path
            path = _public_file_path(path, self.runtime.workstream_files, self.runtime.config)
        return path

    def list(self, path: str = ".", offset: int = 0, limit: int = 100):
        target = self.path(path)
        if not target.is_dir() or not 0 <= offset <= 1_000_000 or not 1 <= limit <= 1000:
            raise ValueError("Select a workspace directory and valid page")
        items = sorted(target.iterdir(), key=lambda item: item.name)
        visible = []
        for item in items:
            try:
                safe = self.path(str(item))
            except ValueError:
                continue
            visible.append({
                "path": str(safe.relative_to(self.runtime.shared_root)),
                "is_dir": safe.is_dir(), "size_bytes": safe.stat().st_size,
            })
        return {"entries": visible[offset:offset + limit], "total": len(visible)}

    @staticmethod
    def digest(path):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def read(self, path: str, offset: int = 0, limit: int = 1_048_576):
        from .management_operator import read_file_chunk
        target = self.path(path)
        if not target.is_file() or offset < 0 or not 1 <= limit <= 1_048_576:
            raise ValueError("Select a workspace file and valid byte range")
        return {"path": str(target.relative_to(self.runtime.shared_root)), **read_file_chunk(target, offset, limit)}

    def mkdir(self, path: str):
        target = self.path(path)
        self.runtime.check_write_allowed(target)
        target.mkdir(parents=False, exist_ok=False)
        return {"path": str(target.relative_to(self.runtime.shared_root))}

    def write(self, path: str, content_base64: str, expected_sha256: str | None = None, offset: int = 0):
        target = self.path(path)
        self.runtime.check_write_allowed(target)
        if target == self.runtime.shared_root or not target.parent.is_dir() or offset < 0:
            raise ValueError("Select a workspace file in an existing directory")
        if len(content_base64) > 1_398_104:
            raise ValueError("Write chunks are limited to 1 MiB")
        content = base64.b64decode(content_base64, validate=True)
        if len(content) > 1_048_576:
            raise ValueError("Write chunks are limited to 1 MiB")
        existing = target.exists()
        if existing:
            if not target.is_file() or not expected_sha256 or self.digest(target) != expected_sha256:
                raise ValueError("File changed; read its current digest before editing")
            if offset and offset != target.stat().st_size:
                raise ValueError("Append offset must match the current file size")
        elif expected_sha256 is not None or offset:
            raise ValueError("File is missing; create it with offset zero and no previous digest")
        from .artifact_processor import ArtifactProcessor
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".management-write-", delete=False) as handle:
                temporary = Path(handle.name)
                if offset:
                    with target.open("rb") as original:
                        shutil.copyfileobj(original, handle)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if ArtifactProcessor(self.runtime.db, self.runtime.config).scan_file(temporary).get("status") != "clean":
                raise ValueError("Writing a file requires a successful clean malware scan")
            self.runtime.workstream_files.register_path(target)
            if existing:
                if self.digest(target) != expected_sha256:
                    raise ValueError("File changed while the write was being scanned")
                os.replace(temporary, target)
            else:
                os.link(temporary, target)
            self.runtime.workstream_files.track_file(target)
            self.runtime.normalize_and_index_path(target, source="management")
            return {"path": str(target.relative_to(self.runtime.shared_root)),
                    "sha256": self.digest(target), "size_bytes": target.stat().st_size}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def catalog():
    from .tools import ToolRuntime
    rows = []
    for mapping, access in ((READ_ACTIONS, "read"), (WRITE_ACTIONS, "write")):
        for action, method in mapping.items():
            rows.append({"action": action, "access": access,
                         "requires_task": action in {"scripts.execute", "dropbox.import", "dropbox.export"},
                         "arguments": argument_model(action, getattr(ToolRuntime, method)).model_json_schema()})
    for action, method in (("workspace.list", WorkspaceFiles.list), ("workspace.read", WorkspaceFiles.read),
                          ("workspace.write", WorkspaceFiles.write), ("workspace.mkdir", WorkspaceFiles.mkdir)):
        rows.append({"action": action, "access": "read" if action in {"workspace.list", "workspace.read"} else "write",
                     "arguments": argument_model(action, method).model_json_schema()})
    rows.extend({"action": action, "access": "write", "arguments": model.model_json_schema()}
                for action, model in PROJECT_ARGUMENTS.items())
    return {"actions": rows}


def execute(db, config, payload, *, mutate=False):
    from .tools import ToolRuntime
    action = payload["action"]
    if action == "catalog" and not mutate:
        return catalog()
    arguments = payload.get("arguments", {})
    if len(json.dumps(arguments).encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("Domain arguments exceed 2 MiB")
    if action in PROJECT_ARGUMENTS and mutate:
        if any(payload.get(key) is not None for key in ("task_id", "workstream_id", "client_id")):
            raise ValueError("Project actions select their context through project_id only")
        return project_action(db, action, arguments)
    mapping = WRITE_ACTIONS if mutate else READ_ACTIONS
    if action in {"workspace.list", "workspace.read", "workspace.write", "workspace.mkdir"}:
        if mutate != (action in {"workspace.write", "workspace.mkdir"}):
            raise ValueError("Unsupported workspace action")
        argument_method = getattr(WorkspaceFiles, action.split(".")[1])
    elif action in mapping:
        argument_method = getattr(ToolRuntime, mapping[action])
    else:
        raise ValueError("Unsupported management domain action")
    cleaned = argument_model(action, argument_method).model_validate(arguments).model_dump(exclude_unset=True)
    task_id = payload.get("task_id")
    if action in {"scripts.execute", "dropbox.import", "dropbox.export"} and not task_id:
        raise ValueError("This operation requires an existing task for its approval and transfer context")
    if task_id:
        job = db.fetch_one(
            """SELECT j.* FROM jobs j JOIN workstreams w ON w.id = j.workstream_id
               WHERE j.id = %s AND j.status != 'deleted' AND w.status = 'active'""", (task_id,),
        )
        if not job:
            raise ValueError("Select an available task")
        if payload.get("workstream_id") is not None and payload["workstream_id"] != job["workstream_id"]:
            raise ValueError("The selected task does not belong to the selected project")
    else:
        workstream_id = payload.get("workstream_id")
        if mutate and workstream_id is None:
            workstream_id = db.fetch_one(
                "SELECT ensure_workstream('platform-management', 'Platform management') AS id",
            )["id"]
        if workstream_id:
            project = db.fetch_one("SELECT id FROM workstreams WHERE id = %s AND status = 'active'", (workstream_id,))
            if not project:
                raise ValueError("Select an active project")
            db = db.for_workstream(workstream_id)
        job = {"id": None, "thread_id": f"management:{workstream_id or 'platform'}",
               "workstream_id": workstream_id, "metadata": {"source_channel": "local"}}
    if payload.get("client_id"):
        from .client_channels import ClientChannels
        ClientChannels(db, config).client(payload["client_id"])
        job = {**job, "metadata": {**(job.get("metadata") or {}),
                                  "reply_route": {"channel": "client", "client_id": payload["client_id"]}}}
    if mutate:
        job = {**job, "metadata": {**(job.get("metadata") or {}), "admin_authenticated": True,
                                  "reply_request_id": payload.get("request_key") or uuid.uuid4().hex}}
    runtime = ToolRuntime(db, config, job)
    if action == "memory.forget" and mutate:
        from .memory_store import MemoryStore
        with runtime.db.object_version_transaction("memory", str(cleaned["memory_id"]), cleaned["expected_version"]):
            row = MemoryStore(runtime.db, config).delete(
                memory_id=cleaned["memory_id"], reason="Deleted through the authenticated Management API",
                job_id=job["id"], actor="management",
            )
        return {"deleted": row}
    if action in {"dropbox.list", "dropbox.delete"}:
        from .dropbox_provider import DropboxClient
        client = DropboxClient(config, db)
        if not mutate and action == "dropbox.list":
            return client.list_files(cleaned.get("folder", "Inbox"), cleaned.get("cursor"))
        if mutate and action == "dropbox.delete":
            return client.delete_file(cleaned["path"], cleaned["revision"], cleaned["folder"])
    if action in {"workspace.list", "workspace.read", "workspace.write", "workspace.mkdir"}:
        method = getattr(WorkspaceFiles(runtime), action.split(".")[1])
        return method(**cleaned)
    method = mapping[action]
    if action in {"workspace.copy", "workspace.move", "workspace.delete"}:
        files = WorkspaceFiles(runtime)
        for key in ("source_path", "destination_path", "path"):
            if key in cleaned:
                target = files.path(cleaned[key])
                if target.is_dir() and any(item.is_symlink() for item in target.rglob("*")):
                    raise ValueError("Directories containing symlinks cannot be copied, moved or deleted")
    return getattr(runtime, method)(**cleaned) if action in LOCAL_READ_ACTIONS and not mutate else runtime.run(method, cleaned)


def project_action(db, action, arguments):
    from .workstreams import WorkstreamStore
    data = PROJECT_ARGUMENTS[action].model_validate(arguments)
    if action == "projects.create":
        if not data.title.strip():
            raise ValueError("A new project requires a title")
        import uuid
        row = db.fetch_one("SELECT ensure_workstream(%s, %s) AS id",
                           ("client-project:" + uuid.uuid4().hex, data.title))
        return {"project_id": row["id"]}
    if action == "projects.attach":
        return WorkstreamStore(db).attach(data.project_id, data.kind, data.key)
    if action == "projects.rename":
        if not data.title.strip():
            raise ValueError("A project title is required")
        row = db.fetch_one(
            "UPDATE workstreams SET title = %s WHERE id = %s AND status = 'active' RETURNING id",
            (data.title, data.project_id),
        )
    else:
        status, previous = ("archived", "active") if action == "projects.archive" else ("active", "archived")
        row = db.fetch_one(
            """UPDATE workstreams SET status = %s WHERE id = %s AND status = %s
               AND NOT EXISTS (SELECT 1 FROM jobs WHERE workstream_id = %s
                 AND status IN ('queued', 'running', 'waiting', 'needs_review'))
               AND NOT EXISTS (SELECT 1 FROM reminders r JOIN workstream_objects o ON o.id = r.object_id
                 WHERE o.origin_workstream_id = %s AND r.status IN ('scheduled', 'queued')) RETURNING id""",
            (status, data.project_id, previous, data.project_id, data.project_id),
        )
    if not row:
        raise ValueError("Project is unavailable or has unfinished tasks or reminders")
    return {"project_id": row["id"]}
