# Native Model Providers

The task-agent LLM client supports provider-native APIs as well as OpenRouter
and custom OpenAI-compatible endpoints. Select **Model provider** in
**Models & access**. `AGENT_LLM_PROVIDER=auto` preserves existing installations
and detects providers from their official API hosts. Explicit native providers
must use their official hosts; use `openai-compatible` for another endpoint.

| Provider | API base URL | Credential | Example model ID |
|---|---|---|---|
| OpenRouter | `https://openrouter.ai/api/v1` | `AGENT_LLM_OPENROUTER_API_KEY` | `anthropic/claude-sonnet-4.6` |
| OpenAI | `https://api.openai.com/v1` | `AGENT_LLM_OPENAI_API_KEY` | `gpt-4.1` |
| Anthropic Claude | `https://api.anthropic.com` | `AGENT_LLM_ANTHROPIC_API_KEY` | `claude-sonnet-4-6` |
| Gemini Developer API | `https://generativelanguage.googleapis.com/v1beta` | `AGENT_LLM_GEMINI_API_KEY` | `gemini-2.5-flash` |
| Cloudflare Workers AI | `https://api.cloudflare.com/client/v4/accounts/<account-id>/ai/v1` | `AGENT_LLM_CLOUDFLARE_API_KEY` | `@cf/meta/llama-3.1-8b-instruct` |
| OpenAI-compatible | Your HTTPS API base URL | `AGENT_LLM_OPENAI_API_KEY` | Provider-specific |

Use the model identifier supplied by the selected provider. The macOS client can
discover models for configured native providers through the authenticated
Management API at `GET /v2/configuration/models`; provider keys remain on the
server. Discovery is advisory and does not guarantee that the account can run a
model. Save provider and credential changes before requesting a fresh list.
OpenRouter's public catalog remains separate and does not require the server
credential.

## Strict Zero Data Retention

`AGENT_LLM_ZDR_ONLY=true` applies a fail-closed policy to task models and model
fallbacks. Set `AGENT_LLM_ZDR_VERIFIED_PROVIDERS` to the provider accounts whose
retention controls you verified, and
`AGENT_LLM_ZDR_VERIFIED_MODELS` to exact `provider:model` entries. These are
administrator attestations; Assistant cannot inspect provider account settings.
Unverified routes and models are blocked before a request is sent. The same
policy is checked on separate model paths, including OpenRouter model tools,
speech, and screening.

- **OpenRouter:** also set the account's ZDR privacy control or a guardrail to
  exclude Safety Retention endpoints. `provider.zdr=true` alone can permit
  those endpoints. Hosted tools have separate retention policies.
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
