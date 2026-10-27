# Changelog

## v2.0.0 (prerelease): Platform Refresh

This breaking release lives on `main`. The previous version is preserved
unchanged as v1 (`v1` branch, release `v1.0.0`). v2 is not an in-place `git pull` of v1:
read [the upgrade guide](docs/upgrading.md) before starting new workers.

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
- `setup tools` troubleshooting menus and `setup reset` selective factory reset
  with dependency-closed selection, backups and preserved settings.
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

- Upgrade interlock: migration 005 (which drops retired tables) refuses to run on a
  database holding v1 data until `ASSISTANT_ACCEPT_V2_MIGRATION=yes` is set in `.env`.
- Optional API requires TLS and bearer authentication, including health endpoints.
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