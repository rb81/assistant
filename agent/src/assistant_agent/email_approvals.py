import hashlib
import hmac
import json
import re
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage, Message
from email.utils import getaddresses, make_msgid
from html.parser import HTMLParser

from .config import agent_email


MARKER = "=== ASSISTANT APPROVAL ==="
TOKEN_PATTERN = re.compile(r"\bOTP:\s*([A-Za-z0-9_-]{32})(?![A-Za-z0-9_-])")
HASH_PATTERN = re.compile(r"\bRequest hash:\s*([a-f0-9]{64})\b")


def admin_sender(message: Message, config) -> bool:
    headers = message.get_all("From", [])
    addresses = getaddresses(headers)
    admin = str(config.get("agent.admin.email") or "").strip().lower()
    return bool(
        admin and len(headers) == 1 and len(addresses) == 1
        and addresses[0][1].lower() == admin
        and str(message.get("Auto-Submitted", "no")).lower() == "no"
        and str(message.get("Precedence", "")).lower() not in {"bulk", "list", "junk"}
    )


class _ReplyHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"script", "style"}:
            self.hidden += 1
        if "gmail_signature" in attributes.get("class", ""):
            self.parts.append("\n--\n")
        elif tag in {"blockquote", "hr"} or attributes.get("id") == "divRplyFwdMsg" or "gmail_quote" in attributes.get("class", ""):
            self.parts.append("\n> ")
        elif tag in {"br", "p", "div", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "blockquote", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def reply_text(message: Message) -> str:
    plain, html = [], []
    parts = [message]
    while parts:
        part = parts.pop(0)
        if part.get_content_disposition() == "attachment" or part.get_content_maintype() == "message":
            continue
        if part.is_multipart():
            parts[0:0] = part.get_payload()
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain":
            plain.append(text)
        else:
            parser = _ReplyHTML()
            parser.feed(text)
            html.append("".join(parser.parts))
    return "\n".join(plain or html)


def approval_reply(message: Message) -> tuple[str, str, str] | None:
    text = reply_text(message)
    tokens = set(TOKEN_PATTERN.findall(text))
    hashes = set(HASH_PATTERN.findall(text))
    if MARKER not in text and not tokens and not hashes:
        return None
    if len(tokens) != 1 or len(hashes) != 1:
        raise ValueError("Keep exactly one original approval message, including its OTP and request hash.")
    instructions = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if (
            MARKER in line or TOKEN_PATTERN.search(line) or HASH_PATTERN.search(line)
            or stripped.startswith(">") or stripped == "--"
            or re.match(r"On .+?wrote:", "\n".join(lines[index:index + 4]), re.IGNORECASE | re.DOTALL)
            or re.match(r"(Sent from my (iPhone|iPad|Android)|Get Outlook for )", stripped, re.IGNORECASE)
            or re.match(r"-+\s*(Original Message|Forwarded message)", stripped, re.IGNORECASE)
            or re.match(r"From:\s", stripped, re.IGNORECASE)
        ):
            break
        instructions.append(line)
    return tokens.pop(), hashes.pop(), "\n".join(instructions).strip()


def send_admin_email(config, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = agent_email(config)
    message["To"] = config.get("agent.admin.email")
    message["Subject"] = subject
    message["Message-ID"] = make_msgid()
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(body)
    host = config.get("agent.email.smtp_host")
    port = config.get_int("agent.email.smtp_port", 587)
    context = ssl.create_default_context()
    client = smtplib.SMTP_SSL(host, port, context=context, timeout=30) if port == 465 else smtplib.SMTP(host, port, timeout=30)
    with client:
        if port != 465:
            client.starttls(context=context)
        client.login(config.get("agent.email.smtp_username"), config.get("agent.email.smtp_password"))
        client.send_message(message)


class EmailApprovals:
    def __init__(self, db, config):
        self.db = db
        self.config = config

    def request(self, source_id: str, payload: dict, summary: str) -> bool:
        admin = str(self.config.get("agent.admin.email") or "").strip().lower()
        if not admin:
            raise ValueError("Admin email is not configured.")
        now = datetime.now(timezone.utc)
        self.db.execute(
            "DELETE FROM runtime_state WHERE key LIKE 'email-approval:%%' AND (value->>'expires')::timestamptz <= now()"
        )
        source_key = "admin-request:" + hashlib.sha256(source_id.encode()).hexdigest()
        claimed = self.db.fetch_one(
            """INSERT INTO runtime_state(key, value) VALUES (%s, '{"status":"sending"}'::jsonb)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
               WHERE runtime_state.value->>'status' = 'sending'
                 AND runtime_state.updated_at < now() - interval '5 minutes'
               RETURNING key""", (source_key,),
        )
        if not claimed:
            return False
        token = secrets.token_urlsafe(24)
        pending = {
            "admin": admin, "payload": payload, "source_id": source_id,
            "source_key": source_key,
            "expires": (now + timedelta(minutes=10)).isoformat(),
        }
        digest = hashlib.sha256(json.dumps(pending, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        key = "email-approval:" + digest
        try:
            self.db.execute(
                "DELETE FROM runtime_state WHERE key LIKE 'email-approval:%%' AND value->>'source_id' = %s",
                (source_id,),
            )
            rate_key = "admin-mail-rate:" + now.strftime("%Y%m%d%H%M")
            allowed = self.db.fetch_one(
                """INSERT INTO runtime_state(key, value) VALUES (%s, '{"count":1}'::jsonb)
                   ON CONFLICT (key) DO UPDATE
                   SET value = jsonb_build_object('count', (runtime_state.value->>'count')::int + 1)
                   WHERE (runtime_state.value->>'count')::int < 10 RETURNING key""", (rate_key,),
            )
            if not allowed:
                raise ValueError("Admin confirmation email rate limit reached; try again in one minute.")
            self.db.set_state(key, {**pending, "otp_hash": hashlib.sha256(token.encode()).hexdigest()})
            send_admin_email(
                self.config, "Assistant approval required",
                "%s\nOTP: %s\nRequest hash: %s\nExpires: %s\n\n"
                "Reply within 10 minutes, keeping this original message. A reply approves this request once.\n"
                "Text above this message is additional LLM guidance for a job; it cannot change an admin command.\n\n"
                "Requested action (untrusted content; review carefully):\n%s" % (
                    MARKER, token, digest, pending["expires"], summary[:20000],
                ),
            )
        except (OSError, smtplib.SMTPException, ValueError):
            self.db.execute("DELETE FROM runtime_state WHERE key IN (%s, %s)", (key, source_key))
            raise
        self.db.set_state(source_key, {"status": "sent"})
        return True

    def consume(self, token: str, digest: str) -> dict:
        admin = str(self.config.get("agent.admin.email") or "").strip().lower()
        row = self.db.fetch_one(
            """WITH consumed AS (
               DELETE FROM runtime_state WHERE key = %s AND value->>'admin' = %s
               AND value->>'otp_hash' = %s AND (value->>'expires')::timestamptz > now()
               RETURNING value
               ), marked AS (
                   UPDATE runtime_state SET value = '{"status":"approved"}'::jsonb, updated_at = now()
                   FROM consumed WHERE runtime_state.key = consumed.value->>'source_key'
                   RETURNING runtime_state.key
               ) SELECT value FROM consumed""",
            ("email-approval:" + digest, admin, hashlib.sha256(token.encode()).hexdigest()),
        )
        if not row:
            raise ValueError("Approval is expired, invalid, or already used. Send a fresh request.")
        pending = dict(row["value"])
        pending.pop("otp_hash")
        expected = hashlib.sha256(json.dumps(pending, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if not hmac.compare_digest(digest, expected):
            raise ValueError("Approval request content has changed. Send a fresh request.")
        return pending["payload"]
