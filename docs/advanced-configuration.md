# Advanced configuration

Use `./run.sh` for routine settings and credentials. Use the supported expert
settings in `data/overrides.env` only when the setup forms do not expose what you
need. Keep deployment settings in `data/.env` and credentials in the protected
secret store. See [configuration](configuration.md) and
[environment overrides](env-overrides.md).

## Models and limits

- Choose the primary and fallback task models in `./run.sh --section models`.
  The fallback is tried once only for HTTP 429, 503, or 529 responses.
- Use `./run.sh --section advanced` for per-task iteration/token limits,
  per-call output limits, and maximum task duration.
- Configure provider-side spending limits separately. Task limits are not
  spending caps.
- The optional model preset and text-to-speech tools share a rolling USD budget.
  It is checked before each request, so the final admitted request can exceed
  the threshold. Ordinary task-agent, research, and web-search requests are not
  covered.

## Email and integrations

Email intake is optional. Before enabling it, use a dedicated mailbox, restrict
actionable senders and outbound recipient domains, and review
[email administration](email-administration.md).

Guides: [Calendar and Zoom](calendar-providers.md), [Dropbox](dropbox.md),
[Telegram and voice](telegram-and-voice.md), and
[memory and embeddings](memory-system.md).

## Scaling and runtime

Set `TASK_AGENT_WORKERS` in `data/.env` to change the task-agent replica count;
this is Compose configuration, not an application override. Apply it from the
repository root:

```bash
docker compose --env-file data/.env up -d task-agent
docker compose --env-file data/.env ps task-agent
```

Workers claim queued jobs through PostgreSQL. More workers increase both
provider usage and database load. See
[concurrent job processing](concurrent-job-processing.md).

Compose services, networks, mounts, and sandbox settings are defined in
`src/docker-compose.yml`; application defaults are in
`src/config/agent.yaml`. Keep local changes in ignored data files or an external
Compose override, not tracked defaults. See [Docker setup](docker-setup.md) and
[security](security.md).
