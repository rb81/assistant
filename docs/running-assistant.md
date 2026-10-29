# Running Assistant

## Requirements and setup

Install Docker with Compose, Python 3.12+ with virtual-environment support, and
OpenSSL. Setup offers to create `data/.venv-setup` and install its dependencies.
You also need model API credentials and an email mailbox or Telegram bot for
task intake. From the repository root, run:

```bash
./setup.sh
```

The root launcher resolves the repository path; invoke it by its path if
starting outside the repository. Configure identity, model
access, and any enabled integrations in the wizard. It stores mutable settings,
credentials, setup state, and runtime data beneath the gitignored `data/`
directory. See [configuration](configuration.md).

Accept the wizard's offer to copy the example instructions, or create a
nonempty `data/config/AGENT.md` yourself before starting. The task agent refuses
to start without instructions.

## Start and inspect

Run Compose from the repository root, explicitly loading the local environment:

```bash
docker compose --env-file data/.env up -d --build
docker compose --env-file data/.env ps
docker compose --env-file data/.env logs -f task-agent
```

Setup writes an absolute `COMPOSE_FILE` setting in `data/.env`, pointing to
`src/docker-compose.yml` and the generated secrets overlay. The `email`,
`telegram`, and `voice` profiles are enabled by configuration when those
integrations are selected. Run `./setup.sh tools` for guided status, logs, and
administration.

Starting the stack enables workers and may process existing inbox messages.
If you move the checkout, rerun setup to refresh its absolute deployment paths.

## Data locations

- `data/postgres/` — database files.
- `data/share/` — shared files accessible to agent file tools and sandbox runs.
- `data/private/` — artifacts, TLS material, and other private state.
- `data/ollama/` — local embedding model cache.
- `data/clamav/` — malware signatures.
- `data/.env`, `data/overrides.env`, `data/.secrets/` — local configuration and credentials.
- `data/.compose-secrets.yaml` — generated credential mounts.
- `data/config/agent.yaml` — runtime defaults, initialized from `src/config/agent.yaml`.
- `data/config/AGENT.md` — operator-maintained runtime instructions.

Tracked templates and defaults are under `src/config/`. Host documentation is
mounted read-only into the runtime at `/data/share/.assistant/docs`; it is not
copied into the shared workspace. The agent can use it for tool and environment
diagnostics but cannot use it to inspect host secrets, host source files, or
restart services.

Back up the database, shared files, private files, and local configuration and
credentials. Stop the stack before taking filesystem copies that must be
consistent. If using Docker-volume credential storage, back up that external
volume separately along with `data/.secrets/state.json`; copying `data/` alone
does not include those credentials. See [credential storage](security.md#optional-docker-volume-storage).
Keep backups protected as confidential data.

### Factory reset

Run `./setup.sh reset` to permanently destroy and recreate the bundled PostgreSQL
cluster under `data/postgres/pgdata`. This deletes all databases and database
state in that cluster, including rows, indexes, ID sequences, and migration
history; startup recreates the schema and applies migrations. The reset makes
no backup and does not offer a selective data reset.

The reset preserves configuration and settings (`data/.env`, `data/overrides.env`,
`data/config/`), credentials (`data/.secrets/` or the optional credential Docker
volume), and files, including `data/config/AGENT.md`, `data/share/`, and
`data/private/`. It also leaves other persistent service data alone. Once
PostgreSQL is ready, the workspace indexer starts. If workspace indexing is
enabled in settings, it rebuilds its index from the preserved workspace files
in the background.

Factory reset refuses installations configured with a custom database; reset
that database separately. Type `RESET` at the confirmation prompt to proceed.

## Operations

```bash
docker compose --env-file data/.env down
docker compose --env-file data/.env up -d --build
docker compose --env-file data/.env logs --tail=100 task-agent
```

`down` does not remove bind-mounted `data/` files. Do not use
`down --volumes` unless intentionally deleting Compose-managed volumes such as
the transcription and sandbox runtime volumes. For service topology and trust
boundaries, see [architecture](architecture.md) and [Docker setup](docker-setup.md).
