# Data Model

This document summarizes `database/schema.sql` and the ordered migrations in
`agent/src/assistant_agent/migrations`.

## Workstreams: ownership and provenance

Migration 008 introduces a channel-neutral root above transport-specific threads:

- `workstreams`: title, immutable parent relationship, active/deleting state.
- `workstream_threads`: maps existing logical conversation identifiers to owners.
- `workstream_objects`: typed reusable objects with immutable origin identifiers.
- `workstream_attachments`: many-to-many retaining relationships.
- `workstream_files`: physical file paths and digests, independent of indexing.
- `workstream_deletions`: retryable cleanup journal; successful entries retain
  only identifiers, counts and completion status, not deleted content.

A first email, Telegram request or manual/API task establishes a workstream.
Follow-ups resolved to the same task reuse it; sharing a sender or chat alone
does not merge tasks. Existing channel routing continues to determine task
identity. Spawned reminders create child workstreams, preferring the reminder's
original owner while it survives. Producers running in a job-scoped database
inherit that job's workstream as parent for new conversations; other producers
can explicitly provide `parent_workstream_id`. Descendants remain part of the
original parent's tree.

Jobs, memories, contacts, reminders, research, calendar events, Zoom meetings,
Dropbox transfers, received artifacts and files are reusable object aggregates.
Logs/checkpoints belong to their job, research events to their run, and search
chunks to their file. Attaching research also retains its execution job;
artifact/import attachments retain their registered local files, and linked
managed calendar/Zoom objects are retained together.
Inbound messages and outbound delivery/email records have exclusive workstream
ownership and cannot be attached. Provider inputs are not owned remote outputs:
importing a Dropbox Inbox file does not grant deletion ownership of that file.

New objects attach to their creating workstream automatically. Updating an
object from another job also attaches that job's workstream; merely finding or
reading an object does not. `workstream_attach` explicitly retains an existing
object. Attachments share the live object, not snapshots. The original origin
identifier remains even if that workstream is later erased.

Migration backfills known thread/job links and unambiguous execution ancestry.
Objects without reliable source links are placed in an explicitly unattributed
administrative/legacy workstream, not attributed by semantic similarity.
Existing disk files are registered when modified or explicitly attached; the
migration does not guess ownership of unindexed, out-of-band files.

## Core Pipeline Entities

## 1) Email ingestion

- `emails`
- `processed_artifacts`

`emails` stores inbound thread context; `processed_artifacts` stores attachment/URL processing outputs and scan/conversion state.

## 2) Job execution

- `jobs`
- `task_logs`
- `agent_checkpoints`
- `supervisor_instructions`
- `outbound_email_logs`
- `thread_summaries`

`jobs` is the primary work queue. `task_logs` stores detailed event timeline. `agent_checkpoints` stores resumable context snapshots.

## Dropbox Transfers

`dropbox_transfers` records workstream-retained import/export operations, remote paths,
file IDs/revisions, local/content hashes, byte sizes, scan/conversion state and
safe errors. Status is `pending|ready|failed|uncertain`. Stable request keys and
transaction-scoped advisory locks serialize retries. Removing a job clears its
source pointer without losing the transfer's workstream relationships. Confirmed
workstream deletion performs local and exported-remote cleanup before erasing
records; deleting arbitrary database rows alone is not that workflow.
Shared received artifacts can survive deletion of their original message.

## Queue and Concurrency Guarantees

- Jobs are claimed with `FOR UPDATE SKIP LOCKED` semantics.
- Partial unique index `jobs_one_open_per_thread_idx` enforces one open job per thread for statuses `queued|running|waiting|needs_review`.

## Agent Context Stores

### Memory

- `agent_memories`
- `memory_events`

Unified durable facts and expiring working context, with suggestions awaiting review and an audit/event log. Migration 005 converted the former separate notes into memories and kept entity labels as tags.

### Contacts

- `contacts`

Dedicated contact store with unique normalized email constraint.

## Workspace and Document Index

- `workspace_files`
- `workspace_file_chunks`
- `workspace_document_conversions`

Tracks indexed files, chunked searchable text, embedding metadata, and conversion lineage.

## Scheduling and Async Work

### Reminders

- `reminders`

Supports one-time and recurring schedules.

### Deep research

- `deep_research_runs`
- `deep_research_events`

Asynchronous research execution and event timeline.

## Calendar and Zoom

- `calendar_managed_events`
- `calendar_event_audit`
- `zoom_managed_meetings`

Managed-event ownership and audit history. `calendar_managed_events` records events created by the assistant across any provider, with columns `provider_name` (e.g. `google`, `outlook`, `zoho`), `provider_event_id` (the remote event ID returned by the API), and `calendar_id` (the target calendar within the provider). Update and delete operations match exclusively on rows in this table; the assistant refuses to modify events it did not create. `zoom_managed_meetings` records Zoom meetings created by the assistant (migration 006); erasing a job removes these local rows, not the meetings at Zoom.

## Retired Tables

`projects`, `project_tasks`, `agent_notes`, `note_events`, `entities` and `entity_object_links` are created by the base `database/schema.sql` and then dropped by migration 005 (`agent/src/assistant_agent/migrations`). Supported history is preserved on parent jobs and in memories first. See [upgrading](upgrading.md).

## Operational and Diagnostics Tables

- `runtime_state`
- `manual_events`

Runtime metadata and manual/rule-based monitor actions.

## Status Enums (selected)

- `jobs.status`: `queued|running|waiting|completed|failed|needs_review|cancelled`
- `reminders.status`: `scheduled|queued|completed|failed|cancelled`
- `deep_research_runs.status`: `queued|running|waiting_for_input|completed|failed|cancelled`

## Updated-at Trigger Pattern

Many tables use `updated_at` with a shared trigger function `set_updated_at()` and per-table `BEFORE UPDATE` triggers.

This keeps row-update timestamps consistent without repeating update logic in every SQL statement.
