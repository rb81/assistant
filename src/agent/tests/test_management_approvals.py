import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import hashlib
import json
import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from assistant_agent.config import AppConfig
from assistant_agent.database import Database
from assistant_agent.email_approvals import EmailApprovals
from assistant_agent.management_approvals import (
    ApprovalApplicationIncomplete,
    decide_approval,
    list_approvals,
)


@pytest.fixture
def db():
    url = os.environ.get("ASSISTANT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires disposable assistant_test database")
    original = Database(url)
    schema = "test_" + uuid.uuid4().hex
    with original.connect() as conn:
        if conn.info.dbname != "assistant_test":
            raise RuntimeError("management approval tests require disposable assistant_test database")
        conn.execute("CREATE SCHEMA " + schema)
    database = Database(make_conninfo(url, options="-c search_path=" + schema))
    try:
        database.execute(Path(__file__).resolve().parents[2].joinpath("database/schema.sql").read_text())
        database.ensure_feature_schema()
        database.run_migrations()
        yield database
    finally:
        original.execute("DROP SCHEMA " + schema + " CASCADE")


@pytest.fixture
def config():
    return AppConfig({"agent": {
        "admin": {"email": "admin@example.com"},
        "security": {"email_authentication": {"enabled": False}},
    }})


def create_pending(db, config, payload, source_id=None):
    with patch("assistant_agent.email_approvals.send_admin_email"):
        assert EmailApprovals(db, config).request(
            source_id or "management-test:" + uuid.uuid4().hex,
            payload,
            "The original delivery summary.",
        )


def create_job(db, title="Approval test", body="Task body"):
    job = db.create_manual_job(title, body)
    db.execute(
        "UPDATE jobs SET status = 'needs_review', updated_at = now() WHERE id = %s",
        (job["id"],),
    )
    return db.fetch_one("SELECT * FROM jobs WHERE id = %s", (job["id"],))


def single_approval(db, config):
    items = list_approvals(db, config)
    assert len(items) == 1
    assert items[0]["actionable"], (items[0]["reason_code"], items[0]["reason"], items[0].get("action"))
    return items[0]

def test_client_only_approval_is_delivered_without_email_or_otp_and_consumed_once(db):
    from assistant_agent.client_channels import ClientChannels
    config = AppConfig({"agent": {"approvals": {"channel": "client"}}})
    channels = ClientChannels(db, config)
    identifier = "c" * 32
    channels.register(identifier, "Desktop", "desktop", [])
    channels.poll(identifier)
    job = create_job(db)
    with patch("assistant_agent.email_approvals.send_admin_email") as email:
        assert EmailApprovals(db, config).request("client-review", {
            "kind": "review", "job_id": job["id"], "version": job["updated_at"].isoformat(),
        }, "Please review this task")
        email.assert_not_called()
    message = channels.poll(identifier)["messages"][0]
    assert message["purpose"] == "approval"
    assert "Please review this task" in message["body"]
    assert "OTP:" not in message["body"]
    approval = single_approval(db, config)
    decide_approval(db, config, approval["approval_id"], approval["version"], "deny")
    assert db.fetch_one("SELECT status FROM jobs WHERE id = %s", (job["id"],))["status"] == "cancelled"
    assert db.fetch_one(
        "SELECT output_data->>'reason' AS reason FROM task_logs WHERE job_id = %s AND event_type = 'status_change' ORDER BY sequence DESC LIMIT 1",
        (job["id"],),
    )["reason"] == "admin denied approval"
    with pytest.raises(ValueError):
        decide_approval(db, config, approval["approval_id"], approval["version"], "deny")


def test_command_review_shows_exact_command_without_otp_material_and_consumes_once(db, config):
    payload = {
        "kind": "command",
        "subject": "/admin config set service.example false",
        "body": "Set the feature off.",
    }
    create_pending(db, config, payload)

    approval = single_approval(db, config)
    assert approval["kind"] == "command"
    assert approval["action"]["subject"] == payload["subject"]
    assert approval["action"]["body"] == payload["body"]
    assert approval["action"]["decision_options"] == ["approve", "deny"]
    assert set(approval) == {
        "approval_id", "version", "kind", "expires_at", "status", "actionable", "action",
    }
    assert "otp_hash" not in repr(approval)
    assert len(approval["approval_id"]) == 32

    with patch("assistant_agent.admin_commands.AdminCommands.execute", return_value="executed") as execute:
        assert decide_approval(
            db, config, approval["approval_id"], approval["version"], "approve",
        ) == "executed"
    execute.assert_called_once_with(payload["subject"], payload["body"], confirmed=True)
    with pytest.raises(ValueError, match="expired, stale, or already used"):
        decide_approval(db, config, approval["approval_id"], approval["version"], "approve")


def test_external_action_failure_reports_consumed_approval_without_claiming_no_changes(db, config):
    payload = {"kind": "command", "subject": "/admin cancel 14", "body": ""}
    create_pending(db, config, payload)
    approval = single_approval(db, config)
    failure = RuntimeError("sensitive provider detail")

    with patch("assistant_agent.admin_commands.AdminCommands.execute", side_effect=failure):
        with pytest.raises(ApprovalApplicationIncomplete) as raised:
            decide_approval(db, config, approval["approval_id"], approval["version"], "approve")

    assert str(raised.value) == ApprovalApplicationIncomplete.MESSAGE
    assert "consumed" in str(raised.value).lower()
    assert "may have partially occurred" in str(raised.value).lower()
    assert "sensitive provider detail" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert db.fetch_one(
        "SELECT key FROM runtime_state WHERE key LIKE 'email-approval:%%'",
    ) is None
    source = db.fetch_one(
        "SELECT value FROM runtime_state WHERE key LIKE 'admin-request:%%'",
    )
    assert source["value"]["status"] == "approved"
    with pytest.raises(ValueError, match="expired, stale, or already used"):
        decide_approval(db, config, approval["approval_id"], approval["version"], "approve")


def test_transport_redaction_makes_the_approval_explicitly_non_actionable(db, config):
    secret = "sensitive-command-value"
    payload = {"kind": "command", "subject": "/admin config set service.key", "body": secret}
    create_pending(db, config, payload)

    with patch("assistant_agent.admin_commands.configured_secret_names", return_value=["service.key"]), patch(
        "assistant_agent.admin_commands.read_secret", return_value=secret,
    ):
        approval = list_approvals(db, config)[0]

    assert approval["status"] == "non_actionable"
    assert approval["actionable"] is False
    assert approval["approval_id"] is None
    assert approval["reason_code"] == "redacted_details"
    assert "redacted" in approval["reason"].lower()
    assert approval["action"]["body"] == "[redacted]"
    assert secret not in repr(approval)
    with pytest.raises(ValueError, match="Invalid approval reference"):
        decide_approval(db, config, approval["approval_id"], approval["version"], "approve")
    assert db.fetch_one("SELECT key FROM runtime_state WHERE key LIKE 'email-approval:%%'")


def test_stale_and_malformed_approvals_are_visible_but_have_no_decision_reference(db, config):
    payload = {"kind": "command", "subject": "/admin cancel 12", "body": ""}
    create_pending(db, config, payload)
    approval_row = db.fetch_one(
        "SELECT key, value FROM runtime_state WHERE key LIKE 'email-approval:%%'",
    )
    db.execute(
        "UPDATE runtime_state SET value = jsonb_set(value, '{payload,subject}', %s::jsonb) WHERE key = %s",
        (Jsonb("/admin cancel 99"), approval_row["key"]),
    )
    malformed = list_approvals(db, config)[0]
    assert malformed["status"] == "non_actionable"
    assert malformed["actionable"] is False
    assert malformed["approval_id"] is None
    assert malformed["reason_code"] == "invalid_record"
    assert "otp_hash" not in repr(malformed)
    assert approval_row["key"].removeprefix("email-approval:") not in repr(malformed)

    db.execute("DELETE FROM runtime_state WHERE key LIKE 'email-approval:%%'")
    db.execute("DELETE FROM runtime_state WHERE key LIKE 'admin-request:%%'")
    create_pending(db, config, {"kind": "network", "job_id": 999999, "request_id": "missing"})
    stale = list_approvals(db, config)[0]
    assert stale["status"] == "non_actionable"
    assert stale["actionable"] is False
    assert stale["approval_id"] is None
    assert stale["reason_code"] == "stale_action"
    assert stale["action"] is None


def _seed_pending_command(db, config, index):
    source_id = "pagination:" + str(index)
    source_key = "admin-request:" + hashlib.sha256(source_id.encode()).hexdigest()
    payload = {"kind": "command", "subject": "/admin cancel %s" % index, "body": ""}
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    pending = {
        "admin": str(config.get("agent.admin.email")).strip().lower(),
        "payload": payload,
        "source_id": source_id,
        "source_key": source_key,
        "expires": expires,
    }
    digest = hashlib.sha256(json.dumps(pending, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    value = {**pending, "otp_hash": hashlib.sha256(("otp-%s" % index).encode()).hexdigest()}
    with db.connect() as conn:
        conn.cursor().executemany(
            "INSERT INTO runtime_state(key, value) VALUES (%s, %s)",
            [(source_key, Jsonb({"status": "sent"})), ("email-approval:" + digest, Jsonb(value))],
        )


def test_offset_pagination_is_bounded_stable_and_reuses_review_references(db, config):
    for index in range(105):
        _seed_pending_command(db, config, index)

    first = list_approvals(db, config, limit=40, offset=0)
    repeated = list_approvals(db, config, limit=40, offset=0)
    second = list_approvals(db, config, limit=40, offset=40)
    third = list_approvals(db, config, limit=40, offset=80)

    assert len(first) == len(repeated) == len(second) == 40
    assert len(third) == 25
    assert [item["approval_id"] for item in first] == [item["approval_id"] for item in repeated]
    all_ids = [item["approval_id"] for page in (first, second, third) for item in page]
    assert len(set(all_ids)) == 105
    assert db.fetch_one(
        "SELECT count(*) AS total FROM runtime_state WHERE key LIKE 'management-approval-ref:%%'"
    )["total"] == 105
    assert len(list_approvals(db, config, limit=500, offset=0)) == 100
    with pytest.raises(ValueError, match="offset"):
        list_approvals(db, config, offset=-1)


def test_permission_review_includes_version_source_and_invokes_permission_domain(db, config):
    job = create_job(db)
    script = db.fetch_one("INSERT INTO sandbox_scripts(entry) VALUES ('approval.py') RETURNING id")
    source = "print('reviewed')\n"
    manifest = {
        "files": [{"path": "approval.py", "source": source, "sha256": "a" * 64}],
        "command": ["python", "approval.py"],
        "workdir": ".",
    }
    version = db.fetch_one(
        """INSERT INTO sandbox_script_versions(script_id, fingerprint, manifest, purpose, reusable)
           VALUES (%s, %s, %s, %s, true) RETURNING id""",
        (script["id"], "f" * 64, Jsonb(manifest), "run the reviewed script"),
    )
    pending_request = {
        "id": "request-1",
        "kind": "run",
        "version_id": version["id"],
        "fingerprint": "f" * 64,
        "purpose": "run the reviewed script",
        "targets": ["docs.example.com"],
        "screening": {"level": "not_malicious"},
        "reusable": True,
    }
    db.execute(
        "UPDATE jobs SET metadata = %s WHERE id = %s",
        (Jsonb({"permission_request": pending_request}), job["id"]),
    )
    payload = {
        "kind": "permission",
        "job_id": job["id"],
        "request_id": "request-1",
        "permission_type": "run",
        "version_id": version["id"],
        "reusable": True,
    }
    create_pending(db, config, payload)

    approval = single_approval(db, config)
    action = approval["action"]
    assert action["request"]["targets"] == ["docs.example.com"]
    assert action["script"]["source"] == "=== approval.py ===\n" + source
    assert action["script"]["manifest"]["command"] == ["python", "approval.py"]
    with patch("assistant_agent.script_permissions.apply_decision", return_value="permission applied") as apply:
        assert decide_approval(
            db, config, approval["approval_id"], approval["version"], "approve_once",
        ) == "permission applied"
    apply.assert_called_once()
    assert apply.call_args.args[1] == payload
    assert apply.call_args.args[2] == "once"


def test_network_review_shows_hosts_urls_and_decision_is_version_bound(db, config):
    job = create_job(db)
    request = {
        "id": "network-request-1",
        "hosts": ["docs.example.com"],
        "urls": ["https://docs.example.com/api"],
        "purpose": "retrieve documentation",
    }
    db.execute(
        "UPDATE jobs SET metadata = %s WHERE id = %s",
        (Jsonb({"network_request": request}), job["id"]),
    )
    payload = {"kind": "network", "job_id": job["id"], "request_id": request["id"]}
    create_pending(db, config, payload)

    approval = single_approval(db, config)
    assert approval["action"]["request"] == request
    assert approval["action"]["decision_options"] == ["block_once", "block_always", "cancel_job"]
    with patch("assistant_agent.network_access.apply_decision", return_value="blocked") as apply:
        assert decide_approval(
            db, config, approval["approval_id"], approval["version"],
            "block_once", "The host is not needed.",
        ) == "blocked"
    assert apply.call_args.args[2:4] == ("block", "The host is not needed.")


def test_network_action_change_after_listing_is_not_decidable(db, config):
    job = create_job(db)
    request = {
        "id": "network-request-2",
        "hosts": ["docs.example.com"],
        "urls": ["https://docs.example.com/api"],
        "purpose": "retrieve documentation",
    }
    db.execute(
        "UPDATE jobs SET metadata = %s WHERE id = %s",
        (Jsonb({"network_request": request}), job["id"]),
    )
    create_pending(db, config, {"kind": "network", "job_id": job["id"], "request_id": request["id"]})
    approval = single_approval(db, config)
    changed = {**request, "hosts": ["other.example.com"]}
    db.execute("UPDATE jobs SET metadata = %s WHERE id = %s",
               (Jsonb({"network_request": changed}), job["id"]))

    with pytest.raises(ValueError, match="action changed after it was reviewed"):
        decide_approval(db, config, approval["approval_id"], approval["version"], "block_once")
    assert db.fetch_one("SELECT key FROM runtime_state WHERE key LIKE 'email-approval:%%'")


def test_parallel_decisions_from_separate_review_lists_still_consume_once(db, config):
    payload = {"kind": "command", "subject": "/admin cancel 12", "body": ""}
    create_pending(db, config, payload)
    first, second = list_approvals(db, config)[0], list_approvals(db, config)[0]

    def decide(approval):
        try:
            return decide_approval(
                db, config, approval["approval_id"], approval["version"], "approve",
            )
        except ValueError:
            return None

    with patch("assistant_agent.admin_commands.AdminCommands.execute", return_value="executed"), \
            ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(decide, (first, second)))
    assert results.count("executed") == 1
    assert results.count(None) == 1


def test_review_shows_paused_tool_call_and_denial_cancels_only_unchanged_task(db, config):
    job = create_job(db)
    db.execute(
        "UPDATE jobs SET metadata = %s, last_error = %s WHERE id = %s",
        (Jsonb({"security_pending_action": {"tool": "calendar_create", "fingerprint": "f" * 64}}),
         "security review required for calendar_create", job["id"]),
    )
    db.log_event(job["id"], "tool_call", tool_name="calendar_create",
                 input_data={"title": "Review this event", "start": "2026-12-01T10:00:00Z"})
    job = db.fetch_one("SELECT * FROM jobs WHERE id = %s", (job["id"],))
    payload = {
        "kind": "review",
        "job_id": job["id"],
        "version": job["updated_at"].isoformat(),
        "security_action": True,
    }
    create_pending(db, config, payload)

    with patch("assistant_agent.admin_commands.redact_secrets", side_effect=lambda value: value):
        approval = single_approval(db, config)
    assert approval["action"]["effect"].startswith("Resume the existing task with the exact paused action")
    assert approval["action"]["tool_call"]["tool_name"] == "calendar_create"
    assert approval["action"]["tool_call"]["input_data"]["title"] == "Review this event"
    assert decide_approval(
        db, config, approval["approval_id"], approval["version"], "deny",
    ) == "Request denied. Task cancelled; completed actions were not undone."
    assert db.fetch_one("SELECT status FROM jobs WHERE id = %s", (job["id"],))["status"] == "cancelled"


def test_review_with_missing_security_tool_inputs_is_non_actionable(db, config):
    job = create_job(db)
    db.execute(
        "UPDATE jobs SET metadata = %s, last_error = %s WHERE id = %s",
        (Jsonb({"security_pending_action": {"tool": "calendar_create", "fingerprint": "f" * 64}}),
         "security review required for calendar_create", job["id"]),
    )
    job = db.fetch_one("SELECT * FROM jobs WHERE id = %s", (job["id"],))
    payload = {
        "kind": "review",
        "job_id": job["id"],
        "version": job["updated_at"].isoformat(),
        "security_action": True,
    }
    create_pending(db, config, payload)

    approval = list_approvals(db, config)[0]
    assert approval["status"] == "non_actionable"
    assert approval["actionable"] is False
    assert approval["approval_id"] is None
    assert approval["reason_code"] == "missing_action_inputs"
    assert approval["action"] is None
    assert "inputs" in approval["reason"]
    assert db.fetch_one(
        "SELECT key FROM runtime_state WHERE key LIKE 'management-approval-ref:%%'",
    ) is None


def email_bytes():
    message = EmailMessage()
    message["From"] = "admin@example.com"
    message["To"] = "assistant@example.com"
    message["Subject"] = "Start reviewed work"
    message["Message-ID"] = "<request@example.com>"
    message.set_content("Please process this exact request.")
    message.add_attachment(b"untrusted attachment", maintype="application", subtype="octet-stream",
                           filename="input.bin")
    return message.as_bytes()


@pytest.mark.parametrize(
    ("kind", "expected_type"),
    [
        ("email", "email_ingestion"),
        ("screened_email", "screened_email_release"),
    ],
)
def test_email_reviews_show_message_and_attachment_contents_then_enqueue_execution(
    db, config, kind, expected_type,
):
    raw = email_bytes()
    payload = {
        "kind": kind,
        "raw": base64.b64encode(raw).decode("ascii"),
        "folder": "INBOX",
        "thread_id": "thread-1",
        **({"job": None} if kind == "email" else {}),
    }
    create_pending(db, config, payload)

    approval = single_approval(db, config)
    action = approval["action"]
    assert action["type"] == expected_type
    assert action["folder"] == "INBOX"
    assert any(part.get("content") == "Please process this exact request.\n"
               for part in action["parts"])
    attachment = next(part for part in action["parts"] if part["filename"] == "input.bin")
    assert base64.b64decode(attachment["content_base64"]) == b"untrusted attachment"
    assert "otp_hash" not in repr(action)

    with patch("assistant_agent.email_ingest.EmailDownloader.ingest_raw") as ingest:
        assert decide_approval(
            db, config, approval["approval_id"], approval["version"], "approve",
            "Continue with the request.",
        ) == "Approval recorded. Email processing is queued."
    ingest.assert_not_called()
    queued = db.fetch_one("SELECT value FROM runtime_state WHERE key LIKE 'approved-email-execution:%%'")
    assert queued["value"]["status"] == "pending"
    assert queued["value"]["instruction"] == "Continue with the request."
    assert base64.b64decode(queued["value"]["payload"]["raw"]) == raw


def test_email_execution_is_claimed_once_and_retains_uncertain_failure(db, config):
    from assistant_agent.approved_email_execution import mark_interrupted, process_one
    from assistant_agent.email_ingest import IngestResult

    payload = {"kind": "email", "raw": base64.b64encode(email_bytes()).decode(),
               "folder": "INBOX", "thread_id": "thread-once", "job": None}
    create_pending(db, config, payload)
    approval = single_approval(db, config)
    decide_approval(db, config, approval["approval_id"], approval["version"], "approve", "Use bullets")
    downloader = MagicMock()
    downloader.ingest_raw.return_value = IngestResult(True, True, "<request@example.com>")
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: process_one(db, downloader), range(4)))
    assert sum(results) == 1
    downloader.ingest_raw.assert_called_once()
    assert downloader.ingest_raw.call_args.kwargs["approved_instruction"] == "Use bullets"
    row = db.fetch_one("SELECT value FROM runtime_state WHERE key LIKE 'approved-email-execution:%%'")
    assert row["value"]["status"] == "uncertain"
    assert row["value"]["error"] == "email_record_missing"
    assert "payload" in row["value"]

    create_pending(db, config, {**payload, "thread_id": "thread-failure"}, "another-email")
    approval = single_approval(db, config)
    decide_approval(db, config, approval["approval_id"], approval["version"], "approve")
    downloader.reset_mock()
    downloader.ingest_raw.side_effect = OSError("private provider detail")
    assert process_one(db, downloader)
    assert not process_one(db, downloader)
    uncertain = db.fetch_one(
        "SELECT value FROM runtime_state WHERE key LIKE 'approved-email-execution:%%' AND value->>'error' = 'OSError'"
    )["value"]
    assert uncertain["error"] == "OSError"
    assert "private provider detail" not in str(uncertain)
    assert downloader.ingest_raw.call_count == 1

    with db.connect() as conn:
        conn.execute("ALTER TABLE runtime_state DISABLE TRIGGER runtime_state_set_updated_at")
        conn.execute(
            """UPDATE runtime_state SET value = value || '{"status":"running"}'::jsonb,
               updated_at = now() - interval '31 minutes'
               WHERE key LIKE 'approved-email-execution:%%' AND value->>'status' = 'uncertain'"""
        )
        conn.execute("ALTER TABLE runtime_state ENABLE TRIGGER runtime_state_set_updated_at")
    mark_interrupted(db)
    assert not process_one(db, downloader)
    assert db.fetch_one(
        "SELECT value->>'status' AS status FROM runtime_state WHERE value->>'error' = 'execution_interrupted_or_still_running'"
    )["status"] == "uncertain"


def test_enqueue_failure_rolls_back_approval_consumption(db, config):
    payload = {"kind": "email", "raw": base64.b64encode(email_bytes()).decode(),
               "folder": "INBOX", "thread_id": "thread-rollback", "job": None}
    create_pending(db, config, payload)
    approval = single_approval(db, config)
    with patch("assistant_agent.approved_email_execution.enqueue", side_effect=RuntimeError("database unavailable")):
        with pytest.raises(RuntimeError):
            decide_approval(db, config, approval["approval_id"], approval["version"], "approve")
    assert db.fetch_one("SELECT key FROM runtime_state WHERE key LIKE 'email-approval:%%'")
    assert not db.fetch_one("SELECT key FROM runtime_state WHERE key LIKE 'approved-email-execution:%%'")


def test_downloader_processes_approved_email_and_attachment_into_one_job(db, config, tmp_path):
    from assistant_agent.approved_email_execution import process_one
    from assistant_agent.email_ingest import EmailDownloader
    from assistant_agent.request_router import RoutingDecision

    config.values["agent"]["artifacts"] = {"raw_root": str(tmp_path)}
    payload = {"kind": "email", "raw": base64.b64encode(email_bytes()).decode(),
               "folder": "INBOX", "thread_id": "thread-attachment", "job": None}
    create_pending(db, config, payload)
    approval = single_approval(db, config)
    decide_approval(db, config, approval["approval_id"], approval["version"], "approve")
    downloader = EmailDownloader(db, config)
    with patch.object(downloader, "acceptance_context", return_value={"reasons": ["explicit_sender_policy"]}), \
            patch.object(downloader, "process_artifacts"), \
            patch("assistant_agent.request_router.RequestRouter.route",
                  return_value=RoutingDecision("thread-attachment", "new", "New request")):
        assert process_one(db, downloader)
        assert not process_one(db, downloader)
    email_row = db.fetch_one("SELECT * FROM emails WHERE message_id = %s", ("<request@example.com>",))
    assert email_row is not None
    assert Path(email_row["attachments"][0]["path"]).read_bytes() == b"untrusted attachment"
    assert db.fetch_one("SELECT count(*) AS total FROM request_messages WHERE email_id = %s", (email_row["id"],))["total"] == 1
    assert db.fetch_one("SELECT count(*) AS total FROM jobs WHERE trigger_email_id = %s", (email_row["id"],))["total"] == 1
    execution = db.fetch_one("SELECT value FROM runtime_state WHERE key LIKE 'approved-email-execution:%%'")["value"]
    assert execution["status"] == "succeeded"
    assert "payload" not in execution


def test_email_telegram_and_management_compete_for_same_one_use_decision(db, config):
    from assistant_agent.email_approvals import approval_reply
    from assistant_agent.management_approvals import decide_channel_approval

    config.values["agent"].update({
        "approvals": {"channel": "both"},
        "telegram": {"allowed_user_ids": ["123"], "recipients": {"admin@example.com": 123}},
    })
    payload = {"kind": "email", "raw": base64.b64encode(email_bytes()).decode(),
               "folder": "INBOX", "thread_id": "thread-race", "job": None}
    with patch("assistant_agent.telegram.TelegramClient"), \
            patch("assistant_agent.email_approvals.send_admin_email") as send:
        assert EmailApprovals(db, config).request("race", payload, "Email")
    message = EmailMessage()
    message.set_content(send.call_args.args[2])
    token, digest, _ = approval_reply(message)
    pending = db.get_state("email-approval:" + digest)
    approval = single_approval(db, config)
    with pytest.raises(ValueError, match="invalid|credential"):
        decide_channel_approval(db, config, digest, "approve", actor="admin-email", otp_hash="0" * 64)
    with pytest.raises(ValueError, match="permission"):
        decide_channel_approval(db, config, digest, "approve", actor="telegram-admin",
                                callback_id=pending["callback_id"], sender="456")

    def decide(channel):
        try:
            if channel == "email":
                return decide_channel_approval(db, config, digest, None, actor="admin-email",
                                               otp_hash=hashlib.sha256(token.encode()).hexdigest())
            if channel == "telegram":
                return decide_channel_approval(db, config, digest, "approve", actor="telegram-admin",
                                               callback_id=pending["callback_id"], sender="123")
            return decide_approval(db, config, approval["approval_id"], approval["version"], "approve")
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(decide, ["email", "telegram", "management"]))
    assert results.count("Approval recorded. Email processing is queued.") == 1
    assert db.fetch_one("SELECT count(*) AS total FROM runtime_state WHERE key LIKE 'approved-email-execution:%%'")["total"] == 1


@pytest.mark.parametrize("model_fails", [False, True])
def test_email_approval_summary_is_concise_and_original_remains_reviewable(db, config, model_fails):
    config.values["agent"].update({
        "approvals": {"channel": "both"},
        "telegram": {"allowed_user_ids": ["123"], "recipients": {"admin@example.com": 123}},
    })
    message = EmailMessage()
    message["From"] = "admin@example.com"
    message["Subject"] = "R\u00e9sum\u00e9 request"
    message["Date"] = "Mon, 23 Nov 2026 10:00:00 +0000"
    message["Message-ID"] = "<summary@example.com>"
    message.set_content("Private original body, not the summary.")
    message.add_attachment(b"attachment contents", maintype="application", subtype="octet-stream",
                           filename="report.bin")
    payload = {"kind": "email", "raw": base64.b64encode(message.as_bytes()).decode(),
               "folder": "INBOX", "thread_id": "summary-thread", "job": None}
    response = {"choices": [{"message": {"content": "Prepare a report."}}]}
    with patch("assistant_agent.telegram.TelegramClient") as telegram, \
            patch("assistant_agent.email_approvals.send_admin_email") as send, \
            patch("assistant_agent.llm_client.LlmClient") as llm:
        if model_fails:
            llm.return_value.chat.side_effect = RuntimeError("model unavailable")
        else:
            llm.return_value.chat.return_value = response
        assert EmailApprovals(db, config).request("summary", payload, "dump of original")
    for delivery in (send.call_args.args[2], telegram.return_value.call.call_args.args[1]["text"]):
        assert "Private original body" not in delivery
        assert "attachment contents" not in delivery
        assert "admin@example.com" in delivery and "R\u00e9sum\u00e9 request" in delivery
        assert "23 Nov 2026" in delivery and "report.bin" in delivery
        assert ("Summary unavailable" if model_fails else "AI-generated summary: Prepare a report.") in delivery
    action = single_approval(db, config)["action"]
    assert action["review"]["summary_source"] == ("unavailable" if model_fails else "ai")
    assert any("Private original body" in (part.get("content") or "") for part in action["parts"])


def test_email_channel_rejects_changed_or_unbound_legacy_network_action(db, config):
    from assistant_agent.management_approvals import decide_channel_approval
    from assistant_agent.network_access import approval_payload

    job = create_job(db)
    request = {"id": "request", "hosts": ["first.example.com"], "urls": [], "purpose": "Read public data"}
    db.execute("UPDATE jobs SET metadata = %s WHERE id = %s",
               (Jsonb({"network_request": request}), job["id"]))
    job = db.fetch_one("SELECT * FROM jobs WHERE id = %s", (job["id"],))
    payload = approval_payload(job)
    create_pending(db, config, payload)
    row = db.fetch_one("SELECT key, value FROM runtime_state WHERE key LIKE 'email-approval:%%'")
    digest = row["key"].removeprefix("email-approval:")
    db.execute("UPDATE jobs SET metadata = %s WHERE id = %s",
               (Jsonb({"network_request": {**request, "hosts": ["changed.example.com"]}}), job["id"]))
    with pytest.raises(ValueError, match="changed"):
        decide_channel_approval(db, config, digest, None, "BLOCK ONCE", actor="admin-email",
                                otp_hash=row["value"]["otp_hash"])
    assert db.fetch_one("SELECT key FROM runtime_state WHERE key = %s", (row["key"],))

    db.execute("DELETE FROM runtime_state WHERE key = %s", (row["key"],))
    create_pending(db, config, {key: value for key, value in payload.items() if key != "request_fingerprint"},
                   source_id="legacy-network")
    row = db.fetch_one("SELECT key, value FROM runtime_state WHERE key LIKE 'email-approval:%%'")
    with pytest.raises(ValueError, match="older approval"):
        decide_channel_approval(db, config, row["key"].removeprefix("email-approval:"), None, "BLOCK ONCE",
                                actor="admin-email", otp_hash=row["value"]["otp_hash"])


def test_changed_after_review_expires_and_invalid_decisions_fail_closed(db, config):
    payload = {"kind": "command", "subject": "/admin cancel 12", "body": ""}
    create_pending(db, config, payload)
    approval = single_approval(db, config)
    key = db.fetch_one(
        "SELECT key FROM runtime_state WHERE key LIKE 'email-approval:%%'",
    )["key"]
    db.execute(
        """UPDATE runtime_state SET value = jsonb_set(value, '{payload,subject}', %s::jsonb),
           updated_at = %s WHERE key = %s""",
        (Jsonb("/admin cancel 99"), approval["version"], key),
    )
    with pytest.raises(ValueError, match="changed after it was reviewed"):
        decide_approval(db, config, approval["approval_id"], approval["version"], "approve")
    assert db.fetch_one("SELECT 1 FROM runtime_state WHERE key = %s", (key,))

    # Restore a fresh, valid record and prove an old review version cannot be reused.
    db.execute("DELETE FROM runtime_state WHERE key LIKE 'management-approval-ref:%%'")
    db.execute("DELETE FROM runtime_state WHERE key LIKE 'email-approval:%%'")
    create_pending(db, config, payload, "expiry-test")
    approval = single_approval(db, config)
    with pytest.raises(ValueError, match="decision is not available"):
        decide_approval(db, config, approval["approval_id"], approval["version"], "approve_once")
    key = db.fetch_one("SELECT key FROM runtime_state WHERE key LIKE 'email-approval:%%'")["key"]
    db.execute(
        """UPDATE runtime_state SET value = jsonb_set(value, '{expires}',
           to_jsonb((now() - interval '1 second')::text)) WHERE key = %s""",
        (key,),
    )
    with pytest.raises(ValueError):
        decide_approval(db, config, approval["approval_id"], approval["version"], "approve")


def test_telegram_only_approval_works_without_admin_email(db):
    telegram_config = AppConfig({"agent": {
        "approvals": {"channel": "telegram"},
        "telegram": {"allowed_user_ids": ["123"]},
    }})
    payload = {"kind": "command", "subject": "/admin cancel 13", "body": ""}
    with patch("assistant_agent.email_approvals.telegram_admins", return_value=["123"]), patch(
        "assistant_agent.telegram.TelegramClient",
    ), patch("assistant_agent.admin_commands.AdminCommands.execute", return_value="executed") as execute:
        assert EmailApprovals(db, telegram_config).request(
            "telegram-only", payload, "Review this command",
        )
        approval = single_approval(db, telegram_config)
        assert approval["action"]["subject"] == payload["subject"]
        assert decide_approval(
            db, telegram_config, approval["approval_id"], approval["version"], "approve",
        ) == "executed"
    execute.assert_called_once_with(payload["subject"], payload["body"], confirmed=True)
