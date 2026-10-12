# Telegram and Local Voice

Telegram is an optional private messaging channel for the same agent that receives email.
All requests enter `RequestService.receive_request`; channel-specific code does not run a
separate agent or bypass tool permissions. `ReplyRouter` delivers normal answers and
clarifications. New email requests use the same router; historical email jobs retain
their original completion requirements.

## Setup

1. Create a bot using Telegram's BotFather and keep its token private.
2. Run `./scripts/setup.sh --section telegram` on an already configured installation,
   or select Telegram during initial setup. Enter the bot token through the masked prompt.
3. Supply an explicit allowlist of numeric Telegram user IDs. Private chats only are
   accepted; empty allowlists grant nobody access. `/start` never grants admin rights.
4. Enter verified identity mappings as JSON, for example
   `{"owner@example.net":123456789}`. Confirm the numeric ID with that person outside
   an untrusted forwarded message. The mapped IDs must also be allowlisted.
5. Optionally run `./scripts/setup.sh --section transcription`. This generates a
   protected internal token and enables the managed multilingual English/Arabic service.
6. Review and save, then start or recreate the selected services with
   `docker compose up -d --build`. Setup selects the `telegram` and `voice` profiles.

No public Telegram webhook or transcription port is necessary. The receiver uses long
polling with a durable cursor and a single-receiver database lock. Preprocessors and
task agents can be replicated independently. Use the managed roles rather than
sequential `AGENT_ROLE=all` with Telegram enabled.

Relevant overrides are `TELEGRAM_ENABLED`, `TELEGRAM_ALLOWED_USER_IDS`,
`TELEGRAM_RECIPIENTS`, `TELEGRAM_MAX_MESSAGES_PER_HOUR`, `TRANSCRIPTION_ENABLED`,
`TRANSCRIPTION_BASE_URL`, and `TRANSCRIPTION_LANGUAGE` (`auto`, `en`, or `ar`).
`TELEGRAM_BOT_TOKEN` and `TRANSCRIPTION_API_TOKEN` are protected file-backed secrets,
not ordinary overrides. Setup must regenerate the Compose secret overlay on upgrades.
The runtime token copy is owned by the unprivileged inference user; the decoder cannot
read the initialization container's original secret mount.

## Conversations and Files

- A standalone message starts a task. Brief text/file bursts and albums are grouped.
- Reply to a task message or its status message to continue that task, including after
  completion. Task identity is separate from chat identity.
- An answer to the only waiting requester clarification resumes it. If several tasks
  need answers, choose a task using the provided buttons.
- `/new` starts a separate task. `/tasks` and `/status` show active tasks.
  `/cancel 123` cancels your active task 123; it cannot undo already completed actions.
- Normal replies contain just the answer, without automatic task-number or completion
  notifications. `/tasks` and `/status` still expose task IDs when needed. Set
  `agent.telegram.show_task_status: true` in the YAML configuration to restore one
  editable progress message per task. There is no token stream or disclosure of
  internal reasoning.
- Assistant Markdown is converted to Telegram's supported HTML formatting for
  bold, italics, strikethrough, links, quotations, and code. Headings become bold
  text; lists and tables use readable text separators. Long replies are split with
  balanced formatting, and literal HTML is escaped rather than executed.
- Documents and photos are downloaded into private storage, scanned, and processed by
  the existing artifact pipeline. Conversion limitations are reported in task context.
- `telegram_send(text, recipient="me", attachments=...)` is a loadable tool, even in
  an email task. `me` means the requester's configured identity. Named recipients must
  be configured email-to-Telegram mappings, never arbitrary chat IDs.
- Outbound attachments accept `path` inside the shared workspace, inline `content`
  with a filename, or a clean `artifact_id` belonging to the current task. Exact outbound
  bytes are scanned. The batch is capped at 50 MB and ten attachments.

Hosted Bot API downloads are limited to 20 MB; larger incoming files need another
channel such as Dropbox. New document uploads are limited to 50 MB. Sending an image
as a document preserves its original bytes; Telegram's photo mode can resize images.
Bots receive only messages addressed to them, not a user's entire Telegram history.
Telegram keeps unreceived updates for at most 24 hours, so a longer receiver outage
can lose updates. Bot chats are not end-to-end encrypted.
Allowlisted people share the same assistant and memory trust domain; the allowlist is
not multi-tenant isolation. Do not open this bot to the public.

## Voice Transcription

The managed service builds whisper.cpp v1.7.6 from an exact commit and downloads the
multilingual `small` model only after verifying its pinned SHA-256. It transcribes in
the original language rather than translating to English. A custom HTTPS endpoint
implementing the same narrow API can replace the managed backend without changing intake.

Voice notes become request text after scanning and transcription. Uploaded audio
recordings are treated as attachment content, not independent authority to execute
instructions. Supported email audio attachments can also use local transcription.
Failed voice processing asks for text or a new recording; other text requests continue.
Voice text never authenticates an administrator. Names, dates, accents, background
noise, and mixed-language speech can be misrecognized; review consequential details.

The API accepts only bounded audio bytes, with a 20 MB limit, a 30-second upload deadline,
a ten-minute audio limit, and bounded decoder/inference timeouts. It normalizes audio
to mono PCM16 WAV, rejects silence, and permits only `auto`, `en`, or `ar` language
selection. It exposes neither model loading nor arbitrary command/URL inputs.

## Hardening

Inference runs as UID/GID 10001, with a read-only root filesystem, all capabilities
dropped, `no-new-privileges`, no host ports, and an internal-only network. Its only writable
space is a bounded, non-executable scratch tmpfs. Models and the runtime token volume
are read-only. CPU, memory, PID, upload, and subprocess limits constrain resource use.
Temporary audio and decoded files are deleted after each request, including errors.

Only initialization has internet access, to provision a checksum-verified model. It
has a read-only root filesystem and narrowly scoped filesystem capabilities; no audio
is accepted or decoded there. The inference service has no database credentials, bot
token, shared workspace, Docker socket, or Ollama cache access. Ollama stays unchanged.
Containers on macOS do not gain native Metal acceleration; the managed build is CPU
only. Benchmark the actual Ubuntu host before increasing transcription worker counts.

## Reminders and Delivery

New reminders prefer the requester's linked Telegram destination, otherwise email.
The destination is saved when scheduling, not inferred again from a later active chat.
Use `notification_channel="email"` or `"telegram"` to override; an explicit Telegram
selection does not silently fall back to email. Automatic selection can fall back to
email after a definite Telegram failure. It never does so after an ambiguous timeout.
Set `notification_only=true` for simple notifications that need no model invocation;
other reminders execute ordinary jobs and route their final results.

Accepted message/file parts are recorded individually. Retrying the same send skips
accepted parts and respects Telegram backoff. Pending final reply content is kept
stable across delivery retries. A timeout after sending is `uncertain`, not proof of
failure: inspect the recipient chat and ledger before retrying. Review-required jobs
remain paused. A Telegram reply is not an administrative approval.

Normal final replies must be accepted by the transport before the task completes.
Transport acceptance is not a read receipt. Notification retries do not repeat the
scheduled task's side effects. Historical reminders without a saved recipient keep
their previous behavior rather than being redirected to an inferred user.

## History and Erasure

Native messages, transcripts, artifacts, and sent Telegram parts are included in
thread history and task context. Unified context search includes keyword matches for
native requests without duplicating email projections. Current message embeddings are
not stored separately; ordinary jobs and memories retain existing semantic indexing.
The received-message reset category includes private raw and processed attachments.
Job erasure removes eligible local native message/artifact records while respecting
shared-thread history. It does not delete remote Telegram messages or model caches.

See [the upgrade guide](upgrading.md) for automatic history migration and backups.