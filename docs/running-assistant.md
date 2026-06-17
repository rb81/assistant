# Running Assistant

## Configuration

Create the local secrets file from the template:

```bash
cp .env.example .env
# Fill in your secrets and identity values in .env
```

Use `.env` for secrets and account/safety values such as provider keys, mail credentials, `AGENT_NAME`, `AGENT_EMAIL`, `ADMIN_EMAIL`, `ORG_NAME`, `ORG_SECURITY_EMAIL`, `ORG_INTERNAL_EMAIL_DOMAINS`, permitted recipient domains, and actionable senders. `config/agent.yaml` is a tracked file with sensible operational defaults for model names, limits, feature settings, ports, paths, and tool behavior — you should not need to edit it for normal deployments.

Minimum values for manual dashboard jobs processed by the task-agent:

- `POSTGRES_PASSWORD`
- `AGENT_API_PUBLISHED_HOST`
- `AGENT_API_PUBLISHED_PORT`
- `AGENT_APP_BASE_URL`
- `OPENROUTER_API_KEY`
- `ADMIN_EMAIL`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_FROM`

`AGENT_API_PUBLISHED_HOST=127.0.0.1` keeps the dashboard available only from the server itself. To keep access from other trusted machines on the local network, set it to the server's LAN address, for example `AGENT_API_PUBLISHED_HOST=192.168.1.50`, and continue using `http://192.168.1.50:8000`. This controls host binding only, not generated links.

`AGENT_APP_BASE_URL` controls the dashboard links sent in admin emails and overrides `agent.app.base_url` when non-blank. In production, set it to the public URL users should click, for example `https://assistant.example.com`.

Persistent data defaults to the repository-local `./data` directory. Docker Compose bind-mounts PostgreSQL data, the shared workspace, private artifacts, ClamAV data, and Ollama model/cache data under that directory.

Additional values for email-driven jobs:

- `IMAP_HOST`
- `IMAP_PORT`
- `IMAP_USERNAME`
- `IMAP_PASSWORD`
- `IMAP_FOLDER`
- `IMAP_ARCHIVE_FOLDER`
- `IMAP_SENT_FOLDER`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`

Set `EMAIL_ALLOWED_RECIPIENT_DOMAINS` before enabling outbound email. The configured allowlist is enforced before SMTP delivery.

`ORG_INTERNAL_EMAIL_DOMAINS` is the domain list considered internal for autonomous-agent disclosure footers; it is separate from the recipient delivery allowlist. External outbound mail receives a short footer using `ORG_NAME` and `ORG_SECURITY_EMAIL`, and inbound replies are stripped before storage so the agent does not see its own disclosure.

IMAP is used for inbound email polling, archiving queued messages out of the polling folder, and optionally appending sent-message copies. SMTP is used for outbound replies and clarification requests. There is no Gmail-specific integration, so use provider-specific folder names such as `[Gmail]/All Mail` or `[Gmail]/Sent Mail` when needed.

Outbound `email_send` supports attachments either from an existing shared-workspace file path or from small inline text content. Prefer shared files for deliverables and binary files.

Search uses OpenRouter's `openrouter:web_search` server tool. It is included in the model payload only when `agent.search.enabled=true`, `agent.llm.base_url` points at OpenRouter, and `OPENROUTER_API_KEY` is set.

Fusion uses OpenRouter's `openrouter:fusion` server tool for multi-model deliberation. It is included in the model payload only when `agent.fusion.enabled=true`, `agent.llm.base_url` points at OpenRouter, and `OPENROUTER_API_KEY` is set.

`request_input` emails either the original sender (`recipient=requester`) or `ADMIN_EMAIL` (`recipient=admin`) and pauses the job. The configured admin email is included in runtime prompt context and is the approval/contact endpoint for admin escalation.

`task_complete` requires a user-visible `response`. Manual dashboard jobs display that response in the job detail view. For external email jobs, Assistant rejects completion until a sent `email_send` reply exists for the latest external requester.

Prompt context and agent docs:

- `config/AGENT.md` is the runtime system prompt file (resolved by `agent.prompt.agent_file`, default `AGENT.md` in `/app/config`). Startup fails if it is missing or empty. Create it with `cp config/AGENT.md.example config/AGENT.md` and then edit it for your deployment.
- Runtime prompt context also includes generated identity/admin/time metadata from configuration.
- Packaged docs such as `.assistant/docs/SANDBOX_CAPABILITIES.md` are copied from app defaults into the shared workspace and refreshed when prompt context is prepared, so updates can improve tool/environment references.

Memory and reminders:

- Persistent memories are stored in PostgreSQL and managed by a synchronous Memory Steward model, defaulting to `openai/gpt-4.1-mini`.
- The Memory Steward only stores high-signal durable memories such as decisions, agreements, incidents, preferences, operating rules, and important project context. It intentionally does not store contact records.
- The Memory Steward uses the internal Ollama service for semantic embeddings and falls back to keyword/tag search when embeddings are unavailable.
- Docker Compose runs `ollama-model-puller` at startup to pull the configured embedding model into `./data/ollama`.
- Reminders are stored in PostgreSQL and exposed through `reminder_create`, `reminder_list`, `reminder_update`, and `reminder_cancel`.
- `reminder_create` supports recurring schedules through `recurrence_unit` (`hour`, `day`, `week`, or `month`) and `recurrence_interval` for every X units.
- The `reminder-scheduler` service checks due reminders every 2 seconds by default and queues each due reminder as a normal job. Completed recurring reminders are rescheduled to their next run.
- For a host cron instead of the Compose service, run the one-shot role every minute: `docker compose run --rm -e AGENT_ROLE=reminder-cron task-agent`.

Projects and deep research:

- `project_create` lets the task-agent create an ordered project. The current job pauses, and `project-scheduler` queues one child job at a time. Sequence 2 is not queued until sequence 1 completes.
- Each project gets a shared workspace path in metadata. Child jobs receive recent original-thread context, prior task summaries, and can inspect linked progress with `project_status`.
- Project child jobs can use normal tools and `deep_research_request`, but they cannot call `project_create`. This is enforced in both tool exposure and runtime validation.
- `deep_research_request` creates a research run and pauses the current job. The `deep-research-agent` uses a focused research prompt with email tools, file tools, direct OpenRouter web search, and terminal research controls.
- `deep_research_status` lets linked jobs inspect run progress, result data, errors, and recent search/tool events.
- A research run can pause by emailing the requester for guidance. A reply in the same thread resumes the waiting research run before the original job is requeued.
- The task-agent sends the original requester a short status email when it successfully starts a project or deep research run, unless the sender is local.
- `heartbeat` periodically surveys active jobs, projects, and research runs with a rule-based queue monitor. It can flag stuck work for review and send admin notifications/digests.
- For host cron instead of Compose services, run `docker compose run --rm -e AGENT_ROLE=project-cron task-agent`, `docker compose run --rm -e AGENT_ROLE=deep-research-cron task-agent`, and `docker compose run --rm -e AGENT_ROLE=heartbeat-cron task-agent`.

Polling defaults are split by external and internal workload. IMAP polling uses `agent.email.imap_poll_interval_seconds` and defaults to 60 seconds. Internal PostgreSQL-backed queues use shorter defaults: `agent.task_agent.poll_interval_seconds`, `agent.reminders.scheduler_poll_interval_seconds`, `agent.projects.scheduler_poll_interval_seconds`, and `agent.deep_research.poll_interval_seconds` default to 2 seconds. Project and reminder schedulers keep looping immediately after they process work, then sleep only once they find nothing ready.

Shared workspace settings live in local `config/agent.yaml`:

- `agent.filesystem.shared_root`: container path for agent and sandbox file access. Defaults to `/data/share`.
- `agent.filesystem.require_mount`: when `true`, file tools are exposed only if the shared root is an actual mount point.
- `agent.filesystem.shared_file_umask`: process umask for new shared-workspace files from agent services. Defaults to `0002`, producing group-writable files/directories on normal Unix filesystems.
- `agent.tool_result_cache.root`: shared-workspace path for cached bulky tool output. Defaults to `.assistant/cache/tool-results`.
- `agent.tool_result_cache.retention_days`: how long cleanup keeps cached tool-output files. Defaults to `7`.
- `agent.prompt.agent_file`: prompt filename loaded from `/app/config`. Defaults to `AGENT.md`.
- `agent.workspace.convertible_document_extensions`: extensions that can be indexed through MarkItDown and converted explicitly to Markdown.
- `agent.conversion.pandoc_path`: Pandoc executable for explicit workspace and agent file conversions. Defaults to `pandoc`.
- `agent.conversion.pdf_engine`: Pandoc PDF engine. Defaults to `weasyprint`.
- `agent.workspace.index.enabled`: maintains embedding-backed search over indexable workspace file chunks.

By default, Docker Compose mounts host `./data/share` to container `/data/share` in every service that reads or writes shared files. The sandbox broker also passes the host path to per-command run containers. Sandbox runtime defaults are kept in `docker-compose.yml`; app-level sandbox retry defaults are kept in `config/agent.yaml`. Do not add these defaults to `.env`, which should stay focused on secrets and deployment-specific standard app settings.

Sandbox command execution:

- `command_execute` requests go to the sandbox broker at `agent.sandbox.base_url`.
- In the default Compose setup, the broker starts a fresh Docker container for each command and removes it after completion.
- The broker is on the internal `sandbox_net` control network. Run containers are created on a fresh one-container bridge network, are not attached to app networks, and receive only the shared workspace mount.
- Sandbox request failures are retried according to `agent.sandbox.max_attempts` and `agent.sandbox.retry_backoff_seconds`. Normal non-zero command exits are returned to the agent and are not retried automatically.

Production sandbox defaults in Compose:

- Run image: `assistant-sandbox:latest`.
- Isolation mode: `container`.
- Runtime: Docker's default runtime.
- Network: fresh bridge per command, with inter-container communication disabled.
- Mounted data: shared workspace only, from host `./data/share` to container `/data/share`.
- Limits: `2` CPUs, `2147483648` bytes memory, `512` PIDs.
- Capabilities dropped in run containers: `NET_RAW`; broker container drops all Linux capabilities.

gVisor can be enabled later with a local Compose override that sets `SANDBOX_RUN_RUNTIME=runsc`, but Docker's embedded DNS on user-defined bridge networks is not reliable under gVisor. Keep the default runtime unless that hardening is paired with a tested networking/proxy design.

For SMB/NAS shares, keep `agent.filesystem.shared_file_umask` as `0002` when your SMB user is in the shared directory's group. If the share requires broad write access, use `0000`. Existing files may still need a one-time host-side repair, such as `chmod -R u+rwX,g+rwX,o+rX data/share` and, on Linux if ownership is wrong, `sudo chown -R smbuser:smbgroup data/share`.

Tool-result cache cleanup:

```bash
docker compose run --rm -e AGENT_ROLE=tool-cache-cleanup-cron task-agent
```

## Start

```bash
docker compose up --build
```

`--build` rebuilds local images but still uses cached layers. For a full local-image rebuild, use:

```bash
docker compose build --no-cache
docker compose up -d --force-recreate
```

Services:

- `agent-api`: FastAPI dashboard and JSON API on `http://localhost:8000`.
- `downloader`: IMAP polling and job creation.
- `task-agent`: LLM tool loop for queued jobs.
- `reminder-scheduler`: Queues due reminders as jobs.
- `project-scheduler`: Queues ordered project tasks as child jobs and returns project results.
- `deep-research-agent`: Runs constrained deep research loops with direct OpenRouter web search.
- `workspace-indexer`: Maintains the semantic file index. PDF and Office files are text-extracted for search without moving the originals.
- `supervisor`: Stalled job and review monitor.
- `heartbeat`: Rule-based active-work survey, stale-work review flagging, and admin digest monitor.
- `ollama`: Internal embedding service for semantic memory, notes, and workspace file search.
- `ollama-model-puller`: One-shot startup service that pulls the configured embedding model.
- `sandbox`: Command execution broker for isolated per-command run containers.
- `postgres`: Durable state.

## First Run

1. Open `http://localhost:8000`.
2. Create a manual job from the left panel.
3. Watch the job move from `queued` to `running`, then to `completed`, `failed`, or `needs_review`.
4. Inspect the Agent Response and logs in the job detail view.

If required configuration is missing, the affected service exits at startup instead of running in a degraded state.

The embedding model is pulled automatically by `ollama-model-puller` when the Compose stack starts. To force another model pull manually:

```bash
docker compose up ollama-model-puller
```

If the model pull fails, memory, notes, and workspace search still use keyword fallbacks where available and log embedding failures in best-effort mode.

## Ollama Maintenance

The weekly updater script refreshes the Ollama Docker image and pulls the configured embedding model:

```bash
./scripts/update-ollama-weekly.sh
```

Install it as a weekly host cron entry on the machine running Docker Compose:

```cron
0 3 * * 0 cd "/srv/assistant" && ./scripts/update-ollama-weekly.sh >> /var/log/assistant-ollama-update.log 2>&1
```

Use the actual repository path in place of `/srv/assistant`. The cron user must be allowed to run Docker commands.

## API

Useful endpoints:

- `GET /health`
- `GET /api/stats`
- `GET /api/config/status`
- `GET /api/jobs`
- `POST /api/jobs`
- `GET /api/jobs/{job_id}`
- `POST /api/jobs/{job_id}/requeue`
- `POST /api/jobs/{job_id}/cancel`
- `POST /api/jobs/{job_id}/instructions`
- `GET /api/memories`
- `POST /api/memories`
- `GET /api/memories/{memory_id}`
- `PATCH /api/memories/{memory_id}`
- `DELETE /api/memories/{memory_id}`
- `GET /api/emails`

Manual job payload:

```json
{
  "subject": "Summarize this file",
  "body": "Read the configured shared workspace file input.txt and write a summary to summary.txt"
}
```

## Data Locations

- PostgreSQL data is stored under `./data/postgres` by default.
- PostgreSQL contains all app database records: emails, jobs, task logs, checkpoints, thread summaries, outbound email logs, reminders, runtime state, durable memories, memory events, and stored memory embedding vectors.
- Project and deep research state is also stored in PostgreSQL, including project tasks, research runs, and research event logs.
- Ollama model and embedding cache data is stored under `./data/ollama` by default.
- Raw inbound attachments are saved under `./data/private/artifacts/attachments` by default.
- Processed artifact Markdown output is written under `./data/share/processed` by default.
- Agent-created files are limited to `./data/share` by default.
- The runtime prompt file is `config/AGENT.md` (inside the bind-mounted `./config` directory).
- Packaged agent docs are refreshed under `./data/share/.assistant/docs`.
- Soft-deleted files are moved to `./data/share/.trash` by default.

To move Assistant to another machine, stop the Compose stack, copy the repository and the `data` directory, then run Docker Compose from the new checkout. The copied `data` directory contains all databases, shared workspace files, artifacts, and local Ollama model cache.

The `data` directory covers runtime state. Keep `.env` and local `config/agent.yaml` with the deployment as well.

## macOS Testing

Use Docker Desktop with the default local `./data` directory and run `docker compose up --build`. To bind a different local macOS folder instead, adjust the bind mounts in `docker-compose.yml`.

## Ubuntu Server Deployment

Create a durable host directory and keep all runtime state under it:

```bash
sudo mkdir -p /srv/assistant/data
sudo chown -R "$USER":"$USER" /srv/assistant/data
```

For a production host path such as `/srv/assistant/data`, adjust the bind mounts in `docker-compose.yml`. Keep `.env` focused on secrets and account/safety values.

Then start the stack:

```bash
docker compose up -d --build
```

If you previously ran Assistant with Docker named volumes, export those volumes before switching to the `data` directory layout. Docker will not automatically copy existing named-volume contents into the new bind-mounted directories.

Prompt context is loaded from `config/AGENT.md` (or the file named by `agent.prompt.agent_file` in `config/agent.yaml`), not from workspace instruction files.

## Production Notes

Before exposing this beyond local development:

- Put authentication in front of the dashboard.
- Publish the dashboard only on `127.0.0.1` or the server's LAN IP with `AGENT_API_PUBLISHED_HOST`; avoid `0.0.0.0` unless another firewall layer enforces access.
- Disable generated API docs with `AGENT_API_DOCS_ENABLED=false` and `AGENT_API_OPENAPI_ENABLED=false`, or set `agent.api.docs_enabled: false` and `agent.api.openapi_enabled: false` in `config/agent.yaml`.
- Replace example config values and rotate secrets.
- Restrict SMTP domains to the minimum required list.
- Add backup and retention policies for PostgreSQL and attachments.
- Enforce private-range egress blocks for sandbox run containers at the host firewall or egress proxy.
