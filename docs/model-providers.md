# Native Model Providers

The task-agent LLM client supports provider-native APIs as well as OpenRouter
and custom OpenAI-compatible endpoints. Configure credentials and provider
controls in **Providers & Privacy**, then choose a provider and its model for
each supported model role in **Models & Workflows**. `AGENT_LLM_PROVIDER=auto`
preserves existing installations and detects providers from their API hosts.
Explicit native providers must use their official hosts; use
`openai-compatible` for another endpoint.

| Provider | API base URL | Credential | Example model ID |
|---|---|---|---|
| OpenRouter | `https://openrouter.ai/api/v1` | `AGENT_LLM_OPENROUTER_API_KEY` | `anthropic/claude-sonnet-4.6` |
| OpenAI | `https://api.openai.com/v1` | `AGENT_LLM_OPENAI_API_KEY` | `gpt-4.1` |
| Anthropic Claude | `https://api.anthropic.com` | `AGENT_LLM_ANTHROPIC_API_KEY` | `claude-sonnet-4-6` |
| Gemini Developer API | `https://generativelanguage.googleapis.com/v1beta` | `AGENT_LLM_GEMINI_API_KEY` | `gemini-2.5-flash` |
| Cloudflare Workers AI | `https://api.cloudflare.com/client/v4/accounts/<account-id>/ai/v1` | `AGENT_LLM_CLOUDFLARE_API_KEY` | `@cf/meta/llama-3.1-8b-instruct` |
| OpenAI-compatible | Your HTTPS API base URL | `AGENT_PROVIDERS_OPENAI_COMPATIBLE_API_KEY` | Provider-specific |

**AI providers** contains a separate block for each provider, like Calendar's
provider blocks. Native endpoint URLs are prefilled; Cloudflare substitutes
your account ID into its prefilled Workers AI URL. Only custom OpenAI-compatible
services require you to supply an endpoint. Their dedicated credential is
separate from OpenAI's; existing installations keep using their old saved key
until a dedicated one is set. The legacy shared URL is an advanced migration
setting, not a required setup step for native providers.

Provider activation, selector ordering, hosting-provider restrictions and ZDR
verification use selection lists in both clients, not comma-separated text
editors. macOS lists are searchable; ordered selections have move controls.
In the terminal, use Space to select entries and Ctrl-Up / Ctrl-Down to reorder
priority and fallback lists. Use **Load choices** after selecting a route's
provider; **Keep draft** before moving between settings pages. Saved choices
that disappear from discovery stay visible until you remove or replace them.

Use the model identifier supplied by the selected provider. The macOS client can
discover models for configured providers through the authenticated Management
API at `GET /v2/configuration/models?provider=<provider>`; provider keys remain
on the server. Discovery is advisory and does not guarantee that the account
can run a model. Save provider and credential changes before requesting a
fresh list. OpenRouter's public catalog remains separate and does not require
the server credential.

Each task, naming, routing, summarization, memory, research, and development
route can select an enabled provider independently. Routes marked **inherit**
use the configured default. A route's ordered `provider:model` fallbacks are
tried only after transient rate-limit or overload responses; they do not
silently substitute a model when a selected provider cannot run it. Model
discovery in the settings UI is scoped to the provider selected for that role.
OpenRouter-backed web search, fusion, model tools, and speech retain their
provider-specific controls. Embeddings and transcription are separate local
services, and decision-model screening uses its own supported provider list.

## Strict Zero Data Retention

`AGENT_LLM_ZDR_ONLY=true` applies ZDR routing to every routed model
request and its fallback candidates. For direct providers, set
`AGENT_LLM_ZDR_VERIFIED_PROVIDERS` to the accounts whose retention controls you
verified, and `AGENT_LLM_ZDR_VERIFIED_MODELS` to exact `provider:model` entries.
These are administrator attestations; Assistant cannot inspect provider account settings.
Unverified direct-provider routes and models are blocked before a request is
sent. ZDR routing is also applied on separate model paths, including OpenRouter
model tools, speech, and screening. Provider-specific ZDR controls can only
make this policy stricter.

- **OpenRouter:** uses `provider.zdr=true` and OpenRouter's ZDR-filtered model
  catalog without manual verification allowlists. Safety Retention endpoints
  may still be permitted by account privacy settings or a guardrail. Review
  those controls to exclude Safety Retention if desired. This caveat is
  warning-only and does not block installation, startup, model discovery or
  usage. Hosted tools have separate retention policies.
- **OpenAI and Anthropic:** verify ZDR is enabled for the account and that each
  selected model is eligible. OpenAI requests set `store: false`; that request
  option does not establish account-level ZDR.
- **Gemini Developer API:** does not meet strict ZDR eligibility and is blocked
  when the policy is enabled.
- **Cloudflare Workers AI:** strict mode permits only verified `@cf/` models.
  Requests disable AI Gateway request logging, payload logging, and caching
  where supported. These headers do not disable Logpush; verify Gateway and
  account-level logging, caching, and Logpush settings separately.
- **Custom OpenAI-compatible endpoints:** are not verifiable by this policy
  and are blocked in strict mode.

The ZDR policy describes provider inference retention only. It does not prevent
local storage, provider billing records, or retention by tools and services
outside the model request.

See [OpenRouter](openrouter.md) for provider routing, endpoint catalogs, hosted
tools, and model-preset behavior.
