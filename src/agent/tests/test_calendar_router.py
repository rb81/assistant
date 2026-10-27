"""
Tests for the CalendarRouter and the calendar_configured() validation helper.

All provider API calls are mocked — no network or database access required.
"""

import os
import sys
import types
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

# Keep calendar tests from importing the real psycopg Jsonb adapter, which
# would leak into later tests that stub Jsonb as an identity wrapper.
psycopg_module = types.ModuleType("psycopg")
_mock_conn = MagicMock()
_mock_conn.__enter__.return_value = _mock_conn
_mock_conn.__exit__.return_value = None
_mock_cursor = MagicMock()
_mock_cursor.__enter__.return_value = _mock_cursor
_mock_cursor.__exit__.return_value = None
_mock_conn.cursor.return_value = _mock_cursor
psycopg_module.connect = MagicMock(return_value=_mock_conn)
psycopg_module.Connection = object
sys.modules.setdefault("psycopg", psycopg_module)

rows_module = types.ModuleType("psycopg.rows")
rows_module.dict_row = object()
sys.modules.setdefault("psycopg.rows", rows_module)
json_module = types.ModuleType("psycopg.types.json")
json_module.Jsonb = lambda value: value
sys.modules.setdefault("psycopg.types", types.ModuleType("psycopg.types"))
sys.modules.setdefault("psycopg.types.json", json_module)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_config(extras: Optional[dict[str, Any]] = None) -> MagicMock:
    """Return a minimal AppConfig mock."""
    config = MagicMock()
    defaults = {
        "agent.calendar.enabled": True,
        "agent.calendar.default_provider": "",
        "agent.calendar.default_alert_minutes": 30,
        "agent.calendar.timezone": "UTC",
        "agent.app.timezone": "UTC",
        "agent.calendar.policy.allow_read_event_details": False,
        "agent.reminders.default_timezone": "UTC",
    }
    if extras:
        defaults.update(extras)

    def _get(key, default=None):
        return defaults.get(key, default)

    def _get_bool(key, default=False):
        val = defaults.get(key, default)
        if isinstance(val, bool):
            return val
        return str(val).lower() in ("1", "true", "yes")

    def _get_int(key, default=0):
        return int(defaults.get(key, default))

    config.get = _get
    config.get_bool = _get_bool
    config.get_int = _get_int
    return config


def _make_db(managed_rows: Optional[list[dict]] = None) -> MagicMock:
    """Return a minimal DB mock with calendar table helpers pre-wired."""
    db = MagicMock()
    db.fetch_all.return_value = managed_rows or []
    db.fetch_one.return_value = None
    db.execute.return_value = None
    return db


def _make_job(job_id: int = 1) -> dict[str, Any]:
    return {"id": job_id, "thread_id": "thread-001"}


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc) if iso.endswith("Z") else datetime.fromisoformat(iso)


# ---------------------------------------------------------------------------
# calendar_configured() tests
# ---------------------------------------------------------------------------


class TestCalendarConfigured:
    """calendar_configured() must require calendar.enabled plus a provider client ID."""

    def setup_method(self):
        # Clear any leftover env vars before each test.
        for key in (
            "CALENDAR_GOOGLE_CLIENT_ID",
            "CALENDAR_OUTLOOK_CLIENT_ID",
            "CALENDAR_ZOHO_CLIENT_ID",
        ):
            os.environ.pop(key, None)

    def teardown_method(self):
        for key in (
            "CALENDAR_GOOGLE_CLIENT_ID",
            "CALENDAR_OUTLOOK_CLIENT_ID",
            "CALENDAR_ZOHO_CLIENT_ID",
        ):
            os.environ.pop(key, None)

    def test_disabled_by_config(self):
        from assistant_agent.validation import calendar_configured

        config = _make_config({"agent.calendar.enabled": False})
        assert calendar_configured(config) is False

    def test_enabled_but_no_provider(self):
        from assistant_agent.validation import calendar_configured

        config = _make_config({"agent.calendar.enabled": True})
        assert calendar_configured(config) is False

    def test_google_provider_set(self):
        from assistant_agent.validation import calendar_configured

        os.environ["CALENDAR_GOOGLE_CLIENT_ID"] = "google-client-id"
        config = _make_config({"agent.calendar.enabled": True})
        assert calendar_configured(config) is True

    def test_outlook_provider_set(self):
        from assistant_agent.validation import calendar_configured

        os.environ["CALENDAR_OUTLOOK_CLIENT_ID"] = "outlook-client-id"
        config = _make_config({"agent.calendar.enabled": True})
        assert calendar_configured(config) is True

    def test_zoho_provider_set(self):
        from assistant_agent.validation import calendar_configured

        os.environ["CALENDAR_ZOHO_CLIENT_ID"] = "zoho-client-id"
        config = _make_config({"agent.calendar.enabled": True})
        assert calendar_configured(config) is True

    def test_empty_string_env_not_counted(self):
        from assistant_agent.validation import calendar_configured

        os.environ["CALENDAR_GOOGLE_CLIENT_ID"] = ""
        config = _make_config({"agent.calendar.enabled": True})
        assert calendar_configured(config) is False

    def test_placeholder_value_not_counted(self):
        from assistant_agent.validation import calendar_configured

        os.environ["CALENDAR_GOOGLE_CLIENT_ID"] = "change-me"
        config = _make_config({"agent.calendar.enabled": True})
        assert calendar_configured(config) is False


# ---------------------------------------------------------------------------
# Provider env-var loading tests
# ---------------------------------------------------------------------------


class TestLoadProviderConfigs:
    """_load_provider_configs should discover providers from env vars."""

    GOOGLE_VARS = {
        "CALENDAR_GOOGLE_CLIENT_ID": "g-id",
        "CALENDAR_GOOGLE_CLIENT_SECRET": "g-secret",
        "CALENDAR_GOOGLE_REFRESH_TOKEN": "g-refresh",
    }
    OUTLOOK_VARS = {
        "CALENDAR_OUTLOOK_CLIENT_ID": "o-id",
        "CALENDAR_OUTLOOK_CLIENT_SECRET": "o-secret",
        "CALENDAR_OUTLOOK_REFRESH_TOKEN": "o-refresh",
    }

    def setup_method(self):
        for key in list(os.environ):
            if key.startswith("CALENDAR_"):
                os.environ.pop(key, None)

    teardown_method = setup_method

    def test_no_env_vars_returns_empty(self):
        from assistant_agent.calendar.router import _load_provider_configs

        assert _load_provider_configs(_make_config()) == []

    def test_google_provider_loaded(self):
        from assistant_agent.calendar.router import _load_provider_configs

        os.environ.update(self.GOOGLE_VARS)
        configs = _load_provider_configs(_make_config())
        assert len(configs) == 1
        assert configs[0].name == "google"
        assert configs[0].provider_type == "google"

    def test_outlook_provider_loaded(self):
        from assistant_agent.calendar.router import _load_provider_configs

        os.environ.update(self.OUTLOOK_VARS)
        configs = _load_provider_configs(_make_config())
        assert len(configs) == 1
        assert configs[0].name == "outlook"

    def test_multiple_providers_loaded(self):
        from assistant_agent.calendar.router import _load_provider_configs

        os.environ.update(self.GOOGLE_VARS)
        os.environ.update(self.OUTLOOK_VARS)
        configs = _load_provider_configs(_make_config())
        names = [c.name for c in configs]
        assert "google" in names
        assert "outlook" in names

    def test_numbered_suffix_creates_extra_instance(self):
        from assistant_agent.calendar.router import _load_provider_configs

        os.environ.update(self.GOOGLE_VARS)
        os.environ.update({
            "CALENDAR_GOOGLE_2_CLIENT_ID": "g2-id",
            "CALENDAR_GOOGLE_2_CLIENT_SECRET": "g2-secret",
            "CALENDAR_GOOGLE_2_REFRESH_TOKEN": "g2-refresh",
        })
        configs = _load_provider_configs(_make_config())
        names = [c.name for c in configs]
        assert "google" in names
        assert "google_2" in names

    def test_incomplete_google_vars_ignored(self):
        from assistant_agent.calendar.router import _load_provider_configs

        os.environ["CALENDAR_GOOGLE_CLIENT_ID"] = "g-id"
        # Missing CLIENT_SECRET and REFRESH_TOKEN
        assert _load_provider_configs(_make_config()) == []

    def test_zoho_api_base_stored_in_extra(self):
        from assistant_agent.calendar.router import _load_provider_configs

        os.environ.update({
            "CALENDAR_ZOHO_CLIENT_ID": "z-id",
            "CALENDAR_ZOHO_CLIENT_SECRET": "z-secret",
            "CALENDAR_ZOHO_REFRESH_TOKEN": "z-refresh",
        })
        configs = _load_provider_configs(_make_config({
            "agent.calendar.zoho.api_base": "https://calendar.zoho.eu/api/v1",
        }))
        assert len(configs) == 1
        assert configs[0].extra.get("api_base") == "https://calendar.zoho.eu/api/v1"


# ---------------------------------------------------------------------------
# CalendarRouter construction
# ---------------------------------------------------------------------------


def _make_router_with_mock_provider(provider_mock=None, env_vars: Optional[dict] = None):
    """Build a CalendarRouter with a mocked provider and DB."""
    from assistant_agent.calendar.router import CalendarRouter

    # Reset class-level token store so tests don't bleed.
    CalendarRouter._token_store = None

    default_env = {
        "CALENDAR_GOOGLE_CLIENT_ID": "test-id",
        "CALENDAR_GOOGLE_CLIENT_SECRET": "test-secret",
        "CALENDAR_GOOGLE_REFRESH_TOKEN": "test-refresh",
    }
    if env_vars is not None:
        default_env = env_vars

    if provider_mock is None:
        provider_mock = MagicMock()

    db = _make_db()
    config = _make_config()
    job = _make_job()

    with patch.dict(os.environ, default_env, clear=False):
        # Clear all CALENDAR_ vars first to isolate test.
        env_backup = {k: v for k, v in os.environ.items() if k.startswith("CALENDAR_")}
        for k in env_backup:
            del os.environ[k]
        os.environ.update(default_env)
        with patch(
            "assistant_agent.calendar.router.build_provider",
            return_value=provider_mock,
        ):
            router = CalendarRouter(db, config, job)

    return router, db, config, job, provider_mock


class TestCalendarRouterConstruction:
    def setup_method(self):
        from assistant_agent.calendar.router import CalendarRouter
        CalendarRouter._token_store = None

    def test_router_registers_provider(self):
        router, *_ = _make_router_with_mock_provider()
        assert "google" in router._providers

    def test_router_with_no_providers_empty(self):
        from assistant_agent.calendar.router import CalendarRouter
        CalendarRouter._token_store = None

        db = _make_db()
        config = _make_config()
        job = _make_job()

        with patch.dict(os.environ, {}, clear=True):
            router = CalendarRouter(db, config, job)
        assert router._providers == {}

    def test_default_provider_from_env(self):
        from assistant_agent.calendar.router import CalendarRouter
        CalendarRouter._token_store = None

        db = _make_db()
        config = _make_config()
        job = _make_job()

        env = {
            "CALENDAR_GOOGLE_CLIENT_ID": "gid",
            "CALENDAR_GOOGLE_CLIENT_SECRET": "gs",
            "CALENDAR_GOOGLE_REFRESH_TOKEN": "gr",
            "CALENDAR_DEFAULT_PROVIDER": "google",
        }
        mock_prov = MagicMock()
        with patch.dict(os.environ, env, clear=False):
            with patch("assistant_agent.calendar.router.build_provider", return_value=mock_prov):
                router = CalendarRouter(db, config, job)
        assert router._default_provider_name == "google"


# ---------------------------------------------------------------------------
# CalendarRouter.sync()
# ---------------------------------------------------------------------------


class TestCalendarRouterSync:
    def setup_method(self):
        from assistant_agent.calendar.router import CalendarRouter
        CalendarRouter._token_store = None

    def test_sync_no_providers(self):
        from assistant_agent.calendar.router import CalendarRouter
        CalendarRouter._token_store = None

        db = _make_db()
        config = _make_config()
        job = _make_job()
        with patch.dict(os.environ, {}, clear=True):
            router = CalendarRouter(db, config, job)
        result = router.sync("test")
        assert result["status"] == "no_providers"

    def test_sync_ok(self):
        mock_prov = MagicMock()
        mock_prov.list_calendars.return_value = []
        router, *_ = _make_router_with_mock_provider(mock_prov)
        result = router.sync("test")
        assert result["status"] == "ok"
        assert any(r["status"] == "ok" for r in result["providers"])

    def test_sync_auth_error_reported(self):
        from assistant_agent.calendar.provider import ProviderAuthError

        mock_prov = MagicMock()
        mock_prov.list_calendars.side_effect = ProviderAuthError("token expired")
        router, *_ = _make_router_with_mock_provider(mock_prov)
        result = router.sync("test")
        assert result["status"] == "degraded"
        assert result["providers"][0]["status"] == "auth_error"


# ---------------------------------------------------------------------------
# CalendarRouter.list_busy()
# ---------------------------------------------------------------------------


class TestCalendarRouterListBusy:
    def setup_method(self):
        from assistant_agent.calendar.router import CalendarRouter
        CalendarRouter._token_store = None

    def _busy_window(self, title="Meeting", managed_id=None):
        from assistant_agent.calendar.models import BusyWindow

        return BusyWindow(
            start=datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc),
            end=datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc),
            title=title,
            provider_name="google",
            calendar_id="primary",
            all_day=False,
            assistant_id=managed_id,
        )

    def test_list_busy_returns_window(self):
        mock_prov = MagicMock()
        mock_prov.get_free_busy.return_value = [self._busy_window()]
        router, db, *_ = _make_router_with_mock_provider(mock_prov)
        db.fetch_all.return_value = []  # no managed events

        result = router.list_busy("2026-09-10T00:00:00+00:00", "2026-09-10T23:59:00+00:00")
        assert len(result["busy"]) == 1

    def test_list_busy_redacts_non_managed_title(self):
        mock_prov = MagicMock()
        mock_prov.get_free_busy.return_value = [self._busy_window("Private Meeting")]
        router, db, *_ = _make_router_with_mock_provider(mock_prov)
        db.fetch_all.return_value = []

        result = router.list_busy("2026-09-10T00:00:00+00:00", "2026-09-10T23:59:00+00:00")
        assert result["busy"][0]["title"] == "Busy"

    def test_list_busy_shows_managed_title(self):
        aid = str(uuid.uuid4())
        mock_prov = MagicMock()
        mock_prov.get_free_busy.return_value = [self._busy_window("Team Standup", managed_id=aid)]
        router, db, *_ = _make_router_with_mock_provider(mock_prov)
        db.fetch_all.side_effect = [
            [{"assistant_id": aid}],
            [{"assistant_id": aid, "provider_name": "google", "provider_event_id": "prov-evt-001"}],
        ]


        result = router.list_busy("2026-09-10T00:00:00+00:00", "2026-09-10T23:59:00+00:00")
        assert result["busy"][0]["title"] == "Team Standup"
        assert result["busy"][0]["managed"] is True

    def test_list_busy_matches_managed_event_by_provider_id(self):
        aid = str(uuid.uuid4())
        from assistant_agent.calendar.models import BusyWindow

        window = BusyWindow(
            start=datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc),
            end=datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc),
            title="Busy",
            provider_name="google",
            calendar_id="primary",
            all_day=False,
            assistant_id=None,
            provider_event_id="prov-evt-001",
        )
        mock_prov = MagicMock()
        mock_prov.get_free_busy.return_value = [window]
        router, db, *_ = _make_router_with_mock_provider(mock_prov)
        db.fetch_all.side_effect = [
            [{"assistant_id": aid}],
            [{"assistant_id": aid, "provider_name": "google", "provider_event_id": "prov-evt-001"}],
        ]

        result = router.list_busy("2026-09-10T00:00:00+00:00", "2026-09-10T23:59:00+00:00")
        assert result["busy"][0]["managed"] is True
        assert result["busy"][0]["event_id"] == aid


    def test_list_busy_invalid_range_raises(self):
        router, *_ = _make_router_with_mock_provider()
        from assistant_agent.calendar.provider import CalendarError

        with pytest.raises(CalendarError):
            router.list_busy("2026-09-10T10:00:00+00:00", "2026-09-10T09:00:00+00:00")

    def test_list_busy_provider_error_skipped(self):
        from assistant_agent.calendar.provider import CalendarError

        mock_prov = MagicMock()
        mock_prov.get_free_busy.side_effect = CalendarError("provider down")
        router, db, *_ = _make_router_with_mock_provider(mock_prov)
        db.fetch_all.return_value = []

        result = router.list_busy("2026-09-10T00:00:00+00:00", "2026-09-10T23:59:00+00:00")
        assert result["busy"] == []
        assert result["status"] == "degraded"
        assert result["complete"] is False
        assert result["provider_errors"][0]["status"] == "error"

    def test_empty_healthy_calendar_is_complete_but_auth_failure_is_not(self):
        from assistant_agent.calendar.provider import ProviderAuthError

        mock_prov = MagicMock()
        mock_prov.get_free_busy.return_value = []
        router, db, *_ = _make_router_with_mock_provider(mock_prov)
        db.fetch_all.return_value = []
        result = router.list_busy("2026-10-09T00:00:00+00:00", "2026-10-10T00:00:00+00:00")
        assert result["complete"] is True
        assert result["status"] == "ok"
        mock_prov.get_free_busy.side_effect = ProviderAuthError("PRIVATE-TOKEN-CANARY")
        result = router.list_busy("2026-10-09T00:00:00+00:00", "2026-10-10T00:00:00+00:00")
        assert result["complete"] is False
        assert result["provider_errors"][0]["status"] == "auth_error"
        assert "PRIVATE-TOKEN-CANARY" not in str(result)

    def test_no_providers_is_not_verified_availability(self):
        router, db, *_ = _make_router_with_mock_provider()
        router._providers.clear()
        db.fetch_all.return_value = []
        result = router.list_busy("2026-10-09T00:00:00+00:00", "2026-10-10T00:00:00+00:00")
        assert result["status"] == "no_providers"
        assert result["complete"] is False

    def test_healthy_windows_survive_another_provider_failure(self):
        from assistant_agent.calendar.provider import ProviderAuthError

        healthy = MagicMock()
        healthy.get_free_busy.return_value = [self._busy_window()]
        router, db, *_ = _make_router_with_mock_provider(healthy)
        failed = MagicMock()
        failed.get_free_busy.side_effect = ProviderAuthError("expired")
        router._providers["zoho"] = failed
        router._provider_configs["zoho"] = next(iter(router._provider_configs.values()))
        db.fetch_all.return_value = []
        result = router.list_busy("2026-10-09T00:00:00+00:00", "2026-10-10T00:00:00+00:00")
        assert len(result["busy"]) == 1
        assert result["complete"] is False
        assert result["provider_errors"][0]["provider"] == "zoho"


# ---------------------------------------------------------------------------
# CalendarRouter.create_event()
# ---------------------------------------------------------------------------


class TestCalendarRouterCreateEvent:
    def setup_method(self):
        from assistant_agent.calendar.router import CalendarRouter
        CalendarRouter._token_store = None

    def _created_event(self, provider_event_id="prov-evt-001"):
        mock_event = MagicMock()
        mock_event.provider_event_id = provider_event_id
        mock_event.start = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)
        mock_event.end = datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc)
        return mock_event

    def _make_router_with_db_insert(self, provider_event_id="prov-evt-001"):
        mock_prov = MagicMock()
        mock_prov.create_event.return_value = self._created_event(provider_event_id)
        router, db, config, job, _ = _make_router_with_mock_provider(mock_prov)

        # Simulate the INSERT ... RETURNING row.
        inserted_row = {
            "assistant_id": str(uuid.uuid4()),
            "provider_name": "google",
            "provider_event_id": provider_event_id,
            "calendar_id": "primary",
            "summary": "Test Event",
            "starts_at": datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc),
            "ends_at": datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc),
            "status": "active",
            "metadata": {},
        }
        db.fetch_one.return_value = inserted_row
        db.fetch_all.return_value = []
        return router, db, mock_prov

    def test_create_event_calls_provider(self):
        router, db, mock_prov = self._make_router_with_db_insert()
        result = router.create_event(
            title="Test Event",
            start="2026-09-15T14:00:00+00:00",
            end="2026-09-15T15:00:00+00:00",
        )
        mock_prov.create_event.assert_called_once()
        assert result["event"]["title"] == "Test Event"

    def test_create_event_inserts_db_row(self):
        router, db, _ = self._make_router_with_db_insert()
        router.create_event(
            title="DB Test",
            start="2026-09-15T14:00:00+00:00",
            end="2026-09-15T15:00:00+00:00",
        )
        db.fetch_one.assert_called()

    def test_create_event_empty_title_raises(self):
        from assistant_agent.calendar.provider import CalendarError

        router, *_ = _make_router_with_mock_provider()
        with pytest.raises(CalendarError, match="title is required"):
            router.create_event(title="", start="2026-09-15T14:00:00+00:00", end="2026-09-15T15:00:00+00:00")

    def test_create_event_idempotency_reuse(self):
        ikey = "event-ikey-001"
        existing_row = {
            "assistant_id": "existing-id",
            "provider_name": "google",
            "provider_event_id": "prov-001",
            "calendar_id": "primary",
            "summary": "Existing",
            "starts_at": datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc),
            "ends_at": datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc),
            "status": "active",
            "metadata": {"idempotency_key": ikey},
        }
        mock_prov = MagicMock()
        router, db, config, job, _ = _make_router_with_mock_provider(mock_prov)
        db.fetch_one.return_value = existing_row
        db.fetch_all.return_value = []

        result = router.create_event(
            title="Any Title",
            start="2026-09-15T14:00:00+00:00",
            end="2026-09-15T15:00:00+00:00",
            idempotency_key=ikey,
        )
        assert result.get("idempotent_reuse") is True
        mock_prov.create_event.assert_not_called()

    def test_create_event_unknown_provider_raises(self):
        from assistant_agent.calendar.provider import CalendarError

        router, db, _ = self._make_router_with_db_insert()
        with pytest.raises(CalendarError):
            router.create_event(
                title="Routed Wrong",
                start="2026-09-15T14:00:00+00:00",
                end="2026-09-15T15:00:00+00:00",
                provider="nonexistent",
            )


# ---------------------------------------------------------------------------
# Timezone parsing for timed events and busy ranges
# ---------------------------------------------------------------------------


class TestCalendarRouterTimezone:
    def test_all_day_dates_survive_provider_round_trips(self):
        from zoneinfo import ZoneInfo
        from assistant_agent.calendar.providers.google import _dt_to_google, _google_to_dt
        from assistant_agent.calendar.providers.outlook import _dt_to_graph, _graph_to_dt
        from assistant_agent.calendar.providers.zoho import _dt_to_zoho, _zoho_to_dt
        for zone in ("Asia/Dubai", "America/New_York"):
            local_start = datetime(2026, 10, 8, tzinfo=ZoneInfo(zone))
            google = _dt_to_google(local_start, True)
            assert google["date"] == "2026-10-08"
            assert _google_to_dt(google, zone)[0] == local_start
            graph = _dt_to_graph(local_start, True)
            assert graph["dateTime"] == "2026-10-08T00:00:00"
            assert graph["timeZone"] == zone
            assert _graph_to_dt(graph) == local_start.astimezone(timezone.utc)
            zoho = _dt_to_zoho(local_start, True)
            assert zoho == "20261008"
            assert _zoho_to_dt(zoho, zone)[0] == local_start

    def test_router_retains_local_midnight_for_all_day_provider_requests(self):
        router, _, _ = self._router_with_tz({"agent.app.timezone": "Asia/Dubai"})
        start = router._event_start("2026-10-08", True)
        assert start.date().isoformat() == "2026-10-08"
        assert start.hour == 0
        assert start.utcoffset().total_seconds() == 14400

    def setup_method(self):
        from assistant_agent.calendar.router import CalendarRouter
        CalendarRouter._token_store = None

    def _router_with_tz(self, extras: dict[str, Any], provider_mock=None):
        router, db, _, _, mock_prov = _make_router_with_mock_provider(provider_mock)
        router._config = _make_config(extras)
        db.fetch_all.return_value = []
        return router, db, mock_prov

    def test_timed_event_naive_start_uses_assistant_timezone(self):
        mock_event = MagicMock()
        mock_event.provider_event_id = "prov-evt-tz"
        mock_event.start = datetime(2026, 6, 4, 13, 0, tzinfo=timezone.utc)
        mock_event.end = datetime(2026, 6, 4, 14, 0, tzinfo=timezone.utc)
        mock_prov = MagicMock()
        mock_prov.create_event.return_value = mock_event
        router, db, _ = self._router_with_tz(
            {
                "agent.calendar.timezone": "America/New_York",
                "agent.app.timezone": "Asia/Dubai",
                "agent.reminders.default_timezone": "UTC",
            },
            mock_prov,
        )
        db.fetch_one.return_value = {
            "assistant_id": str(uuid.uuid4()),
            "provider_name": "google",
            "provider_event_id": "prov-evt-tz",
            "calendar_id": "primary",
            "summary": "TZ Event",
            "starts_at": datetime(2026, 6, 4, 13, 0, tzinfo=timezone.utc),
            "ends_at": datetime(2026, 6, 4, 14, 0, tzinfo=timezone.utc),
            "status": "active",
            "metadata": {},
        }

        router.create_event(
            title="TZ Event",
            start="2026-06-04T17:00:00",
            end="2026-06-04T18:00:00",
        )
        request = mock_prov.create_event.call_args.args[0]
        assert request.start == datetime(2026, 6, 4, 13, 0, tzinfo=timezone.utc)
        assert request.end == datetime(2026, 6, 4, 14, 0, tzinfo=timezone.utc)

    def test_list_busy_naive_range_uses_assistant_timezone(self):
        mock_prov = MagicMock()
        mock_prov.get_free_busy.return_value = []
        router, _, mock_prov = self._router_with_tz(
            {
                "agent.calendar.timezone": "America/New_York",
                "agent.app.timezone": "Asia/Dubai",
                "agent.reminders.default_timezone": "UTC",
            },
            mock_prov,
        )

        result = router.list_busy("2026-06-04T17:00:00", "2026-06-04T18:00:00")
        kwargs = mock_prov.get_free_busy.call_args.kwargs
        assert kwargs["start"] == datetime(2026, 6, 4, 13, 0, tzinfo=timezone.utc)
        assert kwargs["end"] == datetime(2026, 6, 4, 14, 0, tzinfo=timezone.utc)
        assert result["start"] == "2026-06-04T17:00:00+04:00"
        assert result["end"] == "2026-06-04T18:00:00+04:00"

    def test_explicit_offset_still_honored(self):
        mock_prov = MagicMock()
        mock_prov.get_free_busy.return_value = []
        router, _, mock_prov = self._router_with_tz(
            {
                "agent.calendar.timezone": "UTC",
                "agent.app.timezone": "UTC",
            },
            mock_prov,
        )

        router.list_busy("2026-06-04T17:00:00+04:00", "2026-06-04T18:00:00+04:00")
        kwargs = mock_prov.get_free_busy.call_args.kwargs
        assert kwargs["start"] == datetime(2026, 6, 4, 13, 0, tzinfo=timezone.utc)
        assert kwargs["end"] == datetime(2026, 6, 4, 14, 0, tzinfo=timezone.utc)

    def test_zulu_suffix_still_honored(self):
        mock_prov = MagicMock()
        mock_prov.get_free_busy.return_value = []
        router, _, mock_prov = self._router_with_tz(
            {
                "agent.calendar.timezone": "Asia/Dubai",
                "agent.app.timezone": "",
            },
            mock_prov,
        )

        router.list_busy("2026-06-04T17:00:00Z", "2026-06-04T18:00:00Z")
        kwargs = mock_prov.get_free_busy.call_args.kwargs
        assert kwargs["start"] == datetime(2026, 6, 4, 17, 0, tzinfo=timezone.utc)
        assert kwargs["end"] == datetime(2026, 6, 4, 18, 0, tzinfo=timezone.utc)

    def test_falls_back_to_app_timezone_when_calendar_timezone_unset(self):
        mock_prov = MagicMock()
        mock_prov.get_free_busy.return_value = []
        router, _, mock_prov = self._router_with_tz(
            {
                "agent.calendar.timezone": "",
                "agent.app.timezone": "Asia/Dubai",
                "agent.reminders.default_timezone": "UTC",
            },
            mock_prov,
        )

        router.list_busy("2026-06-04T17:00:00", "2026-06-04T18:00:00")
        kwargs = mock_prov.get_free_busy.call_args.kwargs
        assert kwargs["start"] == datetime(2026, 6, 4, 13, 0, tzinfo=timezone.utc)
        assert kwargs["end"] == datetime(2026, 6, 4, 14, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# CalendarRouter.update_event()
# ---------------------------------------------------------------------------


class TestCalendarRouterUpdateEvent:
    def setup_method(self):
        from assistant_agent.calendar.router import CalendarRouter
        CalendarRouter._token_store = None

    def test_update_event_not_found_raises(self):
        from assistant_agent.calendar.provider import CalendarError

        mock_prov = MagicMock()
        router, db, *_ = _make_router_with_mock_provider(mock_prov)
        db.fetch_one.return_value = None  # managed_row returns None
        db.fetch_all.return_value = []

        with pytest.raises(CalendarError, match="not found"):
            router.update_event(event_id="nonexistent-id", title="New Title")

    def test_update_event_calls_provider(self):
        aid = str(uuid.uuid4())
        existing_provider_event = MagicMock()
        existing_provider_event.start = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)
        existing_provider_event.end = datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc)
        existing_provider_event.all_day = False
        existing_provider_event.title = "Old Title"

        updated_provider_event = MagicMock()
        updated_provider_event.start = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)
        updated_provider_event.end = datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc)

        managed_row = {
            "assistant_id": aid,
            "provider_name": "google",
            "provider_event_id": "prov-evt-001",
            "calendar_id": "primary",
            "summary": "Old Title",
            "starts_at": datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc),
            "ends_at": datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc),
            "status": "active",
            "metadata": {},
        }
        updated_row = dict(managed_row, summary="New Title")

        mock_prov = MagicMock()
        mock_prov.get_event.return_value = existing_provider_event
        mock_prov.update_event.return_value = updated_provider_event
        router, db, *_ = _make_router_with_mock_provider(mock_prov)
        # First fetch_one for managed_row, second for update RETURNING.
        db.fetch_one.side_effect = [managed_row, updated_row]
        db.fetch_all.return_value = []

        result = router.update_event(event_id=aid, title="New Title")
        mock_prov.update_event.assert_called_once()
        assert result["event"]["title"] == "New Title"

    def test_update_event_recovers_when_provider_already_has_requested_state(self):
        from assistant_agent.calendar.models import CalendarEvent

        aid = str(uuid.uuid4())
        starts_at = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)
        ends_at = datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc)
        managed_row = {
            "assistant_id": aid,
            "provider_name": "google",
            "provider_event_id": "prov-evt-001",
            "calendar_id": "primary",
            "summary": "Old Title",
            "starts_at": starts_at,
            "ends_at": ends_at,
            "status": "active",
            "metadata": {},
        }
        updated_row = dict(managed_row, summary="New Title")
        provider_event = CalendarEvent(
            provider_event_id="prov-evt-001",
            provider_name="google",
            calendar_id="primary",
            title="New Title",
            start=starts_at,
            end=ends_at,
        )
        mock_prov = MagicMock()
        mock_prov.get_event.return_value = provider_event
        router, db, *_ = _make_router_with_mock_provider(mock_prov)
        db.fetch_one.side_effect = [managed_row, updated_row]
        db.fetch_all.return_value = []

        result = router.update_event(event_id=aid, title="New Title")

        mock_prov.update_event.assert_not_called()
        assert result["event"]["title"] == "New Title"


# ---------------------------------------------------------------------------
# CalendarRouter.delete_event()
# ---------------------------------------------------------------------------


class TestCalendarRouterDeleteEvent:
    def setup_method(self):
        from assistant_agent.calendar.router import CalendarRouter
        CalendarRouter._token_store = None

    def test_delete_event_not_found_raises(self):
        from assistant_agent.calendar.provider import CalendarError

        mock_prov = MagicMock()
        router, db, *_ = _make_router_with_mock_provider(mock_prov)
        db.fetch_one.return_value = None
        db.fetch_all.return_value = []

        with pytest.raises(CalendarError, match="not found"):
            router.delete_event("nonexistent")

    def test_delete_event_calls_provider(self):
        from assistant_agent.calendar.models import CalendarEvent

        aid = str(uuid.uuid4())
        managed_row = {
            "assistant_id": aid,
            "provider_name": "google",
            "provider_event_id": "prov-evt-001",
            "calendar_id": "primary",
            "summary": "To Delete",
            "starts_at": datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc),
            "ends_at": datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc),
            "status": "active",
            "metadata": {},
        }
        deleted_row = dict(managed_row, status="deleted")

        mock_prov = MagicMock()
        mock_prov.get_event.return_value = CalendarEvent(
            provider_event_id="prov-evt-001",
            provider_name="google",
            calendar_id="primary",
            title="To Delete",
            start=managed_row["starts_at"],
            end=managed_row["ends_at"],
        )
        router, db, *_ = _make_router_with_mock_provider(mock_prov)
        db.fetch_one.side_effect = [managed_row, deleted_row]
        db.fetch_all.return_value = []

        result = router.delete_event(aid)
        mock_prov.delete_event.assert_called_once_with("primary", "prov-evt-001")
        assert result["event"]["status"] == "deleted"

    def test_delete_event_already_gone_on_provider_marks_deleted(self):
        """ProviderEventNotFoundError should be swallowed; DB still updated."""
        from assistant_agent.calendar.models import CalendarEvent
        from assistant_agent.calendar.provider import ProviderEventNotFoundError

        aid = str(uuid.uuid4())
        managed_row = {
            "assistant_id": aid,
            "provider_name": "google",
            "provider_event_id": "prov-evt-gone",
            "calendar_id": "primary",
            "summary": "Gone Event",
            "starts_at": datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc),
            "ends_at": datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc),
            "status": "active",
            "metadata": {},
        }
        deleted_row = dict(managed_row, status="deleted")

        mock_prov = MagicMock()
        mock_prov.get_event.return_value = CalendarEvent(
            provider_event_id="prov-evt-gone",
            provider_name="google",
            calendar_id="primary",
            title="Gone Event",
            start=managed_row["starts_at"],
            end=managed_row["ends_at"],
        )
        mock_prov.delete_event.side_effect = ProviderEventNotFoundError("already deleted")
        router, db, *_ = _make_router_with_mock_provider(mock_prov)
        db.fetch_one.side_effect = [managed_row, deleted_row]
        db.fetch_all.return_value = []

        result = router.delete_event(aid)
        assert result["event"]["status"] == "deleted"


# ---------------------------------------------------------------------------
# tool_status() calendar_tools section
# ---------------------------------------------------------------------------


class TestToolStatusCalendarTools:
    def setup_method(self):
        for key in list(os.environ):
            if key.startswith("CALENDAR_"):
                os.environ.pop(key, None)

    teardown_method = setup_method

    def test_calendar_tools_unavailable_when_no_provider(self):
        from assistant_agent.validation import tool_status

        config = _make_config({"agent.calendar.enabled": True})
        status = tool_status(config)
        assert status["calendar_tools"]["available"] is False
        assert status["calendar_tools"]["tools"] == []

    def test_calendar_tools_available_with_google_provider(self):
        from assistant_agent.validation import tool_status

        os.environ["CALENDAR_GOOGLE_CLIENT_ID"] = "gid"
        config = _make_config({"agent.calendar.enabled": True})
        status = tool_status(config)
        assert status["calendar_tools"]["available"] is True
        assert "calendar_create_event" in status["calendar_tools"]["tools"]

    def test_calendar_tools_providers_list(self):
        from assistant_agent.validation import tool_status

        os.environ["CALENDAR_GOOGLE_CLIENT_ID"] = "gid"
        os.environ["CALENDAR_ZOHO_CLIENT_ID"] = "zid"
        config = _make_config({"agent.calendar.enabled": True})
        status = tool_status(config)
        assert "google" in status["calendar_tools"]["providers"]
        assert "zoho" in status["calendar_tools"]["providers"]
        assert "outlook" not in status["calendar_tools"]["providers"]

    def test_calendar_tools_include_list_calendars(self):
        from assistant_agent.validation import tool_status

        os.environ["CALENDAR_GOOGLE_CLIENT_ID"] = "gid"
        config = _make_config({"agent.calendar.enabled": True})
        status = tool_status(config)
        assert "calendar_list_calendars" in status["calendar_tools"]["tools"]

    def test_calendar_tools_exposes_provider_status_not_local_store(self):
        from assistant_agent.validation import tool_status

        os.environ["CALENDAR_GOOGLE_CLIENT_ID"] = "gid"
        config = _make_config({"agent.calendar.enabled": True})
        status = tool_status(config)
        cal = status["calendar_tools"]
        assert "store" not in cal
        assert "sync" not in cal
        assert "providers" in cal
        assert "default_provider" in cal
        assert "default_alert_minutes" in cal
