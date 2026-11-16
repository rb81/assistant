# Architecture

Assistant is a Docker Compose application. PostgreSQL stores durable state;
Python services handle intake, execution, indexing, scheduling, and monitoring.

## Services

- `management` — authenticated reads, reviewed mutations, and client delivery;
  published only on host loopback for local clients or SSH forwarding.
- `management-docker` — internal, project-scoped Docker read gateway and private
  typed executor for administrative operations. It has Docker daemon authority;
  the public API container itself has no Docker socket.
- `postgres` — jobs, messages, logs, memories, reminders, integrations, and indexes.
- `downloader` — optional email intake (the `email` profile).
- `telegram-receiver` and `request-preprocessor` — optional private Telegram intake
  and attachment/voice preprocessing (the `telegram` profile).
- `task-agent` — LLM-driven execution of queued tasks.
- `deep-research-agent` — asynchronous research execution.
- `workspace-indexer` — workspace document indexing.
- `reminder-scheduler` — queues due tasks, delivers notification-only reminders,
  and reschedules recurring reminders.
- `supervisor` and `heartbeat` — job review and queue-health monitoring.
- `ollama` and `ollama-model-puller` — local embedding service and model setup.
- `clamav` — artifact scanning.
- `transcription` and `transcription-init` — optional local voice processing (`voice` profile).
- `sandbox` and `sandbox-init` — authenticated command broker and protected runtime setup.

Most application roles use the Python agent image and select behavior through
`AGENT_ROLE`. Services and optional profiles are defined in `src/docker-compose.yml`.

## Request and job flow

Email, Telegram, or an authenticated client records a request and queues work in
an owning workstream. Email/Telegram intake also processes permitted attachments.
Task-agent claims queued work through PostgreSQL,
builds context, calls the configured model, and runs available tools. Automatically
routed jobs deliver replies through the request route; legacy email jobs require
explicit email tools. PostgreSQL records progress and side effects. Research has
a dedicated worker; reminders either queue task-agent jobs or send notifications
without a model call. System-thread outcomes are available in the owning
workstream's main conversation.

Task-agent workers claim jobs with database locking, preventing simultaneous
claims of the same queued job. This is not an exactly-once guarantee for external
side effects after retries; see [concurrent job processing](concurrent-job-processing.md).

## Runtime boundaries

Agent file tools are rooted at `/data/share`, not the host repository.
Credentials, database files, and private artifacts are kept outside the workspace.

The sandbox broker starts a disposable container for each command with the
shared workspace mounted. The command container has no application credentials,
Docker socket, or host-repository access. The broker itself has Docker daemon access; see
[security](security.md) for the residual risk and host requirements.
Command containers have no direct internet: a per-run proxy sidecar allows
HTTPS only to approved hosts, and the agent requests new hosts through an admin
approval. See [sandbox network isolation](sandbox.md#network-isolation).

Documentation is bundled with the application image and retrieved through
`help_search` and `help_topic`; `runtime_capabilities` supplies live settings
and limits. No host documentation is mounted into the workspace, sandbox broker
or per-command containers. Help is reference material, not access to host
secrets, source files or service controls.

PostgreSQL and sandbox control traffic use verified TLS. Compose networks scope
internal communication. The operator API publishes only a loopback port; there
is no browser administration interface. Desktop, CLI, and TUI reads and writes
share the Management API. The TUI is a thin API client and does not directly
manage Docker, PostgreSQL, or host files. Its separate client runtime has no
installation-root or Docker-socket mount. Docker-backed operations run through
the API's private typed executor; the installed launcher may use disposable
administration containers for bootstrap. Host-side server and credential-sync
commands are explicit operator operations. The default installation needs no
host Python.

## Persistent state

Local deployment state is under gitignored `data/`: `.env`, overrides,
credentials, setup state, PostgreSQL, private files, and the shared workspace.
Tracked defaults and templates are under `src/config`.
See [running Assistant](running-assistant.md) for paths and commands.
