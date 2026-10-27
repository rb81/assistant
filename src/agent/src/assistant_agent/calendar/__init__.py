"""
Calendar provider plugin package.

Public API:
  CalendarRouter  — the single bridge used by agent tools
  CalendarError   — base exception raised for expected errors
"""

from typing import TYPE_CHECKING

from .provider import CalendarError, ProviderAuthError, ProviderEventNotFoundError

if TYPE_CHECKING:
  from .router import CalendarRouter


def __getattr__(name: str) -> type["CalendarRouter"]:
  if name == "CalendarRouter":
    from .router import CalendarRouter

    return CalendarRouter
  raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CalendarRouter",
    "CalendarError",
    "ProviderAuthError",
    "ProviderEventNotFoundError",
]
