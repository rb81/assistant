from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from unittest.mock import MagicMock
import json

import pytest
from psycopg.errors import CheckViolation

from assistant_agent import development_sessions as sessions
from assistant_agent import script_permissions as permissions
from assistant_agent.tools import ToolRuntime
from test_script_permissions import db, runtime, version  # noqa: F401


APPROVAL = {"decision": "approve", "comments": "Public read-only report stays within the approved brief.", "model": "supervisor"}


def save_pending(runtime, pending):
    runtime.db.execute("UPDATE jobs SET status = 'needs_review', metadata = metadata || %s WHERE id = %s",
                       (permissions.Jsonb({"permission_request": pending}), runtime.job["id"]))
    runtime.job["metadata"] = {"permission_request": pending}
    return permissions.approval_payload(runtime.job)


def begin(runtime, *, runs=3, hosts=None, changes=False):
    item = version(runtime)
    with pytest.raises(permissions.PermissionRequired) as raised:
        sessions.start(runtime, item, "Build a public report", "report input and output files", "public HTTPS reads only",
                       hosts or [], 10, runs, changes)
    pending = raised.value.request
    permissions.apply_decision(runtime.db, save_pending(runtime, pending), "once")
    return item, sessions.current(runtime, item)


def test_admin_plan_then_supervisor_authorizes_exact_versions_and_repeated_runs(runtime):
    item, session = begin(runtime)
    assert session["status"] == "active"
    assert permissions.run_status(runtime.db, item, runtime.job["id"]) == "not_approved"
    with pytest.raises(permissions.PermissionDenied, match="supervisor approval"):
        permissions.execution(runtime, item)
    with patch("assistant_agent.development_supervisor.review", return_value=APPROVAL) as review:
        assert sessions.authorize(runtime, item, [])["supervisor"] == APPROVAL
        sessions.authorize(runtime, item, [])
        assert review.call_count == 1
        permissions.execution(runtime, item)
        permissions.execution(runtime, item)
        (runtime.shared_root / "helper.py").write_text("value = 2\n")
        changed = version(runtime)
        with pytest.raises(permissions.PermissionDenied, match="supervisor approval"):
            permissions.execution(runtime, changed)
        sessions.authorize(runtime, changed, [])
        assert review.call_count == 2
        assert review.call_args.args[1] == session["brief"]
        permissions.execution(runtime, changed)
    with pytest.raises(permissions.PermissionDenied, match="budget exhausted"):
        permissions.execution(runtime, changed)


def test_session_budget_is_atomic_and_scoped_to_job(runtime):
    item, _ = begin(runtime, runs=1)
    with patch("assistant_agent.development_supervisor.review", return_value=APPROVAL):
        sessions.authorize(runtime, item, [])
    def run():
        try:
            permissions.execution(runtime, item)
            return True
        except permissions.PermissionDenied:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: run(), range(2))) == [False, True]
    runtime.job = runtime.db.create_manual_job("Other", "Run report")
    with pytest.raises(permissions.PermissionDenied, match="Run approval is missing"):
        permissions.execution(runtime, item)


def test_expiry_blocks_review_and_execution_and_passes_deadline_to_broker(runtime):
    item, session = begin(runtime)
    with patch("assistant_agent.development_supervisor.review", return_value=APPROVAL):
        sessions.authorize(runtime, item, [])
    _, policy = permissions.execution(runtime, item)
    assert policy["expires_at"] == session["expires_at"].isoformat()
    runtime.db.execute("UPDATE sandbox_development_sessions SET expires_at = now() - interval '1 second' WHERE id = %s", (session["id"],))
    with pytest.raises(permissions.PermissionDenied, match="expired"):
        permissions.execution(runtime, item)
    with pytest.raises(permissions.PermissionDenied, match="expired"):
        sessions.authorize(runtime, item, [])


def test_host_changes_need_explicit_delegation_and_exact_review(runtime):
    item, session = begin(runtime, hosts=["api.example.com"])
    with patch("assistant_agent.development_supervisor.review", return_value=APPROVAL) as review:
        sessions.authorize(runtime, item, ["api.example.com"])
        with pytest.raises(permissions.PermissionDenied, match="New hosts require"):
            sessions.authorize(runtime, item, ["cdn.example.com"])
        assert review.call_count == 1
    # A new admin-approved brief is necessary; the agent cannot mutate the existing one.
    with pytest.raises(ValueError, match="changing the approved brief"):
        sessions.start(runtime, item, "Build a public report", "report input and output files", "public HTTPS reads only",
                       ["api.example.com"], 10, 3, True)
    with patch.object(ToolRuntime, "sandbox_control", return_value={}):
        permissions.management(runtime.db, runtime.config, ["revoke-script", str(item["script_id"])], True)
    item, session = begin(runtime, hosts=["api.example.com"], changes=True)
    with patch("assistant_agent.development_supervisor.review", return_value=APPROVAL):
        sessions.authorize(runtime, item, ["cdn.example.com"])
    _, policy = permissions.execution(runtime, item)
    assert policy["allow"] == ["cdn.example.com"]


@pytest.mark.parametrize("decision", ["reject", "escalate"])
def test_supervisor_rejection_never_authorizes_execution(runtime, decision):
    item, _ = begin(runtime)
    with patch("assistant_agent.development_supervisor.review", return_value={"decision": decision, "comments": "Outside objective"}) as review:
        for _ in range(2):
            with pytest.raises(permissions.PermissionDenied, match="Outside objective"):
                sessions.authorize(runtime, item, [])
        assert review.call_count == 1
    with pytest.raises(permissions.PermissionDenied, match="supervisor approval"):
        permissions.execution(runtime, item)


def test_completion_requires_final_development_decision_before_sending_receipt(runtime):
    from assistant_agent.task_agent import TaskAgent
    begin(runtime)
    agent = TaskAgent.__new__(TaskAgent)
    agent.db, agent.config = runtime.db, runtime.config
    history = []
    with patch("assistant_agent.task_agent.ReplyRouter") as router:
        assert not agent.handle_tool_call(runtime.job, history, runtime, "call", "task_complete",
                                          {"response": "Finished", "summary": "Done"}, set())
    router.assert_not_called()
    assert "script_approval_request" in history[-1]["content"]


def test_supervisor_outage_does_not_authorize_or_cache_success(runtime):
    from assistant_agent.development_supervisor import SupervisorError
    item, _ = begin(runtime)
    with patch("assistant_agent.development_supervisor.review", side_effect=SupervisorError("provider unavailable")):
        with pytest.raises(permissions.PermissionDenied, match="execution paused"):
            sessions.authorize(runtime, item, [])
    with pytest.raises(permissions.PermissionDenied, match="supervisor approval"):
        permissions.execution(runtime, item)


def test_final_approval_includes_supervisor_and_hosts_and_closes_development(runtime):
    item, session = begin(runtime, hosts=["api.example.com"])
    with patch("assistant_agent.development_supervisor.review", return_value=APPROVAL) as review:
        sessions.authorize(runtime, item, ["api.example.com"])
        with pytest.raises(permissions.PermissionRequired) as raised:
            permissions.final_request(runtime, item, ["api.example.com"], "Finished report")
        assert review.call_args.kwargs["final"] is True
    pending = raised.value.request
    assert pending["supervisor"] == APPROVAL
    assert pending["targets"] == ["api.example.com"]
    with pytest.raises(permissions.PermissionDenied, match="No active"):
        permissions.execution(runtime, item)
    payload = save_pending(runtime, pending)
    summary = permissions.summary(runtime.db, runtime.job)
    assert APPROVAL["comments"] in summary.split("\nDetails:")[0]
    assert "api.example.com" in summary.split("\nDetails:")[0]
    permissions.apply_decision(runtime.db, payload, "always")
    assert sessions.current(runtime, item) is None
    assert runtime.db.fetch_one("SELECT status FROM sandbox_development_sessions WHERE id = %s", (session["id"],))["status"] == "closed"
    for _ in range(2):
        assert permissions.execution(runtime, item)[1]["allow"] == ["api.example.com"]
    (runtime.shared_root / "helper.py").write_text("value = 3\n")
    changed = version(runtime)
    assert permissions.host_policy(runtime.db, runtime.config, changed["id"], runtime.job["id"])["allow"] == []


def test_final_once_consumes_execution_and_all_hosts_together(runtime):
    item, _ = begin(runtime, hosts=["api.example.com"])
    with patch("assistant_agent.development_supervisor.review", return_value=APPROVAL), \
            pytest.raises(permissions.PermissionRequired) as raised:
        permissions.final_request(runtime, item, ["api.example.com"], "Done")
    permissions.apply_decision(runtime.db, save_pending(runtime, raised.value.request), "once")
    assert permissions.execution(runtime, item)[1]["allow"] == ["api.example.com"]
    with pytest.raises(permissions.PermissionDenied, match="Run approval is missing"):
        permissions.execution(runtime, item)
    assert permissions.host_policy(runtime.db, runtime.config, item["id"], runtime.job["id"])["allow"] == []


def test_revoke_cancels_session_and_pending_final_request(runtime):
    item, _ = begin(runtime)
    with patch("assistant_agent.development_supervisor.review", return_value=APPROVAL), \
            pytest.raises(permissions.PermissionRequired) as raised:
        permissions.final_request(runtime, item, [], "Done")
    payload = save_pending(runtime, raised.value.request)
    with patch.object(ToolRuntime, "sandbox_control", return_value={}):
        permissions.management(runtime.db, runtime.config, ["revoke-script", str(item["script_id"])], True)
    with pytest.raises(ValueError, match="already handled"):
        permissions.apply_decision(runtime.db, payload, "always")
    with pytest.raises(permissions.PermissionDenied, match="Run approval is missing"):
        permissions.execution(runtime, item)


def test_yolo_skips_reviews_and_approvals_but_keeps_explicit_blocks(runtime):
    runtime.config.values["agent"]["security"] = {"script_yolo": True}
    item = version(runtime)
    with patch("assistant_agent.development_supervisor.review", side_effect=AssertionError("must not review")):
        assert permissions.final_request(runtime, item, ["api.example.com"], "Done")["mode"] == "script_yolo"
        assert permissions.execution(runtime, item)[1]["allow"] == ["*"]
    with patch.object(ToolRuntime, "sandbox_control", return_value={}):
        permissions.management(runtime.db, runtime.config, ["block-host", "api.example.com"], True)
    assert permissions.execution(runtime, item)[1]["block"] == ["api.example.com"]
    with pytest.raises(permissions.PermissionDenied, match="Administrator blocked"):
        permissions.final_request(runtime, item, ["api.example.com"], "Done")
    with patch.object(ToolRuntime, "sandbox_control", return_value={}):
        permissions.management(runtime.db, runtime.config, ["block-script", str(item["script_id"])], True)
    with pytest.raises(permissions.PermissionDenied, match="blocked or denied"):
        permissions.execution(runtime, item)


def test_default_and_legacy_global_hosts_cannot_authorize_scripts(runtime):
    item = version(runtime)
    runtime.db.execute("INSERT INTO sandbox_network_rules(host, decision) VALUES ('legacy.example.com', 'allow')")
    policy = permissions.host_policy(runtime.db, runtime.config, item["id"], runtime.job["id"])
    assert policy["allow"] == []
    with pytest.raises(CheckViolation, match="sandbox_no_global_allow"):
        runtime.db.execute(
            """INSERT INTO sandbox_permission_grants(kind, target, scope, decision, created_by)
               VALUES ('host', 'global.example.com', 'global', 'allow', 'test')""")


def test_tool_dispatch_pins_supervised_code_hosts_and_expiry(runtime):
    item, session = begin(runtime, hosts=["api.example.com"])
    health = {"permission_epoch": "epoch", "execution_identity": "image-v1"}
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "exit_code": 0, "stdout": "1", "stderr": "", "egress": "proxy", "network_events": [],
    }).encode()
    with patch.object(ToolRuntime, "sandbox_control", return_value=health), \
            patch("assistant_agent.development_supervisor.review", return_value=APPROVAL), \
            patch("assistant_agent.tools.read_secret", return_value="token"), \
            patch("assistant_agent.tools.ssl.create_default_context"), \
            patch("assistant_agent.tools.open_https_request", return_value=response) as send:
        runtime.script_development_review(["python3", "report.py"], ["https://api.example.com/report"])
        assert runtime.command_execute(["python3", "report.py"])["exit_code"] == 0
    payload = json.loads(send.call_args.args[0].data)
    assert payload["network_policy"] == {"allow": ["api.example.com"], "block": []}
    assert payload["authorization_expires_at"] == session["expires_at"].isoformat()
    assert payload["approved_files"]["helper.py"]
    assert payload["script_id"] == item["script_id"]


def test_upgrade_retires_global_allows_but_preserves_blocks_and_audit(runtime):
    db = runtime.db
    db.execute("DROP TABLE sandbox_development_reviews, sandbox_development_sessions")
    db.execute("ALTER TABLE sandbox_permission_grants DROP CONSTRAINT sandbox_no_global_allow")
    db.execute("DELETE FROM schema_migrations WHERE filename = '016_development_sessions.sql'")
    db.execute("""INSERT INTO sandbox_permission_grants(kind, target, scope, decision, created_by)
                  VALUES ('host', 'old.example.com', 'global', 'allow', 'admin'),
                         ('host', 'blocked.example.com', 'global', 'block', 'admin')""")
    assert db.run_migrations() == ["016_development_sessions.sql"]
    assert db.fetch_one("SELECT target FROM sandbox_permission_grants")["target"] == "blocked.example.com"
    assert db.fetch_one("SELECT action FROM sandbox_permission_history")["action"] == "retire-global-host-allow"
    assert db.run_migrations() == []
