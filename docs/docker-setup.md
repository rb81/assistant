# Docker Setup

This guide explains how the Docker images and Compose stack are wired for Assistant.

## Images

## Agent image (`agent/Dockerfile`)

The image uses `python:3.12-slim`, installs runtime OS packages (`pandoc`,
`weasyprint`) and Python dependencies, and copies the agent source. There is no frontend or Node/Vite build stage.

Run `./scripts/setup.sh` first. It creates non-secret `.env` settings, protected
credential files and a generated Compose overlay with per-service secret mounts.
Start from the repository directory with `docker compose up -d --build`.
The `email` profile selects the downloader. The wizard configures it through
`COMPOSE_PROFILES`.

Existing saved installations open a section picker, even if credential activation
is pending. Use `./scripts/setup.sh --section embeddings` to update semantic
search independently of language-model credentials, or `--section calendar` / `--section zoom`
to repair Calendar/Zoom. `--section advanced` opens grouped privacy, behavior,
and limit controls without repeating provider credentials. `--wizard` runs every section. Review validates the
whole configuration before saving; Cancel never saves staged changes.

EmbeddingGemma requires Ollama 0.11.10 or newer. The managed model downloader and
weekly updater use the same `AGENT_EMBEDDINGS_MODEL` setting; a failed download
is a startup failure. Disabled embeddings and custom endpoints skip managed pulls.
For manually edited YAML, rerun setup and save to refresh the generated model-puller
overlay, or explicitly set the corresponding `AGENT_EMBEDDINGS_*` environment
overrides. `MEMORY_EMBEDDING_MODEL` is deprecated and only a bootstrap fallback
when no canonical setting or generated overlay supersedes it.

Saving embedding changes does not automatically rebuild data or recreate running
consumers. Separate confirmation offers provisioning, a connectivity/dimension
probe, and a resumable rebuild. See [embedding maintenance](memory-system.md#embeddings-and-ollama).

The wizard asks before copying the example instructions and prints the resulting
`config/AGENT.md` path. After saving it optionally builds/starts containers and
checks Docker-reported status/health. Long-running services get a verified
`unless-stopped` policy; the one-shot model downloader remains excluded. A bounded,
read-only Docker access probe skips the sudo question when direct access works.
Otherwise, non-root Linux users can explicitly consent to noninteractive sudo;
authorization is checked before startup changes containers. Failures report the
stage and a credential-safe reason, with retry commands using the selected access
mode. Saved settings may not yet be applied even if older containers are running. On local
systemd-based Linux hosts, it offers to enable Docker at boot. Privileged commands
use noninteractive sudo only with consent; otherwise manual instructions are shown.
See [configuration](configuration.md#startup-health-and-reboot-persistence).

## Sandbox image (`sandbox/Dockerfile`)

The sandbox image is separate and includes tooling useful for command execution (`curl`, `git`, `jq`, `unzip`, etc.) plus `server.py` exposed via Uvicorn.

In production defaults, this image is used both for the sandbox broker and per-command run containers (`SANDBOX_IMAGE`, default `assistant-sandbox:latest`).

## Compose topology

Main services in `docker-compose.yml`:

- `postgres`
- `clamav`
- `downloader`
- `task-agent`
- `workspace-indexer`
- `reminder-scheduler`
- `deep-research-agent`
- `supervisor`
- `heartbeat`
- `ollama`
- `ollama-model-puller` (one-shot startup helper)
- `sandbox-init` (one-shot protected-file staging helper, no network or Docker socket)
- `sandbox`

Most Python roles share one image (`build: ./agent`) and are selected by `AGENT_ROLE`.

## Networks

- `database_net` (`internal: true`): agent roles and PostgreSQL only.
- `model_net` (`internal: true`): Ollama and roles that use embeddings.
- `clamav_net` (`internal: true`): ClamAV and artifact-scanning roles.
- `sandbox_net` (`internal: true`): sandbox broker and its authorized callers.
- `egress_net`: outbound provider access; bridge inter-container forwarding is disabled.
- `service_egress_net`: Ollama downloads and ClamAV signature updates only;
  bridge inter-container forwarding is disabled, and agent clients never join it.

Services join only the internal networks required by their role, plus `egress_net`
when they need outbound access. PostgreSQL has no egress attachment. Ollama and
ClamAV need egress for model downloads and signature updates; their separate
service egress bridge disables inter-container forwarding. Keeping this bridge
separate from agent egress ensures service DNS resolves through the clients'
scoped internal networks rather than a blocked shared egress address.
The sandbox broker is reachable only from `task-agent` over verified HTTPS with
a separate bearer token.

Per-command run containers are created by the broker on ephemeral per-run bridge
networks (not on application networks). They run as root for package installation,
receive only the shared workspace mount, and have no provider credentials.

## Volumes and mounts

Default bind mounts:

- `./data/postgres` → PostgreSQL data
- `./data/share` → shared workspace
- `./data/private/artifacts` → private inbound artifacts
- `./data/ollama` → Ollama model cache
- `./data/clamav` → ClamAV signatures/state
- `./config` (read-only) → `/app/config`
- PostgreSQL and sandbox service certificates (read-only) → agent clients
- PostgreSQL and sandbox private keys (read-only) → only their respective servers
- Selected `.secrets` files (read-only) → `/run/secrets` in authorized services

The common config and public trust mounts use anchors reused by every agent-role
service, including services with their own volume lists. File binds use
`create_host_path: false`: missing certificates/configuration fail startup instead
of creating directories. Run setup before Compose. Public certificates are `0644`;
private keys and credential files remain `0600` under private directories.

The `sandbox-init` helper reads only the sandbox key, certificate and token. It
validates TLS identity and stages a complete generation into the project-scoped
`sandbox-runtime` volume as root-owned protected files. Only the helper has
`DAC_READ_SEARCH`; it has no network, workspace or Docker socket. The broker mounts
the runtime volume read-only and retains `cap_drop: ALL`. No host ownership changes
or ACL repair are required. The runtime volume is a derived plaintext credential
copy protected by Docker, not encryption or an authoritative backup.

Setup startup and token activation stop the broker before staging, then recreate
and probe it. Failed staging leaves the broker stopped and activation pending.
For manual upgrades, run the updated setup's **Review and save** before startup so
the generated overlay grants the token to `sandbox-init`, not directly to the
broker. The managed Shadow deployment wrapper performs this save as the setup
owner, retaining existing credentials, before staging. Rotate tokens through setup;
plain container restart does not stage changed
host files. Unchanged saves preserve protected file inodes.

## Compose anchors and reuse

`docker-compose.yml` uses anchors to avoid duplication:

- `x-agent-secrets`: legacy-named shared non-secret deployment overrides
- `x-agent-config-volume`: read-only config bind mount
- `x-agent-common`: shared build/restart/environment/network defaults

Role services (`task-agent`, `downloader`, `supervisor`, etc.) extend `x-agent-common` and override only role-specific fields.

The wizard's `.compose-secrets.yaml` removes migrated inline credentials from
container environments and grants mounts by service. Do not omit this overlay.
Compose does not encrypt host source files. PostgreSQL uses
`POSTGRES_PASSWORD_FILE`; changing it does not rotate an initialized database.
Use `./scripts/setup.sh secrets update` for coordinated updates. Activation
recreates affected running containers; a restart alone is insufficient.

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
- Follow [the upgrade guide](upgrading.md) before starting new code on existing data.
- If you change host paths, update all relevant bind mounts consistently.
- For gVisor (`runsc`), use a Compose override and validate networking carefully before production use.