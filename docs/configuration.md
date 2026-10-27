# Configuration

## Setup

Install Python 3.12+ and Docker Compose, then run the repository launcher from
the repository root:

```bash
./setup.sh
```

The launcher resolves the repository root and forwards its arguments to
`src/scripts/setup.sh`; invoke it by its path if starting outside the repository.
The setup wizard creates or updates local configuration only after review and
save. Common section commands include:

```bash
./setup.sh --section email
./setup.sh --section calendar
./setup.sh --section telegram
./setup.sh secrets status
./setup.sh secrets update OPENROUTER_API_KEY
```

Use the wizard for managed settings and credentials. Do not edit tracked
defaults to configure an installation.

## Local and tracked files

| Path | Purpose |
|---|---|
| `src/config/agent.yaml` | Tracked application defaults |
| `src/config/AGENT.md.example` | Example agent instructions |
| `data/.env` | Ignored deployment settings; includes an absolute `COMPOSE_FILE` |
| `data/overrides.env` | Optional ignored expert runtime overrides |
| `data/.secrets/` | Ignored credential files and setup metadata |
| `data/.compose-secrets.yaml` | Generated ignored Compose secret mounts |
| `data/config/AGENT.md` | Local runtime instructions |
| `data/.setup.lock`, `data/.venv-setup`, `data/backups/` | Setup state, environment, and local backups |

Other mutable service state, including PostgreSQL, private files and the shared
workspace, remains under `data/`. Do not commit any contents of that directory.

The Compose stack reads `data/.env`; setup records the absolute stack path there.
Start the stack from the repository root with:

```bash
docker compose --env-file data/.env up -d --build
```

## Overrides

The setup wizard manages routine settings. For supported expert settings, add
one `NAME=value` per line to `data/overrides.env`. Compose loads this after
`data/.env`, but a Compose `environment:` entry takes precedence. Do not put
credentials in the overrides file. Use the `secrets` commands instead.

Setting values and their environment names are defined alongside defaults in
`src/config/agent.yaml` and the configuration loader. See
[environment overrides](env-overrides.md) for the documented override policy.
After a configuration change, recreate affected services; setup provides the
appropriate activation workflow.

## Credentials and safety

Setup stores credentials in protected, gitignored files and grants only the
services that need them read-only mounts. Docker Compose secret mounts are not
encrypted storage. Protect the host, Docker daemon, disks, and backups. Never
put credentials in the shared workspace, tracked files, shell arguments, or
logs. See [security](security.md#docker-secrets).

Use a dedicated assistant mailbox. Configure sender and recipient restrictions
before enabling email intake or outbound email. See [email administration](email-administration.md)
and [security](security.md).

## Integrations

- [Calendar and Zoom](calendar-providers.md)
- [Dropbox](dropbox.md)
- [Telegram and voice](telegram-and-voice.md)
- [Memory and embeddings](memory-system.md)
- [OpenRouter](openrouter.md)

The project is a prerelease. A separate legacy conversion bundle is published
at the [layout transition release](https://github.com/rb81/assistant/releases/tag/layout-transition).
