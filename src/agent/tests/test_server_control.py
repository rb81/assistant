import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tarfile
import time
import uuid
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from assistant_agent.management_docker import DockerReader, PROJECT_LABEL
from assistant_agent.management_server import connection
from assistant_agent.server_control import bootstrap, start
from assistant_agent.setup import Installation, SetupError
from assistant_agent.management_api import token_path
from assistant_agent.management_client import ManagementClient
from assistant_agent.credential_volume import CredentialVolume
from assistant_agent.management import Management


ROOT = Path(__file__).resolve().parents[3]


def sources(root):
    shutil.copytree(ROOT / "src/config", root / "src/config")
    shutil.copyfile(ROOT / "src/docker-compose.yml", root / "src/docker-compose.yml")


def restore_test_database_ownership(instance, image="assistant-management:local"):
    database = instance.data / "postgres"
    if database.exists():
        instance.docker([
            "run", "--rm", "--network", "none", "--cap-drop", "ALL",
            "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE",
            "--mount", f"type=bind,source={database},target=/fixture-postgres",
            image, "python", "-c",
            "import os,sys\nuid,gid=map(int,sys.argv[1:])\n"
            "for root,dirs,files in os.walk('/fixture-postgres'):\n"
            " for name in dirs+files: os.chown(os.path.join(root,name),uid,gid,follow_symlinks=False)\n"
            " os.chown(root,uid,gid,follow_symlinks=False)\n",
            str(os.getuid()), str(os.getgid()),
        ], timeout=30)


def test_fresh_bootstrap_is_idempotent_and_does_not_enable_workers(tmp_path):
    sources(tmp_path)
    instance = bootstrap(tmp_path)
    assert not instance.state.get("setup_complete")
    assert instance.secret_values["AGENT_DATABASE_PASSWORD"]
    token = token_path(tmp_path).read_bytes()
    password = instance.secret_values["AGENT_DATABASE_PASSWORD"]
    reopened = bootstrap(tmp_path)
    assert reopened.secret_values["AGENT_DATABASE_PASSWORD"] == password
    assert token_path(tmp_path).read_bytes() == token
    assert connection(tmp_path)["port"] == 8765
    assert connection(tmp_path)["token"] == token.decode().strip()
    with patch.object(Installation, "compose", return_value="") as compose:
        start(tmp_path)
    assert compose.call_args.args[0][-2:] == ["management", "postgres"]
    assert not reopened.state.get("setup_complete")


def test_bootstrap_refuses_to_guess_an_existing_database_password(tmp_path):
    sources(tmp_path)
    (tmp_path / "data/postgres/pgdata").mkdir(parents=True)
    (tmp_path / "data/postgres/pgdata/PG_VERSION").write_text("16")
    with pytest.raises(SetupError, match="current password"):
        bootstrap(tmp_path)
    assert not token_path(tmp_path).exists()


def test_management_reads_mounted_credential_volume_without_socket_helpers(tmp_path, monkeypatch):
    store = tmp_path / "store.json"
    record = {"values": {"AGENT_DATABASE_PASSWORD": "private-fixture-value"}, "grants": {}}
    store.write_text(json.dumps(record))
    monkeypatch.setenv("ASSISTANT_CREDENTIAL_STORE", str(store))
    installation = Mock()
    volume = CredentialVolume(installation, "test-volume")
    assert volume.load() == record
    with pytest.raises(SetupError, match="cannot create"):
        volume.load(create=True)
    store.unlink()
    with pytest.raises(SetupError, match="not mounted"):
        volume.load()
    installation.docker.assert_not_called()


@pytest.mark.parametrize("service, stopped", [(None, ["postgres", "task-agent"]), ("management", ["management"])])
def test_stopping_application_preserves_management_unless_explicitly_selected(tmp_path, service, stopped):
    sources(tmp_path)
    backend = Management(tmp_path, lambda *args: False)
    backend.load()
    config = json.dumps({"services": {
        "management": {"restart": "unless-stopped"},
        "management-docker": {"restart": "unless-stopped"},
        "postgres": {"restart": "unless-stopped"},
        "task-agent": {"restart": "unless-stopped"},
    }})
    with patch.object(backend, "access"), patch.object(backend, "services", return_value=[]), \
            patch.object(backend.installation, "compose", side_effect=[
                config, "management\nmanagement-docker\npostgres\ntask-agent\n", "",
            ]) as compose:
        backend.lifecycle("stop", service)
    assert compose.call_args_list[-1].args[0] == ["--profile", "*", "stop", *stopped]


def test_docker_reader_restricts_lists_and_resource_access_to_this_project():
    reader = DockerReader("test-project")
    own = {"Id": "a" * 64, "Labels": {PROJECT_LABEL: "test-project"}}
    other = {"Id": "b" * 64, "Labels": {PROJECT_LABEL: "another-project"}}
    with patch.object(reader, "request", return_value=(200, {}, json.dumps([own, other]).encode())) as request:
        _, _, body = reader.read('/v1.53/containers/json?all=1&filters={"label":{"custom=yes":true}}')
    assert json.loads(body) == [own]
    query = parse_qs(urlsplit(request.call_args.args[0]).query)
    assert PROJECT_LABEL + "=test-project" in json.loads(query["filters"][0])["label"]
    with patch.object(reader, "request", return_value=(200, {}, json.dumps({
        "Config": {"Labels": {PROJECT_LABEL: "another-project"}},
    }).encode())) as request:
        with pytest.raises(PermissionError):
            reader.read("/containers/" + "b" * 64 + "/logs?tail=150")
        assert request.call_count == 1


@pytest.mark.parametrize("target", [
    "/containers/create", "/images/json", "/volumes", "/events",
    "/containers/" + "a" * 64 + "/logs?follow=1",
    "/containers/" + "a" * 64 + "/logs?tail=all",
    "/containers/" + "a" * 64 + "/logs?tail=151",
    "/containers/" + "a" * 64 + "/archive",
])
def test_docker_reader_rejects_non_read_operations(target):
    reader = DockerReader("test")
    with patch.object(reader, "request") as upstream, pytest.raises(PermissionError):
        reader.read(target)
    upstream.assert_not_called()


def test_root_launcher_resolves_installed_symlink_and_preserves_arguments(tmp_path):
    installation = tmp_path / "installation with spaces"
    (installation / "src/scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "run.sh", installation / "run.sh")
    (installation / "src/scripts/server.sh").write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    link = tmp_path / "assistant"
    link.symlink_to(installation / "run.sh")
    result = subprocess.run(["sh", str(link), "connect", "a b"], capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ["connect", "a b"]


def test_installer_reuses_existing_installation_and_does_not_consume_piped_stdin(tmp_path):
    home = tmp_path / "home"
    destination = tmp_path / "installation"
    (destination / "src/scripts").mkdir(parents=True)
    (destination / "src/scripts/server.sh").write_text("# marker\n")
    (destination / "run.sh").write_text('#!/bin/sh\nread -r unexpected && exit 42\nprintf "server-started\\n"\n')
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "docker").write_text("#!/bin/sh\nexit 0\n")
    (tools / "docker").chmod(0o700)
    result = subprocess.run(
        ["sh", str(ROOT / "install.sh")], input="must-not-be-read\n", capture_output=True, text=True,
        env=dict(os.environ, HOME=str(home), PATH=str(tools) + ":" + os.environ["PATH"],
                 ASSISTANT_INSTALL_DIR=str(destination)), check=True,
    )
    assert "server-started" in result.stdout
    assert (home / ".local/bin/assistant").is_symlink()
    assert not Path(str(destination) + ".install-lock").exists()


def test_installer_downloads_and_installs_a_source_bundle_without_host_python(tmp_path):
    source = tmp_path / "source/assistant-test"
    (source / "src/scripts").mkdir(parents=True)
    (source / "src/scripts/server.sh").write_text("# marker\n")
    (source / "run.sh").write_text('#!/bin/sh\nprintf "bootstrapped\\n"\n')
    archive = tmp_path / "bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(source, arcname="assistant-test")
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "docker").write_text("#!/bin/sh\nexit 0\n")
    (tools / "curl").write_text(
        '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n'
        'if [ "$1" = -o ]; then cp "$TEST_ARCHIVE" "$2"; exit; fi\nshift\ndone\nexit 1\n'
    )
    for path in tools.iterdir():
        path.chmod(0o700)
    destination = tmp_path / "installed"
    result = subprocess.run(
        ["sh", str(ROOT / "install.sh")], stdin=subprocess.DEVNULL, capture_output=True, text=True,
        env=dict(os.environ, HOME=str(tmp_path / "home"), PATH=str(tools) + ":" + os.environ["PATH"],
                 ASSISTANT_INSTALL_DIR=str(destination), TEST_ARCHIVE=str(archive)), check=True,
    )
    assert "bootstrapped" in result.stdout
    assert (destination / "src/scripts/server.sh").is_file()
    assert not Path(str(destination) + ".install-lock").exists()


@pytest.mark.skipif(os.getenv("ASSISTANT_TEST_DOCKER") != "1", reason="requires opt-in isolated Docker project")
def test_containerized_api_and_scoped_docker_reader(tmp_path):
    sources(tmp_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    instance = bootstrap(tmp_path)
    management_image = "assistant-management-test:" + uuid.uuid4().hex[:12]
    instance.values.update(
        COMPOSE_PROJECT_NAME="assistant-management-test-" + uuid.uuid4().hex[:12],
        ASSISTANT_MANAGEMENT_PORT=str(port),
        ASSISTANT_MANAGEMENT_IMAGE=management_image,
    )
    instance.save()
    try:
        instance.docker(["build", "-t", management_image, "-f", str(ROOT / "src/management/Dockerfile"), str(ROOT)], timeout=900)
        instance.compose(["up", "-d", "--no-build", "--wait", "--wait-timeout", "60", "management"], timeout=120)
        token = token_path(tmp_path).read_text().strip()
        client = ManagementClient(f"http://127.0.0.1:{port}", token)
        assert client.health()["api_version"] == 2
        assert "clients.register" in client.health()["capabilities"]
        v2_headers = {"Authorization": "Bearer " + token}
        v2_health = httpx.get(f"http://127.0.0.1:{port}/v2/health", headers=v2_headers, timeout=10)
        assert v2_health.status_code == 200
        assert v2_health.json()["api_version"] == 2
        runtime_check = httpx.get(
            f"http://127.0.0.1:{port}/v2/configuration/check",
            params={"runtime": "true"}, headers=v2_headers, timeout=30,
        )
        assert runtime_check.status_code == 200, instance.compose(
            ["logs", "--no-color", "--tail", "20", "management-docker"],
        )
        assert runtime_check.json()["runtime_checked"]
        assert runtime_check.json()["ok"], runtime_check.json()
        assert not client.configuration()["setup_complete"]
        rows = client.services()
        assert {row["id"] for row in rows} >= {"management", "management-docker", "task-agent"}
        assert next(row for row in rows if row["id"] == "task-agent")["state"] == "not created"
        assert "Started server process" in client.logs("management")
        status = json.loads(instance.compose(["ps", "--format", "json", "management"]))
        if isinstance(status, list):
            status = status[0]
        assert all(publisher["URL"] == "127.0.0.1" for publisher in status["Publishers"])
        instance.compose(["restart", "management"], timeout=30)
        instance.compose(["up", "-d", "--no-build", "--wait", "--wait-timeout", "60", "management"], timeout=90)
        assert client.health()["api_version"] == 2
    except SetupError:
        print(instance.compose(["ps", "--all"]))
        print(instance.compose(["logs", "--no-color", "--tail", "20", "management", "management-docker"]))
        raise
    finally:
        instance.compose(["down", "--volumes"], timeout=60)
        instance.docker(["image", "rm", management_image], timeout=30)


@pytest.mark.skipif(os.getenv("ASSISTANT_TEST_DOCKER") != "1", reason="requires opt-in isolated Docker project")
def test_containerized_client_chat_uses_real_executor_and_database(tmp_path):
    sources(tmp_path)
    shutil.copytree(ROOT / "src/database", tmp_path / "src/database")
    shutil.copytree(ROOT / "src/agent/src", tmp_path / "src/agent/src")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    instance = bootstrap(tmp_path)
    project = "assistant-chat-test-" + uuid.uuid4().hex[:12]
    management_image = "assistant-management-test:" + uuid.uuid4().hex[:12]
    task_image = project + "-task-agent"
    instance.values.update(
        COMPOSE_PROJECT_NAME=project, ASSISTANT_MANAGEMENT_PORT=str(port),
        ASSISTANT_MANAGEMENT_IMAGE=management_image,
    )
    instance.state["setup_complete"] = True
    instance.save()
    url = f"http://127.0.0.1:{port}"
    headers = {"Authorization": "Bearer " + token_path(tmp_path).read_text().strip()}
    client_id = uuid.uuid4().hex
    built_images = []

    def operation(key, payload):
        response = httpx.post(
            url + "/v2/operations", headers={**headers, "Idempotency-Key": key},
            json={**payload, "confirmed": True}, timeout=30,
        )
        assert response.status_code == 202, response.text
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = httpx.get(url + "/v2/operations/" + response.json()["id"], headers=headers, timeout=30).json()
            if result["state"] in {"succeeded", "failed", "interrupted"}:
                assert result["state"] == "succeeded", result
                return result["result"]
            time.sleep(0.5)
        pytest.fail("Containerized management operation did not complete")

    try:
        for image, dockerfile in ((management_image, "src/management/Dockerfile"),
                                  (task_image, "src/agent/Dockerfile")):
            instance.docker(["build", "-t", image, "-f", str(ROOT / dockerfile), str(ROOT)], timeout=1800)
            built_images.append(image)
        instance.compose(["up", "-d", "--no-build", "--wait", "--wait-timeout", "90",
                          "management", "postgres"], timeout=180)
        registered = operation("register-container-chat", {
            "operation": "client.register", "client_id": client_id, "name": "Test",
            "client_type": "desktop", "fallback_channels": [],
        })
        assert registered["client"]["id"] == client_id
        inbox = url + f"/v2/clients/{client_id}/messages"
        assert httpx.get(inbox, headers=headers, timeout=60).json()["messages"] == []
        started = operation("start-container-chat", {
            "operation": "task.start", "client_id": client_id, "body": "Synthetic question",
        })
        script = (
            "import sys; from assistant_agent.config import load_config,database_url; "
            "from assistant_agent.database import Database; from assistant_agent.tools import ToolRuntime; "
            "from assistant_agent.replies import ReplyRouter; c=load_config(); d=Database(database_url(c)); "
            "j=d.fetch_one('SELECT * FROM jobs WHERE id = %s', (int(sys.argv[1]),)); "
            "assert ReplyRouter(ToolRuntime(d,c,j)).send('Synthetic answer')['status']=='queued'"
        )
        instance.compose(["run", "--rm", "--no-deps", "--pull", "never", "-T",
                          "--entrypoint", "python", "task-agent", "-c", script, str(started["task_id"])], timeout=120)
        messages = httpx.get(inbox, headers=headers, timeout=60).json()["messages"]
        assert [message["body"] for message in messages] == ["Synthetic answer"]
        acknowledgment = httpx.post(
            url + f"/v2/clients/{client_id}/acknowledgments", headers=headers,
            json={"message_ids": [messages[0]["id"]]}, timeout=60,
        )
        assert acknowledgment.json()["acknowledged"] == [messages[0]["id"]]
        from concurrent.futures import ThreadPoolExecutor

        def acknowledge_again(_):
            response = httpx.post(
                url + f"/v2/clients/{client_id}/acknowledgments", headers=headers,
                json={"message_ids": [messages[0]["id"]]}, timeout=120,
            )
            assert response.status_code == 200, response.text
            assert response.json()["acknowledged"] == [messages[0]["id"]]

        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(acknowledge_again, range(9)))
        instance.compose(["restart", "management"], timeout=30)
        instance.compose(["up", "-d", "--no-build", "--wait", "--wait-timeout", "60", "management"], timeout=90)
        history = httpx.get(url + f"/v2/clients/{client_id}/history", headers=headers, timeout=60).json()
        assert history["messages"][0]["body"] == "Synthetic answer"
        assert history["messages"][0]["status"] == "sent"
    finally:
        try:
            instance.compose(["down", "--volumes"], timeout=120)
        finally:
            try:
                if management_image in built_images:
                    restore_test_database_ownership(instance, management_image)
            finally:
                if built_images:
                    instance.docker(["image", "rm", *built_images], timeout=30)


@pytest.mark.skipif(os.getenv("ASSISTANT_TEST_DOCKER") != "1", reason="requires opt-in isolated Docker project")
def test_management_api_restarts_both_control_plane_services_safely(tmp_path):
    sources(tmp_path)
    shutil.copytree(ROOT / "src/agent", tmp_path / "src/agent")
    shutil.copytree(ROOT / "src/management", tmp_path / "src/management")
    shutil.copyfile(ROOT / ".dockerignore", tmp_path / ".dockerignore")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    instance = bootstrap(tmp_path)
    management_image = "assistant-management-test:" + uuid.uuid4().hex[:12]
    instance.values.update(
        COMPOSE_PROJECT_NAME="assistant-management-restart-test-" + uuid.uuid4().hex[:12],
        ASSISTANT_MANAGEMENT_IMAGE=management_image,
        ASSISTANT_MANAGEMENT_PORT=str(port),
    )
    instance.state["setup_complete"] = True
    instance.save()
    base_url = f"http://127.0.0.1:{port}"
    token = token_path(tmp_path).read_text().strip()
    headers = {"Authorization": "Bearer " + token}

    def wait_operation(operation_id, *, executor_restart=False):
        state = None
        saw_unverified = False
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                response = httpx.get(
                    f"{base_url}/v2/operations/{operation_id}", headers=headers, timeout=10,
                )
                if response.status_code == 200:
                    state = response.json()
                    if state["state"] == "succeeded":
                        if executor_restart:
                            assert saw_unverified, "Executor restart did not expose nonterminal verification"
                        break
                    if executor_restart and state["state"] == "running" and "unverified" in state["progress"]:
                        saw_unverified = True
                        assert state["result"] is None and state["error"] is None
                    if state["state"] in {"failed", "interrupted"}:
                        diagnostic = instance.compose(["logs", "--no-color", "--tail", "20", "management-docker"])
                        assert False, f"{state}; executor diagnostics: {diagnostic}"
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        if state is not None and state["state"] == "failed":
            diagnostic = instance.compose(["logs", "--no-color", "--tail", "20", "management-docker"])
            status = instance.docker([
                "ps", "-a", "--filter",
                "label=com.docker.compose.project=" + instance.values["COMPOSE_PROJECT_NAME"],
                "--format", "{{.Names}} {{.Status}}",
            ])
            inspected = instance.docker([
                "inspect", "--format", "{{json .State}}", before["ID"],
            ])
            assert False, (
                f"{state}; containers: {status}; executor state: {inspected}; "
                f"executor diagnostics: {diagnostic}"
            )
        assert state is not None and state["state"] == "succeeded", state
        return state

    def submit_lifecycle(action, service, key, url=None):
        response = httpx.post(
            (url or base_url) + "/v2/operations",
            headers={**headers, "Idempotency-Key": key},
            json={"operation": "service.lifecycle", "action": action, "service": service, "confirmed": True},
            timeout=15,
        )
        assert response.status_code == 202, response.text
        return response.json()["id"]

    def restart(service, key):
        operation_id = submit_lifecycle("restart", service, key)
        return wait_operation(operation_id, executor_restart=service == "management-docker")

    try:
        instance.compose(["build", "management-docker", "management"], timeout=900)
        instance.compose(["up", "-d", "--no-build", "--wait", "--wait-timeout", "90", "management"], timeout=180)
        client = ManagementClient(base_url, token)
        assert client.health()["api_version"] == 2
        assert restart("management", "restart-management-api-01")["result"]["completed"]
        assert client.health()["api_version"] == 2

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            new_port = listener.getsockname()[1]
        old_url = base_url
        instance.values["ASSISTANT_MANAGEMENT_PORT"] = str(new_port)
        instance.save()
        operation_id = submit_lifecycle("apply", "management", "apply-management-api-port-01", old_url)
        base_url = f"http://127.0.0.1:{new_port}"
        assert wait_operation(operation_id)["result"]["completed"]
        client = ManagementClient(base_url, token)
        assert client.health()["api_version"] == 2

        before = json.loads(instance.compose(["ps", "--format", "json", "management-docker"]))
        if isinstance(before, list):
            before = before[0]
        before_started = instance.docker([
            "inspect", "--format", "{{.State.StartedAt}}", before["ID"],
        ]).strip()

        assert restart("management-docker", "restart-management-docker-01")["result"]["completed"]
        deadline = time.monotonic() + 90
        after_started = before_started
        while time.monotonic() < deadline and after_started == before_started:
            time.sleep(1)
            rows = client.services()
            status = next(row for row in rows if row["id"] == "management-docker")
            current = json.loads(instance.compose(["ps", "--format", "json", "management-docker"]))
            if isinstance(current, list):
                current = current[0]
            after_started = instance.docker([
                "inspect", "--format", "{{.State.StartedAt}}", current["ID"],
            ]).strip()
            if after_started != before_started:
                assert status["state"] == "running"
                assert status["health"] == "healthy"
        assert after_started != before_started
        assert client.health()["api_version"] == 2
    finally:
        instance.compose(["down", "--volumes"], timeout=120)
        try:
            instance.docker(["image", "rm", management_image], timeout=30)
        except Exception:
            pass


@pytest.mark.skipif(os.getenv("ASSISTANT_TEST_DOCKER") != "1", reason="requires opt-in isolated Docker project")
def test_shell_installation_bootstraps_containers_and_installed_connection_helper(tmp_path):
    root = tmp_path / "installed"
    root.mkdir()
    sources(root)
    for relative in ("src/scripts", "src/management", "src/agent/src", "src/database"):
        shutil.copytree(ROOT / relative, root / relative)
    shutil.copyfile(ROOT / "src/agent/requirements-setup.txt", root / "src/agent/requirements-setup.txt")
    shutil.copyfile(ROOT / ".dockerignore", root / ".dockerignore")
    shutil.copy(ROOT / "run.sh", root / "run.sh")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    (root / "data").mkdir()
    (root / "data/.env").write_text(
        "COMPOSE_PROJECT_NAME=assistant-installer-test-" + uuid.uuid4().hex[:12]
        + "\nASSISTANT_MANAGEMENT_PORT=" + str(port) + "\n"
    )
    home = tmp_path / "home"
    environment = dict(os.environ, ASSISTANT_INSTALL_DIR=str(root), HOME=str(home))
    try:
        result = subprocess.run(
            ["sh", str(ROOT / "install.sh")], env=environment, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=600,
        )
        assert result.returncode == 0, result.stderr
        assert "setup is required" in result.stdout
        response = subprocess.run(
            [str(home / ".local/bin/assistant"), "connect"], env=environment,
            capture_output=True, text=True, timeout=30, check=True,
        )
        discovery = json.loads(response.stdout)
        assert discovery["host"] == "127.0.0.1"
        assert discovery["port"] == port
        assert ManagementClient(f"http://127.0.0.1:{port}", discovery["token"]).health()["api_version"] == 2
        read_result = subprocess.run(
            [str(home / ".local/bin/assistant"), "management", "services"], env=environment,
            capture_output=True, text=True, timeout=60, check=True,
        )
        assert any(row["id"] == "management" for row in json.loads(read_result.stdout)["services"])
        help_result = subprocess.run(
            [str(home / ".local/bin/assistant"), "--help"], env=environment,
            capture_output=True, text=True, timeout=60, check=True,
        )
        assert "Assistant local administration" in help_result.stdout
        assert not (root / "data/.venv-setup").exists()
    finally:
        Installation(root).compose(["down", "--volumes"], timeout=60)
        restore_test_database_ownership(Installation(root))
