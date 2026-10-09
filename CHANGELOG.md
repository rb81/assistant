# Changelog

## v2.0.0 (prerelease): Platform Refresh

This breaking release lives on the `v2` branch. The previous version is preserved
unchanged as v1 (`v1` branch, tag `v1.0.0`). v2 is not an in-place `git pull` of v1:
read [the upgrade guide](docs/upgrading.md) before starting new workers.

### Added

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

### Removed

- Browser dashboard, shared editor, frontend build and associated workspace routes.
- Projects/project scheduling, separate notes, entities and their CRUD routes.

### Security

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