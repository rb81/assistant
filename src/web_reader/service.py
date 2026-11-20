import hmac
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


BROWSER_UID = 10001
MAX_REQUEST_BYTES = 16 * 1024
MAX_OUTPUT_BYTES = 512 * 1024


def reap_browser_processes():
    # Playwright can launch a separate Chromium process group. This UID is exclusive
    # to the current extraction, and /proc only sees this container's PID namespace.
    for path in Path("/proc").iterdir():
        if not path.name.isdecimal():
            continue
        try:
            status = (path / "status").read_text()
            uid = next(line for line in status.splitlines() if line.startswith("Uid:"))
            if int(uid.split()[1]) == BROWSER_UID:
                os.kill(int(path.name), signal.SIGKILL)
        except (ProcessLookupError, FileNotFoundError):
            continue


def extract(url, timeout):
    with tempfile.TemporaryDirectory(dir="/tmp") as work, tempfile.TemporaryFile(dir="/tmp") as output:
        os.chown(work, BROWSER_UID, BROWSER_UID)
        try:
            subprocess.run(
                [sys.executable, "/app/extract.py", url, str(timeout)],
                cwd=work, user=BROWSER_UID, group=BROWSER_UID, extra_groups=[],
                env={"HOME": work, "TMPDIR": work, "PLAYWRIGHT_BROWSERS_PATH": os.environ["PLAYWRIGHT_BROWSERS_PATH"],
                     "PYTHONDONTWRITEBYTECODE": "1", "PATH": os.defpath},
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL, timeout=timeout, check=True,
            )
        finally:
            reap_browser_processes()
        output.seek(0)
        body = output.read(MAX_OUTPUT_BYTES + 1)
        if not body or len(body) > MAX_OUTPUT_BYTES:
            raise ValueError("Invalid browser response size")
        return body


def parse_request(body):
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("Expected an object")
    url, timeout = data.get("url"), data.get("timeout_seconds")
    if (not isinstance(url, str) or not url or len(url) > 2048
            or any(ord(char) < 33 or ord(char) == 127 or char in "\\ " for char in url)):
        raise ValueError("Invalid page URL")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 443)):
        raise ValueError("The managed reader requires a public HTTPS URL")
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError("Invalid timeout")
    return url, timeout


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_request(self, *args):
        pass

    def do_GET(self):
        if self.path != "/health":
            self.reply(404, "Not found")
        else:
            self.reply(200, "Ready")

    def do_POST(self):
        if self.path != "/crawl":
            self.reply(404, "Not found")
            return
        authorization = self.headers.get("Authorization", "").encode("latin-1")
        if not hmac.compare_digest(authorization, self.server.authorization):
            self.reply(401, "Unauthorized")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 1 <= length <= MAX_REQUEST_BYTES:
                raise ValueError("Invalid body size")
            url, timeout = parse_request(self.rfile.read(length))
        except (ValueError, UnicodeError, socket.timeout):
            self.reply(400, "Invalid managed reader request; use a public HTTPS URL")
            return
        if not self.server.slot.acquire(blocking=False):
            self.reply(503, "Managed reader is busy; retry shortly")
            return
        try:
            body = extract(url, timeout)
        except subprocess.TimeoutExpired:
            self.reply(504, "Browser exceeded the page deadline")
        except (subprocess.CalledProcessError, OSError, ValueError):
            self.log_error("Managed browser extraction failed")
            self.reply(502, "Browser could not extract the page")
        else:
            self.send_body(200, body)
        finally:
            self.server.slot.release()

    def reply(self, status, message):
        self.send_body(status, json.dumps({"message": message}).encode())

    def send_body(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    token = Path(os.environ["AGENT_WEB_FETCH_MANAGED_TOKEN_FILE"]).read_bytes().strip()
    if not token:
        raise RuntimeError("Managed page reader token is missing")
    with ThreadingHTTPServer(("0.0.0.0", 8080), Handler) as server:
        server.authorization = b"Bearer " + token
        server.slot = threading.BoundedSemaphore()
        server.serve_forever()


if __name__ == "__main__":
    main()
