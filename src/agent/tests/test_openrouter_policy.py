import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from assistant_agent.config import AppConfig
from assistant_agent.llm_client import LlmClient
from assistant_agent.model_service import OpenRouterModels
from assistant_agent.security_screening import DecisionClient
from assistant_agent.openrouter_policy import apply_routing_policy


@pytest.mark.parametrize("url,enabled,expected", [
    ("https://openrouter.ai/api/v1", True, {"zdr": True}),
    ("https://openrouter.ai/api/v1", False, None),
])
def test_llm_zdr_applies_without_openrouter_attestations(url, enabled, expected):
    config = AppConfig({"agent": {"llm": {
        "base_url": url, "zdr_only": enabled,
    }}})
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"choices":[]}'
    with patch("assistant_agent.llm_client.read_secret", return_value="fixture-key"), \
            patch("assistant_agent.llm_client.refresh_request_context", side_effect=lambda messages, _: messages), \
            patch("assistant_agent.llm_client.open_https_request", return_value=response) as send:
        LlmClient(config).chat([{"role": "user", "content": "Fixture"}], [])
    assert json.loads(send.call_args.args[0].data).get("provider") == expected


def test_zdr_blocks_unverified_direct_provider():
    config = AppConfig({"agent": {"llm": {"base_url": "https://api.openai.com/v1", "zdr_only": True}}})
    with patch("assistant_agent.llm_client.read_secret", return_value="fixture-key"), \
            patch("assistant_agent.llm_client.refresh_request_context", side_effect=lambda messages, _: messages), \
            patch("assistant_agent.llm_client.open_https_request") as send:
        with pytest.raises(ValueError, match="account retention controls"):
            LlmClient(config).chat([], [])
    send.assert_not_called()


def test_fallback_keeps_zdr_enforcement():
    config = AppConfig({"agent": {"llm": {
        "base_url": "https://openrouter.ai/api/v1", "model": "primary",
        "fallback_model": "fallback", "zdr_only": True, "allowed_providers": ["deepinfra"],
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
    assert all(body["provider"] == {"zdr": True, "only": ["deepinfra"]} for body in bodies)


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


@pytest.mark.parametrize("path", ["/chat/completions", "/audio/speech"])
def test_scoped_zdr_routes_without_global_policy_or_attestations(path):
    with patch("assistant_agent.model_service.read_secret", return_value="fixture-key"):
        request = OpenRouterModels(AppConfig({})).request(path, {"model": "fixture"}, scoped_zdr=True)
    assert json.loads(request.data)["provider"] == {"zdr": True}


@pytest.mark.parametrize("role,worker", [
    ("task-agent", "TaskAgent"),
    ("deep-research-agent", "DeepResearchAgent"),
    ("heartbeat", "Heartbeat"),
])
def test_safety_retention_warns_without_blocking_worker_startup(role, worker, caplog):
    from assistant_agent.main import run_role

    config = AppConfig({"agent": {
        "llm": {"base_url": "https://openrouter.ai/api/v1", "model": "primary",
                "fallback_model": "fallback", "zdr_only": True},
        "deep_research": {"model": "primary"},
        "search": {"primary_provider": "exa", "exa_enabled": True, "openrouter_enabled": False},
        "email": {"smtp_host": "mail.example.org", "smtp_from": "assistant@example.org"},
        "admin": {"email": "owner@example.org"},
    }})
    with patch("assistant_agent.main.load_config", return_value=config), \
            patch("assistant_agent.main.apply_shared_file_umask"), \
            patch("assistant_agent.main.ensure_prompt_files"), \
            patch("assistant_agent.main.validate_agent_prompt"), \
            patch("assistant_agent.main.database_url", return_value="fixture"), \
            patch("assistant_agent.validation.database_url", return_value="fixture"), \
            patch("assistant_agent.validation.read_secret", return_value="fixture-key"), \
            patch("assistant_agent.main.Database") as database, \
            patch("assistant_agent.main.install_stopper"), \
            patch("assistant_agent.main." + worker) as worker_class:
        database.return_value.run_migrations.return_value = []
        run_role(role)
    worker_class.return_value.run_forever.assert_called_once()
    assert "Safety Retention" in caplog.text
    assert "configuration error" not in caplog.text


def test_provider_allowlist_and_zdr_are_enforced_together_for_every_route():
    config = AppConfig({"agent": {"llm": {
        "zdr_only": True, "allowed_providers": ["deepinfra", "azure"],
        "zdr_verified_providers": ["openrouter"], "zdr_verified_models": ["openrouter:fixture"],
    }}})
    for path in ("/chat/completions", "/audio/speech"):
        with patch("assistant_agent.model_service.read_secret", return_value="fixture-key"):
            request = OpenRouterModels(config).request(path, {"model": "fixture"})
        assert json.loads(request.data)["provider"] == {"only": ["deepinfra", "azure"], "zdr": True}
    external = {}
    apply_routing_policy(config, "https://example.com/v1", external)
    assert external == {}


def test_existing_provider_restrictions_are_intersected_without_broadening():
    config = AppConfig({"agent": {"llm": {"allowed_providers": ["deepinfra", "azure"]}}})
    payload = {"provider": {"zdr": True, "only": ["deepinfra/turbo"]}}
    apply_routing_policy(config, "https://openrouter.ai/api/v1", payload)
    assert payload["provider"] == {"zdr": True, "only": ["deepinfra/turbo"]}
    with pytest.raises(ValueError, match="conflict"):
        apply_routing_policy(config, "https://openrouter.ai/api/v1", {"provider": {"only": ["openai"]}})
    with pytest.raises(ValueError, match="Invalid"):
        apply_routing_policy(AppConfig({"agent": {"llm": {"allowed_providers": ["bad slug"]}}}),
                             "https://openrouter.ai/api/v1", {})


def test_zdr_only_blocks_unverified_providers_and_models_and_gemini():
    from assistant_agent.openrouter_policy import require_zdr_eligibility

    config = AppConfig({"agent": {"llm": {"zdr_only": True}}})
    with pytest.raises(ValueError, match="account retention controls"):
        require_zdr_eligibility(config, "openai", "gpt-4.1")
    verified = AppConfig({"agent": {"llm": {
        "zdr_only": True, "zdr_verified_providers": ["openai"],
        "zdr_verified_models": ["openai:gpt-4.1"],
    }}})
    require_zdr_eligibility(verified, "openai", "gpt-4.1")
    with pytest.raises(ValueError, match="retention eligibility"):
        require_zdr_eligibility(verified, "openai", "gpt-4.1-mini")
    with pytest.raises(ValueError, match="Gemini Developer API"):
        require_zdr_eligibility(verified, "gemini", "gemini-2.5-flash")
