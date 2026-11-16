# Docker setup

The Compose stack is defined in `src/docker-compose.yml`. Tracked defaults and
templates live under `src/`; mutable configuration and runtime data live under
the ignored `data/` directory.

Install with the root installer (Docker Compose and a local Unix-socket Docker
context are required):

```bash
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/rb81/assistant/main/install.sh | sh
```

Then complete setup and start the application from the installation directory:

```bash
./run.sh
./run.sh server start
```

`./run.sh` administers the instance through Docker; host Python and OpenSSL are
not required. Remote Docker contexts are rejected because host bind mounts must
refer to the installation on the same machine. For remote administration, use
the desktop client's SSH tunnel.

Setup records the absolute Compose file list—including the generated credential
overlay—in `data/.env`. Keep `data/.compose-secrets.yaml` present and do not
replace its setup-generated `COMPOSE_FILE`. Rerun setup after moving the
installation to refresh absolute Compose and sandbox mount paths.

```bash
docker compose --env-file data/.env ps
docker compose --env-file data/.env logs --tail=100 task-agent
```

If you specify Compose files with `-f`, explicitly include
`src/docker-compose.yml` and `data/.compose-secrets.yaml` before your override.

## Troubleshooting local administration

If the setup interface will not open, check Docker in the same terminal:

```bash
docker info
docker version
docker context ls
```

Start the selected Docker runtime and retry. Check `DOCKER_HOST` and
`DOCKER_CONTEXT` if the endpoint is unexpected. Sudo normally is not needed for
Docker Desktop and does not fix a stopped daemon.

If a Docker credential volume is missing, select the original daemon or restore
its backup; do not create an empty replacement. For registry credential-helper
errors, repair the helper installation/PATH, unlock the macOS keychain where
applicable, and renew `docker login` if needed. The setup helper may need to pull
`python:3.12-slim`.

For Compose configuration errors, run
`docker compose --env-file data/.env config --quiet` from the installation root.
Review and redact Docker output before sharing it: logs may reveal credentials
or private paths. Diagnostics and retries do not recreate credential volumes or
reset application data.

## Services and profiles

The stack includes management, a restricted Docker reader, PostgreSQL, task
agents, Ollama, ClamAV, the sandbox broker, and setup helpers. Email intake,
Telegram, and voice transcription are enabled with their Compose profiles when
selected during setup. See [architecture](architecture.md).

Only management publishes a host port, bound to loopback at `127.0.0.1:8765`
by default. Set `ASSISTANT_MANAGEMENT_PORT` in `data/.env` and recreate
management to change it. There is no browser dashboard; API endpoints require
authentication. See [management connections](management-api.md).

## Networks and boundaries

Compose scopes database, model, malware-scanning, transcription, and sandbox
traffic to separate networks. PostgreSQL has no egress. The sandbox broker uses
authenticated HTTPS on its internal control network; command containers use
per-run internal networks and an HTTPS proxy restricted to approved hosts.
`SANDBOX_EGRESS_MODE=open` in `data/.env` restores direct internet access.
See [sandbox network isolation](sandbox.md#network-isolation).

Only the restricted Docker reader mounts the Docker socket for management
status, inspect, and bounded log reads; it cannot create containers or perform
other mutations. The sandbox broker also mounts the socket to create and remove
command containers and therefore has daemon-level authority. This is a
significant host trust boundary; see
[sandbox Docker socket](security.md#sandbox-docker-socket). Do not flatten the
existing isolation networks.

Command containers receive the shared workspace, not application credentials or
the Docker socket. Management mounts the installation read-only and `data/`
writable for token creation. Bind paths remain identical inside and outside
administration containers so nested Compose uses the correct host paths.

## Persistent mounts

Important host state under `data/` includes:

| Path | Contents |
|---|---|
| `data/postgres/` | Database |
| `data/share/` | Shared workspace |
| `data/private/` | Artifacts and TLS files |
| `data/ollama/` | Embedding model cache |
| `data/clamav/` | Malware signatures |
| `data/config/AGENT.md` | Runtime instructions |

`data/config/` is mounted read-only at `/app/config`; application defaults are
baked into each image from `src/config/agent.yaml`. Override supported settings
in `data/overrides.env`.

Documentation is bundled at `/app/docs` for the `help_search` and `help_topic`
tools; it is not mounted from the host or made available in the shared
workspace, broker, or command containers. Rebuild the application image to
update bundled help.

Credentials are protected plaintext host files in `data/.secrets/` by default,
or can use the dedicated external Docker volume. The generated Compose overlay
mounts them read-only only into authorized services. Neither option encrypts
credentials. Keep `data/.compose-secrets.yaml` and
`data/.secrets/state.json`; back up an external credential volume separately.
The sandbox certificate/key/token are staged in a protected Docker volume;
command containers receive none of them. See
[optional Docker volume storage](security.md#optional-docker-volume-storage).

## Sandbox defaults

Compose applies container isolation, a read-only broker filesystem, dropped
capabilities, `no-new-privileges`, and CPU, memory, and PID limits to command
containers. Run containers drop `NET_RAW`. The Docker socket is not mounted in
workers, command containers, or the management API. The broker still has socket
access and residual container-escape risk; see [security](security.md).

`AGENT_SANDBOX_ENABLED=false` disables the agent command tool only. It does not
remove the broker or its Docker socket mount.

## Maintenance

Before upgrading to version-bound script permissions, save setup again so
revoke-only credentials and service-specific grants are generated. Restarting
alone does not apply the change. See the
[upgrade and acceptance checklist](sandbox.md#upgrade-and-acceptance-checklist).

Rebuild and recreate after source changes:

```bash
docker compose --env-file data/.env up -d --build
```

Recreate after environment-file changes; restart alone does not reload
environment values:

```bash
docker compose --env-file data/.env up -d --force-recreate
```

Stop services without deleting persistent bind mounts:

```bash
docker compose --env-file data/.env down
```

Avoid `down --volumes` unless intentionally deleting managed volumes, including
transcription and sandbox runtime volumes. Back up `data/` as confidential data.
External credential volumes are not deleted by `down --volumes`, but explicit
removal or pruning can delete them.
