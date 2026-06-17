import struct
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
sys.modules.setdefault("fastapi", fastapi_module)

pydantic_module = types.ModuleType("pydantic")
pydantic_module.BaseModel = FakeBaseModel
pydantic_module.Field = fake_field
sys.modules.setdefault("pydantic", pydantic_module)

import server


def frame(stream_type: int, payload: bytes) -> bytes:
    return bytes([stream_type, 0, 0, 0]) + struct.pack(">I", len(payload)) + payload


class DockerLogStreamTest(unittest.TestCase):
    def test_split_docker_log_stream_demultiplexes_stdout_and_stderr(self) -> None:
        body = frame(1, b"hello\n") + frame(2, b"warning\n") + frame(1, b"done\n")

        stdout, stderr = server.split_docker_log_stream(body)

        self.assertEqual(stdout, "hello\ndone\n")
        self.assertEqual(stderr, "warning\n")


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
