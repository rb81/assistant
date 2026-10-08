import sys
import types
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda handle: {}))

psycopg_module = types.ModuleType("psycopg")
psycopg_module.connect = MagicMock()
psycopg_module.Connection = object
sys.modules.setdefault("psycopg", psycopg_module)
rows_module = types.ModuleType("psycopg.rows")
rows_module.dict_row = object()
sys.modules.setdefault("psycopg.rows", rows_module)
json_module = types.ModuleType("psycopg.types.json")
json_module.Jsonb = lambda value: value
sys.modules.setdefault("psycopg.types", types.ModuleType("psycopg.types"))
sys.modules.setdefault("psycopg.types.json", json_module)

from assistant_agent.chat_store import ChatStore


class FakeDb:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_one_result: Optional[dict[str, Any]] = None
        self.fetch_all_result: list[dict[str, Any]] = []

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        self.calls.append((sql, params))
        return self.fetch_one_result

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        return self.fetch_all_result

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.calls.append((sql, params))


class ChatStoreTest(unittest.TestCase):
    def test_create_session_inserts_title(self) -> None:
        db = FakeDb()
        db.fetch_one_result = {"id": 1, "title": "hey", "created_at": "t", "updated_at": "t"}
        store = ChatStore(db)
        row = store.create_session("hey")
        self.assertEqual(row["id"], 1)
        sql, params = db.calls[-1]
        self.assertIn("INSERT INTO chat_sessions", sql)
        self.assertEqual(params, ("hey",))

    def test_create_message_touches_session(self) -> None:
        db = FakeDb()
        db.fetch_one_result = {"id": 5, "session_id": 1, "role": "user", "kind": "chat", "content": "hi"}
        store = ChatStore(db)
        row = store.create_message(1, "user", content="hi")
        self.assertEqual(row["id"], 5)
        touch_sql, touch_params = db.calls[-1]
        self.assertIn("UPDATE chat_sessions SET updated_at", touch_sql)
        self.assertEqual(touch_params, (1,))

    def test_recent_messages_orders_ascending_after_limiting_desc(self) -> None:
        db = FakeDb()
        db.fetch_all_result = [{"id": 3}, {"id": 4}]
        store = ChatStore(db)
        rows = store.recent_messages(1, 20)
        self.assertEqual(rows, [{"id": 3}, {"id": 4}])
        sql, params = db.calls[-1]
        self.assertIn("ORDER BY id ASC", sql)
        self.assertEqual(params, (1, 20))

    def test_title_from_message_truncates_at_64_chars(self) -> None:
        store = ChatStore(FakeDb())
        long_text = "x" * 100
        title = store.title_from_message(long_text)
        self.assertEqual(len(title), 64)
        self.assertTrue(title.endswith("…"))

    def test_title_from_message_blank_falls_back(self) -> None:
        store = ChatStore(FakeDb())
        self.assertEqual(store.title_from_message("   "), "New chat")

    def test_count_recent_user_messages_passes_window(self) -> None:
        db = FakeDb()
        db.fetch_one_result = {"count": 3}
        store = ChatStore(db)
        self.assertEqual(store.count_recent_user_messages(60), 3)
        sql, params = db.calls[-1]
        self.assertIn("role = 'user'", sql)
        self.assertEqual(params, (60,))


if __name__ == "__main__":
    unittest.main()
