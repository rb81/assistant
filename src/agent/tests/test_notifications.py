import sys
import types
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock, patch

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
from assistant_agent.notifications import _already_sent, _body, _fingerprint, notify_admin_job_failure


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
    def test_review_notification_uses_standalone_confirmation_for_current_job_version(self):
        db = MagicMock()
        db.fetch_one.return_value = {"updated_at": "2026-10-27T09:00:00+00:00"}
        with patch("assistant_agent.notifications._already_sent", return_value=False), patch(
            "assistant_agent.notifications.admin_configured", return_value=True
        ), patch("assistant_agent.notifications.smtp_configured", return_value=True), patch(
            "assistant_agent.notifications._body", return_value="Review diagnostics"
        ), patch("assistant_agent.email_approvals.EmailApprovals.request", return_value=True) as approval, patch(
            "assistant_agent.notifications.ToolRuntime.email_send"
        ) as ordinary_email:
            notify_admin_job_failure(db, AppConfig({"agent": {}}), {"id": 31}, "needs_review", "limit", "worker")
        approval.assert_called_once_with(
            "review:31:2026-10-27T09:00:00+00:00",
            {"kind": "review", "job_id": 31, "version": "2026-10-27T09:00:00+00:00"},
            "Review diagnostics",
        )
        ordinary_email.assert_not_called()

    def test_admin_input_email_requests_reply_without_retired_dashboard(self) -> None:
        from assistant_agent.task_agent import TaskAgent

        agent = TaskAgent.__new__(TaskAgent)
        agent.config = AppConfig({"agent": {"admin": {"email": "admin@example.com"}, "app": {"base_url": "https://localhost:8000"}}})
        agent.db = MagicMock()
        agent.db.latest_thread_emails.return_value = [{"message_id": "<request@example.com>"}]
        runtime = MagicMock()
        with patch("assistant_agent.task_agent.admin_configured", return_value=True), patch(
            "assistant_agent.task_agent.smtp_configured", return_value=True
        ):
            agent.email_admin_for_input({"id": 11, "thread_id": "thread-1"}, runtime, "May I proceed?")

        sent = runtime.email_send.call_args.kwargs
        self.assertIn("May I proceed?", sent["body"])
        self.assertIn("Reply to this email with your approval or clarification", sent["body"])
        self.assertNotIn("dashboard", sent["body"].lower())
        self.assertNotIn("localhost", sent["body"])
        self.assertEqual(sent["in_reply_to"], "<request@example.com>")

    def test_review_email_preserves_reply_approval_without_retired_dashboard(self) -> None:
        config = AppConfig({"agent": {"app": {"base_url": "https://localhost:8000"}}})
        diagnostics = {
            "explanation": "Repeated tool failure",
            "usage": {"total_tokens": 100, "token_pct": 0.01, "iterations_used": 3, "iteration_pct": 6, "api_calls": 3},
            "limits": {"max_tokens": 1000000, "max_iterations": 50},
        }
        with patch("assistant_agent.notifications._latest_email", return_value={}), patch(
            "assistant_agent.notifications._latest_checkpoint", return_value={}
        ), patch("assistant_agent.notifications._diagnostics", return_value=diagnostics):
            body = _body(FakeDatabase(), config, {"id": 11}, "needs_review", "Tool failure", "task-agent")

        self.assertIn("reply to the separate OTP/hash confirmation", body)
        self.assertIn("Both limits will be increased by 20%.", body)
        self.assertNotIn("dashboard", body.lower())
        self.assertNotIn("localhost", body)

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
