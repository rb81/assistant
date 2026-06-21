# Configuration Reference

Assistant keeps configuration split across two layers:

| File | Purpose | Git Status |
|------|---------|------------|
| `config/agent.yaml` | All operational defaults (tracked, rarely needs editing) | Tracked |
| `.env` | Secrets, credentials, and per-deployment identity overrides | Gitignored |
| `.env.example` | Template showing all available environment variables | Tracked |

## Override precedence (highest wins)

1. **Environment variable** (from `.env` via docker-compose)
2. **`config/agent.yaml`** (tracked defaults)
3. **Code-level fallback defaults**

## Quick start

```bash
cp .env.example .env
# Fill in your secrets and identity values in .env
```

Most users only need to edit `.env` for secrets and identity values. `config/agent.yaml` contains sensible operational defaults and should not need editing for normal deployments. If you do need to change operational behavior (LLM models, timeouts, limits, etc.), edit `config/agent.yaml` directly — your changes will show up as local modifications in Git.

## `.env`: secrets and account/safety values

The normal `.env` file should stay lightweight:

```env
POSTGRES_PASSWORD=replace-me

AGENT_API_PUBLISHED_HOST=127.0.0.1
AGENT_API_PUBLISHED_PORT=8000
AGENT_APP_BASE_URL=https://assistant.example.com
AGENT_API_DOCS_ENABLED=false
AGENT_API_OPENAPI_ENABLED=false

OPENROUTER_API_KEY=sk-or-v1-...

AGENT_NAME=Ada
AGENT_EMAIL=ada@example.com

ADMIN_EMAIL=admin@example.com
ORG_NAME=Example Inc
ORG_SECURITY_EMAIL=security@example.com
ORG_INTERNAL_EMAIL_DOMAINS=example.com

# The agent must have its own dedicated mailbox — do not use your personal email address.
# Create a separate account (e.g., assistant@yourdomain.com) and set the credentials below
# to point at that account. IMAP_USERNAME/SMTP_USERNAME should be the agent's own address.
IMAP_HOST=imap.example.com
IMAP_PORT=993
IMAP_USERNAME=agent@example.com
IMAP_PASSWORD=...
IMAP_FOLDER=INBOX
IMAP_ARCHIVE_FOLDER=Archive
IMAP_SENT_FOLDER=Sent

SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=agent@example.com
SMTP_PASSWORD=...
SMTP_FROM=agent@example.com

EMAIL_ALLOWED_RECIPIENT_DOMAINS=example.com,trusted-partner.com
EMAIL_ACTIONABLE_SENDERS=boss@example.com,team@example.com

CALDAV_URL=https://calendar-provider.example/.well-known/caldav
CALDAV_USERNAME=user@example.com
CALDAV_PASSWORD=...
```

Most of these values are injected into matching runtime config paths at startup. For example, `ADMIN_EMAIL` becomes `agent.admin.email`, `ORG_NAME` becomes `agent.org.name`, `ORG_SECURITY_EMAIL` becomes `agent.org.security_email`, `ORG_INTERNAL_EMAIL_DOMAINS` becomes `agent.org.internal_email_domains`, and `EMAIL_ALLOWED_RECIPIENT_DOMAINS` becomes `agent.email.allowed_recipient_domains`.

`AGENT_API_PUBLISHED_HOST` and `AGENT_API_PUBLISHED_PORT` are consumed by Docker Compose when publishing the dashboard on the host. Use `127.0.0.1` for machine-local access only. Use the server's LAN IP, for example `192.168.1.50`, when trusted LAN clients should reach `http://192.168.1.50:8000` without publishing the dashboard on every interface. These values do not control generated links.

`AGENT_APP_BASE_URL` is the public dashboard URL included in admin emails and other human-facing links, and a non-blank environment value overrides `agent.app.base_url` from `config/agent.yaml`. Set it to the externally reachable URL for production, for example `https://assistant.example.com`. If it is omitted or blank, Assistant uses `agent.app.base_url` from `config/agent.yaml`.

`AGENT_API_DOCS_ENABLED=false` disables Swagger UI, ReDoc, and the dashboard API Docs icon. `AGENT_API_OPENAPI_ENABLED=false` disables `/openapi.json`. Blank values leave `config/agent.yaml` or the built-in defaults unchanged. Set both to `false` in production unless you intentionally expose generated API documentation behind another access-control layer.

The `CALDAV_*` values are different: they are account/provider values passed into the container for the configured calendar sync tool. They are not mapped into Assistant's `agent.calendar` runtime config and are not exposed as assistant tools.

`CALDAV_URL` should be the direct CalDAV URL for the single calendar collection Assistant should sync. The bundled `config/vdirsyncer/config` uses `collections = null`, so it does not perform collection discovery.

## Entity Registry

The entity registry automatically categorizes all objects (memories, notes, reminders, projects, contacts, jobs, emails, calendar events) into high-level entities representing major areas, topics, or projects in the user's life.

Configuration in `agent.yaml`:

```yaml
agent:
  entities:
    enabled: true                  # Enable entity registry and auto-linking
    max_per_object: 3             # Max entities per object (prefer 1-2)
    auto_link_on_create: true     # Auto-link when objects created
```

Environment overrides:

- `AGENT_ENTITIES_ENABLED` — set to `false` to disable entity registry
- `AGENT_ENTITIES_MAX_PER_OBJECT` — override max entities per object
- `AGENT_ENTITIES_AUTO_LINK_ON_CREATE` — set to `false` to disable auto-linking

The entity linker uses the memory steward model (`agent.memory.steward.model`) for classification. Auto-linking is best-effort and never blocks object creation.

When enabled, objects are automatically analyzed and linked to 1-3 high-level entities. The system prefers reusing existing entities over creating new ones, and entities are meant to be broad and meaningful (e.g., "IntelliGulf", "Personal Finance") rather than granular (e.g., "Meeting Notes", "Tuesday Tasks").

`DATABASE_URL` is still accepted as an advanced override, but the normal path is to set `POSTGRES_PASSWORD`; Assistant builds the PostgreSQL URL from local `config/agent.yaml` database defaults plus that password.

## `config/agent.yaml`: operational defaults

`config/agent.yaml` is the tracked configuration file containing all non-secret operational settings:

- app base URL, timezone, API bind host, and API port
- API docs/OpenAPI exposure controls
- database host, port, database name, and username
- LLM provider, model names, base URL, temperature, and token limits
- task limits, tool timeouts, polling intervals, and rate limits
- email behavior defaults such as folder names, attachment limits, and sent-folder behavior
- filesystem paths, shared workspace behavior, and umask
- calendar sync, private vdir storage, and managed-event policy
- artifact processing, ClamAV, prompt context paths, memory, notes, entities, workspace indexing, reminders, projects, deep research, heartbeat, sandbox, search, and fusion defaults

**Every setting in this file can be overridden via environment variables in `.env`.** For normal deployments you should not need to edit this file — use `.env` for both secrets/identity and operational overrides. See [docs/env-overrides.md](env-overrides.md) for the complete list of available environment variable overrides.

### ⚠️ Important: Use `.env` for Customization

**Git updates overwrite `config/agent.yaml` with the upstream version.** If you edit this file directly, your changes will be lost when you pull updates. The recommended approach is to:

1. **Use `.env` for all customizations** — every setting can be overridden from there
2. If you must edit `agent.yaml`, back it up before pulling updates
3. After pulling, review the new `agent.yaml` and migrate your changes to `.env` overrides

This keeps your customizations separate from the tracked defaults and makes updates seamless.

Container deployment defaults, including sandbox run-container runtime and resource limits, live in `docker-compose.yml`. Keep those out of `.env` unless a deployment has an intentional override.

## Persistent data

Docker Compose stores runtime state under `./data` by default:

```text
./data/postgres
./data/share
./data/private/artifacts
./data/ollama
./data/clamav
```

Stop the Compose stack before copying `data` to preserve or move an agent. It contains databases, shared workspace files, private raw inbound artifacts, soft-deleted files, ClamAV data, and the local Ollama model cache.

Prompt configuration is file-based in `./config`:

- `config/AGENT.md` is the runtime prompt file by default.
- `agent.prompt.agent_file` in `config/agent.yaml` can point to a different filename under `/app/config`.
- Startup fails if the resolved prompt file is missing or empty.

Packaged reference docs under `.assistant/docs/*` are refreshed in the shared workspace from app defaults at startup.

The `data` directory covers runtime state. Keep `.env` and local `config/agent.yaml` with the deployment as well.

## Required for core services

For manual dashboard jobs processed by the task-agent, set at least:

- `POSTGRES_PASSWORD`
- `OPENROUTER_API_KEY`
- `ADMIN_EMAIL`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_FROM`

For authenticated SMTP, also set `SMTP_USERNAME` and `SMTP_PASSWORD`.

For email-driven jobs, set IMAP values:

- `IMAP_HOST`
- `IMAP_PORT`
- `IMAP_USERNAME`
- `IMAP_PASSWORD`
- `IMAP_FOLDER`
- `IMAP_ARCHIVE_FOLDER`
- `IMAP_SENT_FOLDER`

## What can be left blank

Not all `.env` values are required. Here is a summary of fallback behavior when values are omitted:

| Variable | If blank… |
|----------|-----------|
| `AGENT_NAME` | Falls back to YAML `identity.name`, then to capitalized app name ("Assistant") |
| `AGENT_EMAIL` | Falls back to YAML `identity.email`, then `SMTP_FROM`, then `SMTP_USERNAME`, then "assistant@local" |
| `ADMIN_NAME` | No admin name displayed in notifications |
| `ADMIN_EMAIL` | ⚠️ Required for core operation — approval notifications go here |
| `ORG_NAME` | Disclosure footers omit organization name |
| `ORG_SECURITY_EMAIL` | Disclosure footers omit security contact |
| `ORG_INTERNAL_EMAIL_DOMAINS` | All recipients treated as external (footers always added) |
| `EMAIL_ALLOWED_RECIPIENT_DOMAINS` | Only admin email is allowlisted for delivery |
| `EMAIL_ACTIONABLE_SENDERS` | No inbound emails create new jobs (dashboard-only) |
| `CALDAV_*` | Calendar features remain disabled |
| `IMAP_*` | Email polling disabled; agent operates in dashboard-only mode |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | SMTP connects without authentication (rare) |

## Email guardrails

Outbound email is exposed only when SMTP is configured. Delivery is constrained by `EMAIL_ALLOWED_RECIPIENT_DOMAINS`, except that the configured admin email is always allowed.

`ORG_NAME` is the organization name used in external autonomous-agent disclosure footers.

`ORG_SECURITY_EMAIL` is the organization-level contact address for safety or security messaging and external autonomous-agent disclosure footers.

`ORG_INTERNAL_EMAIL_DOMAINS` is the comma-separated list of organization domains considered internal for autonomous-agent disclosure footers. This is separate from `EMAIL_ALLOWED_RECIPIENT_DOMAINS`: allowed recipient domains control whether delivery is permitted, while internal org domains control whether a footer is needed.

When any outbound `To` or `Cc` recipient is outside `ORG_INTERNAL_EMAIL_DOMAINS`, Assistant appends an autonomous-agent disclosure footer. Replies containing that footer are stripped during inbound ingestion before the email is stored or shown to the agent.

`EMAIL_ACTIONABLE_SENDERS` controls which senders can create new jobs from inbound email. Replies to open jobs are still used to update/resume those jobs.

Inbound email threading is based on `In-Reply-To` and `References` message IDs. New messages without matching headers start independent threads even when subjects repeat. Set `agent.email.subject_threading_fallback: true` only if you deliberately want the legacy normalized-subject fallback.

SMTP sending does not always create a Sent-folder copy. `agent.email.save_to_sent` in local `config/agent.yaml` controls whether Assistant appends an RFC822 copy through IMAP after SMTP acceptance.

## Polling intervals

External mail polling and internal queue polling are configured separately. `agent.email.imap_poll_interval_seconds` controls IMAP sync cadence and defaults to 60 seconds. Internal PostgreSQL-backed workers default to faster checks so queued work is claimed quickly:

- `agent.task_agent.poll_interval_seconds`: queued job claiming, default `2`
- `agent.reminders.scheduler_poll_interval_seconds`: due reminder scheduling, default `2`
- `agent.projects.scheduler_poll_interval_seconds`: project child-task scheduling, default `2`
- `agent.deep_research.poll_interval_seconds`: queued research-run claiming, default `2`

These intervals are clamped to at least 1 second at runtime. Project and reminder schedulers continue immediately after processing work, then sleep only after an idle tick.

## Tool status

After the API service is running, inspect computed availability:

```bash
curl http://localhost:8000/api/config/status
```

This reports whether email, memory, reminder, calendar, file, command, search, fusion, terminal, admin, and LLM capabilities are available under the current configuration.

## Calendar sync

Assistant calendar access is intentionally local-first:

```text
CalDAV provider <-> configured sync command <-> private vdir store <-> calendar gateway <-> assistant tools
```

The assistant does not receive the CalDAV password and does not write arbitrary `.ics` files. It can use `calendar_list_busy` and `calendar_list_events` through the gateway, and writes go through `calendar_create_event`, `calendar_update_event`, and `calendar_delete_event`.

Writes are managed-only. Created events receive local ownership metadata and iCalendar markers, and update/delete operations require both the database record and marker to match. Non-managed calendar events can contribute to free/busy results, but the gateway refuses to edit or delete them.

Keep the vdir store under `/data/private/calendar/vdir` or another path outside `/data/share`; `/data/share` is visible through normal file tools. The default Compose setup mounts `./data/private/calendar` into the task-agent container for this purpose.

Configure calendar runtime behavior in `config/agent.yaml` under `agent.calendar`. Keep this block provider-neutral: enabled/disabled state, fixed sync command, local vdir path, default calendar collection, read-detail policy, and default alert minutes belong there. Put CalDAV account values in `.env`, and configure the sync tool to consume `CALDAV_URL`, `CALDAV_USERNAME`, and `CALDAV_PASSWORD`.

`agent.calendar.default_alert_minutes` sets how many minutes before each event the assistant adds a VALARM reminder to every `.ics` file it creates or updates. The default is `15`. Set it to `0` to suppress alerts entirely. Override it per-deployment with `AGENT_CALENDAR_DEFAULT_ALERT_MINUTES` in `.env`.

`CALENDAR_TIMEZONE` (or `agent.calendar.timezone` in YAML) sets the timezone used when writing `DTSTART`/`DTEND` values and the accompanying VTIMEZONE component into generated `.ics` files. When blank, it falls back to `AGENT_TIMEZONE`. Setting this correctly ensures events appear at the right local time on all calendar clients regardless of where their CalDAV server is hosted.

The default Compose environment sets `VDIRSYNCER_CONFIG=/app/config/vdirsyncer/config`. That tracked config writes local events to `/data/private/calendar/vdir/default`, stores sync state in `/data/private/calendar/status`, and reads CalDAV account values from `CALDAV_URL`, `CALDAV_USERNAME`, and `CALDAV_PASSWORD`.

To start fresh locally, `scripts/reset-databases.sh` clears calendar ownership/audit records with the database reset. Add `--discard-calendar-sync` when you also want to delete the local vdir files and vdirsyncer status. This does not delete remote calendar events.
