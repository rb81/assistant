import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from assistant_agent.config import AppConfig
from assistant_agent.management_domains import catalog, execute
from assistant_agent.tools import ToolError
from test_network_access import db  # noqa: F401


def mutate(db, config, action, **arguments):
    return execute(db, config, {"action": action, "arguments": arguments}, mutate=True)


def test_contact_management_requires_current_versions_and_rejects_unknown_arguments(db):
    config = AppConfig({})
    created = mutate(db, config, "contacts.create", first_name="Alice", email_address="alice@test.invalid")
    identifier = created["contact"]["id"]
    assert execute(db, config, {"action": "contacts.search", "arguments": {"query": "Alice"}})["contacts"][0]["id"] == identifier
    read = execute(db, config, {"action": "contacts.read", "arguments": {"contact_id": identifier}})["contact"]
    with pytest.raises(ValidationError):
        mutate(db, config, "contacts.update", contact_id=identifier, first_name="Bob")
    updated = mutate(db, config, "contacts.update", contact_id=identifier, first_name="Bob",
                     expected_version=read["object_version"])
    assert updated["contact"]["first_name"] == "Bob"
    with pytest.raises(ToolError):
        mutate(db, config, "contacts.delete", contact_id=identifier, expected_version=read["object_version"])
    with pytest.raises(ValidationError):
        mutate(db, config, "contacts.create", unsupported="field")
    fresh = execute(db, config, {"action": "contacts.read", "arguments": {"contact_id": identifier}})["contact"]
    mutate(db, config, "contacts.delete", contact_id=identifier, expected_version=fresh["object_version"])
    with pytest.raises(ToolError, match="not found"):
        execute(db, config, {"action": "contacts.read", "arguments": {"contact_id": identifier}})


def test_workspace_binary_chunks_preserve_versions_and_reject_symlinks(db, tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    config = AppConfig({"agent": {"filesystem": {"shared_root": str(root)}}})
    with patch("assistant_agent.artifact_processor.ArtifactProcessor.scan_file", return_value={"status": "clean"}), \
            patch("assistant_agent.tools.ToolRuntime.normalize_and_index_path"):
        mutate(db, config, "workspace.mkdir", path="folder")
        created = mutate(db, config, "workspace.write", path="sample.bin",
                         content_base64=base64.b64encode(b"\x00\xff").decode())
        result = execute(db, config, {"action": "workspace.read", "arguments": {"path": "sample.bin"}})
        assert base64.b64decode(result["content_base64"]) == b"\x00\xff"
        assert result["sha256"] == created["sha256"]
        with pytest.raises(ValueError, match="changed"):
            mutate(db, config, "workspace.write", path="sample.bin", content_base64="YQ==", expected_sha256="0" * 64)
        appended = mutate(db, config, "workspace.write", path="sample.bin", content_base64="YQ==",
                          expected_sha256=result["sha256"], offset=2)
        assert appended["size_bytes"] == 3
        assert (root / "sample.bin").read_bytes() == b"\x00\xffa"
        (root / "link.bin").symlink_to(root / "sample.bin")
        with pytest.raises(ValueError, match="symlink"):
            execute(db, config, {"action": "workspace.read", "arguments": {"path": "link.bin"}})
        with pytest.raises((ValueError, ToolError)):
            execute(db, config, {"action": "workspace.read", "arguments": {"path": "../outside"}})
        listing = execute(db, config, {"action": "workspace.list"})
        assert "link.bin" not in [row["path"] for row in listing["entries"]]


def test_projects_have_discoverable_strict_schemas_and_explicit_lifecycle(db):
    from assistant_agent.management_operator import execute as manage
    config = AppConfig({})
    actions = {row["action"]: row for row in catalog()["actions"]}
    assert "title" in actions["projects.create"]["arguments"]["required"]
    with pytest.raises(ValidationError):
        mutate(db, config, "projects.create", title="Project", ignored=True)
    identifier = mutate(db, config, "projects.create", title="Project")["project_id"]
    mutate(db, config, "projects.rename", project_id=identifier, title="Renamed")
    assert any(row["id"] == identifier for row in manage(db, config, {"kind": "projects"})["rows"])
    assert manage(db, config, {"kind": "projects", "id": identifier})["rows"][0]["title"] == "Renamed"
    mutate(db, config, "projects.archive", project_id=identifier)
    assert db.fetch_one("SELECT status FROM workstreams WHERE id = %s", (identifier,))["status"] == "archived"
    mutate(db, config, "projects.restore", project_id=identifier)
    assert db.fetch_one("SELECT title, status FROM workstreams WHERE id = %s", (identifier,)) == {
        "title": "Renamed", "status": "active",
    }


def test_stored_email_remains_readable_with_intake_disabled(db):
    config = AppConfig({"agent": {"email": {"enabled": False}}})
    project = db.fetch_one("SELECT ensure_workstream('stored-email', 'Email history') AS id")["id"]
    row = db.fetch_one(
        """INSERT INTO emails(message_id, thread_id, from_address, subject, body_text, received_at, workstream_id)
           VALUES ('stored@example.test', 'stored-thread', 'sender@example.test', 'Saved subject',
                   'Saved body', now(), %s) RETURNING id""", (project,),
    )
    results = execute(db, config, {"action": "email.search", "arguments": {"query": "Saved subject"}})
    assert results["emails"][0]["id"] == row["id"]
    result = execute(db, config, {"action": "email.read", "arguments": {"email_id": row["id"]}})
    assert result["email"]["body_text"] == "Saved body"


def test_reminders_keep_client_routes_and_prevent_archiving_active_schedules(db):
    from assistant_agent.client_channels import ClientChannels
    config = AppConfig({})
    identifier = "d" * 32
    ClientChannels(db, config).register(identifier, "Desktop", "desktop", [])
    project = mutate(db, config, "projects.create", title="Scheduled project")["project_id"]
    context = {"client_id": identifier, "workstream_id": project}
    created = execute(db, config, {"action": "reminders.create", **context, "arguments": {
        "title": "Check in", "task": "Hello", "run_at": "2099-01-01T12:00:00Z", "notification_only": True,
    }}, mutate=True)["reminder"]
    assert created["metadata"]["delivery"]["primary"] == {"channel": "client", "client_id": identifier}
    with pytest.raises(ValueError, match="reminders"):
        mutate(db, config, "projects.archive", project_id=project)
    updated = execute(db, config, {"action": "reminders.update", **context, "arguments": {
        "reminder_id": created["id"], "title": "New title", "expected_version": created["object_version"],
    }}, mutate=True)["reminder"]
    assert updated["title"] == "New title"
    execute(db, config, {"action": "reminders.cancel", **context, "arguments": {
        "reminder_id": created["id"], "expected_version": updated["object_version"],
    }}, mutate=True)
    mutate(db, config, "projects.archive", project_id=project)


def test_memory_management_uses_admin_authority_without_fabricating_a_task(db):
    config = AppConfig({"agent": {"embeddings": {"enabled": False}}})
    created = mutate(db, config, "memory.remember", content="Use metric units", metadata={
        "explicit_user_requested": True,
    })["memory"]
    memories = execute(db, config, {"action": "memory.search"})["memories"]
    current = next(row for row in memories if row["id"] == created["id"])
    updated = mutate(db, config, "memory.update", memory_id=created["id"], content="Use SI units",
                     expected_version=current["object_version"])["memory"]
    with pytest.raises(ToolError):
        mutate(db, config, "memory.update", memory_id=created["id"], pinned=True,
               expected_version=current["object_version"])
    deleted = mutate(db, config, "memory.forget", memory_id=created["id"], expected_version=updated["object_version"])
    assert deleted["deleted"]["id"] == created["id"]
    assert not db.fetch_one("SELECT id FROM agent_memories WHERE id = %s", (created["id"],))


def test_zoom_management_versions_and_provider_calls_use_shared_domain_logic(db):
    config = AppConfig({"agent": {"zoom": {"enabled": True, "client_id": "test-client",
                                          "client_secret": "test-secret", "account_id": "test-account"}}})
    with patch("assistant_agent.zoom_meetings.ZoomMeetings.request", side_effect=[
        {"id": 123456, "join_url": "https://zoom.us/j/123456", "start_url": "NEVER-EXPOSE"},
        {"topic": "Meeting", "start_time": "2099-01-01T10:00:00Z", "duration": 30},
        {"topic": "Meeting", "start_time": "2099-01-01T10:00:00Z", "duration": 30},
        {},
        {"topic": "Renamed", "start_time": "2099-01-01T10:00:00Z", "duration": 30},
        {},
    ]) as request:
        created = mutate(db, config, "zoom.create", title="Meeting", start="2099-01-01T10:00:00Z",
                         duration_minutes=30, idempotency_key="management-zoom")["meeting"]
        listed = execute(db, config, {"action": "zoom.list"})["meetings"]
        assert listed[0]["id"] == created["id"]
        current = execute(db, config, {"action": "zoom.read", "arguments": {"meeting_id": created["id"]}})["meeting"]
        updated = mutate(db, config, "zoom.update", meeting_id=created["id"], title="Renamed",
                         expected_version=current["object_version"])["meeting"]
        mutate(db, config, "zoom.delete", meeting_id=created["id"], expected_version=updated["object_version"])
        assert request.call_count == 6
    assert "NEVER-EXPOSE" not in repr(listed)


def test_workspace_move_copy_delete_and_project_attachment_preserve_tracking(db, tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    (root / "source.txt").write_text("content")
    config = AppConfig({"agent": {"filesystem": {"shared_root": str(root), "require_mount": False},
                                 "embeddings": {"enabled": False}}})
    project = mutate(db, config, "projects.create", title="Files")["project_id"]
    context = {"workstream_id": project}
    with patch("assistant_agent.tools.ToolRuntime.normalize_and_index_path"):
        execute(db, config, {"action": "workspace.copy", **context, "arguments": {
            "source_path": "source.txt", "destination_path": "copy.txt",
        }}, mutate=True)
        execute(db, config, {"action": "workspace.move", **context, "arguments": {
            "source_path": "copy.txt", "destination_path": "moved.txt",
        }}, mutate=True)
        assert not (root / "copy.txt").exists()
        assert (root / "moved.txt").read_text() == "content"
        execute(db, config, {"action": "workspace.delete", **context, "arguments": {"path": "moved.txt"}}, mutate=True)
        assert not (root / "moved.txt").exists()
    contact = mutate(db, config, "contacts.create", first_name="Attached")["contact"]
    with pytest.raises(ValidationError):
        mutate(db, config, "projects.attach", project_id=project, kind="memory", key="1")
    mutate(db, config, "projects.attach", project_id=project, kind="contact", key=str(contact["id"]))
    assert db.fetch_one("SELECT workstream_id FROM workstream_attachments WHERE workstream_id = %s", (project,))


def test_dropbox_browsing_and_revision_guard_do_not_require_a_task(db):
    from assistant_agent.dropbox_provider import DropboxError
    config = AppConfig({"agent": {"dropbox": {"enabled": True, "client_id": "test", "refresh_token": "test"}}})
    metadata = {".tag": "file", "id": "id:one", "rev": "r1", "size": 0, "path_display": "/Inbox/report.txt",
                "name": "report.txt", "content_hash": "0" * 64}
    with patch("assistant_agent.dropbox_provider.DropboxClient.request", return_value={
        "entries": [metadata], "has_more": False, "cursor": "provider-cursor",
    }):
        result = execute(db, config, {"action": "dropbox.list"})
    assert result["files"][0]["rev"] == "r1"
    with patch("assistant_agent.dropbox_provider.DropboxClient.metadata", return_value={
        "id": "id:one", "rev": "r1", "path": "/Inbox/report.txt", "size": 0,
    }), patch("assistant_agent.dropbox_provider.DropboxClient.request", return_value={}) as request:
        with pytest.raises(DropboxError, match="changed"):
            mutate(db, config, "dropbox.delete", folder="Inbox", path="/Inbox/report.txt", revision="old")
        request.assert_not_called()
        assert mutate(db, config, "dropbox.delete", folder="Inbox", path="/Inbox/report.txt", revision="r1")["deleted"]
        request.assert_called_once_with("files/delete_v2", {"path": "id:one", "parent_rev": "r1"})
    for action in ("dropbox.import", "dropbox.export"):
        assert next(row for row in catalog()["actions"] if row["action"] == action)["requires_task"]
        with pytest.raises(ValueError, match="requires an existing task"):
            mutate(db, config, action, path="/Inbox/report.txt", **({"revision": "r1"} if action.endswith("import") else {}))


def test_management_searches_stored_context_and_workspace_without_network(db, tmp_path):
    from assistant_agent.workspace_index import WorkspaceIndex
    root = tmp_path / "share"
    root.mkdir()
    report = root / "report.md"
    report.write_text("A synthetic observatory report about telescope maintenance.")
    config = AppConfig({"agent": {"filesystem": {"shared_root": str(root), "require_mount": False},
                                 "sandbox": {"enabled": False}, "embeddings": {"enabled": False}}})
    WorkspaceIndex(db, config).index_path(report)
    mutate(db, config, "contacts.create", first_name="Observatory", notes="Telescope maintenance")
    files = execute(db, config, {"action": "workspace.search", "arguments": {"pattern": "*.md"}})
    assert files["matches"] == [str(report)]
    semantic = execute(db, config, {"action": "workspace.semantic-search", "arguments": {"query": "telescope"}})
    assert semantic["matches"][0]["relative_path"] == "report.md"
    context = execute(db, config, {"action": "context.search", "arguments": {"query": "Observatory"}})
    assert context["result_count"] > 0
    from assistant_agent.harness_help import index
    with patch("assistant_agent.harness_help.index", side_effect=lambda: index(Path(__file__).resolve().parents[3] / "docs")):
        capabilities = execute(db, config, {"action": "capabilities.read"})
    assert "file_read" in capabilities["available_tools"]


def test_calendar_management_lifecycle_and_reads_use_provider_and_version_checks(db, monkeypatch):
    from dataclasses import replace
    from datetime import datetime, timezone
    from assistant_agent.calendar.models import BusyWindow, Calendar, CalendarEvent, ProviderConfig
    from assistant_agent.calendar.provider import CalendarProvider
    for suffix in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN"):
        monkeypatch.setenv("AGENT_CALENDAR_GOOGLE_" + suffix, "synthetic")
    config = AppConfig({"agent": {"calendar": {"enabled": True, "default_provider": "google"}}})
    provider = MagicMock(spec=CalendarProvider)
    start = datetime(2099, 1, 1, 10, tzinfo=timezone.utc)
    end = datetime(2099, 1, 1, 11, tzinfo=timezone.utc)
    event = CalendarEvent("provider-event", "google", "primary", "Meeting", start, end)
    updated_event = replace(event, title="Renamed")
    provider.create_event.return_value = event
    provider.get_event.return_value = event
    provider.update_event.return_value = updated_event
    provider.list_events.return_value = [event]
    provider.get_free_busy.return_value = [BusyWindow(start, end, "google", "primary")]
    provider.list_calendars.return_value = [Calendar("primary", "Calendar", "google")]
    pc = ProviderConfig("google", "google", "primary", "test", "test", "test")
    with patch("assistant_agent.calendar.router._load_provider_configs", return_value=[pc]), \
            patch("assistant_agent.calendar.router.build_provider", return_value=provider):
        created = mutate(db, config, "calendar.create", title="Meeting", start=start.isoformat(), end=end.isoformat())["event"]
        bounds = {"start": start.isoformat(), "end": end.isoformat()}
        events = execute(db, config, {"action": "calendar.events", "arguments": bounds})
        assert events["complete"] and events["events"][0]["event_id"] == created["event_id"]
        assert execute(db, config, {"action": "calendar.busy", "arguments": bounds})["busy"]
        assert execute(db, config, {"action": "calendar.calendars"})["providers"]["google"][0]["id"] == "primary"
        assert mutate(db, config, "calendar.sync")["status"] == "ok"
        updated = mutate(db, config, "calendar.update", event_id=created["event_id"], title="Renamed",
                         expected_version=created["object_version"])["event"]
        assert updated["title"] == "Renamed"
        provider.get_event.return_value = updated_event
        with pytest.raises(ToolError):
            mutate(db, config, "calendar.delete", event_id=created["event_id"], expected_version=created["object_version"])
        deleted = mutate(db, config, "calendar.delete", event_id=created["event_id"],
                         expected_version=updated["object_version"])["event"]
        assert deleted["status"] == "deleted"
        provider.delete_event.assert_called_once_with("primary", "provider-event")


def test_workspace_conversion_publishes_and_tracks_output(db, tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    (root / "source.md").write_text("# Synthetic document\n")
    config = AppConfig({"agent": {"filesystem": {"shared_root": str(root), "require_mount": False},
                                 "workspace": {"index": {"enabled": False}}}})
    result = mutate(db, config, "workspace.convert", path="source.md", output_format="markdown",
                    destination_path="converted.md")
    assert result["relative_path"] == "converted.md"
    assert (root / "converted.md").read_text() == "# Synthetic document\n"
    assert (root / "source.md").exists()
    with pytest.raises(ToolError, match="already exists"):
        mutate(db, config, "workspace.convert", path="source.md", output_format="markdown",
               destination_path="converted.md")


def test_management_email_and_telegram_sends_persist_delivery_and_distinguish_operations(db):
    config = AppConfig({"agent": {
        "admin": {"email": "admin@test.invalid"},
        "email": {"smtp_host": "smtp.test.invalid", "smtp_from": "assistant@test.invalid",
                  "save_to_sent": False},
        "telegram": {"enabled": True, "bot_token": "synthetic", "allowed_user_ids": ["123"],
                     "recipients": {"admin@test.invalid": "123"}},
    }})
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.send_message.return_value = {}
    with patch("assistant_agent.tools.smtplib.SMTP", return_value=smtp):
        sent = mutate(db, config, "email.send", to=["admin@test.invalid"], subject="Test", body="Synthetic message")
    assert sent["status"] == "sent"
    assert db.fetch_one("SELECT status FROM outbound_email_logs WHERE id = %s", (sent["log_id"],))["status"] == "sent"
    smtp.send_message.assert_called_once()
    request = {"action": "telegram.send", "arguments": {"text": "Synthetic message"}, "request_key": "operation-one"}
    with patch("assistant_agent.telegram.TelegramClient.call", return_value={"message_id": 321}) as call:
        assert execute(db, config, request, mutate=True)["status"] == "sent"
        assert execute(db, config, request, mutate=True)["status"] == "sent"
        call.assert_called_once()
        assert execute(db, config, {**request, "request_key": "operation-two"}, mutate=True)["status"] == "sent"
        assert call.call_count == 2


def test_zoom_reconciliation_requires_matching_provider_state_and_records_identity(db):
    from assistant_agent.zoom_meetings import ZoomError
    config = AppConfig({"agent": {"zoom": {"enabled": True, "client_id": "test-client",
                                          "client_secret": "test-secret", "account_id": "test-account"}}})
    with patch("assistant_agent.zoom_meetings.ZoomMeetings.request", side_effect=ZoomError("Lost response", uncertain=True)):
        with pytest.raises(ToolError, match="Lost response"):
            mutate(db, config, "zoom.create", title="Reconcile", start="2099-01-01T10:00:00Z",
                   duration_minutes=30, idempotency_key="uncertain-management-meeting")
    row = db.fetch_one("SELECT id, status FROM zoom_managed_meetings WHERE request_key = 'uncertain-management-meeting'")
    assert row["status"] == "uncertain"
    with patch("assistant_agent.zoom_meetings.ZoomMeetings.request", side_effect=[
        {"host_id": "authorized-host", "topic": "Reconcile", "start_time": "2099-01-01T10:00:00Z",
         "duration": 30, "join_url": "https://zoom.us/j/987"},
        {"id": "authorized-host"},
    ]):
        reconciled = mutate(db, config, "zoom.reconcile", meeting_id=row["id"], provider_meeting_id="987")["meeting"]
    assert reconciled["status"] == "active" and reconciled["provider_meeting_id"] == "987"


def test_dropbox_management_transfers_scan_publish_and_record_task_provenance(db, tmp_path):
    from assistant_agent.dropbox_provider import content_hash
    root = tmp_path / "share"
    root.mkdir()
    source = tmp_path / "synthetic.txt"
    source.write_text("Synthetic transfer document")
    config = AppConfig({"agent": {
        "filesystem": {"shared_root": str(root), "require_mount": False},
        "artifacts": {"raw_root": str(tmp_path / "private")},
        "workspace": {"index": {"enabled": False}},
        "dropbox": {"enabled": True, "client_id": "test", "refresh_token": "test", "allow_exports": True},
    }})
    project = mutate(db, config, "projects.create", title="Transfers")["project_id"]
    task = db.fetch_one("INSERT INTO jobs(thread_id, workstream_id) VALUES ('management-transfer', %s) RETURNING id", (project,))
    metadata = {"id": "id:one", "rev": "r1", "path": "/Inbox/document.txt",
                "size": source.stat().st_size, "content_hash": content_hash(source)}

    def download(meta, destination):
        destination.write_bytes(source.read_bytes())

    with patch("assistant_agent.dropbox_provider.DropboxClient.metadata", return_value=metadata), \
            patch("assistant_agent.dropbox_provider.DropboxClient.download", side_effect=download), \
            patch("assistant_agent.artifact_processor.ArtifactProcessor.scan_file", return_value={"status": "clean"}), \
            patch("assistant_agent.document_text.DocumentTextExtractor.convert_to_markdown", return_value=source.read_text()):
        imported = execute(db, config, {"action": "dropbox.import", "task_id": task["id"],
                                       "arguments": {"path": "/Inbox/document.txt", "revision": "r1"}}, mutate=True)
    assert (root / imported["path"]).read_bytes() == source.read_bytes()
    assert (root / imported["markdown_path"]).read_text() == source.read_text()
    destination = f"/Outbox/job-{task['id']}/document.txt"
    with patch("assistant_agent.dropbox_provider.DropboxClient.upload", return_value={**metadata, "path": destination}) as upload, \
            patch("assistant_agent.artifact_processor.ArtifactProcessor.scan_file", return_value={"status": "clean"}):
        exported = execute(db, config, {"action": "dropbox.export", "task_id": task["id"],
                                       "arguments": {"path": imported["path"], "filename": "document.txt"}}, mutate=True)
    assert exported["remote_path"] == destination
    upload.assert_called_once()
    records = db.fetch_all("SELECT direction, status, scan_status FROM dropbox_transfers WHERE job_id = %s", (task["id"],))
    assert {row["direction"] for row in records} == {"import", "export"}
    assert all(row["status"] == "ready" and row["scan_status"] == "clean" for row in records)


def test_management_script_execution_preserves_sandbox_and_approval_boundary(db, tmp_path):
    import json
    from assistant_agent.script_permissions import PermissionDenied
    root = tmp_path / "share"
    root.mkdir()
    (root / "script.py").write_text("print('synthetic')\n")
    config = AppConfig({"agent": {"filesystem": {"shared_root": str(root), "require_mount": False},
                                 "sandbox": {"enabled": True, "base_url": "https://sandbox:8080"}}})
    project = mutate(db, config, "projects.create", title="Scripts")["project_id"]
    task = db.fetch_one("INSERT INTO jobs(thread_id, workstream_id) VALUES ('management-script', %s) RETURNING id", (project,))
    health = {"execution_identity": {"image_id": "synthetic", "python_version": "3.14"},
              "permission_epoch": 1}
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b'{"stdout":"synthetic","stderr":"","exit_code":0}'
    with patch("assistant_agent.tools.read_secret", return_value="synthetic-token"), \
            patch("assistant_agent.tools.ssl.create_default_context"), \
            patch("assistant_agent.tools.ToolRuntime.sandbox_control", return_value=health), \
            patch("assistant_agent.tools.open_https_request", return_value=response) as send:
        result = execute(db, config, {"action": "scripts.execute", "task_id": task["id"],
                                     "arguments": {"command": ["pwd"]}}, mutate=True)
        assert result["stdout"] == "synthetic" and result["attempts"] == 1
        assert json.loads(send.call_args.args[0].data)["network_policy"]["allow"] == []
        send.reset_mock()
        with pytest.raises(PermissionDenied, match="approval"):
            execute(db, config, {"action": "scripts.execute", "task_id": task["id"],
                                 "arguments": {"command": ["python", "script.py"]}}, mutate=True)
        send.assert_not_called()
