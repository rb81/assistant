from calendar import monthrange
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import AppConfig


def default_timezone(config: AppConfig) -> tuple[tzinfo, str]:
    name = str(config.get("agent.reminders.default_timezone") or config.get("agent.app.timezone") or "UTC")
    try:
        return ZoneInfo(name), name
    except ZoneInfoNotFoundError:
        return timezone.utc, "UTC"


def parse_datetime(value: str, config: AppConfig) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("datetime value is required")
    if text.endswith("Z"):
        text = "%s+00:00" % text[:-1]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("datetime must be ISO 8601, for example 2026-06-04T17:00:00+04:00") from exc
    if parsed.tzinfo is None:
        zone, _ = default_timezone(config)
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(timezone.utc)


def recurrence_anchor_day(value: datetime, unit: Optional[str], config: AppConfig) -> Optional[int]:
    if unit != "month":
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    zone, _ = default_timezone(config)
    return value.astimezone(zone).day


def add_months(value: datetime, months: int, anchor_day: Optional[int] = None) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(anchor_day or value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def advance_recurring_datetime(value: datetime, unit: str, interval: int, anchor_day: Optional[int] = None) -> datetime:
    if unit == "hour":
        return value + timedelta(hours=interval)
    if unit == "day":
        return value + timedelta(days=interval)
    if unit == "week":
        return value + timedelta(weeks=interval)
    if unit == "month":
        return add_months(value, interval, anchor_day)
    raise ValueError("invalid recurrence unit")


def next_recurring_run_at(
    run_at: datetime,
    unit: str,
    interval: int,
    config: AppConfig,
    anchor_day: Optional[int] = None,
    now: Optional[datetime] = None,
) -> datetime:
    if interval < 1:
        raise ValueError("recurrence interval must be greater than zero")
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    zone, _ = default_timezone(config)
    candidate = run_at.astimezone(zone)
    now_local = now.astimezone(zone)
    month_anchor = anchor_day if unit == "month" else None

    while True:
        candidate = advance_recurring_datetime(candidate, unit, interval, month_anchor)
        if candidate > now_local:
            return candidate.astimezone(timezone.utc)


def local_datetime_iso(value: datetime, config: AppConfig) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    zone, _ = default_timezone(config)
    return value.astimezone(zone).isoformat()


def datetime_context_label(value: datetime, config: AppConfig) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    zone_name = default_timezone(config)[1]
    local_value = local_datetime_iso(value, config)
    utc_value = value.astimezone(timezone.utc).isoformat()
    if zone_name == "UTC":
        return utc_value
    return "%s (%s; UTC %s)" % (local_value, zone_name, utc_value)


def current_time_context(config: AppConfig) -> dict[str, str]:
    now_utc = datetime.now(timezone.utc)
    zone, zone_name = default_timezone(config)
    local_now = now_utc.astimezone(zone)
    return {
        "utc": now_utc.isoformat(),
        "local": local_now.isoformat(),
        "timezone": zone_name,
    }
