import io
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from assistant_agent.setup import DockerCommandError, Installation, SetupError
from assistant_agent.setup_workstreams import WorkstreamTools


def tools():
    menu = WorkstreamTools.__new__(WorkstreamTools)
    menu.console = MagicMock()
    menu.instance = MagicMock()
    return menu


@pytest.mark.parametrize("confirm", [False, True])
def test_authoritative_preview_requires_separate_default_no_confirmation(confirm):
    menu = tools()
    preview = {"preview_digest": "exact-scope", "running_jobs": [], "running_research": [], "blocked_files": [], "blocked_reminders": []}
    menu.command = MagicMock(side_effect=[preview, {"status": "completed"}])
    menu.confirm = MagicMock(return_value=confirm)
    menu.confirm_delete({"workstream_id": 7, "mode": "delete_objects"})
    assert menu.confirm.call_args.args[1] is False
    assert "Shared objects" in menu.confirm.call_args.args[0]
    if confirm:
        assert menu.command.call_args.args == ("delete", {
            "workstream_id": 7, "mode": "delete_objects", "preview_digest": "exact-scope",
            "confirmed": True, "writers_stopped": True,
        })
    else:
        menu.command.assert_called_once_with("preview", {"workstream_id": 7, "mode": "delete_objects"})


@pytest.mark.parametrize("incomplete", [False, True])
def test_writer_services_restart_only_after_cleanup_is_confirmed_complete(incomplete):
    menu = tools()
    menu.instance.compose.return_value = "task-agent\nworkspace-indexer\npostgres\n"
    menu.command = MagicMock(return_value={"deletions": [{"id": 9, "status": "failed"}] if incomplete else []})
    operation = MagicMock()
    menu.with_stopped_writers(operation)
    operation.assert_called_once()
    assert menu.instance.compose.call_args_list[1].args[0] == ["stop", "--timeout", "120", "task-agent", "workspace-indexer"]
    if incomplete:
        assert menu.instance.compose.call_count == 2
    else:
        assert menu.instance.compose.call_args.args[0] == ["start", "task-agent", "workspace-indexer"]


def test_active_jobs_block_deletion_before_confirmation():
    menu = tools()
    menu.command = MagicMock(return_value={"running_jobs": [1], "running_research": [], "blocked_files": []})
    menu.confirm = MagicMock()
    menu.confirm_delete({"workstream_id": 1, "mode": "connected"})
    menu.confirm.assert_not_called()
    menu.command.assert_called_once_with("preview", {"workstream_id": 1, "mode": "connected"})


@pytest.mark.parametrize("output, expected", [
    (json.dumps({"workstream_error": "Workstream schema is not installed; start the updated stack to apply migrations first"}),
     "Workstream schema is not installed"),
    ("secret-canary", "helper failed"),
    (json.dumps(["secret-canary"]), "helper failed"),
    (json.dumps({"error": "secret-canary"}), "helper failed"),
])
def test_command_surfaces_only_structured_operator_errors(output, expected):
    menu = tools()
    menu.image_ready = True
    menu.instance.root = Path("/installation")
    menu.instance.compose.side_effect = DockerCommandError("Docker command failed; output was suppressed", stdout=output)
    with pytest.raises(SetupError) as raised:
        menu.command("preview", {"workstream_id": 1})
    assert "Workstream preview" in str(raised.value)
    assert expected in str(raised.value)
    assert "secret-canary" not in str(raised.value)


def test_command_identifies_image_build_failure():
    menu = tools()
    menu.instance.compose.side_effect = DockerCommandError("Docker command failed; output was suppressed")
    with pytest.raises(SetupError, match="helper image build failed"):
        menu.command("list")
    assert not getattr(menu, "image_ready", False)
    menu.instance.compose.assert_called_once_with(["build", "task-agent"], timeout=1800)


@pytest.mark.parametrize("error, expected", [
    (psycopg.OperationalError("secret-canary"), "Database connection failed"),
    (psycopg.ProgrammingError("secret-canary"), "Database schema or query failed"),
    (PermissionError("secret-canary"), "Filesystem or network operation failed"),
    (RuntimeError("secret-canary"), "Unexpected workstream failure"),
])
def test_operator_errors_reach_menu_without_exception_details(error, expected, tmp_path, capsys):
    from assistant_agent import workstream_operator

    instance = Installation(tmp_path)

    def run_operator(*args, **kwargs):
        with patch.object(workstream_operator, "load_config", side_effect=error), \
             patch("sys.argv", ["workstream_operator", "list"]), \
             patch("sys.stdin", io.StringIO("{}")), \
             pytest.raises(SystemExit) as raised:
            workstream_operator.main()
        assert raised.value.code == 1
        captured = capsys.readouterr()
        assert not captured.err
        assert "secret-canary" not in captured.out
        return subprocess.CompletedProcess([], 1, captured.out, "secret-canary")

    menu = tools()
    menu.instance = instance
    menu.image_ready = True
    with patch("assistant_agent.setup.subprocess.run", side_effect=run_operator), \
         pytest.raises(SetupError) as raised:
        menu.command("list")
    assert expected in str(raised.value)
    assert "secret-canary" not in str(raised.value)


def test_operator_expected_rejection_reaches_menu(capsys):
    from assistant_agent import workstream_operator
    from assistant_agent.workstreams import WorkstreamError

    with patch.object(workstream_operator, "load_config", side_effect=WorkstreamError("scope changed; preview again before confirming")), \
         patch("sys.argv", ["workstream_operator", "delete"]), \
         patch("sys.stdin", io.StringIO("{}")), \
         pytest.raises(SystemExit):
        workstream_operator.main()
    assert json.loads(capsys.readouterr().out) == {"workstream_error": "scope changed; preview again before confirming"}
