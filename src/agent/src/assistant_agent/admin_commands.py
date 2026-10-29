import argparse
import json
import shlex
from datetime import datetime, timezone
from email.message import Message
from typing import Any

from .config import database_url, load_config
from .database import Database
from .email_approvals import EmailApprovals, admin_sender, send_admin_email
from .memory_store import MemoryStore
from .secret_store import configured_secret_names, read_secret


HELP = """Admin commands (subject line):
/admin status
/admin jobs [--cursor ID] [--limit N] | /admin jobs --recent [--limit N]
/admin logs JOB_ID [--cursor SEQUENCE] [--limit N]
/admin review JOB_ID
/admin cancel JOB_ID
/admin requeue JOB_ID
/admin instruct JOB_ID (instruction in message body; finished jobs are requeued)
/admin memory list|search|suggestions [--query TEXT] [--limit N]
/admin memory show|update|delete|approve|reject|pin|unpin|expire ID
/admin memory create (memory text in message body)
/admin reminders [--limit N]
/admin reminder-cancel ID
/admin permissions scripts|hosts|history
/admin permissions show VERSION_ID
/admin permissions block-script|unblock-script|revoke-script SCRIPT_ID
/admin permissions block-host|unblock-host HOST
/admin permissions revoke-grant GRANT_ID
/admin permissions remove-legacy RULE_ID
Memory update text and review instructions go in the message body.
"""


def is_admin_subject(subject: str) -> bool:
    return subject.strip().lower().startswith("/admin")


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[redacted]" if any(term in key.lower() for term in ("password", "secret", "api_key", "authorization", "token", "signing_key", "start_url")) else redact_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        for key in configured_secret_names():
            secret = read_secret(key) or ""
            if len(secret) >= 8:
                value = value.replace(secret, "[redacted]")
    return value


class CommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


class AdminCommands:
    def __init__(self, db, config):
        self.db = db
        self.config = config

    def handle(self, message: Message, subject: str, body: str) -> bool:
        if not admin_sender(message, self.config):
            return False
        message_id = message.get("Message-ID")
        if not message_id:
            return False
        return EmailApprovals(self.db, self.config).request(
            message_id, {"kind": "command", "subject": subject, "body": body}, subject + "\n\n" + body,
        )

    def execute(self, subject: str, body: str = "", confirmed: bool = False) -> str:
        words = shlex.split(subject)
        if not words or words[0].lower() != "/admin":
            raise ValueError("commands must begin with /admin")
        if len(words) == 1 or words[1] in {"help", "--help"}:
            return HELP
        parser = CommandParser(add_help=False)
        parser.add_argument("command")
        parser.add_argument("args", nargs="*")
        parser.add_argument("--cursor", type=int, default=0)
        parser.add_argument("--limit", type=int, default=25)
        parser.add_argument("--query", default="")
        parser.add_argument("--recent", action="store_true")
        options = parser.parse_args(words[1:])
        limit = min(max(options.limit, 1), 100)
        if options.cursor < 0:
            raise ValueError("cursor cannot be negative")
        command, args = options.command, options.args
        if command == "status" and not args:
            result = self.db.fetch_all("SELECT status, count(*) AS count FROM jobs GROUP BY status ORDER BY status")
        elif command == "jobs" and not args and options.recent:
            result = self.db.fetch_all("SELECT id, status, task_summary, last_error, updated_at FROM jobs ORDER BY id DESC LIMIT %s", (limit,))
        elif command == "jobs" and not args:
            result = self.db.fetch_all("SELECT id, status, task_summary, updated_at FROM jobs WHERE id > %s ORDER BY id LIMIT %s", (options.cursor, limit))
            return self.page(result, "/admin jobs --cursor %s --limit %s", "id", limit)
        elif command == "logs" and len(args) == 1:
            job_id = int(args[0])
            result = self.db.fetch_all("SELECT * FROM task_logs WHERE job_id = %s AND sequence > %s ORDER BY sequence LIMIT %s", (job_id, options.cursor, limit))
            return self.page(result, "/admin logs %s --cursor %%s --limit %%s" % job_id, "sequence", limit)
        elif command == "memory":
            return self.memory(args, body, options.query, limit, confirmed)
        elif command == "permissions":
            from .script_permissions import management
            result = management(self.db, self.config, args, confirmed)
        elif command == "reminders" and not args:
            result = self.db.fetch_all("SELECT id, title, task, run_at, status, recurrence_unit, recurrence_interval FROM reminders ORDER BY run_at NULLS LAST, id LIMIT %s", (limit,))
        elif command in {"cancel", "reminder-cancel", "review", "requeue"} and len(args) == 1:
            identifier = int(args[0])
            if not confirmed:
                return "This action requires confirmation. Reply to its approval email, or use the local CLI with --yes."
            if command == "requeue":
                if not self.db.requeue_job(identifier, "requeued by admin command"):
                    raise ValueError("job not found or cannot be requeued from its current status")
                result = {"id": identifier, "status": "queued"}
            elif command == "reminder-cancel":
                result = self.db.fetch_one("UPDATE reminders SET status = 'cancelled', updated_at = now() WHERE id = %s RETURNING id, status", (identifier,))
                self.db.execute("UPDATE jobs SET status = 'cancelled', locked_at = NULL, locked_by = NULL, updated_at = now() WHERE id = (SELECT job_id FROM reminders WHERE id = %s) AND status IN ('queued', 'running', 'waiting')", (identifier,))
            elif command == "cancel":
                result = self.db.fetch_one("UPDATE jobs SET status = 'cancelled', locked_at = NULL, locked_by = NULL, updated_at = now() WHERE id = %s AND status NOT IN ('completed', 'failed', 'cancelled') RETURNING id, status", (identifier,))
            else:
                job = self.db.fetch_one("SELECT * FROM jobs WHERE id = %s AND status = 'needs_review'", (identifier,))
                if not job:
                    raise ValueError("job is not awaiting review")
                from .email_ingest import EmailDownloader
                EmailDownloader(self.db, self.config).apply_email_review_override(job, 0, body)
                result = {"id": identifier, "status": "queued"}
        elif command == "instruct" and len(args) == 1:
            identifier = int(args[0])
            if not body.strip():
                raise ValueError("instruction text is required in the message body")
            if not self.db.fetch_one("SELECT id FROM jobs WHERE id = %s", (identifier,)):
                raise ValueError("job not found")
            result = {"id": identifier, "status": self.db.add_job_instruction(identifier, body.strip(), "admin")}
        else:
            raise ValueError("unknown command or invalid arguments")
        return json.dumps(redact_secrets(result), default=str, indent=2)

    def page(self, rows, template, cursor_field, limit):
        response = json.dumps(redact_secrets(rows), default=str, indent=2)
        if len(rows) == limit:
            response += "\n\nNext: " + template % (rows[-1][cursor_field], limit)
        return response

    def memory(self, args, body, query, limit, confirmed):
        if not args:
            raise ValueError("memory action is required")
        action = args[0]
        store = MemoryStore(self.db, self.config)
        if action in {"list", "search"} and len(args) == 1:
            result = store.keyword_search(query, limit=limit)
        elif action == "suggestions" and len(args) == 1:
            result = self.db.fetch_all("SELECT id, content, kind, metadata, created_at FROM agent_memories WHERE metadata->>'state' = 'suggested' ORDER BY id LIMIT %s", (limit,))
        elif action == "create" and len(args) == 1:
            result = store.create(body, kind="preference", actor="admin", embed=False, metadata={"explicit_user_requested": True})
        elif len(args) == 2:
            identifier = int(args[1])
            if action == "show":
                result = store.get(identifier, include_expired=True)
            elif action == "update":
                result = store.update(identifier, content=body, actor="admin", include_expired=True, embed=False)
            elif action == "delete":
                if not confirmed:
                    return "This action requires confirmation. Reply to its approval email, or use the local CLI with --yes."
                result = store.delete(identifier, actor="admin", include_expired=True)
            elif action in {"approve", "reject"}:
                memory = store.get(identifier, include_expired=True)
                if not memory or (memory.get("metadata") or {}).get("state") != "suggested":
                    raise ValueError("memory is not a pending suggestion")
                metadata = {**memory["metadata"], "state": "active" if action == "approve" else "rejected"}
                result = store.update(identifier, metadata=metadata, actor="admin", include_expired=True, embed=False)
            elif action in {"pin", "unpin", "expire"}:
                fields = {"expires_at": datetime.now(timezone.utc).isoformat()} if action == "expire" else {"pinned": action == "pin"}
                result = store.update(identifier, actor="admin", include_expired=True, **fields)
            else:
                raise ValueError("unknown memory action")
        else:
            raise ValueError("invalid memory arguments")
        return json.dumps(redact_secrets(result), default=str, indent=2)

    def reply(self, original, body):
        send_admin_email(self.config, "Assistant admin response", body[:100000])


def main():
    parser = argparse.ArgumentParser(description="Local assistant administration (no LLM calls)")
    parser.add_argument("subject", help="quoted /admin command")
    parser.add_argument("--body", default="")
    parser.add_argument("--yes", action="store_true", help="skip the email confirmation token; the local operator already confirmed")
    options = parser.parse_args()
    config = load_config()
    try:
        print(AdminCommands(Database(database_url(config)), config).execute(options.subject, options.body, confirmed=options.yes))
    except (ValueError, TypeError) as exc:
        raise SystemExit("Command error: %s\n\n%s" % (exc, HELP)) from None


if __name__ == "__main__":
    main()