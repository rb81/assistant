from unittest.mock import patch

import json
import pytest
from fastapi.testclient import TestClient

from assistant_agent.management import Management
from assistant_agent.management_api import ManagementService, create_app
from assistant_agent.setup import PublicSetupError, SetupError
from test_management import configured


def test_settings_reads_share_backend_but_detect_external_changes(tmp_path):
    source = tmp_path / "src/config"
    source.mkdir(parents=True)
    (source / "agent.yaml").write_text("agent:\n  identity:\n    name: Nova\n")
    (tmp_path / "data").mkdir()
    service = ManagementService(tmp_path)
    original = Management.load
    try:
        with patch.object(Management, "load", autospec=True, side_effect=original) as load:
            catalog = service.configuration_v2(include_source=False, instructions=False, values=False)
            page = service.configuration_v2(include_source=False, section="identity", instructions=False)
            service.credentials()
            assert load.call_count == 1
            assert catalog["revision"] == page["revision"]
            assert all(field["value"] == "" for field in catalog["settings"])
            (tmp_path / "data/overrides.env").write_text("AGENT_IDENTITY_NAME=Changed\n")
            changed = service.configuration_v2(include_source=False, section="identity")
            assert load.call_count == 2
            assert changed["revision"] != page["revision"]
            assert next(field["value"] for field in changed["settings"]
                        if field["key"] == "AGENT_IDENTITY_NAME") == "Changed"
            service.invalidate_configuration_cache()
            service.configuration_v2(include_source=False, section="identity")
            assert load.call_count == 3
    finally:
        service.close()


def test_catalog_does_not_compute_effective_values(tmp_path):
    service = ManagementService(tmp_path)
    try:
        with patch.object(Management, "value", side_effect=AssertionError("Catalog evaluated a setting")):
            catalog = service.configuration_v2(include_source=False, instructions=False, values=False)
        assert catalog["settings"]
        identity = next(group for group in catalog["groups"] if group["id"] == "identity")
        assert identity["title"] == "Assistant & people"
        fields = {field["key"]: field for field in catalog["settings"]}
        assert fields["AGENT_IDENTITY_NAME"]["metadata"]["level"] == "basic"
        assert fields["AGENT_IDENTITY_NAME"]["metadata"]["section"] == "Your assistant"
        assert fields["AGENT_ADMIN_NAME"]["metadata"]["section"] == "About you"
        assert fields["AGENT_LLM_ZDR_ONLY"]["boolean"] is True
    finally:
        service.close()


def test_lost_receipt_can_be_looked_up_without_replaying(tmp_path):
    service = ManagementService(tmp_path)
    token = "a" * 64
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + token}
    with patch.object(service, "execute_operation", return_value={}) as execute:
        app = create_app(tmp_path, token, service)
        manager = app.state.management_operations
        operation = manager.submit("fixture.action", {}, "lost-receipt-key")
        manager.executor.shutdown(wait=True)
        try:
            client = TestClient(app)
            response = client.get("/v2/operations/lookup?key=lost-receipt-key", headers=headers)
            assert response.status_code == 200
            assert response.json()["status"] == "found"
            assert response.json()["operation"]["id"] == operation["id"]
            assert response.json()["operation"]["state"] == "succeeded"
            assert execute.call_count == 1
            response = client.get("/v2/operations/lookup?key=unknown-receipt", headers=headers)
            assert response.json() == {"status": "not_found", "operation": None}
            assert client.get("/v2/operations/lookup?key=lost-receipt-key").status_code == 401
        finally:
            service.close()


def test_setup_fields_stay_with_their_feature_while_tuning_is_advanced(tmp_path):
    service = ManagementService(tmp_path)
    try:
        catalog = service.configuration_v2(include_source=False, instructions=False, values=False)
        fields = {field["key"]: field for field in catalog["settings"]}
        assert len(fields) == len(catalog["settings"]), "Regrouping must not duplicate settings"
        for key in ("SETUP_EMAIL_ENABLED", "AGENT_EMAIL_IMAP_HOST", "AGENT_EMAIL_IMAP_PORT",
                    "AGENT_EMAIL_IMAP_PASSWORD", "AGENT_EMAIL_SMTP_HOST", "AGENT_EMAIL_SMTP_PASSWORD",
                    "AGENT_CALENDAR_GOOGLE_2_CLIENT_SECRET", "AGENT_CALENDAR_ZOHO_2_CALENDAR_ID",
                    "AGENT_ZOOM_AUTH_MODE", "AGENT_ZOOM_CLIENT_SECRET", "AGENT_DROPBOX_REFRESH_TOKEN",
                    "AGENT_TTS_ENABLED", "AGENT_TTS_MODEL", "AGENT_TTS_VOICE"):
            assert fields[key]["metadata"]["level"] == "basic", key
        for key in ("AGENT_EMAIL_IMAP_POLL_INTERVAL_SECONDS", "AGENT_EMAIL_IMAP_TIMEOUT_SECONDS",
                    "AGENT_MODEL_SERVICE_BUDGET_PERIOD_DAYS"):
            assert fields[key]["metadata"]["level"] == "advanced", key
        speech = next(group for group in catalog["groups"] if group["id"] == "advanced:Model preset tool")
        assert speech["destination"] == "models"
        assert speech["basic"]
        assert speech["title"] == "Model tools & speech"
        assert speech["advanced_title"] == "Model tools & speech tuning"
        assert fields["AGENT_TTS_MODEL"]["group"] == fields["AGENT_MODEL_SERVICE_BUDGET_USD"]["group"] == speech["id"]
        assert not any(group["id"] == "advanced:Text-to-speech" for group in catalog["groups"])
        assert fields["AGENT_TTS_ENABLED"]["label"] == "Enable text to speech"
        groups = {group["id"]: group for group in catalog["groups"]}
        for field in catalog["settings"]:
            if field["metadata"]["level"] == "basic":
                owner = groups[field["group"]]
                while owner["parent"]:
                    owner = groups[owner["parent"]]
                assert owner["basic"], f"{field['key']} would be hidden from every Basic page"
        for key in ("AGENT_MEMORY_STEWARD_ENABLED", "AGENT_WORKSPACE_INDEX_ENABLED"):
            assert fields[key]["metadata"]["level"] == "basic"
            assert fields[key]["group"] == "advanced:Memory and workspace indexing"
        assert groups["catalog:workspace"]["parent"] == "advanced:Memory and workspace indexing"
        assert groups["catalog:identity"]["parent"] == "email"
        assert groups["email"]["advanced_title"] == "Email behavior & delivery"
        assert fields["AGENT_DATABASE_PASSWORD"]["group"] == "catalog:database"
        assert fields["AGENT_DATABASE_PASSWORD"]["metadata"]["level"] == "advanced"
        assert not groups["catalog:skills"]["basic"]
        approval = fields["AGENT_APPROVALS_CHANNEL"]["metadata"]
        assert "both" in approval["choices"]
        assert approval["choice_labels"]["both"] == "Email & Telegram"
    finally:
        service.close()


def test_default_calendar_choices_include_only_complete_saved_accounts(tmp_path):
    configured(tmp_path)
    backend = Management(tmp_path, lambda *_: False)
    backend.load()
    for provider in ("GOOGLE", "GOOGLE_2", "ZOHO_9"):
        for suffix in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN"):
            backend.credentials[f"AGENT_CALENDAR_{provider}_{suffix}"] = "fixture-" + suffix
    backend.credentials["AGENT_CALENDAR_OUTLOOK_CLIENT_ID"] = "incomplete"
    backend.values["AGENT_CALENDAR_GOOGLE_3_CALENDAR_ID"] = "primary"
    backend.save()
    service = ManagementService(tmp_path)
    try:
        page = service.configuration_v2(include_source=False, instructions=False, section="calendar")
        field = next(field for field in page["settings"] if field["key"] == "AGENT_CALENDAR_DEFAULT_PROVIDER")
        assert field["metadata"]["choices"] == ["google", "google_2", "zoho_9"]
        assert field["metadata"]["choice_labels"] == {
            "google": "Google", "google_2": "Google calendar 2", "zoho_9": "Zoho calendar 9",
        }
        assert "fixture-" not in json.dumps(page)
    finally:
        service.close()


def test_saved_change_review_survives_multiple_saves_and_never_contains_secrets(tmp_path):
    configured(tmp_path)
    service = ManagementService(tmp_path)
    try:
        assert not service.configuration_activation()["tracked"]
        for values in ({"AGENT_IDENTITY_NAME": "Updated"},
                       {"AGENT_LLM_OPENROUTER_API_KEY": "replacement-private-key"}):
            payload = {"revision": service.configuration_activation()["revision"], "values": values}
            preview = service.preview_configuration(payload)
            assert preview["valid"], preview["problems"]
            service.save_configuration({**payload, "preview_id": preview["preview_id"]})
        service.close()
        service = ManagementService(tmp_path)
        summary = service.configuration_activation()
        assert summary["tracked"] and summary["pending"]
        changes = {change["key"]: change for change in summary["changes"]}
        assert changes["AGENT_IDENTITY_NAME"]["value"] == "Updated"
        assert changes["AGENT_LLM_OPENROUTER_API_KEY"]["value"] == "Credential changed"
        state = (tmp_path / "data/.secrets/state.json").read_text()
        assert "replacement-private-key" not in state + json.dumps(summary)
        client = TestClient(create_app(tmp_path, "a" * 64, service))
        assert client.get("/v2/configuration/activation").status_code == 401
        response = client.get("/v2/configuration/activation",
                              headers={"Host": "127.0.0.1", "Authorization": "Bearer " + "a" * 64})
        assert response.status_code == 200 and response.json()["pending"]
    finally:
        service.close()


@pytest.mark.parametrize("service_id,fail,cleared", [
    (None, False, True), ("task-agent", False, False), (None, True, False),
])
def test_pending_settings_clear_only_after_successful_whole_application_apply(tmp_path, service_id, fail, cleared):
    configured(tmp_path)
    backend = Management(tmp_path, lambda *_: False)
    backend.load()
    backend.values["AGENT_IDENTITY_NAME"] = "Updated"
    backend.credentials["AGENT_LLM_OPENROUTER_API_KEY"] = "replacement-key"
    backend.save()
    with patch.object(backend, "access"), \
            patch.object(backend, "_service_catalog", return_value={"task-agent": {"restart": "unless-stopped"}}), \
            patch.object(backend, "services", return_value=[]), \
            patch.object(backend.installation, "reconcile_grants"), \
            patch.object(backend.installation, "compose", side_effect=RuntimeError("apply failed") if fail else None):
        if fail:
            with pytest.raises(RuntimeError, match="apply failed"):
                backend.lifecycle("apply", service_id, preserve_management=True)
        else:
            backend.lifecycle("apply", service_id, preserve_management=True)
    state = json.loads((tmp_path / "data/.secrets/state.json").read_text())
    assert (not state["pending_configuration"]) is cleared
    assert ("AGENT_LLM_OPENROUTER_API_KEY" not in state["pending"]) is cleared


def test_apply_rejects_configuration_changed_since_review(tmp_path):
    configured(tmp_path)
    service = ManagementService(tmp_path)
    try:
        old = service.configuration_activation()["revision"]
        backend = service.backend()
        backend.values["AGENT_IDENTITY_NAME"] = "Changed elsewhere"
        backend.save()
        with patch.object(Management, "lifecycle") as apply:
            with pytest.raises(PublicSetupError, match="Review the saved changes again"):
                service._execute_operation_local("service.lifecycle", {"action": "apply", "revision": old}, lambda _: None)
            apply.assert_not_called()
    finally:
        service.close()


def test_activation_does_not_clear_newer_saved_changes(tmp_path):
    configured(tmp_path)
    old = Management(tmp_path, lambda *_: False)
    old.load()
    old.values["AGENT_IDENTITY_NAME"] = "First"
    old.save()
    newer = Management(tmp_path, lambda *_: False)
    newer.load()
    newer.values["AGENT_APP_TIMEZONE"] = "Europe/London"
    newer.save()
    with pytest.raises(SetupError, match="Configuration changed"):
        old._record_configuration_applied()
    state = json.loads((tmp_path / "data/.secrets/state.json").read_text())
    assert set(state["pending_configuration"]) >= {"AGENT_IDENTITY_NAME", "AGENT_APP_TIMEZONE"}


@pytest.mark.parametrize("key", ["agent.yaml", "setup"])
def test_initial_setup_and_pending_source_defaults_require_build_before_apply(tmp_path, key):
    installation = configured(tmp_path)
    installation.state["pending_configuration"] = [key] if key == "agent.yaml" else []
    installation.state["setup_complete"] = key != "setup"
    installation.save()
    backend = Management(tmp_path, lambda *_: False)
    backend.load()
    if key == "setup":
        backend.accepted = True
        backend.save()
        assert "setup" in backend.installation.state["pending_configuration"]
    with patch.object(backend, "access"), patch.object(backend, "services", return_value=[]), \
            patch.object(backend.installation, "start_containers") as start:
        backend.lifecycle("apply", preserve_management=True)
        start.assert_called_once_with(preserve_management=True)
    assert not json.loads((tmp_path / "data/.secrets/state.json").read_text())["pending_configuration"]
