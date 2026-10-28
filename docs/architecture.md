# Architecture

Assistant runs as a Docker Compose application. PostgreSQL stores durable
application state; Python services perform intake, job execution, indexing,
scheduling, and monitoring.

## Services

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

Email or Telegram intake validates and records a request, processes permitted
attachments, and queues work in an owning workstream. A task-agent claims a job
through PostgreSQL, builds context, calls the configured model, and executes
available tools. Automatically routed jobs deliver final answers and clarification
through the request's reply route; legacy email jobs require explicit email tools.
Side effects and progress are recorded in PostgreSQL. Research has a dedicated
worker; the reminder scheduler queues task-agent jobs or delivers notification-only
reminders without a model call. System-thread outcomes are available to the
owning workstream's main conversation.

Task-agent workers claim jobs with database locking, preventing simultaneous
claims of the same queued job. This is not an exactly-once guarantee for external
side effects after retries; see [concurrent job processing](concurrent-job-processing.md).

## Runtime boundaries

The agent's file tools are rooted at `/data/share`. That workspace is not the
host repository. Protected credentials, database files, and private artifacts
are kept outside it.

Command execution is delegated to the sandbox broker. For each command, the
broker starts a disposable container with the shared workspace mount. It does
not give the command container application credentials, the Docker socket, or
access to the host repository. The broker itself has Docker daemon access; see
[security](security.md) for the residual risk and host requirements.

The canonical host documentation directory is mounted read-only at
`/data/share/.assistant/docs` in agent services, the sandbox broker, and
per-command containers. An agent can read these files through normal workspace
tools when diagnosing task or command failures. This is diagnostic information,
not access to host secrets, host source files, or controls for restarting
services. Runtime docs are mounted rather than copied into the workspace.

PostgreSQL and sandbox control traffic use verified TLS. Compose networks scope
internal service communication; the application does not publish an operator
web interface.

## Persistent state

Local deployment state is under the gitignored `data/` directory, including
`.env`, optional overrides, credentials, setup state, PostgreSQL, private files,
and the shared workspace. Tracked defaults and templates live under `src/config`.
See [running Assistant](running-assistant.md) for paths and commands.
