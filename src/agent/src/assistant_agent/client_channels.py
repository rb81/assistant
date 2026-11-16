"""Durable, polling-based client delivery with explicit acknowledgment."""
import hashlib
import logging
from typing import Any

from psycopg.types.json import Jsonb

from .database import json_safe
from .requests import TransactionDatabase


LOGGER = logging.getLogger(__name__)


def delivery_accepted(result: dict[str, Any]) -> bool:
    return result.get("status") in {"sent", "queued", "local"}


class ClientChannels:
    def __init__(self, db, config):
        self.db = db
        self.config = config
        timings = (
            self.availability_timeout, self.delivery_timeout,
            config.get_int("agent.client.poll_interval_seconds", 15),
            config.get_int("agent.client.reconciliation_interval_seconds", 15),
            config.get_int("agent.client.retry_interval_seconds", 60),
            config.get_int("agent.client.claim_timeout_seconds", 3600),
        )
        if any(value < 1 for value in timings):
            raise ValueError("Client channel timing settings must be positive integers")

    @property
    def availability_timeout(self):
        return self.config.get_int("agent.client.availability_timeout_seconds", 60)

    @property
    def delivery_timeout(self):
        return self.config.get_int("agent.client.delivery_timeout_seconds", 60)

    def register(self, identifier, name, kind, fallback_channels):
        row = self.db.fetch_one(
            """INSERT INTO client_channels(id, name, kind, fallback_channels)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, kind = EXCLUDED.kind,
                 fallback_channels = EXCLUDED.fallback_channels, disabled = false
               RETURNING id, name, kind, fallback_channels, last_poll_at, disabled""",
            (identifier, name, kind, Jsonb(fallback_channels)),
        )
        if not row:
            raise ValueError("Client registration was not saved")
        return {"client": json_safe(row)}

    def client(self, identifier, *, allow_disabled=False):
        row = self.db.fetch_one(
            """SELECT id, name, kind, fallback_channels, last_poll_at, disabled,
                 COALESCE(NOT disabled AND last_poll_at > now() - %s * interval '1 second', false) AS available
               FROM client_channels WHERE id = %s""", (self.availability_timeout, identifier),
        )
        if not row or row["disabled"] and not allow_disabled:
            raise ValueError("Client channel is not registered or has been disabled")
        return row

    def list(self):
        return {"clients": json_safe(self.db.fetch_all(
            """SELECT id, name, kind, fallback_channels, last_poll_at, disabled,
                 COALESCE(NOT disabled AND last_poll_at > now() - %s * interval '1 second', false) AS available
               FROM client_channels ORDER BY created_at, id""", (self.availability_timeout,),
        ))}

    def unregister(self, identifier):
        row = self.db.fetch_one(
            "UPDATE client_channels SET disabled = true WHERE id = %s RETURNING id", (identifier,),
        )
        if not row:
            raise ValueError("Client channel is not registered")
        return {"client_id": identifier, "disabled": True}

    def poll(self, identifier):
        with self.db.connect() as connection:
            db = TransactionDatabase(connection)
            row = db.fetch_one(
                """UPDATE client_channels SET last_poll_at = now()
                   WHERE id = %s AND NOT disabled RETURNING id""", (identifier,),
            )
            if not row:
                raise ValueError("Client channel is not registered or has been disabled")
            rows = db.fetch_all(
                """WITH pending AS (
                     SELECT id FROM channel_deliveries
                     WHERE channel = 'client' AND destination = %s AND status = 'pending'
                     ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED
                   )
                   UPDATE channel_deliveries d SET offered_at = COALESCE(offered_at, now())
                   FROM pending WHERE d.id = pending.id
                   RETURNING d.id, d.job_id AS task_id, d.request_id, d.purpose,
                             d.body_text AS body, d.attachments, d.created_at""", (identifier,),
            )
        return {
            "messages": json_safe(sorted(rows, key=lambda item: item["id"])),
            "poll_interval_seconds": self.config.get_int("agent.client.poll_interval_seconds", 15),
            "availability_timeout_seconds": self.availability_timeout,
        }

    def acknowledge(self, identifier, message_ids):
        self.client(identifier)
        rows = self.db.fetch_all(
            """UPDATE channel_deliveries SET status = 'sent', sent_at = COALESCE(sent_at, now())
               WHERE channel = 'client' AND destination = %s AND id = ANY(%s)
                 AND offered_at IS NOT NULL AND status IN ('pending', 'sent')
               RETURNING id""", (identifier, message_ids),
        )
        return {"acknowledged": sorted(row["id"] for row in rows)}

    def history(self, identifier, before_id=None):
        self.client(identifier, allow_disabled=True)
        rows = self.db.fetch_all(
            """SELECT id, job_id AS task_id, request_id, purpose, body_text AS body,
                      attachments, created_at, status, fallback_channel, error, sent_at
               FROM channel_deliveries
               WHERE channel = 'client' AND destination = %s
                 AND (%s::bigint IS NULL OR id < %s)
               ORDER BY id DESC LIMIT 101""", (identifier, before_id, before_id),
        )
        return {"messages": json_safe(rows[:100]),
                "next_before_id": rows[99]["id"] if len(rows) > 100 else None}

    def resolve(self, identifier, delivery_id, resolution):
        self.client(identifier, allow_disabled=True)
        if resolution not in {"mark-sent", "return-to-client"}:
            raise ValueError("Invalid delivery resolution")
        state = "sent" if resolution == "mark-sent" else "pending"
        row = self.db.fetch_one(
            """UPDATE channel_deliveries SET status = %s, error = NULL,
                 offered_at = CASE WHEN %s = 'pending' THEN NULL ELSE offered_at END,
                 available_at = now(),
                 sent_at = CASE WHEN %s = 'sent' THEN now() ELSE NULL END
               WHERE channel = 'client' AND destination = %s AND id = %s AND status = 'uncertain'
               RETURNING id, status""",
            (state, state, state, identifier, delivery_id),
        )
        if not row:
            raise ValueError("Delivery is not awaiting resolution")
        return {"delivery": json_safe(row)}

    def send(self, runtime, route, body, purpose):
        client = self.client(route["client_id"], allow_disabled=True)
        metadata = runtime.job.get("metadata") or {}
        identity = f"{runtime.job['id']}:{metadata.get('reply_request_id')}:{purpose}:{body}"
        key = "client:" + hashlib.sha256(identity.encode()).hexdigest()
        row = self.db.fetch_one(
            """INSERT INTO channel_deliveries(
                 job_id, request_id, channel, destination, purpose, request_key, body_text
               ) VALUES (%s, %s, 'client', %s, %s, %s, %s)
               ON CONFLICT (request_key) DO UPDATE SET request_key = EXCLUDED.request_key
               RETURNING id, status""",
            (runtime.job["id"], metadata.get("reply_request_id"), client["id"], purpose, key, body),
        )
        if not row:
            raise ValueError("Client delivery was not persisted")
        if row["status"] in {"failed", "uncertain"}:
            return {"status": row["status"], "log_id": row["id"], "body": body}
        return {"status": "sent" if row["status"] == "sent" else "queued",
                "log_id": row["id"], "body": body}

    def notice(self, body, purpose, request_key, *, job_id=None):
        preferred = None
        if job_id:
            job = self.db.fetch_one("SELECT metadata FROM jobs WHERE id = %s", (job_id,))
            preferred = (((job or {}).get("metadata") or {}).get("reply_route") or {}).get("client_id")
        client = self.db.fetch_one(
            """SELECT id FROM client_channels WHERE id = %s OR NOT disabled
               ORDER BY (id = %s) DESC NULLS LAST, last_poll_at DESC NULLS LAST, created_at DESC LIMIT 1""",
            (preferred, preferred),
        )
        if not client:
            raise ValueError("No registered client can receive the notice; review pending approvals through the management API")
        row = self.db.fetch_one(
            """INSERT INTO channel_deliveries(job_id, channel, destination, purpose, request_key, body_text)
               VALUES (%s, 'client', %s, %s, %s, %s)
               ON CONFLICT (request_key) DO UPDATE SET request_key = EXCLUDED.request_key
               RETURNING id, status""", (job_id, client["id"], purpose, request_key, body),
        )
        if not row:
            raise ValueError("Client notice was not persisted")
        return {"status": "sent" if row["status"] == "sent" else "queued", "log_id": row["id"]}

    def reconcile(self, limit=1):
        """Claim before network I/O; an ambiguous external send is never retried."""
        from .replies import ReplyRouter
        from .telegram import TelegramDelivery, TelegramError, telegram_configured, telegram_destination
        from .tools import ToolRuntime
        from .validation import smtp_configured

        # A crash after claiming can have sent externally. Surface it for review.
        self.db.execute(
            """UPDATE channel_deliveries SET status = 'uncertain',
                 error = 'Client fallback was interrupted; reconcile external delivery before retrying'
               WHERE channel = 'client' AND status = 'sending'
                 AND available_at < now() - %s * interval '1 second'""",
            (self.config.get_int("agent.client.claim_timeout_seconds", 3600),),
        )
        rows = self.db.fetch_all(
            """WITH overdue AS (
                 SELECT d.id, c.fallback_channels FROM channel_deliveries d
                 JOIN client_channels c ON c.id = d.destination
                 WHERE d.channel = 'client' AND d.status = 'pending' AND d.available_at <= now()
                   AND (c.disabled OR c.last_poll_at IS NULL
                     OR c.last_poll_at <= now() - %s * interval '1 second'
                     OR d.offered_at <= now() - %s * interval '1 second')
                 ORDER BY d.id LIMIT %s FOR UPDATE OF d SKIP LOCKED
               )
               UPDATE channel_deliveries d SET status = 'sending', available_at = now()
               FROM overdue WHERE d.id = overdue.id
               RETURNING d.*, overdue.fallback_channels""",
            (self.availability_timeout, self.delivery_timeout, limit),
        )
        for row in rows:
            job = self.db.fetch_one("SELECT * FROM jobs WHERE id = %s", (row["job_id"],))
            if row["job_id"] is None:
                self._notice_fallback(row)
                continue
            if not job:
                self._finish(row["id"], {"status": "failed"}, "Originating task is unavailable")
                continue
            runtime = ToolRuntime(self.db, self.config, job)
            admin = str(self.config.get("agent.admin.email") or "").strip().lower()
            result = {"status": "failed"}
            selected = None
            try:
                for channel in row["fallback_channels"]:
                    if channel == "telegram" and telegram_configured(self.config):
                        try:
                            destination = telegram_destination(self.config, job, recipient=admin)
                            result = TelegramDelivery(self.db, self.config).send(
                                job, row["body_text"], destination=destination, purpose=row["purpose"],
                            )
                        except TelegramError as error:
                            result = {"status": "uncertain" if error.uncertain else "failed"}
                        if result.get("status") == "failed" and result.get("message_ids"):
                            result = {**result, "status": "uncertain"}
                        selected = channel
                    elif channel == "email" and admin and smtp_configured(self.config):
                        result = ReplyRouter(runtime).email(
                            {"channel": "email", "to": admin, "subject": f"Task #{job['id']}"},
                            row["body_text"], row["purpose"],
                        )
                        selected = channel
                    else:
                        continue
                    if result.get("status") in {"sent", "uncertain", "pending"}:
                        break
                state = result.get("status")
                if state == "pending":
                    result = {**result, "status": "uncertain"}
                self._finish(row["id"], result, None if state == "sent" else
                             "No fallback channel confirmed delivery; check channel configuration and delivery logs",
                             selected)
            except Exception:
                self._finish(row["id"], {"status": "uncertain"},
                             "Client fallback failed during delivery; reconcile external delivery before retrying",
                             selected)
                LOGGER.exception("Client fallback failed for delivery %s", row["id"])
        return {"processed": len(rows),
                "reconciliation_interval_seconds": self.config.get_int("agent.client.reconciliation_interval_seconds", 15)}

    def _notice_fallback(self, row):
        from .approval_channels import telegram_admins
        from .email_approvals import send_admin_email
        from .telegram import TelegramClient, TelegramError
        from .telegram_config import telegram_configured
        from .validation import smtp_configured
        result = {"status": "failed"}
        selected = None
        try:
            for channel in row["fallback_channels"]:
                if channel == "telegram" and telegram_configured(self.config):
                    identifiers = telegram_admins(self.config)
                    if not identifiers:
                        continue
                    selected = channel
                    text = row["body_text"]
                    parts = [text[offset:offset + 2000] for offset in range(0, len(text), 2000)] or [text]
                    sent_parts = 0
                    try:
                        client = TelegramClient(self.config)
                        for part in parts:
                            client.call("sendMessage", {"chat_id": identifiers[0], "text": part}, sending=True)
                            sent_parts += 1
                        result = {"status": "sent"}
                    except TelegramError as error:
                        result = {"status": "uncertain" if error.uncertain or sent_parts else "failed"}
                elif channel == "email" and self.config.get("agent.admin.email") and smtp_configured(self.config):
                    selected = channel
                    send_admin_email(self.config, "Assistant client notification", row["body_text"])
                    result = {"status": "sent"}
                else:
                    continue
                if result["status"] in {"sent", "uncertain"}:
                    break
        except Exception:
            result = {"status": "uncertain"}
            LOGGER.exception("Client notice fallback failed for delivery %s", row["id"])
        self._finish(row["id"], result, None if result["status"] == "sent" else
                     "No fallback confirmed client notice delivery; check configuration and delivery logs", selected)

    def _finish(self, identifier, result, error=None, channel=None):
        state = result.get("status")
        if state not in {"sent", "failed", "uncertain"}:
            raise ValueError("Invalid client fallback result")
        self.db.execute(
            """UPDATE channel_deliveries SET status = %s, error = %s, fallback_channel = %s,
                 available_at = now() + %s * interval '1 second',
                 sent_at = CASE WHEN %s = 'sent' THEN now() ELSE sent_at END
               WHERE id = %s AND status = 'sending'""",
            ("pending" if state == "failed" else state, error, channel,
             self.config.get_int("agent.client.retry_interval_seconds", 60), state, identifier),
        )
        if error:
            LOGGER.warning("Client delivery %s: %s", identifier, error)
