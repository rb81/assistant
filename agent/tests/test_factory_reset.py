import argparse
import io
import itertools
import os
import re
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
        instance.values, instance.secret_values = {}, {}
        instance.use_sudo = False
        instance.compose.return_value = "task-agent\npostgres\ndownloader\n"
        return instance

    def test_backup_precedes_stopping_and_deletion_and_services_restart(self):
        from assistant_agent.setup_tools import run_factory_reset
        with tempfile.TemporaryDirectory() as root:
            instance = self.instance(root)
            instance.compose_interactive.return_value = 0
            run_factory_reset(instance, MagicMock(), ["memories"], True, True)
            steps = [arguments.args[0] for arguments in instance.compose_interactive.call_args_list]
            self.assertEqual(steps[0], ["build", "downloader"])
            self.assertEqual(steps[2][:4], ["exec", "-T", "postgres", "pg_dump"])
            self.assertIn("archive", steps[3])
            self.assertEqual(steps[4], ["stop", "downloader", "task-agent"])
            self.assertEqual(steps[5][-3:], ["apply", "--categories", "memories"])
            self.assertEqual(steps[6], ["start", "downloader", "task-agent"])
            backup = next((Path(root) / "backups").iterdir())
            self.assertEqual(oct(backup.stat().st_mode & 0o777), "0o700")
            self.assertEqual(oct((backup / "database.dump").stat().st_mode & 0o777), "0o600")
            self.assertIn('"memories"', (backup / "manifest.json").read_text())

    def test_failed_backup_deletes_nothing(self):
        from assistant_agent.setup import SetupError
        from assistant_agent.setup_tools import run_factory_reset
        with tempfile.TemporaryDirectory() as root:
            instance = self.instance(root)
            instance.compose_interactive.side_effect = lambda arguments, stdout=None: 1 if "pg_dump" in arguments else 0
            with self.assertRaisesRegex(SetupError, "Backing up"):
                run_factory_reset(instance, MagicMock(), ["emails", "jobs", "reminders"], True, True)
            steps = [arguments.args[0] for arguments in instance.compose_interactive.call_args_list]
            self.assertFalse(any(step[0] == "stop" or "apply" in step for step in steps))

    def test_failed_deletion_still_restarts_services(self):
        from assistant_agent.setup import SetupError
        from assistant_agent.setup_tools import run_factory_reset
        with tempfile.TemporaryDirectory() as root:
            instance = self.instance(root)
            instance.compose_interactive.side_effect = lambda arguments, stdout=None: 1 if "apply" in arguments else 0
            with self.assertRaisesRegex(SetupError, "Deleting"):
                run_factory_reset(instance, MagicMock(), ["caches"], False, False)
            self.assertEqual(instance.compose_interactive.call_args_list[-1], call(["start", "downloader", "task-agent"], stdout=None))
            self.assertIn("--discard-mailbox-position", instance.compose_interactive.call_args_list[-2].args[0])

    def test_menu_requires_typed_confirmation_before_docker(self):
        import questionary
        from assistant_agent.setup_tools import factory_reset_menu
        with tempfile.TemporaryDirectory() as root:
            instance = self.instance(root)
            responses = iter([["emails"], "Continue", True, True, "reset"])
            with patch("assistant_agent.setup_tools.run_factory_reset") as run, patch("assistant_agent.setup_tools.ensure_docker_access") as access, \
                    patch.object(questionary, "checkbox", wraps=questionary.checkbox):
                factory_reset_menu(instance, lambda question: next(responses), lambda label, default=False: next(responses), MagicMock())
            run.assert_not_called()
            access.assert_not_called()
            responses = iter([["emails"], "Continue", False, True, "RESET"])
            with patch("assistant_agent.setup_tools.run_factory_reset") as run, patch("assistant_agent.setup_tools.ensure_docker_access"):
                factory_reset_menu(instance, lambda question: next(responses), lambda label, default=False: next(responses), MagicMock())
            run.assert_called_once_with(instance, unittest.mock.ANY, ["jobs", "emails", "reminders"], False, True)


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
        self.assertIn("sudo docker compose run", console.print.call_args_list[0].args[0])
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
