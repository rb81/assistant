import sys
import types
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda handle: {}))
import os
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
    sys.modules["psycopg"].connect = MagicMock(return_value=mock_conn)
    from assistant_agent import api  # noqa: PLC0415

    return api


class FakeDb:
    def __init__(self) -> None:
        self.manual_job_calls: list[dict[str, Any]] = []
        self.metadata_updates: list[dict[str, Any]] = []
        self.created = {"id": 77, "thread_id": None}

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        return dict(self.created)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        if "UPDATE jobs SET metadata" in sql:
            self.metadata_updates.append(params[0])

    def create_manual_job(self, subject: str, body: str, from_address: str, **kwargs: Any) -> dict[str, Any]:
        self.manual_job_calls.append({"subject": subject, "body": body, "kwargs": kwargs})
        return dict(self.created)


class ChatEscalationSourceTest(unittest.TestCase):
    def test_chat_escalation_sets_source_subject_and_extra_metadata(self) -> None:
        api = load_api()
        fake = FakeDb()
        original_db = api.db
        api.db = fake  # type: ignore[assignment]
        try:
            request = api.WorkspaceJobRequest(message="do the thing")
            api.create_workspace_job(
                request,
                source="chat_escalation",
                subject_override="Chat: do the thing",
                extra_metadata={"chat_session_id": 9},
            )
        finally:
            api.db = original_db
        self.assertEqual(fake.manual_job_calls[0]["subject"], "Chat: do the thing")
        metadata = fake.metadata_updates[0]
        self.assertEqual(metadata["source"], "chat_escalation")
        self.assertEqual(metadata["chat_session_id"], 9)

    def test_default_call_still_uses_workspace_source_and_derived_subject(self) -> None:
        api = load_api()
        fake = FakeDb()
        original_db = api.db
        api.db = fake  # type: ignore[assignment]
        try:
            request = api.WorkspaceJobRequest(message="hello", active_path="notes.md")
            api.create_workspace_job(request)
        finally:
            api.db = original_db
        self.assertEqual(fake.manual_job_calls[0]["subject"], "Workspace: notes.md")
        self.assertEqual(fake.metadata_updates[0]["source"], "workspace")
        self.assertNotIn("chat_session_id", fake.metadata_updates[0])


if __name__ == "__main__":
    unittest.main()
