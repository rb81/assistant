import os
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import yaml

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


ENV_OVERRIDES: list[tuple[str, str, Optional[type]]] = [
        # App settings
        ("AGENT_APP_NAME", "agent.app.name", str),
        ("AGENT_APP_REFERER_URL", "agent.app.referer_url", str),
        ("AGENT_TIMEZONE", "agent.app.timezone", str),
        
        # Agent identity
        ("AGENT_NAME", "agent.identity.name", str),
        ("AGENT_EMAIL", "agent.identity.email", str),
        
        # Admin contact
        ("ADMIN_NAME", "agent.admin.name", str),
        ("ADMIN_EMAIL", "agent.admin.email", str),
        ("ZOOM_ENABLED", "agent.zoom.enabled", bool),
        ("ZOOM_AUTH_MODE", "agent.zoom.auth_mode", str),
        ("ZOOM_CLIENT_ID", "agent.zoom.client_id", str),
        ("ZOOM_CLIENT_SECRET", "agent.zoom.client_secret", str),
        ("ZOOM_ACCOUNT_ID", "agent.zoom.account_id", str),
        ("ZOOM_REFRESH_TOKEN", "agent.zoom.refresh_token", str),
        ("ZOOM_USER_ID", "agent.zoom.user_id", str),
        ("DROPBOX_ENABLED", "agent.dropbox.enabled", bool),
        ("DROPBOX_CLIENT_ID", "agent.dropbox.client_id", str),
        ("DROPBOX_REFRESH_TOKEN", "agent.dropbox.refresh_token", str),
        ("DROPBOX_ALLOW_EXPORTS", "agent.dropbox.allow_exports", bool),
        ("DROPBOX_MAX_FILE_BYTES", "agent.dropbox.max_file_bytes", int),
        ("TELEGRAM_ENABLED", "agent.telegram.enabled", bool),
        ("TELEGRAM_BOT_TOKEN", "agent.telegram.bot_token", str),
        ("TELEGRAM_ALLOWED_USER_IDS", "agent.telegram.allowed_user_ids", list),
        ("TELEGRAM_RECIPIENTS", "agent.telegram.recipients", str),
        ("TELEGRAM_MAX_MESSAGES_PER_HOUR", "agent.telegram.max_messages_per_hour", int),
        ("TRANSCRIPTION_ENABLED", "agent.transcription.enabled", bool),
        ("TRANSCRIPTION_API_TOKEN", "agent.transcription.api_token", str),
        ("TRANSCRIPTION_BASE_URL", "agent.transcription.base_url", str),
        ("TRANSCRIPTION_LANGUAGE", "agent.transcription.language", str),
        ("AGENT_MODEL_SERVICE_ENABLED", "agent.model_service.enabled", bool),
        ("AGENT_MODEL_SERVICE_ZDR_ONLY", "agent.model_service.zdr_only", bool),
        ("AGENT_MODEL_SERVICE_BUDGET_USD", "agent.model_service.budget_usd", float),
        ("AGENT_MODEL_SERVICE_BUDGET_PERIOD_DAYS", "agent.model_service.budget_period_days", int),
        ("AGENT_TTS_ENABLED", "agent.tts.enabled", bool),
        ("AGENT_TTS_ZDR_ONLY", "agent.tts.zdr_only", bool),
        ("AGENT_TTS_MODEL", "agent.tts.model", str),
        ("AGENT_TTS_VOICE", "agent.tts.voice", str),
        ("AGENT_APPROVAL_CHANNEL", "agent.approvals.channel", str),
        ("AGENT_NAMING_MODEL", "agent.naming.model", str),
        
        # Organization
        ("ORG_NAME", "agent.org.name", str),
        ("ORG_SECURITY_EMAIL", "agent.org.security_email", str),
        ("ORG_INTERNAL_EMAIL_DOMAINS", "agent.org.internal_email_domains", list),
        
        # Database
        ("AGENT_DATABASE_HOST", "agent.database.host", str),
        ("AGENT_DATABASE_PORT", "agent.database.port", int),
        ("AGENT_DATABASE_NAME", "agent.database.name", str),
        ("AGENT_DATABASE_USER", "agent.database.user", str),
        
        # LLM
        ("AGENT_LLM_MODEL", "agent.llm.model", str),
        ("AGENT_LLM_FALLBACK_MODEL", "agent.llm.fallback_model", str),
        ("AGENT_LLM_BASE_URL", "agent.llm.base_url", str),
        ("AGENT_LLM_TEMPERATURE", "agent.llm.temperature", float),
        ("AGENT_LLM_MAX_TOKENS_PER_CALL", "agent.llm.max_tokens_per_call", int),
        
        # Limits
        ("AGENT_LIMITS_MAX_ITERATIONS_PER_TASK", "agent.limits.max_iterations_per_task", int),
        ("AGENT_LIMITS_MAX_TOKENS_PER_TASK", "agent.limits.max_tokens_per_task", int),
        ("AGENT_LIMITS_MESSAGE_HISTORY_WINDOW", "agent.limits.message_history_window", int),
        ("AGENT_LIMITS_MAX_PROMPT_CHARS", "agent.limits.max_prompt_chars", int),
        ("AGENT_LIMITS_SUMMARIZATION_MODEL", "agent.limits.summarization_model", str),
        ("AGENT_LIMITS_SUMMARIZATION_MAX_TOKENS", "agent.limits.summarization_max_tokens", int),
        ("AGENT_LIMITS_SUMMARIZATION_TIMEOUT_SECONDS", "agent.limits.summarization_timeout_seconds", int),
        ("AGENT_LIMITS_SUMMARIZATION_KEEP_RECENT", "agent.limits.summarization_keep_recent", int),
        ("AGENT_LIMITS_SUMMARIZATION_MAX_INPUT_CHARS", "agent.limits.summarization_max_input_chars", int),
        ("AGENT_LIMITS_TOOL_TIMEOUT_COMMAND_SECONDS", "agent.limits.tool_timeout_command_seconds", int),
        ("AGENT_MAX_EMAILS_PER_HOUR", "agent.limits.max_emails_per_hour", int),
        ("AGENT_LIMITS_POLL_INTERVAL_SECONDS", "agent.limits.poll_interval_seconds", int),
        
        # Task agent
        ("AGENT_TASK_AGENT_POLL_INTERVAL_SECONDS", "agent.task_agent.poll_interval_seconds", int),
        
        # Email
        ("IMAP_HOST", "agent.email.imap_host", str),
        ("IMAP_PORT", "agent.email.imap_port", int),
        ("IMAP_USERNAME", "agent.email.imap_username", str),
        ("IMAP_PASSWORD", "agent.email.imap_password", str),
        ("IMAP_FOLDER", "agent.email.imap_folder", str),
        ("IMAP_ARCHIVE_FOLDER", "agent.email.imap_archive_folder", str),
        ("IMAP_QUARANTINE_FOLDER", "agent.email.imap_quarantine_folder", str),
        ("IMAP_SENT_FOLDER", "agent.email.imap_sent_folder", str),
        ("AGENT_IMAP_POLL_INTERVAL", "agent.email.imap_poll_interval_seconds", int),
        ("SMTP_HOST", "agent.email.smtp_host", str),
        ("SMTP_PORT", "agent.email.smtp_port", int),
        ("SMTP_USERNAME", "agent.email.smtp_username", str),
        ("SMTP_PASSWORD", "agent.email.smtp_password", str),
        ("SMTP_FROM", "agent.email.smtp_from", str),
        ("AGENT_EMAIL_SAVE_TO_SENT", "agent.email.save_to_sent", bool),
        ("AGENT_EMAIL_MAX_ATTACHMENT_COUNT", "agent.email.max_attachment_count", int),
        ("AGENT_EMAIL_MAX_ATTACHMENT_BYTES", "agent.email.max_attachment_bytes", int),
        ("AGENT_EMAIL_MAX_TOTAL_ATTACHMENT_BYTES", "agent.email.max_total_attachment_bytes", int),
        ("EMAIL_ALLOWED_RECIPIENT_DOMAINS", "agent.email.allowed_recipient_domains", list),
        ("AGENT_EMAIL_CONTEXT_BODY_PREVIEW_CHARS", "agent.email.context_body_preview_chars", int),
        ("AGENT_EMAIL_INITIAL_CONTEXT_PRIOR_FULL_BODY_CHAR_LIMIT", "agent.email.initial_context_prior_full_body_char_limit", int),
        ("AGENT_EMAIL_MAX_INITIAL_CONTEXT_BODY_CHARS", "agent.email.max_initial_context_body_chars", int),
        ("EMAIL_ACTIONABLE_SENDERS", "agent.email.actionable_senders", list),
        ("AGENT_EMAIL_SUBJECT_THREADING_FALLBACK", "agent.email.subject_threading_fallback", bool),
        
        # Supervisor
        ("AGENT_SUPERVISOR_STALL_THRESHOLD_MINUTES", "agent.supervisor.stall_threshold_minutes", int),
        ("AGENT_SUPERVISOR_MAX_TASK_DURATION_MINUTES", "agent.supervisor.max_task_duration_minutes", int),
        
        # Filesystem
        ("AGENT_FILESYSTEM_SHARED_ROOT", "agent.filesystem.shared_root", str),
        ("AGENT_FILESYSTEM_REQUIRE_MOUNT", "agent.filesystem.require_mount", bool),
        ("AGENT_FILESYSTEM_SHARED_FILE_UMASK", "agent.filesystem.shared_file_umask", str),
        ("AGENT_FILESYSTEM_MAX_READ_BYTES", "agent.filesystem.max_read_bytes", int),
        ("AGENT_FILESYSTEM_TRASH_DIRECTORY", "agent.filesystem.trash_directory", str),
        
        # Tool result cache
        ("AGENT_TOOL_RESULT_CACHE_ENABLED", "agent.tool_result_cache.enabled", bool),
        ("AGENT_TOOL_RESULT_CACHE_ROOT", "agent.tool_result_cache.root", str),
        ("AGENT_TOOL_RESULT_CACHE_MIN_BYTES", "agent.tool_result_cache.min_bytes", int),
        ("AGENT_TOOL_RESULT_CACHE_RETENTION_DAYS", "agent.tool_result_cache.retention_days", int),
        
        # Artifacts
        ("AGENT_ARTIFACTS_ENABLED", "agent.artifacts.enabled", bool),
        ("AGENT_ARTIFACTS_RAW_ROOT", "agent.artifacts.raw_root", str),
        ("AGENT_ARTIFACTS_PROCESSED_ROOT", "agent.artifacts.processed_root", str),
        ("AGENT_ARTIFACTS_MAX_ATTACHMENT_BYTES", "agent.artifacts.max_attachment_bytes", int),
        ("AGENT_ARTIFACTS_CLAMAV_ENABLED", "agent.artifacts.clamav.enabled", bool),
        ("AGENT_ARTIFACTS_CLAMAV_REQUIRED", "agent.artifacts.clamav.required", bool),
        ("AGENT_ARTIFACTS_CLAMAV_HOST", "agent.artifacts.clamav.host", str),
        ("AGENT_ARTIFACTS_CLAMAV_PORT", "agent.artifacts.clamav.port", int),
        ("AGENT_ARTIFACTS_CLAMAV_TIMEOUT_SECONDS", "agent.artifacts.clamav.timeout_seconds", int),
        
        # Prompt
        ("AGENT_PROMPT_AGENT_FILE", "agent.prompt.agent_file", str),
        ("AGENT_PROMPT_MAX_CONTEXT_FILE_BYTES", "agent.prompt.max_context_file_bytes", int),
        
        # Memory
        ("AGENT_MEMORY_STEWARD_ENABLED", "agent.memory.steward.enabled", bool),
        ("AGENT_MEMORY_STEWARD_MODEL", "agent.memory.steward.model", str),
        ("AGENT_MEMORY_STEWARD_MODE", "agent.memory.steward.mode", str),
        ("AGENT_MEMORY_STEWARD_TIMEOUT_SECONDS", "agent.memory.steward.timeout_seconds", int),
        ("AGENT_MEMORY_STEWARD_MAX_TOKENS_PER_CALL", "agent.memory.steward.max_tokens_per_call", int),
        ("AGENT_MEMORY_STEWARD_MAX_INJECTED_MEMORIES", "agent.memory.steward.max_injected_memories", int),
        ("AGENT_MEMORY_STEWARD_MAX_WRITES_PER_JOB", "agent.memory.steward.max_writes_per_job", int),
        ("AGENT_MEMORY_STEWARD_MAX_TRANSCRIPT_BYTES", "agent.memory.steward.max_transcript_bytes", int),
        ("AGENT_MEMORY_STEWARD_MIN_IMPORTANCE", "agent.memory.steward.min_importance", int),
        ("AGENT_MEMORY_STEWARD_MIN_CONFIDENCE", "agent.memory.steward.min_confidence", float),
        
        # Context
        ("AGENT_CONTEXT_SEARCH_DAYS", "agent.context.search_days", int),
        
        # Embeddings
        ("AGENT_EMBEDDINGS_ENABLED", "agent.embeddings.enabled", bool),
        ("AGENT_EMBEDDINGS_BASE_URL", "agent.embeddings.base_url", str),
        ("AGENT_EMBEDDINGS_MODEL", "agent.embeddings.model", str),
        ("AGENT_EMBEDDINGS_DIMENSIONS", "agent.embeddings.dimensions", int),
        ("AGENT_EMBEDDINGS_TIMEOUT_SECONDS", "agent.embeddings.timeout_seconds", int),
        
        # Workspace
        ("AGENT_WORKSPACE_MAX_CONVERSION_BYTES", "agent.workspace.max_conversion_bytes", int),
        ("AGENT_WORKSPACE_INDEX_ENABLED", "agent.workspace.index.enabled", bool),
        ("AGENT_WORKSPACE_INDEX_POLL_INTERVAL_SECONDS", "agent.workspace.index.poll_interval_seconds", int),
        ("AGENT_WORKSPACE_INDEX_CHUNK_CHARS", "agent.workspace.index.chunk_chars", int),
        ("AGENT_WORKSPACE_INDEX_CANDIDATE_LIMIT", "agent.workspace.index.candidate_limit", int),
        
        # Conversion
        ("AGENT_CONVERSION_PANDOC_PATH", "agent.conversion.pandoc_path", str),
        ("AGENT_CONVERSION_PDF_ENGINE", "agent.conversion.pdf_engine", str),
        ("AGENT_CONVERSION_TIMEOUT_SECONDS", "agent.conversion.timeout_seconds", int),
        ("AGENT_CONVERSION_MAX_INPUT_BYTES", "agent.conversion.max_input_bytes", int),
        
        # Reminders
        ("AGENT_REMINDERS_DEFAULT_TIMEZONE", "agent.reminders.default_timezone", str),
        ("AGENT_REMINDERS_SCHEDULER_POLL_INTERVAL_SECONDS", "agent.reminders.scheduler_poll_interval_seconds", int),
        ("AGENT_REMINDERS_MAX_DUE_PER_TICK", "agent.reminders.max_due_per_tick", int),
        
        # Calendar
        ("CALENDAR_ENABLED", "agent.calendar.enabled", bool),
        ("CALENDAR_TIMEZONE", "agent.calendar.timezone", str),
        ("CALENDAR_DEFAULT_PROVIDER", "agent.calendar.default_provider", str),
        ("CALENDAR_DEFAULT_ALERT_MINUTES", "agent.calendar.default_alert_minutes", int),

        ("AGENT_CALENDAR_DEFAULT_ALERT_MINUTES", "agent.calendar.default_alert_minutes", int),
        ("AGENT_CALENDAR_POLICY_ALLOW_READ_EVENT_DETAILS", "agent.calendar.policy.allow_read_event_details", bool),
        
        # Calendar provider-specific settings
        ("CALENDAR_GOOGLE_CALENDAR_ID", "agent.calendar.google.calendar_id", str),
        ("CALENDAR_OUTLOOK_CALENDAR_ID", "agent.calendar.outlook.calendar_id", str),
        ("CALENDAR_OUTLOOK_TENANT_ID", "agent.calendar.outlook.tenant_id", str),
        ("CALENDAR_ZOHO_CALENDAR_ID", "agent.calendar.zoho.calendar_id", str),
        ("CALENDAR_ZOHO_API_BASE", "agent.calendar.zoho.api_base", str),
        
        # Projects
        
        # Deep research
        ("AGENT_DEEP_RESEARCH_ENABLED", "agent.deep_research.enabled", bool),
        ("AGENT_DEEP_RESEARCH_MODEL", "agent.deep_research.model", str),
        ("AGENT_DEEP_RESEARCH_SEARCH_MODEL", "agent.deep_research.search_model", str),
        ("AGENT_DEEP_RESEARCH_MAX_TOOL_CALLS", "agent.deep_research.max_tool_calls", int),
        ("AGENT_DEEP_RESEARCH_MAX_ITERATIONS", "agent.deep_research.max_iterations", int),
        ("AGENT_DEEP_RESEARCH_POLL_INTERVAL_SECONDS", "agent.deep_research.poll_interval_seconds", int),
        ("AGENT_DEEP_RESEARCH_TIMEOUT_SECONDS", "agent.deep_research.timeout_seconds", int),
        
        # Heartbeat
        ("AGENT_HEARTBEAT_ENABLED", "agent.heartbeat.enabled", bool),
        ("AGENT_HEARTBEAT_POLL_INTERVAL_SECONDS", "agent.heartbeat.poll_interval_seconds", int),
        ("AGENT_HEARTBEAT_STALE_THRESHOLD_MINUTES", "agent.heartbeat.stale_threshold_minutes", int),
        ("AGENT_HEARTBEAT_DEEP_RESEARCH_STALE_MINUTES", "agent.heartbeat.deep_research_stale_minutes", int),
        ("AGENT_HEARTBEAT_PROJECT_STALE_MINUTES", "agent.heartbeat.project_stale_minutes", int),
        ("AGENT_HEARTBEAT_ADMIN_DIGEST_INTERVAL_HOURS", "agent.heartbeat.admin_digest_interval_hours", int),
        
        # Sandbox
        ("AGENT_SANDBOX_ENABLED", "agent.sandbox.enabled", bool),
        ("AGENT_SANDBOX_BASE_URL", "agent.sandbox.base_url", str),
        ("AGENT_SANDBOX_MAX_ATTEMPTS", "agent.sandbox.max_attempts", int),
        ("AGENT_SANDBOX_RETRY_BACKOFF_SECONDS", "agent.sandbox.retry_backoff_seconds", int),
        
        # Search
        ("AGENT_SEARCH_ENABLED", "agent.search.enabled", bool),
        ("AGENT_SEARCH_MODEL", "agent.search.model", str),
        ("AGENT_SEARCH_ENGINE", "agent.search.engine", str),
        ("AGENT_SEARCH_MAX_RESULTS", "agent.search.max_results", int),
        ("AGENT_SEARCH_MAX_TOTAL_RESULTS", "agent.search.max_total_results", int),
        ("AGENT_SEARCH_CONTEXT_SIZE", "agent.search.search_context_size", str),
        ("AGENT_SEARCH_ALLOWED_DOMAINS", "agent.search.allowed_domains", list),
        ("AGENT_SEARCH_EXCLUDED_DOMAINS", "agent.search.excluded_domains", list),
        
        # Fusion
        ("AGENT_FUSION_ENABLED", "agent.fusion.enabled", bool),
        ("AGENT_FUSION_ANALYSIS_MODELS", "agent.fusion.analysis_models", list),
        ("AGENT_FUSION_MODEL", "agent.fusion.model", str),
        ("AGENT_FUSION_MAX_TOOL_CALLS", "agent.fusion.max_tool_calls", int),
        ("AGENT_FUSION_MAX_COMPLETION_TOKENS", "agent.fusion.max_completion_tokens", int),
        ("AGENT_FUSION_TEMPERATURE", "agent.fusion.temperature", float),

        # Security screening
        ("AGENT_SECURITY_NOTIFY_MAX_PER_HOUR", "agent.security.notify_max_per_hour", int),
        ("AGENT_SECURITY_ALLOW_POSSIBLY_MALICIOUS", "agent.security.allow_possibly_malicious", bool),
        ("AGENT_SECURITY_EMAIL_AUTH_ENABLED", "agent.security.email_authentication.enabled", bool),
        ("AGENT_SECURITY_DECISION_ENABLED", "agent.security.decision_model.enabled", bool),
        ("AGENT_SECURITY_DECISION_PROVIDER", "agent.security.decision_model.provider", str),
        ("AGENT_SECURITY_DECISION_URL", "agent.security.decision_model.url", str),
        ("AGENT_SECURITY_DECISION_MODEL", "agent.security.decision_model.model", str),
        ("AGENT_SECURITY_DECISION_TIMEOUT_SECONDS", "agent.security.decision_model.timeout_seconds", int),
        ("AGENT_SECURITY_DECISION_ON_ERROR", "agent.security.decision_model.on_error", str),
        ("AGENT_SECURITY_DECISION_APPROVAL_TTL_MINUTES", "agent.security.decision_model.approval_ttl_minutes", int),
        ("AGENT_SECURITY_DECISION_MALICIOUS_THRESHOLD", "agent.security.decision_model.malicious_threshold", float),
        ("AGENT_SECURITY_DECISION_POSSIBLY_MALICIOUS_THRESHOLD", "agent.security.decision_model.possibly_malicious_threshold", float),
        ("AGENT_SECURITY_DECISION_FLAG_THRESHOLD", "agent.security.decision_model.flag_threshold", float),
        ("AGENT_SECURITY_DECISION_MAX_FIELD_CHARS", "agent.security.decision_model.max_field_chars", int),
        ("AGENT_SECURITY_SCREEN_OUTBOUND_EMAIL", "agent.security.decision_model.screen.outbound_email", bool),
        ("AGENT_SECURITY_SCREEN_SCRIPTS", "agent.security.decision_model.screen.scripts", bool),
        ("AGENT_SECURITY_SCRIPT_ALLOWED_DOMAINS", "agent.security.scripts.allowed_domains", list),
        ("AGENT_SECURITY_SCRIPT_MAX_FILES", "agent.security.scripts.max_files", int),
]


def load_config() -> AppConfig:
    path = Path(os.getenv("AGENT_CONFIG", "/app/config/agent.yaml"))
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
            elif env_name in {"AGENT_SEARCH_ALLOWED_DOMAINS", "AGENT_SEARCH_EXCLUDED_DOMAINS"}:
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
        raise RuntimeError("DATABASE_URL must use sslmode=verify-full and provide sslrootcert")


def database_url(config: AppConfig) -> str:
    value = read_secret("DATABASE_URL")
    if value:
        validate_database_url(value)
        return value

    password = read_secret("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError("POSTGRES_PASSWORD is required")

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
