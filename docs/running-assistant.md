# Running Assistant

## Requirements and setup

Install Docker with Compose, Python 3.12+ with virtual-environment support, and
OpenSSL. Setup offers to create `data/.venv-setup` and install its dependencies.
You also need model API credentials and an email mailbox or Telegram bot for
task intake. From the repository root, run:

```bash
./run.sh
```

`run.sh` is the entry point for both setup and everyday administration.
Existing `data/` settings and the `data/.venv-setup` environment are reused.

The root launcher resolves the repository path; invoke it by its path if
starting outside the repository. Configure identity, model
access, and any enabled integrations in the native forms. It stores mutable settings,
credentials, setup state, and runtime data beneath the gitignored `data/`
directory. See [configuration](configuration.md).

Setup opens a full-screen terminal interface. After initial setup, the home
screen shows actual component health and links to **Services**, **Activity**,
**Connections**, **Settings**, and **Help**. This is one persistent application:
services and database records are native rows and detail views, not terminal
command output or embedded setup menus. Startup checks the existing application
image for management use; browsing records never builds images. If no image is
available, **Services → Start app** explicitly builds and starts the application.
Record requests use short-lived containers with the checkout's Python source
mounted read-only, not a persistent background helper. Loading and failed reads
are distinguished from a successfully loaded empty collection. Navigation stays
available during reads; a page selected while another operation runs loads next.

Use Tab/Shift-Tab or arrows to move, Enter to open an action, and Esc to go back.
The compact, borderless layout keeps navigation at the top and page actions at
the bottom. There is no repeated branding banner. The **About** panel above the
content explains the focused menu option, field or action,
including its purpose, input format and important consequences. **F1** opens
the full explanation and page context in a read-only, scrollable help dialog.
Esc closes help and restores focus without saving or discarding edits.
Navigation, action buttons, pagination and contextual help stay outside the scrolling
content. Status appears only when there is something to report. F5 refreshes the
current view. Service health updates quietly every five
seconds without flashing a loading indicator or rebuilding unchanged content.
Failures still appear in the status area. Service logs have an in-place **Live**
toggle with the same refresh interval.
No shell is opened for service controls or logs.

Settings and connection forms preserve a session draft across sections.
Click a field to edit it; the focused field and insertion position are highlighted,
with a beam cursor where the terminal supports it. In single-line
fields, Up/Down or Tab/Shift-Tab move between controls and Enter advances.
On checkbox fields, Space or Enter toggles the setting; arrows and Tab move
between controls without changing it.
Left/Right move the text cursor; multiline editors retain normal editing keys.
Long single-line values scroll horizontally instead of hiding the edited portion.
**Keep draft** retains form edits; **Discard this form** drops only the current
form. **Review & save** (Ctrl-S) lists changes and validation problems before a
confirmed save. Saving does not restart workers. Open a component under
**Services** and choose **Apply settings** to recreate it from saved configuration.
Connection labels describe the draft, not verified account connectivity.
API keys are masked and existing values are never prefilled into visible fields.
Telegram recipient mappings use a JSON object, for example
`{"you@example.com": "123456"}`; each ID must be on the allowed-user list.

**Activity** provides searchable, paginated tasks, memories, reminders, projects,
and script permissions. Open records for details and available confirmed actions:
retry/cancel/review tasks, add instructions, edit/pin/delete memories, cancel
reminders, inspect project objects, preview project deletion, and revoke or
block script permissions. Deletion previews retain shared objects and require a
new confirmation if the inventory changes. Failed deletions keep writers stopped;
use **Settings → Storage & recovery** to resume. After recovery, start any
previously stopped writers from Services.
Tab or click into long record details or deletion inventories, then use
arrows or PgUp/PgDn to read all content. Live log updates preserve the active
text control and cursor position. Invalid text or credential entries stay in
their dialog for correction. Confirmed writes temporarily hold navigation
until completion so a configuration reload cannot discard new edits; reads
remain navigable.

First-run setup uses these same native forms: acknowledge the safety notice,
configure identity and models, choose connections, review instructions, then
review and save. In model settings, **Generate secure database password** adds
a masked password to the form; keep the draft to retain it, or discard the form
to drop it. Generation refuses existing PostgreSQL data. An existing database
password must be rotated through **Settings → Credentials** instead.

The interface needs at least 76 columns and 24 rows. Smaller windows show a
resize notice without losing the draft. `./run.sh --plain` is an explicit legacy
line-oriented fallback; the native interface does not invoke those menus.

On Linux, if Docker requires sudo, setup asks permission before elevating.
Cached authorization is reused. When authentication is needed, the interface
yields to sudo's own terminal password prompt, then resumes; Assistant never
reads or stores the password. There is no need to run `sudo -v` beforehand or
run the entire setup as root. Expired authorization can be renewed at the next
command. Cancellation leaves that command unexecuted; failed Docker operations
are not automatically repeated. Sudo permissions and daemon availability are
still enforced by the host.

On macOS, start Docker Desktop (or your chosen Docker runtime) and wait until
it is ready. `docker info` should succeed in the same terminal. Use
`docker context ls` to check which daemon is selected; do not use sudo as a
workaround for an unavailable Desktop daemon.

If credentials are stored in a Docker volume, opening administration also
requires the original daemon: setup reads the saved credentials before showing
the home screen. A stopped runtime, changed context, registry credential-helper
failure, or missing volume can therefore prevent startup. The UI explains
recognized failures with **Retry** and **Quit** buttons pinned below the error.
Start Docker, wait until it is ready, then select **Retry**.
Do not delete the volume or switch credential storage to bypass an error; see
[Docker troubleshooting](docker-setup.md#troubleshooting-local-administration).

Open **Settings → Assistant instructions** to review the example or edit your
own instructions, then keep the draft and save. You can also create a nonempty
`data/config/AGENT.md` yourself. The task agent refuses to start without instructions.

## Start and inspect

Run Compose from the repository root, explicitly loading the local environment:

```bash
docker compose --env-file data/.env up -d --build
docker compose --env-file data/.env ps
docker compose --env-file data/.env logs -f task-agent
```

Setup writes an absolute `COMPOSE_FILE` setting in `data/.env`, pointing to
`src/docker-compose.yml` and the generated secrets overlay. The `email`,
`telegram`, and `voice` profiles are enabled by configuration when those
integrations are selected. Run `./run.sh tools` for guided status, logs, and
administration.

Starting the stack enables workers and may process existing inbox messages.
If you move the checkout, rerun setup to refresh its absolute deployment paths.

## Data locations

- `data/postgres/` — database files.
- `data/share/` — shared files accessible to agent file tools and sandbox runs.
- `data/private/` — artifacts, TLS material, and other private state.
- `data/ollama/` — local embedding model cache.
- `data/clamav/` — malware signatures.
- `data/.env`, `data/overrides.env`, `data/.secrets/` — local configuration and credentials.
- `data/.compose-secrets.yaml` — generated credential mounts.
- `data/config/agent.yaml` — runtime defaults, initialized from `src/config/agent.yaml`.
- `data/config/AGENT.md` — operator-maintained runtime instructions.

Tracked templates and defaults are under `src/config/`. Documentation is bundled
with the application image and retrieved using `help_search` and `help_topic`;
it is not mounted from the host or copied into the shared workspace.
`runtime_capabilities` reports live settings and limits. Help does not grant
access to host secrets, host source files or service controls.

Back up the database, shared files, private files, and local configuration and
credentials. Stop the stack before taking filesystem copies that must be
consistent. If using Docker-volume credential storage, back up that external
volume separately along with `data/.secrets/state.json`; copying `data/` alone
does not include those credentials. See [credential storage](security.md#optional-docker-volume-storage).
Keep backups protected as confidential data.

### Factory reset

Run `./run.sh reset` to permanently destroy and recreate the bundled PostgreSQL
cluster under `data/postgres/pgdata`. This deletes all databases and database
state in that cluster, including rows, indexes, ID sequences, and migration
history; startup recreates the schema and applies migrations. The reset makes
no backup and does not offer a selective data reset.

The reset preserves configuration and settings (`data/.env`, `data/overrides.env`,
`data/config/`), credentials (`data/.secrets/` or the optional credential Docker
volume), and `data/config/AGENT.md`. By default, workspace files in `data/share/`
and private attachments in `data/private/artifacts/` are also preserved.

For a clean install, choose **Settings → Storage & recovery → Clean install
(also delete files)**. In the traditional menu (`./run.sh --plain reset`),
answer yes to the clean-install option before typing `RESET`. This additionally
deletes **everything** in `data/share/` and `data/private/artifacts/`, including
shared and generated files, attachments, caches, archives and trash.
Configuration, credentials, `AGENT.md`, TLS certificates and other persistent
service data (such as downloaded models) remain untouched.

Once PostgreSQL is ready, the workspace indexer starts. If workspace indexing is
enabled in settings, it rebuilds its index from the preserved workspace files
in the background.

Factory reset refuses installations configured with a custom database; reset
that database separately. Type `RESET` at the confirmation prompt to proceed.

## Operations

```bash
docker compose --env-file data/.env down
docker compose --env-file data/.env up -d --build
docker compose --env-file data/.env logs --tail=100 task-agent
```

`down` does not remove bind-mounted `data/` files. Do not use
`down --volumes` unless intentionally deleting Compose-managed volumes such as
the transcription and sandbox runtime volumes. For service topology and trust
boundaries, see [architecture](architecture.md) and [Docker setup](docker-setup.md).
