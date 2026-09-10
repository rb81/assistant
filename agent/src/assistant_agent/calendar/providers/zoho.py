"""
Zoho Calendar provider.

API reference: https://www.zoho.com/calendar/help/api/

Required OAuth2 scopes: ZohoCalendar.event.ALL ZohoCalendar.calendar.READ
Token endpoint: https://accounts.zoho.com/oauth/v2/token

Zoho Calendar uses a regional API base URL.  The default is .com but users
in the EU/IN/AU/JP data centres must set CALENDAR_ZOHO_API_BASE to the
matching regional URL (e.g. https://calendar.zoho.eu/api/v1).

To obtain a refresh token, follow the setup guide in docs/calendar-providers.md.
"""

import logging
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

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

_DEFAULT_BASE = "https://calendar.zoho.com/api/v1"

# Zoho epoch: dates are represented as milliseconds since Unix epoch in some
# endpoints; ISO 8601 is used in event create/update bodies.
_ZOHO_DT_FMT = "%Y%m%dT%H%M%SZ"


def _dt_to_zoho(dt: datetime, all_day: bool) -> str:
    """Format a datetime as Zoho's yyyyMMdd'T'HHmmss'Z' string."""
    if all_day:
        return dt.astimezone(timezone.utc).strftime("%Y%m%d")
    return dt.astimezone(timezone.utc).strftime(_ZOHO_DT_FMT)


def _zoho_to_dt(value: str) -> tuple[datetime, bool]:
    """Parse a Zoho datetime string into (datetime, all_day).

    Zoho returns timed values as compact ISO without colons, with either a
    trailing Z or a numeric offset, for example:
      20260910T090000Z
      20260910T090000+0400
      20260910T090000+04:00
    All-day values are YYYYMMDD.
    """
    value = str(value or "").strip()
    if not value:
        raise ValueError("empty Zoho datetime string")
    # All-day format: YYYYMMDD (8 chars, no T)
    if len(value) == 8 and "T" not in value:
        from datetime import time as dtime
        d = datetime.strptime(value, "%Y%m%d").date()
        return datetime.combine(d, dtime.min, tzinfo=timezone.utc), True
    if len(value) < 15:
        raise ValueError("invalid Zoho datetime string")
    naive = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    suffix = value[15:].strip()
    if not suffix or suffix == "Z":
        return naive.replace(tzinfo=timezone.utc), False
    sign_char = suffix[0]
    if sign_char not in "+-":
        raise ValueError("invalid Zoho datetime offset")
    digits = suffix[1:].replace(":", "")
    if len(digits) < 4 or not digits[:4].isdigit():
        raise ValueError("invalid Zoho datetime offset")
    sign = 1 if sign_char == "+" else -1
    tz = timezone(timedelta(hours=sign * int(digits[:2]), minutes=sign * int(digits[2:4])))
    return naive.replace(tzinfo=tz).astimezone(timezone.utc), False


def _parse_attendees(raw: list[dict[str, Any]]) -> list[Attendee]:
    out = []
    for item in raw or []:
        email = item.get("email", "")
        if not email:
            continue
        status_raw = str(item.get("status", "")).lower()
        status_map = {
            "accepted": "accepted",
            "declined": "declined",
            "tentative": "tentative",
            "tentatively accepted": "tentative",
        }
        out.append(Attendee(
            email=email,
            name=item.get("name", ""),
            status=status_map.get(status_raw, "needs-action"),
        ))
    return out


class ZohoCalendarProvider:
    """Zoho Calendar API provider."""

    def __init__(self, config: ProviderConfig, token_store: TokenStore) -> None:
        self._config = config
        self._tokens = token_store
        self._base = str(config.extra.get("api_base") or _DEFAULT_BASE).rstrip("/")
        self._calendar_alias_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # CalendarProvider protocol
    # ------------------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return self._config.name

    def list_calendars(self) -> list[Calendar]:
        data = self._get("/calendars")
        calendars = []
        for item in (data.get("calendars") or []):
            calendars.append(Calendar(
                id=item.get("uid", item.get("id", "")),
                name=item.get("calname", item.get("name", "")),
                provider_name=self.provider_name,
                description=item.get("description", ""),
                color=item.get("color", ""),
                is_primary=bool(item.get("isdefault", False)),
                is_read_only=not bool(item.get("editable", True)),
            ))
        return calendars

    def list_events(
        self,
        calendar_id: str,
        start: datetime,
        end: datetime,
    ) -> list[CalendarEvent]:
        resolved_calendar_id = self._resolved_calendar_id(calendar_id)
        params: dict[str, Any] = {
            "range": json.dumps(
                {
                    "start": _dt_to_zoho(start, False),
                    "end": _dt_to_zoho(end, False),
                },
                separators=(",", ":"),
            ),
        }
        data = self._get("/calendars/%s/events" % _enc(resolved_calendar_id), params=params)
        events = []
        for item in (data.get("events") or []):
            event = self._parse_event(item, resolved_calendar_id)
            if event:
                events.append(event)
        return events

    def create_event(self, request: CreateEventRequest) -> CalendarEvent:
        resolved_calendar_id = self._resolved_calendar_id(request.calendar_id)
        event_data: dict[str, Any] = {
            "title": request.title,
            "dateandtime": {
                "start": _dt_to_zoho(request.start, request.all_day),
                "end": _dt_to_zoho(request.end, request.all_day),
                "timezone": "UTC",
            },
            "isprivate": False,
        }
        if request.description:
            event_data["description"] = request.description
        if request.location:
            event_data["location"] = request.location
        if request.transparency.lower() == "transparent":
            event_data["showasfree"] = True
        if request.attendees:
            event_data["attendees"] = [{"email": e} for e in request.attendees]
        if request.alert_minutes is not None and request.alert_minutes > 0:
            event_data["reminders"] = [
                {"action": "popup", "minutes": str(request.alert_minutes)}
            ]

        params = {"eventdata": json.dumps(event_data, separators=(",", ":"))}
        data = self._post("/calendars/%s/events" % _enc(resolved_calendar_id), params=params)
        created = (data.get("events") or [{}])[0]
        event = self._parse_event(created, resolved_calendar_id)
        if event is None:
            raise CalendarError("Zoho Calendar returned an unparse-able event after create")
        return event

    def update_event(
        self,
        calendar_id: str,
        provider_event_id: str,
        request: UpdateEventRequest,
        existing: CalendarEvent,
    ) -> CalendarEvent:
        resolved_calendar_id = self._resolved_calendar_id(calendar_id)
        
        # Fetch current event to get the required etag
        current = self.get_event(calendar_id, provider_event_id)
        current_data = self._get(
            "/calendars/%s/events/%s" % (_enc(resolved_calendar_id), _enc(provider_event_id))
        )
        current_etag = (current_data.get("events") or [{}])[0].get("etag")
        if not current_etag:
            raise CalendarError("Zoho Calendar event missing required etag for update")
        
        event_data: dict[str, Any] = {}

        if request.title is not None:
            event_data["title"] = request.title
        if request.description is not None:
            event_data["description"] = request.description
        if request.location is not None:
            event_data["location"] = request.location
        if request.transparency is not None:
            event_data["showasfree"] = request.transparency.lower() == "transparent"

        next_all_day = request.all_day if request.all_day is not None else existing.all_day
        next_start = request.start if request.start is not None else existing.start
        next_end = request.end if request.end is not None else existing.end
        
        # Zoho requires dateandtime for all updates
        event_data["dateandtime"] = {
            "start": _dt_to_zoho(next_start, next_all_day),
            "end": _dt_to_zoho(next_end, next_all_day),
            "timezone": "UTC",
        }
        
        # Zoho requires etag for all updates
        event_data["etag"] = current_etag

        if request.attendees is not None:
            event_data["attendees"] = [{"email": e} for e in request.attendees]

        if request.alert_minutes is not None:
            if request.alert_minutes > 0:
                event_data["reminders"] = [
                    {"action": "popup", "minutes": str(request.alert_minutes)}
                ]
            else:
                event_data["reminders"] = []

        params = {"eventdata": json.dumps(event_data, separators=(",", ":"))}
        data = self._put(
            "/calendars/%s/events/%s" % (_enc(resolved_calendar_id), _enc(provider_event_id)),
            params=params,
        )
        updated = (data.get("events") or [{}])[0]
        event = self._parse_event(updated, resolved_calendar_id)
        if event is None:
            raise CalendarError("Zoho Calendar returned an unparse-able event after update")
        return event

    def delete_event(self, calendar_id: str, provider_event_id: str) -> None:
        resolved_calendar_id = self._resolved_calendar_id(calendar_id)
        
        # Fetch current event to get the required etag and uid
        current_data = self._get(
            "/calendars/%s/events/%s" % (_enc(resolved_calendar_id), _enc(provider_event_id))
        )
        current_event = (current_data.get("events") or [{}])[0]
        current_etag = current_event.get("etag")
        current_uid = current_event.get("uid") or current_event.get("id")
        
        if not current_etag or not current_uid:
            raise CalendarError("Zoho Calendar event missing required etag or uid for delete")
        
        # Zoho requires uid and etag in eventdata for delete
        event_data = {
            "uid": current_uid,
            "etag": current_etag,
        }
        params = {"eventdata": json.dumps(event_data, separators=(",", ":"))}
        
        self._request("DELETE", 
            "/calendars/%s/events/%s" % (_enc(resolved_calendar_id), _enc(provider_event_id)),
            params=params
        )

    def get_event(self, calendar_id: str, provider_event_id: str) -> CalendarEvent:
        resolved_calendar_id = self._resolved_calendar_id(calendar_id)
        data = self._get(
            "/calendars/%s/events/%s" % (_enc(resolved_calendar_id), _enc(provider_event_id))
        )
        items = data.get("events") or []
        if not items:
            raise ProviderEventNotFoundError(
                "event %r not found in Zoho Calendar %r" % (provider_event_id, resolved_calendar_id)
            )
        event = self._parse_event(items[0], resolved_calendar_id)
        if event is None:
            raise ProviderEventNotFoundError(
                "event %r not found in Zoho Calendar %r" % (provider_event_id, resolved_calendar_id)
            )
        return event

    def get_free_busy(
        self,
        calendar_ids: list[str],
        start: datetime,
        end: datetime,
    ) -> list[BusyWindow]:
        # Zoho does not offer a dedicated free/busy endpoint; we fall back to
        # listing events from each calendar and treating occupied time as busy.
        windows: list[BusyWindow] = []
        for cal_id in calendar_ids:
            try:
                events = self.list_events(cal_id, start, end)
            except CalendarError:
                continue
            for event in events:
                if not event.blocks_time():
                    continue
                windows.append(BusyWindow(
                    start=event.start,
                    end=event.end,
                    provider_name=self.provider_name,
                    calendar_id=cal_id,
                    all_day=event.all_day,
                    provider_event_id=event.provider_event_id,
                    assistant_id=event.assistant_id,
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
        return {
            "Authorization": "Zoho-oauthtoken %s" % self._access_token(),
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
        retry_on_auth: bool = True,
    ) -> dict[str, Any]:
        url = self._base + path
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
                "network error calling Zoho Calendar API: %s" % exc
            ) from exc

        # Check for scope errors first - retrying won't fix them
        if response.status_code == 401 and "INVALID_OAUTHSCOPE" in response.text.upper():
            raise ProviderAuthError(
                "Zoho Calendar OAuth token is missing required event scope for this operation "
                "(INVALID_OAUTHSCOPE). Re-authorize with scopes including "
                "ZohoCalendar.event.ALL and ZohoCalendar.calendar.READ, then update the refresh token."
            )

        if response.status_code == 401 and retry_on_auth:
            self._tokens.invalidate(self._config.name)
            return self._request(method, path, params=params, body=body, retry_on_auth=False)

        if response.status_code == 404:
            raise ProviderEventNotFoundError(
                "Zoho Calendar resource not found: %s" % path
            )

        if response.status_code == 204:
            return {}

        if not response.is_success:
            raise CalendarError(
                "Zoho Calendar API error %d: %s"
                % (response.status_code, response.text[:500])
            )

        try:
            return response.json()
        except Exception as exc:
            raise CalendarError("unparseable response from Zoho Calendar API") from exc

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self._request("GET", path, params=params)

    def _post(
        self,
        path: str,
        body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return self._request("POST", path, params=params, body=body)

    def _put(
        self,
        path: str,
        body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return self._request("PUT", path, params=params, body=body)

    def _delete(self, path: str) -> None:
        self._request("DELETE", path)

    def _resolved_calendar_id(self, calendar_id: str) -> str:
        clean = str(calendar_id or "").strip()
        if not clean:
            return clean
        alias = clean.lower()
        if alias not in ("default", "primary"):
            return clean
        cached = self._calendar_alias_cache.get(alias)
        if cached:
            return cached
        data = self._get("/calendars")
        calendars = data.get("calendars") or []
        chosen = ""
        for item in calendars:
            uid = str(item.get("uid") or item.get("id") or "").strip()
            if not uid:
                continue
            if bool(item.get("isdefault", False)):
                chosen = uid
                break
            if not chosen:
                chosen = uid
        if chosen:
            self._calendar_alias_cache[alias] = chosen
            if alias == "default":
                self._calendar_alias_cache.setdefault("primary", chosen)
            if alias == "primary":
                self._calendar_alias_cache.setdefault("default", chosen)
            return chosen
        return clean

    def _parse_event(
        self, data: dict[str, Any], calendar_id: str
    ) -> Optional[CalendarEvent]:
        event_id = data.get("uid") or data.get("id")
        if not event_id:
            return None

        dt_block = data.get("dateandtime") or {}
        start_raw = dt_block.get("start") or data.get("startdatetime", "")
        end_raw = dt_block.get("end") or data.get("enddatetime", "")

        if not start_raw:
            return None
        try:
            start, all_day = _zoho_to_dt(start_raw)
            end, _ = _zoho_to_dt(end_raw) if end_raw else (start, all_day)
        except (ValueError, KeyError):
            log.debug("could not parse event dates for Zoho event %r", event_id)
            return None

        show_as_free = bool(data.get("showasfree", False))
        transparency = "transparent" if show_as_free else "opaque"
        cancelled = str(data.get("status", "")).lower() == "cancelled"
        status = "cancelled" if cancelled else "confirmed"

        return CalendarEvent(
            provider_event_id=str(event_id),
            provider_name=self.provider_name,
            calendar_id=calendar_id,
            title=data.get("title", ""),
            start=start,
            end=end,
            all_day=all_day,
            description=data.get("description", ""),
            location=data.get("location", ""),
            status=status,
            transparency=transparency,
            attendees=_parse_attendees(data.get("attendees", [])),
        )


def _enc(value: str) -> str:
    """URL-encode a path segment."""
    return quote(value, safe="")
