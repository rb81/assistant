"""Host-side access to a private Docker volume, without a long-running broker."""
import hashlib
import json


HELPER_IMAGE = "python:3.12-slim"

# Executed in a disposable, network-isolated container. Values travel over stdin,
# never through command arguments, environment variables, or host staging files.
HELPER = r"""
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile

root = Path("/credentials")
os.umask(0o077)
os.chmod(root, 0o700)

def atomic(path, content):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def install(record):
    services = root / "services"
    services.mkdir(mode=0o700, exist_ok=True)
    grants = record.get("grants", {})
    values = record.get("values", {})
    for service in set(grants) | {path.name for path in services.iterdir()}:
        directory = services / service
        directory.mkdir(mode=0o700, exist_ok=True)
        names = set(grants.get(service, []))
        for name in names:
            atomic(directory / name, values[name])
        for path in directory.iterdir():
            if path.name not in names:
                path.unlink()
    atomic(root / "store.json", json.dumps(record))

request = json.load(sys.stdin)
with (root / "lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    path = root / "store.json"
    recovery = root / "recovery.json"
    if recovery.exists():
        install(json.loads(recovery.read_text()))
        recovery.unlink()
    previous = json.loads(path.read_text()) if path.exists() else {}
    if request["operation"] == "write":
        if previous != request["expected"]:
            raise RuntimeError("Credential volume changed")
        record = request["record"]
        for name in record.get("values", {}):
            if not name or Path(name).name != name or name in {".", ".."}:
                raise ValueError("Invalid credential name")
        for service, names in record.get("grants", {}).items():
            if not service or Path(service).name != service or service in {".", ".."}:
                raise ValueError("Invalid service name")
            if any(name not in record["values"] for name in names):
                raise ValueError("Missing credential")
        atomic(recovery, json.dumps(previous))
        try:
            install(record)
            assert json.loads(path.read_text()) == record
            for service, names in record.get("grants", {}).items():
                for name in names:
                    assert (root / "services" / service / name).read_text() == record["values"][name]
        except BaseException:
            install(previous)
            recovery.unlink()
            raise
        recovery.unlink()
        previous = record
    print(json.dumps(previous))
"""


class CredentialVolume:
    def __init__(self, installation, name):
        self.installation = installation
        self.name = name
        self.record = {}

    @staticmethod
    def name_for(root):
        return "assistant-credentials-" + hashlib.sha256(str(root).encode()).hexdigest()[:16]

    def load(self, create=False):
        if create:
            self.installation.docker(["volume", "create", self.name])
        else:
            # Do not silently recreate a missing volume and lose credentials.
            self.installation.docker(["volume", "inspect", self.name])
        self.record = self.request({"operation": "read"})
        return self.record

    def request(self, payload):
        output = self.installation.docker([
            "run", "--rm", "-i", "--log-driver", "none", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--mount", "type=volume,source=" + self.name + ",target=/credentials",
            HELPER_IMAGE, "python", "-c", HELPER,
        ], input_text=json.dumps(payload))
        return json.loads(output)

    def write(self, values, grants):
        record = {"values": values, "grants": grants}
        self.record = self.request({"operation": "write", "expected": self.record, "record": record})

    def verify_mount(self):
        grants = self.record.get("grants", {})
        if not grants:
            return
        service = next(iter(grants))
        self.installation.docker([
            "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--mount", "type=volume,source=" + self.name +
            ",target=/run/secrets,readonly,volume-subpath=services/" + service,
            HELPER_IMAGE, "python", "-c",
            "from pathlib import Path; assert all(p.is_file() for p in Path('/run/secrets').iterdir())",
        ])

    def assert_current(self):
        if self.request({"operation": "read"}) != self.record:
            from .setup import SetupError
            raise SetupError("Credential volume changed during this session; reopen setup")
