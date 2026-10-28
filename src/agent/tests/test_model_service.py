import base64
import io
import json
import os
import threading
import urllib.error
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from psycopg.conninfo import make_conninfo

from assistant_agent.config import AppConfig
from assistant_agent.database import Database
from assistant_agent.model_service import ModelService, ModelServiceError, OpenRouterModels, ProviderRejected
from assistant_agent.tools import ToolError, ToolRuntime, available_function_names, available_tools, tool_catalog


MODELS = [
    {"id": "test/text", "name": "Text", "architecture": {
        "input_modalities": ["text"], "output_modalities": ["text"]},
     "supported_parameters": ["max_tokens", "temperature", "response_format"], "context_length": 10000},
    {"id": "test/image", "name": "Image", "architecture": {
        "input_modalities": ["text", "image"], "output_modalities": ["text", "image"]},
     "supported_parameters": ["max_tokens"]},
    {"id": "test/audio", "name": "Audio", "architecture": {
        "input_modalities": ["text", "audio"], "output_modalities": ["text", "audio"]},
     "supported_parameters": ["max_tokens"], "supported_voices": None},
    {"id": "test/video-input", "architecture": {
        "input_modalities": ["text", "video"], "output_modalities": ["text"]},
     "supported_parameters": []},
]
PROMPT = {"messages": [{"role": "user", "content": "Please respond"}]}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==")


def reply(content="Hello", cost=0.1, **message):
    return {"id": "gen-test", "model": "test/actual",
            "usage": {"total_tokens": 15, "cost": cost},
            "choices": [{"finish_reason": "stop", "message": {"content": content, **message}}]}


@pytest.fixture
def service(tmp_path, monkeypatch):
    url = os.environ.get("ASSISTANT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires disposable assistant_test database")
    original = Database(url)
    schema = "test_" + uuid.uuid4().hex
    with original.connect() as conn:
        if conn.info.dbname != "assistant_test":
            raise RuntimeError("model service tests require a disposable assistant_test database")
        conn.execute("CREATE SCHEMA " + schema)
    db = Database(make_conninfo(url, options="-c search_path=" + schema))
    try:
        db.execute(Path(__file__).resolve().parents[2].joinpath("database/schema.sql").read_text())
        db.ensure_feature_schema()
        db.run_migrations()
        job = db.create_manual_job("Model service test", "Run a preset")
        config = AppConfig({"agent": {
            "model_service": {"enabled": True, "budget_usd": 5, "budget_period_days": 30},
            "filesystem": {"shared_root": str(tmp_path), "require_mount": False},
            "workspace": {"index": {"enabled": False}}, "embeddings": {"enabled": False},
        }})
        monkeypatch.setattr("assistant_agent.model_service.read_secret", lambda name: "test-key")
        monkeypatch.setattr(OpenRouterModels, "models", lambda self: MODELS)
        yield ModelService(ToolRuntime(db, config, job))
    finally:
        original.execute("DROP SCHEMA " + schema + " CASCADE")


def test_preset_crud_is_persistent_and_does_not_overwrite(service):
    service.run("add_preset", name="extractor", model="test/text", system_prompt="Be precise")
    reopened = ModelService(ToolRuntime(service.db, service.config, service.runtime.job))
    assert reopened.run("list_presets")["presets"] == [
        {"name": "extractor", "model": "test/text", "system_prompt": "Be precise"}]
    with pytest.raises(ModelServiceError, match="already exists"):
        service.run("add_preset", name="extractor", model="test/image")
    assert service.run("delete_preset", name="extractor") == {"deleted": "extractor"}
    with pytest.raises(ModelServiceError, match="does not exist"):
        service.run("delete_preset", name="extractor")
    service.run("add_preset", name="extractor", model="test/image")
    with pytest.raises(ModelServiceError, match="current OpenRouter catalog"):
        service.run("add_preset", name="unknown", model="missing/model")


def test_model_filters_and_pagination(service):
    result = service.run("query_models", input_modalities=["image"], output_modalities=["image"])
    assert [row["id"] for row in result["models"]] == ["test/image"]
    result = service.run("query_models", supported_parameters=["temperature"])
    assert [row["id"] for row in result["models"]] == ["test/text"]
    assert service.run("query_models", output_modalities=["video"])["total"] == 0
    assert service.run("query_models", input_modalities=["video"])["total"] == 1
    result = service.run("query_models", limit=1, offset=1)
    assert result["total"] == 4 and result["next_offset"] == 2
    assert result["models"][0]["id"] == "test/image"


def test_completion_is_isolated_and_accounts_cost_and_tokens(service):
    service.add_preset("extractor", "test/text", "Be precise")
    with patch.object(service.client, "complete", return_value=reply('{"ok":true}')) as complete:
        result = service.use_preset("extractor", PROMPT, temperature=0, max_tokens=50,
                                    response_format={"type": "json_object"})
    assert complete.call_args.args[0] == {
        "model": "test/text", "messages": [
            {"role": "system", "content": "Be precise"}, *PROMPT["messages"]],
        "max_tokens": 50, "temperature": 0, "response_format": {"type": "json_object"},
    }
    assert result["content"] == '{"ok":true}' and result["model"] == "test/actual"
    assert result["budget"]["spent_usd"] == 0.1
    assert service.runtime.model_service_tokens == 15
    row = service.db.fetch_one("SELECT * FROM model_service_calls")
    assert row["status"] == "completed" and row["cost_usd"] == Decimal("0.1")
    assert row["preset"]["system_prompt"] == "Be precise"
    service.delete_preset("extractor")
    assert service.db.fetch_one("SELECT count(*) AS count FROM model_service_calls")["count"] == 1
    log = service.db.fetch_one("SELECT tokens_used FROM task_logs WHERE event_type = 'model_service_response'")
    assert log["tokens_used"]["total_tokens"] == 15


@pytest.mark.parametrize("kwargs", [
    {"payload": {"messages": [{"role": "system", "content": "override"}]}},
    {"payload": {**PROMPT, "tools": []}},
    {"payload": {"messages": [{"role": "user", "content": [{"type": "image_url"}]}]}},
    {"payload": {"messages": [{"role": "assistant", "content": "no user"}]}},
    {"temperature": 0.2},
    {"max_tokens": 0},
    {"output_modalities": ["audio"]},
    {"voice": "alloy", "output_modalities": ["text"]},
    {"files": ["a"] * 6},
])
def test_invalid_requests_never_reserve_or_call_provider(service, kwargs):
    service.add_preset("image", "test/image")
    with patch.object(service.client, "complete") as complete, pytest.raises((ModelServiceError, ToolError, ValueError)):
        service.use_preset("image", **{"payload": PROMPT, **kwargs})
    complete.assert_not_called()
    assert service.db.fetch_one("SELECT count(*) AS count FROM model_service_calls")["count"] == 0


def test_image_reference_and_saved_revisions_have_provenance(service):
    service.add_preset("image", "test/image", "Create illustrations")
    source = service.runtime.shared_root / "reference.png"
    source.write_bytes(PNG)
    uri = "data:image/png;base64," + base64.b64encode(PNG).decode()
    response = reply("", images=[{"type": "image_url", "image_url": {"url": uri}}])
    with patch.object(service.client, "complete", return_value=response) as complete:
        first = service.use_preset("image", PROMPT, files=["reference.png"])
        second = service.use_preset("image", PROMPT, files=[first["files"][0]["path"]])
    request = complete.call_args.args[0]
    assert request["modalities"] == ["text", "image"]
    assert request["messages"][-1]["content"][1]["image_url"]["url"] == uri
    assert first["files"][0]["path"] != second["files"][0]["path"]
    for result in (first, second):
        path = service.runtime.shared_root / result["files"][0]["path"]
        assert path.read_bytes() == PNG
        assert service.db.fetch_one("SELECT source_job_id FROM workstream_files WHERE path = %s",
                                    (str(path),))["source_job_id"] == service.runtime.job["id"]
    logs = service.db.fetch_all("SELECT output_data FROM task_logs WHERE event_type = 'model_service_response'")
    assert all("base64" not in json.dumps(log) for log in logs)


@pytest.mark.parametrize("path", ["../outside.png", ".assistant/private.png", ".env", "folder/link.png"])
def test_attachments_reject_escapes_internal_credentials_and_symlinks(service, path):
    service.add_preset("image", "test/image")
    root = service.runtime.shared_root
    (root / "reference.png").write_bytes(PNG)
    (root / ".assistant").mkdir()
    (root / ".assistant/private.png").write_bytes(PNG)
    (root / ".env").write_text("do not send")
    (root / "folder").mkdir()
    (root / "folder/link.png").symlink_to(root / "reference.png")
    with patch.object(service.client, "complete") as complete, pytest.raises((ModelServiceError, ToolError, ValueError)):
        service.use_preset("image", PROMPT, files=[path])
    complete.assert_not_called()


def test_audio_attachments_and_output(service):
    service.add_preset("audio", "test/audio")
    path = service.runtime.shared_root / "speech.wav"
    path.write_bytes(b"RIFF\x04\x00\x00\x00WAVE")
    with patch.object(service.client, "complete", return_value=reply(
        "", audio={"data": base64.b64encode(path.read_bytes()).decode(), "transcript": "Hello"})) as complete:
        result = service.use_preset("audio", PROMPT, files=["speech.wav"], voice="alloy")
    request = complete.call_args.args[0]
    assert request["stream"] is True and request["stream_options"] == {"include_usage": True}
    assert request["audio"] == {"voice": "alloy", "format": "wav"}
    assert request["messages"][-1]["content"][1]["input_audio"]["format"] == "wav"
    assert result["content"] == "Hello" and result["files"][0]["mime_type"] == "audio/wav"


def test_inspection_explicitly_requests_only_text_from_media_model(service):
    service.add_preset("vision", "test/image")
    (service.runtime.shared_root / "input.png").write_bytes(PNG)
    with patch.object(service.client, "complete", return_value=reply("The image is clear")) as complete:
        result = service.use_preset("vision", PROMPT, files=["input.png"], output_modalities=["text"])
    assert complete.call_args.args[0]["modalities"] == ["text"]
    assert "stream" not in complete.call_args.args[0]
    assert result["content"] == "The image is clear" and result["files"] == []


def test_budget_threshold_is_shared_and_survives_job_deletion(service):
    service.add_preset("text", "test/text")
    service.config.values["agent"]["model_service"]["budget_usd"] = 0.05
    with patch.object(service.client, "complete", return_value=reply()) as complete:
        result = service.use_preset("text", PROMPT)
        assert result["budget"]["remaining_usd"] == 0
        with pytest.raises(ModelServiceError, match="budget exhausted"):
            service.use_preset("text", PROMPT)
    assert complete.call_count == 1
    service.db.execute("DELETE FROM jobs WHERE id = %s", (service.runtime.job["id"],))
    assert service.budget()["spent_usd"] == 0.1
    service.db.execute("UPDATE model_service_calls SET created_at = now() - interval '31 days'")
    assert service.budget()["remaining_usd"] == 0.05


def test_concurrent_workers_cannot_both_pass_budget_check(service):
    barrier = threading.Barrier(2)
    def reserve():
        barrier.wait()
        try:
            return service.reserve({"name": "test", "model": "test/text"})
        except ModelServiceError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert sum(value is not None for value in results) == 1
    assert service.budget()["unresolved_requests"] == 1


@pytest.mark.parametrize("failure,expected", [
    (ProviderRejected("HTTP 400"), "failed"),
    (ModelServiceError("interrupted"), "unknown"),
])
def test_failed_calls_do_not_retry_and_uncertain_cost_blocks_spending(service, failure, expected):
    service.add_preset("text", "test/text")
    with patch.object(service.client, "complete", side_effect=failure) as complete:
        with pytest.raises(ModelServiceError):
            service.use_preset("text", PROMPT)
    assert complete.call_count == 1
    row = service.db.fetch_one("SELECT status FROM model_service_calls")
    assert row["status"] == expected
    assert service.budget()["unresolved_requests"] == (1 if expected == "unknown" else 0)


def test_missing_cost_is_not_treated_as_free(service):
    service.add_preset("text", "test/text")
    response = reply()
    del response["usage"]["cost"]
    with patch.object(service.client, "complete", return_value=response):
        with pytest.raises(ModelServiceError, match="omitted usage.cost"):
            service.use_preset("text", PROMPT)
    assert service.runtime.model_service_tokens == 15
    with pytest.raises(ModelServiceError, match="unknown-cost"):
        service.use_preset("text", PROMPT)


def test_invalid_media_still_records_charge(service):
    service.add_preset("image", "test/image")
    response = reply("", images=[{"image_url": {"url": "https://example.com/image.png"}}])
    with patch.object(service.client, "complete", return_value=response):
        with pytest.raises(ModelServiceError, match="Only inline"):
            service.use_preset("image", PROMPT)
    assert service.budget()["spent_usd"] == 0.1


def test_attachment_size_and_media_type_checks_happen_before_spending(service, monkeypatch):
    service.add_preset("image", "test/image")
    path = service.runtime.shared_root / "fake.png"
    path.write_text("<html>not an image</html>")
    with patch.object(service.client, "complete") as complete:
        with pytest.raises(ModelServiceError, match="do not match MIME"):
            service.use_preset("image", PROMPT, files=["fake.png"])
        path.write_bytes(PNG)
        monkeypatch.setattr("assistant_agent.model_service.MAX_FILE_BYTES", 10)
        with pytest.raises(ModelServiceError, match="Attachments exceed"):
            service.use_preset("image", PROMPT, files=["fake.png"])
    complete.assert_not_called()
    assert service.budget()["spent_usd"] == 0


def test_multimodal_attachment_mapping_and_unsupported_modalities(service):
    service.add_preset("video", "test/video-input")
    path = service.runtime.shared_root / "clip.mp4"
    path.write_bytes(b"example video bytes")
    with patch.object(service.client, "complete", return_value=reply()) as complete:
        service.use_preset("video", PROMPT, files=["clip.mp4"])
    request = complete.call_args.args[0]
    assert "max_tokens" not in request
    assert request["messages"][-1]["content"][1]["video_url"]["url"].startswith("data:video/mp4;base64,")
    service.add_preset("text", "test/text")
    with patch.object(service.client, "complete") as complete, pytest.raises(ModelServiceError, match="unsupported"):
        service.use_preset("text", PROMPT, files=["clip.mp4"])
    complete.assert_not_called()


@pytest.mark.parametrize("cost", [-1, "NaN", "Infinity"])
def test_invalid_cost_keeps_request_unresolved(service, cost):
    service.add_preset("text", "test/text")
    with patch.object(service.client, "complete", return_value=reply(cost=cost)):
        with pytest.raises(ModelServiceError, match="invalid cost"):
            service.use_preset("text", PROMPT)
    assert service.budget()["unresolved_requests"] == 1


def test_tool_discovery_gating_and_runtime_dispatch(service, monkeypatch):
    config = service.config
    assert "llm_service" in available_function_names(config)
    assert "llm_service" in tool_catalog(config)
    assert any(tool["function"]["name"] == "llm_service" for tool in available_tools(config, enabled_names={"llm_service"}))
    assert service.runtime.run("llm_service", {"action": "query_models"})["total"] == 4
    config.values["agent"]["model_service"]["enabled"] = False
    assert "llm_service" not in available_function_names(config)
    with pytest.raises(ToolError, match="not available"):
        service.runtime.run("llm_service", {"action": "query_models"})
    config.values["agent"]["model_service"]["enabled"] = True
    monkeypatch.setattr("assistant_agent.model_service.read_secret", lambda name: None)
    assert "llm_service" not in available_function_names(config)


def test_stream_assembles_audio_text_usage_and_actual_model():
    chunks = [
        {"id": "gen-a", "model": "test/audio", "choices": [{"delta": {"audio": {"data": "YW", "transcript": "Hi "}}}]},
        {"choices": [{"delta": {"audio": {"data": "Jj", "transcript": "there"}}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"cost": 0.1, "total_tokens": 20}},
    ]
    stream = b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks) + b"data: [DONE]\n"
    response = OpenRouterModels.read_stream(io.BytesIO(stream))
    assert response["usage"] == {"cost": 0.1, "total_tokens": 20}
    assert response["model"] == "test/audio"
    assert response["choices"][0]["message"]["audio"] == {"data": "YWJj", "transcript": "Hi there"}
    assert response["choices"][0]["finish_reason"] == "stop"
    with pytest.raises(ModelServiceError, match="before completion"):
        OpenRouterModels.read_stream(io.BytesIO(stream.removesuffix(b"data: [DONE]\n")))


def test_http_errors_do_not_echo_prompt_or_retry():
    client = OpenRouterModels(AppConfig({}))
    with patch("assistant_agent.model_service.read_secret", return_value="test-key"), patch(
        "assistant_agent.model_service.open_https_request",
        side_effect=urllib.error.HTTPError("url", 400, "bad", {}, io.BytesIO(b"private prompt")),
    ) as request:
        with pytest.raises(ProviderRejected) as error:
            client.complete({"model": "test/text", **PROMPT})
    assert "private" not in str(error.value)
    assert request.call_count == 1


def test_model_catalog_transport_uses_all_modalities_and_caches(monkeypatch):
    monkeypatch.setattr("assistant_agent.model_service._catalog", {})
    monkeypatch.setattr("assistant_agent.model_service.read_secret", lambda name: "test-key")
    client = OpenRouterModels(AppConfig({}))
    with patch("assistant_agent.model_service.open_https_request", return_value=io.BytesIO(
        json.dumps({"data": MODELS}).encode(),
    )) as request:
        assert client.models() == MODELS
        assert client.models() == MODELS
    assert request.call_count == 1
    assert request.call_args.args[0].full_url == "https://openrouter.ai/api/v1/models?output_modalities=all"


def test_zdr_catalog_cache_is_separate_and_retains_audio_metadata(monkeypatch):
    monkeypatch.setattr("assistant_agent.model_service._catalog", {})
    monkeypatch.setattr("assistant_agent.model_service.read_secret", lambda name: "test-key")
    all_models = OpenRouterModels(AppConfig({}))
    zdr_models = OpenRouterModels(AppConfig({"agent": {"model_service": {"zdr_only": True}}}))
    with patch("assistant_agent.model_service.open_https_request", side_effect=[
        io.BytesIO(json.dumps({"data": MODELS}).encode()),
        io.BytesIO(json.dumps({"data": [MODELS[2]]}).encode()),
    ]) as request:
        assert all_models.models() == MODELS
        assert zdr_models.models() == [MODELS[2]]
        assert zdr_models.models()[0]["architecture"]["output_modalities"] == ["text", "audio"]
        assert all_models.models() == MODELS
    assert request.call_count == 2
    assert request.call_args_list[1].args[0].full_url == "https://openrouter.ai/api/v1/models?output_modalities=all&zdr=true"


@pytest.mark.parametrize("data", [[], [{"missing": "id"}], None])
def test_zdr_discovery_never_falls_back_to_cached_all_models(monkeypatch, data):
    monkeypatch.setattr("assistant_agent.model_service._catalog", {})
    monkeypatch.setattr("assistant_agent.model_service.read_secret", lambda name: "test-key")
    client = OpenRouterModels(AppConfig({"agent": {"model_service": {"zdr_only": True}}}))
    with patch("assistant_agent.model_service.open_https_request", side_effect=[
        io.BytesIO(json.dumps({"data": MODELS}).encode()),
        io.BytesIO(json.dumps({"data": data}).encode()),
    ]) as request:
        assert OpenRouterModels(AppConfig({})).models() == MODELS
        if data == []:
            assert client.models() == []
            assert client.models() == []
        else:
            with pytest.raises(ModelServiceError, match="invalid model catalog"):
                client.models()
    assert request.call_count == 2


def test_zdr_discovery_failure_does_not_use_expired_or_unrestricted_catalog(monkeypatch):
    monkeypatch.setattr("assistant_agent.model_service._catalog", {
        False: (1000, MODELS), True: (1, [MODELS[2]]),
    })
    monkeypatch.setattr("assistant_agent.model_service.time.monotonic", lambda: 1001)
    monkeypatch.setattr("assistant_agent.model_service.read_secret", lambda name: "test-key")
    client = OpenRouterModels(AppConfig({"agent": {"model_service": {"zdr_only": True}}}))
    error = urllib.error.HTTPError("url", 503, "unavailable", {}, io.BytesIO(b"private"))
    with patch("assistant_agent.model_service.open_https_request", side_effect=error) as request:
        with pytest.raises(ModelServiceError, match=r"discovery failed \(HTTP 503\)"):
            client.models()
    assert request.call_count == 1


@pytest.mark.parametrize("zdr_only", [True, False])
@pytest.mark.parametrize("model,outputs", [("test/text", ["text"]), ("test/audio", ["text", "audio"])])
def test_catalog_policy_controls_generation_routing_without_database(zdr_only, model, outputs):
    runtime = MagicMock()
    runtime.config = AppConfig({"agent": {"model_service": {"zdr_only": zdr_only}}})
    runtime.job = {"id": 7}
    runtime.model_service_tokens = 0
    runtime.db.fetch_one.return_value = {"name": "preset", "model": model, "system_prompt": None}
    service = ModelService(runtime)
    with patch.object(service.client, "models", return_value=MODELS), patch.object(
        service.client, "complete", return_value=reply()
    ) as complete, patch.object(service, "reserve", return_value="request-id"), patch.object(
        service, "settle", return_value="completed"
    ), patch.object(service, "save_reply", return_value={"content": "Hello"}), patch.object(
        service, "budget", return_value={}
    ):
        service.use_preset("preset", PROMPT, output_modalities=outputs)
    request = complete.call_args.args[0]
    if zdr_only:
        assert request["provider"] == {"zdr": True}
    else:
        assert "provider" not in request
    if "audio" in outputs:
        assert request["stream"] is True


def test_existing_non_zdr_presets_are_blocked_before_spending():
    runtime = MagicMock()
    runtime.config = AppConfig({"agent": {"model_service": {"zdr_only": True}}})
    runtime.db.fetch_one.return_value = {"name": "old-preset", "model": "test/text", "system_prompt": None}
    service = ModelService(runtime)
    with patch.object(service.client, "models", return_value=[MODELS[2]]), patch.object(
        service.client, "complete"
    ) as complete, patch.object(service, "reserve") as reserve:
        with pytest.raises(ModelServiceError, match="current OpenRouter catalog"):
            service.add_preset("new-preset", "test/text")
        with pytest.raises(ModelServiceError, match="current OpenRouter catalog"):
            service.use_preset("old-preset", PROMPT)
    reserve.assert_not_called()
    complete.assert_not_called()
    runtime.db.connect.assert_not_called()
