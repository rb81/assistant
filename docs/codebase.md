# Codebase guide

## Layout

- `src/agent/src/assistant_agent/` — Python application and worker roles,
  including the current `migrations/` directory.
- `src/agent/tests/` — Python tests.
- `src/sandbox/` — sandbox broker, command-run image and per-run egress proxy
  (`egress_proxy.py`).
- `src/transcription/` — local voice transcription service.
- `src/database/` — PostgreSQL schema and access policy.
- `src/config/` — tracked configuration defaults and prompt template.
- `src/scripts/` — setup implementation and maintenance scripts.
- `src/management/` — container image for management and administration.
- `examples/management-client/` — standalone Python API client and tests.
- `docs/` — canonical host documentation.
- `data/` — gitignored local settings, credentials, and runtime state.

The root `run.sh` resolves installed symlinks and forwards arguments to
`src/scripts/server.sh`, the Docker-backed launcher.
`ASSISTANT_HOST_ADMIN=1` selects the legacy `src/scripts/run.sh` host-Python path.
Compose configuration is in `src/docker-compose.yml`; setup writes an absolute
`COMPOSE_FILE` value to `data/.env`. These paths describe the host checkout;
the runtime receives documentation, not the repository source tree.

The Python entry module is `assistant_agent.setup`.
`management_tui.py` owns the terminal interface; `management_api_backend.py`
adapts its reads and writes to the Management API. The TUI builds settings
navigation and help from the API's server-provided groups and field catalog,
not a local UI catalog. `management.py` provides management operations.
`management_api.py` and
`management_contracts.py` expose authenticated, loopback-only v2 contracts;
`management_client.py` consumes them for CLI/TUI administration. Docker-backed
work goes through the private executor, not directly from the TUI. Its client
container receives only a read-only token bind, not the installation tree or
Docker socket. Host-side bootstrap and privileged operations remain separate
launcher commands. The native macOS client has its own repository,
[assistant-client](https://github.com/rb81/assistant-client); see
[the API reference](management-api.md) for server development commands.

`src/database/schema.sql` is the generated current schema baseline. Startup
requires its baseline marker and applies unrecorded migrations in filename order.
See [data model](data-model.md#schema-baseline-and-migrations) for migration
tracking and the transition path for older databases.

## Application modules

- `main.py` — process entry point and role dispatch.
- `task_agent.py`, `tools.py` — task loop, tool schemas, and execution.
- `database.py` — PostgreSQL access, current-baseline guard, and ordered
  migration runner.
- `email_ingest.py`, `telegram_ingest.py`, `requests.py` — intake and preprocessing.
- `telegram_admin.py` — deterministic browsing, confirmations, guidance and recovery controls.
- `email_approvals.py`, `approval_channels.py`, `approval_actions.py` — shared one-use
    email/Telegram/client approvals, verified administrator identities and action execution.
- `replies.py`, `telegram.py` — channel-aware replies and Telegram delivery;
  `telegram_config.py` holds dependency-free settings helpers used by setup.
- `workstreams.py`, `workstream_files.py`, `workstream_operator.py` — ownership,
  file retention, and local workstream administration.
- `request_router.py` — shared intent routing, reply-context weighting and deterministic
  outage fallback before incoming requests are assigned to workstreams.
- `model_service.py` — configured secondary-model calls through `llm_service`,
  including usage and cost accounting.
- `tts.py` — admin-configured speech synthesis, charge reconciliation and the `voices`/`test` admin CLI.
  `model_resources.py` shares spending admission and tracked output persistence
  between speech and model presets; channel delivery remains in `telegram.py`.
- `network_access.py` — sandbox egress host rules, per-command policy, network
  approval decisions and the `list`/`allow`/`block`/`remove` admin CLI.
- `script_permissions.py` — versioned script registry, run/host grants, screening
  cache, atomic one-execution grants, shared approval decisions and management.
- `memory_store.py`, `memory_manager.py`, `context_store.py` — memory and context search.
- `embedding_client.py`, `embedding_maintenance.py` — embeddings and resumable rebuilds.
- `workspace_index.py`, `artifact_processor.py` — file indexing and artifact processing.
- `deep_research.py`, `reminders.py` — asynchronous research and reminder work.
- `scheduled_scripts.py` — binds scheduled reminders to approved script versions,
  checks them before runs and periodically re-inspects them.
- `calendar/`, `zoom_meetings.py`, `dropbox_files.py` — provider integrations.
- `sandbox` and `transcription` are separate services, not agent subprocess modules.

## Tests

Tests are grouped by module in `src/agent/tests/`. With the dependencies in
`src/agent/requirements.txt` and pytest installed, run a focused test from the
repository root:

```bash
PYTHONPATH=src/agent/src python -m pytest -q src/agent/tests/test_memory_manager.py
```

Sandbox and transcription tests live alongside their respective services.
Docker tests require `ASSISTANT_TEST_DOCKER=1` and disposable infrastructure;
never point destructive integration tests at a live installation.

For code changes, update the focused tests and relevant guide. Start at
[the documentation index](README.md), then consult [architecture](architecture.md)
and [tools reference](tools-reference.md) as applicable.
