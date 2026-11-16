"""Installation bootstrap and lifecycle operations run inside the admin image."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import secrets
import sys

from rich.console import Console

from .management_api import ensure_token
from .setup import DockerCommandError, Installation, SetupError, setup_failure_message


MANAGEMENT_START_WAIT_SECONDS = 300


@contextmanager
def startup_progress():
    console = Console()
    with console.status("Starting Assistant services...") as status:
        def progress(stage):
            if console.is_terminal:
                status.update(stage)
            else:
                console.print("Server: " + stage + "...", markup=False)
        yield progress


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
        with startup_progress() as progress:
            instance.start_containers(progress=progress)
        print("Assistant and its management API are running.")
    else:
        print("Starting management and PostgreSQL; waiting for Docker health checks...", flush=True)
        try:
            with startup_progress() as progress:
                instance.compose(
                    ["up", "-d", "management", "postgres"], timeout=1800,
                )
                instance.wait_for_container_health(
                    ("management", "management-docker", "postgres"),
                    ("management", "management-docker", "postgres"),
                    progress=progress, timeout=MANAGEMENT_START_WAIT_SECONDS,
                )
        except DockerCommandError as error:
            raise SetupError(
                str(error) + " Installation is incomplete. Run ./run.sh server status "
                "and inspect Docker logs before retrying."
            ) from error
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
