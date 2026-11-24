from types import SimpleNamespace
from unittest.mock import patch

import pytest

from assistant_agent.config import AppConfig
from assistant_agent.llm_catalog import ModelCatalogError
from assistant_agent.management_choices import provider_choices_metadata, setting_choices
from assistant_agent.management_tui import ManagementApp, OptionField, SelectionField, checkbox_field


def backend():
    return SimpleNamespace(
        values={"AGENT_PROVIDERS_DISABLED": "anthropic"},
        credentials={"AGENT_LLM_OPENROUTER_API_KEY": "fixture",
                     "AGENT_LLM_OPENAI_API_KEY": "fixture", "AGENT_LLM_ANTHROPIC_API_KEY": "fixture"},
        installation=SimpleNamespace(defaults=AppConfig({})),
    )


def test_provider_lists_use_configured_accounts_and_disabled_providers_can_be_reenabled():
    metadata = provider_choices_metadata(backend())
    assert metadata["AGENT_PROVIDERS_DISABLED"]["choices"] == ["openrouter", "openai", "anthropic"]
    assert metadata["AGENT_LLM_ZDR_VERIFIED_PROVIDERS"]["choices"] == ["openai", "anthropic"]
    assert metadata["AGENT_MODEL_ROUTES_TASK_PROVIDER"]["choices"] == ["inherit", "openrouter", "openai"]
    assert metadata["AGENT_TTS_MODEL"]["choices_source"] == "models"


def test_custom_endpoint_does_not_activate_with_an_unrelated_openrouter_key():
    item = backend()
    item.values["AGENT_PROVIDERS_OPENAI_COMPATIBLE_BASE_URL"] = "https://example.com/v1"
    item.credentials = {"AGENT_LLM_OPENROUTER_API_KEY": "fixture"}
    assert provider_choices_metadata(item)["AGENT_PROVIDERS_DISABLED"]["choices"] == ["openrouter"]


def test_gateway_is_a_separately_enabled_provider_with_shared_cloudflare_credentials():
    item = backend()
    item.credentials = {"AGENT_LLM_CLOUDFLARE_API_KEY": "fixture"}
    item.values["AGENT_PROVIDERS_ENABLED"] = "cloudflare-gateway"
    metadata = provider_choices_metadata(item)
    assert metadata["AGENT_PROVIDERS_DISABLED"]["choices"] == ["cloudflare", "cloudflare-gateway"]
    assert "cloudflare-gateway" in metadata["AGENT_MODEL_ROUTES_TASK_PROVIDER"]["choices"]
    assert "cloudflare" not in metadata["AGENT_MODEL_ROUTES_TASK_PROVIDER"]["choices"]
    assert "cloudflare-gateway" in metadata["AGENT_LLM_ZDR_VERIFIED_PROVIDERS"]["choices"]


def test_provider_selectors_follow_saved_priority_without_changing_routes():
    item = backend()
    item.values["AGENT_PROVIDERS_PRIORITY"] = "openai,openrouter"
    metadata = provider_choices_metadata(item)
    assert metadata["AGENT_MODEL_ROUTES_TASK_PROVIDER"]["choices"] == ["inherit", "openai", "openrouter"]
    assert metadata["AGENT_PROVIDERS_DISABLED"]["choices"] == ["openai", "openrouter", "anthropic"]


def test_terminal_provider_toggle_updates_draft_selectors_without_losing_saved_selection():
    enabled = checkbox_field("Enabled", True)
    selected = OptionField("openai", ["openai", "openrouter"])
    ui = SimpleNamespace(
        backend=SimpleNamespace(
            values={"AGENT_PROVIDERS_OPENROUTER_ENABLED": "true"},
            field_metadata={
                "AGENT_PROVIDERS_PRIORITY": {"choices": ["openai", "openrouter"]},
                "AGENT_PROVIDERS_OPENAI_ENABLED": {"provider_enablement": "openai"},
                "AGENT_PROVIDERS_OPENROUTER_ENABLED": {"provider_enablement": "openrouter"},
                "AGENT_LLM_PROVIDER": {"enum": ["auto", "openrouter", "openai"]},
            }),
        form={"AGENT_PROVIDERS_OPENAI_ENABLED": enabled, "AGENT_LLM_PROVIDER": selected},
    )
    ManagementApp.refresh_provider_choices(ui)
    assert selected.available_choices == ["auto", "openai", "openrouter"]
    enabled.checked = False
    ManagementApp.refresh_provider_choices(ui)
    assert selected.available_choices == ["auto", "openrouter"]
    assert selected.text == "openai"
    enabled.checked = True
    ManagementApp.refresh_provider_choices(ui)
    assert selected.available_choices == ["auto", "openai", "openrouter"]


def test_cloudflare_attestation_choices_exclude_unsupported_partner_routes():
    item = backend()
    item.credentials = {"AGENT_LLM_CLOUDFLARE_API_KEY": "fixture"}
    item.values["AGENT_PROVIDERS_DISABLED"] = "cloudflare-gateway"
    with patch("assistant_agent.management_choices.discover_models", return_value={"models": [
        {"id": "@cf/example/model", "name": "Workers AI"}, {"id": "partner/model", "name": "Partner"},
    ]}):
        result = setting_choices(AppConfig({"agent": {"providers": {"disabled": ["cloudflare-gateway"]}}}),
                                 item, "AGENT_LLM_ZDR_VERIFIED_MODELS")
    assert result["choices"] == ["cloudflare:@cf/example/model"]


def test_attestation_models_can_be_selected_before_enabling_strict_inference():
    config = AppConfig({"agent": {"llm": {"zdr_only": True}}})
    catalog = {"models": [{"id": "gpt-4.1", "name": "GPT-4.1"}]}
    with patch("assistant_agent.management_choices.discover_models", return_value=catalog) as discover:
        result = setting_choices(config, backend(), "AGENT_LLM_ZDR_VERIFIED_MODELS")
    assert result["choices"] == ["openai:gpt-4.1", "anthropic:gpt-4.1"]
    assert all(not call.args[0].get_bool("agent.llm.zdr_only") for call in discover.call_args_list)
    assert config.get_bool("agent.llm.zdr_only"), "Enumeration must not relax inference policy"


def test_fallback_choices_preserve_zdr_and_report_provider_discovery_failures():
    config = AppConfig({"agent": {"llm": {"zdr_only": True}}})
    with patch("assistant_agent.management_choices.discover_models", side_effect=[
        {"models": [{"id": "example/model", "name": "Example"}]},
        ModelCatalogError("Account is not verified"),
        {"models": []},
    ]) as discover:
        result = setting_choices(config, backend(), "AGENT_MODEL_ROUTES_TASK_FALLBACKS")
    assert result["choices"] == ["openrouter:example/model"]
    assert result["warnings"] == ["openai: Account is not verified"]
    assert all(call.args[0].get_bool("agent.llm.zdr_only") for call in discover.call_args_list)


def test_model_choices_use_the_explicit_draft_provider_and_constrain_tool_routes():
    with patch("assistant_agent.management_choices.discover_models", return_value={
        "models": [{"id": "claude-sonnet-4-6", "name": "Claude Sonnet"}],
    }) as discover:
        result = setting_choices(AppConfig({}), backend(), "AGENT_LLM_MODEL", provider_id="anthropic")
    assert result["choices"] == ["claude-sonnet-4-6"]
    assert discover.call_args.kwargs["provider_id"] == "anthropic"
    with pytest.raises(ValueError, match="requires OpenRouter"):
        setting_choices(AppConfig({}), backend(), "AGENT_TTS_MODEL", provider_id="openai")


def test_hosting_provider_discovery_validates_provider_identifiers():
    with patch("assistant_agent.management_choices._read_catalog", return_value={
        "data": [{"slug": "deepinfra", "name": "DeepInfra"}],
    }):
        result = setting_choices(AppConfig({}), backend(), "AGENT_LLM_ALLOWED_PROVIDERS")
    assert result["choices"] == ["deepinfra"]
    assert result["choice_labels"] == {"deepinfra": "DeepInfra"}
    with patch("assistant_agent.management_choices._read_catalog", return_value={"data": [{"slug": "bad slug"}]}):
        with pytest.raises(ModelCatalogError, match="invalid"):
            setting_choices(AppConfig({}), backend(), "AGENT_LLM_ALLOWED_PROVIDERS")


def test_terminal_selection_lists_preserve_unavailable_values_and_reorder_fallbacks():
    field = SelectionField("openai:gpt-4.1,legacy:model", ["openai:gpt-4.1", "anthropic:claude"],
                           ordered=True)
    field._selected_index = 1
    field.move(-1)
    assert field.text == "legacy:model,openai:gpt-4.1"
    field.set_choices(["openai:gpt-4.1"])
    assert field.text == "legacy:model,openai:gpt-4.1"
    assert "saved; unavailable" in field.values[0][1]
    single = OptionField("old-model", [])
    single.set_choices(["new-model"])
    assert single.text == "old-model"
    single._selected_index = 1
    single._handle_enter()
    assert single.text == "new-model"
    single._selected_index = 0
    single._handle_enter()
    assert single.text == "new-model", "Unavailable saved values cannot become a new selection"


def test_terminal_provider_changes_invalidate_the_previous_model_list():
    model = OptionField("old/model", ["old/model", "other/old-model"])
    ui = SimpleNamespace(
        backend=SimpleNamespace(field_metadata={"AGENT_LLM_MODEL": {
            "choices_source": "models", "provider_key": "AGENT_MODEL_ROUTES_TASK_PROVIDER",
            "choices": ["old/model", "other/old-model"],
        }}), form={"AGENT_LLM_MODEL": model},
    )
    ManagementApp.clear_dependent_choices(ui, "AGENT_MODEL_ROUTES_TASK_PROVIDER")
    assert model.available_choices == []
    assert model.text == "old/model"
    assert "choices" not in ui.backend.field_metadata["AGENT_LLM_MODEL"]
