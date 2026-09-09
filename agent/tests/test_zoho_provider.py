"""Tests for Zoho calendar provider request serialization behavior."""

from datetime import datetime, timezone
from unittest.mock import patch

from assistant_agent.calendar.models import CalendarEvent, CreateEventRequest, ProviderConfig, UpdateEventRequest
from assistant_agent.calendar.providers.zoho import ZohoCalendarProvider, _zoho_to_dt


class _FakeTokenStore:
    def get_access_token(self, **kwargs):
        return "test-token"

    def invalidate(self, provider_name: str) -> None:
        return None


def _provider() -> ZohoCalendarProvider:
    config = ProviderConfig(
        name="zoho",
        provider_type="zoho",
        calendar_id="default",
        client_id="cid",
        client_secret="secret",
        refresh_token="refresh",
        extra={},
    )
    return ZohoCalendarProvider(config, _FakeTokenStore())


def test_list_events_sends_range_as_json_query_param() -> None:
    provider = _provider()
    captured = {}

    def fake_request(method, path, params=None, body=None, retry_on_auth=True):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = params
        return {"events": []}

    with patch.object(provider, "_request", side_effect=fake_request):
        provider.list_events(
            "calendar-uid-1",
            datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc),
        )

    assert captured["method"] == "GET"
    assert captured["path"] == "/calendars/calendar-uid-1/events"
    assert captured["params"] == {
        "range": '{"start":"20260910T090000Z","end":"20260910T100000Z"}'
    }


def test_create_event_sends_eventdata_in_query_params_not_body() -> None:
    provider = _provider()
    captured = {}

    def fake_request(method, path, params=None, body=None, retry_on_auth=True):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = params
        captured["body"] = body
        return {
            "events": [
                {
                    "uid": "evt-1@zoho.com",
                    "title": "Test",
                    "dateandtime": {
                        "start": "20260910T090000Z",
                        "end": "20260910T100000Z",
                    },
                }
            ]
        }

    request = CreateEventRequest(
        title="Test",
        start=datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc),
        calendar_id="calendar-uid-1",
    )

    with patch.object(provider, "_request", side_effect=fake_request):
        provider.create_event(request)

    assert captured["method"] == "POST"
    assert captured["path"] == "/calendars/calendar-uid-1/events"
    assert captured["body"] is None
    assert captured["params"] == {
        "eventdata": '{"title":"Test","dateandtime":{"start":"20260910T090000Z","end":"20260910T100000Z","timezone":"UTC"},"isprivate":false}'
    }


def test_update_event_sends_eventdata_in_query_params_not_body() -> None:
    provider = _provider()
    captured = {}

    def fake_request(method, path, params=None, body=None, retry_on_auth=True):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = params
        captured["body"] = body
        return {
            "events": [
                {
                    "uid": "evt-1@zoho.com",
                    "title": "Updated",
                    "dateandtime": {
                        "start": "20260910T090000Z",
                        "end": "20260910T100000Z",
                    },
                }
            ]
        }

    existing_event = CalendarEvent(
        provider_event_id="evt-1@zoho.com",
        provider_name="zoho",
        calendar_id="calendar-uid-1",
        title="Original",
        start=datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc),
    )

    request = UpdateEventRequest(title="Updated")

    with patch.object(provider, "_request", side_effect=fake_request):
        provider.update_event("calendar-uid-1", "evt-1@zoho.com", request, existing_event)

    assert captured["method"] == "PUT"
    assert captured["path"] == "/calendars/calendar-uid-1/events/evt-1%40zoho.com"
    assert captured["body"] is None
    assert captured["params"] == {"eventdata": '{"title":"Updated"}'}


def test_default_calendar_alias_resolves_to_primary_uid() -> None:
    provider = _provider()
    calls = []

    def fake_request(method, path, params=None, body=None, retry_on_auth=True):
        calls.append((method, path, params, body))
        if path == "/calendars":
            return {
                "calendars": [
                    {
                        "uid": "calendar-uid-primary",
                        "calname": "Primary",
                        "isdefault": True,
                    }
                ]
            }
        if path == "/calendars/calendar-uid-primary/events":
            return {"events": []}
        raise AssertionError("unexpected path %s" % path)

    with patch.object(provider, "_request", side_effect=fake_request):
        provider.list_events(
            "default",
            datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc),
        )

    # 1) resolve calendars list, 2) list events using resolved UID
    assert calls[0][1] == "/calendars"
    assert calls[1][1] == "/calendars/calendar-uid-primary/events"


def test_zoho_to_dt_parses_numeric_offset() -> None:
    start, all_day = _zoho_to_dt("20260910T090000+0400")
    end, _ = _zoho_to_dt("20260910T120000+0400")
    assert all_day is False
    assert start == datetime(2026, 9, 10, 5, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)


def test_zoho_to_dt_parses_colon_offset_and_zulu() -> None:
    offseted, _ = _zoho_to_dt("20260910T090000+04:00")
    zulu, _ = _zoho_to_dt("20260910T090000Z")
    assert offseted == datetime(2026, 9, 10, 5, 0, tzinfo=timezone.utc)
    assert zulu == datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)


def test_zoho_to_dt_parses_all_day() -> None:
    value, all_day = _zoho_to_dt("20260910")
    assert all_day is True
    assert value == datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)


def test_list_events_honors_zoho_offset_in_dateandtime() -> None:
    provider = _provider()

    def fake_request(method, path, params=None, body=None, retry_on_auth=True):
        return {
            "events": [
                {
                    "uid": "evt-dubai@zoho.com",
                    "title": "Morning block",
                    "dateandtime": {
                        "timezone": "Asia/Dubai",
                        "start": "20260910T090000+0400",
                        "end": "20260910T120000+0400",
                    },
                }
            ]
        }

    with patch.object(provider, "_request", side_effect=fake_request):
        events = provider.list_events(
            "calendar-uid-1",
            datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 10, 23, 59, tzinfo=timezone.utc),
        )

    assert len(events) == 1
    assert events[0].start == datetime(2026, 9, 10, 5, 0, tzinfo=timezone.utc)
    assert events[0].end == datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)


def test_invalid_oauth_scope_raises_provider_auth_error() -> None:
    provider = _provider()

    class _FakeResponse:
        status_code = 401
        text = "{\"error\":\"INVALID_OAUTHSCOPE\"}"
        is_success = False

        def json(self):
            return {"error": "INVALID_OAUTHSCOPE"}

    with patch("assistant_agent.calendar.providers.zoho.httpx.request", return_value=_FakeResponse()):
        from assistant_agent.calendar.provider import ProviderAuthError

        try:
            provider._request("POST", "/calendars/x/events", params={"eventdata": "{}"})
            raise AssertionError("expected ProviderAuthError")
        except ProviderAuthError as exc:
            assert "INVALID_OAUTHSCOPE" in str(exc)
            assert "ZohoCalendar.event.ALL" in str(exc)
