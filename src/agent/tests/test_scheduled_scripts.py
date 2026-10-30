from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from assistant_agent import scheduled_scripts
from assistant_agent import script_permissions as permissions
from assistant_agent.email_approvals import permission_email_body, permission_keyboard
from assistant_agent.reminders import ReminderScheduler
from assistant_agent.requests import TransactionDatabase
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
    assert permission_keyboard("cb", payload)[0][0]["text"] == "Allow recurring runs of this version"
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


def test_approval_saves_the_original_call_without_the_model_retyping_it(scheduling):
    from unittest.mock import MagicMock
    from assistant_agent.task_agent import TaskAgent
    runtime = scheduling
    arguments = {
        "title": "Weekly report", "task": "Email me the report output.", "run_at": "2030-01-07T09:00:00Z",
        "recurrence_unit": "week", "recurrence_interval": 1, "execution": "script", "script": SCRIPT,
    }
    with pytest.raises(permissions.PermissionRequired) as raised:
        runtime.reminder_create(**arguments)
    pending = {**raised.value.request, "replay": {"tool": "reminder_create", "arguments": arguments}}
    permissions.apply_decision(runtime.db, pause(runtime, pending), "always")
    job = runtime.db.fetch_one("SELECT metadata FROM jobs WHERE id = %s", (runtime.job["id"],))
    assert job["metadata"]["replay_tool_call"]["arguments"] == arguments
    assert job["metadata"]["approved_script"] == {"version_id": pending["version_id"], "hosts": ["api.example.com"]}
    instruction = runtime.db.fetch_one("SELECT instruction FROM supervisor_instructions WHERE job_id = %s",
                                       (runtime.job["id"],))["instruction"]
    assert "system is saving the schedule" in instruction and "Retry" not in instruction
    audit = runtime.db.fetch_one("SELECT details FROM sandbox_permission_history WHERE action = 'always'")
    assert "replay" not in audit["details"]

    agent = TaskAgent.__new__(TaskAgent)
    agent.db = runtime.db
    agent.tool_result_cache = MagicMock(cache_result=lambda job_id, name, result: result,
                                        redact_result=lambda name, result: result)
    runtime.job["metadata"] = job["metadata"]
    history = []
    agent.replay_approved_call(runtime.job, runtime, history)
    reminder = runtime.db.fetch_one("SELECT * FROM reminders")
    assert reminder["metadata"]["script"]["hosts"] == ["api.example.com"]
    assert "schedule is saved" in history[0]["content"]
    assert "replay_tool_call" not in runtime.db.fetch_one(
        "SELECT metadata FROM jobs WHERE id = %s", (runtime.job["id"],))["metadata"]
    agent.replay_approved_call(runtime.job, runtime, history)
    assert len(history) == 1 and len(runtime.db.fetch_all("SELECT id FROM reminders")) == 1


def test_approved_version_with_changed_hosts_is_refused_not_re_requested(scheduling):
    runtime = scheduling
    with pytest.raises(permissions.PermissionRequired) as raised:
        create(runtime)
    version_id = raised.value.request["version_id"]
    permissions.apply_decision(runtime.db, pause(runtime, raised.value.request), "always")
    runtime.job["metadata"] = runtime.db.fetch_one("SELECT metadata FROM jobs WHERE id = %s",
                                                   (runtime.job["id"],))["metadata"]
    with pytest.raises(ToolError, match="just approved in this task with exactly these hosts: api.example.com"):
        create(runtime, script={**SCRIPT, "urls": ["https://api.example.com", "https://extra.example.com"]})
    assert not runtime.db.fetch_all("SELECT id FROM reminders")
    assert create(runtime)["reminder"]["metadata"]["script"]["version_id"] == version_id


def test_once_approval_from_another_job_does_not_satisfy_a_schedule(scheduling):
    runtime = scheduling
    item = permissions.inspect(runtime, SCRIPT["command"], purpose="Weekly report", environment="image-v1")
    with pytest.raises(permissions.PermissionRequired) as raised:
        permissions.final_request(runtime, item, ["api.example.com"], "Weekly report")
    permissions.apply_decision(runtime.db, pause(runtime, raised.value.request), "once")
    runtime.job["metadata"].pop("permission_request")
    with pytest.raises(permissions.PermissionRequired):
        create(runtime)


def schedule_once(runtime):
    with pytest.raises(permissions.PermissionRequired) as raised:
        create(runtime, recurrence_unit=None, recurrence_interval=None)
    payload = pause(runtime, raised.value.request)
    assert payload["scheduled"] and not payload["recurring"]
    buttons = [button for row in permission_keyboard("cb", payload) for button in row]
    assert any(button["text"] == "Approve this scheduled run" for button in buttons)
    assert not any(button["callback_data"] == "ap:cb:always" for button in buttons)
    body = permission_email_body(payload, permissions.summary(runtime.db, runtime.job), "otp", "hash", "soon", 60)
    assert "APPROVE ONCE" in body and "APPROVE ALWAYS" not in body
    with pytest.raises(ValueError, match="approval"):
        permissions.apply_decision(runtime.db, payload, "always")
    permissions.apply_decision(runtime.db, payload, "once")
    runtime.job["metadata"].pop("permission_request")
    return create(runtime, recurrence_unit=None, recurrence_interval=None)["reminder"]


def test_one_off_reserves_approval_until_due_and_consumes_it_exactly_once(scheduling):
    runtime = scheduling
    reminder = schedule_once(runtime)
    binding = reminder["metadata"]["script"]
    assert binding["reservation_id"]
    item = permissions.inspect(runtime, SCRIPT["command"], environment="image-v1")
    assert scheduled_scripts.grant_problem(runtime.db, runtime.config, binding) is None
    # Neither the creator nor a different schedule can spend the reserved grant.
    with pytest.raises(permissions.PermissionDenied, match="approval is missing"):
        permissions.execution(runtime, item)
    with pytest.raises(permissions.PermissionRequired):
        create(runtime, title="Another report", idempotency_key="another",
               recurrence_unit=None, recurrence_interval=None)
    make_due(runtime, reminder)
    assert ReminderScheduler(runtime.db, runtime.config).queue_due_reminders() == 1
    queued = runtime.db.fetch_one("SELECT job_id FROM reminders WHERE id = %s", (reminder["id"],))
    runtime.job = runtime.db.fetch_one("SELECT * FROM jobs WHERE id = %s", (queued["job_id"],))
    def execute():
        try:
            _, policy = permissions.execution(runtime, item)
            assert policy["allow"] == ["api.example.com"]
            return True
        except permissions.PermissionDenied:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: execute(), range(2))) == [False, True]
    with pytest.raises(permissions.PermissionDenied, match="approval is missing"):
        permissions.execution(runtime, item)
    assert "consumed" in scheduled_scripts.grant_problem(runtime.db, runtime.config, binding)


def test_one_off_to_recurring_requires_standing_approval(scheduling):
    runtime = scheduling
    reminder = schedule_once(runtime)
    with pytest.raises(permissions.PermissionRequired) as raised:
        runtime.reminder_update(reminder["id"], recurrence_unit="week")
    payload = pause(runtime, raised.value.request)
    assert payload["recurring"]
    with pytest.raises(ValueError, match="Always allow or Deny"):
        permissions.apply_decision(runtime.db, payload, "once")
    permissions.apply_decision(runtime.db, payload, "always")
    runtime.job["metadata"].pop("permission_request")
    updated = runtime.reminder_update(reminder["id"], recurrence_unit="week")["reminder"]
    assert updated["recurrence_unit"] == "week"
    assert "reservation_id" not in updated["metadata"]["script"]


def test_reserved_grant_cannot_be_used_after_revocation(scheduling):
    runtime = scheduling
    reminder = schedule_once(runtime)
    runtime.db.execute("DELETE FROM sandbox_permission_grants WHERE version_id = %s",
                       (reminder["metadata"]["script"]["version_id"],))
    assert "revoked" in scheduled_scripts.grant_problem(runtime.db, runtime.config, reminder["metadata"]["script"])
    make_due(runtime, reminder)
    with patch.object(scheduled_scripts, "alert"):
        ReminderScheduler(runtime.db, runtime.config).queue_due_reminders()
    row = runtime.db.fetch_one("SELECT status, job_id FROM reminders WHERE id = %s", (reminder["id"],))
    assert row == {"status": "failed", "job_id": None}


def test_failed_save_rolls_back_reservation_and_can_be_retried(scheduling):
    runtime = scheduling
    with pytest.raises(permissions.PermissionRequired) as raised:
        create(runtime, recurrence_unit=None, recurrence_interval=None)
    permissions.apply_decision(runtime.db, pause(runtime, raised.value.request), "once")
    runtime.job["metadata"].pop("permission_request")
    fetch_one = TransactionDatabase.fetch_one

    def fail_insert(db, sql, *args, **kwargs):
        if "INSERT INTO reminders(" in sql:
            raise ToolError("Simulated save failure")
        return fetch_one(db, sql, *args, **kwargs)

    with patch.object(TransactionDatabase, "fetch_one", fail_insert), pytest.raises(ToolError, match="save failure"):
        create(runtime, recurrence_unit=None, recurrence_interval=None)
    assert not runtime.db.fetch_all("SELECT id FROM reminders")
    assert not runtime.db.fetch_all("SELECT id FROM sandbox_permission_grants WHERE reservation_id IS NOT NULL")
    reminder = create(runtime, recurrence_unit=None, recurrence_interval=None)["reminder"]
    assert reminder["metadata"]["script"]["reservation_id"]


@pytest.mark.parametrize("decision", ["once", "always"])
def test_existing_approval_covers_one_off_without_an_extra_prompt(scheduling, decision):
    runtime = scheduling
    item = permissions.inspect(runtime, SCRIPT["command"], purpose="Weekly report", environment="image-v1")
    with pytest.raises(permissions.PermissionRequired) as raised:
        permissions.final_request(runtime, item, ["api.example.com"], "Weekly report")
    permissions.apply_decision(runtime.db, pause(runtime, raised.value.request), decision)
    runtime.job["metadata"].pop("permission_request")
    reminder = create(runtime, recurrence_unit=None, recurrence_interval=None)["reminder"]
    assert bool(reminder["metadata"]["script"].get("reservation_id")) == (decision == "once")


def test_consumed_approval_cannot_be_reserved_for_a_one_off(scheduling):
    runtime = scheduling
    item = permissions.inspect(runtime, SCRIPT["command"], environment="image-v1")
    with pytest.raises(permissions.PermissionRequired) as raised:
        permissions.final_request(runtime, item, ["api.example.com"], "Weekly report")
    permissions.apply_decision(runtime.db, pause(runtime, raised.value.request), "once")
    runtime.job["metadata"].pop("permission_request")
    permissions.execution(runtime, item)
    with pytest.raises(permissions.PermissionRequired):
        create(runtime, recurrence_unit=None, recurrence_interval=None)
    assert not runtime.db.fetch_all("SELECT id FROM reminders")


def test_new_approval_preserves_another_reminders_reservation(scheduling):
    runtime = scheduling
    first = schedule_once(runtime)
    with pytest.raises(permissions.PermissionRequired) as raised:
        create(runtime, task="Different follow-up work", idempotency_key="another",
               recurrence_unit=None, recurrence_interval=None)
    permissions.apply_decision(runtime.db, pause(runtime, raised.value.request), "once")
    runtime.job["metadata"].pop("permission_request")
    second = create(runtime, task="Different follow-up work", idempotency_key="another",
                    recurrence_unit=None, recurrence_interval=None)["reminder"]
    assert first["metadata"]["script"]["reservation_id"] != second["metadata"]["script"]["reservation_id"]
    for reminder in (first, second):
        assert scheduled_scripts.grant_problem(runtime.db, runtime.config, reminder["metadata"]["script"]) is None


def test_one_off_still_requires_fresh_code_and_preserves_reservation_on_reschedule(scheduling):
    runtime = scheduling
    reminder = schedule_once(runtime)
    updated = runtime.reminder_update(reminder["id"], run_at="2030-01-08T09:00:00Z")["reminder"]
    assert updated["metadata"]["script"] == reminder["metadata"]["script"]
    (runtime.shared_root / "helper.py").write_text("value = 2\n")
    assert "changed since approval" in scheduled_scripts.verify(runtime, updated["metadata"]["script"])


def test_one_off_can_use_scripts_that_cannot_receive_standing_approval(scheduling):
    runtime = scheduling
    (runtime.shared_root / "report.py").write_text("import subprocess\nsubprocess.run(['echo', 'report'])\n")
    reminder = schedule_once(runtime)
    version = runtime.db.fetch_one("SELECT reusable FROM sandbox_script_versions WHERE id = %s",
                                   (reminder["metadata"]["script"]["version_id"],))
    assert not version["reusable"]
    with pytest.raises(ToolError, match="cannot be scheduled recurrently"):
        create(runtime, idempotency_key="recurring")


def test_reservation_migration_preserves_existing_standing_grants(scheduling):
    runtime = scheduling
    reminder = schedule_approved(runtime)
    before = runtime.db.fetch_all("SELECT id, scope, decision FROM sandbox_permission_grants ORDER BY id")
    runtime.db.execute("ALTER TABLE sandbox_permission_grants DROP COLUMN reservation_id")
    runtime.db.execute("DELETE FROM schema_migrations WHERE filename = '017_scheduled_run_reservations.sql'")
    assert runtime.db.run_migrations() == ["017_scheduled_run_reservations.sql"]
    assert runtime.db.fetch_all("SELECT id, scope, decision FROM sandbox_permission_grants ORDER BY id") == before
    assert scheduled_scripts.grant_problem(runtime.db, runtime.config, reminder["metadata"]["script"]) is None


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
