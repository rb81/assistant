# Data Model

This document summarizes `database/schema.sql` and the ordered migrations in
`agent/src/assistant_agent/migrations`.

## Workstreams: ownership and provenance

Migrations 008, 009 and 011 define a channel-neutral conversation and data-lifecycle
boundary above transport-specific references:

- `workstreams`: title, main thread, originating channel, naming state, immutable
  legacy parent relationship, and active/deleting state.
- `workstream_threads`: main/system thread identities mapped to their owner.
- `workstream_objects`: typed reusable objects with immutable origin identifiers.
- `workstream_attachments`: many-to-many retaining relationships.
- `workstream_files`: physical file paths and digests, independent of indexing.
- `workstream_deletions`: retryable cleanup journal; successful entries retain
  only identifiers, counts and completion status, not deleted content.

A new email establishes a workstream named from its subject. Telegram starts
with a temporary title and saves an explicit selection; `/list` switches that
selection and `/new` creates a fresh workstream. Linked email/Telegram
references resolve to the same main conversation, without merging unrelated
tasks just because they share a sender or chat.

Each reminder, including a one-off reminder, executes in its owning workstream's
`reminder:<id>` system thread; recurring occurrences create separate jobs there.
Each research run has a `research:<id>` system thread and an `execution_job_id`
reserved for the research worker, not the general task queue. Its tool effects
are attributed to that job. Results and input requests retain their source link
in `workstream_task_notifications` and are available to the main conversation.
Research guidance resumes the existing run, preserving its events and tool
budget. Completion wakes the original task to deliver the user-visible answer.
Main-thread context includes bounded outcomes, not research tool transcripts;
explicit workstream/job/research reads expose execution details.

Contacts and files are the only newly adoptable object categories. Jobs,
memories, reminders, research, calendar events, Zoom meetings, Dropbox transfers
and received artifacts have an owning workstream. Logs/checkpoints belong to
their job, research events to their run, and search chunks to their file.
Inbound messages and outbound delivery/email records have exclusive workstream
ownership and cannot be attached. Provider inputs are not owned remote outputs:
importing a Dropbox Inbox file does not grant deletion ownership of that file.

New objects attach to their creating workstream automatically. Searching,
reading or editing another workstream's object does not adopt it or change its
owner. `workstream_attach` explicitly adopts a live contact or file, not a
snapshot. The original origin identifier remains if another retaining
workstream survives its deletion. Memories retain their owner and source
evidence during cross-workstream recall; relevant local memories are preferred.

`workstream_object_events` records successful field changes, actor provenance
and supplied reasons. `workstream_context_events` notifies affected retaining
workstreams idempotently without sending messages or waking agents. Running
jobs refresh context before further tool calls; version-checked mutations
reject stale state. Provider changes use the durable `provider_mutations`
journal to reconcile uncertain responses before recording success.

Migration backfills known thread/job links and unambiguous execution ancestry.
Objects without reliable source links are placed in an explicitly unattributed
administrative/legacy workstream, not attributed by semantic similarity.
Existing disk files are registered when modified or explicitly attached; the
migration does not guess ownership of unindexed, out-of-band files.
Existing shared non-contact/file objects and historical reminder child
workstreams retain their IDs and retention links rather than losing data during
upgrade. New reminder occurrences use owned system threads. Migration 011 adds
research execution jobs without resetting existing runs or their event history,
and preserves legacy retaining links to their execution.

Confirmed deletion removes owned main/system conversations, jobs, memories and
eligible derived data. Explicitly retained contacts/files (and pre-existing
legacy shared objects) can survive. Surviving object-change events retain
object differences and actor workstream IDs, but lose deleted actor job IDs and
copied reasons. Deleted routing references are hashed tombstones; stale Telegram
selections are marked unavailable rather than redirected or resurrected.

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
