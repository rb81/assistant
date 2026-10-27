import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import migrate_database as database
import migrate_layout as layout


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.root = HERE / (".test-work-" + uuid.uuid4().hex)
        self.root.mkdir()
        (self.root / "config").mkdir()
        (self.root / "config/agent.yaml").write_text("custom: true\n")
        (self.root / "config/AGENT.md").write_text("Personal instructions")
        (self.root / "config/private-extra").mkdir()
        (self.root / "config/private-extra/private").write_text("do not copy")
        (self.root / ".env").write_text(
            "# comment\nCUSTOM='unchanged'\n"
            "COMPOSE_PROJECT_NAME=old-project\n"
            "COMPOSE_FILE=docker-compose.yml:.compose-secrets.yaml\n"
            "SANDBOX_SHARED_ROOT_HOST=./data/share\n"
            f"POSTGRES_PASSWORD_FILE='{self.root}/.secrets/POSTGRES_PASSWORD'\n"
            "OPENAI_API_KEY_FILE=/run/secrets/OPENAI_API_KEY\n"
        )
        (self.root / ".env").chmod(0o600)
        (self.root / ".secrets").mkdir(mode=0o700)
        (self.root / ".secrets/POSTGRES_PASSWORD").write_bytes(b"synthetic-$-credential\n")
        (self.root / ".secrets/POSTGRES_PASSWORD").chmod(0o600)
        self.state = b'{"credential_volume":"assistant-credentials-0123456789abcdef","pending":[]}'
        (self.root / ".secrets/state.json").write_bytes(self.state)
        (self.root / "data/share").mkdir(parents=True)
        (self.root / "data/share/keep").write_text("persistent")

    def tearDown(self):
        shutil.rmtree(self.root)

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): (
                path.lstat().st_mode, path.read_bytes() if path.is_file() else None
            )
            for path in self.root.rglob("*")
        }

    def run_layout(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return layout.main(["--root", str(self.root), *args])

    def test_default_dry_run_changes_nothing(self):
        before = self.snapshot()
        self.assertEqual(self.run_layout(), 0)
        self.assertEqual(self.snapshot(), before)

    def test_late_collision_refuses_before_any_move(self):
        (self.root / "backups").mkdir()
        (self.root / "data/backups").mkdir()
        before = self.snapshot()
        self.assertEqual(self.run_layout("--apply", "--backup-confirmed"), 1)
        self.assertEqual(self.snapshot(), before)

    def test_apply_preserves_secrets_modes_custom_config_and_volume_identity(self):
        self.assertEqual(self.run_layout("--apply", "--backup-confirmed"), 0)
        self.assertFalse((self.root / ".env").exists())
        self.assertEqual((self.root / "data/.env").stat().st_mode & 0o777, 0o600)
        secret = self.root / "data/.secrets/POSTGRES_PASSWORD"
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
        self.assertEqual(secret.read_bytes(), b"synthetic-$-credential\n")
        self.assertEqual((self.root / "data/.secrets/state.json").read_bytes(), self.state)
        self.assertEqual((self.root / "data/config/AGENT.md").read_text(), "Personal instructions")
        self.assertEqual((self.root / "config/agent.yaml").read_text(), "custom: true\n")
        self.assertFalse((self.root / "data/config/private-extra").exists())
        env = (self.root / "data/.env").read_text()
        self.assertIn("CUSTOM='unchanged'", env)
        self.assertIn("COMPOSE_PROJECT_NAME=old-project", env)
        self.assertIn(f'{self.root}/data/.secrets/POSTGRES_PASSWORD', env)
        self.assertIn("OPENAI_API_KEY_FILE=/run/secrets/OPENAI_API_KEY", env)
        self.assertIn(f"{self.root}/src/docker-compose.yml:{self.root}/data/.compose-secrets.yaml", env)
        self.assertEqual((self.root / "data/share/keep").read_text(), "persistent")

    def test_secret_symlink_and_fifo_refused_without_changes(self):
        link = self.root / ".secrets/alias"
        link.symlink_to("POSTGRES_PASSWORD")
        self.assertEqual(self.run_layout("--apply", "--backup-confirmed"), 1)
        self.assertTrue((self.root / ".env").exists())
        link.unlink()
        os.mkfifo(link)
        self.assertEqual(self.run_layout("--apply", "--backup-confirmed"), 1)
        self.assertTrue((self.root / ".env").exists())

    def test_symlinked_destination_and_config_refused(self):
        (self.root / "data/config").symlink_to(self.root / "config")
        self.assertEqual(self.run_layout("--apply", "--backup-confirmed"), 1)
        self.assertTrue((self.root / ".env").exists())

    def test_relative_pointers_and_unknown_paths(self):
        text = ("A_FILE=./.secrets/A\nB_FILE='.secrets/B'\n"
                "C_FILE=/custom/private/C\nUNKNOWN=/old/custom/path\n")
        rewritten = layout.rewrite_env(text, self.root)
        self.assertIn(f'A_FILE="{self.root}/data/.secrets/A"', rewritten)
        self.assertIn(f'B_FILE="{self.root}/data/.secrets/B"', rewritten)
        self.assertIn("C_FILE=/custom/private/C\nUNKNOWN=/old/custom/path", rewritten)

    def test_retired_environment_removed_and_originals_archived(self):
        original = (self.root / ".env").read_text() + (
            "AGENT_API_AUTH_TOKEN='retired synthetic token'\n"
            "export ADMIN_COMMAND_SIGNING_KEY_FILE=/old/key\n"
            "AGENT_API_TLS_CERT_FILE_FILE=/old/cert\n"
            "ASSISTANT_ACCEPT_V2_MIGRATION=yes\n"
            "COMPOSE_PROFILES='api,sandbox,transcription,api' # old API\n"
            "AGENT_APP_BASE_URL=https://old.example\n"
        )
        (self.root / ".env").write_text(original)
        overrides = "SETUP_API_ENABLED=yes\nAGENT_API_AUTH_TOKEN='multi\nline token'\nCUSTOM=kept\n"
        (self.root / "overrides.env").write_text(overrides)
        retired_file = self.root / ".secrets/AGENT_API_AUTH_TOKEN"
        retired_file.write_text("retired credential retained")
        self.assertEqual(self.run_layout("--apply", "--backup-confirmed"), 0)
        env = (self.root / "data/.env").read_text()
        self.assertNotIn("AGENT_API_", env)
        self.assertNotIn("ADMIN_COMMAND_SIGNING_KEY", env)
        self.assertNotIn("ASSISTANT_ACCEPT_V2_MIGRATION", env)
        self.assertNotIn("AGENT_APP_BASE_URL", env)
        self.assertIn('COMPOSE_PROFILES="sandbox,transcription"', env)
        self.assertEqual((self.root / "data/overrides.env").read_text(), "CUSTOM=kept\n")
        archive = self.root / "data/backups" / layout.ENV_ARCHIVE
        self.assertEqual((archive / ".env").read_text(), original)
        self.assertEqual((archive / "overrides.env").read_text(), overrides)
        self.assertEqual((archive / ".env").stat().st_mode & 0o777, 0o600)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / "data/.secrets/AGENT_API_AUTH_TOKEN").read_text(),
                         "retired credential retained")

    def test_environment_backup_collision_refused_before_changes(self):
        (self.root / "backups" / layout.ENV_ARCHIVE).mkdir(parents=True)
        before = self.snapshot()
        self.assertEqual(self.run_layout("--apply", "--backup-confirmed"), 1)
        self.assertEqual(self.snapshot(), before)

    def test_retired_tracking_names_removed_without_changing_other_state(self):
        state = {
            "credential_volume": "assistant-credentials-0123456789abcdef",
            "credential_volume_sudo": True,
            "previous_credential_volume": "assistant-credentials-fedcba9876543210",
            "previous_credential_volume_sudo": False,
            "pending": ["POSTGRES_PASSWORD", "AGENT_API_AUTH_TOKEN", "OPENAI_API_KEY",
                        "ADMIN_COMMAND_SIGNING_KEY"],
            "removed": ["ADMIN_COMMAND_SIGNING_KEY", "SMTP_PASSWORD", "AGENT_API_AUTH_TOKEN"],
            "custom_setting": {"keep": True},
        }
        original = (json.dumps(state, separators=(",", ":")) + "\n").encode()
        state_path = self.root / ".secrets/state.json"
        state_path.write_bytes(original)
        state_path.chmod(0o600)
        self.assertEqual(self.run_layout("--apply", "--backup-confirmed"), 0)
        moved = self.root / "data/.secrets/state.json"
        expected = dict(state, pending=["POSTGRES_PASSWORD", "OPENAI_API_KEY"],
                        removed=["SMTP_PASSWORD"])
        self.assertEqual(json.loads(moved.read_bytes()), expected)
        self.assertEqual(moved.stat().st_mode & 0o777, 0o600)
        archived = self.root / "data/backups" / layout.ENV_ARCHIVE / "state.json"
        self.assertEqual(archived.read_bytes(), original)
        self.assertEqual(archived.stat().st_mode & 0o777, 0o600)

    def test_bad_setup_tracking_state_refused_before_any_move(self):
        (self.root / ".secrets/state.json").write_text('{"pending": "not a list"}')
        before = self.snapshot()
        self.assertEqual(self.run_layout("--apply", "--backup-confirmed"), 1)
        self.assertEqual(self.snapshot(), before)

    def test_acknowledgement_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            layout.main(["--root", str(self.root), "--apply"])


class DatabaseTests(unittest.TestCase):
    def test_marker_follows_all_migrations_in_one_transaction(self):
        sql = database.build_sql()
        self.assertIn("pg_advisory_xact_lock(hashtext('assistant_schema_upgrade'))", sql)
        marker_insert = f"INSERT INTO schema_migrations(filename) VALUES ('{database.MARKER}');"
        self.assertGreater(sql.index(marker_insert), sql.index("011_research_system_threads.sql');"))
        self.assertTrue(sql.endswith("COMMIT;\n"))
        self.assertIn("\\set ON_ERROR_STOP on\nBEGIN;", sql)
        self.assertEqual(sql.count(marker_insert), 1)

    def test_project_and_workers_are_verified(self):
        info = {"Id": "123", "State": {"Running": True}, "Config": {"Labels": {
            "com.docker.compose.project": "expected", "com.docker.compose.service": "postgres"
        }}}
        with patch.object(database, "docker", side_effect=[json.dumps([info]), "123\n"]):
            self.assertEqual(database.verify_container("123", "expected"), "123")
        with patch.object(database, "docker", side_effect=[json.dumps([info]), "123\n456\n"]):
            with self.assertRaises(RuntimeError):
                database.verify_container("123", "expected")
        with patch.object(database, "docker", return_value=json.dumps([info])):
            with self.assertRaises(RuntimeError):
                database.verify_container("123", "different")
        info["Config"]["Labels"]["com.docker.compose.service"] = "worker"
        with patch.object(database, "docker", return_value=json.dumps([info])):
            with self.assertRaises(RuntimeError):
                database.verify_container("123", "expected")

    def test_acknowledgements_checked_before_docker(self):
        with patch.object(database, "docker") as command, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                database.main(["--container", "123", "--project", "p", "--apply", "--backup-confirmed"])
            command.assert_not_called()

    def test_dry_run_never_executes_psql(self):
        with patch.object(database, "verify_container", return_value="123"), \
                patch.object(database, "docker") as command, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(database.main(["--container", "123", "--project", "p"]), 0)
            command.assert_not_called()

    def test_failure_does_not_disclose_psql_output(self):
        import subprocess
        result = subprocess.CompletedProcess([], 1, "private row contents", "private query")
        with patch.object(database.subprocess, "run", return_value=result):
            with self.assertRaises(RuntimeError) as error:
                database.docker("exec")
        self.assertNotIn("private row contents", str(error.exception))
        self.assertNotIn("private query", str(error.exception))


if __name__ == "__main__":
    unittest.main()
