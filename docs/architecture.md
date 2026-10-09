# System Architecture

This document describes how Assistant is structured in production and local development.

## Runtime Topology (Docker Compose)

The default stack runs these services:

- `postgres` — durable state (jobs, logs, memories, reminders, research, contacts, and related records).
- `clamav` — malware scanning support for inbound artifacts.
- `agent-api` — optional authenticated FastAPI JSON API; no browser UI is served.
- `downloader` — IMAP ingestion and job creation.
- `task-agent` — main LLM-driven autonomous worker loop.
- `workspace-indexer` — semantic indexer for shared workspace files.
- `reminder-scheduler` — schedules due reminders into normal jobs.
- `deep-research-agent` — asynchronous constrained research loop.
- `supervisor` — stalled/failure review monitor.
- `heartbeat` — rule-based queue health checks and notifications.
- `ollama` — embedding service for supported semantic search and memory operations.
- `sandbox` — command-execution broker that starts isolated run containers.

## Networks

- `database_net`, `model_net`, and `clamav_net` are internal networks scoped to their clients.
- `sandbox_net` is internal and carries authenticated HTTPS control traffic.
- `egress_net` permits outbound provider access while disabling bridge inter-container forwarding.

The sandbox broker itself sits on `sandbox_net`. Per-command run containers are created dynamically by the broker on separate ephemeral bridge networks.

## Storage Layout

Compose bind-mounts runtime data under `./data` by default:

- `./data/postgres` — PostgreSQL files.
- `./data/share` — the shared workspace: the working directory the agent's file tools and the sandbox read and write. It is not exposed through any browser UI; the old shared editor was removed.
- `./data/private/artifacts` — private raw/processed artifact storage.
- `./data/ollama` — local Ollama model/cache storage.
- `./data/clamav` — ClamAV signatures/state.

Database schema lives at `database/schema.sql` and is initialized into PostgreSQL on first start.

## Service Roles and `AGENT_ROLE`

Most roles share the same Python image and are selected by `AGENT_ROLE` in `assistant_agent.main:run_role`:

- `api`
- `downloader`
- `task-agent`
- `workspace-indexer`
- `reminder-scheduler`
- `deep-research-agent`
- `supervisor`
- `heartbeat`

There are also one-shot cron-style roles (for host cron usage) such as `reminder-cron`, `deep-research-cron`, `workspace-index-cron`, `heartbeat-cron`, and `tool-cache-cleanup-cron`.

## End-to-End Flow

### 1) Inbound request

- Email-driven: `downloader` pulls IMAP messages, stores in `emails`, decides actionability, and queues/updates jobs.
- API-driven: a client creates a manual job through the optional HTTPS API (`POST /api/jobs`).

### 2) Job execution

- `task-agent` claims queued jobs using DB locking (`FOR UPDATE SKIP LOCKED` pattern in DB layer).
- It builds context (thread, memory, supervisor instructions, prior actions) and runs the tool-driven LLM loop.

### 3) Durable side effects

Depending on tool calls, the job may:

- send emails,
- read/write shared workspace files,
- execute sandbox commands,
- create reminders,
- start deep research runs,
- update contacts or calendar records.

All important events are logged to `task_logs`.

### 4) Monitoring & intervention

- `supervisor` checks running/review-needed jobs for stalls/failure patterns.
- `heartbeat` performs rule-based queue health checks and sends notifications.
- Admin notifications are sent on failure/review conditions.

## Security Boundaries

- File tools are restricted to configured shared root (`agent.filesystem.shared_root`).
- `.assistant` subtree is read-only for agent file writes/deletes.
- Command execution is delegated to sandbox service, not run in agent process.
- Outbound email is rate-limited and constrained by recipient allowlist.
- Large tool outputs are cached and redacted in prompt history to control context size.

## Key Operational Characteristics

- Durable DB-first architecture.
- Horizontal task-agent scaling supported (`TASK_AGENT_WORKERS` replicas).
- Clear separation between control loops: downloader, agent, schedulers, supervisor, heartbeat.
- Explicit fallback behavior for unavailable semantic services (e.g., embeddings).
