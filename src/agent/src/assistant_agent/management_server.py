"""Container entry points, health checks, and SSH-authorized connection discovery."""
import argparse
import json
import os
from pathlib import Path
import urllib.request

from .management_api import create_app, ensure_token, read_token, token_path
from .setup import Installation, SetupError


def verify(root, expected_revision):
    """Verify the API actually serving this installation, without printing its data."""
    from .management_client import ManagementClient

    with ManagementClient("http://127.0.0.1:8765", read_token(token_path(root))) as client:
        health = client.health()
        if expected_revision != "unknown" and health["revision"] != expected_revision:
            raise SetupError("The running management API is not the requested revision; update verification failed.")
        required = {"configuration.structured", "services.read", "workspace.manage"}
        if not required.issubset(health["capabilities"]):
            raise SetupError("The running management API lacks required client capabilities.")
        configuration = client.configuration()
        client.services()
        if configuration["setup_complete"]:
            if not {"events.stream", "tasks.snapshot"}.issubset(health["capabilities"]):
                raise SetupError("The persistent management reader is not enabled.")
            client.get("tasks/summary")
            client.request("POST", "query", {"action": "workspace.list"})
    print("Management API revision, settings, services and configured database reads verified.")


def connection(root):
    instance = Installation(root)
    port = int(instance.values.get("ASSISTANT_MANAGEMENT_PORT", "8765"))
    if not 1 <= port <= 65535:
        raise SetupError("Invalid management port in saved deployment settings.")
    return {
        "api_version": 1, "host": "127.0.0.1", "port": port,
        "token": read_token(token_path(root)),
    }


def main():
    parser = argparse.ArgumentParser(description="Containerized Assistant management")
    parser.add_argument("action", choices=("serve", "health", "connection", "verify"))
    args = parser.parse_args()
    root = Path(os.environ["ASSISTANT_INSTALL_ROOT"])
    if args.action == "verify":
        try:
            verify(root, os.environ.get("ASSISTANT_EXPECTED_REVISION", "unknown"))
        except SetupError as error:
            raise SystemExit(str(error)) from None
        return
    if args.action == "connection":
        print(json.dumps(connection(root)))
        return
    if args.action == "health":
        request = urllib.request.Request(
            "http://127.0.0.1:8765/v1/health",
            headers={"Authorization": "Bearer " + read_token(token_path(root))},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            if json.load(response).get("api_version") != 1:
                raise SystemExit(1)
        return
    import uvicorn

    token = ensure_token(root)
    if os.geteuid() == 0:
        os.chown(token_path(root), int(os.getenv("ASSISTANT_UID", "0")), int(os.getenv("ASSISTANT_GID", "0")))
    uvicorn.run(create_app(root, token), host="0.0.0.0", port=8765, access_log=False)


if __name__ == "__main__":
    main()
