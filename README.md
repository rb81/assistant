<p align="center">
  <img src="docs/assets/logo.png" alt="Assistant" width="200">
</p>

<h1 align="center">Assistant</h1>

<p align="center">
  <strong>Delegate the work. Keep the control.</strong><br>
  A self-hosted AI assistant that turns messages into finished work.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="Apache 2.0"></a>
  <img src="https://img.shields.io/badge/version-v2%20prerelease-orange.svg" alt="V2 prerelease">
  <img src="https://img.shields.io/badge/self--hosted-Docker%20Compose-2496ed.svg" alt="Docker Compose">
  <img src="https://img.shields.io/badge/python-3.12%2B-3776ab.svg" alt="Python 3.12+">
</p>

<p align="center">
  <a href="#get-started">Get started</a> &nbsp;·&nbsp;
  <a href="#work-that-goes-beyond-chat">Features</a> &nbsp;·&nbsp;
  <a href="docs/README.md">Documentation</a>
</p>

---

Most AI tools wait for your next prompt. **Assistant keeps working.** Send a task by email or private Telegram message. It can research, work with files, run code, manage meetings, and return the result through your conversation.

It runs on your infrastructure, remembers useful context, and keeps related work together. You choose the models, enable the integrations, and set the limits.

> **Try asking:** “Compare these proposals and send me a recommendation with a spreadsheet.”<br>
> “Research this market and prepare a briefing.”<br>
> “Find a free slot next week and create a meeting.”

## Work that goes beyond chat

| Capability | What it means for you |
|---|---|
| **Email and Telegram** | Delegate from a dedicated inbox or private bot. Optional local voice transcription turns Telegram voice messages into requests. |
| **Research that runs in the background** | Hand off substantial investigations while a separate research worker gathers findings and brings them back to the task. |
| **Files, documents, and code** | Process attachments, search workspace files, convert documents, produce deliverables, and run scripts in disposable sandbox containers. |
| **Memory with continuity** | Durable facts and expiring working context help it pick up where you left off. Review, edit, or erase memories when needed. |
| **Calendar, Zoom, and Dropbox** | Connect Google, Outlook, or Zoho calendars; manage Zoom meetings; exchange files through a private Dropbox App Folder. Each integration is opt-in. |
| **Reminders and ongoing work** | Schedule future tasks and recurring reminders. Workstreams preserve ownership and related history as work continues. |
| **Your models, your budget** | Configure primary and fallback models. Optional model presets support isolated text, image, and audio requests with a shared spending threshold. |
| **Operational visibility** | Guided setup, local diagnostics, logs, and deterministic email administration let you operate the harness without asking a model to manage itself. |

## How it works

```text
Email / Telegram
       |
       v
Request + workstream --> Task agent --> Models + tools
                              |             |
                              |       Research, files,
                              |       code, integrations
                              v
                    Results in your conversation
```

Independent workers handle intake, task execution, research, reminders, indexing, and supervision. PostgreSQL holds shared state; a private workspace holds files. The agent can consult its own **read-only harness documentation** when it needs to understand a tool or troubleshoot a limitation.

## Get started

**You need:** Docker with Compose, Python 3.12+ with virtual-environment support, OpenSSL, and model API credentials (OpenRouter is the default). For email, provide a **dedicated assistant mailbox** with IMAP/SMTP access. Telegram is an optional alternative channel.

```sh
git clone https://github.com/rb81/assistant.git
cd assistant
./setup.sh
```

The wizard walks through identity, models, channels, credentials, and optional integrations. It validates your choices and asks you to review before saving. It can then build and start the services.

Accept the example instructions or create a nonempty `data/config/AGENT.md` before starting. If you choose to start the services later, run from the repository root:

```sh
docker compose --env-file data/.env up -d --build
```

Send a task through your configured channel. For email administration, send `/admin` as the subject and follow the confirmation email to get help without invoking a model.

> [!IMPORTANT]
> **Use an assistant-owned mailbox, not your personal inbox.** Restrict actionable senders and recipient domains during setup. Models and enabled external providers receive the information needed for their requests; self-hosting does not make those calls local.

### Everyday controls

| Command | Purpose |
|---|---|
| `./setup.sh` | Configure a new installation or edit an existing one |
| `./setup.sh --section models` | Change model settings |
| `./setup.sh secrets status` | Check credential configuration |
| `./setup.sh secrets update OPENROUTER_API_KEY` | Replace a credential |
| `./setup.sh tools` | Inspect services, logs, jobs, memories, reminders, and workstreams |
| `./setup.sh reset` | Selectively reset data, with an optional backup and confirmation |
| `docker compose --env-file data/.env down` | Stop the stack without deleting persistent volumes |

## A clean home for everything

```text
src/           Application code, scripts, deployment files, and defaults
data/          Local settings, credentials, workspace, backups, and persistent state
docs/          Canonical human- and agent-readable documentation
.env.example   Settings reference; setup creates the real configuration
setup.sh       One entry point for setup and local administration
```

**`data/` is entirely gitignored.** Deployment settings live in `data/.env`, expert overrides in `data/overrides.env`, and runtime configuration in `data/config/agent.yaml` (initialized from tracked defaults). Credentials use protected `data/.secrets/` files or an optional dedicated Docker volume with service-specific mounts. Docker-managed volumes remain outside the repository; back up the credential volume separately if enabled.

## Built for deliberate autonomy

Assistant can take real actions. Start with narrow permissions and expand them intentionally.

- No application port is published by default; there is no browser dashboard or management API.
- Commands run in fresh sandbox containers, not directly on your host. The sandbox broker's Docker socket still grants high privilege: review the [security boundaries](docs/security.md).
- Email administration is restricted to the configured admin; optional sender authentication and decision-model screening add further controls.
- Secrets are scoped to the services that need them. Read-only documentation access does not expose host configuration or credentials.

**V2 is a prerelease.** Back up your data, review integration permissions, and verify behaviour before relying on it for consequential work.

## Documentation

Start at the **[documentation index](docs/README.md)** for task-based navigation.

| You want to… | Read |
|---|---|
| Install and operate Assistant | [Running Assistant](docs/running-assistant.md) |
| Configure models, limits, and integrations | [Configuration](docs/configuration.md) |
| Understand the harness | [Architecture](docs/architecture.md) · [Agent loop](docs/agent-loop.md) |
| Understand the available tools | [Tool reference](docs/tools-reference.md) |
| Check isolation, credentials, and permissions | [Security](docs/security.md) · [Sandbox](docs/sandbox.md) |
| Change the implementation | [Codebase guide](docs/codebase.md) |

Upgrading an older installation? Use the separate **[transition package and guide](https://github.com/rb81/assistant/releases/tag/layout-transition)** before starting the current code.

---

<p align="center">
  <strong>Your infrastructure. Your context. Work that keeps moving.</strong><br>
  <a href="LICENSE">Apache 2.0 licensed</a>
</p>
