# Security Guide

## Access model

The email-first deployment publishes no application port. Browser administration
and shared editing are removed. The optional API requires HTTPS and bearer
authentication for every route, including `/health`. See [API access](dashboard-and-api.md).
PostgreSQL, Ollama, ClamAV and sandbox broker ports remain private. PostgreSQL
connections and sandbox RPC use verified TLS; the sandbox also requires its
service-scoped bearer token.

Use a dedicated assistant mailbox, sender/recipient allowlists, verified admin
identity, and explicit review for sensitive actions. See [email administration](email-administration.md).
DMARC authenticates a domain, not an individual mailbox. Only trust receiving
providers that strip forged authentication headers and enforce the authorized
mailbox identity. HMAC-signed admin commands provide a separate verification path.

## Docker secrets

Run `./scripts/setup.sh` to configure credentials without editing `.env`.
Credentials are stored in gitignored `.secrets` files with directory mode `0700`
and file mode `0600`. A generated `.compose-secrets.yaml` grants read-only mounts
under `/run/secrets` only to relevant services. `.env` contains non-secret settings.
The sandbox initialization helper receives only its broker token and TLS pair.
It has no network or Docker socket and temporarily uses `DAC_READ_SEARCH` to read
protected host-owned sources. It stages root-owned keys/token (`0600`) into a
private Docker-managed volume. The capability-free broker mounts this volume
read-only; its runtime credentials are never made world-readable. The token is
also mounted only into its two callers (`task-agent` and the optional API).
Ephemeral command containers receive no credential mounts.

This is ordinary Docker Compose, not Swarm: host source files are plaintext, not
encrypted. Compose file-backed mounts do not reliably implement requested
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
Generated client tokens/signing keys are shown only after a separate reveal request.
Activation recreates affected running containers: a plain restart may retain the
old file bind mount after atomic replacement. Inactive profiles are not started.
Saved credentials can remain pending; inspect service health after activation.

Obtain replacement third-party credentials from the provider, apply and verify
them, then revoke the old credentials. The CLI does not rotate keys at providers.
API-token/signing-key changes invalidate clients still using the previous value.

Database-password updates use a coordinated maintenance flow: stop consumers,
alter the database role, replace the file, verify network authentication, and
recreate consumers. `POSTGRES_PASSWORD_FILE` alone only controls initialization,
not the password in an existing database. If interrupted, use
`./scripts/setup.sh secrets recover` to restore the previous credential and resume
the recorded services. Recovery data is protected locally; database and filesystem
changes are not one atomic transaction. Custom/external databases require a
deployment-specific rotation procedure and are explicitly refused by this flow.

## API exposure

Compose publishes the optional API only on `127.0.0.1`; remote LAN/VPN binding is
not supported by the managed setup. The API binds inside its container for Docker
port forwarding, but host access remains loopback-only. Use an SSH tunnel for
remote administration rather than widening the host bind. Do not expose the Docker
daemon through unauthenticated TCP: use OS-controlled sockets, SSH, or mTLS.

Import a trusted, hostname-valid TLS certificate for production. The wizard's
development certificate covers localhost/127.0.0.1 only; explicitly trust its
certificate in development clients instead of disabling certificate verification.
Keep API docs/OpenAPI disabled unless needed. A reverse proxy must retain a secure
HTTPS backend and correct, narrowly trusted forwarding configuration if one is
used on the host; HTTP is rejected by the API. The managed deployment does not
publish the API for remote clients.

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