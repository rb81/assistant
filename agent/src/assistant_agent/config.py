import os
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

import yaml


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


def load_config() -> AppConfig:
    path = Path(os.getenv("AGENT_CONFIG", "/app/config/agent.yaml"))
    values: dict[str, Any] = {}

    if path.exists() and not path.is_file():
        raise RuntimeError("%s must be a file, not a directory" % path)
    if path.is_file():
        with path.open("r", encoding="utf-8") as handle:
            values = yaml.safe_load(handle) or {}

    env_overrides: list[tuple[str, str, Optional[type]]] = [
        ("AGENT_APP_NAME", "agent.app.name", str),
        ("AGENT_APP_BASE_URL", "agent.app.base_url", str),
        ("AGENT_NAME", "agent.identity.name", str),
        ("AGENT_EMAIL", "agent.identity.email", str),
        ("IMAP_HOST", "agent.email.imap_host", str),
        ("IMAP_PORT", "agent.email.imap_port", int),
        ("IMAP_USERNAME", "agent.email.imap_username", str),
        ("IMAP_PASSWORD", "agent.email.imap_password", str),
        ("IMAP_FOLDER", "agent.email.imap_folder", str),
        ("IMAP_ARCHIVE_FOLDER", "agent.email.imap_archive_folder", str),
        ("IMAP_SENT_FOLDER", "agent.email.imap_sent_folder", str),
        ("SMTP_HOST", "agent.email.smtp_host", str),
        ("SMTP_PORT", "agent.email.smtp_port", int),
        ("SMTP_USERNAME", "agent.email.smtp_username", str),
        ("SMTP_PASSWORD", "agent.email.smtp_password", str),
        ("SMTP_FROM", "agent.email.smtp_from", str),
        ("EMAIL_ALLOWED_RECIPIENT_DOMAINS", "agent.email.allowed_recipient_domains", list),
        ("EMAIL_ACTIONABLE_SENDERS", "agent.email.actionable_senders", list),
        ("AGENT_API_BIND_HOST", "agent.api.bind_host", str),
        ("AGENT_API_PORT", "agent.api.port", int),
        ("AGENT_API_DOCS_ENABLED", "agent.api.docs_enabled", bool),
        ("AGENT_API_OPENAPI_ENABLED", "agent.api.openapi_enabled", bool),
        ("AGENT_API_DASHBOARD_ENABLED", "agent.api.dashboard_enabled", bool),
        ("AGENT_API_WORKSPACE_ENABLED", "agent.api.workspace_enabled", bool),
        ("AGENT_API_ALLOW_PUBLIC_BIND", "agent.api.allow_public_bind", bool),
        ("AGENT_API_MAX_UPLOAD_BYTES", "agent.api.max_upload_bytes", int),
        ("ADMIN_NAME", "agent.admin.name", str),
        ("ADMIN_EMAIL", "agent.admin.email", str),
        ("ORG_NAME", "agent.org.name", str),
        ("ORG_SECURITY_EMAIL", "agent.org.security_email", str),
        ("ORG_INTERNAL_EMAIL_DOMAINS", "agent.org.internal_email_domains", list),
        ("AGENT_TIMEZONE", "agent.app.timezone", str),
        ("AGENT_LLM_MODEL", "agent.llm.model", str),
        ("AGENT_LLM_FALLBACK_MODEL", "agent.llm.fallback_model", str),
        ("AGENT_MAX_DAILY_COST_USD", "agent.limits.max_daily_cost_usd", float),
        ("AGENT_MAX_EMAILS_PER_HOUR", "agent.limits.max_emails_per_hour", int),
        ("AGENT_IMAP_POLL_INTERVAL", "agent.email.imap_poll_interval_seconds", int),
        ("CALENDAR_ENABLED", "agent.calendar.enabled", bool),
        ("CALENDAR_TIMEZONE", "agent.calendar.timezone", str),
    ]

    for env_name, path_name, caster in env_overrides:
        if env_name not in os.environ:
            continue
        raw_value = os.environ[env_name]
        if not raw_value.strip():
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

    return AppConfig(values)


def _clean_config_text(config: AppConfig, path: str, default: str) -> str:
    value = str(config.get(path, default) or default).strip()
    return value or default


def app_name(config: AppConfig) -> str:
    return _clean_config_text(config, "agent.app.name", "assistant")


def app_display_name(config: AppConfig) -> str:
    name = app_name(config)
    return name[:1].upper() + name[1:] if name.islower() else name


def app_referer_url(config: AppConfig) -> str:
    return _clean_config_text(config, "agent.app.referer_url", "https://%s.local" % app_name(config))


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


def database_url(config: AppConfig) -> str:
    value = os.getenv("DATABASE_URL")
    if value:
        return value

    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError("POSTGRES_PASSWORD is required")

    user = str(config.get("agent.database.user", "assistant"))
    host = str(config.get("agent.database.host", "postgres"))
    port = config.get_int("agent.database.port", 5432)
    name = str(config.get("agent.database.name", "assistant"))
    return "postgres://%s:%s@%s:%s/%s" % (
        quote(user, safe=""),
        quote(password, safe=""),
        host,
        port,
        quote(name, safe=""),
    )


def worker_id(role: str) -> str:
    return "%s:%s" % (role, os.getenv("HOSTNAME", "local"))
