"""Durable, single-claim execution of approved inbound email."""
import base64
import logging
import secrets

from psycopg.types.json import Jsonb

from .requests import TransactionDatabase


LOGGER = logging.getLogger("assistant.approved_email_execution")
PREFIX = "approved-email-execution:"


def enqueue(db, digest, pending, instruction):
    """Persist execution intent in the same transaction as approval consumption."""
    key = PREFIX + digest
    value = {
        "status": "pending", "source_key": pending["source_key"],
        "payload": pending["payload"], "instruction": instruction,
        "review": pending["payload"].get("review"),
    }
    db.execute("INSERT INTO runtime_state(key, value) VALUES (%s, %s)", (key, Jsonb(value)))
    db.execute(
        """UPDATE runtime_state SET value = value || %s, updated_at = now()
           WHERE key = %s""",
        (Jsonb({"status": "execution_pending"}), pending["source_key"]),
    )
    return "Approval recorded. Email processing is queued."


def process_one(db, downloader):
    """Claim one queued email; never automatically replay an interrupted attempt."""
    claim = secrets.token_hex(16)
    row = db.fetch_one(
        """WITH candidate AS (
             SELECT key FROM runtime_state
             WHERE key LIKE %s AND value->>'status' = 'pending'
             ORDER BY updated_at, key FOR UPDATE SKIP LOCKED LIMIT 1
           )
           , claimed AS (
             UPDATE runtime_state AS state
             SET value = state.value || %s, updated_at = now()
             FROM candidate WHERE state.key = candidate.key
             RETURNING state.key, state.value
           ), marked AS (
             UPDATE runtime_state AS source SET value = source.value || %s, updated_at = now()
             FROM claimed WHERE source.key = claimed.value->>'source_key'
             RETURNING source.key
           ) SELECT key, value FROM claimed""",
        (PREFIX + "%", Jsonb({"status": "running", "claim": claim}),
         Jsonb({"status": "execution_running"})),
    )
    if not row:
        return False
    key, value = row["key"], row["value"]
    payload = value["payload"]
    try:
        current = None
        if payload["kind"] == "email":
            current = db.fetch_one(
                """SELECT * FROM jobs WHERE thread_id = %s
                   AND status IN ('queued', 'running', 'waiting', 'needs_review')""",
                (payload["thread_id"],),
            )
            snapshot = {
                "id": current["id"],
                "review_version": str(current["updated_at"]) if current["status"] == "needs_review" else None,
            } if current else None
            if snapshot != payload["job"]:
                _finish(db, key, claim, value, "failed", "target_changed")
                return True
        result = downloader.ingest_raw(
            base64.b64decode(payload["raw"], validate=True), payload["folder"],
            approved_thread_id=payload["thread_id"],
            approved_instruction=value["instruction"] if payload["kind"] == "email" else None,
            security_approved=value["instruction"] if payload["kind"] == "screened_email" else None,
            approved_review=current if current and current["status"] == "needs_review" else None,
        )
    except Exception as exc:
        LOGGER.error("Approved email processing encountered %s; reconcile before retrying", type(exc).__name__)
        _finish(db, key, claim, value, "uncertain", type(exc).__name__)
    else:
        if not result.inserted:
            _finish(db, key, claim, value, "uncertain", "email_already_present_or_rejected")
        elif not db.fetch_one("SELECT id FROM emails WHERE message_id = %s", (result.message_id,)):
            _finish(db, key, claim, value, "uncertain", "email_record_missing")
        else:
            _finish(db, key, claim, value, "succeeded", None, message_id=result.message_id)
    return True


def _finish(db, key, claim, value, status, error, *, message_id=None):
    result = {"status": status, "claim": claim, "source_key": value["source_key"],
              "review": value.get("review")}
    if error:
        result["error"] = error
    if message_id:
        result["message_id"] = message_id
    if status != "succeeded":
        result["payload"] = value["payload"]
        result["instruction"] = value["instruction"]
    with db.connect() as connection:
        tx = TransactionDatabase(connection)
        updated = tx.fetch_one(
            """UPDATE runtime_state SET value = %s, updated_at = now()
               WHERE key = %s AND value->>'status' IN ('running', 'uncertain')
                 AND value->>'claim' = %s RETURNING key""",
            (Jsonb(result), key, claim),
        )
        if not updated:
            raise RuntimeError("Approved email execution claim was lost")
        tx.execute(
            """UPDATE runtime_state SET value = value || %s, updated_at = now()
               WHERE key = %s""",
            (Jsonb({"status": "execution_" + status}), value["source_key"]),
        )


def mark_interrupted(db):
    """Surface long-running claims as uncertain without scheduling another attempt."""
    rows = db.fetch_all(
        """WITH interrupted AS (
             UPDATE runtime_state SET value = value || %s, updated_at = now()
             WHERE key LIKE %s AND value->>'status' = 'running'
               AND updated_at < now() - interval '30 minutes'
             RETURNING key, value
           ), marked AS (
             UPDATE runtime_state AS source SET value = source.value || %s, updated_at = now()
             FROM interrupted WHERE source.key = interrupted.value->>'source_key'
             RETURNING source.key
           ) SELECT key FROM interrupted""",
        (Jsonb({"status": "uncertain", "error": "execution_interrupted_or_still_running"}),
         PREFIX + "%", Jsonb({"status": "execution_uncertain"})),
    )
    if rows:
        LOGGER.error("%s approved email execution(s) need reconciliation", len(rows))
