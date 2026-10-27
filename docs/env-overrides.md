# Environment overrides

The setup wizard manages routine application settings and credentials. Use the
ignored `data/overrides.env` file only for non-secret settings that setup does
not manage.

```env
AGENT_LLM_TEMPERATURE=0.3
AGENT_SANDBOX_ENABLED=false
```

Use the exact environment variable names and types supported by the current
configuration loader in `src/agent/src/assistant_agent/config.py`. Application
defaults are in `src/config/agent.yaml`; Compose also defines deployment-level
settings in `src/docker-compose.yml`. These files are the source of truth for
the current build.

Compose loads `data/overrides.env` after `data/.env`. A Compose service's
`environment:` entry overrides both. Do not put credentials in the overrides
file; update them with `./setup.sh secrets update NAME`. After editing settings,
recreate the affected services with the setup workflow or:

```bash
docker compose --env-file data/.env up -d --force-recreate
```

The root `setup.sh` resolves the repository path; invoke it by its path if
starting elsewhere. Compose commands should be run from the repository root
with `--env-file data/.env`.
See [configuration](configuration.md) for local files and
[security](security.md#docker-secrets) for credential handling.
