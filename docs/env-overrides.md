# Environment overrides

The setup wizard manages routine application settings and credentials. Use the
ignored `data/overrides.env` file only for non-secret settings that setup does
not manage.

Setup creates a commented template on first save, listing supported expert
settings with their types and defaults at creation time. It never rewrites
that file; after upgrading, check the current loader and setup policy for
changes. Uncomment the settings you need, for example:

```env
AGENT_LLM_TEMPERATURE=0.3
AGENT_SANDBOX_ENABLED=false
```

Use the exact environment variable names and types supported by the current
configuration loader in `src/agent/src/assistant_agent/config.py`. Application
runtime defaults are in `data/config/agent.yaml`, initialized from
`src/config/agent.yaml`. Not every loader setting is permitted here: setup
rejects unknown, mistyped, wizard-managed, and Compose-managed keys at review.
Use the wizard for models, integrations, and the limits it manages.

For application containers, precedence is: Compose `environment:` entries,
then `data/overrides.env`, then `data/.env`, then `data/config/agent.yaml`, then
code fallback defaults. Use `true`/`false` for booleans and comma-separated
values for lists. Blank values generally leave the YAML value unchanged; they
do not reliably disable or clear a setting.

The overrides file does **not** supply Compose interpolation variables. Put
deployment settings such as `TASK_AGENT_WORKERS`, `COMPOSE_PROFILES`, and
`SANDBOX_ISOLATION_MODE` in `data/.env`, not here. Do not put credentials in the
overrides file; update them with `./run.sh secrets update NAME`.
`AGENT_SANDBOX_ENABLED=false` disables the command tool, but does not stop the
Compose sandbox service or remove its Docker socket mount.

After editing settings, recreate the affected services with the setup workflow
or:

```bash
docker compose --env-file data/.env up -d --force-recreate
```

The root `run.sh` resolves the repository path; invoke it by its path if
starting elsewhere. Compose commands should be run from the repository root
with `--env-file data/.env`.
See [configuration](configuration.md) for local files and
[security](security.md#docker-secrets) for credential handling.
