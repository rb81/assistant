import argparse
import hashlib
import hmac
import json
import os
import secrets
import shlex
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage, Message
from email.utils import getaddresses
from typing import Any

import authres
from psycopg.types.json import Jsonb

from .config import agent_email, database_url, load_config
from .database import Database
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
/admin confirm TOKEN
Memory update text and review instructions go in the message body.
"""


def is_admin_subject(subject: str) -> bool:
    return subject.strip().lower().startswith("/admin")


def authenticated_admin(message: Message, config, subject: str, body: str) -> bool:
    admin = str(config.get("agent.admin.email") or "").strip().lower()
    headers = message.get_all("From", [])
    addresses = getaddresses(headers)
    if not admin or len(headers) != 1 or len(addresses) != 1 or addresses[0][1].lower() != admin:
        return False
    if str(message.get("Auto-Submitted", "no")).lower() != "no" or str(message.get("Precedence", "")).lower() in {"bulk", "list", "junk"}:
        return False
    signing_key = str(config.get("agent.admin.command_signing_key") or "")
    signature = message.get("X-Assistant-Admin-Signature", "")
    timestamp = message.get("X-Assistant-Admin-Timestamp", "")
    if signing_key and signature:
        try:
            if abs(datetime.now(timezone.utc).timestamp() - int(timestamp)) > 300:
                return False
            payload = "\n".join((message.get("Message-ID", ""), subject, body, timestamp))
            expected = hmac.new(signing_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(signature, expected)
        except ValueError:
            return False
    trusted = config.get_list("agent.admin.trusted_authserv_ids")
    results = message.get_all("Authentication-Results", [])
    if not trusted or not results:
        return False
    try:
        header = authres.AuthenticationResultsHeader.parse("Authentication-Results: " + results[0])
        if header.authserv_id not in trusted:
            return False
        domain = admin.rsplit("@", 1)[-1]
        return any(result.method == "dmarc" and result.result == "pass" and any(prop.type == "header" and prop.name == "from" and prop.value.lower() == domain for prop in result.properties) for result in header.results)
    except (ValueError, authres.SyntaxError):
        return False


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
        if not authenticated_admin(message, self.config, subject, body):
            return False
        message_id = message.get("Message-ID")
        if not message_id:
            return False
        recent = self.db.fetch_one("SELECT count(*) AS count FROM runtime_state WHERE key LIKE 'admin-command:%%' AND updated_at > now() - interval '1 minute'")
        if recent and recent["count"] >= 10:
            return False
        key = "admin-command:" + hashlib.sha256(message_id.encode()).hexdigest()
        claimed = self.db.fetch_one("INSERT INTO runtime_state(key, value) VALUES (%s, '{\"status\":\"claimed\"}'::jsonb) ON CONFLICT DO NOTHING RETURNING key", (key,))
        if not claimed:
            return False
        try:
            response = self.execute(subject, body)
        except (ValueError, TypeError) as exc:
            response = "Command error: %s\n\n%s" % (exc, HELP)
        self.db.set_state(key, {"status": "executed"})
        self.reply(message, response)
        self.db.set_state(key, {"status": "replied"})
        return True

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
        elif command == "reminders" and not args:
            result = self.db.fetch_all("SELECT id, title, task, run_at, status, recurrence_unit, recurrence_interval FROM reminders ORDER BY run_at NULLS LAST, id LIMIT %s", (limit,))
        elif command in {"cancel", "reminder-cancel", "review", "requeue"} and len(args) == 1:
            identifier = int(args[0])
            if command != "review" and not confirmed:
                return self.confirmation(subject)
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
        elif command == "confirm" and len(args) == 1:
            key = "admin-confirm:" + hashlib.sha256(args[0].encode()).hexdigest()
            row = self.db.fetch_one("DELETE FROM runtime_state WHERE key = %s RETURNING value", (key,))
            if not row or row["value"]["admin"] != self.config.get("agent.admin.email") or datetime.fromisoformat(row["value"]["expires"]) <= datetime.now(timezone.utc):
                raise ValueError("confirmation is expired, invalid, or already used")
            return self.execute(row["value"]["subject"], confirmed=True)
        else:
            raise ValueError("unknown command or invalid arguments")
        return json.dumps(redact_secrets(result), default=str, indent=2)

    def page(self, rows, template, cursor_field, limit):
        response = json.dumps(redact_secrets(rows), default=str, indent=2)
        if len(rows) == limit:
            response += "\n\nNext: " + template % (rows[-1][cursor_field], limit)
        return response

    def confirmation(self, subject):
        token = secrets.token_urlsafe(24)
        self.db.set_state("admin-confirm:" + hashlib.sha256(token.encode()).hexdigest(), {"subject": subject, "admin": self.config.get("agent.admin.email"), "expires": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()})
        return "Confirm this action within 10 minutes:\n/admin confirm " + token

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
                    return self.confirmation("/admin memory delete %s" % identifier)
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
        message = EmailMessage()
        message["From"] = agent_email(self.config)
        message["To"] = self.config.get("agent.admin.email")
        message["Subject"] = "Assistant admin response"
        message["Auto-Submitted"] = "auto-replied"
        message["In-Reply-To"] = original["Message-ID"]
        message.set_content(body[:100000])
        host = self.config.get("agent.email.smtp_host")
        port = self.config.get_int("agent.email.smtp_port", 587)
        context = ssl.create_default_context()
        client = smtplib.SMTP_SSL(host, port, context=context) if port == 465 else smtplib.SMTP(host, port)
        with client:
            if port != 465:
                client.starttls(context=context)
            client.login(self.config.get("agent.email.smtp_username"), self.config.get("agent.email.smtp_password"))
            client.send_message(message)


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