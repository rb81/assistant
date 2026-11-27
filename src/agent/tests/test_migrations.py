import json
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "migrations/001-derived-config-names.sh"


@unittest.skipUnless(SCRIPT.exists(), "migration has been retired")
class DerivedConfigNamesMigrationTest(unittest.TestCase):
    def run_migration(self, data):
        return subprocess.run(["sh", str(SCRIPT), str(data)], capture_output=True, text=True)

    def test_old_installation_is_converted_once(self):
        with tempfile.TemporaryDirectory() as root:
            data = Path(root)
            (data / ".secrets").mkdir()
            (data / ".env").write_text(
                "# bootstrap\nCOMPOSE_PROFILES=email\nAGENT_NAME='Ada Bot'\nIMAP_HOST=mail.example\n"
                "IMAP_PASSWORD='p@ss word'\nTASK_AGENT_WORKERS=2\nAGENT_LLM_MODEL=model\n")
            (data / "overrides.env").write_text("# mine\nAGENT_TIMEZONE=UTC\nIMAP_HOST=wins.example\nCUSTOM_FLAG=1\n")
            (data / ".secrets/POSTGRES_PASSWORD").write_text("db-secret")
            (data / ".secrets/CALENDAR_GOOGLE_2_CLIENT_ID").write_text("second")
            (data / ".secrets/state.json").write_text(json.dumps(
                {"pending": ["POSTGRES_PASSWORD"], "removed": ["CALENDAR_GOOGLE_2_CLIENT_ID"]}))
            (data / ".compose-secrets.yaml").write_text("services: {}\n")

            result = self.run_migration(data)
            self.assertEqual(result.returncode, 0, result.stderr)

            env = (data / ".env").read_text()
            self.assertIn("COMPOSE_PROFILES=email", env)
            self.assertIn("TASK_AGENT_WORKERS=2", env)
            self.assertNotIn("AGENT_LLM", env)
            self.assertNotIn("AGENT_IDENTITY", env)
            self.assertNotIn("p@ss", env)
            overrides = (data / "overrides.env").read_text()
            self.assertIn("AGENT_APP_TIMEZONE=UTC", overrides)
            self.assertIn("AGENT_EMAIL_IMAP_HOST=wins.example", overrides)
            self.assertNotIn("mail.example", overrides)
            self.assertIn("AGENT_IDENTITY_NAME='Ada Bot'", overrides)
            self.assertIn("AGENT_LLM_MODEL=model", overrides)
            self.assertIn("CUSTOM_FLAG=1", overrides)
            self.assertEqual((data / ".secrets/AGENT_DATABASE_PASSWORD").read_text(), "db-secret")
            self.assertEqual((data / ".secrets/AGENT_CALENDAR_GOOGLE_2_CLIENT_ID").read_text(), "second")
            self.assertEqual((data / ".secrets/AGENT_EMAIL_IMAP_PASSWORD").read_text(), "p@ss word")
            self.assertFalse((data / ".secrets/POSTGRES_PASSWORD").exists())
            state = json.loads((data / ".secrets/state.json").read_text())
            self.assertEqual(state, {"pending": ["AGENT_DATABASE_PASSWORD"], "removed": ["AGENT_CALENDAR_GOOGLE_2_CLIENT_ID"]})
            self.assertFalse((data / ".compose-secrets.yaml").exists())
            self.assertTrue(list((data / "backups").glob("config-names-*/.env")))

            before = (data / "overrides.env").read_text()
            self.assertEqual(self.run_migration(data).returncode, 0)
            self.assertEqual((data / "overrides.env").read_text(), before)

    def test_fresh_or_missing_data_is_left_alone(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(self.run_migration(Path(root) / "absent").returncode, 0)
            self.assertEqual(self.run_migration(Path(root)).returncode, 0)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_unrecognized_credential_volume_is_refused_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            data = Path(root)
            (data / ".secrets").mkdir()
            (data / ".env").write_text("AGENT_NAME=x\n")
            (data / ".secrets/state.json").write_text('{"credential_volume": "vol"}')
            self.assertNotEqual(self.run_migration(data).returncode, 0)
            self.assertEqual((data / ".env").read_text(), "AGENT_NAME=x\n")
            self.assertFalse((data / "backups").exists())

    @unittest.skipUnless(os.getenv("ASSISTANT_TEST_DOCKER") == "1", "requires Docker")
    def test_credential_volume_is_renamed_in_place(self):
        volume = "assistant-credentials-" + secrets.token_hex(8)
        docker = lambda *arguments, **options: subprocess.run(["docker", *arguments], check=True, capture_output=True, **options)
        docker("volume", "create", volume)
        self.addCleanup(subprocess.run, ["docker", "volume", "rm", "-f", volume], capture_output=True)
        record = {"values": {"SANDBOX_API_TOKEN": "old", "AGENT_SANDBOX_API_TOKEN": "fresh", "POSTGRES_PASSWORD": "pg"},
                  "grants": {"sandbox-init": ["SANDBOX_API_TOKEN"], "postgres": ["POSTGRES_PASSWORD"]}}
        seed = ("import json,os,sys; os.makedirs('/c/services/sandbox-init'); "
                "open('/c/services/sandbox-init/SANDBOX_API_TOKEN','w').write('old'); open('/c/store.json','w').write(sys.stdin.read())")
        docker("run", "--rm", "-i", "-v", volume + ":/c", "python:3.12-slim", "python", "-c", seed, input=json.dumps(record), text=True)
        with tempfile.TemporaryDirectory() as root:
            data = Path(root)
            (data / ".secrets").mkdir()
            (data / ".env").write_text("AGENT_NAME=x\nOPENROUTER_API_KEY='inline'\n")
            (data / ".secrets/state.json").write_text(json.dumps({"credential_volume": volume, "pending": ["SANDBOX_API_TOKEN"]}))
            result = self.run_migration(data)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(sorted(path.name for path in (data / ".secrets").iterdir()), ["state.json"])
            self.assertEqual(json.loads((data / ".secrets/state.json").read_text())["pending"], ["AGENT_SANDBOX_API_TOKEN"])
            self.assertEqual((data / ".env").read_text(), "")
        listing = ("import json,os; print(json.dumps([json.load(open('/c/store.json')), sorted(os.listdir('/c/services/sandbox-init')), "
                   "open('/c/services/sandbox-init/AGENT_SANDBOX_API_TOKEN').read()]))")
        store, files, token = json.loads(docker("run", "--rm", "-v", volume + ":/c", "python:3.12-slim", "python", "-c", listing,
                                                text=True).stdout)
        self.assertEqual(store["values"], {"AGENT_SANDBOX_API_TOKEN": "old", "AGENT_DATABASE_PASSWORD": "pg",
                                           "AGENT_LLM_OPENROUTER_API_KEY": "inline"})
        self.assertEqual(store["grants"], {"sandbox-init": ["AGENT_SANDBOX_API_TOKEN"], "postgres": ["AGENT_DATABASE_PASSWORD"]})
        self.assertEqual((files, token), (["AGENT_SANDBOX_API_TOKEN"], "old"))


SHIPPED_YAML = Path(__file__).resolve().parents[3] / "migrations/002-shipped-agent-yaml.py"
DEFAULTS = Path(__file__).resolve().parents[3] / "src/config/agent.yaml"


@unittest.skipUnless(SHIPPED_YAML.exists(), "migration has been retired")
class ShippedAgentYamlMigrationTest(unittest.TestCase):
    def installation(self, root, edit, overrides=""):
        import yaml
        data = Path(root)
        (data / "config").mkdir()
        settings = yaml.safe_load(DEFAULTS.read_text())
        edit(settings["agent"])
        (data / "config/agent.yaml").write_text(yaml.safe_dump(settings))
        if overrides:
            (data / "overrides.env").write_text(overrides)
        return data

    def run_migration(self, data):
        env = dict(os.environ, ASSISTANT_DEFAULTS_DIR=str(DEFAULTS.parent))
        return subprocess.run([sys.executable, str(SHIPPED_YAML), str(data)],
                              capture_output=True, text=True, env=env)

    def test_edits_become_overrides_and_stale_defaults_are_dropped(self):
        def edit(agent):
            agent["llm"]["model"] = "openai/gpt-5"
            agent["sandbox"]["network"]["allowed_hosts"] = ["a.com", "b.com"]
            agent["sandbox"]["base_url"] = "http://sandbox:8080"
            agent["limits"]["max_emails_per_hour"] = 7
            agent["email"]["subject_threading_fallback"] = True
        with tempfile.TemporaryDirectory() as root:
            data = self.installation(root, edit, "AGENT_LIMITS_MAX_EMAILS_PER_HOUR='9'\n")
            result = self.run_migration(data)
            self.assertEqual(result.returncode, 0, result.stderr)
            overrides = (data / "overrides.env").read_text()
            self.assertIn("AGENT_LLM_MODEL='openai/gpt-5'", overrides)
            self.assertIn("AGENT_SANDBOX_NETWORK_ALLOWED_HOSTS='a.com,b.com'", overrides)
            self.assertIn("AGENT_LIMITS_MAX_EMAILS_PER_HOUR='9'", overrides)
            self.assertNotIn("SANDBOX_BASE_URL", overrides)
            self.assertNotIn("THREADING", overrides)
            self.assertFalse((data / "config/agent.yaml").exists())
            self.assertTrue((data / "config").is_dir())
            self.assertEqual(len(list((data / "backups").glob("agent-yaml-*/agent.yaml"))), 1)

    def test_credential_in_yaml_aborts_without_changes(self):
        def edit(agent):
            agent["llm"]["model"] = "openai/gpt-5"
            agent["email"]["smtp_password"] = "hunter2"
        with tempfile.TemporaryDirectory() as root:
            data = self.installation(root, edit)
            result = self.run_migration(data)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("AGENT_EMAIL_SMTP_PASSWORD", result.stderr)
            self.assertNotIn("hunter2", result.stdout + result.stderr)
            self.assertTrue((data / "config/agent.yaml").exists())
            self.assertFalse((data / "overrides.env").exists())


PROVIDER_OPT_IN = Path(__file__).resolve().parents[3] / "migrations/003-provider-opt-in.py"


@unittest.skipUnless(PROVIDER_OPT_IN.exists(), "migration has been retired")
class ProviderOptInMigrationTest(unittest.TestCase):
    def run_migration(self, data):
        env = dict(os.environ, ASSISTANT_DEFAULTS_DIR=str(DEFAULTS.parent))
        return subprocess.run([sys.executable, str(PROVIDER_OPT_IN), str(data)],
                              capture_output=True, text=True, env=env)

    def test_selected_routes_and_fallbacks_are_preserved_and_exclusions_remain(self):
        from assistant_agent.setup import parse_environment
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "data"
            data.mkdir()
            (data / ".env").write_text(
                "AGENT_LLM_PROVIDER=openai\nAGENT_LLM_BASE_URL=https://api.openai.com/v1\n"
                "AGENT_MODEL_ROUTES_NAMING_PROVIDER=anthropic\n"
                "AGENT_MODEL_ROUTES_TASK_FALLBACKS=gemini:example\n"
                "AGENT_PROVIDERS_DISABLED=gemini\n")
            result = self.run_migration(data)
            self.assertEqual(result.returncode, 0, result.stderr)
            overrides, _ = parse_environment((data / "overrides.env").read_text())
            self.assertEqual(set(overrides["AGENT_PROVIDERS_ENABLED"].split(",")),
                             {"openai", "anthropic", "openrouter"})
            saved = (data / "overrides.env").read_text()
            self.assertEqual(self.run_migration(data).returncode, 0)
            self.assertEqual((data / "overrides.env").read_text(), saved)

    def test_fresh_install_and_existing_enablement_are_not_rewritten(self):
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "data"
            data.mkdir()
            (data / ".install-initializing").write_text("assistant-initial-install\n")
            (data / ".env").write_text("AGENT_LLM_PROVIDER=openrouter\n")
            self.assertEqual(self.run_migration(data).returncode, 0)
            self.assertFalse((data / "overrides.env").exists())
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "data"
            data.mkdir()
            (data / "overrides.env").write_text("AGENT_PROVIDERS_ENABLED=anthropic\n")
            self.assertEqual(self.run_migration(data).returncode, 0)
            self.assertEqual((data / "overrides.env").read_text(), "AGENT_PROVIDERS_ENABLED=anthropic\n")

    def test_inactive_default_openrouter_routes_do_not_enable_it(self):
        from assistant_agent.setup import parse_environment
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "data"
            data.mkdir()
            (data / ".env").write_text(
                "AGENT_LLM_PROVIDER=openai\nAGENT_LLM_BASE_URL=https://api.openai.com/v1\n"
                "AGENT_SEARCH_ENABLED=false\nAGENT_DEEP_RESEARCH_ENABLED=false\nAGENT_FUSION_ENABLED=false\n")
            result = self.run_migration(data)
            self.assertEqual(result.returncode, 0, result.stderr)
            overrides, _ = parse_environment((data / "overrides.env").read_text())
            self.assertEqual(overrides["AGENT_PROVIDERS_ENABLED"], "openai")


EXPLICIT_ROUTES = Path(__file__).resolve().parents[3] / "migrations/004-explicit-model-routes.py"


@unittest.skipUnless(EXPLICIT_ROUTES.exists(), "migration has been retired")
class ExplicitModelRoutesMigrationTest(unittest.TestCase):
    def run_migration(self, data):
        env = dict(os.environ, ASSISTANT_DEFAULTS_DIR=str(DEFAULTS.parent))
        return subprocess.run([sys.executable, str(EXPLICIT_ROUTES), str(data)],
                              capture_output=True, text=True, env=env)

    def test_inherited_routes_are_snapshotted_without_changing_explicit_routes(self):
        from assistant_agent.setup import parse_environment
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "data"
            data.mkdir()
            (data / ".env").write_text(
                "AGENT_LLM_PROVIDER=auto\nAGENT_LLM_BASE_URL=https://api.openai.com/v1\n"
                "AGENT_LLM_MODEL=gpt-4.1\nAGENT_PROVIDERS_DISABLED=anthropic\n")
            (data / "overrides.env").write_text(
                "AGENT_MODEL_ROUTES_NAMING_PROVIDER=anthropic\n"
                "AGENT_MODEL_ROUTES_TASK_FALLBACKS=gemini:example\n"
                "AGENT_SECURITY_DEVELOPMENT_MODEL=custom-model\n")
            result = self.run_migration(data)
            self.assertEqual(result.returncode, 0, result.stderr)
            contents = (data / "overrides.env").read_text()
            overrides, _ = parse_environment(contents)
            self.assertEqual(overrides["AGENT_MODEL_ROUTES_TASK_PROVIDER"], "openai-compatible")
            self.assertEqual(overrides["AGENT_MODEL_ROUTES_RESEARCH_PROVIDER"], "openai-compatible")
            self.assertEqual(overrides["AGENT_MODEL_ROUTES_NAMING_PROVIDER"], "anthropic")
            self.assertEqual(overrides["AGENT_MODEL_ROUTES_TASK_FALLBACKS"], "gemini:example")
            self.assertEqual(overrides["AGENT_DEEP_RESEARCH_MODEL"], "gpt-4.1")
            self.assertEqual(overrides["AGENT_SECURITY_DEVELOPMENT_MODEL"], "custom-model")
            self.assertEqual(self.run_migration(data).returncode, 0)
            self.assertEqual((data / "overrides.env").read_text(), contents)

    def test_legacy_openai_endpoint_keeps_chat_completions_and_credential_fallback(self):
        from unittest.mock import patch
        from assistant_agent.config import load_config
        from assistant_agent.llm_client import LlmClient
        from assistant_agent.llm_providers import build_request
        from assistant_agent.setup import parse_environment

        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "data"
            data.mkdir()
            env_content = (
                "AGENT_LLM_BASE_URL=https://api.openai.com/v1\n"
                "AGENT_LLM_MODEL=gpt-4.1\n"
                "AGENT_SEARCH_ENABLED=false\nAGENT_DEEP_RESEARCH_ENABLED=false\nAGENT_FUSION_ENABLED=false\n"
            )
            (data / ".env").write_text(env_content)
            provider_result = ProviderOptInMigrationTest().run_migration(data)
            self.assertEqual(provider_result.returncode, 0, provider_result.stderr)
            result = self.run_migration(data)
            self.assertEqual(result.returncode, 0, result.stderr)
            env, _ = parse_environment(env_content)
            overrides, _ = parse_environment((data / "overrides.env").read_text())
            self.assertEqual(overrides["AGENT_PROVIDERS_ENABLED"], "openai-compatible")
            self.assertEqual(overrides["AGENT_MODEL_ROUTES_TASK_PROVIDER"], "openai-compatible")
            self.assertEqual(overrides["AGENT_PROVIDERS_OPENAI_COMPATIBLE_BASE_URL"], "https://api.openai.com/v1")
            with patch.dict(os.environ, {**env, **overrides, "AGENT_CONFIG": str(DEFAULTS)}, clear=True), \
                    patch("assistant_agent.llm_client.read_secret",
                          side_effect=lambda name: "legacy-key" if name == "AGENT_LLM_OPENROUTER_API_KEY" else None):
                client = LlmClient(load_config())
                url, headers, _ = build_request(client.provider, client.base_url, client.api_key,
                                                {"model": client.model, "messages": []})
            self.assertEqual(url, "https://api.openai.com/v1/chat/completions")
            self.assertEqual(headers["Authorization"], "Bearer legacy-key")

    def test_existing_provider_opt_in_migration_enables_legacy_adapter(self):
        from assistant_agent.setup import parse_environment
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "data"
            (data / ".migrations").mkdir(parents=True)
            (data / ".migrations/003-provider-opt-in").write_text("complete\n")
            (data / ".env").write_text("AGENT_LLM_BASE_URL=https://api.openai.com/v1\n")
            (data / "overrides.env").write_text("AGENT_PROVIDERS_ENABLED=openai,openrouter\n")
            result = self.run_migration(data)
            self.assertEqual(result.returncode, 0, result.stderr)
            overrides, _ = parse_environment((data / "overrides.env").read_text())
            self.assertEqual(overrides["AGENT_MODEL_ROUTES_TASK_PROVIDER"], "openai-compatible")
            self.assertEqual(set(overrides["AGENT_PROVIDERS_ENABLED"].split(",")),
                             {"openai", "openrouter", "openai-compatible"})

    def test_explicitly_disabled_legacy_adapter_stays_disabled(self):
        from assistant_agent.setup import parse_environment
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "data"
            data.mkdir()
            (data / ".env").write_text("AGENT_LLM_BASE_URL=https://api.openai.com/v1\n")
            (data / "overrides.env").write_text(
                "AGENT_PROVIDERS_ENABLED=openai\nAGENT_PROVIDERS_DISABLED=openai-compatible\n")
            result = self.run_migration(data)
            self.assertEqual(result.returncode, 0, result.stderr)
            overrides, _ = parse_environment((data / "overrides.env").read_text())
            self.assertEqual(overrides["AGENT_PROVIDERS_ENABLED"], "openai")

    def test_fresh_install_is_not_assigned_legacy_routes(self):
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "data"
            data.mkdir()
            (data / ".install-initializing").write_text("assistant-initial-install\n")
            (data / ".env").write_text("AGENT_LLM_PROVIDER=openrouter\n")
            self.assertEqual(self.run_migration(data).returncode, 0)
            self.assertFalse((data / "overrides.env").exists())

    def test_legacy_fallback_keeps_the_existing_task_route_provider(self):
        from assistant_agent.setup import parse_environment
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "data"
            data.mkdir()
            (data / ".env").write_text(
                "AGENT_LLM_PROVIDER=openrouter\n"
                "AGENT_MODEL_ROUTES_TASK_PROVIDER=openai\n"
                "AGENT_LLM_FALLBACK_MODEL=gpt-4.1-mini\n")
            result = self.run_migration(data)
            self.assertEqual(result.returncode, 0, result.stderr)
            overrides, _ = parse_environment((data / "overrides.env").read_text())
            self.assertEqual(overrides["AGENT_MODEL_ROUTES_TASK_FALLBACKS"], "openai:gpt-4.1-mini")

    def test_legacy_endpoint_is_preserved_in_the_provider_settings(self):
        from assistant_agent.setup import parse_environment
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "data"
            data.mkdir()
            (data / ".env").write_text(
                "AGENT_LLM_PROVIDER=openai\n"
                "AGENT_LLM_BASE_URL=https://api.openai.com/custom/v1\n")
            result = self.run_migration(data)
            self.assertEqual(result.returncode, 0, result.stderr)
            overrides, _ = parse_environment((data / "overrides.env").read_text())
            self.assertEqual(overrides["AGENT_PROVIDERS_OPENAI_BASE_URL"], "https://api.openai.com/custom/v1")

    def test_blank_model_override_preserves_the_old_shipped_default(self):
        from assistant_agent.setup import parse_environment
        import yaml
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "data"
            data.mkdir()
            (data / "overrides.env").write_text("AGENT_NAMING_MODEL=''\n")
            result = self.run_migration(data)
            self.assertEqual(result.returncode, 0, result.stderr)
            overrides, _ = parse_environment((data / "overrides.env").read_text())
            expected = yaml.safe_load(DEFAULTS.read_text())["agent"]["naming"]["model"]
            self.assertEqual(overrides["AGENT_NAMING_MODEL"], expected)

    def test_cloudflare_account_migration_respects_the_explicit_account(self):
        from assistant_agent.setup import parse_environment
        for configured_account in ("", "AGENT_PROVIDERS_CLOUDFLARE_ACCOUNT_ID=actual\n"):
            with self.subTest(configured_account=configured_account), tempfile.TemporaryDirectory() as root:
                data = Path(root) / "data"
                data.mkdir()
                (data / ".env").write_text(
                    "AGENT_LLM_PROVIDER=cloudflare\n"
                    "AGENT_LLM_BASE_URL=https://api.cloudflare.com/client/v4/accounts/legacy/ai/run\n"
                    + configured_account)
                result = self.run_migration(data)
                self.assertEqual(result.returncode, 0, result.stderr)
                overrides, _ = parse_environment((data / "overrides.env").read_text())
                if configured_account:
                    self.assertNotIn("AGENT_PROVIDERS_CLOUDFLARE_ACCOUNT_ID", overrides)
                else:
                    self.assertEqual(overrides["AGENT_PROVIDERS_CLOUDFLARE_ACCOUNT_ID"], "legacy")
                self.assertNotIn("AGENT_PROVIDERS_CLOUDFLARE_BASE_URL", overrides)
