import json
import os
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
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
notifications_module = types.ModuleType("assistant_agent.notifications")
notifications_module.notify_admin_job_failure = lambda *args, **kwargs: None
sys.modules.setdefault("assistant_agent.notifications", notifications_module)

from assistant_agent.config import AppConfig
from assistant_agent.database import Database
from assistant_agent.task_agent import TaskAgent


def raw(value):
    return getattr(value, "obj", value)


class TaskAgentEmailContextTest(unittest.TestCase):
    def test_idle_task_poll_does_not_run_indexing(self):
        agent = self.agent()
        agent.db = MagicMock()
        agent.db.fetch_one.return_value = None
        with patch("assistant_agent.task_agent.EmbeddingScheduler") as scheduler:
            assert not agent.run_once()
            scheduler.assert_not_called()
        agent.db.claim_job.assert_not_called()

    def test_task_waits_for_indexing_and_keeps_gate_during_execution(self):
        agent = self.agent()
        agent.db = MagicMock()
        agent.db.fetch_one.return_value = {"id": 31}
        agent.locked_by = "test-worker"
        job = {"id": 31}
        agent.db.claim_job.return_value = job
        agent.process_job = MagicMock()
        with patch("assistant_agent.task_agent.EmbeddingScheduler") as scheduler:
            gate = scheduler.return_value.execution.return_value
            gate.__enter__.return_value = False

            def process(current):
                gate.__enter__.assert_called_once()
                gate.__exit__.assert_not_called()

            agent.process_job.side_effect = process
            assert agent.run_once()
            gate.__exit__.assert_called_once()
        agent.db.log_event.assert_called_once_with(
            31, "timeout", output_data={"reason": "embedding_index_incomplete",
                                       "retrieval": "keyword_only", "wait_seconds": 60},
        )

    def test_unreachable_requester_question_goes_to_admin_as_plain_message_after_pausing(self):
        agent = self.agent()
        agent.db = MagicMock()
        agent.db.latest_thread_emails.return_value = []
        job = {"id": 12, "thread_id": "thread-12", "metadata": {}}
        agent.record_system_task_notification = MagicMock()
        with patch("assistant_agent.task_agent.smtp_configured", return_value=True), patch.object(
            agent, "message_admin", return_value=True
        ) as send:
            def paused(*args):
                updates = [call.args[0] for call in agent.db.fetch_one.call_args_list]
                self.assertTrue(any("status = 'waiting'" in sql for sql in updates))
                return True
            send.side_effect = paused
            self.assertTrue(agent.handle_request_input(
                job, [], MagicMock(), "request_input",
                {"recipient": "requester", "question": "Which voice?"}, "call-12",
            ))
        send.assert_called_once_with(job, "Which voice?")
        pause = next(call for call in agent.db.fetch_one.call_args_list if "status = 'waiting'" in call.args[0])
        self.assertTrue(raw(pause.args[1][2])["waiting_for_admin"])
        agent.db.update_job_status.assert_not_called()

    def test_admin_requester_questions_are_ordinary_replies_not_confirmations(self):
        for route in ({"channel": "email", "to": "Admin <admin@example.com>"}, {"channel": "telegram", "chat_id": 42}):
            with self.subTest(route=route):
                agent = self.agent()
                agent.config.values["agent"]["admin"] = {"email": "admin@example.com"}
                agent.db = MagicMock()
                job = {"id": 12, "thread_id": "thread-12", "metadata": {"reply_route": route, "auto_reply": True}}
                agent.defer_stale_tool_calls = MagicMock(return_value=False)
                agent.record_system_task_notification = MagicMock()
                with patch.object(agent, "message_admin") as admin, patch.object(
                    agent, "requester_is_admin", return_value=True
                ), patch("assistant_agent.task_agent.ReplyRouter") as router:
                    router.return_value.send.return_value = {"status": "sent"}
                    self.assertTrue(agent.handle_request_input(
                        job, [], MagicMock(), "request_input",
                        {"recipient": "admin", "question": "Which voice?"}, "call-12",
                    ))
                router.return_value.send.assert_called_once_with("Which voice?", purpose="clarification")
                admin.assert_not_called()
                pause = next(call for call in agent.db.fetch_one.call_args_list if "status = 'waiting'" in call.args[0])
                self.assertTrue(raw(pause.args[1][2])["waiting_for_input"])

    def test_requester_is_admin_matches_email_and_telegram_routes(self):
        agent = self.agent()
        agent.config.values["agent"]["admin"] = {"email": "admin@example.com"}
        self.assertTrue(agent.requester_is_admin({"metadata": {"reply_route": {"channel": "email", "to": "Admin <ADMIN@example.com>"}}}))
        self.assertFalse(agent.requester_is_admin({"metadata": {"reply_route": {"channel": "email", "to": "other@example.com"}}}))
        with patch("assistant_agent.approval_channels.telegram_admins", return_value=["42"]):
            self.assertTrue(agent.requester_is_admin({"metadata": {"reply_route": {"channel": "telegram", "chat_id": 42}}}))
            self.assertFalse(agent.requester_is_admin({"metadata": {"reply_route": {"channel": "telegram", "chat_id": 7}}}))

    def test_external_requester_clarification_remains_an_ordinary_email(self):
        agent = self.agent()
        agent.config.values["agent"]["admin"] = {"email": "admin@example.com"}
        agent.db = MagicMock()
        agent.db.latest_thread_emails.return_value = [{
            "from_address": "Requester <requester@example.com>", "message_id": "<request@example.com>",
        }]
        job = {"id": 12, "thread_id": "thread-12", "metadata": {}}
        runtime = MagicMock()
        runtime.email_send.return_value = {"status": "sent"}
        with patch("assistant_agent.task_agent.smtp_configured", return_value=True):
            self.assertTrue(agent.email_original_sender_for_input(job, runtime, "Which voice?"))
        self.assertEqual(runtime.email_send.call_args.kwargs["to"], ["Requester <requester@example.com>"])
        self.assertEqual(runtime.email_send.call_args.kwargs["body"], "Which voice?")

    def test_requester_clarification_delivery_failures_fall_back_to_admin(self):
        for route, result in (
            ({"channel": "telegram", "chat_id": 42}, {"status": "failed"}),
            ({"channel": "email", "to": "requester@example.com"}, {"status": "uncertain"}),
        ):
            with self.subTest(route=route, result=result):
                agent = self.agent()
                agent.db = MagicMock()
                job = {"id": 12, "thread_id": "thread-12", "metadata": {"reply_route": route}}
                runtime = MagicMock()
                with patch("assistant_agent.task_agent.ReplyRouter") as router:
                    router.return_value.send.return_value = result
                    self.assertFalse(agent.email_original_sender_for_input(job, runtime, "Which voice?"))
                agent.db.latest_thread_emails.return_value = [{
                    "from_address": "Requester <requester@example.com>",
                }]
                with patch("assistant_agent.task_agent.smtp_configured", return_value=True):
                    runtime.email_send.return_value = result
                    self.assertFalse(agent.email_original_sender_for_input(
                        {"id": 12, "thread_id": "thread-12", "metadata": {}},
                        runtime, "Which voice?",
                    ))

    def test_undeliverable_admin_question_moves_job_to_review(self):
        agent = self.agent()
        agent.db = MagicMock()
        agent.record_system_task_notification = MagicMock()
        job = {"id": 12, "thread_id": "thread-12", "metadata": {}}
        with patch.object(agent, "message_admin", return_value=False), patch(
            "assistant_agent.task_agent.notify_admin_job_failure"
        ) as notify:
            self.assertTrue(agent.handle_request_input(
                job, [], MagicMock(), "request_input", {"recipient": "admin", "question": "Which account?"}, "call-12",
            ))
        self.assertEqual(agent.db.update_job_status.call_args.args[:2], (12, "needs_review"))
        notify.assert_called_once()

    def test_job_naming_uses_cheap_model_without_changing_task(self):
        agent = self.agent()
        agent.db = MagicMock()
        agent.config.values["agent"]["naming"] = {"model": "example/cheap"}
        job = {"id": 12, "task_summary": "Create a female voice note and send it on Telegram", "metadata": {}}
        with patch("assistant_agent.task_agent.LlmClient") as llm:
            llm.return_value.chat.return_value = {"choices": [{"message": {"content": '"Female Voice Note"'}}]}
            agent.name_job(job)
        self.assertEqual(job["metadata"]["title"], "Female Voice Note")
        self.assertEqual(job["task_summary"], "Create a female voice note and send it on Telegram")
        self.assertEqual(llm.call_args.kwargs, {
            "model": "example/cheap", "max_tokens": 60, "timeout_seconds": 15, "allow_fallback": False,
        })

    def test_naming_timeout_keeps_readable_fallback_and_manual_names_are_preserved(self):
        agent = self.agent()
        agent.db = MagicMock()
        job = {"id": 12, "task_summary": "  A readable\n task summary  ", "metadata": {}}
        with patch("assistant_agent.task_agent.LlmClient", side_effect=TimeoutError):
            agent.name_job(job)
        self.assertEqual(job["metadata"]["title"], "A readable task summary")
        agent.db.reset_mock()
        job["metadata"]["title"] = "My chosen name"
        with patch("assistant_agent.task_agent.LlmClient") as llm:
            agent.name_job(job)
        llm.assert_not_called()
        agent.db.fetch_one.assert_not_called()

    def test_naming_transient_failure_never_uses_expensive_fallback(self):
        from assistant_agent.llm_client import LlmClient, _TransientLlmError
        agent = self.agent()
        agent.config.values["agent"]["llm"] = {"fallback_model": "example/expensive"}
        with patch("assistant_agent.llm_client.read_secret", return_value="test-key"):
            client = LlmClient(agent.config, model="example/cheap", allow_fallback=False)
        with patch.object(client, "_chat_with_model", side_effect=_TransientLlmError("rate limited")) as chat:
            with self.assertRaises(RuntimeError):
                client.chat([], [])
        chat.assert_called_once_with("example/cheap", [], [])

    def test_admin_question_pauses_with_resume_checkpoint_before_message_is_sent(self):
        agent = self.agent()
        agent.db = MagicMock()
        agent.db.fetch_one.return_value = {"id": 55}
        agent.record_system_task_notification = MagicMock()
        job = {"id": 12, "thread_id": "thread-12", "metadata": {}, "_enabled_tool_names": {"task_complete", "reminder_create"}}
        messages = [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "call-12", "function": {"name": "request_input"}},
            {"id": "call-13", "function": {"name": "reminder_create"}},
        ]}]

        def send(job, question):
            self.assertTrue(any("status = 'waiting'" in call.args[0] for call in agent.db.fetch_one.call_args_list))
            self.assertEqual(question, "Which account?")
            return True

        with patch.object(agent, "message_admin", side_effect=send) as message:
            stopped = agent.handle_request_input(
                job, messages, MagicMock(), "request_input",
                {"recipient": "admin", "question": "Which account?"}, "call-12",
            )
        self.assertTrue(stopped)
        message.assert_called_once()
        saved = next(call for call in agent.db.fetch_one.call_args_list if "agent_checkpoints" in call.args[0])
        history = raw(saved.args[1][1])
        self.assertEqual([item.get("tool_call_id") for item in history if item["role"] == "tool"], ["call-12", "call-13"])
        self.assertIn("not_executed", history[-1]["content"])
        pause = next(call for call in agent.db.fetch_one.call_args_list if "status = 'waiting'" in call.args[0])
        self.assertEqual(raw(pause.args[1][2])["resume_checkpoint_id"], 55)
        self.assertEqual(raw(pause.args[1][2])["resume_tools"], ["reminder_create", "task_complete"])

    def test_resume_history_restores_checkpoint_with_new_replies_and_guidance(self):
        agent = self.agent()
        agent.db = MagicMock()
        saved = [{"role": "assistant", "content": "Planning the tracker"}]
        agent.db.fetch_one.return_value = {"message_history": saved, "reason": "resume: admin input requested: Which account?",
                                           "created_at": "2025-01-01"}
        agent.db.fetch_all.side_effect = [
            [{"channel": "telegram", "sender_id": "42", "body_text": "Use the work account"}],
            [{"instruction": "Send the report on Mondays", "created_by": "telegram-admin"}],
        ]
        history = agent.resume_history({"id": 12, "thread_id": "thread-12", "metadata": {"resume_checkpoint_id": 55}})
        self.assertEqual(history[0], saved[0])
        self.assertIn("Use the work account", history[-1]["content"])
        self.assertIn("Send the report on Mondays", history[-1]["content"])
        self.assertIn("Do not repeat completed steps", history[-1]["content"])
        self.assertIn("resume_checkpoint_id", agent.db.execute.call_args.args[0])
        self.assertEqual(agent.resume_history({"id": 12, "metadata": {}}), [])

    def test_model_and_sandbox_tools_explain_internal_authentication(self):
        agent = self.agent()
        enabled = set()
        with patch("assistant_agent.task_agent.available_function_names", return_value={"llm_service", "command_execute"}):
            result = agent.load_tool_specs({"id": 7}, {"tools": ["llm_service", "command_execute"]}, enabled)
        self.assertEqual(result["loaded"], ["command_execute", "llm_service"])
        self.assertIn("Authentication is handled internally", result["guardrails"])
        self.assertIn("Never search environment variables or secret files", result["guardrails"])
        messages = agent.messages_from_base_context(
            {"id": 7, "thread_id": "thread-1", "task_summary": "Generate audio"}, self.context([]),
            include_full_email_context=True,
        )
        self.assertIn("Never search for, read or print API keys", messages[0]["content"])
        self.assertIn("do not recreate authenticated integrations", messages[0]["content"])
        with patch("assistant_agent.task_agent.available_function_names", return_value={"telegram_send"}):
            telegram = agent.load_tool_specs({"id": 7}, {"tools": ["telegram_send"]}, set())
        self.assertIn("voice_note: true", telegram["guardrails"])
        self.assertIn("never announce", telegram["guardrails"])

    def test_nested_model_tokens_stop_the_tool_batch_and_checkpoint_parent(self):
        agent = self.agent()
        agent.db = MagicMock()
        agent.db.get_state.return_value = None
        job = {"id": 123, "metadata": {}}
        calls = [{"id": "a", "function": {"name": "llm_service", "arguments": "{}"}},
                 {"id": "b", "function": {"name": "file_write", "arguments": "{}"}}]
        response = {"choices": [{"message": {"role": "assistant", "tool_calls": calls}}],
                    "usage": {"total_tokens": 1}}
        agent.update_channel_status = MagicMock()
        agent.name_pending_workstream = MagicMock()
        agent.build_base_context = MagicMock(return_value={})
        agent.messages_from_base_context = MagicMock(return_value=[{"role": "system", "content": "test"}])
        agent.admin_review_resume_messages = MagicMock(return_value=[])
        agent.initial_tool_names = MagicMock(return_value=set())
        agent.task_limits = MagicMock(return_value=(5, 20))
        agent.still_owns_job = MagicMock(return_value=True)
        agent.inject_new_context_if_needed = MagicMock()
        agent.messages_for_call = MagicMock(return_value=[])
        agent.tools_for_call = MagicMock(return_value=[])
        agent.defer_stale_tool_calls = MagicMock(return_value=False)
        agent.parse_tool_call = MagicMock(return_value=("llm_service", {}))
        agent.prune_tool_results = MagicMock()
        agent.create_checkpoint = MagicMock()

        def handle(job, history, runtime, call_id, name, arguments, enabled):
            runtime.model_service_tokens += 30
            return False

        agent.handle_tool_call = MagicMock(side_effect=handle)
        with patch("assistant_agent.task_agent.LlmClient") as llm, patch("assistant_agent.task_agent.notify_admin_job_failure"):
            llm.return_value.chat.return_value = response
            agent.process_job(job)
        self.assertEqual(agent.handle_tool_call.call_count, 1)
        self.assertEqual(agent.create_checkpoint.call_args.args[3], 31)
        agent.db.update_job_status.assert_called_once_with(123, "needs_review", last_error="token budget exceeded")

    def agent(self) -> TaskAgent:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        shared_root = Path(temp_dir.name)
        # Create dummy AGENT.md so load_agent_prompt() succeeds
        (shared_root / "AGENT.md").write_text("You are a test assistant.", encoding="utf-8")
        os.environ["AGENT_CONFIG"] = str(shared_root / "agent.yaml")
        self.addCleanup(lambda: os.environ.pop("AGENT_CONFIG", None))
        agent = TaskAgent.__new__(TaskAgent)
        agent.memory_steward = MagicMock()
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
        from assistant_agent.tool_result_cache import ToolResultCache
        agent.tool_result_cache = ToolResultCache(agent.config)
        agent.locked_by = "test-worker"
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
            self.assertTrue(agent.run_ready_job())

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
            self.assertTrue(agent.run_ready_job())

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

    def test_prior_action_summary_ignores_non_object_project_payload(self) -> None:
        agent = self.agent()

        class FakeDb:
            def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
                return [
                    {
                        "event_type": "tool_result",
                        "tool_name": "project_create",
                        "input_data": {"title": "Existing project"},
                        "output_data": {"project": "legacy project payload"},
                    }
                ]

        agent.db = FakeDb()  # type: ignore[assignment]

        self.assertEqual(agent.prior_action_summary(319), [])

    def test_prior_action_summary_flags_side_effect_without_recorded_outcome(self) -> None:
        agent = self.agent()

        class FakeDb:
            def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
                return [
                    {"event_type": "tool_call", "tool_name": "file_write", "input_data": {"path": "a.txt"}},
                    {"event_type": "tool_result", "tool_name": "file_write", "output_data": {"path": "a.txt"}},
                    {"event_type": "tool_call", "tool_name": "email_send", "input_data": {"to": ["user@example.com"], "subject": "Re: Tests"}},
                    {"event_type": "status_change", "output_data": {"status": "needs_review", "reason": "stalled"}},
                ]

        agent.db = FakeDb()  # type: ignore[assignment]

        lines = agent.prior_action_summary(17)

        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "Wrote workspace file a.txt")
        self.assertIn("email_send was started to user@example.com, subject 'Re: Tests'", lines[1])
        self.assertIn("outcome was never recorded", lines[1])
        self.assertEqual(lines[2], "Job status changed to needs_review: stalled")

    def test_still_owns_job_requires_running_status_and_same_worker(self) -> None:
        agent = self.agent()
        agent.locked_by = "task-agent:a"
        rows: list[Optional[dict[str, Any]]] = [
            {"status": "running", "locked_by": "task-agent:a"},
            {"status": "needs_review", "locked_by": None},
            {"status": "running", "locked_by": "task-agent:b"},
            {"status": "cancelled", "locked_by": None},
            None,
        ]

        class FakeDb:
            def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
                return rows.pop(0)

        agent.db = FakeDb()  # type: ignore[assignment]

        with patch("assistant_agent.task_agent.LOGGER.warning", lambda *args, **kwargs: None):
            results = [agent.still_owns_job(17) for _ in range(5)]

        self.assertEqual(results, [True, False, False, False, False])

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

    def test_telegram_reply_channel_and_no_duplicate_delivery_are_explicit(self) -> None:
        agent = self.agent()
        messages = agent.messages_from_base_context(
            {
                "id": 7,
                "thread_id": "thread-1",
                "task_summary": "Answer the question",
                "metadata": {"reply_route": {"channel": "telegram"}},
            },
            self.context([]),
            include_full_email_context=True,
        )

        self.assertIn("Reply channel: telegram", messages[1]["content"])


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
            if "FROM request_messages" in sql or "FROM channel_deliveries" in sql:
                return []
            raise AssertionError("unexpected query: %s" % sql)

        db.fetch_all = fetch_all  # type: ignore[method-assign]

        messages = db.latest_thread_messages("thread-1", limit=3)

        self.assertEqual([message["thread_item_id"] for message in messages], ["email:1", "outbound:99", "email:2"])
        self.assertEqual(messages[1]["message_id"], "<agent-99@example.com>")
        self.assertEqual(messages[1]["direction"], "outbound")

    def test_latest_thread_messages_applies_composite_cursor_to_received_order(self) -> None:
        db = Database.__new__(Database)
        queries: list[tuple[str, tuple[Any, ...]]] = []
        cursor = {
            "timestamp": "2026-06-06T10:00:00Z",
            "id": 5,
            "thread_item_id": "request:5",
        }

        def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
            queries.append((sql, params))
            return []

        db.fetch_all = fetch_all  # type: ignore[method-assign]
        db.latest_thread_messages("thread-1", limit=2, through=json.dumps(cursor))

        self.assertEqual(len(queries), 4)
        for query, params in queries:
            self.assertIn(" < (%s::timestamptz, %s::bigint, %s::text)", query)
            self.assertIn(cursor["timestamp"], params)
            self.assertIn(cursor["id"], params)
            self.assertIn(cursor["thread_item_id"], params)
        self.assertIn("COALESCE(received_at, created_at)", queries[0][0])
        self.assertIn("COALESCE(r.received_at, r.created_at)", queries[2][0])

    def test_thread_message_order_has_stable_item_tiebreaker(self) -> None:
        db = Database.__new__(Database)
        messages = [
            {"received_at": "2026-06-06T10:00:00Z", "id": 5, "thread_item_id": "request:5"},
            {"received_at": "2026-06-06T10:00:00Z", "id": 5, "thread_item_id": "email:5"},
        ]
        self.assertEqual(
            [row["thread_item_id"] for row in sorted(messages, key=db.thread_message_sort_key)],
            ["email:5", "request:5"],
        )


if __name__ == "__main__":
    unittest.main()
