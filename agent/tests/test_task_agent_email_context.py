import os
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Optional
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
notifications_module = types.ModuleType("assistant_agent.notifications")
notifications_module.notify_admin_job_failure = lambda *args, **kwargs: None
sys.modules.setdefault("assistant_agent.notifications", notifications_module)

from assistant_agent.config import AppConfig
from assistant_agent.database import Database
from assistant_agent.task_agent import TaskAgent


class TaskAgentEmailContextTest(unittest.TestCase):
    def agent(self) -> TaskAgent:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        shared_root = Path(temp_dir.name)
        # Create dummy AGENT.md so load_agent_prompt() succeeds
        (shared_root / "AGENT.md").write_text("You are a test assistant.", encoding="utf-8")
        os.environ["AGENT_CONFIG"] = str(shared_root / "agent.yaml")
        self.addCleanup(lambda: os.environ.pop("AGENT_CONFIG", None))
        agent = TaskAgent.__new__(TaskAgent)
        agent.config = AppConfig(
            {
                "agent": {
                    "email": {
                        "context_body_preview_chars": 8,
                        "initial_context_prior_full_body_char_limit": 10,
                        "max_initial_context_body_chars": 20,
                    },
                    "limits": {
                        "max_prompt_chars": 200,
                        "summarization_keep_recent": 2,
                    },
                    "filesystem": {"shared_root": temp_dir.name},
                    "projects": {"enabled": False},
                    "deep_research": {"enabled": False},
                }
            }
        )
        return agent

    def email(self, email_id: int, body: str) -> dict[str, Any]:
        return {
            "id": email_id,
            "message_id": "<%s@example.com>" % email_id,
            "context_type": "email",
            "direction": "inbound",
            "thread_item_id": "email:%s" % email_id,
            "in_reply_to": "",
            "from_address": "user@example.com",
            "to_addresses": ["agent@example.com"],
            "cc_addresses": [],
            "subject": "Subject %s" % email_id,
            "received_at": "2026-06-06T10:00:00Z",
            "body_text": body,
            "attachments": [],
        }

    def outbound_email(self, log_id: int, body: str) -> dict[str, Any]:
        return {
            "id": log_id,
            "outbound_log_id": log_id,
            "context_type": "outbound_email",
            "direction": "outbound",
            "thread_item_id": "outbound:%s" % log_id,
            "message_id": "<agent-%s@example.com>" % log_id,
            "in_reply_to": "<1@example.com>",
            "from_address": "assistant@local",
            "to_addresses": ["user@example.com"],
            "cc_addresses": [],
            "subject": "Re: Subject",
            "sent_at": "2026-06-06T10:05:00Z",
            "received_at": "2026-06-06T10:05:00Z",
            "created_at": "2026-06-06T10:05:00Z",
            "body_text": body,
            "attachments": [],
        }

    def context(self, emails: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "emails": emails,
            "thread_messages": emails,
            "instructions": [],
            "reminder": None,
            "memory_context": {},
            "artifacts_by_email": {},
            "async_context": {"projects": [], "deep_research_runs": []},
            "prior_actions": [],
        }

    def test_initial_context_includes_latest_full_and_only_small_prior_full(self) -> None:
        agent = self.agent()
        old_large = "older body is too long"
        old_small = "small"
        latest = "latest body is included in full"
        messages = agent.messages_from_base_context(
            {"id": 7, "thread_id": "thread-1", "task_summary": "Do it"},
            self.context([self.email(1, old_large), self.email(2, old_small), self.email(3, latest)]),
            include_full_email_context=True,
        )
        content = messages[1]["content"]

        self.assertIn("Body preview", content)
        self.assertIn("call email_read with email_id 1", content)
        self.assertNotIn(old_large, content)
        self.assertIn("Body (5 chars):\nsmall", content)
        self.assertIn("Body (99 chars):\nlatest body is inclu", content)
        self.assertIn("body truncated at 20 chars; call email_read with email_id 3", content)

    def test_compact_context_previews_every_email_body(self) -> None:
        agent = self.agent()
        old_small = "small"
        latest = "latest body is included in full"
        messages = agent.messages_from_base_context(
            {"id": 7, "thread_id": "thread-1", "task_summary": "Do it"},
            self.context([self.email(2, old_small), self.email(3, latest)]),
            include_full_email_context=False,
        )
        content = messages[1]["content"]

        self.assertIn("Body preview", content)
        self.assertNotIn("Body (5 chars):\nsmall", content)
        self.assertNotIn(latest, content)
        self.assertIn("call email_read with email_id 3", content)

    def test_substantive_tool_call_excludes_meta_tools(self) -> None:
        agent = self.agent()

        self.assertFalse(
            agent.has_substantive_tool_call(
                [
                    {
                        "function": {
                            "name": "get_tool_specs",
                        }
                    }
                ]
            )
        )
        self.assertTrue(
            agent.has_substantive_tool_call(
                [
                    {
                        "function": {
                            "name": "file_read",
                        }
                    }
                ]
            )
        )

    def test_malformed_tool_call_arguments_are_returned_as_tool_error(self) -> None:
        agent = self.agent()
        events: list[dict[str, Any]] = []

        class FakeDb:
            def log_event(self, *args: Any, **kwargs: Any) -> None:
                events.append({"args": args, "kwargs": kwargs})

        agent.db = FakeDb()  # type: ignore[assignment]
        messages: list[dict[str, Any]] = []

        parsed = agent.parse_tool_call(
            {"id": 31},
            messages,
            {
                "id": "call-1",
                "function": {
                    "name": "file_write",
                    "arguments": '{"path": "x" "content": "missing comma"}',
                },
            },
        )

        self.assertIsNone(parsed)
        self.assertEqual(events[0]["args"][1], "error")
        self.assertEqual(events[0]["kwargs"]["tool_name"], "file_write")
        self.assertIn("Invalid tool arguments JSON", messages[0]["content"])
        self.assertEqual(messages[0]["tool_call_id"], "call-1")

    def test_non_object_tool_call_arguments_are_returned_as_tool_error(self) -> None:
        agent = self.agent()

        class FakeDb:
            def log_event(self, *args: Any, **kwargs: Any) -> None:
                pass

        agent.db = FakeDb()  # type: ignore[assignment]
        messages: list[dict[str, Any]] = []

        parsed = agent.parse_tool_call(
            {"id": 31},
            messages,
            {
                "id": "call-1",
                "function": {
                    "name": "file_read",
                    "arguments": '["not", "an", "object"]',
                },
            },
        )

        self.assertIsNone(parsed)
        self.assertIn("tool arguments must be a JSON object", messages[0]["content"])

    def test_transient_processing_exception_requeues_without_admin_email(self) -> None:
        class FakeDb:
            def __init__(self) -> None:
                self.statuses: list[tuple[int, str, Optional[str]]] = []

            def claim_job(self, locked_by: str) -> dict[str, Any]:
                return {"id": 31, "attempts": 2, "max_attempts": 3}

            def update_job_status(self, job_id: int, status: str, last_error: Optional[str] = None) -> None:
                self.statuses.append((job_id, status, last_error))

        agent = self.agent()
        db = FakeDb()
        agent.db = db  # type: ignore[assignment]
        agent.locked_by = "task-agent:test"
        agent.process_job = lambda job: (_ for _ in ()).throw(RuntimeError("bad tool json"))  # type: ignore[method-assign]
        notifications: list[tuple[Any, ...]] = []

        with (
            patch("assistant_agent.task_agent.notify_admin_job_failure", lambda *args: notifications.append(args)),
            patch("assistant_agent.task_agent.LOGGER.exception", lambda *args, **kwargs: None),
        ):
            self.assertTrue(agent.run_once())

        self.assertEqual(db.statuses, [(31, "queued", "bad tool json")])
        self.assertEqual(notifications, [])

    def test_final_processing_exception_needs_review_sends_admin_email(self) -> None:
        class FakeDb:
            def __init__(self) -> None:
                self.statuses: list[tuple[int, str, Optional[str]]] = []

            def claim_job(self, locked_by: str) -> dict[str, Any]:
                return {"id": 31, "attempts": 3, "max_attempts": 3}

            def update_job_status(self, job_id: int, status: str, last_error: Optional[str] = None) -> None:
                self.statuses.append((job_id, status, last_error))

        agent = self.agent()
        db = FakeDb()
        agent.db = db  # type: ignore[assignment]
        agent.config = AppConfig({"agent": {}})
        agent.locked_by = "task-agent:test"
        agent.process_job = lambda job: (_ for _ in ()).throw(RuntimeError("bad tool json"))  # type: ignore[method-assign]
        notifications: list[tuple[Any, ...]] = []

        with (
            patch("assistant_agent.task_agent.notify_admin_job_failure", lambda *args: notifications.append(args)),
            patch("assistant_agent.task_agent.LOGGER.exception", lambda *args, **kwargs: None),
        ):
            self.assertTrue(agent.run_once())

        self.assertEqual(db.statuses, [(31, "needs_review", "bad tool json")])
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0][3:6], ("needs_review", "bad tool json", "task-agent exception"))

    def test_messages_for_call_uses_llm_summary_when_prompt_too_large(self) -> None:
        agent = self.agent()
        agent.llm_history_summary = lambda messages: "summarized older context"  # type: ignore[method-assign]
        base_messages = [{"role": "system", "content": "base"}, {"role": "user", "content": "x" * 12000}]
        history = [
            {"role": "assistant", "content": "older assistant"},
            {"role": "tool", "name": "file_read", "content": "older tool"},
            {"role": "assistant", "content": "recent assistant"},
            {"role": "user", "content": "recent user"},
        ]

        messages = agent.messages_for_call(base_messages, history, window=8)
        contents = "\n".join(str(message.get("content") or "") for message in messages)

        self.assertIn("COMPACTED CONVERSATION SUMMARY", contents)
        self.assertIn("summarized older context", contents)
        self.assertIn("recent assistant", contents)
        self.assertIn("recent user", contents)

    def test_prior_action_summary_is_included_in_context(self) -> None:
        agent = self.agent()
        context = self.context([self.email(1, "latest")])
        context["prior_actions"] = ["Created reminder #1 'Daily summary' scheduled for 2026-06-08T05:00:00Z, recurrence every 1 day"]

        messages = agent.messages_from_base_context(
            {"id": 7, "thread_id": "thread-1", "task_summary": "Do it"},
            context,
            include_full_email_context=False,
        )
        content = messages[1]["content"]

        self.assertIn("Prior actions already taken in this job:", content)
        self.assertIn("Created reminder #1", content)
        self.assertIn("Do not repeat durable side effects", content)

    def test_task_limits_use_admin_review_override(self) -> None:
        agent = self.agent()
        agent.config = AppConfig(
            {
                "agent": {
                    "limits": {
                        "max_iterations_per_task": 50,
                        "max_tokens_per_task": 1000,
                    }
                }
            }
        )
        job = {
            "metadata": {
                "admin_review_override": {
                    "max_iterations_per_task": 75,
                    "max_tokens_per_task": 2500,
                }
            }
        }

        self.assertEqual(agent.task_limits(job), (75, 2500))

    def test_admin_review_override_resumes_from_latest_checkpoint(self) -> None:
        agent = self.agent()

        class FakeDb:
            def fetch_one(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
                return {
                    "message_history": [{"role": "system", "content": "base"}, {"role": "assistant", "content": "progress"}],
                    "reason": "token budget exceeded",
                    "iteration_count": 12,
                    "token_count": 1000,
                }

        agent.db = FakeDb()  # type: ignore[assignment]
        job = {
            "id": 7,
            "metadata": {
                "admin_review_override": {
                    "max_tokens_per_task": 2500,
                    "instruction": "Finish with the existing research.",
                }
            },
        }

        messages = agent.admin_review_resume_messages(job)

        self.assertEqual(messages[0]["content"], "base")
        self.assertEqual(messages[1]["content"], "progress")
        self.assertIn("ADMIN REVIEW OVERRIDE", messages[2]["content"])
        self.assertIn("Finish with the existing research.", messages[2]["content"])
        self.assertIn("Admin token budget override: 2500", messages[2]["content"])

    def test_context_includes_prior_agent_sent_replies(self) -> None:
        agent = self.agent()
        first = self.email(1, "First question")
        sent = self.outbound_email(99, "Done.")
        latest = self.email(2, "Follow-up question")
        context = self.context([first, latest])
        context["thread_messages"] = [first, sent, latest]

        messages = agent.messages_from_base_context(
            {"id": 7, "thread_id": "thread-1", "task_summary": "Do it"},
            context,
            include_full_email_context=True,
        )
        content = messages[1]["content"]

        self.assertIn("Sent Email Log ID: 99", content)
        self.assertIn("Message-ID: <agent-99@example.com>", content)
        self.assertIn("Body (5 chars):\nDone.", content)
        self.assertIn("Completion requirement: before task_complete, send a substantive email_send reply to user@example.com.", content)
        self.assertLess(content.index("Email ID: 1"), content.index("Sent Email Log ID: 99"))
        self.assertLess(content.index("Sent Email Log ID: 99"), content.index("Email ID: 2"))

    def test_compact_context_does_not_suggest_email_read_for_sent_log(self) -> None:
        agent = self.agent()
        sent = self.outbound_email(99, "This sent reply body is long")
        context = self.context([self.email(2, "Follow-up")])
        context["thread_messages"] = [sent, self.email(2, "Follow-up")]

        messages = agent.messages_from_base_context(
            {"id": 7, "thread_id": "thread-1", "task_summary": "Do it"},
            context,
            include_full_email_context=False,
        )
        content = messages[1]["content"]

        self.assertIn("Sent Email Log ID: 99", content)
        self.assertIn("Sent email body omitted after preview.", content)
        self.assertNotIn("call email_read with email_id 99", content)


class DatabaseThreadMessagesTest(unittest.TestCase):
    def test_latest_thread_messages_merges_inbound_and_sent_agent_replies(self) -> None:
        db = Database.__new__(Database)

        def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
            if "FROM emails" in sql:
                return [
                    {
                        "id": 2,
                        "message_id": "<2@example.com>",
                        "thread_id": "thread-1",
                        "from_address": "user@example.com",
                        "to_addresses": ["assistant@local"],
                        "cc_addresses": [],
                        "subject": "Re: Subject",
                        "body_text": "Follow-up",
                        "attachments": [],
                        "received_at": "2026-06-06T10:10:00Z",
                        "created_at": "2026-06-06T10:10:00Z",
                    },
                    {
                        "id": 1,
                        "message_id": "<1@example.com>",
                        "thread_id": "thread-1",
                        "from_address": "user@example.com",
                        "to_addresses": ["assistant@local"],
                        "cc_addresses": [],
                        "subject": "Subject",
                        "body_text": "Question",
                        "attachments": [],
                        "received_at": "2026-06-06T10:00:00Z",
                        "created_at": "2026-06-06T10:00:00Z",
                    },
                ]
            if "FROM outbound_email_logs" in sql:
                return [
                    {
                        "id": 99,
                        "job_id": 7,
                        "thread_id": "thread-1",
                        "to_addresses": ["user@example.com"],
                        "cc_addresses": [],
                        "subject": "Re: Subject",
                        "body_text": "Answer",
                        "attachments": [],
                        "provider_message_id": "<agent-99@example.com>",
                        "in_reply_to": "<1@example.com>",
                        "status": "sent",
                        "sent_at": "2026-06-06T10:05:00Z",
                        "created_at": "2026-06-06T10:05:00Z",
                    }
                ]
            raise AssertionError("unexpected query: %s" % sql)

        db.fetch_all = fetch_all  # type: ignore[method-assign]

        messages = db.latest_thread_messages("thread-1", limit=3)

        self.assertEqual([message["thread_item_id"] for message in messages], ["email:1", "outbound:99", "email:2"])
        self.assertEqual(messages[1]["message_id"], "<agent-99@example.com>")
        self.assertEqual(messages[1]["direction"], "outbound")


if __name__ == "__main__":
    unittest.main()
