"""Host-side operations for the native management interface."""
import copy
import json
import secrets
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig
from .secret_store import SECRET_NAMES
from .setup import (
    ADVANCED_SETTINGS, AGENT_SERVICES, ENV_OVERRIDES, LONG_RUNNING_SERVICES, DockerCommandError, Installation, SetupError,
    draft_problems, ensure_docker_access, read_regular, validate_secret,
)
DEFAULT_PATHS = {key: path for key, path, _ in ENV_OVERRIDES}
SERVICE_NAMES = {
    "task-agent": "Task worker", "postgres": "Database", "downloader": "Email intake",
    "workspace-indexer": "Workspace indexer", "reminder-scheduler": "Reminders",
    "deep-research-agent": "Research worker", "supervisor": "Health monitor",
    "heartbeat": "Heartbeat", "telegram-receiver": "Telegram",
    "request-preprocessor": "Request screening", "clamav": "Malware scanning",
    "ollama": "Local embeddings", "transcription": "Voice transcription",
    "sandbox": "Script runner",
}


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    default: str = ""
    secret: bool = False
    boolean: bool = False


def field(key, label, default=""):
    return Field(key, label, default, key in SECRET_NAMES, default in {"true", "false"})


GROUPS = {
    "identity": ("About your assistant", [
        field("AGENT_NAME", "Assistant name", "Assistant"), field("AGENT_TIMEZONE", "Timezone", "UTC"),
        field("ADMIN_NAME", "Your name"), field("ADMIN_EMAIL", "Approval email"),
        field("ORG_NAME", "Organization"), field("ORG_SECURITY_EMAIL", "Security contact"),
        field("ORG_INTERNAL_EMAIL_DOMAINS", "Internal email domains (comma-separated)"),
    ]),
    "models": ("Models & access", [
        field("AGENT_LLM_BASE_URL", "Provider API URL", "https://openrouter.ai/api/v1"),
        field("AGENT_LLM_MODEL", "Primary model"), field("AGENT_LLM_FALLBACK_MODEL", "Fallback model"),
        field("OPENROUTER_API_KEY", "OpenRouter API key"), field("OPENAI_API_KEY", "OpenAI-compatible API key"),
        field("POSTGRES_PASSWORD", "Database password"),
    ]),
    "email": ("Email", [
        field("SETUP_EMAIL_ENABLED", "Enable email", "false"),
        *[field(key, label, default) for key, label, default in (
            ("IMAP_HOST", "IMAP server", ""), ("IMAP_PORT", "IMAP TLS port", "993"),
            ("IMAP_USERNAME", "Mailbox login", ""), ("IMAP_PASSWORD", "Mailbox password", ""),
            ("IMAP_FOLDER", "Incoming folder", "INBOX"), ("IMAP_ARCHIVE_FOLDER", "Processed folder", "Archive"),
            ("IMAP_QUARANTINE_FOLDER", "Quarantine folder", "Quarantine"), ("IMAP_SENT_FOLDER", "Sent folder", "Sent"),
            ("SMTP_HOST", "SMTP server", ""), ("SMTP_PORT", "SMTP TLS port", "587"),
            ("SMTP_USERNAME", "SMTP login", ""), ("SMTP_PASSWORD", "SMTP password", ""),
            ("SMTP_FROM", "Sender email", ""), ("AGENT_EMAIL", "Assistant email", ""),
            ("EMAIL_ACTIONABLE_SENDERS", "Allowed senders (comma-separated)", ""),
            ("EMAIL_ALLOWED_RECIPIENT_DOMAINS", "Allowed recipient domains (comma-separated)", ""),
        )],
    ]),
    "telegram": ("Telegram", [
        field("TELEGRAM_ENABLED", "Enable Telegram", "false"), field("TELEGRAM_BOT_TOKEN", "Bot token"),
        field("TELEGRAM_ALLOWED_USER_IDS", "Allowed numeric user IDs (comma-separated)"),
        field("TELEGRAM_RECIPIENTS", 'Verified recipients (JSON: {"you@example.com": "123456"})', "{}"),
    ]),
    "calendar": ("Calendar", [
        field("CALENDAR_ENABLED", "Enable calendar", "false"),
        field("CALENDAR_DEFAULT_PROVIDER", "Default provider (google, outlook, zoho)", "google"),
        *[field(f"CALENDAR_{provider}_{key}", f"{provider.title()} {label}", default)
          for provider in ("GOOGLE", "OUTLOOK", "ZOHO")
          for key, label, default in (
              ("CLIENT_ID", "client ID", ""), ("CLIENT_SECRET", "client secret", ""),
              ("REFRESH_TOKEN", "refresh token", ""), ("CALENDAR_ID", "calendar ID", "primary" if provider == "GOOGLE" else "default"),
          )],
        field("CALENDAR_OUTLOOK_TENANT_ID", "Outlook tenant", "common"),
        field("CALENDAR_ZOHO_API_BASE", "Zoho regional API URL", "https://calendar.zoho.com/api/v1"),
    ]),
    "zoom": ("Zoom", [
        field("ZOOM_ENABLED", "Enable Zoom", "false"),
        field("ZOOM_AUTH_MODE", "Authorization mode (s2s or oauth)", "s2s"),
        *[field("ZOOM_" + key, label) for key, label in (
            ("CLIENT_ID", "Client ID"), ("CLIENT_SECRET", "Client secret"),
            ("ACCOUNT_ID", "Account ID"), ("REFRESH_TOKEN", "Refresh token"),
        )],
    ]),
    "dropbox": ("Dropbox", [
        field("DROPBOX_ENABLED", "Enable private App Folder exchange", "false"),
        field("DROPBOX_ALLOW_EXPORTS", "Allow exports", "false"),
        field("DROPBOX_MAX_FILE_BYTES", "Maximum file size (bytes)", "26214400"),
        field("DROPBOX_CLIENT_ID", "App Folder client ID"), field("DROPBOX_REFRESH_TOKEN", "Refresh token"),
    ]),
    "transcription": ("Voice messages", [
        field("TRANSCRIPTION_ENABLED", "Enable local voice transcription", "false"),
        field("TRANSCRIPTION_LANGUAGE", "Language (auto, en or ar)", "auto"),
    ]),
    "embeddings": ("Search & embeddings", [
        field("AGENT_EMBEDDINGS_ENABLED", "Enable semantic search", "true"),
        field("AGENT_EMBEDDINGS_MODEL", "Embedding model", "embeddinggemma"),
        field("AGENT_EMBEDDINGS_BASE_URL", "Embedding API URL", "http://ollama:11434"),
        field("AGENT_EMBEDDINGS_DIMENSIONS", "Dimensions (blank uses model default)"),
        field("AGENT_EMBEDDINGS_TIMEOUT_SECONDS", "Timeout (seconds)", "20"),
    ]),
    "security": ("Safety & screening", [
        field("AGENT_SECURITY_EMAIL_AUTH_ENABLED", "Authenticate admin email", "false"),
        field("AGENT_SECURITY_DECISION_ENABLED", "Enable content screening", "false"),
        field("AGENT_SECURITY_DECISION_PROVIDER", "Screening provider", "openrouter"),
        field("AGENT_SECURITY_DECISION_URL", "Screening API URL (blank uses provider default)"),
        field("AGENT_SECURITY_DECISION_MODEL", "Screening model"),
        field("AGENT_SECURITY_DECISION_ON_ERROR", "On screening error (review, reject or allow)", "review"),
        field("AGENT_SECURITY_SCREEN_OUTBOUND_EMAIL", "Screen outgoing email", "true"),
        field("AGENT_SECURITY_SCREEN_SCRIPTS", "Screen scripts", "true"),
        field("AGENT_SECURITY_SCRIPT_ALLOWED_DOMAINS", "Allowed script source domains"),
        field("SECURITY_DECISION_API_KEY", "Screening API key"),
    ]),
}
for title, settings in ADVANCED_SETTINGS.items():
    GROUPS["advanced:" + title] = (title, [
        Field(key, label, ",".join(default) if isinstance(default, list) else str(default).lower()
              if isinstance(default, bool) else str(default), False, kind is bool)
        for key, _, label, default, kind, _ in settings
    ])


class Management:
    def __init__(self, root, confirm):
        self.root = Path(root)
        self.confirm = confirm
        self.installation = None
        self.values = {}
        self.credentials = {}
        self.instructions = None
        self.accepted = False
        self.helper_ready = False
        self.dirty = False
        self.progress = ""

    def load(self):
        self.progress = "Reading protected local configuration"
        previous = self.installation
        self.installation = Installation(self.root)
        if previous is not None:
            self.installation.sudo_consent = getattr(previous, "sudo_consent", False)
        self.values = copy.deepcopy(self.installation.values)
        self.credentials = dict(self.installation.secret_values)
        self.instructions = None
        self.dirty = False
        self.accepted = bool(self.installation.state.get("setup_complete"))
        return self.installation

    def value(self, spec):
        if spec.secret:
            return ""
        if spec.key in self.values:
            if spec.boolean:
                return str(AppConfig({"value": self.values[spec.key]}).get_bool("value")).lower()
            return self.values[spec.key]
        if spec.key == "SETUP_EMAIL_ENABLED":
            return str("email" in self.values.get("COMPOSE_PROFILES", "").split(",")).lower()
        path = DEFAULT_PATHS.get(spec.key)
        configured = self.installation.defaults.get(path) if path else None
        if configured is None:
            return spec.default
        if spec.boolean:
            return str(AppConfig({"value": configured}).get_bool("value")).lower()
        if isinstance(configured, list):
            return ",".join(map(str, configured))
        if isinstance(configured, dict):
            return json.dumps(configured)
        return str(configured).lower() if isinstance(configured, bool) else str(configured)

    def stage(self, group, values):
        changes = []
        for spec in GROUPS[group][1]:
            value = values[spec.key] if spec.secret else values[spec.key].strip()
            if spec.secret:
                if value:
                    validate_secret(spec.key, value)
                    if spec.key == "POSTGRES_PASSWORD" and self.installation.secret_values.get(spec.key):
                        raise SetupError("Use Credentials > Rotate database password for an existing database.")
            if not spec.secret or value:
                if self.installation.state.get("setup_complete") and not spec.secret and value == self.value(spec):
                    continue
                changes.append((spec.secret, spec.key, value))
        for secret, key, value in changes:
            target = self.credentials if secret else self.values
            if target.get(key) != value:
                target[key] = value
                self.dirty = True

    def draft(self):
        instance = copy.deepcopy(self.installation)
        instance.values = dict(self.values)
        instance.secret_values = dict(self.credentials)
        profiles = [name.strip() for name in instance.values.get("COMPOSE_PROFILES", "").split(",")
                    if name.strip()]
        for key, profile in (
            ("SETUP_EMAIL_ENABLED", "email"), ("TELEGRAM_ENABLED", "telegram"),
            ("TRANSCRIPTION_ENABLED", "voice")):
            path = DEFAULT_PATHS.get(key)
            configured = instance.defaults.get(path) if path else None
            if key in instance.values or configured is not None:
                profiles = [name for name in profiles if name != profile]
                if AppConfig({"enabled": instance.values.get(key, configured)}).get_bool("enabled"):
                    profiles.append(profile)
        instance.values["COMPOSE_PROFILES"] = ",".join(profiles)
        instance.prompt_copy = self.instructions
        return instance

    def problems(self):
        instance = self.draft()
        errors = draft_problems(instance)
        instructions = instance.prompt_copy if instance.prompt_copy is not None else instance.prompt_original
        if not instructions or not instructions.strip():
            errors.append(("instructions", "Review and save nonempty assistant instructions before completing setup."))
        return errors

    def save(self):
        if not self.accepted:
            raise SetupError("Acknowledge the first-run safety notice before saving.")
        instance = self.draft()
        errors = self.problems()
        if errors:
            raise SetupError("\n".join(message for _, message in errors))
        instance.state["setup_complete"] = True
        instance.save()
        self.load()

    def access(self):
        self.progress = "Checking Docker access"
        ensure_docker_access(self.installation, self.confirm)

    def services(self):
        self.access()
        self.progress = "Reading component health"
        names = self.installation.compose(["config", "--services"]).split()
        output = self.installation.compose(["ps", "--all", "--format", "json"])
        try:
            parsed = json.loads(output) if output.strip() else []
            rows = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            try:
                rows = [json.loads(line) for line in output.splitlines() if line.strip()]
            except json.JSONDecodeError:
                raise SetupError("Docker returned invalid component status. Check Docker and refresh.") from None
        if any(not isinstance(row, dict) or not isinstance(row.get("Service"), str)
               or not isinstance(row.get("State", "unknown"), str)
               or row.get("Health") is not None and not isinstance(row["Health"], str) for row in rows):
            raise SetupError("Docker returned invalid component status. Check Docker and refresh.")
        by_name = {}
        for row in rows:
            by_name.setdefault(row["Service"], []).append(row)
        return [
            {"id": name, "title": SERVICE_NAMES.get(name, name), "worker": name in LONG_RUNNING_SERVICES,
             "state": ", ".join(sorted({row.get("State", "unknown")
               for row in by_name[name]})) if name in by_name else "not created",
             "health": ", ".join(sorted({row.get("Health") or "no healthcheck" for row in by_name[name]}))
             if name in by_name else "not checked"}
            for name in sorted(set(names) | set(by_name))
        ]

    def lifecycle(self, action, service=None):
        if action in {"start", "apply"} and not self.installation.state.get("setup_complete"):
            raise SetupError("Complete and save setup before starting the application.")
        self.access()
        self.installation.assert_current()
        if service and service not in self.installation.compose(["config", "--services"]).split():
            raise SetupError("Service is no longer configured. Refresh the list.")
        if action == "start" and not service:
            if self.dirty:
                raise SetupError("Save or discard your draft before starting the application.")
            self.progress = "Building and starting the application"
            self.installation.start_containers()
        else:
            commands = {
                "start": ["up", "-d", "--wait", "--wait-timeout", "180"],
                "restart": ["restart"], "stop": ["stop"],
                "apply": ["up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "180"],
            }
            if action not in commands:
                raise SetupError("Unsupported service action")
            self.progress = action.title() + " " + (service or "application")
            self.installation.compose(commands[action] + ([service] if service else []), timeout=1800)
        return self.services()

    def logs(self, service):
        self.access()
        if service not in self.installation.compose(["config", "--services"]).split():
            raise SetupError("Select a configured service.")
        return self.installation.compose(
            ["logs", "--no-color", "--timestamps", "--tail", "150", service], timeout=30)

    def request(self, payload):
        if not self.installation.state.get("setup_complete"):
            raise SetupError("Complete and save setup before managing application records.")
        self.access()
        self.prepare_management()
        self.progress = "Reading application records" if payload.get("kind") != "mutate" else "Applying the confirmed change"
        args = ["run", "--rm", "--no-deps", "--pull", "never", "-T", "--volume", self.management_source(),
                "--entrypoint", "python",
                "task-agent", "-m", "assistant_agent.management_operator"]
        try:
            output = self.installation.compose(args, input_text=json.dumps(payload))
        except DockerCommandError as error:
            try:
                failure = json.loads(error.stdout or "")
            except json.JSONDecodeError:
                raise error from None
            if isinstance(failure, dict) and isinstance(failure.get("error"), str):
                raise SetupError(failure["error"]) from None
            raise
        try:
            result = json.loads(output)
            if not isinstance(result, dict) or "error" in result:
                raise ValueError
            if payload.get("kind") in {
                "tasks", "memories", "reminders", "projects", "scripts", "task-events", "project-objects",
            } and (not isinstance(result.get("rows"), list)
                   or any(not isinstance(row, dict) for row in result["rows"])
                   or type(result.get("total")) is not int or result["total"] < 0):
                raise ValueError
            kind = payload.get("kind")
            if kind in {"tasks", "memories", "reminders", "projects", "scripts", "project-objects"}:
                if any(type(row.get("id")) is not int or row["id"] <= 0 for row in result["rows"]):
                    raise ValueError
            if kind == "memories" and any(not isinstance(row.get("content"), str) for row in result["rows"]):
                raise ValueError
            if kind == "script" and (type(result.get("id")) is not int
                                     or not isinstance(result.get("version"), dict)
                                     or result["version"].get("id") != payload.get("id")):
                raise ValueError
            if kind == "mutate" and result.get("result") in (None, False):
                raise ValueError
            return result
        except (json.JSONDecodeError, ValueError):
            raise SetupError("Management response was invalid. Rebuild the application image and refresh; no success was confirmed.") from None

    def mutate(self, action, identifier=None, body=""):
        return self.request({"kind": "mutate", "action": action, "id": identifier,
                             "body": body, "confirmed": True})

    def management_source(self):
        return str(self.root.resolve() / "src/agent/src") + ":/app/src:ro"

    def prepare_management(self):
        if self.helper_ready:
            return
        self.access()
        self.progress = "Checking management runtime"
        output = self.installation.compose(["config", "--format", "json"])
        try:
            config = json.loads(output)
            service = config["services"]["task-agent"]
            image = service.get("image")
            if not image and service.get("build"):
                image = config["name"] + "-task-agent"
            if not isinstance(image, str) or not image.strip():
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise SetupError("Could not identify the task-agent image from saved Compose configuration. "
                             "Check that task-agent has an image or build configuration and Compose reports "
                             "the project name.") from None
        try:
            self.installation.docker(["image", "inspect", image], timeout=30)
        except DockerCommandError as error:
            raise SetupError("The configured task-agent image could not be inspected. "
                             "If it is missing, use Services > Start app to build it. "
                             "Otherwise check Docker access. " + str(error)) from None
        try:
            self.installation.compose([
                "run", "--rm", "--no-deps", "--pull", "never", "-T",
                "--volume", self.management_source(), "--entrypoint", "python",
                "task-agent", "-c", "import assistant_agent.management_operator",
            ], timeout=60)
        except DockerCommandError as error:
            raise SetupError("The management helper failed its startup check. "
                             "This does not mean the application is stopped; restarting may not resolve it. "
                             "Check Docker Compose compatibility and the helper's source mount and dependencies, "
                             "then refresh. " + str(error)) from None
        self.helper_ready = True

    def project_request(self, action, payload):
        if not self.installation.state.get("setup_complete"):
            raise SetupError("Complete and save setup before managing projects.")
        self.access()
        self.prepare_management()
        mount = str(self.root.resolve() / "data/private/artifacts") + ":/data/private/artifacts"
        try:
            output = self.installation.compose([
                "run", "--rm", "--no-deps", "--pull", "never", "-T", "--volume", mount,
                "--volume", self.management_source(), "--entrypoint", "python",
                "task-agent", "-m", "assistant_agent.workstream_operator", action,
            ], input_text=json.dumps(payload), timeout=1800)
        except DockerCommandError as error:
            try:
                failure = json.loads(error.stdout or "")
            except json.JSONDecodeError:
                raise error from None
            if isinstance(failure, dict) and isinstance(failure.get("workstream_error"), str):
                raise SetupError(failure["workstream_error"]) from None
            raise
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            raise SetupError("Invalid project response; no change was confirmed.") from None
        if not isinstance(result, dict) or "workstream_error" in result:
            raise SetupError("Invalid project response; no change was confirmed.")
        if action == "preview" and (not isinstance(result.get("preview_digest"), str)
                                    or not result["preview_digest"]):
            raise SetupError("Invalid project inventory; no deletion can be confirmed.")
        return result

    def preview_project(self, identifier):
        return self.project_request("preview", {"workstream_id": identifier, "mode": "preserve_shared"})

    def delete_project(self, identifier, digest):
        self.access()
        running = sorted(set(self.installation.compose(["ps", "--status", "running", "--services"]).split())
                         & set(AGENT_SERVICES))
        if running:
            self.progress = "Pausing writers for a consistent project inventory"
            self.installation.compose(["stop", "--timeout", "120", *running], timeout=180)
        attempted = completed = False
        try:
            preview = self.preview_project(identifier)
            if any(preview.get(key) for key in ("running_jobs", "running_research", "blocked_files", "blocked_reminders")):
                raise SetupError("Active work or unverified files block deletion. Resolve them and preview again. Nothing was deleted.")
            if preview.get("preview_digest") != digest:
                raise SetupError("The project inventory changed. Preview and confirm the new scope. Nothing was deleted.")
            attempted = True
            result = self.project_request("delete", {
                "workstream_id": identifier, "mode": "preserve_shared", "preview_digest": digest,
                "confirmed": True, "writers_stopped": True,
            })
            completed = result.get("status") == "completed"
            if not completed:
                raise SetupError("Project deletion is incomplete. Writer services remain stopped; inspect pending deletions.")
        finally:
            try:
                pending = self.pending_deletions()
            except (SetupError, OSError, ValueError) as error:
                raise SetupError("Could not verify deletion state. Writer services remain stopped. "
                                 "Check pending deletions from Storage & recovery before restarting.") from error
            if pending or attempted and not completed:
                raise SetupError("An incomplete deletion remains. Writer services stay stopped. Resume it from Storage & recovery.")
            if running:
                self.installation.compose(["start", *running])

    def pending_deletions(self):
        result = self.project_request("pending", {})
        if not isinstance(result.get("deletions"), list) or any(
                not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0
                or not isinstance(row.get("status"), str) for row in result.get("deletions", [])):
            raise SetupError("Could not verify pending deletions. Writer services must remain stopped.")
        return result["deletions"]

    def resume_deletion(self, identifier):
        self.access()
        pending = self.pending_deletions()
        if identifier not in {row["id"] for row in pending}:
            raise SetupError("Select an existing pending deletion.")
        running = sorted(set(self.installation.compose(["ps", "--status", "running", "--services"]).split())
                         & set(AGENT_SERVICES))
        if running:
            self.installation.compose(["stop", "--timeout", "120", *running], timeout=180)
        result = self.project_request("resume", {"deletion_id": identifier, "confirmed": True, "writers_stopped": True})
        if result.get("status") != "completed" or self.pending_deletions():
            raise SetupError("Deletion remains incomplete. Writer services are intentionally stopped.")
        if running:
            self.installation.compose(["start", *running])

    def replace_credential(self, name, value):
        validate_secret(name, value)
        if self.dirty:
            raise SetupError("Save or discard your draft before replacing a live credential.")
        self.access()
        if name == "POSTGRES_PASSWORD":
            self.installation.rotate_database(value)
        else:
            instance = copy.deepcopy(self.installation)
            instance.secret_values[name] = value
            instance.save()
        self.load()

    def apply_credentials(self, name=None):
        if self.dirty:
            raise SetupError("Save or discard your draft before activating credentials.")
        pending = self.installation.state.get("pending", [])
        if name is not None and name not in pending:
            raise SetupError("This credential has no pending activation.")
        self.access()
        self.installation.apply([name] if name is not None else pending)
        self.load()

    def reset(self):
        from .setup_tools import run_factory_reset

        if self.dirty:
            raise SetupError("Save or discard your draft before resetting the database.")
        self.access()

        class Report:
            def __init__(self):
                self.messages = []

            def print(self, value, **kwargs):
                self.messages.append(value)

            def run_command(self, instance, arguments):
                instance.compose(arguments, timeout=1800)
                return 0

        report = Report()
        run_factory_reset(self.installation, report)
        return report.messages[-1]

    def recover(self):
        from .setup import recover_database
        if self.dirty:
            raise SetupError("Save or discard your draft before recovering database credentials.")
        recover_database(self.root, prepare=lambda instance: ensure_docker_access(instance, self.confirm))
        return self.load()

    def embeddings(self, operation):
        if operation not in {"status", "probe", "reindex"}:
            raise SetupError("Unsupported search operation")
        self.access()
        self.prepare_management()
        output = self.installation.compose([
            "run", "--rm", "--no-deps", "--pull", "never", "-T",
            "--volume", self.management_source(), "--entrypoint", "python",
            "task-agent", "-m", "assistant_agent.embedding_maintenance", operation,
        ], timeout=1800)
        try:
            result = json.loads(output)
            if not isinstance(result, dict) or "error" in result:
                raise ValueError
            return result
        except (json.JSONDecodeError, ValueError):
            raise SetupError("Search maintenance returned an invalid response. No success was confirmed.") from None

    def storage(self, volume):
        if self.dirty:
            raise SetupError("Save or discard your draft before changing credential storage.")
        self.access()
        instance = copy.deepcopy(self.installation)
        instance.use_credential_volume = volume
        instance.save()
        self.load()
        try:
            targets = self.installation.apply(sorted(self.installation.secret_values))
            if not volume and self.installation.state.get("previous_credential_volume"):
                self.installation.retire_previous_volume()
        except SetupError as error:
            raise SetupError("Credential storage was saved, but activation or retirement is incomplete. "
                             "Use Activate pending in Credentials or Finish credential storage change "
                             "in Storage & recovery, then check service health. " + str(error)) from None
        self.load()
        return targets

    def instruction_text(self):
        if self.instructions is not None:
            return self.instructions.decode()
        return (read_regular(self.root / "data/config/AGENT.md") or
                read_regular(self.root / "src/config/AGENT.md.example") or b"").decode()

    def generate_database_password(self):
        if self.installation.secret_values.get("POSTGRES_PASSWORD"):
            raise SetupError("An existing database password must be rotated, not overwritten.")
        try:
            initialized = (self.root / "data/postgres/pgdata/PG_VERSION").exists()
        except PermissionError:
            raise SetupError("Cannot inspect existing PostgreSQL data. Supply its current password; no replacement was generated.") from None
        if initialized:
            raise SetupError("Existing PostgreSQL data detected. Supply its current password; no replacement was generated.")
        return secrets.token_urlsafe(36)

    def redact(self, value):
        secrets = set(self.credentials.values())
        if self.installation is not None:
            secrets.update(self.installation.secret_values.values())
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[hidden]")
        return value
