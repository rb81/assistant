import hashlib
import io
import json
import os
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
def store(tmp_path):
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


def remove(store, identifier, mode="preserve_shared"):
    preview = store.preview(identifier, mode)
    deletion = store.begin_delete(identifier, mode, preview["preview_digest"])
    return store.execute_delete(deletion)


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
    with pytest.raises(WorkstreamError, match="messages cannot"):
        store.attach(other["workstream_id"], "request", followup["id"])
    with pytest.raises(WorkstreamError, match="messages cannot"):
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
    store.attach(b["workstream_id"], "memory", shared["id"])
    store.attach(c["workstream_id"], "memory", bridge["id"])
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
    store.attach(b["workstream_id"], "memory", owned["id"])
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
    store.attach(b["workstream_id"], "research", run["id"])
    store.attach(b["workstream_id"], "reminder", reminder["id"])
    remove(store, a["workstream_id"])
    assert store.db.fetch_one("SELECT workstream_id FROM jobs WHERE id = %s", (a["id"],))["workstream_id"] == b["workstream_id"]
    current = store.db.fetch_one("SELECT * FROM reminders WHERE id = %s", (reminder["id"],))
    with store.db.connect() as connection, connection.cursor() as cur:
        spawned = ReminderScheduler(store.db, store.config).create_job_for_reminder(cur, current)
    stream = store.db.fetch_one("SELECT * FROM workstreams WHERE id = %s", (spawned["workstream_id"],))
    assert stream["parent_id"] == b["workstream_id"]


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
    store.attach(b["workstream_id"], "artifact", artifact["id"])
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
        store.attach(other["workstream_id"], "artifact", artifact["id"])
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
    store.attach(b["workstream_id"], "dropbox_transfer", "import")
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
    store.attach(b["workstream_id"], "reminder", reminder["id"])
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
    store.attach(b["workstream_id"], "calendar_event", calendar["assistant_id"])
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
