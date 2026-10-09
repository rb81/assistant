import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


class ZoomMeetingTest(unittest.TestCase):
    def setUp(self):
        from assistant_agent.config import AppConfig
        from assistant_agent.zoom_meetings import ZoomError, ZoomMeetings
        self.error = ZoomError
        self.db = MagicMock()
        self.db.get_state.return_value = {}
        self.service = ZoomMeetings(self.db, AppConfig({"agent": {"app": {"timezone": "Asia/Dubai"}, "zoom": {"enabled": True, "client_id": "test-client", "client_secret": "test-secret", "account_id": "test-account"}}}), {"id": 1})
        self.row = {"id": 1, "provider_meeting_id": "123", "title": "Meeting", "starts_at": datetime(2026, 10, 8, 10, tzinfo=timezone.utc), "duration_minutes": 30, "join_url": "https://zoom.us/j/123", "status": "active", "calendar_event_id": None}

    def test_create_uses_assistant_timezone_and_never_exposes_host_url(self):
        self.db.fetch_one.side_effect = [{**self.row, "status": "creating"}, self.row]
        with patch.object(self.service, "request", return_value={"id": 123, "join_url": self.row["join_url"], "start_url": "host-secret"}) as request:
            result = self.service.create("Meeting", "2026-10-08T14:00:00", 30, "stable-request")
        self.assertEqual(request.call_args.args[2]["start_time"], "2026-10-08T10:00:00Z")
        self.assertNotIn("host-secret", str(result))
        self.assertNotIn("host-secret", str(self.db.mock_calls))

    def test_uncertain_creation_is_not_repeated(self):
        self.db.fetch_one.side_effect = [None, {**self.row, "status": "uncertain", "request_payload": {"different": True}}]
        with patch.object(self.service, "request") as request:
            with self.assertRaises(self.error):
                self.service.create("Meeting", "2026-10-08T14:00:00", 30, "stable-request")
            request.assert_not_called()

    def test_update_preserves_explicit_offset_and_managed_only_access(self):
        self.db.fetch_one.side_effect = [self.row, self.row]
        with patch.object(self.service, "request") as request:
            self.service.update(1, start="2026-10-08T14:00:00-04:00")
            self.assertEqual(request.call_args.args[2]["start_time"], "2026-10-08T18:00:00Z")
        self.db.fetch_one.side_effect = None
        self.db.fetch_one.return_value = None
        with self.assertRaisesRegex(self.error, "not managed"):
            self.service.update(999, title="Foreign meeting")

    def test_creation_timeout_remains_uncertain(self):
        self.db.fetch_one.return_value = self.row
        with patch.object(self.service, "request", side_effect=self.error("timeout", uncertain=True)):
            with self.assertRaises(self.error):
                self.service.create("Meeting", "2026-10-08T14:00:00", 30, "timeout-key")
        self.assertEqual(self.db.execute.call_args.args[1][0], "uncertain")

    def test_rate_limit_prevents_immediate_retry(self):
        response = MagicMock(status_code=429, headers={"Retry-After": "90"})
        with patch.object(self.service, "token", return_value="token"), patch("httpx.request", return_value=response):
            with self.assertRaisesRegex(self.error, "429"):
                self.service.request("GET", "meetings/123")
        state = self.db.set_state.call_args.args[1]
        self.db.get_state.return_value = state
        with patch("httpx.request") as request:
            with self.assertRaisesRegex(self.error, "429"):
                self.service.request("GET", "meetings/123")
            request.assert_not_called()

    def test_zoom_tools_are_loadable_and_calendar_supports_linking(self):
        from assistant_agent.tools import available_function_names, FUNCTION_TOOLS
        available = available_function_names(self.service.config)
        self.assertIn("zoom_create_meeting", available)
        self.assertIn("zoom_update_meeting", available)
        calendar = next(tool["function"] for tool in FUNCTION_TOOLS if tool["function"]["name"] == "calendar_create_event")
        self.assertIn("zoom_meeting_id", calendar["parameters"]["properties"])

    def test_oauth_rotation_is_persisted_and_reused(self):
        self.service.mode = "oauth"
        self.service.refresh_token = "original-refresh"
        cursor = self.db.connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        response = MagicMock(status_code=200)
        response.json.return_value = {"access_token": "access-one", "refresh_token": "rotated-refresh", "expires_in": 3600}
        with patch("httpx.post", return_value=response) as send:
            self.assertEqual(self.service.token(), "access-one")
            wrapped = cursor.execute.call_args.args[1][1]
            state = getattr(wrapped, "obj", wrapped)
            cursor.fetchone.return_value = {"value": state}
            self.service.token(force=True)
            self.assertEqual(send.call_args.kwargs["data"]["refresh_token"], "rotated-refresh")

    def test_linked_calendar_creation_and_partial_update(self):
        from assistant_agent.config import AppConfig
        from assistant_agent.tools import ToolRuntime, ToolError
        from assistant_agent.calendar.provider import CalendarError
        runtime = ToolRuntime.__new__(ToolRuntime)
        runtime.db = self.db
        runtime.job = {"id": 1}
        runtime.config = AppConfig({"agent": {"app": {"timezone": "Asia/Dubai"}, "calendar": {"enabled": True}, "zoom": {"enabled": True}}})
        runtime._zoom_service = MagicMock()
        runtime._zoom_service.managed.return_value = self.row
        calendar = MagicMock()
        calendar.create_event.return_value = {"event": {"event_id": "calendar-id"}}
        with patch("assistant_agent.tools.CalendarRouter", return_value=calendar):
            runtime.calendar_create_event("Meeting", "2026-10-08T14:00:00", "2026-10-08T14:30:00", zoom_meeting_id=1)
        self.assertIn(self.row["join_url"], calendar.create_event.call_args.kwargs["description"])
        self.assertEqual(calendar.create_event.call_args.kwargs["idempotency_key"], "zoom-calendar:1")
        self.db.fetch_one.return_value = {**self.row, "calendar_event_id": "calendar-id"}
        calendar.update_event.side_effect = CalendarError("provider unavailable")
        with patch("assistant_agent.tools.CalendarRouter", return_value=calendar), self.assertRaises(ToolError):
            runtime.calendar_update_event("calendar-id", start="2026-10-08T15:00:00", end="2026-10-08T15:45:00")
        self.assertEqual(runtime._zoom_service.update.call_args.kwargs["duration_minutes"], 45)
        self.assertFalse(runtime._zoom_service.update.call_args.kwargs["sync_calendar"])
        self.assertIn("calendar_sync_pending", self.db.execute.call_args.args[0])