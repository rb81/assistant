# Advanced configuration

Use the management forms for normal configuration:

```bash
./run.sh
./run.sh --section advanced
```

The interface manages identity, models, email, integrations, safety options, and
common limits. Credentials are entered through masked fields, not stored in
`data/.env`. Use `data/overrides.env` only for supported expert settings that
the forms do not manage; see [configuration](configuration.md) and
[environment overrides](env-overrides.md).

## Models and limits

Runtime model defaults are in `data/config/agent.yaml`, initialized from
`src/config/agent.yaml`.
Setup allows primary/fallback model selection and supported provider
configuration through `--section models`. Use `--section advanced` for per-task
iteration/token limits, per-call output limits, and maximum task duration.
These are not a spending cap; configure account spending limits with the model
provider. The optional model preset tool has its own shared rolling USD
threshold, checked before each request; the final request can exceed it and it
does not cover ordinary task-agent model calls.

## Email and integrations

Email intake is optional and enabled through setup. Use a dedicated mailbox,
restrict actionable senders and recipient domains, and review
[email administration](email-administration.md) before relying on autonomous
email processing.

Configuration guides for optional services:

- [Calendar and Zoom](calendar-providers.md)
- [Dropbox](dropbox.md)
- [Telegram and voice](telegram-and-voice.md)
- [Memory and embeddings](memory-system.md)

## Scaling and runtime

Set `TASK_AGENT_WORKERS` in `data/.env` (not `data/overrides.env`) to configure
the task-agent replica count, then apply it from the repository root:

```bash
docker compose --env-file data/.env up -d task-agent
docker compose --env-file data/.env ps task-agent
```

Workers claim queued jobs through PostgreSQL. Increasing replicas also increases
model-provider usage and database load. See [concurrent job processing](concurrent-job-processing.md).

Compose service, network, mount, and sandbox settings are in
`src/docker-compose.yml`; runtime application defaults are in `data/config/agent.yaml`.
Keep local modifications in ignored data files or an external Compose override,
not in tracked defaults. See [Docker setup](docker-setup.md) and
[security](security.md).
