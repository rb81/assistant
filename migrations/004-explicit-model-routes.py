"""Snapshot inherited provider and model selections for existing installations."""
import sys
from pathlib import Path
from urllib.parse import urlsplit

from assistant_agent import settings_schema
from assistant_agent.config import AppConfig
from assistant_agent.llm_providers import PROVIDER_CREDENTIALS, PROVIDER_DEFAULT_URLS, legacy_provider_for, provider_for
from assistant_agent.setup import SetupError, atomic_write, installation_lock, parse_environment, read_regular, render_environment


INHERITED_ROUTES = ("task", "naming", "routing", "summarization", "memory", "research", "development")
INHERITED_MODELS = {
    "AGENT_DEEP_RESEARCH_MODEL": "agent.deep_research.model",
    "AGENT_SECURITY_DEVELOPMENT_MODEL": "agent.security.development.model",
}
MODEL_DEFAULTS = {
    "AGENT_LLM_MODEL": "agent.llm.model",
    "AGENT_NAMING_MODEL": "agent.naming.model",
    "AGENT_ROUTING_MODEL": "agent.routing.model",
    "AGENT_LIMITS_SUMMARIZATION_MODEL": "agent.limits.summarization_model",
    "AGENT_MEMORY_STEWARD_MODEL": "agent.memory.steward.model",
    "AGENT_DEEP_RESEARCH_SEARCH_MODEL": "agent.deep_research.search_model",
    "AGENT_SEARCH_MODEL": "agent.search.model",
}


def migrate(data):
    marker = data / ".migrations/004-explicit-model-routes"
    with installation_lock(data.parent):
        if marker.exists():
            return
        overrides_path = data / "overrides.env"
        env_content = read_regular(data / ".env") or b""
        overrides_content = read_regular(overrides_path) or b""
        try:
            env, _ = parse_environment(env_content.decode("utf-8"))
            overrides, _ = parse_environment(overrides_content.decode("utf-8"))
        except (SetupError, UnicodeError) as error:
            raise SystemExit("Cannot migrate model routes; fix the saved environment first.") from error
        values = {**env, **overrides}
        fresh = (data / ".install-initializing").exists() or not (env_content or overrides_content)
        if not fresh:
            defaults = AppConfig(settings_schema._load("agent.yaml"))
            def effective(key, path, default=None):
                value = values.get(key)
                return value if value and value.strip() else defaults.get(path, default)

            base_url = effective("AGENT_LLM_BASE_URL", "agent.llm.base_url")
            provider = effective("AGENT_LLM_PROVIDER", "agent.llm.provider", "auto")
            try:
                selected = legacy_provider_for(AppConfig({"agent": {"llm": {"provider": provider}}}), base_url)
            except ValueError as error:
                raise SystemExit("Cannot migrate the existing default model provider: %s" % error) from error
            updates = {}
            if selected == "openai-compatible" and "AGENT_PROVIDERS_ENABLED" in values:
                enabled = AppConfig({"items": values["AGENT_PROVIDERS_ENABLED"]}).get_list("items")
                disabled = AppConfig({"items": values.get("AGENT_PROVIDERS_DISABLED", "")}).get_list("items")
                enabled_names = {item.lower() for item in enabled}
                disabled_names = {item.lower() for item in disabled}
                previous = provider_for(AppConfig({"agent": {"llm": {"provider": provider}}}), base_url)
                if (previous != selected and previous in enabled_names and previous not in disabled_names
                        and selected not in enabled_names and selected not in disabled_names):
                    updates["AGENT_PROVIDERS_ENABLED"] = ",".join([*enabled, selected])
            endpoint_provider = "cloudflare" if selected == "cloudflare-gateway" else selected
            endpoint_key = "AGENT_PROVIDERS_" + endpoint_provider.upper().replace("-", "_") + "_BASE_URL"
            endpoint_path = "agent.providers." + endpoint_provider.replace("-", "_") + "_base_url"
            dedicated = values.get(endpoint_key) or defaults.get(endpoint_path)
            default_endpoint = PROVIDER_DEFAULT_URLS.get(endpoint_provider)
            if endpoint_provider == "cloudflare":
                account = effective("AGENT_PROVIDERS_CLOUDFLARE_ACCOUNT_ID", "agent.providers.cloudflare_account_id")
                if not account and base_url:
                    parsed = urlsplit(base_url)
                    parts = parsed.path.strip("/").split("/")
                    if (parsed.hostname or "").lower() == "api.cloudflare.com" and len(parts) >= 5:
                        updates["AGENT_PROVIDERS_CLOUDFLARE_ACCOUNT_ID"] = parts[3]
            elif (not dedicated or dedicated == default_endpoint) and base_url:
                official_host = urlsplit(default_endpoint).hostname if default_endpoint else None
                if endpoint_provider == "openai-compatible" or urlsplit(base_url).hostname == official_host:
                    updates[endpoint_key] = str(base_url)
            for route in INHERITED_ROUTES:
                key = "AGENT_MODEL_ROUTES_" + route.upper() + "_PROVIDER"
                current = values.get(key, "").strip().lower()
                if current in {"", "auto", "inherit"}:
                    updates[key] = selected
                elif current not in PROVIDER_CREDENTIALS:
                    raise SystemExit("Cannot migrate unsupported provider for model route %s." % route)
            task_provider = updates.get("AGENT_MODEL_ROUTES_TASK_PROVIDER") or values["AGENT_MODEL_ROUTES_TASK_PROVIDER"].strip().lower()
            task_fallbacks = effective("AGENT_MODEL_ROUTES_TASK_FALLBACKS", "agent.model_routes.task.fallbacks")
            legacy_fallback = effective("AGENT_LLM_FALLBACK_MODEL", "agent.llm.fallback_model")
            if not task_fallbacks and legacy_fallback:
                updates["AGENT_MODEL_ROUTES_TASK_FALLBACKS"] = "%s:%s" % (task_provider, legacy_fallback)
            task_model = effective("AGENT_LLM_MODEL", "agent.llm.model")
            if not task_model:
                raise SystemExit("Cannot migrate model routes without a task model.")
            for key, path in MODEL_DEFAULTS.items():
                if key in values and not values[key].strip() and defaults.get(path):
                    updates[key] = str(defaults.get(path))
            for key, path in INHERITED_MODELS.items():
                if not effective(key, path):
                    updates[key] = str(task_model)
            if updates:
                atomic_write(overrides_path, render_environment(
                    overrides_content.decode("utf-8"), updates, set()).encode("utf-8"))
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(marker, b"complete\n")


if __name__ == "__main__":
    migrate(Path(sys.argv[1]))
