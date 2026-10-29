# Changelog

## Unreleased: Repository and documentation refresh

- Application code, scripts, deployment files, and defaults now live under `src/`.
- All repository-side local state belongs under ignored `data/`, including
  settings, credentials, configuration, setup dependencies, and backups.
- Root `./setup.sh` remains the single setup and administration entry point.
- Text-to-speech uses an admin-configured model and voice (`AGENT_TTS_MODEL`,
  `AGENT_TTS_VOICE`); the agent supplies only text and optional instructions.
  Admin CLI: `python -m assistant_agent.tts voices|test`. Audio is returned even
  when OpenRouter cost reporting lags, with later reconciliation.
- Telegram offers only `/list` and `/new`; workstream selection from `/list`
  now opens the workstream. Legacy commands reply with a hint.
- Sandbox egress is deny-by-default: each command gets a per-run HTTPS proxy
  sidecar (SNI-checked, no private addresses, DNS sink). Agents request hosts
  with `network_access_request`; admins choose Approve once/always, Block
  once/always or Cancel job via Telegram or email, with optional feedback.
  `SANDBOX_EGRESS_MODE=open` restores the previous behaviour.
- Telegram voice notes are converted to Ogg/Opus (ffmpeg in the task-agent
  image) so they appear as native voice messages, without a filename caption.
  `telegram_send` text is optional with attachments, and
  `task_complete(already_delivered=true)` avoids a follow-up text reply.
- The system prompt now guides channel choice (Telegram for short messages and
  voice notes, email for long content) and forbids announcing voice notes.
- The admin job-card button **Request approval** is now **Resend approval**.
- Refreshed the README and task-oriented documentation for the current product.
- `./setup.sh reset` now destroys and rebuilds the bundled PostgreSQL cluster
  without a backup or selective reset. It preserves settings, secrets, and files,
  then starts workspace reindexing when enabled; custom database configurations
  are refused.
- Canonical `docs/` is mounted read-only for agent tools and sandbox commands.
- Fresh databases initialize directly from the current schema baseline.
  Unsupported baselines are refused without automatic conversion.
- The workspace indexer now automatically refreshes embeddings for all seven
  indexed sources while agents are idle, with database notifications and bounded
  batches. Task and research workers allow up to 60 seconds for pending indexing
  before starting; timed-out runs warn and use keyword retrieval. Shared database
  locks prevent background generation from competing with active tasks. Source
  writes defer embeddings, and content edits invalidate stale vectors for retry.
- Legacy conversion scripts and instructions are distributed separately in the
  [transition release](https://github.com/rb81/assistant/releases/tag/layout-transition).
  V1 is retired and preserved by the `v1-retired` tag.

## v2.0.0 (prerelease): Platform Refresh

Historical prerelease changes are recorded below. Older installations must use
the [transition package](https://github.com/rb81/assistant/releases/tag/layout-transition)
before starting the current code.

### Added

- Shared durable request intake/reply routing and automatic migration of historical task sources.
- Channel-aware workstreams with persistent Telegram selection, runtime naming,
  paginated discovery/history, explicit contact/file adoption and versioned
  object-change notifications. Reminder and research execution use owned system
  threads with linked guidance, bounded main-thread outcomes and preserved
  execution history.
- Optional private Telegram messaging, scanned attachments, proactive sends and routed reminders.
- Isolated, hardened local whisper.cpp English/Arabic transcription with checksum-verified model provisioning.
- Guided setup/reconfiguration with masked credentials, navigation, optional
  integrations, review-before-save and protected file-backed Docker secrets.
- CLI secret status, updates, activation, and coordinated database-password
  rotation with an explicit recovery workflow.
- Deterministic authenticated email administration and local recovery commands.
- Unified memories with suggestions, working-context expiry and controlled-copy erasure.
- Optional managed Zoom meetings and calendar linking.
- Optional native Dropbox connector: private App Folder Inbox/Outbox exchange with
  malware scanning, size limits, no public links and no overwrites.
- Setup navigation: section summaries, summary-first revisits, Jump to section,
  per-section status, and Ctrl-C that discards only the current section.
- `overrides.env`: a commented, never-rewritten template of every setting the
  wizard does not manage, validated at review. It is optional and can also be
  created by hand; it loads after `.env`. `.env.example` is now only a reference
  for wizard-written keys.
- `setup tools` troubleshooting menus and factory reset support.
- `/admin requeue`, `/admin instruct` and `/admin jobs --recent`; local admin CLI `--yes`.
- Security screening, configured in a new setup wizard section:
  - optional DNS-verified DKIM or SPF authentication for mail claiming the admin address;
  - decision-model screening through OpenRouter's normalized Decisions API by default (any decision model slug,
    existing OpenRouter key), with optional direct Perplexity, Jev, Clef, OpenAI or custom endpoints.
    Inbound screening is always on once configured; outbound email and scripts
    are opt-in, and scripts get a static import, call and network analysis.
  - OTP approval for possibly malicious items;
  - YOLO mode, off by default and guarded by a typed confirmation.

### Changed

- Fresh mail from senders that are not accepted is no longer stored. It is moved
  unread to `IMAP_QUARANTINE_FOLDER` (default `Quarantine`).

### Removed

- Browser dashboard, shared editor, frontend build and associated workspace routes.
- Projects/project scheduling, separate notes, entities and their CRUD routes.

### Security

- Earlier prereleases gated legacy table retirement behind an explicit
  acknowledgement; conversion now lives in the separate transition package.
- Internal sandbox API requires TLS and bearer authentication, including health endpoints.
- Setup moves known credentials out of `.env` and grants secret mounts by service.
- File-secret errors fail closed; admin responses redact file-backed credentials.
- Job erasure follows migrated children without retired-table queries and removes
  local Zoom records.

### Breaking Changes

- Migration 005 drops retired tables after retaining supported history.
- The downloader requires the `email` profile; the wizard configures it.
- `AGENT_TIMEZONE` is authoritative for implicit times.
- Browser URLs and unauthenticated HTTP API clients no longer work.
- Compose source secret files remain plaintext host files, not encrypted storage.

Rollback requires restoring a pre-upgrade backup and old code, not just switching branches.