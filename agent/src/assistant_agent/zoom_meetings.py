import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from psycopg.types.json import Jsonb

from .time_utils import default_timezone, local_datetime_iso, parse_datetime


class ZoomError(RuntimeError):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class ZoomMeetings:
    def __init__(self, db, config, job):
        self.db, self.config, self.job = db, config, job
        self.client_id = str(config.get("agent.zoom.client_id") or "")
        self.client_secret = str(config.get("agent.zoom.client_secret") or "")
        self.account_id = str(config.get("agent.zoom.account_id") or "")
        self.refresh_token = str(config.get("agent.zoom.refresh_token") or "")
        self.user_id = str(config.get("agent.zoom.user_id") or "me")
        self.mode = str(config.get("agent.zoom.auth_mode") or ("s2s" if self.account_id else "oauth"))
        if not config.get_bool("agent.zoom.enabled", False) or not self.client_id or not self.client_secret:
            raise ZoomError("Zoom is not configured")
        if self.mode not in {"s2s", "oauth"} or (self.mode == "s2s" and not self.account_id) or (self.mode == "oauth" and not self.refresh_token):
            raise ZoomError("Zoom requires s2s account credentials or an OAuth refresh token")
        self.state_key = "zoom-token:" + hashlib.sha256((self.client_id + self.account_id + self.user_id + self.mode).encode()).hexdigest()

    def token(self, force=False):
        fingerprint = hashlib.sha256(self.refresh_token.encode()).hexdigest()
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.state_key,))
                cur.execute("SELECT value FROM runtime_state WHERE key = %s", (self.state_key,))
                row = cur.fetchone()
                state = row["value"] if row else {}
                if state.get("fingerprint") != fingerprint:
                    state = {}
                if not force and state.get("expires_at", 0) > time.time() + 120:
                    return state["access_token"]
                data = {"grant_type": "account_credentials", "account_id": self.account_id} if self.mode == "s2s" else {"grant_type": "refresh_token", "refresh_token": state.get("refresh_token") or self.refresh_token}
                try:
                    response = httpx.post("https://zoom.us/oauth/token", data=data, auth=(self.client_id, self.client_secret), timeout=20)
                except httpx.RequestError as exc:
                    raise ZoomError("Zoom token service unavailable") from exc
                if response.status_code != 200:
                    raise ZoomError("Zoom authorization failed (HTTP %s)" % response.status_code)
                payload = response.json()
                if not payload.get("access_token"):
                    raise ZoomError("Zoom token service returned an invalid response")
                state = {"access_token": payload["access_token"], "refresh_token": payload.get("refresh_token") or state.get("refresh_token") or self.refresh_token,
                         "expires_at": time.time() + int(payload.get("expires_in", 3600)), "fingerprint": fingerprint}
                cur.execute("INSERT INTO runtime_state(key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()", (self.state_key, Jsonb(state)))
                return state["access_token"]

    def request(self, method, path, payload=None):
        backoff = self.db.get_state(self.state_key + ":backoff", {}) or {}
        if backoff.get("until", 0) > time.time():
            raise ZoomError("Zoom rate limit 429: retry after %s seconds" % int(backoff["until"] - time.time() + 1))
        for attempt in range(2):
            token = self.token(force=bool(attempt))
            try:
                response = httpx.request(method, "https://api.zoom.us/v2/" + path, json=payload, headers={"Authorization": "Bearer " + token}, timeout=30)
            except httpx.RequestError as exc:
                raise ZoomError("Zoom request outcome is uncertain; reconcile before retrying creation", uncertain=True) from exc
            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code == 429:
                try:
                    delay = min(max(int(response.headers.get("Retry-After", "60")), 1), 3600)
                except ValueError:
                    delay = 60
                self.db.set_state(self.state_key + ":backoff", {"until": time.time() + delay})
                raise ZoomError("Zoom rate limit 429: retry after %s seconds" % delay)
            if response.status_code == 404 and method == "DELETE":
                return {}
            if response.status_code >= 400:
                raise ZoomError("Zoom request failed (HTTP %s)" % response.status_code, uncertain=response.status_code >= 500)
            if response.status_code == 204:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise ZoomError("Zoom returned an unreadable response; reconcile before retrying", uncertain=True) from exc
        raise ZoomError("Zoom authorization failed")

    def public(self, row):
        result = {key: row.get(key) for key in ("id", "provider_meeting_id", "title", "duration_minutes", "join_url", "status", "calendar_event_id", "last_error")}
        start = row.get("starts_at")
        result["start"] = local_datetime_iso(parse_datetime(start, self.config) if isinstance(start, str) else start, self.config) if start else None
        result["timezone"] = default_timezone(self.config)[1]
        return result

    def managed(self, meeting_id):
        row = self.db.fetch_one("SELECT * FROM zoom_managed_meetings WHERE id = %s", (int(meeting_id),))
        if not row:
            raise ZoomError("Zoom meeting is not managed by this assistant")
        return row

    def create(self, title, start, duration_minutes, idempotency_key):
        title = str(title).strip()
        if not title or not str(idempotency_key).strip() or not 1 <= int(duration_minutes) <= 1440:
            raise ZoomError("title, stable idempotency_key and duration of 1-1440 minutes are required")
        starts_at = parse_datetime(start, self.config)
        payload = {"topic": title, "type": 2, "start_time": starts_at.isoformat().replace("+00:00", "Z"), "duration": int(duration_minutes),
                   "timezone": default_timezone(self.config)[1], "settings": {"waiting_room": True, "join_before_host": False}}
        row = self.db.fetch_one("INSERT INTO zoom_managed_meetings(request_key, request_payload, title, starts_at, duration_minutes, created_by_job_id) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (request_key) DO NOTHING RETURNING *", (idempotency_key, Jsonb(payload), title, starts_at, int(duration_minutes), self.job.get("id")))
        if row is None:
            row = self.db.fetch_one("SELECT * FROM zoom_managed_meetings WHERE request_key = %s", (idempotency_key,))
            if row["request_payload"] != payload:
                raise ZoomError("idempotency key belongs to a different meeting request")
            if row["status"] == "active":
                return self.public(row)
            if row["status"] != "failed":
                raise ZoomError("creation is pending or uncertain; reconcile the existing request, do not create another meeting")
            row = self.db.fetch_one("UPDATE zoom_managed_meetings SET status = 'creating', last_error = NULL WHERE id = %s AND status = 'failed' RETURNING *", (row["id"],))
            if row is None:
                raise ZoomError("another worker is handling this meeting")
        try:
            meeting = self.request("POST", "users/%s/meetings" % quote(self.user_id, safe=""), payload)
            if not meeting.get("id") or not str(meeting.get("join_url") or "").startswith("https://"):
                raise ZoomError("Zoom creation response is incomplete; reconcile before retrying", uncertain=True)
        except ZoomError as exc:
            self.db.execute("UPDATE zoom_managed_meetings SET status = %s, last_error = %s WHERE id = %s", ("uncertain" if exc.uncertain else "failed", str(exc), row["id"]))
            raise
        saved = self.db.fetch_one("UPDATE zoom_managed_meetings SET provider_meeting_id = %s, join_url = %s, status = 'active', last_error = NULL WHERE id = %s RETURNING *", (str(meeting["id"]), meeting["join_url"], row["id"]))
        return self.public(saved)

    def read(self, meeting_id):
        row = self.managed(meeting_id)
        if row["status"] != "active":
            return self.public(row)
        meeting = self.request("GET", "meetings/" + quote(row["provider_meeting_id"], safe=""))
        row.update({"title": meeting.get("topic", row["title"]), "starts_at": meeting.get("start_time", row["starts_at"]), "duration_minutes": meeting.get("duration", row["duration_minutes"]), "join_url": meeting.get("join_url", row["join_url"])})
        return self.public(row)

    def update(self, meeting_id, title=None, start=None, duration_minutes=None, sync_calendar=True):
        row = self.managed(meeting_id)
        if row["status"] != "active":
            raise ZoomError("only active managed Zoom meetings can be edited")
        payload = {}
        if title is not None:
            if not str(title).strip():
                raise ZoomError("meeting title cannot be empty")
            payload["topic"] = str(title).strip()
        if start is not None:
            payload["start_time"] = parse_datetime(start, self.config).isoformat().replace("+00:00", "Z")
            payload["timezone"] = default_timezone(self.config)[1]
        if duration_minutes is not None:
            if not 1 <= int(duration_minutes) <= 1440:
                raise ZoomError("duration must be 1-1440 minutes")
            payload["duration"] = int(duration_minutes)
        if not payload:
            raise ZoomError("provide at least one meeting field to update")
        self.request("PATCH", "meetings/" + quote(row["provider_meeting_id"], safe=""), payload)
        row = self.db.fetch_one("UPDATE zoom_managed_meetings SET title = COALESCE(%s, title), starts_at = COALESCE(%s, starts_at), duration_minutes = COALESCE(%s, duration_minutes) WHERE id = %s RETURNING *", (payload.get("topic"), parse_datetime(start, self.config) if start is not None else None, payload.get("duration"), row["id"]))
        if row.get("calendar_event_id") and sync_calendar:
            self.sync_calendar(row)
        return self.public(row)

    def sync_calendar(self, row):
        from .calendar.router import CalendarRouter
        start = row["starts_at"]
        try:
            CalendarRouter(self.db, self.config, self.job).update_event(row["calendar_event_id"], title=row["title"], start=start.isoformat(), end=(start + timedelta(minutes=row["duration_minutes"])).isoformat())
            self.db.execute("UPDATE zoom_managed_meetings SET last_error = NULL WHERE id = %s", (row["id"],))
        except Exception as exc:
            self.db.execute("UPDATE zoom_managed_meetings SET last_error = 'calendar_sync_pending' WHERE id = %s", (row["id"],))
            raise ZoomError("Zoom was updated; linked calendar sync is pending. Retry the same update, not meeting creation.") from exc

    def delete(self, meeting_id):
        row = self.managed(meeting_id)
        if row["status"] == "deleted":
            return self.public(row)
        if not row.get("provider_meeting_id"):
            raise ZoomError("uncertain creation must be reconciled before deletion")
        self.request("DELETE", "meetings/" + quote(row["provider_meeting_id"], safe=""))
        if row.get("calendar_event_id"):
            from .calendar.router import CalendarRouter
            try:
                CalendarRouter(self.db, self.config, self.job).delete_event(row["calendar_event_id"])
            except Exception as exc:
                self.db.execute("UPDATE zoom_managed_meetings SET last_error = 'calendar_delete_pending' WHERE id = %s", (row["id"],))
                raise ZoomError("Zoom was deleted; retry deletion to reconcile the linked calendar") from exc
        row = self.db.fetch_one("UPDATE zoom_managed_meetings SET status = 'deleted', last_error = NULL WHERE id = %s RETURNING *", (row["id"],))
        return self.public(row)

    def reconcile(self, meeting_id, provider_meeting_id):
        if not (self.job.get("metadata") or {}).get("admin_authenticated"):
            raise ZoomError("reconciliation requires an authenticated admin request")
        row = self.managed(meeting_id)
        if row["status"] not in {"uncertain", "creating"}:
            raise ZoomError("meeting is not awaiting reconciliation")
        meeting = self.request("GET", "meetings/" + quote(str(provider_meeting_id), safe=""))
        host = self.request("GET", "users/" + quote(self.user_id, safe=""))
        if meeting.get("host_id") != host.get("id") or meeting.get("topic") != row["title"] or parse_datetime(meeting.get("start_time"), self.config) != row["starts_at"] or meeting.get("duration") != row["duration_minutes"]:
            raise ZoomError("meeting does not match the authorized host and pending request")
        saved = self.db.fetch_one("UPDATE zoom_managed_meetings SET provider_meeting_id = %s, join_url = %s, status = 'active', last_error = NULL WHERE id = %s RETURNING *", (str(provider_meeting_id), meeting["join_url"], row["id"]))
        return self.public(saved)


ZOOM_TOOL_NAMES = {"zoom_create_meeting", "zoom_read_meeting", "zoom_list_meetings", "zoom_update_meeting", "zoom_delete_meeting", "zoom_reconcile_meeting"}
ZOOM_FUNCTION_TOOLS = []
for name, description, properties, required in (
    ("zoom_create_meeting", "Create a managed Zoom meeting. Implicit times use the assistant timezone; explicit offsets are preserved. Always reuse the same idempotency_key on retries. Never recreate an uncertain request.",
     {"title": {"type": "string"}, "start": {"type": "string"}, "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 1440}, "idempotency_key": {"type": "string"}}, ["title", "start", "duration_minutes", "idempotency_key"]),
    ("zoom_read_meeting", "Read an assistant-managed Zoom meeting. Returns participant link, never host credentials.", {"meeting_id": {"type": "integer"}}, ["meeting_id"]),
    ("zoom_list_meetings", "List assistant-managed Zoom meetings, including requests needing reconciliation.", {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}, []),
    ("zoom_update_meeting", "Edit an assistant-managed Zoom meeting and synchronize its linked calendar event. A partial failure must be reconciled by retrying the same update, not by creating a new meeting.",
     {"meeting_id": {"type": "integer"}, "title": {"type": "string"}, "start": {"type": "string"}, "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 1440}}, ["meeting_id"]),
    ("zoom_delete_meeting", "Delete an assistant-managed Zoom meeting and its linked calendar event. Repeating deletion is safe.", {"meeting_id": {"type": "integer"}}, ["meeting_id"]),
    ("zoom_reconcile_meeting", "Authenticated-admin recovery for an uncertain creation: associate a verified Zoom provider ID with the existing local meeting ID. Host, title, start and duration must match.", {"meeting_id": {"type": "integer"}, "provider_meeting_id": {"type": "string"}}, ["meeting_id", "provider_meeting_id"]),
):
    ZOOM_FUNCTION_TOOLS.append({"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}})