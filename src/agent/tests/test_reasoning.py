import json
from unittest.mock import patch

import pytest

from assistant_agent.config import AppConfig
from assistant_agent.reasoning import configure, validate_config
from assistant_agent.llm_providers import build_request, normalize_response


def config(mode, **settings):
    return AppConfig({"agent": {"llm": {"reasoning": {"mode": mode, **settings}}}})


@pytest.mark.parametrize("route", [None, "task"])
def test_missing_task_policy_applies_advertised_low_without_increasing_output_cap(route):
    payload = {"max_tokens": 8192}
    with patch("assistant_agent.reasoning.capabilities", return_value={"supported_efforts": ["low", "high"]}):
        decision = configure(AppConfig({}), "openrouter", "test", route, payload)
    assert decision["mode"] == "auto"
    assert payload == {"max_tokens": 8192, "reasoning": {"effort": "low"}}


def test_partial_task_policy_and_explicit_high_effort():
    with patch("assistant_agent.reasoning.capabilities", return_value={"supported_efforts": ["low", "high"]}):
        partial = AppConfig({"agent": {"llm": {"reasoning": {"max_tokens": 2048}}}})
        payload = {"max_tokens": 8192}
        assert configure(partial, "openrouter", "test", "task", payload)["mode"] == "auto"
        assert payload["reasoning"] == {"effort": "low"}
        payload = {"max_tokens": 8192}
        configure(config("effort", effort="high"), "openrouter", "test", "task", payload)
        assert payload == {"max_tokens": 8192, "reasoning": {"effort": "high"}}


def test_auto_uses_advertised_low_and_explicit_budget_never_becomes_effort():
    payload = {"max_tokens": 8192}
    with patch("assistant_agent.reasoning.capabilities", return_value={"supported_efforts": ["low", "high"]}):
        configure(config("auto"), "openrouter", "test", "task", payload)
        assert payload["reasoning"] == {"effort": "low"}
        with pytest.raises(ValueError, match="separate thinking-token cap"):
            configure(config("budget"), "openrouter", "test", "task", {"max_tokens": 8192})


def test_budget_reserves_answer_capacity_and_mandatory_reasoning_cannot_be_disabled():
    with patch("assistant_agent.reasoning.capabilities", return_value={
        "supports_max_tokens": True, "mandatory": True,
    }):
        payload = {"max_tokens": 8192}
        configure(config("budget", max_tokens=2048), "openrouter", "test", "task", payload)
        assert payload["reasoning"] == {"max_tokens": 2048}
        with pytest.raises(ValueError, match="leave room"):
            configure(config("budget", max_tokens=8192), "openrouter", "test", "task", {"max_tokens": 8192})
        with pytest.raises(ValueError, match="mandatory"):
            configure(config("off"), "openrouter", "test", "task", {"max_tokens": 8192})


def test_unknown_capabilities_are_explicit_and_auxiliary_routes_have_separate_policy():
    with patch("assistant_agent.reasoning.capabilities", return_value=None):
        payload = {"max_tokens": 8192}
        decision = configure(config("auto"), "openrouter", "test", "task", payload)
        assert "reason" in decision and "reasoning" not in payload
        with pytest.raises(ValueError, match="Cannot verify"):
            configure(config("effort"), "openrouter", "test", "task", payload)
        assert configure(config("effort"), "openrouter", "test", "naming", payload)["mode"] == "provider_default"
    with pytest.raises(ValueError, match="valid reasoning"):
        validate_config(config("invented"))


@pytest.mark.parametrize("value", [1.2, True, "1.5", -1, 0])
def test_invalid_reasoning_token_budgets_are_rejected(value):
    with pytest.raises(ValueError, match="token budget"):
        validate_config(config("budget", max_tokens=value))


def test_gemini_replays_signatures_and_does_not_expose_thought_text():
    parts = [{"text": "internal thought", "thought": True},
             {"functionCall": {"id": "native-call", "name": "lookup", "args": {}}, "thoughtSignature": "opaque"}]
    response = normalize_response("gemini", {
        "responseId": "r1", "candidates": [{"content": {"parts": parts}}],
        "usageMetadata": {"thoughtsTokenCount": 12, "candidatesTokenCount": 3, "totalTokenCount": 17},
    })
    message = response["choices"][0]["message"]
    assert message["content"] is None
    assert response["usage"]["completion_tokens_details"]["reasoning_tokens"] == 12
    assert response["usage"]["completion_tokens"] == 15
    call_id = message["tool_calls"][0]["id"]
    _, _, body = build_request("gemini", "https://generativelanguage.googleapis.com/v1beta", "test", {
        "model": "test", "max_tokens": 256,
        "messages": [message, {"role": "tool", "tool_call_id": call_id, "content": "{}"}],
    })
    contents = json.loads(body)["contents"]
    assert contents[0]["parts"] == parts
    assert contents[1]["parts"][0]["functionResponse"]["id"] == "native-call"


def test_openai_responses_replays_encrypted_reasoning_and_translates_tools():
    output = [{"type": "reasoning", "id": "r1", "encrypted_content": "opaque", "summary": []},
              {"type": "function_call", "call_id": "c1", "name": "lookup", "arguments": "{}"}]
    response = normalize_response("openai", {"output": output, "usage": {
        "input_tokens": 5, "output_tokens": 10, "total_tokens": 15,
        "output_tokens_details": {"reasoning_tokens": 8},
    }})
    _, _, body = build_request("openai", "https://api.openai.com/v1", "test", {
        "model": "gpt-6-astra", "max_tokens": 8192, "temperature": 0.2,
        "reasoning": {"effort": "low"},
        "messages": [response["choices"][0]["message"],
                     {"role": "tool", "tool_call_id": "c1", "content": "{}"}],
    })
    request = json.loads(body)
    assert request["input"][:2] == output
    assert request["input"][2]["type"] == "function_call_output"
    assert "temperature" not in request and request["store"] is False
    assert request["max_output_tokens"] == 8192
