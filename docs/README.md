# Documentation index

This is the canonical documentation set for the current Assistant version.
Use it as a task index; verify implementation details against the current source
when contributing. For project overview and quick start, see the
[root README](https://github.com/rb81/assistant#readme).

Source paths in contributor guides refer to a repository checkout. Runtime
containers receive this documentation, not the host source tree.

## For agents

1. Read the task-relevant guide below.
2. Read [tool reference](tools-reference.md) before using or describing tools.
3. Use `.assistant/docs/README.md` in the shared workspace for runtime
   environment and diagnostic guidance. It does not grant access to host files,
   secrets, or service controls.
4. See [sandbox capabilities](SANDBOX_CAPABILITIES.md) for command environment
   and diagnostic limits.

## For operators

- [Running Assistant](running-assistant.md) — setup, start, and local state.
- [Configuration](configuration.md) — configuration files and credential handling.
- [Advanced configuration](advanced-configuration.md) — model limits and worker scaling.
- [Environment overrides](env-overrides.md) — supported expert settings and precedence.
- [Security](security.md) — trust boundaries, approvals, and hardening.
- [Script and website permissions](sandbox.md#script-versions-and-run-permissions) —
  approval scopes, revocation, and an [upgrade/testing checklist](sandbox.md#upgrade-and-acceptance-checklist).
- [Docker setup](docker-setup.md) — images, Compose services, mounts, and networks.

## System and development

- [Architecture](architecture.md) — service topology and data flow.
- [Agent loop](agent-loop.md) — job lifecycle and execution.
- [Prompting](prompting.md) — prompt and context construction.
- [Tools reference](tools-reference.md) — available tool groups and constraints.
- [Extending tools](extending-tools.md) — implementation checklist.
- [Codebase guide](codebase.md) — source layout and tests.
- [Data model](data-model.md) — persistent records and ownership.
- [Concurrent jobs](concurrent-job-processing.md) — task-agent workers.

## Integrations and operations

- [Email and artifacts](email-and-artifacts.md) — inbound email and attachment processing.
- [Email administration](email-administration.md) — authenticated `/admin` commands.
- [Calendar providers](calendar-providers.md) — Google, Outlook, Zoho, and Zoom.
- [Dropbox](dropbox.md) — scoped file exchange.
- [Telegram and voice](telegram-and-voice.md) — private messaging and transcription.
- [Memory system](memory-system.md) — memory, embeddings, and workspace search.
- [OpenRouter](openrouter.md) — model and hosted-tool integration.
- [Sandbox](sandbox.md) — command execution boundaries.

The project is a prerelease. For the separate legacy conversion bundle, see
the [layout transition release](https://github.com/rb81/assistant/releases/tag/layout-transition).
