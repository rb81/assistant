import io
import json
import sys
import tempfile
import types
import unittest
import urllib.error
from typing import Any
from unittest.mock import patch

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda handle: {}))
psycopg_module = types.ModuleType("psycopg")
psycopg_module.connect = lambda *args, **kwargs: None
psycopg_module.Connection = object
sys.modules.setdefault("psycopg", psycopg_module)
rows_module = types.ModuleType("psycopg.rows")
rows_module.dict_row = object()
sys.modules.setdefault("psycopg.rows", rows_module)
json_module = types.ModuleType("psycopg.types.json")
json_module.Jsonb = lambda value: value
sys.modules.setdefault("psycopg.types", types.ModuleType("psycopg.types"))
sys.modules.setdefault("psycopg.types.json", json_module)
markdown_module = types.ModuleType("markdown_it")


class FakeMarkdownIt:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def enable(self, *args: Any, **kwargs: Any) -> "FakeMarkdownIt":
        return self

    def render(self, value: str) -> str:
        return value


markdown_module.MarkdownIt = FakeMarkdownIt
sys.modules.setdefault("markdown_it", markdown_module)

from assistant_agent.config import AppConfig
from assistant_agent.tools import SandboxAttemptsExhausted, SandboxHostConfigurationError, ToolRuntime


class FakeResponse:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def http_error(status: int, detail: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://sandbox:8080/execute",
        status,
        "error",
        {},
        io.BytesIO(json.dumps({"detail": detail}).encode("utf-8")),
    )


def retry_config(shared_root: str) -> AppConfig:
    return AppConfig(
        {
            "agent": {
                "filesystem": {"shared_root": shared_root, "require_mount": False},
                "sandbox": {
                    "base_url": "http://sandbox:8080",
                    "max_attempts": 3,
                    "retry_backoff_seconds": 0,
                },
                "limits": {"tool_timeout_command_seconds": 5},
            }
        }
    )


class SandboxCommandRetryTest(unittest.TestCase):
    def runtime(self, shared_root: str) -> ToolRuntime:
        return ToolRuntime(None, retry_config(shared_root), {"id": 57, "thread_id": "thread-1"})  # type: ignore[arg-type]

    def test_retries_transient_http_error_then_returns_attempt_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            calls = 0

            def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise http_error(502, "docker daemon unavailable")
                return FakeResponse({"exit_code": 0, "stdout": "ok\n", "stderr": "", "duration_ms": 10, "timed_out": False})

            with patch("urllib.request.urlopen", fake_urlopen):
                result = self.runtime(temp_dir).command_execute(["python", "--version"])

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["retry_errors"][0]["attempt"], 1)
        self.assertIn("docker daemon unavailable", result["retry_errors"][0]["error"])

    def test_exhausted_retries_raise_structured_tool_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:

            def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
                raise urllib.error.URLError("connection refused")

            with patch("urllib.request.urlopen", fake_urlopen):
                with self.assertRaises(SandboxAttemptsExhausted) as raised:
                    self.runtime(temp_dir).command_execute(["python", "--version"])

        self.assertEqual(raised.exception.attempts, 3)
        self.assertEqual(len(raised.exception.attempt_errors), 3)
        self.assertIn("connection refused", raised.exception.reason)

    def test_non_transient_http_error_does_not_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            calls = 0

            def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
                nonlocal calls
                calls += 1
                raise http_error(400, "workdir does not exist")

            with patch("urllib.request.urlopen", fake_urlopen):
                with self.assertRaises(Exception) as raised:
                    self.runtime(temp_dir).command_execute(["python", "--version"])

        self.assertEqual(calls, 1)
        self.assertIn("HTTP 400", str(raised.exception))

    def test_unknown_runsc_runtime_is_non_retryable_configuration_error(self) -> None:
        detail = (
            'Docker API POST /containers/create?name=assistant-sandbox-run-abc failed with HTTP 400: '
            '{"message":"unknown or invalid runtime name: runsc"}'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            calls = 0

            def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
                nonlocal calls
                calls += 1
                raise http_error(502, detail)

            with patch("urllib.request.urlopen", fake_urlopen):
                with self.assertRaises(SandboxHostConfigurationError) as raised:
                    self.runtime(temp_dir).command_execute(["python", "--version"])

        self.assertEqual(calls, 1)
        self.assertIn("Docker does not have that runtime registered", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
