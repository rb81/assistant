import os
import sys
import types
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda handle: {}))
os.environ.setdefault("DATABASE_URL", "postgresql://stub:stub@localhost/stub")

mock_cursor = MagicMock()
mock_cursor.__enter__.return_value = mock_cursor
mock_cursor.__exit__.return_value = None
mock_conn = MagicMock()
mock_conn.__enter__.return_value = mock_conn
mock_conn.__exit__.return_value = None
mock_conn.cursor.return_value = mock_cursor

psycopg_module = types.ModuleType("psycopg")
psycopg_module.connect = MagicMock(return_value=mock_conn)
psycopg_module.Connection = object
sys.modules.setdefault("psycopg", psycopg_module)
rows_module = types.ModuleType("psycopg.rows")
rows_module.dict_row = object()
sys.modules.setdefault("psycopg.rows", rows_module)
json_module = types.ModuleType("psycopg.types.json")
json_module.Jsonb = lambda value: value
sys.modules.setdefault("psycopg.types", types.ModuleType("psycopg.types"))
sys.modules.setdefault("psycopg.types.json", json_module)


def load_api():
    """Import assistant_agent.api lazily (repo test convention).

    Other test modules may have installed a psycopg stub whose connect()
    returns None; make connect usable as a context manager before api's
    module-level Database init runs.
    """
    sys.modules["psycopg"].connect = MagicMock(return_value=mock_conn)
    from assistant_agent import api  # noqa: PLC0415

    return api


class FakeDb:
    def __init__(self, parent_row: dict[str, Any]) -> None:
        self.parent_row = parent_row
        self.manual_job_calls: list[dict[str, Any]] = []
        self.created = {"id": 99, "thread_id": None}

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        if "FROM jobs" in sql and params and params[0] == self.parent_row["id"]:
            return self.parent_row
        return dict(self.created)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        return None

    def create_manual_job(self, subject: str, body: str, from_address: str, **kwargs: Any) -> dict[str, Any]:
        self.manual_job_calls.append({"subject": subject, "kwargs": kwargs})
        self.created["thread_id"] = kwargs.get("thread_id")
        return dict(self.created)


class WorkspaceFollowUpThreadTest(unittest.TestCase):
    def test_follow_up_job_reuses_parent_thread_id(self) -> None:
        api = load_api()
        parent = {
            "id": 5,
            "status": "completed",
            "thread_id": "<manual-1@assistant.local>",
            "metadata": {"source": "workspace"},
        }
        fake = FakeDb(parent)
        original_db = api.db
        api.db = fake  # type: ignore[assignment]
        try:
            request = api.WorkspaceJobRequest(message="follow up question")
            api.workspace_job_message(5, request)
        finally:
            api.db = original_db
        self.assertEqual(len(fake.manual_job_calls), 1)
        self.assertEqual(
            fake.manual_job_calls[0]["kwargs"].get("thread_id"),
            "<manual-1@assistant.local>",
        )

    def test_new_job_does_not_pass_thread_id(self) -> None:
        api = load_api()
        fake = FakeDb({"id": -1, "status": "completed", "thread_id": "x", "metadata": {}})
        original_db = api.db
        api.db = fake  # type: ignore[assignment]
        try:
            api.create_workspace_job(api.WorkspaceJobRequest(message="hello"))
        finally:
            api.db = original_db
        self.assertEqual(fake.manual_job_calls[0]["kwargs"].get("thread_id"), None)


if __name__ == "__main__":
    unittest.main()
