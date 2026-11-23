from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from assistant_agent.config import AppConfig
from assistant_agent.management_operator import execute as execute_management
from assistant_agent.management_api import (
    MAX_LOG_BYTES, ManagementExecutorUnavailable, ManagementService, create_app, ensure_token, read_token, token_path,
)
from assistant_agent.management_client import ManagementClient
from assistant_agent.management_operations import OperationManager
from assistant_agent.setup import PublicSetupError, SetupError
from test_network_access import db  # noqa: F401


TOKEN = "a" * 64
CLIENT_ID = "b" * 32


@pytest.fixture(autouse=True)
def registered_client(request):
    if "db" not in request.fixturenames:
        return
    from assistant_agent.client_channels import ClientChannels
    database = request.getfixturevalue("db")
    channels = ClientChannels(database, AppConfig({}))
    channels.register(CLIENT_ID, "Test client", "custom", [])
    channels.poll(CLIENT_ID)


def backend():
    item = Mock()
    item.accepted = True
    item.credentials = {"AGENT_LLM_OPENROUTER_API_KEY": "private-model-key"}
    item.installation = SimpleNamespace(
        values={"AGENT_IDENTITY_NAME": "Assistant", "AGENT_LLM_OPENROUTER_API_KEY": "private-model-key"},
        defaults=AppConfig({"agent": {"model": "example", "api_key": "yaml-private"}}),
    )
    item.redact.side_effect = lambda value: value.replace("private-model-key", "[hidden]")
    item.services.return_value = [{"id": "task-agent", "title": "Task worker", "state": "running", "health": "healthy"}]
    item.logs.return_value = "result private-model-key\nAuthorization: Bearer abcdefg\nhttps://user:pass@example.org/"
    return item


def test_service_status_and_storage_metadata_do_not_open_the_credential_volume(tmp_path):
    from assistant_agent.credential_volume import CredentialVolume
    from assistant_agent.setup import DockerCommandError, Installation

    directory = tmp_path / "data/.secrets"
    directory.mkdir(parents=True)
    (directory / "state.json").write_text('{"credential_volume":"assistant-credentials-0123456789abcdef"}')
    service = ManagementService(tmp_path)
    try:
        with patch.object(CredentialVolume, "load", side_effect=DockerCommandError("Docker is unavailable")) as volume, \
                patch.object(Installation, "docker", side_effect=[
                    "29.0", '{"services":{"task-agent":{}}}', '{"Service":"task-agent","State":"running"}',
                ]):
            assert service.credential_storage() == {"mode": "volume", "retirement_pending": False}
            assert service.services()["services"][0]["state"] == "running"
            volume.assert_not_called()
            with pytest.raises(DockerCommandError, match="Could not load saved credentials"):
                service.credentials()
            volume.assert_called_once()
    finally:
        service.close()


def test_v2_registered_client_chat_and_domain_management_over_http(db, tmp_path, monkeypatch):
    import time
    from assistant_agent.replies import ReplyRouter
    from assistant_agent.workstream_files import WorkstreamFileTracker

    monkeypatch.delenv("ASSISTANT_MANAGEMENT_EXECUTOR_URL", raising=False)
    config = AppConfig({"agent": {"filesystem": {"shared_root": str(tmp_path)}}})
    path = tmp_path / "report.txt"
    path.write_text("Synthetic workspace file")
    workstream_id = db.fetch_one("SELECT ensure_workstream('http-project', 'HTTP project') AS id")["id"]
    WorkstreamFileTracker(db.for_workstream(workstream_id), config, None).track_file(path)
    file_id = db.fetch_one("SELECT id FROM workstream_files WHERE path = %s", (str(path),))["id"]
    item = backend()
    item.request.side_effect = lambda payload: execute_management(db, config, payload)
    app = create_app(tmp_path, TOKEN, ManagementService(tmp_path, Mock(return_value=item)))
    client = TestClient(app)
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}

    def operation(key, **payload):
        response = client.post("/v2/operations", headers={**headers, "Idempotency-Key": key},
                               json={**payload, "confirmed": True})
        assert response.status_code == 202, response.text
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = client.get("/v2/operations/" + response.json()["id"], headers=headers).json()
            if result["state"] in {"succeeded", "failed"}:
                assert result["state"] == "succeeded", result
                return result["result"]
            time.sleep(0.01)
        pytest.fail("Management operation did not finish")

    try:
        registered = operation("register-http", operation="client.register", name="HTTP desktop",
                               client_type="desktop", fallback_channels=[])
        identifier = registered["client"]["id"]
        inbox = f"/v2/clients/{identifier}/messages"
        assert client.get(inbox, headers=headers).json()["messages"] == []
        started = operation("question-http", operation="task.start", client_id=identifier, body="Synthetic question",
                            workstream_id=workstream_id, attachment_file_ids=[file_id])
        assert started["workstream_id"] == workstream_id
        message = db.fetch_one("SELECT attachments FROM request_messages WHERE id = %s", (started["request_id"],))
        assert message["attachments"][0]["file_id"] == file_id
        assert message["attachments"][0]["path"] == "report.txt"
        job = db.fetch_one("SELECT * FROM jobs WHERE id = %s", (started["task_id"],))
        assert job["metadata"]["source_channel"] == "client"
        delivery = ReplyRouter(SimpleNamespace(db=db, config=config, job=job)).send("Synthetic answer")
        assert delivery["status"] == "queued"
        messages = client.get(inbox, headers=headers).json()["messages"]
        assert messages[0]["body"] == "Synthetic answer"
        ack = client.post(f"/v2/clients/{identifier}/acknowledgments", headers=headers,
                          json={"message_ids": [messages[0]["id"]]})
        assert ack.json()["acknowledged"] == [delivery["log_id"]]
        assert client.get(inbox, headers=headers).json()["messages"] == []
        history = client.get(f"/v2/clients/{identifier}/history", headers=headers)
        assert history.status_code == 200
        assert history.json()["messages"][0]["status"] == "sent"
        assert history.json()["messages"][0]["body"] == "Synthetic answer"
        notice_id = db.fetch_one(
            """INSERT INTO channel_deliveries(channel, destination, purpose, request_key, body_text, status)
               VALUES ('client', %s, 'notice', 'http-uncertain', 'Check this', 'uncertain') RETURNING id""",
            (identifier,),
        )["id"]
        operation("resolve-http", operation="client.delivery-resolve", client_id=identifier,
                  delivery_id=notice_id, resolution="return-to-client")
        assert client.get(inbox, headers=headers).json()["messages"][0]["id"] == notice_id
        created = operation("contact-http", operation="domain.mutate", action="contacts.create",
                            arguments={"first_name": "HTTP contact"})
        contact_id = created["data"]["contact"]["id"]
        read = client.post("/v2/query", headers=headers,
                           json={"action": "contacts.read", "arguments": {"contact_id": contact_id}})
        assert read.status_code == 200 and read.json()["contact"]["first_name"] == "HTTP contact"
        def skill_request(action, **arguments):
            return {"operation": "domain.mutate", "action": "skills." + action, "arguments": arguments}

        assert client.post("/v2/query", headers={"Host": "127.0.0.1"},
                           json={"action": "skills.list"}).status_code == 401
        document = {"id": "RELEASE.NOTES", "title": "Release notes", "description": "Compose notes",
                    "body": "Use the reviewed release notes guide."}
        operation("create-skill-http", **skill_request("create", **document))
        read_skill = client.post("/v2/query", headers=headers,
                                  json={"action": "skills.read", "arguments": {"id": document["id"]}})
        assert read_skill.json()["body"] == document["body"]
        operation("edit-skill-http", **skill_request(
            "edit", **{**document, "title": "Reviewed releases"}, revision=read_skill.json()["revision"]))
        edited_skill = client.post("/v2/query", headers=headers,
                                    json={"action": "skills.read", "arguments": {"id": document["id"]}}).json()
        assert edited_skill["title"] == "Reviewed releases"
        delete = skill_request("delete", id=document["id"], revision=edited_skill["revision"])
        assert client.post("/v2/operations", headers={**headers, "Idempotency-Key": "unconfirmed-skill"},
                           json=delete).status_code == 422
        operation("delete-skill-http", **delete)
        filtered = client.post("/v2/query", headers=headers,
                               json={"action": "skills.list", "arguments": {"query": document["id"]}})
        assert filtered.json() == {"skills": [], "total": 0}
        preference_body = "For all outbound mail, sign as the Assistant."
        operation("create-preferences-http", **skill_request("create", overlay_for="EMAIL", body=preference_body))
        def email_preferences():
            return client.post("/v2/query", headers=headers,
                               json={"action": "skills.read", "arguments": {"id": "EMAIL"}}).json()["preferences"]

        preference = email_preferences()
        assert preference["body"] == preference_body
        assert preference["overlay_for"] == "EMAIL"
        for include_overlays, expected in ((True, {"EMAIL", preference["id"]}), (False, {"EMAIL"})):
            result = client.post("/v2/query", headers=headers, json={
                "action": "skills.list", "arguments": {"query": "EMAIL", "include_overlays": include_overlays}
            }).json()
            assert {skill["id"] for skill in result["skills"] if skill["id"] in {"EMAIL", preference["id"]}} == expected
        operation("edit-preferences-http", **skill_request(
            "edit", **{key: preference[key] for key in ("id", "title", "description", "revision")}, body="Updated signoff."))
        updated = email_preferences()
        assert updated["body"] == "Updated signoff."
        operation("delete-preferences-http", **skill_request("delete", id=updated["id"], revision=updated["revision"]))
        assert email_preferences() is None
        journal = "\n".join(path.read_text() for path in (tmp_path / "data").rglob("*.json"))
        assert "Synthetic question" not in journal and "Synthetic answer" not in journal
        assert "HTTP contact" not in journal
        assert document["body"] not in journal
        assert preference_body not in journal
    finally:
        app.state.management_operations.executor.shutdown(wait=True)


def test_expired_operation_status_does_not_allow_duplicate_submission(tmp_path, monkeypatch):
    import time
    import assistant_agent.management_operations as operations

    monkeypatch.setattr(operations, "MAX_OPERATIONS", 1)
    execute = Mock(return_value={})
    manager = OperationManager(tmp_path, execute)
    try:
        for key in ("first-key", "second-key"):
            result = manager.submit("service.lifecycle", {}, key)
            for _ in range(500):
                if manager.get(result["id"])["state"] == "succeeded":
                    break
                time.sleep(0.01)
            else:
                pytest.fail("Operation did not finish")
        with pytest.raises(SetupError, match="status has expired"):
            manager.submit("service.lifecycle", {}, "first-key")
        assert execute.call_count == 2
    finally:
        manager.executor.shutdown(wait=True)
    restarted = OperationManager(tmp_path, execute)
    try:
        with pytest.raises(SetupError, match="status has expired"):
            restarted.submit("service.lifecycle", {}, "first-key")
        assert execute.call_count == 2
    finally:
        restarted.executor.shutdown(wait=True)


def test_reconciliation_drains_backlog_before_waiting(tmp_path):
    from threading import Event

    drained = Event()
    service = Mock()

    def reconcile(action):
        assert action == "reconcile"
        count = service.client_channels.call_count
        if count == 2:
            drained.set()
        return {"processed": 1 if count == 1 else 0, "reconciliation_interval_seconds": 3600}

    service.client_channels.side_effect = reconcile
    with TestClient(create_app(tmp_path, TOKEN, service)):
        assert drained.wait(2)


@pytest.mark.parametrize("status", ["failed", "blocked", "uncertain"])
def test_domain_delivery_failures_do_not_become_successful_operations(status):
    from assistant_agent.management_operations import operation_result
    with pytest.raises(SetupError, match="did not confirm completion"):
        operation_result("domain.mutate", {"status": status, "reason": "private provider detail"})


def test_configuration_redacts_each_layer_without_hiding_credential_names(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/overrides.env").write_text("CUSTOM_TOKEN=do-not-show\nAGENT_IDENTITY_NAME=Override\n")
    item = backend()
    service = ManagementService(tmp_path, Mock(return_value=item))
    result = service.configuration()
    assert result["credential_names"] == ["AGENT_LLM_OPENROUTER_API_KEY"]
    assert result["files"][0]["values"]["agent"]["api_key"] == "[redacted]"
    assert result["files"][1]["values"]["AGENT_LLM_OPENROUTER_API_KEY"] == "[redacted]"
    assert result["files"][2]["values"] == {"CUSTOM_TOKEN": "[redacted]", "AGENT_IDENTITY_NAME": "Override"}
    assert result["read_only"]


def test_api_requires_token_rejects_browser_origins_and_does_not_redirect(tmp_path):
    service = Mock()
    client = TestClient(create_app(tmp_path, TOKEN, service))
    assert client.get("/v1/health").status_code == 401
    assert client.get("/v1/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    headers = {"Authorization": "Bearer " + TOKEN, "Host": "127.0.0.1"}
    assert client.get("/v1/health", headers={**headers, "Origin": "https://example.org"}).status_code == 401
    response = client.get("/v1/health", headers=headers)
    assert response.status_code == 200
    assert response.json()["api_version"] == 1
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/v1/health", headers={**headers, "Host": "evil.example"}).status_code == 400
    assert client.post("/v1/configuration", headers=headers, json={}).status_code == 405
    service.configuration.assert_not_called()


def test_services_logs_are_shared_redacted_and_bounded(tmp_path):
    item = backend()
    service = ManagementService(tmp_path, Mock(return_value=item))
    assert service.services()["services"][0]["id"] == "task-agent"
    result = service.logs("task-agent")
    assert "private-model-key" not in result["content"]
    assert "abcdefg" not in result["content"]
    assert "user:pass" not in result["content"]
    item.logs.assert_called_once_with("task-agent")
    item.logs.return_value = "x" * (MAX_LOG_BYTES + 1)
    result = service.logs("task-agent")
    assert result["truncated"]
    assert len(result["content"]) == MAX_LOG_BYTES
    with pytest.raises(SetupError):
        service.logs("--help")
    assert item.logs.call_count == 2


def test_failure_never_exposes_docker_or_configuration_exception_contents(tmp_path):
    service = Mock()
    service.services.side_effect = SetupError("password=do-not-display")
    client = TestClient(create_app(tmp_path, TOKEN, service))
    response = client.get("/v1/services", headers={"Host": "localhost", "Authorization": "Bearer " + TOKEN})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "management_failed"
    assert "do-not-display" not in response.text
    assert "Server log reference" in response.json()["error"]["message"]


def test_curated_failures_explain_themselves_to_clients(tmp_path):
    service = Mock()
    service.services.side_effect = PublicSetupError("Select a configured service.")
    client = TestClient(create_app(tmp_path, TOKEN, service))
    response = client.get("/v1/services", headers={"Host": "localhost", "Authorization": "Bearer " + TOKEN})
    assert response.status_code == 409
    assert response.json()["error"] == {"code": "management_failed", "message": "Select a configured service."}


def test_task_activity_route_validates_and_delegates(tmp_path):
    service = Mock()
    service.task_activity.return_value = {"rows": [{
        "id": "4:1", "task_id": 4, "created_at": "2025-01-01T00:00:00Z", "tool_name": "web_search",
        "status": "succeeded", "input": {"q": "x"}, "output": "done",
    }], "total": 1, "truncated": False}
    client = TestClient(create_app(tmp_path, TOKEN, service))
    headers = {"Host": "localhost", "Authorization": "Bearer " + TOKEN}
    response = client.get("/v2/tasks/4/activity?since=2025-01-01T00:00:00Z", headers=headers)
    assert response.status_code == 200
    assert response.json()["rows"][0]["tool_name"] == "web_search"
    service.task_activity.assert_called_once_with(4, "2025-01-01T00:00:00Z")


def test_task_activity_rejects_invalid_start_time(tmp_path):
    with pytest.raises(PublicSetupError, match="ISO 8601"):
        ManagementService(tmp_path).task_activity(4, "yesterday")


@pytest.mark.parametrize("body, expected", [
    ("Thanks!\n\nOn Mon, 3 Mar 2025 at 10:00, Assistant <a@example.com> wrote:\n> Earlier answer",
     "Thanks!"),
    ("Sounds good\nOn Tue, Mar 4, 2025 at 9:00 AM Rakan <\nr@example.com> wrote:\n\n> Old", "Sounds good"),
    ("Approved.\n\n-----Original Message-----\nFrom: Someone", "Approved."),
    ("Yes please.\n\n________________________________\nFrom: Assistant\nSent: Monday\nTo: Rakan\nOld text",
     "Yes please."),
    ("> quoted question\nMy inline answer\n> another quote\nMore answer", None),
    ("On Monday we should ship.\nThe team wrote: a plan.", None),
    ("> only quoted history", None),
])
def test_strip_quoted_history_keeps_only_the_new_reply(body, expected):
    from assistant_agent.email_quotes import strip_quoted_history

    assert strip_quoted_history(body) == (body if expected is None else expected)


def test_conversations_route_validates_and_delegates(tmp_path):
    service = Mock()
    service.conversations.return_value = {"rows": [{
        "thread_id": "client:abc", "channel": "client", "message_count": 2, "opening_message": "Hi",
        "last_activity_at": "2025-01-01T00:00:00Z", "latest_task_id": 4, "latest_task_status": "completed",
    }], "total": 1}
    client = TestClient(create_app(tmp_path, TOKEN, service))
    headers = {"Host": "localhost", "Authorization": "Bearer " + TOKEN}
    response = client.get("/v2/conversations?page=1&query=hi&channel=client", headers=headers)
    assert response.status_code == 200
    assert response.json()["rows"][0]["latest_task_id"] == 4
    service.conversations.assert_called_once_with(1, "hi", "client")
    assert client.get("/v2/conversations?channel=fax", headers=headers).status_code == 422


@pytest.mark.parametrize("body, expected", [
    ({"error": "Docker is unavailable.", "public": True}, "Docker is unavailable."),
    ({"error": "password=hidden"}, "could not complete the read"),
])
def test_executor_reads_forward_only_curated_failures(tmp_path, monkeypatch, body, expected):
    monkeypatch.setenv("ASSISTANT_MANAGEMENT_EXECUTOR_URL", "http://management-docker:2376")
    ensure_token(tmp_path)
    transport = httpx.MockTransport(lambda request: httpx.Response(409, json=body))
    real_client = httpx.Client
    with patch("assistant_agent.management_api.httpx.Client",
               lambda **options: real_client(transport=transport)):
        with pytest.raises(PublicSetupError, match=expected) as failure:
            ManagementService(tmp_path, token=TOKEN)._executor_read({"request": "services.read"})
    assert "hidden" not in str(failure.value)


def test_token_creation_is_idempotent_and_rejects_unsafe_files(tmp_path):
    first = ensure_token(tmp_path)
    assert ensure_token(tmp_path) == first
    assert read_token(token_path(tmp_path)) == first
    token_path(tmp_path).chmod(0o644)
    with pytest.raises(SetupError, match="owner"):
        read_token(token_path(tmp_path))
    token_path(tmp_path).unlink()
    target = tmp_path / "other"
    target.write_text(TOKEN)
    token_path(tmp_path).symlink_to(target)
    with pytest.raises(SetupError, match="symlinks"):
        ensure_token(tmp_path)


def test_instance_identity_survives_management_token_rotation(tmp_path):
    from assistant_agent.management_api import ensure_instance_id

    identity = ensure_instance_id(tmp_path, "first-token")
    assert ensure_instance_id(tmp_path, "replacement-token") == identity


def test_database_password_generation_does_not_replace_existing_database(tmp_path):
    (tmp_path / "src/config").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "src/config/agent.yaml").write_text("agent: {}\n")
    client = TestClient(create_app(tmp_path, TOKEN))
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}
    response = client.post("/v2/credentials/database-password/generate", headers=headers)
    assert response.status_code == 200
    password = response.json()["password"]
    assert len(password) >= 32
    assert not (tmp_path / "data/.secrets/AGENT_DATABASE_PASSWORD").exists()
    (tmp_path / "data/postgres/pgdata").mkdir(parents=True)
    (tmp_path / "data/postgres/pgdata/PG_VERSION").write_text("16")
    assert client.post("/v2/credentials/database-password/generate", headers=headers).status_code == 409
    client.app.state.management_operations.executor.shutdown(wait=True)


def test_private_executor_dispatches_typed_summary_and_password_reads(tmp_path):
    from assistant_agent.management_executor import READ_ADAPTER, create_server

    server = create_server(("127.0.0.1", 0), tmp_path)
    service = Mock()
    try:
        request = READ_ADAPTER.validate_python({"request": "tasks.summary"})
        server.RequestHandlerClass.read(service, request)
        service.task_summary.assert_called_once_with()
        request = READ_ADAPTER.validate_python({"request": "credentials.generate-database-password"})
        server.RequestHandlerClass.read(service, request)
        service.generate_database_password.assert_called_once_with()
        request = READ_ADAPTER.validate_python({"request": "tasks.deletion-preview", "task_id": 7})
        server.RequestHandlerClass.read(service, request)
        service.task_deletion_preview.assert_called_once_with(7)
    finally:
        server.server_close()


def test_task_deletion_through_management_api(db, tmp_path, monkeypatch):
    import time

    monkeypatch.delenv("ASSISTANT_MANAGEMENT_EXECUTOR_URL", raising=False)
    item = backend()
    item.request.side_effect = lambda payload: execute_management(db, AppConfig({}), payload)
    item.mutate.side_effect = lambda action, identifier, body: execute_management(
        db, AppConfig({}), {"kind": "mutate", "action": action, "id": identifier, "body": body, "confirmed": True},
    )
    service = ManagementService(tmp_path, Mock(return_value=item))
    client = TestClient(create_app(tmp_path, TOKEN, service))
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}
    manager = client.app.state.management_operations

    def submit(payload, key):
        response = client.post("/v2/operations", headers={**headers, "Idempotency-Key": key}, json=payload)
        assert response.status_code == 202
        for _ in range(500):
            operation = manager.get(response.json()["id"])
            if operation["state"] in {"succeeded", "failed"}:
                return operation
            time.sleep(0.01)
        pytest.fail("operation did not complete")

    try:
        started = submit({"operation": "task.start", "body": "Preserve this conversation",
                          "client_id": CLIENT_ID, "confirmed": True},
                         "delete-task-start-001")
        assert started["state"] == "succeeded", started
        task_id = started["result"]["task_id"]
        db.execute("UPDATE jobs SET status = 'completed' WHERE id = %s", (task_id,))
        db.execute(
            """UPDATE request_messages SET metadata = metadata || '{"queued":true}'::jsonb
               WHERE thread_id = (SELECT thread_id FROM jobs WHERE id = %s)""", (task_id,),
        )
        db.log_event(task_id, "tool_result", output_data={"private": "execution detail"})
        tasks = client.get("/v2/records/tasks", headers=headers).json()
        assert any(row["id"] == task_id and row["status"] == "completed" for row in tasks["rows"])
        events = client.get(f"/v2/records/tasks/{task_id}/children", headers=headers).json()
        assert any(row["event_type"] == "tool_result" for row in events["rows"])
        preview = client.get(f"/v2/tasks/{task_id}/deletion-preview", headers=headers).json()
        assert preview["deletable"]
        db.execute("UPDATE jobs SET updated_at = clock_timestamp() WHERE id = %s", (task_id,))
        deletion = {"operation": "task.delete", "task_id": task_id,
                    "preview_digest": preview["preview_digest"], "confirmed": True}
        assert submit(deletion, "delete-task-stale-001")["state"] == "failed"
        preview = client.get(f"/v2/tasks/{task_id}/deletion-preview", headers=headers).json()
        deletion["preview_digest"] = preview["preview_digest"]
        assert submit(deletion, "delete-task-confirm-001")["state"] == "succeeded"
        record = client.get(f"/v2/records/tasks/{task_id}", headers=headers).json()["rows"][0]
        assert record["status"] == "deleted"
        assert record["task_summary"] == "[Deleted task]"
        assert record["deleted_at"]
        assert client.get(f"/v2/tasks/{task_id}/messages", headers=headers).json()["rows"][0]["body"] == \
            "Preserve this conversation"
        assert db.fetch_one("SELECT count(*) AS n FROM task_logs WHERE job_id = %s", (task_id,))["n"] == 0
        reply = submit({"operation": "task.reply", "task_id": task_id, "body": "Do not send",
                        "client_id": CLIENT_ID, "confirmed": True}, "delete-task-reply-001")
        assert reply["state"] == "failed"
        for action in ("cancel", "retry", "review", "instruct"):
            result = submit({"operation": "record.mutate", "action": action, "record_id": task_id,
                             "body": "Do not run", "confirmed": True}, "delete-task-" + action + "-001")
            assert result["state"] == "failed", (action, result)
    finally:
        manager.executor.shutdown(wait=True)


def test_task_cancel_retry_and_instruct_through_management_api(db, tmp_path, monkeypatch):
    import time

    monkeypatch.delenv("ASSISTANT_MANAGEMENT_EXECUTOR_URL", raising=False)
    item = backend()
    item.request.side_effect = lambda payload: execute_management(db, AppConfig({}), payload)
    item.mutate.side_effect = lambda action, identifier, body: execute_management(
        db, AppConfig({}), {"kind": "mutate", "action": action, "id": identifier, "body": body, "confirmed": True},
    )
    service = ManagementService(tmp_path, Mock(return_value=item))
    app = create_app(tmp_path, TOKEN, service)
    client = TestClient(app)
    manager = app.state.management_operations
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}
    task = db.create_manual_job("Task controls", "Check API controls")
    try:
        for action, expected in (("cancel", "cancelled"), ("retry", "queued"), ("instruct", "queued")):
            response = client.post(
                "/v2/operations", headers={**headers, "Idempotency-Key": "task-control-" + action},
                json={"operation": "record.mutate", "action": action, "record_id": task["id"],
                      "body": "Additional guidance" if action == "instruct" else "", "confirmed": True},
            )
            assert response.status_code == 202
            for _ in range(200):
                operation = manager.get(response.json()["id"])
                if operation["state"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.01)
            assert operation["state"] == "succeeded", operation
            assert db.fetch_one("SELECT status FROM jobs WHERE id = %s", (task["id"],))["status"] == expected
        guidance = db.fetch_one("SELECT instruction FROM supervisor_instructions WHERE job_id = %s", (task["id"],))
        assert guidance["instruction"] == "Additional guidance"
        reminder = db.fetch_one(
            """INSERT INTO reminders(title, task, run_at, created_by_job_id)
               VALUES ('Follow up', 'Follow up task', now() + interval '1 day', %s) RETURNING id""",
            (task["id"],),
        )
        reminders = client.get("/v2/records/reminders", headers=headers).json()["rows"]
        assert any(row["id"] == reminder["id"] and row["status"] == "scheduled" for row in reminders)
        response = client.post(
            "/v2/operations", headers={**headers, "Idempotency-Key": "reminder-cancel-001"},
            json={"operation": "record.mutate", "action": "cancel-reminder",
                  "record_id": reminder["id"], "confirmed": True},
        )
        assert response.status_code == 202
        for _ in range(200):
            operation = manager.get(response.json()["id"])
            if operation["state"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert operation["state"] == "succeeded", operation
        assert db.fetch_one("SELECT status FROM reminders WHERE id = %s", (reminder["id"],))["status"] == "cancelled"
    finally:
        manager.executor.shutdown(wait=True)


def test_memory_management_through_api(db, tmp_path, monkeypatch):
    import time

    monkeypatch.delenv("ASSISTANT_MANAGEMENT_EXECUTOR_URL", raising=False)
    item = backend()
    item.request.side_effect = lambda payload: execute_management(db, AppConfig({}), payload)
    item.mutate.side_effect = lambda action, identifier, body: execute_management(
        db, AppConfig({}), {"kind": "mutate", "action": action, "id": identifier, "body": body, "confirmed": True},
    )
    app = create_app(tmp_path, TOKEN, ManagementService(tmp_path, Mock(return_value=item)))
    client = TestClient(app)
    manager = app.state.management_operations
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}

    def mutate(action, identifier=None, body=""):
        response = client.post(
            "/v2/operations", headers={**headers, "Idempotency-Key": "memory-test-" + action},
            json={"operation": "record.mutate", "action": action, "record_id": identifier,
                  "body": body, "confirmed": True},
        )
        assert response.status_code == 202
        for _ in range(200):
            operation = manager.get(response.json()["id"])
            if operation["state"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert operation["state"] == "succeeded", operation

    try:
        mutate("create-memory", body="A preference")
        memory = db.fetch_one("SELECT id FROM agent_memories WHERE content = 'A preference'")
        memory_id = memory["id"]
        rows = client.get("/v2/records/memories", headers=headers).json()["rows"]
        assert any(row["id"] == memory_id for row in rows)
        assert client.get(f"/v2/records/memories/{memory_id}", headers=headers).json()["rows"][0]["content"] == \
            "A preference"
        mutate("update-memory", memory_id, "An updated preference")
        assert db.fetch_one("SELECT content FROM agent_memories WHERE id = %s", (memory_id,))["content"] == \
            "An updated preference"
        for action, expected in (("pin-memory", True), ("unpin-memory", False)):
            mutate(action, memory_id)
            assert db.fetch_one("SELECT pinned FROM agent_memories WHERE id = %s", (memory_id,))["pinned"] is expected
        for action, expected in (("approve-memory", "active"), ("reject-memory", "rejected")):
            db.execute("UPDATE agent_memories SET metadata = metadata || '{\"state\":\"suggested\"}'::jsonb WHERE id = %s",
                       (memory_id,))
            mutate(action, memory_id)
            assert db.fetch_one("SELECT metadata FROM agent_memories WHERE id = %s",
                                (memory_id,))["metadata"]["state"] == expected
        mutate("expire-memory", memory_id)
        assert db.fetch_one("SELECT expires_at <= now() AS expired FROM agent_memories WHERE id = %s",
                            (memory_id,))["expired"]
        mutate("delete-memory", memory_id)
        assert db.fetch_one("SELECT id FROM agent_memories WHERE id = %s", (memory_id,)) is None
    finally:
        manager.executor.shutdown(wait=True)


def test_approval_review_and_single_use_through_management_api(db, tmp_path, monkeypatch):
    import time
    from assistant_agent.admin_commands import redact_secrets
    from assistant_agent.database import json_safe
    from test_management_approvals import create_pending

    monkeypatch.delenv("ASSISTANT_MANAGEMENT_EXECUTOR_URL", raising=False)
    config = AppConfig({"agent": {"admin": {"email": "admin@example.com"},
                                 "security": {"email_authentication": {"enabled": False}}}})
    create_pending(db, config, {"kind": "command", "subject": "/admin cancel 12", "body": "Reviewed text"})
    item = backend()
    item.request.side_effect = lambda payload: redact_secrets(json_safe(execute_management(db, config, payload)))
    client = TestClient(create_app(tmp_path, TOKEN, ManagementService(tmp_path, Mock(return_value=item))))
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}
    manager = client.app.state.management_operations
    try:
        response = client.get("/v2/approvals?limit=1&offset=0", headers=headers)
        assert response.status_code == 200
        review = response.json()["approvals"][0]
        assert review["actionable"]
        assert review["action"]["subject"] == "/admin cancel 12"
        assert review["action"]["body"] == "Reviewed text"
        assert "otp_hash" not in response.text
        payload = {"operation": "approval.decide", "approval_id": review["approval_id"],
                   "version": review["version"], "decision": "approve", "confirmed": True}
        with patch("assistant_agent.admin_commands.AdminCommands.execute", return_value="executed") as apply:
            response = client.post("/v2/operations", headers={**headers, "Idempotency-Key": "approval-api-first"}, json=payload)
            assert response.status_code == 202
            for _ in range(500):
                result = manager.get(response.json()["id"])
                if result["state"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.01)
            assert result["state"] == "succeeded", result
            apply.assert_called_once_with("/admin cancel 12", "Reviewed text", confirmed=True)
            response = client.post("/v2/operations", headers={**headers, "Idempotency-Key": "approval-api-second"}, json=payload)
            for _ in range(500):
                result = manager.get(response.json()["id"])
                if result["state"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.01)
            assert result["state"] == "failed"
            assert apply.call_count == 1
        create_pending(db, config, {"kind": "command", "subject": "/admin cancel 13", "body": "Other text"})
        review = client.get("/v2/approvals", headers=headers).json()["approvals"][0]
        payload.update(approval_id=review["approval_id"], version=review["version"])
        with patch("assistant_agent.admin_commands.AdminCommands.execute", side_effect=RuntimeError("private detail")):
            response = client.post("/v2/operations", headers={**headers, "Idempotency-Key": "approval-api-partial"}, json=payload)
            for _ in range(500):
                result = manager.get(response.json()["id"])
                if result["state"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.01)
            assert result["state"] == "failed"
            assert "approval was consumed" in result["error"]
            assert "partially" in result["error"]
            assert "private detail" not in result["error"]
        assert "Reviewed text" not in (tmp_path / "data/.management-operations.json").read_text()
    finally:
        manager.executor.shutdown(wait=True)


def test_search_maintenance_results_remain_available_without_sensitive_fields(tmp_path):
    import time
    from assistant_agent.embedding_maintenance import SOURCES
    from assistant_agent.management_operations import operation_result

    pending = {item[0]: 0 for item in SOURCES}
    pending["jobs"] = 2
    result = {"model": "private-model-value", "dimensions": 768, "enabled": True,
              "pending": pending, "complete": False}
    item = backend()
    item.embeddings.return_value = result
    service = ManagementService(tmp_path, Mock(return_value=item))
    client = TestClient(create_app(tmp_path, TOKEN, service))
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN, "Idempotency-Key": "search-status-001"}
    response = client.post("/v2/operations", headers=headers, json={
        "operation": "search.maintenance", "action": "status",
    })
    assert response.status_code == 202
    manager = client.app.state.management_operations
    for _ in range(200):
        state = manager.get(response.json()["id"])
        if state["state"] in {"succeeded", "failed"}:
            break
        time.sleep(0.01)
    assert state["state"] == "succeeded", state
    assert state["result"]["pending"]["jobs"] == 2
    assert "private-model-value" not in str(state)
    assert operation_result("search.status", state["result"]) == state["result"]
    manager.executor.shutdown(wait=True)


@pytest.mark.parametrize("url", [
    "https://remote.example", "http://remote.example", "http://user@localhost",
    "http://127.0.0.1:8765/path", "http://localhost?token=secret",
])
def test_cli_client_rejects_non_loopback_or_credential_urls(url):
    with pytest.raises(SetupError, match="loopback"):
        ManagementClient(url, TOKEN)


def test_cli_reads_use_authenticated_api_and_ignore_proxy_environment():
    client = ManagementClient("http://127.0.0.1:8765", TOKEN)
    response = httpx.Response(200, json={"services": []})
    with patch("assistant_agent.management_client.httpx.Client") as transport:
        transport.return_value.__enter__.return_value.request.return_value = response
        assert client.services() == []
    assert transport.call_args.kwargs["trust_env"] is False
    request = transport.return_value.__enter__.return_value.request
    assert request.call_args.args[:2] == ("GET", "http://127.0.0.1:8765/v2/services")
    assert request.call_args.kwargs["headers"]["Authorization"] == "Bearer " + TOKEN


def test_real_saved_configuration_and_openapi_contract_do_not_need_docker(tmp_path):
    (tmp_path / "src/config").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "src/config/agent.yaml").write_text("agent:\n  identity:\n    name: My instance\n")
    (tmp_path / "data/.env").write_text("AGENT_LLM_OPENROUTER_API_KEY=private-key-value\n")
    (tmp_path / "data/overrides.env").write_text("AGENT_IDENTITY_NAME=Example\n")
    client = TestClient(create_app(tmp_path, TOKEN))
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}
    response = client.get("/v1/configuration", headers=headers)
    assert response.status_code == 200
    result = response.json()
    assert result["files"][0]["values"]["agent"]["identity"]["name"] == "My instance"
    assert result["files"][2]["values"]["AGENT_IDENTITY_NAME"] == "Example"
    assert "private-key-value" not in response.text
    assert result["credential_names"] == ["AGENT_LLM_OPENROUTER_API_KEY"]
    schema = client.get("/v1/schema", headers=headers).json()
    assert "ConfigurationResponse" in schema["components"]["schemas"]
    assert schema["security"] == [{"ManagementToken": []}]


def test_unexpected_error_is_reported_without_secret_traceback(tmp_path, caplog):
    service = Mock()
    service.services.side_effect = ValueError("private-error-content")
    client = TestClient(create_app(tmp_path, TOKEN, service))
    response = client.get("/v1/services", headers={"Host": "localhost", "Authorization": "Bearer " + TOKEN})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "private-error-content" not in response.text + caplog.text
    assert "ValueError" in caplog.text


def test_cli_rejects_malformed_typed_response():
    client = ManagementClient("http://127.0.0.1:8765", TOKEN)
    with patch.object(client, "get", return_value={"services": "not-a-list"}):
        with pytest.raises(SetupError, match="incompatible"):
            client.services()


def test_opt_in_tui_uses_same_api_for_services_and_logs(tmp_path):
    import hashlib

    from assistant_agent.management_client import run_command

    local_token = ensure_token(tmp_path)
    options = SimpleNamespace(
        root=tmp_path, management_action="tui", token_file=None, url="http://127.0.0.1:8765",
    )
    with patch("assistant_agent.management_client.ManagementClient.from_installation") as factory, \
            patch("assistant_agent.management_tui.ManagementApp") as app:
        client = factory.return_value
        client.health.return_value = {"instance_id": hashlib.sha256(local_token.encode()).hexdigest()[:32]}
        client.services.return_value = [{"id": "task-agent"}]
        client.logs.return_value = "recent logs"
        run_command(options)
        backend = app.call_args.kwargs["backend"]
        assert backend.services() == [{"id": "task-agent"}]
        assert backend.logs("task-agent") == "recent logs"
        client.logs.assert_called_once_with("task-agent")


def test_tui_uses_the_same_remote_transport_for_reads_and_writes(tmp_path):
    from assistant_agent.management_client import run_command

    ensure_token(tmp_path)
    options = SimpleNamespace(
        root=tmp_path, management_action="tui", token_file=None, url="http://127.0.0.1:8765",
    )
    with patch("assistant_agent.management_client.ManagementClient.from_installation") as factory, \
            patch("assistant_agent.management_tui.ManagementApp") as ui:
        factory.return_value.health.return_value = {"instance_id": "different-instance"}
        run_command(options)
        backend = ui.call_args.kwargs["backend"]
        backend.lifecycle("restart", "task-agent")
        factory.return_value.operation.assert_called_once_with({
            "operation": "service.lifecycle", "action": "restart", "service": "task-agent", "confirmed": True,
        })


def test_configuration_check_endpoint_reports_saved_problems_without_runtime(tmp_path):
    from assistant_agent.management_api import ManagementService

    class Backend:
        installation = object()

    service = ManagementService(tmp_path)
    service.backend = lambda: Backend()
    with patch("assistant_agent.config_check.check", return_value=(["data/.env: X is not used by Assistant"], [])) as check:
        client = TestClient(create_app(tmp_path, TOKEN, service))
        response = client.get("/v1/configuration/check", headers={"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN})
    assert response.status_code == 200
    assert check.call_args.args[1] is False
    assert response.json()["ok"] is False
    assert response.json()["runtime_checked"] is False


def test_v2_configuration_exposes_settings_and_only_redacted_credentials(tmp_path):
    (tmp_path / "src/config").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "src/config/agent.yaml").write_text("agent:\n  identity:\n    name: My instance\n")
    (tmp_path / "src/config/AGENT.md.example").write_text("Instructions\n")
    (tmp_path / "data/.env").write_text("AGENT_LLM_OPENROUTER_API_KEY=private-key-value\n")
    client = TestClient(create_app(tmp_path, TOKEN))
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}
    response = client.get("/v2/configuration", headers=headers)
    assert response.status_code == 200
    result = response.json()
    assert result["api_version"] == 2
    assert result["read_only"] is False
    assert result["revision"]
    assert "private-key-value" not in response.text
    assert any(item["key"] == "AGENT_IDENTITY_NAME" and item["value"] == "My instance"
               for item in result["settings"])
    assert any(item["key"] == "AGENT_LLM_OPENROUTER_API_KEY" and item["value"] == ""
               and item["secret"] for item in result["settings"])
    settings = {item["key"]: item for item in result["settings"]}
    assert all(item["metadata"].get("help") for item in result["settings"])
    assert settings["AGENT_CALENDAR_ZOHO_CLIENT_ID"]["label"] == "Client ID"
    assert settings["AGENT_CALENDAR_ZOHO_CLIENT_ID"]["metadata"]["section"] == "Zoho"
    assert not settings["AGENT_CALENDAR_ZOHO_2_CLIENT_ID"]["metadata"]["help"].startswith("Zoho:")
    assert settings["AGENT_CALENDAR_DEFAULT_PROVIDER"]["metadata"]["choices"] == []
    groups = {group["id"]: group for group in result["groups"]}
    assert groups["calendar:zoho_2"]["parent"] == "calendar"
    assert groups["calendar:zoho_2"]["section"] == settings["AGENT_CALENDAR_ZOHO_CLIENT_ID"]["metadata"]["section"]
    assert groups["catalog:fusion"]["help"] and groups["catalog:fusion"]["parent"] is None
    assert set(groups) == {item["group"] for item in result["settings"]}


def test_model_settings_overview_is_complete_revision_bound_and_secret_safe(tmp_path):
    import shutil
    from pathlib import Path
    from assistant_agent.settings_schema import catalog
    from test_management import configured

    source = Path(__file__).resolve().parents[2] / "config"
    (tmp_path / "src/config").mkdir(parents=True)
    for name in ("agent.yaml", "agent.schema.yaml", "AGENT.md.example"):
        shutil.copyfile(source / name, tmp_path / "src/config" / name)
    installation = configured(tmp_path)
    installation.values["AGENT_LLM_MODEL"] = "example/primary"
    installation.values["AGENT_SECURITY_DECISION_MODEL_MODEL"] = ""
    installation.values["AGENT_SECURITY_DECISION_MODEL_URL"] = ""
    installation.state["pending_configuration"] = ["AGENT_LLM_MODEL"]
    installation.save()
    service = ManagementService(tmp_path)
    client = TestClient(create_app(tmp_path, TOKEN, service))
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}
    try:
        assert client.get("/v2/configuration/model-settings", headers={"Host": "127.0.0.1"}).status_code == 401
        assert "configuration.model-settings" in client.get("/v2/health", headers=headers).json()["capabilities"]
        response = client.get("/v2/configuration/model-settings", headers=headers)
        assert response.status_code == 200, response.text
        current = response.json()
        assert current["revision"] == client.get("/v2/configuration/catalog", headers=headers).json()["revision"]
        assert current["value_state"] == "saved" and current["read_only"]
        assert current["activation_tracked"] and current["pending_activation"]
        fields = [field for section in current["sections"] for field in section["settings"]]
        keys = [field["key"] for field in fields]
        assert len(keys) == len(set(keys))
        assert set(keys) == {item["env"] for item in catalog() if item["metadata"].get("model_section")}
        assert {
            "AGENT_LLM_MODEL", "AGENT_LLM_FALLBACK_MODEL", "AGENT_ROUTING_MODEL", "AGENT_NAMING_MODEL",
            "AGENT_LIMITS_SUMMARIZATION_MODEL", "AGENT_SECURITY_DECISION_MODEL_MODEL",
            "AGENT_SECURITY_DEVELOPMENT_MODEL", "AGENT_DEEP_RESEARCH_MODEL", "AGENT_DEEP_RESEARCH_SEARCH_MODEL",
            "AGENT_FUSION_ANALYSIS_MODELS", "AGENT_FUSION_MODEL", "AGENT_SEARCH_MODEL", "AGENT_EMBEDDINGS_MODEL",
            "AGENT_TRANSCRIPTION_MODEL_PATH", "AGENT_TTS_MODEL", "AGENT_LLM_TEMPERATURE", "AGENT_MODEL_SERVICE_ENABLED",
        } <= set(keys)
        assert not {"AGENT_DATABASE_PASSWORD", "AGENT_SECURITY_SCRIPT_YOLO", "AGENT_SEARCH_EXA_API_KEY"} & set(keys)
        assert "database-password" not in response.text and "model-key" not in response.text
        assert "agent_yaml" not in current and "instructions" not in current
        fields = {field["key"]: field for field in fields}
        assert fields["AGENT_LLM_MODEL"]["pending_activation"]
        assert fields["AGENT_DEEP_RESEARCH_MODEL"]["display_value"] == "example/primary"
        assert fields["AGENT_DEEP_RESEARCH_MODEL"]["value_source"] == "inherited"
        assert fields["AGENT_DEEP_RESEARCH_MODEL"]["pending_activation"]
        assert fields["AGENT_SECURITY_DEVELOPMENT_MODEL"]["inherited_from"] == "AGENT_LLM_MODEL"
        assert fields["AGENT_FUSION_ANALYSIS_MODELS"]["display_value"] == "Provider-selected models"
        assert fields["AGENT_SECURITY_DECISION_MODEL_MODEL"]["display_value"] == "perplexity/pplx-decider-v1.2"
        assert fields["AGENT_LLM_OPENROUTER_API_KEY"]["display_value"] == "Configured"
        assert fields["AGENT_LLM_OPENROUTER_API_KEY"]["value"] == fields["AGENT_LLM_OPENROUTER_API_KEY"]["default"] == ""
        sections = {section["id"]: section for section in current["sections"]}
        assert sections["task"]["selection_keys"] == ["AGENT_LLM_MODEL", "AGENT_LLM_FALLBACK_MODEL"]
        assert sections["speech"]["selection_keys"] == ["AGENT_TTS_MODEL"]
        assert sections["tools"]["selection_keys"] == []
        selector_keys = {key for section in current["sections"] for key in section["selection_keys"]}
        assert "AGENT_LLM_TEMPERATURE" not in selector_keys
        assert "AGENT_TTS_VOICE" not in selector_keys
        assert "AGENT_TRANSCRIPTION_MODEL_PATH" in selector_keys
        links = sections["task"]["related_settings"]
        assert len({(link["group"], link["destination"]) for link in links}) == len(links)
        assert any(link["destination"] == "advanced" for link in links)
        groups = {group["id"]: group for group in
                  client.get("/v2/configuration/catalog", headers=headers).json()["groups"]}
        for section in current["sections"]:
            section_fields = {field["key"]: field for field in section["settings"]}
            assert set(section["selection_keys"]) <= section_fields.keys()
            for link in section["related_settings"]:
                assert link["group"] in groups
                assert link["field_key"] in section_fields
                assert link["field_key"] not in section["selection_keys"]
                assert link["destination"] in {"models", "advanced", "assistant", "safety", "connections"}
        assert sections["speech"]["enabled"] is False and sections["speech"]["settings"]
        assert sections["screening"]["enabled"] is False and sections["screening"]["settings"]
    finally:
        service.close()


def test_provider_model_catalog_is_authenticated_and_keeps_credentials_server_side(tmp_path):
    import json
    import shutil
    from pathlib import Path
    from test_management import configured

    source = Path(__file__).resolve().parents[2] / "config"
    (tmp_path / "src/config").mkdir(parents=True)
    for name in ("agent.yaml", "agent.schema.yaml", "AGENT.md.example"):
        shutil.copyfile(source / name, tmp_path / "src/config" / name)
    installation = configured(tmp_path)
    installation.values.update({
        "AGENT_LLM_PROVIDER": "openai",
        "AGENT_LLM_BASE_URL": "https://api.openai.com/v1",
        "AGENT_LLM_ZDR_ONLY": "true",
        "AGENT_LLM_ZDR_VERIFIED_PROVIDERS": "openai",
        "AGENT_LLM_ZDR_VERIFIED_MODELS": "openai:gpt-4.1",
    })
    installation.secret_values["AGENT_LLM_OPENAI_API_KEY"] = "private-model-key"
    installation.save()
    service = ManagementService(tmp_path)
    client = TestClient(create_app(tmp_path, TOKEN, service))
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "data": [{"id": "gpt-4.1"}, {"id": "gpt-4.1-mini"}],
    }).encode()
    try:
        with patch("assistant_agent.llm_catalog.read_secret", return_value="private-model-key"), \
                patch("assistant_agent.llm_catalog.open_https_request", return_value=response) as send:
            assert client.get("/v2/configuration/models", headers={"Host": "127.0.0.1"}).status_code == 401
            result = client.get("/v2/configuration/models", headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["provider"] == "openai"
        assert result.json()["zdr_only"] is True
        assert [model["id"] for model in result.json()["models"]] == ["gpt-4.1"]
        assert "private-model-key" not in result.text
        assert send.call_args.args[0].full_url == "https://api.openai.com/v1/models"
    finally:
        service.close()


def test_model_settings_overview_updates_inheritance_provider_defaults_and_activation(tmp_path):
    import shutil
    from pathlib import Path
    from test_management import configured

    source = Path(__file__).resolve().parents[2] / "config"
    (tmp_path / "src/config").mkdir(parents=True)
    for name in ("agent.yaml", "agent.schema.yaml", "AGENT.md.example"):
        shutil.copyfile(source / name, tmp_path / "src/config" / name)
    installation = configured(tmp_path)
    service = ManagementService(tmp_path)
    try:
        original = service.model_settings()
        installation.values.update(
            AGENT_DEEP_RESEARCH_MODEL="example/research",
            AGENT_SECURITY_DEVELOPMENT_MODEL="example/supervisor",
            AGENT_ROUTING_MODEL="",
            AGENT_NAMING_MODEL="",
            AGENT_LIMITS_SUMMARIZATION_MODEL="",
            AGENT_SECURITY_DECISION_MODEL_PROVIDER="perplexity",
            AGENT_SECURITY_DECISION_MODEL_MODEL="",
            AGENT_SECURITY_DECISION_MODEL_URL="",
        )
        installation.state["pending_configuration"] = ["agent.yaml"]
        installation.save()
        updated = service.model_settings()
        assert updated["revision"] != original["revision"]
        fields = {field["key"]: field for section in updated["sections"] for field in section["settings"]}
        assert fields["AGENT_DEEP_RESEARCH_MODEL"]["display_value"] == "example/research"
        assert fields["AGENT_DEEP_RESEARCH_MODEL"]["value_source"] == "configured"
        for key in ("AGENT_ROUTING_MODEL", "AGENT_NAMING_MODEL", "AGENT_LIMITS_SUMMARIZATION_MODEL"):
            assert fields[key]["display_value"] == "model"
            assert fields[key]["value_source"] == "inherited"
        assert fields["AGENT_SECURITY_DECISION_MODEL_MODEL"]["display_value"] == "pplx-decider-latest"
        assert fields["AGENT_SECURITY_DECISION_MODEL_URL"]["display_value"] == "https://api.perplexity.ai/v1/decisions"
        assert all(field["pending_activation"] for field in fields.values())
        installation.state["pending_configuration"] = []
        installation.save()
        active = service.model_settings()
        assert active["activation_tracked"] and not active["pending_activation"]
        installation.state.pop("pending_configuration")
        installation.save()
        assert not service.model_settings()["activation_tracked"]
    finally:
        service.close()


def test_v2_approval_settings_share_a_safety_page(tmp_path):
    (tmp_path / "src/config").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "src/config/agent.yaml").write_text("agent:\n  identity:\n    name: My instance\n")
    client = TestClient(create_app(tmp_path, TOKEN))
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}

    response = client.get("/v2/configuration/settings?group=approvals", headers=headers)
    assert response.status_code == 200
    result = response.json()
    approval_page = next(group for group in result["groups"] if group["id"] == "approvals")
    assert approval_page["destination"] == "safety" and approval_page["basic"] and approval_page["parent"] is None
    fields = {item["key"]: item["metadata"] for item in result["settings"]}
    assert fields.keys() == {"AGENT_ADMIN_EMAIL", "AGENT_APPROVALS_CHANNEL"}
    assert all(field["level"] == "basic" for field in fields.values())
    assert fields["AGENT_APPROVALS_CHANNEL"]["choices"] == ["email", "telegram", "both", "client"]

    response = client.get("/v2/configuration/settings?group=catalog:routing", headers=headers)
    assert response.status_code == 200
    naming = next(item for item in response.json()["settings"] if item["key"] == "AGENT_NAMING_MODEL")
    assert naming["group"] == "catalog:routing"
    assert naming["metadata"]["level"] == "advanced"
    assert naming["metadata"]["section"] == "Conversation naming"


def test_v2_configuration_preview_is_revision_checked_and_secret_safe(tmp_path):
    (tmp_path / "src/config").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "src/config/agent.yaml").write_text(
        "agent:\n  identity:\n    name: Assistant\n  app:\n    timezone: UTC\n"
        "  llm:\n    base_url: https://openrouter.ai/api/v1\n    model: example/model\n"
        "  embeddings:\n    enabled: false\n"
    )
    (tmp_path / "src/config/AGENT.md.example").write_text("Instructions\n")
    client = TestClient(create_app(tmp_path, TOKEN))
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}
    current = client.get("/v2/configuration", headers=headers).json()
    draft = {
        "revision": current["revision"],
        "values": {
            "AGENT_IDENTITY_NAME": " New assistant ",
            "AGENT_DATABASE_PASSWORD": "new-database-password-value",
            "AGENT_LLM_OPENROUTER_API_KEY": "new-private-key-value",
        },
        "instructions": "Updated instructions",
        "acknowledge": True,
    }
    response = client.post("/v2/configuration/preview", headers=headers, json=draft)
    assert response.status_code == 200
    preview = response.json()
    assert preview["valid"]
    assert "new-private-key" not in response.text
    assert any(item["key"] == "AGENT_LLM_OPENROUTER_API_KEY" and item["after"] == "[redacted]"
               for item in preview["changes"])
    stale = {**draft, "revision": "0" * 64}
    assert client.post("/v2/configuration/preview", headers=headers, json=stale).status_code == 409
    unknown = {**draft, "values": {"NOT_A_SETTING": "value"}}
    assert client.post("/v2/configuration/preview", headers=headers, json=unknown).status_code == 409


def test_v2_catalog_settings_preview_save_and_reload(tmp_path):
    from pathlib import Path
    import time

    from assistant_agent.settings_schema import catalog
    from assistant_agent.setup import Installation, parse_environment
    from test_management import configured

    source = Path(__file__).resolve().parents[2] / "config"
    (tmp_path / "src/config").mkdir(parents=True)
    for name in ("agent.yaml", "agent.schema.yaml", "AGENT.md.example"):
        (tmp_path / "src/config" / name).write_bytes((source / name).read_bytes())
    configured(tmp_path)
    client = TestClient(create_app(tmp_path, TOKEN))
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}
    settings_response = client.get("/v2/configuration", headers=headers)
    assert settings_response.status_code == 200
    current = settings_response.json()
    fields = {item["key"]: item for item in current["settings"]}
    assert {item["env"] for item in catalog()} <= fields.keys()
    assert fields["AGENT_LLM_TEMPERATURE"]["data_type"] == "number"
    assert fields["AGENT_LLM_ALLOWED_PROVIDERS"]["metadata"]["level"] == "basic"
    assert fields["AGENT_TRANSCRIPTION_API_TOKEN"]["secret"]
    assert not fields["AGENT_SANDBOX_REVOKE_TOKEN"]["editable"]
    assert fields["AGENT_CALENDAR_GOOGLE_2_CLIENT_SECRET"]["secret"]
    assert fields["AGENT_CALENDAR_GOOGLE_2_CALENDAR_ID"]["editable"]
    assert fields["TASK_AGENT_WORKERS"]["data_type"] == "integer"
    assert fields["SANDBOX_EGRESS_MODE"]["metadata"]["warning"]
    assert not fields["ASSISTANT_INSTALL_ROOT"]["editable"]
    catalog_response = client.get("/v2/configuration/catalog", headers=headers).json()
    assert next(group for group in catalog_response["groups"] if group["id"] == "catalog:web_fetch")["destination"] == "models"
    assert fields["AGENT_WEB_FETCH_ENABLED"]["metadata"]["level"] == "basic"
    for key in ("AGENT_WEB_FETCH_BASE_URL", "AGENT_WEB_FETCH_API_TOKEN"):
        assert fields[key]["metadata"]["level"] == "advanced"
    search_section = client.get("/v2/configuration/settings", params={"group": "catalog:search"}, headers=headers).json()
    search_fields = {item["key"]: item for item in search_section["settings"]}
    assert search_section["revision"] == catalog_response["revision"]
    assert search_fields["AGENT_SEARCH_PRIMARY_PROVIDER"]["metadata"]["choices"] == ["openrouter", "exa"]
    assert search_fields["AGENT_SEARCH_EXA_API_KEY"]["secret"]
    for protected in ("AGENT_DATABASE_PASSWORD", "AGENT_SANDBOX_API_TOKEN", "AGENT_SANDBOX_REVOKE_TOKEN",
                      "AGENT_WEB_FETCH_MANAGED_TOKEN"):
        assert client.post("/v2/configuration/preview", headers=headers, json={
            "revision": current["revision"], "values": {}, "remove_credentials": [protected],
        }).status_code == 409

    secret = "private-transcription-token-" + "x" * 32
    exa_key = "private-exa-provider-test-key"
    scraper_key = "private-crawler-token-test-key"
    draft = {
        "revision": current["revision"],
        "values": {
            "AGENT_LLM_TEMPERATURE": "0.35",
            "AGENT_LLM_ALLOWED_PROVIDERS": "deepinfra,azure",
            "AGENT_MEMORY_STEWARD_MAX_TRANSCRIPT_BYTES": "123456",
            "AGENT_TRANSCRIPTION_API_TOKEN": secret,
            "AGENT_CALENDAR_GOOGLE_2_CALENDAR_ID": "work-calendar",
            "AGENT_CALENDAR_GOOGLE_2_CLIENT_ID": "calendar-client",
            "AGENT_CALENDAR_GOOGLE_2_CLIENT_SECRET": "calendar-private-secret",
            "AGENT_CALENDAR_GOOGLE_2_REFRESH_TOKEN": "calendar-private-refresh",
            "TASK_AGENT_WORKERS": "3",
            "SANDBOX_RUN_CPUS": "1.5",
            "AGENT_SEARCH_PRIMARY_PROVIDER": "exa",
            "AGENT_SEARCH_EXA_ENABLED": "true",
            "AGENT_SEARCH_EXA_API_KEY": exa_key,
            "AGENT_WEB_FETCH_ENABLED": "true",
            "AGENT_WEB_FETCH_BASE_URL": "https://scraper.example.test",
            "AGENT_WEB_FETCH_API_TOKEN": scraper_key,
        },
    }
    invalid = {**draft, "values": {"AGENT_LLM_TEMPERATURE": "not-a-number"}}
    response = client.post("/v2/configuration/preview", headers=headers, json=invalid)
    assert response.status_code == 200
    assert not response.json()["valid"]
    invalid_providers = {**draft, "values": {"AGENT_LLM_ALLOWED_PROVIDERS": "invalid provider"}}
    assert not client.post("/v2/configuration/preview", headers=headers, json=invalid_providers).json()["valid"]
    nonfinite = {**draft, "values": {"AGENT_LLM_TEMPERATURE": "nan"}}
    assert not client.post("/v2/configuration/preview", headers=headers, json=nonfinite).json()["valid"]
    for key, value in (("TASK_AGENT_WORKERS", "-1"), ("SANDBOX_RUN_CPUS", "nan"),
                       ("SANDBOX_EGRESS_MODE", "anything"), ("ASSISTANT_MANAGEMENT_PORT", "65536"),
                       ("SANDBOX_RUN_DNS_SERVERS", "not-an-address"),
                       ("SANDBOX_RUN_TMPFS_SIZE", "-1"), ("SANDBOX_RUN_NETWORK_MODE", "existing"),
                       ("EGRESS_PROXY_MAX_CONNECTIONS", "0"), ("LOG_LEVEL", "NOT_A_LEVEL"),
                       ("AGENT_FILESYSTEM_SHARED_FILE_UMASK", "9999")):
        assert not client.post("/v2/configuration/preview", headers=headers, json={
            **draft, "values": {key: value},
        }).json()["valid"]
    assert client.post("/v2/configuration/preview", headers=headers, json={
        **draft, "values": {"SANDBOX_RUN_NETWORK_MODE": "existing", "SANDBOX_RUN_NETWORK": "private-network"},
    }).json()["valid"]
    assert client.post("/v2/configuration/preview", headers=headers, json={
        **draft, "values": {"ASSISTANT_INSTALL_ROOT": "/elsewhere"},
    }).status_code == 409
    preview_response = client.post("/v2/configuration/preview", headers=headers, json=draft)
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["valid"], preview["problems"]
    assert secret not in preview_response.text
    assert exa_key not in preview_response.text and scraper_key not in preview_response.text
    assert client.post("/v2/configuration/preview", headers=headers, json={
        "revision": current["revision"], "values": {"AGENT_WEB_FETCH_ENABLED": "true"},
    }).json()["valid"]
    for bad_values in (
        {"AGENT_SEARCH_PRIMARY_PROVIDER": "unsupported"},
        {"AGENT_SEARCH_OPENROUTER_ENABLED": "false", "AGENT_SEARCH_EXA_ENABLED": "false"},
        {"AGENT_WEB_FETCH_ENABLED": "true", "AGENT_WEB_FETCH_BASE_URL": "https://scraper.example.test"},
    ):
        assert not client.post("/v2/configuration/preview", headers=headers, json={
            "revision": current["revision"], "values": bad_values,
        }).json()["valid"]
    restricted = {**draft, "values": {"AGENT_SANDBOX_REVOKE_TOKEN": "x" * 32}}
    assert client.post("/v2/configuration/preview", headers=headers, json=restricted).status_code == 409

    response = client.post(
        "/v2/operations", headers={**headers, "Idempotency-Key": "save-catalog-settings"},
        json={**draft, "operation": "configuration.save",
              "preview_id": preview["preview_id"], "confirmed": True},
    )
    assert response.status_code == 202
    manager = client.app.state.management_operations
    for _ in range(200):
        state = manager.get(response.json()["id"])
        if state["state"] in {"succeeded", "failed"}:
            break
        time.sleep(0.01)
    assert state["state"] == "succeeded", state
    replay = client.post(
        "/v2/operations", headers={**headers, "Idempotency-Key": "save-catalog-settings"},
        json={**draft, "operation": "configuration.save",
              "preview_id": preview["preview_id"], "confirmed": True},
    )
    assert replay.status_code == 202
    assert replay.json()["id"] == response.json()["id"]
    saved = Installation(tmp_path)
    assert saved.values["AGENT_LLM_TEMPERATURE"] == "0.35"
    assert saved.values["AGENT_LLM_ALLOWED_PROVIDERS"] == "deepinfra,azure"
    assert saved.values["AGENT_MEMORY_STEWARD_MAX_TRANSCRIPT_BYTES"] == "123456"
    assert saved.secret_values["AGENT_TRANSCRIPTION_API_TOKEN"] == secret
    assert saved.values["AGENT_CALENDAR_GOOGLE_2_CALENDAR_ID"] == "work-calendar"
    assert saved.values["TASK_AGENT_WORKERS"] == "3"
    assert saved.values["SANDBOX_RUN_CPUS"] == "1.5"
    assert saved.values["AGENT_SEARCH_PRIMARY_PROVIDER"] == "exa"
    assert saved.secret_values["AGENT_SEARCH_EXA_API_KEY"] == exa_key
    assert saved.secret_values["AGENT_WEB_FETCH_API_TOKEN"] == scraper_key
    assert {service for service, grants in saved.credential_grants().items() if "AGENT_SEARCH_EXA_API_KEY" in grants} == {
        "task-agent", "deep-research-agent", "heartbeat",
    }
    assert saved.secret_values["AGENT_CALENDAR_GOOGLE_2_CLIENT_SECRET"] == "calendar-private-secret"
    assert parse_environment((tmp_path / "data/overrides.env").read_text())[0][
        "AGENT_CALENDAR_GOOGLE_2_CALENDAR_ID"
    ] == "work-calendar"
    assert "AGENT_CALENDAR_GOOGLE_2_CALENDAR_ID" not in (tmp_path / "data/.env").read_text()
    assert "calendar-private-secret" not in preview_response.text
    assert "calendar-private-secret" not in (tmp_path / "data/.management-operations.json").read_text()
    assert secret not in (tmp_path / "data/overrides.env").read_text()
    assert secret not in (tmp_path / "data/.management-operations.json").read_text()
    reloaded = client.get("/v2/configuration", headers=headers)
    assert reloaded.status_code == 200
    assert secret not in reloaded.text
    assert exa_key not in reloaded.text and scraper_key not in reloaded.text
    assert next(item["value"] for item in reloaded.json()["settings"]
                if item["key"] == "AGENT_LLM_TEMPERATURE") == "0.35"
    removals = [f"AGENT_CALENDAR_GOOGLE_2_{name}"
                for name in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN")]
    removal_draft = {"revision": reloaded.json()["revision"], "values": {}, "remove_credentials": removals}
    removal_preview = client.post("/v2/configuration/preview", headers=headers, json=removal_draft).json()
    assert removal_preview["valid"], removal_preview["problems"]
    assert {row["key"] for row in removal_preview["changes"]} == set(removals)
    tampered = {**removal_draft, "remove_credentials": removals[:1], "operation": "configuration.save",
                "preview_id": removal_preview["preview_id"], "confirmed": True}
    assert client.post("/v2/operations", headers={**headers, "Idempotency-Key": "tampered-removal"},
                       json=tampered).status_code == 409
    response = client.post(
        "/v2/operations", headers={**headers, "Idempotency-Key": "remove-calendar-credentials"},
        json={**removal_draft, "operation": "configuration.save",
              "preview_id": removal_preview["preview_id"], "confirmed": True},
    )
    assert response.status_code == 202
    for _ in range(200):
        state = manager.get(response.json()["id"])
        if state["state"] in {"succeeded", "failed"}:
            break
        time.sleep(0.01)
    assert state["state"] == "succeeded", state
    assert not set(removals) & set(Installation(tmp_path).secret_values)
    assert not (tmp_path / "data/.secrets/AGENT_CALENDAR_GOOGLE_2_CLIENT_SECRET").exists()
    credential_status = client.get("/v2/credentials", headers=headers).json()["credentials"]
    assert all({"name": name, "status": "removal pending"} in credential_status for name in removals)
    assert client.get("/v2/credentials/storage", headers=headers).json() == {
        "mode": "host-files", "retirement_pending": False,
    }
    manager.executor.shutdown(wait=True)


def test_numbered_calendar_setting_is_migrated_from_compose_env(tmp_path):
    from assistant_agent.setup import Installation, parse_environment
    from test_management import configured

    configured(tmp_path)
    env = tmp_path / "data/.env"
    with env.open("a") as output:
        output.write("\nAGENT_CALENDAR_OUTLOOK_3_TENANT_ID='tenant-legacy'\n")
    Installation(tmp_path).save()
    assert "AGENT_CALENDAR_OUTLOOK_3_TENANT_ID" not in parse_environment(env.read_text())[0]
    overrides = parse_environment((tmp_path / "data/overrides.env").read_text())[0]
    assert overrides["AGENT_CALENDAR_OUTLOOK_3_TENANT_ID"] == "tenant-legacy"


def test_management_operation_keys_are_idempotent_and_payload_bound(tmp_path):
    (tmp_path / "data").mkdir()
    completed = []

    def execute(operation_id, action, payload, report):
        report("Working")
        completed.append((action, payload))

    manager = OperationManager(tmp_path, execute)
    first = manager.submit("service.lifecycle", {"action": "restart"}, "restart-key-001")
    replay = manager.submit("service.lifecycle", {"action": "restart"}, "restart-key-001")
    assert replay["id"] == first["id"]
    with pytest.raises(SetupError, match="different operation"):
        manager.submit("service.lifecycle", {"action": "stop"}, "restart-key-001")
    for _ in range(100):
        result = manager.get(first["id"])
        if result["state"] in {"succeeded", "failed"}:
            break
        import time
        time.sleep(0.01)
    assert result["state"] == "succeeded"
    assert completed == [("service.lifecycle", {"action": "restart"})]
    manager.executor.shutdown(wait=True)


def test_file_operation_results_expose_only_safe_identifiers():
    from assistant_agent.management_operations import operation_result

    assert operation_result("file.upload.project.4", {
        "workstream_id": 4, "file_id": 9, "object_ids": [11], "content": "private",
    }) == {"completed": True, "workstream_id": 4, "file_id": 9, "object_ids": [11]}
    with pytest.raises(SetupError, match="valid identifiers"):
        operation_result("file.attach", {"workstream_id": 4})


def test_operation_status_refreshes_after_api_restart_and_external_completion(tmp_path):
    import threading

    from assistant_agent.management_operations import persist_external_result

    running = threading.Event()
    release = threading.Event()

    def execute(operation_id, action, payload, report):
        running.set()
        assert release.wait(5)

    original = OperationManager(tmp_path, execute)
    resumed = None
    try:
        operation = original.submit("service.lifecycle", {"action": "restart"}, "restart-status-test")
        assert running.wait(5)
        resumed = OperationManager(tmp_path, execute)
        assert resumed.get(operation["id"])["state"] == "interrupted"
        persist_external_result(tmp_path, operation["id"], succeeded=True)
        assert resumed.get(operation["id"])["state"] == "succeeded"
        assert resumed.list()[0]["state"] == "succeeded"
    finally:
        release.set()
        original.executor.shutdown(wait=True)
        if resumed:
            resumed.executor.shutdown(wait=True)


def test_v2_operation_endpoint_requires_confirmation_and_idempotency(tmp_path):
    service = Mock()
    client = TestClient(create_app(tmp_path, TOKEN, service))
    headers = {
        "Host": "127.0.0.1",
        "Authorization": "Bearer " + TOKEN,
        "Idempotency-Key": "restart-service-001",
    }
    request = {
        "operation": "service.lifecycle", "action": "restart",
        "service": "task-agent", "confirmed": True,
    }
    first = client.post("/v2/operations", headers=headers, json=request)
    assert first.status_code == 202
    replay = client.post("/v2/operations", headers=headers, json=request)
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]
    conflict = client.post(
        "/v2/operations", headers=headers,
        json={**request, "action": "stop"},
    )
    assert conflict.status_code == 409
    assert client.get(
        "/v2/operations/" + first.json()["id"],
        headers={"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN},
    ).status_code == 200
    assert service.execute_operation.call_count == 1
    manager = client.app.state.management_operations
    manager.executor.shutdown(wait=True)


def test_management_task_conversation_and_research_reply(db, tmp_path, monkeypatch):
    import time

    from assistant_agent.replies import ReplyRouter

    monkeypatch.delenv("ASSISTANT_MANAGEMENT_EXECUTOR_URL", raising=False)
    backend = Mock()
    backend.request.side_effect = lambda payload: execute_management(db, AppConfig({}), payload)
    service = ManagementService(tmp_path)
    service.backend = lambda: backend
    client = TestClient(create_app(tmp_path, TOKEN, service))
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}

    def submit(payload, key):
        response = client.post("/v2/operations", headers={**headers, "Idempotency-Key": key}, json=payload)
        assert response.status_code == 202
        manager = client.app.state.management_operations
        for _ in range(200):
            operation = manager.get(response.json()["id"])
            if operation["state"] in {"succeeded", "failed"}:
                return operation
            time.sleep(0.01)
        pytest.fail("operation did not complete")

    start = {"operation": "task.start", "body": "Research this question", "client_id": CLIENT_ID, "confirmed": True}
    operation = submit(start, "management-start-001")
    assert operation["state"] == "succeeded", operation
    task_id = operation["result"]["task_id"]
    job = db.fetch_one("SELECT * FROM jobs WHERE id = %s", (task_id,))
    assert job["status"] == "queued"
    assert job["metadata"]["source_channel"] == "client"
    assert job["metadata"]["auto_reply"] is True
    summary = client.get("/v2/tasks/summary", headers=headers)
    assert summary.status_code == 200
    assert {"status": "queued", "count": 1} in summary.json()["jobs"]
    assert submit(start, "management-start-001")["id"] == operation["id"]
    assert db.fetch_one("SELECT count(*) AS n FROM request_messages WHERE thread_id = %s",
                        (job["thread_id"],))["n"] == 1

    runtime = SimpleNamespace(db=db, config=AppConfig({}), job=job)
    assert ReplyRouter(runtime).send("What period?", purpose="clarification")["status"] == "queued"
    assert ReplyRouter(runtime).send("What period?", purpose="clarification")["status"] == "queued"
    assert sum(row.get("context_type") == "outbound_client"
               for row in db.latest_thread_messages(job["thread_id"])) == 1
    db.execute("UPDATE jobs SET status = 'waiting' WHERE id = %s", (task_id,))
    reply = submit(
        {"operation": "task.reply", "task_id": task_id, "body": "Use November",
         "client_id": CLIENT_ID, "confirmed": True},
        "management-reply-001",
    )
    assert reply["state"] == "succeeded", reply
    assert reply["result"]["task_id"] == task_id
    assert db.fetch_one("SELECT status FROM jobs WHERE id = %s", (task_id,))["status"] == "queued"
    messages = client.get(f"/v2/tasks/{task_id}/messages", headers=headers).json()
    assert [row["body"] for row in messages["rows"]] == [
        "Research this question", "What period?", "Use November",
    ]

    db.execute("UPDATE jobs SET status = 'waiting' WHERE id = %s", (task_id,))
    run = db.for_job(task_id).fetch_one(
        """INSERT INTO deep_research_runs(original_job_id, original_thread_id, title, research_question, status)
           VALUES (%s, %s, 'Research', 'Question', 'waiting_for_input') RETURNING id""",
        (task_id, job["thread_id"]),
    )
    db.execute(
        """INSERT INTO deep_research_events(run_id, sequence, event_type, output_data)
           VALUES (%s, 1, 'status_change', '{"status":"waiting_for_input"}')""",
        (run["id"],),
    )
    research = client.get("/v2/records/research", headers=headers).json()
    assert any(row["id"] == run["id"] for row in research["rows"])
    details = client.get(f"/v2/records/research/{run['id']}", headers=headers).json()
    assert details["rows"][0]["status"] == "waiting_for_input"
    events = client.get(f"/v2/records/research/{run['id']}/children", headers=headers).json()
    assert events["rows"][0]["event_type"] == "status_change"
    research_reply = submit(
        {"operation": "task.reply", "task_id": task_id, "body": "Use December",
         "client_id": CLIENT_ID, "confirmed": True},
        "management-research-reply-001",
    )
    assert research_reply["state"] == "succeeded", research_reply
    assert db.fetch_one("SELECT status FROM deep_research_runs WHERE id = %s", (run["id"],))["status"] == "queued"
    db.execute("UPDATE jobs SET status = 'completed' WHERE id = %s", (task_id,))
    followup = submit(
        {"operation": "task.reply", "task_id": task_id, "body": "One more question",
         "client_id": CLIENT_ID, "confirmed": True},
        "management-followup-001",
    )
    assert followup["state"] == "succeeded", followup
    assert followup["result"]["task_id"] != task_id
    assert followup["result"]["workstream_id"] == operation["result"]["workstream_id"]
    other = db.create_manual_job("Other channel", "Other body")
    cross_channel = submit(
        {"operation": "task.reply", "task_id": other["id"], "body": "Reply here",
         "client_id": CLIENT_ID, "confirmed": True},
        "management-cross-channel",
    )
    assert cross_channel["state"] == "succeeded", cross_channel
    assert db.fetch_one("SELECT metadata FROM jobs WHERE id = %s", (other["id"],))["metadata"]["reply_route"] == {
        "channel": "client", "client_id": CLIENT_ID,
    }
    assert "Research this question" not in (tmp_path / "data/.management-operations.json").read_text()
    client.app.state.management_operations.executor.shutdown(wait=True)


@pytest.mark.parametrize("channel", ["email", "telegram"])
def test_management_reply_uses_latest_request_channel(db, channel):
    from assistant_agent.replies import ReplyRouter
    from assistant_agent.requests import IncomingRequest, RequestService
    from assistant_agent.task_agent import TaskAgent

    thread = "cross-channel-" + channel
    route = ({"channel": "email", "to": "person@example.com"} if channel == "email"
             else {"channel": "telegram", "chat_id": "123"})

    def receive(external_id, body):
        email_id = None
        if channel == "email":
            email = db.fetch_one(
                """INSERT INTO emails(message_id, thread_id, from_address, body_text, received_at)
                   VALUES (%s, %s, 'person@example.com', %s, now()) RETURNING id""",
                (external_id, thread, body),
            )
            email_id = email["id"]
        return RequestService(db).receive_request(IncomingRequest(
            channel, external_id, thread, "person@example.com" if channel == "email" else "123",
            thread, body, reply_route=route, email_id=email_id,
        ))

    receive("first-" + channel, "Original question")
    job = db.fetch_one("SELECT * FROM jobs WHERE thread_id = %s", (thread,))
    result = execute_management(db, AppConfig({}), {
        "kind": "task.reply", "task_id": job["id"], "body": "Reply through the API",
        "request_key": "cross-channel-key", "client_id": CLIENT_ID, "confirmed": True,
    })
    current = db.fetch_one("SELECT * FROM jobs WHERE id = %s", (result["task_id"],))
    assert current["metadata"]["reply_route"] == {"channel": "client", "client_id": CLIENT_ID}
    assert not TaskAgent(db, AppConfig({})).completion_blocker(current, "An answer")
    assert ReplyRouter(SimpleNamespace(db=db, config=AppConfig({}), job=current)).send("API answer")["status"] == "queued"
    messages = execute_management(db, AppConfig({}), {"kind": "task-messages", "id": job["id"]})
    assert {(item["channel"], item["body"]) for item in messages["rows"]} >= {
        (channel, "Original question"), ("client", "Reply through the API"), ("client", "API answer"),
    }
    receive("second-" + channel, "Reply through the original channel")
    current = db.fetch_one("SELECT metadata FROM jobs WHERE id = %s", (job["id"],))
    assert current["metadata"]["reply_route"] == route


def test_conversations_list_human_threads_with_their_latest_task(db):
    from assistant_agent.requests import IncomingRequest, RequestService

    def receive(channel, thread, external_id, body):
        RequestService(db).receive_request(IncomingRequest(
            channel, external_id, thread, "person", thread, body,
        ))

    receive("telegram", "chat-a", "a-1", "Plan the offsite")
    receive("telegram", "chat-a", "a-2", "Add a dinner")
    receive("client", "chat-b", "b-1", "Summarise 50% of the report")
    everything = execute_management(db, AppConfig({}), {"kind": "conversations"})
    assert everything["total"] == 2
    threads = {row["thread_id"]: row for row in everything["rows"]}
    assert threads["chat-a"]["message_count"] == 2
    assert threads["chat-a"]["opening_message"] == "Plan the offsite"
    assert threads["chat-a"]["latest_task_id"] == db.fetch_one(
        "SELECT max(id) AS id FROM jobs WHERE thread_id = 'chat-a'")["id"]
    telegram = execute_management(db, AppConfig({}), {"kind": "conversations", "channel": "telegram"})
    assert [row["thread_id"] for row in telegram["rows"]] == ["chat-a"]
    search = execute_management(db, AppConfig({}), {"kind": "conversations", "query": "50%"})
    assert [row["thread_id"] for row in search["rows"]] == ["chat-b"]
    assert execute_management(db, AppConfig({}), {"kind": "conversations", "query": "5_%"})["total"] == 0


@pytest.mark.parametrize("backfilled", [False, True])
def test_scheduled_threads_are_read_only_and_separate_from_chats(db, backfilled):
    from assistant_agent.requests import IncomingRequest, RequestService

    config = AppConfig({})
    thread = "reminder:123"
    requests = RequestService(db)
    requests.receive_request(IncomingRequest(
        "reminder", "reminder:123:daily", thread, thread, thread, "Scheduled instructions",
        reply_route={"channel": "telegram", "chat_id": "123"}, metadata={"reminder_id": 123},
    ))
    requests.receive_request(IncomingRequest(
        "client", "human-chat", "human-chat", CLIENT_ID, "human-chat", "A real message",
    ))
    db.execute(
        """INSERT INTO emails(message_id, thread_id, from_address, subject, body_text, received_at)
           VALUES ('legacy-reminder', 'reminder:456', 'reminder@local', 'Older reminder', 'Legacy instructions', now());
           INSERT INTO jobs(thread_id, task_summary) VALUES ('reminder:456', 'Older reminder')"""
    )
    if not backfilled:
        db.execute("DELETE FROM request_messages WHERE channel = 'local' AND external_id = 'legacy-reminder'")
    scheduled = execute_management(db, config, {"kind": "conversations", "channel": "reminder"})
    assert {row["thread_id"] for row in scheduled["rows"]} == {thread, "reminder:456"}
    chats = execute_management(db, config, {"kind": "conversations", "channel": "chat"})
    assert [row["thread_id"] for row in chats["rows"]] == ["human-chat"]
    for row in scheduled["rows"]:
        assert row["message_count"] == 1
        task = row["latest_task_id"]
        messages = execute_management(db, config, {"kind": "task-messages", "id": task})
        assert [(msg["kind"], msg["channel"]) for msg in messages["rows"]] == [("request", "reminder")]
        with pytest.raises(ValueError, match="Scheduled reminder runs are read-only"):
            execute_management(db, config, {
                "kind": "task.reply", "task_id": task, "body": "Do it again",
                "request_key": "must-not-send", "client_id": CLIENT_ID, "confirmed": True,
            })
    assert db.fetch_one("SELECT count(*) AS total FROM request_messages WHERE body_text = 'Do it again'")["total"] == 0
    assert db.fetch_one("SELECT count(*) AS total FROM jobs WHERE thread_id LIKE 'reminder:%'")["total"] == 2


def test_task_activity_spans_the_thread_and_pairs_results(db):
    from assistant_agent.requests import IncomingRequest, RequestService

    first = db.create_manual_job("Research", "Find the venue")
    db.execute("UPDATE jobs SET status = 'completed' WHERE id = %s", (first["id"],))
    db.log_event(first["id"], "llm_request", input_data={"message_count": 1})
    db.log_event(first["id"], "tool_call", tool_name="web_search", input_data={"query": "venues"})
    db.log_event(first["id"], "tool_result", tool_name="web_search", output_data={"results": 3})
    db.log_event(first["id"], "tool_result", tool_name="reply_send", output_data={"status": "sent"})
    RequestService(db).receive_request(IncomingRequest(
        "email", "follow-up", first["thread_id"], "person", first["thread_id"],
        "Book it\n\nOn Mon, 3 Mar 2025 at 10:00, Assistant <a@example.com> wrote:\n> Found three venues",
    ))
    second = db.fetch_one("SELECT id FROM jobs WHERE thread_id = %s ORDER BY id DESC LIMIT 1",
                          (first["thread_id"],))
    assert second["id"] != first["id"]
    db.log_event(second["id"], "tool_call", tool_name="calendar", input_data={"title": "x" * 5000})
    db.log_event(second["id"], "error", tool_name="calendar", output_data={"error": "busy"})
    db.log_event(second["id"], "tool_call", tool_name="email_lookup", input_data={"id": 1})
    db.execute("UPDATE jobs SET status = 'running' WHERE id = %s", (second["id"],))

    activity = execute_management(db, AppConfig({}), {"kind": "task-activity", "id": second["id"]})
    assert [(row["tool_name"], row["status"]) for row in activity["rows"]] == [
        ("web_search", "succeeded"), ("calendar", "failed"), ("email_lookup", "running"),
    ]
    assert activity["rows"][0]["input"] == {"query": "venues"}
    assert activity["rows"][0]["output"] == {"results": 3}
    assert activity["rows"][1]["input"].endswith("…") and len(activity["rows"][1]["input"]) <= 4001
    assert activity["truncated"] is False
    later = execute_management(db, AppConfig({}), {
        "kind": "task-activity", "id": first["id"], "since": "2999-01-01T00:00:00+00:00",
    })
    assert later["rows"] == []
    messages = execute_management(db, AppConfig({}), {"kind": "task-messages", "id": first["id"]})
    assert "Book it" in [row["body"] for row in messages["rows"] if row["channel"] == "email"]


def test_activity_marks_unpaired_retries_interrupted_without_merging_distinct_calls():
    from assistant_agent.management_operator import _task_activity

    events = [
        {"job_id": 7, "sequence": sequence, "created_at": f"2026-11-16T10:00:0{sequence}Z",
         "event_type": kind, "tool_name": "task_complete", "tool_action": None,
         "input_data": {"response": "Answer"} if kind == "tool_call" else None,
         "output_data": {"response": "Answer"} if kind == "tool_result" else None,
         "duration_ms": None, "task_status": "completed"}
        for sequence, kind in enumerate(["tool_call", "tool_call", "tool_result"], start=1)
    ]
    database = Mock()
    database.fetch_one.return_value = {"thread_id": "thread"}
    database.fetch_all.return_value = list(reversed(events))
    result = _task_activity(database, 7)
    assert [(row["id"], row["status"]) for row in result["rows"]] == [
        ("7:1", "interrupted"), ("7:2", "succeeded"),
    ]
    assert result["rows"][1]["input"] == result["rows"][1]["output"]


def test_message_attachment_references_are_contained_and_missing_files_are_explicit(tmp_path):
    from assistant_agent.management_operator import _message_attachments

    shared = tmp_path / "workspace"
    shared.mkdir()
    audio = shared / "note.mp3"
    audio.write_bytes(b"audio")
    config = AppConfig({"agent": {"filesystem": {"shared_root": str(shared)}}})
    tracker = SimpleNamespace(shared_root=shared)
    tracker._safe_path = lambda path: path.resolve() if shared in path.resolve().parents else (_ for _ in ()).throw(ValueError("outside"))
    database = Mock()
    database.fetch_one.return_value = {"id": 8}
    message = {"workstream_id": 4, "attachments": [
        {"path": str(audio), "filename": "note.mp3", "content_type": "audio/mpeg", "voice_note": True},
        {"path": str(tmp_path / "private.txt"), "filename": "private.txt"},
        {"path": str(shared / "gone.pdf"), "filename": "gone.pdf"},
    ]}
    with patch("assistant_agent.management_operator.WorkstreamFileTracker", return_value=tracker):
        attachments = _message_attachments(database, config, message)
    assert attachments[0] == {
        "filename": "note.mp3", "content_type": "audio/mpeg", "voice_note": True,
        "available": True, "file_id": 8, "project_id": 4,
    }
    assert attachments[1:] == [
        {"filename": "private.txt", "available": False}, {"filename": "gone.pdf", "available": False},
    ]


@pytest.mark.parametrize("stored_digest", [None, "0" * 64])
def test_project_file_download_hashes_current_bytes_not_tracking_metadata(tmp_path, stored_digest):
    import base64
    import hashlib

    audio = tmp_path / "note.mp3"
    audio.write_bytes(b"synthetic audio")
    config = AppConfig({"agent": {"filesystem": {"shared_root": str(tmp_path)}}})
    database = Mock()
    database.fetch_one.return_value = {"path": str(audio), "sha256": stored_digest}
    for offset in (0, 4):
        content = execute_management(database, config, {
            "kind": "project-file-content", "id": 4, "file_id": 8, "offset": offset, "limit": 4,
        })
        assert content["sha256"] == hashlib.sha256(audio.read_bytes()).hexdigest()
        assert base64.b64decode(content["content_base64"]) == audio.read_bytes()[offset:offset + 4]
        assert content["next_offset"] == offset + 4


def test_transcript_audio_attachment_downloads_through_its_project(db, tmp_path):
    import base64
    from psycopg.types.json import Jsonb
    from assistant_agent.workstream_files import WorkstreamFileTracker

    config = AppConfig({"agent": {"filesystem": {"shared_root": str(tmp_path)}}})
    job = db.create_manual_job("Audio reply", "Send a voice note")
    audio = tmp_path / "note.mp3"
    audio.write_bytes(b"synthetic audio")
    WorkstreamFileTracker(db, config, job["id"]).track_file(audio)
    db.execute(
        """INSERT INTO channel_deliveries(job_id, channel, destination, request_key, attachments, status)
           VALUES (%s, 'telegram', '123', 'audio-fixture', %s, 'sent')""",
        (job["id"], Jsonb([{"path": str(audio), "filename": audio.name,
                           "content_type": "audio/mpeg", "voice_note": True}])),
    )
    transcript = execute_management(db, config, {"kind": "task-messages", "id": job["id"]})
    attachment = next(row for row in transcript["rows"] if row["channel"] == "telegram")["attachments"][0]
    assert attachment["available"] is True and attachment["voice_note"] is True
    assert "path" not in attachment
    content = execute_management(db, config, {
        "kind": "project-file-content", "id": attachment["project_id"], "file_id": attachment["file_id"],
    })
    assert base64.b64decode(content["content_base64"]) == audio.read_bytes()


def test_task_messages_include_sent_mail_once_and_paginate_stably(db):
    from assistant_agent.requests import IncomingRequest, RequestService

    job = db.create_manual_job("History", "Original")
    for index in range(51):
        RequestService(db).receive_request(IncomingRequest(
            "telegram", f"history-{index}", job["thread_id"], "person", job["thread_id"], f"Message {index}",
        ), queue=False)
    db.execute(
        """INSERT INTO outbound_email_logs(job_id, to_addresses, subject, body_text, status)
           VALUES (%s, ARRAY['person@example.com'], 'Reply', 'One email', 'sent'),
                  (%s, ARRAY['admin@example.com'], 'Admin reply', 'Admin email', 'sent')""",
        (job["id"], job["id"]),
    )
    db.execute(
        """INSERT INTO channel_deliveries(job_id, channel, destination, request_key, body_text, status)
           VALUES (%s, 'email', 'person@example.com', 'history-mail', 'One email', 'sent')""",
        (job["id"],),
    )
    config = AppConfig({"agent": {"admin": {"email": "ADMIN@example.com"}}})
    first = execute_management(db, config, {"kind": "task-messages", "id": job["id"]})
    second = execute_management(db, config, {"kind": "task-messages", "id": job["id"], "page": 1})
    rows = first["rows"] + second["rows"]
    assert first["total"] == second["total"] == len(rows) == 54
    assert len(first["rows"]) == 50
    assert sum(row["body"] == "One email" for row in rows) == 1
    assert len({(row["channel"], row["kind"], row["id"]) for row in rows}) == len(rows)
    external = next(row for row in rows if row["body"] == "One email")
    assert external["external"] is True and external["recipients"] == ["person@example.com"]
    assert external["subject"] == "Reply"
    assert next(row for row in rows if row["body"] == "Admin email")["external"] is False
    assert execute_management(db, config, {"kind": "task-messages", "id": job["id"]}) == first


def test_management_permission_controls(db, tmp_path, monkeypatch):
    import time

    monkeypatch.delenv("ASSISTANT_MANAGEMENT_EXECUTOR_URL", raising=False)
    backend = Mock()
    backend.request.side_effect = lambda payload: execute_management(db, AppConfig({}), payload)
    service = ManagementService(tmp_path)
    service.backend = lambda: backend
    client = TestClient(create_app(tmp_path, TOKEN, service))
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}
    payload = {"operation": "permission.control", "action": "block-host",
               "target": "example.com", "confirmed": True}
    with patch("assistant_agent.tools.ToolRuntime.sandbox_control", return_value={"stopped": 0}):
        response = client.post(
            "/v2/operations", headers={**headers, "Idempotency-Key": "permission-block-host"},
            json=payload,
        )
        assert response.status_code == 202
        manager = client.app.state.management_operations
        for _ in range(200):
            operation = manager.get(response.json()["id"])
            if operation["state"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
    assert operation["state"] == "succeeded", operation
    hosts = client.get("/v2/permissions/hosts", headers=headers)
    assert hosts.status_code == 200
    assert any(row["target"] == "example.com" and row["decision"] == "block"
               for row in hosts.json()["data"]["grants"])
    assert client.get("/v2/permissions/history", headers=headers).status_code == 200
    assert client.get("/v2/permissions/unknown", headers=headers).status_code == 409
    assert client.post(
        "/v2/operations", headers={**headers, "Idempotency-Key": "permission-invalid-01"},
        json={**payload, "action": "other"},
    ).status_code == 422
    manager.executor.shutdown(wait=True)


def test_management_permission_removals_update_real_grants_and_rules(db, tmp_path, monkeypatch):
    import time

    monkeypatch.delenv("ASSISTANT_MANAGEMENT_EXECUTOR_URL", raising=False)
    backend = Mock()
    backend.request.side_effect = lambda payload: execute_management(db, AppConfig({}), payload)
    service = ManagementService(tmp_path)
    service.backend = lambda: backend
    app = create_app(tmp_path, TOKEN, service)
    client = TestClient(app)
    manager = app.state.management_operations
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}
    grant = db.fetch_one(
        """INSERT INTO sandbox_permission_grants(kind, target, scope, decision, created_by)
           VALUES ('host', 'blocked.example', 'global', 'block', 'test') RETURNING id"""
    )
    revoked = db.fetch_one(
        """INSERT INTO sandbox_permission_grants(kind, target, scope, decision, created_by)
           VALUES ('host', 'revoked.example', 'global', 'block', 'test') RETURNING id"""
    )
    legacy = db.fetch_one(
        "INSERT INTO sandbox_network_rules(host, decision) VALUES ('old.example', 'block') RETURNING id"
    )
    try:
        with patch("assistant_agent.tools.ToolRuntime.sandbox_control", return_value={"stopped": 0}):
            for action, target in (
                ("unblock-host", "blocked.example"),
                ("revoke-grant", str(revoked["id"])),
                ("remove-legacy", str(legacy["id"])),
            ):
                response = client.post(
                    "/v2/operations", headers={**headers, "Idempotency-Key": "permission-" + action},
                    json={"operation": "permission.control", "action": action,
                          "target": target, "confirmed": True},
                )
                assert response.status_code == 202
                for _ in range(200):
                    operation = manager.get(response.json()["id"])
                    if operation["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                assert operation["state"] == "succeeded", operation
        assert db.fetch_one("SELECT id FROM sandbox_permission_grants WHERE id = %s", (grant["id"],)) is None
        assert db.fetch_one("SELECT id FROM sandbox_permission_grants WHERE id = %s", (revoked["id"],)) is None
        assert db.fetch_one("SELECT id FROM sandbox_network_rules WHERE id = %s", (legacy["id"],)) is None
    finally:
        manager.executor.shutdown(wait=True)


def test_script_permission_management_through_api(db, tmp_path, monkeypatch):
    import time

    monkeypatch.delenv("ASSISTANT_MANAGEMENT_EXECUTOR_URL", raising=False)
    item = backend()
    item.request.side_effect = lambda payload: execute_management(db, AppConfig({}), payload)
    item.mutate.side_effect = lambda action, identifier, body: execute_management(
        db, AppConfig({}), {"kind": "mutate", "action": action, "id": identifier, "body": body, "confirmed": True},
    )
    app = create_app(tmp_path, TOKEN, ManagementService(tmp_path, Mock(return_value=item)))
    client = TestClient(app)
    manager = app.state.management_operations
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}
    script = db.fetch_one("INSERT INTO sandbox_scripts(entry) VALUES ('example.py') RETURNING id")
    version = db.fetch_one(
        """INSERT INTO sandbox_script_versions(script_id, fingerprint, manifest, purpose, reusable)
           VALUES (%s, 'test-fingerprint', '{}'::jsonb, 'Test', true) RETURNING id""", (script["id"],),
    )
    db.execute(
        """INSERT INTO sandbox_permission_grants(version_id, kind, scope, decision, created_by)
           VALUES (%s, 'run', 'script', 'allow', 'test')""", (version["id"],),
    )
    try:
        rows = client.get("/v2/records/scripts", headers=headers).json()["rows"]
        assert any(row["id"] == script["id"] and row["version_id"] == version["id"] for row in rows)
        details = client.get(f"/v2/records/scripts/{version['id']}", headers=headers).json()
        assert details["version"]["id"] == version["id"]
        assert len(details["grants"]) == 1
        with patch("assistant_agent.tools.ToolRuntime.sandbox_control", return_value={"stopped": 0}):
            for action, expected in (("block-script", True), ("unblock-script", False), ("revoke-script", False)):
                response = client.post(
                    "/v2/operations", headers={**headers, "Idempotency-Key": "permission-" + action},
                    json={"operation": "record.mutate", "action": action,
                          "record_id": script["id"], "confirmed": True},
                )
                assert response.status_code == 202
                for _ in range(200):
                    operation = manager.get(response.json()["id"])
                    if operation["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                assert operation["state"] == "succeeded", operation
                assert db.fetch_one("SELECT blocked FROM sandbox_scripts WHERE id = %s",
                                    (script["id"],))["blocked"] is expected
        assert not db.fetch_all("SELECT id FROM sandbox_permission_grants WHERE version_id = %s", (version["id"],))
    finally:
        manager.executor.shutdown(wait=True)


def test_management_research_start_is_atomic_and_uses_research_workflow(db, tmp_path, monkeypatch):
    import time

    from assistant_agent.deep_research import DeepResearchAgent

    monkeypatch.delenv("ASSISTANT_MANAGEMENT_EXECUTOR_URL", raising=False)
    backend = Mock()
    backend.request.side_effect = lambda payload: execute_management(db, AppConfig({}), payload)
    service = ManagementService(tmp_path)
    service.backend = lambda: backend
    client = TestClient(create_app(tmp_path, TOKEN, service))
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN, "Idempotency-Key": "research-start-001"}
    request = {"operation": "research.start", "question": "Compare the options",
               "instructions": "Focus on reliability", "client_id": CLIENT_ID, "confirmed": True}
    with patch("assistant_agent.tools.search_configured", return_value=True):
        response = client.post("/v2/operations", headers=headers, json=request)
        assert response.status_code == 202
        manager = client.app.state.management_operations
        for _ in range(200):
            operation = manager.get(response.json()["id"])
            if operation["state"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
    assert operation["state"] == "succeeded", operation
    result = operation["result"]
    run = db.fetch_one("SELECT * FROM deep_research_runs WHERE id = %s", (result["research_id"],))
    source = db.fetch_one("SELECT * FROM jobs WHERE id = %s", (result["task_id"],))
    execution = db.fetch_one("SELECT * FROM jobs WHERE id = %s", (run["execution_job_id"],))
    assert run["status"] == "queued" and run["instructions"] == "Focus on reliability"
    assert source["status"] == "waiting"
    assert execution["workstream_id"] == source["workstream_id"] == result["workstream_id"]
    assert client.post("/v2/operations", headers=headers, json=request).json()["id"] == operation["id"]
    agent = DeepResearchAgent(db, AppConfig({}))
    claimed = agent.claim_run()
    assert claimed["id"] == run["id"]
    execution = db.fetch_one("SELECT * FROM jobs WHERE id = %s", (run["execution_job_id"],))
    assert agent.handle_tool_call(claimed, execution, [], {
        "id": "question-1", "function": {"name": "research_request_human_input",
                                        "arguments": '{"question":"Which month?"}'},
    })
    messages = client.get(f"/v2/tasks/{source['id']}/messages", headers=headers).json()
    assert messages["rows"][-1]["body"] == "Which month?"
    response = client.post(
        "/v2/operations", headers={**headers, "Idempotency-Key": "research-guidance-001"},
        json={"operation": "task.reply", "task_id": execution["id"], "body": "November",
              "client_id": CLIENT_ID, "confirmed": True},
    )
    assert response.status_code == 202
    for _ in range(200):
        reply = manager.get(response.json()["id"])
        if reply["state"] in {"succeeded", "failed"}:
            break
        time.sleep(0.01)
    assert reply["state"] == "succeeded", reply
    assert reply["result"]["task_id"] == source["id"]
    resumed = agent.claim_run()
    assert resumed["id"] == run["id"]
    assert "November" in agent.build_messages(resumed, execution)[1]["content"]
    agent.complete_run(resumed, "November is suitable.", {})
    assert db.fetch_one("SELECT status FROM jobs WHERE id = %s", (source["id"],))["status"] == "queued"
    with patch("assistant_agent.tools.search_configured", return_value=False):
        with pytest.raises(ValueError, match="requires OpenRouter"):
            execute_management(db, AppConfig({}), {
                "kind": "research.start", "question": "Cannot start",
                "client_id": CLIENT_ID,
                "request_key": "missing-search", "confirmed": True,
            })
    assert db.fetch_one("SELECT id FROM request_messages WHERE external_id = 'missing-search'") is None
    manager.executor.shutdown(wait=True)


def test_partial_permission_activation_is_reported_through_operation(tmp_path):
    import time

    from assistant_agent.management import Management
    from assistant_agent.management_errors import PERMISSION_ACTIVATION_ERROR
    from assistant_agent.setup import DockerCommandError, PermissionActivationError

    backend = Management(tmp_path, lambda *args: False)
    backend.installation = Mock(state={"setup_complete": True})
    backend.installation.compose.side_effect = DockerCommandError(
        "command failed", stdout='{"code":"permission_activation_incomplete","error":"private detail"}',
    )
    with patch.object(backend, "access"), patch.object(backend, "prepare_management"), \
            patch.object(backend, "management_source", return_value="source"):
        with pytest.raises(PermissionActivationError, match="active sandbox runs"):
            backend.request({"kind": "permission.control"})

    def execute(operation_id, action, payload, report):
        raise PermissionActivationError("untrusted detail")

    manager = OperationManager(tmp_path, execute)
    operation = manager.submit("permission.control", {}, "permission-partial-01")
    for _ in range(200):
        state = manager.get(operation["id"])
        if state["state"] == "failed":
            break
        time.sleep(0.01)
    assert state["error"] == PERMISSION_ACTIVATION_ERROR
    assert "untrusted detail" not in str(state)
    manager.executor.shutdown(wait=True)


def test_partial_approval_application_survives_management_transport(tmp_path):
    from assistant_agent.management import Management
    from assistant_agent.management_errors import ApprovalApplicationIncomplete
    from assistant_agent.setup import DockerCommandError

    backend = Management(tmp_path, lambda *args: False)
    backend.installation = Mock(state={"setup_complete": True})
    backend.installation.compose.side_effect = DockerCommandError(
        "command failed", stdout='{"code":"approval_application_incomplete","error":"private detail"}',
    )
    with patch.object(backend, "access"), patch.object(backend, "prepare_management"), \
            patch.object(backend, "management_source", return_value="source"):
        with pytest.raises(ApprovalApplicationIncomplete, match="approval was consumed"):
            backend.request({"kind": "approval.decide"})


def test_v2_project_file_routes_and_upload_confirmation(tmp_path):
    service = Mock()
    service.execute_operation.return_value = {"file_id": 9, "workstream_id": 4, "object_ids": [11]}
    service.project_files.return_value = {
        "files": [{
            "id": 9, "path": "workspace/report.bin", "sha256": None,
            "retaining_workstreams": 1, "available": True, "size_bytes": 4,
        }],
        "total": 1,
    }
    service.project_file_content.return_value = {
        "id": 9, "path": "workspace/report.bin", "sha256": None,
        "offset": 0, "next_offset": 4, "size_bytes": 4,
        "truncated": False, "content_base64": "AAECAw==",
    }
    client = TestClient(create_app(tmp_path, TOKEN, service))
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN}
    listing = client.get("/v2/projects/4/files", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["files"][0]["id"] == 9
    content = client.get("/v2/projects/4/files/9/content?offset=0&limit=4", headers=headers)
    assert content.status_code == 200
    assert content.json()["content_base64"] == "AAECAw=="
    assert client.get(
        "/v2/projects/4/files/9/content?limit=1048577", headers=headers,
    ).status_code == 422
    operation = {
        "operation": "file.upload", "workstream_id": 4, "path": "report.bin",
        "content_base64": "AAECAw==",
    }
    assert client.post(
        "/v2/operations", headers={**headers, "Idempotency-Key": "file-upload-0001"},
        json=operation,
    ).status_code == 422
    response = client.post(
        "/v2/operations", headers={**headers, "Idempotency-Key": "file-upload-0001"},
        json={**operation, "confirmed": True},
    )
    assert response.status_code == 202
    manager = client.app.state.management_operations
    for _ in range(100):
        state = manager.get(response.json()["id"])
        if state["state"] in {"succeeded", "failed"}:
            break
        import time
        time.sleep(0.01)
    assert state["state"] == "succeeded"
    assert state["result"] == {"completed": True, "file_id": 9, "workstream_id": 4, "object_ids": [11]}
    assert service.execute_operation.call_args.args[1] == "file.upload"
    assert "AAECAw==" not in (tmp_path / "data/.management-operations.json").read_text()
    manager.executor.shutdown(wait=True)


def test_project_file_reads_are_attached_path_safe_and_binary_safe(tmp_path):
    share = tmp_path / "share"
    raw = tmp_path / "raw"
    share.mkdir()
    raw.mkdir()
    path = share / "report.bin"
    content = b"\x00\xffbinary"
    path.write_bytes(content)
    config = AppConfig({"agent": {
        "filesystem": {"shared_root": str(share)},
        "artifacts": {"raw_root": str(raw)},
    }})
    db = Mock()
    db.fetch_one.side_effect = [{"id": 4}, {"total": 1}]
    db.fetch_all.return_value = [{
        "id": 9, "path": str(path), "sha256": "a" * 64, "retaining_workstreams": 1,
    }]
    listing = execute_management(db, config, {"kind": "project-files", "id": 4})
    assert listing["files"][0]["path"] == "workspace/report.bin"
    assert listing["files"][0]["available"]
    db.fetch_one.side_effect = [{"path": str(path), "sha256": "a" * 64}]
    content_result = execute_management(db, config, {
        "kind": "project-file-content", "id": 4, "file_id": 9, "offset": 0, "limit": 4,
    })
    import base64
    assert base64.b64decode(content_result["content_base64"]) == content[:4]
    assert content_result["truncated"]
    db.fetch_one.side_effect = [None]
    with pytest.raises(ValueError, match="unavailable"):
        execute_management(db, config, {"kind": "project-file-content", "id": 5, "file_id": 9})


def test_internal_executor_requires_token_and_rejects_browser_origins(tmp_path):
    from assistant_agent.management_executor import create_server

    (tmp_path / "data").mkdir()
    token_path(tmp_path).write_text(TOKEN + "\n")
    token_path(tmp_path).chmod(0o600)
    server = create_server(("127.0.0.1", 0), tmp_path)
    import threading
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        address = f"http://127.0.0.1:{server.server_address[1]}/v1/execute"
        assert httpx.post(address, json={}, timeout=2).status_code == 401
        assert httpx.post(
            address, headers={"Authorization": "Bearer " + TOKEN, "Origin": "https://example.org"},
            json={}, timeout=2,
        ).status_code == 404
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.parametrize(("failure", "message"), [
    (PublicSetupError("Docker is unavailable."), "Docker is unavailable."),
    (SetupError("password=do-not-display"), "Management operation failed. Refresh current state and check server diagnostics."),
])
def test_internal_executor_reports_only_curated_operation_errors(tmp_path, failure, message):
    import threading
    from assistant_agent.management_executor import create_server

    (tmp_path / "data").mkdir()
    token_path(tmp_path).write_text(TOKEN + "\n")
    token_path(tmp_path).chmod(0o600)
    server = create_server(("127.0.0.1", 0), tmp_path)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with patch.object(ManagementService, "_execute_operation_local", side_effect=failure), patch(
                "assistant_agent.management_executor.persist_external_result") as persist:
            response = httpx.post(
                f"http://127.0.0.1:{server.server_address[1]}/v1/execute",
                headers={"Authorization": "Bearer " + TOKEN},
                json={"operation_id": "test-operation", "operation": {
                    "operation": "service.lifecycle", "action": "apply", "confirmed": True,
                }}, timeout=5,
            )
        assert response.status_code == 409
        assert response.json()["error"] == message
        assert "do-not-display" not in response.text
        persist.assert_called_once_with(tmp_path, "test-operation", succeeded=False, error=message)
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_internal_executor_bounds_concurrent_client_reads_without_blocking_polls(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from assistant_agent.management_executor import MAX_CLIENT_READS, create_server

    (tmp_path / "data").mkdir()
    token_path(tmp_path).write_text(TOKEN + "\n")
    token_path(tmp_path).chmod(0o600)
    server = create_server(("127.0.0.1", 0), tmp_path)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    ready = threading.Barrier(MAX_CLIENT_READS + 1)
    release = threading.Event()
    url = f"http://127.0.0.1:{server.server_address[1]}/v1/read"
    headers = {"Authorization": "Bearer " + TOKEN}

    def blocked_read(*args):
        ready.wait(timeout=10)
        if not release.wait(timeout=10):
            raise RuntimeError("Timed out waiting for test release")
        return {"messages": []}

    def read(action):
        return httpx.post(url, headers=headers, json={
            "request": "clients.read", "action": action, "client_id": CLIENT_ID,
        }, timeout=10)

    try:
        with patch.object(ManagementService, "client_channels", side_effect=blocked_read):
            with ThreadPoolExecutor(max_workers=MAX_CLIENT_READS) as pool:
                futures = [pool.submit(read, "reconcile")]
                futures += [pool.submit(read, "poll") for _ in range(MAX_CLIENT_READS - 1)]
                try:
                    ready.wait(timeout=10)
                    assert read("poll").status_code == 503
                finally:
                    release.set()
                assert all(future.result().status_code == 200 for future in futures)
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_executor_unavailability_is_reported_as_retryable(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    token_path(tmp_path).write_text(TOKEN + "\n")
    token_path(tmp_path).chmod(0o600)
    monkeypatch.setenv("ASSISTANT_MANAGEMENT_EXECUTOR_URL", "http://management-docker:2376")
    transport = Mock()
    transport.__enter__ = Mock(return_value=transport)
    transport.__exit__ = Mock(return_value=False)
    transport.post.return_value.status_code = 503
    with patch("assistant_agent.management_api.httpx.Client", return_value=transport):
        with pytest.raises(ManagementExecutorUnavailable, match="retry"):
            ManagementService(tmp_path)._executor_read({"request": "clients.read", "action": "poll"})

    service = Mock()
    service.client_channels.side_effect = ManagementExecutorUnavailable("retry")
    client = TestClient(create_app(tmp_path, TOKEN, service))
    try:
        response = client.get(
            f"/v2/clients/{CLIENT_ID}/messages",
            headers={"Host": "127.0.0.1", "Authorization": "Bearer " + TOKEN},
        )
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"
        assert response.json()["error"]["code"] == "executor_unavailable"
    finally:
        client.app.state.management_operations.executor.shutdown(wait=True)


def test_internal_executor_persists_safe_task_identifiers(tmp_path):
    import threading
    import time

    from assistant_agent.management_executor import create_server
    from assistant_agent.management_errors import ApprovalApplicationIncomplete, PERMISSION_ACTIVATION_ERROR
    from assistant_agent.setup import PermissionActivationError

    (tmp_path / "data").mkdir()
    token_path(tmp_path).write_text(TOKEN + "\n")
    token_path(tmp_path).chmod(0o600)
    server = create_server(("127.0.0.1", 0), tmp_path)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    def execute(operation_id, action, payload, report):
        response = httpx.post(
            f"http://127.0.0.1:{server.server_address[1]}/v1/execute",
            headers={"Authorization": "Bearer " + TOKEN},
            json={"operation_id": operation_id, "operation": {"operation": action, **payload}}, timeout=5,
        )
        response.raise_for_status()
        return response.json()["result"]

    manager = OperationManager(tmp_path, execute)
    try:
        with patch.object(ManagementService, "_execute_operation_local", return_value={
            "task_id": 7, "request_id": 8, "workstream_id": 9, "body": "never-persist",
        }):
            operation = manager.submit(
                "task.start", {"body": "private-message", "client_id": CLIENT_ID, "confirmed": True}, "external-task-start",
            )
            for _ in range(200):
                result = manager.get(operation["id"])
                if result["state"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.01)
        assert result["state"] == "succeeded"
        assert result["result"] == {"completed": True, "task_id": 7, "request_id": 8, "workstream_id": 9}
        history = (tmp_path / "data/.management-operations.json").read_text()
        assert "private-message" not in history
        assert "never-persist" not in history
        with patch.object(ManagementService, "_execute_operation_local",
                          side_effect=PermissionActivationError("untrusted detail")):
            partial = manager.submit(
                "permission.control", {"action": "block-host", "target": "example.com", "confirmed": True},
                "external-permission-partial",
            )
            for _ in range(200):
                failed = manager.get(partial["id"])
                if failed["state"] == "failed":
                    break
                time.sleep(0.01)
        assert failed["error"] == PERMISSION_ACTIVATION_ERROR
        with patch.object(ManagementService, "_execute_operation_local",
                          side_effect=ApprovalApplicationIncomplete()):
            partial = manager.submit(
                "approval.decide", {"approval_id": "a" * 32, "version": "version",
                                    "decision": "approve", "confirmed": True},
                "external-approval-partial",
            )
            for _ in range(200):
                failed = manager.get(partial["id"])
                if failed["state"] == "failed":
                    break
                time.sleep(0.01)
        assert failed["error"] == ApprovalApplicationIncomplete.MESSAGE
    finally:
        manager.executor.shutdown(wait=True)
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
