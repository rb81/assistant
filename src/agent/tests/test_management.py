import io
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from assistant_agent.config import AppConfig
from assistant_agent.management import GROUPS, Management
from assistant_agent.management_operator import execute
from assistant_agent.setup import DockerCommandError, Installation, SetupError
from assistant_agent.setup_privileges import authorization_handler, terminal_authorization
from test_network_access import db  # noqa: F401


def configured(root):
    installation = Installation(root)
    installation.values = {
        "AGENT_NAME": "Original", "AGENT_TIMEZONE": "UTC", "AGENT_LLM_MODEL": "model",
        "AGENT_LLM_BASE_URL": "https://openrouter.ai/api/v1",
        "COMPOSE_PROFILES": "custom", "UNRELATED": "preserve",
        "AGENT_SECURITY_EMAIL_AUTH_ENABLED": "false",
    }
    installation.secret_values = {"POSTGRES_PASSWORD": "database-password", "OPENROUTER_API_KEY": "model-key"}
    installation.prompt_copy = b"Ask for approval before external actions."
    installation.state["setup_complete"] = True
    installation.save()
    return installation


def management(root):
    backend = Management(root, Mock(return_value=False))
    backend.load()
    return backend


def test_native_forms_save_reviewed_configuration_without_mutating_the_live_snapshot():
    with tempfile.TemporaryDirectory() as root:
        original = configured(root)
        backend = management(root)
        values = {spec.key: backend.value(spec) for spec in GROUPS["identity"][1]}
        values["AGENT_NAME"] = "Changed"
        backend.stage("identity", values)
        assert backend.installation.values["AGENT_NAME"] == "Original"
        assert Installation(root).values["AGENT_NAME"] == "Original"
        assert backend.problems() == []
        assert backend.installation.values["AGENT_NAME"] == "Original"
        backend.save()
        reopened = Installation(root)
        assert reopened.values["AGENT_NAME"] == "Changed"
        assert reopened.values["UNRELATED"] == "preserve"
        assert reopened.values["COMPOSE_PROFILES"] == "custom"
        assert reopened.secret_values == original.secret_values
        assert not backend.dirty


def test_invalid_form_staging_is_atomic_and_does_not_replace_database_password():
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        values = {spec.key: backend.value(spec) for spec in GROUPS["models"][1]}
        values["AGENT_LLM_MODEL"] = "different-model"
        values["POSTGRES_PASSWORD"] = "replacement-password"
        before = dict(backend.values)
        with pytest.raises(SetupError, match="Rotate database"):
            backend.stage("models", values)
        assert backend.values == before
        assert backend.credentials["POSTGRES_PASSWORD"] == "database-password"


def test_save_validates_the_entire_draft_and_preserves_files_on_failure():
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        original = backend.installation.env_original
        backend.values["AGENT_TIMEZONE"] = "invalid/timezone"
        with pytest.raises(SetupError, match="timezone"):
            backend.save()
        assert Path(root, "data/.env").read_bytes() == original


def test_failed_first_save_preserves_the_saved_snapshot_and_keeps_the_draft():
    with tempfile.TemporaryDirectory() as root:
        backend = management(root)
        backend.accepted = True
        backend.values.update(AGENT_LLM_MODEL="model", AGENT_LLM_BASE_URL="https://openrouter.ai/api/v1")
        backend.credentials.update(POSTGRES_PASSWORD="database-password", OPENROUTER_API_KEY="model-key")
        backend.instructions = b"Ask for approval before external actions."
        backend.dirty = True
        with patch("assistant_agent.setup.write_transaction", side_effect=SetupError("Write failed")):
            with pytest.raises(SetupError, match="Write failed"):
                backend.save()
        assert backend.installation.state == {"pending": []}
        assert backend.installation.secret_values == {}
        assert backend.installation.tls_files == {}
        assert backend.values["AGENT_LLM_MODEL"] == "model"
        assert backend.dirty
        assert not Path(root, "data/.env").exists()


def test_services_parse_compose_json_lines_and_include_not_created_components():
    with tempfile.TemporaryDirectory() as root:
        backend = management(root)
        rows = [
            {"Service": "task-agent", "State": "running", "Health": "healthy"},
            {"Service": "postgres", "State": "exited", "Health": ""},
        ]
        with patch.object(backend, "access"), patch.object(
            backend.installation, "compose",
            side_effect=["task-agent\npostgres\nreminder-scheduler", "\n".join(map(json.dumps, rows))],
        ):
            result = backend.services()
        by_name = {row["id"]: row for row in result}
        assert by_name["task-agent"]["health"] == "healthy"
        assert by_name["postgres"]["state"] == "exited"
        assert by_name["reminder-scheduler"]["state"] == "not created"


def test_service_actions_validate_the_service_and_never_repeat_failed_operations():
    with tempfile.TemporaryDirectory() as root:
        backend = management(root)
        with patch.object(backend, "access"), patch.object(
            backend.installation, "compose", return_value="task-agent\n"
        ) as compose:
            with pytest.raises(SetupError, match="no longer configured"):
                backend.lifecycle("restart", "missing-service")
        compose.assert_called_once_with(["config", "--services"])
        with patch.object(backend, "access"), patch.object(
            backend.installation, "compose",
            side_effect=["task-agent\n", DockerCommandError("Docker failed")],
        ) as compose:
            with pytest.raises(DockerCommandError):
                backend.lifecycle("restart", "task-agent")
        assert compose.call_count == 2
        assert compose.call_args.args[0] == ["restart", "task-agent"]


def test_structured_transport_reuses_the_image_and_passes_requests_via_stdin_without_building():
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        response = '{"rows": [{"id": 7, "status": "running"}], "total": 1}'
        with patch.object(backend, "access"), patch.object(
            backend.installation, "compose", side_effect=["", response, response]
        ) as compose:
            result = backend.request({"kind": "tasks", "page": 0})
            backend.request({"kind": "tasks", "page": 1})
        assert result["rows"][0]["id"] == 7
        probe = compose.call_args_list[0].args[0]
        assert probe[-3:] == ["task-agent", "-c", "import assistant_agent.management_operator"]
        assert "--no-build" in probe
        assert probe[probe.index("--pull") + 1] == "never"
        assert "management_operator" in compose.call_args_list[1].args[0][-1]
        assert "--no-build" in compose.call_args.args[0]
        assert str(Path(root).resolve() / "src/agent/src") + ":/app/src:ro" in compose.call_args.args[0]
        assert json.loads(compose.call_args.kwargs["input_text"])["page"] == 1


def test_missing_management_image_requires_explicit_start_instead_of_a_hidden_build():
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        with patch.object(backend, "access"), patch.object(
            backend.installation, "compose", side_effect=DockerCommandError("Image is unavailable.")
        ) as compose:
            with pytest.raises(SetupError, match="Services > Start app"):
                backend.request({"kind": "memories"})
        assert compose.call_count == 1
        assert "--no-build" in compose.call_args.args[0]
        assert "build" not in compose.call_args.args[0]
        assert not backend.helper_ready


def test_project_transport_reuses_management_preparation_without_building_another_image():
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        with patch.object(backend, "access"), patch.object(
            backend.installation, "compose",
            side_effect=["", '{"rows": [], "total": 0}', '{"preview_digest": "reviewed"}'],
        ) as compose:
            backend.request({"kind": "memories"})
            assert backend.preview_project(7)["preview_digest"] == "reviewed"
        assert compose.call_count == 3
        assert "--no-build" in compose.call_args.args[0]


def test_collection_search_and_pagination_use_parameters_not_sql_interpolation():
    db = Mock()
    db.fetch_one.return_value = {"total": 19}
    db.fetch_all.return_value = [{"id": 7, "content": "Useful preference"}]
    query = "x'; DROP TABLE jobs; --"
    result = execute(db, AppConfig({}), {"kind": "memories", "page": 2, "query": query})
    assert result["total"] == 19
    sql, parameters = db.fetch_all.call_args.args
    assert query not in sql
    assert parameters == ("%" + query + "%", 16)


def test_mutations_require_explicit_confirmation_and_reuse_the_existing_admin_backend():
    db = Mock()
    config = AppConfig({})
    with pytest.raises(ValueError, match="confirmation"):
        execute(db, config, {"kind": "mutate", "action": "cancel", "id": 9})
    db.fetch_one.assert_not_called()
    with patch("assistant_agent.management_operator.AdminCommands") as admin:
        admin.return_value.execute.return_value = '{"id": 9, "status": "cancelled"}'
        result = execute(db, config, {"kind": "mutate", "action": "cancel", "id": 9, "confirmed": True})
    admin.return_value.execute.assert_called_once_with("/admin cancel 9", "", confirmed=True)
    assert result["result"]["status"] == "cancelled"


@pytest.mark.parametrize("payload", [
    {"kind": "tasks", "page": -1}, {"kind": "memories", "id": "1; drop table"},
    {"kind": "mutate", "action": "shell", "confirmed": True},
    {"kind": "mutate", "action": "create-memory", "confirmed": True, "body": " "},
])
def test_invalid_management_requests_do_not_touch_database(payload):
    db = Mock()
    with pytest.raises(ValueError):
        execute(db, AppConfig({}), payload)
    db.fetch_one.assert_not_called()
    db.fetch_all.assert_not_called()


def test_sudo_authorization_only_repeats_policy_checks_not_mutations():
    with tempfile.TemporaryDirectory() as root:
        instance = Installation(root)
        instance.use_sudo = True
        authorize = Mock(return_value=True)
        results = [
            subprocess.CompletedProcess([], 1, "", "sudo: a password is required"),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "done", ""),
        ]
        with authorization_handler(authorize), patch(
            "assistant_agent.setup.subprocess.run", side_effect=results
        ) as run:
            assert instance.docker(["compose", "restart", "task-agent"]) == "done"
        authorize.assert_called_once()
        assert run.call_args_list[0].args == run.call_args_list[1].args
        assert run.call_args_list[0].args[0][:4] == ["sudo", "-n", "-l", "--"]
        assert run.call_args.args[0] == ["sudo", "-n", "docker", "compose", "restart", "task-agent"]


def test_native_password_prompt_inherits_the_terminal_without_a_second_legacy_menu():
    with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), \
            patch("questionary.confirm") as confirm, patch("builtins.print"), \
            patch("assistant_agent.setup_privileges.subprocess.run",
                  return_value=subprocess.CompletedProcess([], 0)) as run:
        assert terminal_authorization(confirmed=True)
    confirm.assert_not_called()
    run.assert_called_once_with(["sudo", "-v"], check=False)


@pytest.mark.parametrize("blocker", ["changed", "running_jobs", "blocked_files"])
def test_project_deletion_rechecks_inventory_before_delete_and_restores_writers(blocker):
    with tempfile.TemporaryDirectory() as root:
        backend = management(root)
        preview = {"preview_digest": "new" if blocker == "changed" else "reviewed"}
        if blocker != "changed":
            preview[blocker] = [9]
        with patch.object(backend, "access"), patch.object(
            backend.installation, "compose", return_value="postgres\ntask-agent\n"
        ) as compose, patch.object(backend, "project_request", side_effect=[
            preview, {"deletions": []},
        ]) as helper:
            with pytest.raises(SetupError, match="Nothing was deleted"):
                backend.delete_project(7, "reviewed")
        assert [call.args[0] for call in helper.call_args_list] == ["preview", "pending"]
        assert compose.call_args_list[1].args[0] == ["stop", "--timeout", "120", "task-agent"]
        assert compose.call_args.args[0] == ["start", "task-agent"]


@pytest.mark.parametrize("result, pending, restored", [
    ({"status": "completed"}, {"deletions": []}, True),
    ({"status": "failed"}, {"deletions": [{"id": 8}]}, False),
    ({"status": "failed"}, {"deletions": []}, False),
    ({"status": "completed"}, {}, False),
    ({"status": "completed"}, SetupError("Database unavailable"), False),
])
def test_project_deletion_only_restores_writers_after_verified_completion(result, pending, restored):
    with tempfile.TemporaryDirectory() as root:
        backend = management(root)
        with patch.object(backend, "access"), patch.object(
            backend.installation, "compose", return_value="task-agent\n"
        ) as compose, patch.object(backend, "project_request", side_effect=[
            {"preview_digest": "reviewed"}, result, pending,
        ]) as helper:
            if restored:
                backend.delete_project(7, "reviewed")
            else:
                with pytest.raises(SetupError, match="stopped"):
                    backend.delete_project(7, "reviewed")
        assert helper.call_args_list[1].args == ("delete", {
            "workstream_id": 7, "mode": "preserve_shared", "preview_digest": "reviewed",
            "confirmed": True, "writers_stopped": True,
        })
        assert (compose.call_args.args[0] == ["start", "task-agent"]) is restored


def test_resuming_project_deletion_keeps_writers_stopped_until_completion_is_verified():
    with tempfile.TemporaryDirectory() as root:
        backend = management(root)
        with patch.object(backend, "access"), patch.object(
            backend.installation, "compose", return_value="task-agent\n"
        ) as compose, patch.object(backend, "project_request", side_effect=[
            {"deletions": [{"id": 8, "status": "failed"}]}, {"status": "completed"}, {"deletions": []},
        ]) as helper:
            backend.resume_deletion(8)
        assert helper.call_args_list[1].args == (
            "resume", {"deletion_id": 8, "confirmed": True, "writers_stopped": True})
        assert compose.call_args.args[0] == ["start", "task-agent"]


def test_viewing_an_unchanged_form_does_not_create_a_dirty_draft_or_pin_defaults():
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        before = dict(backend.values)
        backend.stage("email", {spec.key: backend.value(spec) for spec in GROUPS["email"][1]})
        assert backend.values == before
        assert not backend.dirty


def test_editing_unrelated_settings_preserves_existing_profiles_until_explicitly_disabled():
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        backend.values["COMPOSE_PROFILES"] = "custom,email,telegram"
        assert backend.draft().values["COMPOSE_PROFILES"] == "custom,email,telegram"
        backend.values["TELEGRAM_ENABLED"] = "false"
        assert backend.draft().values["COMPOSE_PROFILES"] == "custom,email"


def test_logs_redact_saved_credentials_even_when_a_replacement_is_in_the_draft():
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        backend.credentials["OPENROUTER_API_KEY"] = "replacement-key"
        assert backend.redact("model-key and replacement-key") == "[hidden] and [hidden]"


@pytest.mark.parametrize("response", [
    '{}', '{"rows": null, "total": 0}', '{"rows": [null], "total": 1}',
    '{"rows": [], "total": -1}', '{"rows": [], "total": true}',
])
def test_malformed_record_responses_are_reported_instead_of_crashing_the_ui(response):
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        backend.helper_ready = True
        with patch.object(backend, "access"), patch.object(backend.installation, "compose", return_value=response):
            with pytest.raises(SetupError, match="response was invalid"):
                backend.request({"kind": "memories"})


def test_search_status_uses_existing_runtime_instead_of_building_an_image():
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        backend.helper_ready = True
        with patch.object(backend, "access"), patch.object(
            backend.installation, "compose", return_value='{"pending": 0}'
        ) as compose:
            assert backend.embeddings("status") == {"pending": 0}
        assert "--no-build" in compose.call_args.args[0]
        assert compose.call_count == 1


def test_script_details_refresh_live_block_state_as_well_as_the_version():
    db = Mock()
    db.fetch_one.return_value = {"id": 7, "entry": "report.py", "blocked": True}
    with patch("assistant_agent.script_permissions.management", return_value={
        "version": {"id": 9, "script_id": 7}, "grants": [],
    }):
        result = execute(db, AppConfig({}), {"kind": "script", "id": 9})
    assert result["blocked"] is True
    assert result["id"] == 7
    assert db.fetch_one.call_args.args[1] == (7,)


def test_script_search_pages_beyond_the_old_hundred_row_limit():
    db = Mock()
    db.fetch_all.return_value = [{"id": 7, "entry": "report.py"}]
    db.fetch_one.return_value = {"total": 145}
    result = execute(db, AppConfig({}), {"kind": "scripts", "page": 15, "query": "report"})
    assert result["total"] == 145
    sql, parameters = db.fetch_all.call_args.args
    assert "report" not in sql
    assert parameters == ("%report%", 8, 120)


def test_service_polling_ignores_changing_uptime_when_state_and_health_are_unchanged():
    with tempfile.TemporaryDirectory() as root:
        backend = management(root)
        first = {"Service": "task-agent", "State": "running", "Health": "healthy",
                 "Status": "Up 20 seconds", "RunningFor": "20 seconds ago"}
        second = {**first, "Status": "Up 25 seconds", "RunningFor": "25 seconds ago"}
        with patch.object(backend, "access"), patch.object(
            backend.installation, "compose", side_effect=[
                "task-agent", json.dumps(first), "task-agent", json.dumps(second),
            ],
        ):
            assert backend.services() == backend.services()


def test_secret_form_edits_preserve_significant_leading_and_trailing_spaces():
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        values = {spec.key: backend.value(spec) for spec in GROUPS["email"][1]}
        values["IMAP_PASSWORD"] = " secret with spaces "
        backend.stage("email", values)
        assert backend.credentials["IMAP_PASSWORD"] == " secret with spaces "


def test_telegram_form_preserves_yaml_recipients_as_valid_json():
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        backend.installation.defaults = AppConfig({"agent": {
            "telegram": {"recipients": {"admin@example.com": "123"}},
            "org": {"internal_email_domains": ["example.com"]},
        }})
        specs = {spec.key: spec for spec in GROUPS["telegram"][1]}
        assert json.loads(backend.value(specs["TELEGRAM_RECIPIENTS"])) == {"admin@example.com": "123"}
        domains = next(spec for spec in GROUPS["identity"][1] if spec.key == "ORG_INTERNAL_EMAIL_DOMAINS")
        assert backend.value(domains) == "example.com"
        backend.values.update(TELEGRAM_ENABLED="true", TELEGRAM_ALLOWED_USER_IDS="123")
        backend.credentials["TELEGRAM_BOT_TOKEN"] = "bot-token"
        backend.stage("telegram", {spec.key: backend.value(spec) for spec in specs.values()})
        assert not [message for section, message in backend.problems() if section == "telegram"]


@pytest.mark.parametrize("token, identifiers, recipients, message", [
    ("", "123", "{}", "TELEGRAM_BOT_TOKEN"),
    ("bot-token", "", "{}", "positive numeric"),
    ("bot-token", "-123", "{}", "positive numeric"),
    ("bot-token", "123", "admin@example.com=123", "JSON object"),
    ("bot-token", "123", '{"admin@example.com": "456"}', "authorized numeric"),
])
def test_telegram_validation_blocks_unusable_native_configuration(token, identifiers, recipients, message):
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        backend.values.update(TELEGRAM_ENABLED="true", TELEGRAM_ALLOWED_USER_IDS=identifiers,
                              TELEGRAM_RECIPIENTS=recipients)
        backend.credentials["TELEGRAM_BOT_TOKEN"] = token
        assert any(section == "telegram" and message in error for section, error in backend.problems())
        with pytest.raises(SetupError, match=message):
            backend.save()


def test_native_save_requires_nonempty_instructions_and_does_not_change_saved_files():
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        backend.instructions = b" \n"
        assert ("instructions", "Review and save nonempty assistant instructions before completing setup.") in backend.problems()
        original = backend.installation.env_original
        with pytest.raises(SetupError, match="nonempty assistant instructions"):
            backend.save()
        assert Path(root, "data/.env").read_bytes() == original
        assert Path(root, "data/config/AGENT.md").read_bytes().strip()


def test_database_password_generation_does_not_replace_an_existing_database():
    with tempfile.TemporaryDirectory() as root:
        backend = management(root)
        pg_version = Path(root, "data/postgres/pgdata/PG_VERSION")
        pg_version.parent.mkdir(parents=True)
        pg_version.write_text("17")
        with pytest.raises(SetupError, match="Existing PostgreSQL data"):
            backend.generate_database_password()
        assert backend.credentials == {}


def test_named_credential_activation_only_applies_the_requested_pending_credential():
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        with patch.object(backend, "access"), patch.object(backend, "load"), \
                patch.object(backend.installation, "apply") as apply:
            backend.apply_credentials("OPENROUTER_API_KEY")
            apply.assert_called_once_with(["OPENROUTER_API_KEY"])
            with pytest.raises(SetupError, match="no pending activation"):
                backend.apply_credentials("UNKNOWN")
            assert apply.call_count == 1


def test_start_requires_saved_setup_and_reports_the_build_in_progress():
    with tempfile.TemporaryDirectory() as root:
        backend = management(root)
        with patch.object(backend, "access"), patch.object(backend.installation, "start_containers") as start, \
                patch.object(backend, "services", return_value=[]):
            with pytest.raises(SetupError, match="Complete and save setup"):
                backend.lifecycle("start")
            start.assert_not_called()
            backend.installation.state["setup_complete"] = True
            def starting():
                assert backend.progress == "Building and starting the application"
            start.side_effect = starting
            backend.lifecycle("start")
            start.assert_called_once()


def test_native_validation_honors_models_and_integration_modes_from_yaml():
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        backend.values.pop("AGENT_LLM_MODEL")
        backend.values.pop("AGENT_LLM_BASE_URL")
        backend.installation.defaults = AppConfig({"agent": {
            "llm": {"model": "yaml-model", "base_url": "https://openrouter.ai/api/v1"},
            "zoom": {"auth_mode": "s2s"},
            "calendar": {"default_provider": "google"},
        }})
        backend.values.update(ZOOM_ENABLED="true", CALENDAR_ENABLED="true")
        backend.credentials.update({
            "ZOOM_CLIENT_ID": "client", "ZOOM_CLIENT_SECRET": "client-secret", "ZOOM_ACCOUNT_ID": "account",
            "CALENDAR_GOOGLE_CLIENT_ID": "client", "CALENDAR_GOOGLE_CLIENT_SECRET": "client-secret",
            "CALENDAR_GOOGLE_REFRESH_TOKEN": "refresh-token",
        })
        assert backend.problems() == []


@pytest.mark.parametrize("enabled", ["true", "1", "yes", "on"])
def test_native_boolean_fields_preserve_supported_enabled_values_and_profiles(enabled):
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        backend.values.update(TELEGRAM_ENABLED=enabled, COMPOSE_PROFILES="custom,telegram")
        spec = GROUPS["telegram"][1][0]
        assert backend.value(spec) == "true"
        backend.stage("telegram", {field.key: backend.value(field) for field in GROUPS["telegram"][1]})
        assert not backend.dirty
        assert backend.values["TELEGRAM_ENABLED"] == enabled
        assert backend.draft().values["COMPOSE_PROFILES"] == "custom,telegram"
        assert any("TELEGRAM_BOT_TOKEN" in message for section, message in backend.problems()
                   if section == "telegram")


@pytest.mark.parametrize("group, values, section, fragment", [
    ("models", {"AGENT_LLM_BASE_URL": "https://["}, "models", "valid HTTPS"),
    ("models", {"AGENT_LLM_BASE_URL": "https://"}, "models", "valid HTTPS"),
    ("models", {"AGENT_LLM_BASE_URL": "https://api.example.com:invalid/v1"}, "models", "valid HTTPS"),
    ("models", {"AGENT_LLM_BASE_URL": "https://api.example.com?key=private"}, "models", "valid HTTPS"),
    ("security", {"AGENT_SECURITY_DECISION_ENABLED": "true", "AGENT_SECURITY_DECISION_URL": "https://["},
     "security", "HTTPS URL"),
    ("transcription", {"TRANSCRIPTION_ENABLED": "true", "TRANSCRIPTION_LANGUAGE": "fr"}, "transcription", "auto, en or ar"),
    ("email", {"SETUP_EMAIL_ENABLED": "true", "IMAP_HOST": "https://mail.example.com"}, "email", "hostname"),
    ("identity", {"ADMIN_EMAIL": "not-an-email"}, "identity", "valid email"),
    ("calendar", {"CALENDAR_ENABLED": "true", "CALENDAR_DEFAULT_PROVIDER": "typo"}, "calendar", "Default calendar provider"),
    ("zoom", {"ZOOM_ENABLED": "true", "ZOOM_AUTH_MODE": "typo"}, "zoom", "authorization mode"),
])
def test_each_native_form_reports_invalid_input_without_crashing_review(group, values, section, fragment):
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        fields = {spec.key: backend.value(spec) for spec in GROUPS[group][1]}
        fields.update(values)
        backend.stage(group, fields)
        assert any(key == section and fragment in message for key, message in backend.problems())
        with pytest.raises(SetupError, match=fragment):
            backend.save()


@pytest.mark.parametrize("provider, expected", [
    ("calendar", "CALENDAR_GOOGLE_CLIENT_ID"), ("zoom", "ZOOM_CLIENT_ID"),
    ("dropbox", "DROPBOX_CLIENT_ID"), ("telegram", "TELEGRAM_BOT_TOKEN"),
])
def test_yaml_enabled_integrations_cannot_bypass_credential_validation(provider, expected):
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        backend.installation.defaults = AppConfig({"agent": {provider: {"enabled": True}}})
        assert any(expected in message for _, message in backend.problems())


def test_extra_calendar_accounts_require_complete_credential_sets():
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        backend.values.update(CALENDAR_ENABLED="true", CALENDAR_DEFAULT_PROVIDER="google_2")
        backend.credentials.update({
            "CALENDAR_GOOGLE_2_CLIENT_ID": "client", "CALENDAR_GOOGLE_2_CLIENT_SECRET": "client-secret",
            "CALENDAR_GOOGLE_2_REFRESH_TOKEN": "refresh-token", "CALENDAR_OUTLOOK_CLIENT_ID": "partial",
        })
        messages = [message for section, message in backend.problems() if section == "calendar"]
        assert len(messages) == 2
        assert all("CALENDAR_OUTLOOK_" in message for message in messages)


def test_failed_credential_save_preserves_the_saved_snapshot_and_can_be_retried():
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        saved = dict(backend.installation.secret_values)
        with patch.object(backend, "access"), \
                patch("assistant_agent.setup.write_transaction", side_effect=SetupError("Write failed")):
            with pytest.raises(SetupError, match="Write failed"):
                backend.replace_credential("OPENROUTER_API_KEY", "replacement")
        assert backend.installation.secret_values == saved
        assert backend.credentials == saved
        with patch.object(backend, "access"):
            backend.replace_credential("OPENROUTER_API_KEY", "replacement")
        assert Installation(root).secret_values["OPENROUTER_API_KEY"] == "replacement"


@pytest.mark.parametrize("response", ["null", "[]", '"text"', "{}", '{"State": "running"}', "not json"])
def test_invalid_component_status_is_an_actionable_error(response):
    with tempfile.TemporaryDirectory() as root:
        backend = management(root)
        with patch.object(backend, "access"), patch.object(
            backend.installation, "compose", side_effect=["task-agent", response]
        ):
            if response == "[]":
                assert backend.services()[0]["state"] == "not created"
            else:
                with pytest.raises(SetupError, match="invalid component status"):
                    backend.services()


@pytest.mark.parametrize("response", ["null", "[]", '"text"', '{"error": "failed"}'])
def test_search_maintenance_rejects_malformed_results(response):
    with tempfile.TemporaryDirectory() as root:
        configured(root)
        backend = management(root)
        backend.helper_ready = True
        with patch.object(backend, "access"), patch.object(backend.installation, "compose", return_value=response):
            with pytest.raises(SetupError, match="invalid response"):
                backend.embeddings("status")


@pytest.mark.parametrize("state", [None, [], {"pending": "KEY"}, {"pending": [1]},
                                   {"setup_complete": "false"}, {"credential_volume": 123}])
def test_invalid_saved_state_is_reported_without_crashing_startup(state):
    with tempfile.TemporaryDirectory() as root:
        path = Path(root, "data/.secrets/state.json")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(state))
        with pytest.raises(SetupError, match="Saved setup state is invalid"):
            management(root)


def test_failed_storage_save_does_not_change_the_active_in_memory_storage():
    from test_setup import SetupTest
    with tempfile.TemporaryDirectory() as root, SetupTest().credential_volume_backend():
        configured(root)
        backend = management(root)
        with patch.object(backend, "access"), \
                patch("assistant_agent.setup.write_transaction", side_effect=SetupError("Write failed")):
            with pytest.raises(SetupError, match="Write failed"):
                backend.storage(True)
        assert not backend.installation.use_credential_volume
        assert backend.installation.credential_volume is None
        assert not Installation(root).use_credential_volume


def test_failed_storage_activation_reloads_saved_state_and_has_a_retry_path():
    from test_setup import SetupTest
    with tempfile.TemporaryDirectory() as root, SetupTest().credential_volume_backend():
        configured(root)
        backend = management(root)
        with patch.object(backend, "access"), \
                patch.object(Installation, "apply", side_effect=SetupError("Activation failed")):
            with pytest.raises(SetupError, match="storage was saved.*incomplete"):
                backend.storage(True)
        assert backend.installation.use_credential_volume
        assert backend.installation.credential_volume
        assert backend.installation.state["pending"]
        backend.installation.assert_current()


def test_partial_permission_revocation_reports_saved_changes_without_leaking_diagnostics(capsys):
    from assistant_agent.management_operator import main
    from assistant_agent.script_permissions import PermissionRevocationIncomplete
    with patch("sys.stdin", io.StringIO("{}")), \
            patch("assistant_agent.management_operator.load_config"), \
            patch("assistant_agent.management_operator.database_url"), \
            patch("assistant_agent.management_operator.execute",
                  side_effect=PermissionRevocationIncomplete("private broker diagnostic")):
        with pytest.raises(SystemExit) as failure:
            main()
    assert failure.value.code == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert "Permissions were saved" in error
    assert "Stop the sandbox service" in error
    assert "private broker diagnostic" not in error


def test_management_collections_details_and_mutations_persist_in_a_real_database(db):
    config = AppConfig({"agent": {"embeddings": {"enabled": False}}})
    def request(kind, **values):
        return execute(db, config, {"kind": kind, **values})
    def mutate(action, identifier=None, body=""):
        return request("mutate", action=action, id=identifier, body=body, confirmed=True)["result"]
    job = db.create_manual_job("Management audit task", "Inspect management screens")
    identifier = job["id"]
    db.log_event(identifier, "tool_result", input_data={"request": "inspect"}, output_data={"answer": "verified"})
    assert request("tasks", query="Management audit")["rows"][0]["id"] == identifier
    assert request("tasks", id=identifier)["total"] == 1
    event = request("task-events", id=identifier)["rows"][0]
    assert event["input_data"] == {"request": "inspect"}
    assert event["output_data"] == {"answer": "verified"}
    assert mutate("cancel", identifier)["status"] == "cancelled"
    assert mutate("retry", identifier)["status"] == "queued"
    mutate("instruct", identifier, "Use the updated specification")
    assert db.fetch_one("SELECT instruction FROM supervisor_instructions WHERE job_id = %s",
                        (identifier,))["instruction"] == "Use the updated specification"
    memory = mutate("create-memory", body="A verified preference")
    memory_id = memory["id"]
    assert request("memories", id=memory_id)["rows"][0]["content"] == "A verified preference"
    mutate("update-memory", memory_id, "An updated preference")
    mutate("pin-memory", memory_id)
    assert request("memories", id=memory_id)["rows"][0]["pinned"]
    mutate("unpin-memory", memory_id)
    assert not request("memories", id=memory_id)["rows"][0]["pinned"]
    for action, expected in (("approve-memory", "active"), ("reject-memory", "rejected")):
        db.execute("UPDATE agent_memories SET metadata = %s WHERE id = %s",
                   ('{"state": "suggested"}', memory_id))
        mutate(action, memory_id)
        assert request("memories", id=memory_id)["rows"][0]["metadata"]["state"] == expected
    mutate("delete-memory", memory_id)
    assert request("memories", id=memory_id)["rows"] == []
    reminder = db.fetch_one(
        "INSERT INTO reminders(title, task, run_at) VALUES (%s, %s, now() + interval '1 day') RETURNING id",
        ("Management reminder", "Inspect again"))
    assert request("reminders", id=reminder["id"])["rows"][0]["status"] == "scheduled"
    assert mutate("cancel-reminder", reminder["id"])["status"] == "cancelled"
    projects = request("projects")["rows"]
    assert projects
    assert any(request("project-objects", id=project["id"])["rows"] for project in projects)
    script = db.fetch_one("INSERT INTO sandbox_scripts(entry) VALUES (%s) RETURNING id", ("audit.py",))
    version = db.fetch_one(
        "INSERT INTO sandbox_script_versions(script_id, fingerprint, manifest, purpose, reusable) "
        "VALUES (%s, %s, %s, %s, true) RETURNING id", (script["id"], "audit-fingerprint", "{}", "Audit"))
    assert request("scripts", query="audit.py")["rows"][0]["version_id"] == version["id"]
    assert request("script", id=version["id"])["version"]["purpose"] == "Audit"
    with patch("assistant_agent.tools.ToolRuntime.sandbox_control", return_value={"stopped": 0}):
        mutate("block-script", script["id"])
        assert request("script", id=version["id"])["blocked"]
        mutate("unblock-script", script["id"])
        assert not request("script", id=version["id"])["blocked"]
        db.execute("INSERT INTO sandbox_permission_grants(version_id, kind, scope, decision, created_by) "
                   "VALUES (%s, 'run', 'script', 'allow', 'test')", (version["id"],))
        mutate("revoke-script", script["id"])
        assert request("script", id=version["id"])["grants"] == []
    from assistant_agent.script_permissions import PermissionRevocationIncomplete
    from assistant_agent.tools import ToolError
    with patch("assistant_agent.tools.ToolRuntime.sandbox_control", side_effect=ToolError("Broker unavailable")):
        with pytest.raises(PermissionRevocationIncomplete, match="Permissions were saved"):
            mutate("block-script", script["id"])
    assert request("script", id=version["id"])["blocked"]
