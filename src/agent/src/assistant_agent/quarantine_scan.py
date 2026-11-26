"""Bounded, durable discovery of messages in the IMAP quarantine."""
import base64
import email
import hashlib
import imaplib
import logging
import ssl
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from .approval_channels import channels, telegram_admins
from .config import message_id_domain
from .database import json_safe
from .email_approvals import EmailApprovals, send_admin_email
from .imap_utils import imap_mailbox_arg, imap_status_ok
from .quarantine import blocked, privileged_message, sender, sender_lock
from .requests import TransactionDatabase
from .security_screening import MALICIOUS, SecurityScreener, inbound_email_state, screening_excerpt


LOGGER = logging.getLogger("assistant.quarantine_scan")
BATCH_SIZE = 5


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _malicious_notice(db, config, key, message, identity, screening):
    from .email_ingest import decode_mime_header
    details = []
    for header in ("From", "Subject"):
        value = " ".join((decode_mime_header(message.get(header)) or "Not provided").split())
        details.append("%s (untrusted header): %s" % (header, screening_excerpt(value, 256)))
    _notice(db, config, key, "Email remains quarantined after security screening",
            "A quarantined message did not pass security screening. It was not released.\n"
            + "\n".join(details) + "\nQuarantine ID: " + identity + "\nScreening: " + screening_excerpt(screening))


def _notice(db, config, key, subject, body):
    """Claim a notice before external delivery; ambiguous delivery is not repeated."""
    if db.get_state(key):
        return
    rate_key = "quarantine-notice-rate:" + datetime.now(timezone.utc).strftime("%Y%m%d%H")
    allowed = db.fetch_one(
        """INSERT INTO runtime_state(key,value) VALUES (%s, '{"count":1}'::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = jsonb_build_object('count', (runtime_state.value->>'count')::int + 1)
           WHERE (runtime_state.value->>'count')::int < %s RETURNING key""",
        (rate_key, max(1, config.get_int("agent.security.notify_max_per_hour", 20))),
    )
    if not allowed:
        raise RuntimeError("Quarantine security notice rate limit reached")
    claim = db.fetch_one(
        """INSERT INTO runtime_state(key, value) VALUES (%s, '{"status":"sending"}'::jsonb)
           ON CONFLICT DO NOTHING RETURNING key""", (key,),
    )
    if not claim:
        return
    try:
        mode = channels(config)
        if mode == "client":
            from .client_channels import ClientChannels
            ClientChannels(db, config).notice(subject + "\n\n" + body, "security", key)
        if mode in {"telegram", "both"}:
            from .telegram import TelegramClient
            try:
                client = TelegramClient(config)
                admins = telegram_admins(config)
                if not admins:
                    raise ValueError("No verified Telegram administrator is configured")
                for identifier in admins:
                    client.call("sendMessage", {"chat_id": identifier, "text": subject + "\n\n" + body}, sending=True)
            except Exception:
                from .validation import smtp_configured
                if not smtp_configured(config):
                    raise
                LOGGER.warning("Telegram quarantine notice failed or is uncertain; attempting email fallback")
                send_admin_email(config, subject, body)
                if mode == "both":
                    mode = "telegram"
        if mode in {"email", "both"}:
            send_admin_email(config, subject, body)
    except Exception as exc:
        db.set_state(key, {"status": "delivery_uncertain"})
        LOGGER.error("quarantine notice delivery failed or is uncertain (%s); inspect the quarantine mailbox",
                     type(exc).__name__)
        return
    db.set_state(key, {"status": "sent"})


def review_message(db, config, raw, folder):
    """Review one message without trusting headers as sender authentication."""
    from .email_ingest import message_id_for

    message = email.message_from_bytes(raw)
    address = sender(message)
    identity = _digest(raw)
    notice_key = "quarantine-notice:" + identity
    prior_notice = db.get_state(notice_key)
    if prior_notice:
        if prior_notice.get("status") != "sent":
            LOGGER.warning("Quarantine notice delivery is uncertain; inspect the quarantine mailbox")
        return
    if privileged_message(message, config) or not address:
        _notice(db, config, notice_key, "Quarantined email requires manual inspection",
                "A message with administrative or ambiguous sender details remains quarantined. "
                "Inspect it directly in the quarantine mailbox; it cannot be released through this review.")
        return
    with sender_lock(db, address):
        if blocked(db, address):
            return
        message_id = json_safe(message_id_for(raw, message, message_id_domain(config)))
        source_id = "quarantine:" + identity
        source_key = "admin-request:" + _digest(source_id.encode())
        source = db.get_state(source_key)
        if source and source.get("status") != "sending":
            return
        if db.fetch_one("SELECT id FROM emails WHERE message_id = %s", (message_id,)):
            _notice(db, config, notice_key, "Quarantined email requires manual inspection",
                    "A message has an existing or ambiguous message ID. It remains quarantined; "
                    "inspect it directly in the mailbox.")
            return
        old_source = "admin-request:" + _digest(message_id.encode())
        old = db.get_state(old_source)
        if old:
            previous = db.fetch_one(
                """SELECT value FROM runtime_state WHERE key LIKE 'email-approval:%%'
                   AND value->>'source_id' = %s""", (message_id,),
            )
            previous_payload = (previous or {}).get("value", {}).get("payload", {})
            if previous_payload.get("raw") == base64.b64encode(raw).decode("ascii"):
                return
            _notice(db, config, notice_key, "Quarantined email requires manual inspection",
                    "A message has an existing or ambiguous review reference. It remains quarantined; "
                    "inspect it directly in the mailbox.")
            return
        if source and db.fetch_one(
            "SELECT key FROM runtime_state WHERE key LIKE 'email-approval:%%' AND value->>'source_id' = %s",
            (source_id,),
        ):
            db.set_state(source_key, {"status": "delivery_uncertain"})
            LOGGER.warning("Quarantine approval delivery was interrupted; the existing approval was preserved")
            return
        malicious_key = "quarantine-malicious:" + identity
        prior_verdict = db.get_state(malicious_key)
        if prior_verdict:
            _malicious_notice(db, config, notice_key, message, identity, prior_verdict["screening"])
            return
        screener = SecurityScreener(config)
        screening = "Security screening is disabled."
        if screener.enabled("inbound_email"):
            result = screener.screen("inbound_email", inbound_email_state(message))
            screening = result.summary()
            if result.level == MALICIOUS:
                db.set_state(malicious_key, {"screening": screening})
                _malicious_notice(db, config, notice_key, message, identity, screening)
                return
        payload = {
            "kind": "quarantined_email", "raw": base64.b64encode(raw).decode("ascii"),
            "folder": folder, "thread_id": "quarantine:" + identity, "sender": address, "screening": screening,
            "quarantine_id": identity,
        }
        requested = EmailApprovals(db, config).request(
            source_id, payload, "Quarantined email requires review.\nScreening: " + screening,
            ttl_minutes=config.get_int("agent.security.decision_model.approval_ttl_minutes", 1440),
        )
        if not requested and (db.get_state(source_key) or {}).get("status") == "sending":
            raise RuntimeError("Quarantine approval is still being prepared")


def scan(db, config):
    host = config.get("agent.email.imap_host")
    username = config.get("agent.email.imap_username")
    password = config.get("agent.email.imap_password")
    if not host or host == "imap.example.com" or not username or not password:
        return
    folder = config.get("agent.email.imap_quarantine_folder") or "Quarantine"
    if folder.strip().lower() == str(config.get("agent.email.imap_folder", "INBOX")).strip().lower():
        raise ValueError("Quarantine mailbox must not be the inbound mailbox.")
    scope = _digest(("\0".join((str(host), str(username), str(folder)))).encode())
    cursor_key = "quarantine-cursor:" + scope
    with db.connect() as lock:
        row = lock.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked",
                           (cursor_key,)).fetchone()
        if not row["locked"]:
            return
        try:
            with imaplib.IMAP4_SSL(
                host, config.get_int("agent.email.imap_port", 993), ssl_context=ssl.create_default_context(),
                timeout=config.get_int("agent.email.imap_timeout_seconds", 60),
            ) as mailbox:
                mailbox.login(username, password)
                status, _ = mailbox.select(imap_mailbox_arg(folder), readonly=True)
                if not imap_status_ok(status):
                    mailbox.create(imap_mailbox_arg(folder))
                    status, _ = mailbox.select(imap_mailbox_arg(folder), readonly=True)
                if not imap_status_ok(status):
                    raise RuntimeError("Cannot select quarantine mailbox.")
                _, values = mailbox.response("UIDVALIDITY")
                if not values or not values[0]:
                    raise RuntimeError("Quarantine mailbox did not supply UIDVALIDITY.")
                validity = values[0].decode("ascii")
                if not validity.isdecimal() or not 1 <= int(validity) <= 0xFFFFFFFF:
                    raise RuntimeError("Quarantine mailbox supplied an invalid UIDVALIDITY.")
                cursor = db.get_state(cursor_key) or {}
                if cursor and cursor.get("uidvalidity") != validity:
                    LOGGER.warning("Quarantine UIDVALIDITY changed; scanning the folder again.")
                last_uid = int(cursor.get("last_uid", 0)) if cursor.get("uidvalidity") == validity else 0
                prefix = "quarantine-item:" + scope + ":" + validity + ":"
                status, result = mailbox.uid("search", None, "UID", "%s:*" % (last_uid + 1))
                if not imap_status_ok(status):
                    raise RuntimeError("Cannot search quarantine mailbox.")
                uids = sorted(int(uid) for uid in (result[0].split() if result and result[0] else []))
                for uid in [uid for uid in uids if uid > last_uid][:BATCH_SIZE]:
                    with db.connect() as connection:
                        tx = TransactionDatabase(connection)
                        tx.execute(
                            """INSERT INTO runtime_state(key,value) VALUES (%s,%s)
                               ON CONFLICT (key) DO NOTHING""",
                            (prefix + str(uid), Jsonb({"uid": uid, "status": "pending"})),
                        )
                        tx.set_state(cursor_key, {"uidvalidity": validity, "last_uid": uid})
                pending = db.fetch_all(
                    """SELECT key,value FROM runtime_state
                       WHERE key LIKE %s AND value->>'status' = 'pending'
                         AND COALESCE((value->>'retry_at')::timestamptz, now()) <= now()
                       ORDER BY updated_at, key LIMIT %s""", (prefix + "%", BATCH_SIZE),
                )
                for item in pending:
                    uid = str(item["value"]["uid"]).encode("ascii")
                    try:
                        status, data = mailbox.uid("fetch", uid, "(BODY.PEEK[])")
                        raw = next((part[1] for part in data or [] if isinstance(part, tuple)), None)
                        if not imap_status_ok(status) or raw is None:
                            raise RuntimeError("Quarantined message is no longer available")
                        review_message(db, config, raw, folder)
                    except Exception as exc:
                        LOGGER.exception("Could not review quarantined IMAP UID %s", uid.decode())
                        db.set_state(item["key"], {
                            "uid": int(uid), "status": "pending", "error": type(exc).__name__,
                            "retry_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
                        })
                    else:
                        db.execute("DELETE FROM runtime_state WHERE key = %s", (item["key"],))
        finally:
            lock.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (cursor_key,))
