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
    app_referer_url,
    load_config,
    message_id_domain,
    database_url,
)
from assistant_agent.secret_store import read_secret


class ConfigTest(unittest.TestCase):
    def test_obsolete_environment_overrides_are_ignored(self):
        retired = {
            "AGENT_MEMORY_RECENT_PROMPT_LIMIT": "agent.memory.recent_prompt_limit",
            "AGENT_LLM_PROVIDER": "agent.llm.provider",
            "AGENT_MAX_DAILY_COST_USD": "agent.limits.max_daily_cost_usd",
            "AGENT_LIMITS_TOOL_TIMEOUT_DEFAULT_SECONDS": "agent.limits.tool_timeout_default_seconds",
            "AGENT_SUPERVISOR_REVIEW_MODEL": "agent.supervisor.review_model",
            "AGENT_SUPERVISOR_RECENT_LOG_LIMIT": "agent.supervisor.recent_log_limit",
            "AGENT_MEMORY_STEWARD_MAX_ITERATIONS": "agent.memory.steward.max_iterations",
            "AGENT_SANDBOX_DEFAULT_TIMEOUT_SECONDS": "agent.sandbox.default_timeout_seconds",
            "AGENT_SANDBOX_HARD_KILL_GRACE_SECONDS": "agent.sandbox.hard_kill_grace_seconds",
            "AGENT_API_BIND_HOST": "agent.api.bind_host",
            "AGENT_API_PORT": "agent.api.port",
            "AGENT_API_DOCS_ENABLED": "agent.api.docs_enabled",
            "AGENT_API_AUTH_TOKEN": "agent.api.auth_token",
        }
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"AGENT_CONFIG": root + "/missing", **dict.fromkeys(retired, "99")}, clear=True):
                config = load_config()
                for path in retired.values():
                    with self.subTest(path=path):
                        self.assertIsNone(config.get(path))
            defaults = Path(__file__).resolve().parents[2] / "config/agent.yaml"
            with patch.dict(os.environ, {"AGENT_CONFIG": str(defaults)}, clear=True):
                config = load_config()
                for path in retired.values():
                    with self.subTest(default=path):
                        self.assertIsNone(config.get(path))

    def test_blank_embedding_dimensions_reset_yaml_override_to_model_default(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as config_file:
            config_file.write("agent:\n  embeddings:\n    dimensions: 256\n")
            config_file.flush()
            with patch.dict(os.environ, {"AGENT_CONFIG": config_file.name, "AGENT_EMBEDDINGS_DIMENSIONS": ""}, clear=True):
                self.assertIsNone(load_config().get("agent.embeddings.dimensions"))

    def test_file_secrets_load_without_populating_environment(self):
        with tempfile.TemporaryDirectory() as root:
            secret = Path(root) / "credential"
            secret.write_text("safe$#'\\credential\n", encoding="utf-8")
            with patch.dict(os.environ, {"AGENT_CONFIG": root + "/missing", "SMTP_PASSWORD_FILE": str(secret), "POSTGRES_PASSWORD_FILE": str(secret)}, clear=True):
                config = load_config()
                self.assertEqual(config.get("agent.email.smtp_password"), "safe$#'\\credential")
                self.assertIn("safe%24%23%27%5Ccredential", database_url(config))
                self.assertIn("sslmode=verify-full", database_url(config))
                self.assertIn("sslrootcert=%2Frun%2Fassistant%2Ftls%2Fpostgres.crt", database_url(config))
                self.assertNotIn("SMTP_PASSWORD", os.environ)

    def test_custom_database_url_must_verify_server_certificate(self):
        config = AppConfig({})
        for value in (
            "postgres://user:password@database.example/assistant",
            "postgres://user:password@database.example/assistant?sslmode=require",
            "postgres://user:password@database.example/assistant?sslmode=verify-full",
        ):
            with self.subTest(value=value), patch.dict(os.environ, {"DATABASE_URL": value}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "sslmode=verify-full and provide sslrootcert"):
                    database_url(config)
        secure = "postgres://user:password@database.example/assistant?sslmode=verify-full&sslrootcert=%2Fetc%2Fssl%2Fdb.crt"
        with patch.dict(os.environ, {"DATABASE_URL": secure}, clear=True):
            self.assertEqual(database_url(config), secure)

    def test_file_secrets_fail_closed_and_redact_errors(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "secret"
            path.write_text("", encoding="utf-8")
            for pointer in (str(path), root + "/missing", ""):
                with patch.dict(os.environ, {"OPENROUTER_API_KEY_FILE": pointer}, clear=True):
                    with self.assertRaisesRegex(RuntimeError, "OPENROUTER_API_KEY_FILE"):
                        read_secret("OPENROUTER_API_KEY")
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "PRIVATE-CANARY", "OPENROUTER_API_KEY_FILE": str(path)}, clear=True):
                with self.assertRaises(RuntimeError) as raised:
                    read_secret("OPENROUTER_API_KEY")
                self.assertNotIn("PRIVATE-CANARY", str(raised.exception))

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
        self.assertEqual(app_referer_url(config), "https://github.com/rb81/assistant")
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
                "AGENT_NAME": "Agent",
                "AGENT_EMAIL": "agent@acme.example",
            }
            with patch.dict(os.environ, env, clear=True):
                config = load_config()

        self.assertEqual(config.get("agent.app.name"), "assistant")
        self.assertEqual(config.get("agent.identity.name"), "Agent")
        self.assertEqual(config.get("agent.identity.email"), "agent@acme.example")

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
                "CALENDAR_DEFAULT_PROVIDER": "google",
                "CALENDAR_DEFAULT_ALERT_MINUTES": "15",
                "CALENDAR_GOOGLE_CLIENT_ID": "google-client-id",
                "CALENDAR_GOOGLE_CLIENT_SECRET": "google-client-secret",
                "CALENDAR_GOOGLE_REFRESH_TOKEN": "google-refresh-token",
            }
            with patch.dict(os.environ, env, clear=True):
                config = load_config()

        self.assertTrue(config.get_bool("agent.calendar.enabled", False))
        self.assertEqual(config.get("agent.calendar.timezone"), "America/New_York")
        self.assertEqual(config.get("agent.calendar.default_provider"), "google")
        self.assertEqual(config.get_int("agent.calendar.default_alert_minutes", 30), 15)
        # Provider OAuth credentials are consumed by CalendarRouter from the
        # environment; they are not copied into agent.yaml config paths.
        self.assertIsNone(config.get("agent.calendar.google.client_id"))
        self.assertIsNone(config.get("agent.calendar.providers"))



if __name__ == "__main__":
    unittest.main()
