"""Validated reasoning policy shared by model routes and provider adapters."""
import logging


LOGGER = logging.getLogger("assistant.reasoning")
MODES = {"auto", "provider_default", "off", "effort", "budget"}
EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max"}
REASONING_ROUTES = ("naming", "routing", "summarization", "memory", "research", "development")


def policy(config, route):
    path = "agent.llm.reasoning" if route in {None, "task"} else "agent.model_routes.%s.reasoning" % route
    value = config.get(path)
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("Reasoning settings must be an object")
    mode = value.get("mode", "auto" if route in {None, "task"} else "provider_default")
    effort = value.get("effort", "low")
    tokens = value.get("max_tokens", 2048)
    if mode not in MODES or effort not in EFFORTS:
        raise ValueError("Choose a valid reasoning mode and effort for route %s" % (route or "task"))
    if isinstance(tokens, (bool, float)) or isinstance(tokens, str) and not tokens.isdecimal():
        raise ValueError("Reasoning token budget must be a positive integer")
    try:
        tokens = int(tokens)
    except (TypeError, ValueError) as exc:
        raise ValueError("Reasoning token budget must be a positive integer") from exc
    if isinstance(value.get("max_tokens"), bool) or not 1 <= tokens <= 131072:
        raise ValueError("Reasoning token budget must be between 1 and 131072")
    return {"mode": mode, "effort": effort, "max_tokens": tokens}


def validate_config(config):
    caps = {
        "task": ("agent.llm.max_tokens_per_call", 8192),
        "naming": ("agent.naming.max_tokens_per_call", 1024),
        "routing": ("agent.routing.max_tokens_per_call", 2048),
        "summarization": ("agent.limits.summarization_max_tokens", 2000),
        "memory": ("agent.memory.steward.max_tokens_per_call", 1600),
        "research": ("agent.llm.max_tokens_per_call", 8192),
        "development": ("agent.llm.max_tokens_per_call", 8192),
    }
    for route in ("task", *REASONING_ROUTES):
        selected = policy(config, route)
        if selected["mode"] == "budget":
            cap = config.get_int(*caps[route])
            if selected["max_tokens"] >= cap:
                raise ValueError("Total %s output tokens must exceed the thinking-token budget" % route)
    for path, minimum, maximum in (
        ("agent.limits.repeated_tool_result_limit", 2, 20),
        ("agent.limits.incomplete_response_limit", 1, 10),
        ("agent.naming.max_tokens_per_call", 64, 131072),
        ("agent.routing.max_tokens_per_call", 64, 131072),
    ):
        value = config.get(path)
        if value is None:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("%s must be an integer" % path) from exc
        if isinstance(value, (bool, float)) or not minimum <= number <= maximum:
            raise ValueError("%s must be between %s and %s" % (path, minimum, maximum))


def capabilities(config, provider, model):
    if provider != "openrouter":
        return None
    from .model_service import ModelServiceError, OpenRouterModels
    try:
        models = OpenRouterModels(config).models()
    except ModelServiceError as exc:
        LOGGER.warning("Reasoning capability discovery unavailable: %s", exc)
        return None
    entry = next((item for item in models if item["id"] == model), None)
    if entry is None:
        raise ValueError("Selected model is absent from the eligible OpenRouter catalog: %s" % model)
    advertised = entry.get("reasoning")
    if isinstance(advertised, dict):
        return advertised
    if "reasoning" in (entry.get("supported_parameters") or []):
        return {"supported_efforts": None}
    return {}


def configure(config, provider, model, route, payload):
    selected = policy(config, route)
    mode = selected["mode"]
    decision = {"mode": mode, "applied": None}
    if mode == "provider_default":
        return decision
    advertised = capabilities(config, provider, model)
    if mode == "auto":
        efforts = advertised.get("supported_efforts") if advertised is not None else []
        if advertised and "low" in (efforts or []):
            payload["reasoning"] = {"effort": "low"}
        elif advertised and advertised.get("supports_max_tokens") and payload["max_tokens"] > selected["max_tokens"]:
            payload["reasoning"] = {"max_tokens": selected["max_tokens"]}
        else:
            decision["reason"] = "No advertised low-effort or fitting token-budget capability; provider default."
        decision["applied"] = payload.get("reasoning")
        return decision
    if provider in {"cloudflare", "cloudflare-gateway", "openai-compatible"}:
        raise ValueError("Explicit reasoning controls are not supported by this provider adapter; use Auto or Provider default")
    if provider == "openrouter" and advertised is None:
        raise ValueError("Cannot verify explicit reasoning controls while model capability discovery is unavailable")
    if mode == "budget":
        if provider == "openai" or (provider == "openrouter" and not advertised.get("supports_max_tokens")):
            raise ValueError("This model does not advertise a separate thinking-token cap; use effort or provider default")
        if selected["max_tokens"] >= payload["max_tokens"]:
            raise ValueError("Total output tokens must exceed the thinking-token budget to leave room for the answer")
        if provider == "anthropic" and selected["max_tokens"] < 1024:
            raise ValueError("Anthropic thinking-token budgets must be at least 1024")
        payload["reasoning"] = {"max_tokens": selected["max_tokens"]}
    else:
        effort = "none" if mode == "off" else selected["effort"]
        if advertised is not None:
            if effort == "none" and advertised.get("mandatory"):
                raise ValueError("Reasoning is mandatory for this model and cannot be disabled")
            accepted = advertised.get("supported_efforts")
            if not advertised or (effort != "none" and effort not in (accepted or [])):
                raise ValueError("Selected model does not advertise reasoning effort %s" % effort)
        payload["reasoning"] = {"enabled": False} if mode == "off" else {"effort": effort}
    decision["applied"] = payload.get("reasoning")
    return decision
