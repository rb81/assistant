"""Preserve existing model routes when provider availability becomes opt-in."""
import sys
from pathlib import Path

from assistant_agent.config import AppConfig
from assistant_agent.llm_providers import PROVIDER_CREDENTIALS, parse_model_reference, provider_for
from assistant_agent.setup import SetupError, atomic_write, installation_lock, parse_environment, read_regular, render_environment
from assistant_agent import settings_schema


def migrate(data):
    marker = data / ".migrations/003-provider-opt-in"
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
            raise SystemExit("Cannot migrate provider availability; fix the saved environment first.") from error
        values = {**env, **overrides}
        fresh = (data / ".install-initializing").exists() or not (env_content or overrides_content)
        if not fresh and "AGENT_PROVIDERS_ENABLED" not in values:
            defaults = settings_schema._load("agent.yaml")
            config = AppConfig(defaults)

            def value(key, path, default=""):
                return values.get(key, config.get(path, default))

            base_url = value("AGENT_LLM_BASE_URL", "agent.llm.base_url") or config.get("agent.llm.base_url")
            provider = value("AGENT_LLM_PROVIDER", "agent.llm.provider", "auto")
            primary = provider_for(AppConfig({"agent": {"llm": {"provider": provider}}}), base_url)
            selected = {primary}
            routes = config.get("agent.model_routes", {})

            def enabled(key, path, default=False):
                return str(value(key, path, default)).strip().lower() in {"true", "1", "yes", "on"}

            search = enabled("AGENT_SEARCH_ENABLED", "agent.search.enabled", True) and enabled(
                "AGENT_SEARCH_OPENROUTER_ENABLED", "agent.search.openrouter_enabled", True)
            active = {"task", "naming", "routing", "summarization", "development"}
            if enabled("AGENT_MEMORY_STEWARD_ENABLED", "agent.memory.steward.enabled", True):
                active.add("memory")
            if enabled("AGENT_DEEP_RESEARCH_ENABLED", "agent.deep_research.enabled", True):
                active.add("research")
                if search:
                    active.add("research_search")
            if search:
                active.add("web_search")
            if enabled("AGENT_FUSION_ENABLED", "agent.fusion.enabled"):
                active.add("fusion")
            if enabled("AGENT_TTS_ENABLED", "agent.tts.enabled"):
                active.add("speech")
            if enabled("AGENT_MODEL_SERVICE_ENABLED", "agent.model_service.enabled"):
                selected.add("openrouter")
            for name, route in routes.items():
                if not isinstance(route, dict):
                    raise SystemExit("Cannot migrate invalid model route configuration.")
                prefix = "AGENT_MODEL_ROUTES_" + name.upper()
                selected_provider = str(value(prefix + "_PROVIDER", "agent.model_routes." + name + ".provider",
                                              "inherit") or "inherit").strip().lower()
                if (name in active or prefix + "_PROVIDER" in values) and selected_provider not in {
                        "", "auto", "inherit", None}:
                    if selected_provider not in PROVIDER_CREDENTIALS:
                        raise SystemExit("Cannot migrate an unsupported model route provider.")
                    selected.add(selected_provider)
                fallback_values = value(prefix + "_FALLBACKS", "agent.model_routes." + name + ".fallbacks", [])
                for fallback in AppConfig({"fallbacks": fallback_values}).get_list("fallbacks"):
                    fallback_provider, _ = parse_model_reference(fallback)
                    selected.add(fallback_provider)
            screening_enabled = str(value("AGENT_SECURITY_DECISION_MODEL_ENABLED",
                                          "agent.security.decision_model.enabled", False)).lower()
            if screening_enabled in {"true", "1", "yes", "on"}:
                screening = value("AGENT_SECURITY_DECISION_MODEL_PROVIDER", "agent.security.decision_model.provider")
                if screening in PROVIDER_CREDENTIALS:
                    selected.add(screening)
            disabled = set(AppConfig({"disabled": value("AGENT_PROVIDERS_DISABLED",
                                                         "agent.providers.disabled", [])}).get_list("disabled"))
            enabled = ",".join(provider for provider in PROVIDER_CREDENTIALS
                               if provider in selected and provider not in disabled)
            text = overrides_content.decode("utf-8")
            atomic_write(overrides_path, render_environment(
                text, {"AGENT_PROVIDERS_ENABLED": enabled}, set()).encode("utf-8"))
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(marker, b"complete\n")


if __name__ == "__main__":
    migrate(Path(sys.argv[1]))
