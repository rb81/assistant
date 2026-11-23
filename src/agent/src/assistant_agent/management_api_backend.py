"""In-memory TUI drafts; every platform read and write uses the management API."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from .management_client import ManagementClient
from .management_contracts import SettingField, SettingChoicesResponse
from .setup import SetupError, validate_secret


CONFIGURED = "[configured]"


class ApiManagementBackend:
    def __init__(self, root, client=None):
        self.root = Path(root)
        self._client = client
        self.installation = None
        self.progress = ""
        self.values = {}
        self.credentials = {}
        self.instructions = None
        self.accepted = False
        self.dirty = False
        self._preview = None
        self.groups = {}
        self.field_metadata = {}
        self.group_metadata = {}

    @property
    def client(self):
        if self._client is None:
            self._client = ManagementClient.from_installation(self.root)
        return self._client

    def load(self):
        self.configuration = self.client.configuration()
        self.group_metadata = {item["id"]: item for item in self.configuration["groups"]}
        groups = {}
        for item in self.configuration["settings"]:
            key = item["group"]
            title = self.group_metadata.get(key, {}).get("title", key)
            groups.setdefault(key, (title, []))[1].append(SettingField.model_validate(item))
        self.groups = groups
        self.field_metadata = {item["key"]: item["metadata"] for item in self.configuration["settings"]}
        self.restrictions = {field["key"]: field["edit_restriction"] for field in self.configuration["settings"]
                             if not field["editable"]}
        credentials = self.client.get("credentials")["credentials"]
        storage = self.client.get("credentials/storage")
        self.values = {field["key"]: field["value"] for field in self.configuration["settings"] if not field["secret"]}
        self.credentials = {name: CONFIGURED for name in self.configuration["credential_names"]}
        self.installation = SimpleNamespace(
            values=dict(self.values), secret_values=dict(self.credentials),
            state={"setup_complete": self.configuration["setup_complete"],
                   "pending": [item["name"] for item in credentials if item["status"] == "activation pending"],
                   "previous_credential_volume": storage["retirement_pending"]},
            credential_volume=storage["mode"] == "volume",
            prompt_original=self.configuration["instructions"].encode() or
                (b"[configured]" if self.configuration.get("instructions_configured") else b""),
        )
        self.accepted = self.configuration["setup_complete"]
        self.instructions = None
        self.dirty = False
        self._preview = None
        return self.installation

    def value(self, spec):
        return "" if spec.secret else self.values.get(spec.key, spec.default)

    def stage(self, group, values):
        self.stage_fields(self.groups[group][1], values)

    def stage_fields(self, specs, values):
        changes = []
        for field in specs:
            if field.key not in values:
                continue
            value = values[field.key]
            if field.key in self.restrictions:
                if value != self.value(field):
                    raise SetupError(self.restrictions[field.key])
                continue
            if field.secret:
                if not value:
                    continue
                validate_secret(field.key, value)
                changes.append((self.credentials, field.key, value))
            else:
                changes.append((self.values, field.key, value.strip()))
        for target, key, value in changes:
            target[key] = value
        self.dirty = (self.values != self.installation.values or self.credentials != self.installation.secret_values
                      or self.instructions is not None or self.accepted != self.configuration["setup_complete"])

    def instruction_text(self):
        if not self.configuration["instructions"] and self.configuration.get("instructions_configured"):
            result = self.client.get("configuration/instructions")
            if result["revision"] != self.configuration["revision"]:
                raise SetupError("Configuration changed. Reload before editing instructions.")
            self.configuration["instructions"] = result["instructions"]
            self.installation.prompt_original = result["instructions"].encode()
        return self.instructions.decode() if self.instructions is not None else self.configuration["instructions"]

    def model_settings(self):
        result = self.client.model_settings()
        if result["revision"] != self.configuration["revision"]:
            raise SetupError("Configuration changed. Save or discard your draft, then reload settings.")
        return result

    def setting_choices(self, key, form_values=None):
        resource = "configuration/choices?key=" + quote(key, safe="")
        metadata = self.field_metadata.get(key, {})
        values = {**self.values, **(form_values or {})}
        provider = values.get(metadata.get("provider_key"), "")
        if provider in {"", "auto", "inherit"}:
            provider = values.get("AGENT_LLM_PROVIDER", "")
        if metadata.get("choices_source") == "models" and provider not in {"", "auto", "inherit"}:
            resource += "&provider=" + quote(provider, safe="")
        return self.client.read(resource, SettingChoicesResponse)

    def draft(self):
        draft = copy.deepcopy(self.installation)
        draft.values = dict(self.values)
        return draft

    def payload(self):
        values = {key: value for key, value in self.values.items() if self.installation.values.get(key) != value}
        values.update({key: value for key, value in self.credentials.items() if value != CONFIGURED})
        return {"revision": self.configuration["revision"], "values": values,
                "instructions": self.instructions.decode() if self.instructions is not None else None,
                "acknowledge": self.accepted}

    def preview(self):
        payload = self.payload()
        signature = json.dumps(payload, sort_keys=True)
        if self._preview is None or self._preview[0] != signature:
            self._preview = (signature, self.client.request("POST", "configuration/preview", payload))
        return self._preview[1]

    def problems(self):
        return [("advanced", message) for message in self.preview()["problems"]]

    def save(self, *, start=False):
        preview = self.preview()
        if not preview["valid"]:
            raise SetupError("\n".join(preview["problems"]))
        self.client.operation({"operation": "configuration.save", **self.payload(),
                               "preview_id": preview["preview_id"], "confirmed": True, "apply": start})
        return self.load()

    def save_and_start(self):
        return self.save(start=True)

    def services(self):
        return self.client.services()

    def logs(self, service):
        return self.client.logs(service)

    def prepare_management(self):
        self.client.health()

    def request(self, payload):
        kind = payload["kind"]
        if kind == "summary":
            return self.client.get("tasks/summary")
        if kind in {"tasks", "memories", "reminders", "projects", "scripts", "research"}:
            resource = f"records/{kind}"
            if payload.get("id"):
                resource += f"/{payload['id']}"
            return self.client.get(resource + f"?page={payload.get('page', 0)}&query=" +
                                   quote(payload.get("query", ""), safe=""))
        collections = {"task-events": "tasks", "research-events": "research", "project-objects": "projects"}
        if kind in collections:
            return self.client.get(f"records/{collections[kind]}/{payload['id']}/children?page={payload.get('page', 0)}")
        if kind == "script":
            return self.client.get(f"records/scripts/{payload['id']}")
        if kind == "mutate":
            return self.mutate(payload["action"], payload.get("id"), payload.get("body", ""))
        raise SetupError("Unsupported terminal management read.")

    def mutate(self, action, identifier=None, body=""):
        return self.client.operation({"operation": "record.mutate", "action": action,
                                      "record_id": identifier, "body": body, "confirmed": True})

    def lifecycle(self, action, service=None):
        return self.client.operation({"operation": "service.lifecycle", "action": action,
                                      "service": service, "confirmed": True})

    def preview_project(self, identifier):
        return self.client.get(f"projects/{identifier}/deletion-preview")

    def delete_project(self, identifier, digest):
        return self.client.operation({"operation": "project.delete", "project_id": identifier,
                                      "preview_digest": digest, "confirmed": True})

    def pending_deletions(self):
        return self.client.get("projects/pending-deletions")["deletions"]

    def resume_deletion(self, identifier):
        return self.client.operation({"operation": "project.resume", "deletion_id": identifier, "confirmed": True})

    def reset(self, *, clean_install=False, keep_mailbox_position=True):
        return self.client.operation({"operation": "database.reset", "clean_install": clean_install,
                                      "keep_mailbox_position": keep_mailbox_position, "confirmation": "RESET"})

    def recover(self):
        self.client.operation({"operation": "database.recover", "confirmed": True})
        return self.load()

    def embeddings(self, operation):
        return self.client.operation({"operation": "search.maintenance", "action": operation,
                                      "confirmed": operation == "reindex"})

    def storage(self, volume):
        result = self.client.operation({"operation": "credential-storage.move", "use_volume": volume, "confirmed": True})
        self.load()
        return result

    def replace_credential(self, name, value):
        self.client.operation({"operation": "credential.replace", "name": name, "value": value, "confirmed": True})
        return self.load()

    def apply_credentials(self, name=None):
        self.client.operation({"operation": "credential.apply", "name": name, "confirmed": True})
        return self.load()

    def generate_database_password(self):
        return self.client.request("POST", "credentials/database-password/generate")["password"]

    def redact(self, value):
        for secret in self.credentials.values():
            if secret and secret != CONFIGURED:
                value = value.replace(secret, "[hidden]")
        return value
