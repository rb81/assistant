import sys
import types
import unittest
from typing import Any

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda handle: {}))
psycopg_module = types.ModuleType("psycopg")
psycopg_module.connect = lambda *args, **kwargs: None
psycopg_module.Connection = object
sys.modules.setdefault("psycopg", psycopg_module)
rows_module = types.ModuleType("psycopg.rows")
rows_module.dict_row = object()
sys.modules.setdefault("psycopg.rows", rows_module)
json_module = types.ModuleType("psycopg.types.json")
json_module.Jsonb = lambda value: value
sys.modules.setdefault("psycopg.types", types.ModuleType("psycopg.types"))
sys.modules.setdefault("psycopg.types.json", json_module)
notifications_module = types.ModuleType("assistant_agent.notifications")
notifications_module.notify_admin_job_failure = lambda *args, **kwargs: None
sys.modules.setdefault("assistant_agent.notifications", notifications_module)

from assistant_agent.config import AppConfig
from assistant_agent.supervisor import Supervisor


class FakeDatabase:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self.rows


class SupervisorRepeatedFailureTest(unittest.TestCase):
    def supervisor_for(self, rows: list[dict[str, Any]]) -> Supervisor:
        return Supervisor(FakeDatabase(rows), AppConfig({"agent": {}}))  # type: ignore[arg-type]

    def test_recovered_tool_failure_does_not_flag(self) -> None:
        rows = [
            {"event_type": "tool_result", "tool_name": "command_execute", "output_data": {"exit_code": 0}},
            {"event_type": "error", "tool_name": "command_execute", "output_data": {"error": "sandbox unavailable"}},
            {"event_type": "error", "tool_name": "command_execute", "output_data": {"error": "sandbox unavailable"}},
            {"event_type": "error", "tool_name": "command_execute", "output_data": {"error": "sandbox unavailable"}},
        ]

        self.assertEqual(self.supervisor_for(rows).repeated_failure(57), "")

    def test_admin_escalation_does_not_flag(self) -> None:
        rows = [
            {
                "event_type": "supervisor_note",
                "tool_name": None,
                "output_data": {"notification": "admin_failure_email", "sent": True},
            },
            {"event_type": "error", "tool_name": "command_execute", "output_data": {"error": "sandbox unavailable"}},
            {"event_type": "error", "tool_name": "command_execute", "output_data": {"error": "sandbox unavailable"}},
            {"event_type": "error", "tool_name": "command_execute", "output_data": {"error": "sandbox unavailable"}},
        ]

        self.assertEqual(self.supervisor_for(rows).repeated_failure(57), "")

    def test_three_unresolved_same_tool_errors_flag(self) -> None:
        rows = [
            {"event_type": "error", "tool_name": "command_execute", "output_data": {"error": "sandbox unavailable"}},
            {"event_type": "error", "tool_name": "command_execute", "output_data": {"error": "sandbox unavailable"}},
            {"event_type": "error", "tool_name": "command_execute", "output_data": {"error": "sandbox unavailable"}},
        ]

        self.assertEqual(
            self.supervisor_for(rows).repeated_failure(57),
            "Tool command_execute failed three times without recovery",
        )

    def test_different_tool_error_breaks_streak(self) -> None:
        rows = [
            {"event_type": "error", "tool_name": "file_read", "output_data": {"error": "missing"}},
            {"event_type": "error", "tool_name": "command_execute", "output_data": {"error": "sandbox unavailable"}},
            {"event_type": "error", "tool_name": "command_execute", "output_data": {"error": "sandbox unavailable"}},
        ]

        self.assertEqual(self.supervisor_for(rows).repeated_failure(57), "")


if __name__ == "__main__":
    unittest.main()
