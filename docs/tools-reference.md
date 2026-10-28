# Tools Reference

Assistant tool schemas and runtime dispatch are assembled in
`src/agent/src/assistant_agent/tools.py`; some integration schemas live in their
own modules. Ordinary tools execute through `ToolRuntime.run(name, arguments)`;
terminal/meta tools are handled by `TaskAgent` in `task_agent.py`.

## Tool Exposure Model

- **Core tools**: always present at loop start (`task_complete`, `task_failed`, `request_input`, plus `email_send` when SMTP available).
- **Meta tool**: `get_tool_specs` for lazy loading.
- **Loadable tools**: other function tools (files, reminders, research, etc.).
  Call `get_tool_specs` with `{"tools":["file_list","file_read"]}` to enable a
  batch. It returns loaded/invalid names and guardrails; schemas are attached to
  subsequent model calls.

Availability is computed by `available_function_names(...)` from runtime config and job context.

## Workstream relationships

`workstream_list(before_id, limit)` lists workstream titles/IDs with bounded
results. Pass `next_before_id` back as `before_id` for older entries; there is
no query filter.
`workstream_read(workstream_id, thread_id, before, limit, ...)` reads paginated
main-thread history by default. Use `next_before` for the next page. Optional
flags expose system threads, owned/adopted objects, object-change events and
execution details. Specify a research system thread with
`include_execution_details=true` to inspect its research events and results.

`workstream_attach(kind, key)` explicitly adopts an existing live contact or
file into the current workstream without copying it. Use its native record
ID/key, or a workspace path for `file`. Other object categories cannot be newly
adopted. Retaining links preserve ownership when an object is intentionally shared.
Searching, reading and editing do not adopt objects or switch the active
conversation. Updates require current object versions where exposed and retain
the original ownership; successful changes notify affected workstream context,
not external recipients.
Deletion belongs to the confirmed administrator terminal workflow, not an LLM
tool. See [workstream deletion](security.md#workstream-deletion).

Updates/deletions of versioned memories, contacts, reminders, calendar events
and Zoom meetings require `expected_version` from the latest object read.
A stale version is rejected; re-read before deciding whether to retry.

## File Tools

- `file_list`
- `file_read`
- `file_write`
- `file_append`
- `file_move`
- `file_copy`
- `file_convert`
- `file_delete` (soft delete to trash)
- `file_search`
- `file_semantic_search`

Guardrails:

- paths must resolve under shared root,
- `.assistant` is protected from file-tool writes/deletes; only its `docs`
  subtree is a read-only mount in command containers,
- cache/source-archive visibility is restricted,
- file mutations attempt to update the workspace index; indexing failures are
  logged and do not necessarily fail the file operation.

## Dropbox Tools

- `dropbox_list`: paginated Inbox/Outbox file metadata, including nested files.
- `dropbox_import`: one revision-pinned Inbox document; mandatory clean scans
	and successful conversion before workspace publication/indexing.
- `dropbox_export`: one scanned workspace snapshot to `Outbox/job-<id>`, only
	with saved standing delivery permission; no overwrite or internal-file export.
- `dropbox_delete`: delete one listed Inbox/Outbox file by path and current
	revision, only when explicitly requested by the requester.

Tools are loadable only when Dropbox is configured and the workspace is available.
Export is additionally hidden unless `DROPBOX_ALLOW_EXPORTS=true`. Retry an
uncertain export using the same source and filename. Dropbox content is untrusted
data. See [connection, limits and privacy](dropbox.md).

## Email Tools

- `email_search`
- `email_read` (character or line paging)
- `email_send`

`email_send` behavior:

- recipient domain allowlist enforcement,
- per-hour send rate limiting,
- optional threading (`in_reply_to`, `References`),
- markdown-to-HTML multipart generation,
- attachment handling from file path or inline content,
- optional append to IMAP Sent folder when configured,
- disclosure footer handling for external domains.

## Telegram Tool

- `telegram_send` — send text or workspace files to a linked Telegram identity,
  including from an email task. Requires a configured Telegram integration;
  destination authorization is checked at runtime. See
  [Telegram and voice](telegram-and-voice.md).

## Memory Tools

Memory:

- `memory_remember` — create an atomic durable memory or expiring working context. Inferred memories require `metadata.evidence` and `metadata.why_future_relevant`; `metadata.explicit_user_requested=true` is the alternative only when the user explicitly asked to remember it. Other metadata keys, including `source_evidence`, are rejected. Kinds include decision, agreement, incident, preference, operating_rule, project_context, and working_context; importance is 1-5.
- `memory_search` — semantic search when query is non-empty, keyword search when blank
- `memory_update`
- `memory_forget`

Memory and contact details use distinct tool groups; see [Memory System](memory-system.md).
`memory_forget` additionally requires an authenticated admin request.

## Contacts Tools

- `contact_search`
- `contact_read`
- `contact_create`
- `contact_update`
- `contact_delete`

Contacts are deliberately separate from memory records.

## Reminders Tools

- `reminder_create`
- `reminder_list`
- `reminder_update`
- `reminder_cancel`

Recurrence supports `hour|day|week|month` with interval and anchor-day logic.

## Deep Research Tools

- `deep_research_request`
- `deep_research_status`

`deep_research_request` starts asynchronous research; the parent job transitions
to `waiting` and resumes with results. It and `web_search` require enabled
OpenRouter search configuration; deep research must also be enabled.

## Command and Calendar Tools

Command:

- `command_execute` — pass `command` as an argv array, with optional
  `timeout_seconds` and an existing workspace `workdir`. Shell syntax requires
  an explicit shell invocation. See [sandbox execution](sandbox.md) for
  isolation, limits and retry behavior.

Calendar:

- `calendar_sync` — triggers a refresh from all configured calendar API providers; no-op when no providers are active
- `calendar_list_busy` — lists busy windows across all configured providers within a date/time range; titles are redacted unless `allow_read_event_details` is enabled. Only `complete=true` verifies availability; `provider_errors` and `status=degraded`/`no_providers` indicate an incomplete check, even when `busy` is empty.
- `calendar_list_events` — lists calendar events from all configured providers; titles and details subject to the same policy
- `calendar_list_calendars` — lists provider calendar IDs and names for diagnostics
- `calendar_create_event` — creates an event on the specified provider (or the default provider when `provider` is omitted); supports `alert_minutes` override
- `calendar_update_event` — updates a managed event by its assistant-assigned ID; accepts `alert_minutes` override
- `calendar_delete_event` — deletes a managed event; no-ops gracefully when the event is already gone on the provider

Calendar tools require `agent.calendar.enabled` and at least one provider client
ID configured through the credential store. Exposure is not a successful
connection check: provider setup/token failures can still occur at runtime.
Updates and deletions are restricted to assistant-managed events; non-managed
events can appear in list results but cannot be modified. See
[calendar providers](calendar-providers.md) for setup.

## Zoom Tools

- `zoom_create_meeting`, `zoom_read_meeting`, `zoom_list_meetings`
- `zoom_update_meeting`, `zoom_delete_meeting`, `zoom_reconcile_meeting`

Require an enabled Zoom integration, client ID/secret, and an account ID or
refresh token. Tools operate on assistant-managed meetings, not arbitrary
provider meetings. Use stable creation idempotency keys and reconcile uncertain
creates rather than duplicating them. To link a meeting to a calendar event,
create Zoom first, then pass its local managed ID as `zoom_meeting_id` with
matching start and duration. The calendar tool adds the participant join URL;
never disclose host start URLs. See [calendar providers](calendar-providers.md).

## Context and Search Tools

- `context_search` — semantic search across memories, jobs, reminders, inbound/outbound emails, and contacts; accepts `recent_only=true` to limit to last 7 days
- `job_search` — search past job history by query and status
- `job_read` — read a past job's full details by ID

## Web and Fusion Tools

- `web_search` is a function tool.
- `openrouter:web_search` is attached to the isolated search-model request inside
  `web_search` when a non-Perplexity search model is used, not to the main loop.
- `openrouter:fusion` is a hosted-tool builder gated by fusion configuration,
  not a loadable function. The normal task loop's enabled-name filter currently
  excludes it, so enabling fusion alone does not attach it to loop requests.

## Model Preset Tool

`llm_service` is an opt-in, loadable tool for isolated OpenRouter chat requests,
including supported image/audio generation and multimodal inputs. It does not
inherit the assistant's context or tools and does not execute returned tool calls.

Enable it with `./setup.sh --section advanced` → **Model preset tool**.
It requires `OPENROUTER_API_KEY` and an available shared workspace, even if the
main assistant uses a different model provider. It is disabled by default.
Setup's **Advanced Settings → Model preset tool** also selects all models
(default) or ZDR-only discovery and routing with `AGENT_MODEL_SERVICE_ZDR_ONLY`.
The restriction applies to existing presets as well as new ones; it does not
govern other tools. See [OpenRouter](openrouter.md#optional-preset-based-model-tool).

Actions:

- `query_models`: search the current catalog by `query`, `input_modalities`,
  `output_modalities`, and `supported_parameters`; paginate with `limit` (1-50)
  and `offset`. Capability filters require all requested values.
- `add_preset`: persist `name`, exact `model` slug, and optional `system_prompt`.
  Names are unique, 1-64 letters/digits/underscores/hyphens, beginning with a
  letter or digit. Maximum 100 presets; duplicate names are rejected.
- `list_presets`: return definitions and current budget; optional exact `name`.
- `delete_preset`: delete by `name`. Recreate to change a preset.
- `use_preset`: call a saved `name` with `payload.messages`, optional `files`,
  `temperature`, `max_tokens`, `response_format`, `output_modalities`, `voice`,
  and `audio_format`.

Presets are local, installation-wide database records shared across jobs and
workers, not OpenRouter-hosted presets. Store reusable instructions, not secrets
or private job-specific content. An in-flight call retains a snapshot even if
its preset is deleted. There is no silent model fallback.

First use `query_models` to confirm that the chosen model currently advertises
image output; the slug below is illustrative, not an availability guarantee.

```json
{"action":"add_preset","name":"illustrator","model":"google/gemini-3.1-flash-image","system_prompt":"Create clear, uncluttered illustrations."}
```

```json
{
  "action": "use_preset",
  "name": "illustrator",
  "payload": {
    "messages": [
      {"role": "user", "content": "Simplify the background of this image."}
    ]
  },
  "files": ["outputs/job-12/previous-image.png"],
  "output_modalities": ["text", "image"]
}
```

Only user/assistant text messages are accepted in the prompt payload; its final
message must be a user message. The preset's system prompt is prepended.
Attachments are encoded by the tool and added to that final user message.
Limits: five regular workspace files, 10 MiB each, 20 MiB combined, 200000 prompt
bytes, and 16384 maximum output tokens (default 4096 when supported). Internal,
credential, source-archive and symlink paths are rejected. Supported attachments
are images, audio, video, PDFs (where advertised), and common UTF-8 text files.
Office files must first be converted using existing file tools.

Generation settings are validated against catalog capabilities; explicitly
unsupported parameters produce errors. Outputs default to the model's advertised
modalities; request `["text"]` when you only need analysis from a media model.
Image and audio output cannot be combined in one request. Audio output uses
streaming internally; voice and format availability remain provider-dependent.
The tool returns text, usage, actual model, finish reason, and media file paths.
Supported inline PNG/JPEG/WebP/GIF and WAV/MP3/FLAC/Ogg/PCM outputs are saved under
`outputs/job-<id>/` with unique names and workstream provenance; previous versions
are never overwritten. Response size is limited to 64 MiB, each decoded media
file to 32 MiB. Remote media URLs and unknown response formats produce explicit
errors after accounting for the charge; they are not silently dropped.
Inspection is the caller's responsibility, for example by attaching an output
to a vision preset. Dedicated image/transcription/video APIs are not part
of this chat-compatible tool. Dedicated speech models use `text_to_speech` below.

The default budget is **USD 5 per rolling 30 days**, shared by all jobs, presets
and the separate text-to-speech tool. Setup can change the amount and period.
The check uses persisted actual costs, including non-token media charges, and
serializes request admission across workers.
It is a **spending threshold, not a hard per-request price ceiling**: the final
request can exceed the remaining amount. Use a provider-side key spending limit
for a hard cap. Model-tool tokens also count toward the parent task token limit.
Disabling/re-enabling the tool, deleting a preset, or deleting a job does not
erase spending history.

Requests are never automatically retried. Interrupted requests, missing/invalid
costs and worker crashes leave an unresolved ledger entry that blocks new
requests within the rolling period. Administrators must reconcile it against
OpenRouter activity in `model_service_calls` (set the verified `cost_usd` and a
terminal `status`, never assume zero). `list_presets` reports unresolved count;
errors identify the request ID where available. Setup factory reset has a
separate **Model presets and model-tool spending history** category; selecting
it explicitly clears presets and spending history but not generated files.

## Text-to-speech Tool

`text_to_speech` is an independently enabled, loadable tool for dedicated
OpenRouter `speech` models, not chat models advertising `audio`.
Enable **Advanced Settings → Text-to-speech** with
`./setup.sh --section advanced`. It requires an internal `OPENROUTER_API_KEY`
and writable shared workspace, but not model presets, Telegram or transcription.
Both enablement and the ZDR-only/all-model choice are independent of
`llm_service`; defaults are disabled and all models.

- `list_models`: filter by `query` and paginate with `limit` (1-50) and `offset`.
  Results include exact slugs, advertised voices/pricing, supported output
  formats and whether separate expression instructions are supported.
- `generate`: supply an exact `model`, literal `text` (1-10000 characters),
  explicit `voice`, optional `audio_format` (`mp3` or `wav`), and optional
  `instructions` (1-2000 characters). Provider limits may be lower.
- `reconcile`: supply the local `request_id` from an unknown-cost speech
  request. It looks up the existing charge, never synthesizes again, and is
  restricted to this job's speech requests with a recorded generation ID.

Discover first; this illustrative slug/voice is not an availability guarantee:

```json
{"action":"list_models","query":"tts"}
```

```json
{"action":"generate","model":"openai/gpt-4o-mini-tts","text":"Hello! Here is your update.","voice":"alloy","audio_format":"mp3","instructions":"Speak warmly and conversationally."}
```

Expression instructions are offered conservatively for documented OpenAI
mini-TTS, Gemini TTS and Fish Audio model families. Other models reject them
before spending rather than silently ignoring them. Emotion, laughter and
other effects remain model-dependent; put directions in `instructions`, not
in literal speech text. Discovery exposes advertised voices; when no voice
list is available, the provider validates the explicit identifier.

MP3 is the default except for Google/HeyGen PCM-only models, which default
to WAV. MiniMax is restricted to MP3. WAV wraps provider PCM using its declared
sample rate/channels; it does not guess metadata or re-encode audio.
Generation returns `files`, actual `usage`, request/generation IDs and the
shared budget. Files are limited to 32 MiB and tracked under
`outputs/job-<id>/`. There is no inherited assistant context, voice cloning,
reference upload, multi-speaker synthesis or channel delivery.

Speech shares the model-preset spending settings and admission lock even if
presets are disabled. Charges use OpenRouter's actual generation cost, not
token-price estimates. Only read-only accounting lookups are retried.
Missing or invalid costs block both tools. If audio was received but accounting
is delayed, the error identifies the saved path and local request ID: reuse
that file and call `reconcile`; **do not repeat synthesis**.
Missing generation IDs or interrupted workers require administrator
reconciliation. Completed requests cannot be reconciled again.

To deliver the saved file, call a separate channel tool. Native Telegram voice
messages use a per-attachment `voice_note: true` flag on `telegram_send`;
ordinary attachments remain documents. See [Telegram and local voice](telegram-and-voice.md).

## Terminal and Meta Tools

Terminal:

- `task_complete`
- `task_failed`
- `request_input`

Meta:

- `get_tool_specs`

`request_limit_increase` is an auxiliary tool dynamically injected by task-agent when near configured resource limits.
