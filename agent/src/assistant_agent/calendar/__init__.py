"""
Calendar provider plugin package.

Public API:
  CalendarRouter  — the single bridge used by agent tools
  CalendarError   — base exception raised for expected errors
"""

from .provider import CalendarError, ProviderAuthError, ProviderEventNotFoundError
from .router import CalendarRouter

__all__ = [
    "CalendarRouter",
    "CalendarError",
    "ProviderAuthError",
    "ProviderEventNotFoundError",
]
