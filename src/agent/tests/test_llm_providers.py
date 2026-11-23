import json
from unittest.mock import MagicMock, patch

import pytest

from assistant_agent.config import AppConfig
from assistant_agent.llm_client import LlmClient, _TransientLlmError
from assistant_agent.llm_providers import build_request, normalize_response, provider_for


@pytest.mark.parametrize(("provider", "base_url", "model", "expected_url", "header"), [
    ("openai", "https://api.openai.com/v1", "gpt-4.1", "https://api.openai.com/v1/chat/completions",
     "Authorization"),
    ("anthropic", "https://api.anthropic.com", "claude-sonnet-4-6", "https://api.anthropic.com/v1/messages",
     "x-api-key"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta", "gemini-2.5-flash",
     "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
     "x-goog-api-key"),
    ("cloudflare", "https://api.cloudflare.com/client/v4/accounts/example/ai/v1",
     "@cf/meta/llama-3.1-8b-instruct",
     "https://api.cloudflare.com/client/v4/accounts/example/ai/v1/chat/completions",
     "Authorization"),
])
def test_native_provider_request_routing(provider, base_url, model, expected_url, header):
    config = AppConfig({"agent": {"llm": {"provider": provider, "base_url": base_url, "model": model}}})
    response = MagicMock()
    response_body = {
        "anthropic": b'{"content":[{"type":"text","text":"ok"}]}',
        "gemini": b'{"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}',
    }.get(provider, b'{"choices":[{"message":{"content":"ok"}}]}')
    response.__enter__.return_value.read.return_value = response_body
    with patch("assistant_agent.llm_client.read_secret", return_value="provider-key"), \
            patch("assistant_agent.llm_client.refresh_request_context", side_effect=lambda messages, _: messages), \
            patch("assistant_agent.llm_client.open_https_request", return_value=response) as send:
        result = LlmClient(config).chat([{"role": "user", "content": "hello"}], [])
    request = send.call_args.args[0]
    assert request.full_url == expected_url
    assert header.lower() in {name.lower() for name, _ in request.header_items()}
    assert result["choices"][0]["message"]["content"] == "ok"


def test_claude_tool_messages_are_translated_to_and_from_native_format():
    payload = {
        "model": "claude-sonnet-4-6", "max_tokens": 256, "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "Use a tool."},
            {"role": "user", "content": "Lookup."},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "lookup", "arguments": "{\"query\":\"value\"}"},
            }]},
            {"role": "tool", "tool_call_id": "call-1", "content": "{\"answer\":\"found\"}"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "lookup", "description": "Lookup a value.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }}],
    }
    _, headers, body = build_request("anthropic", "https://api.anthropic.com", "key", payload)
    request = json.loads(body)
    assert headers["x-api-key"] == "key"
    assert request["system"] == "Use a tool."
    assert request["messages"][1]["content"][0]["type"] == "tool_use"
    assert request["messages"][2]["content"][0]["tool_use_id"] == "call-1"
    assert request["tools"][0]["input_schema"]["type"] == "object"

    normalized = normalize_response("anthropic", {
        "id": "msg-1", "model": payload["model"], "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "call-2", "name": "lookup", "input": {"query": "next"}}],
        "usage": {"input_tokens": 3, "output_tokens": 4},
    })
    assert normalized["choices"][0]["finish_reason"] == "tool_calls"
    assert normalized["choices"][0]["message"]["tool_calls"][0]["id"] == "call-2"


def test_gemini_tools_are_translated_and_normalized():
    payload = {
        "model": "gemini-2.5-flash", "max_tokens": 128, "temperature": 0.4,
        "messages": [
            {"role": "user", "content": "Lookup."},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "lookup", "arguments": "{\"query\":\"value\"}"},
            }]},
            {"role": "tool", "tool_call_id": "call-1", "content": "{\"answer\":\"found\"}"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "lookup", "parameters": {"type": "object", "properties": {}},
        }}],
    }
    url, headers, body = build_request("gemini", "https://generativelanguage.googleapis.com/v1beta", "key", payload)
    request = json.loads(body)
    assert url.endswith("/models/gemini-2.5-flash:generateContent")
    assert headers["x-goog-api-key"] == "key"
    assert request["contents"][1]["parts"][0]["functionCall"]["name"] == "lookup"
    assert request["contents"][2]["parts"][0]["functionResponse"]["name"] == "lookup"
    normalized = normalize_response("gemini", {
        "responseId": "response-1", "modelVersion": payload["model"],
        "candidates": [{
            "content": {"parts": [
                {"functionCall": {"name": "lookup", "args": {"query": "next"}}},
            ]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5},
    })
    assert normalized["choices"][0]["finish_reason"] == "tool_calls"
    assert normalized["usage"]["total_tokens"] == 5


def test_openai_sets_store_false_and_cloudflare_disables_gateway_payload_storage():
    payload = {"model": "gpt-4.1", "messages": []}
    _, _, body = build_request("openai", "https://api.openai.com/v1", "key", payload)
    assert json.loads(body)["store"] is False
    _, headers, _ = build_request(
        "cloudflare", "https://api.cloudflare.com/client/v4/accounts/example/ai/v1", "key", payload
    )
    assert headers["cf-aig-collect-log"] == "false"
    assert headers["cf-aig-collect-log-payload"] == "false"
    assert headers["cf-aig-skip-cache"] == "true"


@pytest.mark.parametrize(("provider", "base_url"), [
    ("openai", "https://proxy.example/v1"),
    ("anthropic", "https://proxy.example"),
    ("gemini", "https://proxy.example/v1beta"),
    ("cloudflare", "https://proxy.example/ai/v1"),
])
def test_native_provider_policies_require_the_official_api_host(provider, base_url):
    with pytest.raises(ValueError, match="official"):
        provider_for(AppConfig({"agent": {"llm": {"provider": provider}}}), base_url)


def test_explicit_model_route_uses_its_selected_provider():
    config = AppConfig({"agent": {
        "llm": {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "model": "gpt-4.1"},
        "model_routes": {"task": {"provider": "openai", "fallbacks": []}},
    }})
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"choices":[{"message":{"content":"ok"}}]}'
    with patch("assistant_agent.llm_client.read_secret", return_value="openai-key"), \
            patch("assistant_agent.llm_client.refresh_request_context", side_effect=lambda messages, _: messages), \
            patch("assistant_agent.llm_client.open_https_request", return_value=response) as send:
        LlmClient(config, route="task").chat([{"role": "user", "content": "hello"}], [])
    assert send.call_args.args[0].full_url == "https://api.openai.com/v1/chat/completions"


def test_explicit_cross_provider_fallbacks_are_ordered_and_disabled_providers_block():
    config = AppConfig({"agent": {
        "llm": {"provider": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4.1"},
        "model_routes": {"task": {
            "provider": "openai", "fallbacks": ["anthropic:claude-sonnet-4-6", "gemini:gemini-2.5-flash"],
        }},
    }})
    response = {"choices": [{"message": {"content": "recovered"}}]}
    with patch("assistant_agent.llm_client.read_secret", side_effect=lambda name: name):
        client = LlmClient(config, route="task")
    with patch("assistant_agent.llm_client.read_secret", side_effect=lambda name: name), \
            patch.object(client, "_chat_with_model", side_effect=[_TransientLlmError("busy"), response]) as send:
        assert client.chat([], []) == response
    assert send.call_args_list[1].kwargs == {
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key": "AGENT_LLM_ANTHROPIC_API_KEY",
    }

    disabled = AppConfig({
        **config.values,
        "agent": {
            **config.values["agent"],
            "providers": {"disabled": ["anthropic"]},
        },
    })
    with patch("assistant_agent.llm_client.read_secret", side_effect=lambda name: name):
        blocked = LlmClient(disabled, route="task")
    with patch.object(blocked, "_chat_with_model", side_effect=_TransientLlmError("busy")) as send:
        with pytest.raises(RuntimeError, match="disabled provider anthropic"):
            blocked.chat([], [])
    send.assert_called_once()
