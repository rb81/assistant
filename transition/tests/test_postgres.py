"""Opt in with TRANSITION_TEST_POSTGRES_IMAGE=postgres:16-alpine.

Creates and removes only its own uniquely named disposable container/volumes.
Requires the archival checkout (fixtures are deliberately not shipped to users).
"""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
import uuid

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import migrate_database as database


@unittest.skipUnless(os.environ.get("TRANSITION_TEST_POSTGRES_IMAGE"), "PostgreSQL integration is opt-in")
class PostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = "assistant-transition-test-" + uuid.uuid4().hex[:12]
        cls.container = database.docker(
            "run", "-d", "--name", cls.project,
            "--label", f"com.docker.compose.project={cls.project}",
            "--label", "com.docker.compose.service=postgres",
            "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
            os.environ["TRANSITION_TEST_POSTGRES_IMAGE"],
        ).strip()
        cls.addClassCleanup(database.docker, "rm", "-f", "-v", cls.container)
        for _ in range(60):
            try:
                database.docker("exec", cls.container, "pg_isready", "-U", "postgres")
                return
            except RuntimeError:
                time.sleep(1)
        raise RuntimeError("Disposable PostgreSQL failed readiness")

    def psql(self, name, sql):
        return database.docker("exec", "-i", self.container, "psql", "-X", "-v",
                               "ON_ERROR_STOP=1", "-At", "-U", "postgres", "-d", name, input=sql)

    def create_database(self, name, v1=False, source_ref="v1-retired"):
        self.psql("postgres", f"CREATE DATABASE {name};")
        if v1:
            baseline = subprocess.run(
                ["git", "show", f"{source_ref}:database/schema.sql"],
                cwd=HERE.parent, check=True, capture_output=True, text=True,
            ).stdout
        else:
            baseline = (HERE.parent / "database/schema.sql").read_text()
        self.psql(name, baseline)

    def migrate(self, name):
        with contextlib.redirect_stdout(io.StringIO()):
            return database.main([
                "--container", self.container, "--project", self.project,
                "--database", name, "--user", "postgres", "--apply",
                "--backup-confirmed", "--accept-destructive-v1-retirement",
            ])

    def test_v1_known_tag_migrates_history_and_is_idempotent(self):
        self.create_database("legacy_v1", v1=True)
        self.psql("legacy_v1", "INSERT INTO agent_notes(title, content) VALUES ('Legacy', 'kept note');")
        self.assertEqual(self.migrate("legacy_v1"), 0)
        self.assertEqual(self.psql("legacy_v1", "SELECT count(*) FROM schema_migrations;").strip(), "12")
        self.assertEqual(self.psql("legacy_v1", "SELECT content FROM agent_memories "
                                              "WHERE metadata ? 'legacy_note_id';").strip(), "kept note")
        self.assertEqual(self.psql("legacy_v1", "SELECT to_regclass('agent_notes') IS NULL;").strip(), "t")
        self.assertEqual(self.migrate("legacy_v1"), 0)
        self.assertEqual(self.psql("legacy_v1", "SELECT count(*) FROM schema_migrations;").strip(), "12")

    def test_v1_release_tag_resolves_to_supported_commit_and_migrates(self):
        expected = "8f934a875daa77a7fe487ca1de3c086ac19f81db"
        for ref in ("v1-retired", "v1.0.0"):
            commit = subprocess.run(
                ["git", "rev-parse", f"{ref}^{{commit}}"],
                cwd=HERE.parent, check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(commit, expected)
        self.create_database("release_v1", v1=True, source_ref="v1.0.0")
        self.assertEqual(self.migrate("release_v1"), 0)
        self.assertEqual(self.psql("release_v1", "SELECT count(*) FROM schema_migrations;").strip(), "12")

    def test_archived_v2_migration_records_preserved(self):
        self.create_database("archived_v2")
        chunks = [
            "BEGIN;",
            "CREATE TABLE schema_migrations(filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());",
            (HERE / "sql/feature_schema.sql").read_text(),
        ]
        for migration in sorted((HERE / "sql").glob("[0-9][0-9][0-9]_*.sql")):
            chunks.extend([migration.read_text(),
                           f"INSERT INTO schema_migrations(filename) VALUES ('{migration.name}');"])
        chunks.append("COMMIT;")
        self.psql("archived_v2", "\n".join(chunks))
        before = self.psql("archived_v2", "SELECT filename, applied_at FROM schema_migrations ORDER BY filename;")
        self.assertEqual(self.migrate("archived_v2"), 0)
        after = self.psql("archived_v2", "SELECT filename, applied_at FROM schema_migrations "
                                          "WHERE filename <> '000_current_baseline' ORDER BY filename;")
        self.assertEqual(before, after)

    def test_late_failure_rolls_back_schema_tracking_and_data(self):
        self.create_database("rollback_v1", v1=True)
        self.psql("rollback_v1", "INSERT INTO agent_notes(title, content) VALUES ('Legacy', 'still here');")
        scratch = HERE / (".test-work-" + uuid.uuid4().hex)
        shutil.copytree(HERE / "sql", scratch)
        try:
            final = scratch / "011_research_system_threads.sql"
            with final.open("a") as stream:
                stream.write("\nSELECT 1 / 0;\n")
            with self.assertRaises(RuntimeError):
                self.psql("rollback_v1", database.build_sql(scratch))
        finally:
            shutil.rmtree(scratch)
        self.assertEqual(self.psql("rollback_v1", "SELECT to_regclass('schema_migrations') IS NULL;").strip(), "t")
        self.assertEqual(self.psql("rollback_v1", "SELECT content FROM agent_notes;").strip(), "still here")
        self.assertEqual(self.psql("rollback_v1", "SELECT to_regclass('zoom_managed_meetings') IS NULL;").strip(), "t")

    def test_concurrent_transitions_serialize_and_record_once(self):
        self.create_database("concurrent_v1", v1=True)
        sql = database.build_sql()
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: self.psql("concurrent_v1", sql), range(2)))
        self.assertEqual(len(results), 2)
        self.assertEqual(self.psql("concurrent_v1", "SELECT count(*) FROM schema_migrations;").strip(), "12")

    def test_partially_migrated_database_skips_existing_filenames(self):
        self.create_database("partial_v1", v1=True)
        chunks = [
            "CREATE TABLE schema_migrations(filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());",
            (HERE / "sql/feature_schema.sql").read_text(),
        ]
        for migration in sorted((HERE / "sql").glob("[0-9][0-9][0-9]_*.sql"))[:4]:
            chunks.extend([migration.read_text(),
                           f"INSERT INTO schema_migrations(filename) VALUES ('{migration.name}');"])
        self.psql("partial_v1", "\n".join(chunks))
        before = self.psql("partial_v1", "SELECT filename, applied_at FROM schema_migrations ORDER BY filename;")
        self.assertEqual(self.migrate("partial_v1"), 0)
        self.assertEqual(self.psql("partial_v1", "SELECT count(*) FROM schema_migrations;").strip(), "12")
        after = self.psql("partial_v1", "SELECT filename, applied_at FROM schema_migrations "
                                          "WHERE filename >= '001' AND filename < '005' ORDER BY filename;")
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
