import asyncio
import sys
import types
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock, patch

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

from fastapi import HTTPException


def load_api():
    sys.modules["psycopg"].connect = MagicMock(return_value=mock_conn)
    from assistant_agent import api  # noqa: PLC0415

    return api


def collect_stream(response) -> list[str]:
    """StreamingResponse wraps a sync generator in an async one (via
    starlette's iterate_in_threadpool) — drain it with a throwaway event loop."""
    async def _collect() -> list[str]:
        return [chunk async for chunk in response.body_iterator]

    return asyncio.run(_collect())


class FakeChatDb:
    """Minimal fake standing in for both `api.db` and the ChatStore's db."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.sessions: list[dict[str, Any]] = []
        self.jobs: dict[int, dict[str, Any]] = {}
        self._next_message_id = 1

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        if "FROM jobs WHERE id" in sql:
            return self.jobs.get(params[0])
        if "FROM chat_sessions WHERE id" in sql:
            for s in self.sessions:
                if s["id"] == params[0]:
                    return s
            return None
        if "COUNT(*) AS count FROM chat_messages" in sql:
            return {"count": len([m for m in self.messages if m["role"] == "user"])}
        return None

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if "FROM jobs WHERE id = ANY" in sql:
            ids = params[0]
            return [self.jobs[i] for i in ids if i in self.jobs]
        if "FROM chat_messages" in sql and "session_id = %s" in sql:
            session_id = params[0]
            rows = [m for m in self.messages if m["session_id"] == session_id]
            return rows[-params[1]:] if "ORDER BY id ASC" in sql and len(params) > 1 else rows
        return []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        return None


class FakeChatStore:
    def __init__(self, backing: FakeChatDb) -> None:
        self.backing = backing
        self._next_session_id = 1

    def create_session(self, title: str) -> dict[str, Any]:
        session = {"id": self._next_session_id, "title": title}
        self._next_session_id += 1
        self.backing.sessions.append(session)
        return session

    def get_session(self, session_id: int) -> Optional[dict[str, Any]]:
        for s in self.backing.sessions:
            if s["id"] == session_id:
                return s
        return None

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.backing.sessions)[:limit]

    def recent_messages(self, session_id: int, limit: int) -> list[dict[str, Any]]:
        rows = [m for m in self.backing.messages if m["session_id"] == session_id]
        return rows[-limit:]

    def list_messages(self, session_id: int) -> list[dict[str, Any]]:
        return [m for m in self.backing.messages if m["session_id"] == session_id]

    def create_message(self, session_id, role, kind="chat", content="", job_id=None, tokens_used=None) -> dict[str, Any]:
        row = {
            "id": self.backing._next_message_id,
            "session_id": session_id,
            "role": role,
            "kind": kind,
            "content": content,
            "job_id": job_id,
            "tokens_used": tokens_used,
        }
        self.backing._next_message_id += 1
        self.backing.messages.append(row)
        return row

    def count_recent_user_messages(self, window_seconds: int) -> int:
        return len([m for m in self.backing.messages if m["role"] == "user"])

    def title_from_message(self, message: str) -> str:
        text = " ".join(str(message or "").split()).strip()
        return text[:64] or "New chat"


class ChatEndpointsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.api = load_api()
        self.fake_db = FakeChatDb()
        self.fake_store = FakeChatStore(self.fake_db)
        self.original_db = self.api.db
        self.api.db = self.fake_db  # type: ignore[assignment]
        self.chat_store_patch = patch.object(self.api, "chat_store", return_value=self.fake_store)
        self.chat_store_patch.start()

    def tearDown(self) -> None:
        self.chat_store_patch.stop()
        self.api.db = self.original_db

    def test_new_session_casual_reply_persists_user_and_assistant_rows(self) -> None:
        with patch.object(
            self.api.chat_responder,
            "generate_reply_events",
            return_value=iter([{"type": "delta", "text": "Hi!"}, {"type": "done", "usage": {"total_tokens": 5}}]),
        ):
            response = self.api.chat_send_message("new", self.api.ChatMessageRequest(message="hey"))
            chunks = collect_stream(response)
        events = [self.api.json.loads(c[len("data: "):]) for c in chunks if c.startswith("data: ")]
        self.assertEqual(events[0]["type"], "session")
        self.assertEqual(events[-2], {"type": "delta", "text": "Hi!"})
        self.assertEqual(events[-1], {"type": "done"})
        roles = [(m["role"], m["kind"], m["content"]) for m in self.fake_db.messages]
        self.assertEqual(roles, [("user", "chat", "hey"), ("assistant", "chat", "Hi!")])

    def test_escalation_creates_job_and_job_ref_row(self) -> None:
        session = self.fake_store.create_session("t")
        self.fake_db.jobs[42] = {"id": 42, "status": "queued"}
        with patch.object(
            self.api.chat_responder,
            "generate_reply_events",
            return_value=iter([{"type": "escalated", "task_summary": "check calendar"}]),
        ), patch.object(self.api, "create_workspace_job", return_value={"id": 42, "status": "queued"}) as mock_create:
            response = self.api.chat_send_message(str(session["id"]), self.api.ChatMessageRequest(message="check my calendar"))
            chunks = collect_stream(response)
        events = [self.api.json.loads(c[len("data: "):]) for c in chunks if c.startswith("data: ")]
        self.assertEqual(events[-1], {"type": "escalated", "job_id": 42, "text": "On it — I'll work on this now."})
        mock_create.assert_called_once()
        self.assertEqual(mock_create.call_args.kwargs["source"], "chat_escalation")
        self.assertEqual(mock_create.call_args.kwargs["extra_metadata"], {"chat_session_id": session["id"]})
        job_ref_rows = [m for m in self.fake_db.messages if m["kind"] == "job_ref"]
        self.assertEqual(len(job_ref_rows), 1)
        self.assertEqual(job_ref_rows[0]["job_id"], 42)

    def test_missing_session_returns_404(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self.api.chat_send_message("999", self.api.ChatMessageRequest(message="hi"))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_rate_limit_returns_429(self) -> None:
        for _ in range(25):
            self.fake_db.messages.append({"session_id": 1, "role": "user", "kind": "chat", "content": "x", "job_id": None})
        with self.assertRaises(HTTPException) as ctx:
            self.api.chat_send_message("new", self.api.ChatMessageRequest(message="one more"))
        self.assertEqual(ctx.exception.status_code, 429)

    def test_active_job_ref_blocks_new_message_with_409(self) -> None:
        session = self.fake_store.create_session("t")
        self.fake_store.create_message(session["id"], "user", content="do work")
        self.fake_store.create_message(session["id"], "assistant", kind="job_ref", content="On it", job_id=42)
        self.fake_db.jobs[42] = {"id": 42, "status": "running"}
        with self.assertRaises(HTTPException) as ctx:
            self.api.chat_send_message(str(session["id"]), self.api.ChatMessageRequest(message="hello again"))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_list_sessions_and_messages_endpoints(self) -> None:
        session = self.fake_store.create_session("t")
        self.fake_store.create_message(session["id"], "user", content="hi")
        sessions_response = self.api.chat_sessions()
        self.assertEqual(len(sessions_response["sessions"]), 1)
        messages_response = self.api.chat_session_messages(session["id"])
        self.assertEqual(len(messages_response["messages"]), 1)

    def test_unknown_session_messages_returns_404(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self.api.chat_session_messages(12345)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_job_ref_final_response_folds_into_llm_context(self) -> None:
        session = self.fake_store.create_session("t")
        self.fake_store.create_message(session["id"], "user", content="check my calendar")
        self.fake_store.create_message(
            session["id"], "assistant", kind="job_ref", content="On it — I'll work on this now.", job_id=42
        )
        self.fake_db.jobs[42] = {"id": 42, "status": "completed", "metadata": {"final_response": "You have a 3pm meeting."}}

        captured: dict[str, Any] = {}

        def fake_generate(config, history, user_message):
            captured["history"] = history
            yield {"type": "delta", "text": "Got it!"}
            yield {"type": "done", "usage": None}

        with patch.object(self.api.chat_responder, "generate_reply_events", side_effect=fake_generate):
            response = self.api.chat_send_message(str(session["id"]), self.api.ChatMessageRequest(message="what did you find?"))
            collect_stream(response)

        job_ref_row = next(row for row in captured["history"] if row.get("kind") == "job_ref")
        self.assertEqual(job_ref_row["content"], "[Completed by the full task pipeline] You have a 3pm meeting.")

    def test_job_ref_without_final_response_keeps_ack_text(self) -> None:
        session = self.fake_store.create_session("t")
        self.fake_store.create_message(session["id"], "user", content="check my calendar")
        self.fake_store.create_message(
            session["id"], "assistant", kind="job_ref", content="On it — I'll work on this now.", job_id=42
        )
        self.fake_db.jobs[42] = {"id": 42, "status": "failed", "metadata": {}}

        captured: dict[str, Any] = {}

        def fake_generate(config, history, user_message):
            captured["history"] = history
            yield {"type": "done", "usage": None}

        with patch.object(self.api.chat_responder, "generate_reply_events", side_effect=fake_generate):
            response = self.api.chat_send_message(str(session["id"]), self.api.ChatMessageRequest(message="any update?"))
            collect_stream(response)

        job_ref_row = next(row for row in captured["history"] if row.get("kind") == "job_ref")
        self.assertEqual(job_ref_row["content"], "On it — I'll work on this now.")


class UsageCostChatTest(unittest.TestCase):
    def test_usage_cost_summary_includes_chat_messages_in_query(self) -> None:
        api = load_api()
        fake = MagicMock()
        fake.fetch_one.return_value = {
            "lifetime_total": 1.5, "month_total": 0.5, "average_per_job": 0.1,
            "job_count": 3, "charged_job_count": 2, "api_call_count": 10,
        }
        original_db = api.db
        api.db = fake
        try:
            summary = api.usage_cost_summary()
        finally:
            api.db = original_db
        sql = fake.fetch_one.call_args.args[0]
        self.assertIn("chat_messages", sql)
        self.assertEqual(summary["lifetime_total"], 1.5)


if __name__ == "__main__":
    unittest.main()
