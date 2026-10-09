"""
CalendarRouter — the single bridge between agent tools and calendar providers.

The router:
  1. Loads all configured providers from env vars at construction time.
  2. Routes create/update/delete to the correct provider.
  3. Aggregates list/busy queries across all providers and merges results.
  4. Tracks agent-managed events in the database (provider_name + provider_event_id).
  5. Audits all write operations.
  6. Exposes a sync() method for compatibility (health check / no-op for API providers).

Agent tools import CalendarRouter and call its methods directly.
The tools layer never imports individual provider classes.
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg.types.json import Jsonb

from ..config import AppConfig
from ..database import json_safe
from ..time_utils import local_datetime_iso, parse_datetime
from ..secret_store import read_secret
from .models import (
    BusyWindow,
    Calendar,
    CalendarEvent,
    CreateEventRequest,
    ProviderConfig,
    UpdateEventRequest,
)
from .provider import CalendarError, CalendarProvider, ProviderAuthError, ProviderEventNotFoundError
from .providers import build_provider
from .token_store import TokenStore

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider config loader
# ---------------------------------------------------------------------------

def _load_provider_configs(config: AppConfig) -> list[ProviderConfig]:
    """
    Discover provider configurations from environment variables and config.

    Pattern per provider type:
      CALENDAR_GOOGLE_CLIENT_ID / _SECRET / _REFRESH_TOKEN (env only, credentials)
      CALENDAR_GOOGLE_CALENDAR_ID (env or agent.calendar.google.calendar_id)
      
      CALENDAR_OUTLOOK_CLIENT_ID / _SECRET / _REFRESH_TOKEN (env only)
      CALENDAR_OUTLOOK_CALENDAR_ID (env or agent.calendar.outlook.calendar_id)
      CALENDAR_OUTLOOK_TENANT_ID (env or agent.calendar.outlook.tenant_id)
      
      CALENDAR_ZOHO_CLIENT_ID / _SECRET / _REFRESH_TOKEN (env only)
      CALENDAR_ZOHO_CALENDAR_ID (env or agent.calendar.zoho.calendar_id)
      CALENDAR_ZOHO_API_BASE (env or agent.calendar.zoho.api_base)

    Multiple instances of the same provider type can be configured by adding
    a numeric suffix (env-only for numbered instances):
      CALENDAR_GOOGLE_2_CLIENT_ID / ...   → second Google provider
      CALENDAR_GOOGLE_3_CLIENT_ID / ...   → third Google provider

    The logical name defaults to the provider type (e.g. "google"), or
    "google_2" / "google_3" for extra instances.
    """
    configs: list[ProviderConfig] = []
    for ptype in ("google", "outlook", "zoho"):
        prefix = "CALENDAR_%s" % ptype.upper()
        client_id = (read_secret("%s_CLIENT_ID" % prefix) or "").strip()
        client_secret = (read_secret("%s_CLIENT_SECRET" % prefix) or "").strip()
        refresh_token = (read_secret("%s_REFRESH_TOKEN" % prefix) or "").strip()
        if client_id and client_secret and refresh_token:
            # For the primary provider instance, read non-credential settings from config
            # (which includes env overrides via config.py mappings)
            calendar_id = str(config.get("agent.calendar.%s.calendar_id" % ptype) or "").strip() or _default_calendar_id(ptype)
            extra: dict[str, Any] = {}
            if ptype == "zoho":
                api_base = str(config.get("agent.calendar.zoho.api_base") or "").strip()
                if api_base:
                    extra["api_base"] = api_base
            if ptype == "outlook":
                tenant_id = str(config.get("agent.calendar.outlook.tenant_id") or "").strip()
                if tenant_id:
                    extra["tenant_id"] = tenant_id
            configs.append(ProviderConfig(
                name=ptype,
                provider_type=ptype,
                calendar_id=calendar_id,
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=refresh_token,
                extra=extra,
            ))

        # Support CALENDAR_GOOGLE_2_*, CALENDAR_GOOGLE_3_*, etc.
        # For numbered instances, read all settings from env (no YAML path for dynamic suffixes)
        for suffix in range(2, 10):
            pfx2 = "CALENDAR_%s_%d" % (ptype.upper(), suffix)
            cid2 = (read_secret("%s_CLIENT_ID" % pfx2) or "").strip()
            cs2 = (read_secret("%s_CLIENT_SECRET" % pfx2) or "").strip()
            rt2 = (read_secret("%s_REFRESH_TOKEN" % pfx2) or "").strip()
            if cid2 and cs2 and rt2:
                cal_id2 = os.environ.get("%s_CALENDAR_ID" % pfx2, "").strip() or _default_calendar_id(ptype)
                extra2: dict[str, Any] = {}
                if ptype == "zoho":
                    api_base2 = os.environ.get("%s_API_BASE" % pfx2, "").strip()
                    if api_base2:
                        extra2["api_base"] = api_base2
                if ptype == "outlook":
                    tenant_id2 = os.environ.get("%s_TENANT_ID" % pfx2, "").strip()
                    if tenant_id2:
                        extra2["tenant_id"] = tenant_id2
                configs.append(ProviderConfig(
                    name="%s_%d" % (ptype, suffix),
                    provider_type=ptype,
                    calendar_id=cal_id2,
                    client_id=cid2,
                    client_secret=cs2,
                    refresh_token=rt2,
                    extra=extra2,
                ))
    return configs


def _default_calendar_id(provider_type: str) -> str:
    return {
        "google": "primary",
        "outlook": "default",
        "zoho": "default",
    }.get(provider_type, "default")


# ---------------------------------------------------------------------------
# CalendarRouter
# ---------------------------------------------------------------------------

class CalendarRouter:
    """
    Single entry point for all calendar operations.

    Instantiated once per tool call (like CalendarGateway was), sharing the
    global token store cached in memory.
    """

    # Class-level token store shared across all instances in this process.
    _token_store: Optional[TokenStore] = None

    def __init__(self, db: Any, config: AppConfig, job: dict[str, Any]) -> None:
        self._db = db
        self._config = config
        self._job = job

        # Lazy-init the token store (shared, not per-request).
        if CalendarRouter._token_store is None:
            CalendarRouter._token_store = TokenStore(db)

        self._providers: dict[str, CalendarProvider] = {}
        self._provider_configs: dict[str, ProviderConfig] = {}
        for pc in _load_provider_configs(self._config):
            pc.extra["assistant_timezone"] = self._timezone_name()
            self._provider_configs[pc.name] = pc
            try:
                provider = build_provider(pc, CalendarRouter._token_store)
                self._providers[pc.name] = provider
                log.debug("calendar provider %r (%s) registered", pc.name, pc.provider_type)
            except CalendarError as exc:
                log.warning("could not register calendar provider %r: %s", pc.name, exc)

        self._default_provider_name = (
            os.environ.get("CALENDAR_DEFAULT_PROVIDER", "").strip()
            or self._config.get("agent.calendar.default_provider", "")
            or (next(iter(self._providers), None) or "")
        )
        self._allow_details = self._config.get_bool(
            "agent.calendar.policy.allow_read_event_details", False
        )
        self._default_alert_minutes = max(
            self._config.get_int("agent.calendar.default_alert_minutes", 30), 0
        )

    # ------------------------------------------------------------------
    # Public API (called directly by tools)
    # ------------------------------------------------------------------

    def sync(self, reason: str = "manual") -> dict[str, Any]:
        """
        Health check for all configured providers.

        API-based providers don't require an explicit sync step.  This method
        verifies token availability for each provider and reports status.
        """
        if not self._providers:
            return {
                "status": "no_providers",
                "reason": "no calendar providers are configured",
                "providers": [],
            }
        results = []
        for name, provider in self._providers.items():
            try:
                provider.list_calendars()
                results.append({"provider": name, "status": "ok"})
            except ProviderAuthError as exc:
                results.append({"provider": name, "status": "auth_error", "detail": str(exc)})
            except CalendarError as exc:
                results.append({"provider": name, "status": "error", "detail": "Calendar access failed; verify the API endpoint and provider connectivity."})
        overall = "ok" if all(r["status"] == "ok" for r in results) else "degraded"
        return {"status": overall, "reason": reason, "providers": results}

    def list_busy(
        self,
        start: str,
        end: str,
        include_details: bool = False,
    ) -> dict[str, Any]:
        range_start, range_end = self._range(start, end)
        allow_details = self._allow_details and bool(include_details)
        managed_ids = self._managed_assistant_ids()
        managed_by_provider_id = self._managed_by_provider_id()
        busy: list[dict[str, Any]] = []
        provider_errors: list[dict[str, str]] = []

        for name, provider in self._providers.items():
            pc = self._provider_config(name)
            if pc is None:
                provider_errors.append({"provider": name, "status": "error", "detail": "Provider configuration is unavailable."})
                continue
            try:
                windows = provider.get_free_busy(
                    calendar_ids=[pc.calendar_id],
                    start=range_start,
                    end=range_end,
                )
            except CalendarError as exc:
                status = "auth_error" if isinstance(exc, ProviderAuthError) else "error"
                detail = "Authentication failed. Verify calendar OAuth credentials and consent scopes." if status == "auth_error" else "Calendar access failed; availability is unknown."
                provider_errors.append({"provider": name, "status": status, "detail": detail})
                log.warning("list_busy: provider %r returned %s", name, status)
                continue
            for w in windows:
                assistant_id = w.assistant_id
                if not assistant_id and w.provider_event_id:
                    assistant_id = managed_by_provider_id.get(
                        "%s:%s" % (w.provider_name or name, w.provider_event_id)
                    )
                is_managed = bool(assistant_id) and assistant_id in managed_ids

                item: dict[str, Any] = {
                    "start": local_datetime_iso(w.start, self._config),
                    "end": local_datetime_iso(w.end, self._config),
                    "provider": w.provider_name,
                    "calendar_id": w.calendar_id,
                    "all_day": w.all_day,
                    "managed": is_managed,
                    "event_id": assistant_id,
                }
                if allow_details or is_managed:
                    item["title"] = w.title
                else:
                    item["title"] = "Busy"
                busy.append(item)

        busy.sort(key=lambda x: (x["start"], x["end"], x["provider"]))
        return {
            "start": local_datetime_iso(range_start, self._config),
            "end": local_datetime_iso(range_end, self._config),
            "busy": busy,
            "providers": list(self._providers.keys()),
            "status": "no_providers" if not self._providers else "degraded" if provider_errors else "ok",
            "complete": bool(self._providers) and not provider_errors,
            "provider_errors": provider_errors,
        }

    def list_events(
        self,
        start: str,
        end: str,
        managed_only: bool = False,
    ) -> dict[str, Any]:
        range_start, range_end = self._range(start, end)
        allow_details = self._allow_details
        managed_by_provider_id = self._managed_by_provider_id()
        events: list[dict[str, Any]] = []

        for name, provider in self._providers.items():
            pc = self._provider_config(name)
            if pc is None:
                continue
            try:
                raw_events = provider.list_events(
                    calendar_id=pc.calendar_id,
                    start=range_start,
                    end=range_end,
                )
            except CalendarError as exc:
                log.warning("list_events: provider %r error: %s", name, exc)
                continue
            for ev in raw_events:
                lookup_key = "%s:%s" % (name, ev.provider_event_id)
                assistant_id = managed_by_provider_id.get(lookup_key)
                is_managed = bool(assistant_id)
                if managed_only and not is_managed:
                    continue
                visible = allow_details or is_managed
                item: dict[str, Any] = {
                    "event_id": assistant_id,
                    "provider": ev.provider_name,
                    "calendar_id": ev.calendar_id,
                    "provider_event_id": ev.provider_event_id,
                    "start": local_datetime_iso(ev.start, self._config),
                    "end": local_datetime_iso(ev.end, self._config),
                    "all_day": ev.all_day,
                    "managed": is_managed,
                    "status": ev.status,
                    "transparency": ev.transparency,
                    "title": ev.title if visible else "Busy",
                }
                if visible:
                    item.update({
                        "description": ev.description,
                        "location": ev.location,
                        "attendees": [
                            {"email": a.email, "name": a.name, "status": a.status}
                            for a in ev.attendees
                        ],
                        "html_link": ev.html_link,
                    })
                events.append(item)

        events.sort(key=lambda x: (x["start"], x["end"], x["provider"]))
        return {
            "start": local_datetime_iso(range_start, self._config),
            "end": local_datetime_iso(range_end, self._config),
            "events": events,
            "providers": list(self._providers.keys()),
        }

    def create_event(
        self,
        title: str,
        start: str,
        end: str,
        provider: Optional[str] = None,
        description: str = "",
        location: str = "",
        all_day: bool = False,
        transparency: str = "opaque",
        attendees: Optional[list[str]] = None,
        alert_minutes: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        clean_title = str(title or "").strip()
        if not clean_title:
            raise CalendarError("event title is required")

        clean_meta = metadata if isinstance(metadata, dict) else {}
        clean_ikey = str(idempotency_key or clean_meta.get("idempotency_key") or "").strip()
        if clean_ikey:
            existing = self._managed_by_idempotency_key(clean_ikey)
            if existing is not None:
                return {"event": existing, "idempotent_reuse": True, "idempotency_key": clean_ikey}

        target_provider = provider or self._default_provider_name
        prov = self._get_provider(target_provider)
        pc = self._provider_config(target_provider)
        if pc is None:
            raise CalendarError("provider config not found for %r" % target_provider)

        event_start, event_end = self._event_range(start, end, all_day)
        alert_mins = alert_minutes if alert_minutes is not None else self._default_alert_minutes
        clean_attendees = self._clean_attendees(attendees)

        request = CreateEventRequest(
            title=clean_title,
            start=event_start,
            end=event_end,
            calendar_id=pc.calendar_id,
            all_day=bool(all_day),
            description=description,
            location=location,
            transparency=transparency,
            attendees=clean_attendees,
            alert_minutes=alert_mins,
        )
        created_event = prov.create_event(request)

        assistant_id = str(uuid.uuid4())
        db_meta = dict(clean_meta)
        if clean_ikey:
            db_meta["idempotency_key"] = clean_ikey
        row = self._insert_managed_event(
            assistant_id=assistant_id,
            provider_name=target_provider,
            provider_event_id=created_event.provider_event_id,
            calendar_id=pc.calendar_id,
            summary=clean_title,
            starts_at=event_start,
            ends_at=event_end,
            metadata=db_meta,
        )
        self._audit(assistant_id, "created", {"event": self._public_row(row)})
        return {"event": self._public_row(row)}

    def update_event(
        self,
        event_id: str,
        title: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        all_day: Optional[bool] = None,
        transparency: Optional[str] = None,
        attendees: Optional[list[str]] = None,
        alert_minutes: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        clean_id = str(event_id or "").strip()
        row = self._managed_row(clean_id)
        if row is None:
            raise CalendarError("managed calendar event not found")

        provider_name = str(row["provider_name"])
        provider_event_id = str(row["provider_event_id"])
        calendar_id = str(row["calendar_id"])

        prov = self._get_provider(provider_name)
        existing = prov.get_event(calendar_id, provider_event_id)

        # Resolve new timestamps.
        next_all_day = existing.all_day if all_day is None else bool(all_day)
        current_duration = existing.end - existing.start
        if start is not None and end is not None:
            next_start, next_end = self._event_range(start, end, next_all_day)
        elif start is not None:
            next_start = self._event_start(start, next_all_day)
            next_end = next_start + current_duration
        elif end is not None:
            next_start = existing.start
            next_end = self._event_end(end, next_all_day)
        else:
            next_start, next_end = None, None

        if next_start is not None and next_end is not None and next_end <= next_start:
            raise CalendarError("event end must be after start")
        if title is not None and not str(title).strip():
            raise CalendarError("event title cannot be empty")

        request = UpdateEventRequest(
            title=str(title).strip() if title is not None else None,
            start=next_start,
            end=next_end,
            all_day=next_all_day if all_day is not None else None,
            description=description,
            location=location,
            transparency=transparency,
            attendees=self._clean_attendees(attendees) if attendees is not None else None,
            alert_minutes=alert_minutes,
        )
        updated_event = prov.update_event(
            calendar_id=calendar_id,
            provider_event_id=provider_event_id,
            request=request,
            existing=existing,
        )

        next_meta = metadata if isinstance(metadata, dict) else (row.get("metadata") or {})
        next_summary = request.title if request.title is not None else str(row.get("summary", ""))
        updated_row = self._update_managed_event(
            assistant_id=clean_id,
            summary=next_summary,
            starts_at=updated_event.start,
            ends_at=updated_event.end,
            metadata=next_meta,
        )
        self._audit(clean_id, "updated", {"event": self._public_row(updated_row)})
        return {"event": self._public_row(updated_row)}

    def delete_event(self, event_id: str) -> dict[str, Any]:
        clean_id = str(event_id or "").strip()
        row = self._managed_row(clean_id)
        if row is None:
            raise CalendarError("managed calendar event not found")

        provider_name = str(row["provider_name"])
        provider_event_id = str(row["provider_event_id"])
        calendar_id = str(row["calendar_id"])

        prov = self._get_provider(provider_name)
        try:
            prov.delete_event(calendar_id, provider_event_id)
        except ProviderEventNotFoundError:
            # Already gone on the provider — mark deleted in our DB anyway.
            log.warning(
                "delete_event: event %r not found on provider %r; marking deleted locally",
                provider_event_id,
                provider_name,
            )

        updated_row = self._mark_deleted(clean_id)
        self._audit(clean_id, "deleted", {"provider_event_id": provider_event_id})
        return {"event": self._public_row(updated_row)}

    def list_provider_calendars(self) -> dict[str, Any]:
        """Return all calendar collections from all providers (diagnostic/admin)."""
        result: dict[str, list[dict[str, Any]]] = {}
        for name, provider in self._providers.items():
            try:
                cals = provider.list_calendars()
                result[name] = [
                    {
                        "id": c.id,
                        "name": c.name,
                        "description": c.description,
                        "is_primary": c.is_primary,
                        "is_read_only": c.is_read_only,
                    }
                    for c in cals
                ]
            except CalendarError as exc:
                result[name] = [{"error": str(exc)}]
        return {"providers": result}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_provider(self, name: str) -> CalendarProvider:
        prov = self._providers.get(name)
        if prov is None:
            available = ", ".join(self._providers) or "none"
            raise CalendarError(
                "calendar provider %r is not configured; available: %s" % (name, available)
            )
        return prov

    def _provider_config(self, name: str) -> Optional[ProviderConfig]:
        return self._provider_configs.get(name)

    def _range(self, start: str, end: str) -> tuple[datetime, datetime]:
        range_start = parse_datetime(start, self._config, timezone_name=self._timezone_name())
        range_end = parse_datetime(end, self._config, timezone_name=self._timezone_name())
        if range_end <= range_start:
            raise CalendarError("range end must be after start")
        return range_start, range_end

    def _event_range(self, start: str, end: str, all_day: bool) -> tuple[datetime, datetime]:
        event_start = self._event_start(start, all_day)
        event_end = self._event_end(end, all_day)
        if event_end <= event_start:
            raise CalendarError("event end must be after start")
        return event_start, event_end

    def _event_start(self, value: str, all_day: bool) -> datetime:
        if not all_day:
            return parse_datetime(value, self._config, timezone_name=self._timezone_name())
        from datetime import date, time
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            tz = ZoneInfo(self._timezone_name())
        except ZoneInfoNotFoundError:
            tz = timezone.utc
        d = date.fromisoformat(str(value)[:10])
        return datetime.combine(d, time.min, tzinfo=tz)

    def _event_end(self, value: str, all_day: bool) -> datetime:
        return self._event_start(value, all_day)

    def _timezone_name(self) -> str:
        from ..time_utils import default_timezone

        return default_timezone(self._config)[1]

    def _clean_attendees(self, attendees: Optional[list[str]]) -> list[str]:
        if not attendees or not isinstance(attendees, list):
            return []
        cleaned = []
        for a in attendees:
            email = str(a or "").strip()
            if email.lower().startswith("mailto:"):
                email = email[7:]
            if email and "@" in email:
                cleaned.append(email)
        return cleaned

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _managed_row(self, assistant_id: str) -> Optional[dict[str, Any]]:
        return self._db.fetch_one(
            "SELECT * FROM calendar_managed_events WHERE assistant_id = %s AND status = 'active'",
            (assistant_id,),
        )

    def _managed_assistant_ids(self) -> set[str]:
        rows = self._db.fetch_all(
            "SELECT assistant_id FROM calendar_managed_events WHERE status = 'active'"
        )
        return {str(r["assistant_id"]) for r in rows}

    def _managed_by_provider_id(self) -> dict[str, str]:
        """Return {provider_name:provider_event_id -> assistant_id} for active events."""
        rows = self._db.fetch_all(
            "SELECT assistant_id, provider_name, provider_event_id FROM calendar_managed_events WHERE status = 'active'"
        )
        return {
            "%s:%s" % (r["provider_name"], r["provider_event_id"]): str(r["assistant_id"])
            for r in rows
            if r.get("provider_name") is not None and r.get("provider_event_id") is not None
        }


    def _managed_by_idempotency_key(self, key: str) -> Optional[dict[str, Any]]:
        row = self._db.fetch_one(
            """
            SELECT * FROM calendar_managed_events
            WHERE status = 'active'
              AND metadata->>'idempotency_key' = %s
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (key,),
        )
        return self._public_row(row) if row else None

    def _insert_managed_event(
        self,
        assistant_id: str,
        provider_name: str,
        provider_event_id: str,
        calendar_id: str,
        summary: str,
        starts_at: datetime,
        ends_at: datetime,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        row = self._db.fetch_one(
            """
            INSERT INTO calendar_managed_events(
              assistant_id,
              provider_name,
              provider_event_id,
              calendar_id,
              calendar_name,
              summary,
              starts_at,
              ends_at,
              status,
              created_by_job_id,
              updated_by_job_id,
              metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s)
            RETURNING *
            """,
            (
                assistant_id,
                provider_name,
                provider_event_id,
                calendar_id,
                calendar_id,  # calendar_name = calendar_id (safe fallback)
                summary,
                starts_at,
                ends_at,
                self._job["id"],
                self._job["id"],
                Jsonb(json_safe(metadata)),
            ),
        )
        if row is None:
            raise CalendarError("managed calendar event was not recorded")
        return row

    def _update_managed_event(
        self,
        assistant_id: str,
        summary: str,
        starts_at: datetime,
        ends_at: datetime,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        row = self._db.fetch_one(
            """
            UPDATE calendar_managed_events
            SET summary = %s,
                starts_at = %s,
                ends_at = %s,
                updated_by_job_id = %s,
                metadata = %s,
                updated_at = now()
            WHERE assistant_id = %s
              AND status = 'active'
            RETURNING *
            """,
            (
                summary,
                starts_at,
                ends_at,
                self._job["id"],
                Jsonb(json_safe(metadata)),
                assistant_id,
            ),
        )
        if row is None:
            raise CalendarError("managed calendar event was not updated")
        return row

    def _mark_deleted(self, assistant_id: str) -> dict[str, Any]:
        row = self._db.fetch_one(
            """
            UPDATE calendar_managed_events
            SET status = 'deleted',
                updated_by_job_id = %s,
                deleted_at = now(),
                updated_at = now()
            WHERE assistant_id = %s
              AND status = 'active'
            RETURNING *
            """,
            (self._job["id"], assistant_id),
        )
        if row is None:
            raise CalendarError("managed calendar event was not deleted")
        return row

    def _audit(self, assistant_id: str, action: str, payload: dict[str, Any]) -> None:
        self._db.execute(
            """
            INSERT INTO calendar_event_audit(assistant_id, job_id, action, payload)
            VALUES (%s, %s, %s, %s)
            """,
            (assistant_id, self._job["id"], action, Jsonb(json_safe(payload))),
        )

    def _public_row(self, row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        starts_at = row.get("starts_at")
        ends_at = row.get("ends_at")
        return {
            "event_id": row.get("assistant_id"),
            "provider": row.get("provider_name"),
            "provider_event_id": row.get("provider_event_id"),
            "calendar_id": row.get("calendar_id"),
            "title": row.get("summary"),
            "start": local_datetime_iso(starts_at, self._config) if hasattr(starts_at, "isoformat") else starts_at,
            "end": local_datetime_iso(ends_at, self._config) if hasattr(ends_at, "isoformat") else ends_at,
            "status": row.get("status"),
            "metadata": row.get("metadata") or {},
        }
