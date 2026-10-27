import unittest
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

class AdminCommandTest(unittest.TestCase):
    def setUp(self):
        global AdminCommands, admin_sender
        from assistant_agent.admin_commands import AdminCommands
        from assistant_agent.email_approvals import admin_sender
        from assistant_agent.config import AppConfig

        self.config = AppConfig({"agent": {"admin": {"email": "admin@example.com"}}})
        self.message = EmailMessage()
        self.message["From"] = "admin@example.com"
        self.message["Message-ID"] = "<admin-test@example.com>"
        self.message["Authentication-Results"] = "receiver.example; dmarc=pass header.from=example.com"

    def test_from_match_only_requests_confirmation_without_executing(self):
        service = AdminCommands(MagicMock(), self.config)
        with patch("assistant_agent.admin_commands.EmailApprovals.request", return_value=True) as request, patch.object(service, "execute") as execute:
            self.assertTrue(service.handle(self.message, "/admin cancel 1", ""))
        request.assert_called_once_with("<admin-test@example.com>", {"kind": "command", "subject": "/admin cancel 1", "body": ""}, "/admin cancel 1\n\n")
        execute.assert_not_called()

    def test_help_and_errors_do_not_call_models_or_create_jobs(self):
        db = MagicMock()
        service = AdminCommands(db, self.config)
        with patch("assistant_agent.llm_client.LlmClient.chat") as chat, patch("assistant_agent.embedding_client.EmbeddingClient.embed") as embed:
            self.assertIn("/admin logs", service.execute("/admin"))
            with self.assertRaises(ValueError):
                service.execute("/admin not-a-command")
            chat.assert_not_called()
            embed.assert_not_called()
            db.execute.assert_not_called()

    def test_logs_paginate_by_sequence(self):
        db = MagicMock()
        db.fetch_all.return_value = [{"sequence": 9, "event_type": "tool_result"}]
        response = AdminCommands(db, self.config).execute("/admin logs 42 --cursor 8 --limit 1")
        self.assertIn("/admin logs 42 --cursor 9 --limit 1", response)
        self.assertEqual(db.fetch_all.call_args.args[1], (42, 8, 1))

    def test_other_sender_cannot_request_admin_commands(self):
        self.message.replace_header("From", "other@example.com")
        with patch("assistant_agent.admin_commands.EmailApprovals.request") as request:
            self.assertFalse(AdminCommands(MagicMock(), self.config).handle(self.message, "/admin", ""))
        request.assert_not_called()

    def test_deletion_requires_confirmation_without_loading_content(self):
        db = MagicMock()
        service = AdminCommands(db, self.config)
        response = service.execute("/admin memory delete 7")
        self.assertIn("--yes", response)
        db.fetch_one.assert_not_called()

    def test_requeue_requires_confirmation_and_uses_shared_status_rules(self):
        db = MagicMock()
        service = AdminCommands(db, self.config)
        self.assertIn("--yes", service.execute("/admin requeue 5"))
        db.requeue_job.assert_not_called()
        db.requeue_job.return_value = True
        self.assertIn('"queued"', service.execute("/admin requeue 5", confirmed=True))
        db.requeue_job.assert_called_once_with(5, "requeued by admin command")
        db.requeue_job.return_value = False
        with self.assertRaisesRegex(ValueError, "cannot be requeued"):
            service.execute("/admin requeue 6", confirmed=True)

    def test_instruct_requires_body_and_existing_job(self):
        db = MagicMock()
        service = AdminCommands(db, self.config)
        with self.assertRaisesRegex(ValueError, "instruction text"):
            service.execute("/admin instruct 5", " ")
        db.fetch_one.return_value = None
        with self.assertRaisesRegex(ValueError, "job not found"):
            service.execute("/admin instruct 5", "Use the newer file")
        db.fetch_one.return_value = {"id": 5}
        db.add_job_instruction.return_value = "requeued"
        self.assertIn('"requeued"', service.execute("/admin instruct 5", " Use the newer file "))
        db.add_job_instruction.assert_called_once_with(5, "Use the newer file", "admin")

    def test_local_cli_yes_skips_token_and_reports_errors_without_traceback(self):
        from assistant_agent import admin_commands
        db = MagicMock()
        db.requeue_job.return_value = True
        with patch.object(admin_commands, "load_config", return_value=self.config), patch.object(admin_commands, "database_url", return_value="postgresql://unused"), patch.object(admin_commands, "Database", return_value=db), patch("sys.argv", ["admin", "/admin requeue 3", "--yes"]), patch("builtins.print") as printed:
            admin_commands.main()
        self.assertIn('"queued"', printed.call_args.args[0])
        with patch.object(admin_commands, "load_config", return_value=self.config), patch.object(admin_commands, "database_url", return_value="postgresql://unused"), patch.object(admin_commands, "Database", return_value=db), patch("sys.argv", ["admin", "/admin bogus"]):
            with self.assertRaisesRegex(SystemExit, "Command error"):
                admin_commands.main()

    def test_reply_to_and_auto_generated_messages_cannot_redirect_admin_access(self):
        self.message["Reply-To"] = "attacker@example.com"
        self.message["Auto-Submitted"] = "auto-replied"
        self.assertFalse(admin_sender(self.message, self.config))

    def test_stale_job_snapshot_cannot_authorize_memory_deletion(self):
        from assistant_agent.tools import ToolRuntime, ToolError
        runtime = ToolRuntime.__new__(ToolRuntime)
        runtime.db = MagicMock()
        runtime.db.fetch_one.return_value = {"metadata": {"admin_authenticated": False}}
        runtime.job = {"id": 1, "metadata": {"admin_authenticated": True}}
        runtime.config = self.config
        with self.assertRaisesRegex(ToolError, "authenticated admin"):
            runtime.memory_forget(7)