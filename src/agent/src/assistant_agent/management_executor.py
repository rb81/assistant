"""Private, typed operation runner for Docker-backed management actions."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
import hmac
import json
import logging
import os
from pathlib import Path
import re
import threading
import time

from pydantic import TypeAdapter, ValidationError

from .management_api import ManagementService, read_token, token_path
from .management_contracts import ManagementOperationRequest, ManagementReadRequest
from .management_errors import ApprovalApplicationIncomplete, PERMISSION_ACTIVATION_ERROR
from .management_operations import (
    RESTART_EXPIRED_ERROR, RESTART_PROGRESS, finish_executor_restart,
    operation_result, persist_control_plane_pending, persist_external_result, restart_intent,
)
from .setup import PermissionActivationError, PublicSetupError, SetupError, atomic_write, installation_lock, read_regular


LOGGER = logging.getLogger(__name__)
MAX_BODY = 8 * 1024 * 1024
MAX_CLIENT_READS = 4
OPERATION_ADAPTER = TypeAdapter(ManagementOperationRequest)
READ_ADAPTER = TypeAdapter(ManagementReadRequest)
RESTART_INTENT_PATH = "data/.management-executor-restart.json"
OPERATION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
RESTART_UNVERIFIED_ERROR = (
    "Executor restart was scheduled; completion is unverified until the private executor returns healthy."
)


def persist_restart_intent(root, operation_id, container_id, project, started_at):
    root = Path(root)
    operations_path = root / "data/.management-operations.json"
    intent_path = root / RESTART_INTENT_PATH
    previous = restart_intent(root)
    if previous:
        finish_executor_restart(root, previous[0]["operation_id"])
    with installation_lock(root):
        raw = read_regular(operations_path)
        try:
            data = json.loads(raw or b"")
            operations = data["operations"]
        except (json.JSONDecodeError, KeyError, TypeError):
            raise SetupError("Management operation state is unavailable.") from None
        operation = next((
            item for item in operations if isinstance(item, dict) and item.get("id") == operation_id
        ), None)
        if (not OPERATION_ID_PATTERN.fullmatch(operation_id)
                or not isinstance(operation, dict)
                or operation.get("action") != "service.restart.management-docker"
                or operation.get("state") != "running"):
            raise SetupError("Management executor restart state is invalid.")
        if not re.fullmatch(r"[a-f0-9]{12,64}", container_id) or not isinstance(project, str):
            raise SetupError("Management executor identity is invalid.")
        if not isinstance(started_at, str):
            raise SetupError("Management executor start time is invalid.")
        existing = read_regular(intent_path)
        if existing is not None:
            try:
                expires_at = datetime.fromisoformat(json.loads(existing)["expires_at"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                raise SetupError("Pending executor restart state is invalid; preserve it for diagnosis.") from None
            if datetime.now(timezone.utc) < expires_at:
                raise SetupError("Another executor restart is awaiting verification.")
            intent_path.unlink()
        now = datetime.now(timezone.utc)
        intent = {
            "operation_id": operation_id,
            "container_id": container_id,
            "project": project,
            "before_started_at": started_at,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=120)).isoformat(),
        }
        atomic_write(intent_path, json.dumps(intent, separators=(",", ":")).encode())
        operation.update(
            state="running", progress=RESTART_PROGRESS, error=None, result=None,
            updated_at=now.isoformat(), verification_deadline=intent["expires_at"],
        )
        atomic_write(operations_path, json.dumps(data, separators=(",", ":")).encode())


def _expire_restart_intent(root, operation_id, raw):
    try:
        finish_executor_restart(root, operation_id)
    except Exception as error:
        LOGGER.warning("Expired executor restart status could not be persisted (%s)", type(error).__name__)
        return


def reconcile_executor_restart(root, reader, project, *, wait_seconds=120):
    root = Path(root)
    path = root / RESTART_INTENT_PATH
    with installation_lock(root):
        raw = read_regular(path)
    if raw is None:
        return
    try:
        intent = json.loads(raw)
        operation_id = intent["operation_id"]
        container_id = intent["container_id"]
        expected_project = intent["project"]
        previous_start = intent["before_started_at"]
        expires_at = datetime.fromisoformat(intent["expires_at"])
        if (not OPERATION_ID_PATTERN.fullmatch(operation_id)
                or not re.fullmatch(r"[a-f0-9]{12,64}", container_id)
                or expected_project != project
                or not isinstance(previous_start, str)
                or expires_at.tzinfo is None):
            raise ValueError
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        LOGGER.warning("Pending executor restart state is invalid")
        return
    if datetime.now(timezone.utc) >= expires_at:
        _expire_restart_intent(root, operation_id, raw)
        return
    remaining = max(0, (expires_at - datetime.now(timezone.utc)).total_seconds())
    deadline = time.monotonic() + min(max(0, wait_seconds), remaining)
    while time.monotonic() < deadline:
        try:
            status, _, body = reader.request("/containers/" + container_id + "/json")
            if status == 200:
                container = json.loads(body)
                labels = container.get("Config", {}).get("Labels") or {}
                state = container.get("State") or {}
                health = state.get("Health") or {}
                if (labels.get("com.docker.compose.project") == project
                        and labels.get("com.docker.compose.service") == "management-docker"
                        and state.get("Running") is True
                        and state.get("StartedAt") != previous_start
                        and health.get("Status") == "healthy"):
                    finish_executor_restart(root, operation_id, verified=True)
                    return
        except Exception as error:
            LOGGER.warning("Management executor restart verification is unavailable (%s)", type(error).__name__)
        time.sleep(0.5)
    if datetime.now(timezone.utc) >= expires_at:
        _expire_restart_intent(root, operation_id, raw)


def create_server(address, root, prepare_executor_restart=None):
    root = Path(root).resolve()
    execution_lock = threading.Lock()
    client_reads = threading.BoundedSemaphore(MAX_CLIENT_READS)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, message, *args):
            return

        def respond(self, code, body):
            encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)
            self.wfile.flush()

        def do_POST(self):
            if self.path not in {"/v1/execute", "/v1/read"} or self.headers.get("Origin"):
                self.respond(404, {"error": "Not found"})
                return
            try:
                token = read_token(token_path(root))
            except (SetupError, OSError):
                self.respond(503, {"error": "Executor is not ready"})
                return
            supplied = self.headers.get("Authorization", "").encode()
            expected = ("Bearer " + token).encode()
            if not hmac.compare_digest(supplied, expected):
                self.respond(401, {"error": "Unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 0 < length <= MAX_BODY:
                    raise ValueError
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError
                if self.path == "/v1/read":
                    request = READ_ADAPTER.validate_python(json.loads(raw))
                    operation_id = None
                else:
                    envelope = json.loads(raw)
                    if (not isinstance(envelope, dict) or not isinstance(envelope.get("operation_id"), str)
                            or len(envelope["operation_id"]) > 64):
                        raise ValueError
                    operation_id = envelope["operation_id"]
                    request = OPERATION_ADAPTER.validate_python(envelope.get("operation"))
            except (TypeError, ValueError, json.JSONDecodeError, ValidationError):
                self.respond(422, {"error": "Invalid operation"})
                return
            service = ManagementService(root, token=token)
            if self.path == "/v1/read":
                lock = client_reads if request.request == "clients.read" else execution_lock
                if not lock.acquire(blocking=request.request != "clients.read"):
                    self.respond(503, {"error": "Management client reads are busy; retry."})
                    return
                try:
                    try:
                        result = self.read(service, request)
                    except PublicSetupError as error:
                        LOGGER.warning("Management executor read failed (%s): %s", request.request, error)
                        self.respond(409, {"error": str(error), "public": True})
                    except SetupError:
                        LOGGER.warning("Management executor read failed (%s)", request.request)
                        self.respond(409, {"error": "Management read failed"})
                    except Exception as error:
                        LOGGER.error("Management executor read failed (%s, %s)", request.request, type(error).__name__)
                        self.respond(500, {"error": "Management read failed"})
                    else:
                        self.respond(200, {"result": result})
                finally:
                    lock.release()
                return
            payload = request.model_dump()
            action = payload.pop("operation")
            with execution_lock:
                restart_after_response = None
                restart_pending = False
                try:
                    if (action == "service.lifecycle" and payload.get("service") == "management"
                            and payload.get("action") in {"restart", "apply"}):
                        persist_control_plane_pending(root, operation_id)
                    result = service._execute_operation_local(
                        action, payload, lambda progress: None, operation_id=operation_id,
                    )
                    if result == {"_restart_management_docker": True}:
                        if not callable(prepare_executor_restart):
                            raise SetupError("The management executor cannot safely restart itself.")
                        prepared = prepare_executor_restart()
                        if (not isinstance(prepared, dict)
                                or not isinstance(prepared.get("container_id"), str)
                                or not isinstance(prepared.get("project"), str)
                                or not isinstance(prepared.get("started_at"), str)
                                or not callable(prepared.get("schedule"))):
                            raise SetupError("The management executor restart was not prepared.")
                        persist_restart_intent(
                            root, operation_id, prepared["container_id"], prepared["project"],
                            prepared["started_at"],
                        )
                        restart_after_response = prepared["schedule"]
                        restart_pending = True
                        result = None
                    result = operation_result(action, result)
                except PermissionActivationError:
                    LOGGER.warning("Management permission activation incomplete (%s)", action)
                    persist_external_result(root, operation_id, succeeded=False, error=PERMISSION_ACTIVATION_ERROR)
                    self.respond(409, {"error": PERMISSION_ACTIVATION_ERROR})
                except ApprovalApplicationIncomplete:
                    LOGGER.warning("Management approval application incomplete (%s)", action)
                    persist_external_result(root, operation_id, succeeded=False, error=ApprovalApplicationIncomplete.MESSAGE)
                    self.respond(409, {"error": ApprovalApplicationIncomplete.MESSAGE})
                except SetupError:
                    LOGGER.warning("Management executor operation failed (%s, SetupError)", action)
                    message = "Management operation failed. Refresh current state and check server diagnostics."
                    persist_external_result(root, operation_id, succeeded=False, error=message)
                    self.respond(409, {"error": message})
                except Exception as error:
                    LOGGER.error("Management executor operation failed (%s, %s)", action, type(error).__name__)
                    message = "Management operation failed. Refresh current state and check server diagnostics."
                    persist_external_result(root, operation_id, succeeded=False, error=message)
                    self.respond(500, {"error": message})
                else:
                    if restart_pending:
                        try:
                            self.respond(503, {"error": RESTART_UNVERIFIED_ERROR})
                        finally:
                            restart_after_response()
                    else:
                        persist_external_result(root, operation_id, succeeded=True, result=result)
                        self.respond(200, {"ok": True, "result": result})

        @staticmethod
        def read(service, request):
            payload = request.model_dump()
            name = payload.pop("request")
            if name == "records.list":
                return service.records(payload["collection"], payload["page"], payload["query"])
            if name == "records.read":
                return service.record(payload["collection"], payload["record_id"])
            if name == "records.children":
                return service.record_children(
                    payload["collection"], payload["record_id"], payload["page"],
                )
            if name == "tasks.messages":
                return service.task_messages(payload["task_id"], payload["page"])
            if name == "tasks.activity":
                return service.task_activity(payload["task_id"], payload["since"])
            if name == "conversations.list":
                return service.conversations(payload["page"], payload["query"], payload["channel"])
            if name == "clients.read":
                return service.client_channels(payload["action"], payload["client_id"], payload["message_ids"],
                                               payload["before_id"])
            if name == "domain.read":
                return service.domain_read(request.model_dump())
            if name == "tasks.summary":
                return service.task_summary()
            if name == "tasks.deletion-preview":
                return service.task_deletion_preview(payload["task_id"])
            if name == "credentials.generate-database-password":
                return service.generate_database_password()
            if name == "approvals.list":
                return service.approvals(payload["limit"], payload["offset"])
            if name == "permissions.read":
                return service.permissions(payload["view"])
            if name == "projects.deletion-preview":
                return service.project_deletion_preview(payload["project_id"])
            if name == "projects.pending-deletions":
                return service.pending_deletions()
            if name == "projects.files":
                return service.project_files(payload["project_id"], payload["page"])
            if name == "projects.file-content":
                return service.project_file_content(
                    payload["project_id"], payload["file_id"], payload["offset"], payload["limit"],
                )
            if name == "configuration.check":
                return service.configuration_check(payload["runtime"])
            raise SetupError("Unsupported management read.")

    server = ThreadingHTTPServer(address, Handler)
    server.prepare_executor_restart = prepare_executor_restart
    return server


def main():
    root = Path(os.environ["ASSISTANT_INSTALL_ROOT"])
    create_server(("0.0.0.0", 2376), root).serve_forever()


if __name__ == "__main__":
    main()
