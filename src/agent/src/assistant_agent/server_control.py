"""Installation bootstrap and lifecycle operations run inside the admin image."""
import argparse
import json
from pathlib import Path
import secrets
import sys

from .management_api import ensure_token
from .setup import Installation, SetupError, setup_failure_message


def bootstrap(root):
    root = Path(root)
    instance = Installation(root)
    if not instance.secret_values.get("AGENT_DATABASE_PASSWORD"):
        if (root / "data/postgres/pgdata/PG_VERSION").exists():
            raise SetupError("Existing PostgreSQL data needs its current password; no replacement was generated.")
        instance.secret_values["AGENT_DATABASE_PASSWORD"] = secrets.token_urlsafe(36)
    instance.save()
    ensure_token(root)
    return Installation(root)


def start(root):
    print("Preparing saved configuration and credentials...", flush=True)
    instance = bootstrap(root)
    if instance.state.get("setup_complete"):
        instance.start_containers(progress=lambda stage: print("Server: " + stage + "...", flush=True))
        print("Assistant and its management API are running.")
    else:
        print("Starting management and PostgreSQL; image downloads may take several minutes...", flush=True)
        instance.compose(["up", "-d", "--wait", "--wait-timeout", "90", "management", "postgres"], timeout=1800)
        print("Assistant management is running; setup is required before task workers start.")
    print("Open the desktop client to inspect this instance, or run ./run.sh to complete setup in the TUI.")


def main():
    parser = argparse.ArgumentParser(description="Containerized Assistant server administration")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("action", choices=("start", "stop", "status"))
    options = parser.parse_args()
    try:
        if options.action == "start":
            start(options.root)
        elif options.action == "stop":
            Installation(options.root).compose(["stop"], timeout=180)
            print("Assistant containers stopped. Saved data was preserved.")
        else:
            print(Installation(options.root).compose(["ps", "--all"]))
    except SetupError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError) as error:
        print(setup_failure_message(error, "server " + options.action), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
