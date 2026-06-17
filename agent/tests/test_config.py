import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.modules.setdefault(
    "yaml",
    types.SimpleNamespace(
        safe_load=lambda handle: {}
    ),
)

from assistant_agent.config import (
    AppConfig,
    agent_display,
    agent_email,
    agent_name,
    app_display_name,
    load_config,
    message_id_domain,
)


class ConfigTest(unittest.TestCase):
    def test_missing_config_file_loads_empty(self) -> None:
        with tempfile.TemporaryDirectory() as config_dir:
            local_config = Path(config_dir) / "agent.yaml"

            env = {"AGENT_CONFIG": str(local_config)}
            with patch.dict(os.environ, env, clear=True):
                config = load_config()

        self.assertIsNone(config.get("agent.app.timezone"))

    def test_config_path_must_be_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as config_dir:
            config_path = Path(config_dir) / "agent.yaml"
            config_path.mkdir()

            env = {"AGENT_CONFIG": str(config_path)}
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(RuntimeError, "must be a file"):
                    load_config()

    def test_org_email_env_overrides_map_to_config(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as config_file:
            config_file.write("agent: {}\n")
            config_file.flush()

            env = {
                "AGENT_CONFIG": config_file.name,
                "ORG_NAME": "Acme Inc.",
                "ORG_SECURITY_EMAIL": "hello@acme.example",
                "ORG_INTERNAL_EMAIL_DOMAINS": "acme.example,example.org",
            }
            with patch.dict(os.environ, env, clear=True):
                config = load_config()

        self.assertEqual(config.get("agent.org.name"), "Acme Inc.")
        self.assertEqual(config.get("agent.org.security_email"), "hello@acme.example")
        self.assertEqual(config.get_list("agent.org.internal_email_domains"), ["acme.example", "example.org"])

    def test_agent_identity_helpers_use_config(self) -> None:
        config = AppConfig(
            {
                "agent": {
                    "app": {"name": "assistant"},
                    "identity": {"name": "Agent", "email": "agent@acme.example"},
                }
            }
        )

        self.assertEqual(app_display_name(config), "Assistant")
        self.assertEqual(agent_name(config), "Agent")
        self.assertEqual(agent_email(config), "agent@acme.example")
        self.assertEqual(agent_display(config), "Agent <agent@acme.example>")
        self.assertEqual(message_id_domain(config), "acme.example")

    def test_agent_identity_env_overrides_map_to_config(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as config_file:
            config_file.write("agent: {}\n")
            config_file.flush()

            env = {
                "AGENT_CONFIG": config_file.name,
                "AGENT_APP_NAME": "assistant",
                "AGENT_APP_BASE_URL": "https://assistant.example.com",
                "AGENT_NAME": "Agent",
                "AGENT_EMAIL": "agent@acme.example",
            }
            with patch.dict(os.environ, env, clear=True):
                config = load_config()

        self.assertEqual(config.get("agent.app.name"), "assistant")
        self.assertEqual(config.get("agent.app.base_url"), "https://assistant.example.com")
        self.assertEqual(config.get("agent.identity.name"), "Agent")
        self.assertEqual(config.get("agent.identity.email"), "agent@acme.example")

    def test_blank_agent_app_base_url_env_does_not_override_config(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as config_file:
            config_file.write("agent:\n  app:\n    base_url: https://configured.example.com\n")
            config_file.flush()

            env = {
                "AGENT_CONFIG": config_file.name,
                "AGENT_APP_BASE_URL": "",
            }
            loaded_values = {"agent": {"app": {"base_url": "https://configured.example.com"}}}
            with (
                patch.dict(os.environ, env, clear=True),
                patch("assistant_agent.config.yaml.safe_load", return_value=loaded_values),
            ):
                config = load_config()

        self.assertEqual(config.get("agent.app.base_url"), "https://configured.example.com")

    def test_api_env_overrides_map_to_config(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as config_file:
            config_file.write("agent: {}\n")
            config_file.flush()

            env = {
                "AGENT_CONFIG": config_file.name,
                "AGENT_API_BIND_HOST": "0.0.0.0",
                "AGENT_API_PORT": "9000",
                "AGENT_API_DOCS_ENABLED": "false",
                "AGENT_API_OPENAPI_ENABLED": "false",
            }
            with patch.dict(os.environ, env, clear=True):
                config = load_config()

        self.assertEqual(config.get("agent.api.bind_host"), "0.0.0.0")
        self.assertEqual(config.get("agent.api.port"), 9000)
        self.assertFalse(config.get_bool("agent.api.docs_enabled", True))
        self.assertFalse(config.get_bool("agent.api.openapi_enabled", True))

    def test_blank_api_bool_env_does_not_override_config(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as config_file:
            config_file.write("agent:\n  api:\n    docs_enabled: true\n    openapi_enabled: true\n")
            config_file.flush()

            env = {
                "AGENT_CONFIG": config_file.name,
                "AGENT_API_DOCS_ENABLED": "",
                "AGENT_API_OPENAPI_ENABLED": "",
            }
            loaded_values = {"agent": {"api": {"docs_enabled": True, "openapi_enabled": True}}}
            with (
                patch.dict(os.environ, env, clear=True),
                patch("assistant_agent.config.yaml.safe_load", return_value=loaded_values),
            ):
                config = load_config()

        self.assertTrue(config.get_bool("agent.api.docs_enabled", False))
        self.assertTrue(config.get_bool("agent.api.openapi_enabled", False))

    def test_blank_string_env_does_not_override_config(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as config_file:
            config_file.write("agent:\n  llm:\n    model: anthropic/claude-sonnet-4.6\n  app:\n    timezone: UTC\n")
            config_file.flush()

            env = {
                "AGENT_CONFIG": config_file.name,
                "AGENT_LLM_MODEL": "",
                "AGENT_TIMEZONE": "",
            }
            loaded_values = {"agent": {"llm": {"model": "anthropic/claude-sonnet-4.6"}, "app": {"timezone": "UTC"}}}
            with (
                patch.dict(os.environ, env, clear=True),
                patch("assistant_agent.config.yaml.safe_load", return_value=loaded_values),
            ):
                config = load_config()

        self.assertEqual(config.get("agent.llm.model"), "anthropic/claude-sonnet-4.6")
        self.assertEqual(config.get("agent.app.timezone"), "UTC")

    def test_calendar_env_overrides(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as config_file:
            config_file.write("agent:\n  calendar:\n    enabled: false\n")
            config_file.flush()

            env = {
                "AGENT_CONFIG": config_file.name,
                "CALENDAR_ENABLED": "true",
                "CALENDAR_TIMEZONE": "America/New_York",
                "CALENDAR_VDIR_PATH": "/tmp/should-not-apply",
                "CALDAV_URL": "https://calendar-provider.example/.well-known/caldav",
                "CALDAV_USERNAME": "user@example.com",
                "CALDAV_PASSWORD": "secret",
            }
            with patch.dict(os.environ, env, clear=True):
                config = load_config()

        # CALENDAR_ENABLED and CALENDAR_TIMEZONE are supported overrides
        self.assertTrue(config.get_bool("agent.calendar.enabled", False))
        self.assertEqual(config.get("agent.calendar.timezone"), "America/New_York")
        # But vdir_path and CALDAV_* are NOT mapped into agent config
        self.assertIsNone(config.get("agent.calendar.store.vdir_path"))
        self.assertIsNone(config.get("agent.calendar.provider.url"))
        self.assertIsNone(config.get("agent.calendar.provider.username"))
        self.assertIsNone(config.get("agent.calendar.provider.password"))


if __name__ == "__main__":
    unittest.main()
