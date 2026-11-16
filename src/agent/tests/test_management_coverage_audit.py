import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "audit_management_api", ROOT / "src/scripts/audit_management_api.py",
)
audit_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_module)


def test_audit_discovers_catalog_services_and_unclassified_surfaces():
    report = audit_module.audit()
    assert {row["name"] for row in report["settings"]} == {
        row["env"] for row in audit_module.settings_schema.catalog()
    } | {row["env"] for row in audit_module.dynamic_calendar_settings()}
    assert {"management", "management-docker", "task-agent"} <= {
        row["name"] for row in report["services"]
    }
    assert all(row["restart"] in {"blocked", "unverified"} for row in report["services"])
    assert next(row for row in report["services"] if row["name"] == "sandbox-init")["one_shot"]
    assert not next(row for row in report["services"] if row["name"] == "task-agent")["one_shot"]
    assert any("/admin permissions" in row["command"] for row in report["admin_commands"])
    assert all(row["status"] == "contract_present" for row in report["admin_commands"])
    assert {row["method"] for row in report["tui_backend_methods"]} >= {"request", "save", "lifecycle"}
    assert all(row["status"] in {"contract_present", "client_local"} for row in report["tui_backend_methods"])
    assert all(row["status"] in {"contract_present", "restricted"} for row in report["deployment_variables"])
    assert all(row["status"] == "handler_present" for row in report["executor_reads"])
    assert audit_module.compose_variables("${SET:-x} $${CONTAINER_VAR} ${SET}") == ["SET"]
    assert any(row["name"] == "deep_research_request" for row in report["model_tools"])
    deletion = next(row for row in report["requirements"] if row["name"] == "task.delete")
    assert deletion["test_evidence"]["present"]
    assert deletion["test_evidence"]["test"].endswith("::test_task_deletion_through_management_api")
    recovery = next(row for row in report["requirements"] if row["name"] == "database.recover")
    assert recovery["test_evidence"]["present"]
    assert recovery["test_evidence"]["test"].endswith(
        "::SetupTest::test_volume_database_rotation_recovery_keeps_journal_out_of_host_files"
    )
    assert all(row["status"] in {"contract_present", "agent_mediated", "worker_internal"}
               for row in report["model_tools"])
    assert next(row for row in report["model_tools"] if row["name"] == "reminder_create")["status"] == "contract_present"
    assert "src/transcription/server.py" in report["source_files"]
    assert any(row["symbol"] == "ClientChannels.poll" for row in report["entry_points"])
    assert any(row["name"] == "domain.contacts.create" for row in report["requirements"])
    assert all(row["test_evidence"] and row["test_evidence"]["present"] for row in report["requirements"])


def test_new_domain_actions_do_not_inherit_unrelated_behavioral_evidence(monkeypatch):
    domain_catalog = audit_module.domain_catalog()
    domain_catalog["actions"].append({"action": "memory.export", "access": "read"})
    monkeypatch.setattr(audit_module, "domain_catalog", lambda: domain_catalog)
    report = audit_module.audit()
    requirement = next(row for row in report["requirements"] if row["name"] == "domain.memory.export")
    assert requirement["test_evidence"] is None


def test_audit_catches_missing_dynamic_calendar_fields(monkeypatch):
    fields = audit_module.ManagementService._setting_specs()
    fields.pop("AGENT_CALENDAR_GOOGLE_2_CLIENT_SECRET")
    monkeypatch.setattr(audit_module.ManagementService, "_setting_specs", lambda: fields)
    report = audit_module.audit()
    assert next(row for row in report["settings"]
                if row["name"] == "AGENT_CALENDAR_GOOGLE_2_CLIENT_SECRET")["status"] == "missing"


def test_compose_inventory_rejects_duplicate_keys_but_allows_merge_overrides():
    with pytest.raises(ValueError, match="Duplicate Compose key 'healthcheck'"):
        audit_module.load_compose("services:\n  worker:\n    healthcheck: {}\n    healthcheck: {}\n")
    compose = audit_module.load_compose(
        "common: &defaults\n  healthcheck: {}\nservices:\n  worker:\n    <<: *defaults\n    healthcheck: false\n"
    )
    assert compose["services"]["worker"]["healthcheck"] is False


def test_runtime_environment_discovery_and_unknown_classification(tmp_path, monkeypatch):
    source = tmp_path / "src/agent/src/assistant_agent"
    source.mkdir(parents=True)
    (source / "example.py").write_text(
        'os.getenv("AGENT_FUTURE_SETTING")\n'
        'os.environ.get("SANDBOX_FUTURE_SETTING")\n'
        'os.environ["ASSISTANT_FUTURE_SETTING"]\n'
        'read_secret("AGENT_FUTURE_SECRET")\n',
        encoding="utf-8",
    )
    variables = audit_module.runtime_environment(tmp_path)
    assert set(variables) == {
        "AGENT_FUTURE_SETTING", "SANDBOX_FUTURE_SETTING", "ASSISTANT_FUTURE_SETTING", "AGENT_FUTURE_SECRET",
    }
    monkeypatch.setattr(audit_module, "runtime_environment", lambda: variables)
    report = audit_module.audit()
    assert all(row["status"] == "unclassified" for row in report["runtime_variables"])


def test_audit_catches_missing_routes_and_implementation_mapping(monkeypatch):
    routes = audit_module.routes()
    routes.remove(("POST", "/v2/configuration/preview"))
    monkeypatch.setattr(audit_module, "routes", lambda: routes)
    monkeypatch.setattr(audit_module, "MUTATIONS", {})
    report = audit_module.audit()
    assert all(row["status"] == "missing" for row in report["settings"])
    requirements = {row["name"]: row["status"] for row in report["requirements"]}
    assert requirements["task.cancel"] == "missing"
    assert next(row for row in report["admin_commands"]
                if row["command"] == "/admin cancel JOB_ID")["status"] == "missing"


def test_audit_catches_missing_private_executor_read(tmp_path, monkeypatch):
    source = tmp_path / "src/agent/src/assistant_agent/management_executor.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        'class Handler:\n'
        '    def read(self, service, request):\n'
        '        name = request.request\n'
        '        if name == "tasks.messages":\n'
        '            return service.task_messages()\n'
        '        raise ValueError("unsupported")\n',
        encoding="utf-8",
    )
    discover = audit_module.executor_read_handlers
    monkeypatch.setattr(audit_module, "executor_read_handlers", lambda: discover(tmp_path))
    reads = {row["name"]: row["status"] for row in audit_module.executor_reads()}
    assert reads["tasks.messages"] == "handler_present"
    assert reads["tasks.deletion-preview"] == "missing"


def test_test_execution_distinguishes_passes_skips_and_missing_cases(monkeypatch):
    def run(command, **kwargs):
        destination = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml="))
        Path(destination).write_text(
            '<testsuites><testsuite>'
            '<testcase classname="src.agent.tests.test_example" name="test_one[1]"/>'
            '<testcase classname="src.agent.tests.test_example" name="test_one[2]"><skipped/></testcase>'
            '<testcase classname="src.agent.tests.test_example.ExampleTest" name="test_two"/>'
            '<testcase classname="src.agent.tests.test_example" name="test_three"><failure/></testcase>'
            '</testsuite></testsuites>',
            encoding="utf-8",
        )
        return type("Result", (), {"returncode": 1})()

    monkeypatch.setattr(audit_module.subprocess, "run", run)
    evidence = {
        "first": {"test": "src/agent/tests/test_example.py::test_one", "present": True},
        "second": {"test": "src/agent/tests/test_example.py::ExampleTest::test_two", "present": True},
        "third": {"test": "src/agent/tests/test_example.py::test_three", "present": True},
        "missing": {"test": "src/agent/tests/test_example.py::test_missing", "present": True},
    }
    result = audit_module.run_test_evidence(evidence)
    assert result["exit_code"] == 1
    assert result["tests"] == {
        "src/agent/tests/test_example.py::test_one": "skipped",
        "src/agent/tests/test_example.py::ExampleTest::test_two": "passed",
        "src/agent/tests/test_example.py::test_three": "failed",
        "src/agent/tests/test_example.py::test_missing": "not_collected",
    }


def test_new_admin_command_requires_classification(monkeypatch):
    monkeypatch.setattr(audit_module, "HELP", audit_module.HELP + "\n/admin new-action\n")
    report = audit_module.audit()
    assert next(row for row in report["admin_commands"]
                if row["command"] == "/admin new-action")["status"] == "unclassified"


def test_tui_backend_methods_require_explicit_mapping(monkeypatch):
    mappings = dict(audit_module.TUI_REQUIREMENTS)
    mappings.pop("lifecycle")
    monkeypatch.setattr(audit_module, "TUI_REQUIREMENTS", mappings)
    report = audit_module.audit()
    assert next(row for row in report["tui_backend_methods"]
                if row["method"] == "lifecycle")["status"] == "unclassified"


def test_json_output_is_machine_readable_and_does_not_claim_verified(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["audit_management_api.py", "--json"])
    assert audit_module.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["settings"]
    assert result["entry_points"]
    assert result["routes"]
    assert all(row["status"] != "verified" for row in result["settings"])
    assert any("not verified" in limitation for limitation in result["limitations"])
    assert result["verification"] == "static_contract_only"
    assert not result["coverage_failures"]
    assert audit_module.coverage_failures(result, require_behavior=True)
