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

## Memory and Notes Tools

Memory:

- `memory_remember` — create a durable memory with optional `kind` (decision, agreement, incident, preference, operating_rule, project_context) and `importance` (1-5)
- `memory_search` — semantic search when query is non-empty, keyword search when blank
- `memory_update`
- `memory_forget`

Notes:

- `note_create`
- `note_search`
- `note_read`
- `note_update`
- `note_delete`

Notes are private to explicit tool access and never injected into prompt context.

**Auto-Entity Linking**: When enabled (`agent.entities.auto_link_on_create`), newly created memories and notes are automatically linked to 1-3 high-level entities via LLM classification. This grouping is best-effort and never blocks object creation.

## Contacts Tools

- `contact_search`
- `contact_read`
- `contact_create`
- `contact_update`
- `contact_delete`

Contacts are deliberately separate from memory records.

**Auto-Entity Linking**: When enabled, newly created contacts are automatically linked to high-level entities based on name, company, title, and notes fields.

## Reminders Tools

- `reminder_create`
- `reminder_list`
- `reminder_update`
- `reminder_cancel`

Recurrence supports `hour|day|week|month` with interval and anchor-day logic.

**Auto-Entity Linking**: When enabled, newly created reminders are automatically linked to high-level entities based on title and task description.

## Projects and Deep Research Tools

Projects:

- `project_create`
- `project_status`

Deep research:

- `deep_research_request`
- `deep_research_status`

`project_create` and `deep_research_request` are async request tools; parent job transitions to `waiting`.

**Auto-Entity Linking**: When enabled, newly created projects are automatically linked to high-level entities based on project title and first few task descriptions.

## Command and Calendar Tools

Command:

- `command_execute`

Calendar:

- `calendar_sync`
- `calendar_list_busy`
- `calendar_list_events`
- `calendar_create_event`
- `calendar_update_event`
- `calendar_delete_event`

Calendar writes are managed-only through gateway rules.

## Context and Search Tools

- `context_search` — semantic search across all sources (emails, jobs, memories, notes, reminders, contacts, projects); accepts `recent_only=true` to limit to last 7 days
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
