import os
import smtplib
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

from assistant_agent.config import AppConfig
from assistant_agent.email_approvals import (
    MARKER, EmailApprovals, admin_sender, approval_reply, send_admin_email,
)


def reply(body, sender="admin@example.com", html=False):
    message = EmailMessage()
    message["From"] = sender
    message["Message-ID"] = "<reply@example.com>"
    message["Subject"] = "Re: Assistant approval required"
    message.set_content(body, subtype="html" if html else "plain")
    return message


class ApprovalReplyTest(unittest.TestCase):
    def setUp(self):
        self.config = AppConfig({"agent": {"admin": {"email": "admin@example.com"}}})
        self.confirmation = "%s\nOTP: %s\nRequest hash: %s\n" % (MARKER, "a" * 32, "b" * 64)

    def test_extracts_only_new_guidance_from_plain_and_html_replies(self):
        for body, html in (
            ("Use the new file.\n\nOn Tuesday, Assistant wrote:\n" + "\n".join("> " + line for line in self.confirmation.splitlines()), False),
            ("Use the new file.\n\nOn Tuesday,\nAssistant wrote:\n> " + self.confirmation, False),
            ("Use the new file.\n\n" + self.confirmation, False),
            ("<div>Use the new file.</div><blockquote>" + self.confirmation.replace("\n", "<br>") + "</blockquote>", True),
            ('<div>Use the new file.</div><div id="divRplyFwdMsg">From: Assistant</div>' + self.confirmation.replace("\n", "<br>"), True),
        ):
            with self.subTest(html=html, body=body[:40]):
                self.assertEqual(approval_reply(reply(body, html=html)), ("a" * 32, "b" * 64, "Use the new file."))
        self.assertEqual(approval_reply(reply(self.confirmation))[2], "")
        self.assertEqual(approval_reply(reply(self.confirmation.replace("a" * 32, "-" * 32)))[0], "-" * 32)

    def test_signatures_and_attachments_do_not_become_instructions_or_credentials(self):
        message = reply("Continue.\n-- \nMy signature\n" + self.confirmation)
        self.assertEqual(approval_reply(message)[2], "Continue.")
        message = reply("An ordinary task")
        message.add_attachment(self.confirmation.encode(), maintype="text", subtype="plain", filename="old.txt")
        message.add_attachment(reply(self.confirmation))
        self.assertIsNone(approval_reply(message))

    def test_multiple_or_incomplete_challenges_fail_closed(self):
        for body in (MARKER, self.confirmation + "\nOTP: " + "c" * 32):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    approval_reply(reply(body))

    def test_sender_filter_rejects_multiple_senders_and_automation(self):
        for sender in ("attacker@example.com", "admin@example.com, attacker@example.com"):
            self.assertFalse(admin_sender(reply(self.confirmation, sender), self.config))
        automated = reply(self.confirmation)
        automated["Auto-Submitted"] = "auto-replied"
        self.assertFalse(admin_sender(automated, self.config))

    def test_standalone_delivery_uses_configured_admin_and_tls(self):
        config = AppConfig({"agent": {"admin": {"email": "admin@example.com"}, "email": {"smtp_port": 587}}})
        with patch("assistant_agent.email_approvals.smtplib.SMTP") as smtp:
            send_admin_email(config, "Approval", self.confirmation)
        client = smtp.return_value
        message = client.send_message.call_args.args[0]
        self.assertEqual(message["To"], "admin@example.com")
        for header in ("In-Reply-To", "References", "Reply-To"):
            self.assertIsNone(message[header])
        client.starttls.assert_called_once()

    def test_ingest_rejects_forwarded_challenge_before_model_or_artifact_processing(self):
        from assistant_agent.email_ingest import EmailDownloader
        downloader = EmailDownloader.__new__(EmailDownloader)
        downloader.config = self.config
        downloader.db = MagicMock()
        downloader.db.fetch_one.return_value = None
        with patch("assistant_agent.email_approvals.EmailApprovals.consume") as consume:
            result = downloader.ingest_raw(reply(self.confirmation, "attacker@example.com").as_bytes())
        self.assertFalse(result.queued)
        consume.assert_not_called()
        downloader.db.execute.assert_not_called()

    def test_automation_cannot_bypass_confirmation_by_becoming_an_ordinary_task(self):
        from assistant_agent.email_ingest import EmailDownloader
        downloader = EmailDownloader.__new__(EmailDownloader)
        downloader.config = self.config
        downloader.db = MagicMock()
        downloader.db.fetch_one.return_value = None
        automated = reply("Run this task")
        automated["Auto-Submitted"] = "auto-generated"
        with patch.object(downloader, "resolve_thread_id", return_value="thread"), patch.object(
            downloader, "is_actionable", return_value=True
        ), patch("assistant_agent.email_ingest.RequestService.receive_request") as receive:
            result = downloader.ingest_raw(automated.as_bytes())
        self.assertFalse(result.queued)
        receive.assert_not_called()


@unittest.skipUnless(os.getenv("ASSISTANT_TEST_DATABASE_URL"), "requires disposable assistant_test database")
class ApprovalDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import make_conninfo
        from assistant_agent.database import Database
        cls.original_db = Database(os.environ["ASSISTANT_TEST_DATABASE_URL"])
        cls.schema = "test_" + uuid.uuid4().hex
        with cls.original_db.connect() as conn:
            if conn.info.dbname != "assistant_test":
                raise RuntimeError("integration tests require a disposable database named assistant_test")
            conn.execute("CREATE SCHEMA " + cls.schema)
        cls.db = Database(make_conninfo(os.environ["ASSISTANT_TEST_DATABASE_URL"], options="-c search_path=" + cls.schema))
        cls.db.execute("CREATE TABLE runtime_state (key text PRIMARY KEY, value jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())")

    @classmethod
    def tearDownClass(cls):
        cls.original_db.execute("DROP SCHEMA " + cls.schema + " CASCADE")

    def setUp(self):
        self.db.execute("TRUNCATE runtime_state")
        self.config = AppConfig({"agent": {"admin": {"email": "admin@example.com"}}})
        self.service = EmailApprovals(self.db, self.config)
        self.payload = {"kind": "command", "subject": "/admin cancel 7", "body": ""}

    def request(self, source="<original@example.com>"):
        with patch("assistant_agent.email_approvals.send_admin_email") as send:
            self.assertTrue(self.service.request(source, self.payload, "/admin cancel 7"))
        self.confirmation = send.call_args.args[2]
        token, digest, _ = approval_reply(reply(self.confirmation))
        return token, digest

    def test_mismatch_expiry_and_replay_cannot_execute_and_wrong_token_does_not_consume(self):
        token, digest = self.request()
        with self.assertRaises(ValueError):
            self.service.consume("z" * 32, digest)
        with self.assertRaises(ValueError):
            self.service.consume(token, "0" * 64)
        self.assertEqual(self.service.consume(token, digest), self.payload)
        with self.assertRaises(ValueError):
            self.service.consume(token, digest)
        token, digest = self.request("<expired@example.com>")
        self.db.execute("UPDATE runtime_state SET value = jsonb_set(value, '{expires}', to_jsonb((now() - interval '1 second')::text)) WHERE key = %s", ("email-approval:" + digest,))
        with self.assertRaises(ValueError):
            self.service.consume(token, digest)

    def test_changed_admin_or_pending_action_fails_closed(self):
        token, digest = self.request()
        changed_admin = EmailApprovals(self.db, AppConfig({"agent": {"admin": {"email": "other@example.com"}}}))
        with self.assertRaises(ValueError):
            changed_admin.consume(token, digest)
        self.db.execute("UPDATE runtime_state SET value = jsonb_set(value, '{payload,subject}', '\"/admin cancel 99\"') WHERE key = %s", ("email-approval:" + digest,))
        with self.assertRaisesRegex(ValueError, "content has changed"):
            self.service.consume(token, digest)

    def test_concurrent_replies_consume_once(self):
        token, digest = self.request()

        def consume(_):
            try:
                return self.service.consume(token, digest)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(consume, range(4)))
        self.assertEqual(results.count(self.payload), 1)

    def test_duplicate_requests_delivery_failure_and_stale_send_recovery(self):
        old_token, old_digest = self.request()
        with patch("assistant_agent.email_approvals.send_admin_email") as send:
            self.assertFalse(self.service.request("<original@example.com>", self.payload, "duplicate"))
            send.assert_not_called()
        self.db.execute("UPDATE runtime_state SET value = '{\"status\":\"sending\"}', updated_at = now() - interval '6 minutes' WHERE key LIKE 'admin-request:%%'")
        token, digest = self.request()
        with self.assertRaises(ValueError):
            self.service.consume(old_token, old_digest)
        self.assertEqual(self.service.consume(token, digest), self.payload)
        with patch("assistant_agent.email_approvals.send_admin_email", side_effect=smtplib.SMTPException("delivery failed")):
            with self.assertRaises(smtplib.SMTPException):
                self.service.request("<failed@example.com>", self.payload, "retry")
        self.request("<failed@example.com>")

    def test_rate_limit_is_atomic_and_bounds_confirmation_floods(self):
        with patch("assistant_agent.email_approvals.send_admin_email") as send:
            for index in range(10):
                self.service.request(str(index), self.payload, "request")
            with self.assertRaisesRegex(ValueError, "rate limit"):
                self.service.request("overflow", self.payload, "request")
        self.assertEqual(send.call_count, 10)

    def test_command_is_executed_only_after_confirmation_with_no_model_calls(self):
        from assistant_agent.admin_commands import AdminCommands
        from assistant_agent.email_ingest import EmailDownloader
        original = reply("Cancel it")
        original.replace_header("Subject", "/admin cancel 7")
        with patch("assistant_agent.email_approvals.send_admin_email") as send, patch.object(AdminCommands, "execute") as execute:
            AdminCommands(self.db, self.config).handle(original, "/admin cancel 7", "Cancel it")
            execute.assert_not_called()
        confirmation = send.call_args.args[2]
        downloader = EmailDownloader.__new__(EmailDownloader)
        downloader.config = self.config
        downloader.db = MagicMock(wraps=self.db)
        downloader.db.fetch_one.side_effect = lambda sql, params=(): None if "FROM emails" in sql else self.db.fetch_one(sql, params)
        with patch.object(AdminCommands, "execute", return_value="cancelled") as execute, patch("assistant_agent.admin_commands.send_admin_email"), patch("assistant_agent.llm_client.LlmClient.chat") as chat:
            result = downloader.ingest_raw(reply("Do not change the target.\n\n" + confirmation).as_bytes())
        execute.assert_called_once_with("/admin cancel 7", "Cancel it", confirmed=True)
        self.assertFalse(result.queued)
        chat.assert_not_called()

    def test_ordinary_admin_email_waits_then_replays_original_without_credentials(self):
        from assistant_agent.email_ingest import EmailDownloader
        downloader = EmailDownloader.__new__(EmailDownloader)
        downloader.config = self.config
        downloader.db = MagicMock(wraps=self.db)
        downloader.db.execute = MagicMock()

        def fetch(sql, params=()):
            if "FROM emails" in sql or "FROM jobs" in sql:
                return None
            if "INSERT INTO emails" in sql:
                return {"id": 5}
            return self.db.fetch_one(sql, params)

        downloader.db.fetch_one.side_effect = fetch
        original = reply("Summarize the attached report.")
        original.replace_header("Subject", "Summarize report")
        original.add_attachment(b"report", maintype="application", subtype="pdf", filename="report.pdf")
        with tempfile.TemporaryDirectory() as root:
            downloader.attachment_root = Path(root)
            with patch.object(downloader, "resolve_thread_id", return_value="original-thread"), patch.object(
                downloader, "is_actionable", return_value=True
            ), patch.object(downloader, "process_artifacts") as artifacts, patch(
                "assistant_agent.email_ingest.RequestService.receive_request"
            ) as receive, patch("assistant_agent.email_approvals.send_admin_email") as send:
                pending = downloader.ingest_raw(original.as_bytes())
                self.assertFalse(pending.queued)
                receive.assert_not_called()
                artifacts.assert_not_called()
                self.assertEqual(list(Path(root).iterdir()), [])
                confirmation = send.call_args.args[2]
                result = downloader.ingest_raw(reply("Use bullet points.\n\n" + confirmation).as_bytes())
                attachments = artifacts.call_args.args[1]
                self.assertEqual(len(attachments), 1)
                self.assertEqual(Path(attachments[0]["path"]).read_bytes(), b"report")
        self.assertTrue(result.queued)
        request = receive.call_args.args[0]
        self.assertEqual(request.thread_id, "original-thread")
        self.assertEqual(request.body, "Summarize the attached report.\n\nAdmin instruction:\nUse bullet points.")
        self.assertTrue(receive.call_args.kwargs["admin_authenticated"])
        self.assertNotIn("OTP:", request.body)
        self.assertNotIn("Request hash:", request.body)
