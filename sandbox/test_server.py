import struct
import os
import sys
import types
import unittest
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
        with patch.dict("os.environ", {"SANDBOX_API_TOKEN": token}, clear=True):
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
    def test_run_container_defaults_to_docker_runtime_and_default_dns(self) -> None:
        original_volume = server.SHARED_VOLUME_NAME
        try:
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


if __name__ == "__main__":
    unittest.main()
