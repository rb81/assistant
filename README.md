<p align="center">
  <img src="docs/assets/logo.png" alt="Assistant logo" width="180">
</p>

<h1 align="center">Assistant</h1>

<p align="center">
  <strong>A semi-autonomous, self-hosted AI personal assistant that lives in your inbox.</strong><br>
  Give it a task. Walk away. Come back to results.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License"></a>
  <img src="https://img.shields.io/badge/version-v2%20(prerelease)-orange.svg" alt="Version v2 prerelease">
  <img src="https://img.shields.io/badge/python-3.12%2B-3776ab.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/runs%20on-Docker%20Compose-2496ed.svg" alt="Docker Compose">
</p>

---

> [!CAUTION]
> **This is Assistant v2, a breaking release. It is not an in-place `git pull` of v1.**
>
> - **Running v1?** Do **not** pull this branch into a running deployment until you have followed the **[upgrade guide](docs/upgrading.md)**. As a safety net, v2 refuses to migrate a database that holds v1 data until you set `ASSISTANT_ACCEPT_V2_MIGRATION=yes` in `.env`; workers exit with an "Upgrade blocked" message and nothing is changed.
> - v2 permanently **drops** the Projects, separate Notes and Entity tables from the database. Supported history is migrated first, but the change cannot be undone without a backup.
> - **Back up your database and files first** (`pg_dump` plus your `data/`, `.env` and `config/`), and rehearse the upgrade on a copy.
> - The browser dashboard, shared workspace editor, and optional management HTTPS API are gone.
> - **Staying on v1?** It remains available and unchanged on the [`v1` branch](https://github.com/rb81/assistant/tree/v1) and the `v1.0.0` release. If you already pulled v2 by mistake, run `git checkout v1` and restart; your data is untouched. Ongoing legacy maintenance is not promised.

## Which version do I want?

| | **v1** | **v2** (`main`) |
|---|---|---|
| Status | Stable, unchanged | Prerelease |
| Code | [`v1` branch](https://github.com/rb81/assistant/tree/v1), release `v1.0.0` | `main` branch |
| Configuration | Hand-edited `.env` | Guided setup wizard, protected secrets, optional `overrides.env` |
| Interface | Email, browser dashboard, workspace editor | Email and local CLI |
| Integrations | Calendar | Calendar, **Zoom**, **Dropbox** |
| Upgrade path | n/a | [Upgrade guide](docs/upgrading.md) (breaking) |

## What's new in v2

- **Guided setup wizard.** `./scripts/setup.sh` configures identity, models, embeddings, email, integrations and advanced settings, with review-before-save, Back/Edit/Cancel navigation and no secrets in your shell history. Existing installs get a section picker instead of repeating everything.
- **Protected secrets.** Credentials live in gitignored `.secrets/` files or an optional dedicated Docker volume, mounted only into the services that need them. Update or rotate them with `./scripts/setup.sh secrets`.
- **Native Zoom connector.** Create, edit and cancel managed meetings, linked to your calendar events. Opt-in.
- **Native Dropbox connector.** A private App Folder Inbox/Outbox for on-demand file exchange: malware-scanned, size-limited, no public links, no overwrites. Opt-in. See [Dropbox](docs/dropbox.md).
- **Memory and data tools.** Unified memories (durable facts plus expiring working context, with reviewable suggestions), `setup tools` troubleshooting menus, and `setup reset`, a selective factory reset that takes a backup first.
- **Workstream provenance.** Channel-neutral task ownership, parent/child workstreams and shared-object attachments. `setup tools` previews and confirms workstream deletion with three explicit sharing policies and retryable local/provider cleanup. See [deletion behavior and limits](docs/security.md#workstream-deletion).
- **Expert `overrides.env`.** An optional, gitignored file for advanced settings the wizard does not manage. See [Expert overrides](#expert-overrides).
- **Email administration.** Authenticated `/admin` commands (`jobs`, `logs`, `memory`, `reminders`, `requeue`, `instruct`) that never invoke a model. See [Email Administration](docs/email-administration.md).
- **Smaller, safer surface.** No browser UI or management API; administration is through email and the local CLI.

### Removed in v2

- Browser dashboard, shared workspace editor and frontend build
- Projects and project scheduling (long tasks use the ordinary agent loop)
- Separate Notes and Entities (migrated into unified memories)

## Quick Start (new installation)

**You'll need:** Docker with Compose, Python 3.12 or newer for setup, an [OpenRouter](https://openrouter.ai) API key, and **a dedicated email mailbox for the assistant** (see the warning below).

```bash
git clone https://github.com/rb81/assistant.git
cd assistant
./scripts/setup.sh
```

The wizard explains every choice, validates as you go and writes nothing until you confirm. Setup can then build and start the stack, or start it yourself:

```bash
docker compose up -d --build
```

Send a task to the assistant's mailbox. Send `/admin` as a subject for deterministic help; commands do not invoke models or create jobs. No application port is published by default.

> [!WARNING]
> **Give the agent its own mailbox**, such as `assistant@yourdomain.com`. Never give it access to your personal email. You send tasks to its address; results and notifications return to your `ADMIN_EMAIL`. Set `EMAIL_ACTIONABLE_SENDERS` to your own address and `EMAIL_ALLOWED_RECIPIENT_DOMAINS` to your own domain so only you can trigger it and it can only email people you trust.

Useful commands:

| Command | Purpose |
|---|---|
| `./scripts/setup.sh` | Full wizard (new) or section picker (existing) |
| `./scripts/setup.sh --section <name>` | Jump to `identity`, `models`, `embeddings`, `email`, `calendar`, `zoom`, `dropbox`, `advanced` or `instructions` |
| `./scripts/setup.sh secrets status\|update\|apply` | Manage credentials |
| `./scripts/setup.sh tools` | Status, logs, restarts, jobs, memories, reminders, embeddings |
| `./scripts/setup.sh reset` | Factory reset of selected data, with backup; keeps settings and `AGENT.md` |

## Upgrading from v1

> [!IMPORTANT]
> Read the full **[upgrade guide](docs/upgrading.md)** before touching a running v1 install. The short version:
>
> 1. Record your current v1 commit/images and stop the old workers (including the project scheduler). Never run v1 and v2 workers against the same database.
> 2. With PostgreSQL still running, take a logical backup: `docker compose exec -T postgres pg_dump -U assistant -d assistant > assistant-before-upgrade.sql`. Keep it confidential and test restoring it into a disposable database.
> 3. Stop the stack **without** `--volumes`. Keep `data/`, `.env`, `config/` and any local prompt files.
> 4. Update to v2 (`git pull` on `main`, or switch from `v1` with `git checkout main && git pull`), run `./scripts/setup.sh` and keep your current database password. Known inline credentials move into protected secret files; your prompts and unknown settings are preserved. Decline replacing customized instructions, and decline automatic startup until you are ready.
> 5. Only after your backup, add `ASSISTANT_ACCEPT_V2_MIGRATION=yes` to `.env` (the acknowledgement that the retired tables will be dropped), then start v2 (`docker compose up -d --build`). Without it, v2 refuses to migrate a database holding v1 data. Ordered migrations then run automatically; verify with `/admin status`, your memories, reminders and integrations.
> 6. Rotate provider credentials that were previously stored in plaintext `.env`, backups or history.
>
> **Rolling back** means restoring the pre-upgrade backup together with the old code. Checking out v1 against a migrated database is unsafe.

What happens to your data:

| v1 data | In v2 |
|---|---|
| Notes | Converted to unified memories |
| Entities | Kept as tags on memories |
| Projects and project tasks | Snapshots kept on the parent jobs; waiting parents are requeued with retirement instructions; unstarted tasks are not queued |
| Working context | Migrated working context **expires after 14 days**; pin or review anything that must persist |
| Jobs, emails, memories, reminders, calendar records | Preserved |
| Browser UI URLs and management API clients | **No longer work** |

Email intake now requires the `email` Compose profile; the wizard selects it. `AGENT_TIMEZONE` is authoritative for implicit times.

## How it works

```
You → email
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

Separate services handle email ingestion, job execution, native reminders, deep research, private file indexing, supervision and health monitoring, sharing PostgreSQL and private working storage. See [architecture](docs/architecture.md) and the [agent loop](docs/agent-loop.md).

## What it does

- **Schedules itself.** Recurring reminders wake it to rerun tasks or continue work.
- **Writes and runs code** in an isolated Docker sandbox, not on your host.
- **Deep research.** Asynchronous research loops with web search; it pauses to ask when stuck.
- **Persistent memory.** Decisions, preferences and context are remembered and injected into relevant jobs.
- **Private files and contacts.** Attachment scanning, extraction and conversion, with email deliverables.
- **Calendar** (Google, Outlook, Zoho) with strict managed-event rules. Opt-in.
- **Zoom** managed meetings. Opt-in.
- **Dropbox** private file exchange. Opt-in.

## Configuration

Credentials live in gitignored `.secrets/` files by default; non-secret settings live in `.env`. Choose **Move credentials to a dedicated Docker volume** during setup review or in the post-setup menu to move credential files out of the installation directory. The volume is not encrypted. Service-specific read-only mounts are preserved, and original credential files are removed only after the copy and volume mounts are verified. See [credential storage](docs/security.md#docker-secrets) for requirements and backup precautions.

The same menu offers **Move credentials back to plaintext host files** when volume storage is selected; setup recreates affected services and destroys the volume automatically. Keep `.secrets/state.json` in either mode: it contains setup metadata, not credentials. Factory reset preserves both credential stores.

You never need to copy `.env.example`: it is only a reference for the keys setup writes.

```bash
./scripts/setup.sh --section advanced   # task limits, email behavior, memory, search, research
./scripts/setup.sh secrets update OPENROUTER_API_KEY
```

### Expert overrides

Everything the wizard does not manage can be set in an optional `overrides.env` in the repository root. Setup creates a commented template on first save, or you can create it by hand with `NAME=value` lines using the names in [environment overrides](docs/env-overrides.md).

- Gitignored and loaded after `.env`; never rewritten by setup.
- Validated at the wizard's review step (unknown names, wrong types, credentials, conflicts).
- **Never put credentials in it.** Use `./scripts/setup.sh secrets update`.
- Apply changes with `docker compose up -d --force-recreate`.

Details: [Manual overrides file](docs/configuration.md#manual-overrides-file).

## Security and autonomy

> [!WARNING]
> PostgreSQL, sandbox, SMTP/IMAP, and external-service connections retain their required TLS verification. Admin email requests must come from an authenticated identity; see [Email Administration](docs/email-administration.md).

Secure defaults:

- No application port is published, and no browser UI exists.
- No email triggers until you set `EMAIL_ACTIONABLE_SENDERS`.
- No outbound email beyond your admin address until you set `EMAIL_ALLOWED_RECIPIENT_DOMAINS`.

Before going live:

- [ ] `POSTGRES_PASSWORD` is strong and unique (the wizard keeps your existing one on upgrade)
- [ ] `ADMIN_EMAIL` is your own address
- [ ] `EMAIL_ACTIONABLE_SENDERS` and `EMAIL_ALLOWED_RECIPIENT_DOMAINS` are locked down
- [ ] Docker daemon access uses local sockets, SSH or mutual TLS, never unauthenticated TCP

See [docs/security.md](docs/security.md) for the threat model, sandbox socket risks and hardening.

## Documentation

Start with [Configuration](docs/configuration.md), [Architecture](docs/architecture.md) and [Security](docs/security.md). The full index is in [docs/README.md](docs/README.md).

| Topic | Guides |
|---|---|
| Upgrading | [Upgrade guide](docs/upgrading.md), [Changelog](CHANGELOG.md) |
| Configuration | [Advanced configuration](docs/advanced-configuration.md), [Configuration reference](docs/configuration.md), [Environment overrides](docs/env-overrides.md), [Docker setup](docs/docker-setup.md), [Running Assistant](docs/running-assistant.md) |
| Integrations | [Calendar providers](docs/calendar-providers.md), [Dropbox](docs/dropbox.md), [OpenRouter](docs/openrouter.md) |
| Internals | [Agent loop](docs/agent-loop.md), [Prompting](docs/prompting.md), [Tools reference](docs/tools-reference.md), [Data model](docs/data-model.md), [Memory system](docs/memory-system.md), [Sandbox](docs/sandbox.md), [Email and artifacts](docs/email-and-artifacts.md), [Concurrent jobs](docs/concurrent-job-processing.md) |
| Extending | [Extending tools](docs/extending-tools.md), [Codebase guide](docs/codebase.md) |

## Contributing

Contributions are welcome. Fork the repository, branch from `main` for v2 work, read [docs/codebase.md](docs/codebase.md), write tests and open a pull request describing what and why.

```bash
cd agent
python3 -m venv ../.venv && . ../.venv/bin/activate
pip install -r requirements.txt -r requirements-setup.txt pytest
PYTHONPATH=src pytest tests/
```

Open an issue first if you are unsure whether something is in scope.

## License

Licensed under the **Apache License 2.0**. See [LICENSE](LICENSE).
