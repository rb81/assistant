"""Server-owned choices for provider settings and explicit model routes."""
import copy
import urllib.request

from .config import AppConfig
from .llm_catalog import ModelCatalogError, _read_catalog, discover_models
from .llm_providers import PROVIDER_CREDENTIALS, provider_is_disabled, route_provider
from .openrouter_policy import valid_provider_slugs
from . import settings_schema


MODEL_ROUTES = {
    "AGENT_LLM_MODEL": "task", "AGENT_LLM_FALLBACK_MODEL": "task",
    "AGENT_NAMING_MODEL": "naming", "AGENT_ROUTING_MODEL": "routing",
    "AGENT_LIMITS_SUMMARIZATION_MODEL": "summarization", "AGENT_MEMORY_STEWARD_MODEL": "memory",
    "AGENT_DEEP_RESEARCH_MODEL": "research", "AGENT_DEEP_RESEARCH_SEARCH_MODEL": "research_search",
    "AGENT_SEARCH_MODEL": "web_search", "AGENT_SECURITY_DEVELOPMENT_MODEL": "development",
    "AGENT_TTS_MODEL": "speech", "AGENT_FUSION_MODEL": "fusion",
}


def configured_providers(backend):
    """Use credential presence, never credential values, to populate provider controls."""
    providers = [provider for provider, key in PROVIDER_CREDENTIALS.items()
                 if provider != "openai-compatible" and backend.credentials.get(key)]
    spec_values = backend.values
    custom_url = spec_values.get("AGENT_PROVIDERS_OPENAI_COMPATIBLE_BASE_URL") or \
        backend.installation.defaults.get("agent.providers.openai_compatible_base_url")
    custom_credentials = ("AGENT_PROVIDERS_OPENAI_COMPATIBLE_API_KEY", "AGENT_LLM_OPENAI_API_KEY")
    if custom_url and any(backend.credentials.get(name) for name in custom_credentials):
        providers.append("openai-compatible")
    priority = spec_values.get("AGENT_PROVIDERS_PRIORITY")
    if priority is None:
        priority = backend.installation.defaults.get_list("agent.providers.priority")
    if isinstance(priority, str):
        priority = [item.strip() for item in priority.split(",") if item.strip()]
    return sorted(providers, key=lambda provider: priority.index(provider) if provider in priority else len(priority))


def provider_choices_metadata(backend):
    """Cheap local choices belong in the settings catalog; remote lists load on demand."""
    from .management_api import AI_PROVIDERS

    configured = configured_providers(backend)
    verified = [provider for provider in configured if provider in {"openai", "anthropic", "cloudflare"}]
    result = {
        key: {"choices": values, "choice_labels": AI_PROVIDERS}
        for key, values in {
            "AGENT_PROVIDERS_DISABLED": configured,
            "AGENT_PROVIDERS_PRIORITY": configured,
            "AGENT_LLM_ZDR_VERIFIED_PROVIDERS": verified,
        }.items()
    }
    disabled = backend.values.get("AGENT_PROVIDERS_DISABLED")
    if disabled is None:
        disabled = backend.installation.defaults.get("agent.providers.disabled", [])
    disabled = disabled.split(",") if isinstance(disabled, str) else disabled
    active = [provider for provider in configured if provider not in disabled]
    for item in settings_schema.catalog():
        key = item["env"]
        if key == "AGENT_LLM_PROVIDER" or (key.startswith("AGENT_MODEL_ROUTES_") and key.endswith("_PROVIDER")):
            allowed = item["metadata"].get("enum", [])
            result[key] = {"selection": "single", "choices": [value for value in ("auto", "inherit", *active)
                                                               if value in allowed],
                           "choice_labels": {**AI_PROVIDERS, "auto": "Automatic (legacy endpoint)",
                                             "inherit": "Use default provider"}}
        elif key in MODEL_ROUTES:
            result[key] = {"selection": "single", "choices_source": "models",
                           "provider_key": "AGENT_MODEL_ROUTES_" + MODEL_ROUTES[key].upper() + "_PROVIDER"}
    return result


def setting_choices(config, backend, key, provider_id=None):
    """Discover options without changing routing policy or submitting inference requests."""
    if key in MODEL_ROUTES:
        provider = provider_id or route_provider(config, MODEL_ROUTES[key])
        if MODEL_ROUTES[key] in {"speech", "fusion", "web_search", "research_search"} and provider != "openrouter":
            raise ValueError("This model route requires OpenRouter")
        catalog = discover_models(config, provider_id=provider, credential_reader=backend.credentials.get)
        models = catalog["models"]
        return {"key": key, "choices": [model["id"] for model in models],
                "choice_labels": {model["id"]: model["name"] for model in models},
                "warnings": [catalog["warning"]] if catalog.get("warning") else []}
    if key == "AGENT_LLM_ALLOWED_PROVIDERS":
        data = _read_catalog(urllib.request.Request("https://openrouter.ai/api/v1/providers"))
        rows = data.get("data")
        if not isinstance(rows, list) or any(
                not isinstance(row, dict) or not isinstance(row.get("slug"), str) or not row["slug"]
                for row in rows):
            raise ModelCatalogError("OpenRouter returned an invalid hosting provider list")
        slugs = [row["slug"] for row in rows]
        if not valid_provider_slugs(slugs) or len(slugs) != len(set(slugs)):
            raise ModelCatalogError("OpenRouter returned invalid or duplicate hosting provider identifiers")
        return {"key": key, "choices": slugs,
                "choice_labels": {row["slug"]: row.get("name") or row["slug"] for row in rows},
                "warnings": []}
    attestation = key == "AGENT_LLM_ZDR_VERIFIED_MODELS"
    fallback = key.startswith("AGENT_MODEL_ROUTES_") and key.endswith("_FALLBACKS")
    if not attestation and not fallback:
        raise ValueError("This setting does not have a remote selection list")
    choices, labels, warnings = [], {}, []
    providers = configured_providers(backend)
    if attestation:
        providers = [provider for provider in providers if provider in {"openai", "anthropic", "cloudflare"}]
        # Account/model attestation must be possible before strict routing is enabled.
        values = copy.deepcopy(config.values)
        values.setdefault("agent", {}).setdefault("llm", {})["zdr_only"] = False
        config = AppConfig(values)
    if key in {"AGENT_MODEL_ROUTES_WEB_SEARCH_FALLBACKS", "AGENT_MODEL_ROUTES_RESEARCH_SEARCH_FALLBACKS"}:
        providers = [provider for provider in providers if provider == "openrouter"]
    for provider in providers:
        if provider_is_disabled(config, provider):
            continue
        try:
            catalog = discover_models(config, provider_id=provider, credential_reader=backend.credentials.get)
        except (ModelCatalogError, ValueError) as exc:
            warnings.append("%s: %s" % (provider, exc))
            continue
        for model in catalog["models"]:
            if attestation and provider == "cloudflare" and not model["id"].startswith("@cf/"):
                continue
            reference = provider + ":" + model["id"]
            choices.append(reference)
            labels[reference] = provider + " / " + model["name"]
        if catalog.get("warning"):
            warnings.append(catalog["warning"])
    return {"key": key, "choices": choices, "choice_labels": labels, "warnings": warnings}
