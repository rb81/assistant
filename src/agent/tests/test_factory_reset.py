import argparse
import io
import itertools
import os
import re
import subprocess
import tarfile
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from assistant_agent.config import AppConfig
from assistant_agent.factory_reset import CATEGORIES, Layout, all_targets, archive, delete_files, expand

REPOSITORY = Path(__file__).resolve().parents[2]
STATE_TABLES = {"runtime_state", "schema_migrations"}


def defined_tables():
    sources = [REPOSITORY / "database/schema.sql", *sorted((REPOSITORY / "agent/src/assistant_agent/migrations").glob("*.sql")),
               REPOSITORY / "agent/src/assistant_agent/database.py"]
    return {name for path in sources for name in re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?([a-z_]+)", path.read_text())}


class CategoryGraphTest(unittest.TestCase):
    def test_every_selection_expands_to_a_dependency_closed_set(self):
        keys = list(CATEGORIES)
        for size in range(1, len(keys) + 1):
            for selection in itertools.combinations(keys, size):
                categories, added = expand(selection)
                self.assertEqual(set(categories), set(selection) | set(added))
                for key in categories:
                    self.assertLessEqual(set(CATEGORIES[key].requires), set(categories), key)
                self.assertEqual(categories, [key for key in keys if key in categories])

    def test_required_categories_explain_why_they_were_added(self):
        categories, added = expand(["emails"])
        self.assertEqual(categories, ["jobs", "emails", "reminders"])
        self.assertIn("jobs reply", added["jobs"])
        self.assertIn("queued reminder", added["reminders"])
        self.assertEqual(expand(["workspace_files"])[0], ["workspace_index", "caches", "workspace_files"])
        self.assertEqual(expand(["memories"]), (["memories"], {}))
        with self.assertRaises(ValueError):
            expand(["settings"])

    def test_every_database_table_is_classified(self):
        listed = {table for category in CATEGORIES.values() for table in category.tables}
        tables = defined_tables()
        self.assertEqual(tables - listed - STATE_TABLES, set(), "new tables need a factory reset category or an explicit keep rule")
        self.assertEqual(listed - tables, set(), "factory reset lists a table that no schema defines")


class FileResetTest(unittest.TestCase):
    def build(self, root):
        share, raw, outside = root / "share", root / "artifacts", root / "outside"
        for path in ("processed/email/a", ".assistant/docs", ".assistant/cache/tool-results", ".assistant/archive/source-documents", ".trash", "projects"):
            (share / path).mkdir(parents=True)
        raw.mkdir()
        outside.mkdir()
        (outside / "precious.txt").write_text("outside")
        for path in ("processed/email/a/x.md", ".assistant/docs/TOOLS.md", ".assistant/cache/tool-results/r.json",
                     ".assistant/archive/source-documents/d.pdf", ".trash/old.txt", "projects/plan.md", "notes.txt"):
            (share / path).write_text(path)
        (raw / "attachment.bin").write_text("raw")
        (share / "escape").symlink_to(outside, target_is_directory=True)
        (share / ".assistant/cache/tool-results/link").symlink_to(outside / "precious.txt")
        config = AppConfig({"agent": {"filesystem": {"shared_root": str(share), "trash_directory": str(share / ".trash")},
                                      "artifacts": {"raw_root": str(raw)}}})
        return Layout.from_config(config), share, raw, outside

    def test_workspace_reset_keeps_docs_and_attachments_and_never_follows_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            layout, share, raw, outside = self.build(Path(temporary))
            categories, _ = expand(["workspace_files"])
            planned = all_targets(layout, categories)
            buffer = io.BytesIO()
            archive(layout, categories, buffer)
            buffer.seek(0)
            with tarfile.open(fileobj=buffer, mode="r:gz") as bundle:
                archived = bundle.getnames()
            self.assertIn(str(share / "notes.txt").lstrip("/"), archived)
            self.assertFalse(any("precious.txt" in name and "link" not in name and "escape" not in name for name in archived))
            removed, errors = delete_files(layout, categories)
            self.assertEqual((removed, errors), (len(planned), []))
            self.assertEqual(sorted(path.name for path in share.iterdir()), [".assistant", "processed"])
            self.assertEqual([path.name for path in (share / ".assistant").iterdir()], ["docs"])
            self.assertTrue((share / ".assistant/docs/TOOLS.md").is_file())
            self.assertTrue((share / "processed/email/a/x.md").is_file())
            self.assertTrue((raw / "attachment.bin").is_file())
            self.assertEqual((outside / "precious.txt").read_text(), "outside")

    def test_email_reset_clears_attachments_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            layout, share, raw, _ = self.build(Path(temporary))
            delete_files(layout, expand(["emails"])[0])
            self.assertEqual(list(raw.iterdir()), [])
            self.assertEqual(list((share / "processed").iterdir()), [])
            self.assertTrue((share / "notes.txt").is_file())
            self.assertTrue((share / ".trash/old.txt").is_file())

    def test_layout_rejects_paths_outside_the_workspace(self):
        config = AppConfig({"agent": {"filesystem": {"shared_root": "/data/share", "trash_directory": "/etc"}}})
        with self.assertRaisesRegex(ValueError, "trash"):
            Layout.from_config(config)
        config = AppConfig({"agent": {"tool_result_cache": {"root": "../../etc"}}})
        with self.assertRaisesRegex(ValueError, "tool_cache"):
            Layout.from_config(config)

    def test_cli_refuses_incomplete_selection(self):
        from assistant_agent import factory_reset
        with patch("sys.argv", ["factory_reset", "apply", "--categories", "emails"]):
            with self.assertRaisesRegex(SystemExit, "must include: jobs, reminders"):
                factory_reset.main()


@unittest.skipUnless(os.getenv("ASSISTANT_TEST_DATABASE_URL"), "requires disposable assistant_test database")
class DatabaseResetTest(unittest.TestCase):
    def setUp(self):
        from psycopg.conninfo import make_conninfo

        from assistant_agent.database import Database

        self.original = Database(os.environ["ASSISTANT_TEST_DATABASE_URL"])
        self.schema = "test_" + uuid.uuid4().hex
        with self.original.connect() as conn:
            if conn.info.dbname != "assistant_test":
                raise RuntimeError("integration tests require a disposable database named assistant_test")
            conn.execute("CREATE SCHEMA " + self.schema)
        self.url = make_conninfo(os.environ["ASSISTANT_TEST_DATABASE_URL"], options="-c search_path=" + self.schema)
        self.db = Database(self.url)
        self.db.execute((REPOSITORY / "database/schema.sql").read_text())
        self.db.ensure_feature_schema()
        self.db.run_migrations()

    def tearDown(self):
        self.original.execute("DROP SCHEMA " + self.schema + " CASCADE")

    def seed(self):
        email = self.db.fetch_one("INSERT INTO emails(message_id, thread_id, from_address, subject, received_at) VALUES ('m1', 't1', 'a@example.com', 's', now()) RETURNING id")
        job = self.db.fetch_one("INSERT INTO jobs(thread_id, task_summary, trigger_email_id) VALUES ('t1', 'job', %s) RETURNING id", (email["id"],))
        self.db.log_event(job["id"], "status_change", output_data={"status": "queued"})
        self.db.execute("INSERT INTO agent_memories(content, source_job_id) VALUES ('remember', %s)", (job["id"],))
        self.db.execute("INSERT INTO reminders(title, task, run_at, job_id, status) VALUES ('r', 't', now(), %s, 'queued')", (job["id"],))
        self.db.execute("INSERT INTO contacts(first_name) VALUES ('Kept')")
        self.db.execute("INSERT INTO outbound_email_logs(job_id, to_addresses, subject, body_text, status) VALUES (%s, ARRAY['b@example.com'], 's', 'b', 'sent')", (job["id"],))
        for key, value in (("imap:host:INBOX:last_uid", "41"), ("calendar_token_zoho", '{"access_token": "x"}')):
            self.db.execute("INSERT INTO runtime_state(key, value) VALUES (%s, %s::jsonb)", (key, value))
        return job

    def reset(self, selection, keep=True):
        import psycopg

        from assistant_agent.factory_reset import reset_database
        with psycopg.connect(self.url) as connection:
            return reset_database(connection, expand(selection)[0], keep_mailbox_position=keep)

    def count(self, table):
        return self.db.fetch_one("SELECT count(*) AS count FROM " + table)["count"]

    def test_jobs_reset_keeps_memories_and_nulls_their_job_reference(self):
        self.seed()
        counts = self.reset(["jobs"])
        self.assertEqual(counts["jobs"], 1)
        self.assertEqual((self.count("jobs"), self.count("task_logs"), self.count("reminders")), (0, 0, 0))
        self.assertEqual((self.count("emails"), self.count("agent_memories"), self.count("contacts"), self.count("outbound_email_logs")), (1, 1, 1, 1))
        self.assertIsNone(self.db.fetch_one("SELECT source_job_id FROM agent_memories")["source_job_id"])
        self.assertGreater(self.count("schema_migrations"), 0)

    def test_full_reset_keeps_mailbox_position_and_migrations_unless_discarded(self):
        self.seed()
        self.reset(list(CATEGORIES))
        for category in CATEGORIES.values():
            for table in category.tables:
                if self.db.fetch_one("SELECT to_regclass(%s) AS name", (table,))["name"]:
                    self.assertEqual(self.count(table), 0, table)
        self.assertEqual([row["key"] for row in self.db.fetch_all("SELECT key FROM runtime_state ORDER BY key")], ["imap:host:INBOX:last_uid", "memory_revision"])
        self.assertGreater(self.count("schema_migrations"), 0)
        self.reset(["runtime_state"], keep=False)
        self.assertEqual(self.count("runtime_state"), 0)

    def test_memory_reset_publishes_a_new_memory_revision(self):
        self.seed()
        self.reset(["memories"])
        self.assertEqual(self.count("agent_memories"), 0)
        self.assertEqual(self.db.get_state("memory_revision")["reason"], "factory reset")
        self.assertEqual(self.count("jobs"), 1)


class ResetOrchestrationTest(unittest.TestCase):
    def instance(self, root):
        instance = MagicMock()
        instance.root = Path(root)
        instance.data = instance.root / "data"
        instance.values, instance.secret_values = {}, {}
        instance.defaults = AppConfig({})
        instance.use_sudo = False
        instance.compose.return_value = "task-agent\npostgres\ndownloader\n"
        return instance

    def test_full_reset_rebuilds_postgres_and_starts_workspace_indexer(self):
        from assistant_agent.setup_tools import run_factory_reset
        with tempfile.TemporaryDirectory() as root:
            instance = self.instance(root)
            instance.compose_interactive.return_value = 0
            run_factory_reset(instance, MagicMock())
            steps = [arguments.args[0] for arguments in instance.compose_interactive.call_args_list]
            self.assertEqual(steps[0], ["stop", "downloader", "postgres", "task-agent"])
            self.assertEqual(steps[1][-3:], ["postgres", "-rf", "/var/lib/postgresql/data/pgdata"])
            self.assertIn("--user", steps[1])
            self.assertEqual(steps[2], ["up", "-d", "--wait", "--wait-timeout", "120", "postgres"])
            self.assertEqual(steps[3], ["up", "-d", "--wait", "--wait-timeout", "180", "downloader", "task-agent", "workspace-indexer"])
            self.assertFalse((Path(root) / "data/backups").exists())

    def test_reset_failure_restores_previously_running_services(self):
        from assistant_agent.setup import SetupError
        from assistant_agent.setup_tools import run_factory_reset
        with tempfile.TemporaryDirectory() as root:
            instance = self.instance(root)
            instance.compose.side_effect = ["downloader\npostgres\ntask-agent\n", ""]
            instance.compose_interactive.side_effect = lambda arguments, stdout=None: 1 if "--entrypoint" in arguments else 0
            with self.assertRaisesRegex(SetupError, "Destroying the PostgreSQL"):
                run_factory_reset(instance, MagicMock())
            steps = [arguments.args[0] for arguments in instance.compose_interactive.call_args_list]
            self.assertEqual(steps[-1], ["up", "-d", "downloader", "postgres", "task-agent"])

    def test_failed_deletion_still_restarts_services(self):
        from assistant_agent.setup import SetupError
        from assistant_agent.setup_tools import run_factory_reset
        with tempfile.TemporaryDirectory() as root:
            instance = self.instance(root)
            instance.compose.side_effect = ["downloader\npostgres\ntask-agent\n", ""]
            instance.compose_interactive.side_effect = lambda arguments, stdout=None: 1 if arguments[-1] == "postgres" and arguments[0] == "up" else 0
            with self.assertRaisesRegex(SetupError, "Rebuilding PostgreSQL"):
                run_factory_reset(instance, MagicMock())
            self.assertEqual(instance.compose_interactive.call_args_list[-1], call(["up", "-d", "downloader", "postgres", "task-agent"], stdout=None))

    def test_menu_requires_typed_confirmation_and_refuses_custom_database(self):
        import questionary
        from assistant_agent.setup_tools import factory_reset_menu
        with tempfile.TemporaryDirectory() as root:
            instance = self.instance(root)
            responses = iter([False, "reset"])
            with patch("assistant_agent.setup_tools.run_factory_reset") as run, patch("assistant_agent.setup_tools.ensure_docker_access") as access, \
                    patch.object(questionary, "text", wraps=questionary.text):
                factory_reset_menu(instance, lambda question: next(responses), lambda label, default=False: next(responses), MagicMock())
            run.assert_not_called()
            access.assert_not_called()
            responses = iter([False, "RESET"])
            with patch("assistant_agent.setup_tools.run_factory_reset") as run, patch("assistant_agent.setup_tools.ensure_docker_access"):
                factory_reset_menu(instance, lambda question: next(responses), lambda label, default=False: next(responses), MagicMock())
            run.assert_called_once_with(instance, unittest.mock.ANY, clean_install=False)
            responses = iter([True, "RESET"])
            with patch("assistant_agent.setup_tools.run_factory_reset") as run, patch("assistant_agent.setup_tools.ensure_docker_access"):
                factory_reset_menu(instance, lambda question: next(responses), lambda label, default=False: next(responses), MagicMock())
            run.assert_called_once_with(instance, unittest.mock.ANY, clean_install=True)
            instance.secret_values["DATABASE_URL"] = "postgresql://custom"
            console = MagicMock()
            with patch("assistant_agent.setup_tools.run_factory_reset") as run, patch("assistant_agent.setup_tools.ensure_docker_access") as access:
                factory_reset_menu(instance, lambda question: self.fail("custom database must be refused"), lambda label, default=False: False, console)
            run.assert_not_called()
            access.assert_not_called()
            self.assertIn("custom database", str(console.print.call_args).lower())

    def test_clean_install_deletes_files_without_following_symlinks_or_deleting_configuration(self):
        from assistant_agent.setup_tools import run_factory_reset
        for clean_install in (False, True):
            with self.subTest(clean_install=clean_install), tempfile.TemporaryDirectory() as root:
                instance = self.instance(root)
                instance.compose.return_value = "postgres\ntask-agent\n"
                files = (
                    "share/projects/result.txt", "share/.assistant/cache/result.json",
                    "share/.trash/old.txt", "private/artifacts/email/attachment.pdf",
                    "private/artifacts/dropbox/staged.txt",
                )
                preserved = (
                    "config/AGENT.md", "config/agent.yaml", ".env", "overrides.env",
                    ".secrets/POSTGRES_PASSWORD", ".setup-state.json", ".compose-secrets.yaml",
                    "private/tls/postgres.key", "ollama/models/model", "backups/snapshot",
                )
                for name in (*files, *preserved):
                    path = instance.data / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(name)
                (instance.data / "share/escape").symlink_to(instance.data / "config", target_is_directory=True)
                (instance.data / "private/artifacts/escape").symlink_to(instance.data / ".secrets", target_is_directory=True)

                def execute(arguments, stdout=None):
                    if "--entrypoint" in arguments and arguments[arguments.index("--entrypoint") + 1] == "find":
                        mounts = dict(arguments[index + 1].rsplit(":", 1)[::-1]
                                      for index, value in enumerate(arguments) if value == "--volume")
                        command = ["find", *[mounts.get(value, value) for value in arguments[arguments.index("postgres") + 1:]]]
                        return subprocess.run(command, check=False).returncode
                    return 0

                instance.compose_interactive.side_effect = execute
                report = MagicMock()
                run_factory_reset(instance, report, clean_install=clean_install)
                for name in files:
                    self.assertEqual((instance.data / name).exists(), not clean_install)
                for name in preserved:
                    self.assertEqual((instance.data / name).read_text(), name)
                for name in ("share", "private/artifacts"):
                    self.assertTrue((instance.data / name).is_dir())
                    self.assertEqual((instance.data / name / "escape").is_symlink(), not clean_install)
                    if clean_install:
                        self.assertEqual(list((instance.data / name).iterdir()), [])
                if clean_install:
                    self.assertIn("Clean install complete", report.print.call_args.args[0])

    def test_clean_install_refuses_unsafe_roots_before_stopping_services(self):
        from assistant_agent.setup import SetupError
        from assistant_agent.setup_tools import run_factory_reset
        for name in ("share", "private", "private/artifacts"):
            for symlink in (False, True):
                with self.subTest(name=name, symlink=symlink), tempfile.TemporaryDirectory() as root:
                    instance = self.instance(root)
                    path = instance.data / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if symlink:
                        path.symlink_to(instance.root / "outside", target_is_directory=True)
                    else:
                        path.write_text("not a directory")
                    with self.assertRaisesRegex(SetupError, "Refusing clean install"):
                        run_factory_reset(instance, MagicMock(), clean_install=True)
                    instance.compose.assert_not_called()
                    instance.compose_interactive.assert_not_called()

    def test_failed_file_cleanup_is_not_reported_as_success(self):
        from assistant_agent.setup import SetupError
        from assistant_agent.setup_tools import run_factory_reset
        with tempfile.TemporaryDirectory() as root:
            instance = self.instance(root)
            instance.compose_interactive.side_effect = lambda arguments, stdout=None: int("find" in arguments)
            report = MagicMock()
            with self.assertRaisesRegex(SetupError, "Deleting all shared files"):
                run_factory_reset(instance, report, clean_install=True)
            self.assertFalse(any("complete." in str(item) for item in report.print.call_args_list))


class ToolsMenuTest(unittest.TestCase):
    def test_service_failures_do_not_show_database_tool_guidance(self):
        from assistant_agent.setup_tools import ADMIN, Tools
        instance = MagicMock(use_sudo=False)
        instance.compose_interactive.return_value = 1
        console = MagicMock()
        tools = Tools(instance, None, lambda *args: True, console)
        tools.run(["restart"])
        self.assertNotIn("PostgreSQL must", str(console.print.call_args_list))
        console.reset_mock()
        tools.run([*ADMIN, "/admin status"])
        self.assertIn("PostgreSQL must", str(console.print.call_args_list))

    def test_commands_are_shown_and_changes_need_confirmation(self):
        from assistant_agent.setup_tools import ADMIN, Tools
        instance = MagicMock(use_sudo=True)
        instance.compose_interactive.return_value = 0
        console = MagicMock()
        confirmations = iter([False, True])
        tools = Tools(instance, lambda question: None, lambda label, default=False: next(confirmations), console)
        tools.admin("/admin requeue 4", mutating=True)
        instance.compose_interactive.assert_not_called()
        self.assertIn("sudo docker compose --env-file data/.env run", console.print.call_args_list[0].args[0])
        tools.admin("/admin instruct 4", body="Use v2", mutating=True)
        instance.compose_interactive.assert_called_once_with([*ADMIN, "/admin instruct 4", "--body", "Use v2", "--yes"])
        tools.admin("/admin status")
        self.assertEqual(instance.compose_interactive.call_args.args[0], [*ADMIN, "/admin status"])

    def test_setup_cli_exposes_tools_and_reset_only_after_setup(self):
        from assistant_agent.setup import SetupError, interactive
        with tempfile.TemporaryDirectory() as root:
            for command in ("tools", "reset"):
                with self.subTest(command=command), patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"):
                    with self.assertRaisesRegex(SetupError, "Complete and save setup"):
                        interactive(argparse.Namespace(root=Path(root), command=command))


if __name__ == "__main__":
    unittest.main()
