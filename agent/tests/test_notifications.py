import sys
import types
import unittest
from typing import Any, Optional

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
from assistant_agent.notifications import _already_sent, _fingerprint, notify_admin_job_failure


class FakeDatabase:
    def __init__(self, fetch_result: Optional[dict[str, Any]] = None):
        self.fetch_result = fetch_result
        self.fetch_params: Optional[tuple[Any, ...]] = None
        self.logged: list[dict[str, Any]] = []

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        self.fetch_params = params
        return self.fetch_result

    def log_event(self, *args: Any, **kwargs: Any) -> None:
        self.logged.append({"args": args, "kwargs": kwargs})


class NotificationDedupeTest(unittest.TestCase):
    def test_fingerprint_ignores_notification_source(self) -> None:
        self.assertEqual(
            _fingerprint(31, "needs_review", "max iterations reached", "task-agent iteration budget"),
            _fingerprint(31, "needs_review", "max iterations reached", "heartbeat rule"),
        )

    def test_already_sent_matches_same_status_and_reason(self) -> None:
        db = FakeDatabase({"id": 27})

        sent = _already_sent(
            db, 31, "different-fingerprint", "needs_review", "max iterations reached"
        )

        self.assertTrue(sent)
        self.assertEqual(db.fetch_params, (31, "different-fingerprint", "needs_review", "max iterations reached"))

    def test_notify_returns_without_logging_when_same_failure_was_sent(self) -> None:
        db = FakeDatabase({"id": 27})

        notify_admin_job_failure(
            db,  # type: ignore[arg-type]
            AppConfig({"agent": {}}),
            {"id": 31, "thread_id": "thread-1"},
            "needs_review",
            "max iterations reached",
            "heartbeat rule",
        )

        self.assertEqual(db.logged, [])


if __name__ == "__main__":
    unittest.main()
