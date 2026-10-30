# Changelog

## Unreleased: Repository and documentation refresh

- Scheduled scripts are approved when scheduled, not when they first run.
  `reminder_create` requires `execution` (`notify`, `agent_task` or `script`);
  script reminders need standing (Always) approval for the exact version and hosts
  before they are saved. Scheduled approval requests offer only Always or Deny.
  Due runs are checked against the bound approval and skipped with an admin alert
  if it was revoked or the script, image or policy changed. Scheduled runs cannot
  open development sessions, and new approval requests from them are flagged as
  drift. The Python `notification_only=True` argument still maps to `notify`.
- Agent questions are ordinary messages, not approvals. `request_input` no
  longer sends one-time-code confirmations or "Review reply"/"Continue task"
  buttons; the `input` approval kind is removed. Admin questions arrive as plain
  Telegram messages (reply or tap **Reply**) or email (reply, or
  `/admin instruct JOB_ID`); requester questions go to the requester, including
  when the requester is the admin. Answers resume the job as guidance and never
  grant permissions. Guidance sent while a job runs is injected at its next step.
- Paused jobs keep their working memory: question and review pauses checkpoint
  the conversation and loaded tools, and the job resumes from that checkpoint
  with new replies and instructions appended. Prior-action context now lists
  questions, admin guidance, commands and script approval requests.
- The agent is instructed to call approval-gated tools directly, never to ask
  for a go-ahead (it still checks in before irreversible ungated actions that
  the request did not clearly cover); one script reminder per scheduled task; inside a development
  session, `reminder_create(execution="script")` is the final approval.
- Approved script schedules are saved by the system from the original
  `reminder_create`/`reminder_update` call instead of asking the agent to retry
  it, which could drop or add hosts and trigger repeated approval requests.
  Re-requesting an approved version with different hosts in the same task is
  refused with the approved host list.
- Admin sender authentication (DKIM/SPF) is recommended and now on by default.
  Turn it off in `./run.sh` → Security only if your mail provider cannot pass it.
- Application code, scripts, deployment files, and defaults now live under `src/`.
- All repository-side local state belongs under ignored `data/`, including
  settings, credentials, configuration, setup dependencies, and backups.
- Root `./run.sh` replaces `setup.sh` for setup and local administration.
- Native full-screen management now uses one persistent application with
  service-health rows, live logs, searchable records, detail screens, draft forms,
  review/save validation, and confirmed actions. Navigation and pagination remain
  fixed; only sudo's system password prompt leaves the interface.
- Removed the embedded-menu wrapper and fictional-data mockup.
- Docker startup failures provide actionable diagnostics and a visible retry.
- Every administration page, menu action and configuration field has contextual
  help in a fixed About panel, with full explanations available through F1.
- Administration uses a compact, borderless layout: contextual help sits above
  content, actions are grouped together, and empty status messages take no space.
- Text-to-speech uses an admin-configured model and voice (`AGENT_TTS_MODEL`,
  `AGENT_TTS_VOICE`); the agent supplies only text and optional instructions.
  Admin CLI: `python -m assistant_agent.tts voices|test`. Audio is returned even
  when OpenRouter cost reporting lags, with later reconciliation.
- Telegram offers `/menu`, `/new`, `/tasks`, and admin-only `/permissions`.
  The main menu shows Conversations, Tasks, and administrator controls;
  `/list` is removed, with no compatibility alias. Unknown commands reply with
  a hint. Workstream selection opens the workstream.
- Sandbox egress is deny-by-default: each command gets a per-run HTTPS proxy
  sidecar (SNI-checked, no private addresses, DNS sink). Agents request hosts
  with `network_access_request`; new approvals use exact hosts, not URL paths
  or implicit subdomains.
- Scripts require **Approve once**, **Approve always**, or **Deny** through
  email, Telegram, or both, with optional feedback. Reusable grants bind code
  and dependency hashes, argv, workdir, sandbox image/runtime, and security
  policy. Executions use read-only code snapshots; once grants are consumed
  before dispatch and registered scripts are not automatically redispatched.
- Host grants can cover one execution, one script version, or all approved
  scripts. Telegram **/permissions**, setup Tools, and the admin CLI
  expose versions, code/diffs, grants, blocks, and history. Revocation stops
  active sandbox runs. Legacy network rules keep their original scopes.
- Script screening findings are cached per version/security profile; reusable
  administrator approval avoids repeated checks. Screening is optional, but
  script permission is not. Malicious findings remain hard-blocked.
- Upgrading script permissions requires saving setup again and rebuilding/
  recreating services to provision revoke-only credentials and service grants.
- Permission requests use compact summaries on Telegram and email, with code,
  hashes, and full review context in Details or the attached manifest. Email
  choices match the request's available scopes. Successful Telegram decisions
  no longer include a redundant Permissions navigation button.
- Telegram voice notes are converted to Ogg/Opus (ffmpeg in the task-agent
  image) so they appear as native voice messages, without a filename caption.
  `telegram_send` text is optional with attachments, and
  `task_complete(already_delivered=true)` avoids a follow-up text reply.
- The system prompt now guides channel choice (Telegram for short messages and
  voice notes, email for long content) and forbids announcing voice notes.
- The admin job-card button **Request approval** is now **Resend approval**.
- Refreshed the README and task-oriented documentation for the current product.
- `./run.sh reset` now destroys and rebuilds the bundled PostgreSQL cluster
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