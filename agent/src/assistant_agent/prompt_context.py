import logging
import os
from pathlib import Path
from typing import Any, Optional

from .config import AppConfig, agent_display, agent_email, agent_name
from .time_utils import current_time_context


LOGGER = logging.getLogger("assistant.prompt_context")
SHARED_WORKSPACE_DEFAULTS = Path(__file__).with_name("shared_workspace")
DOCS_DIR = ".assistant/docs"
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
    """Seed shared workspace defaults (docs only)."""
    root = shared_root(config)
    root.mkdir(parents=True, exist_ok=True)
    ensure_shared_workspace_defaults(root)


def ensure_shared_workspace_defaults(root: Path) -> None:
    """Copy packaged docs into the shared workspace. Docs are refreshed; other files are not overwritten."""
    if not SHARED_WORKSPACE_DEFAULTS.is_dir():
        return
    for source in SHARED_WORKSPACE_DEFAULTS.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(SHARED_WORKSPACE_DEFAULTS)
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            continue
        overwrite = relative.parts[:2] == (".assistant", "docs")
        if target.exists() and not overwrite:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        if target.suffix == ".py":
            target.chmod(target.stat().st_mode | 0o111)


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
    lines.extend(
        [
            "- Current UTC time: %s" % time_context["utc"],
            "- Current local time: %s" % time_context["local"],
            "- Default timezone: %s" % time_context["timezone"],
            "- Shared workspace docs: .assistant/docs/ (reference documentation for tools and environment)",
        ]
    )
    return "\n".join(lines)
