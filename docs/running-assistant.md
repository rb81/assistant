# Running Assistant

## Requirements and setup

Use macOS or Linux with **Docker and Docker Compose running**, plus curl and tar.
Your account must already have Docker access without sudo. The installer does
not install Docker or change host permissions. No host Python, Node, Rust, or
Git installation is needed.

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/rb81/assistant/main/install.sh | sh
"$HOME/.local/bin/assistant"
```

The installer installs **or updates** source, builds images in Docker, and starts management
and PostgreSQL. Fresh installations leave task workers stopped until setup is
complete. A configured installation starts its configured services.

Installation shows four concise stages and retains startup diagnostics in
owner-only `data/logs/install.*` files. Set `ASSISTANT_VERBOSE=1` to stream
details; `NO_COLOR` disables color. Every fifteen seconds, unfinished startup
reports that it is still pending. The diagnostic log's health-wait
updates name the pending services and show how many are ready. Containers can
be running before their health checks pass; workers must complete a polling
cycle and verify database access, which may take longer if they pick up work.
After image preparation,
health checks get up to five minutes on a fresh install and ten minutes on a
configured one. Transient unhealthy checks do not end that wait early. Exited
services, failed helpers, and expired deadlines fail explicitly; installation
does not report success before readiness is verified.

The installed `assistant` command opens the terminal interface; from a source
checkout, use `./run.sh`. The interface uses the local Management API for
settings, service controls, and activity. The launcher handles local deployment
bootstrap; the interface does not manage Docker or PostgreSQL directly. For the
desktop client, including its connection and usage instructions, see the
[client repository](https://github.com/rb81/assistant-client).

In the terminal interface:

1. Choose a model and API key, then set identity and timezone. Connections are optional.
2. Review **Settings > Assistant > Standing instructions** and keep your draft.
3. **Review & save**, then explicitly choose **Save & start**. To save without
   starting workers, choose **Save configuration** and start from Services later.

Open **Settings > Models & Voice > Model settings overview** to see all saved
model roles, providers, limits and tuning on one searchable page, including
disabled features. Editor links keep the existing review/save workflow.
The overview excludes unsaved drafts and does not verify running models or
provider availability. It requires the updated Management API.

The task agent requires nonempty instructions in `data/config/AGENT.md`.
Starting workers may process existing inbox messages: use an assistant-owned
mailbox and restrict senders before starting.

The [macOS app](https://github.com/rb81/assistant-client) provides another
interface to the same API. See [configuration](configuration.md) for settings
and credentials, or [Docker troubleshooting](docker-setup.md#troubleshooting-local-administration)
if administration will not open.

### Installation options

Default location: `~/.local/share/assistant`. To choose another path or source ref:

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/rb81/assistant/main/install.sh |
  ASSISTANT_INSTALL_DIR="$HOME/assistant" ASSISTANT_VERSION=main sh
```

Running the same installer again discovers the installation through
`~/.local/bin/assistant`, downloads the latest commit on its configured ref, and
rebuilds/reconciles the services. A local `sh install.sh` also recognizes its own
source directory when no launcher exists. `ASSISTANT_INSTALL_DIR` overrides
discovery; a conflicting launcher is rejected rather than creating a second install.

The source/ref and exact installed commit are retained in owner-only
`data/.installation`. The default source is `github:rb81/assistant` and the default
ref is `main`. An existing Git checkout uses its tracked upstream, or requires an
explicit source when it has none. For another Git source, set
`ASSISTANT_SOURCE` and `ASSISTANT_VERSION` when installing (Git is needed for
non-GitHub sources). Subsequent runs remember that source.
Do not include credentials in a source URL; use the host's normal SSH/Git access.
Git updates must fast-forward and the checkout must be clean. The installer never
resets history or discards edits. Managed archive installations likewise reject
modified source. Old non-Git installations without a source manifest need a
one-time `ASSISTANT_ADOPT=1` after reviewing/backing up their source.

`ASSISTANT_UPDATE=0 sh install.sh` explicitly repairs/rebuilds the existing source
without downloading an update. This is also useful for developer checkouts.
Automatic source updates build installation-specific, revision-tagged management
images rather than replacing another installation's tag. If you deliberately
provide an externally managed `ASSISTANT_MANAGEMENT_IMAGE`, update that image
through its own deployment process and use `ASSISTANT_UPDATE=0`; the installer
will not overwrite it with a local source build.
Add `~/.local/bin` to PATH to use `assistant` directly, or use the full path.
In a checkout, `./run.sh` is equivalent.

### Terminal controls

Use a terminal at least **76 columns by 24 rows**.

| Control | Action |
|---|---|
| Tab / Shift-Tab, arrows | Move between controls |
| Enter | Open or confirm the focused action |
| Space | Toggle a checkbox |
| Esc | Go back or close help |
| F1 | Explain the focused option |
| F5 | Refresh |
| Ctrl-S | Review and save |

Forms retain a session draft. **Keep draft** does not write settings;
**Review & save** validates and confirms changes. Saving alone does not restart
workers: use **Services > Apply settings** on affected components.
**Save & apply** deliberately saves and activates together.
For saved credential changes, use **Settings > Server > Credentials > Activate pending**.
Restarts and activation can interrupt active work.

## Start and inspect

From the installation root:

```sh
./run.sh                         # Open the terminal interface
./run.sh server start            # Prepare saved deployment files and start services
./run.sh server status
./run.sh management health
./run.sh management services
./run.sh management logs task-agent
./run.sh server stop             # Stop the whole stack; preserve data
```

The `server` commands are host-side launcher operations using Docker; they are
explicit operator controls for starting, inspecting, or stopping the deployment.
The terminal interface itself uses the Management API. For advanced credential
grant maintenance, `./run.sh secrets sync` is likewise a host-side operator
command, not a TUI action.

The TUI's **Services > Stop app** stops workers but keeps management available.
Closing a client never stops services. Docker restart policies keep started
services running independently; Docker itself must start after a host reboot.

**Activity** provides tasks, memories, reminders, projects, and script permissions.
Record deletion uses a fresh preview and confirmation. If project deletion is
interrupted, recover through **Settings > Server > Storage & recovery**, then explicitly
restart any writers that remain stopped.

### What service health verifies

Worker health verifies recent completed polling and an authenticated database
read, **not successful jobs or working integrations**. Long jobs can report
unverified progress without being cancelled or restarted. Inspect logs and actual
task outcomes before treating a component as ready.

ClamAV checks daemon responsiveness, not signature freshness; Ollama checks model
inventory, not inference. One-shot services are evaluated by exit status.
Disabled profiles are not failed services. See [Docker setup](docker-setup.md)
for service details.

### Noninteractive administration

CLI, TUI, and desktop clients use the same Management API. Pass private JSON
through stdin, not command-line arguments:

```sh
./run.sh management records tasks
./run.sh management query < query.json
./run.sh management operation < operation.json
./run.sh management operation-status --operation-id OPERATION_ID
```

Writes require operation-specific confirmation; configuration saves also need a
current preview and revision. Accepted writes are not necessarily complete:
inspect their operation status. See the [API reference](management-api.md) and
[client guide](building-clients.md) for contracts, polling, and recovery.

### Updating or moving a source installation

Back up persistent data before upgrading, especially before database migrations.
Run the same installer used for initial installation:

```sh
sh install.sh
```

The installer resolves one exact source commit, validates/stages it, and builds
images before stopping the old stack. It then activates the source, runs shipped
deployment migrations, regenerates configuration/credential mounts, and starts
the configured services. Database migrations run during application startup.
Expect an interruption to active work during cutover. Success also requires
checking the running API's revision, structured settings, services, and (after
setup) database/workspace reads through the persistent reader.

Downloads/build failures leave the running installation untouched. After cutover,
`data/.install-pending` records an incomplete update, and the next installer run
finishes that exact revision after verifying that its source is complete. If a
Git checkout has already fast-forwarded to a fix, rerun `sh install.sh` from
that clean checkout. The installer stages and builds the checked-out revision
before replacing the pending record. It rejects older or divergent checkouts
and does not reset Git or roll back data. A partial archive activation stops
explicitly for recovery instead of starting mixed files. Archive updates retain
previous source in the reported sibling
`*.previous.*` directory; Git checkouts record the previous commit in
`data/.install-previous-revision`. Retained source backups are owner-only and may
be removed manually after validating the update; they do not contain `data/`.
Previous source is **not** a database backup. There is no automatic database
rollback; downgrading across incompatible schema changes requires a compatible
backup and an explicit recovery procedure.

The installer writes recovery metadata to a temporary file before publishing it.
If a metadata write fails, investigate the filesystem error before retrying;
the installer does not stop services during that attempt.

If moving an installation, stop the stack first and preserve all of `data/`.
Resolve any launcher still pointing at the old location
before registering the new one. Do not remove an unrelated installation's
launcher. `server start` alone does not run the root `migrations/` scripts.

Management helpers mount current source into the task-agent image, but that does
not install changed Python dependencies. If a management read reports a missing
runtime module (for example `pydantic`), ensure the updated
`src/agent/requirements.txt` is present and rebuild the image with
`./run.sh server start`. Restarting existing containers alone is insufficient.
Rebuilding and starting services can interrupt active tasks.

## Data locations

| Path | Contents |
|---|---|
| `data/postgres/` | PostgreSQL data |
| `data/share/` | Workspace files |
| `data/private/` | Retained attachments, TLS material, and private state |
| `data/ollama/`, `data/clamav/` | Model cache and malware signatures |
| `data/.env`, `data/overrides.env` | Deployment variables and application overrides |
| `data/.secrets/` | Protected credentials and setup state |
| `data/.compose-secrets.yaml` | Generated credential mounts |
| `data/.management-token` | Administrative API token; never commit or share |
| `data/config/AGENT.md` | Assistant instructions |

Tracked defaults are under `src/config/`. Documentation ships in the application
image; it is not mounted from the host or copied into the workspace.

**Back up all of `data/` securely.** Stop the stack before filesystem copies that
must be consistent. With optional Docker-volume credential storage, also back up
that volume separately: `data/` alone does not contain its credentials. Retain
`data/.secrets/state.json`. See [credential storage](security.md#optional-docker-volume-storage).
Credential files and Docker volumes are not encrypted storage.

### Factory reset

`./run.sh reset` permanently destroys and recreates the bundled PostgreSQL
cluster: **all databases, records, indexes, sequences, and migration history**.
It makes no backup. Custom database installations must reset their database
separately. Confirmation requires `RESET`.

| Reset mode | Deletes | Preserves |
|---|---|---|
| Factory reset | Bundled database contents | Settings, credentials, instructions, workspace files, attachments |
| **Storage & recovery > Clean install (also delete files)** | Database plus everything in `data/share/` and `data/private/artifacts/` | Settings, credentials, instructions, TLS, and other service data |

Both modes offer **Keep mailbox position (recommended)**, enabled by default.
This keeps only email UID checkpoints, not messages or tasks. Turning it off
re-reads the configured mailbox; already lost checkpoints cannot be recovered.
Email intake follows UIDs, not read/unread flags.

Telegram skips queued updates on its first post-reset poll. Remote email and
Telegram history are not deleted. If replay-prevention state cannot be restored,
application services remain stopped: resolve the error and rerun reset.
Enabled workspace indexing rebuilds from preserved files.

## Advanced host operations (escape hatch)

These Docker Compose commands are an operator escape hatch for managing the
host deployment directly. They are not steps in the terminal or desktop
interface; routine administration should use the Management API-backed clients.
From the installation root:

```sh
docker compose --env-file data/.env ps
docker compose --env-file data/.env logs --tail=100 task-agent
docker compose --env-file data/.env down
docker compose --env-file data/.env up -d --build
```

Setup writes `COMPOSE_FILE` and selected profiles into `data/.env`.
`down` preserves bind-mounted data. Do not add `--volumes` unless deliberately
removing Compose-managed runtime volumes. See [architecture](architecture.md)
and [Docker setup](docker-setup.md) for topology and trust boundaries.
