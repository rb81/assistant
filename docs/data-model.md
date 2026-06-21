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

## Queue and Concurrency Guarantees

- Jobs are claimed with `FOR UPDATE SKIP LOCKED` semantics.
- Partial unique index `jobs_one_open_per_thread_idx` enforces one open job per thread for statuses `queued|running|waiting|needs_review`.

## Agent Context Stores

### Memory

- `agent_memories`
- `memory_events`

Durable memory records plus audit/event log.

### Notes

- `agent_notes`
- `note_events`

Agent-private note records (not prompt-injected by steward).

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

### Projects

- `projects`
- `project_tasks`

Ordered task orchestration with one-sequence-at-a-time progression.

### Deep research

- `deep_research_runs`
- `deep_research_events`

Asynchronous research execution and event timeline.

## Calendar

- `calendar_managed_events`
- `calendar_event_audit`

Managed-event ownership and audit history.

## Entity Registry

- `entities`
- `entity_object_links`

High-level entity groupings (projects, topics, areas of life) that any object in the system can be linked to. The entity registry enables automatic organization and cross-object retrieval via LLM-powered entity resolution.

Unlike the polymorphic `linked_entities` JSONB column in memories/notes (which stores URN-style references to domain objects like contacts/projects/reminders), the entity registry uses a dedicated junction table and provides bidirectional linking with cascade deletion support.

## Operational and Diagnostics Tables

- `runtime_state`
- `manual_events`

Runtime metadata and manual/rule-based monitor actions.

## Status Enums (selected)

- `jobs.status`: `queued|running|waiting|completed|failed|needs_review|cancelled`
- `reminders.status`: `scheduled|queued|completed|failed|cancelled`
- `projects.status`: `queued|running|completed|failed|cancelled`
- `project_tasks.status`: `pending|queued|running|completed|failed|cancelled`
- `deep_research_runs.status`: `queued|running|waiting_for_input|completed|failed|cancelled`

## Updated-at Trigger Pattern

Many tables use `updated_at` with a shared trigger function `set_updated_at()` and per-table `BEFORE UPDATE` triggers.

This keeps row-update timestamps consistent without repeating update logic in every SQL statement.
