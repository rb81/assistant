"""
Calendar provider implementations.

Use build_provider() as the factory — it returns the correct provider instance
for a given ProviderConfig, wired to the shared TokenStore.
"""

from typing import Any

from ..models import ProviderConfig
from ..provider import CalendarError, CalendarProvider
from ..token_store import TokenStore
from .google import GoogleCalendarProvider
from .outlook import OutlookCalendarProvider
from .zoho import ZohoCalendarProvider


_PROVIDER_CLASSES: dict[str, type] = {
    "google": GoogleCalendarProvider,
    "outlook": OutlookCalendarProvider,
    "zoho": ZohoCalendarProvider,
}

SUPPORTED_PROVIDER_TYPES: list[str] = sorted(_PROVIDER_CLASSES.keys())


def build_provider(config: ProviderConfig, token_store: TokenStore) -> CalendarProvider:
    """
    Instantiate the correct provider for the given ProviderConfig.

    Raises CalendarError for unknown provider types.
    """
    cls = _PROVIDER_CLASSES.get(config.provider_type)
    if cls is None:
        raise CalendarError(
            "unsupported calendar provider type %r — supported types: %s"
            % (config.provider_type, ", ".join(SUPPORTED_PROVIDER_TYPES))
        )
    return cls(config=config, token_store=token_store)  # type: ignore[call-arg]


__all__ = [
    "build_provider",
    "SUPPORTED_PROVIDER_TYPES",
    "GoogleCalendarProvider",
    "OutlookCalendarProvider",
    "ZohoCalendarProvider",
]
