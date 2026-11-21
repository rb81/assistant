import json
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from assistant_agent.config import AppConfig


SCRIPT = Path(__file__).resolve().parents[3] / "examples/diagnose-routing.sh"


@pytest.mark.parametrize("arguments", [["invalid"], ["0"], ["1", "2"]])
def test_invalid_diagnostic_arguments_fail_before_docker(arguments):
    result = subprocess.run(
        ["/bin/sh", str(SCRIPT), *arguments], capture_output=True, text=True,
        env={"PATH": "/nonexistent"},
    )
    assert result.returncode == 2
    assert not result.stdout


@pytest.mark.parametrize("task_id", ["", "7"])
def test_diagnostics_are_read_only_and_exclude_private_content(task_id, monkeypatch, capsys):
    source = SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    connection = MagicMock()
    queries = []

    def execute(sql, parameters=None):
        assert connection.read_only is True
        assert sql.strip().startswith(("SET ", "SELECT "))
        projection = re.sub(r"char_length\((?:d\.)?body_text\)", "", sql)
        projection = re.sub(r"output_data->>'(?:error|notification|sent)'", "", projection)
        for private in ("body_text", "to_addresses", "subject", "sender_id", "input_data", "output_data"):
            assert private not in projection
        queries.append((sql, parameters))
        cursor = MagicMock()
        cursor.fetchone.return_value = {"id": 7, "status": "completed", "reply_channel": "client"}
        cursor.fetchall.return_value = []
        return cursor

    connection.execute.side_effect = execute
    connection.__enter__.return_value = connection
    monkeypatch.setattr(sys, "argv", ["diagnose", task_id])
    config = AppConfig({"agent": {"approvals": {"channel": "client"},
                                 "admin": {"email": "private@example.test"},
                                 "database": {"password": "private-password"}}})
    with patch("psycopg.connect", return_value=connection), \
            patch("assistant_agent.config.load_config", return_value=config), \
            patch("assistant_agent.config.database_url", return_value="postgresql://not-used"):
        exec(compile(source, str(SCRIPT), "exec"), {})

    output = capsys.readouterr().out
    assert "private@example.test" not in output
    assert "private-password" not in output
    report = json.loads(output)
    assert report["settings"]["agent.approvals.channel"] == "client"
    if task_id:
        assert report["task"]["id"] == 7
        assert all(parameters == (7,) for sql, parameters in queries if sql.strip().startswith("SELECT "))
    else:
        assert report["recent_tasks"] == []
