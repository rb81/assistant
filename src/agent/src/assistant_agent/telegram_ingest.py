import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from .artifact_processor import ArtifactProcessor
from .config import AppConfig
from .database import Database
from .polling import run_poll_loop
from .requests import IncomingRequest, RequestService
from .telegram import TelegramClient, TelegramDelivery, TelegramError, telegram_identities
from .threading import safe_filename
from .transcription import transcribe_audio


LOGGER = logging.getLogger("assistant.telegram")


def message_files(message: dict[str, Any]) -> list[dict[str, Any]]:
    files = []
    for kind in ("document", "voice", "audio", "video"):
        item = message.get(kind)
        if item:
            files.append({"file_id": item["file_id"], "filename": safe_filename(item.get("file_name") or
                          ("voice.ogg" if kind == "voice" else "%s.bin" % kind)),
                          "content_type": item.get("mime_type") or "application/octet-stream",
                          "size_bytes": item.get("file_size"), "voice": kind == "voice",
                          "audio": kind in ("voice", "audio")})
    if message.get("photo"):
        item = max(message["photo"], key=lambda photo: int(photo.get("width", 0)) * int(photo.get("height", 0)))
        files.append({"file_id": item["file_id"], "filename": "photo.jpg", "content_type": "image/jpeg",
                      "size_bytes": item.get("file_size"), "voice": False, "audio": False})
    return files


class TelegramReceiver:
    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.client = TelegramClient(config)
        self.bot_id = str(self.client.call("getMe", {})["id"])

    def owned_jobs(self, sender: str, conversation: str, *, waiting: bool = False) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """SELECT DISTINCT j.* FROM jobs j
               JOIN workstreams w ON w.id = j.workstream_id AND w.status = 'active'
               WHERE (EXISTS (SELECT 1 FROM request_messages r WHERE r.thread_id = j.thread_id
                              AND r.channel = 'telegram' AND r.sender_id = %s AND r.conversation_id = %s)
                   OR (j.metadata->'reply_route'->>'channel' = 'telegram'
                       AND j.metadata->'reply_route'->>'chat_id' = %s))
               AND j.status IN ('queued', 'running', 'waiting', 'needs_review')
               AND (%s = false OR (j.status = 'waiting' AND j.metadata->>'waiting_for_input' = 'true'))
               ORDER BY j.id DESC LIMIT 20""", (sender, conversation, sender, waiting),
        )

    def linked_addresses(self, sender: str) -> list[str]:
        return [address for address, identifier in telegram_identities(self.config).items() if str(identifier) == sender]

    def selected_workstream(self, sender: str) -> dict[str, Any] | None:
        return self.db.fetch_one(
            """SELECT s.workstream_id, s.unavailable_workstream_id, w.main_thread_id, w.status,
                      w.origin_channel
               FROM telegram_workstream_selections s
               LEFT JOIN workstreams w ON w.id = s.workstream_id
               WHERE s.bot_id = %s AND s.sender_id = %s""", (self.bot_id, sender),
        )

    def visible_workstreams(self, sender: str) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """SELECT w.id, w.title, w.main_thread_id, w.parent_id, w.created_at,
                      count(DISTINCT a.object_id) AS object_count
               FROM workstreams w
               LEFT JOIN workstream_attachments a ON a.workstream_id = w.id
               WHERE w.status = 'active' AND w.main_thread_id <> 'system:unattributed'
               GROUP BY w.id ORDER BY w.id DESC LIMIT 100""",
            (),
        )

    def set_selection(self, sender: str, workstream_id: int) -> None:
        self.db.execute(
            """INSERT INTO telegram_workstream_selections(bot_id, sender_id, workstream_id, unavailable_workstream_id)
               VALUES (%s, %s, %s, NULL)
               ON CONFLICT (bot_id, sender_id) DO UPDATE
               SET workstream_id = EXCLUDED.workstream_id, unavailable_workstream_id = NULL, updated_at = now()""",
            (self.bot_id, sender, workstream_id),
        )

    def deleted_reply_workstream_id(self, message: dict[str, Any]) -> int | None:
        reply_id = (message.get("reply_to_message") or {}).get("message_id")
        if not reply_id:
            return None
        references = [
            "%s:%s:%s" % (self.bot_id, message["chat"]["id"], reply_id),
            "telegram:%s:%s" % (message["chat"]["id"], reply_id),
        ]
        hashes = [hashlib.sha256(value.encode()).hexdigest() for value in references]
        row = self.db.fetch_one(
            """SELECT deleted_workstream_id FROM deleted_thread_references
               WHERE reference_hash = ANY(%s::text[]) ORDER BY deleted_at DESC LIMIT 1""",
            (hashes,),
        )
        return int(row["deleted_workstream_id"]) if row else None

    def task_thread(self, message: dict[str, Any], conversation: str, sender: str) -> tuple[str, list[dict[str, Any]]]:
        reply = message.get("reply_to_message") or {}
        if reply.get("message_id"):
            reference = "%s:%s:%s" % (self.bot_id, message["chat"]["id"], reply["message_id"])
            row = self.db.fetch_one(
                """SELECT r.thread_id FROM request_messages r
                   JOIN workstreams w ON w.id = r.workstream_id AND w.status = 'active'
                   WHERE r.channel = 'telegram' AND r.external_id = %s
                     AND r.sender_id = %s AND r.conversation_id = %s""",
                (reference, sender, conversation),
            )
            if not row:
                row = self.db.fetch_one(
                    """SELECT j.thread_id FROM channel_deliveries d JOIN jobs j ON j.id = d.job_id
                       JOIN workstreams w ON w.id = j.workstream_id AND w.status = 'active'
                       WHERE d.channel = 'telegram' AND d.destination = %s AND d.provider_message_ids @> %s
                       AND (EXISTS (SELECT 1 FROM request_messages r WHERE r.thread_id = j.thread_id
                           AND r.sender_id = %s AND r.conversation_id = %s)
                           OR (j.metadata->'reply_route'->>'channel' = 'telegram' AND j.metadata->'reply_route'->>'chat_id' = %s)
                           ) LIMIT 1""",
                    (str(message["chat"]["id"]), Jsonb([reply["message_id"]]), sender, conversation, sender),
                )
            if row:
                return row["thread_id"], []
            return "telegram:%s:%s" % (conversation, message["message_id"]), []

        selection = self.selected_workstream(sender)
        if selection and selection["workstream_id"] is not None and selection["status"] == "active":
            waiting = self.db.fetch_all(
                """SELECT DISTINCT j.* FROM jobs j
                   JOIN workstreams w ON w.id = j.workstream_id AND w.status = 'active'
                   WHERE j.workstream_id = %s AND j.status = 'waiting'
                     AND j.metadata->>'waiting_for_input' = 'true'
                   ORDER BY j.id DESC LIMIT 20""", (selection["workstream_id"],),
            )
            if len(waiting) == 1:
                return waiting[0]["thread_id"], []
            if len(waiting) > 1:
                return selection["main_thread_id"], waiting
            return selection["main_thread_id"], []

        waiting = self.owned_jobs(sender, conversation, waiting=True)
        if len(waiting) == 1:
            return waiting[0]["thread_id"], []
        if len(waiting) > 1:
            stream = self.db.fetch_one(
                "SELECT main_thread_id FROM workstreams WHERE id = %s AND status = 'active'",
                (waiting[0]["workstream_id"],),
            )
            if stream:
                return stream["main_thread_id"], waiting

        group = message.get("media_group_id")
        recent = self.db.fetch_one(
            """SELECT thread_id FROM request_messages WHERE channel = 'telegram'
               AND sender_id = %s AND conversation_id = %s AND status = 'pending'
               AND NOT (metadata ? 'selection')
             AND (created_at > now() - interval '2 seconds' OR (%s::text IS NOT NULL AND metadata->>'media_group_id' = %s))
               ORDER BY id DESC LIMIT 1""", (sender, conversation, group, group),
        )
        return (recent["thread_id"] if recent else "telegram:%s:%s" % (conversation, message["message_id"])), []

    def send_workstream_list(self, sender: str, external: str) -> None:
        rows = self.visible_workstreams(sender)
        if not rows:
            text = "No workstreams are available yet. Send a message to start one."
            TelegramDelivery(self.db, self.config).send({"id": None}, text, destination=sender,
                                                        purpose="command:" + external)
            return
        self.client.call("sendMessage", {
            "chat_id": sender, "text": "Choose a workstream:",
            "reply_markup": {"inline_keyboard": [[
                {"text": "#%s %s" % (row["id"], str(row["title"])[:45]), "callback_data": "ws:%s" % row["id"]}
            ] for row in rows]},
        }, sending=True)

    def ingest(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if callback:
            self.select_task(callback)
            return
        message = update.get("message") or {}
        sender = str((message.get("from") or {}).get("id") or "")
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or sender not in self.config.get_list("agent.telegram.allowed_user_ids") or str(chat.get("id")) != sender:
            return
        if not message.get("text") and not message.get("caption") and not message_files(message):
            return
        from .telegram_admin import TelegramAdmin
        admin_ui = TelegramAdmin(self)
        try:
            if admin_ui.input_reply(message):
                return
        except ValueError as exc:
            admin_ui.send(sender, str(exc))
            return
        conversation = "%s:%s" % (self.bot_id, chat["id"])
        external = "%s:%s" % (conversation, message["message_id"])
        if self.db.fetch_one("SELECT id FROM request_messages WHERE channel = 'telegram' AND external_id = %s", (external,)):
            return
        body = str(message.get("text") or message.get("caption") or "")
        command, _, command_body = body.strip().partition(" ")
        if command == "/list":
            admin_ui.home(sender)
            self.db.set_state("telegram:%s:command:%s" % (self.bot_id, update["update_id"]), True)
            return
        is_new_workstream = command == "/new"
        if is_new_workstream:
            body = command_body.strip()
            thread = "telegram:%s" % external
            selection = []
        else:
            selected = self.selected_workstream(sender)
            if not message.get("reply_to_message") and selected and selected["workstream_id"] is not None and (
                selected["status"] != "active" or not selected["main_thread_id"]
            ):
                unavailable_id = selected["workstream_id"]
                self.db.execute(
                    """UPDATE telegram_workstream_selections
                       SET workstream_id = NULL, unavailable_workstream_id = %s, updated_at = now()
                       WHERE bot_id = %s AND sender_id = %s""", (unavailable_id, self.bot_id, sender),
                )
                TelegramDelivery(self.db, self.config).send(
                    {"id": None},
                    "Your selected workstream is no longer available. This message was not routed; use /list or /new.",
                    destination=sender, purpose="command:" + external,
                )
                return
            if not message.get("reply_to_message") and selected and selected["unavailable_workstream_id"] is not None:
                TelegramDelivery(self.db, self.config).send(
                    {"id": None},
                    "Your previous workstream is no longer available. This message was not routed; use /list or /new.",
                    destination=sender, purpose="command:" + external,
                )
                return
            thread, selection = self.task_thread(message, conversation, sender)
        creating_new_thread = is_new_workstream or not self.db.fetch_one(
            "SELECT thread_id FROM workstream_threads WHERE thread_id = %s", (thread,),
        )
        if body.startswith(("/tasks", "/status", "/cancel", "/start")):
            jobs = self.owned_jobs(sender, conversation)
            if body.startswith("/cancel"):
                identifier = body.split()[1] if len(body.split()) == 2 else ""
                owned = next((job for job in jobs if str(job["id"]) == identifier), None)
                if owned:
                    self.db.update_job_status(owned["id"], "cancelled", last_error="Cancelled by requester via Telegram")
                    text = "Task #%s cancelled. Already completed actions are not undone." % identifier
                else:
                    text = "No matching active task."
            elif body.startswith("/start"):
                text = "Connected."
            else:
                text = "\n".join("#%s %s: %s" % (job["id"], job["status"], job.get("task_summary") or "Task") for job in jobs) or "No active tasks."
            TelegramDelivery(self.db, self.config).send({"id": None}, text, destination=sender, purpose="command:" + external)
            self.db.set_state("telegram:%s:command:%s" % (self.bot_id, update["update_id"]), True)
            return
        metadata = {"media_group_id": message.get("media_group_id")}
        if creating_new_thread:
            metadata["temporary_workstream"] = "telegram"
            metadata["workstream_title"] = "New workstream"
        reply_starts_new_conversation = bool(
            message.get("reply_to_message")
            and thread == "telegram:%s:%s" % (conversation, message["message_id"])
        )
        if reply_starts_new_conversation:
            metadata["new_conversation_after_deleted_reply"] = True
            deleted_workstream_id = self.deleted_reply_workstream_id(message)
            if deleted_workstream_id is not None:
                metadata["deleted_workstream_id"] = deleted_workstream_id
        if selection:
            metadata["selection"] = [job["id"] for job in selection]
        if is_new_workstream and not body and not message_files(message):
            with self.db.connect() as connection:
                connection.execute("SELECT set_config('assistant.telegram_new_workstream', 'yes', true)")
                connection.execute("SELECT set_config('assistant.workstream_channel', 'telegram', true)")
                stream = connection.execute(
                    "SELECT ensure_workstream(%s, 'New workstream') AS id", (thread,),
                ).fetchone()
            self.set_selection(sender, stream["id"])
            TelegramDelivery(self.db, self.config).send(
                {"id": None}, "Started a new workstream.", destination=sender, purpose="command:" + external,
            )
            return
        request = RequestService(self.db).receive_request(
            IncomingRequest("telegram", external, conversation, sender, thread, body,
                            reply_route={"channel": "telegram", "chat_id": sender, "message_id": message["message_id"]},
                            attachments=message_files(message), metadata=metadata,
                            received_at=datetime.fromtimestamp(message["date"], timezone.utc)), queue=False,
        )
        if is_new_workstream or reply_starts_new_conversation or not self.selected_workstream(sender):
                            self.set_selection(sender, request["workstream_id"])
        self.db.execute("UPDATE request_messages SET available_at = now() + interval '2 seconds' WHERE thread_id = %s AND status = 'pending'", (thread,))
        if selection:
            self.client.call("sendMessage", {"chat_id": sender, "text": "Which task is this for?",
                "reply_markup": {"inline_keyboard": [[{"text": "#%s %s" % (job["id"], str(job.get("task_summary") or "Task")[:50]),
                    "callback_data": "task:%s:%s" % (request["id"], job["id"])}] for job in selection]}}, sending=True)

    def select_task(self, callback: dict[str, Any]) -> None:
        sender = str((callback.get("from") or {}).get("id") or "")
        if sender not in self.config.get_list("agent.telegram.allowed_user_ids"):
            return
        chat = (callback.get("message") or {}).get("chat") or {}
        if chat.get("type") != "private" or str(chat.get("id")) != sender:
            return
        from .telegram_admin import TelegramAdmin
        if TelegramAdmin(self).callback(callback):
            return
        pieces = str(callback.get("data") or "").split(":")
        if len(pieces) == 2 and pieces[0] == "ws" and pieces[1].isdigit():
            workstream_id = int(pieces[1])
            if not any(row["id"] == workstream_id for row in self.visible_workstreams(sender)):
                self.client.call("answerCallbackQuery", {
                    "callback_query_id": callback["id"], "text": "That workstream is no longer available.",
                })
                return
            self.set_selection(sender, workstream_id)
            self.client.call("answerCallbackQuery", {
                "callback_query_id": callback["id"], "text": "Workstream selected.",
            })
            return
        if len(pieces) != 3 or pieces[0] != "task" or not all(piece.isdigit() for piece in pieces[1:]):
            return
        request = self.db.fetch_one("SELECT * FROM request_messages WHERE id = %s AND sender_id = %s AND status = 'pending' AND created_at > now() - interval '15 minutes'", (int(pieces[1]), sender))
        if not request or int(pieces[2]) not in (request["metadata"].get("selection") or []):
            self.client.call("answerCallbackQuery", {"callback_query_id": callback["id"],
                             "text": "This selection has expired or was already handled. Reply to the task message or send a new request."})
            return
        job = next((item for item in self.owned_jobs(sender, request["conversation_id"]) if item["id"] == int(pieces[2])), None)
        if not job:
            self.client.call("answerCallbackQuery", {"callback_query_id": callback["id"],
                             "text": "This task is no longer active."})
            return
        self.db.execute("UPDATE request_messages SET thread_id = %s, metadata = metadata - 'selection', available_at = now() WHERE id = %s AND metadata ? 'selection'", (job["thread_id"], request["id"]))
        self.client.call("answerCallbackQuery", {"callback_query_id": callback["id"]})

    def poll_once(self) -> int:
        key = "telegram:%s:offset" % self.bot_id
        with self.db.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS claimed", (key,))
                if not cursor.fetchone()["claimed"]:
                    return 0
                try:
                    self.activate_text_requests()
                    offset = int(self.db.get_state(key, 0))
                    pending_text = self.db.fetch_one("SELECT id FROM request_messages WHERE channel = 'telegram' AND status = 'pending' AND attachments = '[]'::jsonb LIMIT 1")
                    updates = self.client.call("getUpdates", {"offset": offset, "timeout": 2 if pending_text else 20,
                                                             "allowed_updates": ["message", "callback_query"]})
                    for update in sorted(updates, key=lambda item: item["update_id"]):
                        self.ingest(update)
                        self.db.set_state(key, int(update["update_id"]) + 1)
                    self.activate_text_requests()
                    return len(updates)
                finally:
                    cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))

    def activate_text_requests(self) -> None:
        requests = self.db.fetch_all(
            """SELECT * FROM request_messages r WHERE channel = 'telegram' AND status = 'pending'
               AND attachments = '[]'::jsonb AND available_at <= now() AND NOT (metadata ? 'selection')
               AND NOT EXISTS (SELECT 1 FROM request_messages other WHERE other.thread_id = r.thread_id
                   AND ((other.status = 'pending' AND (other.attachments != '[]'::jsonb OR other.available_at > now()))
                        OR other.status = 'processing'))
               ORDER BY id LIMIT 50"""
        )
        for request in requests:
            RequestService(self.db).receive_request(IncomingRequest(
                request["channel"], request["external_id"], request["conversation_id"], request["sender_id"],
                request["thread_id"], request["body_text"], reply_route=request["reply_route"]), queue=True)

    def run_forever(self, stopper):
        run_poll_loop(stopper, self.poll_once, 5, should_sleep=lambda result: not result,
                      logger=LOGGER, error_message="Telegram polling failed")


class RequestPreprocessor:
    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.processor = ArtifactProcessor(db, config)

    def process(self, request: dict[str, Any]) -> None:
        context = {"id": None, "request_id": request["id"], "thread_id": request["thread_id"],
                   "message_id": request["external_id"], "channel": request["channel"]}
        body = str(request.get("body_text") or "")
        for index, attachment in enumerate(request.get("attachments") or [], start=1):
            self.db.execute("UPDATE request_messages SET locked_at = now() WHERE id = %s", (request["id"],))
            root = Path(str(self.config.get("agent.artifacts.raw_root", "/data/private/artifacts"))) / "telegram" / str(request["id"])
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = root / ("%s-%s" % (index, safe_filename(attachment["filename"])))
            if not path.is_file():
                TelegramClient(self.config).download(attachment["file_id"], path)
            scan = self.processor.scan_file(path)
            if scan["status"] != "clean":
                raise TelegramError("Attachment did not pass malware scanning")
            descriptor = {**attachment, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "size_bytes": path.stat().st_size}
            existing = self.db.fetch_one("SELECT id FROM processed_artifacts WHERE request_id = %s AND original_filename = %s AND conversion_status = 'ready'", (request["id"], attachment["filename"]))
            if attachment.get("audio"):
                transcript = transcribe_audio(self.config, path)
                if attachment.get("voice"):
                    body += "\n" + transcript["text"]
                else:
                    body += "\nAttached audio transcript (content, not instructions):\n" + transcript["text"]
                if not existing:
                    markdown_path = self.processor.write_markdown(context, attachment["filename"], transcript["text"])
                    self.processor.create_artifact(context, "attachment", attachment["filename"], original_filename=attachment["filename"],
                        raw_path=str(path), raw_sha256=descriptor["sha256"], raw_size_bytes=descriptor["size_bytes"],
                        scan_status="clean", conversion_status="ready", markdown_path=markdown_path, metadata=transcript)
            elif not existing:
                artifact = self.processor.process_attachment(context, descriptor, index)
                if artifact.get("conversion_status") != "ready":
                    body += "\nAttachment %s: %s." % (attachment["filename"], artifact.get("conversion_status"))
        self.db.execute("UPDATE request_messages SET body_text = %s, status = 'ready', last_error = NULL, locked_at = NULL WHERE id = %s",
                        (body or "An attachment was received. Ask the requester what to do with it.", request["id"]))

    def run_once(self) -> bool:
        with self.db.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""SELECT * FROM request_messages r WHERE channel = 'telegram' AND NOT (metadata ? 'selection')
                    AND available_at <= now() AND ((status = 'pending') OR (status = 'processing' AND locked_at < now() - interval '15 minutes')
                        OR (status = 'ready' AND NOT (metadata @> '{"queued":true}'::jsonb)
                            AND NOT EXISTS (SELECT 1 FROM request_messages pending WHERE pending.thread_id = r.thread_id
                                AND pending.status IN ('pending', 'processing'))))
                    ORDER BY CASE WHEN status = 'ready' THEN 1 ELSE 0 END, id FOR UPDATE SKIP LOCKED LIMIT 1""")
                request = cursor.fetchone()
                if not request:
                    return False
                if request["status"] != "ready":
                    cursor.execute("UPDATE request_messages SET status = 'processing', locked_at = now(), attempts = attempts + 1 WHERE id = %s", (request["id"],))
        try:
            if request["status"] != "ready":
                self.process(request)
            unsettled = self.db.fetch_one("SELECT id FROM request_messages WHERE thread_id = %s AND status IN ('pending', 'processing') LIMIT 1", (request["thread_id"],))
            if not unsettled:
                ready = self.db.fetch_one("SELECT * FROM request_messages WHERE thread_id = %s AND status = 'ready' AND metadata->>'queued' IS DISTINCT FROM 'true' ORDER BY id DESC LIMIT 1", (request["thread_id"],))
                if ready:
                    RequestService(self.db).receive_request(IncomingRequest(
                        ready["channel"], ready["external_id"], ready["conversation_id"], ready["sender_id"],
                        ready["thread_id"], ready["body_text"], reply_route=ready["reply_route"]), queue=True)
                    self.db.execute("UPDATE request_messages SET metadata = metadata || '{\"queued\":true}'::jsonb WHERE thread_id = %s AND status = 'ready'", (ready["thread_id"],))
        except Exception:
            LOGGER.warning("request preprocessing failed for request %s", request["id"])
            exhausted = int(request["attempts"]) >= 2
            self.db.execute("UPDATE request_messages SET status = %s, available_at = now() + interval '30 seconds', locked_at = NULL, last_error = 'Attachment processing failed' WHERE id = %s", ("failed" if exhausted else "pending", request["id"]))
            if exhausted:
                TelegramDelivery(self.db, self.config).send({"id": None}, "I couldn't process this attachment or voice note. Please resend it or send text.", destination=request["sender_id"], purpose="error:%s" % request["id"])
        return True

    def run_forever(self, stopper):
        run_poll_loop(stopper, self.run_once, 2, should_sleep=lambda result: not result,
                      logger=LOGGER, error_message="request preprocessing failed")