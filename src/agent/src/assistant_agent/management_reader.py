"""Persistent, private management reads; no Docker socket or installation access."""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import threading
import time

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic import TypeAdapter, ValidationError

from .admin_commands import redact_secrets
from .config import database_url, load_config
from .database import json_safe
from .management_contracts import ManagementReadRequest
from .management_operator import execute
from .requests import TransactionDatabase


LOGGER = logging.getLogger(__name__)
READ_ADAPTER = TypeAdapter(ManagementReadRequest)
MAX_BODY = 64 * 1024
MAX_READS = 4
READ_KINDS = {
    "conversations.list": "conversations", "tasks.messages": "task-messages",
    "tasks.activity": "task-activity", "tasks.summary": "summary",
    "records.list": None, "records.read": None, "records.children": None,
    "clients.read": "clients.read", "tasks.snapshot": "task-snapshot",
    "events.snapshot": "event-snapshot",
}


def operator_request(request):
    """Translate only allowlisted typed reads into the existing domain boundary."""
    payload = request.model_dump()
    name = payload.pop("request")
    if name not in READ_KINDS:
        raise ValueError("This operation is not available on the read service.")
    if name == "clients.read" and payload["action"] == "reconcile":
        raise ValueError("External delivery reconciliation belongs to the isolated executor.")
    if name.startswith("records."):
        collection = payload.pop("collection")
        if name == "records.children":
            kind = {"tasks": "task-events", "research": "research-events", "projects": "project-objects"}[collection]
        else:
            kind = "script" if name == "records.read" and collection == "scripts" else collection
    else:
        kind = READ_KINDS[name]
    if "task_id" in payload:
        payload["id"] = payload.pop("task_id")
    if "record_id" in payload:
        payload["id"] = payload.pop("record_id")
    return {"kind": kind, **payload}


def read(db, config, payload):
    """Return one consistent snapshot, without allowing arbitrary domain mutations."""
    if payload["kind"] == "event-snapshot":
        row = db.fetch_one("""
            SELECT pg_current_snapshot()::text AS transactions,
              (SELECT COALESCE(string_agg(key || updated_at::text, ',' ORDER BY key), '')
               FROM runtime_state WHERE key LIKE 'email-approval:%%') AS approvals
        """)
        # Transaction snapshots include committed progress and still-open transactions.
        # Max IDs/timestamps miss deletes, in-place edits and late commits.
        return {
            "revision": hashlib.sha256(row["transactions"].encode()).hexdigest(),
            "approvals_revision": hashlib.sha256(row["approvals"].encode()).hexdigest(),
        }
    if payload["kind"] == "task-snapshot":
        identifier = payload["id"]
        messages = execute(db, config, {"kind": "task-messages", "id": identifier, "latest": True})
        page = messages.pop("page")
        since = messages["rows"][0]["created_at"].isoformat() if page and messages["rows"] else None
        return {
            "messages": messages, "page": page,
            "activity": execute(db, config, {"kind": "task-activity", "id": identifier, "since": since}),
            "task": execute(db, config, {"kind": "tasks", "id": identifier})["rows"][0],
        }
    return execute(db, config, payload)


def create_server(address, token, config, pool):
    """Create the authenticated internal service with bounded independent read capacity."""
    capacity = threading.BoundedSemaphore(MAX_READS)

    class Handler(BaseHTTPRequestHandler):
        timeout = 15
        protocol_version = "HTTP/1.1"

        def log_message(self, message, *args):
            return

        def respond(self, status, body):
            encoded = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self):
            if self.path != "/v1/read" or self.headers.get("Origin"):
                self.close_connection = True
                self.respond(404, {"error": "Not found"})
                return
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()):
                self.close_connection = True
                self.respond(401, {"error": "Unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 0 < length <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                    raise ValueError
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError
                payload = operator_request(READ_ADAPTER.validate_python(json.loads(raw)))
            except (ValueError, KeyError, ValidationError):
                self.close_connection = True
                self.respond(422, {"error": "Invalid read"})
                return
            if not capacity.acquire(blocking=False):
                self.respond(503, {"error": "Management reads are busy; retry."})
                return
            started = time.monotonic()
            try:
                with pool.connection(timeout=5) as connection:
                    with connection.transaction():
                        # Delivery polling renews leases; acknowledgements/reconciliation also write.
                        mode = "" if payload["kind"] == "clients.read" else " READ ONLY"
                        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ" + mode)
                        connection.execute("SET LOCAL statement_timeout = '10s'")
                        result = read(TransactionDatabase(connection), config, payload)
                self.respond(200, {"result": redact_secrets(json_safe(result))})
            except (PoolTimeout, psycopg.OperationalError, psycopg.errors.QueryCanceled):
                LOGGER.warning("Management read temporarily unavailable (%s)", payload["kind"])
                self.respond(503, {"error": "Management reads are temporarily unavailable."})
            except (ValueError, KeyError, RuntimeError, psycopg.Error, OSError):
                LOGGER.exception("Management read failed (%s)", payload["kind"])
                self.respond(409, {"error": "Management read failed. Check service health and migrations."})
            finally:
                LOGGER.info("Management read timing kind=%s duration_ms=%d", payload["kind"],
                            (time.monotonic() - started) * 1000)
                capacity.release()

    server = ThreadingHTTPServer(address, Handler)
    server.daemon_threads = True
    return server


def main():
    """Start only on the private management network with a mounted connection token."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    token = Path("/run/assistant/management-token").read_text().strip()
    if not 43 <= len(token) <= 128 or not all(char.isascii() and (char.isalnum() or char in "_-") for char in token):
        raise RuntimeError("Management connection token is invalid.")
    config = load_config()
    with ConnectionPool(database_url(config), min_size=0, max_size=MAX_READS,
                        kwargs={"row_factory": dict_row, "connect_timeout": 5},
                        check=ConnectionPool.check_connection) as pool:
        with create_server(("0.0.0.0", 8766), token, config, pool) as server:
            server.serve_forever()


if __name__ == "__main__":
    main()
