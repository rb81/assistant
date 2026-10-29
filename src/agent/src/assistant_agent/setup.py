import argparse
import copy
import fcntl
import hashlib
import hmac
import io
import json
import math
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from email.utils import parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urlsplit

import yaml
from dotenv.parser import parse_stream

from .secret_store import SECRET_NAMES, is_secret_name
from .config import ENV_OVERRIDES, AppConfig, validate_database_url
from .transport_security import validate_embedding_url, validate_https_url
from .validation import WEAK_PASSWORDS
from .setup_privileges import authorization_handler, prepare_sudo, terminal_authorization


AGENT_SERVICES = ("downloader", "task-agent", "workspace-indexer",
                  "reminder-scheduler", "deep-research-agent", "supervisor", "heartbeat",
                  "telegram-receiver", "request-preprocessor")
MODEL_SERVICES = ("task-agent", "deep-research-agent", "supervisor", "heartbeat")
TOOL_SERVICES = ("task-agent", "deep-research-agent", "heartbeat")
LONG_RUNNING_SERVICES = frozenset(AGENT_SERVICES) | {"postgres", "clamav", "ollama", "sandbox", "transcription"}
GENERATED_SECRETS = {"POSTGRES_PASSWORD", "SANDBOX_API_TOKEN", "SANDBOX_REVOKE_TOKEN", "TRANSCRIPTION_API_TOKEN"}
SECTION_IDS = ("identity", "models", "embeddings", "email", "calendar", "zoom", "dropbox", "telegram", "transcription", "security", "advanced", "instructions")
ADVANCED_SETTINGS = {
    "Calendar privacy": (
        ("AGENT_CALENDAR_POLICY_ALLOW_READ_EVENT_DETAILS", "agent.calendar.policy.allow_read_event_details", "Allow reading existing event titles, descriptions, locations and attendees? (does not grant edit/delete access)", False, bool, None),
    ),
    "Task limits": (
        ("AGENT_LIMITS_MAX_ITERATIONS_PER_TASK", "agent.limits.max_iterations_per_task", "Maximum iterations per task", 50, int, 1),
        ("AGENT_LIMITS_MAX_TOKENS_PER_TASK", "agent.limits.max_tokens_per_task", "Maximum tokens per task", 1000000, int, 1),
        ("AGENT_LLM_MAX_TOKENS_PER_CALL", "agent.llm.max_tokens_per_call", "Maximum output tokens per model call", 4096, int, 1),
        ("AGENT_SUPERVISOR_MAX_TASK_DURATION_MINUTES", "agent.supervisor.max_task_duration_minutes", "Maximum task duration (minutes)", 60, int, 1),
    ),
    "Email behavior": (
        ("AGENT_EMAIL_SAVE_TO_SENT", "agent.email.save_to_sent", "Save outgoing email to the Sent folder?", True, bool, None),
        ("AGENT_IMAP_POLL_INTERVAL", "agent.email.imap_poll_interval_seconds", "Inbox polling interval (seconds)", 60, int, 1),
        ("AGENT_MAX_EMAILS_PER_HOUR", "agent.limits.max_emails_per_hour", "Maximum outgoing emails per hour", 10, int, 1),
        ("AGENT_EMAIL_MAX_ATTACHMENT_COUNT", "agent.email.max_attachment_count", "Maximum attachments per outgoing email (0 = disabled)", 5, int, 0),
    ),
    "Memory and workspace indexing": (
        ("AGENT_MEMORY_STEWARD_ENABLED", "agent.memory.steward.enabled", "Enable automatic memory stewardship?", True, bool, None),
        ("AGENT_MEMORY_STEWARD_MAX_INJECTED_MEMORIES", "agent.memory.steward.max_injected_memories", "Maximum memories included in prompts", 8, int, 1),
        ("AGENT_CONTEXT_SEARCH_DAYS", "agent.context.search_days", "Email context search lookback (days)", 90, int, 1),
        ("AGENT_WORKSPACE_INDEX_ENABLED", "agent.workspace.index.enabled", "Index workspace files for search? (separate from embedding model configuration)", True, bool, None),
    ),
    "Web search": (
        ("AGENT_SEARCH_ENABLED", "agent.search.enabled", "Enable web search? (requires supported model access)", True, bool, None),
        ("AGENT_SEARCH_MAX_RESULTS", "agent.search.max_results", "Maximum results per search", 5, int, 1),
        ("AGENT_SEARCH_ALLOWED_DOMAINS", "agent.search.allowed_domains", "Allowed search domains (comma-separated; blank = unrestricted)", [], list, None),
        ("AGENT_SEARCH_EXCLUDED_DOMAINS", "agent.search.excluded_domains", "Excluded search domains (comma-separated; blank = none)", [], list, None),
    ),
    "Model preset tool": (
        ("AGENT_MODEL_SERVICE_ENABLED", "agent.model_service.enabled", "Enable the OpenRouter model preset tool? (text, image and audio; requires OPENROUTER_API_KEY)", False, bool, None),
        ("AGENT_MODEL_SERVICE_ZDR_ONLY", "agent.model_service.zdr_only", "Restrict the model preset tool to Zero Data Retention (ZDR) models and routing? (No = all models)", False, bool, None),
        ("AGENT_MODEL_SERVICE_BUDGET_USD", "agent.model_service.budget_usd", "Shared model preset and TTS budget (USD; checked before each request, final request may exceed it)", 5.0, float, 0.01),
        ("AGENT_MODEL_SERVICE_BUDGET_PERIOD_DAYS", "agent.model_service.budget_period_days", "Rolling budget period (days, 1-365)", 30, int, 1),
    ),
    "Text-to-speech": (
        ("AGENT_TTS_ENABLED", "agent.tts.enabled", "Enable dedicated text-to-speech? (requires OPENROUTER_API_KEY; shares spending limits with the model preset tool)", False, bool, None),
        ("AGENT_TTS_ZDR_ONLY", "agent.tts.zdr_only", "Restrict text-to-speech to ZDR models and routing? (No = all speech models)", False, bool, None),
        ("AGENT_TTS_MODEL", "agent.tts.model", "Speech model slug (list options with: python -m assistant_agent.tts voices)", "mistralai/voxtral-mini-tts-3-0", str, None),
        ("AGENT_TTS_VOICE", "agent.tts.voice", "Voice identifier for the speech model", "gb_jane_confident", str, None),
    ),
    "Approvals and names": (
        ("AGENT_APPROVAL_CHANNEL", "agent.approvals.channel", "Approval delivery: email, telegram, or both (Telegram recommended when configured)", "email", str, None),
        ("AGENT_NAMING_MODEL", "agent.naming.model", "Inexpensive model for job and workstream names", "openai/gpt-4.1-mini", str, None),
    ),
    "Research and notifications": (
        ("AGENT_DEEP_RESEARCH_ENABLED", "agent.deep_research.enabled", "Enable deep research? (requires supported model access)", True, bool, None),
        ("AGENT_DEEP_RESEARCH_MAX_TOOL_CALLS", "agent.deep_research.max_tool_calls", "Maximum tool calls per research run", 40, int, 1),
        ("AGENT_HEARTBEAT_ADMIN_DIGEST_INTERVAL_HOURS", "agent.heartbeat.admin_digest_interval_hours", "Admin digest interval (hours; 0 = disabled)", 0, int, 0),
    ),
}
SECURITY_SETTINGS = {
    "AGENT_SECURITY_EMAIL_AUTH_ENABLED": "agent.security.email_authentication.enabled",
    "AGENT_SECURITY_DECISION_ENABLED": "agent.security.decision_model.enabled",
    "AGENT_SECURITY_DECISION_PROVIDER": "agent.security.decision_model.provider",
    "AGENT_SECURITY_DECISION_URL": "agent.security.decision_model.url",
    "AGENT_SECURITY_DECISION_MODEL": "agent.security.decision_model.model",
    "AGENT_SECURITY_DECISION_ON_ERROR": "agent.security.decision_model.on_error",
    "AGENT_SECURITY_SCREEN_OUTBOUND_EMAIL": "agent.security.decision_model.screen.outbound_email",
    "AGENT_SECURITY_SCREEN_SCRIPTS": "agent.security.decision_model.screen.scripts",
    "AGENT_SECURITY_SCRIPT_ALLOWED_DOMAINS": "agent.security.scripts.allowed_domains",
    "AGENT_SECURITY_ALLOW_POSSIBLY_MALICIOUS": "agent.security.allow_possibly_malicious",
    "AGENT_SECURITY_SCRIPT_YOLO": "agent.security.script_yolo",
    "AGENT_SECURITY_DEVELOPMENT_MODEL": "agent.security.development.model",
    "AGENT_SECURITY_DEVELOPMENT_MAX_MINUTES": "agent.security.development.max_minutes",
    "AGENT_SECURITY_DEVELOPMENT_MAX_RUNS": "agent.security.development.max_runs",
}
YOLO_CONFIRMATION = "ALLOW POSSIBLY MALICIOUS"
ADVANCED_ENV_NAMES = frozenset(setting[0] for settings in ADVANCED_SETTINGS.values() for setting in settings)
PROMPT_DEFAULTS = {
    "AGENT_NAME": "agent.identity.name", "AGENT_EMAIL": "agent.identity.email",
    "ADMIN_NAME": "agent.admin.name", "ADMIN_EMAIL": "agent.admin.email",
    "ORG_NAME": "agent.org.name", "ORG_SECURITY_EMAIL": "agent.org.security_email",
    "AGENT_TIMEZONE": "agent.app.timezone",
    "AGENT_LLM_BASE_URL": "agent.llm.base_url", "AGENT_LLM_MODEL": "agent.llm.model",
    "AGENT_LLM_FALLBACK_MODEL": "agent.llm.fallback_model",
    "CALENDAR_ENABLED": "agent.calendar.enabled", "ZOOM_ENABLED": "agent.zoom.enabled",
    "DROPBOX_ENABLED": "agent.dropbox.enabled", "DROPBOX_ALLOW_EXPORTS": "agent.dropbox.allow_exports",
    "DROPBOX_MAX_FILE_BYTES": "agent.dropbox.max_file_bytes",
    "TELEGRAM_ENABLED": "agent.telegram.enabled", "TELEGRAM_ALLOWED_USER_IDS": "agent.telegram.allowed_user_ids",
    "TRANSCRIPTION_ENABLED": "agent.transcription.enabled", "TRANSCRIPTION_LANGUAGE": "agent.transcription.language",
    **SECURITY_SETTINGS,
     **{"AGENT_EMBEDDINGS_" + field.upper(): "agent.embeddings." + field for field in
         ("enabled", "model", "base_url", "dimensions", "timeout_seconds")},
    **{name: "agent.email." + name.lower() for name in (
        "IMAP_HOST", "IMAP_PORT", "IMAP_USERNAME", "IMAP_FOLDER", "IMAP_ARCHIVE_FOLDER", "IMAP_QUARANTINE_FOLDER", "IMAP_SENT_FOLDER",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_FROM",
    )},
}


class SetupError(RuntimeError):
    pass


SECTION_KEYS = {
    "identity": (("AGENT_NAME", "AGENT_TIMEZONE", "ADMIN_NAME", "ADMIN_EMAIL", "ORG_NAME", "ORG_SECURITY_EMAIL", "ORG_INTERNAL_EMAIL_DOMAINS"), ()),
    "models": (("AGENT_LLM_BASE_URL", "AGENT_LLM_MODEL", "AGENT_LLM_FALLBACK_MODEL", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "POSTGRES_PASSWORD"), ()),
    "embeddings": ((), ("AGENT_EMBEDDINGS_",)),
    "email": (("SETUP_EMAIL_ENABLED", "EMAIL_ACTIONABLE_SENDERS", "EMAIL_ALLOWED_RECIPIENT_DOMAINS", "AGENT_EMAIL"), ("IMAP_", "SMTP_")),
    "calendar": ((), ("CALENDAR_",)),
    "zoom": ((), ("ZOOM_",)),
    "dropbox": ((), ("DROPBOX_",)),
    "telegram": ((), ("TELEGRAM_",)),
    "transcription": ((), ("TRANSCRIPTION_",)),
    "security": ((*SECURITY_SETTINGS, "SECURITY_DECISION_API_KEY"), ()),
    "advanced": (tuple(sorted(ADVANCED_ENV_NAMES)), ()),
    "instructions": ((), ()),
}


def section_settings(values, secret_values, section):
    """Return the (name, displayed value) rows a section owns; credentials are masked."""
    names, prefixes = SECTION_KEYS[section]
    rows = {}
    for source, hidden in ((values, False), (secret_values, True)):
        for name, value in source.items():
            if name in names or name.startswith(prefixes):
                rows[name] = "configured (hidden)" if hidden or is_secret_name(name) else value
    return sorted(rows.items())


OVERRIDES_FILE = "overrides.env"
WIZARD_EXTRA_NAMES = frozenset({"AGENT_FUSION_ENABLED", "COMPOSE_PROFILES", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "SANDBOX_SHARED_ROOT_HOST"})
OVERRIDES_HEADER = """\
# Assistant manual overrides
#
# Uncomment a line and set a value to override config/agent.yaml in the agent containers.
# Setup creates this file once and never rewrites it, so your edits survive re-running setup.
# Settings the setup wizard manages are deliberately absent: change those with ./run.sh.
# Never put credentials here; use: ./run.sh secrets update NAME
# Booleans: true/false. Lists: comma-separated. Apply edits by recreating containers:
#   docker compose --env-file data/.env up -d --force-recreate
# Setup reports unknown, misspelled, mistyped or wizard-managed keys at its review step.
"""


def wizard_managed(name):
    return (name in PROMPT_DEFAULTS or name in ADVANCED_ENV_NAMES or name in WIZARD_EXTRA_NAMES
            or name.startswith(("SETUP_", "COMPOSE_"))
            or any(name in names or name.startswith(prefixes) for names, prefixes in SECTION_KEYS.values()))


def compose_environment_names(root):
    """Names set in a service environment: block, which Compose prefers over env_file values."""
    source = read_regular(Path(root) / "src/docker-compose.yml")
    services = ((yaml.safe_load(source) or {}).get("services") or {}) if source else {}
    names = set()
    for service in services.values():
        environment = (service or {}).get("environment") or {}
        if isinstance(environment, list):
            environment = dict(item.partition("=")[::2] for item in environment)
        names |= set(environment)
    return names


def manual_override_entries(root):
    blocked = compose_environment_names(root)
    excluded = {name for name, _, _ in ENV_OVERRIDES if wizard_managed(name) or name in blocked or is_secret_name(name)}
    managed_paths = {path for name, path, _ in ENV_OVERRIDES if name in excluded}
    return [(name, path, caster) for name, path, caster in ENV_OVERRIDES
            if name not in excluded and path not in managed_paths]


def overrides_template(root, defaults):
    lines = [OVERRIDES_HEADER]
    group = None
    for name, path, caster in manual_override_entries(root):
        section = path.split(".")[1]
        if section != group:
            group = section
            lines.append("\n# --- " + {"llm": "LLM"}.get(section, section.replace("_", " ").title()) + " ---\n")
        value = defaults.get(path)
        shown = "unset" if value is None else ",".join(map(str, value)) if isinstance(value, list) else str(value).lower() if isinstance(value, bool) else str(value)
        kind = caster.__name__ if caster else "str"
        lines.append(f"# {path} ({kind}; default: {shown})\n# {name}=\n")
    return "".join(lines)


def overrides_problems(root, text):
    try:
        values, _ = parse_environment(text)
    except SetupError as exc:
        return [OVERRIDES_FILE + ": " + str(exc).replace(".env", OVERRIDES_FILE)]
    allowed = {name: caster for name, _, caster in manual_override_entries(root)}
    problems = []
    for name, value in values.items():
        if is_secret_name(name.removesuffix("_FILE")) or any(part in name for part in ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "SIGNING_KEY")) and name not in allowed:
            problems.append(OVERRIDES_FILE + ": remove credential " + name + "; use ./run.sh secrets update")
        elif wizard_managed(name) or name in compose_environment_names(root):
            problems.append(OVERRIDES_FILE + ": " + name + " is managed by setup or docker-compose.yml and would be ignored or conflict; remove it")
        elif name not in allowed:
            problems.append(OVERRIDES_FILE + ": unknown setting " + name)
        elif value.strip():
            caster = allowed[name]
            try:
                if caster is bool and value.strip().lower() not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                    raise ValueError
                if caster in (int, float):
                    caster(value)
            except ValueError:
                problems.append(OVERRIDES_FILE + ": " + name + " must be " + ("a boolean" if caster is bool else "an integer" if caster is int else "a number"))
    return problems


class DockerCommandError(SetupError):
    def __init__(self, message, *, stdout=None, reason=None):
        super().__init__(message)
        self.stdout = stdout
        self.reason = reason


def docker_failure(arguments, result):
    """Translate known failure signatures without displaying Docker's raw output."""
    stderr = (result.stderr or "").lower()
    reason = None
    if "context" in stderr and any(part in stderr for part in ("does not exist", "context not found")):
        reason = "context"
        detail = "The selected Docker context does not exist. Check `docker context ls` and select the intended context."
    elif "permission denied" in stderr and any(part in stderr for part in ("docker.sock", "docker daemon socket")):
        reason = "permission"
        detail = "Access to the Docker socket was denied."
        detail += (" Check Docker Desktop and the selected context; macOS normally does not require sudo for Docker."
                   if platform.system() == "Darwin" else "Use the interactive authorization flow if your account permits sudo.")
    elif any(part in stderr for part in (
        "cannot connect to the docker daemon", "is the docker daemon running",
        "failed to connect to the docker", "error during connect",
    )) or ("docker" in stderr and "connection refused" in stderr):
        reason = "unavailable"
        detail = "Docker is unavailable. Check that the selected daemon is running and reachable."
        if platform.system() == "Darwin":
            detail += " Open Docker Desktop (or start your chosen runtime), wait until it is ready, then retry."
        detail += " Verify with `docker info`; check the endpoint with `docker context ls`."
    elif "error getting credentials" in stderr or "docker-credential-" in stderr:
        reason = "credentials"
        detail = ("Docker's registry credential helper failed. Check that your Docker runtime is installed correctly, "
                  "its credential helper is on PATH, and your keychain is unlocked. Retry `docker login` if needed.")
    elif any(part in stderr for part in ("client version", "client is newer than server", "minimum supported api version")):
        reason = "version"
        detail = "Docker client and server API versions are incompatible. Update Docker and check `docker version`."
    elif "no such volume" in stderr:
        reason = "volume"
        detail = ("The saved Docker credential volume is missing from the selected daemon. Check `docker context ls`; "
                  "use the original daemon or restore the credential-volume backup. Do not reset storage to bypass this.")
    elif "mounts denied" in stderr or "is not shared from the host" in stderr:
        reason = "mount"
        detail = "Docker cannot mount a host path. Allow the checkout and data directories in your runtime's file-sharing settings."
    elif "no matching manifest" in stderr or "exec format error" in stderr:
        reason = "architecture"
        detail = "The image does not support the selected CPU architecture. Check image platform support, especially on Apple Silicon."
    elif "no space left on device" in stderr:
        reason = "disk"
        detail = "Docker has run out of disk space. Check the runtime's disk allocation; do not delete data volumes to make room."
    elif any(part in stderr for part in ("pull access denied", "failed to resolve reference", "toomanyrequests", "unauthorized: authentication required")):
        reason = "registry"
        detail = "Docker could not pull an image. Check registry access, login, network connectivity, and rate limits."
    elif any(part in stderr for part in ("'compose' is not a docker command", "unknown command: docker compose")):
        reason = "compose"
        detail = "Docker Compose is unavailable. Install or update the Compose plugin and verify `docker compose version`."
    else:
        detail = ("Docker command failed; output was suppressed to protect credentials. "
                  "Check `docker info` and `docker version`. Inspect the failing command locally if those succeed; "
                  "review and redact its output before sharing it.")
    operation = "Docker command"
    if arguments[:1] == ["volume"]:
        operation = "Accessing Docker credential storage"
    elif arguments[:1] == ["run"]:
        operation = "Running a Docker helper"
    elif arguments[:1] == ["compose"]:
        operation = "Docker Compose"
        detail += " For configuration diagnostics, run `docker compose --env-file data/.env config --quiet` from the repository root."
    return DockerCommandError(f"{operation} failed (exit status {result.returncode}). {detail}",
                              stdout=result.stdout, reason=reason)


def secret_services(name):
    if name == "POSTGRES_PASSWORD":
        return ("postgres", *AGENT_SERVICES)
    if name == "DATABASE_URL":
        return AGENT_SERVICES
    if name == "OPENROUTER_API_KEY":
        # The downloader receives it only while inbound screening uses OpenRouter (see Installation.grants_secret).
        return (*MODEL_SERVICES, "downloader")
    if name == "OPENAI_API_KEY":
        return MODEL_SERVICES
    if name == "IMAP_PASSWORD":
        return ("downloader", *MODEL_SERVICES)
    if name == "SMTP_PASSWORD":
        return ("downloader", *MODEL_SERVICES, "reminder-scheduler", "telegram-receiver", "request-preprocessor")
    if name == "TELEGRAM_BOT_TOKEN":
        return ("downloader", "telegram-receiver", "request-preprocessor", "reminder-scheduler", *TOOL_SERVICES)
    if name == "TRANSCRIPTION_API_TOKEN":
        return ("transcription-init", "request-preprocessor", "downloader")
    if name == "SANDBOX_API_TOKEN":
        return ("sandbox-init", "sandbox", "task-agent")
    if name == "SANDBOX_REVOKE_TOKEN":
        return ("downloader", "telegram-receiver")
    if name.startswith(("CALENDAR_", "ZOOM_", "DROPBOX_")):
        return TOOL_SERVICES
    if name == "SECURITY_DECISION_API_KEY":
        return ("downloader", *TOOL_SERVICES)
    raise SetupError("Unknown secret name")


def read_regular(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SetupError("Configuration targets must be regular files, not symlinks")
    return path.read_bytes() if path.exists() else None


def atomic_write(path, content, mode=0o600):
    previous = read_regular(path)
    if path.parent.is_symlink():
        raise SetupError("Configuration directories must not be symlinks")
    if previous == content and stat.S_IMODE(path.stat().st_mode) == mode:
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".setup-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def installation_lock(root):
    directory = root / "data"
    if directory.is_symlink():
        raise SetupError("Runtime data directory must not be a symlink")
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / ".setup.lock"
    read_regular(path)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def parse_environment(content):
    values = {}
    bindings = list(parse_stream(io.StringIO(content)))
    for binding in bindings:
        if binding.error:
            raise SetupError("Malformed .env entry; repair it before setup")
        if binding.key:
            if binding.key in values:
                raise SetupError("Duplicate .env entry: " + binding.key)
            values[binding.key] = binding.value or ""
    return values, bindings


def render_environment(content, updates, removed):
    _, bindings = parse_environment(content)
    remaining = dict(updates)
    output = []
    for binding in bindings:
        if binding.key in removed:
            continue
        if binding.key in remaining:
            value = remaining.pop(binding.key)
            output.append(env_assignment(binding.key, value))
        else:
            output.append(binding.original.string)
    text = "".join(output)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + "".join(env_assignment(name, value) for name, value in remaining.items())


def env_assignment(name, value):
    if "\x00" in value or "\n" in value or "\r" in value:
        raise SetupError("Deployment settings must be single-line values")
    escaped = value.replace("'", "\\'")
    return f"{name}='{escaped}'\n"


def write_transaction(writes, originals):
    completed = []
    original_modes = {path: stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600 for path in writes}
    try:
        for path, content in writes.items():
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.parent.name in {".secrets", "tls"}:
                os.chmod(path.parent, 0o700)
            completed.append(path)
            if content is None:
                path.unlink(missing_ok=True)
            else:
                mode = 0o644 if path.parent.name == "tls" and path.suffix == ".crt" else 0o600
                if mode == 0o600:
                    atomic_write(path, content)
                else:
                    atomic_write(path, content, mode=mode)
    except (OSError, KeyboardInterrupt):
        for path in reversed(completed):
            original = originals[path]
            if original is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, original, mode=original_modes[path])
        raise SetupError("Save failed; previous configuration was restored") from None


def validate_secret(name, value):
    if not is_secret_name(name):
        raise SetupError("Unknown secret name")
    if not value or not value.strip() or "\x00" in value or "\n" in value or "\r" in value:
        raise SetupError("Credentials must be nonempty single-line values")
    if name in {"SANDBOX_API_TOKEN", "TRANSCRIPTION_API_TOKEN"} and len(value) < 32:
        raise SetupError("Authentication credentials must contain at least 32 characters")
    if value in WEAK_PASSWORDS:
        raise SetupError("Replace the placeholder credential")


class Installation:
    def __init__(self, root, recovering=False):
        self.root = Path(root).resolve()
        self.source = self.root / "src"
        self.data = self.root / "data"
        if self.data.is_symlink():
            raise SetupError("Runtime data directory must not be a symlink")
        self.directory = self.data / ".secrets"
        if self.directory.is_symlink() or (self.directory.exists() and not self.directory.is_dir()):
            raise SetupError("Secret directory must be a real directory")
        self.env_path = self.data / ".env"
        self.env_original = read_regular(self.env_path)
        self.env_text = (self.env_original or b"").decode("utf-8")
        self.values, _ = parse_environment(self.env_text)
        defaults = read_regular(self.data / "config/agent.yaml")
        if defaults is None:
            defaults = read_regular(self.source / "config/agent.yaml")
        try:
            parsed = yaml.safe_load(defaults.decode()) if defaults else {}
            if parsed is not None and not isinstance(parsed, dict):
                raise ValueError
        except (yaml.YAMLError, ValueError):
            raise SetupError("Operational configuration must be valid YAML with a mapping at its root") from None
        self.defaults = AppConfig(parsed or {})
        unsupported = [name for name in self.values if self.values[name] and
                       any(part in name for part in ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "SIGNING_KEY"))
                       and name not in ADVANCED_ENV_NAMES
                       and not is_secret_name(name.removesuffix("_FILE"))]
        if unsupported:
            raise SetupError("Review unsupported credential settings before migration: " + ", ".join(sorted(unsupported)))
        self.secret_values = {}
        self.tls_files = {}
        self.prompt_copy = None
        self.prompt_original = read_regular(self.data / "config/AGENT.md")
        self.snapshots = {}
        self.migrated = set()
        self.state_path = self.directory / "state.json"
        state = read_regular(self.state_path)
        self.state_original = state
        self.overlay_original = read_regular(self.data / ".compose-secrets.yaml")
        self.state = json.loads(state) if state else {"pending": []}
        if (not isinstance(self.state, dict)
                or any(not isinstance(self.state.get(key, []), list)
                       or any(not isinstance(name, str) for name in self.state.get(key, []))
                       for key in ("pending", "removed"))
                or any(key in self.state and type(self.state[key]) is not bool
                       for key in ("setup_complete", "credential_volume_sudo", "previous_credential_volume_sudo"))
                or any(key in self.state and (not isinstance(self.state[key], str)
                                              or not re.fullmatch(r"assistant-credentials-[a-f0-9]{16}", self.state[key]))
                       for key in ("credential_volume", "previous_credential_volume"))):
            raise SetupError("Saved setup state is invalid. Restore data/.secrets/state.json from a trusted backup.")
        self.credential_volume = None
        self.use_credential_volume = bool(self.state.get("credential_volume"))
        if self.state.get("previous_credential_volume"):
            self.use_sudo = platform.system() == "Linux" and bool(self.state.get("previous_credential_volume_sudo"))
        if self.use_credential_volume:
            self.use_sudo = platform.system() == "Linux" and bool(self.state.get("credential_volume_sudo"))
            from .credential_volume import CredentialVolume
            name = self.state["credential_volume"]
            if not re.fullmatch(r"assistant-credentials-[a-f0-9]{16}", name):
                raise SetupError("Invalid credential volume name in setup state")
            self.credential_volume = CredentialVolume(self, name)
            try:
                self.credential_volume.load()
            except DockerCommandError as error:
                raise DockerCommandError(
                    "Could not load saved credentials from Docker. " + str(error),
                    reason=error.reason,
                ) from None
        if not recovering and self.read_credential("rotation.json"):
            raise SetupError("Database rotation recovery is pending; run secrets recover first")
        names = set(SECRET_NAMES) | {name for name in self.values if is_secret_name(name)}
        names |= {name.removesuffix("_FILE") for name in self.values if is_secret_name(name.removesuffix("_FILE"))}
        if self.directory.exists():
            names |= {path.name for path in self.directory.iterdir() if is_secret_name(path.name)}
        if self.credential_volume:
            names |= {name for name in self.credential_volume.record.get("values", {}) if is_secret_name(name)}
        for name in sorted(names):
            if self.credential_volume and read_regular(self.directory / name) is not None:
                raise SetupError("Host credential files conflict with Docker volume storage; move them aside before setup: " + name)
            stored = self.read_credential(name)
            self.snapshots[name] = stored
            value = stored.decode("utf-8") if stored is not None else None
            inline = self.values.get(name) or None
            pointer = self.values.get(name + "_FILE")
            if inline and pointer:
                raise SetupError("Conflicting inline/file credential: " + name)
            if pointer:
                source = Path(pointer)
                if not source.is_absolute():
                    source = self.root / source
                if str(source).startswith("/run/secrets/"):
                    if value is None:
                        raise SetupError("Missing host source for " + name)
                else:
                    data = read_regular(source)
                    if data is None:
                        raise SetupError("Missing file credential: " + name)
                    inline = data.decode("utf-8").removesuffix("\n").removesuffix("\r")
            if inline:
                if value is not None and inline != value:
                    raise SetupError("Stored and legacy credentials disagree: " + name)
                value = inline
                self.migrated.add(name)
            if value:
                self.secret_values[name] = value

    def read_credential(self, name):
        if self.credential_volume:
            value = self.credential_volume.record.get("values", {}).get(name)
            return value.encode("utf-8") if value is not None else None
        return read_regular(self.directory / name)

    def grants_secret(self, name, service):
        if name == "OPENROUTER_API_KEY" and service == "downloader":
            return (security_value(self, "AGENT_SECURITY_DECISION_ENABLED", False) == "true"
                    and (security_value(self, "AGENT_SECURITY_DECISION_PROVIDER", "openrouter") or "openrouter") == "openrouter")
        return True

    def credential_grants(self):
        return {
            service: sorted(key.removesuffix("_FILE") for key, value in spec.get("environment", {}).items()
                            if key.endswith("_FILE") and str(value).startswith("/run/secrets/"))
            for service, spec in self.overlay()["services"].items()
            if any(str(value).startswith("/run/secrets/") for value in spec.get("environment", {}).values())
        }

    def write_credential(self, name, value):
        if self.credential_volume:
            values = dict(self.credential_volume.record.get("values", {}))
            if value is None:
                values.pop(name, None)
            else:
                values[name] = value.decode("utf-8")
            self.credential_volume.write(values, self.credential_grants())
        elif value is None:
            (self.directory / name).unlink(missing_ok=True)
        else:
            atomic_write(self.directory / name, value)

    def remove_settings(self, prefix):
        if not prefix.startswith(("CALENDAR_", "ZOOM_", "DROPBOX_")):
            raise SetupError("Only optional integration settings can be removed here")
        self.values = {name: value for name, value in self.values.items() if not name.startswith(prefix)}
        self.secret_values = {name: value for name, value in self.secret_values.items() if not name.startswith(prefix)}

    def copy_prompt_example(self):
        example = read_regular(self.source / "config/AGENT.md.example")
        if not example or not example.strip():
            raise SetupError("The prompt example is missing or empty; create data/config/AGENT.md manually")
        self.prompt_copy = example

    def overlay(self):
        services = {}
        declarations = {}
        compose_source = read_regular(self.source / "docker-compose.yml")
        compose_services = (yaml.safe_load(compose_source) or {}).get("services", {}) if compose_source else None
        staged_sandbox = compose_services is not None and "sandbox-init" in compose_services
        for name in sorted(self.secret_values):
            if name.startswith("CALENDAR_") and self.values.get("CALENDAR_ENABLED", "").lower() == "false":
                continue
            if name.startswith("ZOOM_") and self.values.get("ZOOM_ENABLED", "").lower() == "false":
                continue
            if name.startswith("DROPBOX_") and self.values.get("DROPBOX_ENABLED", "").lower() == "false":
                continue
            if name == "TRANSCRIPTION_API_TOKEN" and self.values.get("TRANSCRIPTION_ENABLED", "").lower() == "false":
                continue
            declarations[name] = {"file": str(self.directory / name)}
            for service in secret_services(name):
                if not self.grants_secret(name, service):
                    continue
                if compose_services is not None and service not in compose_services:
                    continue
                if service == "sandbox-init" and not staged_sandbox:
                    continue
                spec = services.setdefault(service, {"environment": {}, "secrets": []})
                spec["environment"][name] = None
                if service == "sandbox" and name == "SANDBOX_API_TOKEN" and staged_sandbox:
                    spec["environment"][name + "_FILE"] = "/run/assistant/runtime/current/SANDBOX_API_TOKEN"
                    continue
                spec["environment"][name + "_FILE"] = "/run/secrets/" + name
                spec["secrets"].append(name)
            embedding_settings = {}
            for name, default in (("AGENT_EMBEDDINGS_MODEL", "embeddinggemma"),
                          ("AGENT_EMBEDDINGS_ENABLED", True),
                          ("AGENT_EMBEDDINGS_BASE_URL", "http://ollama:11434")):
                value = self.values.get(name, self.defaults.get(PROMPT_DEFAULTS[name], default))
                embedding_settings[name] = str(value).lower() if isinstance(value, bool) else str(value)
            if compose_services is None or "ollama-model-puller" in compose_services:
                services["ollama-model-puller"] = {"environment": embedding_settings}
        if self.use_credential_volume:
            from .credential_volume import CredentialVolume
            for service, spec in services.items():
                if spec.pop("secrets", []):
                    spec["volumes"] = [{
                        "type": "volume", "source": "assistant-credentials",
                        "target": "/run/secrets", "read_only": True,
                        "volume": {"nocopy": True, "subpath": "services/" + service},
                    }]
            return {
                "services": services,
                "volumes": {"assistant-credentials": {
                    "external": True,
                    "name": self.state.get("credential_volume") or CredentialVolume.name_for(self.root),
                }},
            }
        return {"services": services, "secrets": declarations}

    def save(self):
        with installation_lock(self.root):
            self.save_locked()

    def assert_current(self):
        if (read_regular(self.env_path) != self.env_original or read_regular(self.state_path) != self.state_original
                or read_regular(self.data / ".compose-secrets.yaml") != self.overlay_original):
            raise SetupError("Configuration changed during this session; reopen setup")
        if self.credential_volume:
            self.credential_volume.assert_current()
        for name, snapshot in self.snapshots.items():
            if self.credential_volume and read_regular(self.directory / name) is not None:
                raise SetupError("Host credential files appeared during this session; reopen setup")
            if self.read_credential(name) != snapshot:
                raise SetupError("Credentials changed during this session; reopen setup")

    def save_locked(self):
        self.assert_current()
        if not self.secret_values.get("SANDBOX_API_TOKEN"):
            self.secret_values["SANDBOX_API_TOKEN"] = secrets.token_urlsafe(32)
        self.secret_values["SANDBOX_REVOKE_TOKEN"] = hmac.new(
            self.secret_values["SANDBOX_API_TOKEN"].encode(), b"assistant-sandbox-revoke-v1", hashlib.sha256,
        ).hexdigest()
        if self.values.get("TRANSCRIPTION_ENABLED") == "true":
            if not self.secret_values.get("TRANSCRIPTION_API_TOKEN"):
                self.secret_values["TRANSCRIPTION_API_TOKEN"] = secrets.token_urlsafe(32)
            self.values.setdefault("TRANSCRIPTION_BASE_URL", "http://transcription:8080")
            self.values.setdefault("TRANSCRIPTION_LANGUAGE", "auto")
            current_profiles = [p.strip() for p in self.values.get("COMPOSE_PROFILES", "").split(",") if p.strip()]
            if current_profiles and "voice" not in current_profiles:
                current_profiles.append("voice")
                self.values["COMPOSE_PROFILES"] = ",".join(current_profiles)
        elif self.values.get("TRANSCRIPTION_ENABLED") == "false":
            current_profiles = [p.strip() for p in self.values.get("COMPOSE_PROFILES", "").split(",") if p.strip()]
            if "voice" in current_profiles:
                current_profiles.remove("voice")
                self.values["COMPOSE_PROFILES"] = ",".join(current_profiles)
        for name, value in self.secret_values.items():
            validate_secret(name, value)
        service_tls_names = {"postgres.crt", "postgres.key", "sandbox.crt", "sandbox.key"}
        tls_directory = self.root / "data/private/tls"
        existing_service_tls = set()
        for name in service_tls_names:
            path = tls_directory / name
            for parent in (path.parent, *path.parent.parents):
                if parent == self.root:
                    break
                if parent.is_symlink():
                    raise SetupError("TLS configuration directories must not be symlinks")
            if read_regular(path) is not None:
                existing_service_tls.add(name)
        if existing_service_tls and existing_service_tls != service_tls_names:
            raise SetupError("Internal service TLS files are incomplete; restore or rotate the full certificate set")
        if not existing_service_tls:
            self.tls_files.update(prepare_internal_tls())
        else:
            import ssl
            for service in ("postgres", "sandbox"):
                certificate, key = tls_directory / (service + ".crt"), tls_directory / (service + ".key")
                try:
                    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(certificate, key, password="")
                    verified = subprocess.run(["openssl", "verify", "-verify_hostname", service, "-CAfile", str(certificate), str(certificate)],
                                              capture_output=True, timeout=15)
                except (ssl.SSLError, OSError, subprocess.TimeoutExpired):
                    raise SetupError("Internal TLS certificate/key validation failed for " + service) from None
                if verified.returncode:
                    raise SetupError("Internal TLS certificate identity or validity failed for " + service)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        original_values, _ = parse_environment(self.env_text)
        updates = {key: value for key, value in self.values.items()
               if not is_secret_name(key.removesuffix("_FILE")) and original_values.get(key) != value}
        updates["COMPOSE_FILE"] = str(self.source / "docker-compose.yml") + os.pathsep + str(self.data / ".compose-secrets.yaml")
        updates["SANDBOX_SHARED_ROOT_HOST"] = str(self.root / "data/share")
        updates["SANDBOX_DOCS_ROOT_HOST"] = str(self.root / "docs")
        removed = {key for key in original_values if key not in self.values or is_secret_name(key.removesuffix("_FILE"))}
        deleted_secrets = {name for name, snapshot in self.snapshots.items() if snapshot is not None and name not in self.secret_values}
        deleted_secrets |= self.migrated - self.secret_values.keys()
        changed = {name for name, value in self.secret_values.items()
                   if self.snapshots.get(name) != value.encode("utf-8")}
        # A secret granted to different services (e.g. OpenRouter screening toggled) must be re-applied.
        previous_overlay = yaml.safe_load(read_regular(self.data / ".compose-secrets.yaml") or b"") or {}
        if previous_overlay:
            def granted(overlay, name):
                return {service for service, spec in (overlay.get("services") or {}).items()
                        if (spec.get("environment") or {}).get(name + "_FILE")}
            current_overlay = self.overlay()
            changed |= {name for name in self.secret_values if granted(previous_overlay, name) != granted(current_overlay, name)}
        self.state["pending"] = sorted(set(self.state.get("pending", [])) | changed | self.migrated | deleted_secrets)
        storage_changed = self.use_credential_volume != bool(self.credential_volume)
        if storage_changed:
            self.state["pending"] = sorted(set(self.state["pending"]) | self.secret_values.keys())
        self.state["removed"] = sorted((set(self.state.get("removed", [])) | deleted_secrets) - self.secret_values.keys())
        writes = {self.directory / name: value.encode("utf-8") for name, value in self.secret_values.items()}
        writes.update({self.directory / name: None for name in deleted_secrets})
        volume_previous = None
        if self.use_credential_volume:
            from .credential_volume import CredentialVolume
            if not self.credential_volume:
                volume = CredentialVolume(self, CredentialVolume.name_for(self.root))
                volume.load(create=True)
                if (volume.record.get("values") and volume.record.get("values") != self.secret_values
                        and self.state.get("previous_credential_volume") != volume.name):
                    raise SetupError("Credential volume already contains different data; no host credentials were removed")
            else:
                volume = self.credential_volume
            volume_previous = volume.record
            volume_values = dict(volume.record.get("values", {}))
            for path, content in writes.items():
                if content is None:
                    volume_values.pop(path.name, None)
                else:
                    volume_values[path.name] = content.decode("utf-8")
            self.state["credential_volume"] = volume.name
            self.state["credential_volume_sudo"] = bool(getattr(self, "use_sudo", False))
            self.state.pop("previous_credential_volume", None)
            self.state.pop("previous_credential_volume_sudo", None)
            # Only remove host sources after the volume has verified the copy.
            writes = {path: None for path in writes}
        elif self.credential_volume:
            self.state["previous_credential_volume"] = self.credential_volume.name
            self.state["previous_credential_volume_sudo"] = bool(getattr(self, "use_sudo", False))
            self.state.pop("credential_volume", None)
            self.state.pop("credential_volume_sudo", None)
        writes[self.data / ".compose-secrets.yaml"] = yaml.safe_dump(self.overlay(), sort_keys=True).encode()
        writes[self.env_path] = render_environment(self.env_text, updates, removed).encode()
        writes[self.state_path] = json.dumps(self.state).encode()
        config_path = self.data / "config/agent.yaml"
        if read_regular(config_path) is None:
            defaults = read_regular(self.source / "config/agent.yaml")
            if defaults is not None:
                writes[config_path] = defaults
        for name, value in self.tls_files.items():
            writes[self.root / "data/private/tls" / name] = value
        for name in existing_service_tls:
            writes[tls_directory / name] = read_regular(tls_directory / name)
        prompt = self.data / "config/AGENT.md"
        if self.prompt_copy is not None:
            if read_regular(prompt) != self.prompt_original:
                raise SetupError("Assistant instructions changed during setup; reopen setup")
            writes[prompt] = self.prompt_copy
        overrides = self.data / OVERRIDES_FILE
        if read_regular(overrides) is None:
            writes[overrides] = overrides_template(self.root, self.defaults).encode()
        originals = {}
        for path in writes:
            for parent in (path.parent, *path.parent.parents):
                if parent == self.root:
                    break
                if parent.is_symlink():
                    raise SetupError("Configuration directories must not be symlinks")
            originals[path] = read_regular(path)
        try:
            if volume_previous is not None:
                volume.write(volume_values, self.credential_grants())
                if not self.credential_volume:
                    probe = self.overlay()
                    for spec in probe["services"].values():
                        spec["image"] = "python:3.12-slim"
                    self.docker(["compose", "-f", "-", "config", "--quiet"], input_text=yaml.safe_dump(probe))
                    volume.verify_mount()
            write_transaction(writes, originals)
        except BaseException:
            if volume_previous is not None:
                volume.write(volume_previous.get("values", {}), volume_previous.get("grants", {}))
            raise
        if self.use_credential_volume:
            self.credential_volume = volume
        else:
            self.credential_volume = None
        self.env_original = read_regular(self.env_path)
        self.env_text = self.env_original.decode()
        self.values, _ = parse_environment(self.env_text)
        self.state_original = read_regular(self.state_path)
        self.overlay_original = read_regular(self.data / ".compose-secrets.yaml")
        self.snapshots = {name: self.read_credential(name) for name in self.secret_values}
        self.prompt_original = read_regular(prompt)
        self.prompt_copy = None
        self.migrated.clear()

    def compose(self, arguments, input_text=None, timeout=180):
        return self.docker(self.compose_prefix() + arguments, input_text=input_text, timeout=timeout)

    def compose_prefix(self):
        command = ["compose", "--project-directory", str(self.source), "--env-file", str(self.env_path),
                   "-f", str(self.source / "docker-compose.yml")]
        if (self.data / ".compose-secrets.yaml").is_file():
            command += ["-f", str(self.data / ".compose-secrets.yaml")]
        return command

    def docker_environment(self):
        return {key: value for key, value in os.environ.items()
                if not is_secret_name(key.removesuffix("_FILE")) and key not in {"COMPOSE_FILE", "COMPOSE_PROFILES", "COMPOSE_PROJECT_NAME"}}

    def compose_interactive(self, arguments, stdout=None, stderr=None):
        """Run Compose attached to the terminal (or stdout) so operators see real output; returns the exit code."""
        prefix = ["sudo", "-n", "docker"] if getattr(self, "use_sudo", False) else ["docker"]
        try:
            runner = getattr(self, "command_runner", subprocess.run)
            if getattr(self, "use_sudo", False):
                prepare_sudo(["docker", *self.compose_prefix(), *arguments],
                             runner=runner, environment=self.docker_environment())
            return runner([*prefix, *self.compose_prefix(), *arguments], env=self.docker_environment(),
                          stdout=stdout, stderr=stderr).returncode
        except subprocess.TimeoutExpired:
            raise DockerCommandError("Docker command timed out. Inspect service state before retrying; the command may have partially completed.") from None
        except OSError:
            raise DockerCommandError("Docker command could not be launched; check that Docker and any required sudo are installed") from None

    def docker(self, arguments, input_text=None, timeout=180):
        environment = self.docker_environment()
        elevated = getattr(self, "use_sudo", False)
        try:
            prefix = ["sudo", "-n", "docker"] if elevated else ["docker"]
            runner = getattr(self, "command_runner", subprocess.run)
            if elevated:
                prepare_sudo(["docker", *arguments], runner=runner, environment=environment)
            result = runner([*prefix, *arguments], input=input_text, text=True, capture_output=True,
                            stdin=subprocess.DEVNULL if input_text is None else None,
                            env=environment, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise DockerCommandError("Docker command timed out; command output was suppressed") from None
        except OSError:
            raise DockerCommandError("Docker command could not be launched; check that Docker and any required sudo are installed") from None
        if result.returncode:
            stderr = (result.stderr or "").lower()
            if elevated and any(message in stderr for message in (
                "is not in the sudoers file", "is not allowed to execute",
                "not allowed to run sudo",
            )):
                raise DockerCommandError("sudo authorization failed: this account is not permitted to run the required command.")
            if elevated and any(message in stderr for message in (
                "sudo: a password is required", "sudo: a terminal is required", "is not in the sudoers file",
                "is not allowed to execute", "sudo: authentication is required", "sudo: interactive authentication is required",
            )):
                raise DockerCommandError(
                    "Noninteractive sudo authorization failed. The command was not automatically retried. "
                    "Return to the interactive action to renew authorization; Assistant never reads your password."
                )
            raise docker_failure(arguments, result)
        return result.stdout

    def stage_sandbox_runtime(self):
        self.compose(["stop", "sandbox"])
        self.compose(["run", "--rm", "--no-deps", "sandbox-init"])

    def verify_sandbox_runtime(self):
        self.compose(["exec", "-T", "--interactive=false", "sandbox", "python", "-c",
                      "import ssl,urllib.request; from pathlib import Path; "
                      "root=Path('/run/assistant/runtime/current'); "
                      "context=ssl.create_default_context(cafile=str(root/'sandbox.crt')); "
                      "token=(root/'SANDBOX_API_TOKEN').read_text().strip(); "
                      "request=urllib.request.Request('https://sandbox:8080/health',headers={'Authorization':'Bearer '+token}); "
                      "response=urllib.request.urlopen(request,context=context,timeout=15); "
                      "assert response.status == 200; print('Sandbox authenticated TLS probe passed')"])

    def start_containers(self):
        self.startup_stage = "checking saved configuration"
        with installation_lock(self.root):
            self.assert_current()
            self.startup_stage = "Compose configuration"
            configuration = json.loads(self.compose(["config", "--format", "json"]))
            services = sorted(set(configuration["services"]) & LONG_RUNNING_SERVICES)
            if not services:
                raise SetupError("No enabled long-running Assistant services were found")
            staged_sandbox = "sandbox-init" in configuration["services"]
            if staged_sandbox:
                self.startup_stage = "building sandbox initialization image"
                self.compose(["build", "sandbox-init"], timeout=1800)
                self.startup_stage = "staging sandbox protected files"
                self.stage_sandbox_runtime()
            self.startup_stage = "building and starting containers"
            self.compose(["up", "-d", "--build"], timeout=1800)
            self.startup_stage = "waiting for container health"
            self.compose(["up", "-d", "--wait", "--wait-timeout", "180", *services], timeout=240)
            self.startup_stage = "checking container status"
            payload = self.compose(["ps", "--all", "--format", "json"])
            try:
                parsed = json.loads(payload)
                rows = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                rows = [json.loads(line) for line in payload.splitlines() if line.strip()]
            if any(row.get("Service") in configuration["services"] and row.get("Service") not in LONG_RUNNING_SERVICES and
                   (row.get("State") == "dead" or (row.get("State") == "exited" and int(row.get("ExitCode") or 0) != 0)) for row in rows):
                raise SetupError("A startup helper failed; inspect docker compose --env-file data/.env ps -a and logs")
            active = [row for row in rows if row.get("Service") in services]
            if set(services) != {row.get("Service") for row in active}:
                raise SetupError("Some enabled containers are missing; inspect docker compose --env-file data/.env ps -a")
            if any(row.get("State") != "running" or row.get("Health") not in (None, "", "healthy") for row in active):
                raise SetupError("Some containers are not running or healthy; inspect docker compose --env-file data/.env ps -a and logs")
            if staged_sandbox:
                self.startup_stage = "verifying sandbox protected-file activation"
                self.verify_sandbox_runtime()
            identities = [row.get("ID") or row.get("Name") for row in active]
            if not all(identities):
                raise SetupError("Docker did not return container identities; restart policies were not verified")
            self.startup_stage = "verifying restart policies"
            self.docker(["update", "--restart", "unless-stopped", *identities])
            policies = [json.loads(line) for line in self.docker(
                ["inspect", "--format", "{{json .HostConfig.RestartPolicy.Name}}", *identities]
            ).splitlines() if line.strip()]
            verified = policies == ["unless-stopped"] * len(identities)
            if not verified:
                raise SetupError("Container restart policies could not be verified")
            self.startup_stage = "recording credential activation"
            deleted = set(self.state.get("removed", []))
            grants = self.overlay()["services"]
            applied = {name for name in self.state.get("pending", []) if any(service in services and
                       (name in deleted or grants.get(service, {}).get("environment", {}).get(name + "_FILE")) for service in secret_services(name))}
            self.state["pending"] = sorted(set(self.state.get("pending", [])) - applied)
            self.state["removed"] = sorted(deleted - applied)
            atomic_write(self.state_path, json.dumps(self.state).encode())
            self.state_original = read_regular(self.state_path)
            return rows

    def apply(self, names):
        with installation_lock(self.root):
            self.assert_current()
            return self.apply_locked(names)

    def retire_previous_volume(self):
        """Activate host credential mounts, detach every container, then destroy the old volume."""
        name = self.state.get("previous_credential_volume")
        if not name:
            return []
        if not re.fullmatch(r"assistant-credentials-[a-f0-9]{16}", name):
            raise SetupError("Invalid previous credential volume name in setup state")
        with installation_lock(self.root):
            self.assert_current()
            for secret, value in self.secret_values.items():
                if read_regular(self.directory / secret) != value.encode("utf-8"):
                    raise SetupError("Host credential files are incomplete; the credential volume was kept")
            targets = []
            if self.docker(["volume", "ls", "-q", "--filter", "name=^" + name + "$"]).strip():
                project = json.loads(self.compose(["config", "--format", "json"]))["name"]
                targets = self.apply_locked(sorted(self.secret_values))

                def users():
                    rows = self.docker(["ps", "-a", "--filter", "volume=" + name, "--format",
                                        '{{.ID}}\t{{.State}}\t{{.Label "com.docker.compose.project"}}\t'
                                        '{{.Label "com.docker.compose.service"}}\t{{.Names}}'])
                    return [line.split("\t") for line in rows.splitlines() if line.strip()]

                containers = users()
                outside = [row[4] for row in containers if row[2] != project]
                if outside:
                    raise SetupError("Credential volume is used by containers outside this installation ("
                                     + ", ".join(outside) + "); it was kept")
                running = sorted({row[3] for row in containers if row[1] == "running"})
                if running:
                    self.compose(["up", "-d", "--no-deps", "--force-recreate", *running])
                    targets = sorted(set(targets) | set(running))
                # Stopped containers are recreated with host mounts by the next "compose up".
                stopped = [row[0] for row in users() if row[1] != "running"]
                if stopped:
                    self.docker(["rm", *stopped])
                if users():
                    raise SetupError("Credential volume is still in use; it was kept and removal will be retried")
                self.docker(["volume", "rm", name])
            self.state.pop("previous_credential_volume", None)
            self.state.pop("previous_credential_volume_sudo", None)
            atomic_write(self.state_path, json.dumps(self.state).encode())
            self.state_original = read_regular(self.state_path)
            return targets

    def apply_locked(self, names):
        if "SANDBOX_API_TOKEN" in names:
            names = list(set(names) | {"SANDBOX_REVOKE_TOKEN"})
        grants = self.overlay()["services"]
        deleted = set(self.state.get("removed", []))
        affected = {service for name in names for service in secret_services(name)
                if name in deleted or grants.get(service, {}).get("environment", {}).get(name + "_FILE")}
        running = set(self.compose(["ps", "--status", "running", "--services"]).split())
        targets = sorted(affected & running)
        compose_source = read_regular(self.source / "docker-compose.yml")
        configured = (yaml.safe_load(compose_source) or {}).get("services", {}) if compose_source else {}
        staged_sandbox = "SANDBOX_API_TOKEN" in names and "sandbox-init" in configured and "sandbox" in running
        if staged_sandbox:
            self.stage_sandbox_runtime()
            targets = sorted(set(targets) | {"sandbox"})
        if targets:
            arguments = ["up", "-d", "--no-deps", "--force-recreate", *targets]
            if staged_sandbox:
                arguments[2:2] = ["--wait", "--wait-timeout", "90"]
            self.compose(arguments)
        if staged_sandbox:
            self.verify_sandbox_runtime()
        pending = set(self.state.get("pending", []))
        applied = {name for name in names if any(service in running and
               (name in deleted or grants.get(service, {}).get("environment", {}).get(name + "_FILE")) for service in secret_services(name))}
        if staged_sandbox:
            applied.add("SANDBOX_API_TOKEN")
        self.state["pending"] = sorted(pending - applied)
        self.state["removed"] = sorted(deleted - applied)
        atomic_write(self.state_path, json.dumps(self.state).encode())
        self.state_original = read_regular(self.state_path)
        return targets

    def rotate_database(self, replacement):
        with installation_lock(self.root):
            self.assert_current()
            if self.read_credential("rotation.json"):
                raise SetupError("Database rotation recovery is pending; run secrets recover first")
            self.rotate_database_locked(replacement)

    def rotate_database_locked(self, replacement):
        validate_secret("POSTGRES_PASSWORD", replacement)
        overrides, _ = parse_environment((read_regular(self.data / OVERRIDES_FILE) or b"").decode())
        if "DATABASE_URL" in self.secret_values or any(self.values.get(name) or overrides.get(name) for name in ("AGENT_DATABASE_HOST", "AGENT_DATABASE_USER", "AGENT_DATABASE_NAME")):
            raise SetupError("Custom database deployments require their own coordinated rotation procedure")
        previous = self.secret_values.get("POSTGRES_PASSWORD")
        if not previous:
            raise SetupError("Existing database credential is required for recovery")
        running = set(self.compose(["ps", "--status", "running", "--services"]).split())
        if "postgres" not in running:
            raise SetupError("Start PostgreSQL before coordinated password rotation; no credential was changed")
        consumers = sorted(running & set(AGENT_SERVICES))
        self.directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.write_credential("rotation.json", json.dumps({"previous": previous, "replacement": replacement, "consumers": consumers}).encode())
        try:
            if consumers:
                self.compose(["stop", *consumers])
            self.set_database_password(replacement)
            self.secret_values["POSTGRES_PASSWORD"] = replacement
            self.save_locked()
            self.verify_database_password(replacement)
            self.compose(["up", "-d", "--wait", "--wait-timeout", "90", "--no-deps", "--force-recreate", "postgres", *consumers])
            self.verify_database_password(replacement)
            self.state["pending"] = [name for name in self.state.get("pending", []) if name != "POSTGRES_PASSWORD"]
            atomic_write(self.state_path, json.dumps(self.state).encode())
            self.state_original = read_regular(self.state_path)
            self.write_credential("rotation.json", None)
        except BaseException:
            raise SetupError("Database rotation is incomplete. Consumers may be stopped; run secrets recover before any other changes") from None

    def set_database_password(self, value):
        escaped = value.replace("'", "''")
        statement = "SET log_statement = 'none';\nSET log_min_error_statement = 'panic';\nSET standard_conforming_strings = on;\nALTER ROLE assistant PASSWORD '" + escaped + "';\n"
        self.compose(["exec", "-T", "postgres", "psql", "--no-psqlrc", "-U", "assistant", "-d", "assistant", "-v", "ON_ERROR_STOP=1"], statement)

    def verify_database_password(self, value):
        self.compose(["exec", "-T", "postgres", "sh", "-c", 'IFS= read -r PGPASSWORD; export PGPASSWORD PGSSLMODE=verify-full PGSSLROOTCERT=/run/assistant/tls/postgres.crt; exec psql --no-psqlrc -h postgres -U assistant -d assistant -v ON_ERROR_STOP=1 -c "SELECT 1"'], value + "\n")


def recover_database(root, prepare=None):
    root = Path(root).resolve()
    with installation_lock(root):
        recover_database_locked(root, prepare=prepare)


def recover_database_locked(root, prepare=None):
    state = json.loads(read_regular(root / "data/.secrets/state.json") or b"{}")
    if state.get("credential_volume"):
        instance = Installation(root, recovering=True)
        contents = instance.read_credential("rotation.json")
        if not contents:
            raise SetupError("No database rotation needs recovery")
        record = json.loads(contents)
        if prepare is not None:
            prepare(instance)
        instance.set_database_password(record["previous"])
        instance.write_credential("POSTGRES_PASSWORD", record["previous"].encode())
        instance.verify_database_password(record["previous"])
        instance.compose(["up", "-d", "--wait", "--wait-timeout", "90", "--no-deps", "--force-recreate", "postgres", *record["consumers"]])
        instance.verify_database_password(record["previous"])
        instance.write_credential("rotation.json", None)
        return
    path = root / "data/.secrets/rotation.json"
    contents = read_regular(path)
    if not contents:
        raise SetupError("No database rotation needs recovery")
    record = json.loads(contents)
    instance = object.__new__(Installation)
    instance.root = root
    instance.source = root / "src"
    instance.data = root / "data"
    instance.env_path = root / "data/.env"
    if prepare is not None:
        prepare(instance)
    instance.set_database_password(record["previous"])
    atomic_write(root / "data/.secrets/POSTGRES_PASSWORD", record["previous"].encode())
    instance.verify_database_password(record["previous"])
    instance.compose(["up", "-d", "--wait", "--wait-timeout", "90", "--no-deps", "--force-recreate", "postgres", *record["consumers"]])
    instance.verify_database_password(record["previous"])
    path.unlink()


def setup_failure_message(error, stage):
    location = ""
    trace = error.__traceback__
    while trace and trace.tb_next:
        trace = trace.tb_next
    if trace:
        code = trace.tb_frame.f_code
        location = f", {Path(code.co_filename).name}:{trace.tb_lineno} in {code.co_name}"
    kind = type(error).__name__
    guidance = "Check the named section and diagnostic location; no credential values are shown."
    if isinstance(error, PermissionError):
        guidance = "Your account cannot access an installation file. Check ownership and permissions; do not make secrets world-readable."
    elif isinstance(error, FileNotFoundError):
        guidance = "A required file or executable is missing. Check the selected file paths and installed tools."
    elif isinstance(error, UnicodeError):
        guidance = "A configuration or credential file is not valid UTF-8. No file contents are shown."
    elif isinstance(error, (json.JSONDecodeError, yaml.YAMLError)):
        guidance = "A saved configuration file has invalid syntax. No file contents are shown."
    return f"Setup could not complete during {stage} ({kind}{location}). {guidance}"


def main():
    parser = argparse.ArgumentParser(description="Assistant local administration, guided setup, and credential management")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    navigation = parser.add_mutually_exclusive_group()
    navigation.add_argument("--section", choices=SECTION_IDS, help="Edit one section of an existing installation")
    navigation.add_argument("--wizard", action="store_true", help="Open setup configuration (all sections with --plain)")
    parser.add_argument("--plain", action="store_true", help="Use the traditional line-oriented menus")
    commands = parser.add_subparsers(dest="command")
    secret_parser = commands.add_parser("secrets", help="Manage credentials without displaying values")
    secret_parser.add_argument("action", choices=("status", "update", "apply", "recover"))
    secret_parser.add_argument("name", nargs="?")
    commands.add_parser("tools", help="Open management at Services (legacy troubleshooting menus with --plain)")
    commands.add_parser("reset", help="Factory reset: rebuild the database while preserving settings, secrets and files")
    options = parser.parse_args()
    try:
        if options.command == "secrets" and options.action == "status":
            instance = Installation(options.root)
            for name in sorted(instance.secret_values):
                pending = name in instance.state.get("pending", []) or name in instance.migrated
                print(f"{name}: configured" + (" (activation pending)" if pending else " (saved; runtime health not verified)"))
            if not instance.secret_values:
                print("No credentials configured. Run setup first.")
            return
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise SetupError("Interactive setup requires a terminal; no files were changed")
        if options.plain:
            with authorization_handler(terminal_authorization):
                interactive(options)
        else:
            from .management_tui import ManagementApp
            ui = ManagementApp(options.root)
            with authorization_handler(ui.authorize):
                ui.run(options)
    except SetupError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(setup_failure_message(exc, getattr(options, "stage", "loading configuration")), file=sys.stderr)
        raise SystemExit(1) from None
    except (KeyboardInterrupt, EOFError):
        print("Cancelled. No pending prompt values were saved.")


def activate_embeddings(instance, confirm, console):
    command = ["run", "--rm", "--no-deps", "--entrypoint", "python", "task-agent", "-m", "assistant_agent.embedding_maintenance"]
    console.print("Inspect later: docker compose --env-file data/.env run --rm --no-deps --entrypoint python task-agent -m assistant_agent.embedding_maintenance status")
    console.print("Rebuild later: docker compose --env-file data/.env run --rm --no-deps --entrypoint python task-agent -m assistant_agent.embedding_maintenance reindex")
    if not confirm("Build the maintenance image and provision/verify the saved embedding model now?", False):
        return
    stage = "checking Docker access"
    arguments = ["info", "--format", "{{.ServerVersion}}"]
    try:
        ensure_docker_access(instance, confirm)

        def run(label, args, timeout=180):
            nonlocal stage, arguments
            stage, arguments = label, ["compose", "--env-file", "data/.env", *args]
            console.print(label + "...", markup=False)
            return instance.compose(args, timeout=timeout)

        run("Building the maintenance image", ["build", "task-agent"], timeout=1800)
        endpoint = instance.values.get("AGENT_EMBEDDINGS_BASE_URL", instance.defaults.get("agent.embeddings.base_url", "http://ollama:11434"))
        if endpoint.rstrip("/") == "http://ollama:11434":
            run("Starting managed Ollama", ["up", "-d", "--no-deps", "ollama"])
            run("Provisioning the embedding model", ["run", "--rm", "--no-deps", "ollama-model-puller"], timeout=1800)
        console.print(run("Verifying the embedding model", command + ["probe"]), markup=False)
        if confirm("Rebuild incompatible embeddings now? Source data is preserved; interrupted work can be resumed.", False):
            run("Starting PostgreSQL for reindexing", ["up", "-d", "--wait", "--wait-timeout", "180", "postgres"], timeout=240)
            console.print(run("Rebuilding incompatible embeddings", command + ["reindex"], timeout=1800), markup=False)
    except DockerCommandError as error:
        docker = "sudo docker" if getattr(instance, "use_sudo", False) else "docker"
        retry = docker + " " + " ".join(arguments)
        raise SetupError(
            f"Embedding provisioning failed during {stage}. Configuration is already saved; source data is preserved.\n"
            f"{error}\n"
            f"To see Docker diagnostics, run locally from the repository directory: {retry}\n"
            "Command output may contain credentials; do not share it without reviewing/redacting it.\n"
            "If sudo needs a password, enter it directly in your terminal; the wizard never collects it.\n"
            "After resolving the failure, retry ./run.sh --section embeddings."
        ) from None
    console.print("Recreate existing embedding consumers to activate saved settings: docker compose --env-file data/.env up -d --no-deps --force-recreate task-agent workspace-indexer deep-research-agent supervisor heartbeat")


def interactive(options):
    options.stage = "checking prerequisites"
    import questionary
    from rich.console import Console
    from rich.table import Table

    console = Console()

    def answer(question):
        result = question.unsafe_ask()
        if result is None:
            raise KeyboardInterrupt
        return result

    def confirm(label, default=False):
        return answer(questionary.confirm(label, default=default))

    if shutil.which("docker") is None:
        raise SetupError("Install Docker and Docker Compose before setup")
    if options.command == "secrets" and options.action == "recover":
        console.print("Recovery restores the previous database password and recreates its consumers.")
        if confirm("Run database rotation recovery?"):
            def prepare_recovery(instance):
                ensure_docker_access(instance, confirm)

            recover_database(options.root, prepare=prepare_recovery)
            console.print("Previous database credential restored. Check service health.")
        return
    options.stage = "loading saved configuration"
    instance = Installation(options.root)
    if instance.state.get("previous_credential_volume"):
        options.stage = "removing the previous credential volume"
        finish_host_storage(instance, console)
    if options.command in {"tools", "reset"}:
        if not instance.state.get("setup_complete"):
            raise SetupError("Complete and save setup before using " + options.command)
        options.stage = options.command
        run_maintenance(options.command, instance, answer, confirm, console)
        return
    if options.command == "secrets":
        if options.action == "apply":
            names = instance.state.get("pending", [])
            if options.name:
                if options.name not in names:
                    raise SetupError("This credential has no pending activation")
                names = [options.name]
            if names and confirm("Recreate affected running services? Brief downtime is expected."):
                ensure_docker_access(instance, confirm)
                targets = instance.apply(names)
                console.print("Recreated: " + (", ".join(targets) or "none; credentials remain pending until startup"))
            return
        name = options.name or answer(questionary.select("Credential to update", choices=sorted(instance.secret_values) or sorted(SECRET_NAMES)))
        if not is_secret_name(name):
            raise SetupError("Unknown secret name")
        console.print("Values are masked. Provider credentials must be obtained from the provider first.")
        console.print("Affected services: " + ", ".join(secret_services(name)))
        if name == "POSTGRES_PASSWORD":
            console.print("Database rotation stops consumers, updates the role, and recreates containers. Keep a current backup.")
        value = prompt_credential(answer, name, None)
        if not confirm("Save and coordinate database rotation?" if name == "POSTGRES_PASSWORD" else "Save this replacement credential?"):
            return
        if name == "POSTGRES_PASSWORD":
            ensure_docker_access(instance, confirm)
            instance.rotate_database(value)
        else:
            instance.secret_values[name] = value
            instance.save()
            if confirm("Activate now by recreating affected running services?"):
                ensure_docker_access(instance, confirm)
                instance.apply([name])
            else:
                console.print("Saved; activation pending. Run secrets apply when ready.")
        console.print("Update saved. Verify service connectivity before revoking an old provider credential.")
        return

    console.print("Assistant setup\nSecrets use protected files, not encryption. At review, you can move credentials into a private Docker volume.")
    if instance.migrated:
        console.print("Existing inline credentials will be moved out of .env only when you confirm the final save.")
    console.print("Use a dedicated assistant mailbox, never your personal mailbox.")
    existing_profiles = instance.values.get("COMPOSE_PROFILES", "").split(",")
    instance.values.setdefault("SETUP_EMAIL_ENABLED", str("email" in existing_profiles).lower())
    configured = bool(instance.env_original and instance.state.get("setup_complete"))
    if instance.env_original and not configured:
        original_values = instance.values
        instance.values = {name: str(value).lower() if isinstance(value, bool) else str(value)
                           for name, path in PROMPT_DEFAULTS.items()
                           if (value := instance.defaults.get(path)) is not None}
        instance.values.update(original_values)
        try:
            configured = not any(not message.startswith(OVERRIDES_FILE) for _, message in draft_problems(instance))
        finally:
            instance.values = original_values
    selected_section = getattr(options, "section", None)
    if selected_section and not configured:
        raise SetupError("Complete initial setup before using --section; run setup --wizard")
    embeddings_edited = False

    def text(name, label, default=""):
        options.stage = "setting " + name
        configured = instance.defaults.get(PROMPT_DEFAULTS.get(name, ""))
        baseline = str(configured) if configured is not None else default
        previous = instance.values.get(name, baseline) if name == "AGENT_EMBEDDINGS_DIMENSIONS" else instance.values.get(name) or baseline
        instance.values[name] = answer(questionary.text(label, default=previous)).strip()

    def credential(name):
        options.stage = "configuring " + name
        instance.secret_values[name] = prompt_credential(answer, name, instance.secret_values.get(name))

    def capability(name, label, default=False):
        options.stage = "selecting " + name
        configured = instance.defaults.get(PROMPT_DEFAULTS.get(name, ""))
        if configured is not None:
            default = instance.defaults.get_bool(PROMPT_DEFAULTS[name], default)
        enabled = confirm(label, instance.values.get(name, str(default)).lower() == "true")
        instance.values[name] = str(enabled).lower()
        return enabled

    def identity():
        text("AGENT_NAME", "Assistant name", "Assistant")
        text("AGENT_TIMEZONE", "Timezone (IANA identifier, e.g. Europe/London)", "UTC")
        text("ADMIN_NAME", "Your name")
        text("ADMIN_EMAIL", "Your email address for approvals and notifications")
        text("ORG_NAME", "Organization name (optional)")
        text("ORG_SECURITY_EMAIL", "Security contact email (optional)")
        text("ORG_INTERNAL_EMAIL_DOMAINS", "Internal email domains (comma-separated, optional)")

    def models():
        previous_url = instance.values.get("AGENT_LLM_BASE_URL") or instance.defaults.get("agent.llm.base_url") or "https://openrouter.ai/api/v1"
        provider = answer(questionary.select("Model provider", choices=["OpenRouter", "OpenAI-compatible"],
                                            default="OpenRouter" if "openrouter.ai" in previous_url else "OpenAI-compatible"))
        default = "https://openrouter.ai/api/v1" if provider == "OpenRouter" else "https://api.openai.com/v1"
        instance.values["AGENT_LLM_BASE_URL"] = previous_url if (provider == "OpenRouter") == ("openrouter.ai" in previous_url) else default
        text("AGENT_LLM_BASE_URL", "Model API base URL", default)
        credential("OPENROUTER_API_KEY" if provider == "OpenRouter" else "OPENAI_API_KEY")
        text("AGENT_LLM_MODEL", "Primary model identifier", "anthropic/claude-sonnet-4.6" if provider == "OpenRouter" else "gpt-4.1")
        text("AGENT_LLM_FALLBACK_MODEL", "Fallback model identifier (optional)", "openai/gpt-5.4" if provider == "OpenRouter" else "")
        instance.values["AGENT_SEARCH_ENABLED"] = str(provider == "OpenRouter").lower()
        if provider != "OpenRouter":
            instance.values["AGENT_FUSION_ENABLED"] = "false"
        current_password = instance.secret_values.get("POSTGRES_PASSWORD")
        if current_password and current_password not in WEAK_PASSWORDS:
            console.print("Existing database password retained. Use secrets update for coordinated rotation.")
        else:
            options.stage = "checking existing PostgreSQL data"
            try:
                database_initialized = (instance.root / "data/postgres/pgdata/PG_VERSION").exists()
            except PermissionError:
                raise SetupError("PostgreSQL data is private and no usable password is saved. Supply the existing database password or use coordinated password rotation; setup will not generate a replacement") from None
            if database_initialized:
                if current_password in WEAK_PASSWORDS:
                    raise SetupError("Existing database uses a placeholder password. Use secrets update POSTGRES_PASSWORD for coordinated rotation first")
                raise SetupError("Existing database detected. Supply its current password; setup will not replace it")
            instance.secret_values["POSTGRES_PASSWORD"] = prompt_credential(answer, "POSTGRES_PASSWORD", None)

    def embeddings():
        nonlocal embeddings_edited
        embeddings_edited = True
        if not capability("AGENT_EMBEDDINGS_ENABLED", "Enable local semantic search embeddings?", True):
            return
        console.print("EmbeddingGemma requires Ollama 0.11.10+, has a 2K-token input limit, and defaults to 768 dimensions. Changing models requires rebuilding existing vectors, not deleting data.")
        text("AGENT_EMBEDDINGS_MODEL", "Embedding model", "embeddinggemma")
        text("AGENT_EMBEDDINGS_BASE_URL", "Embedding API base URL", "http://ollama:11434")
        text("AGENT_EMBEDDINGS_DIMENSIONS", "Embedding dimensions (blank = model default)")
        text("AGENT_EMBEDDINGS_TIMEOUT_SECONDS", "Embedding request timeout in seconds", "20")

    def email():
        enabled = capability("SETUP_EMAIL_ENABLED", "Enable email intake and outgoing email?", True)
        if enabled:
            for name, label, default in (
                ("IMAP_HOST", "IMAP server", ""), ("IMAP_PORT", "IMAP TLS port", "993"),
                ("IMAP_USERNAME", "Assistant mailbox login", ""), ("SMTP_HOST", "SMTP server", ""),
                ("SMTP_PORT", "SMTP TLS/STARTTLS port", "587"), ("SMTP_USERNAME", "SMTP login", instance.values.get("IMAP_USERNAME", "")),
                ("SMTP_FROM", "Assistant sender address", instance.values.get("IMAP_USERNAME", "")),
                ("IMAP_FOLDER", "Incoming folder", "INBOX"), ("IMAP_ARCHIVE_FOLDER", "Processed-mail folder", "Archive"),
                ("IMAP_QUARANTINE_FOLDER", "Quarantine folder (rejected, held and unaccepted mail)", "Quarantine"),
                ("IMAP_SENT_FOLDER", "Sent-mail folder", "Sent"),
            ):
                text(name, label, default)
            credential("IMAP_PASSWORD")
            credential("SMTP_PASSWORD")
            text("EMAIL_ACTIONABLE_SENDERS", "Addresses allowed to start jobs", instance.values.get("ADMIN_EMAIL", ""))
            domain = instance.values.get("ADMIN_EMAIL", "").partition("@")[2]
            text("EMAIL_ALLOWED_RECIPIENT_DOMAINS", "Allowed outgoing recipient domains", domain)
            text("AGENT_EMAIL", "Assistant email identity", instance.values.get("SMTP_FROM", ""))
            console.print("Admin requests use a separate, ten-minute OTP/hash email confirmation. No signing key is needed. "
                          "Mail from senders that are not accepted is moved to the quarantine folder unread.")
        instance.values["AGENT_DEEP_RESEARCH_ENABLED"] = str(enabled and "openrouter.ai" in instance.values.get("AGENT_LLM_BASE_URL", "")).lower()
        instance.values.setdefault("AGENT_HEARTBEAT_ADMIN_DIGEST_INTERVAL_HOURS", str(
            instance.defaults.get("agent.heartbeat.admin_digest_interval_hours", 0)))

    def calendar():
        if capability("CALENDAR_ENABLED", "Enable calendar integration?"):
            console.print("Select the providers to keep. Deselecting a provider removes its saved settings and credentials when you save.")
            choices = [questionary.Choice(provider.title(), value=provider, checked=any(
                name.startswith("CALENDAR_" + provider.upper() + "_") for name in instance.secret_values
            )) for provider in ("google", "outlook", "zoho")]
            providers = prompt_calendar_providers(answer, choices, console)
            for provider in ("google", "outlook", "zoho"):
                if provider not in providers:
                    instance.remove_settings("CALENDAR_" + provider.upper() + "_")
            if not providers:
                instance.values["CALENDAR_ENABLED"] = "false"
                instance.values.pop("CALENDAR_DEFAULT_PROVIDER", None)
            else:
                previous = instance.values.get("CALENDAR_DEFAULT_PROVIDER")
                instance.values["CALENDAR_DEFAULT_PROVIDER"] = providers[0] if len(providers) == 1 else answer(
                    questionary.select("Default calendar provider", choices=providers, default=previous if previous in providers else providers[0])
                )
                for provider in providers:
                    console.print("Configure " + provider.title() + " Calendar")
                    for field in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN"):
                        credential("CALENDAR_" + provider.upper() + "_" + field)
                    text("CALENDAR_" + provider.upper() + "_CALENDAR_ID", provider.title() + " calendar identifier", "primary" if provider == "google" else "default")
                    if provider == "outlook":
                        text("CALENDAR_OUTLOOK_TENANT_ID", "Microsoft tenant ID", "common")
                    if provider == "zoho":
                        text("CALENDAR_ZOHO_API_BASE", "Zoho regional API base (including /api/v1)", "https://calendar.zoho.com/api/v1")
                text("CALENDAR_DEFAULT_ALERT_MINUTES", "Default alert lead time (minutes)", "30")
                if confirm("Verify calendar credentials with fresh-token, read-only calendar and event requests now?", False):
                    from .calendar.diagnostics import verify_provider
                    from .calendar.models import ProviderConfig
                    for provider in providers:
                        prefix = "CALENDAR_" + provider.upper() + "_"
                        result = verify_provider(ProviderConfig(
                            name=provider, provider_type=provider,
                            calendar_id=instance.values[prefix + "CALENDAR_ID"],
                            client_id=instance.secret_values[prefix + "CLIENT_ID"],
                            client_secret=instance.secret_values[prefix + "CLIENT_SECRET"],
                            refresh_token=instance.secret_values[prefix + "REFRESH_TOKEN"],
                            extra={"api_base": instance.values.get(prefix + "API_BASE", "https://calendar.zoho.com/api/v1"),
                                   "tenant_id": instance.values.get(prefix + "TENANT_ID", "common")},
                        ))
                        console.print(result["provider"] + ": " + result["status"] + " - " + result["detail"], markup=False)
        elif any(name.startswith("CALENDAR_") for name in instance.secret_values):
            if confirm("Also remove all saved calendar settings and credentials?"):
                instance.remove_settings("CALENDAR_")
                instance.values["CALENDAR_ENABLED"] = "false"
    def zoom():
        if capability("ZOOM_ENABLED", "Enable managed Zoom meetings?"):
            mode = answer(questionary.select("Zoom authentication", choices=[
                questionary.Choice("Server-to-server OAuth (Account ID, Client ID, Client Secret)", value="s2s"),
                questionary.Choice("User OAuth (Client ID, Client Secret, Refresh Token)", value="oauth"),
            ], default=instance.values.get("ZOOM_AUTH_MODE") or "s2s"))
            instance.values["ZOOM_AUTH_MODE"] = mode
            console.print("Use your Zoom app credentials. Access tokens are obtained automatically; a webhook Secret Token is not used for meeting management.")
            instance.remove_settings("ZOOM_REFRESH_TOKEN" if mode == "s2s" else "ZOOM_ACCOUNT_ID")
            for name in ("ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET", "ZOOM_ACCOUNT_ID" if mode == "s2s" else "ZOOM_REFRESH_TOKEN"):
                credential(name)
            text("ZOOM_USER_ID", "Zoom user ID", "me")
        elif any(name.startswith("ZOOM_") for name in instance.secret_values):
            if confirm("Also remove all saved Zoom settings and credentials?"):
                instance.remove_settings("ZOOM_")
                instance.values["ZOOM_ENABLED"] = "false"

    def dropbox():
        options.stage = "configuring Dropbox"
        prompt_dropbox(instance, answer, confirm, console)

    def telegram():
        if not capability("TELEGRAM_ENABLED", "Enable private Telegram messaging?"):
            return
        credential("TELEGRAM_BOT_TOKEN")
        text("TELEGRAM_ALLOWED_USER_IDS", "Authorized numeric Telegram user IDs (comma-separated)", "")
        identifiers = [value.strip() for value in instance.values["TELEGRAM_ALLOWED_USER_IDS"].split(",") if value.strip()]
        if not identifiers or any(not value.isdigit() or int(value) < 1 for value in identifiers):
            raise SetupError("Telegram requires an explicit list of numeric user IDs")
        text("TELEGRAM_RECIPIENTS", 'Verified email-to-Telegram identity mapping (JSON object)', "{}")
        try:
            identities = json.loads(instance.values["TELEGRAM_RECIPIENTS"])
            if not isinstance(identities, dict) or any(str(value) not in identifiers for value in identities.values()):
                raise ValueError
        except ValueError:
            raise SetupError("Telegram identity mappings must use authorized numeric IDs") from None

    def transcription():
        if capability("TRANSCRIPTION_ENABLED", "Enable local English/Arabic voice transcription?"):
            instance.secret_values.setdefault("TRANSCRIPTION_API_TOKEN", secrets.token_urlsafe(32))
            instance.values["TRANSCRIPTION_BASE_URL"] = "http://transcription:8080"
            instance.values["TRANSCRIPTION_LANGUAGE"] = "auto"
            console.print("The managed multilingual small model is downloaded with SHA-256 verification. Voice processing is isolated from Ollama.")

    def security():
        prompt_security(instance, answer, confirm, console)

    def instructions():
        prompt = instance.data / "config/AGENT.md"
        console.print("Edit your assistant's instructions later at:")
        console.print(str(prompt), markup=False, highlight=False, soft_wrap=True)
        example = instance.source / "config/AGENT.md.example"
        if not example.is_file():
            console.print("Example not found. Create the instructions file manually before starting containers.")
            return
        copy = confirm("Copy src/config/AGENT.md.example to data/config/AGENT.md?", instance.prompt_original is None)
        if copy and instance.prompt_original is not None:
            copy = confirm("Replace your existing AGENT.md instructions with the example?", False)
        if copy:
            instance.copy_prompt_example()
            console.print("The example will be copied only when you save.")
        else:
            instance.prompt_copy = None
            if instance.prompt_original is None:
                console.print("No instructions file exists yet. Create it at the path above before starting.")
            else:
                console.print("Your existing instructions will be preserved.")

    sections = [("Identity and timezone", "Identify the assistant and its approval recipient.", identity),
                ("Models and spending", "Choose model access and limits. Existing database credentials are preserved.", models),
                ("Embeddings and search", "Configure semantic search independently of your paid language models.", embeddings),
                ("Email and administration", "Configure a dedicated mailbox, sender restrictions, and verified admin commands.", email),
                ("Calendar", "Connect calendar providers and choose a default calendar.", calendar),
                ("Zoom", "Configure managed Zoom meeting credentials independently.", zoom),
                ("Dropbox", "Connect Dropbox and configure file exchange permissions.", dropbox),
                ("Telegram", "Connect a private bot and explicitly link authorized identities.", telegram),
                ("Local voice transcription", "Configure isolated whisper.cpp transcription for English and Arabic.", transcription),
                ("Security screening", "Authenticate admin email and screen content with an optional decision model.", security),
                ("Advanced Settings", "Optional behavior and limits. Current defaults are retained unless you edit a group.",
                 lambda: prompt_advanced_settings(instance, answer, console)),
                ("Assistant instructions", "Choose whether to copy the example instructions. Existing files are never replaced without confirmation.", instructions)]
    section_map = dict(zip(SECTION_IDS, sections))
    baseline = copy.deepcopy((instance.values, instance.secret_values))
    initial_volume_choice = instance.use_credential_volume
    visited = set()

    def unsaved_changes():
        return ((instance.values, instance.secret_values) != baseline or bool(instance.tls_files)
                or instance.prompt_copy is not None or instance.use_credential_volume != initial_volume_choice)

    def choose_credential_storage():
        if instance.use_credential_volume:
            console.print("Restores plaintext credential files in .secrets with directory mode 0700 and file mode 0600 "
                          "on Save, briefly recreates running services to use them, then destroys the credential volume. "
                          "If removal cannot finish, setup keeps the volume and retries automatically.")
            if confirm("Store credentials in plaintext host files when saving?", False):
                instance.use_credential_volume = False
                console.print("Host storage selected. Select Save and finish to apply; nothing has been moved yet.")
                return True
            return False
        console.print("Moves credential files out of .secrets into an unencrypted Docker volume on Save. "
                      "Docker administrators can still read them. Services retain individual access. "
                      "Requires Docker volume subpath support; the first use may pull python:3.12-slim. "
                      "Back up the volume separately. TLS files and database OAuth tokens are not moved.")
        if confirm("Move credentials to a dedicated Docker volume when saving?", False):
            instance.use_credential_volume = True
            console.print("Migration is pending. Select Save and finish to move credentials; nothing has been moved yet.")
            return True
        return False

    def credential_storage_label():
        return ("Move credentials back to plaintext host files" if instance.use_credential_volume else
                "Move credentials to a dedicated Docker volume")

    def section_changed(key):
        if key == "instructions":
            return instance.prompt_copy is not None
        return section_settings(instance.values, instance.secret_values, key) != section_settings(*baseline, key)

    def section_label(key, problems):
        if any(section == key for section, _ in problems):
            status = "needs attention"
        elif section_changed(key):
            status = "changed"
        else:
            status = "reviewed" if key in visited else ""
        return section_map[key][0] + ("  [" + status + "]" if status else "")

    def show_summary(key):
        table = Table(title=section_map[key][0] + " summary")
        table.add_column("Setting")
        table.add_column("Value")
        rows = section_settings(instance.values, instance.secret_values, key)
        if key == "instructions":
            rows = [("data/config/AGENT.md", "Copy example on save" if instance.prompt_copy is not None else "Preserve existing / create manually")]
        for name, value in rows:
            table.add_row(name, value)
        if rows:
            console.print(table)
        else:
            console.print("No settings saved in this section.")

    def menu(build):
        # Ctrl-C at a menu exits; confirm first when it would discard edits.
        while True:
            try:
                return answer(build())
            except KeyboardInterrupt:
                if not unsaved_changes() or confirm("Discard all unsaved changes and exit?", False):
                    raise

    def run_section(key, header=None):
        nonlocal embeddings_edited
        title, description, action = section_map[key]
        options.stage = title
        console.print((header or title) + "\n" + description + "\nCtrl-C discards this section's changes and returns to navigation.", markup=False)
        snapshot = copy.deepcopy((instance.values, instance.secret_values, instance.tls_files, instance.prompt_copy, instance.state, embeddings_edited))
        try:
            action()
        except KeyboardInterrupt:
            instance.values, instance.secret_values, instance.tls_files, instance.prompt_copy, instance.state, embeddings_edited = snapshot
            console.print("Section changes discarded.", markup=False)
            return False
        visited.add(key)
        return True

    def choose_section(prompt):
        problems = draft_problems(instance)
        return menu(lambda: questionary.select(prompt, choices=[
            *[questionary.Choice(section_label(key, problems), value=key) for key in SECTION_IDS],
            questionary.Choice("Back", value="back"),
        ]))

    if selected_section:
        run_section(selected_section)
    elif configured and not getattr(options, "wizard", False):
        while True:
            options.stage = "choosing a setup section"
            problems = draft_problems(instance)
            selected = menu(lambda: questionary.select("Update configuration", choices=[
                *[questionary.Choice(section_label(key, problems), value=key) for key in SECTION_IDS],
                questionary.Choice("Review and save", value="review"),
                questionary.Choice("Run full wizard", value="wizard"),
                questionary.Choice(credential_storage_label(), value="credential-volume"),
                questionary.Separator(),
                questionary.Choice("Tools and troubleshooting", value="tools"),
                questionary.Choice("Factory reset (rebuild database, keep settings and files)", value="reset"),
                questionary.Choice("Cancel without saving", value="cancel"),
            ]))
            if selected == "cancel":
                console.print("Cancelled. No changes saved.")
                return
            if selected == "credential-volume":
                if choose_credential_storage():
                    selected = "review"
                    break
                continue
            if selected in {"tools", "reset"}:
                if unsaved_changes():
                    console.print("Unsaved setting changes are kept in this session; tools use the saved configuration.", markup=False)
                try:
                    run_maintenance(selected, instance, answer, confirm, console)
                except (SetupError, KeyboardInterrupt) as exc:
                    console.print(str(exc) if isinstance(exc, SetupError) else "Returned to setup.", markup=False)
                continue
            if selected in {"review", "wizard"}:
                break
            if run_section(selected):
                show_summary(selected)
        selected_section = selected != "wizard"
    index, edit = 0, False
    while not selected_section and index < len(sections):
        key = SECTION_IDS[index]
        header = f"\n{index + 1}/{len(sections)}  {section_map[key][0]}"
        if edit or key not in visited:
            run_section(key, header)
        else:
            console.print(header + "\nAlready reviewed this session. Choose Edit this section to change it.", markup=False)
        show_summary(key)
        options.stage = "navigation after " + section_map[key][0]
        direction = menu(lambda: questionary.select("Continue", choices=[
            "Next", "Edit this section", "Back", "Jump to section", "Review and save", "Cancel"]))
        edit = direction == "Edit this section"
        if direction == "Cancel":
            console.print("Cancelled. No changes saved.")
            return
        if direction == "Next":
            index += 1
        elif direction == "Back":
            index = max(index - 1, 0)
        elif direction == "Jump to section":
            target = choose_section("Jump to section")
            if target != "back":
                index = SECTION_IDS.index(target)
        elif direction == "Review and save":
            break
    while True:
        options.stage = "reviewing configuration"
        profiles = [name for name in existing_profiles if name and name not in {"email", "api", "telegram", "voice"}]
        profiles += [name for setting, name in (("SETUP_EMAIL_ENABLED", "email"),) if instance.values.get(setting) == "true"]
        profiles += [name for setting, name in (("TELEGRAM_ENABLED", "telegram"), ("TRANSCRIPTION_ENABLED", "voice")) if instance.values.get(setting) == "true"]
        instance.values["COMPOSE_PROFILES"] = ",".join(profiles)
        problems = draft_problems(instance)
        error = problems[0][1] if problems else None
        table = Table(title="Review before saving")
        table.add_column("Setting")
        table.add_column("Value")
        for name in ("AGENT_NAME", "AGENT_TIMEZONE", "AGENT_LLM_MODEL", "AGENT_EMBEDDINGS_ENABLED", "AGENT_EMBEDDINGS_MODEL", "AGENT_EMBEDDINGS_DIMENSIONS", "COMPOSE_PROFILES", "CALENDAR_ENABLED", "CALENDAR_DEFAULT_PROVIDER", "ZOOM_ENABLED", "ZOOM_AUTH_MODE", "DROPBOX_ENABLED", "DROPBOX_ALLOW_EXPORTS", "DROPBOX_MAX_FILE_BYTES", "TELEGRAM_ENABLED", "TELEGRAM_ALLOWED_USER_IDS", "TELEGRAM_RECIPIENTS", "TRANSCRIPTION_ENABLED", "TRANSCRIPTION_LANGUAGE"):
            table.add_row(name, instance.values.get(name, ""))
        for settings in ADVANCED_SETTINGS.values():
            for name, *_ in settings:
                if name in instance.values:
                    table.add_row(name, instance.values[name])
        for name in sorted(instance.secret_values):
            table.add_row(name, "configured (hidden)")
        table.add_row("Credential storage", "Private Docker volume (unencrypted)" if instance.use_credential_volume else "Protected .secrets files")
        removed_credentials = {name for name, data in instance.snapshots.items() if data is not None and name not in instance.secret_values}
        removed_credentials |= instance.migrated - instance.secret_values.keys()
        for name in sorted(removed_credentials):
            table.add_row(name, "REMOVE on save")
        table.add_row("data/config/AGENT.md", "Copy example on save" if instance.prompt_copy is not None else "Preserve existing / create manually")
        overrides_path = instance.data / OVERRIDES_FILE
        table.add_row(OVERRIDES_FILE, "Hand-edited; never rewritten" if overrides_path.is_file() else "Create commented template on save")
        console.print(table)
        if error:
            for section, message in problems:
                console.print("NOT SAVED. Fix this before saving: " + section_map[section][0] + ": " + message, style="bold red", markup=False)
        else:
            console.print("Ready to save. Nothing has been written yet.")
        selection = menu(lambda: questionary.select("Finish setup", choices=[
            questionary.Choice("Save and finish", value="save", disabled="Fix the validation error first" if error else None),
            questionary.Choice("Edit a section", value="edit"),
            questionary.Choice(credential_storage_label(), value="credential-volume"),
            questionary.Choice("Cancel without saving", value="cancel"),
        ]))
        if selection == "cancel":
            console.print("Cancelled. No changes saved.")
            return
        if selection == "save" and not error:
            break
        if selection == "credential-volume":
            choose_credential_storage()
        if selection == "edit":
            key = choose_section("Edit configuration")
            if key == "back":
                continue
            show_summary(key)
            if menu(lambda: questionary.select(section_map[key][0], choices=["Edit this section", "Keep and return to review"])) == "Edit this section":
                run_section(key)
    console.print("Existing backups may retain inline credentials. Saving alone does not start containers or migrate the database.")
    options.stage = "saving configuration"
    instance.state["setup_complete"] = True
    storage_changed = instance.use_credential_volume != bool(instance.credential_volume)
    if storage_changed:
        ensure_docker_access(instance, confirm)
    instance.save()
    if storage_changed and instance.credential_volume:
        console.print("Credentials saved in Docker volume " + instance.credential_volume.name + ". Plaintext .secrets sources removed.")
        options.stage = "activating credential volume mounts"
        try:
            targets = instance.apply(sorted(instance.secret_values))
            console.print("Recreated with volume mounts: " + (", ".join(targets) or "none running; mounts apply at startup"))
        except SetupError as error:
            console.print("Credentials are saved, but activation failed: " + str(error)
                          + "\nRun ./run.sh secrets apply to retry.", markup=False)
    elif storage_changed:
        console.print("Credentials saved in protected plaintext .secrets files.")
        options.stage = "removing the previous credential volume"
        finish_host_storage(instance, console)
    if embeddings_edited and instance.values.get("AGENT_EMBEDDINGS_ENABLED") == "true":
        options.stage = "embedding provisioning (configuration already saved)"
        console.print("Embedding settings saved. Source data is preserved; semantic recall may be incomplete until incompatible vectors are rebuilt.")
        activate_embeddings(instance, confirm, console)
    console.print("Saved. Edit your assistant's instructions later at:")
    console.print(str(instance.data / "config/AGENT.md"), markup=False, highlight=False, soft_wrap=True)
    if not profiles:
        console.print("No external task intake is enabled. Configure email when ready to submit tasks.")
    console.print("Starting containers builds images, applies pending database migrations, and enables workers. Existing inbox messages may be processed.")
    if not confirm("Start the Docker containers now and check their status?", False):
        console.print("Start later from the repository directory: docker compose --env-file data/.env up -d --build --wait --wait-timeout 180")
        return
    rows = start_saved_containers(instance, confirm, console)
    status = Table(title="Container status")
    for heading in ("Service", "State", "Docker health", "Restart policy"):
        status.add_column(heading)
    for row in rows:
        persistent = row.get("Service") in LONG_RUNNING_SERVICES
        status.add_row(row.get("Service", "unknown"), row.get("State", "unknown"), row.get("Health") or "No healthcheck defined",
                       "unless-stopped (verified)" if persistent else "One-shot / not changed")
    console.print(status)
    console.print("Long-running services restart after failures and Docker/host restarts unless deliberately stopped. One-shot helpers are not restarted.")
    configure_docker_boot(instance, confirm, console)


def finish_host_storage(instance, console):
    name = instance.state["previous_credential_volume"]
    try:
        targets = instance.retire_previous_volume()
    except SetupError as error:
        console.print("Credential volume " + name + " was kept: " + str(error)
                      + "\nSetup retries removal the next time it opens; credentials in .secrets remain authoritative.", markup=False)
        return False
    console.print("Recreated with host mounts: " + (", ".join(targets) or "none running; mounts apply at startup"))
    console.print("Previous credential volume destroyed: " + name, markup=False)
    return True


def run_maintenance(command, instance, answer, confirm, console):
    if command == "tools":
        from .setup_tools import tools_menu
        tools_menu(instance, answer, confirm, console)
    else:
        from .setup_tools import factory_reset_menu
        factory_reset_menu(instance, answer, confirm, console)


def ensure_docker_access(instance, confirm):
    instance.use_sudo = False
    try:
        instance.docker(["info", "--format", "{{.ServerVersion}}"], timeout=15)
    except DockerCommandError as error:
        if error.reason not in {None, "permission"}:
            raise
        if platform.system() != "Linux" or os.geteuid() == 0:
            raise
        instance.use_sudo = getattr(instance, "sudo_consent", False) or confirm(
            "Direct Docker access failed. Use sudo? Your system may ask for authorization; Assistant never reads your password.", False)
        if not instance.use_sudo:
            raise
        instance.sudo_consent = True
        instance.docker(["info", "--format", "{{.ServerVersion}}"], timeout=15)


def start_saved_containers(instance, confirm, console):
    instance.use_sudo = False
    instance.startup_stage = "checking Docker access"
    try:
        ensure_docker_access(instance, confirm)
        with console.status("Building and starting containers; this may take several minutes..."):
            return instance.start_containers()
    except (SetupError, OSError, ValueError, KeyboardInterrupt) as error:
        console.print("Configuration is saved, but startup verification is incomplete.", style="bold red")
        console.print("Failed stage: " + instance.startup_stage, markup=False)
        reason = str(error) if isinstance(error, SetupError) else "Startup was interrupted or its result could not be verified; command output was suppressed"
        console.print(reason, markup=False)
        command = "sudo docker" if instance.use_sudo else "docker"
        console.print("From the repository directory, run: " + command + " compose up -d --build --wait --wait-timeout 180")
        console.print("Then inspect: " + command + " compose ps -a && " + command + " compose logs --tail=100")
        if instance.use_sudo:
            console.print("If sudo needs a password, enter it directly in your terminal; the wizard never collects it.")
        raise SetupError("Container startup verification is incomplete; resolve the reported failure and retry setup") from None


def advanced_value_error(setting, value):
    name, path, label, default, kind, minimum = setting
    if name == "AGENT_APPROVAL_CHANNEL" and value not in {"email", "telegram", "both"}:
        return "Approval delivery must be email, telegram or both"
    if name == "AGENT_NAMING_MODEL" and not str(value).strip():
        return "Naming model must not be blank"
    if name in {"AGENT_TTS_MODEL", "AGENT_TTS_VOICE"} and not str(value).strip():
        return "Speech model and voice must not be blank"
    if kind is bool:
        if str(value).lower() not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            return "Advanced Settings: " + name + " must be a boolean"
    elif kind is int:
        try:
            if int(value) < minimum:
                raise ValueError
            if name == "AGENT_MODEL_SERVICE_BUDGET_PERIOD_DAYS" and int(value) > 365:
                return "Advanced Settings: " + name + " must be an integer from 1 to 365"
        except (ValueError, TypeError, OverflowError):
            return "Advanced Settings: " + name + " must be an integer of at least " + str(minimum)
    elif kind is float:
        try:
            if not math.isfinite(float(value)) or float(value) < minimum:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            return "Advanced Settings: " + name + " must be a finite number of at least " + str(minimum)
    return None


def prompt_advanced_settings(instance, answer, console):
    import questionary

    console.print("Advanced overrides are saved to .env only after final Save. Blank domain lists clear restrictions. Calendar details remain private unless explicitly enabled.")
    while True:
        group = answer(questionary.select("Advanced Settings", choices=[
            *ADVANCED_SETTINGS, questionary.Choice("Done (keep remaining settings)", value="done"),
        ], default="done"))
        if group == "done":
            return
        for setting in ADVANCED_SETTINGS[group]:
            name, path, label, default, kind, minimum = setting
            previous = instance.values.get(name, instance.defaults.get(path, default))
            if previous is None:
                previous = default
            if kind is bool:
                baseline = previous if isinstance(previous, bool) else str(previous).lower() in {"true", "1", "yes", "on"}
                instance.values[name] = str(answer(questionary.confirm(label, default=baseline))).lower()
            else:
                baseline = ",".join(str(item) for item in previous) if isinstance(previous, list) else str(previous)
                instance.values[name] = answer(questionary.text(label, default=baseline,
                    validate=lambda value, current=setting: advanced_value_error(current, value) or True)).strip()


def enter_selects_highlighted(question):
    """Make Enter tick the highlighted checkbox item when nothing is ticked yet."""
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.keys import Keys
    from questionary.prompts.common import InquirerControl

    application = question.application
    control = next((item for item in application.layout.find_all_controls() if isinstance(item, InquirerControl)), None)
    if control is None:
        return question

    # Added last, so prompt_toolkit prefers it over questionary's own Enter binding while active.
    @application.key_bindings.add(Keys.ControlM, eager=True, filter=Condition(lambda: not control.selected_options))
    def _(event):
        control.selected_options.append(control.get_pointed_at().value)
        control.is_answered = True
        event.app.exit(result=[choice.value for choice in control.get_selected_values()])

    return question


def prompt_calendar_providers(answer, choices, console):
    import questionary

    message = "Press Space to tick providers, then Enter. Enter alone selects the highlighted provider."
    while True:
        providers = answer(enter_selects_highlighted(questionary.checkbox(
            "Calendar providers (Space ticks; Enter confirms)", choices=choices,
            validate=lambda selected: True if selected else message,
        )))
        if providers:
            return providers
        console.print(message, markup=False)


def configure_docker_boot(instance, confirm, console):
    if platform.system() != "Linux" or not Path("/run/systemd/system").is_dir() or not shutil.which("systemctl"):
        console.print("Docker must also start on host boot. On Docker Desktop, enable automatic startup at login; otherwise enable Docker in your host's service manager.")
        return False
    try:
        host = os.getenv("DOCKER_HOST") or json.loads(instance.docker(["context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"])).strip()
        if not host.startswith("unix://") or "/run/user/" in host:
            console.print("Remote/rootless Docker detected. Configure daemon startup on its host; rootless installs also require user-service startup and login lingering.")
            return False
        enabled = subprocess.run(["systemctl", "is-enabled", "docker.service"], capture_output=True, text=True, timeout=20)
        if enabled.returncode == 0 and enabled.stdout.strip() == "enabled":
            console.print("Docker daemon startup on boot is already enabled.")
            return True
        if not confirm("Enable Docker to start automatically when this Linux server boots?", True):
            console.print("Container policies are configured, but automatic Docker startup on boot is not verified.")
            return False
        command = ["systemctl", "enable", "--now", "docker.service"]
        if os.geteuid() != 0:
            prepare_sudo(command, runner=subprocess.run)
            command = ["sudo", "-n", *command]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise SetupError("Docker boot setup requires administrator authorization")
        verified = subprocess.run(["systemctl", "is-enabled", "docker.service"], capture_output=True, text=True, timeout=20)
        if verified.returncode != 0 or verified.stdout.strip() != "enabled":
            raise SetupError("Docker boot setup could not be verified")
        console.print("Docker daemon startup on boot is enabled and verified.")
        return True
    except (SetupError, OSError, ValueError, subprocess.TimeoutExpired):
        console.print("Docker auto-start could not be configured. Run this yourself: sudo systemctl enable --now docker")
        console.print("Then verify with: systemctl is-enabled docker")
        return False


def security_value(instance, name, default=None):
    """Current wizard value for a security setting, falling back to config/agent.yaml."""
    if default is None:
        default = {
            "AGENT_SECURITY_SCRIPT_YOLO": False,
            "AGENT_SECURITY_DEVELOPMENT_MODEL": "",
            "AGENT_SECURITY_DEVELOPMENT_MAX_MINUTES": 30,
            "AGENT_SECURITY_DEVELOPMENT_MAX_RUNS": 20,
        }.get(name)
    if name in instance.values:
        value = instance.values[name]
    else:
        value = instance.defaults.get(SECURITY_SETTINGS[name], default)
    if next((caster for key, _, caster in ENV_OVERRIDES if key == name), None) is bool:
        normalized = str(value).strip().lower()
        if normalized in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            return str(normalized in {"true", "1", "yes", "on"}).lower()
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return "" if value is None else str(value)


def security_config(instance):
    """AppConfig reflecting the draft security settings, used for the setup connection test."""
    values = copy.deepcopy(instance.defaults.values)
    for name, path in SECURITY_SETTINGS.items():
        current = values
        *parents, leaf = path.split(".")
        for part in parents:
            current = current.setdefault(part, {})
        current[leaf] = security_value(instance, name)
    return AppConfig(values)


def prompt_security(instance, answer, confirm, console):
    import questionary
    from .security_screening import (
        DEFAULT_PROVIDER, OPENROUTER_SECRET_NAME, PROVIDERS, SECRET_NAME, DecisionError, test_connection,
    )

    def toggle(name, label):
        enabled = confirm(label, security_value(instance, name, False) == "true")
        instance.values[name] = str(enabled).lower()
        return enabled

    def ask(name, label, default=""):
        instance.values[name] = answer(questionary.text(label, default=security_value(instance, name) or default)).strip()

    prompt_development_security(instance, answer, confirm, console)

    console.print("Admin email authentication verifies, via DNS, the DKIM signature or SPF authorization of email "
                  "that claims to come from your admin address. Either must pass for your admin domain. Failures are "
                  "quarantined and you are notified. Other senders are unaffected.")
    toggle("AGENT_SECURITY_EMAIL_AUTH_ENABLED", "Require DKIM or SPF on admin email?")

    console.print("A decision model rates content as not malicious, possibly malicious or malicious. "
                  "When configured, every accepted inbound email is screened; this cannot be turned off. "
                  "Possibly malicious items wait for your OTP approval; malicious items are quarantined and reported.")
    enabled = security_value(instance, "AGENT_SECURITY_DECISION_ENABLED") == "true"
    current = security_value(instance, "AGENT_SECURITY_DECISION_PROVIDER", DEFAULT_PROVIDER) or DEFAULT_PROVIDER
    route = answer(questionary.select(
        "Decision model endpoint", default=("openrouter" if current == "openrouter" else "direct") if enabled else "none",
        choices=[questionary.Choice("None (no screening)", value="none"),
                 questionary.Choice("OpenRouter Decisions API (recommended; any decision model, uses your OpenRouter key)", value="openrouter"),
                 questionary.Choice("A provider's own decision API", value="direct")]))
    if route == "none":
        instance.values["AGENT_SECURITY_DECISION_ENABLED"] = "false"
        for name in ("AGENT_SECURITY_SCREEN_OUTBOUND_EMAIL", "AGENT_SECURITY_SCREEN_SCRIPTS", "AGENT_SECURITY_ALLOW_POSSIBLY_MALICIOUS"):
            instance.values[name] = "false"
        instance.secret_values.pop(SECRET_NAME, None)
        return
    instance.values["AGENT_SECURITY_DECISION_ENABLED"] = "true"
    if route == "openrouter":
        provider = "openrouter"
    else:
        provider = answer(questionary.select(
            "Decision model provider", default=current if current in PROVIDERS and current != "openrouter" else "perplexity",
            choices=[questionary.Choice("Perplexity decision API", value="perplexity"),
                     questionary.Choice("Jev System One", value="jev"),
                     questionary.Choice("Clef (DeepInfra)", value="clef"),
                     questionary.Choice("OpenAI decisions API", value="openai"),
                     questionary.Choice("Custom System One-compatible endpoint", value="custom")]))
    if provider != current:
        instance.values["AGENT_SECURITY_DECISION_URL"] = ""
        instance.values["AGENT_SECURITY_DECISION_MODEL"] = ""
    instance.values["AGENT_SECURITY_DECISION_PROVIDER"] = provider
    spec = PROVIDERS[provider]
    if provider == "openrouter":
        console.print("OpenRouter normalizes all decision models to one format. Enter any decision model slug, "
                      "for example perplexity/pplx-decider-v1.2, typesafe/jev-1.13, cloudflare/clef or "
                      "openai/gpt-6-luna-decisions. A pinned version keeps screening thresholds stable.")
        instance.values["AGENT_SECURITY_DECISION_URL"] = ""
        ask("AGENT_SECURITY_DECISION_MODEL", "Decision model slug", spec.model)
        instance.secret_values.pop(SECRET_NAME, None)
        key_name = OPENROUTER_SECRET_NAME
        if not instance.secret_values.get(key_name):
            console.print("No OpenRouter API key is configured yet; enter one to use OpenRouter for screening.")
            instance.secret_values[key_name] = prompt_credential(answer, key_name, None)
    else:
        ask("AGENT_SECURITY_DECISION_URL", "Decision API URL" + ("" if spec.url else " (HTTPS)"), spec.url)
        ask("AGENT_SECURITY_DECISION_MODEL", "Decision model identifier", spec.model)
        if instance.values["AGENT_SECURITY_DECISION_URL"] == spec.url:
            instance.values["AGENT_SECURITY_DECISION_URL"] = ""
        key_name = SECRET_NAME
        instance.secret_values[key_name] = prompt_credential(answer, key_name, instance.secret_values.get(key_name))
    if instance.values["AGENT_SECURITY_DECISION_MODEL"] == spec.model:
        instance.values["AGENT_SECURITY_DECISION_MODEL"] = ""
    instance.values["AGENT_SECURITY_DECISION_ON_ERROR"] = answer(questionary.select(
        "If the decision model is unavailable", default=security_value(instance, "AGENT_SECURITY_DECISION_ON_ERROR", "review") or "review",
        choices=[questionary.Choice("Hold for my approval (recommended)", value="review"),
                 questionary.Choice("Reject", value="reject"),
                 questionary.Choice("Allow without screening", value="allow")]))
    console.print("Inbound email screening: always on.")
    toggle("AGENT_SECURITY_SCREEN_OUTBOUND_EMAIL", "Also screen outgoing email to external recipients?")
    if toggle("AGENT_SECURITY_SCREEN_SCRIPTS", "Also screen scripts before the assistant runs them? (imports, calls and network hosts are analysed)"):
        ask("AGENT_SECURITY_SCRIPT_ALLOWED_DOMAINS", "Domains scripts may contact without extra scrutiny (comma-separated; blank = none)")
    if confirm("Send a test request to the decision model now?", True):
        try:
            result = test_connection(security_config(instance), instance.secret_values[key_name])
            console.print("Decision model responded: " + result.summary(), markup=False)
        except (DecisionError, OSError, ValueError) as exc:
            console.print("Test failed: %s" % exc, markup=False)
    yolo = security_value(instance, "AGENT_SECURITY_ALLOW_POSSIBLY_MALICIOUS", False) == "true"
    if confirm("Enable YOLO mode for possibly malicious email? (script permissions still required)", yolo):
        if not yolo:
            console.print("[bold red]WARNING:[/bold red] YOLO mode lets email and outgoing messages the decision model "
                          "flags as possibly malicious run without your approval. Prompt injection, phishing and data "
                          "exfiltration attempts in that category will reach the assistant. You will only be notified afterwards. "
                          "Malicious items are still blocked.")
            typed = answer(questionary.text("Type %s to enable YOLO mode" % YOLO_CONFIRMATION))
            yolo = typed.strip() == YOLO_CONFIRMATION
            if not yolo:
                console.print("Confirmation did not match; YOLO mode stays off.")
    else:
        yolo = False
    instance.values["AGENT_SECURITY_ALLOW_POSSIBLY_MALICIOUS"] = str(yolo).lower()


def prompt_development_security(instance, answer, confirm, console):
    import questionary
    from .development_supervisor import SCRIPT_YOLO_WARNING

    console.print("Development sessions always use an independent, tool-free supervisor unless script YOLO is "
                  "explicitly enabled. The supervisor reviews the entire source version against your approved brief. "
                  "Leave the model blank to use your agent model, provider and existing credential. Enter an "
                  "OpenRouter provider/model ID to select another model using OPENROUTER_API_KEY. Presets are not used.")
    for name, label, default in (
        ("AGENT_SECURITY_DEVELOPMENT_MODEL", "Development supervisor model ID (blank = agent model)", ""),
        ("AGENT_SECURITY_DEVELOPMENT_MAX_MINUTES", "Maximum development session duration (minutes)", "30"),
        ("AGENT_SECURITY_DEVELOPMENT_MAX_RUNS", "Maximum development session runs", "20"),
    ):
        instance.values[name] = answer(questionary.text(
            label, default=security_value(instance, name) or default,
        )).strip()
    enabled = False
    if confirm("Enable script YOLO? Scripts + public network without approvals or supervisor",
               security_value(instance, "AGENT_SECURITY_SCRIPT_YOLO", False) == "true"):
        console.print("[bold red]WARNING:[/bold red] " + SCRIPT_YOLO_WARNING)
        typed = answer(questionary.text("Type ALLOW UNSUPERVISED SCRIPTS to enable script YOLO"))
        enabled = typed.strip() == "ALLOW UNSUPERVISED SCRIPTS"
        if not enabled:
            console.print("Confirmation did not match; script YOLO stays off.")
    instance.values["AGENT_SECURITY_SCRIPT_YOLO"] = str(enabled).lower()


def prompt_credential(answer, name, existing):
    import questionary
    choices = (["Keep existing"] if existing else []) + ["Enter replacement"]
    if name in GENERATED_SECRETS:
        choices.append("Generate securely")
    while True:
        choice = answer(questionary.select(name, choices=choices))
        if choice == "Keep existing":
            return existing
        value = secrets.token_urlsafe(32) if choice == "Generate securely" else answer(questionary.password("New value for " + name))
        try:
            validate_secret(name, value)
            return value
        except SetupError as exc:
            print(str(exc))


def prepare_internal_tls():
    if not shutil.which("openssl"):
        raise SetupError("OpenSSL is required to provision internal service TLS certificates")
    files = {}
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        for service in ("postgres", "sandbox"):
            certificate = directory / (service + ".crt")
            key = directory / (service + ".key")
            result = subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "825",
                 "-keyout", str(key), "-out", str(certificate), "-subj", "/CN=" + service,
                 "-addext", "subjectAltName=DNS:" + service,
                 "-addext", "basicConstraints=critical,CA:FALSE",
                 "-addext", "extendedKeyUsage=serverAuth"],
                capture_output=True,
                timeout=60,
            )
            if result.returncode:
                raise SetupError("Internal TLS certificate generation failed")
            try:
                import ssl
                ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(certificate, key, password="")
            except (ssl.SSLError, OSError):
                raise SetupError("Internal TLS certificate/key generation failed") from None
            files[service + ".crt"] = certificate.read_bytes()
            files[service + ".key"] = key.read_bytes()
    return files


def prompt_dropbox(instance, answer, confirm, console):
    import questionary
    from .dropbox_provider import DropboxClient, DropboxError, authorization, exchange_code

    def default(name, fallback):
        return instance.values.get(name, instance.defaults.get(PROMPT_DEFAULTS[name], fallback))

    enabled = confirm("Enable private Dropbox file exchange?", str(default("DROPBOX_ENABLED", False)).lower() == "true")
    instance.values["DROPBOX_ENABLED"] = str(enabled).lower()
    if not enabled:
        instance.values["DROPBOX_ALLOW_EXPORTS"] = "false"
        if any(name.startswith("DROPBOX_") for name in instance.secret_values) and confirm("Also remove all saved Dropbox settings and credentials?", False):
            instance.remove_settings("DROPBOX_")
            instance.values["DROPBOX_ENABLED"] = "false"
            instance.values["DROPBOX_ALLOW_EXPORTS"] = "false"
        return
    console.print("Dropbox uses a dedicated App Folder only. Create a scoped App Folder app in the Dropbox developer console, not a Full Dropbox app. Files in Inbox are imported on demand; outputs use Outbox/job-<id>. No synchronization, public links or deletions.")
    console.print("Dropbox is not end-to-end encrypted. Imported content may reach configured model providers. Credentials are stored in permission-protected local files after Save.")
    connected = bool(instance.secret_values.get("DROPBOX_CLIENT_ID") and instance.secret_values.get("DROPBOX_REFRESH_TOKEN"))
    reconnect = not connected or answer(questionary.select("Dropbox connection", choices=["Keep existing", "Reconnect"], default="Keep existing")) == "Reconnect"
    if reconnect:
        if not confirm("Confirm this Dropbox app is registered with App Folder access, not Full Dropbox", False):
            raise SetupError("Dropbox requires an App Folder app; no authorization saved")
        client_id = answer(questionary.text("Dropbox app key (client ID)", default=instance.secret_values.get("DROPBOX_CLIENT_ID", ""))).strip()
        validate_secret("DROPBOX_CLIENT_ID", client_id)
        url, verifier = authorization(client_id)
        console.print("Open this URL in your browser, authorize, then enter the displayed code here. Do not send the code by email or chat.")
        console.print(url, markup=False, highlight=False, soft_wrap=True)
        code = answer(questionary.password("Dropbox authorization code")).strip()
        try:
            refresh_token = exchange_code(client_id, code, verifier)
        except DropboxError as exc:
            raise SetupError(str(exc)) from None
        validate_secret("DROPBOX_REFRESH_TOKEN", refresh_token)
        instance.secret_values.update({"DROPBOX_CLIENT_ID": client_id, "DROPBOX_REFRESH_TOKEN": refresh_token})
    instance.values["DROPBOX_ALLOW_EXPORTS"] = str(confirm(
        "Allow any task to deliver workspace files to Dropbox Outbox without additional confirmation?",
        str(default("DROPBOX_ALLOW_EXPORTS", False)).lower() == "true")).lower()
    instance.values["DROPBOX_MAX_FILE_BYTES"] = answer(questionary.text(
        "Maximum Dropbox file size in bytes (default 25 MiB)", default=str(default("DROPBOX_MAX_FILE_BYTES", 25 * 1024 * 1024)))).strip()
    if confirm("Verify Dropbox authorization now? (read-only; no files or folders created)", False):
        config = AppConfig({"agent": {"dropbox": {"enabled": True, "client_id": instance.secret_values["DROPBOX_CLIENT_ID"],
                            "refresh_token": instance.secret_values["DROPBOX_REFRESH_TOKEN"]}}})
        try:
            DropboxClient(config).token(force=True)
        except DropboxError as exc:
            raise SetupError(str(exc)) from None
        console.print("Dropbox authorization verified. Create Inbox in the app folder before importing. Outbox is created on first authorized export.")


def validate_draft(instance):
    problems = draft_problems(instance)
    if problems:
        raise SetupError(problems[0][1])


def draft_problems(instance):
    """Return every validation problem as (section_id, message), in the order validate_draft reports them."""
    values = {}
    for name, path, _ in ENV_OVERRIDES:
        configured = instance.defaults.get(path)
        if configured is not None and not is_secret_name(name):
            values[name] = (str(configured).lower() if isinstance(configured, bool) else
                            ",".join(map(str, configured)) if isinstance(configured, list) else
                            json.dumps(configured) if isinstance(configured, dict) else str(configured))
    values.update(instance.values)
    problems = []

    def problem(section, message):
        problems.append((section, message))

    for name, _, caster in ENV_OVERRIDES:
        if caster is bool and name in values:
            value = str(values[name]).strip().lower()
            if value in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                values[name] = str(value in {"true", "1", "yes", "on"}).lower()
            elif name not in ADVANCED_ENV_NAMES:
                section = next((section for section, (names, prefixes) in SECTION_KEYS.items()
                                if name in names or name.startswith(prefixes)), "advanced")
                problem(section, name + " must be a boolean")
    values.setdefault("SETUP_EMAIL_ENABLED",
                      str("email" in [profile.strip() for profile in values.get("COMPOSE_PROFILES", "").split(",")]).lower())
    if instance.secret_values.get("DATABASE_URL"):
        try:
            validate_database_url(instance.secret_values["DATABASE_URL"])
        except RuntimeError as exc:
            problem("models", "DATABASE_URL: " + str(exc))
    for settings in ADVANCED_SETTINGS.values():
        for setting in settings:
            if setting[0] in values:
                error = advanced_value_error(setting, values[setting[0]])
                if error:
                    problem("advanced", error)
    model_service_enabled = values.get("AGENT_MODEL_SERVICE_ENABLED", instance.defaults.get("agent.model_service.enabled", False))
    if str(model_service_enabled).lower() in {"true", "1", "yes", "on"} and not instance.secret_values.get("OPENROUTER_API_KEY"):
        problem("advanced", "Model preset tool requires OPENROUTER_API_KEY; configure it with setup secrets update OPENROUTER_API_KEY")
    tts_enabled = values.get("AGENT_TTS_ENABLED", instance.defaults.get("agent.tts.enabled", False))
    if str(tts_enabled).lower() in {"true", "1", "yes", "on"} and not instance.secret_values.get("OPENROUTER_API_KEY"):
        problem("advanced", "Text-to-speech requires OPENROUTER_API_KEY; configure it with setup secrets update OPENROUTER_API_KEY")
    approval_channel = values.get("AGENT_APPROVAL_CHANNEL", instance.defaults.get("agent.approvals.channel", "email"))
    if approval_channel in {"telegram", "both"}:
        enabled = values.get("TELEGRAM_ENABLED", instance.defaults.get("agent.telegram.enabled", False))
        allowed = AppConfig({"ids": values.get("TELEGRAM_ALLOWED_USER_IDS",
                            instance.defaults.get("agent.telegram.allowed_user_ids", []))}).get_list("ids")
        if str(enabled).lower() not in {"true", "1", "yes", "on"} or not instance.secret_values.get("TELEGRAM_BOT_TOKEN"):
            problem("advanced", "Telegram approvals require an enabled Telegram integration and bot token")
        from .approval_channels import telegram_admins
        try:
            admins = telegram_admins(AppConfig({"agent": {
                "admin": {"email": values.get("ADMIN_EMAIL", instance.defaults.get("agent.admin.email", ""))},
                "telegram": {
                    "allowed_user_ids": allowed,
                    "recipients": values.get("TELEGRAM_RECIPIENTS", instance.defaults.get("agent.telegram.recipients", {})),
                },
            }}))
            if not admins:
                raise ValueError("Missing admin mapping")
        except ValueError:
            problem("advanced", "Telegram approvals require the admin email's verified Telegram recipient mapping "
                    "to an allowlisted numeric user ID; configure ADMIN_EMAIL and Telegram identity mappings")
    def embedding_value(field, default):
        return values.get("AGENT_EMBEDDINGS_" + field.upper(), instance.defaults.get("agent.embeddings." + field, default))

    enabled = str(embedding_value("enabled", True)).lower()
    if enabled not in {"true", "false"}:
        problem("embeddings", "Embeddings and search: enabled must be true or false")
    if enabled == "true":
        model = str(embedding_value("model", "embeddinggemma")).strip()
        try:
            endpoint_url = str(embedding_value("base_url", "http://ollama:11434"))
            validate_embedding_url(endpoint_url)
            dimensions = embedding_value("dimensions", None)
            timeout = int(embedding_value("timeout_seconds", 20))
            if not model:
                raise ValueError
            if timeout <= 0 or (dimensions not in (None, "") and int(dimensions) <= 0):
                raise ValueError
            if model.split(":")[0] == "embeddinggemma" and dimensions not in (None, "") and int(dimensions) not in {128, 256, 512, 768}:
                raise ValueError
        except (RuntimeError, ValueError, TypeError):
            problem("embeddings", "Embeddings and search: use HTTPS except for local Ollama at http://ollama:11434; check the model, positive timeout, and supported dimensions (EmbeddingGemma: 128/256/512/768)")
    try:
        ZoneInfo(values.get("AGENT_TIMEZONE", instance.defaults.get("agent.app.timezone") or "UTC"))
    except (ValueError, ZoneInfoNotFoundError):
        problem("identity", "Check the timezone (IANA identifier, e.g. Europe/London)")
    for name, section in (("IMAP_PORT", "email"), ("SMTP_PORT", "email")):
        try:
            if values.get(name) and not 1 <= int(values[name]) <= 65535:
                raise ValueError
        except ValueError:
            problem(section, "Check the port number for " + name)
    base_url = values.get("AGENT_LLM_BASE_URL", instance.defaults.get("agent.llm.base_url") or "")
    model = values.get("AGENT_LLM_MODEL", instance.defaults.get("agent.llm.model"))
    try:
        if not model or not str(model).strip():
            raise RuntimeError
        validate_https_url(base_url, "Model API URL")
        if any(char.isspace() for char in base_url):
            raise RuntimeError
    except RuntimeError:
        problem("models", "A model and valid HTTPS model API URL without credentials are required")
    required = {"POSTGRES_PASSWORD": "models", ("OPENROUTER_API_KEY" if "openrouter.ai" in base_url else "OPENAI_API_KEY"): "models"}
    if values.get("SETUP_EMAIL_ENABLED") == "true":
        required.update(dict.fromkeys(("IMAP_PASSWORD", "SMTP_PASSWORD"), "email"))
        for name in ("ADMIN_EMAIL", "IMAP_HOST", "IMAP_USERNAME", "SMTP_HOST", "SMTP_USERNAME", "SMTP_FROM", "EMAIL_ACTIONABLE_SENDERS", "EMAIL_ALLOWED_RECIPIENT_DOMAINS"):
            if not values.get(name):
                problem("identity" if name == "ADMIN_EMAIL" else "email", "Email operation requires " + name)
        for name in ("IMAP_HOST", "SMTP_HOST"):
            host = values.get(name, "")
            if host and (any(char.isspace() for char in host) or any(char in host for char in "/@")):
                problem("email", name + " must be a server hostname, not a URL")
    for name, section in (("ADMIN_EMAIL", "identity"), ("ORG_SECURITY_EMAIL", "identity"),
                          ("SMTP_FROM", "email"), ("AGENT_EMAIL", "email")):
        address = values.get(name, "")
        if address and not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", parseaddr(address)[1]):
            problem(section, name + " must be a valid email address")
    if values.get("CALENDAR_ENABLED") == "true":
        provider = values.get("CALENDAR_DEFAULT_PROVIDER", instance.defaults.get("agent.calendar.default_provider") or "google").upper()
        if not re.fullmatch(r"(GOOGLE|OUTLOOK|ZOHO)(_[2-9])?", provider):
            problem("calendar", "Default calendar provider must be google, outlook or zoho")
        else:
            required.update(dict.fromkeys(("CALENDAR_" + provider + "_" + field for field in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN")), "calendar"))
        for name, value in instance.secret_values.items():
            match = re.fullmatch(r"(CALENDAR_(?:GOOGLE|OUTLOOK|ZOHO)(?:_[2-9])?)_(?:CLIENT_ID|CLIENT_SECRET|REFRESH_TOKEN)", name)
            if match and value:
                required.update(dict.fromkeys((match[1] + "_" + field for field in
                                               ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN")), "calendar"))
    zoho_api_bases = [value for name, value in values.items()
                      if name == "CALENDAR_ZOHO_API_BASE"
                      or (name.startswith("CALENDAR_ZOHO_") and name.endswith("_API_BASE"))]
    if zoho_api_bases:
        from .calendar.providers.zoho import normalize_api_base
        for value in zoho_api_bases:
            try:
                normalize_api_base(value)
            except ValueError:
                problem("calendar", "Zoho Calendar API base must use HTTPS on a supported Zoho Calendar host")
                break
    if values.get("ZOOM_ENABLED") == "true":
        mode = values.get("ZOOM_AUTH_MODE", instance.defaults.get("agent.zoom.auth_mode") or "s2s")
        if mode not in {"s2s", "oauth"}:
            problem("zoom", "Zoom authorization mode must be s2s or oauth")
        required.update(dict.fromkeys(("ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET", "ZOOM_ACCOUNT_ID" if mode == "s2s" else "ZOOM_REFRESH_TOKEN"), "zoom"))
    if values.get("TRANSCRIPTION_ENABLED") == "true" and values.get("TRANSCRIPTION_LANGUAGE", "auto") not in {"auto", "en", "ar"}:
        problem("transcription", "Voice transcription language must be auto, en or ar")
    telegram_enabled = values.get("TELEGRAM_ENABLED", instance.defaults.get("agent.telegram.enabled", False))
    if AppConfig({"enabled": telegram_enabled}).get_bool("enabled"):
        from .telegram_config import telegram_identities
        required["TELEGRAM_BOT_TOKEN"] = "telegram"
        identifiers = AppConfig({"ids": values.get("TELEGRAM_ALLOWED_USER_IDS",
                                instance.defaults.get("agent.telegram.allowed_user_ids", []))}).get_list("ids")
        if not identifiers or any(not re.fullmatch(r"[1-9][0-9]{0,18}", identifier)
                                  or int(identifier) >= 2**63 for identifier in identifiers):
            problem("telegram", "Telegram requires an explicit list of positive numeric user IDs")
        try:
            identities = telegram_identities(AppConfig({"agent": {"telegram": {
                "recipients": values.get("TELEGRAM_RECIPIENTS", instance.defaults.get("agent.telegram.recipients", {})),
            }}}))
            if any(str(identifier) not in identifiers for identifier in identities.values()):
                raise ValueError("Telegram recipient mappings must use authorized numeric user IDs")
        except ValueError as error:
            problem("telegram", str(error))
    for name in ("DROPBOX_ENABLED", "DROPBOX_ALLOW_EXPORTS"):
        if values.get(name, "false") not in {"true", "false"}:
            problem("dropbox", name + " must be true or false")
    if values.get("DROPBOX_ENABLED") == "true":
        required.update(dict.fromkeys(("DROPBOX_CLIENT_ID", "DROPBOX_REFRESH_TOKEN"), "dropbox"))
        try:
            if not 1 <= int(values.get("DROPBOX_MAX_FILE_BYTES", 25 * 1024 * 1024)) <= 150 * 1024 * 1024:
                raise ValueError
        except ValueError:
            problem("dropbox", "DROPBOX_MAX_FILE_BYTES must be between 1 byte and 150 MiB")
    from .development_supervisor import configuration_errors
    for error in configuration_errors(security_config(instance)):
        problem("security", error)
    if security_value(instance, "AGENT_SECURITY_DEVELOPMENT_MODEL", "").strip():
        required.setdefault("OPENROUTER_API_KEY", "security")
    from .security_screening import PROVIDER_CHOICES
    if security_value(instance, "AGENT_SECURITY_EMAIL_AUTH_ENABLED", False) not in {"true", "false"}:
        problem("security", "AGENT_SECURITY_EMAIL_AUTH_ENABLED must be true or false")
    if values.get("AGENT_SECURITY_DECISION_ENABLED", "false") == "true":
        provider = security_value(instance, "AGENT_SECURITY_DECISION_PROVIDER", "openrouter") or "openrouter"
        url = security_value(instance, "AGENT_SECURITY_DECISION_URL")
        if provider not in PROVIDER_CHOICES:
            problem("security", "Decision model provider must be one of " + ", ".join(PROVIDER_CHOICES))
        try:
            if provider == "custom" and not url:
                raise RuntimeError
            if url:
                validate_https_url(url, "Decision model URL")
                if any(char.isspace() for char in url):
                    raise RuntimeError
        except RuntimeError:
            problem("security", "Decision model URL must be an HTTPS URL without credentials")
        if provider == "custom" and not security_value(instance, "AGENT_SECURITY_DECISION_MODEL"):
            problem("security", "A custom decision model requires a model identifier")
        if security_value(instance, "AGENT_SECURITY_DECISION_ON_ERROR", "review") not in {"review", "reject", "allow"}:
            problem("security", "Decision model error handling must be review, reject or allow")
        required.setdefault("OPENROUTER_API_KEY" if provider == "openrouter" else "SECURITY_DECISION_API_KEY", "security")
    elif security_value(instance, "AGENT_SECURITY_ALLOW_POSSIBLY_MALICIOUS", False) == "true":
        problem("security", "YOLO mode requires a configured decision model")
    for name in sorted(required):
        try:
            validate_secret(name, instance.secret_values.get(name, ""))
        except SetupError as exc:
            problem(required[name], name + ": " + str(exc))
    try:
        overrides = (read_regular(instance.data / OVERRIDES_FILE) or b"").decode("utf-8")
    except UnicodeDecodeError:
        overrides = None
        problem("advanced", OVERRIDES_FILE + ": file must be UTF-8 text")
    for message in overrides_problems(instance.root, overrides) if overrides else ():
        problem("advanced", message)
    return problems


if __name__ == "__main__":
    # Share exception classes with maintenance modules that import .setup.
    from .setup import main as setup_main

    setup_main()