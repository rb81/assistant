# Data model

## Schema baseline and migrations

`src/database/schema.sql` is the generated current PostgreSQL schema, not a
legacy bootstrap schema. It seeds `schema_migrations` with
`000_current_baseline` and the applied records for `006`–`011`. Those migration
files remain in `src/agent/src/assistant_agent/migrations/`; the startup runner
applies only unrecorded SQL files, in filename order.

The retired `001`–`005` migrations and feature-schema compatibility conversion
are not part of current startup. `ensure_feature_schema` checks for the current
baseline marker; it does not convert an older database. A database without that
marker is refused before migration writes. Use the
[external transition package](https://github.com/rb81/assistant/releases/tag/layout-transition)
to prepare an older database before starting current services.

For future schema work, update the current schema and add a numbered migration
for supported existing current-baseline databases. Keep schema changes in
application migration tracking; do not apply individual migration files
manually.

The paths above refer to a contributor's repository checkout. Runtime containers
receive the docs mounted in the shared workspace, not the source tree.

## Conversations and work ownership

`workstreams` own conversations and durable user-facing data. A workstream has
a main conversation and can have system threads for work such as reminders and
research. `workstream_threads` maps thread identities to their owner.
Workstream status is `active`, `archived`, or `deleting`; archival retains all data
and requires idle work. Archived workstreams remain retaining owners when deleting
shared data elsewhere. Job display names live in `jobs.metadata.title`, separate
from the original task summary and instructions.

Setup's workstream browser separates **User workstreams** from **Administrative
data**. The latter is the internal `system:unattributed` bucket for legacy data
without known provenance and administrative deliveries such as Telegram command
responses. Zero retained objects does not mean it contains no delivery records.
Ownership and deletion rules are unchanged; later administrative messages can
recreate the bucket after deletion. System threads belonging to actual user
workstreams remain with those workstreams, not in this administrative category.

`workstream_objects` records object identity and origin ownership;
`workstream_attachments` records retaining relationships. Contacts and files
can be attached to another workstream; reading or editing an object does not
change its owner. `workstream_object_events` and
`workstream_context_events` record successful changes and notify related
workstreams. Deletion uses `workstream_deletions` as a cleanup journal before
erasing owned records and eligible derived files.

## Core records

- `emails` and `outbound_email_logs` — inbound and outbound message records.
- `request_messages` and `channel_deliveries` — channel-neutral requests,
  preprocessing state, reply routing, and delivery attempts.
- `telegram_workstream_selections` — each Telegram sender's selected workstream.
- `jobs`, `task_logs`, and `agent_checkpoints` — queued work, execution history,
  and resumable context.
- `supervisor_instructions` and `workstream_task_notifications` — pending job
  instructions and system-task outcomes surfaced to the main conversation.
- `processed_artifacts` — attachment/URL scan and conversion state.
- `agent_memories` and `memory_events` — unified durable and expiring memory.
- `contacts` — contact details separate from memory.
- `reminders` — scheduled and recurring tasks.
- `deep_research_runs` and `deep_research_events` — asynchronous research state.
- `workstream_files` — tracked file paths, digests, and object ownership,
  including derived files.
- `workspace_files`, `workspace_file_chunks`, and
  `workspace_document_conversions` — indexed file metadata, chunks, and
  conversion lineage.
- `calendar_managed_events` and `zoom_managed_meetings` — provider records
  created and managed by Assistant.
- `dropbox_transfers` — transfer state and retry/reconciliation metadata.
- `provider_mutations` — durable mutation attempts and reconciliation state.
- `model_presets` and `model_service_calls` — secondary-model configuration,
  calls, token usage, and cost accounting.
- `sandbox_scripts` — registered entry identities and persistent script blocks.
- `sandbox_script_versions` — fingerprinted invocation/code manifests,
  reusable-approval eligibility, and cached screening findings.
- `sandbox_permission_grants` — run and host allows/blocks, scoped to one
  execution or one version; only blocks can be global. Once grants record consumption.
- `sandbox_development_sessions` — immutable admin-approved briefs, lifecycle,
  job/script identity, approval-time expiry, and atomic execution budget.
- `sandbox_development_reviews` — complete-version supervisor decisions and exact
  host sets, separated into iteration and final reviews.
- `sandbox_permission_history` — administrator changes, supervisor decisions
  and development execution audit history.
- `sandbox_network_rules` — retained legacy egress allow/block rules, global
  (`job_id` NULL) or scoped to one job. Legacy allows are inactive; blocks still
  apply. Global grants are retired with audit records by migration 016.
- `runtime_state` and `manual_events` — runtime metadata and monitor actions.

## Queue and execution

Jobs move through `queued`, `running`, `waiting`, `completed`, `failed`,
`needs_review`, and `cancelled`. Task-agent workers claim work using PostgreSQL
row locking (`FOR UPDATE SKIP LOCKED`), selecting due jobs in active workstreams.
A partial unique index prevents more than one open job for the same thread;
open means `queued`, `running`, `waiting`, or `needs_review`. Separate threads in
one workstream can still run concurrently.

Research and reminder work has dedicated execution records and system threads,
with outcomes routed back to the owning conversation. These workers do not
create a separate user-facing ownership boundary.

## Search and deletion

Memory and workspace search may use embeddings alongside keyword search.
Embeddings and indexed chunks are derived data; source records and files remain
authoritative. Reindexing or changing embedding configuration must not delete
source content.

Deletion previews and cleanup track owned data and retained contacts/files.
External effects such as sent messages, remote provider objects, model copies,
backups, and arbitrary sandbox network effects cannot be undone by deleting
local database records. See [security](security.md#workstream-deletion).
