"""Durable status and idempotency records for management mutations."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import threading
import secrets

from .management_errors import ApprovalApplicationIncomplete, PERMISSION_ACTIVATION_ERROR
from .setup import PermissionActivationError, SetupError, atomic_write, installation_lock, read_regular


LOGGER = logging.getLogger(__name__)
KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{8,128}")
MAX_OPERATIONS = 500
MAX_PENDING_OPERATIONS = 20
IDEMPOTENCY_RETENTION_DAYS = 30
OPERATION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
DIGEST_PATTERN = re.compile(r"[a-f0-9]{64}")
RESTART_INTENT_PATH = "data/.management-executor-restart.json"
RESTART_PROGRESS = "Restart scheduled; completion unverified until executor returns healthy"
RESTART_EXPIRED_ERROR = "Executor restart could not be verified before its deadline."
CONTROL_PROGRESS = "Control-plane lifecycle running; completion unverified until executor confirms"


def persist_control_plane_pending(root, operation_id):
    root = Path(root)
    path = root / "data/.management-operations.json"
    with installation_lock(root):
        data = json.loads(read_regular(path))
        operation = next(item for item in data["operations"] if item["id"] == operation_id)
        if (operation["action"] not in {"service.restart.management", "service.apply.management"}
                or operation["state"] != "running"):
            raise SetupError("Management control-plane operation is invalid.")
        operation.update(
            state="running", progress=CONTROL_PROGRESS, result=None, error=None,
            verification_deadline=(datetime.now(timezone.utc) + timedelta(seconds=1800)).isoformat(),
            updated_at=timestamp(),
        )
        atomic_write(path, json.dumps(data, separators=(",", ":")).encode())


def expire_control_plane_operations(root):
    root = Path(root)
    path = root / "data/.management-operations.json"
    with installation_lock(root):
        raw = read_regular(path)
        if raw is None:
            return []
        data = json.loads(raw)
        pending = []
        changed = False
        for operation in data["operations"]:
            deadline = operation.get("verification_deadline")
            if operation["state"] != "running" or deadline is None:
                continue
            expires_at = datetime.fromisoformat(deadline)
            if datetime.now(timezone.utc) >= expires_at:
                operation.update(
                    state="failed", progress="Failed", result=None,
                    error=(RESTART_EXPIRED_ERROR if operation["action"] == "service.restart.management-docker"
                           else "Control-plane lifecycle could not be verified before its deadline."),
                    updated_at=timestamp(),
                )
                changed = True
            else:
                pending.append((operation["id"], expires_at))
        if changed:
            atomic_write(path, json.dumps(data, separators=(",", ":")).encode())
        return pending


def restart_intent(root):
    raw = read_regular(Path(root) / RESTART_INTENT_PATH)
    if raw is None:
        return None
    try:
        intent = json.loads(raw)
        expires_at = datetime.fromisoformat(intent["expires_at"])
        if (not OPERATION_ID_PATTERN.fullmatch(intent["operation_id"])
                or not re.fullmatch(r"[a-f0-9]{12,64}", intent["container_id"])
                or not isinstance(intent["project"], str)
                or not isinstance(intent["before_started_at"], str)
                or expires_at.tzinfo is None):
            raise ValueError
        return intent, expires_at
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise SetupError("Pending executor restart state is invalid; preserve it for diagnosis.") from None


def finish_executor_restart(root, operation_id, *, verified=False):
    root = Path(root)
    path = root / "data/.management-operations.json"
    with installation_lock(root):
        pending = restart_intent(root)
        if pending is None or pending[0]["operation_id"] != operation_id:
            return
        expired = datetime.now(timezone.utc) >= pending[1]
        if not verified and not expired:
            return
        data = json.loads(read_regular(path))
        operation = next((item for item in data["operations"] if item["id"] == operation_id), None)
        if (operation is not None and operation["state"] == "running"
                and operation["action"] == "service.restart.management-docker"):
            succeeded = verified and not expired
            operation.update(
                state="succeeded" if succeeded else "failed",
                progress="Completed" if succeeded else "Failed",
                result={"completed": True} if succeeded else None,
                error=None if succeeded else RESTART_EXPIRED_ERROR,
                updated_at=timestamp(),
            )
            atomic_write(path, json.dumps(data, separators=(",", ":")).encode())
        (root / RESTART_INTENT_PATH).unlink()


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def request_digest(action, payload):
    encoded = json.dumps(
        {"action": action, "payload": payload}, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def operation_result(action, result):
    if action == "domain.mutate" or action.startswith("domain."):
        if not isinstance(result, dict):
            raise SetupError("Domain action did not return valid details.")
        if result.get("status") in {"failed", "blocked", "uncertain"}:
            raise SetupError("Domain action did not confirm completion (%s). Inspect current state and delivery logs before retrying." %
                             result["status"])
        identifiers = {"id", "file_id", "project_id", "meeting_id", "assistant_id", "event_id",
                       "log_id", "request_id", "object_version", "path", "sha256", "size_bytes", "status"}
        safe = {key: value for key, value in result.items() if key in identifiers
                and isinstance(value, (str, int, bool))}
        for key in ("contact", "reminder", "event", "meeting", "memory"):
            if isinstance(result.get(key), dict):
                safe[key] = {name: value for name, value in result[key].items()
                             if name in identifiers and isinstance(value, (str, int, bool))}
        return {"completed": True, "data": safe}
    if action == "client.register":
        if not isinstance(result, dict) or not isinstance(result.get("client"), dict):
            raise SetupError("Client registration did not return valid details.")
        return {"completed": True, "client": result["client"]}
    if action == "client.unregister":
        return {"completed": True}
    if action.startswith(("file.upload.project.", "file.attach.project.")):
        action = action.split(".project.", 1)[0]
    if action in {"file.upload", "file.attach"}:
        if (not isinstance(result, dict)
                or any(type(result.get(key)) is not int or result[key] <= 0
                       for key in ("file_id", "workstream_id"))
                or not isinstance(result.get("object_ids"), list)
                or any(type(value) is not int or value <= 0 for value in result["object_ids"])):
            raise SetupError("File operation did not return valid identifiers.")
        return {"completed": True, "file_id": result["file_id"],
                "workstream_id": result["workstream_id"], "object_ids": result["object_ids"]}
    if action in {"search.maintenance", "search.status", "search.probe", "search.reindex"}:
        from .embedding_contracts import EmbeddingTable
        tables = set(EmbeddingTable)
        if not isinstance(result, dict):
            raise SetupError("Search maintenance did not return valid status.")
        dimensions = result.get("dimensions")
        if dimensions is not None and (type(dimensions) is not int or dimensions <= 0):
            raise SetupError("Search maintenance did not return valid dimensions.")
        if result.get("status") == "ok":
            return {"completed": True, "status": "ok", "dimensions": dimensions}
        pending = result.get("pending")
        if (type(result.get("enabled")) is not bool or type(result.get("complete")) is not bool
                or not isinstance(pending, dict) or set(pending) != tables
                or any(type(count) is not int or count < 0 for count in pending.values())):
            raise SetupError("Search maintenance did not return valid status.")
        safe = {"completed": True, "dimensions": dimensions, "enabled": result["enabled"],
                "pending": pending, "complete": result["complete"]}
        for key in ("refreshed", "conflicts"):
            if key in result:
                if type(result[key]) is not int or result[key] < 0:
                    raise SetupError("Search maintenance did not return valid counts.")
                safe[key] = result[key]
        return safe
    if action not in {"task.start", "task.reply", "research.start"}:
        return {"completed": True}
    keys = ("task_id", "request_id", "workstream_id", "research_id") if action == "research.start" else (
        "task_id", "request_id", "workstream_id"
    )
    if not isinstance(result, dict) or any(
        type(result.get(key)) is not int or result[key] <= 0
        for key in keys
    ):
        raise SetupError("Task operation did not return valid identifiers.")
    return {"completed": True, **{
        key: result[key] for key in keys
    }}


class OperationManager:
    def __init__(self, root, execute):
        self.path = Path(root) / "data/.management-operations.json"
        self.execute = execute
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="management-operation")
        self.data = self._load()
        self.restart_watchers = {}
        self._mark_interrupted()

    def _load(self):
        raw = read_regular(self.path)
        if raw is None:
            return {"operations": [], "idempotency": {}}
        try:
            value = json.loads(raw)
            if (not isinstance(value, dict) or not isinstance(value.get("operations"), list)
                    or not isinstance(value.get("idempotency"), dict)):
                raise ValueError
            operation_ids = set()
            for item in value["operations"]:
                if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                        or not OPERATION_ID_PATTERN.fullmatch(item["id"])
                        or item["id"] in operation_ids
                        or not isinstance(item.get("action"), str)
                        or not isinstance(item.get("progress"), str)
                        or item.get("state") not in {"queued", "running", "succeeded", "failed", "interrupted"}
                        or not isinstance(item.get("created_at"), str)
                        or not isinstance(item.get("updated_at"), str)
                        or item.get("error") is not None and not isinstance(item.get("error"), str)
                        or item.get("result") is not None and not isinstance(item.get("result"), dict)):
                    raise ValueError
                if item.get("verification_deadline") is not None:
                    deadline = datetime.fromisoformat(item["verification_deadline"])
                    if (deadline.tzinfo is None
                            or item["action"] not in {
                                "service.restart.management", "service.apply.management",
                                "service.restart.management-docker",
                            }):
                        raise ValueError
                operation_ids.add(item["id"])
            for key, entry in value["idempotency"].items():
                if (not isinstance(key, str) or not KEY_PATTERN.fullmatch(key)
                        or not isinstance(entry, dict)
                        or not isinstance(entry.get("digest"), str)
                        or not DIGEST_PATTERN.fullmatch(entry["digest"])
                        or (entry.get("operation_id") is not None and entry["operation_id"] not in operation_ids)):
                    raise ValueError
                if entry.get("operation_id") is None and not isinstance(entry.get("created_at"), str):
                    raise ValueError
                if "created_at" in entry and datetime.fromisoformat(entry["created_at"]).tzinfo is None:
                    raise ValueError
            return value
        except (json.JSONDecodeError, TypeError, ValueError):
            raise SetupError("Management operation history is invalid; preserve the file for diagnosis.") from None

    def _persist(self):
        with installation_lock(self.path.parent.parent):
            disk = self._load()
            operations = {item["id"]: item for item in disk["operations"]}
            for item in self.data["operations"]:
                previous = operations.get(item["id"])
                if previous is None or item["updated_at"] >= previous["updated_at"]:
                    operations[item["id"]] = item
            self.data["operations"] = sorted(operations.values(), key=lambda item: item["created_at"])
            self.data["idempotency"] = {**disk["idempotency"], **self.data["idempotency"]}
            created = {item["id"]: item["created_at"] for item in self.data["operations"]}
            self.data["operations"] = self.data["operations"][-MAX_OPERATIONS:]
            retained = {item["id"] for item in self.data["operations"]}
            cutoff = datetime.now(timezone.utc) - timedelta(days=IDEMPOTENCY_RETENTION_DAYS)
            idempotency = {}
            for key, entry in self.data["idempotency"].items():
                operation_id = entry.get("operation_id")
                created_at = entry.get("created_at") or created[operation_id]
                if operation_id in retained:
                    idempotency[key] = {**entry, "created_at": created_at}
                elif datetime.fromisoformat(created_at) >= cutoff:
                    idempotency[key] = {**entry, "operation_id": None, "created_at": created_at}
            self.data["idempotency"] = idempotency
            atomic_write(self.path, json.dumps(self.data, separators=(",", ":")).encode("utf-8"))

    def _mark_interrupted(self):
        self._refresh()
        changed = False
        pending = restart_intent(self.path.parent.parent)
        for operation in self.data["operations"]:
            if operation["state"] in {"queued", "running"}:
                if operation["state"] == "running" and operation.get("verification_deadline"):
                    continue
                if (pending and operation["id"] == pending[0]["operation_id"]
                        and operation["action"] == "service.restart.management-docker"
                        and operation["state"] == "running"):
                    continue
                operation["state"] = "interrupted"
                operation["progress"] = "Server restarted before completion"
                operation["updated_at"] = timestamp()
                changed = True
        if changed:
            self._persist()

    def submit(self, action, payload, idempotency_key, audit_action=None, preflight=None):
        if not isinstance(idempotency_key, str) or not KEY_PATTERN.fullmatch(idempotency_key):
            raise SetupError("An Idempotency-Key of 8 to 128 safe characters is required.")
        digest = request_digest(action, payload)
        with self.lock:
            self._refresh()
            previous = self.data["idempotency"].get(idempotency_key)
            if previous:
                if previous.get("digest") != digest:
                    raise SetupError("This Idempotency-Key was already used for a different operation.")
                if previous.get("operation_id") is None:
                    raise SetupError("This operation's status has expired. Inspect current state before retrying.")
                return self.get(previous["operation_id"])
            pending = sum(item["state"] in {"queued", "running"} for item in self.data["operations"])
            if pending >= MAX_PENDING_OPERATIONS:
                raise SetupError("Management operation queue is full; wait for an operation to finish.")
            if preflight is not None:
                preflight()
            now = timestamp()
            operation = {
                "id": secrets.token_urlsafe(18),
                "action": audit_action or action,
                "state": "queued",
                "progress": "Waiting to start",
                "created_at": now,
                "updated_at": now,
                "error": None,
                "result": None,
            }
            self.data["operations"].append(operation)
            self.data["idempotency"][idempotency_key] = {
                "digest": digest, "operation_id": operation["id"], "created_at": now,
            }
            self._persist()
            self.executor.submit(self._run, operation["id"], action, payload)
            return dict(operation)

    def _run(self, operation_id, action, payload):
        self.update(operation_id, state="running", progress="Starting")
        try:
            result = self.execute(operation_id, action, payload, lambda progress: self.update(
                operation_id, state="running", progress=str(progress)[:200],
            ))
            if (action == "service.lifecycle"
                    and (payload.get("service"), payload.get("action")) in {
                        ("management-docker", "restart"), ("management", "restart"), ("management", "apply"),
                    }):
                if self._awaiting_restart(operation_id):
                    return
                raise SetupError("Control-plane lifecycle did not publish a verification intent.")
            result = operation_result(action, result)
        except Exception as error:
            LOGGER.error("Management operation %s failed (%s)", action, type(error).__name__)
            external = self._external_result(operation_id)
            if external and external["state"] in {"succeeded", "failed"}:
                self.update(
                    operation_id, state=external["state"], progress=external["progress"],
                    error=external.get("error"), result=external.get("result"),
                )
                return
            if self._awaiting_restart(operation_id):
                return
            self.update(
                operation_id, state="failed", progress="Failed",
                error=(PERMISSION_ACTIVATION_ERROR if isinstance(error, PermissionActivationError)
                       else ApprovalApplicationIncomplete.MESSAGE if isinstance(error, ApprovalApplicationIncomplete)
                       else "Management operation failed. Refresh current state and check server diagnostics."),
            )
        else:
            if self._awaiting_restart(operation_id):
                return
            self.update(operation_id, state="succeeded", progress="Completed", result=result)

    def _awaiting_restart(self, operation_id):
        with self.lock:
            self._refresh()
            operation = next((item for item in self.data["operations"] if item["id"] == operation_id), None)
            if (operation is not None and operation["action"] in {
                    "service.restart.management", "service.apply.management",
            } and operation.get("verification_deadline")):
                return True
            return (operation is not None
                    and operation["action"] == "service.restart.management-docker"
                    and (operation["progress"] == RESTART_PROGRESS
                         or operation["state"] in {"succeeded", "failed"}))

    def update(self, operation_id, **changes):
        with self.lock:
            operation = next((item for item in self.data["operations"] if item["id"] == operation_id), None)
            if operation is None:
                return
            operation.update(changes)
            operation["updated_at"] = timestamp()
            self._persist()

    def _external_result(self, operation_id):
        with installation_lock(self.path.parent.parent):
            raw = read_regular(self.path)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return next(item for item in data["operations"] if item.get("id") == operation_id)
        except (json.JSONDecodeError, KeyError, StopIteration, TypeError):
            return None

    def _refresh(self):
        with installation_lock(self.path.parent.parent):
            pending = restart_intent(self.path.parent.parent)
        if pending:
            finish_executor_restart(self.path.parent.parent, pending[0]["operation_id"])
        control_pending = expire_control_plane_operations(self.path.parent.parent)
        with installation_lock(self.path.parent.parent):
            disk = self._load()
        operations = {item["id"]: item for item in self.data["operations"]}
        for item in disk["operations"]:
            previous = operations.get(item["id"])
            if previous is None or item["updated_at"] >= previous["updated_at"]:
                operations[item["id"]] = item
        self.data["operations"] = sorted(operations.values(), key=lambda item: item["created_at"])
        self.data["idempotency"] = {**disk["idempotency"], **self.data["idempotency"]}
        if pending and pending[1] > datetime.now(timezone.utc):
            control_pending.append((pending[0]["operation_id"], pending[1]))
        deadlines = {}
        for operation_id, expires_at in control_pending:
            deadlines[operation_id] = min(deadlines.get(operation_id, expires_at), expires_at)
        for operation_id in set(self.restart_watchers) - deadlines.keys():
            self.restart_watchers.pop(operation_id).cancel()
        for operation_id, expires_at in deadlines.items():
            if operation_id not in self.restart_watchers:
                delay = max(0, (expires_at - datetime.now(timezone.utc)).total_seconds())
                timer = threading.Timer(delay + 0.05, self._restart_deadline)
                timer.daemon = True
                self.restart_watchers[operation_id] = timer
                timer.start()

    def _restart_deadline(self):
        with self.lock:
            self._refresh()

    def get(self, operation_id):
        with self.lock:
            self._refresh()
            operation = next((item for item in self.data["operations"] if item["id"] == operation_id), None)
            if operation is None:
                raise SetupError("Management operation was not found.")
            return dict(operation)

    def list(self, limit=50):
        with self.lock:
            self._refresh()
            return [dict(item) for item in reversed(self.data["operations"][-limit:])]


def persist_external_result(root, operation_id, *, succeeded, error=None, result=None):
    path = Path(root) / "data/.management-operations.json"
    with installation_lock(Path(root)):
        raw = read_regular(path)
        try:
            data = json.loads(raw or b"")
            operations = data["operations"]
        except (json.JSONDecodeError, KeyError, TypeError):
            raise SetupError("Management operation state is unavailable.") from None
        operation = next((item for item in operations if item.get("id") == operation_id), None)
        if operation is None:
            raise SetupError("Management operation state is unavailable.")
        if operation.get("verification_deadline"):
            if operation["state"] in {"succeeded", "failed"}:
                return
            if datetime.now(timezone.utc) >= datetime.fromisoformat(operation["verification_deadline"]):
                succeeded, result = False, None
                error = "Control-plane lifecycle could not be verified before its deadline."
        operation["state"] = "succeeded" if succeeded else "failed"
        operation["progress"] = "Completed" if succeeded else "Failed"
        operation["result"] = operation_result(operation["action"], result) if succeeded else None
        operation["error"] = None if succeeded else error
        operation["updated_at"] = timestamp()
        atomic_write(path, json.dumps(data, separators=(",", ":")).encode("utf-8"))
