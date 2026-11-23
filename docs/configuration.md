# Configuration

## Setup

Follow [Running Assistant](running-assistant.md) to install and open setup.
The setup forms write configuration only after **Review & save** and
confirmation.
Saving settings and applying them to running services are separate steps.

In the terminal, use **Settings > Safety & Approvals > Approvals** for
approval delivery and the administrator email address. Use **Settings >
Models & Voice** for the conversation naming model.

Open a section directly when needed:

```bash
./run.sh --section email
./run.sh --section calendar
./run.sh --section telegram
./run.sh secrets status
./run.sh secrets update AGENT_LLM_OPENROUTER_API_KEY
```

Use the forms for managed settings and credentials. Do not edit tracked defaults
to configure an installation.

## Local and tracked files

| Path | Purpose |
|---|---|
| `src/config/agent.yaml` | Tracked application defaults, included in each image |
| `src/config/AGENT.md.example` | Example agent instructions |
| `data/.env` | Ignored deployment settings and Compose bootstrap, including the absolute `COMPOSE_FILE` |
| `data/overrides.env` | Optional ignored expert application overrides |
| `data/.secrets/` | Protected, ignored credential files and setup metadata |
| `data/.compose-secrets.yaml` | Setup-generated Compose secret mounts |
| `data/config/AGENT.md` | Local runtime instructions |
| `data/.setup.lock`, `data/backups/` | Setup locking and local backups |

Other runtime state, including PostgreSQL and the shared workspace, lives
under `data/`. Do not commit that directory. Defaults are read
from the shipped `src/config/agent.yaml`; there is no runtime copy. Review and
save the example instructions in Settings, or create a nonempty
`data/config/AGENT.md` before starting task agents.

Start or rebuild the stack from the installation root:

```bash
docker compose --env-file data/.env up -d --build
```

## Overrides

Put supported non-secret expert settings in `data/overrides.env`, one
`NAME=value` per line. Compose loads this file after `data/.env` into application
containers, but explicit Compose `environment:` entries take precedence. This
file is not used for Compose interpolation: settings such as
`TASK_AGENT_WORKERS` belong in `data/.env`.

Setup rejects unknown names, invalid types, credentials, and deployment-only
keys in application overrides. Managed forms also write to `overrides.env`;
the file is not limited to settings absent from the UI.
Never put credentials in either environment file; use the
`secrets` commands. Defaults and supported override rules are documented in
[configuration files and overrides](env-overrides.md). Recreate affected
services after changing runtime configuration.

## Credentials and safety

Setup stores credentials in protected, gitignored files and grants only
authorized services read-only mounts. Alternatively, select the dedicated
unencrypted Docker credential volume in **Settings > Server > Storage & recovery**.
Back up that volume separately and retain `data/.secrets/state.json`.
Compose secret mounts do not encrypt credentials; protect the host, Docker
daemon, disks, and backups. Never put credentials in the shared workspace,
tracked files, shell arguments, or logs. See [Docker secrets](security.md#docker-secrets).

Use a dedicated assistant mailbox. Set sender and recipient restrictions before
enabling email intake or outbound email. See
[email administration](email-administration.md) and [security](security.md).

## Integrations

- [Calendar and Zoom](calendar-providers.md)
- [Dropbox](dropbox.md)
- [Telegram and voice](telegram-and-voice.md)
- [Memory and embeddings](memory-system.md)
- [OpenRouter](openrouter.md)
- [Native model providers](model-providers.md)

Older schema/layout installations may need the separate
[layout transition release](https://github.com/rb81/assistant/releases/tag/layout-transition)
before current deployment migrations can run.
