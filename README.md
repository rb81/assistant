# Assistant — A Semi-Autonomous AI Personal Assistant

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

> **Give it a task. Walk away. Come back to results.**

Assistant is an open-source, self-hosted, semi-autonomous AI personal assistant that lives in your email inbox. Send it a message, and it gets to work — reading files, browsing the web, writing and running scripts, scheduling follow-ups, and emailing you back when it's done. No app to open, no chat window to babysit. Just email, like you already use every day.

It's built for people who want a capable AI agent they actually control: one that runs on their own infrastructure, keeps its data private, and can be configured precisely to match how much autonomy they're comfortable giving it.

---

## ⚡ Quick Start

**You'll need:**
- Docker and Docker Compose
- An [OpenRouter](https://openrouter.ai) API key
- An email address for yourself (to receive notifications and results)

```bash
# 1. Clone the repo
git clone https://github.com/rb81/assistant.git
cd assistant

# 2. Create your config files
cp .env.example .env
cp config/AGENT.md.example config/AGENT.md
```

**Edit `.env` — fill in these required values at minimum:**

```env
POSTGRES_PASSWORD=          # Generate one: openssl rand -base64 32
OPENROUTER_API_KEY=         # Your key from openrouter.ai
ADMIN_EMAIL=you@example.com # Where notifications and results go
SMTP_HOST=smtp.example.com  # Outbound mail server
SMTP_PORT=587
SMTP_FROM=assistant@example.com
```

**Enable the dashboard** (it's off by default for security — safe to turn on locally):

```env
AGENT_API_DASHBOARD_ENABLED=true
AGENT_API_WORKSPACE_ENABLED=true
```

> **Note:** If you are not configuring email (IMAP), the dashboard and/or workspace UI must be enabled — they are your only way to submit jobs and interact with the agent. Without email *and* without at least one UI surface active, there is no way to give the agent work to do.

```bash
# 3. Start the stack
docker compose up -d --build
```

**Open the dashboard:** [http://localhost:8000/admin](http://localhost:8000/admin)

Create your first job from the dashboard. No email configuration needed for this step. Watch it move from `queued` → `running` → `completed` and inspect the logs in the job detail view.

> **Want email-triggered jobs?** See [Adding Email](#adding-email-triggered-jobs) below, or jump to the [Advanced Configuration guide](docs/advanced-configuration.md).

---

## Adding Email-Triggered Jobs

> **Important:** The agent needs its **own dedicated email address and mailbox** — for example, `assistant@yourdomain.com`. Create a separate account for it with your email provider and configure IMAP/SMTP to point at that account. **Do not give the agent access to your personal email.** You communicate with the agent by sending messages *to* its address; results and notifications come back to you at your `ADMIN_EMAIL`.

Once SMTP is working, add IMAP credentials to `.env` to let the agent pick up tasks from its inbox:

```env
IMAP_HOST=imap.example.com
IMAP_PORT=993
IMAP_USERNAME=assistant@example.com
IMAP_PASSWORD=your-password

SMTP_USERNAME=assistant@example.com
SMTP_PASSWORD=your-password

# Safety guardrails — set these before enabling email
EMAIL_ACTIONABLE_SENDERS=you@example.com      # Only these addresses can trigger jobs
EMAIL_ALLOWED_RECIPIENT_DOMAINS=example.com   # Agent can only email these domains
```

Restart the stack (`docker compose restart`) and send an email to the agent's inbox. It will be picked up within the polling interval (default: 60 seconds).

> **Bottom line on email safety:** Set `EMAIL_ACTIONABLE_SENDERS` to your own address and `EMAIL_ALLOWED_RECIPIENT_DOMAINS` to your own domain. With those two in place, the agent can only be triggered by you and can only email people you've already trusted.

---

## What It Does

Once you trigger a task, the agent works autonomously — using a full suite of tools until the job is done, then sends you the result.

- **Schedules itself** — creates recurring reminders that wake it up in the future to rerun tasks, check on things, or continue work it started
- **Writes and runs code** — scripts run in a sandboxed, isolated Docker container, not on your host system
- **Multi-step projects** — complex tasks broken into ordered sequences where each step uses the output of the previous
- **Deep research** — asynchronous research loops with web search, file tools, and guidance checkpoints; pauses to ask for input when stuck
- **Persistent memory** — decisions, preferences, and context are remembered across sessions and injected into relevant jobs
- **Private notes and contacts** — separate from memory; searchable on demand
- **Semantic workspace** — a shared folder with full-text and semantic search, PDF/Office file extraction, format conversion
- **Calendar** — read/write CalDAV access with strict managed-event rules (opt-in, disabled by default)

---

## The Workspace

The built-in browser UI (`http://localhost:8000`) has two surfaces:

**`/workspace` — Your shared office**

A full-featured document environment you and the agent share. Anything you put here the agent can read, edit, and build on — and anything the agent creates you can open, review, and refine.

- **WYSIWYG editor** — rich-text editing (Milkdown) with Source and Preview toggles; auto-saves drafts every 30 seconds
- **File explorer** — create folders, rename, move, duplicate, and trash files
- **Uploads and file conversion** — drop in PDFs, Word docs, spreadsheets; the workspace indexer extracts text for semantic search automatically. Convert any document to Markdown, HTML, PDF, or DOCX from the file context menu.
- **CSV / table rendering** — tabular data rendered as a readable, interactive table
- **Workspace chat** — submit tasks directly from the workspace with the currently open file attached as context

**`/admin` — The control room**

Manage the agent's technical operations: create and monitor jobs, inspect execution logs, requeue stuck work, browse memories and contacts, view tool call history, and inject supervisor instructions into running jobs.

---

## How It Works

```
You → email (or workspace chat or dashboard)
        ↓
   Job is queued
        ↓
   Task agent claims the job
   → builds context (memory, thread history, prior actions)
   → enters tool loop (LLM + tools, up to configured limit)
   → reads/writes files, searches the web, runs scripts,
     sends emails, creates reminders, launches research...
        ↓
   Job completes → agent emails you back the result
```

The agent runs as a set of Docker services. Each concern has its own process: email ingestion, job execution, reminder scheduling, project orchestration, deep research, workspace indexing, supervision, and health monitoring. They all share a PostgreSQL database and a bind-mounted workspace directory.

For the full picture, see [docs/architecture.md](docs/architecture.md) and [docs/agent-loop.md](docs/agent-loop.md).

---

## ⚠️ Security and Autonomy

**The API has no built-in authentication.** It is designed for private-network deployments — running behind a firewall, accessed over a VPN, or behind a reverse proxy that provides its own authentication layer.

**Secure defaults out of the box:**
- The dashboard binds to `127.0.0.1` — only your local machine can reach it
- The `/admin` and `/workspace` UIs are disabled until you explicitly turn them on
- API docs and OpenAPI schema are off by default
- No email triggers until you set `EMAIL_ACTIONABLE_SENDERS`
- No outbound email beyond your admin address until you set `EMAIL_ALLOWED_RECIPIENT_DOMAINS`
- The agent can only be triggered by you and can only reply to people you've approved

**Before going live, make sure:**
- [ ] `POSTGRES_PASSWORD` is a strong, unique password (not the placeholder)
- [ ] `ADMIN_EMAIL` is set to your own address
- [ ] `EMAIL_ACTIONABLE_SENDERS` is locked to your address
- [ ] `EMAIL_ALLOWED_RECIPIENT_DOMAINS` is locked to your domain(s)
- [ ] `AGENT_API_PUBLISHED_HOST` stays at `127.0.0.1` unless you intend LAN access
- [ ] If exposing publicly: put a reverse proxy with HTTPS + authentication in front

See **[docs/security.md](docs/security.md)** for the full threat model, hardening checklist, sandbox socket risks, and reverse-proxy configuration examples (nginx, Caddy).

---

## Documentation

New here? Start with:

1. [Quick Start](#-quick-start) — you're already looking at it
2. [docs/advanced-configuration.md](docs/advanced-configuration.md) — models, limits, email, integrations, UI, scaling
3. [docs/architecture.md](docs/architecture.md) — service topology, runtime boundaries, data flow
4. [docs/security.md](docs/security.md) — threat model, hardening, reverse-proxy examples

### Core Guides

- [docs/architecture.md](docs/architecture.md) — service topology, runtime boundaries, and data flow
- [docs/agent-loop.md](docs/agent-loop.md) — job lifecycle and agent execution loop
- [docs/prompting.md](docs/prompting.md) — prompt construction, context, and compaction
- [docs/tools-reference.md](docs/tools-reference.md) — all agent tools, grouped by capability
- [docs/extending-tools.md](docs/extending-tools.md) — how to add a new tool safely
- [docs/openrouter.md](docs/openrouter.md) — OpenRouter integration and model/tool usage

### Configuration and Deployment

- [docs/advanced-configuration.md](docs/advanced-configuration.md) — **start here** for tuning and integrations
- [docs/configuration.md](docs/configuration.md) — full `.env` and `config/agent.yaml` reference
- [docs/security.md](docs/security.md) — threat model, hardening checklist, and reverse-proxy examples
- [docs/docker-setup.md](docs/docker-setup.md) — image builds, Compose topology, networks, and mounts
- [docs/running-assistant.md](docs/running-assistant.md) — local and server deployment guide
- [docs/concurrent-job-processing.md](docs/concurrent-job-processing.md) — task-agent scaling

### Data and Storage

- [docs/data-model.md](docs/data-model.md) — PostgreSQL schema and entity relationships
- [docs/memory-system.md](docs/memory-system.md) — Memory Steward, notes, embeddings, and workspace index

### Runtime Infrastructure

- [docs/sandbox.md](docs/sandbox.md) — command execution sandbox architecture
- [docs/email-and-artifacts.md](docs/email-and-artifacts.md) — IMAP ingestion, ClamAV scanning, and attachment conversion

### UI and API

- [docs/visual-workspace-ui.md](docs/visual-workspace-ui.md) — `/admin` and `/workspace` UI behavior
- [docs/dashboard-and-api.md](docs/dashboard-and-api.md) — dashboard capabilities and API endpoint map
- [docs/codebase.md](docs/codebase.md) — module map, test structure, and contribution workflow

---

## Contributing

Contributions are welcome! Here's how to get involved:

1. **Fork** the repository and create a feature branch off `main`
2. **Read** [docs/codebase.md](docs/codebase.md) for the module map and contribution guidelines
3. **Write tests** — the test suite lives in `agent/tests/` and uses `pytest`
4. **Open a pull request** with a clear description of what you changed and why

To run the tests locally:

```bash
cd agent
pip install -r requirements.txt
pytest tests/
```

Bug reports, feature requests, and documentation improvements are all appreciated. If you're unsure whether something is in scope, open an issue first to discuss.

---

## License

Licensed under the **Apache License 2.0**. See [LICENSE](LICENSE) for the full text.
