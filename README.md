<p align="center">
  <img src="docs/assets/assistant-wide.png" alt="Assistant">
</p>

<h1 align="center">Assistant</h1>

<p align="center">
  Your own AI assistant. Your server. Your choice of models.
</p>

Assistant is a self-hosted AI assistant that researches, works with files, runs
code, and helps manage your day. Talk to it through email, Telegram, or the
[macOS app](https://github.com/rb81/assistant-client). The server runs in Docker
and keeps working when you close the client.

Upgrading from V1? Start with the
[layout transition guide](https://github.com/rb81/assistant/releases/tag/layout-transition).

## What it can do

| Feature | What it does |
|---|---|
| **Research and writing** | Answer questions, investigate topics, and produce reports. |
| **Files** | Read documents, process attachments, and create files. |
| **Code execution** | Run scripts in an isolated sandbox with approval controls. |
| **Reminders and calendars** | Schedule reminders and connect calendars and meeting services. |
| **Memory** | Organize related work and retain searchable context. |
| **Channels** | Use email, Telegram with voice support, or the macOS app. |

## Requirements

| Requirement | Details |
|---|---|
| **macOS or Linux** | Install and start Docker and Docker Compose. Your account needs Docker access without `sudo`. |
| **curl and tar** | No host Python, Node, Rust, or Git installation is needed. |
| **Model API key** | Use OpenRouter or another supported OpenAI-compatible provider. Usage may incur charges. |
| **Internet access** | Required for installation and connected services. |
| **Optional integrations** | Email and Telegram need separate credentials. The desktop client requires macOS 14 or later. |

## Install

Run the [installer](install.sh) in a terminal:

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/rb81/assistant/main/install.sh | sh
```

The installer downloads Assistant, builds its Docker images, and starts the
Management API and database. The first build can take several minutes.

| Next step | What to do |
|---|---|
| **Open setup** | Run the launcher command printed by the installer. Use a terminal at least 76 columns by 24 rows. |
| **Configure and start** | Set your model, API key, and standing instructions. Choose **Review & save**, then **Save & start**. To start later, choose **Save configuration**, then **Services > Start app** when ready. |
| **Connect from macOS** | Follow the [client connection guide](https://github.com/rb81/assistant-client#connect) to chat or manage the server from the desktop app. |

## Before you connect your accounts

> [!WARNING]
> **Use an assistant-owned mailbox, not your personal inbox.** Email intake can
> move messages, and starting workers may process mail already present.
> Restrict allowed senders and outgoing recipients.

| Boundary | What to know |
|---|---|
| **Model providers and integrations** | Self-hosting does not keep all processing on your server. Tasks may send content to the services you configure. |
| **Permissions** | Start with narrowly scoped accounts and review approval requests. Sandbox infrastructure requires privileged Docker access. |
| **Administration** | Keep the Management API off public networks and use SSH for remote access. Protect credentials and backups; credential storage is not encrypted by Assistant. |

## Documentation

| Guide | What it covers |
|---|---|
| **[Documentation index](docs/README.md)** | All setup, integration, and development guides. |
| **[Running Assistant](docs/running-assistant.md)** | Installation options, updates, backups, and troubleshooting. |
| **[Configuration](docs/configuration.md)** | Settings, standing instructions, and credentials. |
| **[Security](docs/security.md)** | Account isolation, permissions, approvals, and trust boundaries. |
| **[Build a client](docs/building-clients.md)** | Connect an independent client to the Management API. |

[Apache 2.0 license](LICENSE)
