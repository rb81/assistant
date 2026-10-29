import os
import json
import http.client
import hmac
import socket
import subprocess
import time
import uuid
import base64
import hashlib
import io
import tarfile
import threading
import re
import logging
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException
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
DOCS_ROOT_HOST = os.getenv("SANDBOX_DOCS_ROOT_HOST", "").strip()
SHARED_VOLUME_NAME = os.getenv("SANDBOX_SHARED_VOLUME_NAME", "").strip()
RUN_CONTAINER_PREFIX = os.getenv("SANDBOX_RUN_CONTAINER_PREFIX", "assistant-sandbox-run").strip()
RUN_NETWORK_PREFIX = os.getenv("SANDBOX_RUN_NETWORK_PREFIX", "assistant-sandbox-net").strip()
# "proxy": per-run networks are internal and the only route out is a per-run
# egress proxy sidecar enforcing the policy the agent sends with each command.
# "open": legacy behaviour, per-run networks have unrestricted internet access.
EGRESS_MODE = os.getenv("SANDBOX_EGRESS_MODE", "proxy").strip().lower()
PROXY_PORT = 3128
PROXY_MEMORY_BYTES = int(os.getenv("SANDBOX_PROXY_MEMORY_BYTES", str(256 * 1024 * 1024)))
PROXY_READY_SECONDS = 10.0
# Unroutable TEST-NET address: run containers on internal networks must not
# resolve names through Docker's embedded DNS forwarding (a DNS exfiltration channel).
DNS_SINK = ["192.0.2.1"]
RUN_LABEL = "assistant.sandbox.run"
RUN_CREATED_LABEL = "assistant.sandbox.created"
ORPHAN_SWEEP_SECONDS = 600
_last_orphan_sweep = 0.0
_permission_lock = threading.RLock()
_permission_epoch = uuid.uuid4().hex


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
    network_policy: Optional[dict[str, list[str]]] = None
    approved_files: Optional[dict[str, str]] = None
    approved_arguments: Optional[dict[str, str]] = None
    approved_python: bool = False
    permission_epoch: Optional[str] = None
    execution_identity: Optional[str] = None
    script_id: Optional[int] = None


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
    egress: Optional[str] = None
    network_events: list[dict[str, str]] = Field(default_factory=list)


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
        body: Optional[dict[str, Any] | bytes] = None,
        expected: tuple[int, ...] = (200,),
    ) -> tuple[int, bytes]:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/x-tar" if isinstance(body, bytes) else "application/json"} if payload is not None else {}
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
    if not DOCS_ROOT_HOST or not Path(DOCS_ROOT_HOST).is_absolute():
        raise RuntimeError("SANDBOX_DOCS_ROOT_HOST must be an absolute host documentation path")
    docs = {
        "Type": "bind", "Source": DOCS_ROOT_HOST,
        "Target": str(RUN_SHARED_ROOT / ".assistant/docs"), "ReadOnly": True,
    }
    if SHARED_VOLUME_NAME:
        return [], [{"Type": "volume", "Source": SHARED_VOLUME_NAME, "Target": str(RUN_SHARED_ROOT)}, docs]
    if not SHARED_ROOT_HOST:
        raise RuntimeError("SANDBOX_SHARED_ROOT_HOST or SANDBOX_SHARED_VOLUME_NAME is required for container isolation")
    return ["%s:%s:rw" % (SHARED_ROOT_HOST, RUN_SHARED_ROOT)], [docs]


def proxy_enabled() -> bool:
    return RUN_NETWORK_MODE == "per_run_bridge" and EGRESS_MODE != "open"


def run_labels(run_id: str) -> dict[str, str]:
    return {RUN_LABEL: run_id, RUN_CREATED_LABEL: str(int(time.time()))}


def clean_policy(policy: Optional[dict[str, Any]]) -> Optional[dict[str, list[str]]]:
    if policy is None:
        return None
    if not isinstance(policy, dict):
        raise HTTPException(status_code=400, detail="network_policy must be an object")
    cleaned: dict[str, list[str]] = {}
    for key in ("allow", "block"):
        items = policy.get(key) or []
        if not isinstance(items, list) or len(items) > 2000 or not all(isinstance(item, str) and 0 < len(item) <= 260 for item in items):
            raise HTTPException(status_code=400, detail="network_policy.%s must be a list of host patterns" % key)
        cleaned[key] = items
    return cleaned


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
            # Proxied runs: internal network shared only with this run's proxy,
            # so inter-container traffic must be allowed for the proxy hop.
            "Internal": proxy_enabled(),
            "Labels": run_labels(run_id),
            "Options": {
                "com.docker.network.bridge.enable_icc": "true" if proxy_enabled() else "false",
            },
        },
        expected=(201,),
    )
    return name, name


def create_egress_network(client: DockerClient, run_id: str) -> str:
    name = "%s-egress-%s" % (RUN_NETWORK_PREFIX, run_id)
    client.json_request(
        "POST",
        "/networks/create",
        {
            "Name": name,
            "Driver": "bridge",
            "CheckDuplicate": False,
            "Internal": False,
            "Labels": run_labels(run_id),
            "Options": {"com.docker.network.bridge.enable_icc": "false"},
        },
        expected=(201,),
    )
    return name


def start_egress_proxy(client: DockerClient, run_id: str, run_network: str, egress_network: str,
                       policy: dict[str, list[str]]) -> tuple[str, str]:
    """Start the per-run proxy and return (container id, its address on the run network)."""
    response = client.json_request(
        "POST",
        "/containers/create?name=%s" % quote("%s-proxy-%s" % (RUN_CONTAINER_PREFIX, run_id), safe=""),
        {
            "Image": RUN_IMAGE,
            "Cmd": ["python", "/app/egress_proxy.py"],
            "Env": ["EGRESS_POLICY=%s" % json.dumps(policy, separators=(",", ":"))],
            "User": "65534:65534",
            "Labels": run_labels(run_id),
            "HostConfig": {
                "NetworkMode": egress_network,
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Memory": PROXY_MEMORY_BYTES,
                "NanoCpus": 1_000_000_000,
                "PidsLimit": 64,
            },
        },
        expected=(201,),
    )
    container_id = str(response["Id"])
    client.request("POST", "/networks/%s/connect" % quote(run_network, safe=""), {"Container": container_id}, expected=(200,))
    client.request("POST", "/containers/%s/start" % container_id, expected=(204, 304))
    deadline = time.monotonic() + PROXY_READY_SECONDS
    while True:
        stdout, _ = docker_logs(client, container_id)
        if '"event": "ready"' in stdout:
            break
        running, _ = container_running(client, container_id)
        if not running or time.monotonic() >= deadline:
            raise RuntimeError("sandbox egress proxy did not start")
        time.sleep(0.1)
    details = client.json_request("GET", "/containers/%s/json" % container_id)
    address = (((details.get("NetworkSettings") or {}).get("Networks") or {}).get(run_network) or {}).get("IPAddress")
    if not address:
        raise RuntimeError("sandbox egress proxy has no address on the run network")
    return container_id, str(address)


def proxy_environment(address: Optional[str]) -> list[str]:
    if not address:
        return []
    url = "http://%s:%s" % (address, PROXY_PORT)
    return [
        "HTTP_PROXY=" + url, "HTTPS_PROXY=" + url, "http_proxy=" + url, "https_proxy=" + url,
        "ALL_PROXY=" + url, "all_proxy=" + url,
        "NO_PROXY=localhost,127.0.0.1", "no_proxy=localhost,127.0.0.1",
        "NODE_USE_ENV_PROXY=1",
    ]


def egress_events(client: DockerClient, container_id: Optional[str]) -> list[dict[str, str]]:
    if not container_id:
        return []
    try:
        stdout, _ = docker_logs(client, container_id)
    except Exception:
        return []
    events = []
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("event") == "egress":
            events.append({key: str(item.get(key) or "") for key in ("host", "decision", "reason")})
    return events


def sweep_orphans(client: DockerClient) -> None:
    """Remove run containers and networks left behind by a crashed or restarted broker."""
    global _last_orphan_sweep
    now = time.time()
    if now - _last_orphan_sweep < ORPHAN_SWEEP_SECONDS:
        return
    _last_orphan_sweep = now
    cutoff = now - MAX_TIMEOUT_SECONDS - 300
    filters = quote(json.dumps({"label": [RUN_LABEL]}), safe="")
    try:
        _, body = client.request("GET", "/containers/json?all=1&filters=%s" % filters)
        for item in json.loads(body or b"[]"):
            if int((item.get("Labels") or {}).get(RUN_CREATED_LABEL) or now) < cutoff:
                remove_container(client, item.get("Id"))
        _, body = client.request("GET", "/networks?filters=%s" % filters)
        for item in json.loads(body or b"[]"):
            if int((item.get("Labels") or {}).get(RUN_CREATED_LABEL) or now) < cutoff:
                remove_network(client, item.get("Name"))
        _, body = client.request("GET", "/volumes?filters=%s" % filters)
        for item in json.loads(body).get("Volumes") or []:
            if int((item.get("Labels") or {}).get(RUN_CREATED_LABEL) or now) < cutoff:
                client.request("DELETE", "/volumes/%s" % quote(item["Name"], safe=""), expected=(204, 404))
    except (DockerApiError, OSError, ValueError) as exc:
        logging.getLogger(__name__).warning("Sandbox orphan cleanup failed: %s", exc)


def create_run_container(client: DockerClient, run_id: str, request: CommandRequest, workdir: Path, network_mode: str,
                         proxy_address: Optional[str] = None, code_mounts: Optional[list[dict[str, Any]]] = None) -> str:
    binds, mounts = run_mounts()
    mounts = [*mounts, *(code_mounts or [])]
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
    if proxy_enabled():
        host_config["Dns"] = DNS_SINK
    elif RUN_DNS_SERVERS:
        host_config["Dns"] = RUN_DNS_SERVERS

    container_config: dict[str, Any] = {
        "Image": RUN_IMAGE,
        "Cmd": command_with_umask(approved_command(request, workdir)),
        "WorkingDir": child_workdir(workdir),
        "Env": [
            "PIP_CACHE_DIR=/tmp/pip-cache",
            "NPM_CONFIG_CACHE=/tmp/npm-cache",
            "SHARED_FILE_UMASK=%s" % os.getenv("SHARED_FILE_UMASK", DEFAULT_SHARED_FILE_UMASK),
            *(["PYTHONPATH=/approved-code/%s:/approved-code" % workdir.relative_to(SHARED_ROOT)]
              if getattr(request, "approved_files", None) is not None else []),
            *proxy_environment(proxy_address),
        ],
        "Labels": run_labels(run_id),
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


def approved_command(request: CommandRequest, workdir: Path) -> list[str]:
    command = list(request.command)
    if getattr(request, "approved_files", None) is None:
        return command
    for index, path in (getattr(request, "approved_arguments", None) or {}).items():
        if path not in request.approved_files or not str(index).isdigit() or int(index) >= len(command):
            raise HTTPException(status_code=400, detail="invalid approved command argument")
        command[int(index)] = "/approved-code/" + path
    if getattr(request, "approved_python", False) and re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?", Path(command[0]).name):
        prefix = (
            "import sys,runpy; "
            "sys.path=%r+[p for p in sys.path if p and not p.startswith(%r)]; " %
            (["/approved-code/" + str(workdir.relative_to(SHARED_ROOT)), "/approved-code"], str(RUN_SHARED_ROOT))
        )
        indexes = sorted(int(index) for index in (getattr(request, "approved_arguments", None) or {}) if int(index) > 0)
        if indexes:
            index = indexes[0]
            return command[:index] + ["-I", "-c", prefix +
                "sys.path.insert(0,sys.argv[1].rsplit('/',1)[0]); "
                "entry=sys.argv.pop(1); runpy.run_path(entry,run_name='__main__')"] + command[index:]
        if "-m" in command[1:] and not request.approved_arguments:
            index = command.index("-m")
            return command[:index] + ["-I", "-c", prefix +
                "module=sys.argv.pop(1); runpy.run_module(module,run_name='__main__',alter_sys=True)"] + command[index + 1:]
        if "-c" in command[1:] and not request.approved_arguments:
            index = command.index("-c")
            return command[:index] + ["-I", "-c", prefix +
                "source=sys.argv.pop(1); exec(compile(source,'<approved-inline>','exec'))"] + command[index + 1:]
    return command


def execution_identity(client: DockerClient) -> str:
    image = client.json_request("GET", "/images/%s/json" % quote(RUN_IMAGE, safe=""))
    material = [image["Id"], RUN_RUNTIME, RUN_USER, RUN_CAP_DROP, RUN_SECURITY_OPT,
                ISOLATION_MODE, RUN_NETWORK_MODE, EGRESS_MODE]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def approved_code_mounts(client: DockerClient, run_id: str, files: dict[str, str]):
    """Populate a private code volume, mounted read-only outside the writable workspace."""
    if len(files) > 100:
        raise HTTPException(status_code=400, detail="too many approved code files")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, encoded in files.items():
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise HTTPException(status_code=400, detail="invalid approved code path")
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid approved code encoding") from exc
            if len(content) > 1_000_000:
                raise HTTPException(status_code=400, detail="approved file is too large")
            info = tarfile.TarInfo(str(path))
            info.size, info.mode = len(content), 0o555
            tar.addfile(info, io.BytesIO(content))
    name = "%s-code-%s" % (RUN_CONTAINER_PREFIX, run_id)
    client.json_request("POST", "/volumes/create",
                        {"Name": name, "Driver": "local", "Labels": run_labels(run_id)}, expected=(201,))
    helper = None
    try:
        helper = client.json_request("POST", "/containers/create", {
            "Image": RUN_IMAGE, "Cmd": ["true"], "Labels": run_labels(run_id),
            "HostConfig": {"NetworkMode": "none", "Mounts": [
                {"Type": "volume", "Source": name, "Target": "/bundle"},
            ]},
        }, expected=(201,))["Id"]
        client.request("PUT", "/containers/%s/archive?path=/bundle" % helper, archive.getvalue(), expected=(200,))
        return name, [{"Type": "volume", "Source": name, "Target": "/approved-code", "ReadOnly": True}]
    except Exception:
        remove_container(client, helper)
        helper = None
        client.request("DELETE", "/volumes/%s" % quote(name, safe=""), expected=(204, 404))
        raise
    finally:
        remove_container(client, helper)


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
def health(authorization: str = Header(default="")) -> dict[str, str]:
    require_api_token(authorization)
    identity = execution_identity(DockerClient(DOCKER_SOCKET, timeout=30)) if ISOLATION_MODE == "container" else "process"
    return {
        "status": "ok",
        "isolation_mode": ISOLATION_MODE,
        "runtime": RUN_RUNTIME or "default",
        "run_network_mode": RUN_NETWORK_MODE,
        "egress": "proxy" if proxy_enabled() else "open",
        "execution_identity": identity,
        "permission_epoch": _permission_epoch,
    }


@app.post("/permissions/revoke")
def revoke_permissions(authorization: str = Header(default="")) -> dict[str, Any]:
    """Invalidate queued policies and stop active sandbox runs after an administrator revokes access."""
    require_api_token(authorization, allow_revoke=True)
    global _permission_epoch
    with _permission_lock:
        _permission_epoch = uuid.uuid4().hex
        client = DockerClient(DOCKER_SOCKET, timeout=30)
        filters = quote(json.dumps({"label": [RUN_LABEL]}), safe="")
        _, body = client.request("GET", "/containers/json?filters=" + filters)
        rows = json.loads(body)
        for row in rows:
            client.request("POST", "/containers/%s/kill" % row["Id"], expected=(204, 404, 409))
    return {"status": "revoked", "stopped_containers": len(rows)}


@app.post("/execute", response_model=CommandResponse)
def execute(request: CommandRequest, authorization: str = Header(default="")) -> CommandResponse:
    require_api_token(authorization)
    workdir = resolve_workdir(request.workdir)
    timeout = request.timeout_seconds or DEFAULT_TIMEOUT_SECONDS
    timeout = min(timeout, MAX_TIMEOUT_SECONDS)
    started = time.monotonic()
    if getattr(request, "permission_epoch", None) is not None and (
        ISOLATION_MODE != "container" or (not proxy_enabled() and RUN_NETWORK_MODE != "none")
    ):
        raise HTTPException(status_code=409, detail="Approved scripts require container isolation and proxy egress")

    if ISOLATION_MODE == "container":
        return execute_container(request, workdir, timeout, started)
    if ISOLATION_MODE != "process":
        raise HTTPException(status_code=500, detail="unsupported SANDBOX_ISOLATION_MODE: %s" % ISOLATION_MODE)
    return execute_process(request, workdir, timeout, started)


def require_api_token(authorization: str, *, allow_revoke: bool = False) -> None:
    token = os.getenv("SANDBOX_API_TOKEN", "")
    token_file = os.getenv("SANDBOX_API_TOKEN_FILE", "")
    if token and token_file:
        raise HTTPException(status_code=503, detail="sandbox authentication is misconfigured")
    if token_file:
        try:
            path = Path(token_file)
            if not path.is_file() or path.stat().st_size > 4096:
                raise ValueError
            token = path.read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeError, ValueError):
            raise HTTPException(status_code=503, detail="sandbox authentication is unavailable") from None
    if len(token) < 32:
        raise HTTPException(status_code=503, detail="sandbox authentication is not configured")
    scheme, separator, supplied = authorization.partition(" ")
    revoke_token = hmac.new(token.encode(), b"assistant-sandbox-revoke-v1", hashlib.sha256).hexdigest()
    valid = hmac.compare_digest(token.encode("utf-8"), supplied.encode("utf-8"))
    if allow_revoke:
        valid = valid or hmac.compare_digest(revoke_token.encode(), supplied.encode("utf-8"))
    if not separator or scheme.lower() != "bearer" or not valid:
        raise HTTPException(status_code=401, detail="authentication required")


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
    proxy_id: Optional[str] = None
    proxy_address: Optional[str] = None
    cleanup_network: Optional[str] = None
    egress_network: Optional[str] = None
    code_volume: Optional[str] = None
    timed_out = False
    exit_code: Optional[int] = None
    policy = clean_policy(getattr(request, "network_policy", None))
    egress = "none" if RUN_NETWORK_MODE == "none" else "open"
    if proxy_enabled():
        egress = "proxy" if policy is not None else "none"

    try:
        with _permission_lock:
            if getattr(request, "permission_epoch", None) is not None:
                if request.permission_epoch != _permission_epoch or request.execution_identity != execution_identity(client):
                    raise HTTPException(status_code=409, detail="Permissions or sandbox image changed; request current approval")
            sweep_orphans(client)
            code_mounts = []
            if getattr(request, "approved_files", None):
                code_volume, code_mounts = approved_code_mounts(client, run_id, request.approved_files)
            network_mode, cleanup_network = create_run_network(client, run_id)
            if egress == "proxy":
                egress_network = create_egress_network(client, run_id)
                proxy_id, proxy_address = start_egress_proxy(client, run_id, network_mode, egress_network, policy)
            container_id = create_run_container(client, run_id, request, workdir, network_mode, proxy_address, code_mounts)
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
            egress=egress,
            network_events=egress_events(client, proxy_id),
        )
    except (DockerApiError, OSError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        remove_container(client, container_id)
        remove_container(client, proxy_id)
        if code_volume:
            client.request("DELETE", "/volumes/%s" % quote(code_volume, safe=""), expected=(204, 404))
        remove_network(client, cleanup_network)
        remove_network(client, egress_network)
