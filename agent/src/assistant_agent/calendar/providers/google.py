"""
Google Calendar provider.

API reference: https://developers.google.com/calendar/api/v3/reference

Required OAuth2 scope: https://www.googleapis.com/auth/calendar
Grant type: refresh_token (offline access)

To obtain a refresh token, follow the setup guide in docs/calendar-providers.md.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ..models import (
    Attendee,
    BusyWindow,
    Calendar,
    CalendarEvent,
    CreateEventRequest,
    ProviderConfig,
    UpdateEventRequest,
)
from ..provider import CalendarError, ProviderAuthError, ProviderEventNotFoundError
from ..token_store import TokenStore

log = logging.getLogger(__name__)

_BASE = "https://www.googleapis.com/calendar/v3"


def _dt_to_google(dt: datetime, all_day: bool) -> dict[str, str]:
    """Convert a datetime to a Google API dateTime/date dict."""
    if all_day:
        return {"date": dt.astimezone(timezone.utc).date().isoformat()}
    return {"dateTime": dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}


def _google_to_dt(value: dict[str, str]) -> tuple[datetime, bool]:
    """Parse a Google API dateTime/date dict into (datetime, all_day)."""
    if "date" in value:
        from datetime import date, time
        d = date.fromisoformat(value["date"])
        dt = datetime.combine(d, time.min, tzinfo=timezone.utc)
        return dt, True
    raw = value.get("dateTime", "")
    # Google returns RFC3339 — datetime.fromisoformat handles this in 3.11+.
    # For compatibility, normalise the trailing Z first.
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc), False


def _parse_attendees(raw: list[dict[str, Any]]) -> list[Attendee]:
    out = []
    for item in raw or []:
        email = item.get("email", "")
        if not email:
            continue
        status_map = {
            "accepted": "accepted",
            "declined": "declined",
            "tentative": "tentative",
            "needsAction": "needs-action",
        }
        out.append(Attendee(
            email=email,
            name=item.get("displayName", ""),
            status=status_map.get(item.get("responseStatus", "needsAction"), "needs-action"),
        ))
    return out


class GoogleCalendarProvider:
    """Google Calendar REST API provider."""

    def __init__(self, config: ProviderConfig, token_store: TokenStore) -> None:
        self._config = config
        self._tokens = token_store

    # ------------------------------------------------------------------
    # CalendarProvider protocol
    # ------------------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return self._config.name

    def list_calendars(self) -> list[Calendar]:
        data = self._get("/users/me/calendarList")
        calendars = []
        for item in data.get("items", []):
            calendars.append(Calendar(
                id=item["id"],
                name=item.get("summary", item["id"]),
                provider_name=self.provider_name,
                description=item.get("description", ""),
                color=item.get("backgroundColor", ""),
                is_primary=bool(item.get("primary")),
                is_read_only=(item.get("accessRole") in ("reader", "freeBusyReader")),
            ))
        return calendars

    def list_events(
        self,
        calendar_id: str,
        start: datetime,
        end: datetime,
    ) -> list[CalendarEvent]:
        params: dict[str, Any] = {
            "timeMin": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "timeMax": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "singleEvents": "true",     # expand recurring events
            "orderBy": "startTime",
            "maxResults": 2500,
        }
        data = self._get("/calendars/%s/events" % _enc(calendar_id), params=params)
        events = []
        for item in data.get("items", []):
            event = self._parse_event(item, calendar_id)
            if event:
                events.append(event)
        return events

    def create_event(self, request: CreateEventRequest) -> CalendarEvent:
        body: dict[str, Any] = {
            "summary": request.title,
            "start": _dt_to_google(request.start, request.all_day),
            "end": _dt_to_google(request.end, request.all_day),
            "description": request.description or "",
            "location": request.location or "",
            "transparency": "transparent" if request.transparency.lower() == "transparent" else "opaque",
            "status": "confirmed",
        }
        if request.attendees:
            body["attendees"] = [{"email": e} for e in request.attendees]
        if request.alert_minutes is not None and request.alert_minutes > 0:
            body["reminders"] = {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": request.alert_minutes}],
            }
        data = self._post("/calendars/%s/events" % _enc(request.calendar_id), body)
        event = self._parse_event(data, request.calendar_id)
        if event is None:
            raise CalendarError("Google Calendar returned an unparse-able event after create")
        return event

    def update_event(
        self,
        calendar_id: str,
        provider_event_id: str,
        request: UpdateEventRequest,
        existing: CalendarEvent,
    ) -> CalendarEvent:
        # Build PATCH body with only changed fields.
        patch: dict[str, Any] = {}
        if request.title is not None:
            patch["summary"] = request.title
        if request.description is not None:
            patch["description"] = request.description
        if request.location is not None:
            patch["location"] = request.location
        if request.transparency is not None:
            patch["transparency"] = (
                "transparent" if request.transparency.lower() == "transparent" else "opaque"
            )

        # Resolve new start/end considering all_day flag.
        next_all_day = request.all_day if request.all_day is not None else existing.all_day
        next_start = request.start if request.start is not None else existing.start
        next_end = request.end if request.end is not None else existing.end
        if request.start is not None or request.end is not None or request.all_day is not None:
            patch["start"] = _dt_to_google(next_start, next_all_day)
            patch["end"] = _dt_to_google(next_end, next_all_day)

        if request.attendees is not None:
            patch["attendees"] = [{"email": e} for e in request.attendees]

        if request.alert_minutes is not None:
            if request.alert_minutes > 0:
                patch["reminders"] = {
                    "useDefault": False,
                    "overrides": [{"method": "popup", "minutes": request.alert_minutes}],
                }
            else:
                patch["reminders"] = {"useDefault": False, "overrides": []}

        if not patch:
            # Nothing changed — fetch and return current state.
            return self.get_event(calendar_id, provider_event_id)

        data = self._patch(
            "/calendars/%s/events/%s" % (_enc(calendar_id), _enc(provider_event_id)),
            patch,
        )
        event = self._parse_event(data, calendar_id)
        if event is None:
            raise CalendarError("Google Calendar returned an unparse-able event after update")
        return event

    def delete_event(self, calendar_id: str, provider_event_id: str) -> None:
        self._delete(
            "/calendars/%s/events/%s" % (_enc(calendar_id), _enc(provider_event_id))
        )

    def get_event(self, calendar_id: str, provider_event_id: str) -> CalendarEvent:
        data = self._get(
            "/calendars/%s/events/%s" % (_enc(calendar_id), _enc(provider_event_id))
        )
        event = self._parse_event(data, calendar_id)
        if event is None:
            raise ProviderEventNotFoundError(
                "event %r not found in Google Calendar %r" % (provider_event_id, calendar_id)
            )
        return event

    def get_free_busy(
        self,
        calendar_ids: list[str],
        start: datetime,
        end: datetime,
    ) -> list[BusyWindow]:
        body: dict[str, Any] = {
            "timeMin": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "timeMax": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "items": [{"id": cid} for cid in calendar_ids],
        }
        data = self._post("/freeBusy", body)
        windows: list[BusyWindow] = []
        calendars_data = data.get("calendars", {})
        for cal_id, cal_data in calendars_data.items():
            for slot in cal_data.get("busy", []):
                slot_start, _ = _google_to_dt({"dateTime": slot["start"]})
                slot_end, _ = _google_to_dt({"dateTime": slot["end"]})
                windows.append(BusyWindow(
                    start=slot_start,
                    end=slot_end,
                    provider_name=self.provider_name,
                    calendar_id=cal_id,
                ))
        return windows

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _access_token(self) -> str:
        return self._tokens.get_access_token(
            provider_name=self._config.name,
            provider_type=self._config.provider_type,
            client_id=self._config.client_id,
            client_secret=self._config.client_secret,
            refresh_token=self._config.refresh_token,
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer %s" % self._access_token()}

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
        retry_on_auth: bool = True,
    ) -> dict[str, Any]:
        url = _BASE + path
        try:
            response = httpx.request(
                method,
                url,
                headers=self._headers(),
                params=params,
                json=body,
                timeout=30,
            )
        except httpx.RequestError as exc:
            raise CalendarError(
                "network error calling Google Calendar API: %s" % exc
            ) from exc

        if response.status_code == 401 and retry_on_auth:
            self._tokens.invalidate(self._config.name)
            return self._request(method, path, params=params, body=body, retry_on_auth=False)

        if response.status_code == 404:
            raise ProviderEventNotFoundError(
                "Google Calendar resource not found: %s" % path
            )

        if response.status_code == 204:
            return {}

        if not response.is_success:
            raise CalendarError(
                "Google Calendar API error %d: %s"
                % (response.status_code, response.text[:500])
            )

        try:
            return response.json()
        except Exception as exc:
            raise CalendarError("unparseable response from Google Calendar API") from exc

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body=body)

    def _patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", path, body=body)

    def _delete(self, path: str) -> None:
        self._request("DELETE", path)

    def _parse_event(
        self, data: dict[str, Any], calendar_id: str
    ) -> Optional[CalendarEvent]:
        event_id = data.get("id")
        if not event_id:
            return None
        start_raw = data.get("start") or {}
        end_raw = data.get("end") or {}
        if not start_raw:
            return None
        try:
            start, all_day = _google_to_dt(start_raw)
            end, _ = _google_to_dt(end_raw) if end_raw else (start, all_day)
        except (ValueError, KeyError):
            log.debug("could not parse event dates for Google event %r", event_id)
            return None

        status_map = {"confirmed": "confirmed", "tentative": "tentative", "cancelled": "cancelled"}
        return CalendarEvent(
            provider_event_id=event_id,
            provider_name=self.provider_name,
            calendar_id=calendar_id,
            title=data.get("summary", ""),
            start=start,
            end=end,
            all_day=all_day,
            description=data.get("description", ""),
            location=data.get("location", ""),
            status=status_map.get(data.get("status", "confirmed"), "confirmed"),
            transparency="transparent" if data.get("transparency") == "transparent" else "opaque",
            attendees=_parse_attendees(data.get("attendees", [])),
            html_link=data.get("htmlLink", ""),
        )


def _enc(value: str) -> str:
    """URL-encode a path segment."""
    from urllib.parse import quote
    return quote(value, safe="")
