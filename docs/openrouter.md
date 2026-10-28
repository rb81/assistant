# OpenRouter Integration

This document explains how Assistant uses OpenRouter for LLM calls and hosted tool features.

## LLM Client

`src/agent/src/assistant_agent/llm_client.py` provides `LlmClient`.

By default:

- `base_url`: `https://openrouter.ai/api/v1`
- endpoint: `POST /chat/completions`
- auth: `OPENROUTER_API_KEY`

For another HTTPS `agent.llm.base_url`, `OPENAI_API_KEY` takes precedence,
with `OPENROUTER_API_KEY` as fallback. Configure keys through setup's protected
secret store; `_FILE` references are supported. Plain HTTP endpoints are rejected.

## Request Shape

Each chat request includes:

- `model`
- `messages`
- `temperature`
- `max_tokens`

`tools` and `tool_choice: "auto"` are included only when tools are supplied.
Request-time context is refreshed before sending.

Headers include:

- `Authorization: Bearer ...`
- `HTTP-Referer` from `app_referer_url(config)`
- `X-Title` from `app_display_name(config)`

## Model Roles Across the System

Configured models are used in different loops:

- `agent.llm.model` — primary task-agent model.
- `agent.llm.fallback_model` — one retry after HTTP 429, 503 or 529, if
  configured and different from the requested model. Other HTTP errors and
  network failures do not trigger fallback.
- `agent.limits.summarization_model` — prompt history compaction summarizer.
- `agent.memory.steward.model` — Memory Steward consolidation/recall model.
- `agent.search.model` — model used by `web_search` function tool.
- `agent.deep_research.search_model` — deep research search-focused model.
- `agent.naming.model` — inexpensive job/workstream display names; defaults to
  `openai/gpt-4.1-mini`, with a fifteen-second request timeout and no fallback.

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

The search model falls back to `agent.llm.model` when unset. In Perplexity-native
mode, the hosted tool's engine, result-count and domain-filter parameters are
not sent to the provider.

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

## Optional preset-based model tool

`llm_service` uses `model_service.py`, independently of the primary `LlmClient`.
It calls the fixed OpenRouter HTTPS base URL with `OPENROUTER_API_KEY`, does not
refresh/inject assistant context, and never substitutes another model on error.
Model discovery requests `GET /models?output_modalities=all` and caches successful
catalog results for five minutes, separately for each catalog policy. Requests use `/chat/completions`; audio output
is assembled from SSE `delta.audio` chunks with final usage accounting.

Enable and budget it through setup's **Advanced Settings → Model preset tool**.
Choose all models (the default) or enable **Zero Data Retention (ZDR) only**
(`agent.model_service.zdr_only`, `AGENT_MODEL_SERVICE_ZDR_ONLY`). ZDR-only discovery
uses OpenRouter's documented [`zdr=true` model catalog filter](https://openrouter.ai/docs/api/api-reference/models/get-models),
preserving modality and voice metadata. Preset creation and use validate against
that filtered catalog, including existing presets after a policy change. Every
generation request also sets [`provider.zdr=true`](https://openrouter.ai/docs/guides/features/zdr)
to prevent routing to non-ZDR endpoints. Empty results or discovery failures
never fall back to all models. All-model mode does not override stricter
OpenRouter account or guardrail policies.

This setting only governs `llm_service`, not the primary assistant, research,
web search or other integrations. ZDR describes provider inference retention,
not local workspace storage: generated files and local accounting still persist.
The tool manages authentication internally; agents must not search for API keys
or recreate authenticated model calls in sandbox scripts.

Defaults: disabled, USD 5 per rolling 30 days. Provider-reported `usage.cost` is
the source of truth, not token-price estimates (music can have per-song charges
despite zero token pricing). This limit is shared with `text_to_speech`, but
excludes ordinary assistant calls, web search, and research. The final admitted request can exceed
the threshold; provider-side spending limits remain recommended.

See [Model Preset Tool](tools-reference.md#model-preset-tool) for schemas, file
limits, supported modalities and uncertain-request reconciliation.

## Dedicated text-to-speech

Enable **Advanced Settings → Text-to-speech** independently of model presets.
`AGENT_TTS_ENABLED` / `agent.tts.enabled` defaults to false;
`AGENT_TTS_ZDR_ONLY` / `agent.tts.zdr_only` defaults to false (all models).
The latter selects the filtered catalog and enforces `provider.zdr=true` on
every speech request; it does not inherit the model-preset ZDR flag.

The `text_to_speech` tool selects dedicated `speech` models, calls
`POST /audio/speech`, and saves binary audio in the workspace. Authentication
uses the internal OpenRouter secret. It shares the model-preset budget settings
and ledger even when presets are disabled. Both model tools retry eligible
unresolved speech accounting before admitting new spending, including charges
from previous jobs, without regenerating audio. Actual costs come from
`GET /generation?id=...`, using the response's `X-Generation-Id`, not estimates
from token prices. Unresolved charges block both tools.

See [Text-to-speech](tools-reference.md#text-to-speech-tool) for formats,
expression controls and safe accounting recovery. Telegram delivery is a
separate `telegram_send` operation, not a feature of this tool.
