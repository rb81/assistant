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
from .approval_channels import channels, telegram_admins


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


def _reply_bodies(message: Message) -> tuple[list[str], list[str]]:
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
    return plain, html


def reply_text(message: Message) -> str:
    plain, html = _reply_bodies(message)
    return "\n".join(plain or html)


def approval_reply(message: Message) -> tuple[str, str, str] | None:
    plain, html = _reply_bodies(message)
    credential_text = "\n".join(plain + html)
    tokens = set(TOKEN_PATTERN.findall(credential_text))
    hashes = set(HASH_PATTERN.findall(credential_text))
    if MARKER not in credential_text and not tokens and not hashes:
        return None
    if len(tokens) != 1 or len(hashes) != 1:
        raise ValueError("Keep exactly one original approval message, including its OTP and request hash.")
    instructions = []
    lines = "\n".join(plain or html).splitlines()
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


def send_admin_email(config, subject: str, body: str, *, attachment: tuple[str, bytes] | None = None) -> None:
    message = EmailMessage()
    message["From"] = agent_email(config)
    message["To"] = config.get("agent.admin.email")
    message["Subject"] = subject
    message["Message-ID"] = make_msgid()
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(body)
    if attachment:
        filename, data = attachment
        maintype, subtype = ("text", "plain") if filename.lower().endswith(".txt") else ("application", "json")
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    host = config.get("agent.email.smtp_host")
    port = config.get_int("agent.email.smtp_port", 587)
    context = ssl.create_default_context()
    client = smtplib.SMTP_SSL(host, port, context=context, timeout=30) if port == 465 else smtplib.SMTP(host, port, timeout=30)
    with client:
        if port != 465:
            client.starttls(context=context)
        client.login(config.get("agent.email.smtp_username"), config.get("agent.email.smtp_password"))
        client.send_message(message)


NETWORK_EMAIL_INSTRUCTIONS = (
    "Legacy website requests cannot grant access. Request fresh combined script and host approval to allow access.\n"
    "Reply with this original message quoted. Put your decision on the first line:\n"
    "  BLOCK ONCE | BLOCK ALWAYS | CANCEL JOB\n"
    "Optional feedback for the agent can follow on the next lines."
)


def permission_email_body(payload, summary, token, digest, expires, ttl_minutes, email_body_details=None):
    permission_type = payload.get("permission_type")
    if permission_type == "development":
        choices = ["APPROVE ONCE - approve development session"]
    elif permission_type == "final":
        choices = ["APPROVE ONCE - one execution with the listed hosts"]
        if payload.get("reusable"):
            choices.append("APPROVE ALWAYS - repeat runs of this unchanged version with the listed hosts")
    else:
        choices = ["APPROVE ONCE - one execution"]
        if payload.get("reusable"):
            choices.append("APPROVE ALWAYS - this unchanged script version")
    choices.append("DENY")
    return (
        "%s\n\n%s\n\nYOUR DECISION\n"
        "Reply with one of these on the first line:\n%s\n\n"
        "Optional feedback can follow on the next lines.\n"
        "Keep this original message quoted in your reply.\n"
        "Expires in %s minutes.\n\n"
        "%s"
        "APPROVAL REFERENCE (keep in your reply)\n"
        "OTP: %s\nRequest hash: %s\nExpires: %s"
    ) % (MARKER, summary.split("\n\nDetails:", 1)[0].replace("See Details", "See attachment"), "\n".join(choices),
         ttl_minutes,
         ("\nSCRIPT AND VERSION DETAILS\n\n%s\n\n" % email_body_details if email_body_details else
          "Full code and hashes are in the attached version manifest.\n\n"),
         token, digest, expires)


def permission_keyboard(callback: str, payload: dict) -> list:
    permission_type = payload.get("permission_type")
    once_label = "Approve development session" if permission_type == "development" else (
        "Approve one run" if permission_type == "final" else "Approve once")
    rows = [[{"text": once_label, "callback_data": "ap:%s:once" % callback},
             {"text": "Deny", "callback_data": "ap:%s:deny" % callback}]]
    if permission_type != "development" and payload.get("reusable"):
        always_label = "Always allow this version" if permission_type == "final" else "Approve always"
        rows.append([{"text": always_label, "callback_data": "ap:%s:always" % callback}])
    rows.append([{"text": "View script & details", "callback_data": "ap:%s:details" % callback},
                 {"text": "Add feedback", "callback_data": "ap:%s:feedback" % callback}])
    return rows


def telegram_permission_summary(summary: str, max_units: int = 3500) -> str:
    card = str(summary).split("\n\nDetails:", 1)[0]
    lines = card.splitlines()
    host_start = next((index for index, line in enumerate(lines)
                       if line.startswith("HTTPS hosts (") or line == "Hosts:"), None)

    def units(text):
        return len(text.encode("utf-16-le")) // 2

    def take(text, budget, *, from_end=False):
        result = []
        used = 0
        characters = reversed(text) if from_end else iter(text)
        for character in characters:
            size = units(character)
            if used + size > budget:
                break
            result.append(character)
            used += size
        if from_end:
            result.reverse()
        return "".join(result)

    def shorten(text, budget, *, middle=False):
        if units(text) <= budget:
            return text
        marker = "…"
        remaining = max(0, budget - units(marker))
        if middle:
            first = take(text, remaining // 2)
            last = take(text, remaining - units(first), from_end=True)
            return first + marker + last
        return marker + take(text, remaining, from_end=True)

    if host_start is None:
        return shorten(card, max_units, middle=True)

    host_end = host_start + 1
    while host_end < len(lines) and lines[host_end].startswith("- "):
        host_end += 1
    host_block = "\n".join(lines[host_start:host_end])
    prefix = "\n".join(lines[:host_start]).strip()
    suffix = "\n".join(lines[host_end:]).strip()
    if units(card) <= max_units:
        return card

    notice = "Brief/comments shortened; use View script & details for full context."
    overhead = units(host_block) + units("\n\n") * 2 + units(notice) + units("\n\n") + 2
    remaining = max(0, max_units - overhead)
    suffix_budget = min(units(suffix), min(350, remaining // 3))
    prefix_budget = max(0, remaining - suffix_budget)
    prefix_preview = shorten(prefix, prefix_budget, middle=True)
    suffix_preview = shorten(suffix, suffix_budget)
    parts = [part for part in (prefix_preview, host_block, suffix_preview) if part]
    return "\n\n".join(parts) + "\n\n" + notice


def network_keyboard(callback: str, payload: dict) -> list:
    return [
        [{"text": "Block once", "callback_data": "ap:%s:block" % callback},
         {"text": "Block always", "callback_data": "ap:%s:blockall" % callback}],
        [{"text": "Cancel job", "callback_data": "ap:%s:cancel" % callback},
         {"text": "Add feedback first", "callback_data": "ui:guidance:%s" % payload["job_id"]}],
        [{"text": "Details", "callback_data": "ap:%s:details" % callback},
         {"text": "Not now", "callback_data": "ap:%s:later" % callback}],
    ]


class ApprovalDeliveryError(RuntimeError):
    """A stored approval may be available; delivery must not be blindly repeated."""


class EmailApprovals:
    def __init__(self, db, config):
        self.db = db
        self.config = config

    def request(self, source_id: str, payload: dict, summary: str, ttl_minutes: int = 10,
                *, email_subject: str = "Assistant approval required",
                details_attachment: tuple[str, bytes] | None = None,
                email_body_details: str | None = None) -> bool:
        if (payload.get("kind") != "permission" and email_body_details is None
                and details_attachment and details_attachment[0].lower().endswith(".txt")):
            email_body_details = details_attachment[1].decode("utf-8", errors="replace")
        admin = str(self.config.get("agent.admin.email") or "").strip().lower()
        mode = channels(self.config)
        admins = telegram_admins(self.config) if mode in {"telegram", "both"} else []
        if mode in {"email", "both"} and not admin:
            raise ValueError("Admin email is not configured.")
        if mode in {"telegram", "both"} and not admins:
            raise ValueError("Verified Telegram administrators are not configured.")
        now = datetime.now(timezone.utc)
        self.db.execute(
            "DELETE FROM runtime_state WHERE key LIKE 'email-approval:%%' AND (value->>'expires')::timestamptz < now() - interval '30 days'"
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
            "expires": (now + timedelta(minutes=max(1, int(ttl_minutes)))).isoformat(),
        }
        if mode != "email":
            pending["telegram_admin_ids"] = admins
            pending["channel"] = mode
            pending["callback_id"] = secrets.token_hex(12)
            pending["summary"] = summary[:20000]
        digest = hashlib.sha256(json.dumps(pending, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        key = "email-approval:" + digest
        prepared = False
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
            prepared = True
            delivery_errors = []
            if mode in {"telegram", "both"}:
                from .telegram import TelegramClient
                try:
                    client = TelegramClient(self.config)
                    callback = pending["callback_id"]
                    title = (telegram_permission_summary(summary) if payload.get("kind") == "permission"
                             else summary.split("\n\nDetails:", 1)[0][:3700])
                    if payload.get("kind") == "network":
                        title = ("Legacy website requests cannot grant access. Request fresh combined script and host "
                                 "approval to allow access.\n\n" + title[:3500])
                    keyboard = network_keyboard(callback, payload) if payload.get("kind") == "network" else None
                    if payload.get("kind") == "permission":
                        keyboard = permission_keyboard(callback, payload)
                    for identifier in admins:
                        client.call("sendMessage", {
                            "chat_id": identifier, "text": title + "\n\nChoose below. Expires in %s minutes." % ttl_minutes,
                            "reply_markup": {"inline_keyboard": keyboard or [
                                [{"text": "Confirm & start" if payload.get("kind") == "email" else
                                          "Review reply" if payload.get("kind") == "input" else "Review & approve",
                                  "callback_data": "ap:%s:%s" % (
                                      callback, "ok" if payload.get("kind") == "email" else "review")},
                                 {"text": "Not now", "callback_data": "ap:%s:later" % callback}],
                                [{"text": "Details", "callback_data": "ap:%s:details" % callback}],
                            ] + ([[{"text": "Add reply", "callback_data": "ui:guidance:%s" % payload["job_id"]}]]
                                 if payload.get("kind") == "input" else [])},
                        }, sending=True)
                except Exception as exc:
                    delivery_errors.append(("Telegram", exc))
            if mode in {"email", "both"}:
                try:
                    body = (
                        permission_email_body(payload, summary, token, digest, pending["expires"], ttl_minutes,
                                              email_body_details)
                        if payload.get("kind") == "permission" else
                        "%s\nOTP: %s\nRequest hash: %s\nExpires: %s\n\n%s\n\n%s" % (
                            MARKER, token, digest, pending["expires"],
                            NETWORK_EMAIL_INSTRUCTIONS if payload.get("kind") == "network" else
                            "Reply with this original message to confirm once. Add guidance above it if needed.\n"
                            "Review the requested action below before confirming.",
                            summary[:20000],
                        )
                    )
                    send_admin_email(
                        self.config, email_subject, body,
                        **({"attachment": details_attachment} if details_attachment else {}),
                    )
                except Exception as exc:
                    delivery_errors.append(("email", exc))
            if delivery_errors:
                raise ApprovalDeliveryError(
                    "Approval delivery failed or is uncertain for %s; other selected channels were attempted. "
                    "The same one-use approval is preserved; do not resend automatically."
                    % ", ".join(name for name, _ in delivery_errors)
                ) from delivery_errors[0][1]
        except Exception:
            if prepared:
                # Delivery may have succeeded before a timeout; keep the same one-use record.
                self.db.set_state(source_key, {"status": "delivery_uncertain"})
            else:
                self.db.execute("DELETE FROM runtime_state WHERE key IN (%s, %s)", (key, source_key))
            raise
        self.db.set_state(source_key, {"status": "sent"})
        return True

    def consume(self, token: str, digest: str) -> dict:
        if channels(self.config) == "telegram":
            raise ValueError("Approvals are configured for Telegram only")
        return self._consume(digest, hashlib.sha256(token.encode()).hexdigest())

    def consume_telegram(self, callback_id: str, sender: str) -> dict:
        if channels(self.config) not in {"telegram", "both"} or sender not in telegram_admins(self.config):
            raise ValueError("Administrator permission is required")
        row = self.telegram_pending(callback_id, sender)
        return self._consume(row["key"].removeprefix("email-approval:"), row["value"]["otp_hash"])

    def telegram_pending(self, callback_id: str, sender: str) -> dict:
        if channels(self.config) not in {"telegram", "both"} or sender not in telegram_admins(self.config):
            raise ValueError("Administrator permission is required")
        if not re.fullmatch(r"[a-f0-9]{24}", callback_id):
            raise ValueError("Invalid approval reference")
        row = self.db.fetch_one(
            "SELECT key, value FROM runtime_state WHERE key LIKE 'email-approval:%%' AND value->>'callback_id' = %s",
            (callback_id,),
        )
        if not row or sender not in row["value"].get("telegram_admin_ids", []):
            raise ValueError("This approval was already handled or is no longer available")
        return row

    def _consume(self, digest: str, otp_hash: str) -> dict:
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
            ("email-approval:" + digest, admin, otp_hash),
        )
        if not row:
            raise ValueError("Approval is expired, invalid, or already used. Send a fresh request.")
        pending = dict(row["value"])
        pending.pop("otp_hash")
        expected = hashlib.sha256(json.dumps(pending, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if not hmac.compare_digest(digest, expected):
            raise ValueError("Approval request content has changed. Send a fresh request.")
        return pending["payload"]
