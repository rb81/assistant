# Codebase Guide

This document helps contributors navigate the repository quickly.

## Repository Layout

- `agent/src/assistant_agent/` — core application modules.
- `agent/tests/` — unit/integration-focused tests.
- `sandbox/` — sandbox broker implementation.
- `database/schema.sql` — PostgreSQL schema.
- `config/` — tracked operational config defaults.
- `docs/` — project documentation.

## Core Python Modules (high-level)

- `main.py` — role bootstrap and process entrypoint.
- `task_agent.py` — primary autonomous loop.
- `tools.py` — tool schemas, runtime execution, availability gating.
- `database.py` — DB helpers and queue operations.
- `email_ingest.py` — IMAP downloader + actionable job creation.
- `memory_manager.py` / `memory_store.py` — memory stewardship and storage.
- `context_store.py` — semantic and keyword search across supported data sources.
- `embedding_maintenance.py` — embedding status, probing, and reindex operations.
- `workspace_index.py` — workspace indexing + semantic search backend.
- `deep_research.py` — deep research async worker.
- `reminders.py` — reminder scheduling and recurrence handling.
- `calendar/` — OAuth calendar providers (`google`, `outlook`, `zoho`) and `CalendarRouter`.
- `supervisor.py` — running/review job oversight.
- `heartbeat.py` — rule-based health checks and notifications.
- `llm_client.py` — OpenRouter/OpenAI-compatible chat client.
- `prompt_context.py` — AGENT prompt loading and runtime context assembly.

## Browser UI

The browser dashboard, workspace editor, and optional management API have been
retired. Local operator workflows remain available through the setup CLI.

## Test Suite

Tests are under `agent/tests/` and cover:

- config and validation behavior,
- polling/supervisor loops,
- reminder/tool behavior,
- calendar router and provider configuration,
- mailbox/threading handling,
- prompt context and local operator workflows,
- sandbox retry handling,
- memory/context search and workspace index interactions.

Run tests from `agent/` with pytest (inside container or local venv with dependencies installed).

## Contributor Workflow

1. Read `docs/README.md` and relevant domain docs.
2. Make focused changes by module boundary.
3. Add/update tests for behavior changes.
4. Keep docs aligned with implementation changes (especially tools and config).
