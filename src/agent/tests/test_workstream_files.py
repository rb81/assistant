import hashlib
import os
import shutil
import sys
import types
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda handle: {}))
psycopg_module = types.ModuleType("psycopg")
psycopg_module.connect = lambda *args, **kwargs: None
psycopg_module.Connection = object
sys.modules.setdefault("psycopg", psycopg_module)
rows_module = types.ModuleType("psycopg.rows")
rows_module.dict_row = object()
sys.modules.setdefault("psycopg.rows", rows_module)
json_module = types.ModuleType("psycopg.types.json")
json_module.Jsonb = lambda value: value
sys.modules.setdefault("psycopg.types", types.ModuleType("psycopg.types"))
sys.modules.setdefault("psycopg.types.json", json_module)
markdown_module = types.ModuleType("markdown_it")
markdown_module.MarkdownIt = object
sys.modules.setdefault("markdown_it", markdown_module)

from assistant_agent.config import AppConfig
from assistant_agent.database import Database
from assistant_agent.file_conversion import FileConversionResult
from assistant_agent.tools import SandboxAttemptsExhausted, ToolRuntime
from assistant_agent.workstream_files import WorkstreamFileTracker
from assistant_agent.workspace_index import WorkspaceNormalizer

try:
    from test_workstreams import job, remove, store  # noqa: F401
except ImportError:
    store = None


class RecordingDatabase(Database):
    def __init__(self):
        super().__init__("unused")
        self.statements = []
        self.scoped_as = []
        self.source_job_id = 71
        self.active_job_workstream_id = 31
        self.active_owners = [{"workstream_id": 31}, {"workstream_id": 42}]

    def execute(self, sql, params=()):
        self.statements.append((" ".join(sql.split()), params))

    def for_job(self, job_id):
        self.scoped_as.append(("job", job_id))
        return self

    def for_workstream(self, workstream_id):
        self.scoped_as.append(("workstream", workstream_id))
        return self

    def fetch_one(self, sql, params=()):
        if "FROM workstream_files AS file" in sql:
            return {
                "source_job_id": self.source_job_id,
                "active_job_workstream_id": self.active_job_workstream_id,
            }
        return None

    def fetch_all(self, sql, params=()):
        if "FROM workstream_files AS file" in sql:
            return list(self.active_owners)
        return []


class WorkstreamFileTrackerTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / (".workstream-files-test-%s" % os.getpid())
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir()
        self.db = RecordingDatabase()
        self.config = AppConfig({"agent": {"filesystem": {"shared_root": str(self.root)}}})
        self.tracker = WorkstreamFileTracker(self.db, self.config, 19)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        outside = self.root.parent / (self.root.name + "-outside")
        outside.unlink(missing_ok=True)

    def test_registers_provisional_and_final_digest(self):
        path = self.root / "created.txt"
        self.tracker.register_path(path)
        path.write_text("final bytes", encoding="utf-8")
        self.tracker.track_file(path)

        self.assertEqual(len(self.db.statements), 2)
        self.assertIn("ON CONFLICT (path) DO UPDATE", self.db.statements[0][0])
        self.assertEqual(self.db.statements[0][1], (str(path), None, 19))
        self.assertEqual(self.db.statements[1][1][0], str(path))
        self.assertEqual(
            self.db.statements[1][1][1],
            hashlib.sha256(b"final bytes").hexdigest(),
        )

    def test_rejects_root_credentials_protected_docs_and_symlinks(self):
        (self.root / "secret.pem").write_text("private", encoding="utf-8")
        (self.root / ".assistant" / "docs").mkdir(parents=True)
        (self.root / ".assistant" / "docs" / "guide.md").write_text("docs", encoding="utf-8")
        outside = self.root.parent / (self.root.name + "-outside")
        outside.write_text("outside", encoding="utf-8")
        (self.root / "link").symlink_to(outside)

        for path in (
            self.root,
            self.root / "secret.pem",
            self.root / ".assistant" / "docs" / "guide.md",
            self.root / "link",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.tracker.register_path(path)

    def test_tracks_archive_and_configured_raw_artifact_files(self):
        archive_file = self.root / ".assistant" / "archive" / "converted" / "source.pdf"
        archive_file.parent.mkdir(parents=True)
        archive_file.write_bytes(b"archived source")
        raw_root = self.root.parent / (self.root.name + "-raw")
        raw_root.mkdir()
        raw_file = raw_root / "attachment.bin"
        raw_file.write_bytes(b"raw attachment")
        tracker = WorkstreamFileTracker(
            self.db,
            AppConfig(
                {
                    "agent": {
                        "filesystem": {"shared_root": str(self.root)},
                        "artifacts": {"raw_root": str(raw_root)},
                    }
                }
            ),
            19,
        )
        try:
            tracker.track_file(archive_file)
            tracker.track_file(raw_file)
        finally:
            shutil.rmtree(raw_root, ignore_errors=True)

        tracked = {params[0] for _, params in self.db.statements}
        self.assertIn(str(archive_file), tracked)
        self.assertIn(str(raw_file), tracked)

    def test_background_conversion_inherits_all_source_workstream_attachments(self):
        source = self.root / "source.pdf"
        source.write_bytes(b"source")
        self.db.active_owners = [{"workstream_id": 31}, {"workstream_id": 42}]
        normalizer = WorkspaceNormalizer(self.db, self.config)

        with patch("assistant_agent.document_text.DocumentTextExtractor.convert_to_markdown", return_value="# converted"):
            result = normalizer.convert_to_markdown(source, source="background")

        self.assertTrue(result.converted)
        self.assertEqual(self.db.scoped_as, [("job", 71), ("job", 71)])
        inserts = [
            (statement, params)
            for statement, params in self.db.statements
            if "INSERT INTO workstream_files" in statement
        ]
        self.assertEqual(len(inserts), 2)
        self.assertEqual(inserts[0][1][2], 71)
        self.assertIsNone(inserts[0][1][1])
        self.assertEqual(inserts[1][1][1], hashlib.sha256(b"# converted").hexdigest())
        attachment_copies = [
            statement
            for statement, _ in self.db.statements
            if "INSERT INTO workstream_attachments" in statement
        ]
        self.assertEqual(len(attachment_copies), 2)
        self.assertTrue(all("owner.status = 'active'" in statement for statement in attachment_copies))

    def test_background_derivative_uses_surviving_owner_if_source_job_is_gone(self):
        source = self.root / "source.pdf"
        source.write_bytes(b"source")
        self.db.source_job_id = None
        self.db.active_job_workstream_id = None
        self.db.active_owners = [{"workstream_id": 42}, {"workstream_id": 56}]
        normalizer = WorkspaceNormalizer(self.db, self.config)

        with patch("assistant_agent.document_text.DocumentTextExtractor.convert_to_markdown", return_value="# converted"):
            normalizer.convert_to_markdown(source, source="background")

        self.assertEqual(self.db.scoped_as, [("workstream", 42), ("workstream", 42)])
        inserts = [
            params
            for statement, params in self.db.statements
            if "INSERT INTO workstream_files" in statement
        ]
        self.assertTrue(inserts)
        self.assertTrue(all(params[2] is None for params in inserts))
        attachment_copies = [
            params
            for statement, params in self.db.statements
            if "INSERT INTO workstream_attachments" in statement
        ]
        self.assertEqual(attachment_copies, [(str(source.with_suffix(".md")), str(source))] * 2)

    def test_reconcile_tracks_only_changed_new_and_unambiguously_moved_files(self):
        old = self.root / "old.txt"
        changed = self.root / "changed.txt"
        untouched = self.root / "untouched.txt"
        old.write_text("same bytes", encoding="utf-8")
        changed.write_text("old", encoding="utf-8")
        untouched.write_text("unchanged", encoding="utf-8")
        before = self.tracker.snapshot()
        old.rename(self.root / "renamed.txt")
        changed.write_text("new", encoding="utf-8")
        (self.root / "created.txt").write_text("new file", encoding="utf-8")
        after = self.tracker.snapshot()

        self.tracker.reconcile(before, after)

        sql = [statement for statement, _ in self.db.statements]
        self.assertEqual(sum("UPDATE workstream_files" in statement for statement in sql), 1)
        self.assertEqual(sum("INSERT INTO workstream_files" in statement for statement in sql), 3)
        self.assertTrue(all("untouched.txt" not in repr(params) for _, params in self.db.statements))
        self.assertEqual(self.db.statements[0][1][0], str(self.root / "renamed.txt"))

    def test_fake_databases_disable_provenance(self):
        class FakeDatabase:
            def execute(self, sql, params=()):
                raise AssertionError("isolated fake database must not receive provenance SQL")

        tracker = WorkstreamFileTracker(FakeDatabase(), self.config, 19)
        path = self.root / "fake.txt"
        path.write_text("bytes", encoding="utf-8")
        self.assertFalse(tracker.enabled)
        tracker.track_file(path)

    def test_command_tracks_new_and_changed_files_even_when_execution_fails(self):
        changed = self.root / "changed.txt"
        untouched = self.root / "untouched.txt"
        changed.write_text("before", encoding="utf-8")
        untouched.write_text("same", encoding="utf-8")
        config = AppConfig(
            {
                "agent": {
                    "filesystem": {"shared_root": str(self.root)},
                    "sandbox": {"base_url": "https://sandbox.example", "max_attempts": 1},
                }
            }
        )
        runtime = ToolRuntime(self.db, config, {"id": 19})

        def failed_command(*args, **kwargs):
            changed.write_text("after", encoding="utf-8")
            (self.root / "created.txt").write_text("new", encoding="utf-8")
            raise urllib.error.URLError("failed after modifying workspace")

        with patch("assistant_agent.tools.read_secret", return_value="token"), \
                patch.object(ToolRuntime, "sandbox_control", return_value={
                    "permission_epoch": "epoch", "execution_identity": "image",
                }), \
                patch("assistant_agent.tools.ssl.create_default_context"), \
                patch("assistant_agent.tools.open_https_request", side_effect=failed_command), \
                self.assertRaises(SandboxAttemptsExhausted):
            runtime.command_execute(["test"])

        tracked = {
            params[0]
            for statement, params in self.db.statements
            if "INSERT INTO workstream_files" in statement
        }
        self.assertIn(str(changed), tracked)
        self.assertIn(str(self.root / "created.txt"), tracked)
        self.assertNotIn(str(untouched), tracked)

    def test_file_conversion_tracks_generated_output(self):
        source = self.root / "source.txt"
        destination = self.root / "converted.md"
        source.write_text("source", encoding="utf-8")
        runtime = ToolRuntime(
            self.db,
            AppConfig(
                {
                    "agent": {
                        "filesystem": {"shared_root": str(self.root)},
                        "workspace": {"index": {"enabled": False}},
                    }
                }
            ),
            {"id": 19},
        )

        class FakeConversionService:
            def __init__(self, config, shared_root):
                pass

            def clean_output_format(self, value):
                return "markdown"

            def convert(self, source_path, output_format, output_path):
                output_path.write_bytes(b"converted bytes")
                return FileConversionResult(source_path, output_path, "markdown", "test", 15)

        with patch("assistant_agent.tools.FileConversionService", FakeConversionService):
            runtime.file_convert("source.txt", "markdown", "converted.md")

        registrations = [
            params
            for statement, params in self.db.statements
            if "INSERT INTO workstream_files" in statement
        ]
        self.assertEqual([item[0] for item in registrations], [str(destination), str(destination)])
        self.assertEqual(registrations[-1][1], hashlib.sha256(b"converted bytes").hexdigest())


if __name__ == "__main__":
    unittest.main()


def test_background_derivative_inherits_only_surviving_source_owner(store):
    a, b = job(store, "A"), job(store, "B")
    source = Path(store.config.get("agent.filesystem.shared_root")) / "source.pdf"
    source.write_bytes(b"source bytes")
    source_row = store.db.for_job(a["id"]).fetch_one(
        "INSERT INTO workstream_files(path, sha256, source_job_id) VALUES (%s, %s, %s) RETURNING *",
        (str(source), hashlib.sha256(source.read_bytes()).hexdigest(), a["id"]),
    )
    store.attach(b["workstream_id"], "file", source_row["id"])
    remove(store, a["workstream_id"])
    assert store.db.fetch_one("SELECT source_job_id FROM workstream_files WHERE id = %s", (source_row["id"],))[
        "source_job_id"
    ] is None

    with patch("assistant_agent.document_text.DocumentTextExtractor.convert_to_markdown", return_value="# converted"):
        result = WorkspaceNormalizer(store.db, store.config).convert_to_markdown(source, source="background")

    derivative = store.db.fetch_one(
        """
        SELECT file.*, object.origin_workstream_id
        FROM workstream_files AS file
        JOIN workstream_objects AS object ON object.id = file.object_id
        WHERE file.path = %s
        """,
        (str(result.path),),
    )
    owners = store.db.fetch_all(
        "SELECT workstream_id FROM workstream_attachments WHERE object_id = %s ORDER BY workstream_id",
        (derivative["object_id"],),
    )
    assert result.converted
    assert derivative["sha256"] == hashlib.sha256(result.path.read_bytes()).hexdigest()
    assert derivative["origin_workstream_id"] == b["workstream_id"]
    assert [row["workstream_id"] for row in owners] == [b["workstream_id"]]

    remove(store, b["workstream_id"])
    assert not source.exists()
    assert not result.path.exists()
