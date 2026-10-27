# Security Guide

## Workstream deletion

Back up the database and runtime files before upgrading. Rebuild/start the
updated stack so migration 008 runs, then open
`./scripts/setup.sh tools` -> **Workstreams and data deletion**.
The existing terminal menus list workstreams, their parents and retained objects,
allow explicit attachments, and offer three deletion policies:

1. **Preserve shared objects (default):** delete the selected workstream and its
   descendants, their messages, and objects with no retaining workstream outside
   that tree. Shared objects survive; their links to the deleted tree do not.
2. **Delete all attached objects:** also remove shared objects from surviving
   workstreams. The preview identifies those affected workstreams.
3. **Delete the connected group:** recursively include all workstreams connected
   through shared objects, and all their descendants. A widely shared object
   can connect most or all of the installation; review the complete list.

The terminal shows per-table counts, shared-object relationships, local-file
counts, remote cleanup operations and blockers. It stops the writer services,
refreshes the preview, and requires a separate default-No confirmation of that
exact scope. A changed inventory invalidates confirmation. Running jobs/research
must finish first; deleting records is not a way to interrupt an in-flight call.
Shared reminders with queued work in the deletion tree must finish or be
cancelled first, rather than silently leaving a stranded queued reminder.
Schema changes are applied at normal service startup, not by a preview.
The helper uses task-agent's integration credentials and a one-shot mount of
the complete private artifact store; it does not publish an application port.

Filesystem cleanup checks exact managed paths and recorded SHA-256 digests,
rejects symlinks/protected documentation, and removes only named regular files.
Files changed outside tracked operations must be reconciled before deletion.
Artifact Markdown paths are resolved relative to the shared workspace. Voice
transcriptions record their Markdown digest and byte size; older voice artifacts
without a digest are checked against the exact transcript retained in their
metadata. A changed file remains blocked, not automatically trusted.
File tools and shared-workspace command snapshots record creations and changes
even when indexing is disabled. File-tool moves retain identity and sharing links.
Dropbox uploads use the recorded revision; changed or uncertain remote objects
must be reconciled, rather than deleting an unverified replacement. Calendar
events and Zoom meetings are deleted through their configured providers.

Each cleanup step is journaled before database erasure. Failures or interruptions
leave a pending/failed deletion with its identifiers and database records intact for retry;
some already-completed remote/file removals may be irreversible. Writer services
stay stopped when a deletion remains incomplete. **Resume an incomplete
deletion** retries that original scope. A successful receipt contains no deleted
message bodies or object contents. The old per-job deletion path refused workstream-owned jobs because it bypassed
shared-object retention.

Browsing lists object relationships; deletion preview additionally verifies
physical files and cleanup dependencies. Workstream command failures identify
the failing operation and report safe application errors without exposing raw
Docker, database or provider diagnostics. If deletion was already confirmed,
inspect **Resume an incomplete deletion** before starting another deletion.

This is **deletion, not rollback or universal erasure**. Sent email/Telegram
messages, provider/model copies, backups and logs outside managed storage cannot
be recalled. Imported Dropbox Inbox originals remain at their source. Retained
shared objects and other workstreams may contain quoted or derived information.
Untracked host writes, files outside managed roots, and sandbox effects outside
its shared mount have no reliable ownership and are not guessed or erased.
Arbitrary script network effects cannot be enumerated or undone by this model.
Empty containing directories may remain. Settings, credentials, service tokens
and installation-level state are not task data and are not deleted.

## Access model

The deployment publishes no application port. Browser administration, shared
editing, and the optional management API are removed.
PostgreSQL, Ollama, ClamAV and sandbox broker ports remain private. PostgreSQL
connections and sandbox RPC use verified TLS; the sandbox also requires its
service-scoped bearer token.

Use a dedicated assistant mailbox, sender/recipient allowlists, verified admin
identity, and explicit review for sensitive actions. See [email administration](email-administration.md).
Admin requests require a one-use OTP and request hash delivered in an independent
email to the configured admin, valid for ten minutes. From headers alone cannot
authorize requests. This trusts possession of the admin mailbox; a leaked
confirmation or compromised mailbox defeats that trust. The hash binds the
pending action and is not a second authentication factor. Approval credentials
are excluded from model context.

## Docker secrets

Run `./scripts/setup.sh` to configure credentials without editing `.env`.
Credentials are stored in gitignored `.secrets` files with directory mode `0700`
and file mode `0600`. A generated `.compose-secrets.yaml` grants read-only mounts
under `/run/secrets` only to relevant services. `.env` contains non-secret settings.

### Optional Docker volume storage

Choose **Move credentials to a dedicated Docker volume** at the wizard's final
review, or in the post-setup configuration menu. The choice takes effect only on
**Save**. Setup copies credentials into an installation-specific external Docker
volume, verifies their contents and volume-subpath support, then deletes the
original `.secrets` credential files. Failed migration leaves the original
configuration and credential files intact. Setup then recreates running services
that use credentials so they switch to the volume mounts; if that step fails, run
`./scripts/setup.sh secrets apply` to retry.

The generated overlay mounts a separate service-specific directory from the
volume read-only at `/run/secrets`, so services do not receive each other's
credentials. This requires Docker Engine and Compose versions supporting volume
subpaths. A disposable `python:3.12-slim` helper (pulled on first use if absent)
reads and updates the volume with no network, dropped capabilities, and container
logging disabled. Credentials are passed over stdin, not command arguments or
environment variables; setup does not stage plaintext copies on the host.
No additional persistent service is needed.

`secrets status`, `secrets update`, `secrets apply` and database rotation/recovery
continue to use the selected store. Docker must be available to manage credentials
once this option is enabled. If migration uses explicitly authorized noninteractive
sudo, subsequent volume access uses that authorization too.

The volume remains **plaintext**. This option keeps credentials out of ordinary
project reads, indexing and project archives; it does not protect against Docker
administrators or compromise of a service authorized to read a credential.
Setup metadata stays in `.secrets/state.json`; TLS files under `data/private/tls`
and OAuth tokens persisted in PostgreSQL are not moved by this option.
Keep `state.json`: it contains no credential values, but records the active volume,
setup completion and pending credential activation. Removing it prevents setup
from finding the selected store reliably. The otherwise empty `.secrets` directory
is retained for this metadata.

To reverse the move, choose **Move credentials back to plaintext host files** in
the post-setup menu or final review, then **Save and finish**. Setup restores
restricted `.secrets` files (`0700` directory, `0600` files), switches the Compose
overlay back to file-backed secrets, then handles the cleanup automatically: it
confirms the host files match, recreates running services with host mounts,
removes stopped containers of this installation that still reference the volume
(the next `docker compose up` recreates them), and destroys the volume. Setup
refuses to remove a volume used by containers from another Compose project. If
any step fails, `.secrets` stays authoritative, the volume is kept, and setup
retries removal the next time it opens. Moving back to the
volume later copies the current host values, including intervening updates.

Factory reset preserves both credential stores and `.secrets/state.json`; it
does not delete Docker volumes. Selecting the runtime-state reset category can
still clear cached provider tokens and pending approvals in PostgreSQL, as
described by that category; it does not erase the stored setup credentials.

Back up the credential volume **and** `.secrets/state.json`, `.env` and
`.compose-secrets.yaml`. Backing up the project directory alone no longer includes
these credentials. The volume is external, so `docker compose down --volumes`
does not remove it; explicit volume removal or pruning unused volumes can still
destroy it. Do not remove it until its credentials have been backed up or revoked.
Deleting source files does not remove copies in older backups.

### Runtime access

The sandbox initialization helper receives only its broker token and TLS pair.
It has no network or Docker socket and temporarily uses `DAC_READ_SEARCH` to read
protected host-owned sources. It stages root-owned keys/token (`0600`) into a
private Docker-managed volume. The capability-free broker mounts this volume
read-only; its runtime credentials are never made world-readable. The token is
also mounted only into its caller (`task-agent`).
Ephemeral command containers receive no credential mounts.

This is ordinary Docker Compose, not Swarm: both host source files and optional
volume contents are plaintext, not encrypted. Compose file-backed mounts do not reliably implement requested
`uid`/`gid`/`mode` remapping; verify effective access on the deployment host. Keep
host permissions restrictive and use FileVault/LUKS if disk encryption is needed.
Host administrators, Docker-daemon users and compromised authorized services can
read credentials. Secret files reduce accidental exposure, not those privileges.

Never bake secrets into images, build arguments, Git commits, shell arguments,
logs, or the agent-accessible `data/share` directory. Do not print fully rendered
Compose configuration from a legacy inline-secret deployment. Protect backups:
`.secrets`, TLS keys, database dumps and runtime OAuth token caches are confidential.
Providers can store refreshed access/refresh tokens in PostgreSQL; file-backed
initial credentials do not encrypt those database records.

## Dropbox file exchange

Use a Dropbox **App Folder** registration with only file metadata/read/write
scopes. The tools restrict paths to Inbox and task-specific Outbox destinations;
setup requires the operator to confirm App Folder registration. Do not use a
Full Dropbox app. No public links, webhooks or inbound ports are needed.

Dropbox downloads remain in private quarantine until mandatory clean scans and
successful conversion. Exports scan a bounded snapshot of eligible workspace
bytes, exclude internal/credential paths and never overwrite remote files.
Remote deletion is limited to regular Inbox/Outbox files, requires a current
listed revision, and must follow an explicit requester instruction.
Standing Outbox permission applies to any task when enabled, not just authenticated
admin tasks. Keep email sender restrictions appropriate for that authority.

Dropbox is not end-to-end encrypted. Imported content may be sent to configured
model/embedding providers. Scanning does not eliminate prompt injection, and
path restrictions cannot detect all secrets embedded in a deliverable. Local
removal does not retract remote files or revoke Dropbox authorization. See the
[Dropbox guide](dropbox.md) for setup, retries, revocation and retention.

## Updating credentials

```bash
./scripts/setup.sh secrets status
./scripts/setup.sh secrets update
./scripts/setup.sh secrets update OPENROUTER_API_KEY
./scripts/setup.sh secrets apply
```

Status shows names, not values. Updates use masked input and explicit confirmation.
Activation recreates affected running containers: a plain restart may retain the
old file bind mount after atomic replacement. Inactive profiles are not started.
Saved credentials can remain pending; inspect service health after activation.

Obtain replacement third-party credentials from the provider, apply and verify
them, then revoke the old credentials. The CLI does not rotate keys at providers.

Database-password updates use a coordinated maintenance flow: stop consumers,
alter the database role, replace the file, verify network authentication, and
recreate consumers. `POSTGRES_PASSWORD_FILE` alone only controls initialization,
not the password in an existing database. If interrupted, use
`./scripts/setup.sh secrets recover` to restore the previous credential and resume
the recorded services. Recovery data is protected locally; database and filesystem
changes are not one atomic transaction. Custom/external databases require a
deployment-specific rotation procedure and are explicitly refused by this flow.

## Transport security

External model and integration requests use HTTPS with normal certificate
verification. Runtime model URLs must be HTTPS; Zoho Calendar accepts only HTTPS
on supported regional Zoho hosts. Redirects are not followed for authenticated
model or sandbox requests. IMAP uses implicit TLS. SMTP requires certificate-
verified STARTTLS before login or message transfer and does not fall back to
plaintext.

PostgreSQL clients verify the service certificate and hostname; PostgreSQL rejects
non-TLS network connections. Sandbox RPC uses HTTPS with hostname verification and
a separate bearer token. Ollama and ClamAV do not use TLS in this deployment;
their ports are unpublished and their Compose networks are private and limited to
required clients. Docker bridge traffic is not itself encrypted, so the host and
Docker daemon remain trusted. Internal service certificates are stored under
`data/private/tls`; rotate the complete PostgreSQL/sandbox certificate set
together and recreate those services and their clients.

## Sandbox Docker socket

The sandbox broker mounts `/var/run/docker.sock` to create fresh per-command
containers. Anyone controlling that socket can control the daemon and potentially
mount host files or other containers' secrets. Read-only broker filesystems,
dropped capabilities, `no-new-privileges`, private broker networking and resource
limits do not eliminate this privilege. Run containers receive only the shared
workspace, not the socket or provider credentials. Run containers execute as root
to support package installation; their writable layer is ephemeral, but workspace
changes and outbound data transfer persist. Container escape remains a residual
risk.

To eliminate the mount exposure, disable `agent.sandbox.enabled` and do not start
the `sandbox` service; disabling the tool alone does not remove a running broker's
socket mount. Use a dedicated Docker host/daemon for stronger isolation. Validate
gVisor/networking overrides before production use.

## Retention and upgrades

Controlled-copy deletion cannot retract already-sent mail/model requests,
third-party data, external logs or backups. Source documents may cause relearning.
Read [deletion limits](email-administration.md#memory-and-deletion) and
[upgrade/rollback requirements](upgrading.md) before deployment.

## Responsible disclosure

Prefer private contact with the repository maintainer before public disclosure
of exploitable details. This project has no formal security response team;
responses are best effort. Do not include credentials or private user data in issues.