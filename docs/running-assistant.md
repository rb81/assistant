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

The installer downloads source, builds images in Docker, and starts management
and PostgreSQL. Fresh installations leave task workers stopped until setup is
complete. A configured installation starts its configured services.

Installation shows management-image build output and timestamped startup stages.
Every fifteen seconds, an unfinished stage reports that it is still pending. Health-wait
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

1. Configure identity, model credentials, and connections.
2. Review **Settings > Assistant instructions** and keep your draft.
3. **Review & save**, then choose **Services > Start app**.

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

To register an existing source checkout:

```sh
ASSISTANT_INSTALL_DIR="$PWD" sh install.sh
```

Rerunning the installer preserves existing source and data; **it does not update
them**. A conflicting launcher or unrelated destination is rejected.
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
For saved credential changes, use **Settings > Credentials > Activate pending**.
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
interrupted, recover through **Settings > Storage & recovery**, then explicitly
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

Back up first and stop the stack **before** moving or replacing source. Update
from a reviewed server revision using your chosen source-download or Git workflow,
preserving `data/`. The installer is not an updater.

From the updated installation root, run:

```sh
ASSISTANT_INSTALL_DIR="$PWD" sh install.sh
```

For an existing installation, this runs the shipped deployment migrations,
regenerates absolute paths and credential mounts, rebuilds images, and starts the
configured services without downloading over your chosen source revision.
Database migrations run during application startup.

If the checkout moved, resolve any launcher still pointing at the old location
before registering the new one. Do not remove an unrelated installation's
launcher. `server start` alone does not run the root `migrations/` scripts.

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
