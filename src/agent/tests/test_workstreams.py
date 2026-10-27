import hashlib
import io
import json
import os
import shutil
import uuid
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from assistant_agent.config import AppConfig
from assistant_agent.database import Database
from assistant_agent.requests import IncomingRequest, RequestService
from assistant_agent.workstreams import WorkstreamError, WorkstreamStore, deletion_scope


def test_connected_scope_is_recursive_and_includes_descendants():
    parents = {1: None, 2: 1, 3: None, 4: None, 5: 4}
    attachments = {10: {1, 3}, 11: {3, 4}, 12: {2}}
    assert deletion_scope(1, "preserve_shared", parents, attachments) == ({1, 2}, {12}, {10}, set())
    assert deletion_scope(1, "delete_objects", parents, attachments) == ({1, 2}, {10, 12}, {10}, {3})
    assert deletion_scope(1, "connected", parents, attachments) == ({1, 2, 3, 4, 5}, {10, 11, 12}, set(), set())


@pytest.fixture
def store(tmp_path, request):
    url = os.environ.get("ASSISTANT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires disposable assistant_test database")
    original = Database(url)
    schema = "test_" + uuid.uuid4().hex
    with original.connect() as conn:
        if conn.info.dbname != "assistant_test":
            raise RuntimeError("workstream tests require a disposable assistant_test database")
        conn.execute("CREATE SCHEMA " + schema)
    db = Database(make_conninfo(url, options="-c search_path=" + schema))
    try:
        db.execute(Path(__file__).resolve().parents[2].joinpath("database/schema.sql").read_text())
        db.ensure_feature_schema()
        if hasattr(request, "param"):
            db.execute("DELETE FROM schema_migrations WHERE filename > %s", (request.param,))
            db.execute("""
                DROP FUNCTION workstream_research_job_control() CASCADE;
                DROP FUNCTION workstream_research_execution_status() CASCADE;
                DROP FUNCTION workstream_research_execution() CASCADE;
                ALTER TABLE deep_research_runs DROP COLUMN execution_job_id;
            """)
            migrations = tmp_path / "migrations"
            migrations.mkdir()
            for source in Path(__file__).resolve().parents[1].joinpath("src/assistant_agent/migrations").glob("*.sql"):
                if source.name <= request.param:
                    shutil.copy2(source, migrations)
            db.run_migrations(migrations)
        else:
            db.run_migrations()
        share, raw = tmp_path / "share", tmp_path / "raw"
        share.mkdir()
        raw.mkdir()
        config = AppConfig({"agent": {"filesystem": {"shared_root": str(share)},
                                     "artifacts": {"raw_root": str(raw)}, "embeddings": {"enabled": False}}})
        yield WorkstreamStore(db, config)
    finally:
        original.execute("DROP SCHEMA " + schema + " CASCADE")


def job(store, title="Task", parent=None):
    return store.db.create_manual_job(title, "Workstream test", parent_workstream_id=parent)


def memory(store, source, text="Remember this"):
    return store.db.for_job(source["id"]).fetch_one(
        "INSERT INTO agent_memories(content, source_job_id) VALUES (%s, %s) RETURNING *", (text, source["id"]),
    )


def legacy_attach(store, workstream_id, kind, key):
    obj = store.db.fetch_one(
        "SELECT id FROM workstream_objects WHERE kind = %s AND object_key = %s",
        (kind, str(key)),
    )
    assert obj
    pending = [obj["id"]]
    attached = set()
    while pending:
        object_id = pending.pop()
        if object_id in attached:
            continue
        attached.add(object_id)
        store.db.execute(
            "INSERT INTO workstream_attachments(workstream_id, object_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (workstream_id, object_id),
        )
        related = store.db.fetch_all(
            """SELECT j.object_id FROM deep_research_runs r JOIN jobs j ON j.id = r.original_job_id
               WHERE r.object_id = %s
               UNION
               SELECT j.object_id FROM deep_research_runs r JOIN jobs j ON j.id = r.execution_job_id
               WHERE r.object_id = %s
               UNION
               SELECT f.object_id FROM dropbox_transfers d JOIN workstream_files f
                 ON f.path IN (d.local_path, d.markdown_path)
                 WHERE d.object_id = %s AND d.direction = 'import'
               UNION
               SELECT f.object_id FROM workstream_files f
                 WHERE f.path IN (
                   SELECT raw_path FROM processed_artifacts WHERE object_id = %s
                   UNION SELECT markdown_path FROM processed_artifacts WHERE object_id = %s
                 )
               UNION
               SELECT z.object_id FROM calendar_managed_events c JOIN zoom_managed_meetings z
                 ON z.id::text = c.metadata->>'zoom_meeting_id' OR z.calendar_event_id = c.assistant_id
                 WHERE c.object_id = %s""",
            (object_id, object_id, object_id, object_id, object_id, object_id),
        )
        pending.extend(item["object_id"] for item in related)
        artifact = store.db.fetch_one("SELECT markdown_path FROM processed_artifacts WHERE object_id = %s", (object_id,))
        if artifact and artifact.get("markdown_path") and not Path(artifact["markdown_path"]).is_absolute():
            resolved = Path(store.config.get("agent.filesystem.shared_root")) / artifact["markdown_path"]
            file_obj = store.db.fetch_one("SELECT object_id FROM workstream_files WHERE path = %s", (str(resolved),))
            if file_obj:
                pending.append(file_obj["object_id"])


def remove(store, identifier, mode="preserve_shared"):
    preview = store.preview(identifier, mode)
    deletion = store.begin_delete(identifier, mode, preview["preview_digest"])
    return store.execute_delete(deletion)


def test_read_returns_stable_message_cursor():
    class FakeDatabase:
        def fetch_one(self, query, params):
            if "FROM workstreams" in query:
                return {"id": 1, "main_thread_id": "thread-1"}
            return {"thread_id": "thread-1", "role": "main"}

        def latest_thread_messages(self, thread_id, *, limit, through):
            assert through is None
            return [
                {"id": 5, "thread_item_id": "email:5", "received_at": "2026-06-06T10:00:00Z"},
                {"id": 6, "thread_item_id": "request:6", "received_at": "2026-06-06T10:01:00Z"},
            ]

    store = WorkstreamStore.__new__(WorkstreamStore)
    store.db = FakeDatabase()
    result = store.read(1, limit=2)

    assert json.loads(result["next_before"]) == {
        "timestamp": "2026-06-06T10:00:00Z",
        "id": 5,
        "thread_item_id": "email:5",
    }


def test_channels_share_logical_owner_but_not_sender_or_chat(store):
    service = RequestService(store.db)
    email = job(store, "Email task")
    followup = service.receive_request(IncomingRequest(
        "telegram", "update:1", "same-chat", "user", email["thread_id"], "Follow-up"))
    other = service.receive_request(IncomingRequest(
        "telegram", "update:2", "same-chat", "user", "new-task", "Different task"))
    assert followup["workstream_id"] == email["workstream_id"]
    assert other["workstream_id"] != email["workstream_id"]
    assert service.receive_request(IncomingRequest(
        "telegram", "update:1", "same-chat", "user", email["thread_id"], "Retry"))["id"] == followup["id"]
    with pytest.raises(WorkstreamError, match="only contacts and files"):
        store.attach(other["workstream_id"], "request", followup["id"])
    with pytest.raises(WorkstreamError, match="only contacts and files"):
        store.attach(other["workstream_id"], "outbound_email", "1")
    spawned = RequestService(store.db.for_job(email["id"])).receive_request(IncomingRequest(
        "future-channel", "spawned:1", "spawned-conversation", "system", "spawned-thread", "Subtask"))
    assert store.db.fetch_one("SELECT parent_id FROM workstreams WHERE id = %s",
                              (spawned["workstream_id"],))["parent_id"] == email["workstream_id"]


@pytest.mark.parametrize("mode", ["preserve_shared", "delete_objects", "connected"])
def test_deletion_policies_preserve_or_remove_shared_data_and_children(store, mode):
    a, b, c = job(store, "A"), job(store, "B"), job(store, "C")
    child = job(store, "Child of A", a["workstream_id"])
    grandchild = job(store, "Grandchild", child["workstream_id"])
    shared = memory(store, a)
    bridge = memory(store, b, "Bridge from B to C")
    legacy_attach(store, b["workstream_id"], "memory", shared["id"])
    legacy_attach(store, c["workstream_id"], "memory", bridge["id"])
    private = memory(store, child, "Exclusive child memory")
    store.db.log_event(child["id"], "tool_result", output_data={"private": "erase me"})
    store.db.execute("INSERT INTO memory_events(memory_id, job_id, event_type) VALUES (%s, %s, 'create')",
                     (private["id"], child["id"]))
    store.db.execute(
        """INSERT INTO outbound_email_logs(job_id, to_addresses, subject, body_text)
           VALUES (%s, ARRAY['test@example.com'], 'Exclusive outbound', 'test')""", (a["id"],),
    )
    preview = store.preview(a["workstream_id"], mode)
    assert preview["deleted_counts"]["emails"] == (5 if mode == "connected" else 3)
    assert preview["deleted_counts"]["outbound_email_logs"] == 1
    assert remove(store, a["workstream_id"], mode)["status"] == "completed"
    assert store.db.fetch_one("SELECT id FROM jobs WHERE id = %s", (grandchild["id"],)) is None
    assert store.db.fetch_one("SELECT id FROM agent_memories WHERE id = %s", (private["id"],)) is None
    assert store.db.fetch_all("SELECT * FROM memory_events") == []
    current = store.db.fetch_one("SELECT * FROM agent_memories WHERE id = %s", (shared["id"],))
    assert (current is not None) == (mode == "preserve_shared")
    assert (store.db.fetch_one("SELECT id FROM jobs WHERE id = %s", (b["id"],)) is None) == (mode == "connected")
    if current:
        origin = store.db.fetch_one("SELECT origin_workstream_id FROM workstream_objects WHERE id = %s", (current["object_id"],))
        assert origin["origin_workstream_id"] == a["workstream_id"]
        assert store.db.fetch_one("SELECT source_job_id FROM agent_memories WHERE id = %s", (shared["id"],))["source_job_id"] is None
    receipt = store.db.fetch_one("SELECT * FROM workstream_deletions ORDER BY id DESC LIMIT 1")
    assert "records" not in receipt["manifest"]


def test_preview_is_read_only_and_invalidated_by_new_relationship(store):
    a, b = job(store), job(store)
    owned = memory(store, a)
    before = store.db.fetch_all("SELECT * FROM workstreams ORDER BY id")
    preview = store.preview(a["workstream_id"])
    assert store.db.fetch_all("SELECT * FROM workstreams ORDER BY id") == before
    legacy_attach(store, b["workstream_id"], "memory", owned["id"])
    with pytest.raises(WorkstreamError, match="scope changed"):
        store.begin_delete(a["workstream_id"], "preserve_shared", preview["preview_digest"])
    assert store.db.fetch_all("SELECT * FROM workstream_deletions") == []


def test_running_work_and_raw_workstream_deletion_are_refused(store):
    source = job(store)
    store.db.execute("UPDATE jobs SET status = 'running' WHERE id = %s", (source["id"],))
    preview = store.preview(source["workstream_id"])
    assert preview["running_jobs"] == [source["id"]]
    with pytest.raises(WorkstreamError, match="still running"):
        store.begin_delete(source["workstream_id"], "preserve_shared", preview["preview_digest"])
    with pytest.raises(psycopg.Error, match="confirmed workstream deletion"):
        store.db.execute("DELETE FROM workstreams WHERE id = %s", (source["workstream_id"],))


def test_pending_deletion_blocks_new_effects_and_is_retryable(store):
    source = job(store)
    owned = memory(store, source)
    preview = store.preview(source["workstream_id"])
    identifier = store.begin_delete(source["workstream_id"], "preserve_shared", preview["preview_digest"])
    with pytest.raises(psycopg.Error, match="deletion is pending"):
        store.db.execute("UPDATE agent_memories SET content = 'late edit' WHERE id = %s", (owned["id"],))
    with pytest.raises(psycopg.Error, match="being deleted"):
        store.db.execute("INSERT INTO jobs(thread_id, status) VALUES (%s, 'completed')", (source["thread_id"],))
    assert store.execute_delete(identifier)["status"] == "completed"
    assert store.execute_delete(identifier)["status"] == "completed"


def test_shared_research_retains_its_job_and_reminder_spawns_under_surviving_owner(store):
    from assistant_agent.reminders import ReminderScheduler
    a, b = job(store, "A"), job(store, "B")
    run = store.db.fetch_one(
        """INSERT INTO deep_research_runs(original_job_id, original_thread_id, title, research_question)
           VALUES (%s, %s, 'Research', 'Question') RETURNING *""", (a["id"], a["thread_id"]),
    )
    reminder = store.db.fetch_one(
        """INSERT INTO reminders(title, task, run_at, created_by_job_id) VALUES ('Reminder', 'Task', now(), %s)
           RETURNING *""", (a["id"],),
    )
    legacy_attach(store, b["workstream_id"], "research", run["id"])
    legacy_attach(store, b["workstream_id"], "reminder", reminder["id"])
    remove(store, a["workstream_id"])
    assert store.db.fetch_one("SELECT workstream_id FROM jobs WHERE id = %s", (a["id"],))["workstream_id"] == b["workstream_id"]
    current = store.db.fetch_one("SELECT * FROM reminders WHERE id = %s", (reminder["id"],))
    with store.db.connect() as connection, connection.cursor() as cur:
        spawned = ReminderScheduler(store.db, store.config).create_job_for_reminder(cur, current)
    stream = store.db.fetch_one("SELECT * FROM workstreams WHERE id = %s", (spawned["workstream_id"],))
    assert stream["id"] == b["workstream_id"]
    assert store.db.fetch_one(
        "SELECT role FROM workstream_threads WHERE thread_id = %s", (spawned["thread_id"],),
    )["role"] == "system"


def test_file_cleanup_checks_digests_and_keeps_recovery_records(store):
    source = job(store)
    path = Path(store.config.get("agent.filesystem.shared_root")) / "output.txt"
    path.write_text("original")
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    store.db.fetch_one("INSERT INTO workstream_files(path, sha256, source_job_id) VALUES (%s, %s, %s) RETURNING *",
                       (str(path), checksum, source["id"]))
    preview = store.preview(source["workstream_id"])
    path.write_text("changed before confirmation")
    with pytest.raises(WorkstreamError, match="scope changed"):
        store.begin_delete(source["workstream_id"], "preserve_shared", preview["preview_digest"])
    path.write_text("original")
    preview = store.preview(source["workstream_id"])
    identifier = store.begin_delete(source["workstream_id"], "preserve_shared", preview["preview_digest"])
    path.write_text("late change")
    with pytest.raises(WorkstreamError, match="file changed"):
        store.execute_delete(identifier)
    assert store.db.fetch_one("SELECT id FROM jobs WHERE id = %s", (source["id"],))
    assert store.db.fetch_one("SELECT status FROM workstream_deletions WHERE id = %s", (identifier,))["status"] == "failed"
    path.write_text("original")
    assert store.execute_delete(identifier)["status"] == "completed"
    assert not path.exists()


def test_shared_files_and_artifacts_survive_source_message_deletion(store):
    a, b = job(store), job(store)
    path = Path(store.config.get("agent.artifacts.raw_root")) / "received.txt"
    path.write_text("shared received bytes")
    artifact = store.db.fetch_one(
        """INSERT INTO processed_artifacts(email_id, thread_id, source_type, source_label, raw_path, raw_sha256)
           VALUES (%s, %s, 'attachment', 'received', %s, %s) RETURNING *""",
        (a["trigger_email_id"], a["thread_id"], str(path), hashlib.sha256(path.read_bytes()).hexdigest()),
    )
    legacy_attach(store, b["workstream_id"], "artifact", artifact["id"])
    remove(store, a["workstream_id"])
    assert path.is_file()
    current = store.db.fetch_one("SELECT * FROM processed_artifacts WHERE id = %s", (artifact["id"],))
    assert current["email_id"] is None
    assert current["request_id"] is None
    remove(store, b["workstream_id"])
    assert not path.exists()


@pytest.mark.parametrize("shared", [False, True])
def test_generated_artifact_markdown_previews_and_deletes_with_last_owner(store, shared):
    from assistant_agent.artifact_processor import ArtifactProcessor
    from assistant_agent.workstream_files import WorkstreamFileTracker

    source = job(store)
    processor = ArtifactProcessor(store.db, store.config)
    message = store.db.fetch_one("SELECT * FROM emails WHERE id = %s", (source["trigger_email_id"],))
    relative = processor.write_markdown(message, "report.pdf", "Converted document")
    assert not Path(relative).is_absolute()
    path = processor.shared_root / relative
    artifact = processor.create_artifact(
        message, source_type="attachment", source_label="report.pdf",
        markdown_path=relative, markdown_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    tracked = WorkstreamFileTracker(store.db.for_job(source["id"]), store.config, source["id"])
    tracked.track_file(path)
    if shared:
        other = job(store)
        legacy_attach(store, other["workstream_id"], "artifact", artifact["id"])
        file_object = store.db.fetch_one("SELECT object_id FROM workstream_files WHERE path = %s", (str(path),))
        assert file_object["object_id"] in {o["id"] for o in store.objects(other["workstream_id"])}
    preview = store.preview(source["workstream_id"], "connected" if not shared else "preserve_shared")
    assert preview["blocked_files"] == []
    assert remove(store, source["workstream_id"], "connected" if not shared else "preserve_shared")["status"] == "completed"
    assert path.exists() is shared
    if shared:
        remove(store, other["workstream_id"])
        assert not path.exists()


@pytest.mark.parametrize("relative", ["../outside.md", ".assistant/docs/reference.md"])
def test_artifact_relative_paths_cannot_escape_or_delete_protected_files(store, relative):
    source = job(store)
    store.db.execute(
        """INSERT INTO processed_artifacts(email_id, thread_id, source_type, source_label, markdown_path)
           VALUES (%s, %s, 'attachment', 'unsafe', %s)""",
        (source["trigger_email_id"], source["thread_id"], relative),
    )
    with pytest.raises(WorkstreamError, match="invalid cleanup path|protected"):
        store.preview(source["workstream_id"], "connected")
    assert store.db.fetch_all("SELECT * FROM workstream_deletions") == []


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_voice_artifact_digest_and_legacy_transcript_verification(store, legacy, changed):
    from assistant_agent.artifact_processor import ArtifactProcessor

    source = job(store)
    config = AppConfig({**store.config.values, "agent": {
        **store.config.values["agent"], "transcription": {"enabled": True},
    }})
    processor = ArtifactProcessor(store.db, config)
    message = store.db.fetch_one("SELECT * FROM emails WHERE id = %s", (source["trigger_email_id"],))
    audio = Path(config.get("agent.artifacts.raw_root")) / "voice.ogg"
    audio.write_bytes(b"mock audio")
    text = "A stored transcript with unicode: \u00e9\n"
    with patch.object(processor, "scan_file", return_value={"status": "clean"}), \
         patch("assistant_agent.transcription.transcribe_audio", return_value={"text": text}):
        artifact = processor.process_attachment(message, {
            "path": str(audio), "filename": "voice.ogg",
            "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
        }, 1)
    assert artifact["markdown_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert artifact["markdown_size_bytes"] == len(text.encode("utf-8"))
    if legacy:
        store.db.execute("UPDATE processed_artifacts SET markdown_sha256 = NULL WHERE id = %s", (artifact["id"],))
    markdown = processor.shared_root / artifact["markdown_path"]
    if changed:
        markdown.write_text("Modified after transcription")
    preview = store.preview(source["workstream_id"], "connected")
    assert bool(preview["blocked_files"]) is changed
    if changed:
        with pytest.raises(WorkstreamError, match="file provenance"):
            store.begin_delete(source["workstream_id"], "connected", preview["preview_digest"])
        assert markdown.exists()
    else:
        assert remove(store, source["workstream_id"], "connected")["status"] == "completed"
        assert not markdown.exists()
        assert not audio.exists()


def test_remote_failure_retains_manifest_and_retries_only_incomplete_steps(store):
    source = job(store)
    store.db.execute(
        """INSERT INTO dropbox_transfers(request_key, job_id, direction, remote_path, revision, status)
           VALUES ('upload', %s, 'export', '/Outbox/job-1/report.txt', 'rev-1', 'ready')""", (source["id"],),
    )
    preview = store.preview(source["workstream_id"])
    assert preview["remote_actions"][0]["revision"] == "rev-1"
    identifier = store.begin_delete(source["workstream_id"], "preserve_shared", preview["preview_digest"])
    with patch("assistant_agent.dropbox_provider.DropboxClient") as client:
        client.return_value.delete_file.side_effect = RuntimeError("provider unavailable")
        with pytest.raises(RuntimeError, match="provider unavailable"):
            store.execute_delete(identifier)
        assert store.db.fetch_one("SELECT request_key FROM dropbox_transfers WHERE request_key = 'upload'")
        client.return_value.delete_file.side_effect = None
        assert store.execute_delete(identifier)["status"] == "completed"
        client.return_value.delete_file.assert_called_with("/Outbox/job-1/report.txt", "rev-1", "Outbox")


def test_object_provenance_and_ancestry_cannot_be_rewritten(store):
    a, b = job(store), job(store)
    obj = memory(store, a)
    with pytest.raises(psycopg.Error, match="provenance is immutable"):
        store.db.execute("UPDATE workstream_objects SET origin_workstream_id = %s WHERE id = %s",
                         (b["workstream_id"], obj["object_id"]))
    with pytest.raises(psycopg.Error, match="ancestry is immutable"):
        store.db.execute("UPDATE workstreams SET parent_id = %s WHERE id = %s",
                         (a["workstream_id"], b["workstream_id"]))
    store.db.ensure_feature_schema()
    assert store.db.run_migrations() == []


def test_contact_adoption_is_explicit_and_updates_notify_retainers(store):
    source, editor, unrelated = job(store, "Source"), job(store, "Editor"), job(store, "Unrelated")
    contact = store.db.for_job(source["id"]).fetch_one(
        "INSERT INTO contacts(first_name) VALUES ('Ada') RETURNING *",
    )
    with pytest.raises(WorkstreamError, match="only contacts and files"):
        store.attach(editor["workstream_id"], "memory", "1")
    adopted = store.attach(editor["workstream_id"], "contact", str(contact["id"]))
    assert adopted["origin_workstream_id"] == source["workstream_id"]
    store.db.execute("UPDATE jobs SET status = 'running' WHERE id = %s", (source["id"],))
    store.db.for_job(editor["id"]).execute(
        "UPDATE contacts SET notes = 'Updated by editor' WHERE id = %s", (contact["id"],),
    )
    assert store.db.fetch_one("SELECT has_new_context FROM jobs WHERE id = %s", (source["id"],))["has_new_context"]
    assert store.db.fetch_one(
        "SELECT object_id FROM workstream_attachments WHERE workstream_id = %s AND object_id = %s",
        (unrelated["workstream_id"], contact["object_id"]),
    ) is None
    change = store.db.fetch_one(
        """SELECT e.* FROM workstream_context_events c
           JOIN workstream_object_events e ON e.id = c.object_event_id
           WHERE c.workstream_id = %s AND e.object_id = %s AND e.event_type = 'UPDATE'
           ORDER BY e.id DESC LIMIT 1""", (source["workstream_id"], contact["object_id"]),
    )
    assert change["actor_workstream_id"] == editor["workstream_id"]
    assert change["changes"]["notes"] == {"before": "", "after": "Updated by editor"}


def test_contact_updates_require_current_object_version(store):
    source = job(store, "Versioned contact")
    contact = store.db.for_job(source["id"]).fetch_one(
        "INSERT INTO contacts(first_name) VALUES ('Ada') RETURNING id",
    )
    from assistant_agent.tools import ToolError, ToolRuntime
    runtime = ToolRuntime(store.db, store.config, source)
    initial = runtime.contact_read(contact["id"])["contact"]

    updated = runtime.run("contact_update", {
        "contact_id": contact["id"],
        "expected_version": initial["object_version"],
        "notes": "updated",
    })
    assert updated["contact"]["object_version"] > initial["object_version"]
    with pytest.raises(ToolError, match="object changed since it was read"):
        runtime.run("contact_update", {
            "contact_id": contact["id"],
            "expected_version": initial["object_version"],
            "notes": "stale overwrite",
        })
    assert runtime.contact_read(contact["id"])["contact"]["notes"] == "updated"


def test_workstream_read_exposes_main_and_system_threads_with_provenance(store):
    source = job(store, "Main conversation")
    RequestService(store.db).receive_request(IncomingRequest(
        "telegram", "update:main", "chat", "sender", source["thread_id"], "Main message",
    ), queue=False)
    with store.db.connect() as connection:
        connection.execute("SELECT set_config('assistant.parent_workstream_id', %s, true)",
                           (str(source["workstream_id"]),))
        connection.execute("SELECT set_config('assistant.thread_role', 'system', true)")
        system = connection.execute(
            "INSERT INTO jobs(thread_id, task_summary) VALUES ('system:test', 'System task') RETURNING *",
        ).fetchone()
    result = store.read(
        source["workstream_id"], include_system_threads=True, include_execution_details=True,
    )
    assert result["thread"]["thread_id"] == source["thread_id"]
    assert result["thread"]["is_main"]
    assert [item["thread_id"] for item in result["system_threads"]] == ["system:test"]
    assert system["workstream_id"] == source["workstream_id"]
    assert result["executions"][0]["thread_id"] == source["thread_id"]


def test_system_task_results_and_input_requests_are_available_from_main_workstream(store):
    source = job(store, "Research workstream")
    system_thread = "research:test:%s" % uuid.uuid4().hex
    with store.db.connect() as connection:
        connection.execute(
            "SELECT set_config('assistant.parent_workstream_id', %s, true)",
            (str(source["workstream_id"]),),
        )
        connection.execute("SELECT set_config('assistant.thread_role', 'system', true)")
        system_job = connection.execute(
            """INSERT INTO jobs(thread_id, task_summary, status)
               VALUES (%s, 'Research the request', 'completed') RETURNING *""",
            (system_thread,),
        ).fetchone()
    from assistant_agent.task_agent import TaskAgent
    agent = TaskAgent(store.db, store.config)
    agent.record_system_task_notification(
        system_job, "input_requested", "input:call-1", "Input requested", "Which date range?",
    )
    agent.record_final_response(system_job["id"], "The research result.", "Research completed")

    result = store.read(source["workstream_id"], include_system_threads=True)
    notifications = result["task_notifications"]
    assert [item["notification_type"] for item in notifications] == ["completed", "input_requested"]
    assert notifications[0]["content"] == "The research result."
    assert notifications[0]["source_thread_id"] == system_thread


def test_system_task_admin_input_is_available_from_main_workstream(store):
    source = job(store, "Research workstream")
    system_thread = "research:admin:%s" % uuid.uuid4().hex
    with store.db.connect() as connection:
        connection.execute(
            "SELECT set_config('assistant.parent_workstream_id', %s, true)",
            (str(source["workstream_id"]),),
        )
        connection.execute("SELECT set_config('assistant.thread_role', 'system', true)")
        system_job = connection.execute(
            """INSERT INTO jobs(thread_id, task_summary, status)
               VALUES (%s, 'Research the request', 'running') RETURNING *""",
            (system_thread,),
        ).fetchone()

    from assistant_agent.task_agent import TaskAgent
    agent = TaskAgent(store.db, store.config)
    with patch.object(agent, "email_admin_for_input"), patch.object(agent.memory_steward, "consolidate"):
        agent.handle_request_input(
            system_job, [], None, "request_admin_input",
            {"question": "Approve the proposed source?"}, "call-admin",
        )

    notification = store.db.fetch_one(
        """SELECT summary, content, source_thread_id FROM workstream_task_notifications
           WHERE source_job_id = %s AND notification_key = 'input:call-admin'""",
        (system_job["id"],),
    )
    assert notification == {
        "summary": "Admin input requested for Research the request",
        "content": "Approve the proposed source?",
        "source_thread_id": system_thread,
    }


@pytest.mark.parametrize("channel", ["email", "telegram"])
@pytest.mark.parametrize("system_reply", [False, True])
def test_research_system_thread_routes_guidance_and_returns_only_outcomes(store, channel, system_reply):
    from assistant_agent.deep_research import DeepResearchAgent, ResearchToolRuntime
    from assistant_agent.task_agent import TaskAgent
    from assistant_agent.telegram_ingest import TelegramReceiver
    from assistant_agent.tools import ToolRuntime
    from psycopg.types.json import Jsonb

    source = job(store, "Research request")
    store.config.values["agent"]["filesystem"]["require_mount"] = False
    route = ({"channel": "telegram", "chat_id": "123", "message_id": 1}
             if channel == "telegram" else {"channel": "email", "to": "user@example.com"})
    store.config.values["agent"]["memory"] = {"steward": {"enabled": False}}
    store.db.execute(
        "UPDATE jobs SET status = 'waiting', metadata = %s WHERE id = %s",
        (Jsonb({"reply_route": route, "source_channel": channel, "auto_reply": True}), source["id"]),
    )
    source = store.db.fetch_one("SELECT * FROM jobs WHERE id = %s", (source["id"],))
    with patch("assistant_agent.tools.search_configured", return_value=True):
        run = ToolRuntime(store.db, store.config, source).deep_research_request(
            research_question="Find suitable dates", title="Date research",
        )["deep_research_run"]
    agent = DeepResearchAgent(store.db, store.config)
    claimed = agent.claim_run()
    assert claimed["id"] == run["id"]
    execution = store.db.fetch_one("SELECT * FROM jobs WHERE id = %s", (run["execution_job_id"],))
    assert execution["workstream_id"] == source["workstream_id"]
    assert execution["thread_id"] != source["thread_id"]
    assert execution["status"] == "running"
    runtime = ResearchToolRuntime(store.db, store.config, claimed, execution, agent)
    runtime.run("file_write", {"path": "research.txt", "content": "Initial findings"})
    audit = store.db.fetch_one(
        """SELECT actor_job_id FROM workstream_object_events
           WHERE object_kind = 'file' ORDER BY id DESC LIMIT 1""",
    )
    assert audit["actor_job_id"] == execution["id"]

    with patch("assistant_agent.deep_research.ReplyRouter.send", return_value={"status": "sent"}) as send:
        assert agent.handle_tool_call(claimed, execution, [], {
            "id": "question-1", "function": {"name": "research_request_human_input",
                                            "arguments": '{"question":"Which month?"}'},
        })
    send.assert_called_once_with("Which month?", purpose="clarification")
    waiting = store.db.fetch_one("SELECT * FROM jobs WHERE id = %s", (execution["id"],))
    assert waiting["status"] == "waiting"
    assert waiting["metadata"]["waiting_for_input"]
    if channel == "telegram":
        receiver = TelegramReceiver.__new__(TelegramReceiver)
        receiver.db, receiver.bot_id, receiver.config = store.db, "bot", store.config
        receiver.set_selection("123", source["workstream_id"])
        assert receiver.task_thread(
            {"message_id": 2, "chat": {"id": 123}, "text": "November"}, "bot:123", "123",
        ) == (execution["thread_id"], [])
    reply_thread = execution["thread_id"] if system_reply else source["thread_id"]
    if channel == "email":
        store.db.execute(
            """INSERT INTO emails(message_id, thread_id, from_address, body_text, received_at)
               VALUES ('guidance:1', %s, 'user@example.com', 'November', now())""", (reply_thread,),
        )
    request = IncomingRequest(channel, "guidance:1", "conversation", "user", reply_thread, "November",
                              reply_route=route)
    RequestService(store.db).receive_request(request)
    RequestService(store.db).receive_request(request)
    assert store.db.fetch_one("SELECT status FROM jobs WHERE id = %s", (source["id"],))["status"] == "waiting"
    assert store.db.claim_job("task-worker") is None
    resumed = agent.claim_run()
    assert resumed["id"] == run["id"]
    assert resumed["execution_job_id"] == execution["id"]
    assert "November" in agent.build_messages(resumed, execution)[1]["content"]
    agent.log_event(run["id"], "search_result", output_data={"content": "Internal search transcript"})
    agent.complete_run(resumed, "November is suitable.", {"output_files": ["research.txt"]})
    assert store.db.fetch_one("SELECT status FROM jobs WHERE id = %s", (execution["id"],))["status"] == "completed"
    assert store.db.fetch_one("SELECT status FROM jobs WHERE id = %s", (source["id"],))["status"] == "queued"
    main = store.read(source["workstream_id"], include_system_threads=True)
    assert {item["source_job_id"] for item in main["task_notifications"]} == {execution["id"]}
    assert {item["notification_type"] for item in main["task_notifications"]} == {"input_requested", "completed"}
    instructions = store.db.fetch_one(
        "SELECT instruction FROM supervisor_instructions WHERE job_id = %s ORDER BY id DESC LIMIT 1",
        (source["id"],),
    )["instruction"]
    assert "Internal search transcript" not in instructions
    assert "research_context" not in TaskAgent(store.db, store.config).async_status_context(source)["deep_research_runs"][0]["result_data"]
    details = store.read(source["workstream_id"], thread_id=execution["thread_id"], include_execution_details=True)
    assert any(event["event_type"] == "search_result" for event in details["executions"][0]["research"]["events"])
    store.db.update_job_status(source["id"], "completed")
    remove(store, source["workstream_id"])
    assert store.db.fetch_one("SELECT id FROM deep_research_runs WHERE id = %s", (run["id"],)) is None
    assert store.db.fetch_one("SELECT id FROM jobs WHERE id = %s", (execution["id"],)) is None


@pytest.mark.parametrize("store", ["010_model_service.sql"], indirect=True)
def test_research_upgrade_preserves_existing_run_history_and_retaining_links(store, tmp_path):
    source, retainer = job(store, "Original research"), job(store, "Legacy retainer")
    run = store.db.for_job(source["id"]).fetch_one(
        """INSERT INTO deep_research_runs(original_job_id, original_thread_id, title, research_question,
                                          status, tool_call_count)
           VALUES (%s, %s, 'Existing run', 'Existing question', 'waiting_for_input', 4) RETURNING *""",
        (source["id"], source["thread_id"]),
    )
    store.db.execute(
        """INSERT INTO deep_research_events(run_id, sequence, event_type, output_data)
           VALUES (%s, 1, 'search_result', '{"content":"Existing evidence"}')""", (run["id"],),
    )
    store.db.execute(
        "INSERT INTO workstream_attachments(workstream_id, object_id) VALUES (%s, %s)",
        (retainer["workstream_id"], source["object_id"]),
    )
    store.db.execute(
        "INSERT INTO workstream_attachments(workstream_id, object_id) VALUES (%s, %s)",
        (retainer["workstream_id"], run["object_id"]),
    )
    migrations = tmp_path / "research-migration"
    migrations.mkdir()
    source_path = Path(__file__).resolve().parents[1] / "src/assistant_agent/migrations/011_research_system_threads.sql"
    shutil.copy2(source_path, migrations)
    assert store.db.run_migrations(migrations) == [source_path.name]
    assert store.db.run_migrations(migrations) == []
    upgraded = store.db.fetch_one("SELECT * FROM deep_research_runs WHERE id = %s", (run["id"],))
    execution = store.db.fetch_one("SELECT * FROM jobs WHERE id = %s", (upgraded["execution_job_id"],))
    assert upgraded["tool_call_count"] == 4
    assert upgraded["object_id"] == run["object_id"]
    assert execution["metadata"]["waiting_for_input"]
    assert execution["workstream_id"] == source["workstream_id"]
    assert store.db.fetch_one(
        "SELECT 1 FROM workstream_attachments WHERE object_id = %s AND workstream_id = %s",
        (execution["object_id"], retainer["workstream_id"]),
    )
    assert store.db.fetch_one("SELECT count(*) AS n FROM deep_research_events WHERE run_id = %s", (run["id"],))["n"] == 1


@pytest.mark.parametrize("interrupt", [None, "context", "cancel"])
def test_research_worker_preserves_completed_effects_and_defers_stale_calls(store, interrupt):
    from assistant_agent.deep_research import DeepResearchAgent, ResearchToolRuntime

    source = job(store, "Research request")
    contact = store.db.for_job(source["id"]).fetch_one(
        "INSERT INTO contacts(first_name) VALUES ('Ada') RETURNING id",
    )
    store.config.values["agent"]["filesystem"]["require_mount"] = False
    store.config.values["agent"]["memory"] = {"steward": {"enabled": False}}
    store.db.update_job_status(source["id"], "waiting")
    run = store.db.for_job(source["id"]).fetch_one(
        """INSERT INTO deep_research_runs(original_job_id, original_thread_id, title, research_question)
           VALUES (%s, %s, 'Research', 'Find the answer') RETURNING *""",
        (source["id"], source["thread_id"]),
    )
    original_run = ResearchToolRuntime.run

    def run_tool(runtime, name, arguments):
        result = original_run(runtime, name, arguments)
        if interrupt == "context" and arguments.get("path") == "first.txt":
            store.db.for_job(source["id"]).execute(
                "UPDATE contacts SET notes = 'New requirements' WHERE id = %s", (contact["id"],),
            )
        return result

    def response(messages, tools):
        if model.chat.call_count == 1:
            if interrupt == "cancel":
                store.db.update_job_status(source["id"], "cancelled")
            calls = [
                {"id": path, "function": {"name": "file_write",
                                         "arguments": json.dumps({"path": path, "content": "Evidence"})}}
                for path in ("first.txt", "second.txt")
            ]
        else:
            if interrupt == "context":
                assert any("New workstream object changes" in str(item.get("content")) for item in messages)
            calls = [{"id": "complete", "function": {"name": "research_complete",
                                                    "arguments": '{"summary":"Research finished"}'}}]
        return {"choices": [{"message": {"role": "assistant", "tool_calls": calls}}]}

    with patch("assistant_agent.deep_research.search_configured", return_value=True), \
         patch("assistant_agent.deep_research.LlmClient") as llm, \
         patch.object(ResearchToolRuntime, "run", run_tool):
        model = llm.return_value
        model.chat.side_effect = response
        assert DeepResearchAgent(store.db, store.config).run_once()
    root = Path(store.config.get("agent.filesystem.shared_root"))
    assert (root / "first.txt").exists() == (interrupt != "cancel")
    assert (root / "second.txt").exists() == (interrupt is None)
    result = store.db.fetch_one("SELECT * FROM deep_research_runs WHERE id = %s", (run["id"],))
    assert result["status"] == ("cancelled" if interrupt == "cancel" else "completed")
    assert result["tool_call_count"] == (0 if interrupt == "cancel" else 1 if interrupt == "context" else 2)


def test_recall_prioritizes_owned_memories_before_global_candidate_limits(store):
    from assistant_agent.memory_manager import MemorySteward

    source, other = job(store, "Astronomy"), job(store, "Other astronomy")
    own = memory(store, source, "Astronomy preference for dark skies")
    irrelevant = memory(store, source, "Unrelated cooking instructions")
    for index in range(6):
        item = memory(store, other, "Astronomy external observation %s" % index)
        store.db.execute("UPDATE agent_memories SET pinned = true, importance = 5 WHERE id = %s", (item["id"],))
    store.config.values["agent"]["memory"] = {"steward": {"max_injected_memories": 2}}
    steward = MemorySteward(store.db, store.config)
    candidates = steward.retrieve_memory_candidates(source, [], None, [])
    assert candidates[0]["id"] == own["id"]
    assert candidates[0]["origin_workstream_id"] == source["workstream_id"]
    assert len(candidates) == 2
    assert candidates[1]["origin_workstream_id"] == other["workstream_id"]
    assert irrelevant["id"] not in {item["id"] for item in candidates}


@pytest.mark.parametrize("source_status", ["waiting", "needs_review"])
def test_research_completion_and_main_thread_notification_commit_together(store, source_status):
    from assistant_agent.deep_research import DeepResearchAgent

    source = job(store, "Research request")
    store.db.update_job_status(source["id"], source_status, last_error="Original review reason")
    store.db.for_job(source["id"]).execute(
        """INSERT INTO deep_research_runs(original_job_id, original_thread_id, title, research_question)
           VALUES (%s, %s, 'Research', 'Find the answer')""", (source["id"], source["thread_id"]),
    )
    agent = DeepResearchAgent(store.db, store.config)
    run = agent.claim_run()
    with patch.object(DeepResearchAgent, "notify_original_job", side_effect=RuntimeError("notification failed")):
        with pytest.raises(RuntimeError, match="notification failed"):
            agent.complete_run(run, "The answer", {})
    assert agent.current_run(run["id"])["status"] == "running"
    assert store.db.fetch_one("SELECT status FROM jobs WHERE id = %s", (run["execution_job_id"],))["status"] == "running"
    assert not store.db.fetch_all("SELECT id FROM workstream_task_notifications")
    agent.complete_run(run, "The answer", {})
    agent.complete_run(run, "The answer", {})
    agent.handle_run_exception(run, "post-completion log failure")
    assert agent.current_run(run["id"])["status"] == "completed"
    original = store.db.fetch_one("SELECT status, last_error FROM jobs WHERE id = %s", (source["id"],))
    assert original["status"] == ("queued" if source_status == "waiting" else "needs_review")
    if source_status == "needs_review":
        assert original["last_error"] == "Original review reason"
    assert len(store.db.fetch_all("SELECT id FROM workstream_task_notifications")) == 1
    assert len(store.db.fetch_all("SELECT id FROM supervisor_instructions WHERE job_id = %s", (source["id"],))) == 1


def test_uncertain_research_guidance_stays_paused_until_explicit_admin_requeue(store):
    from assistant_agent.deep_research import DeepResearchAgent
    from assistant_agent.tools import ToolError
    from psycopg.types.json import Jsonb

    source = job(store, "Research request")
    route = {"channel": "telegram", "chat_id": "123"}
    store.db.execute("UPDATE jobs SET status = 'waiting', metadata = %s WHERE id = %s",
                     (Jsonb({"reply_route": route, "auto_reply": True}), source["id"]))
    store.db.for_job(source["id"]).execute(
        """INSERT INTO deep_research_runs(original_job_id, original_thread_id, title, research_question)
           VALUES (%s, %s, 'Research', 'Find the answer')""", (source["id"], source["thread_id"]),
    )
    agent = DeepResearchAgent(store.db, store.config)
    run = agent.claim_run()
    execution = store.db.fetch_one("SELECT * FROM jobs WHERE id = %s", (run["execution_job_id"],))
    with patch("assistant_agent.deep_research.ReplyRouter.send", return_value={"status": "uncertain"}):
        with pytest.raises(ToolError, match="requires attention") as error:
            agent.handle_tool_call(run, execution, [], {
                "id": "question", "function": {"name": "research_request_human_input",
                                              "arguments": '{"question":"Which month?"}'},
            })
    agent.handle_run_exception(run, str(error.value))
    assert agent.current_run(run["id"])["status"] == "waiting_for_input"
    RequestService(store.db).receive_request(IncomingRequest(
        "telegram", "guidance", "chat", "123", execution["thread_id"], "November", reply_route=route,
    ))
    assert store.db.fetch_one("SELECT status FROM jobs WHERE id = %s", (execution["id"],))["status"] == "needs_review"
    assert agent.claim_run() is None
    assert store.db.requeue_job(execution["id"], "delivery reconciled by administrator")
    assert store.db.claim_job("task-worker") is None
    resumed = agent.claim_run()
    assert resumed["id"] == run["id"]
    store.db.update_job_status(execution["id"], "cancelled", last_error="Cancelled by administrator")
    assert agent.current_run(run["id"])["status"] == "cancelled"
    assert store.db.fetch_one("SELECT status FROM jobs WHERE id = %s", (source["id"],))["status"] == "queued"
    assert store.db.fetch_one(
        """SELECT content FROM workstream_task_notifications
           WHERE source_job_id = %s AND notification_key = 'final'""", (execution["id"],),
    )["content"] == "Cancelled by administrator"


def test_workstreams_remain_bound_to_their_originating_channel(store):
    from assistant_agent.email_ingest import EmailDownloader
    from assistant_agent.telegram_ingest import TelegramReceiver

    telegram_request = RequestService(store.db).receive_request(IncomingRequest(
        "telegram", "tg:1", "tg:chat", "sender", "telegram:main", "Telegram request",
    ), queue=False)
    email_request = RequestService(store.db).receive_request(IncomingRequest(
        "email", "<email-1@example.com>", "email:thread", "sender@example.com",
        "email:thread", "Email request",
    ), queue=False)
    assert telegram_request["workstream_id"] != email_request["workstream_id"]
    assert store.db.fetch_one(
        "SELECT origin_channel FROM workstreams WHERE id = %s",
        (telegram_request["workstream_id"],),
    )["origin_channel"] == "telegram"
    assert store.db.fetch_one(
        "SELECT origin_channel FROM workstreams WHERE id = %s",
        (email_request["workstream_id"],),
    )["origin_channel"] == "email"

    receiver = TelegramReceiver.__new__(TelegramReceiver)
    receiver.db, receiver.bot_id = store.db, "bot"
    receiver.config = AppConfig({"agent": {"telegram": {"recipients": {
        "sender@example.com": "sender",
    }}}})
    visible = receiver.visible_workstreams("sender")
    assert {row["id"] for row in visible} == {
        telegram_request["workstream_id"], email_request["workstream_id"],
    }
    receiver.set_selection("sender", email_request["workstream_id"])
    assert receiver.task_thread(
        {"message_id": 25, "chat": {"id": "sender"}, "text": "Continue this workstream"},
        "bot:sender", "sender",
    )[0] == email_request["thread_id"]

    store.db.execute(
        """INSERT INTO emails(message_id, thread_id, from_address, to_addresses, subject, body_text, received_at)
           VALUES ('<telegram-sent@example.com>', 'telegram:main', 'agent@example.com',
                   ARRAY['sender@example.com'], 'Telegram workstream message', 'sent', now())""",
    )
    ingestor = EmailDownloader.__new__(EmailDownloader)
    ingestor.db, ingestor.config = store.db, AppConfig({"agent": {"email": {}}})
    assert ingestor.resolve_thread_id(
        "<email-reply@example.com>", "<telegram-sent@example.com>", [],
        "Re: Telegram workstream message", ["sender@example.com"],
    ) == "<email-reply@example.com>"

    outbound_job = store.db.fetch_one(
        "INSERT INTO jobs(thread_id, task_summary) VALUES ('telegram:main', 'Send the requested email') RETURNING id",
    )
    store.db.execute(
        """INSERT INTO outbound_email_logs(
             job_id, to_addresses, subject, body_text, provider_message_id, status, sent_at
           ) VALUES (%s, ARRAY['sender@example.com'], 'Requested details', 'Details', '<agent-email@example.com>',
             'sent', now())""",
        (outbound_job["id"],),
    )
    reply_thread = ingestor.resolve_thread_id(
        "<email-response@example.com>", "<agent-email@example.com>", [],
        "Re: Requested details", ["sender@example.com"],
    )
    assert reply_thread == "telegram:main"
    email_reply = RequestService(store.db).receive_request(IncomingRequest(
        "email", "<email-response@example.com>", reply_thread, "sender@example.com",
        reply_thread, "Here is my response",
    ), queue=False)
    assert email_reply["workstream_id"] == telegram_request["workstream_id"]
    assert store.db.fetch_one(
        "SELECT origin_channel FROM workstreams WHERE id = %s",
        (email_reply["workstream_id"],),
    )["origin_channel"] == "telegram"
    store.db.execute(
        """INSERT INTO emails(message_id, in_reply_to, thread_id, from_address, to_addresses, subject, body_text, received_at)
           VALUES ('<email-follow-up@example.com>', '<email-response@example.com>', 'telegram:main',
                   'sender@example.com', ARRAY['agent@example.com'], 'Re: Requested details', 'Follow-up', now())""",
    )
    assert ingestor.resolve_thread_id(
        "<email-follow-up-reply@example.com>", "<email-follow-up@example.com>", [],
        "Re: Requested details", ["sender@example.com"],
    ) == "telegram:main"


def test_calendar_create_reconciles_provider_success_after_lost_response(store):
    from types import SimpleNamespace

    from assistant_agent.calendar.models import CalendarEvent
    from assistant_agent.calendar.provider import CalendarError
    from assistant_agent.calendar.router import CalendarRouter

    source = job(store, "Calendar recovery")

    class Provider:
        def __init__(self):
            self.event = None
            self.create_calls = 0

        def create_event(self, request):
            self.create_calls += 1
            self.event = CalendarEvent(
                provider_event_id="provider-event-1",
                provider_name="google",
                calendar_id=request.calendar_id,
                title=request.title,
                start=request.start,
                end=request.end,
                all_day=request.all_day,
                description=request.description,
                location=request.location,
                transparency=request.transparency,
            )
            raise CalendarError("response lost after provider accepted create")

        def list_events(self, calendar_id, start, end):
            return [self.event] if self.event and self.event.start < end and self.event.end > start else []

    provider = Provider()
    router = CalendarRouter.__new__(CalendarRouter)
    router._db = store.db.for_job(source["id"])
    router._job = source
    router._config = store.config
    router._providers = {"google": provider}
    router._provider_configs = {"google": SimpleNamespace(calendar_id="primary")}
    router._default_provider_name = "google"
    router._default_alert_minutes = 30

    request = {
        "title": "Project review",
        "start": "2026-10-28T14:00:00+00:00",
        "end": "2026-10-28T15:00:00+00:00",
    }
    with pytest.raises(CalendarError, match="outcome is uncertain"):
        router.create_event(**request)
    result = router.create_event(**request)
    assert provider.create_calls == 1
    assert result["event"]["title"] == "Project review"
    mutation = store.db.fetch_one(
        "SELECT status FROM provider_mutations WHERE resource_type = 'calendar_event' AND operation = 'CREATE'",
    )
    assert mutation["status"] == "completed"


def test_zoom_update_reconciles_after_provider_response_and_state_read_are_lost(store):
    from assistant_agent.zoom_meetings import ZoomError, ZoomMeetings

    source = job(store, "Zoom recovery")
    row = store.db.fetch_one(
        """INSERT INTO zoom_managed_meetings(request_key, request_payload, title, starts_at, duration_minutes,
           provider_meeting_id, created_by_job_id, status)
           VALUES ('zoom-recovery', '{}', 'Initial title', '2026-10-28T14:00:00Z', 30, 'zoom-1', %s, 'active')
           RETURNING *""",
        (source["id"],),
    )
    service = ZoomMeetings.__new__(ZoomMeetings)
    service.db, service.config, service.job = store.db.for_job(source["id"]), store.config, source
    remote = {"topic": "Initial title", "start_time": "2026-10-28T14:00:00Z", "duration": 30}
    get_calls = 0

    def request(method, path, payload=None, allow_not_found=False):
        nonlocal remote, get_calls
        if method == "GET":
            get_calls += 1
            if get_calls == 2:
                raise ZoomError("provider state temporarily unavailable", uncertain=True)
            return dict(remote)
        if method == "PATCH":
            remote = {
                "topic": payload.get("topic", remote["topic"]),
                "start_time": payload.get("start_time", remote["start_time"]),
                "duration": payload.get("duration", remote["duration"]),
            }
            raise ZoomError("update response lost", uncertain=True)
        raise AssertionError("unexpected provider request")

    service.request = request
    update = {"title": "Updated title", "duration_minutes": 45}
    with pytest.raises(ZoomError, match="state could not be read"):
        service.update(row["id"], **update)
    result = service.update(row["id"], **update)
    assert get_calls == 3
    assert result["title"] == "Updated title"
    assert result["duration_minutes"] == 45
    mutation = store.db.fetch_one(
        "SELECT status FROM provider_mutations WHERE resource_type = 'zoom_meeting' AND operation = 'UPDATE'",
    )
    assert mutation["status"] == "completed"


def test_zoom_delete_reconciles_lost_provider_response(store):
    from assistant_agent.zoom_meetings import ZoomError, ZoomMeetings

    source = job(store, "Zoom deletion")
    row = store.db.fetch_one(
        """INSERT INTO zoom_managed_meetings(request_key, request_payload, title, starts_at, duration_minutes,
           provider_meeting_id, created_by_job_id, status)
           VALUES ('zoom-delete', '{}', 'Meeting', '2026-10-28T14:00:00Z', 30, 'zoom-2', %s, 'active')
           RETURNING *""",
        (source["id"],),
    )
    service = ZoomMeetings.__new__(ZoomMeetings)
    service.db, service.config, service.job = store.db.for_job(source["id"]), store.config, source
    remote = {"topic": "Meeting", "start_time": "2026-10-28T14:00:00Z", "duration": 30}

    def request(method, path, payload=None, allow_not_found=False):
        nonlocal remote
        if method == "GET":
            return dict(remote) if remote else {"not_found": True}
        if method == "DELETE":
            remote = None
            raise ZoomError("delete response lost", uncertain=True)
        raise AssertionError("unexpected provider request")

    service.request = request
    result = service.delete(row["id"])
    assert result["status"] == "deleted"
    mutation = store.db.fetch_one(
        "SELECT status FROM provider_mutations WHERE resource_type = 'zoom_meeting' AND operation = 'DELETE'",
    )
    assert mutation["status"] == "completed"


def test_deletion_leaves_only_hashed_reply_tombstones_and_invalidates_selection(store):
    source = job(store, "Deleted conversation")
    email = store.db.fetch_one("SELECT message_id FROM emails WHERE id = %s", (source["trigger_email_id"],))
    RequestService(store.db).receive_request(IncomingRequest(
        "telegram", "bot:chat:42", "bot:chat", "sender", source["thread_id"], "Telegram reply target",
    ), queue=False)
    store.db.execute(
        """INSERT INTO telegram_workstream_selections(bot_id, sender_id, workstream_id)
           VALUES ('bot', 'sender', %s)""", (source["workstream_id"],),
    )
    remove(store, source["workstream_id"])
    selection = store.db.fetch_one(
        "SELECT workstream_id, unavailable_workstream_id FROM telegram_workstream_selections",
    )
    assert selection == {"workstream_id": None, "unavailable_workstream_id": source["workstream_id"]}
    from assistant_agent.email_ingest import EmailDownloader
    ingestor = EmailDownloader.__new__(EmailDownloader)
    ingestor.db = store.db
    assert ingestor.deleted_reply_workstream_id(email["message_id"], []) == source["workstream_id"]
    assert store.db.fetch_one(
        "SELECT reference_hash FROM deleted_thread_references",
    )["reference_hash"] != email["message_id"]
    from assistant_agent.telegram_ingest import TelegramReceiver
    receiver = TelegramReceiver.__new__(TelegramReceiver)
    receiver.db, receiver.bot_id = store.db, "bot"
    assert receiver.deleted_reply_workstream_id({
        "chat": {"id": "chat"}, "reply_to_message": {"message_id": 42},
    }) == source["workstream_id"]


def test_real_file_tools_preserve_object_identity_on_move_and_track_trash(store):
    from assistant_agent.tools import ToolRuntime
    a, b = job(store), job(store)
    runtime = ToolRuntime(store.db, store.config, a)
    runtime.file_write("report.txt", "report bytes")
    root = Path(store.config.get("agent.filesystem.shared_root"))
    original = store.db.fetch_one("SELECT * FROM workstream_files WHERE path = %s", (str(root / "report.txt"),))
    assert original["sha256"] == hashlib.sha256(b"report bytes").hexdigest()
    store.attach(b["workstream_id"], "file", original["id"])
    runtime.file_move("report.txt", "moved.txt")
    moved = store.db.fetch_one("SELECT * FROM workstream_files WHERE path = %s", (str(root / "moved.txt"),))
    assert moved["object_id"] == original["object_id"]
    remove(store, a["workstream_id"])
    assert (root / "moved.txt").exists()
    remove(store, b["workstream_id"])
    assert not (root / "moved.txt").exists()


def test_idempotent_inserts_do_not_create_orphan_provenance(store):
    source = job(store)
    store.db.execute(
        """INSERT INTO jobs(thread_id, status) VALUES (%s, 'queued') ON CONFLICT DO NOTHING""",
        (source["thread_id"],),
    )
    store.db.execute(
        """INSERT INTO dropbox_transfers(request_key, job_id, direction, remote_path)
           VALUES ('retry', %s, 'export', '/Outbox/file.txt') ON CONFLICT DO NOTHING""", (source["id"],),
    )
    store.db.execute(
        """INSERT INTO dropbox_transfers(request_key, job_id, direction, remote_path)
           VALUES ('retry', %s, 'export', '/Outbox/file.txt') ON CONFLICT DO NOTHING""", (source["id"],),
    )
    objects = store.objects(source["workstream_id"])
    assert len([o for o in objects if o["kind"] == "job"]) == 1
    assert len([o for o in objects if o["kind"] == "dropbox_transfer"]) == 1


def test_import_attachment_retains_files_and_deletes_them_with_last_owner(store):
    a, b = job(store), job(store)
    path = Path(store.config.get("agent.filesystem.shared_root")) / "import.txt"
    path.write_text("imported bytes")
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    file = store.db.fetch_one(
        "INSERT INTO workstream_files(path, sha256, source_job_id) VALUES (%s, %s, %s) RETURNING *",
        (str(path), checksum, a["id"]),
    )
    store.db.execute(
        """INSERT INTO dropbox_transfers(request_key, job_id, direction, remote_path, local_path, local_sha256, status)
           VALUES ('import', %s, 'import', '/Inbox/original.txt', %s, %s, 'ready')""", (a["id"], str(path), checksum),
    )
    legacy_attach(store, b["workstream_id"], "dropbox_transfer", "import")
    assert file["object_id"] in {o["id"] for o in store.objects(b["workstream_id"])}
    remove(store, a["workstream_id"])
    assert path.exists()
    assert store.db.fetch_one("SELECT id FROM workstream_files WHERE id = %s", (file["id"],))
    with patch("assistant_agent.dropbox_provider.DropboxClient") as provider:
        remove(store, b["workstream_id"])
        provider.assert_not_called()
    assert not path.exists()


def test_queued_shared_reminder_blocks_deletion_that_would_strand_its_work(store):
    a, b = job(store), job(store)
    reminder = store.db.fetch_one(
        """INSERT INTO reminders(title, task, run_at, created_by_job_id, job_id, status)
           VALUES ('Queued reminder', 'Task', now(), %s, %s, 'queued') RETURNING *""", (a["id"], a["id"]),
    )
    legacy_attach(store, b["workstream_id"], "reminder", reminder["id"])
    preview = store.preview(a["workstream_id"])
    assert preview["blocked_reminders"] == [reminder["id"]]
    with pytest.raises(WorkstreamError, match="shared reminders"):
        store.begin_delete(a["workstream_id"], "preserve_shared", preview["preview_digest"])


def test_sharing_calendar_retains_linked_zoom_and_cleanup_has_no_extra_provider_effects(store):
    a, b = job(store), job(store)
    calendar = store.db.fetch_one(
        """INSERT INTO calendar_managed_events(assistant_id, provider_name, provider_event_id, calendar_id,
           starts_at, ends_at, created_by_job_id) VALUES ('event', 'test', 'remote-event', 'primary',
           now(), now() + interval '30 minutes', %s) RETURNING *""", (a["id"],),
    )
    meeting = store.db.fetch_one(
        """INSERT INTO zoom_managed_meetings(request_key, request_payload, title, starts_at, duration_minutes,
           provider_meeting_id, calendar_event_id, created_by_job_id, status)
           VALUES ('zoom', '{}', 'Meeting', now(), 30, '12345', 'event', %s, 'active') RETURNING *""", (a["id"],),
    )
    legacy_attach(store, b["workstream_id"], "calendar_event", calendar["assistant_id"])
    assert meeting["object_id"] in {o["id"] for o in store.objects(b["workstream_id"])}
    with patch("assistant_agent.calendar.router.CalendarRouter") as cal, patch("assistant_agent.zoom_meetings.ZoomMeetings") as zoom:
        remove(store, a["workstream_id"])
        cal.assert_not_called()
        zoom.assert_not_called()
        remove(store, b["workstream_id"])
        cal.return_value._get_provider.return_value.delete_event.assert_called_once_with("primary", "remote-event")
        zoom.return_value.request.assert_called_once_with("DELETE", "meetings/12345")


def test_operator_json_preview_and_confirmed_delete_joined_flow(store, capsys):
    from assistant_agent import workstream_operator
    source = job(store)
    memory(store, source)

    def invoke(action, payload):
        with patch.object(workstream_operator, "load_config", return_value=store.config), \
             patch.object(workstream_operator, "database_url", return_value=store.db.database_url), \
             patch("sys.argv", ["workstream_operator", action]), \
             patch("sys.stdin", io.StringIO(json.dumps(payload))):
            workstream_operator.main()
        return json.loads(capsys.readouterr().out)

    preview = invoke("preview", {"workstream_id": source["workstream_id"], "mode": "preserve_shared"})
    assert preview["deleted_counts"]["agent_memories"] == 1
    payload = {"workstream_id": source["workstream_id"], "mode": "preserve_shared",
               "preview_digest": preview["preview_digest"], "confirmed": True, "writers_stopped": True}
    assert invoke("delete", payload)["status"] == "completed"
    assert invoke("pending", {}) == {"deletions": []}
