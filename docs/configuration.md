# Configuration Reference

## Guided setup

Install Python 3.12+ and Docker Compose, then run from any directory:

```bash
./scripts/setup.sh
```

The launcher resolves its repository root and asks before creating an isolated
`.venv-setup` or installing setup dependencies. Set `PYTHON` to a suitable Python
executable if necessary. The wizard's sections cover identity/timezone, models
and spending, embeddings/search, email/administration, optional Calendar/Zoom/Dropbox,
Telegram, voice transcription, security screening, Advanced Settings, and
assistant instructions.
Back/Edit/Cancel are available. The final review offers **Save and finish**,
**Edit a section**, and **Cancel without saving**. If validation fails, the review
shows the error and disables saving until it is fixed. Secret errors name the
credential to correct. No changes are written until you choose Save and finish.

### Navigation

- After each section the wizard shows a summary of that section's settings
  (credentials masked), then offers **Next**, **Edit this section**, **Back**,
  **Jump to section**, **Review and save** and **Cancel**.
- Returning to a section you already completed (Back, Jump, or Edit a section at
  review) shows its summary first; prompts are repeated only if you choose
  **Edit this section**.
- Section menus show a status: `changed`, `reviewed`, or `needs attention`
  (a validation problem belongs to that section). Review lists every problem.
- **Ctrl-C inside a section** discards only that section's changes and returns
  to navigation. Ctrl-C at a menu exits, asking first if unsaved changes exist.
- In the calendar provider checklist, Space ticks providers; pressing Enter with
  nothing ticked selects the highlighted provider.

### Manual overrides file

On first save, setup creates `overrides.env` in the repository root: a commented
list of every runtime setting the wizard does not manage, with its YAML path, type
and current default. Uncomment a line and set a value, then recreate containers
(`docker compose up -d --force-recreate`). Setup never rewrites this file, so edits
survive re-running the wizard. The review step reports unknown or misspelled keys,
wrongly typed values, credentials (use `secrets update`) and keys the wizard or
`docker-compose.yml` already set, because Compose `environment:` entries take
precedence over `env_file` values and would silently ignore them. `overrides.env`
is git-ignored; include it in configuration backups. Manual keys added to `.env`
are also preserved, but `overrides.env` keeps them separate and validated.

**Creating it by hand.** The file is optional; you can create it yourself before
or after running setup. Put one `NAME=value` per line, using only names listed in
[environment overrides](env-overrides.md) (booleans `true`/`false`, lists
comma-separated, no quotes needed). If the file already exists, setup leaves it
untouched and does not generate the commented template, so keep the reference
open while editing. Compose loads it after `.env`, so its values win over `.env`
and `config/agent.yaml`, but never over `environment:` entries in
`docker-compose.yml`. Never put credentials in it; use
`./scripts/setup.sh secrets update NAME`. Setup's review step rejects unknown or
misspelled names, wrongly typed values, credentials, and settings that the wizard
or Compose already controls. Compose-only variables read for interpolation
(`TASK_AGENT_WORKERS`, `SANDBOX_ISOLATION_MODE`,
`COMPOSE_PROFILES`) are not read from `overrides.env`; keep them in `.env`.
Changes apply after `docker compose up -d --force-recreate`.

### Tools and factory reset

The update picker (and `./scripts/setup.sh tools`) opens **Tools and
troubleshooting**: service status, recent or live logs, restart/recreate/start/stop,
jobs (counts, recent jobs, logs, cancel, requeue, add instruction, approve review),
memories, reminders, embedding status/probe/rebuild and pending credential
activation. Every action prints the exact `docker compose` command first and asks
before changing anything. Job, memory and reminder tools run
`assistant_agent.admin_commands` in a one-off `task-agent` container and need
PostgreSQL running; rebuild that image after upgrading (`docker compose build task-agent`).

**Factory reset** (`./scripts/setup.sh reset`, or from the picker) deletes the data
you tick: jobs and logs, emails and attachments, memories, reminders,
calendar/Zoom records, the sent-email log, contacts, the workspace index, caches
and trash, all workspace files, or runtime state. Selections that would leave
inconsistent data automatically include what they depend on, with the reason
shown (for example, deleting emails also deletes jobs, and deleting jobs also
deletes reminders). It always keeps `.env`, `overrides.env`, `.secrets/`, the
Compose overlay, `config/` (including `AGENT.md`), `data/skills`, TLS files,
Ollama models, ClamAV signatures and the database schema. By default it keeps the
mailbox position so old inbox messages are not processed again, and it first
writes `backups/<time>-factory-reset/` (`database.dump` from `pg_dump` and
`files.tar.gz` of every deleted file, mode 0600). You must type `RESET`. Writer
services are stopped during the reset and restarted afterwards; database deletion
is a single verified transaction. Custom external databases are not included in
the built-in backup. Tick every item to return to an empty installation.

Existing settings and credentials are preserved unless explicitly changed. Known
inline credentials are migrated out of `.env`; conflicting or duplicate settings
must be resolved first. Unknown non-secret overrides and comments are preserved.
Unrecognized credential settings require review rather than silently remaining
exposed. Existing custom YAML files are preserved. The instructions section asks
whether to copy `config/AGENT.md.example` to `config/AGENT.md`; replacing existing
instructions requires a second explicit confirmation. Copying happens only on
final save. The wizard prints the absolute file path so you can edit it later.

Saving alone does not start services. After saving, the wizard separately asks
whether to build/start the enabled containers and verify their status. Starting
workers applies pending database migrations and may process existing inbox mail.
For an upgrade, follow [the upgrade guide](upgrading.md) before accepting startup.

## Dropbox File Exchange

Run `./scripts/setup.sh --section dropbox` to connect a scoped Dropbox
App Folder using offline PKCE authorization. Imports use a dedicated Inbox;
standing permission for task-based Outbox delivery is a separate opt-in.
The default transfer limit is 25 MiB. Connection and permission changes are
staged until Save, with optional read-only authorization verification.
See [Dropbox setup and privacy](dropbox.md) for registration, supported files,
credentials, removal and activation.

## Security screening

Run `./scripts/setup.sh --section security` to configure the following:

- **Admin sender authentication:** DNS-verified DKIM or SPF for mail that claims
  your admin address.
- **Decision model:** none, OpenRouter (the default; enter any decision model
  slug and reuse your OpenRouter key), or a provider's own API (Perplexity, Jev,
  Clef, OpenAI or a custom endpoint, with its own key). Then choose the error
  policy and optionally send a test request.
- **Screening scope:** inbound screening is always on once a model is set.
  Screening outbound email and scripts are separate opt-ins, and scripts can
  have a list of allowed domains.
- **YOLO mode:** off by default. Turning it on requires typing a confirmation.

See [Email and action screening](security.md#email-and-action-screening).

## Advanced Settings

On an existing installation, run:

```bash
./scripts/setup.sh --section advanced
```

This section also appears in the update picker and full wizard. Choose a group
to edit, then **Done (keep remaining settings)** to return to review. Skipping
the section does not add advanced overrides. Existing `.env` values take
precedence over your YAML defaults; only edited groups are staged. Numeric
values are validated at the prompt and again before Save. Cancel discards all
staged changes, including permission changes.

| Group | Controls |
| --- | --- |
| Calendar privacy | Read existing event titles, descriptions, locations and attendees; disabled by default |
| Task limits | Iterations and total tokens per task, output tokens per model call, maximum task duration |
| Email behavior | Save to Sent, inbox polling interval, outgoing emails per hour, attachments per outgoing email (0 disables attachments) |
| Memory and workspace indexing | Automatic memory stewardship, maximum injected memories, email-context lookback, automatic workspace indexing |
| Web search | Enable search, results per search, allowed and excluded domains |
| Research and notifications | Enable deep research, tool calls per research run, admin digest interval (0 disables digests) |

For calendar content access, choose **Calendar privacy** and explicitly enable
reading event details. This saves
`AGENT_CALENDAR_POLICY_ALLOW_READ_EVENT_DETAILS=true`; it does not enable a
provider or grant permission to edit/delete existing events. Calendar writes
remain restricted to assistant-managed events. Full event content is retrieved
through `calendar_list_events`; free/busy endpoints may still omit details.

Domain lists are comma-separated hostnames. Blank clears the corresponding
search restriction, including a list inherited from YAML. Email sender and
recipient restrictions remain in the email section. Enabling search or research
does not provision model access; the configured provider must support it. Digests
also require working outgoing email.

Advanced Settings uses existing runtime environment overrides, not edits to the
tracked YAML. Service endpoints, storage paths, antivirus requirements, mount
checks, TLS/authentication and internal tuning parameters remain in their
existing sections or manual configuration.

Saving does not change running containers. Accept the separate startup option
to build/recreate services, or follow your normal upgrade procedure and recreate
the affected containers later. Recreating services may resume inbox processing
and apply pending database migrations; review the upgrade guide first.

## Startup, health and reboot persistence

Optional startup runs Compose builds, starts the selected profiles, then waits
for long-running services to become running or healthy where a Docker healthcheck
is defined. A status table distinguishes actual health results from services with
no healthcheck; running alone is not proof of application readiness. Failed startup
leaves the saved configuration in place and prints commands for retry/status/logs.

The wizard sets and verifies `unless-stopped` on long-running containers. They
restart after failures and Docker/host restarts unless deliberately stopped.
The one-shot model downloader is excluded from restart-policy changes.

Docker itself must start at boot. On a local, systemd-based Linux server, the
wizard checks this and offers `systemctl enable --now docker.service`, using
`sudo -n` only when needed. It never collects passwords or waits on an interactive
sudo prompt. If authorization is unavailable, it prints the manual command:

```bash
sudo systemctl enable --now docker
systemctl is-enabled docker
```

Before startup, the wizard performs a bounded, read-only Docker daemon probe.
If direct access works, it uses Docker without asking for sudo. If direct access
fails on non-root Linux, it offers a separate choice to use existing sudo
authorization and verifies access before changing containers. It never prompts
for a password or escalates without consent. An authorization failure is not a
container health failure: startup reports its failed stage and a safe reason,
without exposing captured command output. If this fails, run the displayed
commands yourself using the indicated direct or sudo access mode. Configuration
remains saved, but running containers may still use the previous settings until
startup succeeds.
Docker Desktop requires automatic startup at login. Remote/rootless Docker
requires configuring daemon startup on its actual host; the wizard does not alter
an unrelated local system service. Rootless deployments also require appropriate
user-service startup and login lingering.

## Configuration layers

| Location | Purpose | Git status |
|---|---|---|
| `config/agent.yaml` | Operational defaults | Tracked |
| `.env` | Non-secret settings written by setup; profile selection | Ignored |
| `overrides.env` | Optional hand-edited expert overrides; loaded after `.env`, never rewritten | Ignored |
| `.secrets/` | Protected credential source files and update state | Ignored |
| `.compose-secrets.yaml` | Generated per-service secret mounts | Ignored |
| `config/AGENT.md` | Operator-maintained instructions | Ignored |

Runtime environment overrides take precedence over YAML defaults and code
fallbacks. For a known credential, `NAME_FILE` reads a mounted file directly;
nonempty `NAME` plus `NAME_FILE` is an error. A configured missing/unreadable/empty
file is an error, not permission to fall back to a less secure value. File values
are not copied into process environment variables. One terminal newline is
removed when reading a credential file; other characters are preserved.

Legacy inline credential variables remain supported for migration. The wizard
writes `COMPOSE_FILE` to select both the base stack and the generated secrets
overlay. Start from the repository directory with `docker compose up -d --build`.
Do not omit that overlay when operating a migrated installation.

## Secret management

```bash
./scripts/setup.sh secrets status
./scripts/setup.sh secrets update
./scripts/setup.sh secrets update SMTP_PASSWORD
./scripts/setup.sh secrets apply
./scripts/setup.sh secrets recover
```

Only credential names appear in command arguments. Values are entered through
masked prompts, validated, and saved with restricted permissions and atomic file
replacement. Status never displays values or claims verified runtime health.
Choose immediate activation or save for later; activation recreates only affected
running services without implicitly starting inactive profiles. Database rotation
is a separate coordinated operation; `recover` handles interrupted rotation.

Source files are plaintext on the host. Docker Compose secrets are not an encrypted
vault. Protect host access, the Docker daemon, disks and backups. See [security](security.md).
Never rotate an existing database password just by changing a file or rerunning
initial setup. Third-party credentials must first be issued at their provider.

## Email-led and local operation

The wizard selects the `email` Compose profile for the downloader. Email-led
operation requires a dedicated mailbox, IMAP/SMTP credentials, an admin address,
and sender/recipient guardrails. Do not use a personal mailbox. SMTP supports
STARTTLS on 587 and implicit TLS on 465. Admin requests require replying to a
separate, ten-minute OTP/hash confirmation sent only to the configured admin;
no client signing key or trusted receiving-provider authority IDs are required.

`EMAIL_ACTIONABLE_SENDERS` restricts new jobs; replies on threads with an open job
are also accepted. Other mail is moved unread to `IMAP_QUARANTINE_FOLDER`. `EMAIL_ALLOWED_RECIPIENT_DOMAINS` restricts outgoing delivery,
with the admin address allowed. `ORG_INTERNAL_EMAIL_DOMAINS` controls disclosure
footers, not delivery permission. Message-ID threading is preferred; normalized
subject fallback is opt-in. `AGENT_EMAIL_SAVE_TO_SENT` controls IMAP Sent copies.

Local-only setup omits the downloader and disables email-dependent research/digests.
Local administration remains available through the CLI; external task intake
requires email.

## Models and integrations

OpenRouter is the default model provider. OpenAI-compatible setup prompts for an
HTTPS API URL/key and disables incompatible OpenRouter search/research/fusion.
Choose primary/fallback models. Per-task token and iteration limits are available
in Advanced Settings; monetary spending limits must be configured with the model
provider, not the agent.

Calendar credentials include client ID, client secret and refresh token for
Google/Outlook/Zoho. The wizard checks complete credential groups. Existing
numbered accounts such as `CALENDAR_GOOGLE_2_CLIENT_ID` can be migrated and updated
through their credential names. See [provider setup](calendar-providers.md).
Writes remain limited to assistant-managed calendar events.

The calendar-provider checklist controls which providers remain configured.
Use the arrow keys to highlight a provider, press **Space** to tick it, then
**Enter** to continue. Highlighting alone does not select it. At least one
provider must be ticked while calendar integration is enabled.
Select Zoho and deselect Google to replace Google; keep both selected to use both.
Deselecting removes that provider's saved settings and credential files only on
final save. Disabling calendars or Zoom offers a separate option to remove their
saved configuration, rather than merely disable it. The review marks credentials
scheduled for removal; cancelling leaves existing files untouched. Recreate
running services after saving so they drop removed credential mounts. This does
not delete remote events/meetings, revoke provider access, or purge cached OAuth
tokens from the database.

Zoom supports server-to-server credentials (client ID/secret/account ID) or OAuth
(client ID/secret/refresh token). The wizard explicitly asks for Client Secret in
both modes. Access tokens are fetched automatically; Zoom's webhook **Secret Token**
is not used for meeting management and is not requested. Providers may cache refreshed tokens in PostgreSQL.
This storage is also confidential; secret-file mounts do not encrypt the database.

`AGENT_TIMEZONE` is authoritative for implicit times, reminders and calendars.
Explicit offsets preserve the instant. Deprecated calendar/reminder timezone
settings no longer override it; invalid/ambiguous times require correction.

## Advanced settings and persistent data

Use non-secret environment overrides for model limits, polling, task workers,
filesystem paths and integration behavior: put them in `overrides.env` (see
[Manual overrides file](#manual-overrides-file) and [environment overrides](env-overrides.md)).
Back up custom YAML before updating tracked defaults; Git may reject overlapping
local edits rather than silently merge the desired configuration.

`DATABASE_URL_FILE` is supported for advanced database deployments, but the URL
must include `sslmode=verify-full` and an `sslrootcert` path readable in each agent
container. Automated password rotation only supports the standard Compose database;
custom databases must use their own maintenance workflow.

Runtime state lives under `data`: PostgreSQL, shared files, private artifacts/TLS,
Ollama cache and ClamAV state. Backups also need local `.env`, `overrides.env`, `.secrets`, the
generated overlay, and prompt/config files. Stop workers and take a consistent
database backup before moving or upgrading an installation. Never use live data
for integration tests.