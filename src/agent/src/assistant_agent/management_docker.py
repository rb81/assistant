"""Project-scoped read gateway and private typed executor; only this container mounts the socket."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
from datetime import datetime, timezone
from threading import Thread, Timer
from urllib.parse import parse_qs, urlencode, urlsplit


LOGGER = logging.getLogger(__name__)
PROJECT_LABEL = "com.docker.compose.project"
MAX_BODY = 4 * 1024 * 1024


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


class DockerReader:
    def __init__(self, project, connection_factory=UnixConnection):
        self.project = project
        self.connection_factory = connection_factory

    def request(self, path):
        connection = self.connection_factory("localhost", timeout=15)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise ValueError("Docker response exceeded the management limit")
            return response.status, dict(response.getheaders()), body
        finally:
            connection.close()

    def belongs_to_project(self, prefix, identifier):
        status, _, body = self.request(prefix + "/containers/" + identifier + "/json")
        if status != 200:
            return False
        record = json.loads(body)
        return (record.get("Config", {}).get("Labels") or {}).get(PROJECT_LABEL) == self.project

    def read(self, target):
        parsed = urlsplit(target)
        match = re.fullmatch(r"(/v[0-9]+\.[0-9]+)?(/.*)", parsed.path)
        if not match or parsed.scheme or parsed.netloc:
            raise PermissionError
        prefix, path = match.group(1) or "", match.group(2)
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
        if path in {"/_ping", "/version", "/info"} and not query:
            status, headers, body = self.request(prefix + path)
            if path == "/info" and status == 200:
                body = json.dumps({"ServerVersion": json.loads(body)["ServerVersion"]}).encode()
            return status, headers, body
        if path == "/containers/json" and set(query) <= {"all", "filters", "limit", "size"}:
            filters = json.loads(query.get("filters", ["{}"])[0])
            if not isinstance(filters, dict):
                raise ValueError
            labels = filters.get("label", [])
            if isinstance(labels, dict):
                labels = [key for key, enabled in labels.items() if enabled]
            if not isinstance(labels, list) or any(not isinstance(item, str) for item in labels):
                raise ValueError
            filters["label"] = [*labels, PROJECT_LABEL + "=" + self.project]
            query["filters"] = [json.dumps(filters)]
            status, headers, body = self.request(prefix + path + "?" + urlencode(query, doseq=True))
            if status == 200:
                body = json.dumps([
                    record for record in json.loads(body)
                    if (record.get("Labels") or {}).get(PROJECT_LABEL) == self.project
                ]).encode()
            return status, headers, body
        container = re.fullmatch(r"/containers/([a-f0-9]{12,64})/(json|logs)", path)
        if not container:
            raise PermissionError
        identifier, operation = container.groups()
        if operation == "json" and query:
            raise PermissionError
        if operation == "logs":
            if set(query) - {"stdout", "stderr", "timestamps", "tail", "since", "until", "follow", "details"}:
                raise PermissionError
            if query.get("follow", ["0"])[0] not in {"0", "false"}:
                raise PermissionError
            tail = query.get("tail", ["150"])[0]
            if not tail.isdigit() or not 1 <= int(tail) <= 150:
                raise PermissionError
            query["tail"] = [tail]
        if not self.belongs_to_project(prefix, identifier):
            raise PermissionError
        return self.request(prefix + path + ("?" + urlencode(query, doseq=True) if query else ""))


def create_server(address, reader):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, message, *args):
            return

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            try:
                status, headers, body = reader.read(self.path)
            except PermissionError:
                status, headers, body = 403, {}, b'{"message":"Docker operation is not permitted"}'
            except (OSError, ValueError, KeyError, http.client.HTTPException):
                LOGGER.warning("Docker read failed")
                status, headers, body = 502, {}, b'{"message":"Docker read failed"}'
            self.send_response(status)
            for name in ("Content-Type", "Api-Version", "API-Version", "Docker-Experimental", "Ostype"):
                if name in headers:
                    self.send_header(name, headers[name])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

    return ThreadingHTTPServer(address, Handler)


def prepare_executor_restart(reader, project, container_id=None):
    container_id = container_id or os.environ.get("HOSTNAME", "")
    if not re.fullmatch(r"[a-f0-9]{12,64}", container_id):
        raise ValueError("The executor container identity is unavailable.")
    status, _, body = reader.request("/containers/" + container_id + "/json")
    if status != 200:
        raise ValueError("The executor container could not be verified.")
    try:
        container = json.loads(body)
        labels = container["Config"]["Labels"]
        restart_policy = container["HostConfig"]["RestartPolicy"]["Name"]
        started_at = container["State"]["StartedAt"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise ValueError("The executor container could not be verified.") from None
    if (not isinstance(labels, dict) or labels.get(PROJECT_LABEL) != project
            or labels.get("com.docker.compose.service") != "management-docker"
            or restart_policy not in {"always", "unless-stopped"}):
        raise ValueError("The executor container is not configured for a safe restart.")
    if not isinstance(started_at, str):
        raise ValueError("The executor container start time could not be verified.")
    try:
        normalized_start = re.sub(r"(\.\d{6})\d+(?=Z$)", r"\1", started_at)
        started = datetime.fromisoformat(normalized_start.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("The executor container start time could not be verified.") from None
    if started.tzinfo is None:
        raise ValueError("The executor container start time could not be verified.")
    uptime = (datetime.now(timezone.utc) - started).total_seconds()
    delay = max(0.75, 10.5 - max(0, uptime))

    def schedule():
        def terminate_executor():
            LOGGER.warning("Sending SIGTERM to management executor for the requested restart.")
            os.kill(os.getpid(), signal.SIGTERM)

        timer = Timer(delay, terminate_executor)
        timer.daemon = True
        timer.start()

    return {
        "container_id": container_id,
        "project": project,
        "started_at": started_at,
        "schedule": schedule,
    }


def main():
    project = os.environ.get("ASSISTANT_COMPOSE_PROJECT", "assistant")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        raise SystemExit("Invalid Assistant Compose project")

    def terminate(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    from .management_executor import (
        create_server as create_executor_server,
        reconcile_executor_restart,
    )

    root = Path(os.environ["ASSISTANT_INSTALL_ROOT"])
    reader = DockerReader(project)
    executor = create_executor_server(
        ("0.0.0.0", 2376), root,
        prepare_executor_restart=lambda: prepare_executor_restart(reader, project),
    )
    Thread(target=executor.serve_forever, daemon=True).start()
    Thread(target=reconcile_executor_restart, args=(root, reader, project), daemon=True).start()
    create_server(("0.0.0.0", 2375), reader).serve_forever()


if __name__ == "__main__":
    main()
