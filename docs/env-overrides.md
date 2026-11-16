# Configuration files and overrides

Use the management forms for routine configuration. For supported expert
settings, edit `data/overrides.env`; keep Compose/bootstrap settings in
`data/.env` and credentials in the protected secret store.

| File | Holds |
|---|---|
| `src/config/agent.yaml` | Shipped defaults for all settings; do not edit for a local installation |
| `data/overrides.env` | Non-secret application overrides |
| `data/.secrets/` or the Docker credential volume | Credentials, stored separately |
| `data/.env` | Compose and installation settings such as `COMPOSE_*`, `ASSISTANT_*`, and `TASK_AGENT_WORKERS` |

`src/config/agent.schema.yaml` describes setting types, secrets, and help text.
Defaults come from `agent.yaml`; schema entries without a matching default are
ignored and logged.

## Names are derived, never mapped

Convert a dotted YAML path to uppercase with dots replaced by underscores:
`agent.email.smtp.host` becomes `AGENT_EMAIL_SMTP_HOST`. The setting type comes
from its default, or from the schema when the default is blank. To add a setting,
define it in `agent.yaml` and add schema metadata if needed.

```env
AGENT_LLM_TEMPERATURE=0.3
AGENT_SANDBOX_ENABLED=false
```

Use `true`/`false` for booleans and comma-separated values for lists. Blank
values generally leave the default unchanged. Setup rejects unknown names,
invalid values, credentials, and deployment-only settings. The management forms
save supported application settings into this same file.

## Credentials

Values marked `secret: true` in the schema (for example
`AGENT_EMAIL_SMTP_PASSWORD`) belong in the credential store, never in either
environment file. Update one with `./run.sh secrets update NAME`. Additional
calendar accounts (`AGENT_CALENDAR_GOOGLE_2_CLIENT_ID`, for example) are the
dynamic exception: numbered accounts have no YAML leaf.

## Precedence

For application containers, explicit Compose `environment:` entries override
`data/overrides.env`, which overrides shipped `agent.yaml`. Code fallbacks must
match the shipped defaults; `src/scripts/audit_settings.py` checks this.
`overrides.env` does not provide Compose interpolation, so deployment settings
belong in `data/.env`. Setting `AGENT_SANDBOX_ENABLED=false` disables the
agent's command tool; it does not stop the sandbox service or remove the
Docker-socket mount.

## Checking an installation

Run `./run.sh config check` to check saved settings and live containers. Output
lists names, not values. It reports settings or credentials in `data/.env`,
unknown or mistyped overrides, credentials absent from `agent.yaml`, a leftover
`data/config/agent.yaml`, and containers that differ from the shipped defaults
plus overrides or cannot read a granted credential. Container checks require
the local CLI's Docker access.

`./run.sh management check` and `GET /v2/configuration/check` check saved files
only. `./run.sh config check` requests
`GET /v2/configuration/check?runtime=true`, delegating container checks to the
private executor. Inspect `runtime_checked` and `containers_checked`; a saved-file
check does not verify running services or provider access. The legacy v1 endpoint
remains saved-file-only.

## Upgrading older installations

The root `install.sh` runs scripts in `migrations/` before starting the server.
`001-derived-config-names.sh` renames older settings and credentials, moves
settings from `.env` to `overrides.env`, and keeps a backup in `data/backups/`.
It is safe to rerun.

`002-shipped-agent-yaml.sh` retires the old `data/config/agent.yaml` copy.
Changed values become overrides; values equal to the old shipped defaults are
dropped so current defaults apply. It backs up the old copy and stops without
changes if that copy contains a credential.

After changing settings, recreate affected services through setup or run:

```bash
docker compose --env-file data/.env up -d --force-recreate
```

See [configuration](configuration.md) and
[Docker secrets](security.md#docker-secrets).
