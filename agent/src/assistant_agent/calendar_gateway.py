import hashlib
import re
import shlex
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg.types.json import Jsonb

from .config import AppConfig, agent_email, agent_name
from .database import json_safe
from .time_utils import parse_datetime


ASSISTANT_MANAGED_PROPERTY = "X-ASSISTANT-MANAGED"
ASSISTANT_ID_PROPERTY = "X-ASSISTANT-ID"
ASSISTANT_ORIGIN_PROPERTY = "X-ASSISTANT-ORIGIN"
ASSISTANT_ORIGIN_VALUE = "local-calendar-gateway"
WEEKDAY_INDEXES = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


class CalendarError(Exception):
    pass


@dataclass
class IcsProperty:
    name: str
    params: dict[str, str]
    value: str


@dataclass
class CalendarOccurrence:
    uid: str
    calendar_name: str
    path: Path
    start: datetime
    end: datetime
    all_day: bool
    summary: str = ""
    description: str = ""
    location: str = ""
    status: str = ""
    transparency: str = ""
    managed_id: Optional[str] = None
    attendees: list[str] = field(default_factory=list)

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and self.end > start


@dataclass
class CalendarEvent:
    uid: str
    calendar_name: str
    path: Path
    props: dict[str, list[IcsProperty]]
    raw_text: str
    start: datetime
    end: datetime
    all_day: bool
    summary: str = ""
    description: str = ""
    location: str = ""
    status: str = ""
    transparency: str = ""
    managed_id: Optional[str] = None
    rrule: dict[str, str] = field(default_factory=dict)
    exdates: set[datetime] = field(default_factory=set)
    attendees: list[str] = field(default_factory=list)

    def blocks_time(self) -> bool:
        return self.status.upper() != "CANCELLED" and self.transparency.upper() != "TRANSPARENT"

    def marker_id(self) -> str:
        values = self.props.get(ASSISTANT_ID_PROPERTY) or []
        return values[0].value.strip() if values else ""

    def has_managed_marker(self) -> bool:
        marker_values = self.props.get(ASSISTANT_MANAGED_PROPERTY) or []
        origin_values = self.props.get(ASSISTANT_ORIGIN_PROPERTY) or []
        marker = marker_values[0].value.strip().upper() if marker_values else ""
        origin = origin_values[0].value.strip() if origin_values else ""
        return marker == "TRUE" and bool(self.marker_id()) and origin == ASSISTANT_ORIGIN_VALUE


class CalendarGateway:
    def __init__(self, db: Any, config: AppConfig, job: dict[str, Any]):
        self.db = db
        self.config = config
        self.job = job
        self.vdir_root = Path(config.get("agent.calendar.store.vdir_path", "/data/private/calendar/vdir")).resolve()
        self.default_calendar = self._clean_calendar_name(config.get("agent.calendar.store.default_calendar", "default"))
        self.max_occurrences_per_event = max(config.get_int("agent.calendar.limits.max_occurrences_per_event", 500), 1)

    def sync(self, reason: str = "manual") -> dict[str, Any]:
        command = self._sync_command()
        if not command:
            return {"status": "skipped", "reason": "calendar sync command is not configured"}
        timeout = max(self.config.get_int("agent.calendar.sync.timeout_seconds", 120), 1)
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.vdir_root.parent),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CalendarError("calendar sync command was not found: %s" % command[0]) from exc
        except subprocess.TimeoutExpired as exc:
            raise CalendarError("calendar sync timed out after %s second(s)" % timeout) from exc
        result = {
            "status": "ok" if completed.returncode == 0 else "failed",
            "reason": reason,
            "returncode": completed.returncode,
            "stdout_tail": (completed.stdout or "")[-4000:],
            "stderr_tail": (completed.stderr or "")[-4000:],
        }
        if completed.returncode != 0:
            detail = result["stderr_tail"] or result["stdout_tail"] or "exit status %s" % completed.returncode
            raise CalendarError("calendar sync failed: %s" % detail)
        return result

    def list_busy(self, start: str, end: str, include_details: bool = False) -> dict[str, Any]:
        range_start, range_end = self._range(start, end)
        self._sync_before_read()
        allow_details = self._allow_details() and bool(include_details)
        busy = []
        for occurrence in self._occurrences(range_start, range_end):
            if not occurrence.overlaps(range_start, range_end):
                continue
            item = {
                "start": occurrence.start.isoformat(),
                "end": occurrence.end.isoformat(),
                "calendar": occurrence.calendar_name,
                "all_day": occurrence.all_day,
                "managed": bool(occurrence.managed_id),
                "event_id": occurrence.managed_id,
            }
            if allow_details or occurrence.managed_id:
                item.update(
                    {
                        "title": occurrence.summary,
                        "location": occurrence.location,
                    }
                )
            else:
                item["title"] = "Busy"
            busy.append(item)
        busy.sort(key=lambda item: (item["start"], item["end"], item["calendar"]))
        return {"start": range_start.isoformat(), "end": range_end.isoformat(), "busy": busy}

    def list_events(self, start: str, end: str, managed_only: bool = False) -> dict[str, Any]:
        range_start, range_end = self._range(start, end)
        self._sync_before_read()
        allow_details = self._allow_details()
        events = []
        for occurrence in self._occurrences(range_start, range_end):
            if managed_only and not occurrence.managed_id:
                continue
            if not occurrence.overlaps(range_start, range_end):
                continue
            visible_details = allow_details or bool(occurrence.managed_id)
            item = {
                "event_id": occurrence.managed_id,
                "calendar": occurrence.calendar_name,
                "start": occurrence.start.isoformat(),
                "end": occurrence.end.isoformat(),
                "all_day": occurrence.all_day,
                "managed": bool(occurrence.managed_id),
                "status": occurrence.status,
                "transparency": occurrence.transparency,
                "title": occurrence.summary if visible_details else "Busy",
            }
            if visible_details:
                item.update({"description": occurrence.description, "location": occurrence.location})
            events.append(item)
        events.sort(key=lambda item: (item["start"], item["end"], item["calendar"]))
        return {"start": range_start.isoformat(), "end": range_end.isoformat(), "events": events}

    def create_event(
        self,
        title: str,
        start: str,
        end: str,
        calendar: Optional[str] = None,
        description: str = "",
        location: str = "",
        all_day: bool = False,
        transparency: str = "OPAQUE",
        attendees: Optional[list[str]] = None,
        alert_minutes: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        clean_title = str(title or "").strip()
        if not clean_title:
            raise CalendarError("event title is required")
        clean_calendar = self._clean_calendar_name(calendar or self.default_calendar)
        clean_metadata = metadata if isinstance(metadata, dict) else {}
        clean_idempotency_key = str(idempotency_key or clean_metadata.get("idempotency_key") or "").strip()
        if clean_idempotency_key:
            existing = self._managed_by_idempotency_key(clean_idempotency_key)
            if existing is not None:
                return {"event": existing, "idempotent_reuse": True, "idempotency_key": clean_idempotency_key}

        self._sync_before_write()
        event_start, event_end = self._event_range(start, end, all_day)
        assistant_id = str(uuid.uuid4())
        uid = "%s@%s" % (assistant_id, ASSISTANT_ORIGIN_VALUE)
        clean_attendees = self._clean_attendees(attendees)
        alert_mins = alert_minutes if alert_minutes is not None else self._default_alert_minutes()
        content = self._render_event(
            uid=uid,
            assistant_id=assistant_id,
            title=clean_title,
            start=event_start,
            end=event_end,
            all_day=bool(all_day),
            description=description,
            location=location,
            transparency=transparency,
            attendees=clean_attendees,
            alert_minutes=alert_mins,
            created=None,
        )
        target = self._event_path(clean_calendar, uid)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        file_hash = self._sha256_text(content)
        db_metadata = dict(clean_metadata)
        if clean_idempotency_key:
            db_metadata["idempotency_key"] = clean_idempotency_key
        row = self._insert_managed_event(
            assistant_id=assistant_id,
            uid=uid,
            calendar_name=clean_calendar,
            path=target,
            summary=clean_title,
            starts_at=event_start,
            ends_at=event_end,
            file_hash=file_hash,
            metadata=db_metadata,
        )
        self._audit(assistant_id, "created", {"event": self._public_managed_row(row)})
        sync_result = self._sync_after_write()
        return {"event": self._public_managed_row(row), "sync": sync_result}

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
        self._sync_before_write()
        event = self._managed_event_from_row(row)
        next_all_day = event.all_day if all_day is None else bool(all_day)
        current_duration = event.end - event.start
        if start is not None and end is not None:
            next_start, next_end = self._event_range(start, end, next_all_day)
        elif start is not None:
            next_start = self._event_start(start, next_all_day)
            next_end = next_start + current_duration
        elif end is not None:
            next_start = event.start
            next_end = self._event_end(end, next_all_day)
        else:
            next_start, next_end = event.start, event.end
        if next_end <= next_start:
            raise CalendarError("event end must be after start")
        next_summary = str(title).strip() if title is not None else event.summary
        if not next_summary:
            raise CalendarError("event title cannot be empty")
        next_description = str(description) if description is not None else event.description
        next_location = str(location) if location is not None else event.location
        next_transparency = str(transparency) if transparency is not None else event.transparency or "OPAQUE"
        next_attendees = self._clean_attendees(attendees) if attendees is not None else event.attendees
        next_alert_minutes = alert_minutes if alert_minutes is not None else self._default_alert_minutes()
        content = self._render_event(
            uid=event.uid,
            assistant_id=clean_id,
            title=next_summary,
            start=next_start,
            end=next_end,
            all_day=next_all_day,
            description=next_description,
            location=next_location,
            transparency=next_transparency,
            attendees=next_attendees,
            alert_minutes=next_alert_minutes,
            created=self._first_prop_value(event.props, "CREATED"),
        )
        event.path.write_text(content, encoding="utf-8")
        file_hash = self._sha256_text(content)
        next_metadata = metadata if isinstance(metadata, dict) else row.get("metadata") or {}
        updated = self._update_managed_event(
            assistant_id=clean_id,
            path=event.path,
            calendar_name=event.calendar_name,
            summary=next_summary,
            starts_at=next_start,
            ends_at=next_end,
            file_hash=file_hash,
            metadata=next_metadata,
        )
        self._audit(clean_id, "updated", {"event": self._public_managed_row(updated)})
        sync_result = self._sync_after_write()
        return {"event": self._public_managed_row(updated), "sync": sync_result}

    def delete_event(self, event_id: str) -> dict[str, Any]:
        clean_id = str(event_id or "").strip()
        row = self._managed_row(clean_id)
        if row is None:
            raise CalendarError("managed calendar event not found")
        self._sync_before_write()
        event = self._managed_event_from_row(row)
        deleted_path = str(event.path)
        event.path.unlink()
        updated = self._mark_deleted(clean_id)
        self._audit(clean_id, "deleted", {"deleted_path": deleted_path})
        sync_result = self._sync_after_write()
        return {"event": self._public_managed_row(updated), "deleted_path": deleted_path, "sync": sync_result}

    def _sync_command(self) -> list[str]:
        value = self.config.get("agent.calendar.sync.command", [])
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str):
            return shlex.split(value)
        return []

    def _sync_before_read(self) -> None:
        if self.config.get_bool("agent.calendar.sync.before_read", False):
            self.sync("before_read")

    def _sync_before_write(self) -> None:
        if self.config.get_bool("agent.calendar.sync.before_write", True):
            self.sync("before_write")

    def _sync_after_write(self) -> dict[str, Any]:
        if self.config.get_bool("agent.calendar.sync.after_write", True):
            return self.sync("after_write")
        return {"status": "skipped", "reason": "after-write calendar sync is disabled"}

    def _range(self, start: str, end: str) -> tuple[datetime, datetime]:
        range_start = parse_datetime(start, self.config)
        range_end = parse_datetime(end, self.config)
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
            return parse_datetime(value, self.config)
        return datetime.combine(date.fromisoformat(str(value)[:10]), time.min, tzinfo=self._timezone()).astimezone(timezone.utc)

    def _event_end(self, value: str, all_day: bool) -> datetime:
        if not all_day:
            return parse_datetime(value, self.config)
        return datetime.combine(date.fromisoformat(str(value)[:10]), time.min, tzinfo=self._timezone()).astimezone(timezone.utc)

    def _timezone(self) -> ZoneInfo:
        name = str(self.config.get("agent.calendar.timezone") or self.config.get("agent.app.timezone") or "UTC")
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")

    def _timezone_name(self) -> str:
        return str(self.config.get("agent.calendar.timezone") or self.config.get("agent.app.timezone") or "UTC")

    def _organizer_email(self) -> str:
        configured = (
            self.config.get("agent.calendar.organizer_email")
            or agent_email(self.config)
        )
        return str(configured).strip()

    def _organizer_name(self) -> str:
        return agent_name(self.config)

    def _default_alert_minutes(self) -> int:
        return max(self.config.get_int("agent.calendar.default_alert_minutes", 15), 0)

    def _allow_details(self) -> bool:
        return self.config.get_bool("agent.calendar.policy.allow_read_event_details", False)

    def _calendar_dirs(self) -> list[tuple[str, Path]]:
        if not self.vdir_root.exists():
            return []
        dirs = []
        if any(self.vdir_root.glob("*.ics")):
            dirs.append((self.default_calendar, self.vdir_root))
        for child in sorted(self.vdir_root.iterdir(), key=lambda item: item.name.lower()):
            if child.is_dir():
                dirs.append((child.name, child))
        return dirs

    def _events(self) -> list[CalendarEvent]:
        managed = self._managed_rows_by_uid()
        events = []
        for calendar_name, directory in self._calendar_dirs():
            for path in sorted(directory.glob("*.ics"), key=lambda item: item.name.lower()):
                try:
                    events.extend(self._parse_ics_file(path, calendar_name, managed))
                except CalendarError:
                    continue
        return events

    def _occurrences(self, start: datetime, end: datetime) -> list[CalendarOccurrence]:
        occurrences = []
        for event in self._events():
            if not event.blocks_time():
                continue
            occurrences.extend(self._expand_event(event, start, end))
        return occurrences

    def _expand_event(self, event: CalendarEvent, range_start: datetime, range_end: datetime) -> list[CalendarOccurrence]:
        if not event.rrule:
            occurrence = self._occurrence(event, event.start, event.end)
            return [occurrence] if occurrence.overlaps(range_start, range_end) else []
        freq = event.rrule.get("FREQ", "").upper()
        interval = self._positive_int(event.rrule.get("INTERVAL"), 1)
        count_limit = self._positive_int(event.rrule.get("COUNT"), self.max_occurrences_per_event)
        until = self._rrule_until(event.rrule.get("UNTIL"), event.start)
        occurrences = []
        generated = 0
        duration = event.end - event.start
        for candidate in self._recurrence_candidates(event.start, freq, interval, event.rrule.get("BYDAY"), range_end + duration):
            if generated >= min(count_limit, self.max_occurrences_per_event):
                break
            if until is not None and candidate > until:
                break
            generated += 1
            candidate_utc = candidate.astimezone(timezone.utc)
            if candidate_utc in event.exdates:
                continue
            occurrence = self._occurrence(event, candidate_utc, candidate_utc + duration)
            if occurrence.overlaps(range_start, range_end):
                occurrences.append(occurrence)
        return occurrences

    def _recurrence_candidates(
        self,
        start: datetime,
        freq: str,
        interval: int,
        byday: Optional[str],
        stop_after: datetime,
    ) -> list[datetime]:
        candidates = []
        if freq in {"DAILY", "WEEKLY"} and byday:
            weekdays = {WEEKDAY_INDEXES[item] for item in byday.split(",") if item in WEEKDAY_INDEXES}
            cursor = start
            while cursor <= stop_after and len(candidates) < self.max_occurrences_per_event:
                if cursor.weekday() in weekdays:
                    if freq == "DAILY" or ((cursor.date() - start.date()).days // 7) % interval == 0:
                        candidates.append(cursor)
                cursor = cursor + timedelta(days=1)
            return candidates
        cursor = start
        while cursor <= stop_after and len(candidates) < self.max_occurrences_per_event:
            candidates.append(cursor)
            if freq == "DAILY":
                cursor = cursor + timedelta(days=interval)
            elif freq == "WEEKLY":
                cursor = cursor + timedelta(weeks=interval)
            elif freq == "MONTHLY":
                cursor = self._add_months(cursor, interval)
            elif freq == "YEARLY":
                cursor = self._add_months(cursor, interval * 12)
            else:
                break
        return candidates

    def _occurrence(self, event: CalendarEvent, start: datetime, end: datetime) -> CalendarOccurrence:
        return CalendarOccurrence(
            uid=event.uid,
            calendar_name=event.calendar_name,
            path=event.path,
            start=start,
            end=end,
            all_day=event.all_day,
            summary=event.summary,
            description=event.description,
            location=event.location,
            status=event.status,
            transparency=event.transparency,
            managed_id=event.managed_id,
            attendees=event.attendees,
        )

    def _parse_ics_file(
        self,
        path: Path,
        calendar_name: str,
        managed_rows: dict[str, dict[str, Any]],
    ) -> list[CalendarEvent]:
        raw = path.read_text(encoding="utf-8", errors="replace")
        events = []
        for lines in self._vevent_blocks(raw):
            props = self._parse_properties(lines)
            uid = self._first_prop_value(props, "UID")
            if not uid:
                continue
            start_prop = self._first_prop(props, "DTSTART")
            if start_prop is None:
                continue
            all_day = start_prop.params.get("VALUE", "").upper() == "DATE" or self._looks_like_ics_date(start_prop.value)
            start = self._parse_ics_datetime(start_prop, all_day)
            end_prop = self._first_prop(props, "DTEND")
            if end_prop:
                end = self._parse_ics_datetime(end_prop, all_day)
            else:
                end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
            event = CalendarEvent(
                uid=uid,
                calendar_name=calendar_name,
                path=path,
                props=props,
                raw_text=raw,
                start=start,
                end=end,
                all_day=all_day,
                summary=self._unescape_text(self._first_prop_value(props, "SUMMARY")),
                description=self._unescape_text(self._first_prop_value(props, "DESCRIPTION")),
                location=self._unescape_text(self._first_prop_value(props, "LOCATION")),
                status=self._first_prop_value(props, "STATUS"),
                transparency=self._first_prop_value(props, "TRANSP") or "OPAQUE",
                rrule=self._parse_rrule(self._first_prop_value(props, "RRULE")),
                exdates=self._parse_exdates(props, all_day),
                attendees=self._parse_attendees(props),
            )
            managed_row = managed_rows.get(uid)
            if managed_row:
                event.managed_id = str(managed_row["assistant_id"])
            events.append(event)
        return events

    def _vevent_blocks(self, raw: str) -> list[list[str]]:
        blocks = []
        current: Optional[list[str]] = None
        for line in self._unfold(raw):
            upper = line.upper()
            if upper == "BEGIN:VEVENT":
                current = []
            elif upper == "END:VEVENT":
                if current is not None:
                    blocks.append(current)
                current = None
            elif current is not None:
                current.append(line)
        return blocks

    def _unfold(self, raw: str) -> list[str]:
        lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        unfolded: list[str] = []
        for line in lines:
            if not line:
                continue
            if line.startswith((" ", "\t")) and unfolded:
                unfolded[-1] += line[1:]
            else:
                unfolded.append(line)
        return unfolded

    def _parse_properties(self, lines: list[str]) -> dict[str, list[IcsProperty]]:
        props: dict[str, list[IcsProperty]] = {}
        for line in lines:
            if ":" not in line:
                continue
            head, value = line.split(":", 1)
            parts = head.split(";")
            name = parts[0].upper()
            params = {}
            for item in parts[1:]:
                if "=" in item:
                    key, param_value = item.split("=", 1)
                    params[key.upper()] = param_value.strip('"')
            props.setdefault(name, []).append(IcsProperty(name=name, params=params, value=value))
        return props

    def _parse_ics_datetime(self, prop: IcsProperty, all_day: bool) -> datetime:
        value = prop.value.strip()
        if all_day:
            parsed_date = datetime.strptime(value[:8], "%Y%m%d").date()
            return datetime.combine(parsed_date, time.min, tzinfo=self._timezone()).astimezone(timezone.utc)
        tzid = prop.params.get("TZID")
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        fmt = "%Y%m%dT%H%M%S" if len(value) >= 15 else "%Y%m%dT%H%M"
        parsed = datetime.strptime(value[:15] if fmt.endswith("%S") else value[:13], fmt)
        zone = self._timezone()
        if tzid:
            try:
                zone = ZoneInfo(tzid)
            except ZoneInfoNotFoundError:
                zone = self._timezone()
        return parsed.replace(tzinfo=zone).astimezone(timezone.utc)

    def _parse_exdates(self, props: dict[str, list[IcsProperty]], all_day: bool) -> set[datetime]:
        values = set()
        for prop in props.get("EXDATE") or []:
            prop_all_day = all_day or prop.params.get("VALUE", "").upper() == "DATE"
            for item in prop.value.split(","):
                if item.strip():
                    values.add(self._parse_ics_datetime(IcsProperty(prop.name, prop.params, item.strip()), prop_all_day))
        return values

    def _parse_rrule(self, value: str) -> dict[str, str]:
        result = {}
        for item in str(value or "").split(";"):
            if "=" not in item:
                continue
            key, raw_value = item.split("=", 1)
            result[key.upper()] = raw_value.upper()
        return result

    def _rrule_until(self, value: Optional[str], start: datetime) -> Optional[datetime]:
        if not value:
            return None
        prop = IcsProperty("UNTIL", {}, value)
        return self._parse_ics_datetime(prop, self._looks_like_ics_date(value)).astimezone(start.tzinfo or timezone.utc)

    def _looks_like_ics_date(self, value: str) -> bool:
        return bool(re.fullmatch(r"\d{8}", str(value or "").strip()))

    def _event_path(self, calendar_name: str, uid: str) -> Path:
        filename = re.sub(r"[^A-Za-z0-9_.@-]+", "_", uid)[:180] + ".ics"
        return self.vdir_root / calendar_name / filename

    def _clean_calendar_name(self, value: Any) -> str:
        clean = str(value or "default").strip().strip("/.")
        clean = re.sub(r"[^A-Za-z0-9_. -]+", "_", clean)
        return clean or "default"

    def _render_event(
        self,
        uid: str,
        assistant_id: str,
        title: str,
        start: datetime,
        end: datetime,
        all_day: bool,
        description: str,
        location: str,
        transparency: str,
        attendees: list[str],
        alert_minutes: int,
        created: Optional[str],
        sequence: int = 0,
    ) -> str:
        now = datetime.now(timezone.utc)
        created_value = created or self._format_ics_datetime(now)
        tz_name = self._timezone_name()
        
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Assistant//Local Calendar Gateway//EN",
            "CALSCALE:GREGORIAN",
        ]
        
        # Add VTIMEZONE component for non-all-day events
        if not all_day:
            lines.extend(self._vtimezone_block(start))
        
        lines.extend([
            "BEGIN:VEVENT",
            "UID:%s" % uid,
            "DTSTAMP:%s" % self._format_ics_datetime(now),
            "CREATED:%s" % created_value,
            "LAST-MODIFIED:%s" % self._format_ics_datetime(now),
        ])
        
        # Format DTSTART/DTEND with TZID for non-all-day events
        if all_day:
            lines.append("DTSTART;VALUE=DATE:%s" % self._format_event_time(start, all_day))
            lines.append("DTEND;VALUE=DATE:%s" % self._format_event_time(end, all_day))
        else:
            lines.append("DTSTART;TZID=%s:%s" % (tz_name, self._format_local_time(start)))
            lines.append("DTEND;TZID=%s:%s" % (tz_name, self._format_local_time(end)))
        
        lines.extend([
            "SUMMARY:%s" % self._escape_text(title),
            "TRANSP:%s" % self._clean_transparency(transparency),
            "STATUS:CONFIRMED",
            "SEQUENCE:%d" % sequence,
        ])
        
        # Add ORGANIZER if there are attendees
        if attendees:
            organizer_email = self._organizer_email()
            organizer_name = self._organizer_name()
            lines.append("ORGANIZER;CN=%s:mailto:%s" % (self._escape_text(organizer_name), organizer_email))
        
        lines.extend([
            "%s:TRUE" % ASSISTANT_MANAGED_PROPERTY,
            "%s:%s" % (ASSISTANT_ID_PROPERTY, assistant_id),
            "%s:%s" % (ASSISTANT_ORIGIN_PROPERTY, ASSISTANT_ORIGIN_VALUE),
        ])
        
        if description:
            lines.append("DESCRIPTION:%s" % self._escape_text(description))
        if location:
            lines.append("LOCATION:%s" % self._escape_text(location))
        
        # Add attendees with proper scheduling parameters
        for attendee in attendees:
            lines.append("ATTENDEE;PARTSTAT=NEEDS-ACTION;RSVP=TRUE;ROLE=REQ-PARTICIPANT:mailto:%s" % attendee)
        
        # Add VALARM components for reminder/alert.
        #
        # Apple Calendar (macOS/iOS) uses a proprietary sentinel VALARM with
        # ACTION:NONE and X-APPLE-DEFAULT-ALARM:TRUE to signal that the default
        # alarm preference has already been applied for this event.  Without
        # this sentinel, Apple Calendar ignores or overrides any embedded
        # VALARM with whatever the account-level default-alert setting is
        # (which may be "None").  The sentinel must always be written so that
        # Apple Calendar honours the real alarm that follows it.
        # The UID value and TRIGGER date-time are the canonical values used by
        # Apple Calendar itself for this sentinel.
        alarm_uid = str(uuid.uuid4()).upper()
        sentinel_uid = str(uuid.uuid4()).upper()
        lines.extend([
            "BEGIN:VALARM",
            "ACTION:NONE",
            "TRIGGER;VALUE=DATE-TIME:19760401T005545Z",
            "X-WR-ALARMUID:%s" % sentinel_uid,
            "UID:%s" % sentinel_uid,
            "X-APPLE-DEFAULT-ALARM:TRUE",
            "END:VALARM",
        ])
        if alert_minutes > 0:
            lines.extend([
                "BEGIN:VALARM",
                "UID:%s" % alarm_uid,
                "X-WR-ALARMUID:%s" % alarm_uid,
                "TRIGGER:-PT%dM" % alert_minutes,
                "ACTION:DISPLAY",
                "DESCRIPTION:Reminder",
                "END:VALARM",
            ])
        
        lines.extend(["END:VEVENT", "END:VCALENDAR", ""])
        return "\r\n".join(self._fold(line) for line in lines)

    def _format_event_time(self, value: datetime, all_day: bool) -> str:
        if all_day:
            return value.astimezone(self._timezone()).date().strftime("%Y%m%d")
        return self._format_ics_datetime(value)

    def _format_ics_datetime(self, value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def _format_local_time(self, value: datetime) -> str:
        """Format datetime in the configured timezone (for TZID-parameterized properties)."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        local = value.astimezone(self._timezone())
        return local.strftime("%Y%m%dT%H%M%S")

    def _vtimezone_block(self, reference_time: datetime) -> list[str]:
        """Generate a VTIMEZONE component for the configured timezone."""
        tz = self._timezone()
        tz_name = self._timezone_name()
        
        # Convert reference time to the target timezone to get offset info
        if reference_time.tzinfo is None:
            reference_time = reference_time.replace(tzinfo=timezone.utc)
        local_time = reference_time.astimezone(tz)
        
        # Get UTC offset in seconds, then convert to ±HHMM format
        offset_seconds = local_time.utcoffset().total_seconds() if local_time.utcoffset() else 0
        offset_hours = int(offset_seconds // 3600)
        offset_minutes = int((abs(offset_seconds) % 3600) // 60)
        offset_str = "%s%02d%02d" % ("+" if offset_seconds >= 0 else "-", abs(offset_hours), offset_minutes)
        
        # For simplicity, we create a single STANDARD block (no DST transitions)
        # Production systems could enhance this to detect DST rules
        return [
            "BEGIN:VTIMEZONE",
            "TZID:%s" % tz_name,
            "BEGIN:STANDARD",
            "DTSTART:19700101T000000",
            "TZOFFSETFROM:%s" % offset_str,
            "TZOFFSETTO:%s" % offset_str,
            "TZNAME:%s" % offset_str,
            "END:STANDARD",
            "END:VTIMEZONE",
        ]

    def _fold(self, line: str) -> str:
        if len(line) <= 74:
            return line
        parts = [line[:74]]
        rest = line[74:]
        while rest:
            parts.append(" " + rest[:73])
            rest = rest[73:]
        return "\r\n".join(parts)

    def _escape_text(self, value: Any) -> str:
        return str(value or "").replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")

    def _unescape_text(self, value: Any) -> str:
        text = str(value or "")
        return text.replace("\\n", "\n").replace("\\N", "\n").replace("\\;", ";").replace("\\,", ",").replace("\\\\", "\\")

    def _clean_transparency(self, value: Any) -> str:
        clean = str(value or "OPAQUE").strip().upper()
        return clean if clean in {"OPAQUE", "TRANSPARENT"} else "OPAQUE"

    def _first_prop(self, props: dict[str, list[IcsProperty]], name: str) -> Optional[IcsProperty]:
        values = props.get(name.upper()) or []
        return values[0] if values else None

    def _first_prop_value(self, props: dict[str, list[IcsProperty]], name: str) -> str:
        prop = self._first_prop(props, name)
        return prop.value if prop else ""

    def _positive_int(self, value: Any, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return number if number > 0 else default

    def _add_months(self, value: datetime, months: int) -> datetime:
        month_index = value.month - 1 + months
        year = value.year + month_index // 12
        month = month_index % 12 + 1
        last_day = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
        return value.replace(year=year, month=month, day=min(value.day, last_day))

    def _sha256_text(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _relative_path(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.vdir_root))

    def _managed_rows_by_uid(self) -> dict[str, dict[str, Any]]:
        rows = self.db.fetch_all("SELECT * FROM calendar_managed_events WHERE status = 'active'")
        return {str(row.get("uid")): row for row in rows}

    def _managed_by_idempotency_key(self, key: str) -> Optional[dict[str, Any]]:
        row = self.db.fetch_one(
            """
            SELECT *
            FROM calendar_managed_events
            WHERE status = 'active'
              AND metadata->>'idempotency_key' = %s
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (key,),
        )
        return self._public_managed_row(row) if row else None

    def _managed_row(self, assistant_id: str) -> Optional[dict[str, Any]]:
        return self.db.fetch_one(
            "SELECT * FROM calendar_managed_events WHERE assistant_id = %s AND status = 'active'",
            (assistant_id,),
        )

    def _managed_event_from_row(self, row: dict[str, Any]) -> CalendarEvent:
        uid = str(row.get("uid") or "")
        events = [event for event in self._events() if event.uid == uid]
        if not events:
            raise CalendarError("managed calendar event file is missing; run sync and try again")
        return events[0]

    def _insert_managed_event(
        self,
        assistant_id: str,
        uid: str,
        calendar_name: str,
        path: Path,
        summary: str,
        starts_at: datetime,
        ends_at: datetime,
        file_hash: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        row = self.db.fetch_one(
            """
            INSERT INTO calendar_managed_events(
              assistant_id,
              uid,
              calendar_name,
              relative_path,
              summary,
              starts_at,
              ends_at,
              file_hash,
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
                uid,
                calendar_name,
                self._relative_path(path),
                summary,
                starts_at,
                ends_at,
                file_hash,
                self.job["id"],
                self.job["id"],
                Jsonb(json_safe(metadata)),
            ),
        )
        if row is None:
            raise CalendarError("managed calendar event was not recorded")
        return row

    def _update_managed_event(
        self,
        assistant_id: str,
        path: Path,
        calendar_name: str,
        summary: str,
        starts_at: datetime,
        ends_at: datetime,
        file_hash: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        row = self.db.fetch_one(
            """
            UPDATE calendar_managed_events
            SET calendar_name = %s,
                relative_path = %s,
                summary = %s,
                starts_at = %s,
                ends_at = %s,
                file_hash = %s,
                updated_by_job_id = %s,
                metadata = %s,
                updated_at = now()
            WHERE assistant_id = %s
              AND status = 'active'
            RETURNING *
            """,
            (
                calendar_name,
                self._relative_path(path),
                summary,
                starts_at,
                ends_at,
                file_hash,
                self.job["id"],
                Jsonb(json_safe(metadata)),
                assistant_id,
            ),
        )
        if row is None:
            raise CalendarError("managed calendar event was not updated")
        return row

    def _mark_deleted(self, assistant_id: str) -> dict[str, Any]:
        row = self.db.fetch_one(
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
            (self.job["id"], assistant_id),
        )
        if row is None:
            raise CalendarError("managed calendar event was not deleted")
        return row

    def _audit(self, assistant_id: str, action: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO calendar_event_audit(
              assistant_id,
              job_id,
              action,
              payload
            )
            VALUES (%s, %s, %s, %s)
            """,
            (assistant_id, self.job["id"], action, Jsonb(json_safe(payload))),
        )

    def _public_managed_row(self, row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        return {
            "event_id": row.get("assistant_id"),
            "uid": row.get("uid"),
            "calendar": row.get("calendar_name"),
            "title": row.get("summary"),
            "start": row.get("starts_at").isoformat() if hasattr(row.get("starts_at"), "isoformat") else row.get("starts_at"),
            "end": row.get("ends_at").isoformat() if hasattr(row.get("ends_at"), "isoformat") else row.get("ends_at"),
            "status": row.get("status"),
            "metadata": row.get("metadata") or {},
        }

    def _clean_attendees(self, attendees: Optional[list[str]]) -> list[str]:
        if not attendees or not isinstance(attendees, list):
            return []
        cleaned = []
        for attendee in attendees:
            email = str(attendee or "").strip()
            if email and "@" in email:
                # Remove mailto: prefix if present
                if email.lower().startswith("mailto:"):
                    email = email[7:]
                cleaned.append(email)
        return cleaned

    def _parse_attendees(self, props: dict[str, list[IcsProperty]]) -> list[str]:
        attendees = []
        for prop in props.get("ATTENDEE") or []:
            value = prop.value.strip()
            # Extract email from mailto: URI
            if value.lower().startswith("mailto:"):
                email = value[7:]
                attendees.append(email)
            elif "@" in value:
                attendees.append(value)
        return attendees
