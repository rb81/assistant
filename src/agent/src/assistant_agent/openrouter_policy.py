"""Shared OpenRouter routing policy; compatible third-party APIs receive no OpenRouter fields."""
from urllib.parse import urlsplit


def is_openrouter_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme.lower() == "https" and (parsed.hostname or "").lower() == "openrouter.ai"


def require_zdr(config, *, scoped=False) -> bool:
    return config.get_bool("agent.llm.zdr_only", False) or scoped


def apply_routing_policy(config, url: str, payload: dict) -> None:
    """Enforce routing on every request, including retries, without relaxing an existing policy."""
    if is_openrouter_url(url) and require_zdr(config):
        payload.setdefault("provider", {})["zdr"] = True
