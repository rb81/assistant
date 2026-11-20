import asyncio
import json
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from assistant_agent.management_api import create_app
from assistant_agent.management_api_backend import ApiManagementBackend
from assistant_agent.management_client import ManagementClient
from assistant_agent.management_tui import ManagementApp
from assistant_agent.setup import SetupError


ROOT = Path(__file__).resolve().parents[3]
TOKEN = "a" * 48


def test_tui_backend_uses_server_catalog_and_api_for_configuration_and_actions(tmp_path):
    server_root = tmp_path / "server"
    (server_root / "src/config").mkdir(parents=True)
    shutil.copyfile(ROOT / "src/config/agent.yaml", server_root / "src/config/agent.yaml")
    (server_root / "src/config/AGENT.md.example").write_text("Ask before external actions.\n")
    api = TestClient(create_app(server_root, TOKEN))
    client = ManagementClient("http://127.0.0.1:8765", TOKEN)
    requests = []

    def request(method, url, **kwargs):
        requests.append((method, url))
        return api.request(method, url, **kwargs)

    transport = Mock()
    transport.__enter__ = Mock(return_value=Mock(request=request))
    transport.__exit__ = Mock(return_value=False)
    terminal_root = tmp_path / "no-installation-here"
    backend = ApiManagementBackend(terminal_root, client)
    with patch("assistant_agent.management_client.httpx.Client", return_value=transport):
        backend.load()
        assert backend.groups["models"][0] == "Assistant models"
        assert backend.field_metadata["AGENT_LLM_MODEL"]["help"]
        with create_pipe_input() as pipe:
            ui = ManagementApp(terminal_root, backend=backend, input=pipe, output=DummyOutput())
            ui.loaded = True
            ui.location.route = "Settings"
            ui.render()
            assert {"Assistant", "Models & Voice", "Connections", "Safety & Approvals", "Server"} <= {
                action.label for action in ui.actions}
            ui.location.route = "Settings:models"
            ui.render()
            assert {"Assistant models", "Web search providers", "Web page reader"} <= {action.label for action in ui.actions}
            ui.location.route = "Form:catalog:search"
            ui.render()
            preferred = ui.form["AGENT_SEARCH_PRIMARY_PROVIDER"]
            assert "Allowed values: openrouter, exa" in ui.control_topics[preferred.control][1]
            preferred.text = "exa"
            ui.form["AGENT_SEARCH_EXA_ENABLED"].checked = True
            ui.keep_draft()
            assert backend.values["AGENT_SEARCH_PRIMARY_PROVIDER"] == "exa"
        backend.stage("identity", {"AGENT_IDENTITY_NAME": "API-managed assistant"})
        backend.stage("models", {"AGENT_LLM_OPENROUTER_API_KEY": "test-provider-key"})
        database_group = next(group for group, (_, fields) in backend.groups.items()
                              if any(field.key == "AGENT_DATABASE_PASSWORD" for field in fields))
        backend.stage(database_group, {"AGENT_DATABASE_PASSWORD": "safe-test-database-password"})
        backend.instructions = b"Ask before external actions.\n"
        backend.accepted = True
        preview = backend.preview()
        assert preview["valid"], preview["problems"]
        backend.save()
        assert backend.values["AGENT_IDENTITY_NAME"] == "API-managed assistant"
        assert backend.values["AGENT_SEARCH_PRIMARY_PROVIDER"] == "exa"
        assert not backend.dirty
        assert backend.credentials["AGENT_LLM_OPENROUTER_API_KEY"] == "[configured]"
        assert not terminal_root.exists()
    assert any(url.endswith("/v2/configuration/preview") for _, url in requests)
    assert not any(url.endswith("/v2/configuration") for _, url in requests)
    assert any(method == "POST" and url.endswith("/v2/operations") for method, url in requests)


def test_backend_catalog_does_not_depend_on_local_settings_or_titles():
    client = Mock()
    client.configuration.return_value = {
        "settings": [{"group": "catalog:new", "key": "NEW_SETTING", "label": "New setting",
                      "value": "on", "default": "off", "secret": False, "boolean": False,
                      "data_type": "string", "metadata": {"help": "Supplied by this server."},
                      "editable": False, "edit_restriction": "Server controls this value."}],
        "groups": [{"id": "catalog:new", "title": "Server feature", "help": "Server help."}],
        "credential_names": [], "setup_complete": True, "instructions": "Instructions",
    }
    client.get.side_effect = [{"credentials": []}, {"retirement_pending": False, "mode": "files"}]
    backend = ApiManagementBackend("/unmounted-installation", client)
    backend.load()
    assert list(backend.groups) == ["catalog:new"]
    assert backend.groups["catalog:new"][0] == "Server feature"
    with pytest.raises(SetupError, match="Server controls"):
        backend.stage("catalog:new", {"NEW_SETTING": "changed"})
    assert backend.values["NEW_SETTING"] == "on"


@pytest.mark.parametrize("body", [
    {"error": {"message": "Configuration changed. Reload before saving."}},
    {"error": "invalid"},
    [],
])
def test_api_errors_preserve_actionable_server_messages(body):
    client = ManagementClient("http://127.0.0.1:8765", TOKEN)
    with patch("assistant_agent.management_client.httpx.Client") as transport:
        transport.return_value.__enter__.return_value.request.return_value = httpx.Response(409, json=body)
        expected = "Configuration changed" if isinstance(body, dict) and isinstance(body.get("error"), dict) else "HTTP 409"
        with pytest.raises(SetupError, match=expected):
            client.get("configuration")


def test_review_waits_for_api_without_blocking_navigation():
    async def scenario():
        backend = Mock()
        backend.groups = {}
        backend.redact.side_effect = lambda value: value
        release = threading.Event()
        entered = threading.Event()

        def validate():
            entered.set()
            assert release.wait(3)
            return []

        backend.problems.side_effect = validate
        with create_pipe_input() as pipe:
            ui = ManagementApp("/unmounted-installation", backend=backend, input=pipe, output=DummyOutput())
            ui.loaded = True
            ui.location.route = "Activity"
            ui.render()
            task = asyncio.create_task(ui.app.run_async())
            await asyncio.sleep(0)
            try:
                ui.review()
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                assert entered.is_set()
                assert ui.busy
                ui.go("Activity")
                assert ui.location.route == "Activity"
                release.set()
                await asyncio.wait_for(ui.active_task, 3)
                assert ui.location.route == "Activity"
                assert not ui.error
            finally:
                release.set()
                if ui.app.is_running:
                    ui.app.exit()
                await asyncio.wait_for(task, 3)

    asyncio.run(scenario())


def test_lazy_instructions_do_not_block_terminal_navigation():
    async def scenario():
        client = Mock()
        release = threading.Event()
        entered = threading.Event()
        def instructions(_):
            entered.set()
            assert release.wait(3)
            return {"revision": "r", "instructions": "Ask first."}
        client.get.side_effect = instructions
        backend = ApiManagementBackend("/unmounted-installation", client)
        backend.configuration = {"revision": "r", "instructions": "", "instructions_configured": True}
        backend.installation = Mock(prompt_original=b"[configured]")
        with create_pipe_input() as pipe:
            ui = ManagementApp("/unmounted-installation", backend=backend, input=pipe, output=DummyOutput())
            ui.loaded = True
            ui.location.route = "Activity"
            ui.render()
            task = asyncio.create_task(ui.app.run_async())
            try:
                await asyncio.sleep(0)
                ui.go("Instructions")
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                assert entered.is_set()
                ui.go("Activity")
                assert ui.location.route == "Activity"
                release.set()
                await asyncio.wait_for(ui.active_task, 3)
                assert ui.location.route == "Activity"
                assert backend.configuration["instructions"] == "Ask first."
            finally:
                release.set()
                if ui.app.is_running:
                    ui.app.exit()
                await asyncio.wait_for(task, 3)
    asyncio.run(scenario())


@pytest.mark.parametrize("arguments", [[], ["management", "health"], ["config", "check"]])
def test_container_client_has_only_readonly_connection_mount(tmp_path, arguments):
    root = tmp_path / "install with spaces"
    (root / "src/scripts").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "data/.env").write_text("")
    (root / "data/.management-token").write_text(TOKEN)
    shutil.copyfile(ROOT / "src/scripts/server.sh", root / "src/scripts/server.sh")
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "docker.jsonl"
    daemon = tmp_path / "docker.sock"
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(daemon))
        (tools / "docker").write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "with open(os.environ['DOCKER_CALLS'], 'a') as log: log.write(json.dumps(args) + '\\n')\n"
            "if args[0] == 'compose' and 'ps' in args: print('api-container')\n"
        )
        (tools / "docker").chmod(0o700)
        subprocess.run(["sh", str(root / "src/scripts/server.sh"), *arguments],
                       env=dict(os.environ, PATH=str(tools) + ":" + os.environ["PATH"],
                                DOCKER_HOST="unix://" + str(daemon), DOCKER_CALLS=str(log),
                                ASSISTANT_MANAGEMENT_IMAGE="fixture-image"),
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    run = next(call for call in calls if call[0] == "run")
    mounts = [run[index + 1] for index, value in enumerate(run) if value == "--mount"]
    assert mounts == [f"type=bind,source={root}/data/.management-token,target=/run/assistant/management-token,readonly"]
    assert "--group-add" not in run
    assert "--read-only" in run and "--cap-drop" in run
    assert run[run.index("--network") + 1] == "container:api-container"


def test_structured_settings_never_returns_source_or_saved_credentials(tmp_path):
    root = tmp_path / "server"
    (root / "src/config").mkdir(parents=True)
    shutil.copyfile(ROOT / "src/config/agent.yaml", root / "src/config/agent.yaml")
    (root / "src/config/AGENT.md.example").write_text("Private standing instructions.\n")
    from assistant_agent.setup import Installation
    installation = Installation(root)
    installation.values["AGENT_IDENTITY_NAME"] = "Configured name"
    installation.secret_values["AGENT_LLM_OPENROUTER_API_KEY"] = "private-model-secret"
    installation.save()
    app = create_app(root, TOKEN)
    headers = {"Authorization": "Bearer " + TOKEN, "Host": "127.0.0.1"}
    try:
        api = TestClient(app)
        catalog = api.get("/v2/configuration/catalog", headers=headers)
        assert catalog.status_code == 200
        assert all(field["value"] == "" for field in catalog.json()["settings"])
        section = api.get("/v2/configuration/settings?group=identity", headers=headers)
        assert section.status_code == 200
        groups = {item["id"] for item in section.json()["groups"] if item.get("parent") == "identity"} | {"identity"}
        assert {field["group"] for field in section.json()["settings"]} <= groups
        assert any(field["value"] == "Configured name" for field in section.json()["settings"])
        for response in (catalog, section):
            assert not {"files", "agent_yaml", "agent_yaml_backups"} & response.json().keys()
            assert response.json()["instructions"] == ""
            assert "private-model-secret" not in response.text
        assert api.get("/v2/configuration/settings?group=missing", headers=headers).status_code == 409
        assert api.get("/v2/configuration", headers=headers).status_code == 200
    finally:
        app.state.management_operations.executor.shutdown(wait=True)


def test_persistent_reader_failure_never_launches_an_executor(tmp_path, monkeypatch):
    from assistant_agent.management_api import ManagementExecutorUnavailable, ManagementService
    monkeypatch.setenv("ASSISTANT_MANAGEMENT_READER_URL", "http://management-reader:8766")
    (tmp_path / "data").mkdir()
    (tmp_path / "data/.management-token").write_text(TOKEN)
    (tmp_path / "data/.management-token").chmod(0o600)
    backend = Mock()
    service = ManagementService(tmp_path, backend)
    try:
        with patch.object(service.reader_client, "post", return_value=httpx.Response(503)):
            with pytest.raises(ManagementExecutorUnavailable):
                service.conversations()
        backend.assert_not_called()
        with patch.object(service.reader_client, "post", return_value=httpx.Response(
                200, json={"result": {"rows": [], "total": 0}})) as post:
            assert service.conversations() == {"rows": [], "total": 0}
            assert post.call_args.kwargs["json"]["request"] == "conversations.list"
    finally:
        service.close()


def test_event_buffer_resumes_and_requires_resync_after_eviction_or_restart():
    from assistant_agent.management_events import EventBuffer

    async def scenario():
        events = EventBuffer(limit=2)
        assert events.since("") == [(events.cursor, "resync")]
        cursor = events.cursor
        await events.publish("state.changed")
        pending = events.since(cursor)
        assert len(pending) == 1 and pending[0][1] == "state.changed"
        assert events.since(pending[0][0]) == []
        await events.publish("state.changed")
        await events.publish("server.unavailable")
        assert events.since(cursor) == [(events.cursor, "resync")]
        assert events.since("old-process:1") == [(events.cursor, "resync")]
        assert events.since(events.epoch + ":9999") == [(events.cursor, "resync")]

    asyncio.run(scenario())


def test_persistent_reader_rejects_mutations_and_releases_capacity():
    from assistant_agent.config import AppConfig
    from assistant_agent.management_reader import create_server
    pool = MagicMock()
    connection = pool.connection.return_value.__enter__.return_value
    server = create_server(("127.0.0.1", 0), TOKEN, AppConfig({}), pool)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as api:
            payload = {"request": "conversations.list"}
            assert api.post("/v1/read", json=payload).status_code == 401
            api.headers["Authorization"] = "Bearer " + TOKEN
            assert api.post("/v1/read", json={"operation": "database.reset"}).status_code == 422
            assert api.post("/v1/read", json={"request": "domain.read", "action": "catalog"}).status_code == 422
            assert api.post("/v1/read", json={"request": "clients.read", "action": "reconcile"}).status_code == 422
            with patch("assistant_agent.management_reader.read", side_effect=ValueError("not available")):
                assert api.post("/v1/read", json=payload).status_code == 409
            with patch("assistant_agent.management_reader.read", return_value={"rows": [], "total": 0}):
                for _ in range(6):
                    assert api.post("/v1/read", json=payload).status_code == 200
            assert any("READ ONLY" in call.args[0] for call in connection.execute.call_args_list)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
