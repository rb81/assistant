import hashlib
from typing import Any

from psycopg.types.json import Jsonb

from .telegram import TelegramDelivery, TelegramError, telegram_configured, telegram_destination, telegram_identities
from .validation import smtp_configured


def reminder_delivery(config: Any, job: dict[str, Any], channel: str = "auto") -> dict[str, Any]:
    if channel not in {"auto", "email", "telegram"}:
        raise ValueError("reminder notification_channel must be auto, email or telegram")
    route = (job.get("metadata") or {}).get("reply_route") or {}
    source_channel = route.get("channel") or (job.get("metadata") or {}).get("source_channel")
    if channel == "auto" and source_channel in {"email", "telegram"}:
        channel = source_channel
    address = str(route.get("to") or "") if route.get("channel") == "email" else ""
    if route.get("channel") == "telegram":
        address = next((email for email, identifier in telegram_identities(config).items()
                        if str(identifier) == str(route.get("chat_id"))), "")
    elif not address:
        address = str(config.get("agent.admin.email") or "")
    email_route = {"channel": "email", "to": address, "subject": "Reminder"} if address and smtp_configured(config) else {}
    telegram_route = {}
    if telegram_configured(config):
        try:
            telegram_route = {"channel": "telegram", "chat_id": telegram_destination(config, job)}
        except TelegramError:
            pass
    primary = telegram_route or email_route if channel == "auto" else telegram_route if channel == "telegram" else email_route
    if not primary and channel != "auto":
        raise ValueError("The requested reminder destination is not configured for this requester")
    return {"primary": primary, "fallback": email_route if channel == "auto" and primary.get("channel") == "telegram" else {}} if primary else {}


class ReplyRouter:
    adapters: dict[str, Any] = {}

    @classmethod
    def register(cls, channel: str, adapter: Any) -> None:
        if channel in {"email", "telegram"}:
            raise ValueError("built-in reply adapters cannot be replaced")
        cls.adapters[channel] = adapter

    def __init__(self, runtime: Any):
        self.runtime = runtime

    def email(self, route: dict[str, Any], body: str, purpose: str) -> dict[str, Any]:
        identity = "%s:%s:%s:%s:%s" % (self.runtime.job["id"], (self.runtime.job.get("metadata") or {}).get("reply_request_id"), route["to"], purpose, body)
        key = "routed-email:" + hashlib.sha256(identity.encode()).hexdigest()
        row = self.runtime.db.fetch_one(
            """INSERT INTO channel_deliveries(job_id, channel, destination, purpose, request_key, body_text)
               VALUES (%s, 'email', %s, %s, %s, %s) ON CONFLICT (request_key) DO NOTHING RETURNING *""",
            (self.runtime.job["id"], route["to"], purpose, key, body),
        )
        if row is None:
            row = self.runtime.db.fetch_one("SELECT * FROM channel_deliveries WHERE request_key = %s", (key,))
        if row["status"] == "sent":
            return {"status": "sent", "log_id": row["id"], "idempotent_reuse": True}
        if row["status"] in {"sending", "uncertain"}:
            return {"status": "uncertain", "log_id": row["id"]}
        claimed = self.runtime.db.fetch_one("UPDATE channel_deliveries SET status = 'sending' WHERE id = %s AND status IN ('pending', 'failed') RETURNING id", (row["id"],))
        if not claimed:
            return {"status": "pending"}
        try:
            result = self.runtime.email_send(to=[route["to"]], subject="Re: " + str(route.get("subject") or "Reply"),
                                             body=body, in_reply_to=route.get("in_reply_to"))
        except Exception:
            self.runtime.db.execute("UPDATE channel_deliveries SET status = 'uncertain', error = 'SMTP delivery requires reconciliation' WHERE id = %s", (row["id"],))
            return {"status": "uncertain", "log_id": row["id"]}
        state = "sent" if result.get("status") == "sent" else "failed"
        self.runtime.db.execute("UPDATE channel_deliveries SET status = %s, sent_at = CASE WHEN %s = 'sent' THEN now() ELSE NULL END WHERE id = %s", (state, state, row["id"]))
        return result

    def send(self, body: str, *, purpose: str = "final") -> dict[str, Any]:
        metadata = self.runtime.job.get("metadata") or {}
        if purpose == "final" and metadata.get("reply_route"):
            expected_request = metadata.get("reply_request_id")
            current = self.runtime.db.fetch_one("SELECT metadata FROM jobs WHERE id = %s", (self.runtime.job["id"],))
            metadata = current["metadata"] if current else metadata
            if metadata.get("reply_request_id") != expected_request:
                raise ValueError("New request context arrived; refresh before replying")
            self.runtime.job["metadata"] = metadata
            pending = metadata.get("pending_final_response")
            if pending:
                body = pending
            else:
                self.runtime.db.execute("UPDATE jobs SET metadata = metadata || %s WHERE id = %s",
                                        (Jsonb({"pending_final_response": body}), self.runtime.job["id"]))
        route = metadata.get("reply_route") or {}
        if route.get("channel") == "telegram":
            try:
                result = TelegramDelivery(self.runtime.db, self.runtime.config).send(
                    self.runtime.job, body, destination=str(route["chat_id"]),
                    purpose=purpose, reply_to=route.get("message_id"),
                )
            except TelegramError as exc:
                result = {"status": "uncertain" if exc.uncertain else "failed", "reason": str(exc)}
            fallback = metadata.get("reply_fallback") or {}
            if result.get("status") == "failed" and not result.get("retry_after") and fallback.get("channel") == "email":
                result = self.email(fallback, body, purpose)
            return {**result, "body": body}
        if route.get("channel") == "email":
            return {**self.email(route, body, purpose), "body": body}
        if route.get("channel") in self.adapters:
            return self.adapters[route["channel"]](self.runtime, route, body, purpose)
        if route.get("channel"):
            raise ValueError("No reply adapter is registered for this channel")
        return {"status": "local"}