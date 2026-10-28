"""Deterministic Telegram administration; never dispatched through an LLM."""
import secrets
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from .approval_actions import apply_approval
from .approval_channels import channels, review_label, review_summary, telegram_admins
from .email_approvals import ApprovalDeliveryError, EmailApprovals
from .requests import TransactionDatabase


STATUS = {"queued": "Queued", "running": "Working", "waiting": "Waiting for a reply",
          "needs_review": "Needs attention", "completed": "Completed", "failed": "Stopped",
          "cancelled": "Cancelled"}


class TelegramAdmin:
    def __init__(self, receiver):
        self.receiver = receiver
        self.db, self.config, self.client = receiver.db, receiver.config, receiver.client

    def is_admin(self, sender):
        return sender in telegram_admins(self.config)

    def send(self, sender, text, rows=None):
        payload = {"chat_id": sender, "text": text[:3900]}
        if rows:
            payload["reply_markup"] = {"inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row] for row in rows
            ]}
        return self.client.call("sendMessage", payload, sending=True)

    def home(self, sender):
        rows = [[("Workstreams", "ui:streams:0"), ("Jobs", "ui:jobs:0")]]
        if self.is_admin(sender):
            rows.append([("Needs attention", "ui:attention:0"), ("Archived", "ui:archived:0")])
        self.send(sender, "Choose what to manage:", rows)

    def page(self, sender, kind, before=0):
        admin = self.is_admin(sender)
        if kind in {"attention", "archived"} and not admin:
            raise ValueError("Administrator permission is required")
        if kind in {"streams", "archived"}:
            rows = self.db.fetch_all(
                """SELECT id, title FROM workstreams WHERE status = %s
                   AND main_thread_id <> 'system:unattributed' AND (%s = 0 OR id < %s)
                   ORDER BY id DESC LIMIT 9""",
                ("archived" if kind == "archived" else "active", before, before),
            )
            buttons = [[(str(row["title"])[:45], "ui:stream:%s" % row["id"])] for row in rows[:8]]
        else:
            rows = self.db.fetch_all(
                """SELECT j.* FROM jobs j JOIN workstreams w ON w.id = j.workstream_id
                   WHERE w.status = 'active' AND (%s = 0 OR j.id < %s)
                   AND (%s = false OR j.status IN ('needs_review', 'waiting', 'failed'))
                   AND (%s OR EXISTS (
                       SELECT 1 FROM request_messages r WHERE r.thread_id = j.thread_id
                       AND r.channel = 'telegram' AND r.sender_id = %s))
                   ORDER BY j.id DESC LIMIT 9""", (before, before, kind == "attention", admin, sender),
            )
            buttons = [[("%s · %s" % (review_label(row)[:32], STATUS.get(row["status"], row["status"])),
                         "ui:job:%s" % row["id"])] for row in rows[:8]]
        if len(rows) > 8:
            buttons.append([("Older", "ui:%s:%s" % (kind, rows[7]["id"]))])
        buttons.append([("Back", "ui:home:0")])
        if kind == "attention" and channels(self.config) in {"telegram", "both"}:
            buttons.insert(0, [("Approval inbox", "ui:approvals:0")])
        self.send(sender, "No items here." if not rows else "Choose an item:", buttons)

    def job(self, sender, identifier):
        row = self.db.fetch_one("SELECT * FROM jobs WHERE id = %s", (identifier,))
        if not row:
            raise ValueError("This task is no longer available")
        if not self.is_admin(sender) and not self.db.fetch_one(
            "SELECT id FROM request_messages WHERE thread_id = %s AND channel = 'telegram' AND sender_id = %s LIMIT 1",
            (row["thread_id"], sender),
        ):
            raise ValueError("This task is not linked to your Telegram identity")
        return row

    def job_card(self, sender, identifier):
        job = self.job(sender, identifier)
        text = "%s\n%s\nJob #%s" % (review_label(job), STATUS.get(job["status"], job["status"]), identifier)
        buttons = [[("Details", "ui:details:%s" % identifier), ("Results", "ui:results:%s" % identifier)]]
        if self.is_admin(sender):
            if job["status"] == "needs_review":
                buttons.append([("Request approval", "ui:approval:%s" % identifier)])
            elif job["status"] == "waiting":
                buttons.append([("Add reply", "ui:guidance:%s" % identifier)])
            elif job["status"] in {"completed", "failed", "cancelled"}:
                buttons.append([("Run again", "ui:rerun:%s" % identifier)])
            if job["status"] not in {"completed", "cancelled", "failed"}:
                buttons.append([("Cancel task", "ui:cancel:%s" % identifier)])
            buttons.append([("Add guidance", "ui:guidance:%s" % identifier),
                            ("Rename", "ui:renamejob:%s" % identifier)])
        buttons.append([("Back", "ui:jobs:0")])
        self.send(sender, text, buttons)

    def stream_card(self, sender, identifier):
        row = self.db.fetch_one("SELECT * FROM workstreams WHERE id = %s AND main_thread_id <> 'system:unattributed'",
                                (identifier,))
        if not row or row["status"] == "deleting":
            raise ValueError("This workstream is no longer available")
        jobs = self.db.fetch_all(
            "SELECT * FROM jobs WHERE workstream_id = %s ORDER BY id DESC LIMIT 8", (identifier,),
        )
        buttons = [[(review_label(job)[:45], "ui:job:%s" % job["id"])] for job in jobs]
        if row["status"] == "active":
            buttons.append([("Continue here", "ws:%s" % identifier)])
        if self.is_admin(sender):
            buttons.append([("Rename", "ui:renamestream:%s" % identifier)])
            buttons.append([("Restore" if row["status"] == "archived" else "Archive",
                             "ui:restore:%s" % identifier if row["status"] == "archived" else "ui:archive:%s" % identifier)])
            buttons.append([("Deletion options", "ui:delete:%s" % identifier)])
        buttons.append([("Back", "ui:streams:0")])
        self.send(sender, "%s\n%s\nWorkstream #%s" % (row["title"], row["status"].capitalize(), identifier), buttons)

    def confirm(self, sender, action, identifier):
        if not self.is_admin(sender):
            raise ValueError("Administrator permission is required")
        if action in {"cancel", "rerun"}:
            row = self.job(sender, identifier)
            version = str(row["updated_at"])
            text = ("Cancel this task? Completed actions will not be undone." if action == "cancel" else
                    "Run this task again? Previously completed external actions may be repeated.")
        else:
            row = self.db.fetch_one("SELECT * FROM workstreams WHERE id = %s", (identifier,))
            if not row:
                raise ValueError("Workstream not found")
            version = row["status"]
            text = "Archive this workstream? It must have no active tasks. Its data will be retained."
        nonce = secrets.token_hex(12)
        self.db.set_state("telegram-control:" + nonce, {
            "sender": sender, "action": action, "id": identifier, "version": version,
            "expires": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        })
        self.send(sender, text, [[("Confirm", "ui:confirm:" + nonce), ("Back", "ui:home:0")]])

    def execute_confirmation(self, sender, nonce):
        if not self.is_admin(sender):
            raise ValueError("Administrator permission is required")
        with self.db.connect() as conn:
            db = TransactionDatabase(conn)
            row = db.fetch_one(
                """DELETE FROM runtime_state WHERE key = %s AND value->>'sender' = %s
                   AND (value->>'expires')::timestamptz > now() RETURNING value""",
                ("telegram-control:" + nonce, sender),
            )
            if not row:
                raise ValueError("This confirmation expired or was already handled")
            value = row["value"]
            action, identifier = value["action"], value["id"]
            if action in {"cancel", "rerun"}:
                old_states = ["queued", "running", "waiting", "needs_review"] if action == "cancel" else ["completed", "failed", "cancelled"]
                changed = db.fetch_one(
                    """UPDATE jobs SET status = %s, locked_at = NULL, locked_by = NULL,
                       run_at = now(), last_error = NULL, updated_at = now()
                       WHERE id = %s AND updated_at = %s AND status = ANY(%s)
                       AND EXISTS (SELECT 1 FROM workstreams w WHERE w.id = jobs.workstream_id AND w.status = 'active')
                       RETURNING id""",
                    ("cancelled" if action == "cancel" else "queued", identifier, value["version"], old_states),
                )
                if not changed:
                    raise ValueError("Task changed; open its current status before trying again")
                db.log_event(identifier, "status_change", output_data={"reason": "Telegram administrator " + action})
            else:
                db.execute("SELECT pg_advisory_xact_lock(hashtext('workstream-maintenance'))")
                changed = db.fetch_one(
                    """UPDATE workstreams w SET status = 'archived' WHERE w.id = %s AND w.status = %s
                       AND main_thread_id <> 'system:unattributed'
                       AND NOT EXISTS (SELECT 1 FROM jobs WHERE workstream_id = w.id
                           AND status IN ('queued', 'running', 'waiting', 'needs_review'))
                       AND NOT EXISTS (SELECT 1 FROM workstreams child WHERE child.parent_id = w.id AND child.status = 'active')
                       AND NOT EXISTS (SELECT 1 FROM request_messages WHERE workstream_id = w.id
                           AND status IN ('pending', 'processing', 'ready') AND metadata->>'queued' IS DISTINCT FROM 'true')
                       AND NOT EXISTS (SELECT 1 FROM emails e WHERE e.workstream_id = w.id AND e.is_actionable
                           AND NOT EXISTS (SELECT 1 FROM request_messages r WHERE r.email_id = e.id))
                       AND NOT EXISTS (SELECT 1 FROM reminders r JOIN workstream_objects o ON o.id = r.object_id
                           WHERE o.origin_workstream_id = w.id AND r.status IN ('scheduled', 'queued'))
                       AND NOT EXISTS (SELECT 1 FROM deep_research_runs r JOIN workstream_objects o ON o.id = r.object_id
                           WHERE o.origin_workstream_id = w.id AND r.status IN ('queued', 'running', 'waiting_for_input'))
                       AND NOT EXISTS (SELECT 1 FROM model_service_calls m JOIN jobs j ON j.id = m.job_id
                           WHERE j.workstream_id = w.id AND m.status IN ('pending', 'unknown'))
                       AND NOT EXISTS (SELECT 1 FROM channel_deliveries WHERE workstream_id = w.id
                           AND status IN ('pending', 'sending', 'uncertain'))
                       RETURNING w.id""", (identifier, value["version"]),
                )
                if not changed:
                    raise ValueError("Workstream changed or still has active work, scheduled reminders, unresolved "
                                     "deliveries/charges, or active child workstreams. Resolve those before archiving.")
        self.send(sender, "Done. Completed external actions have not been undone.", [[("Back", "ui:home:0")]])

    def prompt(self, sender, action, identifier):
        if not self.is_admin(sender):
            raise ValueError("Administrator permission is required")
        row = (self.db.fetch_one("SELECT * FROM workstreams WHERE id = %s", (identifier,))
               if action == "renamestream" else self.job(sender, identifier))
        if not row:
            raise ValueError("Item no longer available")
        response = self.client.call("sendMessage", {
            "chat_id": sender, "text": "Reply to this message with " + (
                "a short new name." if action.startswith("rename") else "your guidance. This does not approve any held action."),
            "reply_markup": {"force_reply": True, "selective": True},
        }, sending=True)
        self.db.set_state("telegram-admin-input:%s:%s" % (self.receiver.bot_id, sender), {
            "action": action, "id": identifier, "message_id": response["message_id"],
            "version": str(row.get("updated_at") or row["status"]),
            "answer_only": bool((row.get("metadata") or {}).get("awaiting_admin_input")
                                and not (row.get("metadata") or {}).get("security_pending_action")),
            "expires": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
        })

    def input_reply(self, message):
        sender = str(message["from"]["id"])
        reply_id = (message.get("reply_to_message") or {}).get("message_id")
        receipt_key = "telegram-admin-reply:%s:%s:%s" % (self.receiver.bot_id, sender, reply_id)
        if reply_id:
            receipt = self.db.get_state(receipt_key)
            if isinstance(receipt, dict) and receipt.get("handled") is True:
                return True
        key = "telegram-admin-input:%s:%s" % (self.receiver.bot_id, sender)
        pending = self.db.get_state(key)
        if not isinstance(pending, dict) or reply_id != pending["message_id"]:
            return False
        if not self.is_admin(sender) or datetime.fromisoformat(pending["expires"]) <= datetime.now(timezone.utc):
            self.send(sender, "This reply prompt expired. Open /list to try again.")
            return True
        text = str(message.get("text") or "").strip()
        if not text or len(text) > (60 if pending["action"].startswith("rename") else 4000):
            self.send(sender, "Please reply with text within %s characters." % (
                60 if pending["action"].startswith("rename") else 4000))
            return True
        with self.db.connect() as conn:
            db = TransactionDatabase(conn)
            claimed = db.fetch_one("DELETE FROM runtime_state WHERE key = %s AND value = %s RETURNING key",
                                   (key, Jsonb(pending)))
            if not claimed:
                raise ValueError("This reply was already handled")
            if pending["action"] == "renamestream":
                changed = db.fetch_one(
                    """UPDATE workstreams SET title = %s, name_status = 'named', name_attempts = 3
                       WHERE id = %s AND status = %s RETURNING id""", (text, pending["id"], pending["version"]),
                )
            elif pending["action"] == "renamejob":
                changed = db.fetch_one(
                    """UPDATE jobs SET metadata = metadata || %s WHERE id = %s AND updated_at = %s RETURNING id""",
                    (Jsonb({"title": text, "title_manual": True}), pending["id"], pending["version"]),
                )
            elif pending.get("answer_only"):
                from .approval_actions import answer_admin_input
                answer_admin_input(db, {"job_id": pending["id"], "version": pending["version"]}, text)
                changed = True
            else:
                changed = db.fetch_one(
                    """UPDATE jobs SET status = CASE WHEN status = 'waiting' THEN 'queued' ELSE status END,
                       has_new_context = true, run_at = now(), updated_at = now()
                       WHERE id = %s AND updated_at = %s RETURNING id""", (pending["id"], pending["version"]),
                )
                if changed:
                    db.execute("INSERT INTO supervisor_instructions(job_id, instruction, created_by) VALUES (%s, %s, %s)",
                               (pending["id"], text, "telegram-admin"))
                    db.log_event(pending["id"], "supervisor_note",
                                 output_data={"instruction": text, "created_by": "telegram-admin"})
            if not changed:
                raise ValueError("This item changed. Open its current status and try again.")
            db.set_state(receipt_key, {"handled": True})
        self.send(sender, "Reply saved. Task resumed without changing permissions or limits."
                  if pending.get("answer_only") and pending["action"] == "guidance"
                  else "Saved. Held actions still require approval.", [[("Back", "ui:home:0")]])
        return True

    def approval(self, sender, callback_id, action):
        service = EmailApprovals(self.db, self.config)
        row = service.telegram_pending(callback_id, sender)
        value = row["value"]
        if action == "details":
            from .admin_commands import redact_secrets
            self.send(sender, str(redact_secrets(value["summary"]))[:3800],
                      [[("Back", "ap:%s:review" % callback_id)]])
            return
        if action == "later":
            self.send(sender, "Task remains paused. Open Needs attention when ready.", [[("Back", "ui:home:0")]])
            return
        expired = datetime.fromisoformat(value["expires"]) <= datetime.now(timezone.utc)
        if expired and action != "renew":
            self.send(sender, "This approval expired; the task is still paused.",
                      [[("Request fresh approval", "ap:%s:renew" % callback_id)]])
            return
        if action == "renew":
            payload = value["payload"]
            if payload["kind"] in {"review", "input"}:
                job = self.job(sender, payload["job_id"])
                if job["status"] != "needs_review" or str(job["updated_at"]) != payload["version"]:
                    raise ValueError("Task changed. Open its status to request a current approval.")
            service.request("renew:" + secrets.token_hex(12), payload, value["summary"])
            self.send(sender, "Fresh approval sent.")
        elif action == "review":
            text = value["summary"].split("\n\nDetails:", 1)[0][:1800]
            self.send(sender, text + "\n\nConfirm this exact request?",
                      [[("Continue task" if value["payload"]["kind"] == "input" else "Approve once",
                         "ap:%s:ok" % callback_id),
                        ("Not now", "ap:%s:later" % callback_id)],
                       [("Details", "ap:%s:details" % callback_id)]] +
                      ([[("Add reply", "ui:guidance:%s" % value["payload"]["job_id"])]]
                       if value["payload"]["kind"] == "input" else []))
        elif action == "ok":
            payload = service.consume_telegram(callback_id, sender)
            result = apply_approval(self.db, self.config, payload)
            from .admin_commands import redact_secrets
            self.send(sender, str(redact_secrets(result))[:3500], [[("Back", "ui:home:0")]])
        else:
            raise ValueError("Unknown approval action")

    def callback(self, callback):
        sender = str(callback["from"]["id"])
        pieces = str(callback.get("data") or "").split(":")
        if not pieces or pieces[0] not in {"ui", "ap"}:
            return False
        self.client.call("answerCallbackQuery", {"callback_query_id": callback["id"]})
        try:
            if len(pieces) != 3:
                raise ValueError("Invalid menu action")
            if pieces[0] == "ap":
                self.approval(sender, pieces[1], pieces[2])
                return True
            _, action, argument = pieces
            if action == "home":
                self.home(sender)
            elif action == "confirm":
                self.execute_confirmation(sender, argument)
            else:
                identifier = int(argument)
                if not 0 <= identifier < 2**63:
                    raise ValueError("Invalid item")
                if action in {"streams", "jobs", "attention", "archived"}:
                    self.page(sender, action, identifier)
                elif action == "job":
                    self.job_card(sender, identifier)
                elif action == "stream":
                    self.stream_card(sender, identifier)
                elif action in {"details", "results"}:
                    job = self.job(sender, identifier)
                    from .admin_commands import redact_secrets
                    detail = (job.get("last_error") or "No error recorded.") if action == "details" else (
                        (job.get("metadata") or {}).get("final_response") or
                        (job.get("metadata") or {}).get("pending_final_response") or "Open the conversation for delivered results.")
                    self.send(sender, str(redact_secrets(detail)), [[("Back", "ui:job:%s" % identifier)]])
                else:
                    if not self.is_admin(sender):
                        raise ValueError("Administrator permission is required")
                    if action == "approvals":
                        rows = self.db.fetch_all(
                            """SELECT value FROM runtime_state WHERE key LIKE 'email-approval:%%'
                               AND value ? 'callback_id' AND (value->>'expires')::timestamptz > now() - interval '30 days'
                               AND value->'telegram_admin_ids' ? %s
                               ORDER BY updated_at DESC LIMIT 20""", (sender,),
                        )
                        self.send(sender, "Pending approvals (expired ones can be renewed):" if rows else "No pending approvals.",
                                  [[(str(row["value"]["summary"]).splitlines()[0][:45],
                                     "ap:%s:review" % row["value"]["callback_id"])] for row in rows] +
                                  [[("Back", "ui:attention:0")]])
                    elif action == "approval":
                        if channels(self.config) == "email":
                            self.send(sender, "Approvals are configured for email. Check the admin mailbox after requesting approval.")
                        job = self.job(sender, identifier)
                        if job["status"] != "needs_review":
                            raise ValueError("Task is not awaiting approval")
                        metadata = job.get("metadata") or {}
                        EmailApprovals(self.db, self.config).request(
                            "menu-review:" + secrets.token_hex(12),
                            {"kind": "input" if metadata.get("awaiting_admin_input")
                             and not metadata.get("security_pending_action") else "review",
                             "job_id": identifier, "version": str(job["updated_at"])},
                            review_summary(job, job.get("last_error") or "Task paused"),
                        )
                        self.send(sender, "A fresh confirmation has been sent through your configured approval channels.",
                                  [[("Add guidance", "ui:guidance:%s" % identifier), ("Back", "ui:home:0")]])
                    elif action in {"cancel", "rerun", "archive"}:
                        self.confirm(sender, action, identifier)
                    elif action in {"guidance", "renamejob", "renamestream"}:
                        self.prompt(sender, action, identifier)
                    elif action == "restore":
                        with self.db.connect() as conn:
                            db = TransactionDatabase(conn)
                            db.execute("SELECT pg_advisory_xact_lock_shared(hashtext('workstream-maintenance'))")
                            restored = db.fetch_one(
                                """UPDATE workstreams w SET status = 'active' WHERE w.id = %s AND w.status = 'archived'
                                   AND (w.parent_id IS NULL OR EXISTS (
                                       SELECT 1 FROM workstreams p WHERE p.id = w.parent_id AND p.status = 'active'))
                                   RETURNING w.id""", (identifier,),
                            )
                            if not restored:
                                raise ValueError("Workstream is not archived, or its parent must be restored first")
                        self.stream_card(sender, identifier)
                    elif action == "delete":
                        self.send(sender, "Permanent deletion requires a verified inventory and stopped writer services. "
                                  "Use setup > Tools > Workstreams and data deletion on the server. "
                                  "Archive is available here without deleting data.", [[("Back", "ui:stream:%s" % identifier)]])
                    else:
                        raise ValueError("Unknown menu action")
        except (ValueError, ApprovalDeliveryError) as exc:
            self.send(sender, str(exc), [[("Back", "ui:home:0")]])
        return True
