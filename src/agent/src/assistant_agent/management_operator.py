"""Structured, local-only management transport. No terminal presentation."""
import json
import sys

import psycopg

from .admin_commands import AdminCommands, redact_secrets
from .config import database_url, load_config
from .database import Database, json_safe
from .script_permissions import PermissionRevocationIncomplete


COLLECTIONS = {
    "tasks": ("jobs", "id, status, task_summary, last_error, metadata->>'final_response' AS result, updated_at", "task_summary"),
    "memories": ("agent_memories", "id, content, kind, pinned, metadata, expires_at, created_at", "content"),
    "reminders": ("reminders", "id, title, task, run_at, status, recurrence_unit, recurrence_interval", "title"),
    "projects": ("workstreams", "id, title, status, main_thread_id, parent_id, created_at", "title"),
}
MUTATIONS = {
    "cancel": "cancel", "retry": "requeue", "review": "review", "instruct": "instruct",
    "cancel-reminder": "reminder-cancel", "delete-memory": "memory delete",
    "update-memory": "memory update", "create-memory": "memory create",
    "pin-memory": "memory pin", "unpin-memory": "memory unpin",
    "approve-memory": "memory approve", "reject-memory": "memory reject",
    "block-script": "permissions block-script", "unblock-script": "permissions unblock-script",
    "revoke-script": "permissions revoke-script",
}


def execute(db, config, request):
    kind = request.get("kind")
    page = request.get("page", 0)
    if type(page) is not int or not 0 <= page <= (2**63 - 1) // 8:
        raise ValueError("Invalid page")
    query = request.get("query", "")
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError("Search must be at most 500 characters")
    identifier = request.get("id")
    if identifier is not None and (type(identifier) is not int or not 0 < identifier < 2**63):
        raise ValueError("Invalid record identifier")
    if kind in COLLECTIONS:
        table, columns, search = COLLECTIONS[kind]
        where, values = (f"WHERE {search} ILIKE %s", ["%" + query + "%"]) if query else ("", [])
        if identifier is not None:
            where, values = "WHERE id = %s", [identifier]
        total = db.fetch_one(f"SELECT count(*) AS total FROM {table} {where}", tuple(values))["total"]
        rows = db.fetch_all(f"SELECT {columns} FROM {table} {where} ORDER BY id DESC LIMIT 8 OFFSET %s",
                            (*values, page * 8))
        return {"rows": rows, "total": total}
    if kind == "summary":
        return {"jobs": db.fetch_all("SELECT status, count(*) AS count FROM jobs GROUP BY status ORDER BY status")}
    if kind == "task-events" and identifier:
        rows = db.fetch_all(
            "SELECT sequence, created_at, event_type, tool_name, tool_action, input_data, output_data, tokens_used, duration_ms FROM task_logs WHERE job_id = %s ORDER BY sequence DESC LIMIT 8 OFFSET %s",
            (identifier, page * 8))
        total = db.fetch_one("SELECT count(*) AS total FROM task_logs WHERE job_id = %s", (identifier,))["total"]
        return {"rows": rows, "total": total}
    if kind == "project-objects" and identifier:
        from .workstreams import WorkstreamStore
        rows = WorkstreamStore(db, config).objects(identifier)
        return {"rows": rows[page * 8:(page + 1) * 8], "total": len(rows)}
    if kind == "scripts":
        from .script_permissions import management
        return management(db, config, ["scripts"], page=page, query=query)
    if kind == "script" and identifier:
        from .script_permissions import management
        detail = management(db, config, ["show", str(identifier)])
        script = db.fetch_one("SELECT id, entry, blocked FROM sandbox_scripts WHERE id = %s",
                              (detail["version"]["script_id"],))
        if not script:
            raise ValueError("Script no longer exists")
        return {**detail, **script}
    if kind == "mutate":
        action = request.get("action")
        if action not in MUTATIONS or request.get("confirmed") is not True:
            raise ValueError("A supported action and explicit confirmation are required")
        if action != "create-memory" and identifier is None:
            raise ValueError("Select an existing record")
        body = request.get("body", "")
        if not isinstance(body, str) or len(body) > 100_000:
            raise ValueError("Invalid text")
        if action in {"create-memory", "update-memory", "instruct"} and not body.strip():
            raise ValueError("Text cannot be empty")
        subject = "/admin " + MUTATIONS[action] + (f" {identifier}" if identifier else "")
        result = json.loads(AdminCommands(db, config).execute(subject, body, confirmed=True))
        if result is None or result is False:
            raise ValueError("The record was not changed; refresh its current state")
        return {"result": result}
    raise ValueError("Unsupported management request")


def main():
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError("Expected a management request")
        config = load_config()
        result = execute(Database(database_url(config)), config, request)
        print(json.dumps(redact_secrets(json_safe(result))))
    except PermissionRevocationIncomplete:
        print(json.dumps({"error": "Permissions were saved, but active sandbox runs could not be stopped. "
                                   "Stop the sandbox service before relying on revocation, then check "
                                   "current permissions and service health."}))
        raise SystemExit(1) from None
    except (ValueError, TypeError, KeyError, RuntimeError, psycopg.Error, OSError):
        print(json.dumps({"error": "Management request failed. Check service health and migrations, "
                                   "then refresh the record before retrying. No success was confirmed."}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
