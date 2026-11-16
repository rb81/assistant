from pathlib import Path
from unittest.mock import patch
import stat
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from assistant_agent.management_api import ManagementService, create_app
from assistant_agent.management_yaml import backups, validate_yaml
from assistant_agent.setup import SetupError
from test_management import configured


@pytest.fixture
def source_installation(tmp_path):
    source = Path(__file__).resolve().parents[2] / "config"
    target = tmp_path / "src/config"
    target.mkdir(parents=True)
    for name in ("agent.yaml", "agent.schema.yaml", "AGENT.md.example"):
        (target / name).write_bytes((source / name).read_bytes())
    configured(tmp_path)
    return tmp_path, target / "agent.yaml"


def draft(service, text):
    current = service.configuration_v2()
    payload = {"revision": current["revision"], "values": {}, "agent_yaml": text, "acknowledge": True}
    preview = service.preview_configuration(payload)
    assert preview["valid"], preview
    return {**payload, "preview_id": preview["preview_id"]}


def test_source_yaml_save_backup_rotation_and_restore(source_installation):
    root, source = source_installation
    service = ManagementService(root)
    original = source.read_text()
    original_info = source.stat()
    validate_yaml(original)
    versions = []
    for number in range(4):
        versions.append(source.read_text())
        edited = source.read_text().replace(
            "  # Agent identity", f"  # Revision {number}\n  # Agent identity",
        )
        service.save_configuration(draft(service, edited))
        assert source.read_text() == edited
        assert stat.S_IMODE(source.stat().st_mode) == stat.S_IMODE(original_info.st_mode)
        assert source.stat().st_uid == original_info.st_uid
    names = backups(source.parent)
    assert len(names) == 3
    assert service.yaml_backup(names[0])["content"] == versions[-1]
    restored = service.yaml_backup(names[-1])["content"]
    service.save_configuration(draft(service, restored))
    assert source.read_text() == restored
    assert len(backups(source.parent)) == 3


def test_yaml_noop_does_not_backup_or_rebuild(source_installation):
    root, source = source_installation
    service = ManagementService(root)
    payload = draft(service, source.read_text())
    with patch("assistant_agent.management.Management.lifecycle") as lifecycle:
        service.save_configuration({**payload, "apply": True})
    lifecycle.assert_not_called()
    assert backups(source.parent) == []


def test_source_and_backup_roll_back_together_on_failed_write(source_installation):
    from assistant_agent.setup import atomic_write

    root, source = source_installation
    service = ManagementService(root)
    original = source.read_bytes()
    payload = draft(service, source.read_text() + "\n# interrupted-save\n")
    failed = False

    def fail_source_once(path, content, *args, **kwargs):
        nonlocal failed
        if path == source and not failed:
            failed = True
            raise OSError("synthetic write failure")
        return atomic_write(path, content, *args, **kwargs)

    with patch("assistant_agent.setup.atomic_write", side_effect=fail_source_once):
        with pytest.raises(SetupError, match="restored"):
            service.save_configuration(payload)
    assert source.read_bytes() == original
    assert backups(source.parent) == []


def test_yaml_stale_preview_and_modified_draft_cannot_overwrite(source_installation):
    root, source = source_installation
    service = ManagementService(root)
    payload = draft(service, source.read_text() + "\n# New source\n")
    with pytest.raises(SetupError, match="preview does not match"):
        service.save_configuration({**payload, "agent_yaml": payload["agent_yaml"] + "# changed\n"})
    source.write_text(source.read_text() + "\n# External edit\n")
    with pytest.raises(SetupError, match="changed"):
        service.save_configuration(payload)
    assert backups(source.parent) == []


def test_yaml_apply_rebuilds_after_save_but_keeps_saved_source_on_activation_failure(source_installation):
    root, source = source_installation
    service = ManagementService(root)
    edited = source.read_text() + "\n# Client edit\n"
    with patch("assistant_agent.management.Management.lifecycle", side_effect=SetupError("build failed")) as lifecycle:
        with pytest.raises(SetupError, match="build failed"):
            service.save_configuration({**draft(service, edited), "apply": True})
    lifecycle.assert_called_once_with("start", preserve_management=True)
    assert source.read_text() == edited
    assert len(backups(source.parent)) == 1


def test_yaml_rejects_secrets_unknown_paths_wrong_types_and_unsafe_backups(source_installation):
    root, source = source_installation
    text = source.read_text()
    parsed = yaml.safe_load(text)
    parsed["agent"]["llm"]["openai_api_key"] = "never-in-backups"
    with pytest.raises(SetupError, match="credentials"):
        validate_yaml(yaml.safe_dump(parsed))
    parsed = yaml.safe_load(text)
    parsed["agent"]["llm"]["max_tokens_per_call"] = True
    with pytest.raises(SetupError, match="type"):
        validate_yaml(yaml.safe_dump(parsed))
    parsed = yaml.safe_load(text)
    parsed["agent"]["unknown"] = True
    with pytest.raises(SetupError, match="paths"):
        validate_yaml(yaml.safe_dump(parsed))
    service = ManagementService(root)
    with pytest.raises(SetupError, match="available"):
        service.yaml_backup("../agent.yaml")
    with pytest.raises(SetupError, match="aliases"):
        validate_yaml("agent: &loop\n  recursive: *loop\n")


def test_source_yaml_http_roundtrip_and_private_journal(source_installation, monkeypatch):
    root, source = source_installation
    monkeypatch.delenv("ASSISTANT_MANAGEMENT_EXECUTOR_URL", raising=False)
    token = "a" * 64
    app = create_app(root, token)
    client = TestClient(app)
    headers = {"Host": "127.0.0.1", "Authorization": "Bearer " + token}
    try:
        assert client.get("/v2/configuration", headers={"Host": "127.0.0.1"}).status_code == 401
        current = client.get("/v2/configuration", headers=headers).json()
        text = current["agent_yaml"] + "\n# private-source-review\n"
        payload = {"revision": current["revision"], "values": {}, "agent_yaml": text}
        preview = client.post("/v2/configuration/preview", headers=headers, json=payload).json()
        assert preview["valid"]
        response = client.post("/v2/operations", headers={**headers, "Idempotency-Key": "source-test-operation"},
                               json={**payload, "operation": "configuration.save",
                                     "preview_id": preview["preview_id"], "confirmed": True})
        assert response.status_code == 202, response.text
        for _ in range(500):
            operation = client.get("/v2/operations/" + response.json()["id"], headers=headers).json()
            if operation["state"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        assert operation["state"] == "succeeded", operation
        assert source.read_text() == text
        names = client.get("/v2/configuration", headers=headers).json()["agent_yaml_backups"]
        backup = client.get("/v2/configuration/agent-yaml/backups/" + names[0], headers=headers).json()
        assert backup["content"] == current["agent_yaml"]
        journal = "\n".join(path.read_text() for path in (root / "data").rglob("*.json"))
        assert "private-source-review" not in journal
    finally:
        app.state.management_operations.executor.shutdown(wait=True)
