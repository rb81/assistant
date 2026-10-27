# Platform Upgrade Notice

This platform refresh is a breaking change. Read [the upgrade guide](upgrading.md)
before starting new code on an existing database. Use [guided setup and secret
management](configuration.md), [current security guidance](security.md), and
Local administration is provided by the setup CLI and authenticated email
commands. The browser interface, management API, Projects, separate notes and
entities are removed; older feature descriptions elsewhere are historical.

# Documentation Index

This directory is the canonical documentation set for the Assistant project.

For a project overview, quick start, and feature introduction, see the [root README](../README.md).

## ⚠️ Breaking Changes

### Calendar system replaced (CalDAV/vdirsyncer removed)

The local CalDAV/vdirsyncer calendar sync system has been **removed** and replaced with a direct OAuth 2.0 API provider architecture. This is a breaking change for any deployment that had calendar tools configured.

**What changed:**
- `CalendarGateway` and `calendar_gateway.py` are gone; replaced by `CalendarRouter` (`calendar/router.py`)
- `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD`, and `VDIRSYNCER_CONFIG` env vars are no longer used
- The `./data/private/calendar` volume mount has been removed
- `config/vdirsyncer/config` no longer exists
- `calendar_create_event` no longer has a `calendar` parameter; use `provider` instead
- `calendar_managed_events` now stores provider IDs instead of CalDAV file metadata; the ordered migrations handle the schema change during startup.

**Migration steps:**
1. Follow [the upgrade guide](upgrading.md) before starting the updated stack. Do not apply individual SQL files manually; migrations run in order at startup.
2. Remove `CALDAV_*` and `VDIRSYNCER_CONFIG` from your `.env` and remove the `./data/private/calendar` bind mount from any custom Compose overrides.
3. Configure OAuth credentials for at least one provider (Google, Outlook, or Zoho) using [calendar-providers.md](calendar-providers.md).

---

If you are new here, read in this order:

1. [advanced-configuration.md](advanced-configuration.md) — models, limits, email, integrations, and deployment topology
2. [architecture.md](architecture.md) — service topology, runtime boundaries, and data flow
3. [agent-loop.md](agent-loop.md) — job lifecycle and agent execution loop
4. [prompting.md](prompting.md) — prompt construction, context, and compaction
5. [tools-reference.md](tools-reference.md) — all agent tools, grouped by capability
6. [openrouter.md](openrouter.md) — OpenRouter integration and model/tool usage

Then use the domain-specific docs below.

## Core Guides

- [architecture.md](architecture.md) — service topology, runtime boundaries, and data flow
- [agent-loop.md](agent-loop.md) — job lifecycle and agent execution loop
- [prompting.md](prompting.md) — prompt construction, context, and compaction
- [tools-reference.md](tools-reference.md) — all agent tools, grouped by capability
- [extending-tools.md](extending-tools.md) — how to add a new tool safely
- [openrouter.md](openrouter.md) — OpenRouter integration and model/tool usage

## Data & Storage

- [data-model.md](data-model.md) — PostgreSQL schema and data lifecycle
- [memory-system.md](memory-system.md) — unified memory, embeddings, and workspace indexing

## Configuration and Deployment

- [advanced-configuration.md](advanced-configuration.md) — **start here** for models, limits, email, integrations, and deployment topology
- [configuration.md](configuration.md) — full `.env` and `config/agent.yaml` reference
- [calendar-providers.md](calendar-providers.md) — OAuth setup for Google Calendar, Microsoft Outlook, and Zoho Calendar
- [dropbox.md](dropbox.md) - private app-folder file exchange, authorization and transfer limits
- [telegram-and-voice.md](telegram-and-voice.md) - shared request routing, private Telegram, reminders and hardened local transcription
- [env-overrides.md](env-overrides.md) — expert `overrides.env` guide and complete list of overridable settings
- [security.md](security.md) — threat model, hardening checklist, network binding, reverse-proxy examples, and responsible disclosure
- [docker-setup.md](docker-setup.md) — image builds, Compose topology, networks, and mounts
- [running-assistant.md](running-assistant.md) — local and server deployment guide
- [concurrent-job-processing.md](concurrent-job-processing.md) — task-agent scaling

## Runtime Infrastructure

- [sandbox.md](sandbox.md) — command execution sandbox architecture

## Email Pipeline

- [email-and-artifacts.md](email-and-artifacts.md) — IMAP ingestion, disclosure handling, ClamAV scanning, and MarkItDown artifact conversion

## Contributor Orientation

- [codebase.md](codebase.md) — module map, test structure, and contribution workflow
