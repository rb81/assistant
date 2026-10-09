# Data Model

This document summarizes the PostgreSQL schema in `database/schema.sql`.

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

`dropbox_transfers` records task-owned import/export operations, remote paths,
file IDs/revisions, local/content hashes, byte sizes, scan/conversion state and
safe errors. Status is `pending|ready|failed|uncertain`. Stable request keys and
transaction-scoped advisory locks serialize retries. Transfers cascade with
their job; deleting database rows does not delete local or Dropbox files.
Email-only `processed_artifacts` ownership and source types remain unchanged.

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
