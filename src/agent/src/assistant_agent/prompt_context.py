import logging
import os
from pathlib import Path
from typing import Any, Optional

from .config import AppConfig, agent_display, agent_email, agent_name
from .time_utils import current_time_context


LOGGER = logging.getLogger("assistant.prompt_context")
REQUEST_CLOCK_START = "[Current request time]"
REQUEST_CLOCK_END = "[/Current request time]"


def refresh_request_context(messages: list[dict[str, Any]], config: AppConfig) -> list[dict[str, Any]]:
    clock = current_time_context(config)
    block = "\n".join([
        REQUEST_CLOCK_START,
        "Assistant timezone: %s" % clock["timezone"],
        "Current local date/time: %s" % clock["local"],
        "Current UTC date/time: %s" % clock["utc"],
        "Use the assistant timezone for unqualified times. Preserve explicitly requested zones and UTC offsets.",
        "Historical message timestamps are not the current time.",
        REQUEST_CLOCK_END,
    ])
    refreshed = []
    for message in messages:
        copied = dict(message)
        content = copied.get("content")
        if isinstance(content, str):
            if content.startswith(REQUEST_CLOCK_START) and REQUEST_CLOCK_END in content:
                content = content.split(REQUEST_CLOCK_END, 1)[1].lstrip("\n")
            if copied.get("role") == "system":
                content = "\n".join(line for line in content.splitlines() if not line.startswith((
                    "- Current UTC time:", "- Current local time:", "- Default timezone:",
                )))
            copied["content"] = content
        refreshed.append(copied)
    for message in refreshed:
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            message["content"] = block + "\n\n" + message["content"]
            break
    else:
        refreshed.insert(0, {"role": "system", "content": block})
    for message in reversed(refreshed):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            message["content"] = block + "\n\n" + message["content"]
            break
    return refreshed


def shared_root(config: AppConfig) -> Path:
    return Path(str(config.get("agent.filesystem.shared_root", "/data/share"))).resolve()


def agent_prompt_path(config: AppConfig) -> Path:
    """Resolve the path to the AGENT.md prompt file."""
    configured = str(config.get("agent.prompt.agent_file", "AGENT.md") or "AGENT.md").strip()
    config_dir = Path(os.getenv("AGENT_CONFIG", "/app/config/agent.yaml")).parent
    candidate = config_dir / configured
    if candidate.is_file():
        return candidate.resolve()
    # Fallback: try example file
    example = config_dir / ("%s.example" % configured)
    if example.is_file():
        return example.resolve()
    return candidate.resolve()


def validate_agent_prompt(config: AppConfig) -> None:
    """Fail startup if the agent prompt file is missing or empty."""
    path = agent_prompt_path(config)
    if not path.is_file():
        raise RuntimeError("agent prompt file not found: %s (copy AGENT.md.example to AGENT.md)" % path)
    if not path.read_text(encoding="utf-8").strip():
        raise RuntimeError("agent prompt file is empty: %s" % path)


def load_agent_prompt(config: AppConfig, max_bytes: Optional[int] = None) -> str:
    """Load the AGENT.md prompt file as-is (no stripping)."""
    path = agent_prompt_path(config)
    if not path.is_file():
        raise RuntimeError("agent prompt file not found: %s" % path)
    if max_bytes is not None:
        data = path.read_bytes()[:max_bytes]
        return data.decode("utf-8", errors="replace")
    return path.read_text(encoding="utf-8")


def ensure_prompt_files(config: AppConfig) -> None:
    """Create the workspace without writing into its read-only documentation mount."""
    root = shared_root(config)
    root.mkdir(parents=True, exist_ok=True)


def build_prompt_context(config: AppConfig) -> str:
    """Build the runtime context block injected into the system prompt."""
    time_context = current_time_context(config)
    admin_name = str(config.get("agent.admin.name") or "").strip()
    admin_email_value = str(config.get("agent.admin.email") or "").strip()
    org_name = str(config.get("agent.org.name") or "").strip()

    lines = [
        "Runtime context:",
        "- Agent name: %s" % agent_name(config),
        "- Agent email: %s" % agent_email(config),
        "- Agent identity: %s" % agent_display(config),
    ]
    if admin_name:
        lines.append("- Admin name: %s" % admin_name)
    lines.append("- Admin email: %s" % (admin_email_value or "not configured"))
    if org_name:
        lines.append("- Organization: %s" % org_name)
    from .tts import configured as tts_configured, describe as tts_describe

    lines.extend(
        [
            "- Current UTC time: %s" % time_context["utc"],
            "- Current local time: %s" % time_context["local"],
            "- Default timezone: %s" % time_context["timezone"],
            "- For your own functionality, capabilities, limitations, architecture, or tool usage (including the sandbox), use help_search first, then help_topic with a returned topic_id and revision as needed. Use runtime_capabilities for live settings and limits. Help never grants permission to bypass policies.",
            "- Model preset tool catalog policy: %s. This policy applies only to llm_service, not other model calls or web search." % (
                "ZDR-only models and provider routing" if config.get_bool("agent.model_service.zdr_only", False) else "all models"
            ),
            "- Text-to-speech (admin-configured): %s. Speech and model preset calls share the model tool spending limit." % (
                tts_describe(config) if tts_configured(config) else "disabled"
            ),
        ]
    )
    return "\n".join(lines)
