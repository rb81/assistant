import os
import tempfile
import sys
import types
import unittest
from pathlib import Path

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda handle: {}))

from assistant_agent.config import AppConfig
from assistant_agent.prompt_context import (
    SHARED_WORKSPACE_DEFAULTS,
    agent_prompt_path,
    build_prompt_context,
    ensure_prompt_files,
    load_agent_prompt,
    validate_agent_prompt,
)


def prompt_config(shared_root: str, extras: dict = None) -> AppConfig:
    values = {"agent": {"filesystem": {"shared_root": shared_root}}}
    if extras:
        values["agent"].update(extras)
    return AppConfig(values)


class EnsurePromptFilesTest(unittest.TestCase):
    def test_docs_are_seeded_under_assistant_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ensure_prompt_files(prompt_config(temp_dir))
            self.assertTrue((root / ".assistant/docs").is_dir())

    def test_docs_are_refreshed_on_subsequent_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = prompt_config(temp_dir)
            ensure_prompt_files(config)
            doc_path = root / ".assistant/docs/SANDBOX_CAPABILITIES.md"
            if doc_path.exists():
                doc_path.write_text("stale doc", encoding="utf-8")
                ensure_prompt_files(config)
                self.assertNotEqual(doc_path.read_text(encoding="utf-8"), "stale doc")


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
