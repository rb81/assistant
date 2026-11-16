"""Installation bootstrap and lifecycle operations run inside the admin image."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import secrets
import sys
import threading
import time

from rich.console import Console

from .management_api import ensure_token
from .setup import DockerCommandError, Installation, SetupError, setup_failure_message


MANAGEMENT_START_WAIT_SECONDS = 300


@contextmanager
def startup_progress(*, interval=15, console=None):
    console = console or Console(highlight=False)
    started = time.monotonic()
    stage_started = started
    current_stage = "preparing startup"
    lock = threading.Lock()
    stopped = threading.Event()

    def emit(message, now):
        console.print(f"[{int(now - started):>4}s] {message}", markup=False)

    def progress(stage):
        nonlocal current_stage, stage_started
        with lock:
            now = time.monotonic()
            duration = int(now - stage_started)
            emit(stage + (f" (previous stage: {duration}s)" if duration else ""), now)
            current_stage, stage_started = stage, now

    def heartbeat():
        while not stopped.wait(interval):
            with lock:
                now = time.monotonic()
                emit(f"Still waiting: {current_stage} ({int(now - stage_started)}s in this stage)", now)

    reporter = threading.Thread(target=heartbeat, name="startup-progress", daemon=True)
    reporter.start()
    completed = False
    try:
        yield progress
        completed = True
    finally:
        stopped.set()
        reporter.join()
        with lock:
            emit("Startup checks completed." if completed else "Startup stopped during: " + current_stage,
                 time.monotonic())


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
    with startup_progress() as progress:
        progress("preparing saved configuration and credentials")
        instance = bootstrap(root)
        configured = instance.state.get("setup_complete")
        if configured:
            instance.start_containers(progress=progress)
        else:
            try:
                progress("creating and starting management, Docker reader, and PostgreSQL")
                instance.compose(
                    ["up", "-d", "management", "postgres"], timeout=1800,
                )
                progress("containers started; checking management and PostgreSQL health")
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
    if configured:
        print("Assistant and its management API are running.")
    else:
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
