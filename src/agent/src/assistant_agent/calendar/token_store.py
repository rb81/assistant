"""
OAuth2 token management for calendar providers.

Access tokens are short-lived (typically 1 hour). This module:
  - Caches access tokens in memory (keyed by provider name)
  - Persists the latest access token in the runtime_state table
  - Refreshes tokens automatically before they expire
  - Raises ProviderAuthError on unrecoverable auth failures

Token lifetimes per provider:
  Google  — access token valid ~1 hour
  Outlook — access token valid ~1 hour
  Zoho    — access token valid ~1 hour

All providers follow RFC 6749 refresh-token flow with the same endpoint shape:
  POST <token_url>
  grant_type=refresh_token
  &refresh_token=<refresh_token>
  &client_id=<client_id>
  &client_secret=<client_secret>
  → { "access_token": "...", "expires_in": 3600, ... }
"""

import json
import logging
import time
from typing import Any, Optional

import httpx

from .provider import ProviderAuthError

log = logging.getLogger(__name__)

# Refresh access tokens this many seconds before their reported expiry to
# avoid edge-case failures caused by clock skew or slow API calls.
_REFRESH_BUFFER_SECONDS = 120

# Token URL for each supported provider type.
# Note: Outlook token URL is built dynamically to support tenant-scoped endpoints.
_TOKEN_URLS: dict[str, str] = {
    "google": "https://oauth2.googleapis.com/token",
    "zoho": "https://accounts.zoho.com/oauth/v2/token",
}

# Scope requirements for each provider (informational; scopes are granted at
# OAuth consent time, not re-requested at refresh).
REQUIRED_SCOPES: dict[str, list[str]] = {
    "google": ["https://www.googleapis.com/auth/calendar"],
    "outlook": ["Calendars.ReadWrite", "offline_access"],
    "zoho": ["ZohoCalendar.event.ALL", "ZohoCalendar.calendar.READ"],
}


class _CachedToken:
    """In-memory access token entry."""

    def __init__(self, access_token: str, expires_at: float) -> None:
        self.access_token = access_token
        self.expires_at = expires_at

    def is_valid(self) -> bool:
        return time.time() < self.expires_at - _REFRESH_BUFFER_SECONDS


class TokenStore:
    """
    Manages OAuth2 access tokens for all configured calendar providers.

    One TokenStore instance is shared across all providers via the
    CalendarRouter.  Tokens are cached in memory; the db is used to
    persist the latest access token across container restarts (best-effort —
    the token may have expired, in which case a fresh refresh is performed).
    """

    def __init__(self, db: Any) -> None:
        self._db = db
        self._cache: dict[str, _CachedToken] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_access_token(
        self,
        provider_name: str,
        provider_type: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        tenant_id: str = "",
    ) -> str:
        """
        Return a valid access token for the given provider.

        Uses a cached token if still valid; otherwise refreshes automatically.
        Raises ProviderAuthError if the refresh fails.
        """
        cached = self._cache.get(provider_name)
        if cached and cached.is_valid():
            return cached.access_token

        return self._refresh(
            provider_name=provider_name,
            provider_type=provider_type,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            tenant_id=tenant_id,
        )

    def invalidate(self, provider_name: str) -> None:
        """Force re-authentication on the next call (e.g. after a 401)."""
        self._cache.pop(provider_name, None)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _refresh(
        self,
        provider_name: str,
        provider_type: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        tenant_id: str = "",
    ) -> str:
        if provider_type == "outlook":
            tid = tenant_id.strip() if tenant_id else "common"
            token_url: Optional[str] = (
                "https://login.microsoftonline.com/%s/oauth2/v2.0/token" % tid
            )
        else:
            token_url = _TOKEN_URLS.get(provider_type)
        if not token_url:
            raise ProviderAuthError(
                "unknown provider type %r — cannot obtain access token" % provider_type
            )

        payload: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }

        # Zoho requires scope to be re-stated on refresh.
        if provider_type == "zoho":
            payload["scope"] = " ".join(REQUIRED_SCOPES["zoho"])

        log.debug("refreshing OAuth2 access token for provider %r", provider_name)

        try:
            response = httpx.post(
                token_url,
                data=payload,
                timeout=15,
                headers={"Accept": "application/json"},
            )
        except httpx.RequestError as exc:
            raise ProviderAuthError(
                "network error while refreshing token for %r: %s" % (provider_name, exc)
            ) from exc

        if response.status_code != 200:
            raise ProviderAuthError(
                "token refresh failed for %r (HTTP %d); verify the OAuth client and refresh token"
                % (provider_name, response.status_code)
            )

        try:
            data = response.json()
        except Exception as exc:
            raise ProviderAuthError(
                "unparseable token response for %r" % provider_name
            ) from exc

        access_token: Optional[str] = data.get("access_token")
        if not access_token:
            error_code = data.get("error")
            known_errors = {"invalid_client", "invalid_grant", "invalid_code", "invalid_scope", "access_denied"}
            detail = error_code if isinstance(error_code, str) and error_code in known_errors else "missing_access_token"
            raise ProviderAuthError(
                "token response for %r did not contain access_token (%s); verify the OAuth client and refresh token" % (provider_name, detail)
            )

        expires_in: int = int(data.get("expires_in", 3600))
        expires_at = time.time() + expires_in

        self._cache[provider_name] = _CachedToken(access_token, expires_at)
        self._persist(provider_name, access_token, expires_at)

        log.debug(
            "access token refreshed for provider %r (expires in %ds)",
            provider_name,
            expires_in,
        )
        return access_token

    def _persist(self, provider_name: str, access_token: str, expires_at: float) -> None:
        """Best-effort persistence of the access token in runtime_state."""
        if self._db is None:
            return
        key = "calendar_token_%s" % provider_name
        try:
            self._db.execute(
                """
                INSERT INTO runtime_state(key, value)
                VALUES (%s, %s::jsonb)
                ON CONFLICT (key) DO UPDATE
                  SET value = EXCLUDED.value,
                      updated_at = now()
                """,
                (
                    key,
                    json.dumps({"access_token": access_token, "expires_at": expires_at}),
                ),
            )
        except Exception:
            # Token persistence is best-effort; a cold-start refresh will recover.
            log.debug("could not persist access token for provider %r", provider_name)
