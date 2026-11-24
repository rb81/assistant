import io
import json
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from assistant_agent.config import AppConfig
from assistant_agent.llm_catalog import ModelCatalogError, _models, _request, discover_models
from assistant_agent.llm_providers import provider_for


def catalog_config(provider="openai", base_url="https://api.openai.com/v1", **llm):
    return AppConfig({"agent": {"llm": {
        "provider": provider,
        "base_url": base_url,
        **llm,
    }}})


def test_native_model_discovery_uses_server_credentials_and_filters_strict_allowlist():
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "data": [{"id": "gpt-4.1"}, {"id": "gpt-4.1-mini"}],
    }).encode()
    with patch("assistant_agent.llm_catalog.read_secret", return_value="private-key"), \
            patch("assistant_agent.llm_catalog.open_https_request", return_value=response) as send:
        result = discover_models(catalog_config(
            zdr_only=True,
            zdr_verified_providers=["openai"],
            zdr_verified_models=["openai:gpt-4.1"],
        ))
    request = send.call_args.args[0]
    assert request.full_url == "https://api.openai.com/v1/models"
    assert request.get_header("Authorization") == "Bearer private-key"
    assert result["models"] == [{"id": "gpt-4.1", "name": "gpt-4.1"}]
    assert result["zdr_only"] is True


def test_openrouter_discovery_warns_without_attestations_or_hiding_zdr_models(monkeypatch):
    monkeypatch.setattr("assistant_agent.model_service._catalog", {})
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "data": [{"id": "example/model"}],
    }).encode()
    with patch("assistant_agent.model_service.read_secret", return_value="fixture-key"), \
            patch("assistant_agent.model_service.open_https_request", return_value=response) as send:
        result = discover_models(catalog_config(
            provider="openrouter", base_url="https://openrouter.ai/api/v1", zdr_only=True,
        ))
    assert result["models"] == [{"id": "example/model", "name": "example/model"}]
    assert "zdr=true" in send.call_args.args[0].full_url
    assert "Safety Retention" in result["warning"]
    assert "does not block" in result["warning"]


def test_openrouter_discovery_does_not_require_database_dependencies():
    code = """
import sys
from unittest.mock import patch
class BlockDatabase:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "psycopg" or fullname.startswith("psycopg."):
            raise ModuleNotFoundError("database dependency unavailable")
sys.meta_path.insert(0, BlockDatabase())
from assistant_agent.config import AppConfig
from assistant_agent.llm_catalog import discover_models
config = AppConfig({"agent": {"llm": {"base_url": "https://openrouter.ai/api/v1"}}})
with patch("assistant_agent.model_service.OpenRouterModels.models",
           return_value=[{"id": "example/model"}]):
    result = discover_models(config, provider_id="openrouter")
assert result["models"][0]["id"] == "example/model"
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_strict_model_discovery_rejects_unverified_and_non_zdr_providers():
    with pytest.raises(ModelCatalogError, match="does not provide strict"):
        discover_models(catalog_config(
            provider="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            zdr_only=True,
        ))
    with pytest.raises(ModelCatalogError, match="Verify the provider account"):
        discover_models(catalog_config(zdr_only=True))


def test_openrouter_url_cannot_be_selected_with_a_different_provider_policy():
    with pytest.raises(ValueError, match="OpenRouter URLs must use"):
        provider_for(
            AppConfig({"agent": {"llm": {"provider": "openai"}}}),
            "https://openrouter.ai/api/v1",
        )


def test_cloudflare_discovery_returns_only_workers_ai_models_and_gateway_warning():
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "result": [
            {"name": "@cf/meta/llama-3.1-8b-instruct"},
            {"name": "@cf/mistral/model"},
            {"name": "external/model"},
        ],
    }).encode()
    config = catalog_config(
        provider="cloudflare",
        base_url="https://api.cloudflare.com/client/v4/accounts/example/ai/v1",
    )
    with patch("assistant_agent.llm_catalog.read_secret", return_value="private-token"), \
            patch("assistant_agent.llm_catalog.open_https_request", return_value=response) as send:
        result = discover_models(config)
    request = send.call_args.args[0]
    assert request.full_url == (
        "https://api.cloudflare.com/client/v4/accounts/example/ai/models/search?page=1&per_page=1000"
    )
    assert [model["id"] for model in result["models"]] == [
        "@cf/meta/llama-3.1-8b-instruct", "@cf/mistral/model",
    ]
    assert "Logpush" in result["warning"]


def test_gateway_discovery_lists_chat_models_across_catalog_pages():
    from assistant_agent.llm_catalog import _gateway_models
    request = _request("cloudflare-gateway", "https://api.cloudflare.com/client/v4/accounts/example/ai/v1", "token")
    pages = [
        {"success": True, "result_info": {"total_count": 3}, "result": [
            {"model_id": "openai/gpt-4.1", "name": "GPT-4.1", "task": "Text Generation",
             "request_formats": ["chat-completions"]},
            {"model_id": "image/model", "task": "Text-to-Image"},
        ]},
        {"success": True, "result_info": {"total_count": 3}, "result": [
            {"model_id": "anthropic/claude", "name": "Claude", "task": "Text Generation"},
        ]},
    ]
    with patch("assistant_agent.llm_catalog._read_catalog", side_effect=pages) as read:
        models = _gateway_models(request)
    assert [model["id"] for model in models] == ["anthropic/claude", "openai/gpt-4.1"]
    assert "page=2" in read.call_args.args[0].full_url
    assert read.call_args.args[0].get_header("Authorization") == "Bearer token"


def test_gateway_discovery_accepts_catalog_pages_larger_than_two_mib():
    response = {
        "success": True, "result_info": {"total_count": 1}, "result": [{
            "model_id": "openai/gpt-4.1", "task": "Text Generation",
            "schema": "x" * (2 * 1024 * 1024),
        }],
    }
    config = AppConfig({"agent": {
        "llm": {"base_url": "https://api.cloudflare.com/client/v4/accounts/large-catalog/ai/v1"},
        "providers": {"cloudflare_account_id": "large-catalog"},
    }})
    with patch("assistant_agent.llm_catalog.open_https_request",
               return_value=io.BytesIO(json.dumps(response).encode())):
        result = discover_models(config, provider_id="cloudflare-gateway", credential_reader=lambda _: "fixture")
    assert [model["id"] for model in result["models"]] == ["openai/gpt-4.1"]


def test_gateway_discovery_rejects_incomplete_catalog():
    from assistant_agent.llm_catalog import _gateway_models
    request = _request("cloudflare-gateway", "https://api.cloudflare.com/client/v4/accounts/example/ai/v1", "token")
    with patch("assistant_agent.llm_catalog._read_catalog", return_value={
        "success": True, "result_info": {"total_count": 1}, "result": [],
    }):
        with pytest.raises(ModelCatalogError, match="incomplete"):
            _gateway_models(request)


def test_native_catalog_requests_use_provider_auth_and_gemini_capability_filtering():
    anthropic = _request("anthropic", "https://api.anthropic.com", "secret")
    assert anthropic.full_url == "https://api.anthropic.com/v1/models?limit=1000"
    assert anthropic.get_header("X-api-key") == "secret"
    assert anthropic.get_header("Anthropic-version") == "2023-06-01"
    gemini = _request(
        "gemini", "https://generativelanguage.googleapis.com/v1beta", "secret"
    )
    assert gemini.full_url.endswith("/v1beta/models?pageSize=1000")
    assert gemini.get_header("X-goog-api-key") == "secret"
    assert _models({
        "models": [
            {"name": "models/gemini-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-embedding", "supportedGenerationMethods": ["embedContent"]},
        ],
    }, "gemini") == [{"id": "models/gemini-flash", "name": "models/gemini-flash"}]
