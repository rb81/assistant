import os
import argparse
import io
import json
import shutil
import pty
import select
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import contextmanager, redirect_stdout
from unittest.mock import MagicMock, patch

import yaml

from assistant_agent.setup import Installation, SetupError, atomic_write, configure_docker_boot, interactive, parse_environment, prepare_internal_tls, prompt_calendar_providers, render_environment, setup_failure_message, validate_draft
from assistant_agent.setup import ADVANCED_SETTINGS, advanced_value_error, prompt_advanced_settings
from assistant_agent.setup import DockerCommandError, start_saved_containers
from assistant_agent.config import load_config


class SetupTest(unittest.TestCase):
    def test_setup_writes_only_runtime_data_and_preserves_local_configuration(self):
        repository = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            shutil.copytree(repository / "config", path / "src/config")
            shutil.copyfile(repository / "docker-compose.yml", path / "src/docker-compose.yml")
            instance = self.saved_configuration(root)
            instance.copy_prompt_example()
            instance.save()
            self.assertEqual(set(item.name for item in path.iterdir()), {"src", "data"})
            self.assertEqual((path / "data/config/agent.yaml").read_bytes(),
                             (path / "src/config/agent.yaml").read_bytes())
            self.assertEqual((path / "data/config/AGENT.md").read_bytes(),
                             (path / "src/config/AGENT.md.example").read_bytes())
            self.assertEqual(instance.values["SANDBOX_DOCS_ROOT_HOST"], str(path / "docs"))
            self.assertEqual(instance.compose_prefix(), [
                "compose", "--project-directory", str(path / "src"),
                "--env-file", str(path / "data/.env"),
                "-f", str(path / "src/docker-compose.yml"),
                "-f", str(path / "data/.compose-secrets.yaml"),
            ])
            custom = b"agent:\n  limits:\n    max_iterations_per_task: 7\n"
            (path / "data/config/agent.yaml").write_bytes(custom)
            reopened = Installation(root)
            self.assertEqual(reopened.defaults.get("agent.limits.max_iterations_per_task"), 7)
            reopened.save()
            self.assertEqual((path / "data/config/agent.yaml").read_bytes(), custom)

    def test_root_launcher_forwards_arguments_from_another_working_directory(self):
        repository = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as elsewhere:
            path = Path(root)
            (path / "src/scripts").mkdir(parents=True)
            shutil.copyfile(repository / "setup.sh", path / "setup.sh")
            launcher = path / "src/scripts/setup.sh"
            launcher.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            launcher.chmod(0o700)
            result = subprocess.run(["sh", str(path / "setup.sh"), "--section", "models", "a b"],
                                    cwd=elsewhere, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.splitlines(), ["--section", "models", "a b"])

    def test_every_workspace_service_mounts_canonical_documentation_read_only(self):
        repository = Path(__file__).resolve().parents[2]
        services = yaml.safe_load((repository / "docker-compose.yml").read_text())["services"]
        for name, service in services.items():
            mounts = service.get("volumes", [])
            if any(isinstance(mount, str) and ":/data/share" in mount for mount in mounts):
                docs = [mount for mount in mounts if isinstance(mount, dict)
                        and mount.get("target") == "/data/share/.assistant/docs"]
                self.assertEqual(len(docs), 1, name)
                self.assertEqual(docs[0]["source"], "../docs", name)
                self.assertTrue(docs[0]["read_only"], name)

    @contextmanager
    def credential_volume_backend(self):
        from assistant_agent.credential_volume import CredentialVolume
        records = {}

        def request(volume, payload):
            previous = json.loads(json.dumps(records.get(volume.name, {})))
            if payload["operation"] == "write":
                if previous != payload["expected"]:
                    raise SetupError("Credential volume changed")
                previous = payload["record"]
                records[volume.name] = json.loads(json.dumps(previous))
            return json.loads(json.dumps(previous))

        with patch.object(CredentialVolume, "request", autospec=True, side_effect=request), patch.object(Installation, "docker", return_value=""):
            yield records

    def test_credential_volume_migration_updates_and_service_scoping(self):
        with tempfile.TemporaryDirectory() as root, self.credential_volume_backend():
            instance = self.saved_configuration(root)
            instance.state["pending"] = []
            instance.save()
            credentials = dict(instance.secret_values)
            instance.use_credential_volume = True
            instance.save()
            self.assertEqual(set(instance.state["pending"]), set(credentials))
            self.assertFalse((Path(root) / "data/.secrets/POSTGRES_PASSWORD").exists())
            self.assertFalse((Path(root) / "data/.secrets/OPENROUTER_API_KEY").exists())
            reopened = Installation(root)
            self.assertEqual(reopened.secret_values, credentials)
            overlay = reopened.overlay()
            self.assertNotIn("secrets", overlay)
            worker = overlay["services"]["task-agent"]
            self.assertEqual(worker["volumes"][0]["volume"]["subpath"], "services/task-agent")
            self.assertTrue(worker["volumes"][0]["read_only"])
            grants = reopened.credential_volume.record["grants"]
            self.assertEqual(grants["postgres"], ["POSTGRES_PASSWORD"])
            self.assertIn("OPENROUTER_API_KEY", grants["task-agent"])
            self.assertNotIn("OPENROUTER_API_KEY", grants["workspace-indexer"])
            reopened.secret_values["OPENROUTER_API_KEY"] = "replacement-key"
            reopened.save()
            with patch.object(reopened, "compose", side_effect=["task-agent\nworkspace-indexer\n", ""]) as compose:
                self.assertEqual(reopened.apply(["OPENROUTER_API_KEY"]), ["task-agent"])
            self.assertIn("--force-recreate", compose.call_args.args[0])
            self.assertEqual(Installation(root).secret_values["OPENROUTER_API_KEY"], "replacement-key")
            self.assertFalse((Path(root) / "data/.secrets/OPENROUTER_API_KEY").exists())

    def test_volume_failure_preserves_host_credentials_and_allows_retry(self):
        from assistant_agent.credential_volume import CredentialVolume
        with tempfile.TemporaryDirectory() as root, self.credential_volume_backend():
            instance = self.saved_configuration(root)
            previous_env = instance.env_path.read_bytes()
            instance.use_credential_volume = True
            with patch.object(CredentialVolume, "verify_mount", side_effect=DockerCommandError("Subpath unsupported")):
                with self.assertRaisesRegex(SetupError, "Subpath unsupported"):
                    instance.save()
            self.assertEqual(instance.env_path.read_bytes(), previous_env)
            self.assertEqual(Installation(root).secret_values, instance.secret_values)
            instance.save()
            self.assertFalse((Path(root) / "data/.secrets/POSTGRES_PASSWORD").exists())

    def test_volume_save_rolls_back_when_configuration_write_fails(self):
        with tempfile.TemporaryDirectory() as root, self.credential_volume_backend():
            instance = self.saved_configuration(root)
            instance.use_credential_volume = True
            instance.save()
            previous = dict(instance.secret_values)
            instance.secret_values["OPENROUTER_API_KEY"] = "not-committed"
            with patch("assistant_agent.setup.write_transaction", side_effect=SetupError("Save failed")):
                with self.assertRaisesRegex(SetupError, "Save failed"):
                    instance.save()
            self.assertEqual(Installation(root).secret_values, previous)

    def test_volume_save_rejects_concurrent_changes(self):
        with tempfile.TemporaryDirectory() as root, self.credential_volume_backend() as records:
            instance = self.saved_configuration(root)
            instance.use_credential_volume = True
            instance.save()
            name = instance.credential_volume.name
            records[name]["values"]["OPENROUTER_API_KEY"] = "concurrent-update"
            with self.assertRaisesRegex(SetupError, "changed during this session"):
                instance.save()
            self.assertEqual(Installation(root).secret_values["OPENROUTER_API_KEY"], "concurrent-update")

    def test_move_credentials_back_to_host_and_then_volume_again(self):
        with tempfile.TemporaryDirectory() as root, self.credential_volume_backend() as records:
            instance = self.saved_configuration(root)
            instance.use_credential_volume = True
            instance.save()
            name = instance.credential_volume.name
            previous = json.loads(json.dumps(records[name]))
            instance.state["pending"] = []
            instance.use_credential_volume = False
            instance.save()
            instance.assert_current()
            self.assertIsNone(instance.credential_volume)
            self.assertNotIn("credential_volume", instance.state)
            self.assertEqual(instance.state["previous_credential_volume"], name)
            self.assertEqual(set(instance.state["pending"]), set(instance.secret_values))
            self.assertEqual(records[name], previous)
            directory = Path(root) / "data/.secrets"
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for name, value in instance.secret_values.items():
                self.assertEqual((directory / name).read_text(), value)
                self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), 0o600)
            self.assertIn("secrets", instance.overlay())
            reopened = Installation(root)
            reopened.secret_values["OPENROUTER_API_KEY"] = "host-updated-key"
            reopened.save()
            reopened.use_credential_volume = True
            reopened.save()
            reopened.assert_current()
            self.assertNotIn("previous_credential_volume", reopened.state)
            self.assertEqual(Installation(root).secret_values["OPENROUTER_API_KEY"], "host-updated-key")
            self.assertFalse((directory / "OPENROUTER_API_KEY").exists())

    def test_move_back_to_host_recreates_services_and_destroys_volume(self):
        with tempfile.TemporaryDirectory() as root, self.credential_volume_backend():
            instance = self.saved_configuration(root)
            instance.use_credential_volume = True
            instance.save()
            name = instance.credential_volume.name
            instance.use_credential_volume = False
            instance.save()
            users = ["c1\trunning\tother\tx\tforeign-app\n"]
            calls = []

            def docker(arguments, input_text=None, timeout=180):
                calls.append(arguments)
                if arguments[:2] == ["volume", "ls"]:
                    return name + "\n"
                if arguments[:1] == ["ps"]:
                    return users.pop(0) if users else ""
                if "config" in arguments:
                    return '{"name": "assistant"}'
                if "ps" in arguments:
                    return "task-agent\n"
                return ""

            with patch.object(Installation, "docker", side_effect=docker):
                with self.assertRaisesRegex(SetupError, "foreign-app"):
                    Installation(root).retire_previous_volume()
                self.assertNotIn(["volume", "rm", name], calls)
                self.assertEqual(Installation(root).state["previous_credential_volume"], name)
                users[:] = ["c2\trunning\tassistant\tworkspace-indexer\tw\nc3\texited\tassistant\tsandbox-init\ts\n",
                            "c3\texited\tassistant\tsandbox-init\ts\n", ""]
                reopened = Installation(root)
                self.assertEqual(reopened.retire_previous_volume(), ["task-agent", "workspace-indexer"])
            self.assertIn(["rm", "c3"], calls)
            self.assertEqual(calls[-1], ["volume", "rm", name])
            self.assertNotIn("previous_credential_volume", Installation(root).state)
            self.assertTrue((Path(root) / "data/.secrets/POSTGRES_PASSWORD").exists())

    def test_failed_move_back_to_host_preserves_volume_and_configuration(self):
        with tempfile.TemporaryDirectory() as root, self.credential_volume_backend() as records:
            instance = self.saved_configuration(root)
            instance.use_credential_volume = True
            instance.save()
            previous = json.loads(json.dumps(records))
            instance.use_credential_volume = False
            with patch("assistant_agent.setup.write_transaction", side_effect=SetupError("Save failed")):
                with self.assertRaisesRegex(SetupError, "Save failed"):
                    instance.save()
            self.assertEqual(records, previous)
            reopened = Installation(root)
            self.assertTrue(reopened.use_credential_volume)
            self.assertEqual(reopened.secret_values, instance.secret_values)

    def test_post_setup_option_restores_plaintext_credentials_on_save_only(self):
        import questionary
        with tempfile.TemporaryDirectory() as root, self.credential_volume_backend():
            instance = self.saved_configuration(root)
            instance.use_credential_volume = True
            instance.save()
            for finish in ("cancel", "save"):
                with self.subTest(finish=finish):
                    responses = ["credential-volume", True, finish] + ([False] if finish == "save" else [])
                    with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary, "select", wraps=questionary.select) as select_prompt, patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                        interactive(argparse.Namespace(root=Path(root), command=None))
                    choices = select_prompt.call_args_list[0].kwargs["choices"]
                    self.assertTrue(any(getattr(choice, "title", "") == "Move credentials back to plaintext host files" for choice in choices))
                    self.assertEqual(Installation(root).use_credential_volume, finish == "cancel")
                    self.assertEqual((Path(root) / "data/.secrets/POSTGRES_PASSWORD").exists(), finish == "save")

    def test_factory_reset_preserves_migrated_credentials_and_setup_state(self):
        from assistant_agent.factory_reset import expand
        from assistant_agent.setup_tools import run_factory_reset
        with tempfile.TemporaryDirectory() as root, self.credential_volume_backend() as records:
            instance = self.saved_configuration(root)
            instance.use_credential_volume = True
            instance.save()
            previous = json.loads(json.dumps(records))
            files = {path: path.read_bytes() for path in
                     (instance.state_path, instance.env_path, Path(root) / "data/.compose-secrets.yaml")}
            with patch.object(instance, "compose", return_value="task-agent\npostgres\n"), patch.object(instance, "compose_interactive", return_value=0) as compose:
                run_factory_reset(instance, MagicMock(), expand(["workstreams"])[0], False, False)
            self.assertFalse(any("down" in call.args[0] or "volume" in call.args[0] for call in compose.call_args_list))
            self.assertEqual(records, previous)
            for path, content in files.items():
                self.assertEqual(path.read_bytes(), content)
            instance.assert_current()
            self.assertEqual(Installation(root).secret_values, instance.secret_values)

    def test_volume_database_rotation_recovery_keeps_journal_out_of_host_files(self):
        from assistant_agent.setup import recover_database
        with tempfile.TemporaryDirectory() as root, self.credential_volume_backend():
            instance = self.saved_configuration(root)
            instance.use_credential_volume = True
            instance.save()
            previous = instance.secret_values["POSTGRES_PASSWORD"]
            with patch.object(instance, "compose", return_value="postgres\ntask-agent\n"), patch.object(instance, "set_database_password"), patch.object(instance, "verify_database_password", side_effect=SetupError("Probe failed")):
                with self.assertRaisesRegex(SetupError, "rotation is incomplete"):
                    instance.rotate_database("replacement-database-password")
            self.assertFalse((Path(root) / "data/.secrets/rotation.json").exists())
            with self.assertRaisesRegex(SetupError, "recovery is pending"):
                Installation(root)
            with patch.object(Installation, "compose"), patch.object(Installation, "set_database_password"), patch.object(Installation, "verify_database_password"):
                recover_database(root)
            self.assertEqual(Installation(root).secret_values["POSTGRES_PASSWORD"], previous)

    def test_volume_option_is_available_in_review_and_post_setup_menu(self):
        import questionary
        for responses in (
            ["credential-volume", True, "cancel"],
            ["review", "credential-volume", True, "cancel"],
            ["credential-volume", False, "cancel"],
        ):
            with self.subTest(responses=responses), tempfile.TemporaryDirectory() as root:
                instance = self.saved_configuration(root)
                previous = instance.env_path.read_bytes()
                with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=responses), patch.object(Installation, "docker") as docker:
                    interactive(argparse.Namespace(root=Path(root), command=None))
                docker.assert_not_called()
                self.assertEqual(instance.env_path.read_bytes(), previous)
                self.assertTrue((Path(root) / "data/.secrets/POSTGRES_PASSWORD").exists())

    def test_post_setup_volume_confirmation_goes_to_review_and_save_migrates(self):
        import questionary
        with tempfile.TemporaryDirectory() as root, self.credential_volume_backend():
            instance = self.saved_configuration(root)
            output = io.StringIO()
            responses = ["credential-volume", True, "save", False]
            with redirect_stdout(output), patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary, "select", wraps=questionary.select) as select_prompt, patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                interactive(argparse.Namespace(root=Path(root), command=None))
            prompts = [call.args[0] for call in select_prompt.call_args_list]
            self.assertEqual(prompts, ["Update configuration", "Finish setup"])
            self.assertIn("Migration is pending", output.getvalue())
            self.assertIn("Plaintext .secrets sources removed", output.getvalue())
            self.assertTrue(Installation(root).credential_volume)
            self.assertFalse((Path(root) / "data/.secrets/POSTGRES_PASSWORD").exists())


    @unittest.skipUnless(os.getenv("ASSISTANT_TEST_DOCKER") == "1" and shutil.which("docker"), "requires opt-in disposable Docker tests")
    def test_live_credential_volume_migration_mounts_and_rotation(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "data").mkdir(exist_ok=True)
            (path / "src").mkdir(exist_ok=True)
            (path / "src/docker-compose.yml").write_text(yaml.safe_dump({
                "name": "assistant-volume-test-" + path.name.lower(),
                "services": {
                    service: {"image": "python:3.12-slim", "command": ["sleep", "300"]}
                    for service in ("task-agent", "workspace-indexer")
                },
            }))
            instance = self.saved_configuration(root)
            instance.use_credential_volume = True
            from assistant_agent.credential_volume import CredentialVolume
            name = CredentialVolume.name_for(instance.root)
            try:
                instance.save()
                instance.compose(["up", "-d"])
                self.assertEqual(Installation(root).secret_values, instance.secret_values)
                result = instance.compose(["exec", "-T", "workspace-indexer", "python", "-c",
                                           "from pathlib import Path; print(','.join(sorted(p.name for p in Path('/run/secrets').iterdir())))"])
                self.assertEqual(result.strip(), "POSTGRES_PASSWORD")
                instance.secret_values["OPENROUTER_API_KEY"] = "replacement-test-key"
                instance.save()
                instance.apply(["OPENROUTER_API_KEY"])
                instance.compose(["exec", "-T", "task-agent", "python", "-c",
                                  "from pathlib import Path; assert Path('/run/secrets/OPENROUTER_API_KEY').read_text() == 'replacement-test-key'"])
                instance.compose(["exec", "-T", "task-agent", "python", "-c",
                                  "import os; from pathlib import Path; p=Path('/run/secrets/OPENROUTER_API_KEY'); "
                                  "assert p.stat().st_mode & 0o777 == 0o600; assert not os.access(p, os.W_OK)"])
                instance.docker([
                    "run", "--rm", "-i", "--log-driver", "none", "--network", "none",
                    "--mount", "type=volume,source=" + name + ",target=/credentials",
                    "python:3.12-slim", "python", "-c",
                    "import sys; from pathlib import Path; root=Path('/credentials'); "
                    "(root/'recovery.json').write_text(sys.stdin.read()); "
                    "(root/'services/task-agent/OPENROUTER_API_KEY').write_text('interrupted-write')",
                ], input_text=json.dumps(instance.credential_volume.record))
                self.assertEqual(Installation(root).secret_values, instance.secret_values)
                instance.compose(["exec", "-T", "task-agent", "python", "-c",
                                  "from pathlib import Path; assert Path('/run/secrets/OPENROUTER_API_KEY').read_text() == 'replacement-test-key'"])
                instance.use_credential_volume = False
                instance.save()
                instance.assert_current()
                self.assertFalse(Installation(root).use_credential_volume)
                instance.compose(["stop", "workspace-indexer"])
                self.assertIn("task-agent", instance.retire_previous_volume())
                self.assertEqual(instance.docker(["volume", "ls", "-q", "--filter", "name=^" + name + "$"]).strip(), "")
                self.assertNotIn("previous_credential_volume", Installation(root).state)
                instance.compose(["exec", "-T", "task-agent", "python", "-c",
                                  "from pathlib import Path; assert Path('/run/secrets/OPENROUTER_API_KEY').read_text() == 'replacement-test-key'"])
                instance.secret_values["OPENROUTER_API_KEY"] = "host-updated-key"
                instance.save()
                instance.use_credential_volume = True
                instance.save()
                instance.apply(list(instance.secret_values))
                instance.compose(["exec", "-T", "task-agent", "python", "-c",
                                  "from pathlib import Path; assert Path('/run/secrets/OPENROUTER_API_KEY').read_text() == 'host-updated-key'"])
                instance.secret_values.pop("OPENROUTER_API_KEY")
                instance.save()
                instance.apply(["OPENROUTER_API_KEY"])
                instance.compose(["exec", "-T", "task-agent", "python", "-c",
                                  "from pathlib import Path; assert not Path('/run/secrets/OPENROUTER_API_KEY').exists()"])
            finally:
                instance.compose(["down", "--volumes"])
                instance.docker(["volume", "rm", name])

    def test_calendar_verification_without_runtime_database_dependencies(self):
        script = """
import importlib.abc
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

class BlockDatabaseImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "psycopg" or fullname.startswith("psycopg."):
            raise ModuleNotFoundError("Setup must not require psycopg")

sys.meta_path.insert(0, BlockDatabaseImports())
from assistant_agent.calendar.diagnostics import verify_provider

assert "assistant_agent.calendar.router" not in sys.modules
config = SimpleNamespace(name="setup-calendar", calendar_id="primary")
provider = MagicMock()
with patch("assistant_agent.calendar.diagnostics.build_provider", return_value=provider) as build:
    result = verify_provider(config)
assert result["status"] == "ok", result
assert build.call_args.args[1]._db is None
provider.list_calendars.assert_called_once_with()
provider.list_events.assert_called_once()
assert provider.list_events.call_args.args[0] == "primary"
assert [call[0] for call in provider.mock_calls] == ["list_calendars", "list_events"]
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def saved_configuration(self, root):
        instance = Installation(root)
        instance.values = {"AGENT_NAME": "Original", "AGENT_TIMEZONE": "UTC", "AGENT_LLM_MODEL": "model",
                           "AGENT_LLM_BASE_URL": "https://openrouter.ai/api/v1", "COMPOSE_PROFILES": "custom",
                           "UNRELATED": "preserve"}
        instance.secret_values = {"POSTGRES_PASSWORD": "database-password", "OPENROUTER_API_KEY": "model-key"}
        instance.save()
        return instance

    def test_existing_configuration_picker_retains_unedited_settings_and_pending_secrets(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            original = self.saved_configuration(root)
            self.assertTrue(original.state["pending"])
            options = argparse.Namespace(root=Path(root), command=None)
            responses = ["identity", "Changed", "UTC", "User", "user@example.com", "", "", "", "review", "save", False]
            with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                interactive(options)
            current = Installation(root)
            self.assertEqual(current.values["AGENT_NAME"], "Changed")
            self.assertEqual(current.values["COMPOSE_PROFILES"], "custom")
            self.assertEqual(current.values["UNRELATED"], "preserve")
            self.assertEqual(current.secret_values, original.secret_values)
            self.assertTrue(current.state["setup_complete"])





    def test_telegram_section_enables_profile_and_hides_bot_token(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            self.saved_configuration(root)
            responses = [True, "Enter replacement", "PRIVATE-TELEGRAM-CANARY", "123", '{"user@example.com":123}', "save", False]
            output = io.StringIO()
            with redirect_stdout(output), patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                interactive(argparse.Namespace(root=Path(root), command=None, section="telegram"))
            current = Installation(root)
            self.assertIn("telegram", current.values["COMPOSE_PROFILES"].split(","))
            self.assertEqual(current.secret_values["TELEGRAM_BOT_TOKEN"], "PRIVATE-TELEGRAM-CANARY")
            self.assertNotIn("PRIVATE-TELEGRAM-CANARY", output.getvalue())
            self.assertNotIn("PRIVATE-TELEGRAM-CANARY", (Path(root) / "data/.env").read_text())

    def test_voice_section_generates_scoped_secret_and_profile(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            self.saved_configuration(root)
            with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=[True, "save", False]):
                interactive(argparse.Namespace(root=Path(root), command=None, section="transcription"))
            current = Installation(root)
            self.assertIn("voice", current.values["COMPOSE_PROFILES"].split(","))
            self.assertGreaterEqual(len(current.secret_values["TRANSCRIPTION_API_TOKEN"]), 32)
            overlay = yaml.safe_load((Path(root) / "data/.compose-secrets.yaml").read_text())
            self.assertIn("TRANSCRIPTION_API_TOKEN_FILE", overlay["services"]["transcription-init"]["environment"])
            self.assertNotIn("TRANSCRIPTION_API_TOKEN_FILE", overlay["services"].get("transcription", {}).get("environment", {}))

    def test_automatic_transcription_secret_generation_and_overlay_filtering(self):
        with tempfile.TemporaryDirectory() as root:
            self.saved_configuration(root)
            instance = Installation(root)
            instance.values["TRANSCRIPTION_ENABLED"] = "true"
            instance.save()
            current = Installation(root)
            self.assertIn("voice", current.values["COMPOSE_PROFILES"].split(","))
            self.assertGreaterEqual(len(current.secret_values["TRANSCRIPTION_API_TOKEN"]), 32)
            self.assertEqual(current.values["TRANSCRIPTION_BASE_URL"], "http://transcription:8080")
            overlay = yaml.safe_load((Path(root) / "data/.compose-secrets.yaml").read_text())
            self.assertIn("TRANSCRIPTION_API_TOKEN", overlay["secrets"])
            self.assertIn("TRANSCRIPTION_API_TOKEN_FILE", overlay["services"]["transcription-init"]["environment"])

            current.values["TRANSCRIPTION_ENABLED"] = "false"
            current.save()
            reloaded = Installation(root)
            self.assertNotIn("voice", reloaded.values.get("COMPOSE_PROFILES", "").split(","))
            disabled_overlay = yaml.safe_load((Path(root) / "data/.compose-secrets.yaml").read_text())
            self.assertNotIn("TRANSCRIPTION_API_TOKEN", disabled_overlay["secrets"])

            from assistant_agent.setup import SetupError, validate_secret
            with self.assertRaises(SetupError):
                validate_secret("TRANSCRIPTION_API_TOKEN", "short-token")

    def test_direct_section_cancel_preserves_saved_configuration(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            original = self.saved_configuration(root)
            options = argparse.Namespace(root=Path(root), command=None, section="identity")
            responses = ["Changed", "UTC", "User", "user@example.com", "", "", "", "cancel"]
            with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                interactive(options)
            self.assertEqual((Path(root) / "data/.env").read_bytes(), original.env_original)
            self.assertNotIn("setup_complete", Installation(root).state)

    def test_advanced_section_saves_privacy_consent_and_limits_without_changing_secrets(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            original = self.saved_configuration(root)
            responses = ["Calendar privacy", True, "Task limits", "25", "50000", "2048", "45", "done", "save", False]
            output = io.StringIO()
            with redirect_stdout(output), patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                interactive(argparse.Namespace(root=Path(root), command=None, section="advanced"))
            current = Installation(root)
            self.assertEqual(current.values["AGENT_CALENDAR_POLICY_ALLOW_READ_EVENT_DETAILS"], "true")
            self.assertEqual(current.values["AGENT_LIMITS_MAX_TOKENS_PER_TASK"], "50000")
            self.assertEqual(current.values["AGENT_LLM_MAX_TOKENS_PER_CALL"], "2048")
            self.assertEqual(current.values["UNRELATED"], "preserve")
            self.assertEqual(current.secret_values, original.secret_values)
            self.assertNotIn("CALENDAR_ENABLED", current.values)
            self.assertNotIn("AGENT_SEARCH_ENABLED", current.values)
            self.assertIn("AGENT_CALENDAR_POLICY_ALLOW_READ", output.getvalue())
            self.assertNotIn("model-key", output.getvalue())
            with patch.dict(os.environ, {**current.values, "AGENT_CONFIG": str(Path(root) / "data/config/agent.yaml")}, clear=True):
                runtime = load_config()
            self.assertTrue(runtime.get_bool("agent.calendar.policy.allow_read_event_details"))
            self.assertEqual(runtime.get_int("agent.limits.max_tokens_per_task", 0), 50000)

    def test_advanced_cancel_and_skip_preserve_saved_files(self):
        import questionary
        for responses in (["Calendar privacy", True, "done", "cancel"], ["done", "save", False]):
            with self.subTest(responses=responses), tempfile.TemporaryDirectory() as root:
                instance = self.saved_configuration(root)
                paths = [Path(root) / "data" / name for name in (".env", ".compose-secrets.yaml", ".secrets/POSTGRES_PASSWORD")]
                originals = {path: path.read_bytes() for path in paths}
                with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                    interactive(argparse.Namespace(root=Path(root), command=None, section="advanced"))
                for path, original in originals.items():
                    if responses[-1] == "cancel" or path.name != ".env":
                        self.assertEqual(path.read_bytes(), original)
                for name, value in instance.values.items():
                    self.assertEqual(Installation(root).values[name], value)
                self.assertNotIn("AGENT_CALENDAR_POLICY_ALLOW_READ_EVENT_DETAILS", Installation(root).values)

    def test_advanced_prompts_use_yaml_defaults_and_explicit_empty_overrides(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            instance = self.saved_configuration(root)
            (Path(root) / "data/config").mkdir(parents=True, exist_ok=True)
            (Path(root) / "src/config").mkdir(parents=True, exist_ok=True)
            (Path(root) / "data/config/agent.yaml").write_text(yaml.safe_dump({"agent": {
                "calendar": {"policy": {"allow_read_event_details": False}},
                "search": {"enabled": False, "max_results": 7, "allowed_domains": ["example.com"], "excluded_domains": ["excluded.test"]},
            }}))
            instance = Installation(root)
            instance.values["AGENT_SEARCH_ALLOWED_DOMAINS"] = ""
            responses = ["Calendar privacy", False, "Web search", False, "7", "", "", "done"]
            with patch.object(questionary, "text", wraps=questionary.text) as text, patch.object(questionary, "confirm", wraps=questionary.confirm) as confirm, patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                prompt_advanced_settings(instance, lambda question: question.unsafe_ask(), MagicMock())
            self.assertFalse(confirm.call_args_list[0].kwargs["default"])
            self.assertFalse(confirm.call_args_list[1].kwargs["default"])
            self.assertEqual([call.kwargs["default"] for call in text.call_args_list], ["7", "", "excluded.test"])
            with patch.dict(os.environ, {**instance.values, "AGENT_CONFIG": str(Path(root) / "data/config/agent.yaml")}, clear=True):
                runtime = load_config()
            self.assertEqual(runtime.get_list("agent.search.allowed_domains"), [])
            self.assertEqual(runtime.get_list("agent.search.excluded_domains"), [])

    def test_every_advanced_setting_matches_yaml_and_reaches_runtime(self):
        repository = Path(__file__).resolve().parents[2]
        config_path = repository / "config/agent.yaml"
        with patch.dict(os.environ, {"AGENT_CONFIG": str(config_path)}, clear=True):
            defaults = load_config()
        for settings in ADVANCED_SETTINGS.values():
            for setting in settings:
                name, path, label, default, kind, minimum = setting
                with self.subTest(setting=name):
                    self.assertEqual(defaults.get(path), default)
                    expected = not default if kind is bool else default + 1 if kind in (int, float) else ["example.com"]
                    override = str(expected).lower() if kind is bool else str(expected) if kind in (int, float) else ",".join(expected)
                    with patch.dict(os.environ, {"AGENT_CONFIG": str(config_path), name: override}, clear=True):
                        self.assertEqual(load_config().get(path), expected)

    def test_advanced_numeric_and_boolean_values_are_validated_before_save(self):
        with tempfile.TemporaryDirectory() as root:
            instance = self.saved_configuration(root)
            for settings in ADVANCED_SETTINGS.values():
                for setting in settings:
                    name, path, label, default, kind, minimum = setting
                    if kind is list:
                        continue
                    valid = str(default).lower()
                    self.assertIsNone(advanced_value_error(setting, valid))
                    invalid_values = (["invalid", ""] if kind is bool else
                                      ["invalid", "", "nan", "inf", str(minimum - 1)] if kind is float else
                                      ["invalid", "", "1.5", "nan", str(minimum - 1)])
                    for invalid in invalid_values:
                        with self.subTest(setting=name, invalid=invalid):
                            instance.values[name] = invalid
                            with self.assertRaisesRegex(SetupError, "Advanced Settings: " + name):
                                validate_draft(instance)
                    instance.values.pop(name)

    def test_advanced_token_limits_do_not_allow_unknown_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            instance = self.saved_configuration(root)
            instance.values["UNRECOGNIZED_TOKEN"] = "private-value"
            instance.save()
            with self.assertRaisesRegex(SetupError, "unsupported credential"):
                Installation(root)

    def test_model_tool_setup_saves_toggle_budget_and_requires_openrouter(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            instance = self.saved_configuration(root)
            responses = ["Model preset tool", True, "2.50", "7", "done"]
            with patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                prompt_advanced_settings(instance, lambda question: question.unsafe_ask(), MagicMock())
            instance.save()
            reopened = Installation(root)
            self.assertEqual(reopened.values["AGENT_MODEL_SERVICE_ENABLED"], "true")
            self.assertEqual(reopened.values["AGENT_MODEL_SERVICE_BUDGET_USD"], "2.50")
            self.assertEqual(reopened.values["AGENT_MODEL_SERVICE_BUDGET_PERIOD_DAYS"], "7")
            reopened.values["AGENT_MODEL_SERVICE_BUDGET_PERIOD_DAYS"] = "366"
            with self.assertRaisesRegex(SetupError, "1 to 365"):
                validate_draft(reopened)
            reopened.values["AGENT_MODEL_SERVICE_BUDGET_PERIOD_DAYS"] = "7"
            reopened.secret_values.pop("OPENROUTER_API_KEY", None)
            with self.assertRaisesRegex(SetupError, "Model preset tool requires OPENROUTER_API_KEY"):
                validate_draft(reopened)

    def test_email_section_preserves_advanced_digest_interval(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            instance = self.saved_configuration(root)
            instance.values["AGENT_HEARTBEAT_ADMIN_DIGEST_INTERVAL_HOURS"] = "24"
            instance.save()
            with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=[False, "save", False]):
                interactive(argparse.Namespace(root=Path(root), command=None, section="email"))
            self.assertEqual(Installation(root).values["AGENT_HEARTBEAT_ADMIN_DIGEST_INTERVAL_HOURS"], "24")

    def test_direct_section_requires_completed_initial_setup(self):
        with tempfile.TemporaryDirectory() as root:
            with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"):
                with self.assertRaisesRegex(SetupError, "Complete initial setup"):
                    interactive(argparse.Namespace(root=Path(root), command=None, section="identity"))

    def test_completed_installation_with_invalid_settings_can_open_picker_for_repair(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            instance = self.saved_configuration(root)
            instance.state["setup_complete"] = True
            instance.values["AGENT_TIMEZONE"] = "invalid-timezone"
            instance.save()
            with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=["review", "cancel"]):
                interactive(argparse.Namespace(root=Path(root), command=None))
            self.assertEqual(Installation(root).values["AGENT_TIMEZONE"], "invalid-timezone")

    def test_existing_picker_can_explicitly_run_the_full_wizard(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            self.saved_configuration(root)
            for wizard, responses in ((False, ["wizard"]), (True, [])):
                with self.subTest(wizard=wizard):
                    responses += ["Assistant", "UTC", "User", "user@example.com", "", "", "", "Cancel"]
                    with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                        interactive(argparse.Namespace(root=Path(root), command=None, wizard=wizard))
            self.assertEqual(Installation(root).values["AGENT_NAME"], "Original")

    def test_model_puller_failure_is_nonzero_and_disabled_or_custom_endpoints_skip(self):
        repository = Path(__file__).resolve().parents[2]
        configuration = yaml.safe_load((repository / "docker-compose.yml").read_text())
        script = configuration["services"]["ollama-model-puller"]["command"][1].replace("$$", "$")
        with tempfile.TemporaryDirectory() as tools:
            ollama = Path(tools) / "ollama"
            ollama.write_text('#!/bin/sh\n[ "$1" = list ] && exit 0\n[ "$1" = pull ] && [ "$2" = selected-model ] && exit 17\nexit 18\n')
            ollama.chmod(0o700)
            environment = dict(os.environ, PATH=tools + os.pathsep + os.environ["PATH"],
                               AGENT_EMBEDDINGS_ENABLED="true", AGENT_EMBEDDINGS_BASE_URL="http://ollama:11434", AGENT_EMBEDDINGS_MODEL="selected-model")
            result = subprocess.run(["sh", "-c", script], env=environment, capture_output=True)
            self.assertEqual(result.returncode, 17)
            for override in ({"AGENT_EMBEDDINGS_ENABLED": "false"}, {"AGENT_EMBEDDINGS_BASE_URL": "https://custom.example"}):
                result = subprocess.run(["sh", "-c", script], env={**environment, **override}, capture_output=True)
                self.assertEqual(result.returncode, 0)
                self.assertIn(b"skipped", result.stdout)

    def test_compose_mounts_private_dropbox_staging_for_task_agent(self):
        repository = Path(__file__).resolve().parents[2]
        configuration = yaml.safe_load((repository / "docker-compose.yml").read_text())
        defaults = yaml.safe_load((repository / "config/agent.yaml").read_text())
        staging = Path(defaults["agent"]["artifacts"]["raw_root"]) / "dropbox"
        shared = Path(defaults["agent"]["filesystem"]["shared_root"])
        mount = "../data/private/artifacts/dropbox:%s" % staging
        task_agent = configuration["services"]["task-agent"]
        self.assertIn(mount, task_agent["volumes"])
        self.assertFalse(staging.is_relative_to(shared))
        self.assertTrue(task_agent["read_only"])
        self.assertIn("no-new-privileges:true", task_agent["security_opt"])
        self.assertIn("clamav_net", task_agent["networks"])
        self.assertNotIn(mount, configuration["services"]["sandbox"]["volumes"])

    def test_compose_segments_service_networks_without_management_api(self):
        repository = Path(__file__).resolve().parents[2]
        configuration = yaml.safe_load((repository / "docker-compose.yml").read_text())
        services = configuration["services"]

        def memberships(name):
            networks = services[name].get("networks", [])
            return set(networks if isinstance(networks, list) else networks.keys())

        self.assertEqual(memberships("postgres"), {"database_net"})
        self.assertEqual(memberships("ollama"), {"model_net", "service_egress_net"})
        self.assertEqual(memberships("clamav"), {"clamav_net", "service_egress_net"})
        self.assertEqual(memberships("sandbox"), {"sandbox_net"})
        self.assertTrue({"database_net", "model_net", "sandbox_net"}.issubset(memberships("task-agent")))
        self.assertTrue({"database_net", "clamav_net"}.issubset(memberships("downloader")))
        self.assertTrue(configuration["networks"]["database_net"]["internal"])
        self.assertTrue(configuration["networks"]["model_net"]["internal"])
        self.assertTrue(configuration["networks"]["clamav_net"]["internal"])
        self.assertEqual(configuration["networks"]["egress_net"]["driver_opts"]["com.docker.network.bridge.enable_icc"], "false")
        self.assertEqual(configuration["networks"]["service_egress_net"]["driver_opts"]["com.docker.network.bridge.enable_icc"], "false")
        self.assertNotIn("service_egress_net", memberships("task-agent"))
        self.assertNotIn("service_egress_net", memberships("downloader"))
        self.assertTrue(services["task-agent"]["read_only"])
        self.assertIn("no-new-privileges:true", services["task-agent"]["security_opt"])
        self.assertIn("NET_RAW", services["task-agent"]["cap_drop"])
        self.assertIn("/tmp:rw,nosuid,nodev,size=512m", services["task-agent"]["tmpfs"])
        self.assertNotIn("agent-api", services)
        self.assertIn("hostnossl all all all reject", (repository / "database/pg_hba.conf").read_text())
        for name in services:
            if services[name].get("environment", {}).get("AGENT_ROLE"):
                volumes = {volume["target"]: volume for volume in services[name]["volumes"] if isinstance(volume, dict)}
                for target in ("/run/assistant/tls/postgres.crt", "/run/assistant/tls/sandbox.crt"):
                    self.assertTrue(volumes[target]["read_only"])
                    self.assertFalse(volumes[target]["bind"]["create_host_path"])
        self.assertEqual(services["sandbox"]["cap_drop"], ["ALL"])
        self.assertNotIn("cap_add", services["sandbox"])
        self.assertEqual(services["sandbox-init"]["network_mode"], "none")
        self.assertFalse(any("docker.sock" in str(volume) for volume in services["sandbox-init"]["volumes"]))
        self.assertEqual(services["sandbox"]["depends_on"]["sandbox-init"]["condition"], "service_completed_successfully")

    def test_setup_rejects_plain_http_zoho_api_base(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.values = {
                "AGENT_LLM_MODEL": "model",
                "AGENT_LLM_BASE_URL": "https://openrouter.ai/api/v1",
                "AGENT_TIMEZONE": "UTC",
                "CALENDAR_ENABLED": "true",
                "CALENDAR_DEFAULT_PROVIDER": "zoho",
                "CALENDAR_ZOHO_API_BASE": "http://calendar.zoho.com/api/v1",
            }
            instance.secret_values = {
                "POSTGRES_PASSWORD": "database-password",
                "OPENROUTER_API_KEY": "model-key",
                "CALENDAR_ZOHO_CLIENT_ID": "client",
                "CALENDAR_ZOHO_CLIENT_SECRET": "client-secret",
                "CALENDAR_ZOHO_REFRESH_TOKEN": "refresh-token",
            }
            with self.assertRaisesRegex(SetupError, "Zoho Calendar API base must use HTTPS"):
                validate_draft(instance)

    def test_setup_rejects_custom_database_url_without_verified_tls(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.values = {
                "AGENT_LLM_MODEL": "model",
                "AGENT_LLM_BASE_URL": "https://openrouter.ai/api/v1",
                "AGENT_TIMEZONE": "UTC",
            }
            instance.secret_values = {
                "POSTGRES_PASSWORD": "database-password",
                "OPENROUTER_API_KEY": "model-key",
                "DATABASE_URL": "postgres://user:password@db.example/assistant?sslmode=require",
            }
            with self.assertRaisesRegex(SetupError, "DATABASE_URL: DATABASE_URL must use sslmode=verify-full"):
                validate_draft(instance)

    def test_database_password_probe_requires_verified_tls(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            with patch.object(instance, "compose") as compose:
                instance.verify_database_password("private-password")
        command = compose.call_args.args[0]
        self.assertIn("PGSSLMODE=verify-full", command[-1])
        self.assertIn("PGSSLROOTCERT=/run/assistant/tls/postgres.crt", command[-1])
        self.assertEqual(compose.call_args.args[1], "private-password\n")

    def test_embedding_section_is_independent_and_uses_canonical_settings(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            original = self.saved_configuration(root)
            options = argparse.Namespace(root=Path(root), command=None, section="embeddings")
            responses = [True, "embeddinggemma", "http://ollama:11434", "768", "30", "save", False, False]
            with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                interactive(options)
            current = Installation(root)
            self.assertEqual(current.secret_values, original.secret_values)
            self.assertEqual(current.values["AGENT_EMBEDDINGS_MODEL"], "embeddinggemma")
            self.assertEqual(current.values["AGENT_EMBEDDINGS_DIMENSIONS"], "768")
            self.assertEqual(current.overlay()["services"]["ollama-model-puller"]["environment"]["AGENT_EMBEDDINGS_MODEL"], "embeddinggemma")
            for name, value in (("AGENT_EMBEDDINGS_DIMENSIONS", "42"), ("AGENT_EMBEDDINGS_TIMEOUT_SECONDS", "0"), ("AGENT_EMBEDDINGS_BASE_URL", "http://user:secret@host"), ("AGENT_EMBEDDINGS_BASE_URL", "http://embedding.example")):
                with self.subTest(setting=name):
                    previous = current.values.get(name)
                    current.values[name] = value
                    with self.assertRaisesRegex(SetupError, "Embeddings and search"):
                        validate_draft(current)
                    current.values[name] = previous

    def test_embedding_activation_requires_separate_provisioning_and_rebuild_consent(self):
        from assistant_agent.setup import activate_embeddings
        instance, console = MagicMock(), MagicMock()
        instance.values = {"AGENT_EMBEDDINGS_BASE_URL": "http://ollama:11434"}
        activate_embeddings(instance, MagicMock(return_value=False), console)
        instance.compose.assert_not_called()
        activate_embeddings(instance, MagicMock(side_effect=[True, False]), console)
        commands = [call.args[0] for call in instance.compose.call_args_list]
        self.assertTrue(any(command[-1] == "probe" for command in commands))
        self.assertFalse(any(command[-1] == "reindex" for command in commands))
        instance.compose.reset_mock()
        activate_embeddings(instance, MagicMock(side_effect=[True, True]), console)
        commands = [call.args[0] for call in instance.compose.call_args_list]
        self.assertTrue(any(command[-1] == "reindex" for command in commands))

    def test_reopening_embeddings_preserves_explicit_model_default_dimensions(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            instance = self.saved_configuration(root)
            instance.values["AGENT_EMBEDDINGS_DIMENSIONS"] = ""
            instance.save()
            (Path(root) / "data/config").mkdir(parents=True, exist_ok=True)
            (Path(root) / "src/config").mkdir(parents=True, exist_ok=True)
            (Path(root) / "data/config/agent.yaml").write_text("agent:\n  embeddings:\n    dimensions: 256\n")
            options = argparse.Namespace(root=Path(root), command=None, section="embeddings")
            responses = [True, "embeddinggemma", "http://ollama:11434", "", "20", "cancel"]
            with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary, "text", wraps=questionary.text) as text, patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                interactive(options)
            prompt = next(call for call in text.call_args_list if call.args[0].startswith("Embedding dimensions"))
            self.assertEqual(prompt.kwargs["default"], "")

    def test_setup_failure_reports_stage_and_location_without_exception_contents(self):
        try:
            raise ValueError("PRIVATE-CREDENTIAL-CANARY")
        except ValueError as error:
            message = setup_failure_message(error, "Models and spending")
        self.assertIn("Models and spending", message)
        self.assertIn("ValueError", message)
        self.assertIn("test_setup.py:", message)
        self.assertNotIn("PRIVATE-CREDENTIAL-CANARY", message)
        message = setup_failure_message(PermissionError("PRIVATE-CREDENTIAL-CANARY"), "saving configuration")
        self.assertIn("ownership and permissions", message)
        self.assertNotIn("PRIVATE-CREDENTIAL-CANARY", message)

    def test_calendar_selection_cannot_silently_skip_without_a_checked_provider(self):
        import questionary
        console = MagicMock()
        answers = iter([[], ["zoho"]])
        with patch.object(questionary, "checkbox", wraps=questionary.checkbox) as checkbox:
            providers = prompt_calendar_providers(lambda question: next(answers), [questionary.Choice("Zoho", value="zoho")], console)
        self.assertEqual(providers, ["zoho"])
        self.assertEqual(checkbox.call_count, 2)
        validator = checkbox.call_args.kwargs["validate"]
        self.assertIn("Press Space", validator([]))
        self.assertIs(validator(["zoho"]), True)
        console.print.assert_called_once()

    def test_calendar_enter_selects_highlighted_provider_only_when_nothing_is_ticked(self):
        import questionary
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from assistant_agent.setup import enter_selects_highlighted
        for keys, expected in (("\r", ["google"]), ("\x1b[B\r", ["outlook"]), ("\x1b[B\x1b[B \r", ["zoho"]), (" \x1b[B \r", ["google", "outlook"])):
            with self.subTest(keys=keys), create_pipe_input() as pipe:
                pipe.send_text(keys)
                choices = [questionary.Choice(name.title(), value=name) for name in ("google", "outlook", "zoho")]
                question = enter_selects_highlighted(questionary.checkbox("Providers", choices=choices, input=pipe, output=DummyOutput()))
                self.assertEqual(question.unsafe_ask(), expected)

    def test_overrides_template_lists_only_unmanaged_settings_and_is_never_rewritten(self):
        from assistant_agent.config import ENV_OVERRIDES
        from assistant_agent.secret_store import is_secret_name
        from assistant_agent.setup import compose_environment_names, manual_override_entries, wizard_managed
        repository = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "data").mkdir(exist_ok=True)
            (path / "src").mkdir(exist_ok=True)
            (path / "data/config").mkdir(parents=True, exist_ok=True)
            (path / "src/config").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repository / "config/agent.yaml", path / "data/config/agent.yaml")
            shutil.copyfile(repository / "docker-compose.yml", path / "src/docker-compose.yml")
            self.saved_configuration(root)
            template = (path / "data/overrides.env").read_text()
            listed = {line[2:].split("=")[0] for line in template.splitlines() if line.startswith("# ") and line.endswith("=")}
            blocked = compose_environment_names(path)
            self.assertIn("AGENT_LLM_TEMPERATURE", listed)
            self.assertIn("AGENT_ARTIFACTS_CLAMAV_REQUIRED", listed)
            self.assertIn("# agent.llm.temperature (float; default: 0.2)", template)
            self.assertEqual(listed, {name for name, _, _ in manual_override_entries(path)})
            for name in listed:
                self.assertFalse(wizard_managed(name) or is_secret_name(name) or name in blocked, name)
            for name in ("AGENT_NAME", "AGENT_LLM_MODEL", "IMAP_HOST", "AGENT_SEARCH_ENABLED", "AGENT_CALENDAR_DEFAULT_ALERT_MINUTES", "OPENROUTER_API_KEY"):
                self.assertNotIn(name, listed)
            covered = listed | {name for name, _, _ in ENV_OVERRIDES if wizard_managed(name) or is_secret_name(name) or name in blocked}
            self.assertLessEqual({name for name, _, _ in ENV_OVERRIDES} - covered, {"AGENT_CALENDAR_DEFAULT_ALERT_MINUTES"})
            (path / "data/overrides.env").write_text("AGENT_LLM_TEMPERATURE=0.5\n")
            instance = Installation(root)
            instance.values["AGENT_NAME"] = "Renamed"
            instance.save()
            self.assertEqual((path / "data/overrides.env").read_text(), "AGENT_LLM_TEMPERATURE=0.5\n")

    def test_security_section_validation_and_yolo_confirmation(self):
        from unittest.mock import MagicMock
        from assistant_agent.setup import draft_problems, prompt_security, secret_services
        with tempfile.TemporaryDirectory() as root:
            instance = self.saved_configuration(root)
            self.assertEqual(draft_problems(instance), [])
            instance.values.update({"AGENT_SECURITY_EMAIL_AUTH_ENABLED": "true", "AGENT_SECURITY_DECISION_ENABLED": "true",
                                    "AGENT_SECURITY_DECISION_PROVIDER": "custom", "AGENT_SECURITY_DECISION_URL": "http://decide.example"})
            messages = [message for section, message in draft_problems(instance) if section == "security"]
            self.assertEqual(len(messages), 3)
            self.assertIn("SECURITY_DECISION_API_KEY", messages[-1])
            self.assertIn("downloader", secret_services("SECURITY_DECISION_API_KEY"))
            for typed, expected in (("yes", "false"), ("ALLOW POSSIBLY MALICIOUS", "true")):
                with self.subTest(typed=typed):
                    instance.values = {"AGENT_SECURITY_DECISION_PROVIDER": "perplexity"}
                    instance.secret_values["SECURITY_DECISION_API_KEY"] = "stale-direct-key"
                    answers = iter([False, "openrouter", "typesafe/jev-1.13", "review", False, False, False, True, typed])
                    prompt_security(instance, lambda _question: next(answers), lambda *_args: next(answers), MagicMock())
                    self.assertEqual(instance.values["AGENT_SECURITY_ALLOW_POSSIBLY_MALICIOUS"], expected)
                    self.assertEqual(instance.values["AGENT_SECURITY_DECISION_ENABLED"], "true")
                    self.assertEqual(instance.values["AGENT_SECURITY_DECISION_PROVIDER"], "openrouter")
                    self.assertEqual(instance.values["AGENT_SECURITY_DECISION_MODEL"], "typesafe/jev-1.13")
                    self.assertNotIn("SECURITY_DECISION_API_KEY", instance.secret_values)
                    self.assertEqual(instance.secret_values["OPENROUTER_API_KEY"], "model-key")
                    self.assertEqual([m for section, m in draft_problems(instance) if section == "security"], [])
            answers = iter([False, "direct", "clef", "", "", "Enter replacement", "decision-key", "review", False, False, False, False])
            prompt_security(instance, lambda _question: next(answers), lambda *_args: next(answers), MagicMock())
            self.assertEqual(instance.values["AGENT_SECURITY_DECISION_PROVIDER"], "clef")
            self.assertEqual((instance.values["AGENT_SECURITY_DECISION_URL"], instance.values["AGENT_SECURITY_DECISION_MODEL"]), ("", ""))
            self.assertEqual(instance.secret_values["SECURITY_DECISION_API_KEY"], "decision-key")

    def test_openrouter_key_reaches_downloader_only_while_screening_uses_openrouter(self):
        with tempfile.TemporaryDirectory() as root, self.credential_volume_backend():
            instance = self.saved_configuration(root)
            instance.use_credential_volume = True
            instance.save()
            instance.state["pending"] = []
            grants = instance.credential_volume.record["grants"]
            self.assertNotIn("OPENROUTER_API_KEY", grants.get("downloader", []))
            instance.values.update({"AGENT_SECURITY_DECISION_ENABLED": "true", "AGENT_SECURITY_DECISION_PROVIDER": "openrouter"})
            instance.save()
            self.assertIn("OPENROUTER_API_KEY", instance.credential_volume.record["grants"]["downloader"])
            self.assertIn("OPENROUTER_API_KEY", instance.state["pending"])
            reopened = Installation(root)
            self.assertEqual(reopened.overlay()["services"]["downloader"]["environment"]["OPENROUTER_API_KEY_FILE"], "/run/secrets/OPENROUTER_API_KEY")
            reopened.values["AGENT_SECURITY_DECISION_PROVIDER"] = "perplexity"
            reopened.save()
            self.assertNotIn("OPENROUTER_API_KEY", reopened.credential_volume.record["grants"].get("downloader", []))
            reopened.use_credential_volume = False
            reopened.values["AGENT_SECURITY_DECISION_PROVIDER"] = "openrouter"
            reopened.save()
            self.assertIn("OPENROUTER_API_KEY", reopened.overlay()["services"]["downloader"]["secrets"])
            self.assertEqual((Path(root) / "data/.secrets/OPENROUTER_API_KEY").read_text(), "model-key")

    def test_overrides_problems_block_save_until_fixed(self):
        import questionary
        from assistant_agent.setup import draft_problems
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "data").mkdir(exist_ok=True)
            (path / "src").mkdir(exist_ok=True)
            instance = self.saved_configuration(root)
            (path / "data/overrides.env").write_text("AGENT_LLM_MODEL=x\nIMAP_PASSWORD=secret\nAGENT_LLM_TEMPRATURE=1\nAGENT_LLM_TEMPERATURE=warm\nAGENT_ARTIFACTS_ENABLED=maybe\nAGENT_LIMITS_MAX_PROMPT_CHARS=\n")
            messages = [message for section, message in draft_problems(instance) if section == "advanced"]
            self.assertEqual(len(messages), 5)
            self.assertIn("AGENT_LLM_MODEL is managed by setup", messages[0])
            self.assertIn("remove credential IMAP_PASSWORD", messages[1])
            self.assertNotIn("secret", " ".join(messages).replace("secrets update", ""))
            self.assertIn("unknown setting AGENT_LLM_TEMPRATURE", messages[2])
            self.assertIn("AGENT_LLM_TEMPERATURE must be a number", messages[3])
            self.assertIn("AGENT_ARTIFACTS_ENABLED must be a boolean", messages[4])
            (path / "data/overrides.env").write_text("AGENT_LLM_TEMPERATURE=0.4\n")
            self.assertEqual(draft_problems(instance), [])
            (path / "data/overrides.env").write_text("AGENT_LLM_MODEL=x\n")
            with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary, "select", wraps=questionary.select) as selection, patch.object(questionary.Question, "unsafe_ask", side_effect=["review", "cancel"]):
                interactive(argparse.Namespace(root=path, command=None))
            review = next(call for call in selection.call_args_list if call.args[0] == "Finish setup")
            self.assertTrue(review.kwargs["choices"][0].disabled)

    def test_back_shows_summary_without_repeating_prompts(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            responses = ["Assistant", "UTC", "User", "user@example.com", "", "", "", "Next",
                         "OpenRouter", "https://openrouter.ai/api/v1", "Enter replacement", "model-key", "model", "", "Generate securely", "Back",
                         "Next", "Cancel"]
            output = io.StringIO()
            with redirect_stdout(output), patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary, "text", wraps=questionary.text) as text, patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                interactive(argparse.Namespace(root=Path(root), command=None))
            self.assertEqual(sum(call.args[0] == "Assistant name" for call in text.call_args_list), 1)
            self.assertEqual(sum(call.args[0] == "Primary model identifier" for call in text.call_args_list), 1)
            self.assertIn("Identity and timezone summary", output.getvalue())
            self.assertIn("Already reviewed this session", output.getvalue())
            self.assertNotIn("model-key", output.getvalue())
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_interrupted_section_discards_only_that_section(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            self.saved_configuration(root)
            responses = ["identity", "Changed", "UTC", "User", "user@example.com", "", "", "",
                         "models", "OpenRouter", "https://openrouter.ai/api/v1", "Enter replacement", KeyboardInterrupt(),
                         "review", "save", False]
            output = io.StringIO()
            with redirect_stdout(output), patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                interactive(argparse.Namespace(root=Path(root), command=None))
            current = Installation(root)
            self.assertEqual(current.values["AGENT_NAME"], "Changed")
            self.assertEqual(current.secret_values["OPENROUTER_API_KEY"], "model-key")
            self.assertIn("Section changes discarded", output.getvalue())

    def test_menu_interrupt_confirms_before_discarding_unsaved_changes(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            responses = ["Assistant", "UTC", "User", "user@example.com", "", "", "", KeyboardInterrupt(), False, KeyboardInterrupt(), True]
            with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=responses) as ask:
                with self.assertRaises(KeyboardInterrupt):
                    interactive(argparse.Namespace(root=Path(root), command=None))
            self.assertEqual(ask.call_count, len(responses))
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_jump_and_review_report_problems_by_section(self):
        import questionary
        from assistant_agent.setup import draft_problems
        with tempfile.TemporaryDirectory() as root:
            instance = self.saved_configuration(root)
            instance.values.update({"AGENT_TIMEZONE": "invalid-timezone"})
            sections = [section for section, _ in draft_problems(instance)]
            self.assertEqual(sections, ["identity"])
            instance.save()
            responses = ["Assistant", "UTC", "User", "user@example.com", "", "", "", "Jump to section", "advanced", "done", "Review and save", "cancel"]
            with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary, "select", wraps=questionary.select) as selection, patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                interactive(argparse.Namespace(root=Path(root), command=None, wizard=True))
            jump = next(call for call in selection.call_args_list if call.args[0] == "Jump to section")
            labels = {choice.value: choice.title for choice in jump.kwargs["choices"]}
            self.assertEqual(labels["identity"], "Identity and timezone  [changed]")
            self.assertNotIn("api", labels)
            self.assertEqual(labels["models"], "Models and spending")

    def test_prompt_copy_requires_explicit_choice_and_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "data").mkdir(exist_ok=True)
            (path / "src").mkdir(exist_ok=True)
            (path / "data/config").mkdir(parents=True, exist_ok=True)
            (path / "src/config").mkdir(parents=True, exist_ok=True)
            (path / "src/config/AGENT.md.example").write_text("Example instructions\n")
            instance = Installation(root)
            instance.save()
            self.assertFalse((path / "data/config/AGENT.md").exists())
            instance.copy_prompt_example()
            self.assertFalse((path / "data/config/AGENT.md").exists())
            instance.save()
            self.assertEqual((path / "data/config/AGENT.md").read_text(), "Example instructions\n")
            (path / "data/config/AGENT.md").write_text("Custom instructions\n")
            Installation(root).save()
            self.assertEqual((path / "data/config/AGENT.md").read_text(), "Custom instructions\n")

    def test_prompt_copy_rejects_concurrent_edits(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "data").mkdir(exist_ok=True)
            (path / "src").mkdir(exist_ok=True)
            (path / "data/config").mkdir(parents=True, exist_ok=True)
            (path / "src/config").mkdir(parents=True, exist_ok=True)
            (path / "src/config/AGENT.md.example").write_text("Example instructions\n")
            instance = Installation(root)
            instance.copy_prompt_example()
            (path / "data/config/AGENT.md").write_text("Newer instructions\n")
            with self.assertRaisesRegex(SetupError, "instructions changed"):
                instance.save()
            self.assertEqual((path / "data/config/AGENT.md").read_text(), "Newer instructions\n")

    def test_direct_docker_access_skips_sudo_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            console, confirm = MagicMock(), MagicMock()
            result = subprocess.CompletedProcess([], 0, "28.0.0\n", "")
            with patch("assistant_agent.setup.subprocess.run", return_value=result) as run, patch.object(instance, "start_containers", return_value=[]) as start:
                self.assertEqual(start_saved_containers(instance, confirm, console), [])
            self.assertFalse(instance.use_sudo)
            confirm.assert_not_called()
            start.assert_called_once_with()
            self.assertEqual(run.call_args.args[0], ["docker", "info", "--format", "{{.ServerVersion}}"])
            self.assertEqual(run.call_args.kwargs["timeout"], 15)

    def test_sudo_authorization_failure_precedes_container_changes(self):
        with tempfile.TemporaryDirectory() as root:
            instance = self.saved_configuration(root)
            console, confirm = MagicMock(), MagicMock(return_value=True)
            results = [subprocess.CompletedProcess([], 1, "", "permission denied"),
                       subprocess.CompletedProcess([], 1, "secret-canary", "sudo: a password is required\nsecret-canary")]
            with patch("assistant_agent.setup.platform.system", return_value="Linux"), patch("assistant_agent.setup.os.geteuid", return_value=1000), patch("assistant_agent.setup.subprocess.run", side_effect=results) as run, patch.object(instance, "start_containers") as start:
                with self.assertRaisesRegex(SetupError, "resolve the reported failure"):
                    start_saved_containers(instance, confirm, console)
            start.assert_not_called()
            confirm.assert_called_once()
            self.assertEqual(run.call_args.args[0][:3], ["sudo", "-n", "docker"])
            output = "\n".join(call.args[0] for call in console.print.call_args_list)
            self.assertIn("Noninteractive sudo authorization failed", output)
            self.assertIn("checking Docker access", output)
            self.assertIn("sudo docker compose up", output)
            self.assertNotIn("secret-canary", output)
            self.assertNotIn("No services were reported healthy", output)
            self.assertEqual((Path(root) / "data/.env").read_bytes(), instance.env_original)
            self.assertEqual((Path(root) / "data/.secrets/state.json").read_bytes(), instance.state_original)

    def test_sudo_startup_requires_consent_and_a_successful_probe(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            console, confirm = MagicMock(), MagicMock(return_value=True)
            results = [subprocess.CompletedProcess([], 1, "", "permission denied"),
                       subprocess.CompletedProcess([], 0, "28.0.0\n", "")]
            with patch("assistant_agent.setup.platform.system", return_value="Linux"), patch("assistant_agent.setup.os.geteuid", return_value=1000), patch("assistant_agent.setup.subprocess.run", side_effect=results) as run, patch.object(instance, "start_containers", return_value=[]) as start:
                self.assertEqual(start_saved_containers(instance, confirm, console), [])
            confirm.assert_called_once()
            self.assertFalse(confirm.call_args.args[1])
            self.assertTrue(instance.use_sudo)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.args[0], ["sudo", "-n", "docker", "info", "--format", "{{.ServerVersion}}"])
            start.assert_called_once_with()

    def test_failed_direct_probe_never_escalates_without_consent(self):
        for system, uid, consent_prompt in (("Linux", 1000, True), ("Linux", 0, False), ("Darwin", 1000, False)):
            with self.subTest(system=system, uid=uid), tempfile.TemporaryDirectory() as root:
                instance = Installation(root)
                console, confirm = MagicMock(), MagicMock(return_value=False)
                result = subprocess.CompletedProcess([], 1, "secret-canary", "secret-canary")
                with patch("assistant_agent.setup.platform.system", return_value=system), patch("assistant_agent.setup.os.geteuid", return_value=uid), patch("assistant_agent.setup.subprocess.run", return_value=result) as run, patch.object(instance, "start_containers") as start:
                    with self.assertRaises(SetupError):
                        start_saved_containers(instance, confirm, console)
                self.assertEqual(confirm.call_count, int(consent_prompt))
                run.assert_called_once()
                start.assert_not_called()
                output = "\n".join(call.args[0] for call in console.print.call_args_list)
                self.assertIn("run: docker compose up", output)
                self.assertNotIn("sudo docker", output)
                self.assertNotIn("secret-canary", output)

    def test_docker_errors_suppress_output_and_exception_details(self):
        cases = [
            (False, subprocess.TimeoutExpired(["secret-canary"], 15, output="secret-canary", stderr="secret-canary"), "timed out"),
            (False, OSError("secret-canary"), "could not be launched"),
            (False, subprocess.CompletedProcess([], 1, "secret-canary", "secret-canary"), "Docker command failed"),
            (True, subprocess.CompletedProcess([], 1, "secret-canary", "sudo: a password is required\nsecret-canary"), "sudo authorization failed"),
            (True, subprocess.CompletedProcess([], 1, "secret-canary", "user is not allowed to execute secret-canary"), "sudo authorization failed"),
            (True, subprocess.CompletedProcess([], 1, "secret-canary", "daemon unavailable secret-canary"), "Docker command failed"),
        ]
        for elevated, result, expected in cases:
            with self.subTest(elevated=elevated, expected=expected), tempfile.TemporaryDirectory() as root:
                instance = Installation(root)
                instance.use_sudo = elevated
                kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with patch("assistant_agent.setup.subprocess.run", **kwargs), self.assertRaises(DockerCommandError) as raised:
                    instance.docker(["info"], timeout=15)
                self.assertIn(expected, str(raised.exception))
                self.assertNotIn("secret-canary", str(raised.exception))

    def test_access_probe_preserves_docker_context_and_filters_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            environment = {"DOCKER_HOST": "ssh://server", "DOCKER_CONTEXT": "remote", "OPENROUTER_API_KEY": "secret-canary",
                           "POSTGRES_PASSWORD_FILE": "/secret-canary", "COMPOSE_FILE": "/secret-canary"}
            with patch.dict(os.environ, environment, clear=True), patch("assistant_agent.setup.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "28.0.0", "")) as run, patch.object(instance, "start_containers", return_value=[]):
                start_saved_containers(instance, MagicMock(), MagicMock())
            self.assertEqual(run.call_args.kwargs["env"], {"DOCKER_HOST": "ssh://server", "DOCKER_CONTEXT": "remote"})

    def test_startup_reporting_preserves_health_and_helper_failures(self):
        cases = [
            ([{"Service": "task-agent", "State": "running", "Health": "unhealthy", "ID": "worker"}], "not running or healthy"),
            ([{"Service": "task-agent", "State": "running", "ID": "worker"},
              {"Service": "ollama-model-puller", "State": "exited", "ExitCode": 1}], "startup helper failed"),
        ]
        for rows, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as root:
                instance = self.saved_configuration(root)
                console = MagicMock()
                with patch.object(instance, "docker", return_value="28.0.0"), patch.object(instance, "compose", side_effect=[json.dumps({"services": {"task-agent": {}, "ollama-model-puller": {}}}), "", "", json.dumps(rows)]):
                    with self.assertRaises(SetupError):
                        start_saved_containers(instance, MagicMock(), console)
                output = "\n".join(call.args[0] for call in console.print.call_args_list)
                self.assertIn(expected, output)
                self.assertIn("checking container status", output)
                self.assertIn("run: docker compose up", output)
                self.assertEqual((Path(root) / "data/.secrets/state.json").read_bytes(), instance.state_original)

    def test_wait_failure_reports_stage_without_exposing_command_output(self):
        with tempfile.TemporaryDirectory() as root:
            instance = self.saved_configuration(root)
            console = MagicMock()
            results = [subprocess.CompletedProcess([], 0, "28.0.0", ""),
                       subprocess.CompletedProcess([], 0, json.dumps({"services": {"task-agent": {}}}), ""),
                       subprocess.CompletedProcess([], 0, "", ""),
                       subprocess.TimeoutExpired(["secret-canary"], 240, output="secret-canary")]
            with patch("assistant_agent.setup.subprocess.run", side_effect=results):
                with self.assertRaises(SetupError):
                    start_saved_containers(instance, MagicMock(), console)
            output = "\n".join(call.args[0] for call in console.print.call_args_list)
            self.assertIn("waiting for container health", output)
            self.assertIn("timed out", output)
            self.assertNotIn("secret-canary", output)
            self.assertEqual((Path(root) / "data/.secrets/state.json").read_bytes(), instance.state_original)

    def test_wizard_startup_uses_direct_docker_without_sudo_question(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            self.saved_configuration(root)
            responses = ["done", "save", True]
            with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=responses) as ask, patch("assistant_agent.setup.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "28.0.0", "")) as run, patch.object(Installation, "start_containers", return_value=[]), patch("assistant_agent.setup.configure_docker_boot") as boot:
                interactive(argparse.Namespace(root=Path(root), command=None, section="advanced"))
            self.assertEqual(ask.call_count, 3)
            docker_calls = [call for call in run.call_args_list if call.args[0][0] == "docker"]
            self.assertEqual(len(docker_calls), 1)
            boot.assert_called_once()
            self.assertFalse(boot.call_args.args[0].use_sudo)

    def test_startup_checks_health_and_persists_restart_policy(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.save()
            rows = [{"Service": "task-agent", "State": "running", "Health": "", "ID": "worker"},
                    {"Service": "postgres", "State": "running", "Health": "healthy", "ID": "database"},
                    {"Service": "ollama-model-puller", "State": "exited", "ExitCode": 0, "ID": "one-shot"}]
            with patch.object(instance, "compose", side_effect=[json.dumps({"services": {"task-agent": {}, "postgres": {}, "ollama-model-puller": {}}}), "", "", "\n".join(json.dumps(row) for row in rows)]) as compose, patch.object(instance, "docker", side_effect=["", '"unless-stopped"\n"unless-stopped"\n']) as docker:
                self.assertEqual(instance.start_containers(), rows)
                self.assertEqual(compose.call_args_list[1].kwargs["timeout"], 1800)
                self.assertIn("--wait", compose.call_args_list[2].args[0])
                self.assertEqual(docker.call_args_list[0].args[0], ["update", "--restart", "unless-stopped", "worker", "database"])
                self.assertNotIn("one-shot", docker.call_args_list[0].args[0])

    def test_unhealthy_startup_is_not_reported_successful(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.save()
            rows = [{"Service": "task-agent", "State": "running", "Health": "unhealthy", "ID": "worker"}]
            with patch.object(instance, "compose", side_effect=[json.dumps({"services": {"task-agent": {}}}), "", "", json.dumps(rows)]), patch.object(instance, "docker") as docker:
                with self.assertRaisesRegex(SetupError, "not running or healthy"):
                    instance.start_containers()
                docker.assert_not_called()

    def test_failed_one_shot_helper_is_not_reported_successful(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.save()
            rows = [{"Service": "task-agent", "State": "running", "ID": "worker"},
                    {"Service": "ollama-model-puller", "State": "exited", "ExitCode": 1, "ID": "helper"}]
            with patch.object(instance, "compose", side_effect=[json.dumps({"services": {"task-agent": {}, "ollama-model-puller": {}}}), "", "", json.dumps(rows)]), patch.object(instance, "docker") as docker:
                with self.assertRaisesRegex(SetupError, "startup helper failed"):
                    instance.start_containers()
                docker.assert_not_called()

    def test_docker_boot_enablement_requires_consent_and_verification(self):
        instance, console, confirm = MagicMock(), MagicMock(), MagicMock(return_value=True)
        instance.docker.return_value = '"unix:///var/run/docker.sock"'
        results = [subprocess.CompletedProcess([], 1, "disabled"), subprocess.CompletedProcess([], 0, ""), subprocess.CompletedProcess([], 0, "enabled\n")]
        with patch("assistant_agent.setup.platform.system", return_value="Linux"), patch("assistant_agent.setup.Path.is_dir", return_value=True), patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/systemctl"), patch("assistant_agent.setup.os.geteuid", return_value=1000), patch.dict(os.environ, {}, clear=True), patch("assistant_agent.setup.subprocess.run", side_effect=results) as run:
            self.assertTrue(configure_docker_boot(instance, confirm, console))
        confirm.assert_called_once()
        self.assertEqual(run.call_args_list[1].args[0], ["sudo", "-n", "systemctl", "enable", "--now", "docker.service"])

    def test_docker_boot_failure_does_not_prompt_for_a_password_or_retry(self):
        instance, console, confirm = MagicMock(), MagicMock(), MagicMock(return_value=True)
        instance.docker.return_value = '"unix:///var/run/docker.sock"'
        results = [subprocess.CompletedProcess([], 1, "disabled"), subprocess.CompletedProcess([], 1, "")]
        with patch("assistant_agent.setup.platform.system", return_value="Linux"), patch("assistant_agent.setup.Path.is_dir", return_value=True), patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/systemctl"), patch("assistant_agent.setup.os.geteuid", return_value=1000), patch.dict(os.environ, {}, clear=True), patch("assistant_agent.setup.subprocess.run", side_effect=results) as run:
            self.assertFalse(configure_docker_boot(instance, confirm, console))
        self.assertEqual(run.call_count, 2)
        self.assertTrue(any("Run this yourself" in call.args[0] for call in console.print.call_args_list))

    def test_existing_or_declined_boot_setup_does_not_run_administrative_commands(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                instance, console, confirm = MagicMock(), MagicMock(), MagicMock(return_value=False)
                instance.docker.return_value = '"unix:///var/run/docker.sock"'
                result = subprocess.CompletedProcess([], 0 if enabled else 1, "enabled" if enabled else "disabled")
                with patch("assistant_agent.setup.platform.system", return_value="Linux"), patch("assistant_agent.setup.Path.is_dir", return_value=True), patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/systemctl"), patch.dict(os.environ, {}, clear=True), patch("assistant_agent.setup.subprocess.run", return_value=result) as run:
                    self.assertEqual(configure_docker_boot(instance, confirm, console), enabled)
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0], ["systemctl", "is-enabled", "docker.service"])

    def test_rootless_or_remote_docker_does_not_modify_local_system_service(self):
        for host in ("ssh://other-server", "unix:///run/user/1000/docker.sock"):
            with self.subTest(host=host):
                instance, console, confirm = MagicMock(), MagicMock(), MagicMock()
                instance.docker.return_value = json.dumps(host)
                with patch("assistant_agent.setup.platform.system", return_value="Linux"), patch("assistant_agent.setup.Path.is_dir", return_value=True), patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/systemctl"), patch.dict(os.environ, {}, clear=True), patch("assistant_agent.setup.subprocess.run") as run:
                    self.assertFalse(configure_docker_boot(instance, confirm, console))
                run.assert_not_called()
                confirm.assert_not_called()
    def test_migration_preserves_values_and_removes_secret_environment(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "data").mkdir(exist_ok=True)
            (path / "src").mkdir(exist_ok=True)
            original = "# Keep my comment\nOPENROUTER_API_KEY='private-$#token'\nPOSTGRES_PASSWORD='" + "x" * 32 + "'\nUNKNOWN='custom'\n"
            (path / "data/.env").write_text(original)
            instance = Installation(root)
            instance.save()
            env = (path / "data/.env").read_text()
            self.assertIn("# Keep my comment", env)
            self.assertIn("UNKNOWN='custom'", env)
            self.assertNotIn("private-$#token", env)
            self.assertNotIn("POSTGRES_PASSWORD=", env)
            self.assertEqual((path / "data/.secrets/OPENROUTER_API_KEY").read_text(), "private-$#token")
            self.assertEqual(stat.S_IMODE((path / "data/.secrets").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((path / "data/.secrets/OPENROUTER_API_KEY").stat().st_mode), 0o600)
            sandbox_token = (path / "data/.secrets/SANDBOX_API_TOKEN").read_text()
            self.assertGreaterEqual(len(sandbox_token), 32)
            self.assertEqual(stat.S_IMODE((path / "data/.secrets/SANDBOX_API_TOKEN").stat().st_mode), 0o600)
            for name in ("postgres.crt", "postgres.key", "sandbox.crt", "sandbox.key"):
                self.assertTrue((path / "data/private/tls" / name).is_file())
            self.assertEqual(stat.S_IMODE((path / "data/private/tls/postgres.key").stat().st_mode), 0o600)
            overlay = yaml.safe_load((path / "data/.compose-secrets.yaml").read_text())
            self.assertEqual(overlay["services"]["sandbox"]["secrets"], ["SANDBOX_API_TOKEN"])
            self.assertIn("SANDBOX_API_TOKEN", overlay["services"]["task-agent"]["secrets"])
            self.assertNotIn("SANDBOX_API_TOKEN", overlay["services"]["downloader"]["secrets"])
            self.assertNotIn("OPENROUTER_API_KEY", overlay["services"]["workspace-indexer"]["secrets"])
            self.assertIsNone(overlay["services"]["task-agent"]["environment"]["OPENROUTER_API_KEY"])
            self.assertEqual(Installation(root).secret_values, instance.secret_values)

    def test_unchanged_save_preserves_secret_inode_and_public_tls_modes(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.save()
            token = Path(root) / "data/.secrets/SANDBOX_API_TOKEN"
            original_inode = token.stat().st_ino
            instance.save()
            self.assertEqual(token.stat().st_ino, original_inode)
            self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
            for name in ("postgres.crt", "sandbox.crt"):
                self.assertEqual(stat.S_IMODE((Path(root) / "data/private/tls" / name).stat().st_mode), 0o644)
            private_key = Path(root) / "data/private/tls/sandbox.key"
            private_key.chmod(0o640)
            instance.save()
            self.assertEqual(stat.S_IMODE(private_key.stat().st_mode), 0o600)

    def test_sandbox_helper_gets_host_secret_and_activation_stages_before_broker(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "data").mkdir(exist_ok=True)
            (path / "src").mkdir(exist_ok=True)
            atomic_write(path / "src/docker-compose.yml", b"services:\n  sandbox: {}\n  sandbox-init: {}\n  task-agent: {}\n")
            instance = Installation(root)
            instance.save()
            grants = instance.overlay()["services"]
            self.assertEqual(grants["sandbox"]["secrets"], [])
            self.assertEqual(grants["sandbox-init"]["secrets"], ["SANDBOX_API_TOKEN"])
            instance.state["pending"] = ["SANDBOX_API_TOKEN"]
            with patch.object(instance, "compose", side_effect=["sandbox\ntask-agent\n", "", "", "", ""]) as compose:
                self.assertEqual(instance.apply(["SANDBOX_API_TOKEN"]), ["sandbox", "task-agent"])
            commands = [call.args[0] for call in compose.call_args_list]
            self.assertEqual(commands[1], ["stop", "sandbox"])
            self.assertEqual(commands[2], ["run", "--rm", "--no-deps", "sandbox-init"])
            self.assertNotIn("SANDBOX_API_TOKEN", instance.state["pending"])

    def test_failed_sandbox_staging_keeps_activation_pending(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "data").mkdir(exist_ok=True)
            (path / "src").mkdir(exist_ok=True)
            atomic_write(path / "src/docker-compose.yml", b"services:\n  sandbox: {}\n  sandbox-init: {}\n")
            instance = Installation(root)
            instance.save()
            instance.state["pending"] = ["SANDBOX_API_TOKEN"]
            with patch.object(instance, "compose", side_effect=["sandbox\n", "", SetupError("staging failed")]):
                with self.assertRaises(SetupError):
                    instance.apply(["SANDBOX_API_TOKEN"])
            self.assertIn("SANDBOX_API_TOKEN", instance.state["pending"])

    def test_rejects_duplicates_symlinks_and_concurrent_changes(self):
        with self.assertRaises(SetupError):
            parse_environment("A=one\nA=two\n")
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "data").mkdir(exist_ok=True)
            (path / "src").mkdir(exist_ok=True)
            (path / "other").write_text("private")
            (path / "data/.env").symlink_to(path / "other")
            with self.assertRaises(SetupError):
                Installation(root)
            (path / "data/.env").unlink()
            instance = Installation(root)
            (path / "data/.env").write_text("UPDATED=yes\n")
            with self.assertRaises(SetupError):
                instance.save()
            self.assertFalse((path / "data/.secrets").exists())

    def test_removed_calendar_settings_do_not_reappear_on_reload(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.values = {"CALENDAR_ENABLED": "true", "CALENDAR_DEFAULT_PROVIDER": "google", "CALENDAR_GOOGLE_CALENDAR_ID": "old-calendar", "CALENDAR_ZOHO_CALENDAR_ID": "keep-calendar"}
            instance.secret_values = {"CALENDAR_GOOGLE_CLIENT_ID": "old-id", "CALENDAR_GOOGLE_CLIENT_SECRET": "old-secret", "CALENDAR_ZOHO_CLIENT_ID": "keep-id"}
            instance.save()
            instance.remove_settings("CALENDAR_GOOGLE_")
            instance.values["CALENDAR_DEFAULT_PROVIDER"] = "zoho"
            instance.save()
            reloaded = Installation(root)
            self.assertNotIn("CALENDAR_GOOGLE_CLIENT_ID", reloaded.secret_values)
            self.assertNotIn("CALENDAR_GOOGLE_CALENDAR_ID", reloaded.values)
            self.assertEqual(reloaded.secret_values["CALENDAR_ZOHO_CLIENT_ID"], "keep-id")
            self.assertFalse((Path(root) / "data/.secrets/CALENDAR_GOOGLE_CLIENT_SECRET").exists())
            self.assertNotIn("CALENDAR_GOOGLE_CLIENT_ID", reloaded.overlay()["secrets"])
            with patch.object(reloaded, "compose", side_effect=["task-agent\n", ""]) as compose:
                self.assertEqual(reloaded.apply(["CALENDAR_GOOGLE_CLIENT_ID"]), ["task-agent"])
                self.assertIn("--force-recreate", compose.call_args.args[0])

    def test_failed_removal_restores_secret_files(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.secret_values["ZOOM_CLIENT_SECRET"] = "original-secret"
            instance.save()
            original_environment = (Path(root) / "data/.env").read_bytes()
            instance.remove_settings("ZOOM_")
            counter = 0

            def fail_once(path, content, mode=0o600):
                nonlocal counter
                counter += 1
                if counter == 1:
                    raise OSError("simulated failure after removal")
                atomic_write(path, content, mode=mode)

            with patch("assistant_agent.setup.atomic_write", side_effect=fail_once):
                with self.assertRaises(SetupError):
                    instance.save()
            self.assertEqual((Path(root) / "data/.secrets/ZOOM_CLIENT_SECRET").read_text(), "original-secret")
            self.assertEqual((Path(root) / "data/.env").read_bytes(), original_environment)

    def test_environment_round_trip_and_invalid_secret_no_write(self):
        value = "literal$#'\\space value"
        output = render_environment("# original\n", {"SETTING": value}, set())
        self.assertEqual(parse_environment(output)[0]["SETTING"], value)
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.secret_values["POSTGRES_PASSWORD"] = "password"
            with self.assertRaises(SetupError):
                instance.save()
            self.assertFalse((Path(root) / "data/.env").exists())

    def test_apply_recreates_only_running_affected_services(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.secret_values["OPENROUTER_API_KEY"] = "private-credential"
            instance.save()
            with patch.object(instance, "compose", side_effect=["task-agent\nsandbox\npostgres\n", ""]) as compose:
                targets = instance.apply(["OPENROUTER_API_KEY"])
                self.assertEqual(targets, ["task-agent"])
                self.assertEqual(compose.call_args.args[0], ["up", "-d", "--no-deps", "--force-recreate", "task-agent"])

    def test_database_rotation_requires_live_database_and_protects_value(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.secret_values["POSTGRES_PASSWORD"] = "old" * 12
            with patch.object(instance, "compose", return_value="task-agent"):
                with self.assertRaises(SetupError):
                    instance.rotate_database("new" * 12)
            self.assertFalse((Path(root) / "data/.secrets").exists())
            with patch.object(instance, "compose", return_value="") as compose:
                instance.set_database_password("quote'and\\dollar$")
                arguments, stdin = compose.call_args.args
                self.assertNotIn("quote", " ".join(arguments))
                self.assertIn("quote''and\\dollar$", stdin)

    def test_failed_save_restores_previous_configuration(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.secret_values["OPENROUTER_API_KEY"] = "original-key"
            instance.save()
            original = (Path(root) / "data/.env").read_bytes()
            instance.secret_values["OPENROUTER_API_KEY"] = "replacement-key"
            counter = 0

            def fail_once(path, content, mode=0o600):
                nonlocal counter
                counter += 1
                if counter == 2:
                    raise OSError("simulated failure")
                atomic_write(path, content, mode=mode)

            with patch("assistant_agent.setup.atomic_write", side_effect=fail_once):
                with self.assertRaises(SetupError):
                    instance.save()
            self.assertEqual((Path(root) / "data/.env").read_bytes(), original)
            self.assertEqual((Path(root) / "data/.secrets/OPENROUTER_API_KEY").read_text(), "original-key")

    def test_stale_secret_session_cannot_overwrite_newer_value(self):
        with tempfile.TemporaryDirectory() as root:
            first = Installation(root)
            first.secret_values["OPENROUTER_API_KEY"] = "original-key"
            first.save()
            stale = Installation(root)
            current = Installation(root)
            current.secret_values["OPENROUTER_API_KEY"] = "newer-key"
            current.save()
            stale.secret_values["OPENROUTER_API_KEY"] = "stale-key"
            with self.assertRaises(SetupError):
                stale.save()
            with self.assertRaises(SetupError):
                stale.apply(["OPENROUTER_API_KEY"])
            self.assertEqual(Installation(root).secret_values["OPENROUTER_API_KEY"], "newer-key")

    def test_database_failure_leaves_recovery_record(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.secret_values["POSTGRES_PASSWORD"] = "previous" * 8
            with patch.object(instance, "compose", side_effect=["postgres\ntask-agent", "", SetupError("failure")]):
                with self.assertRaisesRegex(SetupError, "recover"):
                    instance.rotate_database("replacement" * 8)
            record = Path(root) / "data/.secrets/rotation.json"
            self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
            with self.assertRaisesRegex(SetupError, "recover"):
                Installation(root)

    def test_draft_validation_rejects_partial_integrations(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.values = {"AGENT_TIMEZONE": "UTC", "AGENT_LLM_MODEL": "model", "AGENT_LLM_BASE_URL": "https://openrouter.ai/api/v1", "CALENDAR_ENABLED": "true", "CALENDAR_DEFAULT_PROVIDER": "google"}
            instance.secret_values = {"POSTGRES_PASSWORD": "strong-password", "OPENROUTER_API_KEY": "key", "CALENDAR_GOOGLE_CLIENT_ID": "client"}
            with self.assertRaises(SetupError):
                validate_draft(instance)
            instance.values["CALENDAR_ENABLED"] = "false"
            validate_draft(instance)

    def test_cancel_wizard_writes_nothing(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            options = argparse.Namespace(root=Path(root), command=None)
            with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=["Agent", "UTC", "Name", "user@example.com", "", "", "", "Cancel"]):
                interactive(options)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_real_terminal_cancel_preserves_installation(self):
        import fcntl
        import struct
        import termios
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as tools:
            docker = Path(tools) / "docker"
            docker.write_text("#!/bin/sh\nexit 0\n")
            docker.chmod(0o700)
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 50, 0, 0))
            environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"), PATH=tools + os.pathsep + os.environ.get("PATH", ""))
            process = subprocess.Popen([sys.executable, "-m", "assistant_agent.setup", "--root", root], stdin=slave, stdout=slave, stderr=slave, env=environment)
            os.close(slave)
            try:
                captured = b""

                def read_until(marker):
                    nonlocal captured
                    while marker not in captured:
                        ready, _, _ = select.select([master], [], [], 10)
                        self.assertTrue(ready, "CLI did not render " + marker.decode())
                        captured += os.read(master, 8192)

                read_until(b"Assistant name")
                process.send_signal(signal.SIGINT)
                read_until(b"Continue")
                self.assertIn(b"Section changes discarded", captured)
                # Raw-mode menus receive Ctrl-C as a keystroke, not SIGINT.
                os.write(master, b"\x03")
                self.assertEqual(process.wait(timeout=10), 0)
                self.assertIn(b"Identity and timezone", captured)
                self.assertEqual(list(Path(root).iterdir()), [])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)

    def test_complete_local_setup_and_masked_secret_update(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "data").mkdir(exist_ok=True)
            (path / "src").mkdir(exist_ok=True)
            (path / "data/config").mkdir(parents=True, exist_ok=True)
            (path / "src/config").mkdir(parents=True, exist_ok=True)
            (path / "src/config/AGENT.md.example").write_text("Assistant instructions\n")
            options = argparse.Namespace(root=path, command=None)
            responses = [
                "Assistant", "UTC", "User", "user@example.com", "", "", "", "Next",
                "OpenRouter", "https://openrouter.ai/api/v1", "Enter replacement", "PRIVATE-CANARY-KEY",
                "anthropic/claude-sonnet-4.6", "", "Generate securely", "Next",
                False, "Next", False, "Next", False, "Next", False, "Next", False, "Next", False, "Next", False, "Next", False, "none", "Next", "done", "Next", True, "Next", "save", False,
            ]
            with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                interactive(options)
            instance = Installation(root)
            self.assertEqual(instance.values["COMPOSE_PROFILES"], "")
            self.assertEqual(instance.values["AGENT_DEEP_RESEARCH_ENABLED"], "false")
            self.assertEqual((path / "data/config/AGENT.md").read_text(), "Assistant instructions\n")
            self.assertNotIn("PRIVATE-CANARY-KEY", (path / "data/.env").read_text())
            options = argparse.Namespace(root=path, command="secrets", action="update", name="OPENROUTER_API_KEY")
            with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=["Enter replacement", "REPLACEMENT-CANARY", True, False]):
                interactive(options)
            self.assertEqual(Installation(root).secret_values["OPENROUTER_API_KEY"], "REPLACEMENT-CANARY")

    def test_private_postgres_directory_does_not_block_retaining_saved_password(self):
        import questionary
        for password in ("existing-database-password", None, "password"):
            with self.subTest(password_saved=bool(password)), tempfile.TemporaryDirectory() as root:
                path = Path(root)
                (path / "data").mkdir(exist_ok=True)
                (path / "src").mkdir(exist_ok=True)
                original = "OPENROUTER_API_KEY='existing-model-key'\n"
                if password:
                    original += "POSTGRES_PASSWORD='" + password + "'\n"
                (path / "data/.env").write_text(original)
                original_exists = Path.exists

                def restricted_exists(target):
                    if target.name == "PG_VERSION":
                        raise PermissionError("Docker-owned PostgreSQL data directory")
                    return original_exists(target)

                responses = ["Assistant", "UTC", "User", "user@example.com", "", "", "", "Next",
                    "OpenRouter", "https://openrouter.ai/api/v1", "Keep existing", "model", "", "Cancel"]
                options = argparse.Namespace(root=path, command=None)
                with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(Path, "exists", restricted_exists), patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                    if password == "existing-database-password":
                        interactive(options)
                    else:
                        with self.assertRaisesRegex(SetupError, "will not generate a replacement"):
                            interactive(options)
                self.assertEqual((path / "data/.env").read_text(), original)

    def test_existing_instructions_are_replaced_only_with_separate_confirmation(self):
        import questionary
        for replace in (False, True):
            with self.subTest(replace=replace), tempfile.TemporaryDirectory() as root:
                path = Path(root)
                (path / "data").mkdir(exist_ok=True)
                (path / "src").mkdir(exist_ok=True)
                (path / "data/config").mkdir(parents=True, exist_ok=True)
                (path / "src/config").mkdir(parents=True, exist_ok=True)
                (path / "src/config/AGENT.md.example").write_text("Example instructions\n")
                (path / "data/config/AGENT.md").write_text("Custom instructions\n")
                responses = ["Assistant", "UTC", "User", "user@example.com", "", "", "", "Next",
                    "OpenRouter", "https://openrouter.ai/api/v1", "Enter replacement", "model-key", "model", "", "Generate securely", "Next",
                    False, "Next", False, "Next", False, "Next", False, "Next", False, "Next", False, "Next", False, "Next", False, "none", "Next", "done", "Next", True, replace, "Next", "save", False]
                output = io.StringIO()
                options = argparse.Namespace(root=path, command=None)
                with redirect_stdout(output), patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=responses), patch.object(Installation, "start_containers") as start:
                    interactive(options)
                start.assert_not_called()
                self.assertEqual((path / "data/config/AGENT.md").read_text(), "Example instructions\n" if replace else "Custom instructions\n")
                self.assertIn(str(path.resolve() / "data/config/AGENT.md"), output.getvalue())

    def test_wizard_can_start_containers_after_configuration_is_saved(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "data").mkdir(exist_ok=True)
            (path / "src").mkdir(exist_ok=True)
            (path / "data/config").mkdir(parents=True, exist_ok=True)
            (path / "src/config").mkdir(parents=True, exist_ok=True)
            (path / "src/config/AGENT.md.example").write_text("Example instructions\n")
            responses = ["Assistant", "UTC", "User", "user@example.com", "", "", "", "Next",
                "OpenRouter", "https://openrouter.ai/api/v1", "Enter replacement", "model-key", "model", "", "Generate securely", "Next",
                False, "Next", False, "Next", False, "Next", False, "Next", False, "Next", False, "Next", False, "Next", False, "none", "Next", "done", "Next", True, "Next", "save", True]

            def started(instance):
                self.assertTrue((path / "data/.env").is_file())
                self.assertEqual((path / "data/config/AGENT.md").read_text(), "Example instructions\n")
                return [{"Service": "task-agent", "State": "running", "Health": ""}]

            output = io.StringIO()
            options = argparse.Namespace(root=path, command=None)
            with redirect_stdout(output), patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch("assistant_agent.setup.platform.system", return_value="Darwin"), patch.object(questionary.Question, "unsafe_ask", side_effect=responses), patch.object(Installation, "docker", return_value="28.0.0"), patch.object(Installation, "start_containers", autospec=True, side_effect=started) as start, patch("assistant_agent.setup.configure_docker_boot") as boot:
                interactive(options)
            start.assert_called_once()
            boot.assert_called_once()
            self.assertIn("No healthcheck defined", output.getvalue())

    def test_reconfiguration_keeps_custom_model_defaults(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "data").mkdir(exist_ok=True)
            (path / "src").mkdir(exist_ok=True)
            (path / "data/config").mkdir(parents=True, exist_ok=True)
            (path / "src/config").mkdir(parents=True, exist_ok=True)
            custom_url = "https://models.example.org/v1"
            (path / "data/config/agent.yaml").write_text(yaml.safe_dump({"agent": {"llm": {"base_url": custom_url, "model": "custom-model"}}}))
            options = argparse.Namespace(root=path, command=None)
            responses = ["Assistant", "UTC", "User", "user@example.com", "", "", "", "Next",
                         "OpenAI-compatible", custom_url, "Enter replacement", "PRIVATE-KEY",
                         "custom-model", "", "Generate securely", "Cancel"]
            with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary, "text", wraps=questionary.text) as text, patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                interactive(options)
            model_url = next(call for call in text.call_args_list if call.args[0] == "Model API base URL")
            model = next(call for call in text.call_args_list if call.args[0] == "Primary model identifier")
            self.assertEqual(model_url.kwargs["default"], custom_url)
            self.assertEqual(model.kwargs["default"], "custom-model")
            self.assertFalse((path / "data/.env").exists())

    def test_provider_replacement_and_zoom_mode_specific_credentials(self):
        import questionary
        for mode in ("s2s", "oauth"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                instance = Installation(root)
                instance.values = {"CALENDAR_ENABLED": "true", "CALENDAR_DEFAULT_PROVIDER": "google", "CALENDAR_GOOGLE_CALENDAR_ID": "remove-calendar", "ZOOM_ENABLED": "true", "ZOOM_AUTH_MODE": "oauth"}
                instance.secret_values = {"POSTGRES_PASSWORD": "database-password", "OPENROUTER_API_KEY": "model-key",
                    **{"CALENDAR_" + provider + "_" + field: "credential-" + field for provider in ("GOOGLE", "ZOHO") for field in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN")},
                    **{"ZOOM_" + field: "credential-" + field for field in ("CLIENT_ID", "CLIENT_SECRET", "ACCOUNT_ID", "REFRESH_TOKEN")}}
                instance.save()
                responses = ["Assistant", "UTC", "User", "user@example.com", "", "", "", "Next",
                    "OpenRouter", "https://openrouter.ai/api/v1", "Keep existing", "model", "", "Next",
                    False, "Next", False, "Next", True, ["zoho"], "Keep existing", "Keep existing", "Keep existing", "zoho-calendar", "https://calendar.zoho.com", "30", False, "Next",
                    True, mode, "Keep existing", "Keep existing", "Keep existing", "me", "Next", False, "Next", False, "Next", False, "Next", False, "none", "Next", "done", "Next", "Next", "save", False]
                output = io.StringIO()
                options = argparse.Namespace(root=Path(root), command=None)
                with redirect_stdout(output), patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=responses[:-2] + ["cancel"]):
                    interactive(options)
                self.assertIn("CALENDAR_GOOGLE_CLIENT_SECRET", Installation(root).secret_values)
                with redirect_stdout(output), patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary, "select", wraps=questionary.select) as selection, patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                    interactive(options)
                reloaded = Installation(root)
                self.assertEqual(reloaded.values["CALENDAR_DEFAULT_PROVIDER"], "zoho")
                self.assertFalse(any(name.startswith("CALENDAR_GOOGLE_") for name in reloaded.secret_values))
                self.assertNotIn("CALENDAR_GOOGLE_CALENDAR_ID", reloaded.values)
                required = "ZOOM_ACCOUNT_ID" if mode == "s2s" else "ZOOM_REFRESH_TOKEN"
                removed = "ZOOM_REFRESH_TOKEN" if mode == "s2s" else "ZOOM_ACCOUNT_ID"
                self.assertIn(required, reloaded.secret_values)
                self.assertNotIn(removed, reloaded.secret_values)
                prompts = [call.args[0] for call in selection.call_args_list]
                self.assertIn("ZOOM_CLIENT_SECRET", prompts)
                self.assertIn(required, prompts)
                self.assertNotIn(removed, prompts)
                self.assertIn("REMOVE on save", output.getvalue())
                self.assertIn("webhook Secret Token is not used", output.getvalue())
                self.assertIn("Saved.", output.getvalue())

    def test_validation_error_can_be_fixed_then_saved_from_review(self):
        import questionary
        with tempfile.TemporaryDirectory() as root:
            responses = ["Assistant", "BAD_TIMEZONE", "User", "user@example.com", "", "", "", "Next",
                "OpenRouter", "https://openrouter.ai/api/v1", "Enter replacement", "model-key", "model", "", "Generate securely", "Next",
                False, "Next", False, "Next", False, "Next", False, "Next", False, "Next", False, "Next", False, "Next", False, "none", "Next", "done", "Next", "Next", "edit", "identity", "Edit this section",
                "Assistant", "UTC", "User", "user@example.com", "", "", "", "save", False]
            output = io.StringIO()
            options = argparse.Namespace(root=Path(root), command=None)
            with redirect_stdout(output), patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary, "select", wraps=questionary.select) as selection, patch.object(questionary.Question, "unsafe_ask", side_effect=responses):
                interactive(options)
            self.assertEqual(Installation(root).values["AGENT_TIMEZONE"], "UTC")
            reviews = [call for call in selection.call_args_list if call.args[0] == "Finish setup"]
            self.assertEqual(len(reviews), 2)
            self.assertTrue(reviews[0].kwargs["choices"][0].disabled)
            self.assertFalse(reviews[1].kwargs["choices"][0].disabled)
            self.assertIn("NOT SAVED. Fix this before saving", output.getvalue())
            self.assertIn("Ready to save", output.getvalue())

    def test_missing_zoom_credential_error_names_the_field(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.values = {"AGENT_LLM_BASE_URL": "https://openrouter.ai/api/v1", "AGENT_LLM_MODEL": "model", "ZOOM_ENABLED": "true", "ZOOM_AUTH_MODE": "s2s"}
            instance.secret_values = {"POSTGRES_PASSWORD": "database-password", "OPENROUTER_API_KEY": "model-key", "ZOOM_CLIENT_ID": "client", "ZOOM_ACCOUNT_ID": "account"}
            with self.assertRaisesRegex(SetupError, "ZOOM_CLIENT_SECRET"):
                validate_draft(instance)

    @unittest.skipUnless(shutil.which("openssl"), "requires OpenSSL")
    def test_internal_tls_certificates_have_service_hostname_identity(self):
        files = prepare_internal_tls()
        with tempfile.TemporaryDirectory() as root:
            for service in ("postgres", "sandbox"):
                certificate = Path(root) / (service + ".crt")
                certificate.write_bytes(files[service + ".crt"])
                result = subprocess.run(
                    ["openssl", "x509", "-in", str(certificate), "-noout", "-checkhost", service],
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                trust = subprocess.run(
                    ["openssl", "verify", "-verify_hostname", service, "-CAfile", str(certificate), str(certificate)],
                    capture_output=True,
                )
                self.assertEqual(trust.returncode, 0, trust.stderr.decode(errors="replace"))

    @unittest.skipUnless(shutil.which("openssl"), "requires OpenSSL")
    def test_partial_internal_tls_set_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            tls = Path(root) / "data/private/tls"
            tls.mkdir(parents=True)
            (tls / "postgres.crt").write_text("partial", encoding="utf-8")
            with self.assertRaisesRegex(SetupError, "Internal service TLS files are incomplete"):
                Installation(root).save()

    @unittest.skipUnless(os.getenv("ASSISTANT_TEST_DOCKER") == "1" and shutil.which("docker"), "requires opt-in disposable Docker tests")
    def test_compose_consumes_file_secrets_without_environment_payloads(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "data").mkdir(exist_ok=True)
            (path / "src").mkdir(exist_ok=True)
            repository = Path(__file__).resolve().parents[2]
            shutil.copyfile(repository / "docker-compose.yml", path / "src/docker-compose.yml")
            instance = Installation(root)
            instance.values = {"COMPOSE_PROFILES": "email", "COMPOSE_PROJECT_NAME": "assistant-secret-test-" + path.name.lower(), "LITERAL_SETTING": "literal$#'\\value"}
            instance.secret_values = {"POSTGRES_PASSWORD": "DATABASE-PRIVATE-CANARY", "OPENROUTER_API_KEY": "MODEL-PRIVATE-CANARY"}
            instance.save()
            rendered = json.loads(instance.compose(["config", "--format", "json"]))
            for name, service in rendered["services"].items():
                environment = service.get("environment", {})
                self.assertNotIn("DATABASE-PRIVATE-CANARY", json.dumps(environment))
                self.assertNotIn("MODEL-PRIVATE-CANARY", json.dumps(environment))
                if name in {"task-agent", "downloader"}:
                    self.assertEqual(environment["LITERAL_SETTING"].replace("$$", "$"), "literal$#'\\value")
            self.assertEqual(rendered["services"]["postgres"]["environment"]["POSTGRES_PASSWORD_FILE"], "/run/secrets/POSTGRES_PASSWORD")
            self.assertNotIn("secrets", rendered["services"]["sandbox"])
            overlay_path = path / "data/.compose-secrets.yaml"
            overlay = yaml.safe_load(overlay_path.read_text())
            overlay["services"]["probe"] = {"image": "postgres:16", "env_file": "../data/.env", "entrypoint": ["/bin/sh"], "command": ["-c", "printf '%s' \"$$LITERAL_SETTING\""]}
            atomic_write(overlay_path, yaml.safe_dump(overlay).encode())
            try:
                actual = instance.compose(["run", "--rm", "--no-deps", "probe"])
                self.assertEqual(actual, "literal$#'\\value")
            finally:
                instance.compose(["down"])

    @unittest.skipUnless(os.getenv("ASSISTANT_TEST_DOCKER") == "1" and shutil.which("docker"), "requires opt-in disposable Docker tests")
    def test_model_dns_uses_internal_network_instead_of_blocked_egress(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "data").mkdir(exist_ok=True)
            (path / "src").mkdir(exist_ok=True)
            repository = Path(__file__).resolve().parents[2]
            production = yaml.safe_load((repository / "docker-compose.yml").read_text())
            base = {"name": "assistant-model-routing-test-" + path.name.lower(), "services": {
                "ollama": {"image": "python:3.12-slim", "command": ["python", "-m", "http.server", "11434"],
                           "networks": production["services"]["ollama"]["networks"],
                           "healthcheck": {"test": ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:11434',timeout=2)"], "interval": "1s", "retries": 20}},
                "client": {"image": "python:3.12-slim", "networks": ["model_net", "egress_net"], "profiles": ["unused"]}},
                "networks": {name: production["networks"][name] for name in ("model_net", "egress_net", "service_egress_net")}}
            atomic_write(path / "src/docker-compose.yml", yaml.safe_dump(base).encode())
            instance = Installation(root)
            instance.save()
            try:
                instance.compose(["up", "-d", "--wait", "--wait-timeout", "30", "ollama"])
                identity = instance.compose(["ps", "-q", "ollama"]).strip()
                networks = json.loads(instance.docker(["inspect", "--format", "{{json .NetworkSettings.Networks}}", identity]))
                expected = next(spec["IPAddress"] for name, spec in networks.items() if name.endswith("_model_net"))
                probe = instance.compose(["run", "--rm", "--no-deps", "client", "python", "-c",
                                          "import json,socket,urllib.request; "
                                          "print(json.dumps({'addresses':socket.gethostbyname_ex('ollama')[2],"
                                          "'status':urllib.request.urlopen('http://ollama:11434',timeout=5).status}))"])
                result = json.loads(probe)
                self.assertEqual(set(result["addresses"]), {expected})
                self.assertEqual(result["status"], 200)
            finally:
                instance.compose(["down", "--volumes"])

    @unittest.skipUnless(os.getenv("ASSISTANT_TEST_DOCKER") == "1" and shutil.which("docker"), "requires opt-in disposable Docker tests")
    def test_postgres_missing_tls_never_creates_directories_and_fresh_data_initializes(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "data").mkdir(exist_ok=True)
            (path / "src").mkdir(exist_ok=True)
            repository = Path(__file__).resolve().parents[2]
            configuration = yaml.safe_load((repository / "docker-compose.yml").read_text())
            postgres = configuration["services"]["postgres"]
            postgres["networks"] = ["default"]
            postgres["volumes"][0] = "test_database:/var/lib/postgresql/data"
            base = {"name": "assistant-postgres-startup-test-" + path.name.lower(), "services": {"postgres": postgres},
                    "volumes": {"test_database": {}}}
            shutil.copytree(repository / "database", path / "src/database")
            for name in ("pg_hba.conf", "schema.sql"):
                (path / "src/database" / name).chmod(0o644)
            atomic_write(path / "src/docker-compose.yml", yaml.safe_dump(base).encode())
            instance = Installation(root)
            instance.secret_values["POSTGRES_PASSWORD"] = "PRIVATE-DISPOSABLE-PASSWORD" * 2
            instance.save()
            missing = path / "data/private/tls/postgres.crt"
            preserved = missing.read_bytes()
            missing.unlink()
            try:
                with self.assertRaises(SetupError):
                    instance.compose(["up", "-d", "postgres"])
                self.assertFalse(missing.exists())
                atomic_write(missing, preserved, mode=0o644)
                instance.compose(["up", "-d", "--wait", "--wait-timeout", "90", "postgres"])
                instance.verify_database_password(instance.secret_values["POSTGRES_PASSWORD"])
                result = instance.compose(["exec", "-T", "postgres", "psql", "-U", "assistant", "-d", "assistant", "-Atc", "SELECT count(*) FROM emails"])
                self.assertEqual(result.strip(), "0")
            finally:
                instance.compose(["down", "--volumes"])

    @unittest.skipUnless(os.getenv("ASSISTANT_TEST_DOCKER") == "1" and shutil.which("docker"), "requires opt-in disposable Docker tests")
    def test_live_database_rotation_preserves_data_and_changes_authentication(self):
        self.live_database_rotation(use_volume=False)

    @unittest.skipUnless(os.getenv("ASSISTANT_TEST_DOCKER") == "1" and shutil.which("docker"), "requires opt-in disposable Docker tests")
    def test_live_volume_database_rotation_preserves_data_and_changes_authentication(self):
        self.live_database_rotation(use_volume=True)

    def live_database_rotation(self, use_volume):
        from assistant_agent.setup import AGENT_SERVICES
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "data").mkdir(exist_ok=True)
            (path / "src").mkdir(exist_ok=True)
            base = {"name": "assistant-rotation-test-" + path.name.lower(), "services": {
                name: {"image": "postgres:16", "profiles": ["unused"]} for name in AGENT_SERVICES
            }, "volumes": {"test_database": {}}}
            base["services"]["postgres"] = {
                "image": "postgres:16", "environment": {"POSTGRES_USER": "assistant", "POSTGRES_DB": "assistant"},
                "entrypoint": ["/bin/sh", "-ec"],
                "command": ["install -o postgres -g postgres -m 0644 /run/assistant/tls/postgres.crt /tmp/server.crt; "
                            "install -o postgres -g postgres -m 0600 /run/assistant/tls/postgres.key /tmp/server.key; "
                            "exec /usr/local/bin/docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key"],
                "volumes": ["test_database:/var/lib/postgresql/data",
                            "../data/private/tls/postgres.crt:/run/assistant/tls/postgres.crt:ro",
                            "../data/private/tls/postgres.key:/run/assistant/tls/postgres.key:ro"],
                "healthcheck": {"test": ["CMD", "pg_isready", "-U", "assistant", "-d", "assistant"], "interval": "1s", "retries": 30},
            }
            atomic_write(path / "src/docker-compose.yml", yaml.safe_dump(base).encode())
            instance = Installation(root)
            previous = "PREVIOUS-TEST-PASSWORD" * 2
            replacement = "NEW-$'\\PASSWORD-CANARY" * 2
            instance.secret_values["POSTGRES_PASSWORD"] = previous
            instance.save()
            try:
                if use_volume:
                    instance.use_credential_volume = True
                    instance.save()
                instance.compose(["up", "-d", "--wait", "postgres"])
                instance.compose(["exec", "-T", "postgres", "psql", "-U", "assistant", "-d", "assistant", "-c", "CREATE TABLE preserved_canary(id int); INSERT INTO preserved_canary VALUES (7)"])
                instance.rotate_database(replacement)
                instance.compose(["up", "-d", "--wait", "postgres"])
                instance.verify_database_password(replacement)
                with self.assertRaises(SetupError):
                    instance.verify_database_password(previous)
                rows = instance.compose(["exec", "-T", "postgres", "psql", "-U", "assistant", "-d", "assistant", "-Atc", "SELECT id FROM preserved_canary"])
                self.assertEqual(rows.strip(), "7")
                self.assertEqual(Installation(root).secret_values["POSTGRES_PASSWORD"], replacement)
                self.assertFalse((path / "data/.secrets/rotation.json").exists())
            finally:
                instance.compose(["down", "--volumes"])
                if instance.credential_volume:
                    instance.docker(["volume", "rm", instance.credential_volume.name])

    @unittest.skipUnless(os.getenv("ASSISTANT_TEST_DOCKER") == "1" and shutil.which("docker"), "requires opt-in disposable Docker tests")
    def test_live_startup_waits_for_health_and_leaves_one_shot_policy_unchanged(self):
        from assistant_agent.setup import AGENT_SERVICES
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "data").mkdir(exist_ok=True)
            (path / "src").mkdir(exist_ok=True)
            base = {"name": "assistant-startup-test-" + path.name.lower(), "services": {
                name: {"image": "postgres:16", "profiles": ["unused"]} for name in AGENT_SERVICES
            }}
            base["services"]["task-agent"] = {"image": "postgres:16", "entrypoint": ["tail", "-f", "/dev/null"], "restart": "no",
                "healthcheck": {"test": ["CMD", "test", "-r", "/run/secrets/OPENROUTER_API_KEY"], "interval": "1s", "retries": 30}}
            base["services"]["ollama-model-puller"] = {"image": "postgres:16", "entrypoint": ["/bin/sh", "-c", "exit 0"], "restart": "no"}
            atomic_write(path / "src/docker-compose.yml", yaml.safe_dump(base).encode())
            instance = Installation(root)
            instance.secret_values["OPENROUTER_API_KEY"] = "synthetic-key"
            instance.save()
            try:
                rows = instance.start_containers()
                worker = next(row for row in rows if row["Service"] == "task-agent")
                self.assertEqual(worker["Health"], "healthy")
                self.assertEqual(json.loads(instance.docker(["inspect", "--format", "{{json .HostConfig.RestartPolicy.Name}}", worker["ID"]])), "unless-stopped")
                helper = next(row for row in rows if row["Service"] == "ollama-model-puller")
                self.assertEqual(json.loads(instance.docker(["inspect", "--format", "{{json .HostConfig.RestartPolicy.Name}}", helper["ID"]])), "no")
            finally:
                instance.compose(["down"])



if __name__ == "__main__":
    unittest.main()