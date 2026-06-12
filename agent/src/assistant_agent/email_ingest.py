import email
import hashlib
import imaplib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional

from psycopg.types.json import Jsonb

from .artifact_processor import ArtifactProcessor
from .config import AppConfig, message_id_domain
from .database import Database, json_safe
from .email_disclosure import strip_disclosure_html, strip_disclosure_text
from .imap_utils import imap_mailbox_arg, imap_status_ok
from .polling import poll_interval_seconds, run_poll_loop
from .threading import normalize_subject, parse_addresses, parse_reference_header, safe_filename, safe_message_segment


LOGGER = logging.getLogger("assistant.downloader")


def decode_mime_header(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def message_datetime(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return datetime.now(timezone.utc)


def payload_to_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def extract_bodies_and_attachments(message: Message, attachment_root: Path, message_id: str) -> tuple[str, str, list[dict[str, Any]]]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []

    if message.is_multipart():
        parts = message.walk()
    else:
        parts = iter([message])

    target_dir = attachment_root / safe_message_segment(message_id)
    for part in parts:
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = decode_mime_header(part.get_filename())
        content_type = part.get_content_type()
        if disposition == "attachment" or filename:
            payload = part.get_payload(decode=True) or b""
            target_dir.mkdir(parents=True, exist_ok=True)
            clean_name = safe_filename(filename or "attachment")
            target_path = target_dir / clean_name
            suffix = 1
            while target_path.exists():
                target_path = target_dir / ("%s-%s" % (suffix, clean_name))
                suffix += 1
            target_path.write_bytes(payload)
            attachments.append(
                {
                    "filename": clean_name,
                    "content_type": content_type,
                    "path": str(target_path),
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
            continue

        if content_type == "text/plain":
            text_parts.append(payload_to_text(part))
        elif content_type == "text/html":
            html_parts.append(payload_to_text(part))

    return "\n\n".join(text_parts).strip(), "\n\n".join(html_parts).strip(), attachments


def message_id_for(raw_bytes: bytes, message: Message, domain: str = "assistant.local") -> str:
    header_id = message.get("Message-ID")
    if header_id:
        return header_id.strip()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    return "<missing-%s@%s>" % (digest, domain)


@dataclass(frozen=True)
class IngestResult:
    inserted: bool
    queued: bool
    message_id: str


class EmailDownloader:
    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        raw_root = Path(config.get("agent.artifacts.raw_root", "/data/private/artifacts"))
        self.attachment_root = raw_root / "attachments"
        self.artifact_processor = ArtifactProcessor(db, config)

    def run_forever(self, stop_requested) -> None:
        interval = poll_interval_seconds(
            self.config,
            "agent.email.imap_poll_interval_seconds",
            fallback_path=None,
            default=60,
        )
        run_poll_loop(
            stop_requested,
            self.sync_once,
            interval,
            should_sleep=lambda _count: True,
            on_result=lambda count: LOGGER.info("download sync complete, inserted %s email(s)", count),
            logger=LOGGER,
            error_message="download sync failed",
        )

    def sync_once(self) -> int:
        host = self.config.get("agent.email.imap_host")
        username = self.config.get("agent.email.imap_username")
        password = self.config.get("agent.email.imap_password")
        folder = self.config.get("agent.email.imap_folder", "INBOX")
        archive_folder = self.config.get("agent.email.imap_archive_folder") or "Archive"
        port = self.config.get_int("agent.email.imap_port", 993)
        if not host or host == "imap.example.com" or not username or not password:
            LOGGER.warning("IMAP is not configured; downloader skipped")
            return 0

        state_key = "imap:%s:%s:last_uid" % (host, folder)
        last_uid = int(self.db.get_state(state_key, 0) or 0)
        inserted = 0
        max_uid = last_uid

        with imaplib.IMAP4_SSL(host, port) as mailbox:
            mailbox.login(username, password)
            mailbox.select(imap_mailbox_arg(folder))
            status, data = mailbox.uid("search", None, "UID", "%s:*" % (last_uid + 1))
            if not imap_status_ok(status):
                raise RuntimeError("IMAP UID search failed: %s" % status)
            uid_values = data[0].split() if data and data[0] else []
            for uid_bytes in uid_values:
                uid = int(uid_bytes)
                if uid <= last_uid:
                    continue
                status, fetched = mailbox.uid("fetch", uid_bytes, "(RFC822)")
                if not imap_status_ok(status) or not fetched:
                    LOGGER.warning("failed to fetch UID %s", uid)
                    continue
                raw = None
                for item in fetched:
                    if isinstance(item, tuple):
                        raw = item[1]
                        break
                if raw is None:
                    continue
                result = self.ingest_raw(raw, folder)
                if result.inserted:
                    inserted += 1
                if result.queued:
                    try:
                        self.archive_uid(mailbox, uid_bytes, archive_folder, folder)
                    except Exception as exc:
                        raise RuntimeError("failed to archive queued IMAP UID %s from folder %s" % (uid, folder)) from exc
                max_uid = max(max_uid, uid)

        if max_uid > last_uid:
            self.db.set_state(state_key, max_uid)
        return inserted

    def archive_uid(self, mailbox: imaplib.IMAP4_SSL, uid_bytes: bytes, archive_folder: Optional[str], source_folder: str = "") -> None:
        target_folder = str(archive_folder or "").strip()
        uid_text = uid_bytes.decode("ascii", errors="replace") if isinstance(uid_bytes, bytes) else str(uid_bytes)
        if not target_folder:
            raise RuntimeError("IMAP archive folder is not configured for queued UID %s" % uid_text)
        if source_folder and target_folder.lower() == str(source_folder).strip().lower():
            raise RuntimeError("IMAP archive folder matches source folder %s for queued UID %s" % (source_folder, uid_text))

        target_arg = imap_mailbox_arg(target_folder)
        try:
            status, _ = mailbox.uid("MOVE", uid_bytes, target_arg)
        except Exception as exc:
            status = "ERROR: %s" % exc
        if imap_status_ok(status):
            LOGGER.info("archived IMAP UID %s to %s", uid_text, target_folder)
            return

        LOGGER.info("IMAP UID MOVE for UID %s returned %s; trying COPY/DELETE fallback", uid_text, status)
        status, _ = mailbox.uid("COPY", uid_bytes, target_arg)
        if not imap_status_ok(status):
            raise RuntimeError("IMAP UID COPY to archive folder %s failed: %s" % (target_folder, status))
        status, _ = mailbox.uid("STORE", uid_bytes, "+FLAGS.SILENT", r"(\Deleted)")
        if not imap_status_ok(status):
            raise RuntimeError("IMAP UID STORE deleted flag failed: %s" % status)
        try:
            status, _ = mailbox.uid("EXPUNGE", uid_bytes)
        except Exception as exc:
            status = "ERROR: %s" % exc
        if imap_status_ok(status):
            LOGGER.info("archived IMAP UID %s to %s via COPY/UID EXPUNGE fallback", uid_text, target_folder)
            return
        status, _ = mailbox.expunge()
        if not imap_status_ok(status):
            raise RuntimeError("IMAP EXPUNGE after archive copy failed: %s" % status)
        LOGGER.info("archived IMAP UID %s to %s via COPY/EXPUNGE fallback", uid_text, target_folder)

    def ingest_raw(self, raw_bytes: bytes, folder: str = "INBOX") -> IngestResult:
        parsed = email.message_from_bytes(raw_bytes)
        message_id = json_safe(message_id_for(raw_bytes, parsed, message_id_domain(self.config)))
        existing = self.db.fetch_one("SELECT id, is_actionable FROM emails WHERE message_id = %s", (message_id,))
        if existing:
            return IngestResult(inserted=False, queued=bool(existing.get("is_actionable")), message_id=message_id)

        subject = json_safe(decode_mime_header(parsed.get("Subject")) or "")
        in_reply_to = json_safe(parsed.get("In-Reply-To"))
        references = json_safe(parse_reference_header(parsed.get("References")))
        from_addresses = json_safe(parse_addresses(parsed.get("From")))
        to_addresses = json_safe(parse_addresses(parsed.get("To")))
        cc_addresses = json_safe(parse_addresses(parsed.get("Cc")))
        body_text, body_html, attachments = extract_bodies_and_attachments(parsed, self.attachment_root, message_id)
        body_text = strip_disclosure_text(body_text)
        body_html = strip_disclosure_html(body_html)
        body_text = json_safe(body_text)
        body_html = json_safe(body_html)
        thread_id = self.resolve_thread_id(
            message_id,
            in_reply_to,
            references,
            subject,
            from_addresses,
        )
        actionable = self.is_actionable(from_addresses, thread_id)

        row = self.db.fetch_one(
            """
            INSERT INTO emails(
              message_id,
              in_reply_to,
              references_header,
              thread_id,
              from_address,
              to_addresses,
              cc_addresses,
              subject,
              body_text,
              body_html,
              attachments,
              received_at,
              folder,
              is_actionable
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (message_id) DO NOTHING
            RETURNING *
            """,
            (
                message_id,
                in_reply_to,
                references,
                thread_id,
                from_addresses[0] if from_addresses else "unknown@unknown",
                to_addresses,
                cc_addresses,
                subject,
                body_text,
                body_html,
                Jsonb(json_safe(attachments)),
                message_datetime(parsed.get("Date")),
                folder,
                actionable,
            ),
        )
        if row is None:
            return IngestResult(inserted=False, queued=False, message_id=message_id)

        self.process_artifacts(row, attachments, body_text, body_html)

        if actionable:
            self.queue_or_update_job(thread_id, subject, row["id"], from_addresses=from_addresses, body_text=body_text)
        return IngestResult(inserted=True, queued=actionable, message_id=message_id)

    def process_artifacts(self, email_row: dict[str, Any], attachments: list[dict[str, Any]], body_text: str, body_html: str) -> None:
        try:
            artifacts = self.artifact_processor.process_email(email_row, attachments, body_text, body_html)
        except Exception:
            LOGGER.exception("artifact processing failed for email %s", email_row.get("id"))
            return
        if artifacts:
            LOGGER.info("processed %s artifact(s) for email %s", len(artifacts), email_row.get("id"))

    def resolve_thread_id(
        self,
        message_id: str,
        in_reply_to: Optional[str],
        references: list[str],
        subject: str,
        from_addresses: list[str],
    ) -> str:
        candidates = self.thread_candidate_message_ids(in_reply_to, references)
        for candidate in candidates:
            row = self.db.fetch_one("SELECT thread_id FROM emails WHERE message_id = %s", (candidate,))
            if row:
                return row["thread_id"]

            row = self.db.fetch_one(
                """
                SELECT j.thread_id
                FROM outbound_email_logs o
                JOIN jobs j ON j.id = o.job_id
                WHERE o.provider_message_id = %s
                  AND o.status = 'sent'
                ORDER BY o.id DESC
                LIMIT 1
                """,
                (candidate,),
            )
            if row:
                return row["thread_id"]

        if self.config.get_bool("agent.email.subject_threading_fallback", False):
            return self.resolve_thread_id_by_subject(message_id, subject, from_addresses)

        return message_id

    def thread_candidate_message_ids(self, in_reply_to: Optional[str], references: list[str]) -> list[str]:
        candidates = []
        if in_reply_to:
            candidates.extend(parse_reference_header(in_reply_to))
        candidates.extend(references)
        result = []
        seen = set()
        for candidate in candidates:
            clean = str(candidate or "").strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            result.append(clean)
        return result

    def resolve_thread_id_by_subject(self, message_id: str, subject: str, from_addresses: list[str]) -> str:
        normalized = normalize_subject(subject)
        if normalized and from_addresses:
            recent = self.db.fetch_all(
                """
                SELECT thread_id, subject, from_address
                FROM emails
                WHERE received_at > now() - interval '30 days'
                ORDER BY received_at DESC
                LIMIT 200
                """
            )
            sender_set = set(from_addresses)
            for row in recent:
                if normalize_subject(row["subject"]) != normalized:
                    continue
                if row["from_address"] in sender_set:
                    return row["thread_id"]

        return message_id

    def is_actionable(self, from_addresses: list[str], thread_id: str) -> bool:
        open_job = self.db.fetch_one(
            "SELECT id FROM jobs WHERE thread_id = %s AND status IN ('queued', 'running', 'waiting', 'needs_review')",
            (thread_id,),
        )
        if open_job:
            return True

        allowed = [item.lower() for item in self.config.get_list("agent.email.actionable_senders")]
        if "*" in allowed:
            return True
        return any(address.lower() in allowed for address in from_addresses)

    def is_admin_sender(self, from_addresses: list[str]) -> bool:
        """Return True if any sender in from_addresses matches the configured admin email."""
        admin_email = (
            self.config.get("agent.supervisor.admin_email")
            or self.config.get("agent.notifications.email_to")
            or ""
        ).lower().strip()
        if not admin_email:
            return False
        return any(addr.lower().strip() == admin_email for addr in from_addresses)

    def apply_email_review_override(self, job: dict[str, Any], trigger_email_id: int, body_text: str) -> None:
        """Apply an admin email reply as a review override: increase limits and optionally set instruction."""
        job_id = int(job["id"])
        factor = self.config.get_float("agent.supervisor.email_approval_increase_factor", 1.2)

        # Determine current effective limits (override > job.metadata > config defaults).
        metadata = job.get("metadata") or {}
        existing_override = metadata.get("admin_review_override") or {}
        base_iterations = (
            existing_override.get("max_iterations_per_task")
            or self.config.get_int("agent.limits.max_iterations_per_task", 50)
        )
        base_tokens = (
            existing_override.get("max_tokens_per_task")
            or self.config.get_int("agent.limits.max_tokens_per_task", 1000000)
        )
        new_iterations = max(1, int(base_iterations * factor))
        new_tokens = max(1, int(base_tokens * factor))

        instruction = str(body_text or "").strip()

        override: dict[str, Any] = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": "email",
            "reason": str(job.get("last_error") or ""),
            "max_iterations_per_task": new_iterations,
            "max_tokens_per_task": new_tokens,
        }
        if instruction:
            override["instruction"] = instruction

        self.db.execute(
            """
            UPDATE jobs
            SET metadata = metadata || %s,
                status = 'queued',
                run_at = now(),
                locked_at = NULL,
                locked_by = NULL,
                last_error = NULL,
                has_new_context = true,
                updated_at = now()
            WHERE id = %s
              AND status = 'needs_review'
            """,
            (Jsonb(json_safe({"admin_review_override": override})), job_id),
        )
        if instruction:
            self.db.execute(
                "INSERT INTO supervisor_instructions(job_id, instruction, created_by) VALUES (%s, %s, %s)",
                (job_id, instruction, "email"),
            )
            self.db.log_event(job_id, "supervisor_note", output_data={"instruction": instruction, "created_by": "email"})
        self.db.log_event(
            job_id,
            "status_change",
            output_data={
                "status": "queued",
                "reason": "email review override",
                "override": override,
                "trigger_email_id": trigger_email_id,
            },
        )
        LOGGER.info(
            "job %s approved via email reply; limits → %s iterations / %s tokens",
            job_id,
            new_iterations,
            new_tokens,
        )

    def queue_or_update_job(
        self,
        thread_id: str,
        subject: str,
        trigger_email_id: int,
        from_addresses: Optional[list[str]] = None,
        body_text: str = "",
    ) -> None:
        resumed_research = self.resume_waiting_deep_research_for_thread(thread_id)
        open_job = self.db.fetch_one(
            "SELECT id, status, metadata, last_error FROM jobs WHERE thread_id = %s AND status IN ('queued', 'running', 'waiting', 'needs_review')",
            (thread_id,),
        )
        if open_job:
            if resumed_research and open_job["id"] == resumed_research["original_job_id"]:
                self.db.execute(
                    """
                    UPDATE jobs
                    SET trigger_email_id = COALESCE(trigger_email_id, %s),
                        has_new_context = true,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (trigger_email_id, open_job["id"]),
                )
                self.db.log_event(
                    open_job["id"],
                    "status_change",
                    output_data={
                        "has_new_context": True,
                        "reason": "new email arrived; resumed waiting deep research",
                        "deep_research_run_id": resumed_research["id"],
                    },
                )
                return
            if open_job["status"] == "needs_review" and from_addresses and self.is_admin_sender(from_addresses):
                self.apply_email_review_override(open_job, trigger_email_id, body_text)
                return
            if open_job["status"] in ("waiting", "needs_review"):
                self.db.execute(
                    """
                    UPDATE jobs
                    SET status = 'queued',
                        trigger_email_id = COALESCE(trigger_email_id, %s),
                        run_at = now(),
                        has_new_context = true,
                        last_error = NULL,
                        locked_at = NULL,
                        locked_by = NULL,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (trigger_email_id, open_job["id"]),
                )
                self.db.log_event(
                    open_job["id"],
                    "status_change",
                    output_data={"status": "queued", "has_new_context": True, "reason": "new email arrived"},
                )
                return
            self.db.execute(
                """
                UPDATE jobs
                SET trigger_email_id = COALESCE(trigger_email_id, %s),
                    has_new_context = true,
                    updated_at = now()
                WHERE id = %s
                """,
                (trigger_email_id, open_job["id"]),
            )
            self.db.log_event(
                open_job["id"],
                "status_change",
                output_data={"has_new_context": True, "reason": "new email arrived"},
            )
            return
        if resumed_research:
            return
        self.db.execute(
            "INSERT INTO jobs(thread_id, task_summary, trigger_email_id) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (thread_id, subject, trigger_email_id),
        )

    def resume_waiting_deep_research_for_thread(self, thread_id: str) -> Optional[dict[str, Any]]:
        run = self.db.fetch_one(
            """
            SELECT id, original_job_id
            FROM deep_research_runs
            WHERE original_thread_id = %s
              AND status = 'waiting_for_input'
            ORDER BY waiting_since DESC NULLS LAST, id DESC
            LIMIT 1
            """,
            (thread_id,),
        )
        if run is None:
            return None
        self.db.execute(
            """
            UPDATE deep_research_runs
            SET status = 'queued',
                run_at = now(),
                waiting_since = NULL,
                last_error = NULL,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (run["id"],),
        )
        self.log_deep_research_event(run["id"], "status_change", output_data={"status": "queued", "reason": "new email arrived"})
        return run

    def log_deep_research_event(
        self,
        run_id: int,
        event_type: str,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
        tool_name: Optional[str] = None,
    ) -> None:
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM deep_research_events WHERE run_id = %s", (run_id,))
                sequence = cur.fetchone()["next_sequence"]
                cur.execute(
                    """
                    INSERT INTO deep_research_events(run_id, sequence, event_type, tool_name, input_data, output_data)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        sequence,
                        event_type,
                        tool_name,
                        Jsonb(json_safe(input_data)) if input_data is not None else None,
                        Jsonb(json_safe(output_data)) if output_data is not None else None,
                    ),
                )
