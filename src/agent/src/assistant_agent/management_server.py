"""Container entry points, health checks, and SSH-authorized connection discovery."""
import argparse
import json
import os
from pathlib import Path
import urllib.request

from .management_api import create_app, ensure_token, read_token, token_path
from .setup import Installation, SetupError


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
    parser.add_argument("action", choices=("serve", "health", "connection"))
    args = parser.parse_args()
    root = Path(os.environ["ASSISTANT_INSTALL_ROOT"])
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
