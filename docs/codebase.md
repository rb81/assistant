# Codebase guide

## Layout

- `src/agent/src/assistant_agent/` — Python application and worker roles,
  including the current `migrations/` directory.
- `src/agent/tests/` — Python tests.
- `src/sandbox/` — sandbox broker and command-run image.
- `src/transcription/` — local voice transcription service.
- `src/database/` — PostgreSQL schema and access policy.
- `src/config/` — tracked configuration defaults and prompt template.
- `src/scripts/` — setup implementation and maintenance scripts.
- `docs/` — canonical host documentation.
- `data/` — gitignored local settings, credentials, and runtime state.

The root `setup.sh` forwards all arguments to `src/scripts/setup.sh`. Compose
configuration is in `src/docker-compose.yml`; setup writes an absolute
`COMPOSE_FILE` value to `data/.env`. These paths describe the host checkout;
the runtime receives documentation, not the repository source tree.

`src/database/schema.sql` is the generated current schema baseline. Its
`schema_migrations` table records `000_current_baseline` and migrations
`006`–`011` as applied. The runtime keeps those migration files and processes
unrecorded files in filename order. It does not replay the retired `001`–`005`
conversion path. A database without the current baseline marker is refused
before migrations are written; use the [external transition package](https://github.com/rb81/assistant/releases/tag/layout-transition)
for older databases.

## Application modules

- `main.py` — process entry point and role dispatch.
- `task_agent.py`, `tools.py` — task loop, tool schemas, and execution.
- `database.py` — PostgreSQL access, current-baseline guard, and ordered
  migration runner.
- `email_ingest.py`, `telegram.py`, `requests.py` — request intake and routing.
- `memory_store.py`, `memory_manager.py`, `context_store.py` — memory and context search.
- `workspace_index.py`, `artifact_processor.py` — file indexing and artifact processing.
- `deep_research.py`, `reminders.py` — asynchronous research and reminder work.
- `calendar/`, `zoom_meetings.py`, `dropbox_files.py` — provider integrations.
- `sandbox` and `transcription` are separate services, not agent subprocess modules.

## Tests

Run Python tests from `src/agent` with pytest in an environment containing the
agent test dependencies. Tests are grouped by module in `src/agent/tests/`.

For code changes, update the focused tests and relevant guide. Start at
[the documentation index](README.md), then consult [architecture](architecture.md)
and [tools reference](tools-reference.md) as applicable.
