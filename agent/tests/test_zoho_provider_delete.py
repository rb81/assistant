"""Tests for Zoho calendar provider delete functionality with etag requirement."""

from datetime import datetime, timezone
from unittest.mock import patch

from assistant_agent.calendar.models import ProviderConfig
from assistant_agent.calendar.providers.zoho import ZohoCalendarProvider


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


def test_delete_event_fetches_etag_and_sends_eventdata() -> None:
    """Delete must fetch the event to get etag, then send uid+etag in eventdata."""
    provider = _provider()
    captured = {}
    call_count = [0]

    def fake_request(method, path, params=None, body=None, retry_on_auth=True):
        call_count[0] += 1
        captured[f"call_{call_count[0]}_method"] = method
        captured[f"call_{call_count[0]}_path"] = path
        captured[f"call_{call_count[0]}_params"] = params
        
        # First call: GET to fetch etag
        if call_count[0] == 1:
            return {
                "events": [
                    {
                        "uid": "evt-delete@zoho.com",
                        "title": "Event to Delete",
                        "etag": 1692771653044,
                        "dateandtime": {
                            "start": "20260910T090000Z",
                            "end": "20260910T100000Z",
                        },
                    }
                ]
            }
        # Second call: DELETE with eventdata
        return {
            "events": [
                {
                    "uid": "evt-delete@zoho.com",
                    "estatus": "deleted",
                    "caluid": "calendar-uid-1",
                }
            ]
        }

    with patch.object(provider, "_request", side_effect=fake_request):
        provider.delete_event("calendar-uid-1", "evt-delete@zoho.com")

    # Verify GET was called first
    assert captured["call_1_method"] == "GET"
    assert captured["call_1_path"] == "/calendars/calendar-uid-1/events/evt-delete@zoho.com"
    
    # Verify DELETE was called with eventdata containing uid + etag
    assert captured["call_2_method"] == "DELETE"
    assert captured["call_2_path"] == "/calendars/calendar-uid-1/events/evt-delete@zoho.com"
    
    import json
    eventdata = json.loads(captured["call_2_params"]["eventdata"])
    assert eventdata["uid"] == "evt-delete@zoho.com"
    assert eventdata["etag"] == 1692771653044
