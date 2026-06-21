# Codebase Guide

This document helps contributors navigate the repository quickly.

## Repository Layout

- `agent/src/assistant_agent/` — core application modules.
- `agent/tests/` — unit/integration-focused tests.
- `agent/frontend/` — workspace UI frontend source (Vite/Milkdown).
- `sandbox/` — sandbox broker implementation.
- `database/schema.sql` — PostgreSQL schema.
- `config/` — tracked operational config defaults.
- `docs/` — project documentation.

## Core Python Modules (high-level)

- `main.py` — role bootstrap and process entrypoint.
- `api.py` — FastAPI app and dashboard/API endpoints.
- `task_agent.py` — primary autonomous loop.
- `tools.py` — tool schemas, runtime execution, availability gating.
- `database.py` — DB helpers and queue operations.
- `email_ingest.py` — IMAP downloader + actionable job creation.
- `memory_manager.py` / `memory_store.py` — memory stewardship and storage.
- `entity_store.py` / `entity_linker.py` — entity registry storage and LLM-powered auto-classification.
- `entity_resolver.py` — polymorphic entity reference resolution for entity-linked recall.
- `note_store.py` — notes persistence/search.
- `workspace_index.py` — workspace indexing + semantic search backend.
- `projects.py` — project scheduler/orchestration.
- `deep_research.py` — deep research async worker.
- `reminders.py` — reminder scheduling and recurrence handling.
- `supervisor.py` — running/review job oversight.
- `heartbeat.py` — rule-based health checks and notifications.
- `llm_client.py` — OpenRouter/OpenAI-compatible chat client.
- `prompt_context.py` — AGENT prompt loading and runtime context assembly.

## Frontend and UI

- Static UI templates/assets live under `agent/src/assistant_agent/ui/`.
- Frontend build source lives under `agent/frontend/`.
- Built assets are copied into the Python image during Docker build.

## Test Suite

Tests are under `agent/tests/` and cover:

- config and validation behavior,
- polling/supervisor loops,
- reminder/tool behavior,
- mailbox/threading handling,
- prompt context and UI pages,
- sandbox retry handling,
- notes/workspace index interactions.

Run tests from `agent/` with pytest (inside container or local venv with dependencies installed).

## Contributor Workflow

1. Read `docs/README.md` and relevant domain docs.
2. Make focused changes by module boundary.
3. Add/update tests for behavior changes.
4. Keep docs aligned with implementation changes (especially tools and config).
