from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from assistant_agent.approval_channels import approvals_configured, channels, telegram_admins
from assistant_agent.config import AppConfig
from assistant_agent.email_approvals import ApprovalDeliveryError, EmailApprovals
from assistant_agent.telegram import TelegramError
from assistant_agent.telegram_admin import TelegramAdmin
from assistant_agent.telegram_ingest import TelegramReceiver


def config(mode="telegram"):
    return AppConfig({"agent": {
        "approvals": {"channel": mode},
        "admin": {"email": "admin@example.com"},
        "email": {"smtp_host": "smtp.example.com", "smtp_from": "assistant@example.com"},
        "telegram": {"enabled": True, "bot_token": "test-token", "allowed_user_ids": ["123", "456"],
                     "recipients": {"admin@example.com": 123, "other@example.com": 456}},
    }})


@pytest.fixture
def ui():
    receiver = TelegramReceiver.__new__(TelegramReceiver)
    receiver.db, receiver.config, receiver.client, receiver.bot_id = MagicMock(), config(), MagicMock(), "9"
    return TelegramAdmin(receiver)


@pytest.mark.parametrize("mode,email_calls,telegram_calls", [
    ("email", 1, 0), ("telegram", 0, 1), ("both", 1, 1),
])
def test_approval_delivery_obeys_selected_channels_and_shares_one_record(mode, email_calls, telegram_calls):
    db = MagicMock()
    with patch("assistant_agent.email_approvals.send_admin_email") as email, patch(
        "assistant_agent.telegram.TelegramClient"
    ) as telegram:
        assert EmailApprovals(db, config(mode)).request(
            "review:12", {"kind": "review", "job_id": 12, "version": "version"}, "Voice task\n\nDetails:\nPrivate details",
        )
    assert email.call_count == email_calls
    assert telegram.return_value.call.call_count == telegram_calls
    state = db.set_state.call_args_list[0].args[1]
    assert "otp_hash" in state and "OTP:" not in str(state)
    if telegram_calls:
        payload = telegram.return_value.call.call_args.args[1]
        assert payload["chat_id"] == "123"
        assert "Private details" not in payload["text"]
        for row in payload["reply_markup"]["inline_keyboard"]:
            for button in row:
                assert len(button["callback_data"].encode()) <= 64
                assert state["callback_id"] in button["callback_data"]
        assert state["channel"] == mode


@pytest.mark.parametrize("mode", ["telegram", "both"])
def test_email_confirmation_button_processes_request_without_second_review(ui, mode):
    db = ui.db
    with patch("assistant_agent.email_approvals.send_admin_email"), patch(
        "assistant_agent.telegram.TelegramClient"
    ) as telegram:
        EmailApprovals(db, config(mode)).request(
            "email:12", {"kind": "email", "raw": "original-email", "folder": "INBOX"},
            "Email from your configured admin address. Confirm you sent this to start processing.\n"
            "Subject: Script Testing\n\nPrepare the report.",
        )
    pending = db.set_state.call_args_list[0].args[1]
    message = telegram.return_value.call.call_args.args[1]
    buttons = [button for row in message["reply_markup"]["inline_keyboard"] for button in row]
    confirm = next(button for button in buttons if button["text"] == "Confirm & start")
    assert "Subject: Script Testing" in message["text"]
    assert "Prepare the report." in message["text"]
    assert {button["text"] for button in buttons} == {"Confirm & start", "Not now", "Details"}
    _, callback_id, action = confirm["callback_data"].split(":")
    assert action == "ok"
    with patch.object(EmailApprovals, "telegram_pending", return_value={"value": pending}), \
            patch.object(EmailApprovals, "consume_telegram", return_value=pending["payload"]) as consume, \
            patch("assistant_agent.telegram_admin.apply_approval", return_value="Task queued.") as apply:
        ui.approval("123", callback_id, action)
    consume.assert_called_once_with(callback_id, "123")
    apply.assert_called_once_with(db, ui.config, pending["payload"])
    assert ui.client.call.call_count == 1
    assert ui.client.call.call_args.args[1]["text"] == "Task queued."


def test_email_approval_inbox_review_offers_direct_confirmation(ui):
    pending = {"payload": {"kind": "email"},
               "summary": "Email from your configured admin address.\nSubject: Script Testing",
               "expires": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}
    with patch.object(EmailApprovals, "telegram_pending", return_value={"value": pending}), \
            patch.object(EmailApprovals, "consume_telegram") as consume:
        ui.approval("123", "a" * 24, "review")
    message = ui.client.call.call_args.args[1]
    buttons = [button for row in message["reply_markup"]["inline_keyboard"] for button in row]
    assert next(button for button in buttons if button["text"] == "Confirm & start")[
        "callback_data"] == "ap:%s:ok" % ("a" * 24)
    consume.assert_not_called()


def test_legacy_network_delivery_only_offers_blocks_and_cancellation():
    with patch("assistant_agent.email_approvals.send_admin_email") as email, patch(
        "assistant_agent.telegram.TelegramClient"
    ) as telegram:
        EmailApprovals(MagicMock(), config("both")).request(
            "network:12", {"kind": "network", "job_id": 12}, "Website access requested",
        )
    body = email.call_args.args[2]
    assert "fresh combined script and host approval" in body
    assert "APPROVE ONCE" not in body and "APPROVE ALWAYS" not in body
    assert "BLOCK ONCE | BLOCK ALWAYS | CANCEL JOB" in body
    message = telegram.return_value.call.call_args.args[1]
    assert "fresh combined script and host approval" in message["text"]
    buttons = [button for row in message["reply_markup"]["inline_keyboard"] for button in row]
    assert {button["text"] for button in buttons} == {
        "Block once", "Block always", "Cancel job", "Add feedback first", "Details", "Not now",
    }


@pytest.mark.parametrize("action", ["once", "always"])
def test_legacy_network_approve_callbacks_reject_before_consumption(ui, action):
    pending = {"payload": {"kind": "network", "job_id": 12},
               "expires": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}
    with patch.object(EmailApprovals, "telegram_pending", return_value={"value": pending}), \
            patch.object(EmailApprovals, "consume_telegram") as consume, \
            patch("assistant_agent.network_access.apply_decision") as apply:
        with pytest.raises(ValueError, match="fresh combined script and host approval"):
            ui.approval("123", "a" * 24, action)
    consume.assert_not_called()
    apply.assert_not_called()


def test_legacy_network_review_explains_fresh_combined_approval(ui):
    pending = {"payload": {"kind": "network", "job_id": 12},
               "expires": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
               "summary": "Website access requested\n\nDetails:\nmanifest"}
    with patch.object(EmailApprovals, "telegram_pending", return_value={"value": pending}):
        ui.approval("123", "a" * 24, "review")
    message = ui.client.call.call_args.args[1]
    assert "fresh combined script and host approval" in message["text"]
    buttons = [button for row in message["reply_markup"]["inline_keyboard"] for button in row]
    assert not any(button["callback_data"].endswith((":once", ":always")) for button in buttons)


@pytest.mark.parametrize("action", ["scope", "broad", "global"])
def test_legacy_global_host_approval_callbacks_fail_closed(ui, action):
    pending = {"payload": {"kind": "permission", "job_id": 12, "request_id": "request",
                           "permission_type": "host", "reusable": True, "version_id": 1},
               "expires": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
               "summary": "Allow api.example.com"}
    with patch.object(EmailApprovals, "telegram_pending", return_value={"value": pending}), \
            patch.object(EmailApprovals, "consume_telegram") as consume, \
            patch("assistant_agent.script_permissions.apply_decision") as apply:
        with pytest.raises(ValueError, match="Unknown permission action"):
            ui.approval("123", "a" * 24, action)
        consume.assert_not_called()
        apply.assert_not_called()


@pytest.mark.parametrize("permission_type,reusable", [("development", True), ("final", False), ("host", False)])
def test_unavailable_always_approval_is_rejected_without_consuming(ui, permission_type, reusable):
    pending = {"payload": {"kind": "permission", "job_id": 12, "request_id": "request",
                           "permission_type": permission_type, "reusable": reusable, "version_id": 1},
               "expires": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
               "summary": "Permission approval"}
    with patch.object(EmailApprovals, "telegram_pending", return_value={"value": pending}), \
            patch.object(EmailApprovals, "consume_telegram") as consume, \
            patch("assistant_agent.script_permissions.apply_decision") as apply:
        with pytest.raises(ValueError, match="Unknown permission action"):
            ui.approval("123", "a" * 24, "always")
    consume.assert_not_called()
    apply.assert_not_called()


def test_final_review_card_shows_supervisor_conclusion_and_all_hosts_without_details(ui):
    hosts = [
        "%s.%s.%s.%s" % (chr(97 + number) * 63, "b" * 63, "c" * 63, "d" * 61)
        for number in range(10)
    ]
    summary = ("Final script and host approval\n\nSupervisor conclusion: safe to proceed.\n"
               + "Detailed supervisor comments. " * 300
               + "\n\nHTTPS hosts (all paths; subdomains excluded):\n"
               + "\n".join("- " + host for host in hosts) + "\n\nDetails:\nmanifest")
    pending = {"payload": {"kind": "permission", "job_id": 12, "request_id": "request",
                           "permission_type": "final", "reusable": False, "version_id": 1},
               "expires": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
               "summary": summary}
    with patch.object(EmailApprovals, "telegram_pending", return_value={"value": pending}):
        ui.approval("123", "a" * 24, "review")
    text = ui.client.call.call_args.args[1]["text"]
    assert "Supervisor conclusion: safe to proceed." in text
    assert all(host in text for host in hosts)
    assert len(text.encode("utf-16-le")) // 2 < 3900
    assert "Brief/comments shortened" in text
    assert "manifest" not in text
    keyboard = ui.client.call.call_args.args[1]["reply_markup"]["inline_keyboard"]
    assert not any(button["text"] == "Approve final always" for row in keyboard for button in row)
    assert any(button["text"] == "Review Script" for row in keyboard for button in row)


def test_permission_decision_applies_optional_feedback_without_generic_job_review(ui):
    pending = {"payload": {"kind": "permission", "job_id": 12, "request_id": "request"},
               "expires": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}
    ui.db.get_state.return_value = {"text": "Public data only."}
    with patch.object(EmailApprovals, "telegram_pending", return_value={"value": pending}), \
            patch.object(EmailApprovals, "consume_telegram", return_value=pending["payload"]), \
            patch("assistant_agent.script_permissions.apply_decision", return_value="Approved") as apply:
        ui.approval("123", "a" * 24, "once")
    apply.assert_called_once_with(ui.db, pending["payload"], "once", "Public data only.", "telegram-admin")
    assert ui.client.call.call_args.args[1] == {"chat_id": "123", "text": "Approved"}


def test_permission_delivery_keeps_code_in_attachment_not_message_body():
    db = MagicMock()
    payload = {"kind": "permission", "job_id": 12, "permission_type": "run", "reusable": True}
    with patch("assistant_agent.email_approvals.send_admin_email") as email, \
            patch("assistant_agent.telegram.TelegramClient") as telegram:
        EmailApprovals(db, config("both")).request(
            "permission:12", payload, 'Script run requested\n\nScript: report.py\n\nDetails:\n{"source": "print(1)"}',
            details_attachment=("version.json", b'{"source": "print(1)"}'),
        )
    assert "print(1)" not in telegram.return_value.call.call_args.args[1]["text"]
    assert "print(1)" not in email.call_args.args[2]
    assert b"print(1)" in email.call_args.kwargs["attachment"][1]


def test_initial_telegram_permission_message_preserves_long_host_list(ui):
    hosts = [
        "%s.%s.%s.%s" % (chr(97 + number) * 63, "b" * 63, "c" * 63, "d" * 61)
        for number in range(10)
    ]
    summary = ("Final script and host approval\n\nSupervisor conclusion: safe to proceed.\n"
               + "Detailed supervisor comments. " * 300
               + "\n\nHTTPS hosts (all paths; subdomains excluded):\n"
               + "\n".join("- " + host for host in hosts) + "\n\nDetails:\nmanifest")
    with patch("assistant_agent.telegram.TelegramClient") as telegram:
        EmailApprovals(ui.db, config()).request(
            "permission:12", {"kind": "permission", "job_id": 12, "permission_type": "final"},
            summary,
        )
    text = telegram.return_value.call.call_args.args[1]["text"]
    assert "Supervisor conclusion: safe to proceed." in text
    assert all(host in text for host in hosts)
    assert len(text.encode("utf-16-le")) // 2 < 3900


def test_permission_text_attachment_is_included_in_email_body():
    db = MagicMock()
    with patch("assistant_agent.email_approvals.send_admin_email") as email, patch(
        "assistant_agent.telegram.TelegramClient"
    ) as telegram:
        EmailApprovals(db, config("email")).request(
            "permission:12", {"kind": "permission", "job_id": 12, "permission_type": "final"},
            "Final script approval", details_attachment=("script-version.txt", b"=== report.py ===\nprint('full source')"),
        )
    assert "print('full source')" in email.call_args.args[2]
    telegram.assert_not_called()


def test_permission_management_is_admin_only(ui):
    with pytest.raises(ValueError, match="Administrator permission"):
        ui.permissions("456")
    ui.db.fetch_all.assert_not_called()


def test_host_management_excludes_defaults_and_global_allows_and_marks_legacy_allows_inactive(ui):
    ui.db.fetch_all.return_value = []
    ui.permissions("123", "phosts")
    query = ui.db.fetch_all.call_args.args[0]
    text = ui.client.call.call_args.args[1]["text"]
    assert "scope IN ('script', 'once')" in query
    assert "scope = 'global' AND decision = 'block'" in query
    assert "Defaults and global allows are not shown as effective" in text
    assert "Configured defaults:" not in text

    ui.db.fetch_all.return_value = [{"id": 8, "decision": "allow", "host": "legacy.example.com"}]
    ui.permissions("123", "plegacies")
    payload = ui.client.call.call_args.args[1]
    labels = [button["text"] for row in payload["reply_markup"]["inline_keyboard"] for button in row]
    assert any("INACTIVE allow legacy.example.com" in label for label in labels)
    assert "Legacy allow rules are inactive" in payload["text"]


def test_telegram_delivery_failure_never_falls_back_to_email():
    db = MagicMock()
    with patch("assistant_agent.telegram.TelegramClient") as telegram, patch(
        "assistant_agent.email_approvals.send_admin_email"
    ) as email:
        telegram.return_value.call.side_effect = TelegramError("network interrupted")
        with pytest.raises(ApprovalDeliveryError):
            EmailApprovals(db, config()).request("review:12", {"kind": "review"}, "Paused")
    email.assert_not_called()
    assert db.set_state.call_args.args[1]["status"] == "delivery_uncertain"
    db.execute.assert_called()


def test_both_channels_are_attempted_and_partial_delivery_is_explicit():
    db = MagicMock()
    with patch("assistant_agent.telegram.TelegramClient") as telegram, patch(
        "assistant_agent.email_approvals.send_admin_email"
    ) as email:
        telegram.return_value.call.side_effect = TelegramError("network interrupted")
        with pytest.raises(RuntimeError, match="Telegram.*other selected channels were attempted"):
            EmailApprovals(db, config("both")).request("review:12", {"kind": "review"}, "Paused")
    email.assert_called_once()
    assert db.set_state.call_args.args[1] == {"status": "delivery_uncertain"}


def test_rate_limit_failure_before_delivery_releases_request_claim():
    db = MagicMock()
    db.fetch_one.side_effect = [{"key": "claimed"}, None]
    with patch("assistant_agent.telegram.TelegramClient") as telegram:
        with pytest.raises(ValueError, match="rate limit"):
            EmailApprovals(db, config()).request("review:12", {"kind": "review"}, "Paused")
    telegram.assert_not_called()
    db.set_state.assert_not_called()
    assert "DELETE FROM runtime_state WHERE key IN" in db.execute.call_args.args[0]


def test_only_verified_admins_can_consume_telegram_approvals():
    db = MagicMock()
    service = EmailApprovals(db, config())
    with pytest.raises(ValueError, match="Administrator permission"):
        service.consume_telegram("a" * 24, "456")
    db.fetch_one.assert_not_called()
    with pytest.raises(ValueError, match="Telegram only"):
        service.consume("token", "digest")


def test_telegram_consumption_uses_same_atomic_email_record_and_original_admins():
    service = EmailApprovals(MagicMock(), config("both"))
    service.db.fetch_one.return_value = {"key": "email-approval:" + "d" * 64,
        "value": {"otp_hash": "hashed", "telegram_admin_ids": ["123"]}}
    with patch.object(service, "_consume", return_value={"kind": "review"}) as consume:
        assert service.consume_telegram("a" * 24, "123") == {"kind": "review"}
    consume.assert_called_once_with("d" * 64, "hashed")
    service.db.fetch_one.return_value["value"]["telegram_admin_ids"] = ["456"]
    with pytest.raises(ValueError, match="already handled"):
        service.consume_telegram("a" * 24, "123")


def test_invalid_channel_and_admin_identity_fail_closed():
    values = config()
    assert approvals_configured(values)
    values.values["agent"]["approvals"]["channel"] = "automatic"
    with pytest.raises(ValueError):
        channels(values)
    values.values["agent"]["approvals"]["channel"] = "telegram"
    values.values["agent"]["telegram"]["recipients"]["admin@example.com"] = 999
    with pytest.raises(ValueError, match="allowlist"):
        telegram_admins(values)
    del values.values["agent"]["telegram"]["recipients"]["admin@example.com"]
    assert not approvals_configured(values)


def test_admin_identity_reuses_only_admin_email_mapping_not_other_allowlisted_recipients():
    values = config()
    assert telegram_admins(values) == ["123"]
    values.values["agent"]["admin"]["email"] = " ADMIN@EXAMPLE.COM "
    values.values["agent"]["telegram"]["recipients"] = '{"ADMIN@example.com":123,"other@example.com":456}'
    assert telegram_admins(values) == ["123"]
    values.values["agent"]["admin"]["email"] = "unmapped@example.com"
    assert telegram_admins(values) == []
    values.values["agent"]["admin"]["email"] = ""
    assert telegram_admins(values) == []


def test_admin_mapping_changes_revoke_old_telegram_approval_identity():
    values = config("both")
    values.values["agent"]["telegram"]["recipients"]["admin@example.com"] = 456
    service = EmailApprovals(MagicMock(), values)
    with pytest.raises(ValueError, match="Administrator permission"):
        service.consume_telegram("a" * 24, "123")
    service.db.fetch_one.assert_not_called()
    service.db.fetch_one.return_value = {"key": "email-approval:" + "d" * 64,
                                       "value": {"otp_hash": "hashed", "telegram_admin_ids": ["123"]}}
    with pytest.raises(ValueError, match="already handled"):
        service.consume_telegram("a" * 24, "456")


def test_email_only_approvals_need_no_telegram_admin_mapping():
    values = config("email")
    values.values["agent"]["email"]["smtp_host"] = "mail.example.net"
    values.values["agent"]["email"]["smtp_from"] = "assistant@example.net"
    values.values["agent"].pop("telegram")
    assert approvals_configured(values)
    with patch("assistant_agent.email_approvals.send_admin_email") as send, patch(
        "assistant_agent.telegram.TelegramClient"
    ) as telegram:
        EmailApprovals(MagicMock(), values).request("input:12", {"kind": "input", "job_id": 12}, "Your reply is needed.")
    send.assert_called_once()
    telegram.assert_not_called()


def test_home_and_pagination_do_not_switch_selected_conversation(ui):
    ui.home("123")
    keyboard = ui.client.call.call_args.args[1]["reply_markup"]["inline_keyboard"]
    assert [button["text"] for button in keyboard[0]] == ["Conversations", "Tasks"]
    assert any(button["text"] == "Needs attention" for row in keyboard for button in row)
    ui.db.fetch_all.return_value = [{"id": number, "title": "Stream %s" % number} for number in range(20, 11, -1)]
    ui.page("123", "streams")
    keyboard = ui.client.call.call_args.args[1]["reply_markup"]["inline_keyboard"]
    assert keyboard[-2][0]["callback_data"] == "ui:streams:13"
    assert len(keyboard) == 10
    ui.db.execute.assert_not_called()


def test_nonadmin_job_lists_are_scoped_and_have_no_mutation_controls(ui):
    ui.db.fetch_all.return_value = []
    ui.page("456", "jobs", 20)
    query = ui.db.fetch_all.call_args
    assert query.args[1] == (20, 20, False, False, "456")
    assert "r.sender_id = %s" in query.args[0]
    ui.db.fetch_one.side_effect = [{"id": 12, "thread_id": "t", "status": "needs_review", "metadata": {}}, {"id": 1}]
    ui.job_card("456", 12)
    text = str(ui.client.call.call_args.args[1])
    assert "Resend approval" not in text and "Cancel task" not in text and "Run again" not in text
    with pytest.raises(ValueError, match="Administrator permission"):
        ui.confirm("456", "cancel", 12)


def test_state_aware_job_controls_do_not_offer_blind_resume_for_reviews(ui):
    ui.db.fetch_one.return_value = {"id": 12, "status": "needs_review", "metadata": {"title": "Female voice note"}}
    ui.job_card("123", 12)
    payload = ui.client.call.call_args.args[1]
    assert "Female voice note" in payload["text"]
    assert "Resend approval" in str(payload)
    assert "Run again" not in str(payload)


@pytest.mark.parametrize("kind", ["review", "email"])
def test_expired_approval_offers_renewal_without_consuming_or_running(ui, kind):
    pending = {"payload": {"kind": kind, "job_id": 12, "version": "old"},
               "summary": "Task", "expires": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}
    with patch.object(EmailApprovals, "telegram_pending", return_value={"value": pending}), patch.object(
        EmailApprovals, "consume_telegram"
    ) as consume, patch("assistant_agent.telegram_admin.apply_approval") as execute:
        ui.approval("123", "a" * 24, "ok")
    assert "Request fresh approval" in str(ui.client.call.call_args)
    consume.assert_not_called()
    execute.assert_not_called()


def test_stale_review_cannot_be_renewed(ui):
    pending = {"payload": {"kind": "review", "job_id": 12, "version": "old"},
               "summary": "Task", "expires": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}
    ui.db.fetch_one.return_value = {"id": 12, "status": "needs_review", "updated_at": "new"}
    with patch.object(EmailApprovals, "telegram_pending", return_value={"value": pending}), patch.object(
        EmailApprovals, "request"
    ) as request:
        with pytest.raises(ValueError, match="Task changed"):
            ui.approval("123", "a" * 24, "renew")
    request.assert_not_called()


def test_guidance_reply_is_scoped_to_prompt_and_cannot_approve_security_action(ui):
    now = datetime.now(timezone.utc)
    pending = {"id": 12, "action": "guidance", "message_id": 80, "version": now.isoformat(),
               "expires": (now + timedelta(minutes=10)).isoformat()}
    ui.db.get_state.return_value = pending
    message = {"from": {"id": 123}, "text": "Use a different voice", "reply_to_message": {"message_id": 80}}
    with patch("assistant_agent.telegram_admin.TransactionDatabase") as database, patch(
        "assistant_agent.telegram_admin.apply_approval"
    ) as approve:
        assert ui.input_reply(message)
    updates = [call.args[0] for call in database.return_value.fetch_one.call_args_list]
    assert any("CASE WHEN status = 'waiting' THEN 'queued' ELSE status END" in sql for sql in updates)
    approve.assert_not_called()
    message["reply_to_message"]["message_id"] = 81
    assert not ui.input_reply(message)


def test_reply_to_admin_question_resumes_without_permission_override(ui):
    ui.db.get_state.return_value = {
        "id": 12, "action": "guidance", "answer_only": True, "message_id": 80, "version": "version",
        "expires": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    }
    with patch("assistant_agent.telegram_admin.TransactionDatabase") as database, patch(
        "assistant_agent.approval_actions.answer_admin_input"
    ) as answer, patch("assistant_agent.telegram_admin.apply_approval") as approve:
        assert ui.input_reply({"from": {"id": 123}, "text": "Continue with the saved audio",
                               "reply_to_message": {"message_id": 80}})
    answer.assert_called_once_with(database.return_value, {"job_id": 12, "version": "version"},
                                   "Continue with the saved audio")
    approve.assert_not_called()
    assert "without changing permissions" in ui.client.call.call_args.args[1]["text"]


def test_handled_admin_reply_cannot_be_routed_as_a_new_task_on_delivery_retry(ui):
    ui.db.get_state.return_value = {"handled": True}
    assert ui.input_reply({"from": {"id": 123}, "text": "Use saved audio",
                           "reply_to_message": {"message_id": 80}})
    ui.db.get_state.assert_called_once_with("telegram-admin-reply:9:123:80")
    ui.db.connect.assert_not_called()
    ui.client.call.assert_not_called()


def test_forwarded_or_group_callbacks_never_reach_admin_actions(ui):
    callback = {"id": "c", "from": {"id": 123}, "data": "ui:cancel:12",
                "message": {"chat": {"type": "group", "id": -1}}}
    ui.receiver.select_task(callback)
    ui.db.fetch_one.assert_not_called()
    ui.client.call.assert_not_called()


def test_mutation_confirmation_requires_admin_and_live_version(ui):
    ui.db.fetch_one.return_value = {"id": 12, "status": "completed", "updated_at": "version"}
    ui.confirm("123", "rerun", 12)
    pending = ui.db.set_state.call_args.args[1]
    assert pending["action"] == "rerun" and pending["version"] == "version"
    assert "external actions may be repeated" in ui.client.call.call_args.args[1]["text"]
    with patch("assistant_agent.telegram_admin.TransactionDatabase") as transaction:
        transaction.return_value.fetch_one.side_effect = [{"value": pending}, None]
        with pytest.raises(ValueError, match="Task changed"):
            ui.execute_confirmation("123", "nonce")
    query = transaction.return_value.fetch_one.call_args.args[0]
    assert "updated_at = %s" in query


def test_permanent_deletion_stays_in_safe_local_preview_flow(ui):
    ui.callback({"id": "c", "from": {"id": 123}, "data": "ui:delete:12"})
    assert "stopped writer services" in ui.client.call.call_args.args[1]["text"]
    ui.db.execute.assert_not_called()


def private_message(text, message_id=50):
    return {"update_id": message_id, "message": {
        "from": {"id": 123}, "chat": {"id": 123, "type": "private"},
        "message_id": message_id, "date": 1_700_000_000, "text": text,
    }}


@pytest.mark.parametrize("text", ["/list", "/status", "/cancel 12", "/status@assistant_bot"])
def test_legacy_commands_are_not_routed_or_executed(ui, text):
    receiver = ui.receiver
    receiver.db.fetch_one.return_value = None
    with patch("assistant_agent.telegram_ingest.RequestService") as requests:
        receiver.ingest(private_message(text))
    requests.assert_not_called()
    receiver.db.update_job_status.assert_not_called()
    texts = [call.args[1]["text"] for call in receiver.client.call.call_args_list if call.args[0] == "sendMessage"]
    assert texts[0] == "Unknown command. Use /menu, /tasks or /new."
    assert texts[-1] == "Choose what to manage:"

@pytest.mark.parametrize("command,method,args", [
    ("/menu", "home", ("123",)),
    ("/tasks", "page", ("123", "jobs", 0)),
    ("/permissions", "permissions", ("123",)),
    ("/tasks@assistant_bot", "page", ("123", "jobs", 0)),
])
def test_menu_commands_open_browser_without_starting_agent_job(ui, command, method, args):
    ui.db.fetch_one.return_value = None
    with patch.object(TelegramAdmin, method) as browse, \
            patch("assistant_agent.telegram_ingest.RequestService") as requests:
        ui.receiver.ingest(private_message(command))
    browse.assert_called_once_with(*args)
    requests.assert_not_called()


def test_permissions_command_rejects_non_admin_without_starting_job(ui):
    ui.db.fetch_one.return_value = None
    message = private_message("/permissions")
    message["message"]["from"]["id"] = 456
    message["message"]["chat"]["id"] = 456
    with patch("assistant_agent.telegram_ingest.RequestService") as requests:
        ui.receiver.ingest(message)
    requests.assert_not_called()
    assert ui.client.call.call_args.args[1]["text"] == "Administrator permission is required"


def test_paths_that_look_like_commands_still_reach_the_assistant(ui):
    receiver = ui.receiver
    receiver.db.fetch_one.return_value = None
    receiver.selected_workstream = MagicMock(return_value=None)
    receiver.task_thread = MagicMock(return_value=("thread", []))
    with patch("assistant_agent.telegram_ingest.RequestService") as requests:
        receiver.ingest(private_message("/Users/me/notes.txt summarize this"))
    requests.return_value.receive_request.assert_called_once()


def test_selecting_a_workstream_confirms_in_the_chat(ui):
    receiver = ui.receiver
    receiver.db.fetch_one.return_value = {"id": 7, "title": "Trip planning"}
    receiver.select_task({"id": "cb", "from": {"id": 123}, "data": "ws:7",
                          "message": {"chat": {"type": "private", "id": 123}}})
    receiver.db.execute.assert_called_once()
    method, payload = receiver.client.call.call_args.args
    assert method == "sendMessage" and payload["chat_id"] == "123"
    assert "Trip planning" in payload["text"]


def test_receiver_registers_only_current_commands():
    with patch("assistant_agent.telegram_ingest.TelegramClient") as client:
        client.return_value.call.return_value = {"id": 9}
        TelegramReceiver(MagicMock(), config())
    client.return_value.call.assert_any_call("setMyCommands", {"commands": [
        {"command": "menu", "description": "Open the main menu"},
        {"command": "new", "description": "Start a new workstream"},
        {"command": "tasks", "description": "View tasks and recent history"},
    ]})
    scoped = {call.args[1]["scope"]["chat_id"]: call.args[1]["commands"]
              for call in client.return_value.call.call_args_list
              if call.args[0] == "setMyCommands" and "scope" in call.args[1]}
    assert [item["command"] for item in scoped["123"]] == ["menu", "new", "tasks", "permissions"]
    assert [item["command"] for item in scoped["456"]] == ["menu", "new", "tasks"]
