"""
Microsoft Outlook / Office 365 calendar provider via Microsoft Graph API.

API reference: https://learn.microsoft.com/en-us/graph/api/resources/calendar

Required OAuth2 scope: Calendars.ReadWrite offline_access
Token endpoint: https://login.microsoftonline.com/common/oauth2/v2.0/token

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

_BASE = "https://graph.microsoft.com/v1.0"


def _dt_to_graph(dt: datetime, all_day: bool) -> dict[str, str]:
    """Convert a datetime to a Microsoft Graph dateTimeTimeZone dict."""
    if all_day:
        # Graph uses a separate isAllDay flag; we still need the dateTime field.
        return {
            "dateTime": dt.date().isoformat() + "T00:00:00",
            "timeZone": getattr(dt.tzinfo, "key", "UTC"),
        }
    return {
        "dateTime": dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "timeZone": "UTC",
    }


def _graph_to_dt(value: dict[str, str]) -> datetime:
    """Parse a Microsoft Graph dateTimeTimeZone dict into a UTC datetime."""
    raw = value.get("dateTime", "")
    if "T" not in raw:
        raw = raw + "T00:00:00"
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        from zoneinfo import ZoneInfo
        dt = dt.replace(tzinfo=ZoneInfo(value.get("timeZone") or "UTC"))
    return dt.astimezone(timezone.utc)


def _parse_attendees(raw: list[dict[str, Any]]) -> list[Attendee]:
    out = []
    for item in raw or []:
        ep = item.get("emailAddress") or {}
        email = ep.get("address", "")
        if not email:
            continue
        status_raw = (item.get("status") or {}).get("response", "none")
        status_map = {
            "accepted": "accepted",
            "declined": "declined",
            "tentativelyAccepted": "tentative",
            "none": "needs-action",
            "notResponded": "needs-action",
            "organizer": "accepted",
        }
        out.append(Attendee(
            email=email,
            name=ep.get("name", ""),
            status=status_map.get(status_raw, "needs-action"),
        ))
    return out


class OutlookCalendarProvider:
    """Microsoft Graph (Outlook/Office 365) calendar provider."""

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
        data = self._get("/me/calendars")
        calendars = []
        for item in data.get("value", []):
            calendars.append(Calendar(
                id=item["id"],
                name=item.get("name", item["id"]),
                provider_name=self.provider_name,
                color=item.get("color", ""),
                is_primary=bool(item.get("isDefaultCalendar")),
                is_read_only=not bool(item.get("canEdit", True)),
            ))
        return calendars

    def list_events(
        self,
        calendar_id: str,
        start: datetime,
        end: datetime,
    ) -> list[CalendarEvent]:
        fmt = "%Y-%m-%dT%H:%M:%S"
        start_str = start.astimezone(timezone.utc).strftime(fmt)
        end_str = end.astimezone(timezone.utc).strftime(fmt)

        if calendar_id.lower() == "default":
            path = "/me/calendarView"
        else:
            path = "/me/calendars/%s/calendarView" % _enc(calendar_id)

        params: dict[str, Any] = {
            "startDateTime": start_str,
            "endDateTime": end_str,
            "$top": 999,
            "$select": (
                "id,subject,start,end,isAllDay,body,location,showAs,"
                "sensitivity,attendees,recurrence,webLink,isCancelled,responseStatus"
            ),
        }

        events = []
        url: Optional[str] = None
        while True:
            if url:
                data = self._get_url(url)
            else:
                data = self._get(path, params=params)
            for item in data.get("value", []):
                event = self._parse_event(item, calendar_id)
                if event:
                    events.append(event)
            url = data.get("@odata.nextLink")
            if not url:
                break
        return events

    def create_event(self, request: CreateEventRequest) -> CalendarEvent:
        body: dict[str, Any] = {
            "subject": request.title,
            "start": _dt_to_graph(request.start, request.all_day),
            "end": _dt_to_graph(request.end, request.all_day),
            "isAllDay": request.all_day,
            "body": {"contentType": "text", "content": request.description or ""},
            "location": {"displayName": request.location or ""},
            "showAs": "free" if request.transparency.lower() == "transparent" else "busy",
        }
        if request.attendees:
            body["attendees"] = [
                {
                    "emailAddress": {"address": e},
                    "type": "required",
                }
                for e in request.attendees
            ]
        if request.alert_minutes is not None and request.alert_minutes > 0:
            body["reminderMinutesBeforeStart"] = request.alert_minutes
            body["isReminderOn"] = True
        else:
            body["isReminderOn"] = False

        if request.calendar_id.lower() == "default":
            path = "/me/events"
        else:
            path = "/me/calendars/%s/events" % _enc(request.calendar_id)

        data = self._post(path, body)
        event = self._parse_event(data, request.calendar_id)
        if event is None:
            raise CalendarError("Outlook returned an unparse-able event after create")
        return event

    def update_event(
        self,
        calendar_id: str,
        provider_event_id: str,
        request: UpdateEventRequest,
        existing: CalendarEvent,
    ) -> CalendarEvent:
        patch: dict[str, Any] = {}
        if request.title is not None:
            patch["subject"] = request.title
        if request.description is not None:
            patch["body"] = {"contentType": "text", "content": request.description}
        if request.location is not None:
            patch["location"] = {"displayName": request.location}
        if request.transparency is not None:
            patch["showAs"] = (
                "free" if request.transparency.lower() == "transparent" else "busy"
            )

        next_all_day = request.all_day if request.all_day is not None else existing.all_day
        next_start = request.start if request.start is not None else existing.start
        next_end = request.end if request.end is not None else existing.end
        if request.start is not None or request.end is not None or request.all_day is not None:
            patch["start"] = _dt_to_graph(next_start, next_all_day)
            patch["end"] = _dt_to_graph(next_end, next_all_day)
            patch["isAllDay"] = next_all_day

        if request.attendees is not None:
            patch["attendees"] = [
                {"emailAddress": {"address": e}, "type": "required"}
                for e in request.attendees
            ]

        if request.alert_minutes is not None:
            if request.alert_minutes > 0:
                patch["reminderMinutesBeforeStart"] = request.alert_minutes
                patch["isReminderOn"] = True
            else:
                patch["isReminderOn"] = False

        if not patch:
            return self.get_event(calendar_id, provider_event_id)

        data = self._patch(
            "/me/events/%s" % _enc(provider_event_id),
            patch,
        )
        event = self._parse_event(data, calendar_id)
        if event is None:
            raise CalendarError("Outlook returned an unparse-able event after update")
        return event

    def delete_event(self, calendar_id: str, provider_event_id: str) -> None:
        self._delete("/me/events/%s" % _enc(provider_event_id))

    def get_event(self, calendar_id: str, provider_event_id: str) -> CalendarEvent:
        data = self._get("/me/events/%s" % _enc(provider_event_id))
        event = self._parse_event(data, calendar_id)
        if event is None:
            raise ProviderEventNotFoundError(
                "event %r not found in Outlook calendar" % provider_event_id
            )
        return event

    def get_free_busy(
        self,
        calendar_ids: list[str],
        start: datetime,
        end: datetime,
    ) -> list[BusyWindow]:
        # Graph /me/calendarView is simpler to query than the schedules endpoint
        # for single-user scenarios.  For full fidelity, use the schedules API.
        fmt = "%Y-%m-%dT%H:%M:%S"
        params: dict[str, Any] = {
            "startDateTime": start.astimezone(timezone.utc).strftime(fmt),
            "endDateTime": end.astimezone(timezone.utc).strftime(fmt),
            "$top": 999,
            "$select": "start,end,isAllDay,showAs,isCancelled",
        }
        data = self._get("/me/calendarView", params=params)
        windows: list[BusyWindow] = []
        for item in data.get("value", []):
            if item.get("isCancelled"):
                continue
            show_as = item.get("showAs", "busy")
            if show_as in ("free", "workingElsewhere", "unknown"):
                continue
            try:
                slot_start = _graph_to_dt(item.get("start", {}))
                slot_end = _graph_to_dt(item.get("end", {}))
            except (ValueError, KeyError):
                continue
            windows.append(BusyWindow(
                start=slot_start,
                end=slot_end,
                provider_name=self.provider_name,
                calendar_id=calendar_ids[0] if calendar_ids else "default",
                all_day=bool(item.get("isAllDay")),
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
            tenant_id=self._config.extra.get("tenant_id", ""),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer %s" % self._access_token(),
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        url: str,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
        retry_on_auth: bool = True,
    ) -> dict[str, Any]:
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
                "network error calling Microsoft Graph API: %s" % exc
            ) from exc

        if response.status_code == 401 and retry_on_auth:
            self._tokens.invalidate(self._config.name)
            return self._request(method, url, params=params, body=body, retry_on_auth=False)

        if response.status_code == 404:
            raise ProviderEventNotFoundError(
                "Microsoft Graph resource not found: %s" % url
            )

        if response.status_code == 204:
            return {}

        if not response.is_success:
            raise CalendarError(
                "Microsoft Graph API error %d: %s"
                % (response.status_code, response.text[:500])
            )

        try:
            return response.json()
        except Exception as exc:
            raise CalendarError("unparseable response from Microsoft Graph API") from exc

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self._request("GET", _BASE + path, params=params)

    def _get_url(self, url: str) -> dict[str, Any]:
        """Follow a nextLink URL (already absolute)."""
        return self._request("GET", url)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", _BASE + path, body=body)

    def _patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", _BASE + path, body=body)

    def _delete(self, path: str) -> None:
        self._request("DELETE", _BASE + path)

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
            start = _graph_to_dt(start_raw)
            end = _graph_to_dt(end_raw) if end_raw else start
        except (ValueError, KeyError):
            log.debug("could not parse event dates for Outlook event %r", event_id)
            return None

        all_day = bool(data.get("isAllDay", False))
        show_as = data.get("showAs", "busy")
        transparency = "transparent" if show_as == "free" else "opaque"
        cancelled = bool(data.get("isCancelled", False))
        status = "cancelled" if cancelled else "confirmed"
        body_content = (data.get("body") or {}).get("content", "")

        return CalendarEvent(
            provider_event_id=event_id,
            provider_name=self.provider_name,
            calendar_id=calendar_id,
            title=data.get("subject", ""),
            start=start,
            end=end,
            all_day=all_day,
            description=body_content,
            location=(data.get("location") or {}).get("displayName", ""),
            status=status,
            transparency=transparency,
            attendees=_parse_attendees(data.get("attendees", [])),
            html_link=data.get("webLink", ""),
        )


def _enc(value: str) -> str:
    """URL-encode a path segment."""
    from urllib.parse import quote
    return quote(value, safe="")
