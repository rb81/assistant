# OpenRouter Integration

Assistant uses OpenRouter for its default LLM endpoint and optional hosted
search, model-preset, and speech features.

## LLM Client

`src/agent/src/assistant_agent/llm_client.py` provides `LlmClient`. Its defaults
are `https://openrouter.ai/api/v1`, `POST /chat/completions`, and the protected
`AGENT_LLM_OPENROUTER_API_KEY`. For another HTTPS `agent.llm.base_url`,
`AGENT_LLM_OPENAI_API_KEY` takes precedence; the OpenRouter key is the fallback.
Plain HTTP endpoints are rejected.

## Request Shape

Chat requests contain `model`, `messages`, `temperature`, and `max_tokens`.
`tools` and `tool_choice: "auto"` are included only when tools are supplied.
Request-time context is refreshed before sending.

Requests include `Authorization: Bearer ...`, `HTTP-Referer` from
`app_referer_url(config)`, and `X-Title` from `app_display_name(config)`.

## Model Roles Across the System

| Setting | Use |
|---|---|
| `agent.llm.model` | Primary task-agent model |
| `agent.llm.fallback_model` | One retry for HTTP 429, 503, or 529, if configured and different from the requested model |
| `agent.limits.summarization_model` | Prompt-history compaction |
| `agent.memory.steward.model` | Memory consolidation and recall |
| `agent.search.model` | `web_search` function tool |
| `agent.deep_research.search_model` | Deep-research search model |
| `agent.naming.model` | Job/workstream names; defaults to `openai/gpt-4.1-mini`, 15-second timeout, no fallback |

Other HTTP errors and network failures do not trigger the task-model fallback.

## OpenRouter Hosted Tool Types

Assistant can add OpenRouter server-side tools directly to requests:
`openrouter:web_search` and `openrouter:fusion`, produced by
`openrouter_web_search_tool(config, ...)` and `openrouter_fusion_tool(config)`.

### openrouter:web_search parameters

`engine`, `max_results`, `max_total_results`, optional `search_context_size`,
`allowed_domains`, and `excluded_domains`.

### openrouter:fusion parameters

Optional `analysis_models`, judge `model`, `max_tool_calls`,
`max_completion_tokens`, and `temperature`.

## `web_search` Runtime Behavior

`ToolRuntime.web_search(...)` uses Perplexity-native mode when the search model
contains `perplexity/`; it sends a normal chat request without
`openrouter:web_search`. Otherwise it attaches the hosted tool for source-backed
results.

The search model falls back to `agent.llm.model` when unset. Perplexity-native
mode does not send hosted-tool engine, result-count, or domain-filter settings.
Results are normalized to answer text, annotations, citations, usage metadata,
and a provider indicator (`perplexity` or `openrouter`).

## Configuration Requirements

OpenRouter-backed features need a valid API key, an OpenRouter-compatible base
URL, and their feature flags (for example `agent.search.enabled` or
`agent.fusion.enabled`). Tools are exposed only when their prerequisites are
available.

## Optional preset-based model tool

The model preset tool uses `model_service.py`, independently of the primary
`LlmClient`. It calls OpenRouter's fixed HTTPS endpoint with
`AGENT_LLM_OPENROUTER_API_KEY`; it does not refresh/inject assistant context or
substitute another model after an error.

Discovery queries `GET /models?output_modalities=all` and caches successful
catalogs for five minutes per policy. Generation uses `/chat/completions`;
audio output is assembled from SSE `delta.audio` chunks with provider usage.

Enable the feature in **Advanced Settings → Model preset tool** and select all
models or **Zero Data Retention (ZDR) only**
(`agent.model_service.zdr_only` / `AGENT_MODEL_SERVICE_ZDR_ONLY`). ZDR-only
discovery uses OpenRouter's documented [`zdr=true` catalog filter](https://openrouter.ai/docs/api/api-reference/models/get-models);
presets are validated against that filtered catalog, including existing presets
after a policy change. Each generation request also sets
[`provider.zdr=true`](https://openrouter.ai/docs/guides/features/zdr). Empty or
failed discovery never falls back to all models. All-model mode does not
override stricter account or guardrail policies.

ZDR applies only to `llm_service`, not the primary assistant, research, web
search, or other integrations. It describes provider inference retention, not
local storage; generated files and accounting remain. Authentication is
internal—agents must not search for API keys or recreate authenticated model
calls in sandbox scripts.

The default budget is USD 5 per rolling 30 days. It uses provider-reported
`usage.cost`, not token-price estimates (some music charges are per song).
Preset and text-to-speech requests share this budget, but ordinary assistant,
web-search, and research calls do not. It is checked before each request, so the
last admitted request can exceed the threshold. Provider spending limits are
still recommended.

See [Model Preset Tool](tools-reference.md#model-preset-tool) for schemas, file
limits, supported modalities, and uncertain-request reconciliation.

## Dedicated text-to-speech

Enable **Advanced Settings → Text-to-speech** independently of model presets.
`AGENT_TTS_ENABLED` / `agent.tts.enabled` defaults to `false`.
`AGENT_TTS_ZDR_ONLY` / `agent.tts.zdr_only` defaults to `false`; when enabled it
filters the model catalog and enforces `provider.zdr=true` for speech. It does
not inherit the model-preset ZDR setting.

`AGENT_TTS_MODEL` and `AGENT_TTS_VOICE` select the dedicated speech model and
voice (defaults `mistralai/voxtral-mini-tts-3-0` and
`gb_jane_confident`). The `text_to_speech` tool calls `POST /audio/speech` and
saves audio in the workspace; authentication uses the internal OpenRouter
secret.

Speech shares the preset budget and ledger even when presets are disabled. Both
tools retry accounting for unresolved speech requests before admitting new
spending, without regenerating audio. Cost comes from
`GET /generation?id=...` using `X-Generation-Id`, not token-price estimates.
Unresolved charges block both tools.

See [Text-to-speech](tools-reference.md#text-to-speech-tool) for formats,
expression controls, and accounting recovery. Telegram delivery is a separate
`telegram_send` operation.
