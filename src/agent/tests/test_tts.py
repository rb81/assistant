import io
import json
import urllib.error
import uuid
import wave
from email.message import Message
from http.client import IncompleteRead
from unittest.mock import MagicMock, patch

import pytest

from assistant_agent.config import AppConfig
from assistant_agent.model_resources import ModelServiceError, ModelToolResources, ProviderRejected
from assistant_agent.tools import ToolError, ToolRuntime, available_function_names, available_tools, tool_catalog
from assistant_agent.tts import MAX_AUDIO_BYTES, OpenRouterSpeech, TextToSpeech, playable_audio


MODEL = {
    "id": "openai/gpt-4o-mini-tts-test", "name": "Speech",
    "architecture": {"input_modalities": ["text"], "output_modalities": ["speech"]},
    "supported_voices": ["alloy", "nova"], "pricing": {"prompt": "0.001"},
}
GOOGLE = {**MODEL, "id": "google/gemini-test-tts", "supported_voices": ["Kore"]}
OTHER = {**MODEL, "id": "other/speech"}
CHAT_AUDIO = {**MODEL, "id": "test/chat-audio", "architecture": {"output_modalities": ["text", "audio"]}}
MP3 = b"ID3" + b"\x00" * 24
GENERATION = {"id": "gen-tts-test", "api_type": "tts", "model": MODEL["id"],
              "total_cost": 0.02, "tokens_prompt": 4, "tokens_completion": 6}
ARGUMENTS = {"model": MODEL["id"], "text": "Hello!", "voice": "alloy"}


class AudioResponse(io.BytesIO):
    def __init__(self, data=MP3, mime="audio/mpeg", generation_id="gen-tts-test"):
        super().__init__(data)
        self.headers = Message()
        self.headers["Content-Type"] = mime
        if generation_id is not None:
            self.headers["X-Generation-Id"] = generation_id


@pytest.fixture
def speech(tmp_path, monkeypatch):
    config = AppConfig({"agent": {
        "tts": {"enabled": True, "zdr_only": True}, "model_service": {"enabled": False},
        "filesystem": {"shared_root": str(tmp_path), "require_mount": False},
        "workspace": {"index": {"enabled": False}}, "embeddings": {"enabled": False},
    }})
    runtime = ToolRuntime(MagicMock(), config, {"id": 7, "thread_id": "thread-7"})
    runtime.workstream_files = MagicMock(wraps=runtime.workstream_files)
    monkeypatch.setattr("assistant_agent.tts.read_secret", lambda name: "test-key")
    tool = TextToSpeech(runtime)
    with patch.object(tool.client, "models", return_value=[MODEL, GOOGLE, OTHER, CHAT_AUDIO]), patch.object(
        tool, "reserve", return_value=str(uuid.uuid4())
    ), patch.object(tool, "settle", return_value="completed"), patch.object(
        tool, "budget", return_value={"remaining_usd": 4.98, "unresolved_requests": 0}
    ), patch.object(tool.client, "generation", return_value=GENERATION):
        yield tool


def synthesize(data=MP3, content_type="audio/mpeg", generation_id="gen-tts-test"):
    def send(payload, record_generation):
        if generation_id:
            record_generation(generation_id)
        return data, content_type, generation_id
    return send


def test_speech_discovery_excludes_chat_audio_and_exposes_voices_policy_and_controls(speech):
    result = speech.run("list_models", limit=1)
    assert result["total"] == 3 and result["next_offset"] == 1
    assert result["zdr_only"] is True
    assert result["models"][0]["supported_voices"] == ["alloy", "nova"]
    assert result["models"][0]["supports_instructions"] is True
    assert speech.list_models(query="other")["models"][0]["supports_instructions"] is False
    assert speech.list_models(query="gemini")["models"][0]["audio_formats"] == ["wav"]
    speech.client.models.assert_called_with(zdr_only=True)


@pytest.mark.parametrize("zdr_only", [True, False])
def test_generate_saves_tracked_audio_accounts_actual_charge_and_never_delivers(speech, zdr_only):
    speech.config.values["agent"]["tts"]["zdr_only"] = zdr_only
    with patch.object(speech.client, "synthesize", side_effect=synthesize()) as send:
        result = speech.run("generate", **ARGUMENTS, instructions="Speak warmly, with a laugh.")
    payload = send.call_args.args[0]
    assert payload["input"] == "Hello!" and payload["instructions"] == "Speak warmly, with a laugh."
    assert payload["response_format"] == "mp3"
    assert payload.get("provider") == ({"zdr": True} if zdr_only else None)
    assert "messages" not in payload and "tools" not in payload
    path = speech.runtime.shared_root / result["files"][0]["path"]
    assert path.read_bytes() == MP3 and path.suffix == ".mp3"
    speech.runtime.workstream_files.register_path.assert_called_once_with(path)
    speech.runtime.workstream_files.track_file.assert_called_once_with(path)
    assert speech.runtime.model_service_tokens == 10
    assert result["usage"]["cost"] == 0.02
    speech.settle.assert_any_call(
        result["request_id"], "completed",
        {"cost": 0.02, "prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
        "gen-tts-test", conn=None,
    )
    speech.db.log_event.assert_called_once()


def test_pcm_only_models_produce_playable_wav_using_provider_rate_metadata(speech):
    with patch.object(speech.client, "synthesize", side_effect=synthesize(
        data=b"\x00\x01" * 100, content_type="audio/pcm;rate=24000;channels=1",
    )) as send:
        result = speech.generate(model=GOOGLE["id"], text="Hello", voice="Kore")
    assert send.call_args.args[0]["response_format"] == "pcm"
    output = speech.runtime.shared_root / result["files"][0]["path"]
    with wave.open(str(output), "rb") as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (24000, 1, 2)
        assert audio.getnframes() == 100


@pytest.mark.parametrize("changes, error", [
    ({"model": CHAT_AUDIO["id"]}, "unavailable"),
    ({"model": "missing/model"}, "unavailable"),
    ({"text": ""}, "Speech text"),
    ({"text": "a" * 10001}, "Speech text"),
    ({"voice": ""}, "voice must"),
    ({"voice": "invented"}, "voice is not supported"),
    ({"audio_format": "ogg"}, "audio_format"),
    ({"instructions": ""}, "instructions must"),
    ({"model": OTHER["id"], "instructions": "Happy"}, "not documented"),
    ({"model": GOOGLE["id"], "voice": "Kore", "audio_format": "mp3"}, "audio_format"),
])
def test_invalid_or_unsupported_options_are_rejected_before_spending(speech, changes, error):
    with patch.object(speech.client, "synthesize") as send:
        with pytest.raises(ModelServiceError, match=error):
            speech.generate(**{**ARGUMENTS, **changes})
    speech.reserve.assert_not_called()
    send.assert_not_called()


def test_explicit_rejection_releases_reservation_and_never_retries(speech):
    with patch.object(speech.client, "synthesize", side_effect=ProviderRejected("rejected")) as send:
        with pytest.raises(ProviderRejected):
            speech.generate(**ARGUMENTS)
    assert send.call_count == 1
    speech.settle.assert_called_once_with(speech.reserve.return_value, "failed", {"cost": 0}, None)


def test_interrupted_audio_retains_generation_identity_and_does_not_repeat_synthesis(speech):
    def interrupted(payload, record_generation):
        record_generation("gen-tts-interrupted")
        raise ModelServiceError("interrupted")
    with patch.object(speech.client, "synthesize", side_effect=interrupted) as send:
        with pytest.raises(ModelServiceError, match="uncertain cost.*Do not repeat synthesis"):
            speech.generate(**ARGUMENTS)
    assert send.call_count == 1
    speech.settle.assert_called_once_with(speech.reserve.return_value, "unknown", generation_id="gen-tts-interrupted")


def test_unknown_accounting_preserves_generated_file_for_reuse(speech):
    with patch.object(speech.client, "synthesize", side_effect=synthesize()) as send, patch.object(
        speech.client, "generation", side_effect=ModelServiceError("not ready")
    ):
        with pytest.raises(ModelServiceError, match="Speech saved at .*unresolved accounting.*Do not repeat synthesis"):
            speech.generate(**ARGUMENTS)
    outputs = list(speech.runtime.shared_root.glob("outputs/job-7/*.mp3"))
    assert len(outputs) == 1 and outputs[0].read_bytes() == MP3
    assert send.call_count == 1
    assert all(call.args[1] == "unknown" for call in speech.settle.call_args_list)

def test_missing_generation_id_saves_audio_but_requires_admin_not_unusable_reconcile(speech):
    with patch.object(speech.client, "synthesize", side_effect=synthesize(generation_id=None)) as send:
        with pytest.raises(ModelServiceError, match="Speech saved at .*Ask the administrator"):
            speech.generate(**ARGUMENTS)
    assert len(list(speech.runtime.shared_root.glob("outputs/job-7/*.mp3"))) == 1
    speech.client.generation.assert_not_called()
    assert send.call_count == 1


@pytest.mark.parametrize("cost", ["NaN", "Infinity", -0.5, "invalid", None])
def test_invalid_cost_keeps_charge_unresolved_and_preserves_audio(speech, cost):
    with patch.object(speech.client, "generation", return_value={**GENERATION, "total_cost": cost}), patch.object(
        speech, "settle", wraps=lambda *args, **kwargs: ModelToolResources.settle(speech, *args, **kwargs)
    ), patch.object(speech.client, "synthesize", side_effect=synthesize()):
        with pytest.raises(ModelServiceError, match="unresolved accounting"):
            speech.generate(**ARGUMENTS)
    assert len(list(speech.runtime.shared_root.glob("outputs/job-7/*.mp3"))) == 1
    speech.db.log_event.assert_not_called()


def test_invalid_audio_still_accounts_provider_charge(speech):
    with patch.object(speech.client, "synthesize", side_effect=synthesize(data=b"not MP3")):
        with pytest.raises(ModelServiceError, match="output could not be saved"):
            speech.generate(**ARGUMENTS)
    assert speech.settle.call_args.args[1] == "completed"
    assert not list(speech.runtime.shared_root.glob("outputs/job-7/*"))


def test_reconcile_only_reads_charge_for_this_jobs_unknown_speech_request(speech):
    identifier = str(uuid.uuid4())
    conn = speech.db.connect.return_value.__enter__.return_value
    conn.execute.return_value.fetchone.return_value = {"status": "unknown", "generation_id": "gen-tts-test"}
    with patch.object(speech.client, "synthesize") as send:
        result = speech.reconcile(identifier)
    assert result["status"] == "completed" and result["usage"]["cost"] == 0.02
    assert conn.execute.call_args.args[1] == (identifier, 7)
    assert "preset->>'tool' = 'text_to_speech' FOR UPDATE" in conn.execute.call_args.args[0]
    speech.settle.assert_called_once_with(
        identifier, "completed", result["usage"], "gen-tts-test", conn=conn,
    )
    send.assert_not_called()
    speech.reserve.assert_not_called()


@pytest.mark.parametrize("row", [None, {"status": "completed", "generation_id": "gen-tts-test"},
                                {"status": "pending", "generation_id": None}])
def test_reconcile_does_not_mutate_foreign_completed_or_in_flight_requests(speech, row):
    conn = speech.db.connect.return_value.__enter__.return_value
    conn.execute.return_value.fetchone.return_value = row
    with pytest.raises(ModelServiceError):
        speech.reconcile(str(uuid.uuid4()))
    speech.client.generation.assert_not_called()
    speech.settle.assert_not_called()


def test_transport_uses_speech_endpoint_and_captures_header_before_read(monkeypatch):
    monkeypatch.setattr("assistant_agent.model_service.read_secret", lambda name: "test-key")
    client = OpenRouterSpeech(AppConfig({}))
    callback = MagicMock()
    with patch("assistant_agent.tts.open_https_request", return_value=AudioResponse()) as send:
        data, content_type, generation_id = client.synthesize({"model": MODEL["id"], "input": "Hello"}, callback)
    assert data == MP3 and content_type == "audio/mpeg" and generation_id == "gen-tts-test"
    callback.assert_called_once_with("gen-tts-test")
    request = send.call_args.args[0]
    assert request.full_url == "https://openrouter.ai/api/v1/audio/speech"
    assert request.method == "POST" and request.get_header("Accept") == "audio/*"
    assert json.loads(request.data)["input"] == "Hello"


@pytest.mark.parametrize("code, kind", [(400, ProviderRejected), (429, ProviderRejected), (503, ModelServiceError)])
def test_speech_transport_does_not_retry_or_echo_provider_error_bodies(monkeypatch, code, kind):
    monkeypatch.setattr("assistant_agent.model_service.read_secret", lambda name: "test-key")
    with patch("assistant_agent.tts.open_https_request", side_effect=urllib.error.HTTPError(
        "url", code, "failure", {}, io.BytesIO(b"private-input-canary"),
    )) as send:
        with pytest.raises(kind) as error:
            OpenRouterSpeech(AppConfig({})).synthesize({}, MagicMock())
    assert "private-input-canary" not in str(error.value)
    assert send.call_count == 1


@pytest.mark.parametrize("data", [b"", b"x" * (MAX_AUDIO_BYTES + 1)])
def test_speech_output_bounds_preserve_generation_id(monkeypatch, data):
    monkeypatch.setattr("assistant_agent.model_service.read_secret", lambda name: "test-key")
    callback = MagicMock()
    with patch("assistant_agent.tts.open_https_request", return_value=AudioResponse(data=data)):
        with pytest.raises(ModelServiceError, match="empty or exceeds"):
            OpenRouterSpeech(AppConfig({})).synthesize({}, callback)
    callback.assert_called_once_with("gen-tts-test")

@pytest.mark.parametrize("partial_read", [True, False])
def test_partial_binary_response_is_uncertain_and_never_retried(monkeypatch, partial_read):
    monkeypatch.setattr("assistant_agent.model_service.read_secret", lambda name: "test-key")
    response = AudioResponse()
    if partial_read:
        response.read = MagicMock(side_effect=IncompleteRead(b"ID3"))
    else:
        response.headers["Content-Length"] = str(len(MP3) + 10)
    callback = MagicMock()
    with patch("assistant_agent.tts.open_https_request", return_value=response) as send:
        with pytest.raises(ModelServiceError, match="interrupted|declared length"):
            OpenRouterSpeech(AppConfig({})).synthesize({}, callback)
    assert send.call_count == 1
    callback.assert_called_once_with("gen-tts-test")


def test_accounting_retries_only_read_only_lookup(monkeypatch):
    monkeypatch.setattr("assistant_agent.model_service.read_secret", lambda name: "test-key")
    response = io.BytesIO(json.dumps({"data": GENERATION}).encode())
    with patch("assistant_agent.tts.open_https_request", side_effect=[
        urllib.error.HTTPError("url", 404, "not ready", {}, None), response,
    ]) as send, patch("assistant_agent.tts.time.sleep"):
        assert OpenRouterSpeech(AppConfig({})).generation("gen-tts-test") == GENERATION
    assert send.call_count == 2
    assert all(call.args[0].method == "GET" for call in send.call_args_list)
    assert send.call_args.args[0].full_url.endswith("/generation?id=gen-tts-test")


@pytest.mark.parametrize("data", [{**GENERATION, "id": "different"}, {**GENERATION, "api_type": "completions"}, []])
def test_accounting_rejects_mismatched_generation_or_api(monkeypatch, data):
    monkeypatch.setattr("assistant_agent.model_service.read_secret", lambda name: "test-key")
    with patch("assistant_agent.tts.open_https_request", return_value=io.BytesIO(json.dumps({"data": data}).encode())):
        with pytest.raises(ModelServiceError, match="invalid speech accounting"):
            OpenRouterSpeech(AppConfig({})).generation("gen-tts-test")


@pytest.mark.parametrize("mime,data", [
    ("audio/pcm", b"\x00\x00"),
    ("audio/pcm;rate=24000;channels=1", b"\x00"),
    ("audio/pcm;rate=24000;channels=9", b"\x00" * 18),
    ("audio/mpeg", MP3),
])
def test_wav_requires_real_pcm_metadata_and_complete_frames(mime, data):
    with pytest.raises(ModelServiceError):
        playable_audio(data, mime, "wav")


@pytest.mark.parametrize("unresolved,spent", [(1, 0), (0, 5)])
def test_shared_budget_blocks_speech_before_provider_call(speech, unresolved, spent):
    conn = speech.db.connect.return_value.__enter__.return_value
    conn.execute.return_value.fetchone.return_value = {"spent": spent, "unresolved": unresolved}
    with patch.object(speech, "reserve", wraps=lambda metadata: ModelToolResources.reserve(speech, metadata)), patch.object(
        speech, "budget", wraps=lambda conn=None: ModelToolResources.budget(speech, conn)
    ), patch.object(speech.client, "synthesize") as send:
        with pytest.raises(ModelServiceError, match="unknown-cost|budget exhausted"):
            speech.generate(**ARGUMENTS)
    send.assert_not_called()
    assert conn.execute.call_args_list[0].args[0] == "SELECT pg_advisory_xact_lock(hashtext('assistant_model_service_budget'))"


def test_tool_gating_is_independent_of_presets_and_dispatches_without_telegram(tmp_path, monkeypatch):
    config = AppConfig({"agent": {
        "tts": {"enabled": True}, "model_service": {"enabled": False},
        "filesystem": {"shared_root": str(tmp_path), "require_mount": False},
    }})
    monkeypatch.setattr("assistant_agent.tts.read_secret", lambda name: "test-key")
    assert "text_to_speech" in available_function_names(config)
    assert "llm_service" not in available_function_names(config)
    assert "telegram_send" not in available_function_names(config)
    assert "text_to_speech" in tool_catalog(config)
    assert any(tool["function"]["name"] == "text_to_speech"
               for tool in available_tools(config, enabled_names={"text_to_speech"}))
    runtime = ToolRuntime(MagicMock(), config, {"id": 7})
    with patch.object(OpenRouterSpeech, "models", return_value=[MODEL]):
        assert runtime.run("text_to_speech", {"action": "list_models"})["total"] == 1
    config.values["agent"]["tts"]["enabled"] = False
    with pytest.raises(ToolError, match="not available"):
        runtime.run("text_to_speech", {"action": "list_models"})
    config.values["agent"]["tts"]["enabled"] = True
    monkeypatch.setattr("assistant_agent.tts.read_secret", lambda name: None)
    assert "text_to_speech" not in available_function_names(config)
