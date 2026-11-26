"""Identity and decision rules for message-scoped quarantine review."""
import base64
import email
import hashlib
import re
from contextlib import contextmanager
from email.utils import getaddresses


def sender(message):
    headers = message.get_all("From", [])
    addresses = getaddresses(headers)
    if len(headers) != 1 or len(addresses) != 1:
        return None
    address = addresses[0][1].strip().lower()
    if len(address) > 320 or not address.isprintable() or not re.fullmatch(r"[^@\s<>]+@[^@\s<>]+", address):
        return None
    return address


def blacklist_key(address):
    return "quarantine-blacklist:" + hashlib.sha256(address.encode()).hexdigest()


def lock_key(address):
    return "quarantine-sender:" + address


@contextmanager
def sender_lock(db, address):
    if not address:
        yield
        return
    with db.connect() as conn:
        conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock_key(address),))
        try:
            yield
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (lock_key(address),))


def blocked(db, address):
    if not address:
        return False
    row = db.fetch_one("SELECT value FROM runtime_state WHERE key = %s", (blacklist_key(address),))
    return isinstance(row, dict) and isinstance(row.get("value"), dict) and row["value"].get("address") == address


def payload_sender(payload):
    raw = base64.b64decode(payload["raw"], validate=True)
    return sender(email.message_from_bytes(raw))


def blacklist_eligible(payload, config):
    address = payload_sender(payload)
    admin = str(config.get("agent.admin.email") or "").strip().lower()
    return bool(address and address != admin and (not payload.get("sender") or payload["sender"] == address))


def blacklist(db, config, payload):
    address = payload_sender(payload)
    if not blacklist_eligible(payload, config):
        raise ValueError("This message has no eligible sender to blacklist.")
    db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key(address),))
    db.set_state(blacklist_key(address), {"address": address})


def decision_from_email(instruction):
    lines = str(instruction or "").strip().splitlines()
    options = {"APPROVE": "approve", "REJECT": "deny", "BLACKLIST": "blacklist"}
    decision = options.get(lines[0].strip().upper()) if lines else None
    if decision is None or any(line.strip() for line in lines[1:]):
        raise ValueError("Put only APPROVE, REJECT or BLACKLIST on the first line and quote the original confirmation.")
    return decision


def privileged_message(message, config):
    """Keep control mail and administrator impersonation out of the release path."""
    from .admin_commands import is_admin_subject
    from .email_approvals import HASH_PATTERN, MARKER, TOKEN_PATTERN, _reply_bodies
    from .email_ingest import decode_mime_header

    admin = str(config.get("agent.admin.email") or "").strip().lower()
    addresses = {address.strip().lower() for _, address in getaddresses(message.get_all("From", []))}
    plain, html = _reply_bodies(message)
    headers = [decode_mime_header(str(value)) or "" for _, value in message.items()]
    body = "\n".join(headers + plain + html)
    return bool(
        (admin and admin in addresses) or is_admin_subject(decode_mime_header(message.get("Subject")) or "")
        or MARKER in body or TOKEN_PATTERN.search(body) or HASH_PATTERN.search(body)
    )
