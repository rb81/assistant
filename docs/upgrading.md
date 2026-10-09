# Upgrading from v1 to v2

This is a breaking upgrade: browser administration/shared editing, Projects,
separate notes and entities are retired. The retained API now requires TLS and
bearer authentication. Do not blindly pull the `v2` branch into a running v1
deployment. v1 stays available unchanged on the `v1` branch and the `v1.0.0` tag;
v2 is a prerelease on the `v2` branch.

## Before upgrading

1. Record current commit/image versions and preserve the old code.
2. Stop old workers/downloaders/schedulers, including the project scheduler.
   Do not mix old/new workers against the same database.
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
  browser/project/note/entity routes do not provide compatibility views.
- Calendar, reminders and requests use `AGENT_TIMEZONE`. Deprecated overrides no
  longer control implicit times; explicit offsets preserve their instant.
- Zoom support is optional. Job erasure removes local meeting records, not meetings
  at Zoom or remote provider data.
- The `email` profile now selects the downloader. Setup chooses profiles explicitly;
  existing email deployments must select `email` before expecting intake to resume.

## Apply the upgrade

1. Check out the reviewed v2 code (`git fetch origin && git checkout v2`) while the old stack is stopped.
2. Run `./scripts/setup.sh` and keep the current database password. Known inline
   credentials migrate into protected Compose secret files on final confirmation.
   Unknown non-secret settings and existing prompts are preserved. Resolve
   conflicting/duplicate credentials before proceeding.
   Decline replacing customized instructions. Decline optional container startup
   until all upgrade preparations and client changes are complete.
3. Configure authenticated email administration. For the optional API, import a
   trusted hostname-valid certificate and configure clients with the bearer token.
   Development certificates only cover localhost/127.0.0.1; never disable verification.
4. Accept the wizard's optional startup only when ready, or run
   `docker compose up -d --build`. Ordered migrations run automatically at
   startup. Do not apply individual SQL files manually without migration tracking.
5. Verify service health, `/admin status`, migrated memories, child-job history,
   reminders and optional integrations. API health checks need HTTPS and bearer auth.
   Confirm backend ports and secrets remain private.

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