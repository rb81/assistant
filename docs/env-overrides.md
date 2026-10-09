# Credential Storage

Use `./scripts/setup.sh` and `./scripts/setup.sh secrets update` for credentials.
Known credential names support a matching `NAME_FILE` pointer to a Docker secret;
do not set a nonempty inline value and a file pointer together. Missing configured
files fail closed. `.env` should contain non-secret deployment overrides. Inline
credential examples in this reference describe the legacy compatibility path.
See [configuration](configuration.md) for the current setup and update workflow.

# Environment Variable Overrides

This document lists every setting from `config/agent.yaml` that can be overridden via environment variables.

## Expert overrides file (`overrides.env`)

This is an optional, expert-level mechanism. Most users never need it: the setup
wizard (`./scripts/setup.sh`, including `--section advanced`) manages the common
settings in `.env`. Use `overrides.env` for the rest of the list below.

- **Where:** `overrides.env` in the repository root, next to `.env`. Setup creates
  it on first save with every unmanaged setting as a commented line, but you may
  also create it yourself at any time. It is git-ignored and optional.
- **Format:** one `NAME=value` per line, names exactly as in the tables below.
  Booleans are `true`/`false`; lists are comma-separated; `#` starts a comment.
- **Precedence:** Compose loads it after `.env`, so it wins over `.env`,
  `config/agent.yaml` and code defaults. `environment:` entries in
  `docker-compose.yml` still win over everything.
- **Never rewritten:** setup never edits or regenerates the file, so re-running the
  wizard keeps your lines. If it already exists, setup also will not add the
  commented template.
- **Validated:** the setup review flags unknown or misspelled names, wrongly typed
  values, credentials (use `./scripts/setup.sh secrets update`), and names the
  wizard or `docker-compose.yml` already controls, which would conflict or be
  silently ignored. Remove those lines.
- **Not for Compose variables:** `TASK_AGENT_WORKERS`, `AGENT_API_PUBLISHED_PORT`,
  `SANDBOX_ISOLATION_MODE` and `COMPOSE_PROFILES` are read by Compose for
  interpolation from `.env` only.
- **Apply:** `docker compose up -d --force-recreate`.

```env
# overrides.env
AGENT_SANDBOX_ENABLED=false
AGENT_LLM_TEMPERATURE=0.3
```

## Why Use `overrides.env` Instead of Editing YAML?

**Git updates overwrite `config/agent.yaml` with the upstream version.** Keeping customizations in `overrides.env` means your settings survive updates without manual migration or merge conflicts. This approach:

- ✅ Keeps your customizations safe through updates
- ✅ Separates your configuration from tracked defaults
- ✅ Makes deployments portable and reproducible
- ✅ Prevents accidental loss of settings when pulling updates

## Override Precedence

### Retired No-Op Settings

The following overrides no longer have runtime consumers and are ignored. Remove
them from existing `.env` files when convenient; setup does not rewrite saved
configuration just to remove them:

- `AGENT_LLM_PROVIDER` (provider behavior is selected by the model API base URL)
- `AGENT_MAX_DAILY_COST_USD` (never enforced; use provider-side spending limits)
- `AGENT_LIMITS_TOOL_TIMEOUT_DEFAULT_SECONDS`
- `AGENT_SUPERVISOR_REVIEW_MODEL`
- `AGENT_SUPERVISOR_RECENT_LOG_LIMIT`
- `AGENT_MEMORY_RECENT_PROMPT_LIMIT` (use `AGENT_MEMORY_STEWARD_MAX_INJECTED_MEMORIES`)
- `AGENT_MEMORY_STEWARD_MAX_ITERATIONS`
- `AGENT_SANDBOX_DEFAULT_TIMEOUT_SECONDS` (use `AGENT_LIMITS_TOOL_TIMEOUT_COMMAND_SECONDS`)
- `AGENT_SANDBOX_HARD_KILL_GRACE_SECONDS`
- `AGENT_API_DASHBOARD_ENABLED`
- `AGENT_API_WORKSPACE_ENABLED`
- `AGENT_API_MAX_UPLOAD_BYTES`
- `AGENT_PROJECTS_ENABLED`, `AGENT_PROJECTS_SCHEDULER_POLL_INTERVAL_SECONDS`, `AGENT_PROJECTS_MAX_TASKS`, and `AGENT_PROJECTS_MAX_PROJECTS_PER_TICK` (Projects has been retired)

Workspace indexing is still supported: `AGENT_WORKSPACE_INDEX_ENABLED` controls
file indexing for keyword and semantic search, not the retired workspace UI.
Embedding settings configure vector generation separately.

### Active Setting Precedence

1. **`docker-compose.yml` `environment:` entries**
2. **`overrides.env`**
3. **`.env`**
4. **`config/agent.yaml`** (tracked defaults)
5. **Code-level fallback defaults**

## How to Use

Add any variable below that the wizard does not manage to `overrides.env` to override the corresponding YAML setting. Variables the wizard manages (identity, models, email, embeddings, integrations and the Advanced Settings groups) are listed here for completeness but must be changed with `./scripts/setup.sh`; the setup review rejects them in `overrides.env`.

```env
# Example: Lower the primary model's sampling temperature
AGENT_LLM_TEMPERATURE=0.3

# Example: Disable sandbox
AGENT_SANDBOX_ENABLED=false
```

---

## App Settings

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_APP_NAME` | `agent.app.name` | string | `assistant` |
| `AGENT_APP_BASE_URL` | `agent.app.base_url` | string | `https://localhost:8000` |
| `AGENT_APP_REFERER_URL` | `agent.app.referer_url` | string | `https://assistant.local` |
| `AGENT_TIMEZONE` | `agent.app.timezone` | string | `UTC` |

---

## Identity

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_NAME` | `agent.identity.name` | string | _(empty, falls back to capitalized app name)_ |
| `AGENT_EMAIL` | `agent.identity.email` | string | _(empty, falls back to SMTP_FROM → SMTP_USERNAME → "assistant@local")_ |

---

## Admin Contact

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `ADMIN_NAME` | `agent.admin.name` | string | _(empty)_ |
| `ADMIN_EMAIL` | `agent.admin.email` | string | _(empty)_ **⚠️ Required for core operation** |

---

## Dropbox

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `DROPBOX_ENABLED` | `agent.dropbox.enabled` | bool | `false` |
| `DROPBOX_ALLOW_EXPORTS` | `agent.dropbox.allow_exports` | bool | `false` |
| `DROPBOX_MAX_FILE_BYTES` | `agent.dropbox.max_file_bytes` | int | `26214400` |
| `DROPBOX_CLIENT_ID` | `agent.dropbox.client_id` | credential | unset |
| `DROPBOX_REFRESH_TOKEN` | `agent.dropbox.refresh_token` | credential | unset |

Credentials support matching `_FILE` pointers. Use guided setup, not inline
tokens in `.env`. See [Dropbox](dropbox.md) for App Folder registration and
standing output authorization. Scanning is mandatory regardless of optional
email scanning settings.

## Organization

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `ORG_NAME` | `agent.org.name` | string | _(empty)_ |
| `ORG_SECURITY_EMAIL` | `agent.org.security_email` | string | _(empty)_ |
| `ORG_INTERNAL_EMAIL_DOMAINS` | `agent.org.internal_email_domains` | list | `[]` |

---

## Database

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_DATABASE_HOST` | `agent.database.host` | string | `postgres` |
| `AGENT_DATABASE_PORT` | `agent.database.port` | int | `5432` |
| `AGENT_DATABASE_NAME` | `agent.database.name` | string | `assistant` |
| `AGENT_DATABASE_USER` | `agent.database.user` | string | `assistant` |

---

## LLM

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_LLM_MODEL` | `agent.llm.model` | string | `anthropic/claude-sonnet-4.6` |
| `AGENT_LLM_FALLBACK_MODEL` | `agent.llm.fallback_model` | string | `openai/gpt-5.4` |
| `AGENT_LLM_BASE_URL` | `agent.llm.base_url` | string | `https://openrouter.ai/api/v1` |
| `AGENT_LLM_TEMPERATURE` | `agent.llm.temperature` | float | `0.2` |
| `AGENT_LLM_MAX_TOKENS_PER_CALL` | `agent.llm.max_tokens_per_call` | int | `4096` |

---

## Limits

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_LIMITS_MAX_ITERATIONS_PER_TASK` | `agent.limits.max_iterations_per_task` | int | `50` |
| `AGENT_LIMITS_MAX_TOKENS_PER_TASK` | `agent.limits.max_tokens_per_task` | int | `1000000` |
| `AGENT_LIMITS_MESSAGE_HISTORY_WINDOW` | `agent.limits.message_history_window` | int | `8` |
| `AGENT_LIMITS_MAX_PROMPT_CHARS` | `agent.limits.max_prompt_chars` | int | `400000` |
| `AGENT_LIMITS_SUMMARIZATION_MODEL` | `agent.limits.summarization_model` | string | `openai/gpt-4.1-mini` |
| `AGENT_LIMITS_SUMMARIZATION_MAX_TOKENS` | `agent.limits.summarization_max_tokens` | int | `2000` |
| `AGENT_LIMITS_SUMMARIZATION_TIMEOUT_SECONDS` | `agent.limits.summarization_timeout_seconds` | int | `30` |
| `AGENT_LIMITS_SUMMARIZATION_KEEP_RECENT` | `agent.limits.summarization_keep_recent` | int | `6` |
| `AGENT_LIMITS_SUMMARIZATION_MAX_INPUT_CHARS` | `agent.limits.summarization_max_input_chars` | int | `80000` |
| `AGENT_LIMITS_TOOL_TIMEOUT_COMMAND_SECONDS` | `agent.limits.tool_timeout_command_seconds` | int | `300` |
| `AGENT_MAX_EMAILS_PER_HOUR` | `agent.limits.max_emails_per_hour` | int | `10` |
| `AGENT_LIMITS_POLL_INTERVAL_SECONDS` | `agent.limits.poll_interval_seconds` | int | `60` |

---

## Task Agent

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_TASK_AGENT_POLL_INTERVAL_SECONDS` | `agent.task_agent.poll_interval_seconds` | int | `2` |

---

## Email

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `IMAP_HOST` | `agent.email.imap_host` | string | _(empty)_ |
| `IMAP_PORT` | `agent.email.imap_port` | int | `993` |
| `IMAP_USERNAME` | `agent.email.imap_username` | string | _(empty)_ |
| `IMAP_PASSWORD` | `agent.email.imap_password` | string | _(empty)_ |
| `IMAP_FOLDER` | `agent.email.imap_folder` | string | `INBOX` |
| `IMAP_ARCHIVE_FOLDER` | `agent.email.imap_archive_folder` | string | `Archive` |
| `IMAP_SENT_FOLDER` | `agent.email.imap_sent_folder` | string | `Sent` |
| `AGENT_IMAP_POLL_INTERVAL` | `agent.email.imap_poll_interval_seconds` | int | `60` |
| `SMTP_HOST` | `agent.email.smtp_host` | string | _(empty)_ |
| `SMTP_PORT` | `agent.email.smtp_port` | int | `587` |
| `SMTP_USERNAME` | `agent.email.smtp_username` | string | _(empty)_ |
| `SMTP_PASSWORD` | `agent.email.smtp_password` | string | _(empty)_ |
| `SMTP_FROM` | `agent.email.smtp_from` | string | _(empty)_ |
| `AGENT_EMAIL_SAVE_TO_SENT` | `agent.email.save_to_sent` | bool | `true` |
| `AGENT_EMAIL_MAX_ATTACHMENT_COUNT` | `agent.email.max_attachment_count` | int | `5` |
| `AGENT_EMAIL_MAX_ATTACHMENT_BYTES` | `agent.email.max_attachment_bytes` | int | `10485760` |
| `AGENT_EMAIL_MAX_TOTAL_ATTACHMENT_BYTES` | `agent.email.max_total_attachment_bytes` | int | `20971520` |
| `EMAIL_ALLOWED_RECIPIENT_DOMAINS` | `agent.email.allowed_recipient_domains` | list | `[]` |
| `AGENT_EMAIL_CONTEXT_BODY_PREVIEW_CHARS` | `agent.email.context_body_preview_chars` | int | `600` |
| `AGENT_EMAIL_INITIAL_CONTEXT_PRIOR_FULL_BODY_CHAR_LIMIT` | `agent.email.initial_context_prior_full_body_char_limit` | int | `1200` |
| `AGENT_EMAIL_MAX_INITIAL_CONTEXT_BODY_CHARS` | `agent.email.max_initial_context_body_chars` | int | `20000` |
| `EMAIL_ACTIONABLE_SENDERS` | `agent.email.actionable_senders` | list | `[]` |
| `AGENT_EMAIL_SUBJECT_THREADING_FALLBACK` | `agent.email.subject_threading_fallback` | bool | `false` |

---

## Supervisor

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_SUPERVISOR_STALL_THRESHOLD_MINUTES` | `agent.supervisor.stall_threshold_minutes` | int | `15` |
| `AGENT_SUPERVISOR_MAX_TASK_DURATION_MINUTES` | `agent.supervisor.max_task_duration_minutes` | int | `60` |

---

## Filesystem

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_FILESYSTEM_SHARED_ROOT` | `agent.filesystem.shared_root` | string | `/data/share` |
| `AGENT_FILESYSTEM_REQUIRE_MOUNT` | `agent.filesystem.require_mount` | bool | `true` |
| `AGENT_FILESYSTEM_SHARED_FILE_UMASK` | `agent.filesystem.shared_file_umask` | string | `0002` |
| `AGENT_FILESYSTEM_MAX_READ_BYTES` | `agent.filesystem.max_read_bytes` | int | `102400` |
| `AGENT_FILESYSTEM_TRASH_DIRECTORY` | `agent.filesystem.trash_directory` | string | `/data/share/.trash` |

---

## Tool Result Cache

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_TOOL_RESULT_CACHE_ENABLED` | `agent.tool_result_cache.enabled` | bool | `true` |
| `AGENT_TOOL_RESULT_CACHE_ROOT` | `agent.tool_result_cache.root` | string | `.assistant/cache/tool-results` |
| `AGENT_TOOL_RESULT_CACHE_MIN_BYTES` | `agent.tool_result_cache.min_bytes` | int | `4096` |
| `AGENT_TOOL_RESULT_CACHE_RETENTION_DAYS` | `agent.tool_result_cache.retention_days` | int | `7` |

---

## Artifacts

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_ARTIFACTS_ENABLED` | `agent.artifacts.enabled` | bool | `true` |
| `AGENT_ARTIFACTS_RAW_ROOT` | `agent.artifacts.raw_root` | string | `/data/private/artifacts` |
| `AGENT_ARTIFACTS_PROCESSED_ROOT` | `agent.artifacts.processed_root` | string | `processed` |
| `AGENT_ARTIFACTS_MAX_ATTACHMENT_BYTES` | `agent.artifacts.max_attachment_bytes` | int | `26214400` |
| `AGENT_ARTIFACTS_CLAMAV_ENABLED` | `agent.artifacts.clamav.enabled` | bool | `true` |
| `AGENT_ARTIFACTS_CLAMAV_REQUIRED` | `agent.artifacts.clamav.required` | bool | `true` |
| `AGENT_ARTIFACTS_CLAMAV_HOST` | `agent.artifacts.clamav.host` | string | `clamav` |
| `AGENT_ARTIFACTS_CLAMAV_PORT` | `agent.artifacts.clamav.port` | int | `3310` |
| `AGENT_ARTIFACTS_CLAMAV_TIMEOUT_SECONDS` | `agent.artifacts.clamav.timeout_seconds` | int | `30` |

---

## Prompt

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_PROMPT_AGENT_FILE` | `agent.prompt.agent_file` | string | `AGENT.md` |
| `AGENT_PROMPT_MAX_CONTEXT_FILE_BYTES` | `agent.prompt.max_context_file_bytes` | int | `65536` |

---

## Memory

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_MEMORY_STEWARD_ENABLED` | `agent.memory.steward.enabled` | bool | `true` |
| `AGENT_MEMORY_STEWARD_MODEL` | `agent.memory.steward.model` | string | `openai/gpt-4.1-mini` |
| `AGENT_MEMORY_STEWARD_MODE` | `agent.memory.steward.mode` | string | `best_effort` |
| `AGENT_MEMORY_STEWARD_TIMEOUT_SECONDS` | `agent.memory.steward.timeout_seconds` | int | `45` |
| `AGENT_MEMORY_STEWARD_MAX_TOKENS_PER_CALL` | `agent.memory.steward.max_tokens_per_call` | int | `1600` |
| `AGENT_MEMORY_STEWARD_MAX_INJECTED_MEMORIES` | `agent.memory.steward.max_injected_memories` | int | `8` |
| `AGENT_MEMORY_STEWARD_MAX_WRITES_PER_JOB` | `agent.memory.steward.max_writes_per_job` | int | `5` |
| `AGENT_MEMORY_STEWARD_MAX_TRANSCRIPT_BYTES` | `agent.memory.steward.max_transcript_bytes` | int | `30000` |
| `AGENT_MEMORY_STEWARD_MIN_IMPORTANCE` | `agent.memory.steward.min_importance` | int | `4` |
| `AGENT_MEMORY_STEWARD_MIN_CONFIDENCE` | `agent.memory.steward.min_confidence` | float | `0.55` |

---

## Context

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_CONTEXT_SEARCH_DAYS` | `agent.context.search_days` | int | `30` |

---

## Embeddings

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_EMBEDDINGS_ENABLED` | `agent.embeddings.enabled` | bool | `true` |
| `AGENT_EMBEDDINGS_BASE_URL` | `agent.embeddings.base_url` | string | `http://ollama:11434` |
| `AGENT_EMBEDDINGS_MODEL` | `agent.embeddings.model` | string | `embeddinggemma` |
| `AGENT_EMBEDDINGS_DIMENSIONS` | `agent.embeddings.dimensions` | int | _(empty, uses model default)_ |
| `AGENT_EMBEDDINGS_TIMEOUT_SECONDS` | `agent.embeddings.timeout_seconds` | int | `20` |

---

## Workspace

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_WORKSPACE_MAX_CONVERSION_BYTES` | `agent.workspace.max_conversion_bytes` | int | `26214400` |
| `AGENT_WORKSPACE_INDEX_ENABLED` | `agent.workspace.index.enabled` | bool | `true` |
| `AGENT_WORKSPACE_INDEX_POLL_INTERVAL_SECONDS` | `agent.workspace.index.poll_interval_seconds` | int | `60` |
| `AGENT_WORKSPACE_INDEX_CHUNK_CHARS` | `agent.workspace.index.chunk_chars` | int | `3500` |
| `AGENT_WORKSPACE_INDEX_CANDIDATE_LIMIT` | `agent.workspace.index.candidate_limit` | int | `3000` |

---

## Conversion

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_CONVERSION_PANDOC_PATH` | `agent.conversion.pandoc_path` | string | `pandoc` |
| `AGENT_CONVERSION_PDF_ENGINE` | `agent.conversion.pdf_engine` | string | `weasyprint` |
| `AGENT_CONVERSION_TIMEOUT_SECONDS` | `agent.conversion.timeout_seconds` | int | `120` |
| `AGENT_CONVERSION_MAX_INPUT_BYTES` | `agent.conversion.max_input_bytes` | int | `26214400` |

---

## Reminders

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_REMINDERS_DEFAULT_TIMEZONE` | `agent.reminders.default_timezone` | string | _(deprecated, ignored; use AGENT_TIMEZONE)_ |
| `AGENT_REMINDERS_SCHEDULER_POLL_INTERVAL_SECONDS` | `agent.reminders.scheduler_poll_interval_seconds` | int | `2` |
| `AGENT_REMINDERS_MAX_DUE_PER_TICK` | `agent.reminders.max_due_per_tick` | int | `25` |

---

## Calendar

Calendar tools are activated by setting `CALENDAR_ENABLED=true` and providing OAuth credentials for at least one provider. The agent calls Google, Outlook, or Zoho APIs directly using refresh tokens.

For full OAuth setup instructions see [docs/calendar-providers.md](calendar-providers.md).

### Core settings

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `CALENDAR_ENABLED` | `agent.calendar.enabled` | bool | `false` |
| `CALENDAR_TIMEZONE` | `agent.calendar.timezone` | string | _(deprecated, ignored; use AGENT_TIMEZONE)_ |
| `CALENDAR_DEFAULT_PROVIDER` | `agent.calendar.default_provider` | string | _(first configured provider)_ |
| `CALENDAR_DEFAULT_ALERT_MINUTES` | `agent.calendar.default_alert_minutes` | int | `30` |
| `AGENT_CALENDAR_POLICY_ALLOW_READ_EVENT_DETAILS` | `agent.calendar.policy.allow_read_event_details` | bool | `false` |

### Google Calendar provider

| Environment Variable | YAML Path | Description |
|---|---|---|
| `CALENDAR_GOOGLE_CLIENT_ID` | _(env only)_ | OAuth 2.0 client ID |
| `CALENDAR_GOOGLE_CLIENT_SECRET` | _(env only)_ | OAuth 2.0 client secret |
| `CALENDAR_GOOGLE_REFRESH_TOKEN` | _(env only)_ | Long-lived refresh token |
| `CALENDAR_GOOGLE_CALENDAR_ID` | `agent.calendar.google.calendar_id` | Calendar to use (default: `primary`) |

For a second Google account add `CALENDAR_GOOGLE_2_CLIENT_ID` / `CALENDAR_GOOGLE_2_CLIENT_SECRET` / `CALENDAR_GOOGLE_2_REFRESH_TOKEN` (and optionally `CALENDAR_GOOGLE_2_CALENDAR_ID`). Repeat with suffix `_3`, `_4`, etc.

### Microsoft Outlook / Microsoft 365 provider

| Environment Variable | YAML Path | Description |
|---|---|---|
| `CALENDAR_OUTLOOK_CLIENT_ID` | _(env only)_ | Azure app (client) ID |
| `CALENDAR_OUTLOOK_CLIENT_SECRET` | _(env only)_ | Azure client secret |
| `CALENDAR_OUTLOOK_REFRESH_TOKEN` | _(env only)_ | Long-lived refresh token |
| `CALENDAR_OUTLOOK_TENANT_ID` | `agent.calendar.outlook.tenant_id` | Azure tenant ID (default: `common`) |
| `CALENDAR_OUTLOOK_CALENDAR_ID` | `agent.calendar.outlook.calendar_id` | Calendar to use (default: `default`) |

### Zoho Calendar provider

| Environment Variable | YAML Path | Description |
|---|---|---|
| `CALENDAR_ZOHO_CLIENT_ID` | _(env only)_ | Zoho OAuth client ID |
| `CALENDAR_ZOHO_CLIENT_SECRET` | _(env only)_ | Zoho OAuth client secret |
| `CALENDAR_ZOHO_REFRESH_TOKEN` | _(env only)_ | Long-lived refresh token |
| `CALENDAR_ZOHO_API_BASE` | `agent.calendar.zoho.api_base` | API base URL for regional data centers (default: `https://calendar.zoho.com/api/v1`) |
| `CALENDAR_ZOHO_CALENDAR_ID` | `agent.calendar.zoho.calendar_id` | Calendar UID to use (default: `default`) |

---

## Deep Research

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_DEEP_RESEARCH_ENABLED` | `agent.deep_research.enabled` | bool | `true` |
| `AGENT_DEEP_RESEARCH_MODEL` | `agent.deep_research.model` | string | _(empty, uses AGENT_LLM_MODEL)_ |
| `AGENT_DEEP_RESEARCH_SEARCH_MODEL` | `agent.deep_research.search_model` | string | `perplexity/sonar-pro` |
| `AGENT_DEEP_RESEARCH_MAX_TOOL_CALLS` | `agent.deep_research.max_tool_calls` | int | `40` |
| `AGENT_DEEP_RESEARCH_MAX_ITERATIONS` | `agent.deep_research.max_iterations` | int | `30` |
| `AGENT_DEEP_RESEARCH_POLL_INTERVAL_SECONDS` | `agent.deep_research.poll_interval_seconds` | int | `2` |
| `AGENT_DEEP_RESEARCH_TIMEOUT_SECONDS` | `agent.deep_research.timeout_seconds` | int | `180` |

---

## Heartbeat

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_HEARTBEAT_ENABLED` | `agent.heartbeat.enabled` | bool | `true` |
| `AGENT_HEARTBEAT_POLL_INTERVAL_SECONDS` | `agent.heartbeat.poll_interval_seconds` | int | `300` |
| `AGENT_HEARTBEAT_STALE_THRESHOLD_MINUTES` | `agent.heartbeat.stale_threshold_minutes` | int | `30` |
| `AGENT_HEARTBEAT_DEEP_RESEARCH_STALE_MINUTES` | `agent.heartbeat.deep_research_stale_minutes` | int | `60` |
| `AGENT_HEARTBEAT_PROJECT_STALE_MINUTES` | `agent.heartbeat.project_stale_minutes` | int | `60` |
| `AGENT_HEARTBEAT_ADMIN_DIGEST_INTERVAL_HOURS` | `agent.heartbeat.admin_digest_interval_hours` | int | `0` |

---

## Sandbox

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_SANDBOX_ENABLED` | `agent.sandbox.enabled` | bool | `true` |
| `AGENT_SANDBOX_BASE_URL` | `agent.sandbox.base_url` | string | `https://sandbox:8080` |
| `AGENT_SANDBOX_MAX_ATTEMPTS` | `agent.sandbox.max_attempts` | int | `3` |
| `AGENT_SANDBOX_RETRY_BACKOFF_SECONDS` | `agent.sandbox.retry_backoff_seconds` | int | `1` |

---

## Search

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_SEARCH_ENABLED` | `agent.search.enabled` | bool | `true` |
| `AGENT_SEARCH_MODEL` | `agent.search.model` | string | `openai/gpt-4.1-mini` |
| `AGENT_SEARCH_ENGINE` | `agent.search.engine` | string | `auto` |
| `AGENT_SEARCH_MAX_RESULTS` | `agent.search.max_results` | int | `5` |
| `AGENT_SEARCH_MAX_TOTAL_RESULTS` | `agent.search.max_total_results` | int | `15` |
| `AGENT_SEARCH_CONTEXT_SIZE` | `agent.search.search_context_size` | string | `medium` |
| `AGENT_SEARCH_ALLOWED_DOMAINS` | `agent.search.allowed_domains` | list | `[]` |
| `AGENT_SEARCH_EXCLUDED_DOMAINS` | `agent.search.excluded_domains` | list | `[]` |

---

## Fusion

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_FUSION_ENABLED` | `agent.fusion.enabled` | bool | `true` |
| `AGENT_FUSION_ANALYSIS_MODELS` | `agent.fusion.analysis_models` | list | `[]` |
| `AGENT_FUSION_MODEL` | `agent.fusion.model` | string | _(empty, uses AGENT_LLM_MODEL)_ |
| `AGENT_FUSION_MAX_TOOL_CALLS` | `agent.fusion.max_tool_calls` | int | `8` |
| `AGENT_FUSION_MAX_COMPLETION_TOKENS` | `agent.fusion.max_completion_tokens` | int | _(empty, no limit)_ |
| `AGENT_FUSION_TEMPERATURE` | `agent.fusion.temperature` | float | _(empty, uses AGENT_LLM_TEMPERATURE)_ |

---

## API

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_API_BIND_HOST` | `agent.api.bind_host` | string | `127.0.0.1` |
| `AGENT_API_PORT` | `agent.api.port` | int | `8000` |
| `AGENT_API_DOCS_ENABLED` | `agent.api.docs_enabled` | bool | `false` |
| `AGENT_API_OPENAPI_ENABLED` | `agent.api.openapi_enabled` | bool | `false` |
| `AGENT_API_ALLOW_PUBLIC_BIND` | `agent.api.allow_public_bind` | bool | `false` |

---

## Notes on List Types

List-type environment variables accept comma-separated values:

```env
ORG_INTERNAL_EMAIL_DOMAINS=example.com,subsidiary.com,trusted-partner.org
EMAIL_ALLOWED_RECIPIENT_DOMAINS=example.com,contractor.com
EMAIL_ACTIONABLE_SENDERS=boss@example.com,team-lead@example.com
```

---

## Related Documentation

- [Configuration Reference](configuration.md) — overview of the configuration system
- [Advanced Configuration](advanced-configuration.md) — common customization scenarios
- [`.env.example`](../.env.example) — reference for the keys the setup wizard writes to `.env`
- [`config/agent.yaml`](../config/agent.yaml) — operational defaults (tracked file)
