import os
import json
import http.client
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


SHARED_ROOT = Path(os.getenv("SANDBOX_SHARED_ROOT", "/data/share")).resolve()
RUN_SHARED_ROOT = Path(os.getenv("SANDBOX_RUN_SHARED_ROOT", str(SHARED_ROOT))).resolve()
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("SANDBOX_DEFAULT_TIMEOUT_SECONDS", "300"))
MAX_TIMEOUT_SECONDS = int(os.getenv("SANDBOX_MAX_TIMEOUT_SECONDS", "300"))
DEFAULT_SHARED_FILE_UMASK = "0002"
ISOLATION_MODE = os.getenv("SANDBOX_ISOLATION_MODE", "container").strip().lower()
DOCKER_SOCKET = os.getenv("SANDBOX_DOCKER_SOCKET", "/var/run/docker.sock")
RUN_IMAGE = os.getenv("SANDBOX_RUN_IMAGE", "assistant-sandbox:latest").strip()
RUN_RUNTIME = os.getenv("SANDBOX_RUN_RUNTIME", "").strip()
RUN_NETWORK_MODE = os.getenv("SANDBOX_RUN_NETWORK_MODE", "per_run_bridge").strip().lower()
RUN_NETWORK = os.getenv("SANDBOX_RUN_NETWORK", "").strip()
RUN_USER = os.getenv("SANDBOX_RUN_USER", "").strip()
RUN_MEMORY_BYTES = int(os.getenv("SANDBOX_RUN_MEMORY_BYTES", str(2 * 1024 * 1024 * 1024)))
RUN_NANO_CPUS = int(float(os.getenv("SANDBOX_RUN_CPUS", "2")) * 1_000_000_000)
RUN_PIDS_LIMIT = int(os.getenv("SANDBOX_RUN_PIDS_LIMIT", "512"))
RUN_TMPFS_SIZE = os.getenv("SANDBOX_RUN_TMPFS_SIZE", "512m").strip()
RUN_DNS_SERVERS = [item.strip() for item in os.getenv("SANDBOX_RUN_DNS_SERVERS", "").split(",") if item.strip()]
RUN_CAP_DROP = [item.strip() for item in os.getenv("SANDBOX_RUN_CAP_DROP", "NET_RAW").split(",") if item.strip()]
RUN_SECURITY_OPT = [
    item.strip()
    for item in os.getenv("SANDBOX_RUN_SECURITY_OPT", "no-new-privileges:true").split(",")
    if item.strip()
]
SHARED_ROOT_HOST = os.getenv("SANDBOX_SHARED_ROOT_HOST", "").strip()
SHARED_VOLUME_NAME = os.getenv("SANDBOX_SHARED_VOLUME_NAME", "").strip()
RUN_CONTAINER_PREFIX = os.getenv("SANDBOX_RUN_CONTAINER_PREFIX", "assistant-sandbox-run").strip()
RUN_NETWORK_PREFIX = os.getenv("SANDBOX_RUN_NETWORK_PREFIX", "assistant-sandbox-net").strip()


def parse_umask(value: str) -> int:
    clean = str(value or "").strip()
    if clean.startswith("0o"):
        clean = clean[2:]
    if not clean:
        clean = DEFAULT_SHARED_FILE_UMASK
    try:
        parsed = int(clean, 8)
    except ValueError as exc:
        raise RuntimeError("SHARED_FILE_UMASK must be an octal value such as 0002 or 0022") from exc
    if parsed < 0 or parsed > 0o777:
        raise RuntimeError("SHARED_FILE_UMASK must be between 0000 and 0777")
    return parsed


os.umask(parse_umask(os.getenv("SHARED_FILE_UMASK", DEFAULT_SHARED_FILE_UMASK)))

app = FastAPI(title="Assistant Sandbox")


class CommandRequest(BaseModel):
    command: list[str] = Field(..., min_length=1)
    timeout_seconds: Optional[int] = Field(default=None, ge=1, le=MAX_TIMEOUT_SECONDS)
    workdir: str = "/data/share"


class CommandResponse(BaseModel):
    exit_code: Optional[int]
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    run_id: Optional[str] = None
    isolation_mode: str = ISOLATION_MODE
    runtime: Optional[str] = None
    image: Optional[str] = None


def resolve_workdir(workdir: str) -> Path:
    resolved = Path(workdir).resolve()
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="workdir does not exist")
    if resolved != SHARED_ROOT and SHARED_ROOT not in resolved.parents:
        raise HTTPException(status_code=400, detail="workdir must stay under shared root")
    return resolved


class UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: int):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


class DockerApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: bytes):
        self.method = method
        self.path = path
        self.status = status
        self.body = body
        message = body.decode("utf-8", errors="replace").strip()
        super().__init__("Docker API %s %s failed with HTTP %s: %s" % (method, path, status, message or "no response body"))


class DockerClient:
    def __init__(self, socket_path: str, timeout: int):
        self.socket_path = socket_path
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        expected: tuple[int, ...] = (200,),
    ) -> tuple[int, bytes]:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        conn = UnixSocketHTTPConnection(self.socket_path, timeout=self.timeout)
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            response_body = response.read()
        finally:
            conn.close()
        if response.status not in expected:
            raise DockerApiError(method, path, response.status, response_body)
        return response.status, response_body

    def json_request(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        expected: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        _, response_body = self.request(method, path, body=body, expected=expected)
        if not response_body:
            return {}
        return json.loads(response_body.decode("utf-8"))


def child_workdir(workdir: Path) -> str:
    if workdir == SHARED_ROOT:
        return str(RUN_SHARED_ROOT)
    relative = workdir.relative_to(SHARED_ROOT)
    return str(RUN_SHARED_ROOT / relative)


def command_with_umask(command: list[str]) -> list[str]:
    umask_value = os.getenv("SHARED_FILE_UMASK", DEFAULT_SHARED_FILE_UMASK)
    return ["/bin/sh", "-lc", "umask %s; exec \"$@\"" % umask_value, "sandbox-command", *command]


def run_mounts() -> tuple[list[str], list[dict[str, Any]]]:
    if SHARED_VOLUME_NAME:
        return [], [{"Type": "volume", "Source": SHARED_VOLUME_NAME, "Target": str(RUN_SHARED_ROOT)}]
    if not SHARED_ROOT_HOST:
        raise RuntimeError("SANDBOX_SHARED_ROOT_HOST or SANDBOX_SHARED_VOLUME_NAME is required for container isolation")
    return ["%s:%s:rw" % (SHARED_ROOT_HOST, RUN_SHARED_ROOT)], []


def create_run_network(client: DockerClient, run_id: str) -> tuple[str, Optional[str]]:
    if RUN_NETWORK_MODE == "none":
        return "none", None
    if RUN_NETWORK_MODE == "bridge":
        return "bridge", None
    if RUN_NETWORK_MODE == "existing":
        if not RUN_NETWORK:
            raise RuntimeError("SANDBOX_RUN_NETWORK is required when SANDBOX_RUN_NETWORK_MODE=existing")
        return RUN_NETWORK, None
    if RUN_NETWORK_MODE != "per_run_bridge":
        raise RuntimeError("unsupported SANDBOX_RUN_NETWORK_MODE: %s" % RUN_NETWORK_MODE)

    name = "%s-%s" % (RUN_NETWORK_PREFIX, run_id)
    client.json_request(
        "POST",
        "/networks/create",
        {
            "Name": name,
            "Driver": "bridge",
            "CheckDuplicate": False,
            "Internal": False,
            "Options": {
                "com.docker.network.bridge.enable_icc": "false",
            },
        },
        expected=(201,),
    )
    return name, name


def create_run_container(client: DockerClient, run_id: str, request: CommandRequest, workdir: Path, network_mode: str) -> str:
    binds, mounts = run_mounts()
    host_config: dict[str, Any] = {
        "AutoRemove": False,
        "NetworkMode": network_mode,
        "Binds": binds,
        "Mounts": mounts,
        "Tmpfs": {
            "/tmp": "rw,nosuid,nodev,size=%s" % RUN_TMPFS_SIZE,
            "/var/tmp": "rw,nosuid,nodev,size=%s" % RUN_TMPFS_SIZE,
        },
        "Memory": RUN_MEMORY_BYTES,
        "NanoCpus": RUN_NANO_CPUS,
        "PidsLimit": RUN_PIDS_LIMIT,
        "SecurityOpt": RUN_SECURITY_OPT,
        "CapDrop": RUN_CAP_DROP,
    }
    if RUN_RUNTIME:
        host_config["Runtime"] = RUN_RUNTIME
    if RUN_DNS_SERVERS:
        host_config["Dns"] = RUN_DNS_SERVERS

    container_config: dict[str, Any] = {
        "Image": RUN_IMAGE,
        "Cmd": command_with_umask(request.command),
        "WorkingDir": child_workdir(workdir),
        "Env": [
            "PIP_CACHE_DIR=/tmp/pip-cache",
            "NPM_CONFIG_CACHE=/tmp/npm-cache",
            "SHARED_FILE_UMASK=%s" % os.getenv("SHARED_FILE_UMASK", DEFAULT_SHARED_FILE_UMASK),
        ],
        "Tty": False,
        "OpenStdin": False,
        "HostConfig": host_config,
    }
    if RUN_USER:
        container_config["User"] = RUN_USER

    container_name = "%s-%s" % (RUN_CONTAINER_PREFIX, run_id)
    response = client.json_request(
        "POST",
        "/containers/create?name=%s" % quote(container_name, safe=""),
        container_config,
        expected=(201,),
    )
    return str(response["Id"])


def container_running(client: DockerClient, container_id: str) -> tuple[bool, Optional[int]]:
    response = client.json_request("GET", "/containers/%s/json" % container_id)
    state = response.get("State") or {}
    if state.get("Running"):
        return True, None
    exit_code = state.get("ExitCode")
    return False, int(exit_code) if exit_code is not None else None


def docker_logs(client: DockerClient, container_id: str) -> tuple[str, str]:
    _, body = client.request(
        "GET",
        "/containers/%s/logs?stdout=1&stderr=1" % container_id,
        expected=(200,),
    )
    return split_docker_log_stream(body)


def split_docker_log_stream(body: bytes) -> tuple[str, str]:
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    index = 0
    while index + 8 <= len(body):
        stream_type = body[index]
        size = int.from_bytes(body[index + 4 : index + 8], "big")
        index += 8
        if index + size > len(body):
            break
        payload = body[index : index + size]
        index += size
        if stream_type == 1:
            stdout_parts.append(payload)
        elif stream_type == 2:
            stderr_parts.append(payload)
    if index != len(body):
        return body.decode("utf-8", errors="replace"), ""
    return (
        b"".join(stdout_parts).decode("utf-8", errors="replace"),
        b"".join(stderr_parts).decode("utf-8", errors="replace"),
    )


def remove_container(client: DockerClient, container_id: Optional[str]) -> None:
    if not container_id:
        return
    try:
        client.request("DELETE", "/containers/%s?force=1&v=1" % container_id, expected=(204, 404))
    except Exception:
        pass


def remove_network(client: DockerClient, network_name: Optional[str]) -> None:
    if not network_name:
        return
    try:
        client.request("DELETE", "/networks/%s" % quote(network_name, safe=""), expected=(204, 404))
    except Exception:
        pass


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "isolation_mode": ISOLATION_MODE,
        "runtime": RUN_RUNTIME or "default",
        "run_network_mode": RUN_NETWORK_MODE,
    }


@app.post("/execute", response_model=CommandResponse)
def execute(request: CommandRequest) -> CommandResponse:
    workdir = resolve_workdir(request.workdir)
    timeout = request.timeout_seconds or DEFAULT_TIMEOUT_SECONDS
    timeout = min(timeout, MAX_TIMEOUT_SECONDS)
    started = time.monotonic()

    if ISOLATION_MODE == "container":
        return execute_container(request, workdir, timeout, started)
    if ISOLATION_MODE != "process":
        raise HTTPException(status_code=500, detail="unsupported SANDBOX_ISOLATION_MODE: %s" % ISOLATION_MODE)
    return execute_process(request, workdir, timeout, started)


def execute_process(request: CommandRequest, workdir: Path, timeout: int, started: float) -> CommandResponse:
    try:
        completed = subprocess.run(
            request.command,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        return CommandResponse(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=duration_ms,
            timed_out=False,
            isolation_mode="process",
        )
    except FileNotFoundError:
        duration_ms = int((time.monotonic() - started) * 1000)
        command_name = request.command[0] if request.command else ""
        return CommandResponse(
            exit_code=127,
            stdout="",
            stderr="command not found: %s" % command_name,
            duration_ms=duration_ms,
            timed_out=False,
            isolation_mode="process",
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return CommandResponse(
            exit_code=None,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            duration_ms=duration_ms,
            timed_out=True,
            isolation_mode="process",
        )


def execute_container(request: CommandRequest, workdir: Path, timeout: int, started: float) -> CommandResponse:
    if not RUN_IMAGE:
        raise HTTPException(status_code=500, detail="SANDBOX_RUN_IMAGE is required for container isolation")

    run_id = uuid.uuid4().hex[:12]
    client = DockerClient(DOCKER_SOCKET, timeout=max(timeout + 30, 60))
    container_id: Optional[str] = None
    cleanup_network: Optional[str] = None
    timed_out = False
    exit_code: Optional[int] = None

    try:
        network_mode, cleanup_network = create_run_network(client, run_id)
        container_id = create_run_container(client, run_id, request, workdir, network_mode)
        client.request("POST", "/containers/%s/start" % container_id, expected=(204, 304))

        deadline = started + timeout
        while True:
            running, current_exit_code = container_running(client, container_id)
            if not running:
                exit_code = current_exit_code
                break
            if time.monotonic() >= deadline:
                timed_out = True
                try:
                    client.request("POST", "/containers/%s/kill" % container_id, expected=(204, 404, 409))
                finally:
                    exit_code = None
                break
            time.sleep(0.25)

        stdout, stderr = docker_logs(client, container_id)
        duration_ms = int((time.monotonic() - started) * 1000)
        return CommandResponse(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
            run_id=run_id,
            isolation_mode="container",
            runtime=RUN_RUNTIME or None,
            image=RUN_IMAGE,
        )
    except (DockerApiError, OSError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        remove_container(client, container_id)
        remove_network(client, cleanup_network)
