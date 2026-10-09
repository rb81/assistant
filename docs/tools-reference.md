# Tools Reference

Assistant tools are defined in `assistant_agent/tools.py` and executed through `ToolRuntime.run(name, arguments)`.

## Tool Exposure Model

- **Core tools**: always present at loop start (`task_complete`, `task_failed`, `request_input`, plus `email_send` when SMTP available).
- **Meta tool**: `get_tool_specs` for lazy loading.
- **Loadable tools**: everything else (files, reminders, research, etc.).

Availability is computed by `available_function_names(...)` from runtime config and job context.

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
- `.assistant` is read-only for writes/deletes,
- cache/source-archive visibility is restricted,
- workspace index is updated on file mutations.

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

## Memory Tools

Memory:

- `memory_remember` — create an atomic durable memory or expiring working context. Inferred memories require `metadata.evidence` and `metadata.why_future_relevant`; `metadata.explicit_user_requested=true` is the alternative only when the user explicitly asked to remember it. Other metadata keys, including `source_evidence`, are rejected. Kinds include decision, agreement, incident, preference, operating_rule, project_context, and working_context; importance is 1-5.
- `memory_search` — semantic search when query is non-empty, keyword search when blank
- `memory_update`
- `memory_forget`

Notes and entities are retired and migrated into unified memory; see [Memory System](memory-system.md).

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

`deep_research_request` is an async request tool; the parent job transitions to `waiting`.

## Command and Calendar Tools

Command:

- `command_execute`

Calendar:

- `calendar_sync` — triggers a refresh from all configured calendar API providers; no-op when no providers are active
- `calendar_list_busy` — lists busy windows across all configured providers within a date/time range; titles are redacted unless `allow_read_event_details` is enabled. Only `complete=true` verifies availability; `provider_errors` and `status=degraded`/`no_providers` indicate an incomplete check, even when `busy` is empty.
- `calendar_list_events` — lists calendar events from all configured providers; titles and details subject to the same policy
- `calendar_create_event` — creates an event on the specified provider (or the default provider when `provider` is omitted); supports `alert_minutes` override
- `calendar_update_event` — updates a managed event by its assistant-assigned ID; accepts `alert_minutes` override
- `calendar_delete_event` — deletes a managed event; no-ops gracefully when the event is already gone on the provider

Calendar tools are exposed only when at least one provider has credentials configured in the environment. Write operations (create/update/delete) are restricted to events the assistant itself created — non-managed events appear in free/busy results but cannot be modified. See [docs/calendar-providers.md](calendar-providers.md) for provider setup.

## Context and Search Tools

- `context_search` — semantic search across memories, jobs, reminders, inbound/outbound emails, and contacts; accepts `recent_only=true` to limit to last 7 days
- `job_search` — search past job history by query and status

## Web and Fusion Tools

- `web_search` is a function tool.
- `openrouter:web_search` is attached server-side when non-Perplexity search model is used.
- `openrouter:fusion` is attached when fusion is configured.

## Terminal and Meta Tools

Terminal:

- `task_complete`
- `task_failed`
- `request_input`

Meta:

- `get_tool_specs`

`request_limit_increase` is an auxiliary tool dynamically injected by task-agent when near configured resource limits.
