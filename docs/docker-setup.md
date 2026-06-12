# Docker Setup

This guide explains how the Docker images and Compose stack are wired for Assistant.

## Images

## Agent image (`agent/Dockerfile`)

The agent build is multi-stage:

1. **`ui-build` stage (`node:22-bookworm`)**
   - Installs frontend deps from `agent/frontend/package*.json`
   - Builds the workspace/admin frontend bundle with Vite
2. **Runtime stage (`python:3.12-slim`)**
   - Installs runtime OS packages (`pandoc`, `weasyprint`)
   - Installs Python dependencies from `requirements.txt`
   - Copies Python source code and compiled UI assets

This keeps UI build tooling out of the final runtime image while shipping compiled assets inside the Python container.

## Sandbox image (`sandbox/Dockerfile`)

The sandbox image is separate and includes tooling useful for command execution (`curl`, `git`, `jq`, `unzip`, etc.) plus `server.py` exposed via Uvicorn.

In production defaults, this image is used both for the sandbox broker and per-command run containers (`SANDBOX_IMAGE`, default `assistant-sandbox:latest`).

## Compose topology

Main services in `docker-compose.yml`:

- `postgres`
- `clamav`
- `agent-api`
- `downloader`
- `task-agent`
- `workspace-indexer`
- `reminder-scheduler`
- `project-scheduler`
- `deep-research-agent`
- `supervisor`
- `heartbeat`
- `ollama`
- `ollama-model-puller` (one-shot startup helper)
- `sandbox`

Most Python roles share one image (`build: ./agent`) and are selected by `AGENT_ROLE`.

## Networks

- `app_net`: normal application/data plane traffic.
- `sandbox_net` (`internal: true`): sandbox control-plane traffic.

`sandbox` is attached only to `sandbox_net`. Services that call sandbox endpoints (`agent-api`, `task-agent`) are attached to both networks.

Per-command run containers are created by the sandbox broker on ephemeral one-container bridge networks (not on `app_net`).

## Volumes and mounts

Default bind mounts:

- `./data/postgres` → PostgreSQL data
- `./data/share` → shared workspace
- `./data/private/artifacts` → private inbound artifacts
- `./data/private/calendar` → private calendar sync state
- `./data/ollama` → Ollama model cache
- `./data/clamav` → ClamAV signatures/state
- `./config` (read-only) → `/app/config`

The common config mount is declared once via the `x-agent-config-volume` anchor and reused by all agent-role services.

## Compose anchors and reuse

`docker-compose.yml` uses anchors to avoid duplication:

- `x-agent-secrets`: shared environment variables sourced from `.env`
- `x-agent-config-volume`: read-only config bind mount
- `x-agent-common`: shared build/restart/environment/network defaults

Role services (`task-agent`, `downloader`, `supervisor`, etc.) extend `x-agent-common` and override only role-specific fields.

## Scaling

`task-agent` supports horizontal scaling with:

```yaml
deploy:
  replicas: ${TASK_AGENT_WORKERS:-1}
```

Workers safely claim jobs via DB locking (`FOR UPDATE SKIP LOCKED`).

## Sandbox runtime defaults

The default hardened sandbox settings in Compose include:

- broker container is read-only with `tmpfs: /tmp`
- `security_opt: no-new-privileges:true`
- broker drops all Linux capabilities
- run containers drop `NET_RAW`
- CPU/memory/PID limits for run containers
- Docker socket mounted only into broker (never into run containers)

Important environment variables:

- `SANDBOX_SHARED_ROOT=/data/share`
- `SANDBOX_SHARED_ROOT_HOST=${PWD}/data/share` (override when needed)
- `SANDBOX_RUN_NETWORK_MODE=per_run_bridge`
- `SANDBOX_RUN_CPUS`, `SANDBOX_RUN_MEMORY_BYTES`, `SANDBOX_RUN_PIDS_LIMIT`

## Operational notes

- Keep `./config` mounted read-only so runtime prompt/config files stay operator-controlled.
- If you change host paths, update all relevant bind mounts consistently.
- For gVisor (`runsc`), use a Compose override and validate networking carefully before production use.