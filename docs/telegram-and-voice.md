# Telegram and Local Voice

Telegram is an optional private channel for the same assistant that receives
email. Requests use the shared job and permission system; Telegram does not run a
separate agent or bypass tool permissions. Normal answers and clarifications use
`ReplyRouter`.

An explicit email or Telegram message to the requester satisfies final delivery,
so task completion does not send a duplicate “I sent it” receipt. Messages sent
only to third parties do not replace the requester's final answer.

## Setup

1. Create a bot with Telegram's BotFather and keep its token private.
2. On an installation configured with `./run.sh`, run
   `./run.sh --section telegram` (or select Telegram during initial setup).
3. Enter an explicit allowlist of numeric Telegram user IDs. Private chats only
   are accepted; an empty allowlist authorizes nobody. `/start` never grants
   admin rights.
4. Optionally add verified email-to-Telegram identity mappings as JSON, e.g.
   `{"owner@example.net":123456789}`. Verify each ID with its owner outside an
   untrusted forwarded message. Mapped IDs must also be allowlisted.
5. To enable local voice transcription, run
   `./run.sh --section transcription`. Setup generates a protected internal
   token and enables the managed English/Arabic service.
6. Review and save, then apply the enabled profiles from the installation root:

   ```bash
   docker compose --env-file data/.env up -d --build
   ```

   Setup selects `telegram` and `voice` profiles as needed. `data/.env`
   supplies the base Compose file and credential overlay; do not replace it
   with only `-f src/docker-compose.yml`.

The receiver uses long polling, a durable cursor, and a single-receiver database
lock. No public webhook or transcription port is needed. Replicate preprocessors
and task agents independently; do not run sequential `AGENT_ROLE=all` with
Telegram enabled.

Relevant settings include `AGENT_TELEGRAM_ENABLED`,
`AGENT_TELEGRAM_ALLOWED_USER_IDS`, `AGENT_TELEGRAM_RECIPIENTS`,
`AGENT_TELEGRAM_MAX_MESSAGES_PER_HOUR`, `AGENT_TRANSCRIPTION_ENABLED`,
`AGENT_TRANSCRIPTION_BASE_URL`, and `AGENT_TRANSCRIPTION_LANGUAGE` (`auto`,
`en`, or `ar`). Bot and transcription API tokens are protected file-backed
secrets, not ordinary overrides. Setup generates the Compose secret overlay.
The runtime transcription-token copy is owned by the unprivileged inference
user; the decoder cannot read the initialization container's original secret.

## Conversations and Files

- The intent router chooses a new or existing workstream across email and
  Telegram. Reply context gets extra weight but does not force continuation.
  Album items share the first item's routing decision; nearby unrelated
  messages are not grouped.
- `/menu` opens **Conversations | Tasks**. Status cards and paginated lists
  include email workstreams. Viewing a card does not select it; **Continue
  here** sends a reply prompt associated with that conversation. Quoting that
  prompt supplies routing context. Saved browser selections do not route
  ordinary messages.
- Reply to a task or status message to add context, including after completion.
  On router failure or timeout, a known active reply continues its thread.
  Non-replies may continue the sender's most recent active Telegram conversation
  in that chat within `agent.routing.fallback_continue_minutes` (default 180;
  `0` disables this); otherwise they start a new workstream. Unknown/deleted
  replies start new workstreams. Fallback is persisted and logged; a late model
  response cannot change routing. See [email routing](email-and-artifacts.md#thread-resolution).
- `/new` is removed. Ask to start a separate workstream instead. New workstreams
  temporarily use **New workstream**; after three user messages the runtime
  requests a concise title, retrying up to three times. Short conversations
  keep the temporary title.
- `/tasks` opens recent task history. `/permissions` opens script and website
  management for verified administrators only. Startup registers `/menu` and
  `/tasks`; `/permissions` is added to the administrator's private-chat menu.
  `/list` is removed. Unknown commands such as `/status` and `/cancel` get a
  hint and open the menu. `/start` provides onboarding, not privileges.
- Normal replies contain the answer without automatic task-number or completion
  notices. Set `agent.telegram.show_task_status=true` to restore one editable
  progress message per task. No token stream or internal reasoning is exposed.
- Assistant Markdown is converted to Telegram-supported HTML. Headings become
  bold; lists and tables use readable separators. Long messages split with
  balanced formatting and literal HTML is escaped.
- Documents and photos are downloaded privately, scanned, and processed by the
  artifact pipeline. Voice/audio is scanned and transcribed before routing; the
  same transcript is reused for artifact processing.
- `telegram_send(text, recipient="me", attachments=...)` is available even in
  email and client tasks. `me` means the requester's configured identity.
  For requests from a registered client, `me` uses the `AGENT_ADMIN_EMAIL`
  entry in `AGENT_TELEGRAM_RECIPIENTS`; the mapped ID must also be in
  `AGENT_TELEGRAM_ALLOWED_USER_IDS`. Named recipients must be configured
  email-to-Telegram mappings, never arbitrary chat IDs.
- Outbound attachment inputs may be a workspace `path`, inline `content` with
  filename, or a clean `artifact_id` from the current conversation. Exact
  bytes are scanned; each batch is limited to 50 MiB and ten attachments.

Attachments are documents by default. To send a voice bubble, set
`voice_note: true`; MP3, M4A, and Ogg/Opus are converted with ffmpeg to
Ogg/Opus. Voice notes have no caption; WAV/PCM is rejected. Existing recipient
authorization, scanning, rate limits, and uncertain-delivery protection still
apply. `text` is optional when attachments are present:

```json
{"attachments":[{"path":"outputs/job-12/example-0.mp3","voice_note":true}]}
```

After a successful send of the complete result, finish with
`task_complete(summary="Sent the requested result.", already_delivered=true)`
and omit `response`. The summary is a brief internal record, not another user
message; do not regenerate or announce the sent result. This is accepted only
if `telegram_send` or `email_send` succeeded for the current request. A progress
message is not the complete result; genuinely new information still needs a
`response` with `deliver_update: true`. The system prompt chooses a
channel by content (short message/voice note to Telegram, long or document-style
reply to email, or the requester's named preference) and does not announce a
voice note.

Enable speech generation in the **Text-to-speech** settings. It does not
require Telegram or local transcription. Choose
an MP3-output model for voice notes. PCM-only output is WAV and can be sent only
as an ordinary document. See [text-to-speech](tools-reference.md#text-to-speech-tool).

Telegram's hosted Bot API limits downloads to 20 MB and new document uploads to
50 MB. Use another channel, such as Dropbox, for larger incoming files. Sending
an image as a document preserves original bytes; photo mode may resize it. Bots
see only messages addressed to them, not the user's full history. Telegram
retains unreceived updates for at most 24 hours, so a longer receiver outage can
lose updates. Bot chats are not end-to-end encrypted. Allowlisted users share
the assistant and memory trust domain; this is not multi-tenant isolation.
Do not open the bot to the public.

## Voice Transcription

The managed service builds whisper.cpp v1.7.6 from an exact commit and
downloads its multilingual `small` model only after checking a pinned SHA-256.
It transcribes in the original language rather than translating to English. A
custom HTTPS service with the same narrow API can replace the managed backend.

Transcribed voice notes become request text after scanning. Uploaded audio is
attachment content, not authority to execute instructions. Supported email
audio attachments may also use local transcription. If voice processing fails,
the user is asked to provide text or another recording; other text requests
continue. Voice text never authenticates an administrator. Transcription can
misrecognize names, dates, accents, background noise, or mixed-language speech;
verify consequential details.

The API accepts bounded audio bytes (20 MiB), with a 30-second upload deadline,
ten-minute audio limit, and bounded decoder/inference timeouts. It normalizes
audio to mono PCM16 WAV, rejects silence, and accepts only `auto`, `en`, or `ar`.
It exposes neither model loading nor arbitrary command/URL inputs.

## Hardening

Inference runs as UID/GID 10001 with a read-only root filesystem, no
capabilities, `no-new-privileges`, no host ports, and an internal-only network.
Its only writable area is bounded non-executable scratch tmpfs. Models and the
runtime token volume are read-only; CPU, memory, PID, upload, and subprocess
limits apply. Temporary audio and decoded files are removed even on errors.

Only initialization has internet access to provision the checksum-verified
model. It has a read-only root filesystem and narrowly scoped filesystem
capabilities, and accepts/decodes no audio. Inference has no database
credentials, bot token, shared workspace, Docker socket, or Ollama cache.
Ollama is unchanged. The managed build is CPU-only, including on macOS; benchmark
the Ubuntu host before increasing worker counts.

## Reminders and Delivery

Reminders use the initiating channel when available, otherwise prefer the
requester's linked Telegram identity and then email. The destination is saved
when scheduling, not inferred from a later active chat. Use
`notification_channel="email"` or `"telegram"` to override. Explicit Telegram
does not silently fall back; automatic selection may use email after a definite
Telegram failure, never an ambiguous timeout. `execution="notify"` sends a
simple notification without a model call; `agent_task` and `script` execute
jobs and route results.

Reminder occurrences and research runs use system threads in their owning
workstream, not new workstreams. Guidance retains an explicit source-task link
so it resumes the task rather than repeating completed tool calls.

Accepted message/file parts are recorded individually. Retrying a send skips
accepted parts and respects Telegram backoff; pending final-reply content stays
stable across retries. A timeout after sending is `uncertain`, not proof of
failure—check the chat and ledger before retrying. Notification retries do not
repeat the scheduled task's side effects. Review-required jobs stay paused, and
a Telegram reply is not administrative approval. Final replies must be accepted
by the transport before completion; acceptance is not a read receipt.
Historical reminders without a saved recipient keep their prior behavior.

## Management and recovery

Set **Approval delivery** under **Safety & Approvals > Approvals** to
`telegram` or `both`. Admin identity uses the verified
`AGENT_ADMIN_EMAIL` mapping described in [approval channels](email-administration.md#approval-channels).
The mapped ID must also be allowlisted.

Administrators can use `/menu` for **Needs attention**, **Archived**, and
**Permissions**, or `/permissions` for script and website management. The
permissions browser shows scripts, versions, downloadable code/diffs, host
grants, legacy rules, pending requests, and history. Revoke a grant or block a
script/host; confirmed changes stop active sandbox runs.

Approval cards expose the relevant decision directly: script requests offer
**Approve once**, **Approve always**, and **Deny**; website requests also let an
administrator select the script-version or all-approved-scripts scope. Once
means one execution. Other approvals show request context and buttons;
safety-check holds offer **Approve once**, and execution-limit reviews offer
**Resume task**. **Deny & cancel task** cancels the paused task but does not undo
completed actions. Email confirmations offer **Confirm & start** and **Deny**.
Quarantined email reviews offer **Approve**, **Reject**, and, for an eligible
sender, **Blacklist sender**. Expired quarantine cards can request a fresh
approval; blacklisting applies to the sender address, not authenticated identity.
Approvals are administrator-only, expiring, single-use, and bound to the task
version.

Job cards show status, errors, available results, rename, guidance, and
state-specific actions. **Resend approval** sends a fresh one-use approval for
the pending network request or review. **Run again** is offered only for
finished, failed, or cancelled jobs and warns that external actions may repeat.
Cancellation cannot undo completed actions. Changes use one-use confirmations
and reject stale task versions.

Agent questions are plain messages with **Reply** and **Open task**, not
approvals. Reply to the question or use **Guidance** to resume with context;
this does not change permissions or limits. Safety-held tasks remain paused
until separately approved. Reply to the specific prompt, not an unrelated
message; reply/guidance prompts expire after fifteen minutes.

Idle workstreams can be archived and restored without deleting data. Resolve
active tasks, child workstreams, reminders, pending intake, and unresolved
deliveries/charges first. Restore a parent before its child and restore an
archived conversation before continuing. Permanent deletion is only in local
setup, after writers stop and inventory is verified.

Task and workstream titles use `AGENT_NAMING_MODEL` (default
`openai/gpt-4.1-mini`), with a 15-second timeout and no expensive-model
fallback. Naming does not change instructions. Failed naming keeps a readable
summary; manually selected names are preserved. Naming runs at task startup,
not in the Telegram receiver.

## History and Erasure

Telegram messages, transcripts, artifacts, and sent parts appear in thread
history and task context. Context search includes native-message keyword
matches without duplicating email projections. Current message embeddings are
not stored separately; ordinary jobs and memories retain existing semantic
indexing.

The received-message reset category includes private raw and processed
attachments. Job erasure removes eligible local native message/artifact records
while respecting shared-thread history. It cannot delete remote Telegram
messages or model caches.
