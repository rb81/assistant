"""
Shared data models for the calendar provider plugin system.

These dataclasses are provider-neutral and form the common language
between the CalendarRouter and all provider implementations.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Calendar:
    """A calendar collection returned by a provider."""
    id: str
    name: str
    provider_name: str
    description: str = ""
    color: str = ""
    is_primary: bool = False
    is_read_only: bool = False


@dataclass
class Attendee:
    """An event attendee."""
    email: str
    name: str = ""
    status: str = "needs-action"   # needs-action | accepted | declined | tentative


@dataclass
class CalendarEvent:
    """
    A calendar event returned by a provider.

    provider_event_id is the native ID used by the provider's API.
    assistant_id is our internal tracking ID (set after create; None for
    externally-created events the agent did not manage).
    """
    provider_event_id: str
    provider_name: str
    calendar_id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    description: str = ""
    location: str = ""
    status: str = "confirmed"          # confirmed | tentative | cancelled
    transparency: str = "opaque"       # opaque | transparent
    attendees: list[Attendee] = field(default_factory=list)
    recurrence: list[str] = field(default_factory=list)   # raw RRULE strings
    html_link: str = ""
    assistant_id: Optional[str] = None  # set when this event is agent-managed

    def blocks_time(self) -> bool:
        return (
            self.status.lower() != "cancelled"
            and self.transparency.lower() != "transparent"
        )


@dataclass
class BusyWindow:
    """A free/busy interval from a provider."""
    start: datetime
    end: datetime
    provider_name: str
    calendar_id: str
    title: str = "Busy"         # may be populated when allow_read_event_details is true
    all_day: bool = False
    assistant_id: Optional[str] = None
    provider_event_id: Optional[str] = None


@dataclass
class CreateEventRequest:
    """Parameters for creating a new event."""
    title: str
    start: datetime
    end: datetime
    calendar_id: str
    all_day: bool = False
    description: str = ""
    location: str = ""
    transparency: str = "opaque"
    attendees: list[str] = field(default_factory=list)   # email strings
    alert_minutes: Optional[int] = None


@dataclass
class UpdateEventRequest:
    """Parameters for updating an existing event. None means «no change»."""
    title: Optional[str] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    all_day: Optional[bool] = None
    description: Optional[str] = None
    location: Optional[str] = None
    transparency: Optional[str] = None
    attendees: Optional[list[str]] = None   # email strings; None = no change
    alert_minutes: Optional[int] = None


@dataclass
class ProviderConfig:
    """
    Runtime configuration for a single provider instance.

    Built from env vars by the config loader — never from agent.yaml directly.
    """
    name: str                  # user-chosen logical name (e.g. "work-google")
    provider_type: str         # "google" | "outlook" | "zoho"
    calendar_id: str           # provider-specific calendar ID
    client_id: str
    client_secret: str
    refresh_token: str
    extra: dict[str, Any] = field(default_factory=dict)
