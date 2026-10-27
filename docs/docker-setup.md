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
do not replace the setup-generated value. To inspect services:

```bash
docker compose --env-file data/.env ps
docker compose --env-file data/.env logs --tail=100 task-agent
```

## Services and profiles

The stack includes PostgreSQL, agent workers, Ollama, ClamAV, the sandbox
broker, and one-shot setup helpers. Email intake, Telegram, and voice
transcription are enabled with their corresponding Compose profiles when
selected during setup. See [architecture](architecture.md) for role summaries.

## Networks and boundaries

Compose separates database, model, malware-scanning, transcription, and sandbox
control traffic into scoped networks. Services needing external providers also
use egress. PostgreSQL is not attached to egress. The sandbox broker uses
authenticated HTTPS on its internal control network; command containers are
created per request on separate bridge networks.

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
- `data/config/AGENT.md` — runtime prompt.

The host `docs/` directory is mounted read-only at
`/data/share/.assistant/docs` for task-agent, sandbox broker, and per-command
containers. It is not copied into workspace storage. The agent may read
reference documentation for diagnostics, but this does not expose host source,
credentials, or service controls.

Credential source files and the generated Compose overlay are local plaintext
files protected by host permissions; Docker Compose does not encrypt them.
Credentials are mounted read-only only into authorized services. The sandbox
broker's certificate/key/token are staged by `sandbox-init` in a protected
Docker volume; command containers receive no credentials.

## Sandbox defaults

The Compose default uses container isolation, a read-only broker filesystem,
dropped broker capabilities, `no-new-privileges`, and CPU, memory, and PID
limits for command containers. Run containers drop `NET_RAW`; the Docker
socket is mounted only in the broker. The broker's privileges and residual
container-escape risk are described in [security](security.md).

## Maintenance

Rebuild and recreate after changing tracked source or configuration:

```bash
docker compose --env-file data/.env up -d --build
```

Stop without deleting persistent bind mounts:

```bash
docker compose --env-file data/.env down
```

Avoid `down --volumes` unless intentionally deleting managed volumes, including
the transcription model/runtime and sandbox credential runtime volumes. Back up
`data/` and protect the copies as confidential.
