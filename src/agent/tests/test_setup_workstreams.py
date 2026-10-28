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


def workstream(identifier, thread="user:task", title="Task", parent=None):
    return {"id": identifier, "main_thread_id": thread, "title": title,
            "object_count": 0, "status": "active", "parent_id": parent}


@pytest.mark.parametrize("category, expected_id", [("User workstreams", 2), ("Administrative data", 1)])
def test_chooser_separates_administrative_data_from_user_workstreams(category, expected_id):
    menu = tools()
    rows = [workstream(2), workstream(1, "system:unattributed", "system:unattributed")]
    menu.command = MagicMock(return_value={"workstreams": rows})
    menu.select = MagicMock(side_effect=lambda label, choices: category if label == "Data category" else choices[0])

    assert menu.choose()["id"] == expected_id

    menu.select.assert_any_call("Data category", ["User workstreams", "Administrative data"])
    label, choices = menu.select.call_args.args
    assert label == category
    assert len(choices) == 1
    assert "system:unattributed" not in choices[0]
    assert "parent None" not in choices[0]
    if category == "Administrative data":
        assert "Unattributed administrative data" in choices[0]
        assert "zero objects does not mean empty" in menu.console.print.call_args.args[0]


def test_user_chooser_reports_no_user_workstreams_when_only_administrative_data_exists():
    menu = tools()
    menu.command = MagicMock(return_value={"workstreams": [workstream(1, "system:unattributed")]})
    menu.select = MagicMock(return_value="User workstreams")

    assert menu.choose() is None

    menu.console.print.assert_called_once_with("No user workstreams.")
    assert menu.select.call_count == 1


def test_administrative_chooser_scans_older_pages_without_reclassifying_system_task_threads():
    menu = tools()
    administrative = workstream(1, "system:unattributed", "Legacy administrative records")
    menu.command = MagicMock(side_effect=[
        {"workstreams": [workstream(i, f"system:research:{i}") for i in range(51, 1, -1)]},
        {"workstreams": [administrative]},
    ])
    menu.select = MagicMock(side_effect=lambda label, choices: "Administrative data" if label == "Data category" else choices[0])

    assert menu.choose() == administrative

    assert [call.args for call in menu.command.call_args_list] == [
        ("list", {"before_id": None}), ("list", {"before_id": 2}),
    ]


def test_user_chooser_preserves_pagination_and_parent_labels():
    menu = tools()
    rows = [workstream(i) for i in range(51, 2, -1)] + [workstream(2, "system:unattributed")]
    child = workstream(1, parent=7)
    menu.command = MagicMock(side_effect=[{"workstreams": rows}, {"workstreams": [child]}])
    menu.select = MagicMock(side_effect=["User workstreams", "Older workstreams", "#1 Task (0 objects, active; parent #7)"])

    assert menu.choose() == child

    assert "Older workstreams" in menu.select.call_args_list[1].args[1]
    menu.command.assert_called_with("list", {"before_id": 2})


def test_category_back_does_not_load_workstreams():
    menu = tools()
    menu.select = MagicMock(return_value="Back")
    menu.command = MagicMock()

    assert menu.choose() is None

    menu.command.assert_not_called()


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
