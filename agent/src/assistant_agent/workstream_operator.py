"""Trusted local operator transport; exposed through setup tools, not email or LLMs."""
import argparse
import json
import sys

import psycopg

from .config import database_url, load_config
from .database import Database, json_safe
from .workstreams import MODES, WorkstreamError, WorkstreamStore


def main():
    parser = argparse.ArgumentParser(description="Workstream ownership and confirmed deletion")
    parser.add_argument("action", choices=("list", "objects", "attach", "preview", "delete", "resume", "pending"))
    action = parser.parse_args().action
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise WorkstreamError("expected an operation object")
        config = load_config()
        db = Database(database_url(config))
        if db.fetch_one("SELECT to_regclass('workstreams') AS relation")["relation"] is None:
            raise WorkstreamError("Workstream schema is not installed; start the updated stack to apply migrations first")
        store = WorkstreamStore(db, config)
        if action == "list":
            result = {"workstreams": store.list(payload.get("before_id"))}
        elif action == "pending":
            result = {"deletions": db.fetch_all(
                """SELECT id, workstream_ids, mode, status, error, created_at FROM workstream_deletions
                   WHERE status != 'completed' ORDER BY id DESC LIMIT 50""")}
        elif action == "objects":
            result = {"objects": store.objects(positive_id(payload.get("workstream_id")))}
        elif action == "attach":
            if payload.get("confirmed") is not True:
                raise WorkstreamError("explicit confirmation is required")
            result = store.attach(positive_id(payload.get("workstream_id")), payload.get("kind"), payload.get("key"))
        elif action == "preview":
            result = store.preview(positive_id(payload.get("workstream_id")), mode(payload))
        else:
            if payload.get("confirmed") is not True or payload.get("writers_stopped") is not True:
                raise WorkstreamError("confirmation and stopped writer services are required")
            identifier = (positive_id(payload.get("deletion_id")) if action == "resume" else store.begin_delete(
                positive_id(payload.get("workstream_id")), mode(payload), payload.get("preview_digest")))
            result = store.execute_delete(identifier)
        print(json.dumps(json_safe(result), sort_keys=True))
    except Exception as exc:
        # Never serialize database diagnostics, provider responses or configuration.
        if isinstance(exc, WorkstreamError):
            message = str(exc)
        elif isinstance(exc, psycopg.OperationalError):
            message = "Database connection failed. Check PostgreSQL health and database credentials."
        elif isinstance(exc, psycopg.ProgrammingError):
            message = "Database schema or query failed. Check that the updated stack applied migration 008."
        elif isinstance(exc, OSError):
            message = "Filesystem or network operation failed. Check storage permissions and provider connectivity."
        else:
            message = "Unexpected workstream failure. Inspect pending deletions before retrying."
        print(json.dumps({"workstream_error": message}))
        raise SystemExit(1) from None


def positive_id(value):
    if type(value) is not int or not 1 <= value < 2**63:
        raise WorkstreamError("expected a positive 64-bit identifier")
    return value


def mode(payload):
    value = payload.get("mode")
    if value not in MODES:
        raise WorkstreamError("unknown deletion mode")
    return value


if __name__ == "__main__":
    main()
