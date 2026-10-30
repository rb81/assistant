from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from assistant_agent import scheduled_scripts
from assistant_agent import script_permissions as permissions
from assistant_agent.email_approvals import permission_email_body, permission_keyboard
from assistant_agent.reminders import ReminderScheduler
from assistant_agent.tools import ToolError, ToolRuntime
from test_network_access import db  # noqa: F401
from test_script_permissions import runtime  # noqa: F401


HEALTH = {"permission_epoch": "epoch", "execution_identity": "image-v1"}
SCRIPT = {"command": ["python3", "report.py"], "urls": ["https://api.example.com/v1"], "purpose": "Weekly report"}


@pytest.fixture
def scheduling(runtime):
    runtime.config.values["agent"]["sandbox"]["base_url"] = "https://sandbox:8080"
    with patch.object(ToolRuntime, "sandbox_control", return_value=HEALTH):
        yield runtime


def create(runtime, **overrides):
    arguments = {
        "title": "Weekly report", "task": "Email me the report output.", "run_at": "2030-01-07T09:00:00Z",
        "recurrence_unit": "week", "recurrence_interval": 1, "execution": "script", "script": SCRIPT,
        "idempotency_key": "weekly-report", **overrides,
    }
    return runtime.reminder_create(**arguments)


def pause(runtime, pending):
    runtime.db.execute("UPDATE jobs SET status = 'needs_review', metadata = metadata || %s WHERE id = %s",
                       (permissions.Jsonb({"permission_request": pending}), runtime.job["id"]))
    runtime.job["metadata"] = {**(runtime.job.get("metadata") or {}), "permission_request": pending}
    return permissions.approval_payload(runtime.job)


def schedule_approved(runtime):
    with pytest.raises(permissions.PermissionRequired) as raised:
        create(runtime)
    permissions.apply_decision(runtime.db, pause(runtime, raised.value.request), "always")
    runtime.job["metadata"].pop("permission_request")
    return create(runtime)["reminder"]


def make_due(runtime, reminder):
    runtime.db.execute("UPDATE reminders SET run_at = %s WHERE id = %s",
                       (datetime.now(timezone.utc) - timedelta(minutes=1), reminder["id"]))


def test_script_reminder_needs_standing_approval_before_it_is_saved(scheduling):
    runtime = scheduling
    with pytest.raises(permissions.PermissionRequired) as raised:
        create(runtime)
    assert not runtime.db.fetch_all("SELECT id FROM reminders")
    pending = raised.value.request
    assert pending["scheduled"]["recurrence"] == "every 1 week"
    payload = pause(runtime, pending)
    assert payload["scheduled"] is True
    buttons = [button["callback_data"] for row in permission_keyboard("cb", payload) for button in row]
    assert "ap:cb:always" in buttons and "ap:cb:once" not in buttons
    summary = permissions.summary(runtime.db, runtime.job)
    assert "Scheduled script approval" in summary and "One-run approval is not offered" in summary
    body = permission_email_body(payload, summary, "otp", "hash", "soon", 60)
    assert "APPROVE ALWAYS" in body and "APPROVE ONCE" not in body
    with pytest.raises(ValueError, match="Always allow or Deny"):
        permissions.apply_decision(runtime.db, payload, "once")
    permissions.apply_decision(runtime.db, payload, "always")
    instruction = runtime.db.fetch_one("SELECT instruction FROM supervisor_instructions WHERE job_id = %s",
                                       (runtime.job["id"],))["instruction"]
    assert "Retry the identical reminder_create call" in instruction

    runtime.job["metadata"].pop("permission_request")
    reminder = create(runtime)["reminder"]
    binding = reminder["metadata"]["script"]
    assert reminder["metadata"]["execution"] == "script"
    assert binding["hosts"] == ["api.example.com"] and binding["version_id"] == pending["version_id"]
    assert create(runtime)["idempotent_reuse"]


def test_once_approval_from_another_job_does_not_satisfy_a_schedule(scheduling):
    runtime = scheduling
    item = permissions.inspect(runtime, SCRIPT["command"], purpose="Weekly report", environment="image-v1")
    with pytest.raises(permissions.PermissionRequired) as raised:
        permissions.final_request(runtime, item, ["api.example.com"], "Weekly report")
    permissions.apply_decision(runtime.db, pause(runtime, raised.value.request), "once")
    runtime.job["metadata"].pop("permission_request")
    with pytest.raises(permissions.PermissionRequired):
        create(runtime)


def test_due_reminder_carries_binding_and_skips_after_revocation(scheduling):
    runtime = scheduling
    reminder = schedule_approved(runtime)
    make_due(runtime, reminder)
    scheduler = ReminderScheduler(runtime.db, runtime.config)
    assert scheduler.queue_due_reminders() == 1
    queued = runtime.db.fetch_one("SELECT job_id FROM reminders WHERE id = %s", (reminder["id"],))
    job = runtime.db.fetch_one("SELECT metadata FROM jobs WHERE id = %s", (queued["job_id"],))
    assert job["metadata"]["scheduled_script"]["reminder_id"] == reminder["id"]
    email = runtime.db.fetch_one("SELECT body_text FROM emails WHERE thread_id = %s", ("reminder:%s" % reminder["id"],))
    assert "Run exactly this with command_execute" in email["body_text"]

    runtime.db.execute("UPDATE reminders SET status = 'scheduled', job_id = NULL WHERE id = %s", (reminder["id"],))
    make_due(runtime, reminder)
    with patch.object(ToolRuntime, "sandbox_control", return_value={"status": "revoked"}):
        permissions.management(runtime.db, runtime.config,
                               ["revoke-script", str(reminder["metadata"]["script"]["script_id"])], True)
    with patch.object(scheduled_scripts, "alert") as alert:
        assert scheduler.queue_due_reminders() == 1
    skipped = runtime.db.fetch_one("SELECT * FROM reminders WHERE id = %s", (reminder["id"],))
    assert skipped["status"] == "scheduled" and skipped["job_id"] is None
    assert skipped["run_at"] > datetime.now(timezone.utc) and "revoked" in skipped["last_error"]
    assert "skipped" in alert.call_args.args[3]


def test_sweep_detects_code_changes_once_and_blocks_the_due_run(scheduling):
    runtime = scheduling
    reminder = schedule_approved(runtime)
    (runtime.shared_root / "helper.py").write_text("value = 2\n")
    with patch.object(scheduled_scripts, "alert") as alert:
        assert scheduled_scripts.sweep(runtime.db, runtime.config, force=True) == 1
        assert scheduled_scripts.sweep(runtime.db, runtime.config, force=True) == 1
    assert alert.call_count == 1 and "changed since approval" in alert.call_args.args[3]
    row = runtime.db.fetch_one("SELECT * FROM reminders WHERE id = %s", (reminder["id"],))
    assert "changed since approval" in scheduled_scripts.due_problem(runtime.db, runtime.config, row)


def test_scheduled_run_cannot_develop_and_flags_changed_requests(scheduling):
    runtime = scheduling
    reminder = schedule_approved(runtime)
    runtime.job = runtime.db.create_manual_job("Scheduled run", "Run report")
    runtime.job["metadata"] = {"scheduled_script": {**reminder["metadata"]["script"], "reminder_id": reminder["id"]}}
    with pytest.raises(ToolError, match="unavailable in scheduled runs"):
        runtime.development_session_request(["python3", "report.py"], "x", "none", "none")
    (runtime.shared_root / "helper.py").write_text("value = 5\n")
    with pytest.raises(permissions.PermissionRequired) as raised:
        runtime.script_approval_request(["python3", "report.py"], "Weekly report", urls=["api.example.com"])
    pause(runtime, raised.value.request)
    assert "Warning: requested during a scheduled run of reminder #%s" % reminder["id"] in \
        permissions.summary(runtime.db, runtime.job)
