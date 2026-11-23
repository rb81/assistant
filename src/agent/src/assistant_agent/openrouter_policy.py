"""Shared OpenRouter routing policy; compatible third-party APIs receive no OpenRouter fields."""
import re
from urllib.parse import urlsplit


_PROVIDER_SLUG = re.compile(r"[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)?")


def valid_provider_slugs(providers: list[str]) -> bool:
    return all(_PROVIDER_SLUG.fullmatch(name) for name in providers)


def is_openrouter_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme.lower() == "https" and (parsed.hostname or "").lower() == "openrouter.ai"


def require_zdr(config, *, scoped=False) -> bool:
    return config.get_bool("agent.llm.zdr_only", False) or scoped


def require_zdr_eligibility(config, provider: str, model: str, *, scoped=False) -> None:
    """Fail closed unless an administrator has verified both provider policy and exact model."""
    if not require_zdr(config, scoped=scoped):
        return
    if provider == "gemini":
        raise ValueError("Gemini Developer API does not provide strict zero data retention")
    if provider not in {"openrouter", "openai", "anthropic", "cloudflare"}:
        raise ValueError("Provider %s is not verified for strict zero data retention" % provider)
    verified_providers = config.get_list("agent.llm.zdr_verified_providers")
    verified_models = config.get_list("agent.llm.zdr_verified_models")
    if provider not in verified_providers:
        raise ValueError(
            "ZDR-only mode blocks the %s provider until its account retention controls are verified "
            "and listed in AGENT_LLM_ZDR_VERIFIED_PROVIDERS" % provider
        )
    if "%s:%s" % (provider, model) not in verified_models:
        raise ValueError(
            "ZDR-only mode blocks model %r until its retention eligibility is verified and listed "
            "in AGENT_LLM_ZDR_VERIFIED_MODELS" % model
        )
    if provider == "cloudflare" and not model.startswith("@cf/"):
        raise ValueError("ZDR-only mode allows only direct Cloudflare Workers AI models")


def apply_routing_policy(config, url: str, payload: dict, *, scoped_zdr=False) -> None:
    """Enforce routing on every request, including retries, without relaxing an existing policy."""
    if not is_openrouter_url(url):
        return
    if require_zdr(config, scoped=scoped_zdr):
        model = payload.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("OpenRouter ZDR policy requires an exact model identifier")
        require_zdr_eligibility(config, "openrouter", model, scoped=scoped_zdr)
    providers = config.get_list("agent.llm.allowed_providers")
    if not valid_provider_slugs(providers):
        raise ValueError("Invalid OpenRouter provider slug in AGENT_LLM_ALLOWED_PROVIDERS")
    if providers:
        policy = payload.setdefault("provider", {})
        existing = policy.get("only")
        if existing is not None:
            if not isinstance(existing, list) or not all(isinstance(item, str) for item in existing):
                raise ValueError("Invalid existing OpenRouter provider restriction")
            def contains(parent, child):
                return child == parent or child.startswith(parent + "/")
            providers = list(dict.fromkeys(
                left if contains(right, left) else right
                for left in providers for right in existing
                if contains(left, right) or contains(right, left)
            ))
            if not providers:
                raise ValueError("OpenRouter provider restrictions conflict; update the configured providers")
        policy["only"] = providers
    if require_zdr(config):
        payload.setdefault("provider", {})["zdr"] = True
