"""Shared HTTP transport for CLI and terminal management."""
import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from pydantic import ValidationError

from .management_contracts import (
    ConfigurationCheckResponse, FullConfigurationResponse, FullHealthResponse, LogsResponse, ServicesResponse,
    ManagementOperationResponse,
)
from .setup import SetupError


class ManagementClient:
    @classmethod
    def from_installation(cls, root, *, url=None, token_file=None):
        """Read connection credentials only; platform state comes from HTTP."""
        from .management_api import read_token, token_path
        from .setup import parse_environment, read_regular

        if url is None:
            url = os.environ.get("ASSISTANT_MANAGEMENT_URL")
        if not url:
            port = 8765
            if os.environ.get("ASSISTANT_CONTAINER_ADMIN") != "1":
                values, _ = parse_environment((read_regular(Path(root) / "data/.env") or b"").decode())
                port = int(values.get("ASSISTANT_MANAGEMENT_PORT") or port)
            url = f"http://127.0.0.1:{port}"
        path = token_file or os.environ.get("ASSISTANT_MANAGEMENT_TOKEN_FILE") or token_path(root)
        return cls(url, read_token(path))

    def __init__(self, url, token):
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise SetupError("Management URL contains an invalid address or port.") from None
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or port == 0):
            raise SetupError("Management URL must be a loopback HTTP address. Use SSH forwarding for remote servers.")
        self.url = url.rstrip("/")
        self.token = token

    def get(self, resource):
        return self.request("GET", resource)

    def request(self, method, resource, payload=None, *, idempotency_key=None):
        headers = {"Authorization": "Bearer " + self.token}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            with httpx.Client(timeout=45, trust_env=False, follow_redirects=False) as client:
                response = client.request(
                    method, self.url + "/v2/" + resource, headers=headers, json=payload,
                )
                if not response.is_success:
                    try:
                        error = response.json()
                    except ValueError:
                        error = None
                    message = error.get("error", {}).get("message") if (
                        isinstance(error, dict) and isinstance(error.get("error"), dict)) else None
                    if not isinstance(message, str) or not message.strip():
                        message = f"Management API returned HTTP {response.status_code}. Check the server and token."
                    raise SetupError(message)
                payload = response.json()
                if not isinstance(payload, (dict, list)):
                    raise ValueError
                return payload
        except httpx.HTTPError:
            raise SetupError("Could not reach the management API. Check the server or SSH tunnel.") from None
        except ValueError:
            raise SetupError("Management API returned an invalid response.") from None

    def configuration(self):
        return self.read("configuration", FullConfigurationResponse)

    def configuration_check(self):
        return self.read("configuration/check", ConfigurationCheckResponse)

    def read(self, resource, contract):
        try:
            return contract.model_validate(self.get(resource)).model_dump()
        except ValidationError:
            raise SetupError("Management API returned an incompatible response.") from None

    def health(self):
        return self.read("health", FullHealthResponse)

    def services(self):
        return self.read("services", ServicesResponse)["services"]

    def logs(self, service):
        return self.read("services/" + quote(service, safe="") + "/logs", LogsResponse)["content"]

    def operation(self, payload, *, idempotency_key=None, wait=True):
        def validated(value):
            try:
                return ManagementOperationResponse.model_validate(value).model_dump()
            except ValidationError:
                raise SetupError("Management API returned an incompatible operation response.") from None
        operation = validated(self.request("POST", "operations", payload,
                                           idempotency_key=idempotency_key or uuid.uuid4().hex))
        if not wait:
            return operation
        deadline = time.monotonic() + 3600
        while operation["state"] in {"queued", "running"}:
            if time.monotonic() >= deadline:
                raise SetupError("Management operation is still pending: " + operation["id"])
            time.sleep(0.2)
            operation = validated(self.get("operations/" + quote(operation["id"], safe="")))
        if operation["state"] != "succeeded":
            raise SetupError(operation.get("error") or "Management operation did not complete.")
        return operation["result"]

    def query(self, action, arguments=None, **context):
        return self.request("POST", "query", {"action": action, "arguments": arguments or {}, **context})


def run_command(options):
    if options.management_action == "serve":
        from .management_api import serve
        serve(options.root, options.port)
        return
    client = ManagementClient.from_installation(options.root, url=options.url, token_file=options.token_file)
    if options.management_action == "tui":
        from .management_api_backend import ApiManagementBackend
        from .management_tui import ManagementApp
        ui = ManagementApp(options.root, backend=ApiManagementBackend(options.root, client))
        ui.run(options)
        return
    import json

    if options.management_action == "config":
        result = client.configuration()
    elif options.management_action == "check":
        result = client.configuration_check()
    elif options.management_action == "services":
        result = {"services": client.services()}
    elif options.management_action == "logs":
        if not options.service:
            raise SetupError("Select a service: management logs SERVICE.")
        print(client.logs(options.service))
        return
    elif options.management_action == "query":
        import sys
        result = client.request("POST", "query", json.load(sys.stdin))
    elif options.management_action == "operation":
        import sys
        result = client.operation(json.load(sys.stdin), wait=False)
    elif options.management_action == "operation-status":
        if not options.operation_id:
            raise SetupError("Supply --operation-id.")
        result = client.get("operations/" + quote(options.operation_id, safe=""))
    elif options.management_action in {"domains", "clients"}:
        result = client.get(options.management_action)
    elif options.management_action == "records":
        if options.service not in {"tasks", "projects", "memories", "reminders", "research", "scripts"}:
            raise SetupError("Select a supported record collection.")
        path = "records/" + options.service
        path += f"/{options.record_id}" if options.record_id else f"?page={options.page}&query={quote(options.search, safe='')}"
        result = client.get(path)
    elif options.management_action == "approvals":
        result = client.get(f"approvals?offset={options.page * 50}")
    elif options.management_action == "permissions":
        if options.service not in {"hosts", "history"}:
            raise SetupError("Select permissions hosts or history.")
        result = client.get("permissions/" + options.service)
    elif options.management_action == "messages":
        if not options.record_id:
            raise SetupError("Supply a task number with --record-id.")
        result = client.get(f"tasks/{options.record_id}/messages?page={options.page}")
    elif options.management_action in {"poll", "acknowledge"}:
        if not options.client_id:
            raise SetupError("Supply --client-id for the registered polling client.")
        path = "clients/" + quote(options.client_id, safe="")
        if options.management_action == "poll":
            result = client.get(path + "/messages")
        else:
            result = client.request("POST", path + "/acknowledgments", {"message_ids": options.message_id})
    else:
        result = client.health()
    print(json.dumps(result, indent=2, default=str))
