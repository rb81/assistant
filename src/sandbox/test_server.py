import struct
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


class FakeFastAPI:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def get(self, *args, **kwargs):
        return lambda func: func

    def post(self, *args, **kwargs):
        return lambda func: func


class FakeHTTPException(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class FakeBaseModel:
    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def fake_field(default=None, **kwargs):
    return default


fastapi_module = types.ModuleType("fastapi")
fastapi_module.FastAPI = FakeFastAPI
fastapi_module.HTTPException = FakeHTTPException
fastapi_module.Header = lambda default=None, **kwargs: default
sys.modules.setdefault("fastapi", fastapi_module)

pydantic_module = types.ModuleType("pydantic")
pydantic_module.BaseModel = FakeBaseModel
pydantic_module.Field = fake_field
sys.modules.setdefault("pydantic", pydantic_module)

import server
import bootstrap


class DevelopmentDeadlineTest(unittest.TestCase):
    def test_expired_and_naive_authorizations_are_refused(self):
        for expires, status in (
            (datetime.now(timezone.utc) - timedelta(seconds=1), 409),
            (datetime.now(), 400),
        ):
            request = server.CommandRequest(command=["python", "job.py"], authorization_expires_at=expires)
            with self.assertRaises(server.HTTPException) as raised:
                server.authorization_remaining(request)
            self.assertEqual(raised.exception.status_code, status)
        request = server.CommandRequest(command=["python", "job.py"],
                                        authorization_expires_at=datetime.now(timezone.utc) + timedelta(seconds=20))
        self.assertGreater(server.authorization_remaining(request), 0)

    def test_yolo_public_policy_still_honors_blocks(self):
        import egress_proxy
        policy = egress_proxy.Policy(["*"], ["blocked.example.com"])
        self.assertIsNone(policy.decide("api.example.com"))
        self.assertEqual(policy.decide("blocked.example.com"), "blocked_by_admin")


@unittest.skipUnless(os.getenv("ASSISTANT_TEST_PERMISSION_IMAGE"), "requires disposable permissions Docker image")
class ScriptPermissionDockerTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        import uuid
        from unittest.mock import patch
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.client = server.DockerClient(server.DOCKER_SOCKET, timeout=30)
        self.volume = "assistant-permissions-test-" + uuid.uuid4().hex
        self.client.json_request("POST", "/volumes/create", {"Name": self.volume}, expected=(201,))
        self.addCleanup(lambda: self.client.request("DELETE", "/volumes/" + self.volume, expected=(204, 404)))
        for key, value in {
            "SHARED_ROOT": self.root, "RUN_SHARED_ROOT": Path("/data/share"),
            "RUN_IMAGE": os.environ["ASSISTANT_TEST_PERMISSION_IMAGE"],
            "RUN_LABEL": "assistant.permissions-test.70dc",
            "RUN_CONTAINER_PREFIX": self.volume, "RUN_NETWORK_PREFIX": self.volume,
        }.items():
            override = patch.object(server, key, value)
            override.start()
            self.addCleanup(override.stop)
        override = patch.object(server, "run_mounts", return_value=([], [
            {"Type": "volume", "Source": self.volume, "Target": "/data/share"},
        ]))
        override.start()
        self.addCleanup(override.stop)

    def request(self, source):
        import base64
        return server.CommandRequest(
            command=["python", "/data/share/job.py"], workdir=str(self.root), timeout_seconds=20,
            network_policy={"allow": [], "block": []},
            approved_files={"job.py": base64.b64encode(source.encode()).decode()},
            approved_arguments={"1": "job.py"}, approved_python=True,
            permission_epoch=server._permission_epoch, execution_identity=server.execution_identity(self.client),
            script_id=1,
        )

    def test_approved_bytes_are_mounted_readonly_and_volume_is_cleaned(self):
        import time
        source = (
            "from pathlib import Path\nprint('approved snapshot', flush=True)\n"
            "try:\n Path(__file__).write_text('changed')\n"
            "except OSError:\n print('read-only code', flush=True)\n"
            "Path('/data/share/result.txt').write_text('writable output')\n"
        )
        response = server.execute_container(self.request(source), self.root, 20, time.monotonic())
        self.assertEqual(response.exit_code, 0, response.stderr)
        self.assertIn("approved snapshot", response.stdout)
        self.assertIn("read-only code", response.stdout)
        _, body = self.client.request("GET", "/volumes")
        import json
        self.assertFalse(any(row["Name"].startswith(self.volume + "-code-") for row in json.loads(body)["Volumes"]))

    def test_development_expiry_stops_running_script_and_rejects_late_dispatch(self):
        import time
        request = self.request("import time\nprint('started', flush=True)\ntime.sleep(30)\n")
        request.authorization_expires_at = datetime.now(timezone.utc) + timedelta(seconds=8)
        started = time.monotonic()
        response = server.execute_container(request, self.root, 20, started)
        self.assertTrue(response.timed_out)
        self.assertLess(time.monotonic() - started, 15)
        with self.assertRaises(server.HTTPException) as raised:
            server.execute_container(request, self.root, 20, time.monotonic())
        self.assertEqual(raised.exception.status_code, 409)

    def test_revocation_kills_running_execution_and_rejects_stale_policy(self):
        import time
        import json
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        request = self.request("import time\nprint('started', flush=True)\ntime.sleep(30)\n")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(server.execute_container, request, self.root, 20, time.monotonic())
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                _, body = self.client.request("GET", "/containers/json")
                rows = [r for r in json.loads(body) if r.get("Labels", {}).get(server.RUN_LABEL)]
                if any(not any("proxy" in name for name in row.get("Names", [])) for row in rows):
                    break
                time.sleep(0.1)
            else:
                self.fail("execution did not start")
            with patch.dict(os.environ, {"SANDBOX_API_TOKEN": "s" * 32}):
                result = server.revoke_permissions("Bearer " + "s" * 32)
            self.assertGreater(result["stopped_containers"], 0)
            response = future.result(timeout=10)
            self.assertNotEqual(response.exit_code, 0)
        with self.assertRaises(FakeHTTPException) as raised:
            server.execute_container(request, self.root, 20, time.monotonic())
        self.assertEqual(raised.exception.status_code, 409)

    def test_local_imports_inline_and_modules_ignore_mutable_workspace_code(self):
        import base64
        import time
        source = (
            "from pathlib import Path\n"
            "Path('/data/share/helper.py').write_text(\"value = 'unapproved'\\n\")\n"
            "Path('/data/share/runpy.py').write_text(\"raise RuntimeError('unapproved bootstrap')\\n\")\n"
            "import helper\nprint(helper.value)\n"
        )
        request = self.request(source)
        helper = base64.b64encode(b"value = 'approved helper'\n").decode()
        request.approved_files["helper.py"] = helper
        for _ in range(2):
            response = server.execute_container(request, self.root, 20, time.monotonic())
            self.assertEqual(response.exit_code, 0, response.stderr)
            self.assertIn("approved helper", response.stdout)
        request.command = ["python", "-c", "import helper; print(helper.value)"]
        request.approved_arguments = {}
        response = server.execute_container(request, self.root, 20, time.monotonic())
        self.assertEqual(response.exit_code, 0, response.stderr)
        self.assertIn("approved helper", response.stdout)
        request.command = ["python", "-m", "reports"]
        request.approved_files["reports/__init__.py"] = ""
        request.approved_files["reports/__main__.py"] = base64.b64encode(b"import helper\nprint(helper.value)\n").decode()
        response = server.execute_container(request, self.root, 20, time.monotonic())
        self.assertEqual(response.exit_code, 0, response.stderr)
        self.assertIn("approved helper", response.stdout)


class SandboxBootstrapTest(unittest.TestCase):
    @unittest.skipUnless(os.getenv("ASSISTANT_TEST_DOCKER") == "1" and os.getenv("ASSISTANT_TEST_SANDBOX_IMAGE"), "requires disposable Docker test image")
    def test_capability_free_broker_uses_staged_private_files_after_rotation(self):
        import subprocess
        import tempfile
        import uuid

        image = os.environ["ASSISTANT_TEST_SANDBOX_IMAGE"]
        prefix = "assistant-staging-test-" + uuid.uuid4().hex
        source_volume, runtime_volume = prefix + "-source", prefix + "-runtime"
        repository = Path(__file__).resolve().parent

        def docker(*arguments):
            result = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        try:
            docker("volume", "create", source_volume)
            docker("volume", "create", runtime_volume)
            with tempfile.TemporaryDirectory() as directory:
                fixture = Path(directory)
                subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                                "-keyout", str(fixture / "sandbox.key"), "-out", str(fixture / "sandbox.crt"),
                                "-subj", "/CN=sandbox", "-addext", "subjectAltName=DNS:sandbox"], check=True, capture_output=True)
                for token_value in ("s" * 32, "s" * 32, "t" * 32):
                    (fixture / "SANDBOX_API_TOKEN").write_text(token_value)
                    docker("run", "--rm", "--network", "none", "-v", str(fixture) + ":/fixture:ro",
                           "-v", source_volume + ":/source", "--entrypoint", "python", image, "-c",
                           "import os,shutil; from pathlib import Path; "
                           "[(shutil.copyfile(path,Path('/source')/path.name),os.chmod(Path('/source')/path.name,0o600),"
                           "os.chown(Path('/source')/path.name,1000,1000)) for path in Path('/fixture').iterdir()]")
                    docker("run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL", "--cap-add", "DAC_READ_SEARCH",
                           "-v", str(repository) + ":/app:ro", "-v", source_volume + ":/run/assistant/source:ro",
                           "-v", source_volume + ":/run/secrets:ro", "-v", runtime_volume + ":/run/assistant/runtime",
                           "--entrypoint", "python", image, "/app/bootstrap.py")
                    probe = (
                        "import os,ssl,subprocess,time,urllib.request; from pathlib import Path; "
                        "root=Path('/run/assistant/runtime/current'); "
                        "assert (root/'sandbox.key').stat().st_uid == 0; "
                        "assert (root/'sandbox.key').stat().st_mode & 0o777 == 0o600; "
                        "assert (root/'SANDBOX_API_TOKEN').stat().st_mode & 0o777 == 0o600; "
                        "os.environ['SANDBOX_API_TOKEN_FILE']=str(root/'SANDBOX_API_TOKEN'); "
                        "process=subprocess.Popen(['uvicorn','server:app','--host','127.0.0.1','--port','8080',"
                        "'--ssl-certfile',str(root/'sandbox.crt'),'--ssl-keyfile',str(root/'sandbox.key'),'--timeout-graceful-shutdown','2']); "
                        "context=ssl.create_default_context(cafile=str(root/'sandbox.crt')); "
                        "request=urllib.request.Request('https://sandbox:8080/health',headers={'Authorization':'Bearer '+(root/'SANDBOX_API_TOKEN').read_text()}); "
                        "deadline=time.monotonic()+15\n"
                        "try:\n"
                        "    while time.monotonic() < deadline:\n"
                        "        try:\n"
                        "            response=urllib.request.urlopen(request,context=context,timeout=1); assert response.status == 200; response.read(); response.close(); break\n"
                        "        except urllib.error.URLError:\n"
                        "            if process.poll() is not None: raise RuntimeError('Broker exited during startup')\n"
                        "    else: raise RuntimeError('Broker did not become ready')\n"
                        "finally:\n"
                        "    process.terminate(); process.wait(timeout=10)\n"
                    )
                    docker("run", "--rm", "--network", "none", "--add-host", "sandbox:127.0.0.1", "--read-only", "--cap-drop", "ALL",
                           "-v", str(repository) + ":/app:ro", "-v", runtime_volume + ":/run/assistant/runtime:ro",
                           "-w", "/app", "--entrypoint", "python", image, "-c", probe)
        finally:
            subprocess.run(["docker", "volume", "rm", source_volume, runtime_volume], capture_output=True, text=True)

    def test_real_certificate_requires_sandbox_hostname(self):
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "token"
            token.write_text("s" * 32)
            for hostname in ("sandbox", "wrong-host"):
                subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                                "-keyout", str(root / "sandbox.key"), "-out", str(root / "sandbox.crt"),
                                "-subj", "/CN=" + hostname, "-addext", "subjectAltName=DNS:" + hostname],
                               check=True, capture_output=True)
                if hostname == "sandbox":
                    bootstrap.stage(root, token, root / "runtime")
                else:
                    with self.assertRaises(bootstrap.ssl.SSLCertVerificationError):
                        bootstrap.stage(root, token, root / "runtime")

    def test_staging_preserves_old_generation_on_invalid_pair(self):
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "source", root / "runtime"
            source.mkdir()
            (source / "sandbox.crt").write_bytes(b"certificate")
            (source / "sandbox.key").write_bytes(b"private-key")
            token = root / "token"
            token.write_text("s" * 32)
            with patch.object(bootstrap.ssl, "SSLContext"), patch.object(bootstrap.ssl, "create_default_context"):
                bootstrap.stage(source, token, destination)
            current = (destination / "current").readlink()
            self.assertEqual((destination / "current/SANDBOX_API_TOKEN").read_text(), "s" * 32)
            self.assertEqual((destination / "current/sandbox.key").stat().st_mode & 0o777, 0o600)
            self.assertEqual((destination / "current/sandbox.crt").stat().st_mode & 0o777, 0o644)
            token.write_text("t" * 32)
            with patch.object(bootstrap.ssl, "SSLContext") as context:
                context.return_value.load_cert_chain.side_effect = bootstrap.ssl.SSLError("secret-canary")
                with self.assertRaises(bootstrap.ssl.SSLError):
                    bootstrap.stage(source, token, destination)
            self.assertEqual((destination / "current").readlink(), current)
            self.assertEqual((destination / "current/SANDBOX_API_TOKEN").read_text(), "s" * 32)

    def test_staging_rejects_symlink_source(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sandbox.crt").symlink_to(root / "missing")
            with self.assertRaises(OSError):
                bootstrap.stage(root, root / "token", root / "runtime")


def frame(stream_type: int, payload: bytes) -> bytes:
    return bytes([stream_type, 0, 0, 0]) + struct.pack(">I", len(payload)) + payload


class DockerLogStreamTest(unittest.TestCase):
    def test_split_docker_log_stream_demultiplexes_stdout_and_stderr(self) -> None:
        body = frame(1, b"hello\n") + frame(2, b"warning\n") + frame(1, b"done\n")

        stdout, stderr = server.split_docker_log_stream(body)

        self.assertEqual(stdout, "hello\ndone\n")
        self.assertEqual(stderr, "warning\n")


class SandboxAuthenticationTest(unittest.TestCase):
    def test_revoke_only_token_cannot_authenticate_execution(self):
        import hashlib
        import hmac
        from unittest.mock import patch
        root = "s" * 32
        revoke = hmac.new(root.encode(), b"assistant-sandbox-revoke-v1", hashlib.sha256).hexdigest()
        with patch.dict(os.environ, {"SANDBOX_API_TOKEN": root}, clear=True):
            server.require_api_token("Bearer " + revoke, allow_revoke=True)
            with self.assertRaises(FakeHTTPException):
                server.require_api_token("Bearer " + revoke)

    def test_bearer_token_is_required_and_compared_exactly(self) -> None:
        from unittest.mock import patch

        with patch.dict("os.environ", {"SANDBOX_API_TOKEN": "s" * 32}, clear=True):
            with self.assertRaises(FakeHTTPException) as missing:
                server.require_api_token("")
            self.assertEqual(missing.exception.status_code, 401)
            with self.assertRaises(FakeHTTPException) as incorrect:
                server.require_api_token("Bearer " + "s" * 31 + "x")
            self.assertEqual(incorrect.exception.status_code, 401)
            with self.assertRaises(FakeHTTPException) as malformed:
                server.require_api_token("Bearer \N{LATIN SMALL LETTER E WITH ACUTE}")
            self.assertEqual(malformed.exception.status_code, 401)
            server.require_api_token("Bearer " + "s" * 32)

    def test_missing_token_fails_closed(self) -> None:
        from unittest.mock import patch

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(FakeHTTPException) as raised:
                server.require_api_token("Bearer " + "s" * 32)
        self.assertEqual(raised.exception.status_code, 503)

    def test_health_requires_the_same_bearer_token(self) -> None:
        from unittest.mock import patch

        token = "s" * 32
        with patch.dict("os.environ", {"SANDBOX_API_TOKEN": token}, clear=True), patch.object(
            server, "execution_identity", return_value="image"
        ):
            with self.assertRaises(FakeHTTPException):
                server.health()
            self.assertEqual(server.health("Bearer " + token)["status"], "ok")


class FakeDockerClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def json_request(self, method: str, path: str, body: Optional[dict[str, Any]] = None, expected: tuple[int, ...] = (200,)) -> dict[str, Any]:
        self.requests.append({"method": method, "path": path, "body": body, "expected": expected})
        return {"Id": "container-1"}


class RunContainerConfigTest(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        self.docs = patch.object(server, "DOCS_ROOT_HOST", "/installation/docs")
        self.docs.start()
        self.addCleanup(self.docs.stop)

    def open_egress(self):
        from unittest.mock import patch
        egress = patch.object(server, "EGRESS_MODE", "open")
        egress.start()
        self.addCleanup(egress.stop)

    def test_docs_mount_is_read_only_for_bind_and_volume_workspaces(self):
        from unittest.mock import patch
        for volume in ("", "assistant-test-share"):
            with self.subTest(volume=volume), patch.object(server, "SHARED_VOLUME_NAME", volume), patch.object(server, "SHARED_ROOT_HOST", "/installation/data/share"):
                _, mounts = server.run_mounts()
                docs = next(mount for mount in mounts if mount["Target"].endswith("/.assistant/docs"))
                self.assertEqual(docs["Source"], "/installation/docs")
                self.assertTrue(docs["ReadOnly"])
                self.assertEqual(docs["Type"], "bind")

    def test_missing_or_relative_documentation_source_fails_explicitly(self):
        from unittest.mock import patch
        for source in ("", "docs"):
            with self.subTest(source=source), patch.object(server, "DOCS_ROOT_HOST", source):
                with self.assertRaisesRegex(RuntimeError, "absolute host documentation path"):
                    server.run_mounts()

    @unittest.skipUnless(os.getenv("ASSISTANT_TEST_DOCKER") == "1" and os.getenv("ASSISTANT_TEST_SANDBOX_IMAGE"), "requires disposable Docker test image")
    def test_real_command_container_reads_canonical_docs_but_cannot_write_them(self):
        import subprocess
        import tempfile
        import textwrap

        image = os.environ["ASSISTANT_TEST_SANDBOX_IMAGE"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            share, docs = root / "share", root / "docs"
            share.mkdir()
            (share / ".assistant/docs").mkdir(parents=True)
            docs.mkdir()
            reference = docs / "README.md"
            reference.write_text("Canonical harness reference")
            probe = textwrap.dedent("""\
                import errno
                from pathlib import Path
                path = Path('/data/share/.assistant/docs/README.md')
                assert path.read_text() == 'Canonical harness reference'
                try:
                    path.write_text('must not change')
                except OSError as exc:
                    assert exc.errno == errno.EROFS, exc
                else:
                    raise AssertionError('documentation is writable')
                Path('/data/share/result.txt').write_text('workspace remains writable')
                print('read-only docs verified')
            """)
            driver = (
                "import time, server; "
                "request=server.CommandRequest(command=['python','-c'," + repr(probe) + "], timeout_seconds=30); "
                "result=server.execute_container(request, server.SHARED_ROOT, 30, time.monotonic()); "
                "assert result.exit_code == 0, (result.stdout, result.stderr); print(result.stdout)"
            )
            result = subprocess.run([
                "docker", "run", "--rm", "--network", "none",
                "-e", "SANDBOX_RUN_IMAGE=" + image,
                "-e", "SANDBOX_RUN_NETWORK_MODE=none",
                "-e", "SANDBOX_SHARED_ROOT_HOST=" + str(share),
                "-e", "SANDBOX_DOCS_ROOT_HOST=" + str(docs),
                "-v", "/var/run/docker.sock:/var/run/docker.sock",
                "-v", str(share) + ":/data/share",
                "-v", str(docs) + ":/data/share/.assistant/docs:ro",
                "--entrypoint", "python", image, "-c", driver,
            ], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("read-only docs verified", result.stdout)
            self.assertEqual(reference.read_text(), "Canonical harness reference")
            self.assertEqual((share / "result.txt").read_text(), "workspace remains writable")

    def test_run_container_defaults_to_docker_runtime_and_default_dns(self) -> None:
        original_volume = server.SHARED_VOLUME_NAME
        try:
            self.open_egress()
            server.SHARED_VOLUME_NAME = "assistant-test-share"
            client = FakeDockerClient()
            request = server.CommandRequest(command=["python", "--version"], timeout_seconds=5, workdir=str(server.SHARED_ROOT))

            server.create_run_container(client, "abc123", request, Path(server.SHARED_ROOT), "assistant-test-net")
        finally:
            server.SHARED_VOLUME_NAME = original_volume

        create_body = client.requests[0]["body"]
        self.assertNotIn("Runtime", create_body["HostConfig"])
        self.assertNotIn("Dns", create_body["HostConfig"])

    def test_run_container_allows_runtime_and_dns_overrides(self) -> None:
        original_volume = server.SHARED_VOLUME_NAME
        original_runtime = server.RUN_RUNTIME
        original_dns = server.RUN_DNS_SERVERS
        try:
            self.open_egress()
            server.SHARED_VOLUME_NAME = "assistant-test-share"
            server.RUN_RUNTIME = "runsc"
            server.RUN_DNS_SERVERS = ["1.1.1.1", "8.8.8.8"]
            client = FakeDockerClient()
            request = server.CommandRequest(command=["python", "--version"], timeout_seconds=5, workdir=str(server.SHARED_ROOT))

            server.create_run_container(client, "abc123", request, Path(server.SHARED_ROOT), "assistant-test-net")
        finally:
            server.SHARED_VOLUME_NAME = original_volume
            server.RUN_RUNTIME = original_runtime
            server.RUN_DNS_SERVERS = original_dns

        create_body = client.requests[0]["body"]
        self.assertEqual(create_body["HostConfig"]["Runtime"], "runsc")
        self.assertEqual(create_body["HostConfig"]["Dns"], ["1.1.1.1", "8.8.8.8"])


class ProxyDockerClient:
    """Fake Docker API that records calls and simulates a ready egress proxy."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.inspections = 0

    def json_request(self, method, path, body=None, expected=(200,)):
        self.calls.append((method, path, body))
        if path.startswith("/containers/create"):
            self.created.append(body)
            return {"Id": "proxy-1" if "-proxy-" in path else "run-1"}
        if path.endswith("/json") and path.startswith("/containers/"):
            if "proxy-1" in path:
                return {"State": {"Running": True}, "NetworkSettings": {"Networks": {
                    "assistant-sandbox-net-abc": {"IPAddress": "172.30.0.2"}}}}
            self.inspections += 1
            return {"State": {"Running": False, "ExitCode": 0}}
        return {}

    def request(self, method, path, body=None, expected=(200,)):
        self.calls.append((method, path, body))
        if path.startswith("/containers/proxy-1/logs"):
            lines = b'{"event": "ready", "port": 3128}\n{"event": "egress", "host": "example.com", "decision": "blocked", "reason": "not_approved"}\n'
            return 200, b"\x01\x00\x00\x00" + len(lines).to_bytes(4, "big") + lines
        if path.endswith("/logs?stdout=1&stderr=1"):
            return 200, b""
        if path.startswith("/containers/json") or path.startswith("/networks?"):
            return 200, b"[]"
        return 204, b""


class EgressProxyBrokerTest(unittest.TestCase):
    def run_command(self, policy):
        from unittest.mock import patch
        client = ProxyDockerClient()
        request = server.CommandRequest(command=["curl", "https://example.com"], timeout_seconds=5,
                                        workdir=str(server.SHARED_ROOT), network_policy=policy)
        with patch.object(server, "DOCS_ROOT_HOST", "/installation/docs"), \
                patch.object(server, "SHARED_VOLUME_NAME", "assistant-test-share"), \
                patch.object(server, "RUN_NETWORK_MODE", "per_run_bridge"), \
                patch.object(server, "EGRESS_MODE", "proxy"), \
                patch.object(server, "DockerClient", lambda *args, **kwargs: client), \
                patch.object(server.uuid, "uuid4", lambda: type("U", (), {"hex": "abc"})()):
            response = server.execute_container(request, Path(server.SHARED_ROOT), 5, 0.0)
        return client, response

    def test_proxied_run_is_internal_and_only_reaches_the_proxy(self):
        client, response = self.run_command({"allow": ["pypi.org"], "block": []})
        networks = [body for method, path, body in client.calls if path == "/networks/create"]
        run_network = next(body for body in networks if body["Name"] == "assistant-sandbox-net-abc")
        egress_network = next(body for body in networks if "-egress-" in body["Name"])
        self.assertTrue(run_network["Internal"])
        self.assertEqual(run_network["Options"]["com.docker.network.bridge.enable_icc"], "true")
        self.assertFalse(egress_network["Internal"])
        proxy, run = client.created
        self.assertEqual(proxy["HostConfig"]["NetworkMode"], egress_network["Name"])
        self.assertIn('EGRESS_POLICY={"allow":["pypi.org"],"block":[]}', proxy["Env"])
        self.assertEqual(run["HostConfig"]["NetworkMode"], "assistant-sandbox-net-abc")
        self.assertEqual(run["HostConfig"]["Dns"], server.DNS_SINK)
        self.assertIn("HTTPS_PROXY=http://172.30.0.2:3128", run["Env"])
        self.assertEqual(response.egress, "proxy")
        self.assertEqual(response.network_events, [{"host": "example.com", "decision": "blocked", "reason": "not_approved"}])
        removed = [path for method, path, body in client.calls if method == "DELETE"]
        self.assertEqual(removed[:2], ["/containers/run-1?force=1&v=1", "/containers/proxy-1?force=1&v=1"])
        self.assertEqual(len(removed), 4)

    def test_run_without_policy_has_no_route_out(self):
        client, response = self.run_command(None)
        self.assertEqual(len(client.created), 1)
        run = client.created[0]
        self.assertFalse(any(item.startswith("HTTPS_PROXY=") for item in run["Env"]))
        self.assertEqual(run["HostConfig"]["Dns"], server.DNS_SINK)
        self.assertEqual(response.egress, "none")


class EgressProxyTest(unittest.TestCase):
    def client_hello(self, name):
        import egress_proxy
        server_name = name.encode()
        sni = (len(server_name) + 3).to_bytes(2, "big") + b"\x00" + len(server_name).to_bytes(2, "big") + server_name
        extensions = b"\x00\x00" + len(sni).to_bytes(2, "big") + sni
        body = b"\x03\x03" + b"\x00" * 32 + b"\x00" + b"\x00\x02\x13\x01" + b"\x01\x00" + len(extensions).to_bytes(2, "big") + extensions
        return b"\x01" + len(body).to_bytes(3, "big") + body

    def test_server_name_is_extracted_from_client_hello(self):
        import egress_proxy
        self.assertEqual(egress_proxy.server_name_from_client_hello(self.client_hello("API.Example.com")), "api.example.com")
        self.assertIsNone(egress_proxy.server_name_from_client_hello(b"\x01\x00"))

    def test_blocks_win_and_wildcards_match_subdomains_only(self):
        import egress_proxy
        policy = egress_proxy.Policy(["*.example.com", "pypi.org"], ["bad.example.com"])
        self.assertIsNone(policy.decide("api.example.com"))
        self.assertEqual(policy.decide("example.com"), "not_approved")
        self.assertEqual(policy.decide("bad.example.com"), "blocked_by_admin")
        self.assertIsNone(policy.decide("pypi.org"))

    def test_proxy_rejects_unapproved_hosts_plain_http_and_ip_literals(self):
        import asyncio
        import egress_proxy

        async def scenario():
            proxy = egress_proxy.Proxy(egress_proxy.Policy(["pypi.org"], []))
            server_ = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
            port = server_.sockets[0].getsockname()[1]
            replies = []
            for request in (b"CONNECT evil.example:443 HTTP/1.1\r\n\r\n", b"GET http://pypi.org/ HTTP/1.1\r\n\r\n",
                            b"CONNECT 1.2.3.4:443 HTTP/1.1\r\n\r\n", b"CONNECT pypi.org:22 HTTP/1.1\r\n\r\n"):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(request)
                await writer.drain()
                replies.append(await reader.read())
                writer.close()
            server_.close()
            return replies

        replies = asyncio.run(scenario())
        self.assertTrue(all(reply.startswith(b"HTTP/1.1 403") for reply in replies))
        self.assertIn(b"network_access_request", replies[0])
        self.assertIn(b"X-Sandbox-Egress: plain_http", replies[1])
        self.assertIn(b"X-Sandbox-Egress: ip_literal", replies[2])
        self.assertIn(b"X-Sandbox-Egress: port_not_allowed", replies[3])

    def test_tunnel_requires_tls_server_name_to_match_the_approved_host(self):
        import asyncio
        from unittest.mock import patch
        import egress_proxy

        async def fake_resolve(host):
            return ["127.0.0.1"]

        async def scenario(server_name):
            proxy = egress_proxy.Proxy(egress_proxy.Policy(["pypi.org"], []))
            recorded = []
            proxy.events.record = lambda host, decision, reason="": recorded.append((host, decision, reason))
            server_ = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
            reader, writer = await asyncio.open_connection("127.0.0.1", server_.sockets[0].getsockname()[1])
            writer.write(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
            await writer.drain()
            established = await reader.readuntil(b"\r\n\r\n")
            hello = self.client_hello(server_name)
            writer.write(b"\x16\x03\x01" + len(hello).to_bytes(2, "big") + hello)
            await writer.drain()
            await reader.read()
            writer.close()
            server_.close()
            return established, recorded

        with patch.object(egress_proxy, "resolve_public", fake_resolve), patch.object(egress_proxy, "CONNECT_TIMEOUT", 1):
            established, recorded = asyncio.run(scenario("evil.example"))
            self.assertTrue(established.startswith(b"HTTP/1.1 200"))
            self.assertEqual(recorded, [("pypi.org", "blocked", "sni_mismatch")])
            _, recorded = asyncio.run(scenario("pypi.org"))
            self.assertNotIn(("pypi.org", "blocked", "sni_mismatch"), recorded)

    def test_private_addresses_are_refused(self):
        import asyncio
        import egress_proxy
        with self.assertRaisesRegex(ValueError, "private_address"):
            asyncio.run(egress_proxy.resolve_public("localhost"))


if __name__ == "__main__":
    unittest.main()
