# Security Guide

Start with these boundaries:

- Use an assistant-owned mailbox and restrict senders and outgoing recipients.
- Keep the [Management API](management-api.md) on loopback; use trusted SSH for
  remote access. Its bearer token grants instance-wide administration.
- Treat the host, Docker administrators, private management executor, and sandbox
  broker as trusted. Container restrictions do not remove Docker daemon authority.
- Keep credentials out of the workspace and protect backups. Secret files and
  credential volumes are not encrypted storage.
- Review approvals and external side effects. Deletion cannot recall sent
  messages or provider copies.

The sections below describe the exact behavior and recovery limits.

## Workstream deletion

Open `./run.sh` -> **Activity → Projects & workstreams**. Select a project
to inspect its relationships or **Preview deletion**. The native interface
exposes only the preserve-shared policy. Other deletion policies use the shared
Management API's preview and confirmed deletion operations. Explicit contact/file
attachments use the `projects.attach` domain action.

New explicit adoption is restricted to contacts and files. Inspection or edits
do not create retaining links. There are three deletion policies:

1. **Preserve shared objects (default):** delete the selected workstream and its
   descendants, their messages, and objects with no retaining workstream outside
   that tree. Shared objects survive; their links to the deleted tree do not.
2. **Delete all attached objects:** also remove shared objects from surviving
   workstreams. The preview identifies those affected workstreams.
3. **Delete the connected group:** recursively include all workstreams connected
   through shared objects, and all their descendants. A widely shared object
   can connect most or all of the installation; review the complete list.

The terminal shows per-table counts, shared-object relationships, local-file
counts, remote cleanup operations and blockers. Native deletion requires an
explicit confirmation with Cancel selected by default, then stops the writer
services and rechecks that exact scope. A changed inventory invalidates
confirmation and requires another preview. Running jobs/research
must finish first; deleting records is not a way to interrupt an in-flight call.
Shared reminders with queued work in the deletion tree must finish or be
cancelled first, rather than silently leaving a stranded queued reminder.
Schema changes are applied at normal service startup, not by a preview.
Deletion includes owned system threads and research execution jobs. It
invalidates saved Telegram selections and retains only hashed reply references,
not deleted conversation content. Where an object survives elsewhere, its
change history retains actor workstream identifiers and relevant object
differences, but clears deleted actor job references and copied reasons.
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
stay stopped when a deletion remains incomplete or completion cannot be verified.
**Settings > Server > Storage & recovery > Resume an incomplete project deletion**
retries that original scope. Only writers running when the current operation
began are restarted automatically; after successful recovery, use Services to
start writers left stopped by an earlier failure. A successful receipt contains no deleted
message bodies or object contents.

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

The deployment publishes the authenticated Management API only on host loopback
(`127.0.0.1:8765` by default), not the public network. PostgreSQL, Ollama, ClamAV and
sandbox broker ports remain private. PostgreSQL
connections and sandbox RPC use verified TLS; the sandbox also requires its
service-scoped bearer token.

Use a dedicated assistant mailbox, sender/recipient allowlists, verified admin
identity, and explicit review for sensitive actions. See [email administration](email-administration.md).
Email-delivered admin confirmations use a one-use OTP and request hash sent
independently to the configured admin, normally valid for ten minutes. Telegram
and authenticated Management API clients can decide version-bound approvals
through their respective controls; see [approval channels](email-administration.md#approval-channels).
From headers alone cannot authorize requests. Email confirmation trusts possession
of the admin mailbox; a leaked
confirmation or compromised mailbox defeats that trust. The hash binds the
pending action and is not a second authentication factor. Approval credentials
are excluded from model context.

## Docker secrets

Run `./run.sh` to configure credentials without editing `data/.env`.
Credentials are stored in gitignored `data/.secrets/` files with directory mode `0700`
and file mode `0600`. Generated `data/.compose-secrets.yaml` grants read-only mounts
under `/run/secrets` only to relevant services. `data/.env` contains non-secret settings.

### Optional Docker volume storage

After saving setup, open **Settings > Server > Storage & recovery > Move credentials to
a Docker volume**. This separate confirmed operation requires any settings draft
to be saved or discarded first. Setup copies credentials into an installation-specific external Docker
volume, verifies their contents and volume-subpath support, then deletes the
original `data/.secrets` credential files. A failed transfer leaves the original
configuration and credential files intact. Setup then recreates running services
that use credentials so they switch to the volume mounts; if that step fails, run
`./run.sh secrets apply` to retry.

The generated overlay mounts a separate service-specific directory from the
volume read-only at `/run/secrets`, limiting each service to its assigned
credentials (some credentials are shared by multiple services). This requires
Docker Engine and Compose versions supporting volume subpaths. A disposable
`python:3.12-slim` helper (pulled on first use if absent)
reads and updates the volume with no network, dropped capabilities, and container
logging disabled. Credentials are passed over stdin, not command arguments or
environment variables; setup does not stage plaintext copies on the host.
No additional persistent service is needed.

`secrets status`, `secrets update`, `secrets apply` and database rotation/recovery
continue to use the selected store. Docker must be available to manage credentials
once this option is enabled. The terminal interface does not provide a sudo
authorization flow; configure Docker access for the installation account first.

The volume remains **plaintext**. This option keeps credentials out of ordinary
file reads and indexing; it does not protect against Docker
administrators or compromise of a service authorized to read a credential.
Setup metadata stays in `data/.secrets/state.json`; TLS files under `data/private/tls`
and OAuth tokens persisted in PostgreSQL are not moved by this option.
Keep `state.json`: it contains no credential values, but records the active volume,
setup completion and pending credential activation. Removing it prevents setup
from finding the selected store reliably. The otherwise empty `data/.secrets` directory
is retained for this metadata.

To reverse the move, open **Settings > Server > Storage & recovery > Move credentials to
host files** and confirm. Setup restores
restricted `data/.secrets` files (`0700` directory, `0600` files), switches the Compose
overlay back to file-backed secrets, then handles the cleanup automatically: it
confirms the host files match, recreates running services with host mounts,
removes stopped containers of this installation that still reference the volume
(the next `docker compose --env-file data/.env up` recreates them), and destroys the volume. Setup
refuses to remove a volume used by containers from another Compose project. If
any step fails, `data/.secrets` stays authoritative, the volume is kept, and setup
retries removal the next time it opens. Moving back to the
volume later copies the current host values, including intervening updates.

Factory reset (`./run.sh reset`) permanently destroys and rebuilds the bundled
PostgreSQL cluster at `data/postgres/pgdata`, including all databases, rows,
indexes, sequences and migration history. It is not selective and makes no
backup. Configuration and settings, credentials (`data/.secrets/` or the
optional credential volume) and `data/config/AGENT.md` are always preserved.
The default database-only reset also preserves all workspace and private files.
The optional **Clean install (also delete files)** additionally deletes everything
in `data/share/` and `data/private/artifacts/`, including attachments, generated
files, caches and trash. TLS certificates and other persistent service data
are untouched in either mode. After PostgreSQL starts, the workspace indexer
starts and, when enabled in settings, rebuilds its index from the preserved files in the
background. A custom `AGENT_DATABASE_URL` or custom database settings are refused;
reset that database separately. The reset requires typing `RESET` to confirm.

Back up the credential volume **and** `data/.secrets/state.json`, `data/.env` and
`data/.compose-secrets.yaml`. Backing up the tracked source tree alone does not include
these credentials. The volume is external, so `docker compose --env-file data/.env down --volumes`
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
Compose configuration. Protect backups: `data/.secrets`, TLS keys, database dumps,
and runtime OAuth token caches are confidential.
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
./run.sh secrets status
./run.sh secrets update
./run.sh secrets update AGENT_LLM_OPENROUTER_API_KEY
./run.sh secrets apply
```

Status shows names, not values. Updates use masked input and explicit confirmation.
Activation recreates affected running containers: a plain restart may retain the
old file bind mount after atomic replacement. Inactive profiles are not started.
Saved credentials can remain pending; inspect service health after activation.

Which services receive each credential is decided by the code, not by you. When an
update changes those grants, **Start app**, **Restart** and **Apply settings**
regenerate `data/.compose-secrets.yaml` and the credential volume automatically,
and recreate only the services whose grants changed. To do the same after
`git pull` or from a schedule, run:

```bash
./run.sh secrets sync
```

It runs without a terminal, prints nothing when grants are already current, and
leaves credential values unchanged. To check every 15 minutes, add this line to
the crontab of the account that manages Docker, as permitted by host policy:

```cron
*/15 * * * * /path/to/assistant/run.sh secrets sync >> /path/to/assistant/data/secrets-sync.log 2>&1
```

Obtain replacement third-party credentials from the provider, apply and verify
them, then revoke the old credentials. The CLI does not rotate keys at providers.

Database-password updates use a coordinated maintenance flow: stop consumers,
alter the database role, replace the file, verify network authentication, and
recreate consumers. `AGENT_DATABASE_PASSWORD_FILE` alone only controls initialization,
not the password in an existing database. If interrupted, use
`./run.sh secrets recover` to restore the previous credential and resume
the recorded services. Recovery data is protected locally; database and filesystem
changes are not one atomic transaction. Custom/external databases require a
deployment-specific rotation procedure and are explicitly refused by this flow.

## Transport security

External model and integration requests use HTTPS with normal certificate
verification. Runtime model URLs must be HTTPS; Zoho Calendar accepts only HTTPS
on supported regional Zoho hosts. Redirects are not followed for authenticated
model or sandbox requests. IMAP uses implicit TLS. SMTP uses certificate-verified
implicit TLS on port 465 and requires STARTTLS on other ports before login or
message transfer; it does not fall back to plaintext.

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
limits do not eliminate this privilege. Run containers mount the writable shared
workspace, not host documentation, the socket or provider
credential mounts. Workspace contents are accessible to commands, so never
store secrets there. Run containers execute as root
to support package installation; their writable layer is ephemeral, but workspace
changes and outbound data transfer persist. Container escape remains a residual
risk.

Outbound traffic is deny-by-default: each command reaches the internet only
through a per-run HTTPS proxy that allows version-bound authorized hosts,
checks TLS SNI, refuses private addresses and sinks DNS. Approvals are per host,
so an approved host can receive any data the command sends it, and hosts on
shared CDNs may relay to others. The Docker host's bridge gateway may remain
reachable. See [sandbox network isolation](sandbox.md#network-isolation).

To eliminate the mount exposure, disable `agent.sandbox.enabled` and do not start
the `sandbox` service; disabling the tool alone does not remove a running broker's
socket mount. Use a dedicated Docker host/daemon for stronger isolation. Validate
gVisor/networking overrides before production use.

## Retention

Controlled-copy deletion cannot retract already-sent mail/model requests,
third-party data, external logs or backups. Source documents may cause relearning.
Read [deletion limits](email-administration.md#memory-and-deletion) and
[backup guidance](running-assistant.md#data-locations) before making operational
changes.

## Email and action screening

Configure these layers in **Settings → Safety & screening**. All
are optional; script authorization is required independently of screening
(final admin approval or version-bound supervision within an approved development
session), unless Script YOLO is explicitly enabled.
Telegram is not screened: only allow-listed, owner-proven accounts
can reach it.

### Mail quarantine

Fresh mail from a sender that is not accepted (not in `AGENT_EMAIL_ACTIONABLE_SENDERS`,
not replying on an open job's thread, and not a recipient of mail the assistant
sent on that thread) is moved to the quarantine folder
without storing an email record. The folder is `AGENT_EMAIL_IMAP_QUARANTINE_FOLDER`
(default `Quarantine`, created if missing). Rejected and held mail goes there
too. The downloader also scans existing and newly quarantined mail in bounded
batches using IMAP UIDs and UIDVALIDITY, without moving or marking it read.
Eligible messages receive a message-scoped review. The decision model screens
them when enabled; a malicious verdict or configured reject-on-error keeps the
message quarantined without an approval and sends a security notice. If screening
is disabled, the review says so; it cannot detect malicious messages.
Administrator-address claims, control messages, approval credentials and
ambiguous sender headers require direct inspection rather than release.
Screening and AI summaries send message content to their configured model
providers; held mail is retained in the pending approval payload. Quarantine is
not a promise that no copy is stored or sent externally. If a move fails, the error
is logged and the message stays in the inbox; the polling cursor still advances.
The downloader fetches with `BODY.PEEK`; the normal inbox path marks processed
messages as read.

### Admin sender authentication (SPF/DKIM)

This check is recommended and **on by default**
(`agent.security.email_authentication.enabled: true`, or
`AGENT_SECURITY_EMAIL_AUTHENTICATION_ENABLED`). Turn it off in `./run.sh` under
**Security** only if your admin mail provider cannot pass DKIM or SPF for your
address. When enabled, only email whose From
includes `AGENT_ADMIN_EMAIL`, including OTP approval replies, must pass sender
authentication. The downloader checks it itself through DNS. It does not trust
`Authentication-Results` headers. The email passes if either check below passes
for a domain aligned with its single From address:

- **DKIM:** a `DKIM-Signature` verifies against the public key published in the
  signer's DNS, and the signing domain (`d=`) is aligned with From.
- **SPF:** the `Return-Path` domain is aligned with From, and its SPF record
  authorizes the IP that delivered the email to your mail server. That IP comes
  from the topmost `Received` header with a public address. Sender-supplied
  HELO values are ignored.

Requiring both would reject legitimate mail. Forwarding breaks SPF, and some
providers sign with their own domain. Missing, misaligned or unverifiable
results, including DNS errors, fail. The containers need outbound DNS. At
startup the downloader warns if the admin domain publishes no SPF or DMARC
record. Make sure your admin mail provider signs with DKIM for your domain or
sends from SPF-authorized servers; otherwise your own email is quarantined.

A failure is quarantined before any OTP is issued, and the admin is notified.
Notices are rate limited by `notify_max_per_hour` and never contain an OTP.
For accepted non-admin email, these checks also supply evidence to enabled
decision-model screening; a failed check alone does not quarantine it.
Authentication proves a domain, not a person. Explicit administrative actions
still require approval. Ordinary task replies and established continuations
do not require routine confirmation; fresh admin conversations require it only
when `agent.security.confirm_new_admin_conversations` is enabled (default: true).
Disabling that setting does not disable authentication, content screening or
privileged-action approvals. Alignment uses a simple parent-domain match, not the
public-suffix list.

### Decision-model screening

With `agent.security.decision_model.enabled`, a decision model rates content as
**not malicious**, **possibly malicious** or **malicious**. It answers a verdict
question plus two yes/no risk flags.

| Target | Setting | What is screened |
|---|---|---|
| Inbound email | always on while the decision model is enabled | Fresh accepted task email, including mail claiming the admin address; covers selected headers, text, HTML and attachment names and types. Explicit `/admin` and approval-reply handling occur separately. |
| Outbound email | `screen.outbound_email` (off) | `email_send` calls with any external recipient. |
| Scripts | `screen.scripts` (off) | Registered script versions before run approval; cached per version, policy and screening context. |

Screening includes application-recorded context, not just the proposed content:

- Inbound email includes explicit sender-policy matches (separate from `*`),
  open-thread task context, whether the assistant previously emailed the sender
  on that thread, and DNS-verified authentication results (or an explicit
  unchecked status when authentication is disabled).
- Approving a held email records a **message-only** review in its request
  metadata. Future screening can see that a previous message from the claimed
  sender was approved, including on another thread. This is not a sender grant
  or identity verification; older approvals are not backfilled.
- Inbound and outbound email and scripts include bounded conversation excerpts
  from the relevant thread: up to three requests and two sent emails, with
  message bodies limited to 2,000 characters each. Outbound email and scripts
  also include the task summary, current request's admin-authentication state
  and reply route. Scripts include their stated purpose.

These excerpts are sent to the configured decision provider along with the
content. Attachment bytes, pending approval records and OTP credentials are not
added to screening context. Bodies, summaries and script purposes remain
untrusted evidence, not authority. Ordinary requests to use the assistant's
tools within a legitimate task are not, by themselves, prompt injection.
Sender policies, prior message approvals and expected replies never bypass
screening or authorize new effects. Script cache keys include context and prompt
revision so a finding is not reused for different task evidence.

- **Not malicious:** processed normally.
- **Possibly malicious:**
  - Inbound email is quarantined, and the admin receives an OTP approval
    (default expiry 24 hours, `approval_ttl_minutes`).
  - Outbound-email calls pause for review of the identical call once.
  - Script requests show the finding alongside explicit once/always/deny
    choices. Always is bound to a pinnable code bundle and exact invocation;
    an explicit administrator grant bypasses repeated model checks for that version.
- **Malicious:** quarantined or blocked, and the admin is notified.

`malicious_threshold` applies to P(malicious), and `possibly_malicious_threshold`
to P(malicious) + P(possibly malicious). Any flag at or above `flag_threshold`
also holds. `on_error` (`review`, `reject` or `allow`) decides what happens when
the model is unreachable, refuses or returns malformed output.

#### Script analysis

Before a script is screened, `script_analysis.py` statically builds a manifest.
The decision model sees the manifest together with the source.

- **Which commands:** Python (files, `-c`, `-m`), shell scripts and `-c`
  strings, Node, and other interpreters. Package installs and direct
  workspace executables (shebang or binary) are included too. Plain commands
  such as `ls` are not screened, and executable files outside the shared
  workspace are not inspected. This is not exhaustive command interception.
- **Python:** parsed with `ast`. The manifest records:
  - imports, split into stdlib, third-party and local; local modules in the
    shared workspace are followed, up to `scripts.max_files` files;
  - risky calls, alias-resolved (process, network, code execution, file
    deletion, environment);
  - code obfuscated with `exec`/`eval`.
- **Shell:** commands, `curl | sh`-style remote execution, decode-and-run pipes,
  destructive deletions and package installs.
- **JavaScript:** a regex-based scan, following static local relative imports.
- **All languages:** URLs, hosts, IP addresses and sensitive paths.

Deterministic **hard signals** raise a not-malicious verdict to possibly
malicious:

- `code_not_inspectable`: missing file or stdin;
- `unparseable_code`;
- `obfuscated_code_execution`;
- `remote_code_execution`;
- `destructive_command`;
- `binary_executable`;
- `sensitive_path_access`;
- `host_not_allowed`, only when `scripts.allowed_domains` is set.

The persistent approval identity includes every analysed code file, argv,
workdir and an explicit approval-policy revision, not the Docker image ID or
the entire security configuration. Routine administrator-installed upgrades
are trusted maintenance and preserve unchanged approvals. Each execution is
separately bound to the current sandbox image/runtime identity and revocation
epoch; stale execution authorizations are rejected. Current blocks and runtime
restrictions remain enforced. Code changes require fresh
run approval and script-scoped host grants, or a fresh supervisor review within
an admin-approved development session. Execution uses a read-only code
snapshot outside the writable workspace; data inputs remain mutable.
Legacy approvals are adopted in place only for identical code and invocation;
permissions and denials are not merged across versions. Ambiguous legacy
history requires administrator reconciliation. See [sandbox approvals](sandbox.md)
for upgrade behaviour and the explicit approval-policy revision.
Always approval is withheld for unsupported or uninspectable dependency graphs.
Successful screening findings are cached with a per-version lock to prevent
duplicate concurrent checks. Provider outages are not cached as successful checks.
Static analysis remains best effort, not exhaustive command interception.

#### YOLO mode

`agent.security.allow_possibly_malicious` (off by default) lets possibly
malicious inbound and outbound email proceed without screening approval. The admin
is notified afterwards. This email setting does not bypass script permissions,
and malicious script versions remain blocked. Setup shows a strong
warning in the legacy host wizard; current management changes use the shared
review/confirmation flow. Services log a warning at startup while YOLO mode is on.

The separate `agent.security.script_yolo` (also off by default) disables script,
host and development-supervisor approvals, including script screening. Setup
uses the management review/confirmation flow, and services log a startup warning.
The proxy allows any public HTTPS hostname in this mode, but still enforces explicit
blocks, private-address restrictions and SNI checks; sandbox isolation and limits
remain. Existing installations are not silently opted in. Turning it off creates
no permanent grants. See [supervised development sessions](sandbox.md#supervised-development-sessions)
for the normal bounded workflow and configurable supervisor model.

#### Providers

By default (`provider: openrouter`), requests go to OpenRouter's Decisions API,
`https://openrouter.ai/api/alpha/decisions`. OpenRouter normalizes every decision
model to one request and response format. Use a decision-capable model slug,
not an arbitrary chat model; the configured default is
`perplexity/pplx-decider-v1.2`. Check current provider availability with
`GET https://openrouter.ai/api/v1/models?output_modalities=decisions`. Pin a
version rather than a `~...-latest` alias so thresholds stay calibrated. This
route reuses `AGENT_LLM_OPENROUTER_API_KEY`. Setup already grants the model keys to the
downloader and Telegram receiver for intent routing, for both `data/.secrets`
files and the credential volume.

Direct provider APIs are optional and use the `AGENT_SECURITY_DECISION_MODEL_API_KEY`
credential:

| `provider` | Format | Default endpoint / model |
|---|---|---|
| `perplexity` | System One | `https://api.perplexity.ai/v1/decisions`, `pplx-decider-latest` |
| `jev` | System One | `https://jevmodel.org/v1/systemone`, `jev-latest` |
| `clef` | System One | `https://api.deepinfra.com/v1/decisions` (DeepInfra), `Cloudflare/clef` |
| `openai` | OpenAI Decisions | `https://api.openai.com/v1/decisions`, `gpt-6-luna` |
| `custom` | System One | set `url` and `model` |

To add a direct format, register a translator in `security_screening.py`.
Endpoints must use HTTPS. Screened content is sent to the provider, so review its data handling first.

**Limits:**

- A classifier is a risk filter, not a security boundary. Tool guardrails,
  recipient policy and the sandbox still apply.
- Attachment contents are not screened; they are malware-scanned separately.
- Fresh accepted admin task email is screened when enabled; routine confirmation
  for new admin conversations is a separate setting, not a screening bypass.
- Script permission decisions use the same authenticated shared record over email,
  Telegram, and authenticated Management API clients; ordinary job resumes do
  not grant script permissions.

## Responsible disclosure

Prefer private contact with the repository maintainer before public disclosure
of exploitable details. This project has no formal security response team;
responses are best effort. Do not include credentials or private user data in issues.