# Email and Artifact Processing

This page explains how inbound mail becomes task context, how attachments are
scanned and converted, and how outbound mail is handled.

## Inbound email pipeline

The `downloader` role polls IMAP using `agent.email.imap_poll_interval_seconds`
(default 60 seconds). For each message it parses MIME, deduplicates by
`Message-ID` (or a deterministic fallback), checks admin/approval flows and
sender actionability, then quarantines, holds, or accepts the message.
Accepted messages have their bodies and attachments extracted, the disclosure
footer removed, and an `emails` record created. Artifact processing runs before
actionable mail is queued or routed to an existing job.

Approval and `/admin` handling returns before ordinary attachment processing
and job creation. Messages that queue a job are archived from the polling
mailbox. See [email administration](email-administration.md).

## MIME parsing and attachment storage

`extract_bodies_and_attachments` collects `text/plain` and `text/html` parts.
Attachment payloads are stored under
`agent.artifacts.raw_root/attachments/<safe-message-id>/`; metadata includes
filename, content type, size, and SHA-256. The default private raw root is
`/data/private/artifacts`.

## Disclosure footer stripping

Inbound text and HTML are normalized to remove the autonomous-agent disclosure
footer added to external outbound mail. This keeps the assistant from repeatedly
ingesting its own footer.

## Thread resolution

Transport threads belong to channel-neutral workstreams, which own the main
conversation, system threads, and data. A shared intent router can select an
existing workstream across email and Telegram; completing a task does not end
the conversation. See [workstream ownership](data-model.md#conversations-and-work-ownership).

Email `In-Reply-To` and `References` are used to find stored inbound messages or
sent mail. A recognized reply gives the candidate conversation more weight but
does not force continuation. Subjects are context, not a thread-matching rule.
The read-only `context_search` tool searches permitted workstreams before the
router chooses a new or existing one. Search is scoped to the sender's
conversations, linked Telegram identity, and any resolved active reply target;
verified administrators may search all active workstreams. Routing preserves
the message, attachments, and reply destination, and grants no permissions.

`agent.routing.model` defaults to `openai/gpt-4.1-mini`; the downloader and
Telegram receiver need model credentials. `agent.routing.timeout_seconds`
defaults to 20. If the router is unavailable, busy, timed out, or returns an
invalid decision, fallback continues a known active reply thread; otherwise it
starts a workstream from the email `Message-ID`. Telegram non-replies first try
the sender's recent conversation (see [Telegram](telegram-and-voice.md)).
Routing also receives the sender's three most recently active conversations
from the last three days. Decisions and fallbacks are recorded; a late model
response cannot change ownership. Unknown/deleted reply associations cannot be
continued, though another relevant workstream may be found.

## Actionability rules

New mail becomes actionable when at least one applies:

- It belongs to a thread with an open job (`queued`, `running`, `waiting`, or
  `needs_review`).
- The sender is listed in `agent.email.actionable_senders`.
- It replies through `In-Reply-To`/`References` to assistant mail on that
  thread, and the sender was a To/Cc recipient. This also applies after the
  original job completes.

`*` in `actionable_senders` allows any sender to create new jobs. Notifications
for assistant-managed calendar events are stored as context without queuing a
job. Other new messages are moved to
`agent.email.imap_quarantine_folder` (default `Quarantine`) and not stored.
Accepted mail may also be screened; see
[email and action screening](security.md#email-and-action-screening).

## Job update behavior for new email

`queue_or_update_job` handles messages associated with existing work:

| Existing job state | Effect of new mail |
|---|---|
| `queued` or `running` | Marks `has_new_context=true` |
| `waiting` | Requeues immediately and clears lock/error fields |
| `needs_review` | Remains paused unless explicitly approved |

An OTP-approved admin reply can review a `needs_review` job, optionally increase
limits, and add instructions; the sender address alone cannot resume it.
Guidance to a waiting deep-research run resumes that same run first; its parent
task remains waiting until research returns.

## Artifact pipeline

`ArtifactProcessor.process_email` handles attachments and YouTube URLs found in
the body. For attachments it creates a `processed_artifacts` row, scans with
ClamAV, checks extension and size, converts allowed files to Markdown with
MarkItDown, stores the result in the shared workspace, and records final status
and metadata.

Artifact processing is enabled by default; `agent.artifacts.enabled=false`
skips the pipeline. The default maximum attachment size is 25 MiB
(`agent.artifacts.max_attachment_bytes`).

## Dropbox documents

The optional [Dropbox exchange](dropbox.md) uses ClamAV and document conversion
but records transfers in `dropbox_transfers`, not as synthetic email. Imports
are on demand and remain unpublished until scans and conversion succeed.
Dropbox always requires a clean scan—even if ordinary artifact scanning is
optional or disabled. Exports scan a private snapshot and use conflict-safe
task Outbox paths.

## ClamAV integration

ClamAV uses TCP `INSTREAM` at the configured host and port (`clamav:3310` by
default). Recorded scan states include `pending`, `clean`, `infected`, `error`,
`skipped`, and `not_applicable`.

When ClamAV is unreachable, `agent.artifacts.clamav.required=true` (default)
blocks conversion; `false` permits ordinary document conversion with a recorded
scan error. Disabling ClamAV records `skipped` and permits ordinary conversion.
Voice transcription and Dropbox still require clean scans.

## Conversion and output paths

Successful conversion writes Markdown under:

```text
<shared_root>/<processed_root>/email/<safe-message-id>/<safe-stem>.md
```

By default this is under `/data/share/processed/...`. Duplicate filenames get
numeric suffixes.

## YouTube URL artifacts

The pipeline searches email text and HTML for recognized YouTube URLs,
canonicalizes them, and converts them with MarkItDown. It does not import
arbitrary web pages. These records use `source_type=youtube_url` and
`scan_status=not_applicable`.

## Outbound disclosure behavior

No disclosure footer is added when all recipients use domains in
`agent.org.internal_email_domains`. If any recipient is external, a footer is
appended to text and HTML. It is generated from organization and contact
settings (`agent.org.name`, `agent.org.security_email`, falling back to admin or
agent email).

## Operational guardrails

- A nonempty `agent.email.allowed_recipient_domains` restricts outbound
  recipients, except the configured admin address. Empty or `*` allows every
  domain; set a restrictive list for private deployments.
- Outbound attachment count and total size are bounded, and paths are validated
  against the workspace.
- Optional IMAP sent-folder append happens after SMTP accepts the message.
- Ingest, queue, scan, conversion, requeue, and review transitions are persisted
  to PostgreSQL.
