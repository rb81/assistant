"""Read-only readiness probes backed by actual worker polling progress."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import json
import logging
import math
import os
from pathlib import Path
import stat
import time


LOGGER = logging.getLogger(__name__)
HEALTH_PATH = Path("/run/assistant-health/progress.json")
WORKER_ROLES = frozenset({
    "task-agent", "deep-research-agent", "downloader", "telegram-receiver",
    "request-preprocessor", "reminder-scheduler", "workspace-indexer", "supervisor", "heartbeat",
    "tool-cache-cleanup",
})
_reporter = ContextVar("assistant_worker_health", default=None)


class ProgressReporter:
    def __init__(self, role, path=HEALTH_PATH):
        self.path = Path(path)
        self.record = {
            "role": role, "phase": "starting", "last_success": None, "max_age_seconds": 180,
        }
        self._write()

    def _write(self):
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            pending = self.path.with_suffix(".new")
            descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(self.record, stream, separators=(",", ":"))
            os.replace(pending, self.path)
        except OSError:
            LOGGER.warning("Worker health progress could not be published; readiness remains unverified")

    def fail(self):
        self.record["phase"] = "error"
        self._write()

    @contextmanager
    def cycle(self, interval_seconds):
        self.record.update(phase="polling", max_age_seconds=max(120, 3 * interval_seconds))
        self._write()
        try:
            yield
        except BaseException:
            self.fail()
            raise
        else:
            if self.record["phase"] != "error":
                self.record.update(phase="idle", last_success=time.monotonic())
                self._write()


@contextmanager
def reporting(role, path=HEALTH_PATH):
    reporter = ProgressReporter(role, path) if role in WORKER_ROLES and Path(path).parent.is_dir() else None
    token = _reporter.set(reporter)
    try:
        yield reporter
    except BaseException:
        if reporter:
            reporter.fail()
        raise
    else:
        if reporter:
            reporter.record["phase"] = "stopped"
            reporter._write()
    finally:
        _reporter.reset(token)


def poll_cycle(interval_seconds):
    reporter = _reporter.get()
    return reporter.cycle(interval_seconds) if reporter else nullcontext()


def polling_failed():
    reporter = _reporter.get()
    if reporter:
        reporter.fail()


def database_ready():
    import psycopg
    from .config import database_url, load_config

    with psycopg.connect(
        database_url(load_config()), connect_timeout=3,
        options="-c statement_timeout=3000 -c default_transaction_read_only=on",
    ) as connection:
        return connection.execute("SELECT 1").fetchone() == (1,)


def check_health(role, *, path=HEALTH_PATH, probe_database=database_ready, now=None):
    if role not in WORKER_ROLES:
        return False, "Worker health is not instrumented for this role"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "r") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError
            record = json.loads(stream.read(4097))
        success = record["last_success"]
        max_age = record["max_age_seconds"]
        if (record["role"] != role or record["phase"] not in {"idle", "polling"}
                or type(success) not in {int, float} or not math.isfinite(success)
                or type(max_age) not in {int, float} or not math.isfinite(max_age) or max_age <= 0):
            return False, "Worker has no verified successful polling cycle, or its loop reported an error"
        age = (time.monotonic() if now is None else now) - success
        if age < 0 or age > max_age:
            return False, "Worker progress is unverified: no recent completed cycle (work may still be in progress)"
    except (OSError, ValueError, TypeError, KeyError):
        return False, "Worker polling progress is unavailable or invalid"
    try:
        if not probe_database():
            return False, "Worker database readiness could not be verified"
    except Exception:
        return False, "Worker database readiness could not be verified"
    return True, "Recent worker polling and authenticated database read verified; job/integration outcomes unverified"


def main():
    healthy, message = check_health(os.environ.get("AGENT_ROLE", ""))
    print(message)
    raise SystemExit(0 if healthy else 1)


if __name__ == "__main__":
    main()
