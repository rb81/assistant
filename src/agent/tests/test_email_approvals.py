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
    MARKER, ApprovalDeliveryError, EmailApprovals, admin_sender, approval_reply, send_admin_email,
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

    def test_credentials_in_html_are_found_even_when_plain_alternative_omits_quote(self):
        message = reply("Continue.")
        message.add_alternative(
            "<div>Continue.</div><blockquote>" + self.confirmation.replace("\n", "<br>") + "</blockquote>",
            subtype="html",
        )
        self.assertEqual(approval_reply(message), ("a" * 32, "b" * 64, "Continue."))

    def test_duplicate_credentials_in_both_alternatives_approve_once_without_duplicate_guidance(self):
        message = reply("Continue.\n\n" + self.confirmation)
        message.add_alternative(
            "<div>Continue.</div><blockquote>" + self.confirmation.replace("\n", "<br>") + "</blockquote>",
            subtype="html",
        )
        self.assertEqual(approval_reply(message), ("a" * 32, "b" * 64, "Continue."))

    def test_conflicting_credentials_across_alternatives_fail_closed(self):
        message = reply(self.confirmation)
        message.add_alternative(self.confirmation.replace("a" * 32, "c" * 32).replace("\n", "<br>"), subtype="html")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            approval_reply(message)

    def test_ingest_html_only_credentials_resume_without_starting_another_approval(self):
        from assistant_agent.email_ingest import EmailDownloader
        downloader = EmailDownloader.__new__(EmailDownloader)
        downloader.config, downloader.db = self.config, MagicMock()
        job = {"id": 12, "updated_at": "2026-10-28T17:00:00+00:00", "metadata": {}}
        downloader.db.fetch_one.side_effect = [None, job, {"id": 12}]
        message = reply("Continue.")
        message.add_alternative(
            "<div>Continue.</div><blockquote>" + self.confirmation.replace("\n", "<br>") + "</blockquote>",
            subtype="html",
        )
        payload = {"kind": "review", "job_id": 12, "version": job["updated_at"]}
        with patch("assistant_agent.email_approvals.EmailApprovals.consume", return_value=payload) as consume, patch(
            "assistant_agent.email_approvals.EmailApprovals.request"
        ) as another_request:
            result = downloader.ingest_raw(message.as_bytes())
        self.assertTrue(result.inserted)
        consume.assert_called_once_with("a" * 32, "b" * 64)
        self.assertIn("status = 'queued'", downloader.db.fetch_one.call_args.args[0])
        another_request.assert_not_called()

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

    def test_missing_approval_credentials_never_start_another_challenge(self):
        from assistant_agent.email_ingest import EmailDownloader
        for subject in ("Re: Assistant approval required", "Re: Re: Assistant approval required",
                        "Re: Salma admin input required for job #12"):
            with self.subTest(subject=subject):
                downloader = EmailDownloader.__new__(EmailDownloader)
                downloader.config = self.config
                downloader.db = MagicMock()
                downloader.db.fetch_one.return_value = None
                message = reply("Continue.")
                message.replace_header("Subject", subject)
                with patch("assistant_agent.email_approvals.EmailApprovals.request") as request, patch(
                    "assistant_agent.email_approvals.EmailApprovals.consume"
                ) as consume, patch("assistant_agent.email_ingest.RequestService.receive_request") as receive:
                    result = downloader.ingest_raw(message.as_bytes())
                self.assertTrue(result.quarantine)
                self.assertFalse(result.queued)
                request.assert_not_called()
                consume.assert_not_called()
                receive.assert_not_called()

    def test_admin_input_email_contains_usable_one_step_input_confirmation(self):
        from assistant_agent.email_ingest import EmailDownloader
        from assistant_agent.task_agent import TaskAgent
        agent = TaskAgent.__new__(TaskAgent)
        agent.config, agent.db = self.config, MagicMock()
        version = "2026-10-28T17:00:00+00:00"
        agent.db.fetch_one.return_value = {"updated_at": version}
        with patch("assistant_agent.task_agent.approvals_configured", return_value=True), patch(
            "assistant_agent.task_agent.smtp_configured", return_value=True
        ), patch("assistant_agent.task_agent.latest_request_context", return_value={}), patch(
            "assistant_agent.email_approvals.send_admin_email"
        ) as send:
            agent.email_admin_for_input({"id": 12}, MagicMock(), "May I continue?")
        body = send.call_args.args[2]
        self.assertTrue(body.startswith(MARKER + "\nOTP: "))
        self.assertIsNotNone(approval_reply(reply(body)))
        self.assertIn("May I continue?", body)
        pending = agent.db.set_state.call_args_list[0].args[1]["payload"]
        self.assertEqual(pending, {"kind": "input", "job_id": 12, "version": version})
        downloader = EmailDownloader.__new__(EmailDownloader)
        downloader.config, downloader.db = self.config, MagicMock()
        job = {"id": 12, "updated_at": version, "metadata": {}, "last_error": "May I continue?"}
        downloader.db.fetch_one.side_effect = [None, job, {"id": 12}]
        with patch("assistant_agent.email_approvals.EmailApprovals.consume", return_value=pending), patch(
            "assistant_agent.email_approvals.EmailApprovals.request"
        ) as another_request, patch("assistant_agent.admin_commands.send_admin_email") as acknowledgment, patch(
            "assistant_agent.approval_actions.TransactionDatabase"
        ) as transaction:
            result = downloader.ingest_raw(reply("Continue.\n\n" + body).as_bytes())
        self.assertFalse(result.queued)
        self.assertIn("status = 'queued'", transaction.return_value.fetch_one.call_args.args[0])
        self.assertNotIn("admin_review_override", transaction.return_value.fetch_one.call_args.args[0])
        another_request.assert_not_called()
        acknowledgment.assert_not_called()

    def test_admin_input_confirmation_in_both_mode_uses_same_email_and_telegram_record(self):
        from assistant_agent.task_agent import TaskAgent
        agent = TaskAgent.__new__(TaskAgent)
        agent.config, agent.db = self.config, MagicMock()
        self.config.values["agent"].update({
            "approvals": {"channel": "both"},
            "telegram": {"allowed_user_ids": ["123"], "recipients": {"admin@example.com": 123}},
        })
        agent.db.fetch_one.return_value = {"updated_at": "version"}
        with patch("assistant_agent.task_agent.approvals_configured", return_value=True), patch(
            "assistant_agent.task_agent.latest_request_context", return_value={}
        ), patch("assistant_agent.email_approvals.send_admin_email") as send, patch(
            "assistant_agent.telegram.TelegramClient"
        ) as telegram:
            agent.email_admin_for_input({"id": 12}, MagicMock(), "May I continue?")
        body = send.call_args.args[2]
        token, digest, guidance = approval_reply(reply("Continue with the saved audio.\n\n" + body))
        record = agent.db.set_state.call_args_list[0]
        self.assertEqual(record.args[0], "email-approval:" + digest)
        self.assertEqual(record.args[1]["payload"], {"kind": "input", "job_id": 12, "version": "version"})
        self.assertEqual(record.args[1]["telegram_admin_ids"], ["123"])
        self.assertEqual(guidance, "Continue with the saved audio.")
        self.assertEqual(len(token), 32)
        keyboard = telegram.return_value.call.call_args.args[1]["reply_markup"]["inline_keyboard"]
        self.assertIn(record.args[1]["callback_id"], keyboard[0][0]["callback_data"])

    def test_job_review_approval_resumes_and_logs_without_acknowledgment_email(self):
        from assistant_agent.email_ingest import EmailDownloader
        downloader = EmailDownloader.__new__(EmailDownloader)
        downloader.config = self.config
        downloader.db = MagicMock()
        job = {"id": 12, "updated_at": "2026-10-28T17:00:00+00:00",
               "last_error": "Tool failed three times", "metadata": {}}
        downloader.db.fetch_one.side_effect = [None, job, {"id": 12}]
        payload = {"kind": "review", "job_id": 12, "version": job["updated_at"]}
        with patch("assistant_agent.email_approvals.EmailApprovals.consume", return_value=payload), patch(
            "assistant_agent.admin_commands.send_admin_email"
        ) as send, patch("assistant_agent.email_ingest.RequestService.receive_request") as receive:
            result = downloader.ingest_raw(reply("Try a different approach.\n\n" + self.confirmation).as_bytes())
        self.assertTrue(result.inserted)
        self.assertFalse(result.queued)
        update = downloader.db.fetch_one.call_args
        self.assertIn("status = 'queued'", update.args[0])
        self.assertEqual(update.args[1][1], 12)
        self.assertEqual(update.args[1][0].obj["admin_review_override"]["instruction"], "Try a different approach.")
        self.assertEqual(downloader.db.log_event.call_args.args, (12, "status_change"))
        send.assert_not_called()
        receive.assert_not_called()

    def test_stale_job_review_approval_does_not_resume_or_acknowledge(self):
        from assistant_agent.email_ingest import EmailDownloader
        downloader = EmailDownloader.__new__(EmailDownloader)
        downloader.config = self.config
        downloader.db = MagicMock()
        downloader.db.fetch_one.return_value = None
        payload = {"kind": "review", "job_id": 12, "version": "2026-10-28T17:00:00+00:00"}
        with patch("assistant_agent.email_approvals.EmailApprovals.consume", return_value=payload), patch(
            "assistant_agent.admin_commands.send_admin_email"
        ) as send, patch.object(downloader, "apply_email_review_override") as resume:
            result = downloader.ingest_raw(reply(self.confirmation).as_bytes())
        self.assertFalse(result.queued)
        resume.assert_not_called()
        send.assert_not_called()

    def test_explicit_admin_command_still_sends_its_result(self):
        from assistant_agent.email_ingest import EmailDownloader
        downloader = EmailDownloader.__new__(EmailDownloader)
        downloader.config = self.config
        downloader.db = MagicMock()
        downloader.db.fetch_one.return_value = None
        payload = {"kind": "command", "subject": "/admin status", "body": ""}
        with patch("assistant_agent.email_approvals.EmailApprovals.consume", return_value=payload), patch(
            "assistant_agent.admin_commands.AdminCommands.execute", return_value="Status report"
        ) as execute, patch("assistant_agent.admin_commands.send_admin_email") as send:
            result = downloader.ingest_raw(reply(self.confirmation).as_bytes())
        self.assertFalse(result.queued)
        execute.assert_called_once_with("/admin status", "", confirmed=True)
        send.assert_called_once_with(self.config, "Assistant admin response", "Status report")


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
            with self.assertRaises(ApprovalDeliveryError):
                self.service.request("<failed@example.com>", self.payload, "retry")
        with patch("assistant_agent.email_approvals.send_admin_email") as send:
            self.assertFalse(self.service.request("<failed@example.com>", self.payload, "retry"))
        send.assert_not_called()
        self.assertEqual(self.db.fetch_one(
            "SELECT count(*) AS total FROM runtime_state WHERE key LIKE 'email-approval:%%' AND value->>'source_id' = %s",
            ("<failed@example.com>",),
        )["total"], 1)

    def test_both_channels_share_atomic_consumption(self):
        self.config.values["agent"]["approvals"] = {"channel": "both"}
        self.config.values["agent"]["telegram"] = {"allowed_user_ids": ["123"],
                                                  "recipients": {"admin@example.com": 123}}
        with patch("assistant_agent.telegram.TelegramClient"), patch(
            "assistant_agent.email_approvals.send_admin_email"
        ) as send:
            self.service.request("both:first", self.payload, "Cancel task")
        message = reply(send.call_args.args[2])
        token, digest, _ = approval_reply(message)
        pending = self.db.get_state("email-approval:" + digest)
        self.assertEqual(self.service.consume_telegram(pending["callback_id"], "123"), self.payload)
        with self.assertRaises(ValueError):
            self.service.consume(token, digest)
        with patch("assistant_agent.telegram.TelegramClient"), patch(
            "assistant_agent.email_approvals.send_admin_email"
        ) as send:
            self.service.request("both:second", self.payload, "Cancel task")
        token, digest, _ = approval_reply(reply(send.call_args.args[2]))
        pending = self.db.get_state("email-approval:" + digest)
        self.assertEqual(self.service.consume(token, digest), self.payload)
        with self.assertRaises(ValueError):
            self.service.consume_telegram(pending["callback_id"], "123")

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
