import json
import os
import threading
import urllib.request
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from psycopg.conninfo import make_conninfo

from assistant_agent.config import AppConfig
from assistant_agent.database import Database
from assistant_agent.embedding_client import EmbeddingClient
from assistant_agent.embedding_maintenance import EmbeddingMaintenance, SOURCES
from assistant_agent.embedding_scheduler import EmbeddingScheduler
from assistant_agent.context_store import cosine_similarity
from assistant_agent.workspace_index import WorkspaceIndex


class EmbeddingMaintenanceTest(unittest.TestCase):
    def service(self):
        db = MagicMock()
        db.connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = {"locked": True, "queued": False}
        db.fetch_one.return_value = {"count": 0}
        return EmbeddingMaintenance(db, AppConfig({"agent": {"embeddings": {"model": "test-model", "dimensions": 2}}}))

    def test_rebuild_uses_seven_active_sources_and_stable_text_builders(self):
        self.assertEqual({source[0] for source in SOURCES}, {"agent_memories", "workspace_file_chunks", "jobs", "reminders", "outbound_email_logs", "emails", "contacts"})
        samples = [{"content": "knowledge"}, {"content": "chunk"}, {"task_summary": "job", "metadata": {"final_response": "done"}},
                   {"title": "reminder", "task": "call"}, {"to_addresses": ["a@example.com"], "subject": "outbound", "body_text": "body"},
                   {"from_address": "a@example.com", "subject": "inbound", "body_text": "body"}, {"first_name": "Person", "notes": "detail"}]
        for source, row in zip(SOURCES, samples):
            self.assertTrue(source[2](row))

    def test_interrupted_rebuild_resumes_without_clearing_vectors(self):
        service = self.service()
        rows = [{"id": 1, "content": "first", "embedding_model": "old", "embedding_dimensions": 2},
                {"id": 2, "content": "second", "embedding_model": "old", "embedding_dimensions": 2}]
        service.db.fetch_all.side_effect = [rows]
        with patch.object(service, "probe"), patch.object(service.client, "embed", side_effect=[[1.0, 0.0], RuntimeError("offline")]):
            with self.assertRaises(RuntimeError):
                service.reindex(2)
        updates = [call for call in service.db.fetch_one.call_args_list if call.args[0].startswith("UPDATE")]
        self.assertEqual(len(updates), 1)
        self.assertNotIn("embedding = NULL", updates[0].args[0])
        self.assertIn("content IS NOT DISTINCT FROM %s", updates[0].args[0])
        self.assertNotIn("updated_at =", updates[0].args[0].replace("embedding_updated_at =", ""))
        service.db.fetch_all.side_effect = [[rows[1]], [], *[[] for source in SOURCES[1:]]]
        with patch.object(service, "probe"), patch.object(service.client, "embed", return_value=[0.0, 1.0]):
            result = service.reindex(2)
        self.assertEqual(result["refreshed"], 1)
        self.assertTrue(result["complete"])

    def test_concurrent_source_change_is_not_overwritten(self):
        service = self.service()
        service.db.fetch_all.side_effect = [[{"id": 1, "content": "original"}], [], *[[] for source in SOURCES[1:]]]
        service.db.fetch_one.side_effect = [None, *[{"count": 1} for source in SOURCES]]
        with patch.object(service, "probe"), patch.object(service.client, "embed", return_value=[1.0, 0.0]):
            result = service.reindex()
        self.assertEqual(result["refreshed"], 0)
        self.assertEqual(result["conflicts"], 1)
        self.assertFalse(result["complete"])

    def test_already_current_rows_are_a_noop_and_duplicate_runs_are_blocked(self):
        service = self.service()
        service.db.fetch_all.return_value = []
        with patch.object(service, "probe"), patch.object(service.client, "embed") as embed:
            self.assertTrue(service.reindex()["complete"])
            embed.assert_not_called()
        service.db.connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = {"locked": False}
        service.db.connect.return_value.__enter__.return_value.execute.return_value.fetchone.side_effect = [
            {"locked": True}, {"locked": False},
        ]
        with patch.object(service, "probe"), self.assertRaisesRegex(RuntimeError, "already running"):
            service.reindex()

    def test_automatic_refresh_is_bounded_and_continues_after_failure(self):
        service = self.service()
        service.db.fetch_all.side_effect = [
            [{"id": 1, "content": "first"}],
            [{"id": 2, "content": "second"}],
            *[[] for source in SOURCES[2:]],
        ]
        with patch.object(service.client, "embed", side_effect=[RuntimeError("offline"), [1.0, 0.0]]), \
             self.assertLogs("assistant.embedding_maintenance", level="ERROR") as logs:
            result = service.refresh_pending(1)
        self.assertIsNotNone(logs.records[0].exc_info)
        self.assertEqual(result, {"refreshed": 1, "conflicts": 0, "failed": 1})
        self.assertEqual(len(service.db.fetch_all.call_args_list), len(SOURCES))
        for call in service.db.fetch_all.call_args_list:
            self.assertIn("LIMIT %s", call.args[0])
            self.assertEqual(call.args[1][-1], 1)

        service.db.fetch_all.side_effect = [
            [], [{"id": 1, "content": "first"}], [], [], *[[] for source in SOURCES[2:]],
        ]
        with patch.object(service.client, "embed", return_value=[1.0, 0.0]):
            self.assertEqual(service.refresh_pending(1)["refreshed"], 1)
        self.assertEqual(service.db.fetch_all.call_args_list[len(SOURCES)].args[1][-2:], (1, 1))
        self.assertEqual(service.db.fetch_all.call_args_list[len(SOURCES) + 1].args[1][-2:], (0, 1))

    def test_automatic_refresh_timeout_warns_without_traceback_and_retries(self):
        service = self.service()
        service.db.fetch_all.side_effect = [
            [{"id": 79, "content": "first"}],
            [{"id": 80, "content": "second"}],
            *[[] for source in SOURCES[2:]],
        ]
        with patch.object(service.client, "embed", side_effect=[TimeoutError("timed out"), [1.0, 0.0]]), \
             self.assertLogs("assistant.embedding_maintenance", level="WARNING") as logs:
            result = service.refresh_pending(1)
        self.assertEqual(result, {"refreshed": 1, "conflicts": 0, "failed": 1})
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelname, "WARNING")
        self.assertIsNone(logs.records[0].exc_info)
        self.assertEqual(logs.records[0].getMessage(),
                         "embedding request timed out for agent_memories record 79; will retry")

        service.db.fetch_all.side_effect = [
            [], [{"id": 79, "content": "first"}], [], [], *[[] for source in SOURCES[2:]],
        ]
        with patch.object(service.client, "embed", return_value=[1.0, 0.0]):
            self.assertEqual(service.refresh_pending(1)["refreshed"], 1)

    def test_automatic_refresh_skips_disabled_or_locked_and_retries_conflicts(self):
        service = self.service()
        service.client.enabled = False
        self.assertEqual(service.refresh_pending()["refreshed"], 0)
        service.db.connect.assert_not_called()
        service.client.enabled = True
        service.db.connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = {"locked": False}
        self.assertEqual(service.refresh_pending()["refreshed"], 0)
        service.db.fetch_all.assert_not_called()
        service.db.connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = {"locked": True}
        service.db.fetch_all.side_effect = [[{"id": 1, "content": "changed"}], *[[] for source in SOURCES[1:]]]
        service.db.fetch_one.return_value = None
        with patch.object(service.client, "embed", return_value=[1.0, 0.0]):
            self.assertEqual(service.refresh_pending(), {"refreshed": 0, "conflicts": 1, "failed": 0})

    def test_indexer_refreshes_embeddings_even_if_file_scan_is_disabled_or_fails(self):
        for scan_error in (None, OSError("workspace unavailable")):
            with self.subTest(scan_error=scan_error):
                indexer = WorkspaceIndex(MagicMock(), AppConfig({"agent": {"workspace": {"index": {"enabled": False}}}}))
                stop = MagicMock(side_effect=[False, True])
                with patch.object(indexer, "run_once", side_effect=scan_error, return_value=0), \
                     patch.object(indexer, "refresh_embeddings") as refresh, \
                     patch("assistant_agent.workspace_index.LOGGER"):
                    indexer.run_forever(stop)
                refresh.assert_called_once()

    def test_indexer_continues_after_embedding_outage(self):
        indexer = WorkspaceIndex(MagicMock(), AppConfig({}))
        stop = MagicMock(side_effect=[False, False, True])
        with patch.object(indexer, "run_once", return_value=0) as scan, \
             patch.object(indexer, "refresh_embeddings", side_effect=[RuntimeError("offline"), None]) as refresh, \
             self.assertLogs("assistant.workspace_index", level="ERROR"):
            indexer.run_forever(stop)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(refresh.call_count, 2)

    def test_research_waits_for_indexing_and_keeps_gate_during_execution(self):
        from assistant_agent.deep_research import DeepResearchAgent

        agent = DeepResearchAgent(MagicMock(), AppConfig({}))
        with patch("assistant_agent.embedding_scheduler.EmbeddingScheduler") as scheduler, \
             patch.object(agent, "run_ready_research", return_value=True) as process:
            gate = scheduler.return_value.execution.return_value
            gate.__enter__.return_value = False

            def execute(fresh):
                self.assertFalse(fresh)
                gate.__enter__.assert_called_once()
                gate.__exit__.assert_not_called()
                return True

            process.side_effect = execute
            self.assertTrue(agent.run_once())
            gate.__exit__.assert_called_once()

    def test_embedding_response_rejects_nonfinite_zero_and_wrong_dimension_vectors(self):
        client = EmbeddingClient(AppConfig({"agent": {"embeddings": {"dimensions": 2}}}))
        response = MagicMock()
        for vector in ([float("nan"), 1], [0, 0], [1]):
            response.__enter__.return_value.read.return_value = json.dumps({"embeddings": [vector]}).encode()
            with patch("assistant_agent.embedding_client.open_internal_http_request", return_value=response), self.assertRaises(RuntimeError):
                client.embed("test")

    def test_embedding_client_allows_only_managed_http_ollama(self):
        self.assertTrue(EmbeddingClient(AppConfig({}))._uses_internal_http)
        with self.assertRaisesRegex(RuntimeError, "must use HTTPS"):
            EmbeddingClient(AppConfig({"agent": {"embeddings": {"base_url": "http://embedding.example"}}}))
        client = EmbeddingClient(AppConfig({"agent": {"embeddings": {"base_url": "https://embedding.example/v1"}}}))
        self.assertFalse(client._uses_internal_http)

    def test_local_embedding_http_bypasses_proxies_and_refuses_redirects(self):
        from assistant_agent.transport_security import open_internal_http_request

        response = object()
        request = urllib.request.Request("http://ollama:11434/api/embed")
        with patch("assistant_agent.transport_security.urllib.request.build_opener") as build_opener:
            build_opener.return_value.open.return_value = response
            self.assertIs(open_internal_http_request(request, timeout=2), response)

        handlers = build_opener.call_args.args
        self.assertTrue(any(isinstance(handler, urllib.request.ProxyHandler) for handler in handlers))
        self.assertTrue(any(isinstance(handler, urllib.request.HTTPHandler) for handler in handlers))
        redirect_handler = next(handler for handler in handlers if handler.__class__.__name__ == "_NoRedirectHandler")
        self.assertIsNone(redirect_handler.redirect_request(request, None, 302, "Found", {}, "http://external.example"))


@pytest.fixture(params=["fresh", "upgrade"])
def embedding_database(request):
    url = os.environ.get("ASSISTANT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires disposable assistant_test database")
    original = Database(url)
    schema = "test_" + uuid.uuid4().hex
    with original.connect() as conn:
        if conn.info.dbname != "assistant_test":
            raise RuntimeError("embedding tests require a disposable assistant_test database")
        conn.execute("CREATE SCHEMA " + schema)
    db = Database(make_conninfo(url, options="-c search_path=" + schema))
    try:
        db.execute(Path(__file__).resolve().parents[2].joinpath("database/schema.sql").read_text())
        if request.param == "upgrade":
            db.execute("DROP FUNCTION invalidate_source_embedding() CASCADE")
            db.execute("DROP FUNCTION notify_embedding_change() CASCADE")
            db.execute("DELETE FROM schema_migrations WHERE filename = '014_automatic_embeddings.sql'")
            assert db.run_migrations() == ["014_automatic_embeddings.sql"]
        else:
            assert db.run_migrations() == []
        yield db
    finally:
        original.execute("DROP SCHEMA " + schema + " CASCADE")


def test_help_vectors_persist_across_clients_and_only_query_is_embedded(embedding_database, tmp_path):
    from assistant_agent import harness_help

    (tmp_path / "sandbox.md").write_text("## Proxy\nUse injected HTTPS_PROXY settings.\n")
    current_index = harness_help.index(tmp_path)
    config = AppConfig({"agent": {"embeddings": {"model": "help-test", "dimensions": 2}}})
    with patch.object(harness_help, "index", return_value=current_index), \
            patch("assistant_agent.harness_help.EmbeddingClient.embed", return_value=[1.0, 0.0]) as embed:
        assert harness_help.refresh_embeddings(embedding_database, config, deadline=float("inf")) == 1
        embed.reset_mock()
        other_client = Database(embedding_database.database_url)
        assert harness_help.refresh_embeddings(other_client, config, deadline=float("inf")) == 0
        result = harness_help.search("outbound connectivity", db=other_client, config=config)
    embed.assert_called_once_with("outbound connectivity")
    assert result["search_mode"] == "semantic"
    assert result["results"][0]["source"] == "sandbox.md"


def test_automatic_refresh_indexes_all_sources_and_reindexes_content_changes(embedding_database, tmp_path):
    db = embedding_database
    job = db.create_manual_job("First task", "Inbound body")
    scoped = db.for_job(job["id"])
    scoped.execute("INSERT INTO agent_memories(content, source_job_id) VALUES ('Remember this', %s)", (job["id"],))
    scoped.execute("INSERT INTO reminders(title, task, run_at, created_by_job_id) VALUES ('Reminder', 'Call', now(), %s)",
                   (job["id"],))
    scoped.execute("""INSERT INTO contacts(first_name, embedding, embedding_model, embedding_dimensions)
                      VALUES ('Ada', ARRAY[0.0, 1.0]::float8[], 'old-model', 2)""")
    scoped.execute("""INSERT INTO outbound_email_logs(job_id, to_addresses, subject, body_text)
                      VALUES (%s, ARRAY['test@example.com'], 'Outbound', 'Outbound body')""", (job["id"],))
    file = db.fetch_one("INSERT INTO workspace_files(relative_path, index_status) VALUES ('test.txt', 'indexed') RETURNING id")
    db.execute("INSERT INTO workspace_file_chunks(file_id, chunk_index, content) VALUES (%s, 0, 'Workspace content')", (file["id"],))
    config = AppConfig({"agent": {"embeddings": {"model": "test-model", "dimensions": 2},
                                  "filesystem": {"shared_root": str(tmp_path)},
                                  "workspace": {"index": {"enabled": False}}}})
    indexer = WorkspaceIndex(db, config)
    service = indexer.embedding_maintenance
    assert service.status()["pending"] == {source[0]: 1 for source in SOURCES}
    before = db.fetch_one("SELECT updated_at FROM jobs WHERE id = %s", (job["id"],))
    with patch.object(service.client, "embed", return_value=[1.0, 0.0]) as embed:
        indexer.run_forever(MagicMock(side_effect=[False, True, True]))
        assert service.status()["complete"]
        assert service.refresh_pending(1) == {"refreshed": 0, "conflicts": 0, "failed": 0}
        assert embed.call_count == 7
    assert db.fetch_one("SELECT updated_at FROM jobs WHERE id = %s", (job["id"],)) == before

    scoped.execute("UPDATE jobs SET status = 'completed', metadata = '{\"unrelated\": true}' WHERE id = %s", (job["id"],))
    assert service.status()["complete"]
    edits = {
        "agent_memories": "content = 'Updated memory'",
        "workspace_file_chunks": "content = 'Updated workspace'",
        "jobs": """metadata = '{"final_response": "Task result"}'""",
        "reminders": "task = 'Updated task'",
        "outbound_email_logs": "body_text = 'Updated outbound'",
        "emails": "body_text = 'Updated inbound'",
        "contacts": "notes = 'Updated contact'",
    }
    for table, assignment in edits.items():
        scoped.execute(f"UPDATE {table} SET {assignment}")
        assert db.fetch_one(f"SELECT embedding, embedding_model, embedding_dimensions, embedding_updated_at FROM {table}") == {
            "embedding": None, "embedding_model": None, "embedding_dimensions": None, "embedding_updated_at": None,
        }
    assert service.status()["pending"] == {source[0]: 1 for source in SOURCES}
    with patch.object(service.client, "embed", return_value=[0.0, 1.0]) as embed:
        assert service.refresh_pending(1)["refreshed"] == 7
        assert any("Task result" in call.args[0] for call in embed.call_args_list)
    assert service.status()["complete"]
    scoped.execute("UPDATE contacts SET notes = 'Inline update', embedding = ARRAY[0.5, 0.5]::float8[]")
    assert db.fetch_one("SELECT embedding FROM contacts")["embedding"] == [0.5, 0.5]


def test_automatic_refresh_retries_concurrent_edits_and_skips_inactive_sources(embedding_database):
    db = embedding_database
    job = db.create_manual_job("Original task", "Inbound body")
    db.for_job(job["id"]).execute(
        "INSERT INTO agent_memories(content, source_job_id, expires_at) VALUES ('Expired', %s, now() - interval '1 day')",
        (job["id"],),
    )
    file = db.fetch_one("INSERT INTO workspace_files(relative_path, index_status) VALUES ('deleted.txt', 'deleted') RETURNING id")
    db.execute("INSERT INTO workspace_file_chunks(file_id, chunk_index, content) VALUES (%s, 0, 'Deleted content')", (file["id"],))
    service = EmbeddingMaintenance(db, AppConfig({"agent": {"embeddings": {"model": "test-model", "dimensions": 2}}}))

    def edit_during_embed(text):
        if text == "Original task":
            db.execute("UPDATE jobs SET task_summary = 'Changed task' WHERE id = %s", (job["id"],))
        assert text not in {"Expired", "Deleted content"}
        return [1.0, 0.0]

    with patch.object(service.client, "embed", side_effect=edit_during_embed) as embed:
        assert service.refresh_pending(1) == {"refreshed": 1, "conflicts": 1, "failed": 0}
        assert db.fetch_one("SELECT embedding FROM jobs WHERE id = %s", (job["id"],))["embedding"] is None
        assert service.refresh_pending(1) == {"refreshed": 1, "conflicts": 0, "failed": 0}
        assert embed.call_args.args[0] == "Changed task"
    assert service.status()["complete"]


def test_source_changes_notify_worker_after_commit_without_exposing_content(embedding_database):
    db = embedding_database
    with db.connect() as listener:
        listener.autocommit = True
        listener.execute("LISTEN assistant_embeddings")
        db.create_manual_job("Private task", "Private body")
        notifications = list(listener.notifies(timeout=1, stop_after=1))
        assert notifications
        assert notifications[0].channel == "assistant_embeddings"
        assert notifications[0].payload == ""


def test_execution_drains_embeddings_and_excludes_background_indexing(embedding_database, tmp_path):
    from assistant_agent.memory_store import MemoryStore

    db = embedding_database
    job = db.create_manual_job("Task to index", "Incoming message")
    config = AppConfig({"agent": {"embeddings": {"model": "test-model", "dimensions": 2},
                                  "filesystem": {"shared_root": str(tmp_path)}}})
    scheduler = EmbeddingScheduler(db, config)
    with patch.object(scheduler.maintenance.client, "embed", return_value=[1.0, 0.0]) as embed:
        with scheduler.execution() as fresh:
            assert fresh
            assert scheduler.maintenance.status()["complete"]
            assert embed.call_count == 2
            with EmbeddingScheduler(db, config).idle() as idle:
                assert not idle
            indexer = WorkspaceIndex(db, config)
            with patch.object(indexer, "run_once") as scan, patch.object(indexer, "refresh_embeddings") as refresh:
                assert indexer.run_idle_once() == 0
                scan.assert_not_called()
                refresh.assert_not_called()
            with patch("assistant_agent.embedding_client.EmbeddingClient.embed") as inline:
                MemoryStore(db.for_job(job["id"]), config).create("New memory while working", source_job_id=job["id"])
                inline.assert_not_called()
    with EmbeddingScheduler(db, config).idle() as idle:
        assert idle
    with patch.object(scheduler.maintenance.client, "embed", return_value=[1.0, 0.0]):
        with scheduler.execution() as fresh:
            assert fresh
            assert scheduler.maintenance.status()["complete"]


def test_execution_timeout_uses_keyword_fallback_and_releases_locks(embedding_database):
    from assistant_agent.embedding_client import keyword_only

    db = embedding_database
    db.create_manual_job("Pending task", "Pending message")
    config = AppConfig({"agent": {"embeddings": {"model": "test-model", "dimensions": 2}}})
    scheduler = EmbeddingScheduler(db, config)
    with patch.object(scheduler.maintenance.client, "embed", side_effect=RuntimeError("Ollama offline")):
        with scheduler.execution(wait_seconds=0.02) as fresh:
            assert not fresh
            assert keyword_only.get()
            with patch("assistant_agent.embedding_client.open_internal_http_request") as request:
                with pytest.raises(RuntimeError, match="keyword-only"):
                    EmbeddingClient(config).embed("Find context")
                request.assert_not_called()
            with pytest.raises(RuntimeError, match="while tasks are executing"):
                EmbeddingMaintenance(db, config).reindex()
    assert not keyword_only.get()
    with scheduler.idle() as idle:
        assert idle
    assert scheduler.maintenance.status()["complete"] is False


@pytest.mark.parametrize("role", ["task", "research"])
def test_timed_out_execution_logs_valid_event_and_still_processes_work(embedding_database, role):
    from assistant_agent.deep_research import DeepResearchAgent
    from assistant_agent.task_agent import TaskAgent

    db = embedding_database
    job = db.create_manual_job("Timeout fallback", "Process despite indexing backlog")
    config = AppConfig({"agent": {"embeddings": {"model": "test-model", "dimensions": 2}}})
    scheduler = EmbeddingScheduler(db, config)
    with scheduler.execution(wait_seconds=0) as fresh:
        assert not fresh
        if role == "task":
            agent = TaskAgent(db, config)
            with patch.object(agent, "process_job") as process:
                assert agent.run_ready_job(fresh)
                assert process.call_args.args[0]["id"] == job["id"]
        else:
            agent = DeepResearchAgent(db, config)
            run = {"id": 1, "execution_job_id": job["id"]}
            with patch.object(agent, "claim_run", return_value=run), \
                 patch.object(DeepResearchAgent, "process_run") as process:
                assert agent.run_ready_research(fresh)
                process.assert_called_once_with(run)
    event = db.fetch_one("SELECT event_type, output_data FROM task_logs WHERE job_id = %s", (job["id"],))
    assert event == {
        "event_type": "timeout",
        "output_data": {"reason": "embedding_index_incomplete", "retrieval": "keyword_only", "wait_seconds": 60},
    }


def test_concurrent_execution_does_not_index_while_another_task_is_active(embedding_database):
    db = embedding_database
    config = AppConfig({"agent": {"embeddings": {"model": "test-model", "dimensions": 2}}})
    first = EmbeddingScheduler(db, config)
    second = EmbeddingScheduler(db, config)
    with first.execution() as fresh:
        assert fresh
        db.create_manual_job("Arrived during active work", "Needs indexing later")
        with patch.object(second.maintenance.client, "embed") as embed:
            with second.execution(wait_seconds=0.02) as second_fresh:
                assert not second_fresh
            embed.assert_not_called()
    with patch.object(second.maintenance.client, "embed", return_value=[1.0, 0.0]):
        with second.execution() as fresh:
            assert fresh
            assert second.maintenance.status()["complete"]


def test_execution_handoff_stays_locked_and_releases_after_exception(embedding_database):
    db = embedding_database
    scheduler = EmbeddingScheduler(db, AppConfig({"agent": {"embeddings": {"enabled": False}}}))
    with pytest.raises(RuntimeError, match="Task failed"):
        with scheduler.execution():
            with scheduler.idle() as idle:
                assert not idle
            raise RuntimeError("Task failed")
    with scheduler.idle() as idle:
        assert idle


def test_manual_rebuild_yields_to_task_startup(embedding_database):
    from assistant_agent.embedding_scheduler import STARTUP_LOCK

    db = embedding_database
    db.create_manual_job("Queued task", "Inbound message")
    config = AppConfig({"agent": {"embeddings": {"model": "test-model", "dimensions": 2}}})
    service = EmbeddingMaintenance(db, config)
    with db.connect() as waiter:
        waiter.autocommit = True
        waiter.execute("SELECT pg_advisory_lock_shared(hashtext(%s))", (STARTUP_LOCK,))
        with patch.object(service, "probe"), patch.object(service.client, "embed") as embed:
            with pytest.raises(RuntimeError, match="yielded to queued agent work"):
                service.reindex()
            embed.assert_not_called()
        assert EmbeddingScheduler(db, config).startup_waiting()
        with EmbeddingScheduler(db, config).idle() as idle:
            assert not idle
    assert not EmbeddingScheduler(db, config).startup_waiting()
    with EmbeddingScheduler(db, config).idle() as idle:
        assert idle


def test_incoming_task_waits_for_inflight_idle_indexing(embedding_database):
    db = embedding_database
    db.create_manual_job("New task", "Incoming message")
    config = AppConfig({"agent": {"embeddings": {"model": "test-model", "dimensions": 2}}})
    scheduler = EmbeddingScheduler(db, config)
    embedding_started = threading.Event()
    release_embedding = threading.Event()
    task_waiting = threading.Event()

    def embed(text):
        embedding_started.set()
        assert release_embedding.wait(timeout=5)
        return [1.0, 0.0]

    def background_index():
        with scheduler.idle() as idle:
            assert idle
            scheduler.maintenance.refresh_pending()

    def incoming_task():
        task_waiting.set()
        with EmbeddingScheduler(db, config).execution(wait_seconds=2) as fresh:
            assert fresh
            assert scheduler.maintenance.status()["complete"]

    with patch.object(scheduler.maintenance.client, "embed", side_effect=embed):
        with ThreadPoolExecutor(max_workers=2) as pool:
            background = pool.submit(background_index)
            assert embedding_started.wait(timeout=5)
            task = pool.submit(incoming_task)
            try:
                assert task_waiting.wait(timeout=5)
                assert not task.done()
            finally:
                release_embedding.set()
            background.result(timeout=5)
            task.result(timeout=5)


@unittest.skipUnless(os.getenv("ASSISTANT_TEST_EMBEDDING_URL"), "requires isolated Ollama with embeddinggemma downloaded")
class EmbeddingGemmaSmokeTest(unittest.TestCase):
    def test_real_model_dimensions_retrieval_and_long_input(self):
        config = AppConfig({"agent": {"embeddings": {"model": "embeddinggemma", "base_url": os.environ["ASSISTANT_TEST_EMBEDDING_URL"], "timeout_seconds": 120}}})
        service = EmbeddingMaintenance(MagicMock(), config)
        self.assertEqual(service.probe()["dimensions"], 768)
        query = service.client.embed("When is my dentist appointment?")
        relevant = service.client.embed("Your dentist appointment is on Friday morning at 10 AM.")
        unrelated = service.client.embed("The quarterly financial report shows a rise in operating revenue.")
        self.assertGreater(cosine_similarity(query, relevant), cosine_similarity(query, unrelated))
        self.assertEqual(len(service.client.embed("Meeting agenda and project updates. " * 2000)), 768)


if __name__ == "__main__":
    unittest.main()