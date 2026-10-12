import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from psycopg.types.json import Jsonb

from .config import AppConfig
from .database import Database
from .requests import TransactionDatabase


class TelegramError(ValueError):
    def __init__(self, message: str, *, uncertain: bool = False, retry_after: int = 0):
        super().__init__(message)
        self.uncertain = uncertain
        self.retry_after = retry_after


class TelegramURLFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = re.sub(r"(/(?:file/)?bot)[^/\s]+", r"\1[REDACTED]", record.getMessage())
        record.args = ()
        return True


logging.getLogger("httpx").addFilter(TelegramURLFilter())


def telegram_configured(config: AppConfig) -> bool:
    identifiers = config.get_list("agent.telegram.allowed_user_ids")
    return bool(config.get_bool("agent.telegram.enabled", False) and config.get("agent.telegram.bot_token")
                and identifiers and all(identifier.isdigit() and int(identifier) > 0 for identifier in identifiers))


def telegram_identities(config: AppConfig) -> dict[str, Any]:
    value = config.get("agent.telegram.recipients", {}) or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise TelegramError("Telegram recipient mappings must be a JSON object") from None
    if not isinstance(value, dict):
        raise TelegramError("Telegram recipient mappings must be an object")
    return {str(address).lower(): identifier for address, identifier in value.items()}


def telegram_destination(config: AppConfig, job: dict[str, Any], recipient: str = "me") -> str:
    metadata = job.get("metadata") or {}
    route = metadata.get("reply_route") or {}
    identities = telegram_identities(config)
    if recipient == "me":
        if route.get("channel") == "telegram":
            destination = str(route.get("chat_id") or "")
        else:
            address = str(route.get("to") or "").lower()
            if not address and metadata.get("source_channel") in (None, "local", "reminder"):
                address = str(config.get("agent.admin.email") or "").lower()
            destination = str(identities.get(address) or "")
    else:
        destination = str(identities.get(recipient.lower()) or "")
    if destination not in config.get_list("agent.telegram.allowed_user_ids"):
        raise TelegramError("No authorized Telegram identity is linked to that recipient")
    return destination


class TelegramClient:
    def __init__(self, config: AppConfig):
        if not telegram_configured(config):
            raise TelegramError("Telegram is disabled or missing token/authorized users")
        self.config = config
        self.base_url = "https://api.telegram.org/bot%s" % config.get("agent.telegram.bot_token")

    def call(self, method: str, payload: dict[str, Any], *, files: Any = None,
             timeout: float = 45, sending: bool = False) -> Any:
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                response = client.post(self.base_url + "/" + method,
                                       data=payload if files else None, files=files,
                                       json=None if files else payload)
            result = response.json()
        except (httpx.HTTPError, ValueError):
            raise TelegramError("Telegram transport failed; delivery may be uncertain" if sending else
                                "Telegram transport failed", uncertain=sending) from None
        if not isinstance(result, dict) or not result.get("ok"):
            code = int(result.get("error_code", response.status_code)) if isinstance(result, dict) else response.status_code
            delay = int((result.get("parameters") or {}).get("retry_after", 0)) if isinstance(result, dict) else 0
            raise TelegramError("Telegram rejected the request (HTTP %s)" % code,
                                uncertain=sending and code >= 500, retry_after=delay)
        return result["result"]

    def download(self, file_id: str, target: Path) -> None:
        descriptor = self.call("getFile", {"file_id": file_id})
        limit = min(self.config.get_int("agent.telegram.max_download_bytes", 20 * 1024 * 1024), 20 * 1024 * 1024)
        if int(descriptor.get("file_size") or 0) > limit:
            raise TelegramError("Attachment exceeds Telegram's download limit")
        remote_path = str(descriptor.get("file_path") or "")
        if not remote_path or ".." in remote_path.split("/") or remote_path.startswith("/"):
            raise TelegramError("Invalid Telegram file descriptor")
        temporary = target.with_suffix(target.suffix + ".part")
        try:
            with httpx.Client(timeout=60, follow_redirects=False) as client:
                with client.stream("GET", "https://api.telegram.org/file/bot%s/%s" %
                                   (self.config.get("agent.telegram.bot_token"), remote_path)) as response:
                    response.raise_for_status()
                    size = 0
                    with temporary.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > limit:
                                raise TelegramError("Attachment exceeds the download byte limit")
                            handle.write(chunk)
            temporary.replace(target)
        except httpx.HTTPError:
            raise TelegramError("Telegram attachment download failed") from None
        finally:
            temporary.unlink(missing_ok=True)


class TelegramDelivery:
    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.client = TelegramClient(config)

    def send(self, job: dict[str, Any], text: str, *, destination: str,
             attachments: Optional[list[dict[str, Any]]] = None,
             purpose: str = "message", reply_to: Optional[int] = None) -> dict[str, Any]:
        if destination not in self.config.get_list("agent.telegram.allowed_user_ids"):
            raise TelegramError("Telegram destination is not authorized")
        if not text.strip() and not attachments:
            raise TelegramError("Telegram messages require text or attachments")
        if len(text) > 100000 or len(attachments or []) > 10:
            raise TelegramError("Telegram output exceeds the configured message bounds")
        parts: list[tuple[str, dict[str, Any], Any]] = []
        for offset in range(0, len(text), 2000):
            payload: dict[str, Any] = {"chat_id": destination, "text": text[offset:offset + 2000]}
            if reply_to:
                payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            parts.append(("sendMessage", payload, None))
        for attachment in attachments or []:
            if len(attachment["data"]) > 50 * 1024 * 1024:
                raise TelegramError("Telegram uploads are limited to 50 MB")
            payload = {"chat_id": destination, "caption": "Task #%s: %s" % (job["id"], attachment["filename"][:200])}
            if reply_to:
                payload["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
            parts.append(("sendDocument", payload,
                          {"document": (attachment["filename"], attachment["data"], attachment["content_type"])}))
        identity = json.dumps({"destination": destination, "text": text, "purpose": purpose,
                               "request": (job.get("metadata") or {}).get("reply_request_id"),
                               "files": [(item["filename"], hashlib.sha256(item["data"]).hexdigest()) for item in attachments or []]}, sort_keys=True)
        prefix = "telegram:%s:%s" % (job["id"], hashlib.sha256(identity.encode()).hexdigest())
        sent_ids = []
        for index, (method, payload, files) in enumerate(parts):
            key = "%s:%s" % (prefix, index)
            row = self.db.fetch_one(
                """INSERT INTO channel_deliveries(job_id, channel, destination, purpose, request_key, body_text, attachments)
                   VALUES (%s, 'telegram', %s, %s, %s, %s, %s) ON CONFLICT (request_key) DO NOTHING RETURNING *""",
                (job["id"], destination, purpose, key, payload.get("text", ""),
                 Jsonb([item["metadata"] for item in attachments or []])),
            )
            if row is None:
                row = self.db.fetch_one("SELECT * FROM channel_deliveries WHERE request_key = %s", (key,))
            if row["status"] == "sent":
                sent_ids.extend(row["provider_message_ids"])
                continue
            if row["status"] in ("sending", "uncertain"):
                return {"status": "uncertain", "log_id": row["id"], "message_ids": sent_ids,
                        "reason": "Do not resend: reconcile the earlier delivery first"}
            available = row.get("available_at")
            if available and available > datetime.now(timezone.utc):
                return {"status": "failed", "retry_after": max(1, int((available - datetime.now(timezone.utc)).total_seconds())), "message_ids": sent_ids}
            with self.db.connect() as connection:
                database = TransactionDatabase(connection)
                database.execute("SELECT pg_advisory_xact_lock(hashtext('telegram_send_budget'))")
                budget = database.fetch_one("SELECT count(*) AS total FROM channel_deliveries WHERE channel = 'telegram' AND created_at > now() - interval '1 hour' AND status IN ('sending', 'sent', 'uncertain')")
                if budget["total"] >= self.config.get_int("agent.telegram.max_messages_per_hour", 120):
                    return {"status": "failed", "reason": "Telegram hourly sending limit reached", "retry_after": 60, "message_ids": sent_ids}
                unresolved = database.fetch_one("SELECT id FROM channel_deliveries WHERE job_id = %s AND destination = %s AND purpose != 'progress' AND status IN ('sending', 'uncertain') AND id != %s LIMIT 1", (job["id"], destination, row["id"]))
                if unresolved:
                    return {"status": "uncertain", "reason": "A prior delivery requires reconciliation", "message_ids": sent_ids}
                claimed = database.fetch_one(
                    """UPDATE channel_deliveries SET status = 'sending' WHERE id = %s
                       AND status IN ('pending', 'failed') RETURNING id""", (row["id"],),
                )
            if not claimed:
                return {"status": "pending", "message_ids": sent_ids}
            try:
                result = self.client.call(method, payload, files=files, sending=True)
            except TelegramError as exc:
                state = "uncertain" if exc.uncertain else "failed"
                self.db.execute("UPDATE channel_deliveries SET status = %s, error = %s, available_at = %s WHERE id = %s",
                                (state, str(exc), datetime.now(timezone.utc) + timedelta(seconds=exc.retry_after), row["id"]))
                return {"status": state, "reason": str(exc), "retry_after": exc.retry_after,
                        "log_id": row["id"], "message_ids": sent_ids}
            message_id = result["message_id"]
            self.db.execute(
                """UPDATE channel_deliveries SET status = 'sent', provider_message_ids = %s,
                   sent_at = now(), error = NULL WHERE id = %s""", (Jsonb([message_id]), row["id"]),
            )
            sent_ids.append(message_id)
        return {"status": "sent", "message_ids": sent_ids, "destination": destination,
                "delivery": "telegram_accepted"}

    def update_status(self, job: dict[str, Any], label: str) -> None:
        route = (job.get("metadata") or {}).get("reply_route") or {}
        if route.get("channel") != "telegram":
            return
        destination = str(route["chat_id"])
        if destination not in self.config.get_list("agent.telegram.allowed_user_ids"):
            raise TelegramError("Telegram destination is not authorized")
        body = "Task #%s: %s\n%s" % (job["id"], label, str(job.get("task_summary") or "")[:300])
        row = self.db.fetch_one("SELECT * FROM channel_deliveries WHERE job_id = %s AND channel = 'telegram' AND purpose = 'progress' AND status = 'sent' ORDER BY id LIMIT 1", (job["id"],))
        if row and row.get("provider_message_ids"):
            if row["body_text"] != body:
                self.client.call("editMessageText", {"chat_id": destination, "message_id": row["provider_message_ids"][0], "text": body})
                self.db.execute("UPDATE channel_deliveries SET body_text = %s WHERE id = %s", (body, row["id"]))
        else:
            self.send(job, body, destination=destination, purpose="progress")


TELEGRAM_FUNCTION_TOOLS = [{
    "type": "function", "function": {
        "name": "telegram_send",
        "description": "Send text and generated files to a configured Telegram identity. recipient='me' resolves the requester's linked identity, including requests received by email. Identical sends within a job are deduplicated. Do not retry uncertain delivery.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}, "recipient": {"type": "string", "default": "me"},
            "attachments": {"type": "array", "items": {"type": "object", "properties": {
                "artifact_id": {"type": "integer", "description": "Forward a clean received artifact belonging to the current task. Provide artifact_id instead of path/content."},
                "path": {"type": "string"}, "content": {"type": "string"},
                "filename": {"type": "string"}, "content_type": {"type": "string"}}}},
        }, "required": ["text"]},
    },
}]