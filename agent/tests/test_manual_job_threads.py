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

from assistant_agent.database import Database


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: Optional[tuple[Any, ...]] = None) -> None:
        self.calls.append((" ".join(sql.split()), params or ()))

    def fetchone(self) -> dict[str, Any]:
        return self.rows.pop(0)


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self._cursor


def database_with(cursor: FakeCursor) -> Database:
    db = Database.__new__(Database)
    db.connect = lambda: FakeConnection(cursor)  # type: ignore[method-assign]
    return db


class CreateManualJobThreadTest(unittest.TestCase):
    def cursor(self) -> FakeCursor:
        return FakeCursor(rows=[{"id": 11}, {"id": 42, "thread_id": "ignored"}])

    def test_default_thread_id_is_generated_message_id(self) -> None:
        cursor = self.cursor()
        database_with(cursor).create_manual_job("Subject", "Body")

        email_sql, email_params = cursor.calls[0]
        job_sql, job_params = cursor.calls[1]
        self.assertIn("INSERT INTO emails", email_sql)
        self.assertIn("INSERT INTO jobs", job_sql)
        message_id, thread_id = email_params[0], email_params[1]
        self.assertEqual(message_id, thread_id)
        self.assertEqual(job_params[0], thread_id)

    def test_explicit_thread_id_is_reused_for_email_and_job(self) -> None:
        cursor = self.cursor()
        database_with(cursor).create_manual_job(
            "Subject", "Body", thread_id="<manual-1@assistant.local>"
        )

        _, email_params = cursor.calls[0]
        _, job_params = cursor.calls[1]
        self.assertEqual(email_params[1], "<manual-1@assistant.local>")
        self.assertEqual(job_params[0], "<manual-1@assistant.local>")
        self.assertNotEqual(email_params[0], "<manual-1@assistant.local>")


if __name__ == "__main__":
    unittest.main()
