import os
from pathlib import Path
from typing import Any

from .config import AppConfig, agent_email, database_url
from .time_utils import default_timezone


EXAMPLE_VALUES = {
    "",
    "change-me",
    "REPLACE-WITH-A-STRONG-PASSWORD",
    "imap.example.com",
    "smtp.example.com",
    "user@example.com",
    "agent@example.com",
    "admin@example.com",
}

# Passwords that are known-weak placeholders and should trigger a warning.
# This list is checked at startup; the application still starts but logs an error.
WEAK_PASSWORDS = {
    "change-me",
    "REPLACE-WITH-A-STRONG-PASSWORD",
    "password",
    "postgres",
    "assistant",
    "secret",
    "changeme",
}


def postgres_password_is_weak() -> bool:
    """Return True if POSTGRES_PASSWORD is unset or matches a known weak/placeholder value."""
    password = os.getenv("POSTGRES_PASSWORD", "")
    return not password.strip() or password.strip() in WEAK_PASSWORDS


def is_set(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    return bool(text) and text not in EXAMPLE_VALUES


def has_model_api_key() -> bool:
    return is_set(os.getenv("OPENROUTER_API_KEY")) or is_set(os.getenv("OPENAI_API_KEY"))


def uses_openrouter(config: AppConfig) -> bool:
    return "openrouter.ai" in str(config.get("agent.llm.base_url", "")).lower()


def openrouter_configured(config: AppConfig) -> bool:
    return uses_openrouter(config) and is_set(os.getenv("OPENROUTER_API_KEY"))


def smtp_configured(config: AppConfig) -> bool:
    host = config.get("agent.email.smtp_host")
    from_address = config.get("agent.email.smtp_from") or config.get("agent.email.smtp_username") or agent_email(config)
    return is_set(host) and is_set(from_address)


def admin_configured(config: AppConfig) -> bool:
    return is_set(config.get("agent.admin.email"))


def imap_configured(config: AppConfig) -> bool:
    return all(
        is_set(config.get(path))
        for path in (
            "agent.email.imap_host",
            "agent.email.imap_username",
            "agent.email.imap_password",
        )
    )


def shared_root_status(config: AppConfig) -> dict[str, Any]:
    configured = config.get("agent.filesystem.shared_root")
    if not is_set(configured):
        return {"configured": False, "available": False, "path": configured, "reason": "not configured"}
    path = Path(str(configured))
    if not path.exists():
        return {"configured": True, "available": False, "path": str(path), "reason": "path does not exist"}
    if not path.is_dir():
        return {"configured": True, "available": False, "path": str(path), "reason": "path is not a directory"}
    if config.get_bool("agent.filesystem.require_mount", True) and not os.path.ismount(path):
        return {"configured": True, "available": False, "path": str(path), "reason": "path is not mounted"}
    return {"configured": True, "available": True, "path": str(path), "reason": None}


def sandbox_configured(config: AppConfig) -> bool:
    return config.get_bool("agent.sandbox.enabled", True) and is_set(config.get("agent.sandbox.base_url"))


def search_configured(config: AppConfig) -> bool:
    return config.get_bool("agent.search.enabled", True) and openrouter_configured(config)


def fusion_configured(config: AppConfig) -> bool:
    return config.get_bool("agent.fusion.enabled", False) and openrouter_configured(config)


def calendar_configured(config: AppConfig) -> bool:
    return config.get_bool("agent.calendar.enabled", False) and is_set(config.get("agent.calendar.store.vdir_path"))


def validate_role_config(role: str, config: AppConfig) -> list[str]:
    errors: list[str] = []
    try:
        database_url(config)
    except RuntimeError as exc:
        errors.append(str(exc))

    if role in ("downloader", "all") and not imap_configured(config):
        errors.append("IMAP is required for downloader: set IMAP_HOST, IMAP_USERNAME, and IMAP_PASSWORD")

    deep_research_role_enabled = role in ("deep-research-agent", "deep-research-cron") and config.get_bool("agent.deep_research.enabled", True)
    heartbeat_role_enabled = role in ("heartbeat", "heartbeat-cron") and config.get_bool("agent.heartbeat.enabled", True)
    model_required = role in ("task-agent", "all") or deep_research_role_enabled or heartbeat_role_enabled
    if model_required and uses_openrouter(config) and not openrouter_configured(config):
        errors.append("%s uses OpenRouter and requires OPENROUTER_API_KEY" % role)

    if model_required and not uses_openrouter(config) and not has_model_api_key():
        errors.append("%s requires OPENROUTER_API_KEY or OPENAI_API_KEY" % role)

    if model_required and not is_set(config.get("agent.llm.model")):
        errors.append("%s requires agent.llm.model" % role)

    if role in ("task-agent", "all") and config.get_bool("agent.search.enabled", True) and not openrouter_configured(config):
        errors.append("OpenRouter web search is enabled but OpenRouter is not configured")

    if role in ("workspace-indexer", "workspace-index-cron") and not shared_root_status(config)["available"]:
        errors.append("workspace indexer requires an available shared workspace root")

    if role in ("task-agent", "all") and config.get_bool("agent.fusion.enabled", False) and not openrouter_configured(config):
        errors.append("OpenRouter fusion is enabled but OpenRouter is not configured")

    if role in ("task-agent", "all") and not admin_configured(config):
        errors.append("task-agent requires ADMIN_EMAIL because request_input can email the admin")

    if role in ("task-agent", "all") and not smtp_configured(config):
        errors.append("task-agent requires SMTP config so request_input can send email")

    if role in ("deep-research-agent", "deep-research-cron", "all") and config.get_bool("agent.deep_research.enabled", True):
        if not search_configured(config):
            errors.append("deep research requires OpenRouter web search: set OPENROUTER_API_KEY and keep SEARCH_ENABLED=true")
        if not smtp_configured(config):
            errors.append("deep research requires SMTP config so it can request human guidance by email")
        if not admin_configured(config):
            errors.append("deep research requires ADMIN_EMAIL as a fallback guidance recipient")

    heartbeat_digest_enabled = config.get_int("agent.heartbeat.admin_digest_interval_hours", 0) > 0
    if role in ("heartbeat", "heartbeat-cron", "all") and config.get_bool("agent.heartbeat.enabled", True) and heartbeat_digest_enabled:
        if not admin_configured(config):
            errors.append("heartbeat requires ADMIN_EMAIL so it can send queue-health digests")
        if not smtp_configured(config):
            errors.append("heartbeat requires SMTP config so it can send queue-health digests")

    return errors


def tool_status(config: AppConfig) -> dict[str, Any]:
    shared = shared_root_status(config)
    return {
        "terminal_tools": ["task_complete", "task_failed", "request_input"],
        "email_tools": {
            "available": True,
            "tools": ["email_search", "email_read"] + (["email_send"] if smtp_configured(config) else []),
            "smtp_configured": smtp_configured(config),
        },
        "memory_tools": {
            "available": config.get_bool("agent.memory.steward.enabled", True),
            "managed_by": "memory_steward",
            "model": config.get("agent.memory.steward.model", "openai/gpt-4.1-mini"),
            "mode": config.get("agent.memory.steward.mode", "best_effort"),
            "main_agent_tools_exposed": False,
            "steward_tools": [
                "memory_keyword_search",
                "memory_semantic_search",
                "memory_get",
                "memory_create",
                "memory_update",
                "memory_delete",
            ],
            "embeddings": {
                "available": config.get_bool("agent.embeddings.enabled", config.get_bool("agent.memory.embeddings.enabled", True)),
                "base_url": config.get("agent.embeddings.base_url", config.get("agent.memory.embeddings.base_url", "http://ollama:11434")),
                "model": config.get("agent.embeddings.model", config.get("agent.memory.embeddings.model", "embeddinggemma")),
            },
        },
        "note_tools": {
            "available": True,
            "tools": ["note_create", "note_search", "note_read", "note_update", "note_delete"],
            "prompt_injection": False,
        },
        "reminder_tools": {
            "available": True,
            "tools": ["reminder_create", "reminder_list", "reminder_update", "reminder_cancel"],
            "default_timezone": default_timezone(config)[1],
        },
        "calendar_tools": {
            "available": calendar_configured(config),
            "tools": [
                "calendar_sync",
                "calendar_list_busy",
                "calendar_list_events",
                "calendar_create_event",
                "calendar_update_event",
                "calendar_delete_event",
            ]
            if calendar_configured(config)
            else [],
            "store": {
                "vdir_path": config.get("agent.calendar.store.vdir_path"),
                "default_calendar": config.get("agent.calendar.store.default_calendar", "default"),
            },
            "sync": {
                "command_configured": bool(config.get_list("agent.calendar.sync.command")),
                "before_read": config.get_bool("agent.calendar.sync.before_read", False),
                "before_write": config.get_bool("agent.calendar.sync.before_write", True),
                "after_write": config.get_bool("agent.calendar.sync.after_write", True),
            },
            "policy": {
                "allow_read_event_details": config.get_bool("agent.calendar.policy.allow_read_event_details", False),
                "writes_managed_only": True,
            },
        },
        "project_tools": {
            "available": config.get_bool("agent.projects.enabled", True),
            "tools": ["project_create", "project_status"] if config.get_bool("agent.projects.enabled", True) else [],
            "max_tasks": config.get_int("agent.projects.max_tasks", 25),
        },
        "deep_research_tools": {
            "available": config.get_bool("agent.deep_research.enabled", True) and search_configured(config),
            "tools": ["deep_research_request", "deep_research_status", "web_search"]
            if config.get_bool("agent.deep_research.enabled", True) and search_configured(config)
            else [],
            "max_tool_calls": config.get_int("agent.deep_research.max_tool_calls", 25),
            "max_iterations": config.get_int("agent.deep_research.max_iterations", 15),
            "search_available": search_configured(config),
        },
        "file_tools": {
            "available": shared["available"],
            "shared_root": shared,
            "tools": [
                "file_list",
                "file_read",
                "file_write",
                "file_append",
                "file_move",
                "file_copy",
                "file_convert",
                "file_delete",
                "file_search",
                "file_semantic_search",
            ]
            if shared["available"]
            else [],
            "workspace_index": {
                "enabled": config.get_bool("agent.workspace.index.enabled", True),
                "document_text_extraction": True,
                "convertible_document_extensions": config.get_list("agent.workspace.convertible_document_extensions"),
            },
            "conversion": {
                "pandoc_path": config.get("agent.conversion.pandoc_path", "pandoc"),
                "pdf_engine": config.get("agent.conversion.pdf_engine", "weasyprint"),
                "timeout_seconds": config.get_int("agent.conversion.timeout_seconds", 120),
            },
        },
        "tool_result_cache": {
            "enabled": config.get_bool("agent.tool_result_cache.enabled", True),
            "root": config.get("agent.tool_result_cache.root", ".assistant/cache/tool-results"),
            "min_bytes": config.get_int("agent.tool_result_cache.min_bytes", 4096),
            "retention_days": config.get_int("agent.tool_result_cache.retention_days", 7),
        },
        "artifact_processing": {
            "available": config.get_bool("agent.artifacts.enabled", True),
            "raw_root": config.get("agent.artifacts.raw_root", "/data/private/artifacts"),
            "processed_root": config.get("agent.artifacts.processed_root", "processed"),
            "max_attachment_bytes": config.get_int("agent.artifacts.max_attachment_bytes", 25 * 1024 * 1024),
            "clamav": {
                "enabled": config.get_bool("agent.artifacts.clamav.enabled", True),
                "required": config.get_bool("agent.artifacts.clamav.required", True),
                "host": config.get("agent.artifacts.clamav.host", "clamav"),
                "port": config.get_int("agent.artifacts.clamav.port", 3310),
            },
        },
        "command_tools": {
            "available": sandbox_configured(config),
            "base_url": config.get("agent.sandbox.base_url"),
            "tools": ["command_execute"] if sandbox_configured(config) else [],
        },
        "search_tools": {
            "available": search_configured(config),
            "provider": "openrouter:web_search via web_search",
            "tools": ["web_search"] if search_configured(config) else [],
        },
        "fusion_tools": {
            "available": fusion_configured(config),
            "provider": "openrouter:fusion",
            "tools": ["openrouter:fusion"] if fusion_configured(config) else [],
        },
        "admin": {
            "available": admin_configured(config),
            "email": config.get("agent.admin.email"),
        },
        "org": {
            "security_email": config.get("agent.org.security_email"),
            "internal_email_domains": config.get_list("agent.org.internal_email_domains"),
        },
        "llm": {
            "available": has_model_api_key(),
            "model": config.get("agent.llm.model"),
            "base_url": config.get("agent.llm.base_url"),
            "uses_openrouter": uses_openrouter(config),
        },
        "limits": {
            "max_iterations_per_task": config.get_int("agent.limits.max_iterations_per_task", 50),
            "max_tokens_per_task": config.get_int("agent.limits.max_tokens_per_task", 1000000),
            "max_tokens_per_call": config.get_int("agent.llm.max_tokens_per_call", 4096),
        },
    }
