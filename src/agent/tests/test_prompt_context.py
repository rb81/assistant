import os
import json
import tempfile
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda handle: {}))

from assistant_agent.config import AppConfig
from assistant_agent.llm_client import LlmClient
from assistant_agent.time_utils import default_timezone, parse_datetime
from assistant_agent.prompt_context import (
    agent_prompt_path,
    build_prompt_context,
    ensure_prompt_files,
    load_agent_prompt,
    refresh_request_context,
    validate_agent_prompt,
)


def prompt_config(shared_root: str, extras: dict = None) -> AppConfig:
    values = {"agent": {"filesystem": {"shared_root": shared_root}}}
    if extras:
        values["agent"].update(extras)
    return AppConfig(values)


class EnsurePromptFilesTest(unittest.TestCase):
    def test_workspace_is_created_without_copying_documentation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ensure_prompt_files(prompt_config(temp_dir))
            self.assertTrue(root.is_dir())
            self.assertFalse((root / ".assistant/docs").exists())

    def test_existing_documentation_is_never_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = prompt_config(temp_dir)
            doc_path = root / ".assistant/docs/SANDBOX_CAPABILITIES.md"
            doc_path.parent.mkdir(parents=True)
            doc_path.write_text("Mounted reference", encoding="utf-8")
            with patch.object(Path, "write_bytes", side_effect=AssertionError("read-only docs")):
                ensure_prompt_files(config)
            self.assertEqual(doc_path.read_text(encoding="utf-8"), "Mounted reference")


class AgentPromptPathTest(unittest.TestCase):
    def test_resolves_configured_file_in_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            agent_md = config_dir / "AGENT.md"
            agent_md.write_text("# Test", encoding="utf-8")
            os.environ["AGENT_CONFIG"] = str(config_dir / "agent.yaml")
            try:
                config = prompt_config(temp_dir, {"prompt": {"agent_file": "AGENT.md"}})
                result = agent_prompt_path(config)
                self.assertEqual(result, agent_md.resolve())
            finally:
                del os.environ["AGENT_CONFIG"]

    def test_falls_back_to_example_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            example = config_dir / "AGENT.md.example"
            example.write_text("# Example", encoding="utf-8")
            os.environ["AGENT_CONFIG"] = str(config_dir / "agent.yaml")
            try:
                config = prompt_config(temp_dir, {"prompt": {"agent_file": "AGENT.md"}})
                result = agent_prompt_path(config)
                self.assertEqual(result, example.resolve())
            finally:
                del os.environ["AGENT_CONFIG"]


class ValidateAgentPromptTest(unittest.TestCase):
    def test_raises_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["AGENT_CONFIG"] = str(Path(temp_dir) / "agent.yaml")
            try:
                config = prompt_config(temp_dir, {"prompt": {"agent_file": "AGENT.md"}})
                with self.assertRaisesRegex(RuntimeError, "not found"):
                    validate_agent_prompt(config)
            finally:
                del os.environ["AGENT_CONFIG"]

    def test_raises_when_file_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            agent_md = config_dir / "AGENT.md"
            agent_md.write_text("", encoding="utf-8")
            os.environ["AGENT_CONFIG"] = str(config_dir / "agent.yaml")
            try:
                config = prompt_config(temp_dir, {"prompt": {"agent_file": "AGENT.md"}})
                with self.assertRaisesRegex(RuntimeError, "empty"):
                    validate_agent_prompt(config)
            finally:
                del os.environ["AGENT_CONFIG"]

    def test_passes_when_file_has_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            agent_md = config_dir / "AGENT.md"
            agent_md.write_text("# Agent Prompt\nYou are an assistant.", encoding="utf-8")
            os.environ["AGENT_CONFIG"] = str(config_dir / "agent.yaml")
            try:
                config = prompt_config(temp_dir, {"prompt": {"agent_file": "AGENT.md"}})
                validate_agent_prompt(config)  # Should not raise
            finally:
                del os.environ["AGENT_CONFIG"]


class LoadAgentPromptTest(unittest.TestCase):
    def test_loads_file_as_is(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            content = "# My Agent\n\nYou are helpful."
            agent_md = config_dir / "AGENT.md"
            agent_md.write_text(content, encoding="utf-8")
            os.environ["AGENT_CONFIG"] = str(config_dir / "agent.yaml")
            try:
                config = prompt_config(temp_dir, {"prompt": {"agent_file": "AGENT.md"}})
                result = load_agent_prompt(config)
                self.assertEqual(result, content)
            finally:
                del os.environ["AGENT_CONFIG"]

    def test_respects_max_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            content = "A" * 1000
            agent_md = config_dir / "AGENT.md"
            agent_md.write_text(content, encoding="utf-8")
            os.environ["AGENT_CONFIG"] = str(config_dir / "agent.yaml")
            try:
                config = prompt_config(temp_dir, {"prompt": {"agent_file": "AGENT.md"}})
                result = load_agent_prompt(config, max_bytes=50)
                self.assertEqual(len(result), 50)
            finally:
                del os.environ["AGENT_CONFIG"]


class BuildPromptContextTest(unittest.TestCase):
    def test_model_catalog_policy_is_explicit_and_scoped_to_model_tool(self) -> None:
        for zdr_only, expected in ((True, "ZDR-only models and provider routing"), (False, "all models")):
            config = AppConfig({"agent": {"model_service": {"zdr_only": zdr_only}}})
            context = build_prompt_context(config)
            self.assertIn("Model preset tool catalog policy: %s" % expected, context)
            self.assertIn("only to llm_service", context)

    def test_https_transport_verifies_certificates_and_refuses_redirects(self) -> None:
        import ssl
        import urllib.request

        from assistant_agent.transport_security import open_https_request

        request = urllib.request.Request("https://model.example/v1")
        response = MagicMock()
        with patch("assistant_agent.transport_security.urllib.request.build_opener") as build_opener:
            build_opener.return_value.open.return_value = response
            self.assertIs(open_https_request(request, timeout=3), response)

        handlers = build_opener.call_args.args
        tls_handler = next(handler for handler in handlers if isinstance(handler, urllib.request.HTTPSHandler))
        redirect_handler = next(handler for handler in handlers if handler.__class__.__name__ == "_NoRedirectHandler")
        self.assertTrue(tls_handler._context.check_hostname)
        self.assertEqual(tls_handler._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIsNone(redirect_handler.redirect_request(request, None, 302, "Found", {}, "http://attacker.example"))

    def test_llm_client_rejects_non_https_base_before_reading_credentials(self) -> None:
        for base_url in ("http://model.example/v1", "https:///missing-host"):
            with self.subTest(base_url=base_url):
                config = AppConfig({"agent": {"llm": {"base_url": base_url}}})
                with patch("assistant_agent.llm_client.read_secret") as read_secret:
                    with self.assertRaisesRegex(RuntimeError, "valid HTTPS URL"):
                        LlmClient(config)
                read_secret.assert_not_called()

    def test_llm_payload_contains_fresh_clock_for_research_or_resumed_requests(self) -> None:
        config = AppConfig({"agent": {"app": {"timezone": "Asia/Dubai"}}})
        client = LlmClient.__new__(LlmClient)
        client.config = config
        client.base_url = "https://example.invalid"
        client.api_key = "test-key"
        client.temperature = None
        client.max_tokens = None
        client.timeout_seconds = 1
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        clock = {"timezone": "Asia/Dubai", "utc": "2026-10-08T20:00:00+00:00", "local": "2026-10-09T00:00:00+04:00"}
        with patch("assistant_agent.prompt_context.current_time_context", return_value=clock), patch("assistant_agent.llm_client.open_https_request", return_value=response) as send:
            client._chat_with_model("test-model", [{"role": "system", "content": "Research rules"}, {"role": "user", "content": "Schedule tomorrow"}], [])
        payload = json.loads(send.call_args.args[0].data)
        self.assertIn(clock["local"], payload["messages"][0]["content"])
        self.assertIn(clock["timezone"], payload["messages"][1]["content"])
        self.assertIn(clock["utc"], payload["messages"][1]["content"])

    def test_request_clock_refreshes_without_mutating_checkpoint(self) -> None:
        config = AppConfig({"agent": {"app": {"timezone": "Asia/Dubai"}}})
        messages = [{"role": "system", "content": "Rules\n- Current UTC time: stale"}, {"role": "user", "content": "Book tomorrow"}]
        first_clock = {"timezone": "Asia/Dubai", "utc": "2026-10-08T20:00:00+00:00", "local": "2026-10-09T00:00:00+04:00"}
        next_clock = {"timezone": "Asia/Dubai", "utc": "2026-10-09T20:00:00+00:00", "local": "2026-10-10T00:00:00+04:00"}
        with patch("assistant_agent.prompt_context.current_time_context", return_value=first_clock):
            first = refresh_request_context(messages, config)
        with patch("assistant_agent.prompt_context.current_time_context", return_value=next_clock):
            resumed = refresh_request_context(first, config)
        self.assertEqual(messages[1]["content"], "Book tomorrow")
        self.assertNotIn("stale", resumed[0]["content"])
        self.assertNotIn(first_clock["local"], resumed[1]["content"])
        self.assertIn(next_clock["local"], resumed[1]["content"])
        self.assertEqual(resumed[1]["content"].count("[Current request time]"), 1)

    def test_assistant_timezone_overrides_feature_defaults(self) -> None:
        config = AppConfig({"agent": {
            "app": {"timezone": "Asia/Dubai"},
            "calendar": {"timezone": "America/New_York"},
            "reminders": {"default_timezone": "Europe/London"},
        }})
        self.assertEqual(default_timezone(config)[1], "Asia/Dubai")
        self.assertEqual(parse_datetime("2026-10-08T14:00:00", config), datetime(2026, 10, 8, 10, tzinfo=timezone.utc))
        self.assertIn("Default timezone: Asia/Dubai", build_prompt_context(config))

    def test_explicit_offset_preserves_instant(self) -> None:
        config = AppConfig({"agent": {"app": {"timezone": "Asia/Dubai"}}})
        self.assertEqual(parse_datetime("2026-10-08T14:00:00-04:00", config), datetime(2026, 10, 8, 18, tzinfo=timezone.utc))

    def test_invalid_timezone_fails_instead_of_using_utc(self) -> None:
        config = AppConfig({"agent": {"app": {"timezone": "Invalid/Zone"}}})
        with self.assertRaisesRegex(ValueError, "invalid assistant timezone"):
            default_timezone(config)

    def test_dst_gap_and_fold_require_explicit_offset(self) -> None:
        config = AppConfig({"agent": {"app": {"timezone": "America/New_York"}}})
        for value in ("2026-03-08T02:30:00", "2026-11-01T01:30:00"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "ambiguous or nonexistent"):
                parse_datetime(value, config)

    def test_includes_runtime_context_lines(self) -> None:
        config = AppConfig({
            "agent": {
                "filesystem": {"shared_root": "/tmp/test"},
                "identity": {"name": "TestBot", "email": "bot@test.com"},
                "admin": {"name": "Alice", "email": "alice@test.com"},
                "org": {"name": "TestOrg"},
                "app": {"timezone": "UTC"},
            }
        })
        context = build_prompt_context(config)
        self.assertIn("Agent name: TestBot", context)
        self.assertIn("Agent email: bot@test.com", context)
        self.assertIn("Admin name: Alice", context)
        self.assertIn("Admin email: alice@test.com", context)
        self.assertIn("Organization: TestOrg", context)
        self.assertIn(".assistant/docs/", context)

    def test_omits_admin_name_when_not_configured(self) -> None:
        config = AppConfig({
            "agent": {
                "filesystem": {"shared_root": "/tmp/test"},
                "identity": {"name": "Bot", "email": "bot@test.com"},
                "app": {"timezone": "UTC"},
            }
        })
        context = build_prompt_context(config)
        self.assertNotIn("Admin name:", context)
        self.assertIn("Admin email: not configured", context)


if __name__ == "__main__":
    unittest.main()
