import sys
import types
import unittest
from unittest.mock import patch

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda handle: {}))

from assistant_agent.chat_responder import (
    ESCALATE_TOOL,
    _consume_stream,
    build_messages,
    condense_transcript,
    generate_reply_events,
    parse_sse_lines,
)
from assistant_agent.config import AppConfig


def make_config(**overrides):
    values = {
        "agent": {
            "llm": {"model": "anthropic/claude-sonnet-4.6", "temperature": 0.2, "max_tokens_per_call": 4096},
            "chat": {},
            "prompt": {"agent_file": "AGENT.md"},
        }
    }
    for path, value in overrides.items():
        current = values
        parts = path.split(".")
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = value
    return AppConfig(values)


class ConsumeStreamTest(unittest.TestCase):
    def test_plain_text_reply_yields_deltas_then_done(self) -> None:
        chunks = [
            {"choices": [{"delta": {"content": "Hey"}}]},
            {"choices": [{"delta": {"content": " there!"}}, ], "usage": None},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"total_tokens": 42}},
        ]
        events = list(_consume_stream(iter(chunks)))
        self.assertEqual(events, [
            {"type": "delta", "text": "Hey"},
            {"type": "delta", "text": " there!"},
            {"type": "done", "usage": {"total_tokens": 42}},
        ])

    def test_tool_call_arguments_split_across_chunks_accumulate(self) -> None:
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "escalate_to_job", "arguments": '{"task_'}}
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": 'summary": "check '}}
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": 'my calendar"}'}}
            ]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        events = list(_consume_stream(iter(chunks)))
        self.assertEqual(events, [{"type": "escalated", "task_summary": "check my calendar"}])

    def test_malformed_tool_call_arguments_fall_back_to_empty(self) -> None:
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "escalate_to_job", "arguments": "not json"}}
            ]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        events = list(_consume_stream(iter(chunks)))
        self.assertEqual(events, [{"type": "escalated", "task_summary": ""}])


class ParseSseLinesTest(unittest.TestCase):
    def test_parses_data_lines_and_stops_at_done_sentinel(self) -> None:
        lines = [
            b'data: {"choices": [{"delta": {"content": "hi"}}]}\n',
            b"\n",
            b"data: [DONE]\n",
            b'data: {"choices": [{"delta": {"content": "unreachable"}}]}\n',
        ]
        parsed = list(parse_sse_lines(iter(lines)))
        self.assertEqual(parsed, [{"choices": [{"delta": {"content": "hi"}}]}])

    def test_skips_blank_and_non_data_lines(self) -> None:
        lines = [b"\n", b": comment\n", b'data: {"a": 1}\n']
        parsed = list(parse_sse_lines(iter(lines)))
        self.assertEqual(parsed, [{"a": 1}])


class BuildMessagesTest(unittest.TestCase):
    def test_includes_persona_history_and_new_message(self) -> None:
        config = make_config()
        with patch("assistant_agent.chat_responder.load_agent_prompt", return_value="You are Arqis."):
            messages = build_messages(config, [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello!"}], "how are you?")
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("You are Arqis.", messages[0]["content"])
        self.assertEqual(messages[1], {"role": "user", "content": "hi"})
        self.assertEqual(messages[2], {"role": "assistant", "content": "hello!"})
        self.assertEqual(messages[-1], {"role": "user", "content": "how are you?"})

    def test_blank_history_rows_are_skipped(self) -> None:
        config = make_config()
        with patch("assistant_agent.chat_responder.load_agent_prompt", return_value="persona"):
            messages = build_messages(config, [{"role": "user", "content": "   "}], "hi")
        self.assertEqual(len(messages), 2)  # system + new user message only

    def test_system_prompt_includes_completed_task_framing_instruction(self) -> None:
        config = make_config()
        with patch("assistant_agent.chat_responder.load_agent_prompt", return_value="persona"):
            messages = build_messages(config, [], "hi")
        self.assertIn("Completed by the full task pipeline", messages[0]["content"])


class CondenseTranscriptTest(unittest.TestCase):
    def test_caps_to_max_turns_and_max_chars(self) -> None:
        history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"} for i in range(30)]
        text = condense_transcript(history, max_turns=10, max_chars=4000)
        self.assertEqual(text.count("\n") + 1, 10)
        self.assertIn("turn 29", text)
        self.assertNotIn("turn 0\n", text)

    def test_truncates_to_max_chars_keeping_the_tail(self) -> None:
        history = [{"role": "user", "content": "x" * 100} for _ in range(5)]
        text = condense_transcript(history, max_turns=10, max_chars=50)
        self.assertEqual(len(text), 50)


class GenerateReplyEventsFallbackTest(unittest.TestCase):
    def test_stream_setup_failure_falls_back_to_non_streamed_completion(self) -> None:
        config = make_config()
        with patch("assistant_agent.chat_responder.load_agent_prompt", return_value="persona"), \
             patch("assistant_agent.chat_responder._stream_request", side_effect=RuntimeError("no network")), \
             patch("assistant_agent.chat_responder.LlmClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.chat.return_value = {
                "choices": [{"message": {"content": "Hey! All good."}}],
                "usage": {"total_tokens": 10},
            }
            events = list(generate_reply_events(config, [], "hi"))
        self.assertEqual(events, [
            {"type": "delta", "text": "Hey! All good."},
            {"type": "done", "usage": {"total_tokens": 10}},
        ])
        mock_client.chat.assert_called_once()
        self.assertEqual(mock_client.chat.call_args.kwargs.get("tools") or mock_client.chat.call_args.args[1], [ESCALATE_TOOL])

    def test_fallback_tool_call_yields_escalated(self) -> None:
        config = make_config()
        with patch("assistant_agent.chat_responder.load_agent_prompt", return_value="persona"), \
             patch("assistant_agent.chat_responder._stream_request", side_effect=RuntimeError("no network")), \
             patch("assistant_agent.chat_responder.LlmClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.chat.return_value = {
                "choices": [{"message": {"tool_calls": [
                    {"function": {"name": "escalate_to_job", "arguments": '{"task_summary": "send an email"}'}}
                ]}}],
            }
            events = list(generate_reply_events(config, [], "please email bob"))
        self.assertEqual(events, [{"type": "escalated", "task_summary": "send an email"}])


if __name__ == "__main__":
    unittest.main()
