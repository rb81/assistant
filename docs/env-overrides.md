# Environment Variable Overrides

This document lists every setting from `config/agent.yaml` that can be overridden via environment variables in `.env`.

## Why Use `.env` for Configuration?

**Git updates overwrite `config/agent.yaml` with the upstream version.** Using `.env` for customization means your settings survive updates without manual migration or merge conflicts. This approach:

- ✅ Keeps your customizations safe through updates
- ✅ Separates your configuration from tracked defaults
- ✅ Makes deployments portable and reproducible
- ✅ Prevents accidental loss of settings when pulling updates

## Override Precedence

1. **Environment variable** (from `.env`)
2. **`config/agent.yaml`** (tracked defaults)
3. **Code-level fallback defaults**

## How to Use

Add any of these variables to your `.env` file to override the corresponding YAML setting:

```env
# Example: Change the primary LLM model
AGENT_LLM_MODEL=anthropic/claude-opus-4

# Example: Increase iteration limit
AGENT_LIMITS_MAX_ITERATIONS_PER_TASK=100

# Example: Disable sandbox
AGENT_SANDBOX_ENABLED=false
```

---

## App Settings

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_APP_NAME` | `agent.app.name` | string | `assistant` |
| `AGENT_APP_BASE_URL` | `agent.app.base_url` | string | `http://localhost:8000` |
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
| `AGENT_LLM_PROVIDER` | `agent.llm.provider` | string | `openrouter` |
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
| `AGENT_MAX_DAILY_COST_USD` | `agent.limits.max_daily_cost_usd` | float | `10.00` |
| `AGENT_LIMITS_TOOL_TIMEOUT_DEFAULT_SECONDS` | `agent.limits.tool_timeout_default_seconds` | int | `120` |
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
| `AGENT_SUPERVISOR_REVIEW_MODEL` | `agent.supervisor.review_model` | string | `openai/gpt-4.1-mini` |
| `AGENT_SUPERVISOR_RECENT_LOG_LIMIT` | `agent.supervisor.recent_log_limit` | int | `5` |

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

## Chat (direct-chat fast path)

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_CHAT_MODEL` | `agent.chat.model` | string | _(empty, uses AGENT_LLM_MODEL)_ |
| `AGENT_CHAT_MAX_HISTORY_MESSAGES` | `agent.chat.max_history_messages` | int | `20` |
| `AGENT_CHAT_RATE_LIMIT_PER_MINUTE` | `agent.chat.rate_limit_per_minute` | int | `20` |

---

## Memory

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_MEMORY_RECENT_PROMPT_LIMIT` | `agent.memory.recent_prompt_limit` | int | `10` |
| `AGENT_MEMORY_STEWARD_ENABLED` | `agent.memory.steward.enabled` | bool | `true` |
| `AGENT_MEMORY_STEWARD_MODEL` | `agent.memory.steward.model` | string | `openai/gpt-4.1-mini` |
| `AGENT_MEMORY_STEWARD_MODE` | `agent.memory.steward.mode` | string | `best_effort` |
| `AGENT_MEMORY_STEWARD_MAX_ITERATIONS` | `agent.memory.steward.max_iterations` | int | `4` |
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
| `AGENT_REMINDERS_DEFAULT_TIMEZONE` | `agent.reminders.default_timezone` | string | _(empty, uses AGENT_TIMEZONE)_ |
| `AGENT_REMINDERS_SCHEDULER_POLL_INTERVAL_SECONDS` | `agent.reminders.scheduler_poll_interval_seconds` | int | `2` |
| `AGENT_REMINDERS_MAX_DUE_PER_TICK` | `agent.reminders.max_due_per_tick` | int | `25` |

---

## Calendar

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `CALENDAR_ENABLED` | `agent.calendar.enabled` | bool | `false` |
| `CALENDAR_TIMEZONE` | `agent.calendar.timezone` | string | _(empty, uses AGENT_TIMEZONE)_ |
| `AGENT_CALENDAR_DEFAULT_ALERT_MINUTES` | `agent.calendar.default_alert_minutes` | int | `15` |
| `AGENT_CALENDAR_SYNC_TIMEOUT_SECONDS` | `agent.calendar.sync.timeout_seconds` | int | `120` |
| `AGENT_CALENDAR_SYNC_BEFORE_READ` | `agent.calendar.sync.before_read` | bool | `false` |
| `AGENT_CALENDAR_SYNC_BEFORE_WRITE` | `agent.calendar.sync.before_write` | bool | `true` |
| `AGENT_CALENDAR_SYNC_AFTER_WRITE` | `agent.calendar.sync.after_write` | bool | `true` |
| `AGENT_CALENDAR_STORE_VDIR_PATH` | `agent.calendar.store.vdir_path` | string | `/data/private/calendar/vdir` |
| `AGENT_CALENDAR_STORE_DEFAULT_CALENDAR` | `agent.calendar.store.default_calendar` | string | `default` |
| `AGENT_CALENDAR_POLICY_ALLOW_READ_EVENT_DETAILS` | `agent.calendar.policy.allow_read_event_details` | bool | `false` |
| `AGENT_CALENDAR_LIMITS_MAX_OCCURRENCES_PER_EVENT` | `agent.calendar.limits.max_occurrences_per_event` | int | `500` |

---

## Projects

| Environment Variable | YAML Path | Type | Default |
|---|---|---|---|
| `AGENT_PROJECTS_ENABLED` | `agent.projects.enabled` | bool | `true` |
| `AGENT_PROJECTS_SCHEDULER_POLL_INTERVAL_SECONDS` | `agent.projects.scheduler_poll_interval_seconds` | int | `2` |
| `AGENT_PROJECTS_MAX_TASKS` | `agent.projects.max_tasks` | int | `25` |
| `AGENT_PROJECTS_MAX_PROJECTS_PER_TICK` | `agent.projects.max_projects_per_tick` | int | `25` |

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
| `AGENT_SANDBOX_BASE_URL` | `agent.sandbox.base_url` | string | `http://sandbox:8080` |
| `AGENT_SANDBOX_DEFAULT_TIMEOUT_SECONDS` | `agent.sandbox.default_timeout_seconds` | int | `300` |
| `AGENT_SANDBOX_HARD_KILL_GRACE_SECONDS` | `agent.sandbox.hard_kill_grace_seconds` | int | `30` |
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
| `AGENT_API_DASHBOARD_ENABLED` | `agent.api.dashboard_enabled` | bool | `true` |
| `AGENT_API_WORKSPACE_ENABLED` | `agent.api.workspace_enabled` | bool | `true` |
| `AGENT_API_ALLOW_PUBLIC_BIND` | `agent.api.allow_public_bind` | bool | `false` |
| `AGENT_API_MAX_UPLOAD_BYTES` | `agent.api.max_upload_bytes` | int | `524288000` |

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
- [`.env.example`](../.env.example) — template for your `.env` file
- [`config/agent.yaml`](../config/agent.yaml) — operational defaults (tracked file)
