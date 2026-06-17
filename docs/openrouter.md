# OpenRouter Integration

This document explains how Assistant uses OpenRouter for LLM calls and hosted tool features.

## LLM Client

`assistant_agent/llm_client.py` provides `LlmClient`.

By default:

- `base_url`: `https://openrouter.ai/api/v1`
- endpoint: `POST /chat/completions`
- auth: `OPENROUTER_API_KEY`

If `agent.llm.base_url` points elsewhere, key fallback supports `OPENROUTER_API_KEY` or `OPENAI_API_KEY`.

## Request Shape

Each chat request includes:

- `model`
- `messages`
- `tools`
- `tool_choice: "auto"`
- `temperature`
- `max_tokens`

Headers include:

- `Authorization: Bearer ...`
- `HTTP-Referer` from `app_referer_url(config)`
- `X-Title` from `app_display_name(config)`

## Model Roles Across the System

Configured models are used in different loops:

- `agent.llm.model` — primary task-agent model.
- `agent.llm.fallback_model` — fallback model config value.
- `agent.limits.summarization_model` — prompt history compaction summarizer.
- `agent.memory.steward.model` — Memory Steward consolidation/recall model.
- `agent.search.model` — model used by `web_search` function tool.
- `agent.deep_research.search_model` — deep research search-focused model.
- `agent.supervisor.review_model` — supervisor review model setting.

## OpenRouter Hosted Tool Types

Assistant may attach OpenRouter server-side tools directly in tool lists:

- `openrouter:web_search`
- `openrouter:fusion`

These are produced by:

- `openrouter_web_search_tool(config, ...)`
- `openrouter_fusion_tool(config)`

### openrouter:web_search parameters

- `engine`
- `max_results`
- `max_total_results`
- optional `search_context_size`
- optional `allowed_domains`
- optional `excluded_domains`

### openrouter:fusion parameters

- optional `analysis_models`
- optional `model` (judge model)
- optional `max_tool_calls`
- optional `max_completion_tokens`
- optional `temperature`

## `web_search` Runtime Behavior

`ToolRuntime.web_search(...)` uses two modes:

1. **Perplexity-native mode** (if search model contains `perplexity/`): sends normal chat without attaching `openrouter:web_search` tool.
2. **OpenRouter tool mode**: attaches `openrouter:web_search` to obtain source-backed results.

Results are normalized into:

- answer text,
- annotations,
- extracted citations,
- usage metadata,
- provider indicator (`perplexity` or `openrouter`).

## Configuration Requirements

OpenRouter-dependent features generally require:

- valid API key,
- OpenRouter-compatible base URL,
- feature flags enabled (`agent.search.enabled`, `agent.fusion.enabled`, etc.).

The dynamic `available_function_names(...)` and `available_tools(...)` paths ensure tools are exposed only when prerequisites are met.
