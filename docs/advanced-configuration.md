# Advanced configuration

Use the setup wizard for normal configuration:

```bash
./setup.sh
./setup.sh --section advanced
```

The wizard manages identity, models, email, integrations, safety options, and
common limits. Credentials are entered through protected prompts, not stored in
`data/.env`. Use `data/overrides.env` only for supported expert settings that
the wizard does not manage; see [configuration](configuration.md) and
[environment overrides](env-overrides.md).

## Models and limits

The default model endpoint and model choices are in `src/config/agent.yaml`.
Setup allows primary/fallback model selection and supported provider
configuration. Per-task iteration and token limits constrain agent work; model
provider spending limits are separate and must be configured with the provider.

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

`TASK_AGENT_WORKERS` configures the task-agent replica count in Compose.
Workers claim queued jobs through PostgreSQL. Increasing replicas also increases
model-provider usage and database load. See [concurrent job processing](concurrent-job-processing.md).

Compose service, network, mount, and sandbox settings are in
`src/docker-compose.yml`; application defaults are in `src/config/agent.yaml`.
Keep local modifications in ignored data files or an external Compose override,
not in tracked defaults. See [Docker setup](docker-setup.md) and
[security](security.md).
