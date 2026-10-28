"""Telegram settings helpers with no runtime dependencies (used by setup)."""
import json
from typing import Any

from .config import AppConfig


class TelegramError(ValueError):
    def __init__(self, message: str, *, uncertain: bool = False, retry_after: int = 0):
        super().__init__(message)
        self.uncertain = uncertain
        self.retry_after = retry_after


def telegram_configured(config: AppConfig) -> bool:
    identifiers = config.get_list("agent.telegram.allowed_user_ids")
    return bool(config.get_bool("agent.telegram.enabled", False) and config.get("agent.telegram.bot_token")
                and identifiers and all(identifier.isdigit() and int(identifier) > 0 for identifier in identifiers))


def telegram_identities(config: AppConfig) -> dict[str, Any]:
    value = config.get("agent.telegram.recipients", {}) or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise TelegramError("Telegram recipient mappings must be a JSON object") from None
    if not isinstance(value, dict):
        raise TelegramError("Telegram recipient mappings must be an object")
    return {str(address).lower(): identifier for address, identifier in value.items()}
