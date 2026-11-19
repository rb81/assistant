from unittest.mock import patch

from fastapi.testclient import TestClient

from assistant_agent.management import Management
from assistant_agent.management_api import ManagementService, create_app


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
