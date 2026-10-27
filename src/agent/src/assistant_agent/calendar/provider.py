"""
Abstract CalendarProvider protocol.

Every provider implementation (Google, Outlook, Zoho) must satisfy this
interface. The CalendarRouter depends only on this protocol, so new providers
can be added without touching any routing or tool code.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from .models import (
    BusyWindow,
    Calendar,
    CalendarEvent,
    CreateEventRequest,
    UpdateEventRequest,
)


class CalendarError(Exception):
    """Raised by providers and the router for expected, user-visible errors."""


class ProviderAuthError(CalendarError):
    """Raised when a provider cannot authenticate (expired/invalid token)."""


class ProviderEventNotFoundError(CalendarError):
    """Raised when a requested event does not exist on the provider."""


@runtime_checkable
class CalendarProvider(Protocol):
    """
    Uniform interface that every calendar provider must implement.

    Implementations are synchronous (blocking HTTP); the router calls them
    from the same thread as the agent tool call.  All datetime values are
    timezone-aware (UTC or local-with-tzinfo).
    """

    @property
    def provider_name(self) -> str:
        """The logical name of this provider instance (e.g. 'google', 'outlook')."""
        ...

    def list_calendars(self) -> list[Calendar]:
        """
        Return all calendar collections available to this provider.

        Used for diagnostics and provider health checks.
        """
        ...

    def list_events(
        self,
        calendar_id: str,
        start: datetime,
        end: datetime,
    ) -> list[CalendarEvent]:
        """
        Return all events (including recurring occurrences) within [start, end).

        The provider is responsible for expanding recurring events; the router
        does not perform recurrence expansion.
        """
        ...

    def create_event(
        self,
        request: CreateEventRequest,
    ) -> CalendarEvent:
        """
        Create a new event and return the provider's representation.

        The returned CalendarEvent.provider_event_id is stored in the
        managed-events table so the router can route future update/delete calls.
        """
        ...

    def update_event(
        self,
        calendar_id: str,
        provider_event_id: str,
        request: UpdateEventRequest,
        existing: CalendarEvent,
    ) -> CalendarEvent:
        """
        Update an existing event and return the updated representation.

        existing contains the current event state fetched by the router;
        providers may use it to compute patch payloads.
        """
        ...

    def delete_event(
        self,
        calendar_id: str,
        provider_event_id: str,
    ) -> None:
        """Permanently delete an event from the provider."""
        ...

    def get_free_busy(
        self,
        calendar_ids: list[str],
        start: datetime,
        end: datetime,
    ) -> list[BusyWindow]:
        """
        Return busy windows for the given calendars within [start, end).

        Where the provider's free/busy API does not return event titles,
        implementations should leave BusyWindow.title as the default "Busy".
        """
        ...

    def get_event(
        self,
        calendar_id: str,
        provider_event_id: str,
    ) -> CalendarEvent:
        """Fetch a single event by its provider-native ID."""
        ...
