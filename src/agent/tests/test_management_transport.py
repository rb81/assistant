import asyncio
import json
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path
from unittest.mock import Mock, patch

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
        assert backend.groups["models"][0] == "Models & access"
        assert backend.field_metadata["AGENT_LLM_MODEL"]["help"]
        backend.stage("identity", {"AGENT_IDENTITY_NAME": "API-managed assistant"})
        backend.stage("models", {"AGENT_LLM_OPENROUTER_API_KEY": "test-provider-key",
                                 "AGENT_DATABASE_PASSWORD": "safe-test-database-password"})
        backend.instructions = b"Ask before external actions.\n"
        backend.accepted = True
        preview = backend.preview()
        assert preview["valid"], preview["problems"]
        backend.save()
        assert backend.values["AGENT_IDENTITY_NAME"] == "API-managed assistant"
        assert not backend.dirty
        assert backend.credentials["AGENT_LLM_OPENROUTER_API_KEY"] == "[configured]"
        assert not terminal_root.exists()
    assert any(url.endswith("/v2/configuration/preview") for _, url in requests)
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
