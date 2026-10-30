import base64
import json
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from assistant_agent import script_permissions as permissions
from assistant_agent.config import AppConfig
from assistant_agent.email_approvals import permission_keyboard
from assistant_agent.security_screening import MALICIOUS, POSSIBLY_MALICIOUS, ScreeningResult
from assistant_agent.tools import SandboxAttemptsExhausted, ToolRuntime
from test_network_access import db  # noqa: F401


@pytest.fixture
def runtime(db, tmp_path):
    (tmp_path / "report.py").write_text("from helper import value\nprint(value)\n")
    (tmp_path / "helper.py").write_text("value = 1\n")
    config = AppConfig({"agent": {
        "filesystem": {"shared_root": str(tmp_path), "require_mount": False},
        "sandbox": {"network": {"allowed_hosts": ["pypi.org"]}},
    }})
    job = db.create_manual_job("Permission test", "Run report")
    client = MagicMock(model="test/reviewer", base_url="https://models.example/v1")
    client.chat.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
        "decision": "approve", "comments": "Reads local input and writes a report.",
    })}}]}
    with patch("assistant_agent.development_supervisor.LlmClient", return_value=client):
        yield ToolRuntime(db, config, job)


def version(runtime, command=None, image="image-v1"):
    return permissions.inspect(runtime, command or ["python3", "report.py"], purpose="Daily report", environment=image)


def approve(runtime, item, kind="run", targets=None, decision="always"):
    with pytest.raises(permissions.PermissionRequired) as raised:
        permissions.request(runtime, item, kind, targets, "Daily report")
    pending = raised.value.request
    runtime.db.execute("UPDATE jobs SET status = 'needs_review', metadata = metadata || %s WHERE id = %s",
                       (permissions.Jsonb({"permission_request": pending}), runtime.job["id"]))
    runtime.job["metadata"] = {**(runtime.job.get("metadata") or {}), "permission_request": pending}
    payload = permissions.approval_payload(runtime.job)
    permissions.apply_decision(runtime.db, payload, decision, "Please keep the report concise.", "test-admin")
    return payload


def test_new_versions_include_helpers_arguments_workdir_but_not_image_or_config(runtime):
    original = version(runtime)
    assert version(runtime)["id"] == original["id"]
    assert version(runtime, ["python3", "report.py", "--delete"])["id"] != original["id"]
    assert version(runtime, image="image-v2")["id"] == original["id"]
    runtime.config.values["agent"]["security"] = {"email_authentication": {"enabled": True}}
    assert version(runtime)["id"] == original["id"]
    (runtime.shared_root / "subdir").mkdir()
    assert permissions.inspect(runtime, ["python3", "../report.py"], workdir="subdir")["id"] != original["id"]
    with patch.object(permissions, "APPROVAL_REVISION", 2):
        assert version(runtime)["id"] != original["id"]
    (runtime.shared_root / "helper.py").write_text("value = 2\n")
    assert version(runtime)["id"] != original["id"]


def make_legacy(runtime, item):
    state = {key: value for key, value in item["manifest"].items() if key != "approval_revision"}
    fingerprint = permissions.digest({
        "command": state["command"], "files": {file["path"]: file["sha256"] for file in state["files"]},
        "workdir": state["workdir"], "environment": state["environment"],
        "security_policy": runtime.config.get("agent.security", {}),
    })
    runtime.db.execute(
        "UPDATE sandbox_script_versions SET approval_identity = NULL, fingerprint = %s, manifest = %s WHERE id = %s",
        (fingerprint, permissions.Jsonb(state), item["id"]),
    )
    return runtime.db.fetch_one("SELECT * FROM sandbox_script_versions WHERE id = %s", (item["id"],))


@pytest.mark.parametrize("decision", ["always", "once", "deny", "consumed", "revoked"])
def test_legacy_adoption_preserves_permissions_without_regranting(runtime, decision):
    item = make_legacy(runtime, version(runtime))
    approve(runtime, item, targets=["api.example.com"],
            decision=decision if decision in {"always", "once", "deny"} else "once")
    if decision == "consumed":
        permissions.execution(runtime, item)
    if decision == "revoked":
        runtime.db.execute("DELETE FROM sandbox_permission_grants WHERE version_id = %s", (item["id"],))
    before = runtime.db.fetch_all("SELECT * FROM sandbox_permission_grants ORDER BY id")
    with ThreadPoolExecutor(max_workers=2) as pool:
        adopted = list(pool.map(lambda _: version(runtime, image="rebuilt-image"), range(2)))
    assert all(row["id"] == item["id"] and row["fingerprint"] == item["fingerprint"] for row in adopted)
    assert runtime.db.fetch_all("SELECT * FROM sandbox_permission_grants ORDER BY id") == before
    expected = "denied" if decision == "deny" else "not_approved" if decision in {"consumed", "revoked"} else "approved"
    assert permissions.run_status(runtime.db, adopted[0], runtime.job["id"]) == expected


def test_legacy_adoption_does_not_merge_conflicting_version_permissions(runtime):
    first = make_legacy(runtime, version(runtime))
    approve(runtime, first)
    second = runtime.db.fetch_one(
        """INSERT INTO sandbox_script_versions(script_id, fingerprint, manifest, purpose, reusable)
           VALUES (%s, %s, %s, %s, true) RETURNING *""",
        (first["script_id"], "second-legacy-fingerprint", permissions.Jsonb(first["manifest"]), "Other environment"),
    )
    runtime.db.execute(
        """INSERT INTO sandbox_permission_grants(version_id, kind, scope, decision, created_by)
           VALUES (%s, 'run', 'script', 'block', 'test')""", (second["id"],),
    )
    with pytest.raises(permissions.PermissionDenied, match="reconciliation is required"):
        version(runtime, image="rebuilt-image")
    assert runtime.db.fetch_one("SELECT count(*) AS n FROM sandbox_permission_grants")["n"] == 2
    assert not runtime.db.fetch_all("SELECT id FROM sandbox_script_versions WHERE approval_identity IS NOT NULL")


def test_legacy_pending_approval_stays_valid_and_never_covers_changed_code(runtime):
    item = make_legacy(runtime, version(runtime))
    with pytest.raises(permissions.PermissionRequired) as raised:
        permissions.final_request(runtime, item, [], "Report")
    pending = raised.value.request
    runtime.db.execute("UPDATE jobs SET status = 'needs_review', metadata = %s WHERE id = %s",
                       (permissions.Jsonb({"permission_request": pending}), runtime.job["id"]))
    runtime.job["metadata"] = {"permission_request": pending}
    adopted = version(runtime, image="rebuilt-image")
    assert adopted["id"] == item["id"] and adopted["fingerprint"] == pending["fingerprint"]
    permissions.apply_decision(runtime.db, permissions.approval_payload(runtime.job), "always")
    (runtime.shared_root / "helper.py").write_text("value = 99\n")
    changed = version(runtime, image="rebuilt-image")
    assert changed["id"] != adopted["id"]
    assert permissions.run_status(runtime.db, changed, runtime.job["id"]) == "not_approved"


def test_stable_approval_migration_preserves_rows_and_is_idempotent(runtime):
    item = version(runtime)
    approve(runtime, item)
    before = runtime.db.fetch_all("SELECT * FROM sandbox_permission_grants ORDER BY id")
    runtime.db.execute("ALTER TABLE sandbox_script_versions DROP COLUMN approval_identity")
    runtime.db.execute("DELETE FROM schema_migrations WHERE filename = '018_stable_script_approvals.sql'")
    assert runtime.db.run_migrations() == ["018_stable_script_approvals.sql"]
    assert runtime.db.run_migrations() == []
    assert runtime.db.fetch_all("SELECT * FROM sandbox_permission_grants ORDER BY id") == before
    assert version(runtime, image="rebuilt-image")["id"] == item["id"]


def test_once_grant_is_atomic_and_does_not_increase_limits(runtime):
    item = version(runtime)
    approve(runtime, item, decision="once")
    registered = permissions.management(runtime.db, runtime.config, ["scripts"])[0]
    assert registered["has_once_grant"] and not registered["approved_always"]
    job = runtime.db.fetch_one("SELECT metadata FROM jobs WHERE id = %s", (runtime.job["id"],))
    assert "admin_review_override" not in job["metadata"]
    def execute():
        try:
            permissions.execution(runtime, item)
            return True
        except permissions.PermissionDenied:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: execute(), range(2))) == [False, True]
    assert not permissions.management(runtime.db, runtime.config, ["scripts"])[0]["has_once_grant"]


def test_always_grant_and_screening_override_survive_jobs_but_not_code_changes(runtime):
    runtime.config.values["agent"]["security"] = {
        "decision_model": {"enabled": True, "screen": {"scripts": True}},
    }
    item = version(runtime)
    with patch.object(permissions.SecurityScreener, "screen",
                      return_value=ScreeningResult(POSSIBLY_MALICIOUS, "network access")) as screen:
        approve(runtime, item)
        assert permissions.request(runtime, version(runtime), "run")["status"] == "approved"
        runtime.job = runtime.db.create_manual_job("Next day", "Run report")
        assert permissions.request(runtime, version(runtime), "run")["status"] == "approved"
        assert screen.call_count == 1
        (runtime.shared_root / "helper.py").write_text("value = 3\n")
        with pytest.raises(permissions.PermissionRequired):
            permissions.request(runtime, version(runtime), "run")
        assert screen.call_count == 2


def test_combined_host_grants_are_version_scoped_and_never_global(runtime):
    item = version(runtime)
    approve(runtime, item, "host", ["api.example.com"])
    current = permissions.host_policy(runtime.db, runtime.config, item["id"], runtime.job["id"])
    assert permissions.network_access.decide(current, "api.example.com") == "allowed"
    assert permissions.network_access.decide(current, "sub.api.example.com") == "not_approved"
    other = version(runtime, ["python3", "report.py", "--other"])
    assert permissions.network_access.decide(
        permissions.host_policy(runtime.db, runtime.config, other["id"], runtime.job["id"]), "api.example.com"
    ) == "not_approved"
    with pytest.raises(ValueError, match="Unknown permission decision"):
        approve(runtime, item, "host", ["shared.example.com"], "global")
    assert "shared.example.com" not in permissions.host_policy(runtime.db, runtime.config, other["id"], runtime.job["id"])["allow"]
    approve(runtime, item, "host", ["once.example.com"], "once")
    permissions.execution(runtime, item)
    assert "once.example.com" not in permissions.host_policy(runtime.db, runtime.config, item["id"], runtime.job["id"])["allow"]


def test_denial_and_explicit_blocks_override_allows_and_cannot_be_requested_again(runtime):
    item = version(runtime)
    payload = approve(runtime, item, decision="deny")
    with pytest.raises(permissions.PermissionDenied, match="blocked or denied"):
        permissions.request(runtime, item, "run")
    with pytest.raises(ValueError, match="already handled"):
        permissions.apply_decision(runtime.db, payload, "once")
    with patch.object(ToolRuntime, "sandbox_control", return_value={"status": "revoked"}) as control:
        permissions.management(runtime.db, runtime.config, ["block-host", "pypi.org"], True)
    control.assert_called_once_with("POST", "/permissions/revoke")
    current = permissions.host_policy(runtime.db, runtime.config, item["id"], runtime.job["id"])
    assert permissions.network_access.decide(current, "pypi.org") == "blocked"


def test_changed_files_fail_before_consuming_grants(runtime):
    item = version(runtime)
    approve(runtime, item, decision="once")
    (runtime.shared_root / "helper.py").write_text("value = 8\n")
    with pytest.raises(permissions.PermissionDenied, match="version changed"):
        permissions.execution(runtime, item)
    assert permissions.run_status(runtime.db, item, runtime.job["id"]) == "approved"


def test_symlinked_code_dependencies_are_rejected(runtime):
    helper = runtime.shared_root / "helper.py"
    helper.rename(runtime.shared_root / "real_helper.py")
    helper.symlink_to("real_helper.py")
    with pytest.raises(ValueError, match="must not use symlinks"):
        version(runtime)


def test_uninspectable_code_cannot_receive_always_approval(runtime):
    item = version(runtime, ["python3", "-c", "exec(input())"])
    assert not item["reusable"]
    with pytest.raises(ValueError, match="dependencies cannot be pinned"):
        approve(runtime, item)


def test_malicious_verdict_cannot_be_overridden_by_regular_approval(runtime):
    runtime.config.values["agent"]["security"] = {
        "decision_model": {"enabled": True, "screen": {"scripts": True}},
    }
    with patch.object(permissions.SecurityScreener, "screen", return_value=ScreeningResult(MALICIOUS, "destructive")):
        with pytest.raises(permissions.PermissionDenied, match="malicious"):
            permissions.request(runtime, version(runtime), "run")


@pytest.mark.parametrize("decision", ["approve", "reject", "escalate"])
def test_direct_final_request_gets_supervisor_comments_but_still_requires_admin(runtime, decision):
    item = version(runtime)
    assessment = {"decision": decision, "comments": "Public reads and local report output."}
    with patch("assistant_agent.development_supervisor.review", return_value=assessment) as review, \
            pytest.raises(permissions.PermissionRequired) as raised:
        permissions.final_request(runtime, item, ["api.example.com"], "Finished report")
    review.assert_called_once_with(runtime.config, None, {**item, "purpose": "Finished report"},
                                   ["api.example.com"], final=True)
    assert raised.value.request["supervisor"] == assessment
    assert raised.value.request["session_id"] is None
    assert permissions.run_status(runtime.db, item, runtime.job["id"]) == "not_approved"
    history = runtime.db.fetch_one("SELECT details FROM sandbox_permission_history WHERE action = 'final-review'")
    assert history["details"]["decision"] == decision
    assert history["details"]["version_id"] == item["id"]


def test_direct_final_supervisor_failure_is_explicit_and_never_grants_execution(runtime):
    from assistant_agent.development_supervisor import SupervisorError
    item = version(runtime)
    with patch("assistant_agent.development_supervisor.review", side_effect=SupervisorError("Provider unavailable")), \
            pytest.raises(permissions.PermissionRequired) as raised:
        permissions.final_request(runtime, item, [], "Finished report")
    assert raised.value.request["supervisor"] == {"decision": "unavailable", "comments": "Provider unavailable"}
    assert permissions.run_status(runtime.db, item, runtime.job["id"]) == "not_approved"


def test_networking_renewal_cannot_request_empty_hosts_or_inherit_old_grants(runtime):
    (runtime.shared_root / "report.py").write_text("import requests\nrequests.get('https://api.example.com')\n")
    original = version(runtime)
    approve(runtime, original, "host", ["api.example.com"])
    (runtime.shared_root / "report.py").write_text("import requests\nrequests.get('https://api.example.com/v2')\n")
    changed = version(runtime, image="image-v2")
    assert permissions.host_policy(runtime.db, runtime.config, changed["id"], runtime.job["id"])["allow"] == []
    with patch("assistant_agent.development_supervisor.review") as review:
        with pytest.raises(permissions.PermissionDenied, match="host list is empty"):
            permissions.final_request(runtime, changed, [], "Renew report")
        review.assert_not_called()
    with pytest.raises(permissions.PermissionRequired) as raised:
        permissions.final_request(runtime, changed, ["api.example.com"], "Renew report")
    assert raised.value.request["targets"] == ["api.example.com"]
    assert permissions.run_status(runtime.db, changed, runtime.job["id"]) == "not_approved"


def test_execution_requires_explicit_approval_tool_instead_of_opening_implicit_requests(runtime):
    with patch.object(ToolRuntime, "sandbox_control", return_value={"execution_identity": "image-v1"}), \
            patch("assistant_agent.tools.read_secret", return_value="token"), \
            patch("assistant_agent.tools.ssl.create_default_context"), \
            patch.object(permissions, "final_request") as request, \
            patch("assistant_agent.tools.open_https_request") as dispatch:
        with pytest.raises(permissions.PermissionDenied, match="cannot open implicit approval"):
            runtime.command_execute(["python3", "report.py"])
    request.assert_not_called()
    dispatch.assert_not_called()


def test_direct_final_review_can_serialize_stored_versions_with_database_timestamps(runtime):
    item = version(runtime)
    assert item["created_at"] is not None
    with pytest.raises(permissions.PermissionRequired) as raised:
        permissions.final_request(runtime, item, [], "Finished report")
    assert raised.value.request["supervisor"]["decision"] == "approve"
    assert raised.value.request["supervisor"]["comments"] == "Reads local input and writes a report."


def test_snapshot_contains_exact_approved_bytes(runtime):
    item = version(runtime)
    approve(runtime, item)
    files, _ = permissions.execution(runtime, item)
    assert base64.b64decode(files["helper.py"]) == b"value = 1\n"


def test_revoking_script_removes_host_and_run_grants(runtime):
    item = version(runtime)
    approve(runtime, item)
    approve(runtime, item, "host", ["api.example.com"])
    with patch.object(ToolRuntime, "sandbox_control", return_value={"status": "revoked"}):
        permissions.management(runtime.db, runtime.config, ["revoke-script", str(item["script_id"])], True)
    assert permissions.run_status(runtime.db, item, runtime.job["id"]) == "not_approved"
    assert "api.example.com" not in permissions.host_policy(runtime.db, runtime.config, item["id"], runtime.job["id"])["allow"]


def test_permission_keyboard_and_email_choices_are_explicit():
    buttons = permission_keyboard("abc", {"permission_type": "host", "reusable": True})
    assert any(button["text"] == "Approve always" for row in buttons for button in row)
    assert not any(":global" in button["callback_data"] for row in buttons for button in row)
    assert permissions.parse_decision("APPROVE ONCE - approve development session") == ("once", "")
    assert permissions.parse_decision("APPROVE ONCE - final script and requested hosts") == ("once", "")
    assert permissions.parse_decision("APPROVE ONCE - one execution") == ("once", "")
    assert permissions.parse_decision(
        "APPROVE ALWAYS - this unchanged final script version and requested hosts"
    ) == ("always", "")
    assert permissions.parse_decision("APPROVE ALWAYS - this unchanged script version") == ("always", "")
    with pytest.raises(ValueError):
        permissions.parse_decision("APPROVE ALWAYS FOR ALL SCRIPTS\nOnly public data.")
    with pytest.raises(ValueError):
        permissions.parse_decision("sure")
    assert permissions.requested_hosts(["https://Example.com/feed.rss", "example.com"]) == ["example.com"]
    for invalid in ("http://example.com/feed", "https://example.com:8443", "*.example.com", "https://user:pass@example.com"):
        with pytest.raises(ValueError):
            permissions.requested_hosts([invalid])


def test_uncertain_dispatch_does_not_retry_or_replenish_a_once_grant(runtime):
    item = version(runtime)
    approve(runtime, item, decision="once")
    with patch.object(ToolRuntime, "sandbox_control", return_value={
        "permission_epoch": "epoch", "execution_identity": "image-v1",
    }), patch("assistant_agent.tools.read_secret", return_value="token"), \
            patch("assistant_agent.tools.ssl.create_default_context"), \
            patch("assistant_agent.tools.open_https_request", side_effect=urllib.error.URLError("response lost")) as send:
        with pytest.raises(SandboxAttemptsExhausted):
            runtime.command_execute(["python3", "report.py"])
        assert send.call_count == 1
        assert permissions.run_status(runtime.db, item, runtime.job["id"]) == "not_approved"


def test_execution_uses_current_image_authorization_after_rebuild(runtime):
    item = version(runtime, image="old-image")
    approve(runtime, item)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {"exit_code": 0, "stdout": "", "stderr": "", "egress": "none"}).encode()
    with patch.object(ToolRuntime, "sandbox_control", return_value={
        "permission_epoch": "current-epoch", "execution_identity": "rebuilt-image",
    }), patch("assistant_agent.tools.read_secret", return_value="token"), \
            patch("assistant_agent.tools.ssl.create_default_context"), \
            patch("assistant_agent.tools.open_https_request", return_value=response) as send:
        runtime.command_execute(["python3", "report.py"])
    payload = json.loads(send.call_args.args[0].data)
    assert payload["execution_identity"] == "rebuilt-image"
    assert payload["permission_epoch"] == "current-epoch"
    assert payload["approved_files"]["report.py"]
    assert permissions.run_status(runtime.db, item, runtime.job["id"]) == "approved"


def test_screening_outage_is_not_cached_as_a_success(runtime):
    runtime.config.values["agent"]["security"] = {
        "decision_model": {"enabled": True, "screen": {"scripts": True}},
    }
    with patch.object(permissions.SecurityScreener, "screen", return_value=ScreeningResult(
        POSSIBLY_MALICIOUS, "unavailable", error="timeout"
    )) as screen:
        for _ in range(2):
            with pytest.raises(permissions.PermissionRequired):
                permissions.request(runtime, version(runtime), "run")
        assert screen.call_count == 2


def test_concurrent_screening_is_single_flight_per_version(runtime):
    runtime.config.values["agent"]["security"] = {
        "decision_model": {"enabled": True, "screen": {"scripts": True}},
    }
    item = version(runtime)
    with patch.object(permissions.SecurityScreener, "screen", return_value=ScreeningResult(
        POSSIBLY_MALICIOUS, "requires review"
    )) as screen:
        with ThreadPoolExecutor(max_workers=2) as pool:
            findings = list(pool.map(lambda _: permissions.screen(runtime, item), range(2)))
        assert findings[0] == findings[1]
        assert screen.call_count == 1


def test_javascript_local_dependency_changes_invalidate_approval(runtime):
    (runtime.shared_root / "job.js").write_text("import './helper.js';\n")
    (runtime.shared_root / "helper.js").write_text("export const value = 1;\n")
    original = version(runtime, ["node", "job.js"])
    assert original["reusable"]
    assert {f["path"] for f in original["manifest"]["files"]} == {"job.js", "helper.js"}
    (runtime.shared_root / "helper.js").write_text("export const value = 2;\n")
    assert version(runtime, ["node", "job.js"])["fingerprint"] != original["fingerprint"]


def test_upgrade_preserves_existing_network_rules(db):
    db.execute("INSERT INTO sandbox_network_rules(host, decision) VALUES ('legacy.example.com', 'allow')")
    db.execute("DROP TABLE sandbox_development_reviews, sandbox_development_sessions, sandbox_permission_history, sandbox_permission_grants, sandbox_script_versions, sandbox_scripts")
    db.execute("DELETE FROM schema_migrations WHERE filename IN "
               "('015_script_permissions.sql', '016_development_sessions.sql', '018_stable_script_approvals.sql')")
    assert db.run_migrations() == ["015_script_permissions.sql", "016_development_sessions.sql",
                                   "018_stable_script_approvals.sql"]
    assert db.fetch_one("SELECT host FROM sandbox_network_rules")["host"] == "legacy.example.com"
    assert db.run_migrations() == []


def test_agent_tool_pauses_email_approval_resumes_and_exact_execution_consumes_grant(runtime):
    import re
    from assistant_agent.task_agent import TaskAgent
    from assistant_agent.email_approvals import EmailApprovals
    runtime.config.values["agent"].update({
        "admin": {"email": "admin@example.com"},
        "email": {"smtp_host": "smtp.example.com"},
    })
    runtime.config.values["agent"]["sandbox"]["base_url"] = "https://sandbox:8080"
    agent = TaskAgent.__new__(TaskAgent)
    agent.db, agent.config = runtime.db, runtime.config
    health = {"permission_epoch": "epoch", "execution_identity": "image-v1"}
    with patch.object(ToolRuntime, "sandbox_control", return_value=health), \
            patch.object(agent, "pause_for_review", return_value=True):
        assert agent.handle_tool_call(
            runtime.job, [], runtime, "call1", "script_approval_request",
            {"command": ["python3", "report.py"], "purpose": "Daily report", "urls": []}, {"script_approval_request"},
        )
    job = runtime.db.fetch_one("SELECT * FROM jobs WHERE id = %s", (runtime.job["id"],))
    runtime.db.update_job_status(job["id"], "needs_review")
    assert job["metadata"]["permission_request"]["kind"] == "final"
    with patch("assistant_agent.email_approvals.send_admin_email") as send:
        assert permissions.send_approval(runtime.db, runtime.config, job)
    body = send.call_args.args[2]
    assert "report.py" in send.call_args.kwargs["attachment"][1].decode()
    token = re.search(r"OTP: (\S+)", body).group(1)
    request_hash = re.search(r"Request hash: (\S+)", body).group(1)
    payload = EmailApprovals(runtime.db, runtime.config).consume(token, request_hash)
    permissions.apply_decision(runtime.db, payload, "once")
    with pytest.raises(ValueError, match="already used"):
        EmailApprovals(runtime.db, runtime.config).consume(token, request_hash)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "exit_code": 0, "stdout": "1", "stderr": "", "egress": "proxy", "network_events": [],
    }).encode()
    with patch.object(ToolRuntime, "sandbox_control", return_value=health), \
            patch("assistant_agent.tools.read_secret", return_value="token"), \
            patch("assistant_agent.tools.ssl.create_default_context"), \
            patch("assistant_agent.tools.open_https_request", return_value=response):
        assert runtime.command_execute(["python3", "report.py"])["exit_code"] == 0
    item = version(runtime)
    assert permissions.run_status(runtime.db, item, runtime.job["id"]) == "not_approved"
