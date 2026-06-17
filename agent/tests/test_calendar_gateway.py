import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Optional

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

from assistant_agent.calendar_gateway import CalendarError, CalendarGateway
from assistant_agent.config import AppConfig


class FakeCalendarDatabase:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if "FROM calendar_managed_events" in sql:
            return [event for event in self.events if event["status"] == "active"]
        return []

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        normalized = " ".join(sql.split())
        if "metadata->>'idempotency_key'" in normalized:
            key = str(params[0])
            return next((event for event in self.events if event["status"] == "active" and event["metadata"].get("idempotency_key") == key), None)
        if normalized.startswith("SELECT * FROM calendar_managed_events WHERE assistant_id"):
            assistant_id = str(params[0])
            return next((event for event in self.events if event["assistant_id"] == assistant_id and event["status"] == "active"), None)
        if normalized.startswith("INSERT INTO calendar_managed_events"):
            (
                assistant_id,
                uid,
                calendar_name,
                relative_path,
                summary,
                starts_at,
                ends_at,
                file_hash,
                created_by_job_id,
                updated_by_job_id,
                metadata,
            ) = params
            row = {
                "assistant_id": assistant_id,
                "uid": uid,
                "calendar_name": calendar_name,
                "relative_path": relative_path,
                "summary": summary,
                "starts_at": starts_at,
                "ends_at": ends_at,
                "file_hash": file_hash,
                "status": "active",
                "created_by_job_id": created_by_job_id,
                "updated_by_job_id": updated_by_job_id,
                "metadata": metadata,
            }
            self.events.append(row)
            return row
        if normalized.startswith("UPDATE calendar_managed_events SET calendar_name"):
            (
                calendar_name,
                relative_path,
                summary,
                starts_at,
                ends_at,
                file_hash,
                updated_by_job_id,
                metadata,
                assistant_id,
            ) = params
            row = next(event for event in self.events if event["assistant_id"] == assistant_id and event["status"] == "active")
            row.update(
                {
                    "calendar_name": calendar_name,
                    "relative_path": relative_path,
                    "summary": summary,
                    "starts_at": starts_at,
                    "ends_at": ends_at,
                    "file_hash": file_hash,
                    "updated_by_job_id": updated_by_job_id,
                    "metadata": metadata,
                }
            )
            return row
        if normalized.startswith("UPDATE calendar_managed_events SET status = 'deleted'"):
            updated_by_job_id, assistant_id = params
            row = next(event for event in self.events if event["assistant_id"] == assistant_id and event["status"] == "active")
            row["status"] = "deleted"
            row["updated_by_job_id"] = updated_by_job_id
            return row
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        if "INSERT INTO calendar_event_audit" in sql:
            assistant_id, job_id, action, payload = params
            self.audit.append({"assistant_id": assistant_id, "job_id": job_id, "action": action, "payload": payload})


def calendar_config(vdir_path: str, allow_details: bool = False) -> AppConfig:
    return AppConfig(
        {
            "agent": {
                "app": {"timezone": "UTC"},
                "calendar": {
                    "enabled": True,
                    "store": {"vdir_path": vdir_path, "default_calendar": "main"},
                    "sync": {"command": [], "before_read": False, "before_write": False, "after_write": False},
                    "policy": {"allow_read_event_details": allow_details},
                    "limits": {"max_occurrences_per_event": 20},
                },
            }
        }
    )


def write_external_event(vdir_path: Path) -> None:
    calendar_dir = vdir_path / "main"
    calendar_dir.mkdir(parents=True)
    (calendar_dir / "external.ics").write_text(
        "\r\n".join(
            [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "BEGIN:VEVENT",
                "UID:external@example.test",
                "DTSTART:20260608T090000Z",
                "DTEND:20260608T100000Z",
                "SUMMARY:Private Meeting",
                "LOCATION:Boardroom",
                "END:VEVENT",
                "END:VCALENDAR",
                "",
            ]
        ),
        encoding="utf-8",
    )


class CalendarGatewayTest(unittest.TestCase):
    def test_list_busy_redacts_non_managed_details_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vdir_path = Path(temp_dir) / "vdir"
            write_external_event(vdir_path)

            gateway = CalendarGateway(FakeCalendarDatabase(), calendar_config(str(vdir_path)), {"id": 3, "thread_id": "thread"})
            result = gateway.list_busy("2026-06-08T00:00:00Z", "2026-06-09T00:00:00Z", include_details=True)

            self.assertEqual(result["busy"][0]["title"], "Busy")
            self.assertIsNone(result["busy"][0]["event_id"])

    def test_managed_create_update_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vdir_path = Path(temp_dir) / "vdir"
            write_external_event(vdir_path)
            db = FakeCalendarDatabase()
            gateway = CalendarGateway(db, calendar_config(str(vdir_path)), {"id": 12, "thread_id": "thread"})

            created = gateway.create_event(
                title="Focus block",
                start="2026-06-08T11:00:00Z",
                end="2026-06-08T12:00:00Z",
                idempotency_key="focus-2026-06-08",
            )
            event_id = created["event"]["event_id"]
            created_path = next((vdir_path / "main").glob("*local-calendar-gateway.ics"))
            content = created_path.read_text(encoding="utf-8")

            self.assertIn("X-ASSISTANT-MANAGED:TRUE", content)
            self.assertIn(event_id, content)
            self.assertEqual(db.audit[0]["action"], "created")
            # Verify Apple-compatible VALARM sentinel is always present
            self.assertIn("X-APPLE-DEFAULT-ALARM:TRUE", content)
            self.assertIn("ACTION:NONE", content)
            self.assertIn("TRIGGER;VALUE=DATE-TIME:19760401T005545Z", content)
            # Verify the real display alarm is also present (default 15 min)
            self.assertIn("ACTION:DISPLAY", content)
            self.assertIn("TRIGGER:-PT15M", content)

            updated = gateway.update_event(event_id, title="Deep work", end="2026-06-08T12:30:00Z")
            self.assertEqual(updated["event"]["title"], "Deep work")
            updated_content = created_path.read_text(encoding="utf-8")
            # Verify the new TZID-parameterized format is used
            self.assertIn("DTEND;TZID=UTC:20260608T123000", updated_content)
            self.assertIn("VTIMEZONE", updated_content)

            listed = gateway.list_events("2026-06-08T00:00:00Z", "2026-06-09T00:00:00Z", managed_only=True)
            self.assertEqual(listed["events"][0]["event_id"], event_id)
            self.assertEqual(listed["events"][0]["title"], "Deep work")

            deleted = gateway.delete_event(event_id)
            self.assertEqual(deleted["event"]["status"], "deleted")
            self.assertFalse(created_path.exists())
            self.assertEqual(db.audit[-1]["action"], "deleted")

    def test_delete_refuses_non_managed_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vdir_path = Path(temp_dir) / "vdir"
            write_external_event(vdir_path)
            gateway = CalendarGateway(FakeCalendarDatabase(), calendar_config(str(vdir_path)), {"id": 12, "thread_id": "thread"})

            with self.assertRaisesRegex(CalendarError, "managed calendar event not found"):
                gateway.delete_event("external@example.test")

    def test_create_event_with_attendees(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vdir_path = Path(temp_dir) / "vdir"
            db = FakeCalendarDatabase()
            gateway = CalendarGateway(db, calendar_config(str(vdir_path)), {"id": 15, "thread_id": "thread"})

            created = gateway.create_event(
                title="Team meeting",
                start="2026-06-10T14:00:00Z",
                end="2026-06-10T15:00:00Z",
                attendees=["alice@example.com", "bob@example.com"],
            )
            event_id = created["event"]["event_id"]
            created_path = next((vdir_path / "main").glob("*local-calendar-gateway.ics"))
            content = created_path.read_text(encoding="utf-8")

            # Verify attendees with proper scheduling parameters
            self.assertIn("ATTENDEE;PARTSTAT=NEEDS-ACTION;RSVP=TRUE;ROLE=REQ-PARTICIPANT:mailto:alice@example.com", content.replace("\n ", ""))
            self.assertIn("ATTENDEE;PARTSTAT=NEEDS-ACTION;RSVP=TRUE;ROLE=REQ-PARTICIPANT:mailto:bob@example.com", content.replace("\n ", ""))
            self.assertIn("ORGANIZER;CN=Assistant:mailto:assistant@local", content.replace("\n ", ""))
            self.assertIn("VTIMEZONE", content)

            updated = gateway.update_event(event_id, attendees=["charlie@example.com"])
            updated_content = created_path.read_text(encoding="utf-8")
            unfolded_update = updated_content.replace("\n ", "")
            self.assertIn("ATTENDEE;PARTSTAT=NEEDS-ACTION;RSVP=TRUE;ROLE=REQ-PARTICIPANT:mailto:charlie@example.com", unfolded_update)
            self.assertNotIn("alice@example.com", updated_content)


if __name__ == "__main__":
    unittest.main()
