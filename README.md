<p align="center">
  <img src="docs/assets/logo.png" alt="Assistant" width="160">
</p>

<h1 align="center">Assistant</h1>

<p align="center">
  Your own AI assistant. Your server. Your choice of models.
</p>

Assistant is a self-hosted AI assistant that researches, works with files, runs
code, and helps manage your day. Talk to it through email, Telegram, or the
[macOS app](https://github.com/rb81/assistant-client). It runs in Docker on
your computer or server and keeps working while your client is closed.

> **V2 is a prerelease.** Back up your data and test before relying on it for
> important work. Upgrading from V1? Start with the
> [transition guide](https://github.com/rb81/assistant/releases/tag/layout-transition).

## What it can do

- **Research and write:** ask questions, investigate topics, and produce reports.
- **Work with files:** read documents, process attachments, and create new files.
- **Run code:** execute scripts in an isolated sandbox, with approval controls.
- **Manage your day:** set reminders and connect calendars and meetings.
- **Keep context:** organize related work and retain searchable memories.
- **Meet you where you are:** use email, Telegram with voice support, or the
  native macOS app.

For more advanced use, configure models for different jobs, schedule scripts,
connect Dropbox, run concurrent workers, or build your own client using the
[Management API](docs/building-clients.md).

## Requirements

- **macOS or Linux**, with Docker and Docker Compose installed and running.
  Your account needs Docker access without `sudo`.
- **curl and tar.** No host Python, Node, or Rust installation is needed.
- **A model API key** from OpenRouter or another supported OpenAI-compatible provider.
  Provider usage may cost money.
- **Internet access** for installation and connected services.

Email and Telegram need their own account/bot credentials if enabled.
The optional macOS app requires macOS 14 or later; the server can run elsewhere.

## Install

Copy and paste this into a terminal:

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/rb81/assistant/main/install.sh | sh
```

The [installer](install.sh) downloads Assistant, builds its Docker images, and
starts management services. The default location is `~/.local/share/assistant`.
The first build can take several minutes.

Then open setup:

```sh
"$HOME/.local/bin/assistant"
```

Follow the prompts to configure your assistant, models, and connections.
Review and save your settings, then choose **Services > Start app**.
The terminal interface needs at least 76 columns and 24 rows.

Prefer a desktop interface? Use
[Assistant for macOS](https://github.com/rb81/assistant-client) to connect to
a local or remote installation, chat, and manage settings, services, and files.

For custom install locations, updates, backups, and troubleshooting, see
[Running Assistant](docs/running-assistant.md).

## Before you connect your accounts

> [!WARNING]
> **Use an assistant-owned mailbox, not your personal inbox.** Email intake can
> move messages, and starting workers may process mail already present.
> Restrict allowed senders and outgoing recipients.

Self-hosted does not mean fully local: task content may be sent to your chosen
model providers and integrations. Assistant can send messages, change connected
accounts, and execute code. Start with narrow permissions and review approvals.
The sandbox infrastructure has privileged Docker access.

Keep the Management API private; use SSH for remote administration. Credentials
and runtime state live under the gitignored `data/` directory, but credential
files are **not encrypted storage**. Protect and back up your installation.
Read the [security guide](docs/security.md) before using sensitive data.

## Documentation

Start with the **[documentation index](docs/README.md)** for setup, integrations,
configuration, and development.

- [Running Assistant](docs/running-assistant.md)
- [Configuration](docs/configuration.md)
- [Security](docs/security.md)
- [macOS client](https://github.com/rb81/assistant-client)
- [Build a client](docs/building-clients.md)

[Apache 2.0 license](LICENSE)
