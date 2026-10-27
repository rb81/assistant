import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from psycopg.types.json import Jsonb

from assistant_agent.config import AppConfig
from assistant_agent.database import Database, MigrationBlockedError, V2_ACK_ENV
from assistant_agent.memory_store import MemoryStore
from assistant_agent.tool_result_cache import ToolResultCache


@unittest.skipUnless(os.getenv("ASSISTANT_TEST_DATABASE_URL"), "requires disposable assistant_test database")
class MemoryErasureDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import make_conninfo

        cls.original_db = Database(os.environ["ASSISTANT_TEST_DATABASE_URL"])
        cls.schema = "test_" + uuid.uuid4().hex
        with cls.original_db.connect() as conn:
            if conn.info.dbname != "assistant_test":
                raise RuntimeError("integration tests require a disposable database named assistant_test")
            conn.execute("CREATE SCHEMA " + cls.schema)
        cls.db = Database(make_conninfo(os.environ["ASSISTANT_TEST_DATABASE_URL"], options="-c search_path=" + cls.schema))
        cls.db.execute(Path(__file__).resolve().parents[2].joinpath("database/schema.sql").read_text())
        cls.db.ensure_feature_schema()

    @classmethod
    def tearDownClass(cls):
        cls.original_db.execute("DROP SCHEMA " + cls.schema + " CASCADE")

    def test_embedding_rebuild_preserves_sources_and_excludes_erased_or_expired_content(self):
        from assistant_agent.embedding_maintenance import EmbeddingMaintenance, SOURCES
        config = AppConfig({"agent": {"embeddings": {"enabled": True, "model": "migration-test", "dimensions": 2}}})
        service = EmbeddingMaintenance(self.db, config)
        memory = self.db.fetch_one("INSERT INTO agent_memories(content, metadata) VALUES ('migration memory', '{\"embedding_pending\": true, \"keep\": true}') RETURNING *")
        expired = self.db.fetch_one("INSERT INTO agent_memories(content, expires_at) VALUES ('expired', now() - interval '1 day') RETURNING id")
        file = self.db.fetch_one("INSERT INTO workspace_files(relative_path, index_status) VALUES ('migration.txt', 'indexed') RETURNING id")
        self.db.execute("INSERT INTO workspace_file_chunks(file_id, chunk_index, content) VALUES (%s, 0, 'migration chunk')", (file["id"],))
        deleted = self.db.fetch_one("INSERT INTO workspace_files(relative_path, index_status) VALUES ('deleted.txt', 'deleted') RETURNING id")
        self.db.execute("INSERT INTO workspace_file_chunks(file_id, chunk_index, content) VALUES (%s, 0, 'erased chunk')", (deleted["id"],))
        self.db.execute("INSERT INTO jobs(thread_id, task_summary) VALUES ('embedding-migration-job', 'migration job')")
        self.db.execute("INSERT INTO reminders(title, task, run_at) VALUES ('Migration reminder', 'migration task', now())")
        self.db.execute("INSERT INTO outbound_email_logs(to_addresses, subject, body_text, status) VALUES (ARRAY['migration@example.com'], 'migration outbound', 'body', 'sent')")
        self.db.execute("INSERT INTO emails(message_id, thread_id, from_address, subject, received_at) VALUES ('embedding-migration', 'migration-thread', 'migration@example.com', 'migration inbound', now())")
        self.db.execute("INSERT INTO contacts(first_name, notes) VALUES ('Migration', 'migration contact')")
        self.db.execute("CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$ BEGIN NEW.updated_at = now(); RETURN NEW; END; $$ LANGUAGE plpgsql")
        with patch.object(service.client, "embed", return_value=[1.0, 0.0]):
            result = service.reindex(batch_size=1)
            self.assertTrue(result["complete"])
            self.assertGreaterEqual(result["refreshed"], len(SOURCES))
            self.assertEqual(service.reindex(batch_size=1)["refreshed"], 0)
        current = self.db.fetch_one("SELECT * FROM agent_memories WHERE id = %s", (memory["id"],))
        self.assertEqual(current["content"], memory["content"])
        self.assertEqual(current["updated_at"], memory["updated_at"])
        self.assertEqual(current["metadata"], {"keep": True})
        changed = self.db.fetch_one("UPDATE agent_memories SET content = 'substantive update' WHERE id = %s RETURNING updated_at", (memory["id"],))
        self.assertGreater(changed["updated_at"], memory["updated_at"])
        self.assertIsNone(self.db.fetch_one("SELECT embedding FROM agent_memories WHERE id = %s", (expired["id"],))["embedding"])
        self.assertIsNone(self.db.fetch_one("SELECT embedding FROM workspace_file_chunks WHERE file_id = %s", (deleted["id"],))["embedding"])

    def test_embedding_rebuild_does_not_overwrite_concurrently_changed_or_deleted_records(self):
        from assistant_agent.embedding_maintenance import EmbeddingMaintenance
        service = EmbeddingMaintenance(self.db, AppConfig({"agent": {"embeddings": {"model": "concurrency-test", "dimensions": 2}}}))
        original = self.db.fetch_one("INSERT INTO agent_memories(content) VALUES ('concurrent original') RETURNING id")
        deleted = self.db.fetch_one("INSERT INTO agent_memories(content) VALUES ('concurrent delete') RETURNING id")

        def embed(text):
            if text == 'concurrent original':
                self.db.execute("UPDATE agent_memories SET content = 'concurrent newer' WHERE id = %s", (original["id"],))
            elif text == 'concurrent delete':
                self.db.execute("DELETE FROM agent_memories WHERE id = %s", (deleted["id"],))
            return [1.0, 0.0]

        with patch.object(service.client, "embed", side_effect=embed):
            result = service.reindex()
        self.assertFalse(result["complete"])
        self.assertGreaterEqual(result["conflicts"], 2)
        self.assertIsNone(self.db.fetch_one("SELECT embedding FROM agent_memories WHERE id = %s", (original["id"],))["embedding"])
        self.assertIsNone(self.db.fetch_one("SELECT id FROM agent_memories WHERE id = %s", (deleted["id"],)))

    def test_deletion_removes_historical_logs_checkpoints_and_cache(self):
        with tempfile.TemporaryDirectory() as root:
            config = AppConfig({"agent": {"embeddings": {"enabled": False}, "filesystem": {"shared_root": root}}})
            store = MemoryStore(self.db, config)
            original = "Unique erasure canary original knowledge"
            corrected = "Unique erasure canary corrected knowledge"
            memory = store.create(original)
            store.update(memory["id"], content=corrected)
            job = self.db.fetch_one("INSERT INTO jobs(thread_id) VALUES ('erasure-test') RETURNING id")
            self.db.log_event(job["id"], "tool_result", output_data={"content": original})
            self.db.log_event(job["id"], "tool_result", output_data={"content": "unrelated preserved history"})
            self.db.log_event(job["id"], "llm_response", output_data={"memory_ids": [memory["id"]], "content": "A paraphrase of the deleted knowledge"})
            self.db.execute("INSERT INTO agent_checkpoints(job_id, message_history, reason) VALUES (%s, %s, 'test')", (job["id"], Jsonb([{"role": "user", "content": corrected}])))
            cache = ToolResultCache(config)
            cached = cache.cache_result(job["id"], "command_execute", {"stdout": original})
            receipt = store.delete(memory["id"])
            self.assertTrue(receipt["deleted"])
            self.assertNotIn("content", receipt)
            self.assertIsNone(store.get(memory["id"], include_expired=True))
            self.assertFalse(Path(cached["cached_output_path"]).exists())
            for table in ("memory_events", "task_logs", "agent_checkpoints"):
                serialized = json.dumps(self.db.fetch_all("SELECT * FROM %s" % table), default=str)
                self.assertNotIn(original, serialized)
                self.assertNotIn(corrected, serialized)
                self.assertNotIn("A paraphrase of the deleted knowledge", serialized)
            self.assertIn("unrelated preserved history", json.dumps(self.db.fetch_all("SELECT output_data FROM task_logs")))
            self.assertEqual(len(self.db.fetch_all("SELECT * FROM memory_events WHERE memory_id = %s", (memory["id"],))), 1)

    def test_admin_memory_commands_are_model_free_and_rank_exact_matches(self):
        from assistant_agent.admin_commands import AdminCommands
        with tempfile.TemporaryDirectory() as root:
            config = AppConfig({"agent": {"admin": {"email": "admin@example.com"}, "embeddings": {"enabled": True}, "filesystem": {"shared_root": root}}})
            service = AdminCommands(self.db, config)
            with patch("assistant_agent.llm_client.LlmClient.chat") as chat, patch("assistant_agent.embedding_client.EmbeddingClient.embed") as embed:
                created = json.loads(service.execute("/admin memory create", "Meetings should be scheduled after lunch"))
                self.assertTrue(created["metadata"]["embedding_pending"])
                identifier = created["id"]
                updated = json.loads(service.execute("/admin memory update %s" % identifier, "Meetings should be scheduled after 2pm"))
                self.assertEqual(updated["content"], "Meetings should be scheduled after 2pm")
                rows = json.loads(service.execute('/admin memory search --query "scheduled after 2pm"'))
                self.assertEqual(rows[0]["id"], identifier)
                confirmation = service.execute("/admin memory delete %s" % identifier)
                receipt = json.loads(service.execute("/admin confirm " + confirmation.split()[-1]))
                self.assertTrue(receipt["deleted"])
                self.assertNotIn("content", receipt)
                chat.assert_not_called()
                embed.assert_not_called()

    def test_upgrade_migrates_notes_and_retires_projects_without_recreation(self):
        migrations = Path(__file__).resolve().parents[1] / "src/assistant_agent/migrations"
        with tempfile.TemporaryDirectory() as root:
            legacy = Path(root)
            for path in migrations.glob("00[1-4]*.sql"):
                (legacy / path.name).write_text(path.read_text())
            self.db.run_migrations(legacy)
        parent = self.db.fetch_one("INSERT INTO jobs(thread_id, status) VALUES ('retirement-test', 'waiting') RETURNING id")
        child = self.db.fetch_one("INSERT INTO jobs(thread_id, status) VALUES ('retirement-child', 'queued') RETURNING id")
        project = self.db.fetch_one("INSERT INTO projects(original_job_id, original_thread_id, title, status) VALUES (%s, 'retirement-test', 'Old project', 'running') RETURNING id", (parent["id"],))
        self.db.execute("INSERT INTO project_tasks(project_id, sequence, title, task, status, job_id) VALUES (%s, 1, 'Existing work', 'Do not duplicate', 'queued', %s)", (project["id"], child["id"]))
        self.db.execute("INSERT INTO agent_notes(title, content, status) VALUES ('Old note', 'Migrated working knowledge', 'active')")
        with patch.dict(os.environ, {V2_ACK_ENV: ""}):
            with self.assertRaises(MigrationBlockedError):
                self.db.run_migrations()
        self.assertIsNotNone(self.db.fetch_one("SELECT to_regclass('agent_notes') AS relation")["relation"])
        self.assertEqual(self.db.fetch_all("SELECT filename FROM schema_migrations WHERE filename LIKE '005%'"), [])
        with patch.dict(os.environ, {V2_ACK_ENV: "yes"}):
            self.assertIn("005_unified_memory_retire_projects.sql", self.db.run_migrations())
        self.assertEqual(self.db.fetch_one("SELECT status FROM jobs WHERE id = %s", (parent["id"],))["status"], "queued")
        self.assertEqual(self.db.fetch_one("SELECT status FROM jobs WHERE id = %s", (child["id"],))["status"], "queued")
        migrated = self.db.fetch_one("SELECT * FROM agent_memories WHERE content = 'Migrated working knowledge'")
        self.assertEqual(migrated["kind"], "working_context")
        self.assertTrue(migrated["metadata"]["embedding_pending"])
        self.assertEqual(self.db.run_migrations(migrations), [])
        self.db.ensure_feature_schema()
        for table in ("projects", "project_tasks", "agent_notes", "note_events", "entities", "entity_object_links"):
            self.assertIsNone(self.db.fetch_one("SELECT to_regclass(%s) AS relation", (table,))["relation"])

        unrelated = self.db.fetch_one("INSERT INTO jobs(thread_id, status) VALUES ('retirement-child', 'completed') RETURNING id")
        descendant = self.db.fetch_one("INSERT INTO jobs(thread_id, metadata) VALUES ('descendant', %s) RETURNING id", (Jsonb({"parent_job_id": child["id"]}),))
        meeting = self.db.fetch_one("INSERT INTO zoom_managed_meetings(request_key, request_payload, title, starts_at, duration_minutes, created_by_job_id) VALUES ('erasure-canary', '{}'::jsonb, 'Private meeting canary', now(), 30, %s) RETURNING id", (child["id"],))
        with self.assertRaisesRegex(ValueError, "Workstreams and data deletion"):
            self.db.erase_job(parent["id"])
        self.assertIsNotNone(self.db.fetch_one("SELECT id FROM jobs WHERE id = %s", (unrelated["id"],)))
        self.assertIsNotNone(self.db.fetch_one("SELECT id FROM jobs WHERE id = %s", (descendant["id"],)))
        self.assertIsNotNone(self.db.fetch_one("SELECT id FROM zoom_managed_meetings WHERE id = %s", (meeting["id"],)))

    def test_stale_unified_memories_expire_but_pinned_memories_survive(self):
        from assistant_agent.memory_manager import MemorySteward
        config = AppConfig({"agent": {"embeddings": {"enabled": False}}})
        store = MemoryStore(self.db, config)
        stale = store.create("Stale low-importance knowledge", importance=1, embed=False)
        pinned = store.create("Pinned low-importance knowledge", importance=1, pinned=True, embed=False)
        for memory in (stale, pinned):
            self.db.execute("UPDATE agent_memories SET created_at = now() - interval '100 days' WHERE id = %s", (memory["id"],))
        result = MemorySteward(self.db, config).reap_stale_memories()
        self.assertIn(stale["id"], result["reaped_ids"])
        self.assertNotIn(pinned["id"], result["reaped_ids"])
        self.assertIsNone(store.get(stale["id"]))
        self.assertIsNotNone(store.get(pinned["id"]))

    def test_zoom_create_retry_update_delete_against_database(self):
        from assistant_agent.zoom_meetings import ZoomMeetings
        self.db.run_migrations()
        config = AppConfig({"agent": {"app": {"timezone": "Asia/Dubai"}, "zoom": {"enabled": True, "client_id": "test-client", "client_secret": "test-secret", "account_id": "test-account"}}})
        job = self.db.fetch_one("INSERT INTO jobs(thread_id) VALUES ('zoom-integration') RETURNING id")
        service = ZoomMeetings(self.db, config, job)
        with patch.object(service, "request", side_effect=[{"id": 123456, "join_url": "https://zoom.us/j/123456", "start_url": "NEVER-STORE-HOST-URL"}, {}, {}]) as request:
            created = service.create("Integration meeting", "2026-10-08T14:00:00", 30, "integration-key")
            reused = service.create("Integration meeting", "2026-10-08T14:00:00", 30, "integration-key")
            self.assertEqual(created["id"], reused["id"])
            self.assertEqual(request.call_count, 1)
            updated = service.update(created["id"], start="2026-10-08T15:00:00")
            self.assertEqual(updated["start"], "2026-10-08T15:00:00+04:00")
            self.assertEqual(service.delete(created["id"])["status"], "deleted")
            self.assertEqual(service.delete(created["id"])["status"], "deleted")
            self.assertEqual(request.call_count, 3)
        self.assertNotIn("NEVER-STORE-HOST-URL", json.dumps(self.db.fetch_all("SELECT * FROM zoom_managed_meetings"), default=str))

    def test_zz_concurrent_workers_apply_new_migration_only_once(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as root:
            migration = Path(root) / "007_concurrency_probe.sql"
            migration.write_text("CREATE TABLE concurrency_probe(id int);")
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(lambda _worker: self.db.run_migrations(Path(root)), range(2)))
            self.assertEqual(sorted(len(result) for result in results), [0, 1])
            self.assertEqual(len(self.db.fetch_all("SELECT * FROM schema_migrations WHERE filename = '007_concurrency_probe.sql'")), 1)