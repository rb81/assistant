import json
import os
from pathlib import Path
import shutil
from unittest.mock import MagicMock, Mock, patch

import pytest

from assistant_agent.config import AppConfig
from assistant_agent.heartbeat import Heartbeat
from assistant_agent.polling import run_poll_loop
from assistant_agent.server_control import bootstrap
from assistant_agent.supervisor import Supervisor
from assistant_agent.worker_health import (
    ProgressReporter, check_health, database_ready, poll_cycle, reporting,
)
from assistant_agent.workspace_index import WorkspaceIndex


def probe(path, role="task-agent", **kwargs):
    return check_health(role, path=path, probe_database=lambda: True, **kwargs)


def test_health_requires_real_completed_polling_and_does_not_survive_shutdown(tmp_path):
    path = tmp_path / "progress.json"
    with reporting("task-agent", path):
        assert not probe(path)[0]
        calls = []
        run_poll_loop(lambda delay=0: bool(delay), lambda: calls.append("poll"), 60,
                      should_sleep=lambda result: True)
        healthy, message = probe(path)
        assert calls == ["poll"]
        assert healthy
        assert "job/integration outcomes unverified" in message
    assert not probe(path)[0]


def test_polling_errors_are_unhealthy_even_with_a_previous_successful_cycle(tmp_path):
    path = tmp_path / "progress.json"
    with reporting("task-agent", path):
        with poll_cycle(60):
            pass
        assert probe(path)[0]
        failed_poll = Mock(side_effect=RuntimeError("private-token-must-not-be-published"))
        run_poll_loop(lambda delay=0: bool(delay), failed_poll, 60, should_sleep=lambda result: True,
                      logger=Mock())
        assert not probe(path)[0]
        assert "private-token" not in path.read_text()
        with poll_cycle(60):
            pass
        assert probe(path)[0]


@pytest.mark.parametrize("failing_callback", ["on_result", "should_sleep"])
def test_poll_callback_errors_do_not_report_success(tmp_path, failing_callback):
    path = tmp_path / "progress.json"
    callbacks = {"on_result": lambda result: None, "should_sleep": lambda result: True}
    callbacks[failing_callback] = Mock(side_effect=RuntimeError("callback failed"))
    with reporting("task-agent", path):
        run_poll_loop(lambda delay=0: bool(delay), lambda: True, 60, logger=Mock(), **callbacks)
        assert not probe(path)[0]


def test_busy_or_stalled_worker_does_not_refresh_success_from_a_timer(tmp_path):
    path = tmp_path / "progress.json"
    reporter = ProgressReporter("deep-research-agent", path)
    with reporter.cycle(2):
        pass
    completed = reporter.record["last_success"]
    with reporter.cycle(2):
        healthy, message = probe(path, "deep-research-agent", now=completed + 121)
        assert not healthy
        assert "work may still be in progress" in message
        assert reporter.record["last_success"] == completed


def test_poll_interval_sets_freshness_window_without_refreshing_during_idle(tmp_path):
    path = tmp_path / "progress.json"
    reporter = ProgressReporter("downloader", path)
    with reporter.cycle(600):
        pass
    completed = reporter.record["last_success"]
    assert probe(path, "downloader", now=completed + 1700)[0]
    assert not probe(path, "downloader", now=completed + 1801)[0]


def test_missing_wrong_role_future_or_symlinked_progress_cannot_report_healthy(tmp_path):
    path = tmp_path / "progress.json"
    assert not probe(path)[0]
    reporter = ProgressReporter("task-agent", path)
    with reporter.cycle(60):
        pass
    assert not probe(path, "deep-research-agent")[0]
    assert not probe(path, now=reporter.record["last_success"] - 1)[0]
    linked = tmp_path / "linked.json"
    linked.symlink_to(path)
    assert not probe(linked)[0]


def test_successful_poll_does_not_hide_an_unavailable_database(tmp_path):
    path = tmp_path / "progress.json"
    reporter = ProgressReporter("task-agent", path)
    with reporter.cycle(60):
        pass
    database = Mock(side_effect=RuntimeError("password=secret"))
    healthy, message = check_health("task-agent", path=path, probe_database=database)
    assert not healthy
    assert message == "Worker database readiness could not be verified"
    assert "secret" not in message


def test_database_probe_is_bounded_read_only_and_does_not_run_migrations():
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = (1,)
    with patch("psycopg.connect", return_value=connection) as connect, patch(
        "assistant_agent.config.database_url", return_value="postgresql://health-test",
    ), patch("assistant_agent.config.load_config"):
        assert database_ready()
    assert connect.call_args.kwargs["connect_timeout"] == 3
    assert "statement_timeout=3000" in connect.call_args.kwargs["options"]
    assert "default_transaction_read_only=on" in connect.call_args.kwargs["options"]
    connection.execute.assert_called_once_with("SELECT 1")


@pytest.mark.skipif(not os.getenv("ASSISTANT_TEST_DATABASE_URL"), reason="requires disposable PostgreSQL")
def test_read_only_health_probe_against_postgresql():
    with patch("assistant_agent.config.database_url", return_value=os.environ["ASSISTANT_TEST_DATABASE_URL"]), patch(
        "assistant_agent.config.load_config",
    ):
        assert database_ready()


def test_caught_workspace_scan_failure_is_not_reclassified_as_success(tmp_path):
    path = tmp_path / "progress.json"
    indexer = WorkspaceIndex(Mock(), AppConfig({}))
    scheduler = Mock()
    scheduler.idle.return_value = MagicMock()
    scheduler.idle.return_value.__enter__.return_value = True
    scheduler.startup_waiting.return_value = False
    with reporting("workspace-indexer", path), patch(
        "assistant_agent.workspace_index.EmbeddingScheduler", return_value=scheduler,
    ), patch.object(indexer, "run_once", side_effect=RuntimeError("scan unavailable")), patch.object(
        indexer, "refresh_embeddings",
    ):
        with poll_cycle(60):
            indexer.run_idle_once()
        assert not probe(path, "workspace-indexer")[0]


def test_oneshots_are_not_claimed_as_healthy_workers(tmp_path):
    healthy, message = probe(tmp_path / "progress.json", "deep-research-cron")
    assert not healthy
    assert message == "Worker health is not instrumented for this role"


@pytest.mark.parametrize("worker_type,role", [(Heartbeat, "heartbeat"), (Supervisor, "supervisor")])
def test_monitor_loops_publish_only_completed_cycles(tmp_path, worker_type, role):
    stopped = False

    def stopper(delay=0):
        nonlocal stopped
        if delay:
            stopped = True
        return stopped

    path = tmp_path / "progress.json"
    worker = worker_type(Mock(), AppConfig({}))
    with reporting(role, path), patch.object(worker, "run_once", return_value=0) as poll:
        worker.run_forever(stopper)
        poll.assert_called_once()
        assert probe(path, role)[0]


@pytest.mark.skipif(os.getenv("ASSISTANT_TEST_DOCKER") != "1", reason="requires Docker Compose")
def test_compose_health_configuration_parses_and_covers_long_running_services(tmp_path):
    root = Path(__file__).resolve().parents[3]
    shutil.copytree(root / "src/config", tmp_path / "src/config")
    shutil.copyfile(root / "src/docker-compose.yml", tmp_path / "src/docker-compose.yml")
    installation = bootstrap(tmp_path)
    installation.compose(["--profile", "*", "config", "--quiet"])
    services = json.loads(installation.compose(["--profile", "*", "config", "--format", "json"]))["services"]
    for name, definition in services.items():
        if definition.get("restart", "no") == "no":
            assert not definition.get("healthcheck"), name
            continue
        assert definition.get("healthcheck"), name
        if definition.get("environment", {}).get("AGENT_ROLE"):
            assert "assistant_agent.worker_health" in definition["healthcheck"]["test"], name
            assert any(mount.startswith("/run/assistant-health:") for mount in definition["tmpfs"]), name
