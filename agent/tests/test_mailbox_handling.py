import sys
import types
import unittest
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda handle: {}))

psycopg_module = types.ModuleType("psycopg")
psycopg_module.connect = lambda *args, **kwargs: None
psycopg_module.Connection = object
sys.modules.setdefault("psycopg", psycopg_module)
rows_module = types.ModuleType("psycopg.rows")
rows_module.dict_row = object()
sys.modules.setdefault("psycopg.rows", rows_module)
json_module = types.ModuleType("psycopg.types.json")
json_module.Jsonb = lambda value: value
sys.modules.setdefault("psycopg.types", types.ModuleType("psycopg.types"))
sys.modules.setdefault("psycopg.types.json", json_module)

markdown_module = types.ModuleType("markdown_it")


class FakeMarkdownIt:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.enabled: set[str] = set()
        self.options = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}

    def enable(self, rules: Any, *args: Any, **kwargs: Any) -> "FakeMarkdownIt":
        if isinstance(rules, str):
            self.enabled.add(rules)
        else:
            self.enabled.update(str(rule) for rule in rules)
        return self

    def render(self, value: str) -> str:
        if "table" in self.enabled and "| Name | Value |" in value:
            return "<table><thead><tr><th>Name</th><th>Value</th></tr></thead><tbody><tr><td>A</td><td>1</td></tr></tbody></table>"
        if self.options.get("breaks"):
            value = value.replace("\n", "<br>\n")
        return "<p>%s</p>" % value


markdown_module.MarkdownIt = FakeMarkdownIt
sys.modules.setdefault("markdown_it", markdown_module)

from assistant_agent.config import AppConfig
from assistant_agent.email_disclosure import append_disclosure_html, strip_disclosure_html, strip_disclosure_text
from assistant_agent.email_ingest import EmailDownloader, IngestResult
from assistant_agent.imap_utils import imap_mailbox_arg, imap_status_ok
from assistant_agent.tools import ToolRuntime


class FakeStateDatabase:
    def __init__(self, last_uid: int = 0) -> None:
        self.state: dict[str, Any] = {}
        self.last_uid = last_uid

    def get_state(self, key: str, default: Any = None) -> Any:
        return self.state.get(key, self.last_uid if key.endswith(":last_uid") else default)

    def set_state(self, key: str, value: Any) -> None:
        self.state[key] = value


class FakeEmailDatabase:
    def __init__(self, body: str) -> None:
        self.body = body

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
        return {
            "id": params[0],
            "message_id": "<email@example.com>",
            "thread_id": "thread-1",
            "from_address": "user@example.com",
            "to_addresses": ["agent@example.com"],
            "cc_addresses": [],
            "subject": "Hello",
            "body_text": self.body,
            "body_html": "<p>full html should not leak during paging</p>",
            "attachments": [],
            "received_at": "2026-06-06T10:00:00Z",
        }

    def processed_artifacts_for_email(self, email_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return []


class FakeQueueDatabase:
    def __init__(self, open_job: Optional[dict[str, Any]] = None) -> None:
        self.open_job = open_job
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.logged: list[tuple[int, str, Optional[dict[str, Any]]]] = []

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        if "FROM deep_research_runs" in sql:
            return None
        if "FROM jobs" in sql:
            return self.open_job
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executed.append((sql, params))

    def log_event(
        self,
        job_id: int,
        event_type: str,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
        tool_name: Optional[str] = None,
        tool_action: Optional[str] = None,
        tokens_used: Optional[dict[str, Any]] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        self.logged.append((job_id, event_type, output_data))


class FakeThreadDatabase:
    def __init__(
        self,
        emails: Optional[list[dict[str, Any]]] = None,
        outbound_threads: Optional[dict[str, str]] = None,
    ) -> None:
        self.emails = emails or []
        self.outbound_threads = outbound_threads or {}

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        if "FROM emails" in sql and "message_id = %s" in sql:
            message_id = params[0]
            for email in self.emails:
                if email["message_id"] == message_id:
                    return {"thread_id": email["thread_id"]}
            return None
        if "FROM outbound_email_logs" in sql:
            thread_id = self.outbound_threads.get(params[0])
            return {"thread_id": thread_id} if thread_id else None
        return None

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return list(self.emails)


class FakeDownloader(EmailDownloader):
    def __init__(self, db: FakeStateDatabase, config: AppConfig, results: list[IngestResult]) -> None:
        super().__init__(db, config)  # type: ignore[arg-type]
        self.results = list(results)
        self.ingested: list[tuple[bytes, str]] = []

    def ingest_raw(self, raw_bytes: bytes, folder: str = "INBOX") -> IngestResult:
        self.ingested.append((raw_bytes, folder))
        return self.results.pop(0)


class FakeIMAP:
    instances: list["FakeIMAP"] = []
    uids: list[bytes] = []
    raw_by_uid: dict[bytes, bytes] = {}
    move_status: Any = "OK"

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.selected = ""
        self.uid_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.append_calls: list[tuple[Any, ...]] = []
        FakeIMAP.instances.append(self)

    def __enter__(self) -> "FakeIMAP":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        return "OK", [b""]

    def select(self, folder: str) -> tuple[str, list[bytes]]:
        self.selected = folder
        return "OK", [b""]

    def uid(self, command: str, *args: Any) -> tuple[Any, list[Any]]:
        self.uid_calls.append((command, args))
        if command == "search":
            return "OK", [b" ".join(FakeIMAP.uids)]
        if command == "fetch":
            return "OK", [(b"RFC822", FakeIMAP.raw_by_uid[args[0]])]
        if command == "MOVE":
            return FakeIMAP.move_status, [b""]
        raise AssertionError("unexpected IMAP UID command: %s" % command)

    def append(self, *args: Any) -> tuple[str, list[bytes]]:
        self.append_calls.append(args)
        return "OK", [b""]


class FallbackMailbox:
    def __init__(self) -> None:
        self.uid_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.expunge_called = False

    def uid(self, command: str, *args: Any) -> tuple[str, list[bytes]]:
        self.uid_calls.append((command, args))
        if command == "MOVE":
            return "NO", [b""]
        if command in {"COPY", "STORE"}:
            return "OK", [b""]
        if command == "EXPUNGE":
            return "BAD", [b""]
        raise AssertionError("unexpected IMAP UID command: %s" % command)

    def expunge(self) -> tuple[str, list[bytes]]:
        self.expunge_called = True
        return "OK", [b""]


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host: str, port: int, timeout: int) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.logins: list[tuple[str, str]] = []
        self.sent_messages: list[tuple[EmailMessage, Optional[str], Optional[list[str]]]] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def starttls(self, context: Any) -> None:
        self.started_tls = True

    def login(self, username: str, password: str) -> None:
        self.logins.append((username, password))

    def send_message(
        self,
        message: EmailMessage,
        from_addr: Optional[str] = None,
        to_addrs: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        self.sent_messages.append((message, from_addr, to_addrs))
        return {}


class FakeOutboundEmailDatabase:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.inserted: list[tuple[Any, ...]] = []

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
        if "INSERT INTO outbound_email_logs" in sql:
            self.inserted.append(params)
            return {"id": 99}
        if "COUNT(*) AS count" in sql and "FROM outbound_email_logs" in sql:
            return {"count": 0}
        raise AssertionError("unexpected query: %s" % sql)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executed.append((sql, params))

    def latest_thread_emails(self, thread_id: str, limit: int = 1) -> list[dict[str, Any]]:
        return []


class FakeIngestDatabase:
    def __init__(self) -> None:
        self.inserted: list[tuple[Any, ...]] = []

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        if "SELECT id, is_actionable FROM emails" in sql:
            return None
        if "SELECT id FROM jobs" in sql:
            return None
        if "INSERT INTO emails" in sql:
            self.inserted.append(params)
            return {"id": 123}
        raise AssertionError("unexpected query: %s" % sql)


class FakeArtifactProcessor:
    def process_email(
        self,
        email_row: dict[str, Any],
        attachments: list[dict[str, Any]],
        body_text: str,
        body_html: str,
    ) -> list[dict[str, Any]]:
        return []


def mail_config(email_values: dict[str, Any]) -> AppConfig:
    return AppConfig(
        {
            "agent": {
                "email": email_values,
                "artifacts": {"raw_root": "/tmp/assistant-test-artifacts"},
                "filesystem": {"shared_root": "/tmp"},
            }
        }
    )


class MailboxHandlingTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeIMAP.instances = []
        FakeIMAP.uids = []
        FakeIMAP.raw_by_uid = {}
        FakeIMAP.move_status = "OK"
        FakeSMTP.instances = []

    def thread_downloader(self, db: FakeThreadDatabase, subject_fallback: bool = False) -> EmailDownloader:
        downloader = EmailDownloader.__new__(EmailDownloader)
        downloader.db = db  # type: ignore[assignment]
        downloader.config = AppConfig({"agent": {"email": {"subject_threading_fallback": subject_fallback}}})
        return downloader

    def test_imap_helpers_quote_provider_folders_and_normalize_status(self) -> None:
        self.assertEqual(imap_mailbox_arg("INBOX"), "INBOX")
        self.assertEqual(imap_mailbox_arg("[Gmail]/Sent Mail"), '"[Gmail]/Sent Mail"')
        self.assertTrue(imap_status_ok(b" OK "))

    def test_resolve_thread_id_uses_explicit_email_headers(self) -> None:
        downloader = self.thread_downloader(
            FakeThreadDatabase(
                emails=[
                    {
                        "message_id": "<root@example.com>",
                        "thread_id": "thread-1",
                        "subject": "Original",
                        "from_address": "user@example.com",
                    }
                ]
            )
        )

        thread_id = downloader.resolve_thread_id(
            "<new@example.com>",
            "<missing@example.com> <root@example.com>",
            [],
            "Untitled",
            ["user@example.com"],
        )

        self.assertEqual(thread_id, "thread-1")

    def test_resolve_thread_id_uses_outbound_message_id_without_subject_fallback(self) -> None:
        downloader = self.thread_downloader(FakeThreadDatabase(outbound_threads={"<agent@example.com>": "thread-1"}))

        thread_id = downloader.resolve_thread_id(
            "<new@example.com>",
            "<agent@example.com>",
            [],
            "Untitled",
            ["user@example.com"],
        )

        self.assertEqual(thread_id, "thread-1")

    def test_resolve_thread_id_does_not_match_same_subject_by_default(self) -> None:
        downloader = self.thread_downloader(
            FakeThreadDatabase(
                emails=[
                    {
                        "message_id": "<old@example.com>",
                        "thread_id": "thread-1",
                        "subject": "Untitled",
                        "from_address": "user@example.com",
                    }
                ]
            )
        )

        thread_id = downloader.resolve_thread_id(
            "<new@example.com>",
            None,
            [],
            "Re: Untitled",
            ["user@example.com"],
        )

        self.assertEqual(thread_id, "<new@example.com>")

    def test_resolve_thread_id_legacy_subject_fallback_can_be_enabled(self) -> None:
        downloader = self.thread_downloader(
            FakeThreadDatabase(
                emails=[
                    {
                        "message_id": "<old@example.com>",
                        "thread_id": "thread-1",
                        "subject": "Untitled",
                        "from_address": "user@example.com",
                    }
                ]
            ),
            subject_fallback=True,
        )

        thread_id = downloader.resolve_thread_id(
            "<new@example.com>",
            None,
            [],
            "Re: Untitled",
            ["user@example.com"],
        )

        self.assertEqual(thread_id, "thread-1")

    def test_sync_archives_only_queued_messages(self) -> None:
        FakeIMAP.uids = [b"10", b"11"]
        FakeIMAP.raw_by_uid = {b"10": b"queued", b"11": b"stored-only"}
        db = FakeStateDatabase()
        downloader = FakeDownloader(
            db,
            mail_config(
                {
                    "imap_host": "imap.local",
                    "imap_port": 993,
                    "imap_username": "agent@example.com",
                    "imap_password": "secret",
                    "imap_folder": "INBOX",
                    "imap_archive_folder": "Processed Mail",
                }
            ),
            [
                IngestResult(inserted=True, queued=True, message_id="<queued@example.com>"),
                IngestResult(inserted=True, queued=False, message_id="<stored@example.com>"),
            ],
        )

        with patch("assistant_agent.email_ingest.imaplib.IMAP4_SSL", FakeIMAP):
            inserted = downloader.sync_once()

        mailbox = FakeIMAP.instances[0]
        move_calls = [call for call in mailbox.uid_calls if call[0] == "MOVE"]
        self.assertEqual(inserted, 2)
        self.assertEqual(mailbox.selected, "INBOX")
        self.assertEqual(move_calls, [("MOVE", (b"10", '"Processed Mail"'))])
        self.assertEqual(db.state["imap:imap.local:INBOX:last_uid"], 11)

    def test_sync_archives_duplicate_actionable_messages_without_counting_insert(self) -> None:
        FakeIMAP.uids = [b"12"]
        FakeIMAP.raw_by_uid = {b"12": b"duplicate"}
        db = FakeStateDatabase()
        downloader = FakeDownloader(
            db,
            mail_config(
                {
                    "imap_host": "imap.local",
                    "imap_username": "agent@example.com",
                    "imap_password": "secret",
                    "imap_folder": "INBOX",
                    "imap_archive_folder": "Archive",
                }
            ),
            [IngestResult(inserted=False, queued=True, message_id="<duplicate@example.com>")],
        )

        with patch("assistant_agent.email_ingest.imaplib.IMAP4_SSL", FakeIMAP):
            inserted = downloader.sync_once()

        move_calls = [call for call in FakeIMAP.instances[0].uid_calls if call[0] == "MOVE"]
        self.assertEqual(inserted, 0)
        self.assertEqual(move_calls, [("MOVE", (b"12", "Archive"))])

    def test_sync_does_not_advance_checkpoint_when_archive_fails(self) -> None:
        FakeIMAP.uids = [b"13"]
        FakeIMAP.raw_by_uid = {b"13": b"queued"}
        FakeIMAP.move_status = "NO"
        db = FakeStateDatabase()
        downloader = FakeDownloader(
            db,
            mail_config(
                {
                    "imap_host": "imap.local",
                    "imap_username": "agent@example.com",
                    "imap_password": "secret",
                    "imap_folder": "INBOX",
                    "imap_archive_folder": "Archive",
                }
            ),
            [IngestResult(inserted=True, queued=True, message_id="<queued@example.com>")],
        )

        with patch("assistant_agent.email_ingest.imaplib.IMAP4_SSL", FakeIMAP):
            with self.assertRaises(RuntimeError):
                downloader.sync_once()

        self.assertNotIn("imap:imap.local:INBOX:last_uid", db.state)

    def test_queue_or_update_job_records_trigger_email_for_new_job(self) -> None:
        db = FakeQueueDatabase()
        downloader = EmailDownloader.__new__(EmailDownloader)
        downloader.db = db  # type: ignore[assignment]

        downloader.queue_or_update_job("thread-1", "Subject", 42)

        self.assertEqual(len(db.executed), 1)
        self.assertIn("trigger_email_id", db.executed[0][0])
        self.assertEqual(db.executed[0][1], ("thread-1", "Subject", 42))

    def test_queue_or_update_job_preserves_existing_open_job_trigger(self) -> None:
        db = FakeQueueDatabase(open_job={"id": 7, "status": "running"})
        downloader = EmailDownloader.__new__(EmailDownloader)
        downloader.db = db  # type: ignore[assignment]

        downloader.queue_or_update_job("thread-1", "Subject", 42)

        self.assertEqual(len(db.executed), 1)
        self.assertIn("COALESCE(trigger_email_id", db.executed[0][0])
        self.assertEqual(db.executed[0][1], (42, 7))
        self.assertEqual(db.logged[0][0], 7)
        self.assertEqual(db.logged[0][2], {"has_new_context": True, "reason": "new email arrived"})

    def test_archive_falls_back_to_copy_delete_and_expunge(self) -> None:
        mailbox = FallbackMailbox()
        downloader = EmailDownloader.__new__(EmailDownloader)

        downloader.archive_uid(mailbox, b"7", "Archive", "INBOX")

        self.assertEqual(
            mailbox.uid_calls,
            [
                ("MOVE", (b"7", "Archive")),
                ("COPY", (b"7", "Archive")),
                ("STORE", (b"7", "+FLAGS.SILENT", r"(\Deleted)")),
                ("EXPUNGE", (b"7",)),
            ],
        )
        self.assertTrue(mailbox.expunge_called)

    def test_sent_copy_appends_to_configured_imap_folder(self) -> None:
        message = EmailMessage()
        message["From"] = "agent@example.com"
        message["To"] = "user@example.com"
        message.set_content("hello")
        runtime = ToolRuntime(
            db=object(),  # type: ignore[arg-type]
            config=mail_config(
                {
                    "save_to_sent": True,
                    "imap_host": "imap.local",
                    "imap_port": 993,
                    "imap_username": "agent@example.com",
                    "imap_password": "secret",
                    "imap_sent_folder": "[Gmail]/Sent Mail",
                }
            ),
            job={"id": 1},
        )

        with patch("assistant_agent.tools.imaplib.IMAP4_SSL", FakeIMAP):
            result = runtime._append_to_sent_if_enabled(message)

        self.assertEqual(result, {"enabled": True, "status": "appended", "folder": "[Gmail]/Sent Mail"})
        self.assertEqual(FakeIMAP.instances[0].append_calls[0][0], '"[Gmail]/Sent Mail"')

    def test_email_send_formats_from_header_with_agent_display_name(self) -> None:
        runtime = ToolRuntime(
            db=FakeOutboundEmailDatabase(),  # type: ignore[arg-type]
            config=AppConfig(
                {
                    "agent": {
                        "app": {"name": "assistant"},
                        "identity": {"name": "Agent", "email": "agent@acme.example"},
                        "email": {
                            "smtp_host": "smtp.local",
                            "smtp_port": 587,
                            "smtp_username": "agent@acme.example",
                            "smtp_password": "secret",
                            "smtp_from": "agent@acme.example",
                        },
                        "filesystem": {"shared_root": "/tmp"},
                    }
                }
            ),
            job={"id": 1, "thread_id": "thread-1"},
        )

        with patch("assistant_agent.tools.smtplib.SMTP", FakeSMTP):
            result = runtime.email_send(["user@example.com"], "Hello", "Hi", new_thread=True)

        message, from_addr, to_addrs = FakeSMTP.instances[0].sent_messages[0]
        self.assertEqual(result["status"], "sent")
        self.assertEqual(str(message["From"]), "Agent <agent@acme.example>")
        self.assertEqual(from_addr, "agent@acme.example")
        self.assertEqual(to_addrs, ["user@example.com"])

    def test_email_send_adds_ai_disclosure_for_external_recipients(self) -> None:
        db = FakeOutboundEmailDatabase()
        runtime = ToolRuntime(
            db=db,  # type: ignore[arg-type]
            config=AppConfig(
                {
                    "agent": {
                        "identity": {"name": "Agent", "email": "agent@acme.example"},
                        "org": {
                            "name": "Acme Inc.",
                            "security_email": "security@acme.example",
                            "internal_email_domains": ["acme.example"],
                        },
                        "email": {
                            "smtp_host": "smtp.local",
                            "smtp_username": "agent@acme.example",
                            "smtp_password": "secret",
                        },
                        "filesystem": {"shared_root": "/tmp"},
                    }
                }
            ),
            job={"id": 1, "thread_id": "thread-1"},
        )

        with patch("assistant_agent.tools.smtplib.SMTP", FakeSMTP):
            result = runtime.email_send(["client@example.com"], "Hello", "Hi", new_thread=True)

        message = FakeSMTP.instances[0].sent_messages[0][0]
        plain = message.get_body(("plain",)).get_content()
        html = message.get_body(("html",)).get_content()
        self.assertTrue(result["disclosure_added"])
        self.assertIn("This email was sent by a semi-autonomous AI agent created by Acme Inc..", plain)
        self.assertIn("security@acme.example", plain)
        self.assertIn("assistant-ai-disclosure:start", html)
        self.assertIn("security@acme.example", html)
        self.assertIn("security@acme.example", db.inserted[0][4])

    def test_email_send_omits_ai_disclosure_for_internal_recipients(self) -> None:
        runtime = ToolRuntime(
            db=FakeOutboundEmailDatabase(),  # type: ignore[arg-type]
            config=AppConfig(
                {
                    "agent": {
                        "identity": {"name": "Agent", "email": "agent@acme.example"},
                        "org": {
                            "name": "Acme Inc.",
                            "security_email": "security@acme.example",
                            "internal_email_domains": ["acme.example"],
                        },
                        "email": {
                            "smtp_host": "smtp.local",
                            "smtp_username": "agent@acme.example",
                            "smtp_password": "secret",
                        },
                        "filesystem": {"shared_root": "/tmp"},
                    }
                }
            ),
            job={"id": 1, "thread_id": "thread-1"},
        )

        with patch("assistant_agent.tools.smtplib.SMTP", FakeSMTP):
            result = runtime.email_send(["person@acme.example"], "Hello", "Hi", cc=["team@acme.example"], new_thread=True)

        message = FakeSMTP.instances[0].sent_messages[0][0]
        plain = message.get_body(("plain",)).get_content()
        html = message.get_body(("html",)).get_content()
        self.assertFalse(result["disclosure_added"])
        self.assertNotIn("semi-autonomous AI agent", plain)
        self.assertNotIn("assistant-ai-disclosure", html)

    def test_email_disclosure_strip_handles_quoted_text_and_marked_html(self) -> None:
        text = (
            "Thanks, got it.\n\n"
            "> --\n"
            "> This email was sent by a semi-autonomous AI agent created by Acme Inc.. "
            "If you have any concerns or questions, please email security@acme.example.\n"
        )
        config = AppConfig({"agent": {"org": {"name": "Acme Inc.", "security_email": "security@acme.example"}}})
        html = append_disclosure_html("<html><body><p>Thanks, got it.</p></body></html>", config)

        self.assertEqual(strip_disclosure_text(text), "Thanks, got it.")
        stripped_html = strip_disclosure_html(html)
        self.assertIn("Thanks, got it.", stripped_html)
        self.assertNotIn("semi-autonomous AI agent", stripped_html)
        self.assertNotIn("assistant-ai-disclosure", stripped_html)

    def test_email_ingest_strips_ai_disclosure_before_storage(self) -> None:
        db = FakeIngestDatabase()
        downloader = EmailDownloader.__new__(EmailDownloader)
        downloader.db = db  # type: ignore[assignment]
        downloader.config = AppConfig({"agent": {"email": {}, "identity": {"email": "agent@acme.example"}}})
        downloader.attachment_root = Path("/tmp/assistant-test-artifacts/attachments")
        downloader.artifact_processor = FakeArtifactProcessor()
        message = EmailMessage()
        message["From"] = "client@example.com"
        message["To"] = "agent@acme.example"
        message["Subject"] = "Re: Hello"
        message["Message-ID"] = "<reply@example.com>"
        message.set_content(
            "Thanks, got it.\n\n"
            "> --\n"
            "> This email was sent by a semi-autonomous AI agent created by Acme Inc.. "
            "If you have any concerns or questions, please email security@acme.example.\n"
        )

        result = downloader.ingest_raw(message.as_bytes())

        self.assertTrue(result.inserted)
        self.assertEqual(db.inserted[0][8], "Thanks, got it.")

    def test_markdown_email_html_renders_tables(self) -> None:
        runtime = ToolRuntime(
            db=object(),  # type: ignore[arg-type]
            config=AppConfig({"agent": {"filesystem": {"shared_root": "/tmp"}}}),
            job={"id": 1},
        )

        html = runtime._markdown_email_html("| Name | Value |\n| --- | --- |\n| A | 1 |")

        self.assertIn("<table>", html)
        self.assertIn("<th>Name</th>", html)

    def test_markdown_email_html_preserves_signature_line_breaks(self) -> None:
        runtime = ToolRuntime(
            db=object(),  # type: ignore[arg-type]
            config=AppConfig({"agent": {"filesystem": {"shared_root": "/tmp"}}}),
            job={"id": 1},
        )

        html = runtime._markdown_email_html("Best,\nAgent\nAcme Inc. (FZE)\nhttps://acme.example")

        self.assertIn("Best,<br>", html)
        self.assertIn("Agent<br>", html)
        self.assertIn("Acme Inc. (FZE)<br>", html)

    def test_email_read_supports_body_character_paging(self) -> None:
        runtime = ToolRuntime(
            db=FakeEmailDatabase("0123456789abcdef"),  # type: ignore[arg-type]
            config=AppConfig(
                {
                    "agent": {
                        "email": {"read_max_body_chars": 5},
                        "filesystem": {"shared_root": "/tmp"},
                    }
                }
            ),
            job={"id": 1, "thread_id": "thread-1"},
        )

        result = runtime.email_read(7, body_offset=4, max_body_chars=20)
        email = result["email"]

        self.assertEqual(email["body_text"], "45678")
        self.assertEqual(email["body_html"], "")
        self.assertEqual(email["body_size_chars"], 16)
        self.assertEqual(email["body_offset"], 4)
        self.assertEqual(email["next_body_offset"], 9)
        self.assertTrue(email["body_truncated"])

    def test_email_read_supports_body_line_paging(self) -> None:
        runtime = ToolRuntime(
            db=FakeEmailDatabase("line1\nline2\nline3\n"),  # type: ignore[arg-type]
            config=AppConfig(
                {
                    "agent": {
                        "email": {"read_max_body_lines": 1},
                        "filesystem": {"shared_root": "/tmp"},
                    }
                }
            ),
            job={"id": 1, "thread_id": "thread-1"},
        )

        result = runtime.email_read(7, start_line=2, line_count=4)
        email = result["email"]

        self.assertEqual(email["body_text"], "line2\n")
        self.assertEqual(email["start_line"], 2)
        self.assertEqual(email["lines_read"], 1)
        self.assertEqual(email["next_line"], 3)
        self.assertTrue(email["body_truncated"])


if __name__ == "__main__":
    unittest.main()
