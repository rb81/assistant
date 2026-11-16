# Assistant documentation

New here? [Install Assistant](../README.md#install), then follow
[Running Assistant](running-assistant.md) to finish setup.

## Getting started

| Guide | What you need it for |
|---|---|
| [Running Assistant](running-assistant.md) | Setup, everyday controls, backups, and reset |
| [Configuration](configuration.md) | Settings, instructions, and credentials |
| [Security](security.md) | Account isolation, permissions, approvals, and trust boundaries |
| [macOS app](https://github.com/rb81/assistant-client) | Desktop installation, connections, and source builds |

## Integrations

| Guide | What it covers |
|---|---|
| [Email](email-and-artifacts.md) | Mailboxes, attachments, and replies |
| [Email administration](email-administration.md) | Authenticated `/admin` commands |
| [Telegram and voice](telegram-and-voice.md) | Bot setup, voice messages, and approvals |
| [Calendars and meetings](calendar-providers.md) | Google, Outlook, Zoho, and Zoom |
| [Dropbox](dropbox.md) | Scoped file import and export |
| [OpenRouter](openrouter.md) | Models, provider settings, and hosted tools |

## Operating Assistant

- [Advanced configuration](advanced-configuration.md): limits, models, and scaling.
- [Environment overrides](env-overrides.md): expert settings and precedence.
- [Docker setup](docker-setup.md): services, mounts, networks, and troubleshooting.
- [Sandbox](sandbox.md): code execution, network access, and script permissions.
- [Memory system](memory-system.md): memories, embeddings, and workspace search.

## Development and API reference

- [Codebase guide](codebase.md): layout, contribution, and tests.
- [Architecture](architecture.md) and [data model](data-model.md): system structure.
- [Build a client](building-clients.md) and [Management API](management-api.md):
  connection, schemas, reads, and safe writes.
- [Agent loop](agent-loop.md), [prompting](prompting.md), and
  [concurrent jobs](concurrent-job-processing.md): task execution.
- [Tools reference](tools-reference.md) and [extending tools](extending-tools.md):
  tool contracts and implementation.

## Runtime help

These pages ship with the application image. Agents should use `help_search`,
then `help_topic` with a returned topic ID and revision; use
`runtime_capabilities` for live settings and limits. Read the
[tools reference](tools-reference.md) before describing or invoking tools.
Help grants no access to host files, secrets, or service controls.

Source paths refer to the server repository, not a mounted host checkout.
V1 upgrade instructions live in the separate
[layout transition release](https://github.com/rb81/assistant/releases/tag/layout-transition).
