from datetime import datetime, timedelta, timezone

from .models import ProviderConfig
from .provider import CalendarError, ProviderAuthError
from .providers import build_provider
from .token_store import TokenStore


def verify_provider(config: ProviderConfig) -> dict[str, str]:
    try:
        provider = build_provider(config, TokenStore(None))
        provider.list_calendars()
        start = datetime.now(timezone.utc)
        provider.list_events(config.calendar_id, start, start + timedelta(days=1))
    except ProviderAuthError as error:
        detail = "Authentication failed. Verify the regional OAuth client and refresh token."
        if "INVALID_OAUTHSCOPE" in str(error):
            detail = "INVALID_OAUTHSCOPE: re-authorize with ZohoCalendar.event.ALL and ZohoCalendar.calendar.READ; refreshing cannot add consent scopes."
        return {"provider": config.name, "status": "auth_error", "detail": detail}
    except CalendarError:
        return {"provider": config.name, "status": "error", "detail": "Calendar or event access failed; availability is unknown."}
    return {"provider": config.name, "status": "ok", "detail": "Fresh-token calendar and event access verified."}