# Email and Artifact Processing

This guide documents Assistant's inbound/outbound email flow and artifact pipeline.

## Inbound email pipeline

The `downloader` role runs `EmailDownloader` and polls IMAP on a configured interval (`agent.email.imap_poll_interval_seconds`, default `60`).

High-level flow for each fetched message:

1. Parse raw MIME bytes.
2. Resolve `message_id` (header value or deterministic fallback).
3. Skip duplicates if `emails.message_id` already exists.
4. Apply configured admin sender checks and handle approval replies or `/admin` subjects.
5. Resolve addresses and `thread_id`, then check actionability.
6. Quarantine unaccepted mail; hold actionable admin requests for OTP approval
   and apply configured security screening to other accepted mail.
7. For accepted mail, extract bodies and attachments, strip the disclosure
   footer, and insert the `emails` row.
8. Process artifacts (attachments and YouTube URLs).
9. Route actionable mail through `RequestService.receive_request` to queue or
   update the linked job.

Approval and admin-command handling returns before attachment extraction or
ordinary job creation. See [email administration](email-administration.md).

When a message causes a job to be queued, downloader archives it from the polling mailbox to the configured archive folder.

## MIME parsing and attachment storage

`extract_bodies_and_attachments` walks multipart messages and:

- collects `text/plain` and `text/html` bodies
- writes attachment payloads under `agent.artifacts.raw_root/attachments/<safe-message-id>/...`
- records metadata (`filename`, `content_type`, byte size, SHA-256)

By default, raw artifact root is `/data/private/artifacts`.

## Disclosure footer stripping

Inbound bodies are normalized via:

- `strip_disclosure_text`
- `strip_disclosure_html`

This removes the autonomous-agent disclosure sentence/block added to outbound external emails, so the model does not repeatedly ingest its own footer text.

## Thread resolution

Transport thread IDs identify conversations; a channel-neutral **workstream** owns
their main conversation, system threads and data. A shared router selects a new
or existing workstream by intent, including across email and Telegram. Completing
an execution does not end the conversation.
See [workstream ownership](data-model.md#conversations-and-work-ownership).

Routing happens after sender admission, approvals and security screening, before
storing actionable email and assigning workstream ownership. The router has one
read-only `context_search` tool. It searches permitted workstreams semantically
(with keyword fallback), returning conversation summaries, recent exchanges and
open tasks. It must search before returning a structured new/continue decision.
Subjects are context, not a thread-matching rule.

Email `In-Reply-To` and `References` identify a reply candidate via stored inbound
messages or sent logs. A recognized reply supplies its conversation and recent
exchanges to the router with extra weight, not an obligation to continue. Standalone
requests contain no absent-thread or absent-workstream hints. Search is scoped to
the sender's conversations, configured linked Telegram identity and any known
active reply target already resolved by the channel's intake checks; verified
administrators can search all active workstreams. Routing grants no permissions
and preserves the incoming message, attachments and response destination.

`agent.routing.model` defaults to `openai/gpt-4.1-mini`; the whole routing decision
has a deadline of `agent.routing.timeout_seconds` (default 20). If unavailable,
busy, timed out or invalid, deterministic fallback continues a known active reply
thread, otherwise starts a new workstream using the message's `Message-ID`.
The retired subject-fallback setting is ignored. The decision and rationale,
including fallback use, are stored in request metadata; late model responses
cannot change ownership. Deleted or unknown reply associations cannot be continued,
but the router may find a different relevant active workstream.

## Actionability rules

An inbound email is actionable when either:

- there is already an open job on the same thread (`queued|running|waiting|needs_review`), or
- sender matches `agent.email.actionable_senders`.

If `actionable_senders` contains `*`, all senders can create new jobs.
Notifications for assistant-managed calendar events are stored as context
without queueing a job.

Other new mail is not stored: it is moved to the quarantine folder
(`agent.email.imap_quarantine_folder`, default `Quarantine`). Accepted mail may
also be screened first; see [Email and action screening](security.md#email-and-action-screening).

## Job update behavior for new email

`queue_or_update_job` handles existing thread jobs carefully:

- running/queued job: set `has_new_context=true`
- waiting job: requeue immediately and clear lock/error fields
- needs_review job: remain paused unless explicitly approved
- OTP-approved admin reply on `needs_review`: apply review override (increase
  limits, optional instruction); a From address alone cannot resume the job
- waiting deep-research run: guidance to its linked system thread (or an
  unambiguous original conversation) resumes that same research run first;
  its original task remains waiting until research returns its result

## Artifact pipeline

`ArtifactProcessor.process_email` handles two source types:

- attachments
- YouTube URLs found in email body

For each attachment:

1. Create `processed_artifacts` row with `scan_status=pending`, `conversion_status=pending`.
2. Run ClamAV scan.
3. Enforce allowed extensions and max size.
4. Convert to Markdown with MarkItDown when allowed.
5. Save Markdown to shared workspace processed path.
6. Update DB row with final scan/conversion status and metadata.

When artifact processing is enabled (the default), failures are tracked too.
Set `agent.artifacts.enabled=false` to skip this pipeline.
The default attachment size limit is 25 MiB
(`agent.artifacts.max_attachment_bytes`).

## Dropbox documents

The optional [Dropbox exchange](dropbox.md) reuses ClamAV and document extraction
without changing email artifact ownership. Dropbox operations are associated
with tasks in `dropbox_transfers`, not synthetic email records. Files are imported
on demand from the app Inbox and remain quarantined until clean scans and
successful conversion. Unlike optional email scanning, Dropbox requires a clean
scan even when `clamav.required=false` or scanning is disabled. Outputs use
scanned snapshots and conflict-safe task Outbox destinations.

## ClamAV integration

ClamAV scan uses TCP `INSTREAM` against configured host/port (`clamav:3310` by default).

Common `scan_status` values:

- `pending`
- `clean`
- `infected`
- `error`
- `skipped`
- `not_applicable`

If ClamAV is unreachable:

- with `agent.artifacts.clamav.required=true` (default), conversion is blocked
- with `required=false`, conversion can proceed with recorded scan error

Disabling ClamAV records `scan_status=skipped` and permits ordinary document
conversion. Voice transcription and Dropbox transfers still require a clean scan.

## Conversion and output paths

Successful conversion writes Markdown under:

`<shared_root>/<processed_root>/email/<safe-message-id>/<safe-stem>.md`

Default `processed_root` is `processed`, so the typical path is under `/data/share/processed/...`.

Duplicate filenames are handled by numeric suffixes.

## YouTube URL artifacts

Email text/html is searched for URLs. Recognized YouTube links are canonicalized
and converted via MarkItDown; arbitrary web pages are not imported by this pipeline.

These are stored as `source_type=youtube_url` artifacts with `scan_status=not_applicable`.

## Outbound disclosure behavior

Before sending outbound email, disclosure logic checks recipient domains:

- if all recipients are internal (`agent.org.internal_email_domains`), no footer is added
- if any recipient is external, a disclosure footer is appended to text and HTML bodies

Footer text is generated from organization and contact info (`agent.org.name`, `agent.org.security_email`, fallback to admin/agent email).

## Operational guardrails

- `agent.email.allowed_recipient_domains` restricts outbound domains when
  nonempty, except for the configured admin address. An empty list or `*`
  allows any domain; configure a restrictive list for a private deployment.
- Outbound attachments are bounded by configured count/size and workspace path validation.
- Optional IMAP sent-folder append happens after SMTP acceptance.
- Every important state transition (ingest, queue, scan, conversion, requeue/review) is persisted to PostgreSQL.