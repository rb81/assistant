# Upgrading from v1 to v2

This is a breaking upgrade: browser administration/shared editing, the optional
management API, Projects, separate notes and entities are retired. Do not blindly pull `main` into a running v1
deployment. v1 stays available unchanged on the `v1` branch and the `v1.0.0`
release; v2 is on `main`.

**Safety interlock.** If the database holds v1 data, v2 refuses to apply migration
005 (which drops the retired tables) until you set `ASSISTANT_ACCEPT_V2_MIGRATION=yes`
in `.env`. Without it, workers exit with an "Upgrade blocked" message, and nothing
is dropped or migrated. If you pulled v2 by accident, `git checkout v1` and restart. Fresh and
already-migrated databases are not affected.

## Before upgrading

1. Record current commit/image versions and preserve the old code.
2. Stop old workers/downloaders/schedulers, including the project scheduler.
   If the old Compose configuration includes `agent-api`, stop and remove that
   container before updating:
   `docker compose stop agent-api && docker compose rm -f agent-api`.
   This removes only the container, not PostgreSQL or other data. Do not mix
   old/new workers against the same database.
3. With PostgreSQL still available, take a logical backup. For the default database:
   `docker compose exec -T postgres pg_dump -U assistant -d assistant > assistant-before-upgrade.sql`.
   Protect the dump as confidential and test restoration into a disposable database.
4. Stop the remaining stack. Preserve `data`, `.env`, local prompt/config files,
   `.secrets` and any generated overlay. Do not use `down --volumes` on the live stack.
5. Export project history if you need its original representation. Rehearse the
   upgrade against a disposable restored copy before changing production.

## What changes

- Migration 005 converts notes into unified memories, keeps entity labels as tags,
  and preserves project/task snapshots on parent jobs before dropping retired tables.
  Migrated active working context expires after fourteen days; review/pin important
  records that should persist.
- Waiting project parents are requeued with retirement instructions. Existing
  children remain ordinary jobs; unstarted project tasks are not queued. Jobs
  awaiting admin review stay paused.
- Use email `/admin` commands or the local CLI instead of browser pages. Removed
  browser/API/project/note/entity routes do not provide compatibility views.
- Calendar, reminders and requests use `AGENT_TIMEZONE`. Deprecated overrides no
  longer control implicit times; explicit offsets preserve their instant.
- Zoom support is optional. Job erasure removes local meeting records, not meetings
  at Zoom or remote provider data.
- The `email` profile now selects the downloader. Setup chooses profiles explicitly;
  existing email deployments must select `email` before expecting intake to resume.

## Apply the upgrade

1. Update to the v2 code (`git fetch origin && git checkout main && git pull`) while the old stack is stopped.
2. Run `./scripts/setup.sh` and keep the current database password. Known inline
   credentials migrate into protected Compose secret files on final confirmation.
   Unknown non-secret settings and existing prompts are preserved. Resolve
   conflicting/duplicate credentials before proceeding.
   Decline replacing customized instructions. Decline optional container startup
   until all upgrade preparations and client changes are complete.
3. Configure authenticated email administration. Setup removes retired
   management API options, old admin-signing settings/secrets, and API-only TLS
   files when saving.
4. Once your backup is verified, add `ASSISTANT_ACCEPT_V2_MIGRATION=yes` to `.env`
   (setup preserves it). Then accept the wizard's optional startup, or run
   `docker compose up -d --build`. If you did not remove the old API container
   before updating, inspect `docker compose ps -a --orphans`; after confirming
   the orphan is only `agent-api`, run `docker compose down --remove-orphans`
   (without `--volumes`) and then start the stack again. This stops/removes
   containers and networks but preserves data volumes and bind-mounted data.
   Starting before the migration acknowledgement makes workers exit with
   "Upgrade blocked" and changes nothing. Ordered migrations run automatically
   at startup. Do not apply individual SQL files manually without migration tracking.
5. Verify service health, `/admin status`, migrated memories, child-job history,
   reminders and optional integrations. Confirm backend ports and secrets remain private.

Old backups/history may retain plaintext credentials. File migration does not erase
those copies; rotate exposed provider credentials after successful verification.

## Push deployment and sandbox initialization

A successful Git push does not prove deployment succeeded: a post-receive hook
cannot reject an already accepted revision. Inspect the emitted deployment attempt
log and compare the server checkout/deployed marker with the bare repository's
`main`. An attempt directory without `backup-complete` is not a complete recovery
snapshot. `assistant-deploy --force` retries deployment but never discards tracked,
staged or untracked checkout edits.

For reviewed server-side repairs, preserve the exact repaired files and diff first.
Compare them with the pushed revision before clearing only the explicitly reviewed
edits. Do not reset the checkout wholesale or commit credentials. Keep installed
deployment scripts identical to the reviewed external operator bundle.

This version introduces `sandbox-init`. For manual upgrades, run the updated
setup's **Review and save** to regenerate the overlay while retaining credentials.
The managed Shadow wrapper performs this owner-run save automatically and refuses
to generate a missing token during deployment. The wrapper must stage the helper
before starting sandbox, including when using `--no-deps`. Initialization failures
must not fall back to stale runtime credentials. Do not add the broker's old ACL
workaround or loosen private-key/token modes.

## Telegram and Voice Upgrade

Migration `007_request_channels.sql` runs automatically through `Database.run_migrations`
when updated workers start, after earlier ordered migrations. Its migration record and
advisory transaction lock prevent concurrent workers from applying it twice. No manual
SQL execution is required. Stop old workers and take the database/filesystem backup
described above before updating; do not mix old and new worker versions.

The migration preserves past task IDs, statuses, logs, and email records, including
completed, failed, cancelled, waiting, and review-required work. It backfills native
request identities, links legacy attachment ownership and trigger records, and retains
historical outbound acceptance logs. Reminder/manual tasks with no trigger email are
linked by their existing thread. Pending historical mail delivery is marked uncertain,
never assumed unsent. It does not replay past tasks or invent reminder recipients.

Run setup's review/save to regenerate secret grants for new roles before enabling the
optional profiles. The transcription model is initialized and checksum-verified in
a persistent model volume; decoder/inference runs separately without internet access.
Do not delete production volumes during an upgrade. See [Telegram and voice](telegram-and-voice.md).

## Workstream Conversations

Migrations 008, 009 and 011 add explicit main/system conversations, persistent
Telegram selection, object-change context and dedicated research execution jobs.
They run through the same serialized migration mechanism; no production data
upgrade is needed to run tests against a disposable database.

Back up and stop old writers before starting updated workers. Existing
workstream IDs, retaining links and historical reminder child workstreams are
preserved. New reminder occurrences execute in owned system threads. Existing
research runs keep their IDs, statuses, tool counts and events; migration 011
adds their system execution jobs without restarting or replaying the research.
Review-required tasks remain paused when research results arrive. Workstream
previews/deletion include the new execution records and stale routing cleanup.

## Rollback

Stop the new stack. Restore the pre-upgrade database and filesystem/config snapshot
with the old code/images (the `v1` branch or your recorded v1 commit). Merely checking out old code is unsafe: retired tables
are gone and new workers may already have changed data or caused external effects.
Reconcile emails/events/meetings created after the backup before resuming old
workers. A backup restore does not undo external actions.

## Release policy

Version 1 is preserved on the `v1` branch and tagged `v1.0.0` as the stable
snapshot. v2 is published as a tagged prerelease with breaking-change notes before a stable release. Follow actual
repository version history; do not describe an untested baseline as stable or make
a prerelease the latest stable release. Ongoing legacy maintenance is not promised.