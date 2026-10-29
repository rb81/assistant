# Docker setup

The Compose definition is `src/docker-compose.yml`. It builds the agent,
sandbox, and transcription images from sibling directories under `src/`.
Tracked defaults and templates are under `src/config/`; mutable deployment
configuration and runtime state are stored in the repository's ignored `data/`
directory.

Run setup first, then use the root Compose environment file:

```bash
./setup.sh
docker compose --env-file data/.env up -d --build
```

Setup records the absolute Compose file list in `data/.env`, including the
generated credentials overlay. Keep `data/.compose-secrets.yaml` present and
do not replace the setup-generated value. Rerun setup after moving the checkout
to refresh absolute Compose and sandbox mount paths. To inspect services:

```bash
docker compose --env-file data/.env ps
docker compose --env-file data/.env logs --tail=100 task-agent
```

For a custom Compose override, explicit `-f` options replace `COMPOSE_FILE`.
Include `-f src/docker-compose.yml -f data/.compose-secrets.yaml` before your
override file so the base services and credential mounts are retained.

## Services and profiles

The stack includes PostgreSQL, agent workers, Ollama, ClamAV, the sandbox
broker, and one-shot setup helpers. Email intake, Telegram, and voice
transcription are enabled with their corresponding Compose profiles when
selected during setup. See [architecture](architecture.md) for role summaries.
No application ports are published, and the current stack has no `api` profile,
browser dashboard, or management API.

## Networks and boundaries

Compose separates database, model, malware-scanning, transcription, and sandbox
control traffic into scoped networks. Services needing external providers also
use egress. PostgreSQL is not attached to egress. The sandbox broker uses
authenticated HTTPS on its internal control network; command containers are
created per request on separate internal networks and reach the internet only
through a per-run HTTPS egress proxy restricted to approved hosts
(`SANDBOX_EGRESS_MODE=open` in `data/.env` restores direct access). See
[sandbox network isolation](sandbox.md#network-isolation).

Each command container receives the shared workspace, not application
credentials or the Docker socket. The broker itself requires the Docker socket
to create and remove command containers and therefore has daemon-level
authority. This remains a significant host trust boundary; see
[security](security.md#sandbox-docker-socket).

## Persistent mounts

Compose keeps host state under `data/`, including:

- `data/postgres/` — database.
- `data/share/` — shared workspace.
- `data/private/` — artifacts and TLS files.
- `data/ollama/` — embedding model cache.
- `data/clamav/` — malware signatures.
- `data/config/agent.yaml` — runtime defaults (copied from tracked defaults if absent).
- `data/config/AGENT.md` — runtime prompt.

`data/config/` is mounted read-only at `/app/config` in application containers.
Setup preserves an existing `data/config/agent.yaml`; rebuilding images does
not refresh it from `src/config/agent.yaml`.

The host `docs/` directory is mounted read-only at
`/data/share/.assistant/docs` for task-agent, sandbox broker, and per-command
containers. It is not copied into workspace storage. The agent may read
reference documentation for diagnostics, but this does not expose host source,
credentials, or service controls.

By default, credential source files are protected plaintext files in
`data/.secrets/`; the generated `data/.compose-secrets.yaml` grants read-only
mounts only to authorized services. Setup can instead move credentials to a
dedicated external Docker volume with service-specific mounts. Neither store
is encrypted; retain the overlay and `data/.secrets/state.json` in either mode.
See [credential storage](security.md#optional-docker-volume-storage). The sandbox
broker's certificate/key/token are staged by `sandbox-init` in a protected
Docker volume; command containers receive no credentials.

## Sandbox defaults

The Compose default uses container isolation, a read-only broker filesystem,
dropped broker capabilities, `no-new-privileges`, and CPU, memory, and PID
limits for command containers. Run containers drop `NET_RAW`; the Docker
socket is mounted only in the broker. The broker's privileges and residual
container-escape risk are described in [security](security.md).
Disabling `AGENT_SANDBOX_ENABLED` disables the agent's command tool only; it
does not remove the broker service or its Docker socket mount from Compose.

## Maintenance

When upgrading to version-bound script permissions, save setup again before
rebuilding so revoke-only credentials and service-specific secret grants are
generated. A container restart alone does not activate the change. See the
[upgrade and acceptance checklist](sandbox.md#upgrade-and-acceptance-checklist).

Rebuild and recreate after changing application source:

```bash
docker compose --env-file data/.env up -d --build
```

For environment-file changes, recreate containers to reload their environment
(a restart alone is insufficient):

```bash
docker compose --env-file data/.env up -d --force-recreate
```

Stop without deleting persistent bind mounts:

```bash
docker compose --env-file data/.env down
```

Avoid `down --volumes` unless intentionally deleting managed volumes, including
the transcription model/runtime and sandbox credential runtime volumes. Back up
`data/` and protect the copies as confidential. If enabled, back up the external
credential volume separately; `down --volumes` does not delete that external
volume, but explicit removal or volume pruning can.
