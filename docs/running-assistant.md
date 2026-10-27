# Running Assistant

## Requirements and setup

Use Docker Compose and Python 3.12 or newer for the setup wizard:

```bash
./setup.sh
```

The root launcher resolves the repository path; invoke it by its path if
starting outside the repository. Configure identity, model
access, and any enabled integrations in the wizard. It stores mutable settings,
credentials, setup state, and runtime data beneath the gitignored `data/`
directory. See [configuration](configuration.md).

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

## Data locations

- `data/postgres/` — database files.
- `data/share/` — shared files accessible to agent file tools and sandbox runs.
- `data/private/` — artifacts, TLS material, and other private state.
- `data/ollama/` — local embedding model cache.
- `data/clamav/` — malware signatures.
- `data/.env`, `data/overrides.env`, `data/.secrets/` — local configuration and credentials.
- `data/config/AGENT.md` — operator-maintained runtime instructions.

Tracked templates and defaults are under `src/config/`. Host documentation is
mounted read-only into the runtime at `/data/share/.assistant/docs`; it is not
copied into the shared workspace. The agent can use it for tool and environment
diagnostics but cannot use it to inspect host secrets, host source files, or
restart services.

Back up the database, shared files, private files, and local configuration and
credentials. Stop the stack before taking filesystem copies that must be
consistent. Keep backups protected as confidential data.

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
