import json
from unittest.mock import MagicMock, patch

import pytest

from assistant_agent.config import AppConfig
from assistant_agent.llm_client import LlmClient, _TransientLlmError
from assistant_agent.llm_providers import build_request, normalize_response, provider_for
from assistant_agent.llm_providers import provider_base_url, provider_credential_name, provider_is_disabled


def test_provider_enablement_is_opt_in_when_enabled_list_is_present():
    config = AppConfig({"agent": {"providers": {"enabled": ["openai"], "disabled": ["anthropic"]}}})
    assert not provider_is_disabled(config, "openai")
    assert provider_is_disabled(config, "anthropic")
    assert provider_is_disabled(config, "a-new-provider")
    assert not provider_is_disabled(AppConfig({}), "openai"), "Legacy configurations remain compatible"
    config.values["agent"]["llm"] = {"provider": "openai", "base_url": "https://api.openai.com/v1"}
    config.values["agent"]["providers"]["enabled"] = []
    with pytest.raises(ValueError, match="disabled"):
        LlmClient(config)


def test_gateway_request_uses_cloudflare_token_gateway_id_and_privacy_headers():
    config = AppConfig({"agent": {
        "llm": {"provider": "cloudflare-gateway",
                "base_url": "https://api.cloudflare.com/client/v4/accounts/example/ai/v1",
                "model": "openai/gpt-4.1"},
        "providers": {"cloudflare_account_id": "example", "cloudflare_gateway_id": "private-gateway"},
    }})
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"choices":[{"message":{"content":"ok"}}]}'
    with patch("assistant_agent.llm_client.read_secret", return_value="token"), \
            patch("assistant_agent.llm_client.refresh_request_context", side_effect=lambda messages, _: messages), \
            patch("assistant_agent.llm_client.open_https_request", return_value=response) as send:
        LlmClient(config).chat([{"role": "user", "content": "hello"}], [])
    request = send.call_args.args[0]
    assert request.full_url == "https://api.cloudflare.com/client/v4/accounts/example/ai/v1/chat/completions"
    assert request.get_header("Cf-aig-gateway-id") == "private-gateway"
    assert request.get_header("Cf-aig-collect-log") == "false"
    assert json.loads(request.data)["model"] == "openai/gpt-4.1"


def test_gateway_zdr_requires_account_and_exact_model_attestation():
    config = AppConfig({"agent": {
        "llm": {"provider": "cloudflare-gateway", "zdr_only": True,
                "base_url": "https://api.cloudflare.com/client/v4/accounts/example/ai/v1"},
        "providers": {"cloudflare_account_id": "example"},
    }})
    with patch("assistant_agent.llm_client.read_secret", return_value="token"), \
            patch("assistant_agent.llm_client.open_https_request") as send:
        with pytest.raises(ValueError, match="account retention controls"):
            LlmClient(config).chat([], [])
        send.assert_not_called()
        config.values["agent"]["llm"]["zdr_verified_providers"] = ["cloudflare-gateway"]
        with pytest.raises(ValueError, match="retention eligibility"):
            LlmClient(config).chat([], [])
        send.assert_not_called()
        config.values["agent"]["llm"]["zdr_verified_models"] = ["cloudflare-gateway:anthropic/claude-sonnet-4.6"]
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"choices":[{"message":{"content":"ok"}}]}'
        send.return_value = response
        assert LlmClient(config).chat([], [])["choices"][0]["message"]["content"] == "ok"


def test_provider_sections_use_their_own_endpoints_and_preserve_legacy_custom_keys():
    config = AppConfig({"agent": {"providers": {
        "anthropic_base_url": "https://api.anthropic.com",
        "cloudflare_account_id": "example",
        "cloudflare_base_url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
    }}})
    assert provider_base_url(config, "anthropic") == "https://api.anthropic.com"
    assert provider_base_url(config, "cloudflare") == "https://api.cloudflare.com/client/v4/accounts/example/ai/v1"
    keys = {"AGENT_LLM_OPENAI_API_KEY": "legacy"}
    assert provider_credential_name("openai-compatible", keys.get) == "AGENT_LLM_OPENAI_API_KEY"
    keys["AGENT_PROVIDERS_OPENAI_COMPATIBLE_API_KEY"] = "dedicated"
    assert provider_credential_name("openai-compatible", keys.get) == "AGENT_PROVIDERS_OPENAI_COMPATIBLE_API_KEY"
    legacy = AppConfig({"agent": {"llm": {"base_url": "https://api.openai.com/custom/v1"}}})
    assert provider_base_url(legacy, "openai") == "https://api.openai.com/custom/v1"


@pytest.mark.parametrize("endpoint", ["http://api.openai.com/v1", "https://example.com/v1"])
def test_native_provider_endpoint_controls_reject_insecure_or_wrong_provider_urls(endpoint):
    with pytest.raises(ValueError):
        provider_base_url(AppConfig({"agent": {"providers": {"openai_base_url": endpoint}}}), "openai")


@pytest.mark.parametrize("provider", ["cloudflare", "cloudflare-gateway"])
def test_cloudflare_accepts_run_url_as_chat_api_base(provider):
    config = AppConfig({"agent": {"providers": {
        "cloudflare_account_id": "example",
        "cloudflare_base_url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run",
    }}})
    assert provider_base_url(config, provider) == "https://api.cloudflare.com/client/v4/accounts/example/ai/v1"


def test_explicit_routes_do_not_use_the_legacy_endpoint():
    config = AppConfig({"agent": {
        "llm": {"base_url": "https://api.openai.com/legacy"},
        "model_routes": {"task": {"provider": "openai"}},
    }})
    assert provider_base_url(config, "openai") == "https://api.openai.com/v1"


def test_explicit_route_does_not_inherit_the_task_model():
    config = AppConfig({"agent": {
        "llm": {"model": "task-model"},
        "model_routes": {"naming": {"provider": "openai"}},
    }})
    with patch("assistant_agent.llm_client.read_secret", return_value="token"):
        with pytest.raises(ValueError, match="Select a model for the naming route"):
            LlmClient(config, model="", route="naming")


@pytest.mark.parametrize("provider", ["cloudflare", "cloudflare-gateway"])
def test_cloudflare_accepts_account_specific_run_url(provider):
    endpoint = "https://api.cloudflare.com/client/v4/accounts/example/ai/run"
    config = AppConfig({"agent": {"providers": {"cloudflare_base_url": endpoint}}})
    assert provider_base_url(config, provider) == "https://api.cloudflare.com/client/v4/accounts/example/ai/v1"
    config.values["agent"]["providers"]["cloudflare_account_id"] = "other"
    with pytest.raises(ValueError, match="does not match"):
        provider_base_url(config, provider)


@pytest.mark.parametrize("provider", ["cloudflare", "cloudflare-gateway"])
def test_cloudflare_run_url_sends_openai_chat_request(provider):
    config = AppConfig({"agent": {
        "llm": {"model": "example/model"},
        "model_routes": {"task": {"provider": provider}},
        "providers": {"cloudflare_base_url": "https://api.cloudflare.com/client/v4/accounts/example/ai/run",
                      "cloudflare_gateway_id": "gateway"},
    }})
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"choices":[{"message":{"content":"ok"}}]}'
    with patch("assistant_agent.llm_client.read_secret", return_value="token"), \
            patch("assistant_agent.llm_client.refresh_request_context", side_effect=lambda messages, _: messages), \
            patch("assistant_agent.llm_client.open_https_request", return_value=response) as send:
        LlmClient(config).chat([{"role": "user", "content": "hello"}], [])
    request = send.call_args.args[0]
    assert request.full_url == "https://api.cloudflare.com/client/v4/accounts/example/ai/v1/chat/completions"
    assert json.loads(request.data)["model"] == "example/model"
    assert request.get_header("Cf-aig-gateway-id") == ("gateway" if provider == "cloudflare-gateway" else None)


@pytest.mark.parametrize("provider", ["cloudflare", "cloudflare-gateway"])
def test_cloudflare_rejects_non_base_api_urls(provider):
    config = AppConfig({"agent": {"providers": {
        "cloudflare_account_id": "example",
        "cloudflare_base_url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions",
    }}})
    with pytest.raises(ValueError, match=r"/ai/v1.*?/ai/run"):
        provider_base_url(config, provider)


@pytest.mark.parametrize(("provider", "base_url", "model", "expected_url", "header"), [
    ("openai", "https://api.openai.com/v1", "gpt-4.1", "https://api.openai.com/v1/responses",
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
        "openai": b'{"output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}]}',
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
    payload = {"model": "gpt-4.1", "messages": [], "max_tokens": 256}
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
    response.__enter__.return_value.read.return_value = b'{"output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}]}'
    with patch("assistant_agent.llm_client.read_secret", return_value="openai-key"), \
            patch("assistant_agent.llm_client.refresh_request_context", side_effect=lambda messages, _: messages), \
            patch("assistant_agent.llm_client.open_https_request", return_value=response) as send:
        LlmClient(config, route="task").chat([{"role": "user", "content": "hello"}], [])
    assert send.call_args.args[0].full_url == "https://api.openai.com/v1/responses"


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
