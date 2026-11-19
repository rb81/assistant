import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from assistant_agent.config import AppConfig
from assistant_agent.llm_client import LlmClient
from assistant_agent.model_service import OpenRouterModels
from assistant_agent.security_screening import DecisionClient


@pytest.mark.parametrize("url,enabled,expected", [
    ("https://openrouter.ai/api/v1", True, {"zdr": True}),
    ("https://openrouter.ai/api/v1", False, None),
    ("https://api.openai.com/v1", True, None),
    ("https://openrouter.ai.example.test/v1", True, None),
])
def test_llm_zdr_is_explicit_and_openrouter_only(url, enabled, expected):
    config = AppConfig({"agent": {"llm": {"base_url": url, "zdr_only": enabled}}})
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"choices":[]}'
    with patch("assistant_agent.llm_client.read_secret", return_value="fixture-key"), \
            patch("assistant_agent.llm_client.refresh_request_context", side_effect=lambda messages, _: messages), \
            patch("assistant_agent.llm_client.open_https_request", return_value=response) as send:
        LlmClient(config).chat([{"role": "user", "content": "Fixture"}], [])
    assert json.loads(send.call_args.args[0].data).get("provider") == expected


def test_fallback_keeps_zdr_enforcement():
    config = AppConfig({"agent": {"llm": {
        "base_url": "https://openrouter.ai/api/v1", "model": "primary",
        "fallback_model": "fallback", "zdr_only": True,
    }}})
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"choices":[]}'
    failure = urllib.error.HTTPError("https://openrouter.ai", 429, "busy", {}, io.BytesIO(b"busy"))
    with patch("assistant_agent.llm_client.read_secret", return_value="fixture-key"), \
            patch("assistant_agent.llm_client.refresh_request_context", side_effect=lambda messages, _: messages), \
            patch("assistant_agent.llm_client.open_https_request", side_effect=[failure, response]) as send:
        LlmClient(config).chat([], [])
    bodies = [json.loads(call.args[0].data) for call in send.call_args_list]
    assert [body["model"] for body in bodies] == ["primary", "fallback"]
    assert all(body["provider"] == {"zdr": True} for body in bodies)


def test_global_policy_covers_model_tools_speech_and_openrouter_screening():
    config = AppConfig({"agent": {"llm": {"zdr_only": True}}})
    with patch("assistant_agent.model_service.read_secret", return_value="fixture-key"):
        for path in ("/chat/completions", "/audio/speech"):
            request = OpenRouterModels(config).request(path, {"model": "fixture"})
            assert json.loads(request.data)["provider"] == {"zdr": True}
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"answers":{}}'
    with patch("assistant_agent.security_screening.open_https_request", return_value=response) as send:
        DecisionClient(config, api_key="fixture-key").decide({}, [])
    assert json.loads(send.call_args.args[0].data)["provider"] == {"zdr": True}
