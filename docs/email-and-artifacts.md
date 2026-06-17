# Email and Artifact Processing

This guide documents Assistant's inbound/outbound email flow and artifact pipeline.

## Inbound email pipeline

The `downloader` role runs `EmailDownloader` and polls IMAP on a configured interval (`agent.email.imap_poll_interval_seconds`, default `60`).

High-level flow for each fetched message:

1. Parse raw MIME bytes.
2. Resolve `message_id` (header value or deterministic fallback).
3. Skip duplicates if `emails.message_id` already exists.
4. Extract addresses, subject, threading headers, text/html bodies, and attachments.
5. Strip assistant disclosure footer from inbound text/html.
6. Resolve `thread_id`.
7. Determine whether message is actionable.
8. Insert `emails` row.
9. Process artifacts (attachments + supported URLs).
10. Queue/requeue/update the linked job when actionable.

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

Thread assignment order:

1. Parse candidate IDs from `In-Reply-To`, then `References`.
2. Match candidates against stored inbound emails (`emails.message_id`).
3. Match candidates against outbound sent logs (`outbound_email_logs.provider_message_id`) and reuse that job's `thread_id`.
4. Optionally (only if enabled) use subject fallback with overlapping participants.
5. Otherwise, start a new thread using current message `Message-ID`.

Subject fallback is disabled by default and controlled by `agent.email.subject_threading_fallback`.

## Actionability rules

An inbound email is actionable when either:

- there is already an open job on the same thread (`queued|running|waiting|needs_review`), or
- sender matches `agent.email.actionable_senders`.

If `actionable_senders` contains `*`, all senders can create new jobs.

## Job update behavior for new email

`queue_or_update_job` handles existing thread jobs carefully:

- running/queued job: set `has_new_context=true`
- waiting/needs_review job: requeue immediately and clear lock/error fields
- admin reply on `needs_review`: apply review override (increase limits, optional instruction)
- waiting deep-research run on same thread: resume research queue first

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

Artifacts are always tracked, including failures.

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

## Conversion and output paths

Successful conversion writes Markdown under:

`<shared_root>/<processed_root>/email/<safe-message-id>/<safe-stem>.md`

Default `processed_root` is `processed`, so the typical path is under `/data/share/processed/...`.

Duplicate filenames are handled by numeric suffixes.

## YouTube URL artifacts

Email text/html is scanned for URLs. Recognized YouTube links are canonicalized and converted via MarkItDown.

These are stored as `source_type=youtube_url` artifacts with `scan_status=not_applicable`.

## Outbound disclosure behavior

Before sending outbound email, disclosure logic checks recipient domains:

- if all recipients are internal (`agent.org.internal_email_domains`), no footer is added
- if any recipient is external, a disclosure footer is appended to text and HTML bodies

Footer text is generated from organization and contact info (`agent.org.name`, `agent.org.security_email`, fallback to admin/agent email).

## Operational guardrails

- Outbound delivery is constrained by allowlisted recipient domains.
- Outbound attachments are bounded by configured count/size and workspace path validation.
- Optional IMAP sent-folder append happens after SMTP acceptance.
- Every important state transition (ingest, queue, scan, conversion, requeue/review) is persisted to PostgreSQL.