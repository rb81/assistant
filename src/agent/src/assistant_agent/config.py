import os
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import yaml

from .settings_schema import defaults_dir, env_overrides
from .secret_store import is_secret_name, read_secret


class AppConfig:
    def __init__(self, values: dict[str, Any]):
        self.values = values

    def get(self, path: str, default: Any = None) -> Any:
        current: Any = self.values
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def get_int(self, path: str, default: int) -> int:
        value = self.get(path, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def get_float(self, path: str, default: float) -> float:
        value = self.get(path, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def get_bool(self, path: str, default: bool = False) -> bool:
        value = self.get(path, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def get_list(self, path: str) -> list[str]:
        value = self.get(path, [])
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, Iterable):
            return [str(item) for item in value]
        return []


def _set_nested(values: dict[str, Any], path: str, value: Any) -> None:
    current = values
    parts = path.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


ENV_OVERRIDES: list[tuple[str, str, Optional[type]]] = env_overrides()


def load_config() -> AppConfig:
    # Defaults come only from the agent.yaml shipped with this build; data/overrides.env changes them.
    path = Path(os.getenv("AGENT_CONFIG") or defaults_dir() / "agent.yaml")
    values: dict[str, Any] = {}

    if path.exists() and not path.is_file():
        raise RuntimeError("%s must be a file, not a directory" % path)
    if path.is_file():
        with path.open("r", encoding="utf-8") as handle:
            values = yaml.safe_load(handle) or {}

    for env_name, path_name, caster in ENV_OVERRIDES:
        if env_name not in os.environ and not (is_secret_name(env_name) and env_name + "_FILE" in os.environ):
            continue
        raw_value = (read_secret(env_name) or "") if is_secret_name(env_name) else os.environ[env_name]
        if not raw_value.strip():
            if env_name == "AGENT_EMBEDDINGS_DIMENSIONS":
                _set_nested(values, path_name, None)
            elif env_name in {"AGENT_LLM_MODEL", "AGENT_NAMING_MODEL", "AGENT_ROUTING_MODEL",
                              "AGENT_LIMITS_SUMMARIZATION_MODEL", "AGENT_MEMORY_STEWARD_MODEL",
                              "AGENT_DEEP_RESEARCH_MODEL", "AGENT_SECURITY_DEVELOPMENT_MODEL",
                              "AGENT_DEEP_RESEARCH_SEARCH_MODEL", "AGENT_SEARCH_MODEL"}:
                _set_nested(values, path_name, "")
            elif env_name in {"AGENT_SEARCH_ALLOWED_DOMAINS", "AGENT_SEARCH_EXCLUDED_DOMAINS",
                              "AGENT_SANDBOX_NETWORK_ALLOWED_HOSTS", "AGENT_PROVIDERS_DISABLED",
                              "AGENT_PROVIDERS_ENABLED"}:
                _set_nested(values, path_name, [])
            continue
        if caster is int:
            try:
                value: Any = int(raw_value)
            except ValueError:
                value = raw_value
        elif caster is bool:
            value = raw_value.strip().lower() in ("1", "true", "yes", "on")
        elif caster is float:
            try:
                value = float(raw_value)
            except ValueError:
                value = raw_value
        elif caster is list:
            value = [item.strip() for item in raw_value.split(",") if item.strip()]
        else:
            value = raw_value
        _set_nested(values, path_name, value)

    config = AppConfig(values)
    from .reasoning import validate_config
    validate_config(config)
    return config


def _clean_config_text(config: AppConfig, path: str, default: str) -> str:
    value = str(config.get(path, default) or default).strip()
    return value or default


def app_name(config: AppConfig) -> str:
    return _clean_config_text(config, "agent.app.name", "assistant")


def app_display_name(config: AppConfig) -> str:
    name = app_name(config)
    return name[:1].upper() + name[1:] if name.islower() else name


def app_referer_url(config: AppConfig) -> str:
    return _clean_config_text(config, "agent.app.referer_url", "https://github.com/rb81/assistant")


def agent_name(config: AppConfig) -> str:
    return _clean_config_text(config, "agent.identity.name", app_display_name(config))


def agent_email(config: AppConfig) -> str:
    configured = (
        config.get("agent.identity.email")
        or config.get("agent.email.smtp_from")
        or config.get("agent.email.smtp_username")
        or "assistant@local"
    )
    parsed = parseaddr(str(configured))[1]
    return parsed or "assistant@local"


def agent_display(config: AppConfig) -> str:
    name = agent_name(config)
    email = agent_email(config)
    return "%s <%s>" % (name, email) if email else name


def message_id_domain(config: AppConfig) -> str:
    configured = str(config.get("agent.identity.message_id_domain") or "").strip().lower()
    if configured:
        return configured
    email = agent_email(config)
    parsed = parseaddr(email)[1]
    if "@" in parsed:
        return parsed.rsplit("@", 1)[-1].strip().lower()
    return "%s.local" % app_name(config)


def validate_database_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        parsed.port
        query = parse_qs(parsed.query, keep_blank_values=True)
    except ValueError:
        parsed = None
        query = {}
    if (
        parsed is None
        or parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.hostname
        or len(query.get("sslmode", [])) != 1
        or query["sslmode"][0] != "verify-full"
        or len(query.get("sslrootcert", [])) != 1
        or not query["sslrootcert"][0]
    ):
        raise RuntimeError("AGENT_DATABASE_URL must use sslmode=verify-full and provide sslrootcert")


def database_url(config: AppConfig) -> str:
    value = read_secret("AGENT_DATABASE_URL")
    if value:
        validate_database_url(value)
        return value

    password = read_secret("AGENT_DATABASE_PASSWORD")
    if not password:
        raise RuntimeError("AGENT_DATABASE_PASSWORD is required")

    user = str(config.get("agent.database.user", "assistant"))
    host = str(config.get("agent.database.host", "postgres"))
    port = config.get_int("agent.database.port", 5432)
    name = str(config.get("agent.database.name", "assistant"))
    return "postgres://%s:%s@%s:%s/%s?%s" % (
        quote(user, safe=""),
        quote(password, safe=""),
        host,
        port,
        quote(name, safe=""),
        urlencode({"sslmode": "verify-full", "sslrootcert": "/run/assistant/tls/postgres.crt"}),
    )


def worker_id(role: str) -> str:
    return "%s:%s" % (role, os.getenv("HOSTNAME", "local"))
