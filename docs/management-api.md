# Management API reference

One containerized API serves the CLI, TUI,
[Assistant for macOS](https://github.com/rb81/assistant-client), and
[independent clients](building-clients.md).
The terminal interface is an API client: it does not manage Docker, PostgreSQL,
or host files directly. The installed launcher handles local deployment
bootstrap; client reads and writes go through the authenticated API.
The TUI builds its settings pages and help from the server's configuration
catalog, groups, fields, and help, and reviews edits through the configuration
preview API. Draft previews and database-password generation run as background
tasks with status feedback. Feature parity with the desktop client is not implied.

## Connection

Install the server using [the server guide](running-assistant.md).
`./run.sh server start` starts the Management API with Docker; no host Python
or separate daemon is needed. It is published only on `127.0.0.1:8765`
(`ASSISTANT_MANAGEMENT_PORT` can change this).

Every endpoint requires `Authorization: Bearer TOKEN`. The installed
`~/.local/bin/assistant connect` launcher discovers the current port and token.
Remote clients invoke that fixed launcher using trusted, key-authenticated SSH
and own a loopback tunnel. Never expose the API publicly or bypass SSH host trust.
Discovery version 1 and Management API version 2 are different contracts.

The token in `data/.management-token` grants instance-wide administration.
Client IDs are delivery identities, not credentials. Keep tokens out of URLs,
arguments, logs, and version control. The native client retains them in memory.
Closing the client closes its tunnel, not the server.

## Public resources

All paths below are relative to `/v2`. `/v1/schema` returns authenticated OpenAPI
for both protocol versions. Existing `/v1` reads remain compatible.

| Resource | Purpose |
|---|---|
| `GET health` | Protocol, immutable installation identity, capabilities |
| `GET configuration` | Revision, setting catalog with help, navigation `groups`, instructions, original `agent_yaml`, backup names |
| `GET configuration/catalog` | Source-free navigation/search metadata; no effective values or instructions content |
| `GET configuration/settings?group={id}` | Revision-bound effective values for a page and its children; omit group for all structured settings |
| `GET configuration/instructions` | Lazily read editable standing instructions and revision |
| `POST configuration/preview` | Validate the exact draft; return changes, problems, and `preview_id` |
| `GET configuration/agent-yaml/backups/{name}` | Read one retained fixed-name source backup into a new draft |
| `GET configuration/check` | Saved-file validation; add `?runtime=true` for executor-backed container checks |
| `GET services` | Stable service state/health for CLI/TUI consumers |
| `GET services?details=true` | Add available container age information for a graphical status page |
| `GET services/{id}/logs` | Bounded, redacted logs |
| `GET credentials` | Credential names and activation states, never saved values |
| `GET credentials/storage` | Credential storage mode and pending retirement |
| `POST credentials/database-password/generate` | New password generation before a database exists |
| `GET conversations` | Chat threads and scheduled reminder runs with their latest task (`page`, `query`, `channel`) |
| `GET records/{collection}` | Paged tasks, projects, memories, reminders, scripts, research |
| `GET records/{collection}/{id}` | Record details |
| `GET records/{collection}/{id}/children` | Task/research activity or project objects |
| `GET tasks/{id}/messages` | Conversation transcript, paged chronologically |
| `GET tasks/{id}/snapshot` | Newest message page, bounded activity, and task state in one repeatable-read transaction |
| `GET events` | Authenticated resumable SSE invalidations over the same SSH tunnel |
| `GET tasks/{id}/activity` | Tool calls across the whole thread (`since`) |
| `GET tasks/{id}/deletion-preview` | Deletion inventory, blockers, digest |
| `GET tasks/summary` | Task counts |
| `GET approvals` | Version-bound reviews and non-actionable explanations |
| `GET permissions/{view}` | Sandbox host rules or history |
| `GET projects/{id}/files` | Attached files and ownership metadata |
| `GET projects/{id}/files/{file_id}/content` | Bounded base64 content chunk |
| `GET projects/{id}/deletion-preview` | Exact project deletion inventory |
| `GET projects/pending-deletions` | Incomplete deletion recovery |
| `GET clients` | Channel registrations and availability |
| `GET clients/{id}/messages` | Poll and renew availability |
| `GET clients/{id}/history` | Retained delivery history with `before_id` |
| `POST clients/{id}/acknowledgments` | Acknowledge rendered deliveries |
| `GET domains` | Read/write action catalog and argument schemas |
| `POST query` | Typed, side-effect-free domain queries |
| `POST operations` | Confirmed, typed asynchronous mutation |
| `GET operations` and `GET operations/{id}` | Retained outcomes and progress |

Records and conversations use `page`/`query` (conversations: 25 per page, newest
activity first). Omit `channel` for both chats and reminder runs, use `chat`
for chats across all channels, `reminder` for scheduled runs, or `client`,
`telegram`, or `email` for a single chat channel. Approvals
use `limit`/`offset`; transcripts use 50-message pages. Task activity returns a
bounded window of up to 200 calls, with `since` and `truncated`, not numbered
pages. Workspace domain actions use their documented offsets/limits. Never
assume one response is the whole dataset.

## Writes and recovery

Submit an immutable payload to `operations` with a random `Idempotency-Key`.
HTTP 202 means accepted, not completed. Poll until `succeeded`, `failed`, or
`interrupted`. Persist non-secret keys and operation IDs, not payload bodies.
Unknown outcomes must be inspected, never blindly retried.

The last 500 operation statuses are retained; evicted keys remain protected
against re-execution for at least 30 days. An evicted result may be unrecoverable.
Control-plane restarts require rediscovery, identity verification, and status
inspection; a closed connection is not proof of success.

Supported operations include `configuration.save`, `service.lifecycle`,
`credential.replace`, `credential.apply`, `credential-storage.move`,
`database.reset`, `database.recover`, `search.maintenance`, `record.mutate`,
`file.upload`, `file.attach`, `project.delete`, `project.resume`, `task.start`,
`task.reply`, `task.delete`, `research.start`, `permission.control`,
`approval.decide`, `domain.mutate`, `client.register`, `client.unregister`,
and `client.delivery-resolve`. Canonical contracts live in
`src/agent/src/assistant_agent/management_contracts.py`.

### Structured settings and activation

Ordinary clients use `configuration/catalog`, section reads and the separate
instructions resource, never the legacy bulk/source endpoint. Section reads must
match the catalog revision; stale drafts require reload, not silent merging.
Saved secret values are never returned. Groups include a stable `destination`:
`assistant`, `models`, `connections`, `safety`, or `server`. Approvals appear
under Safety; conversation naming appears under Models. Use the catalog's
group and field presentation metadata rather than hard-coding the layout.
App preferences belong to each client, not the server.

Set `apply: true` on a reviewed configuration save to activate explicitly.
The first setup starts/builds required workers; later override-only saves
recreate application services. Enabled integrations can begin processing messages.
Saving without activation remains supported. Report activation failures and
uncertain outcomes separately from saved state.

### Legacy configuration and source backups

Preserve the revision from `configuration`; send only edited setting values,
optional instructions, and optional original-format `agent_yaml` text to preview.
Submit the same draft with `preview_id` and confirmation to `configuration.save`.
Edits invalidate review; stale revisions are rejected. Set `apply: true` for a
reviewed Save & Apply.

Changed YAML is validated against existing setting paths/types. Credentials
belong in the credential store, not source YAML. A source change and its backup
are saved in the same rollback-capable transaction. Three previous versions are
retained as `agent.yaml.bk_YYYYMMDD_HHMMSS_microseconds` (UTC); loading a backup
does not restore it until a new preview/save. No-op saves create no backup and
trigger no activation after setup. An explicitly activated first setup starts
workers even when only setup acknowledgment changed. YAML changes rebuild application images; override-only
activation recreates application services. Failed activation leaves the saved
configuration and backup available and reports failure, not success.

The public API remains read-only against source. Only its private typed executor
has a narrow writable bind for `src/config`; the installation root remains
read-only. Existing installations must recreate management services from the
updated Compose definition before source saves can work. Ordinary saves without
`apply` retain separate activation. Overrides still take precedence over YAML.

### Configuration metadata

Each setting has `label`, `value`, `default`, `data_type` and `metadata`. Metadata
includes `level` (`basic`/`advanced`), `help` (plain-language explanation), and,
where relevant, `section` (for example a calendar provider), `choices` and
`warning`. `groups` lists every setting group in display order as `{id, title,
help, parent, section, basic}`: top-level pages have no `parent`; nested groups
(such as extra calendar accounts or catalog tuning) name the page they belong to,
and `section` places them under that page's matching field section.

### Persistent reads and live invalidations

Updated Compose deployments route ordinary conversation, task, record, and client
delivery reads through a private pooled read service, independently of deployment
mutations. It has database/config access and read-only shared files, not a Docker
socket or installation-root mount. Reconciliation and approval reviews remain in
the isolated executor because they can write or trigger external effects.
An unavailable reader returns an explicit error, never a disposable-container
fallback. Older deployments retain their prior read path until upgraded.

Negotiate `tasks.snapshot` and `events.stream` capabilities. SSE uses the usual
Bearer header; resume with `Last-Event-ID`. The bounded buffer retains 256
invalidations and emits `resync` after a gap, restart, or invalid cursor.
Kinds are `state.changed`, `approvals.changed`, `server.unavailable`, and `resync`;
payloads contain no conversation content. There are at most eight active streams,
with 15-second keepalives. Clients must reconnect with bounded backoff and refresh
authoritative state on resync. The watcher checks opaque PostgreSQL transaction
snapshots and approval revisions once per second; invalidations intentionally
include unrelated writes and are not an exact changed-record feed.

Keep delivery polling/lease renewal and acknowledgments active even with SSE.
Polling also remains the fallback after stream failure. A snapshot is a bounded
newest-page read, not a transcript delta cursor; use existing message pagination
for older history.

### Chats and deletion

`GET conversations` lists chat threads and scheduled reminder runs, titled by
their opening message, with channel, message count,
last activity, and the latest task's ID, status, workstream title and
`workstream_name_status` (`temporary`, `pending`, `naming`, `named`, `failed`). Read the
transcript with `tasks/{latest_task_id}/messages`. Use `task.reply` for chats
only. Scheduled reminder runs are read-only; use `task.start` to ask about a
run in a new chat.
Email bodies in the transcript have quoted reply history removed, so each
message shows only what its sender wrote.

Transcript messages also include `recipients`, `subject`, and `external` for sent
email. `external` is true if any To/Cc recipient differs from the configured
administrator (case-insensitive). The `client` channel means an Assistant client,
not necessarily the Mac reading the transcript.

`attachments` contains filename, content type, size and voice-note metadata when
recorded, plus `available`. Available attachments have either `project_id` and
`file_id` (read with `projects/{project_id}/files/{file_id}/content`) or
`workspace_path` (read with the `workspace.read` domain query). Internal and
missing paths are not exposed as downloadable references. Inline email content
that was not retained as a file remains unavailable. Downloads are authenticated,
chunked and digest checked; never interpret server attachment paths as local URLs.

`GET tasks/{id}/activity` (capability `tasks.activity`) returns the tool calls
of every job in the task's thread, oldest first, as `{rows, total, truncated}`.
Each row has `id`, `task_id`, `created_at`, `tool_name`, `tool_action`, `status`
(`running`, `succeeded`, `failed`, `interrupted`), `input`, `output` (redacted previews, up to
4000 characters plus an ellipsis when truncated) and `duration_ms`.
Pass `since` (ISO 8601) to skip calls before
the earliest loaded message. Interleave rows with messages by `created_at` to
show what the agent did between replies.
Unpaired calls become `interrupted` when another call starts in the same job or
the job is no longer running, rather than displaying a permanent spinner.

`task.start` creates a conversation and lets the server assign its workstream;
the macOS composer never asks users to create one. `task.reply` continues the
selected task's conversation. Replies from another channel can change the
response route. Request results contain task/request/workstream IDs, not bodies.
Authenticated management-client requests are administrator requests: the agent
reasserts the current requester identity in trusted, per-call model context, including
after a continuation changes channels. Device IDs identify clients, not separate
people. The agent should address the administrator as "you"; historical senders
remain historical context. This identity does not approve actions or bypass
permission and approval checks.

`task.delete` requires the current deletion preview digest. It removes execution
details and leaves a read-only `[Deleted task]` marker, while preserving the
transcript, attachments, and shared workstream data. Active/dependent work blocks
deletion. This is not transcript erasure or workstream deletion.

### Skills

The `skills.manage` capability advertises the skill catalog. Use
`POST /v2/query` with `skills.list` (`query`, `offset`, `limit`) to read metadata
and `skills.read` (`id`) to read the selected instruction body.

Use `POST /v2/operations` with `operation: "domain.mutate"` and
`confirmed: true` for `skills.create`, `skills.edit`, and `skills.delete`.
Poll the operation before refreshing the catalog. Creation takes an ID,
title, description, body and optional `overlay_for` referencing a locked
skill. Edit and delete require the revision returned by `skills.read`.
Locked skills cannot be edited or deleted. See [Prompting and context](prompting.md)
for access, lifecycle, and publishing settings.

### Files, approvals, and destructive operations

Workspace operations are contained to permitted paths. Writes require a clean
malware scan and updates require the current digest. Project-file reads require
membership in that project. Chunked transfers do not imply resumable atomic
uploads. Approval decisions use the exact displayed opaque reference/version;
expired or changed reviews cannot be approved. Database reset requires `RESET`.

## CLI/TUI and checks

Use these commands from the installation root. Direct Compose commands are an
advanced host-operator escape hatch, not a way for clients to bypass the API.

```sh
./run.sh management health
./run.sh management config
./run.sh management services
./run.sh management logs task-agent
./run.sh management tui
PYTHONPATH=src/agent/src .venv/bin/python -m pytest -q src/agent/tests/test_management_api.py src/agent/tests/test_management_yaml.py
.venv/bin/python src/scripts/audit_management_api.py
```

The audit checks static contract coverage, not end-to-end readiness. Database
integration tests require a disposable `assistant_test` database. Docker tests
are opt-in. Never use live destructive actions merely to validate a UI.
