import http.client
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from assistant_agent.management import Management
from assistant_agent.management_api import ManagementService, create_app, token_path
from assistant_agent.management_docker import DockerReader, PROJECT_LABEL, prepare_executor_restart
from assistant_agent.management_executor import (
    RESTART_EXPIRED_ERROR, RESTART_UNVERIFIED_ERROR, create_server, persist_restart_intent,
    reconcile_executor_restart,
)
from assistant_agent.management_operations import (
    CONTROL_PROGRESS, RESTART_PROGRESS, finish_executor_restart,
    persist_control_plane_pending, persist_external_result,
)
from assistant_agent.setup import SetupError


def backend(root, *, profiles=""):
    service = Management(root, Mock(return_value=False))
    service.load()
    service.installation.values["COMPOSE_PROFILES"] = profiles
    return service


def compose_config(**services):
    return json.dumps({"services": services})


def test_service_status_includes_inactive_profiles_and_one_shot_services(tmp_path):
    service = backend(tmp_path)
    config = compose_config(
        **{
            "task-agent": {"restart": "unless-stopped"},
            "downloader": {"profiles": ["email"], "restart": "unless-stopped"},
            "transcription-init": {"profiles": ["voice"], "restart": "no"},
            "ollama-model-puller": {"restart": "no"},
        }
    )
    rows = [
        {"Service": "task-agent", "State": "running", "Health": "healthy"},
        {"Service": "transcription-init", "State": "exited", "Health": "", "ExitCode": 0},
        {"Service": "ollama-model-puller", "State": "exited", "Health": "", "ExitCode": 2},
    ]
    with patch.object(service, "access"), patch.object(
        service.installation, "compose", side_effect=[config, json.dumps(rows)]
    ) as compose:
        statuses = service.services()

    by_id = {row["id"]: row for row in statuses}
    assert set(by_id) == {"task-agent", "downloader", "transcription-init", "ollama-model-puller"}
    assert by_id["task-agent"]["health"] == "healthy"
    assert by_id["downloader"]["state"] == "profile inactive"
    assert by_id["transcription-init"]["state"] == "exited"
    assert by_id["transcription-init"]["health"] == "completed successfully"
    assert by_id["ollama-model-puller"]["health"] == "failed (exit code 2)"
    assert compose.call_args_list[0].args[0][:3] == ["--profile", "*", "config"]
    assert compose.call_args_list[1].args[0][:4] == ["--profile", "*", "ps", "--all"]


@pytest.mark.parametrize("health", ["healthy", "unhealthy"])
def test_service_health_names_the_worker_probe_scope_without_claiming_job_success(tmp_path, health):
    service = backend(tmp_path)
    config = compose_config(**{
        "task-agent": {"restart": "unless-stopped", "healthcheck": {
            "test": ["CMD", "python", "-m", "assistant_agent.worker_health"],
        }},
    })
    with patch.object(service, "access"), patch.object(
        service.installation, "compose", side_effect=[
            config, json.dumps([{"Service": "task-agent", "State": "running", "Health": health}]),
        ],
    ):
        row = service.services()[0]
    assert row["state"] == "running"
    assert "polling" in row["health"] and "database" in row["health"]
    if health == "healthy":
        assert "job/integration outcomes unverified" in row["health"]
    else:
        assert "unverified" in row["health"]


def test_stopped_long_running_service_cannot_reuse_its_previous_healthy_probe(tmp_path):
    service = backend(tmp_path)
    with patch.object(service, "access"), patch.object(
        service.installation, "compose", side_effect=[
            compose_config(**{"task-agent": {"restart": "unless-stopped"}}),
            json.dumps([{"Service": "task-agent", "State": "exited", "Health": "healthy"}]),
        ],
    ):
        row = service.services()[0]
    assert row["state"] == "exited"
    assert row["health"].startswith("not ready")


def test_profiled_service_logs_are_supported_without_following_or_unbounded_output(tmp_path):
    service = backend(tmp_path)
    config = compose_config(**{"downloader": {"profiles": ["email"], "restart": "unless-stopped"}})
    with patch.object(service, "access"), patch.object(
        service.installation, "compose", side_effect=[config, "bounded logs"]
    ) as compose:
        assert service.logs("downloader") == "bounded logs"
    assert compose.call_args.args[0] == [
        "--profile", "*", "logs", "--no-color", "--timestamps", "--tail", "150", "downloader",
    ]


def test_one_shot_profile_restart_executes_once_without_starting_dependencies(tmp_path):
    service = backend(tmp_path)
    service.installation.state["setup_complete"] = True
    config = compose_config(**{"transcription-init": {"profiles": ["voice"], "restart": "no"}})
    with patch.object(service, "access"), patch.object(
        service.installation, "assert_current"
    ), patch.object(service.installation, "reconcile_grants"), patch.object(
        service.installation, "compose", side_effect=[config, ""]
    ) as compose, patch.object(service, "services", return_value=[]):
        service.lifecycle("restart", "transcription-init", preserve_management=True)

    assert compose.call_args_list[1].args[0] == [
        "--profile", "*", "up", "--pull", "never", "--no-build", "--no-deps", "--force-recreate",
        "transcription-init",
    ]


def test_restart_of_uncreated_profile_service_uses_up_and_preserves_executor(tmp_path):
    service = backend(tmp_path)
    config = compose_config(**{
        "management": {"restart": "unless-stopped"},
        "management-docker": {"restart": "unless-stopped"},
        "downloader": {"profiles": ["email"], "restart": "unless-stopped"},
    })
    with patch.object(service, "access"), patch.object(
        service.installation, "assert_current"
    ), patch.object(service.installation, "reconcile_grants"), patch.object(
        service.installation, "compose", side_effect=[config, "", ""]
    ) as compose, patch.object(service, "services", return_value=[]):
        service.lifecycle("restart", "downloader", preserve_management=True)

    assert compose.call_args_list[1].args[0] == ["--profile", "*", "ps", "--all", "--services"]
    assert compose.call_args_list[2].args[0] == [
        "--profile", "*", "up", "-d", "--wait", "--wait-timeout", "180", "downloader",
    ]


def test_application_restart_only_restarts_running_long_lived_services(tmp_path):
    service = backend(tmp_path)
    config = compose_config(**{
        "management": {"restart": "unless-stopped"},
        "management-docker": {"restart": "unless-stopped"},
        "task-agent": {"restart": "unless-stopped"},
        "downloader": {"profiles": ["email"], "restart": "unless-stopped"},
        "transcription-init": {"profiles": ["voice"], "restart": "no"},
    })
    with patch.object(service, "access"), patch.object(
        service.installation, "assert_current"
    ), patch.object(service.installation, "reconcile_grants"), patch.object(
        service.installation, "compose",
        side_effect=[
            config,
            "management\nmanagement-docker\ntask-agent\ndownloader\ntranscription-init\n",
            "",
        ],
    ) as compose, patch.object(service, "services", return_value=[]):
        service.lifecycle("restart", preserve_management=True)

    assert compose.call_args_list[2].args[0] == ["--profile", "*", "restart", "task-agent", "downloader"]


def test_management_api_can_restart_itself_but_only_schedules_executor_restart(tmp_path, monkeypatch):
    service = backend(tmp_path)
    config = compose_config(**{
        "management": {"restart": "unless-stopped"},
        "management-docker": {"restart": "unless-stopped"},
    })
    with patch.object(service, "access"), patch.object(
        service.installation, "assert_current"
    ), patch.object(service.installation, "reconcile_grants"), patch.object(
        service.installation, "compose", side_effect=[config, "management\n", ""]
    ) as compose, patch.object(service, "services", return_value=[]), patch.object(
        service, "_management_container_state", side_effect=[
            ("a" * 64, {"StartedAt": "before"}),
            ("a" * 64, {"StartedAt": "after", "Running": True, "Health": {"Status": "healthy"}}),
        ],
    ):
        service.lifecycle("restart", "management", preserve_management=True)
    assert compose.call_args_list[1].args[0] == ["--profile", "*", "ps", "--all", "--services"]
    assert compose.call_args_list[2].args[0] == ["--profile", "*", "restart", "management"]

    monkeypatch.setenv("ASSISTANT_MANAGEMENT_EXECUTOR", "1")
    with patch.object(service, "access"), patch.object(
        service.installation, "assert_current"
    ), patch.object(service.installation, "compose", return_value=config) as compose:
        assert service.lifecycle("restart", "management-docker", preserve_management=True) == {
            "_restart_management_docker": True,
        }
    assert compose.call_count == 1


@pytest.mark.parametrize("state,expected", [
    ({"StartedAt": "after", "Running": True, "Health": {"Status": "healthy"}}, None),
    ({"StartedAt": "after", "Running": True, "Health": {"Status": "unhealthy"}}, "failed its health"),
    ({"StartedAt": "after", "Running": False, "Status": "exited"}, "failed its health"),
    ({"StartedAt": "after", "Running": True, "Health": {"Status": "starting"}}, "before its deadline"),
    ({"StartedAt": "after", "Running": True}, "before its deadline"),
    ({"StartedAt": "before", "Running": True, "Health": {"Status": "healthy"}}, "before its deadline"),
])
def test_management_restart_requires_a_new_running_healthy_container(tmp_path, state, expected):
    service = backend(tmp_path)
    previous = ("a" * 64, {"StartedAt": "before"})
    with patch.object(service, "_management_container_state", return_value=("a" * 64, state)), patch(
        "assistant_agent.management.time.monotonic", side_effect=[0, 0, 0, 0.5, 2],
    ), patch("assistant_agent.management.time.sleep"):
        if expected is None:
            service._verify_management_restart(previous, wait_seconds=1)
        else:
            with pytest.raises(SetupError, match=expected):
                service._verify_management_restart(previous, wait_seconds=1)


def test_management_restart_verification_rejects_a_container_from_another_project(tmp_path):
    service = backend(tmp_path)
    with patch.object(service.installation, "compose", return_value="a" * 64), patch.object(
        service.installation, "docker", return_value=json.dumps({
            "Config": {"Labels": {
                PROJECT_LABEL: "unrelated", "com.docker.compose.service": "management",
            }},
            "State": {"StartedAt": "after", "Running": True, "Health": {"Status": "healthy"}},
        }),
    ):
        with pytest.raises(SetupError, match="could not be verified"):
            service._management_container_state(timeout=15)


@pytest.mark.parametrize("health,expected", [
    ("unhealthy", "failed its health"),
    ("starting", "before its deadline"),
])
def test_management_restart_lifecycle_does_not_return_success_when_health_verification_fails(
    tmp_path, health, expected,
):
    service = backend(tmp_path)
    with patch.object(service, "access"), patch.object(
        service.installation, "assert_current",
    ), patch.object(service.installation, "reconcile_grants"), patch.object(
        service.installation, "compose",
        side_effect=[compose_config(management={"restart": "unless-stopped"}), "management\n", ""],
    ), patch.object(service, "_management_container_state", side_effect=[
        ("a" * 64, {"StartedAt": "before"}),
        ("a" * 64, {"StartedAt": "after", "Running": True, "Health": {"Status": health}}),
    ]), patch("assistant_agent.management.time.monotonic", side_effect=[0, 0, 0, 179, 181]), patch(
        "assistant_agent.management.time.sleep",
    ), patch.object(service, "services") as services:
        with pytest.raises(SetupError, match=expected):
            service.lifecycle("restart", "management", preserve_management=True)
        services.assert_not_called()


def test_executor_restart_is_rejected_outside_private_executor(tmp_path):
    service = backend(tmp_path)
    config = compose_config(**{"management-docker": {"restart": "unless-stopped"}})
    with patch.object(service, "access"), patch.object(
        service.installation, "assert_current"
    ), patch.object(service.installation, "compose", return_value=config) as compose:
        with pytest.raises(SetupError, match="private executor channel"):
            service.lifecycle("restart", "management-docker", preserve_management=True)
    assert compose.call_count == 1


@pytest.mark.parametrize("action", ["start", "stop"])
@pytest.mark.parametrize("target", ["management", "management-docker"])
def test_control_plane_non_restart_actions_are_blocked_from_api(tmp_path, target, action):
    service = backend(tmp_path)
    service.installation.state["setup_complete"] = True
    config = compose_config(**{target: {"restart": "unless-stopped"}})
    with patch.object(service, "access"), patch.object(
        service.installation, "assert_current"
    ), patch.object(service.installation, "compose", return_value=config) as compose:
        with pytest.raises(SetupError, match="control-plane action"):
            service.lifecycle(action, target, preserve_management=True)
    assert compose.call_count == 1


def test_management_api_can_apply_its_configuration_without_restarting_executor(tmp_path):
    service = backend(tmp_path)
    service.installation.state["setup_complete"] = True
    config = compose_config(**{
        "management": {"restart": "unless-stopped"},
        "management-docker": {"restart": "unless-stopped"},
    })
    with patch.object(service, "access"), patch.object(
        service.installation, "assert_current"
    ), patch.object(service.installation, "reconcile_grants"), patch.object(
        service.installation, "compose", side_effect=[config, ""]
    ) as compose, patch.object(service, "services", return_value=[]):
        service.lifecycle("apply", "management", preserve_management=True)
    assert compose.call_args_list[1].args[0] == [
        "--profile", "*", "up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "180",
        "management",
    ]


def test_management_docker_apply_is_rejected_to_avoid_killing_active_executor(tmp_path):
    service = backend(tmp_path)
    service.installation.state["setup_complete"] = True
    config = compose_config(**{"management-docker": {"restart": "unless-stopped"}})
    with patch.object(service, "access"), patch.object(
        service.installation, "assert_current"
    ), patch.object(service.installation, "compose", return_value=config) as compose:
        with pytest.raises(SetupError, match="control-plane action"):
            service.lifecycle("apply", "management-docker", preserve_management=True)
    assert compose.call_count == 1


def test_self_restart_is_project_and_restart_policy_scoped_and_delayed(tmp_path):
    reader = DockerReader("assistant-test")
    response = {
        "Config": {"Labels": {
            PROJECT_LABEL: "assistant-test", "com.docker.compose.service": "management-docker",
        }},
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        "State": {"StartedAt": (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()},
    }
    with patch.object(reader, "request", return_value=(200, {}, json.dumps(response).encode())) as request, \
            patch("assistant_agent.management_docker.Timer") as timer:
        prepared = prepare_executor_restart(reader, "assistant-test", "a" * 64)
        prepared["schedule"]()
    request.assert_called_once_with("/containers/" + "a" * 64 + "/json")
    assert prepared["container_id"] == "a" * 64
    assert prepared["project"] == "assistant-test"
    timer.assert_called_once()
    assert timer.call_args.args[0] == 0.75
    timer.return_value.start.assert_called_once()
    assert timer.return_value.daemon is True


def test_self_restart_refuses_unmanaged_or_non_restarting_containers():
    reader = DockerReader("assistant-test")
    response = {
        "Config": {"Labels": {
            PROJECT_LABEL: "other-project", "com.docker.compose.service": "management-docker",
        }},
        "HostConfig": {"RestartPolicy": {"Name": "no"}},
        "State": {"StartedAt": datetime.now(timezone.utc).isoformat()},
    }
    with patch.object(reader, "request", return_value=(200, {}, json.dumps(response).encode())):
        with pytest.raises(ValueError, match="not configured for a safe restart"):
            prepare_executor_restart(reader, "assistant-test", "a" * 64)


def test_executor_restart_waits_for_docker_restart_policy_minimum_uptime():
    reader = DockerReader("assistant-test")
    response = {
        "Config": {"Labels": {
            PROJECT_LABEL: "assistant-test", "com.docker.compose.service": "management-docker",
        }},
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        "State": {"StartedAt": (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()},
    }
    with patch.object(reader, "request", return_value=(200, {}, json.dumps(response).encode())), \
            patch("assistant_agent.management_docker.Timer") as timer:
        prepared = prepare_executor_restart(reader, "assistant-test", "a" * 64)
        prepared["schedule"]()
    assert timer.call_args.args[0] >= 8.4


def _restart_operation(root, operation_id, started_at):
    data = root / "data"
    data.mkdir(exist_ok=True)
    (data / ".management-operations.json").write_text(json.dumps({
        "operations": [{
            "id": operation_id,
            "action": "service.restart.management-docker",
            "state": "running",
            "progress": "Starting",
            "created_at": started_at,
            "updated_at": started_at,
            "error": None,
            "result": None,
        }],
        "idempotency": {},
    }))


def test_executor_restart_marks_operation_succeeded_only_after_healthy_reboot(tmp_path):
    before = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    after = datetime.now(timezone.utc).isoformat()
    operation_id = "restart-verify-01"
    _restart_operation(tmp_path, operation_id, before)
    persist_restart_intent(tmp_path, operation_id, "a" * 64, "assistant-test", before)
    reader = Mock()
    reader.request.return_value = (200, {}, json.dumps({
        "Config": {"Labels": {
            PROJECT_LABEL: "assistant-test", "com.docker.compose.service": "management-docker",
        }},
        "State": {"Running": True, "StartedAt": after, "Health": {"Status": "healthy"}},
    }).encode())

    reconcile_executor_restart(tmp_path, reader, "assistant-test", wait_seconds=1)

    operation = json.loads((tmp_path / "data/.management-operations.json").read_text())["operations"][0]
    assert operation["state"] == "succeeded"
    assert operation["result"] == {"completed": True}
    assert not (tmp_path / "data/.management-executor-restart.json").exists()


def test_executor_restart_does_not_claim_success_without_a_new_healthy_container(tmp_path):
    before = datetime.now(timezone.utc).isoformat()
    operation_id = "restart-pending-01"
    _restart_operation(tmp_path, operation_id, before)
    persist_restart_intent(tmp_path, operation_id, "a" * 64, "assistant-test", before)
    reader = Mock()
    reader.request.return_value = (200, {}, json.dumps({
        "Config": {"Labels": {
            PROJECT_LABEL: "assistant-test", "com.docker.compose.service": "management-docker",
        }},
        "State": {"Running": True, "StartedAt": before, "Health": {"Status": "healthy"}},
    }).encode())

    reconcile_executor_restart(tmp_path, reader, "assistant-test", wait_seconds=0)

    operation = json.loads((tmp_path / "data/.management-operations.json").read_text())["operations"][0]
    assert operation["state"] == "running"
    assert operation["progress"] == RESTART_PROGRESS
    assert operation["error"] is None
    assert (tmp_path / "data/.management-executor-restart.json").is_file()


@pytest.mark.parametrize("expires_in", [-1, 1])
def test_executor_restart_expiry_is_persisted_as_an_unverified_failure(tmp_path, expires_in):
    operation_id = "restart-pending-state"
    started_at = datetime.now(timezone.utc).isoformat()
    _restart_operation(tmp_path, operation_id, started_at)
    persist_restart_intent(tmp_path, operation_id, "a" * 64, "assistant-test", started_at)
    intent_path = tmp_path / "data/.management-executor-restart.json"
    intent = json.loads(intent_path.read_text())
    intent["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    intent_path.write_text(json.dumps(intent))
    reader = Mock()
    reader.request.return_value = (200, {}, json.dumps({
        "Config": {"Labels": {
            PROJECT_LABEL: "assistant-test", "com.docker.compose.service": "management-docker",
        }},
        "State": {"Running": True, "StartedAt": started_at, "Health": {"Status": "healthy"}},
    }).encode())

    reconcile_executor_restart(tmp_path, reader, "assistant-test", wait_seconds=2)

    operation = json.loads((tmp_path / "data/.management-operations.json").read_text())["operations"][0]
    assert operation["state"] == "failed"
    assert operation["error"] == RESTART_EXPIRED_ERROR
    assert operation["result"] is None
    assert not intent_path.exists()
    if expires_in < 0:
        reader.request.assert_not_called()
    else:
        reader.request.assert_called()


def test_executor_persists_and_responds_before_scheduling_its_restart(tmp_path):
    root = tmp_path
    token_file = token_path(root)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("A" * 64)
    token_file.chmod(0o600)
    events = []

    def persist_intent(*args, **kwargs):
        events.append("persist")

    def prepare():
        return {
            "container_id": "a" * 64,
            "project": "assistant-test",
            "started_at": "2026-11-15T07:00:00+00:00",
            "schedule": lambda: events.append("restart"),
        }

    server = create_server(("127.0.0.1", 0), root, prepare_executor_restart=prepare)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    with patch("assistant_agent.management_executor.persist_restart_intent", side_effect=persist_intent), patch(
        "assistant_agent.management_executor.persist_external_result",
    ) as persist_result, patch.object(
            ManagementService, "_execute_operation_local", return_value={"_restart_management_docker": True},
    ):
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            body = json.dumps({
                "operation_id": "restart-op",
                "operation": {
                    "operation": "service.lifecycle", "action": "restart",
                    "service": "management-docker", "confirmed": True,
                },
            })
            connection.request(
                "POST", "/v1/execute", body,
                {"Authorization": "Bearer " + "A" * 64, "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    assert response.status == 503
    assert payload == {"error": RESTART_UNVERIFIED_ERROR}
    assert events == ["persist", "restart"]
    persist_result.assert_not_called()


@pytest.mark.parametrize("transport_error", [True, False])
@pytest.mark.parametrize("recovers", [True, False])
def test_public_restart_polling_remains_nonterminal_until_verification_or_deadline(
    tmp_path, transport_error, recovers,
):
    returned = threading.Event()

    def execute(operation_id, action, payload, report):
        persist_restart_intent(
            tmp_path, operation_id, "a" * 64, "assistant-test",
            datetime.now(timezone.utc).isoformat(),
        )
        intent_path = tmp_path / "data/.management-executor-restart.json"
        intent = json.loads(intent_path.read_text())
        intent["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
        intent_path.write_text(json.dumps(intent))
        returned.set()
        if transport_error:
            raise SetupError("Executor response is unverified")
        return None

    service = Mock()
    service.execute_operation.side_effect = execute
    app = create_app(tmp_path, "A" * 64, service)
    managers = [app.state.management_operations]
    headers = {"Authorization": "Bearer " + "A" * 64, "Host": "127.0.0.1",
               "Idempotency-Key": "restart-public-polling"}
    payload = {"operation": "service.lifecycle", "action": "restart",
               "service": "management-docker", "confirmed": True}
    try:
        with TestClient(app) as client:
            response = client.post("/v2/operations", headers=headers, json=payload)
            assert response.status_code == 202
            operation_id = response.json()["id"]
            assert returned.wait(3)
            managers[0].executor.shutdown(wait=True)
            url = "/v2/operations/" + operation_id
            pending = client.get(url, headers=headers).json()
            assert pending["state"] == "running"
            assert pending["progress"] == RESTART_PROGRESS
            assert pending["result"] is None and pending["error"] is None
            assert client.post("/v2/operations", headers=headers, json=payload).json()["id"] == operation_id

        resumed = create_app(tmp_path, "A" * 64, service)
        managers.append(resumed.state.management_operations)
        with TestClient(resumed) as client:
            assert client.get(url, headers=headers).json()["state"] == "running"
            if recovers:
                finish_executor_restart(tmp_path, operation_id, verified=True)
            else:
                # The public manager's deadline runs without an executor or a polling request.
                time.sleep(2.2)
                disk = json.loads((tmp_path / "data/.management-operations.json").read_text())
                assert disk["operations"][0]["state"] == "failed"
                finish_executor_restart(tmp_path, operation_id, verified=True)
            terminal = client.get(url, headers=headers).json()
            assert terminal["state"] == ("succeeded" if recovers else "failed")
            assert terminal["result"] == ({"completed": True} if recovers else None)
            assert terminal["error"] == (None if recovers else RESTART_EXPIRED_ERROR)
    finally:
        for manager in managers:
            manager.executor.shutdown(wait=True)


def test_public_executor_restart_without_verification_intent_never_falls_back_to_success(tmp_path):
    service = Mock()
    service.execute_operation.return_value = {"completed": True}
    app = create_app(tmp_path, "A" * 64, service)
    manager = app.state.management_operations
    headers = {"Authorization": "Bearer " + "A" * 64, "Host": "127.0.0.1",
               "Idempotency-Key": "restart-no-verification"}
    try:
        with TestClient(app) as client:
            response = client.post("/v2/operations", headers=headers, json={
                "operation": "service.lifecycle", "action": "restart",
                "service": "management-docker", "confirmed": True,
            })
            manager.executor.shutdown(wait=True)
            state = client.get("/v2/operations/" + response.json()["id"], headers=headers).json()
            assert state["state"] == "failed"
            assert state["result"] is None
    finally:
        manager.executor.shutdown(wait=True)


@pytest.mark.parametrize("action", ["restart", "apply"])
@pytest.mark.parametrize("recovers", [True, False])
def test_public_api_lifecycle_survives_api_restart_until_external_confirmation(
    tmp_path, action, recovers,
):
    def execute(operation_id, *args):
        persist_control_plane_pending(tmp_path, operation_id)
        path = tmp_path / "data/.management-operations.json"
        data = json.loads(path.read_text())
        data["operations"][0]["verification_deadline"] = (
            datetime.now(timezone.utc) + timedelta(seconds=2)
        ).isoformat()
        path.write_text(json.dumps(data))
        raise SetupError("API transport interrupted during its own lifecycle")

    service = Mock()
    service.execute_operation.side_effect = execute
    app = create_app(tmp_path, "A" * 64, service)
    headers = {"Authorization": "Bearer " + "A" * 64, "Host": "127.0.0.1",
               "Idempotency-Key": "api-own-lifecycle"}
    managers = [app.state.management_operations]
    try:
        with TestClient(app) as client:
            response = client.post("/v2/operations", headers=headers, json={
                "operation": "service.lifecycle", "action": action,
                "service": "management", "confirmed": True,
            })
            assert response.status_code == 202
            managers[0].executor.shutdown(wait=True)
            operation_id = response.json()["id"]
        resumed = create_app(tmp_path, "A" * 64, service)
        managers.append(resumed.state.management_operations)
        with TestClient(resumed) as client:
            url = "/v2/operations/" + operation_id
            pending = client.get(url, headers=headers).json()
            assert pending["state"] == "running"
            assert pending["progress"] == CONTROL_PROGRESS
            assert pending["result"] is None and pending["error"] is None
            assert pending["verification_deadline"]
            if not recovers:
                time.sleep(2.2)
            persist_external_result(tmp_path, operation_id, succeeded=True, result={"completed": True})
            terminal = client.get(url, headers=headers).json()
            assert terminal["state"] == ("succeeded" if recovers else "failed")
            assert terminal["result"] == ({"completed": True} if recovers else None)
    finally:
        for manager in managers:
            manager.executor.shutdown(wait=True)
