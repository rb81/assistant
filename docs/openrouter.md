# OpenRouter Integration

Assistant uses OpenRouter for its default LLM endpoint and optional hosted
search, model-preset, and speech features. Native OpenAI, Claude, Gemini, and
Cloudflare Workers AI routes are covered in [Native Model Providers](model-providers.md).

## LLM Client

`src/agent/src/assistant_agent/llm_client.py` provides `LlmClient`. OpenRouter
uses `https://openrouter.ai/api/v1`, `POST /chat/completions`, and the protected
`AGENT_LLM_OPENROUTER_API_KEY`. Other provider routes use their native request
format or the configured OpenAI-compatible endpoint. Plain HTTP endpoints are
rejected.

## Request Shape

Chat requests contain `model`, `messages`, `temperature`, and `max_tokens`.
`tools` and `tool_choice: "auto"` are included only when tools are supplied.
Request-time context is refreshed before sending.

OpenRouter requests include `Authorization: Bearer ...`, `HTTP-Referer` from
`app_referer_url(config)`, and `X-Title` from `app_display_name(config)`.

## OpenRouter ZDR routing

Enable **Require Zero Data Retention routing** in Providers & Privacy
(`agent.llm.zdr_only` / `AGENT_LLM_ZDR_ONLY`, default `false`) to add
`"provider": {"zdr": true}` to requests sent to OpenRouter. The generalized
`AGENT_LLM_ZDR_ONLY` policy requires verified accounts and exact model allowlists
for direct native providers. OpenRouter uses its own ZDR routing and filtered
model catalog; no manual provider or model attestation is required for OpenRouter,
including fallbacks, model tools, speech and screening. Unavailable ZDR routes
fail rather than falling back to unrestricted routing. Stricter preset/speech
policies are not relaxed.

OpenRouter's ZDR routing can include **Safety Retention** endpoints if permitted
by the account's privacy settings and guardrails. These endpoints may retain
prompts and responses for abuse prevention. For strict ZDR, select **ZDR only**
for both Google and other models in OpenRouter's privacy settings or an
applicable guardrail. Assistant does not verify those settings. This caveat is
advisory: it surfaces warnings, not installation, startup, or usage errors.
OpenRouter's hosted tools and plugins,
including web search, have separate retention
policies and are not covered by its inference ZDR routing.
See [OpenRouter's ZDR documentation](https://openrouter.ai/docs/guides/features/zdr).

The macOS client's OpenRouter model selector maintains separate public full/ZDR catalogs,
filters by the field's capabilities, and supports weekly automatic refresh,
manual refresh, or automatic refresh disabled. These controls and caches live
only on the Mac; there are no new server model-list refresh settings. The public
catalog's `zdr=true` filter indicates eligible models, not account-specific
availability, and does not replace request-time routing enforcement. The
authenticated model-catalog endpoint covers configured native providers; see
[Native Model Providers](model-providers.md).

## Restricting providers

In **Models & access**, choose **Allowed OpenRouter providers**
(`AGENT_LLM_ALLOWED_PROVIDERS` / `agent.llm.allowed_providers`). An empty
selection means no preference. A nonempty selection sends `provider.only` on
OpenRouter requests, including fallback models, model tools, speech and decision
screening; it does not affect other API origins. It combines with global and
feature-specific ZDR, not as an alternative to ZDR. If a model has no eligible
endpoint, the request fails; neither restriction is silently removed.

The macOS selector retrieves provider tags from public model endpoint lists, or
from OpenRouter's ZDR endpoints list when global ZDR is on. Discovery does not
require an OpenRouter key or a configured OpenRouter connection. Full discovery
performs one request per catalog model and may take time. Lists are cached
separately on the Mac; a failed refresh retains the previous list and reports
the error. Saved providers missing from the current list are retained and
highlighted so they can be removed. Public metadata does not guarantee that a
specific model or account can use a provider; ZDR and account policies still
apply at request time.

## Model Roles Across the System

| Setting | Use |
|---|---|
| `agent.llm.model` | Primary task-agent model |
| `agent.llm.fallback_model` | One retry for HTTP 429, 503, or 529, if configured and different from the requested model |
| `agent.limits.summarization_model` | Prompt-history compaction |
| `agent.memory.steward.model` | Memory consolidation and recall |
| `agent.search.model` | `web_search` function tool |
| `agent.deep_research.search_model` | Deep-research search model |
| `agent.naming.model` | Short tasks including job/workstream names and email approval summaries; defaults to `openai/gpt-4.1-mini`, 15-second timeout, no fallback |

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

When the OpenRouter search backend is used, `ToolRuntime.web_search(...)`
attaches `openrouter:web_search` only to its own search-model request. Ordinary
task and deep-research model requests never include the hosted tool; they can
call the local `web_search` function instead. Keep `:online` and server-side
search presets out of model IDs if ordinary calls should not search.

The search model falls back to `agent.llm.model` when unset. OpenRouter results
include answer text, annotations, citations and usage metadata. For direct
Exa and provider failover, see [web access](web-access.md).

## Configuration Requirements

OpenRouter-backed features need a valid API key and their feature flags.
Ordinary hosted model tools such as fusion require the task model's
OpenRouter base URL. Explicit OpenRouter web search can use OpenRouter
even when the task model runs on another endpoint.

## Optional preset-based model tool

The model preset tool uses `model_service.py`, independently of the primary
`LlmClient`. It calls OpenRouter's fixed HTTPS endpoint with
`AGENT_LLM_OPENROUTER_API_KEY`; it does not refresh/inject assistant context or
substitute another model after an error.

Discovery queries `GET /models?output_modalities=all` and caches successful
catalogs for five minutes per policy. Generation uses `/chat/completions`;
audio output is assembled from SSE `delta.audio` chunks with provider usage.

Enable the feature in the **Model preset tool** settings and select all
models or **Zero Data Retention (ZDR) only**
(`agent.model_service.zdr_only` / `AGENT_MODEL_SERVICE_ZDR_ONLY`). ZDR-only
discovery uses OpenRouter's documented [`zdr=true` catalog filter](https://openrouter.ai/docs/api/api-reference/models/get-models);
presets are validated against that filtered catalog, including existing presets
after a policy change. Each generation request also sets
[`provider.zdr=true`](https://openrouter.ai/docs/guides/features/zdr). Empty or
failed discovery never falls back to all models. All-model mode does not
override stricter account or guardrail policies.

The preset-specific ZDR switch applies only to `llm_service`; the installation-wide
switch above also covers the primary assistant and other OpenRouter calls.
ZDR describes provider inference retention, not
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

Use **Text-to-speech** settings to enable speech independently of model presets.
`AGENT_TTS_ENABLED` / `agent.tts.enabled` defaults to `false`.
`AGENT_TTS_ZDR_ONLY` / `agent.tts.zdr_only` defaults to `false`; when enabled it
filters the model catalog and enforces `provider.zdr=true` for speech. It does
not inherit the model-preset ZDR setting, but installation-wide ZDR also applies.

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
