# Manual transition to the current layout and schema

These are **archived, external tools**, not part of current main. Extract the
release bundle somewhere private **outside your checkout**, so updating Git
cannot remove the scripts midway. Python 3 (standard library only), Docker CLI,
and PostgreSQL 16's `psql` inside the existing database container are required.
Nothing downloads application dependencies or starts application services.

Supported sources:

* V1 at `v1-retired`, commit `8f934a875daa77a7fe487ca1de3c086ac19f81db`.
  `v1.0.0` resolves to the same commit and schema. These are annotated tags;
  verify their underlying commits with `git rev-parse 'v1-retired^{commit}'`
  and `git rev-parse 'v1.0.0^{commit}'`, not the tag-object hashes.
* Archived pre-layout V2 at `pre-layout-cleanup`, commit
  `bd22232845fd68a06fda19d6fb321ab2ddc269ef`, including partially applied
  filename-tracked migrations.

Older, forked, untracked custom-schema, or manually modified databases/configs
require an operator-reviewed migration instead. The label check is not a schema
compatibility guarantee. There is no downgrade tool.

## Quick path

1. Extract this bundle outside the checkout. Stop all writers; back up and verify
   the logical database, **all** runtime files, and credential Docker volumes.
2. With only the original project's postgres running, preflight and apply
   `migrate_database.py` using its container/project arguments and both explicit
   acknowledgements below. Verify `000_current_baseline`.
3. Run the **old** `docker compose down` without volume deletion. Dry-run, then
   apply `migrate_layout.py --root /absolute/checkout --apply --backup-confirmed`.
4. Update to verified current main. Run `./setup.sh`, review and save to regenerate
   the overlay; **decline startup** while checking paths, configuration and volume
   identities.
5. Intentionally start services only when ready. Keep backups; review custom
   files and remove obsolete credentials/virtual environments separately.

Read the detailed safety requirements and exact commands below before applying.

## 1. Stop writers and make restorable backups

Keep the old checkout until both migration steps succeed. Stop **all workers,
web/scheduler services, transcription/sandbox jobs, external clients, and setup
processes**; do not allow restart policies or another operator to restart them.
Using the old Compose configuration, stop the project, then start **only
postgres** (for example `docker compose stop`, then
`docker compose up -d --no-deps postgres`). Do not start the new application
against an unmigrated database.

Record the exact original Compose project name, volume names, image versions,
and configuration. Obtain the running postgres container ID using the **old**
Compose configuration:

```sh
docker compose ps -q postgres
```

Inspect only its project label (not its environment, which may contain secrets):

```sh
docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' POSTGRES_CONTAINER_ID
```

Create and verify a restorable logical backup in a protected directory **outside
the checkout**. For the standard database/user, with your actual container ID:

```sh
umask 077
docker exec POSTGRES_CONTAINER_ID pg_dump -U assistant -d assistant -Fc > /secure/backup/assistant-before.dump
```

Also back up **all runtime files**, including `.env`, `overrides.env`, `.secrets`,
`.compose-secrets.yaml`, custom `config`, `data` (including shared/private files),
and existing backups. Separately back up any credential Docker volume while
quiescent: host `.secrets/state.json` may name the authoritative
`credential_volume` and `previous_credential_volume`. Host files alone are **not**
a credential-volume backup. Preserve permissions, ownership, encryption keys,
volume names and setup state verbatim; verify restoration access. If any secret
rotation/recovery is pending, resolve it with the old setup before proceeding.
Protect database dumps and old copies: they can contain sensitive data.

## 2. Upgrade the database while only postgres is running

From the extracted bundle directory, replace the example values below with the
existing container ID and exact project label. The default command is preflight
only; it does not connect to or alter PostgreSQL:

```sh
python3 migrate_database.py --container POSTGRES_CONTAINER_ID --project ORIGINAL_PROJECT
python3 migrate_database.py --container POSTGRES_CONTAINER_ID --project ORIGINAL_PROJECT \
  --apply --backup-confirmed --accept-destructive-v1-retirement
```

For nonstandard installations also supply `--database NAME --user ROLE`.
Local socket authentication inside the container must permit this role; the
script never requests or prints a password.

**The acknowledgement authorizes irreversible V1 retirement**, even on an
already-V2 installation. Migration 005 retires Projects, Tasks, Notes and Entity
tables. Its supported history transfers run first (notes become memories,
project history is attached to jobs); this is not a promise that every retired
feature can be reconstructed without the backup.

The script verifies the container's Compose project/service labels, requires it
to be the only running container in that project, and checks again immediately
before execution. This does not detect clients on another host/project: keeping
all writers stopped remains your responsibility. It executes frozen archived
feature SQL and pending migrations 001–011 in a **single transaction**, using the
application's advisory schema-upgrade lock and `psql -X -v ON_ERROR_STOP=1`.
Existing migration filename/timestamp records are preserved. The marker
`000_current_baseline` is inserted only after every pending migration succeeds;
006–011 remain recorded as applied ordinary migrations. A completed transition
is a no-op on repeat. An SQL failure rolls back the complete transaction,
including the marker. Docker/psql output is suppressed to avoid exposing data.
If the connection drops at commit, verify the marker before assuming whether
commit succeeded; rerunning is safe while quiescent.

Verify success without displaying application data:

```sh
docker exec POSTGRES_CONTAINER_ID psql -X -U assistant -d assistant -Atc \
  "SELECT filename FROM schema_migrations ORDER BY filename"
```

Expect the marker plus all eleven `001_...sql` through `011_...sql` filenames.
Then run **the old** `docker compose down` (without `-v` or `--volumes`). Do not
delete, rename, reset, prune, or reinitialize the PostgreSQL data or credential
volumes.

## 3. Relocate local files, then update main

The layout helper never invokes Docker and cannot stop services itself.
With every service and setup process stopped, use the absolute checkout path:

```sh
python3 migrate_layout.py --root /absolute/path/to/assistant
python3 migrate_layout.py --root /absolute/path/to/assistant --apply --backup-confirmed
```

Dry-run is the default. Both dry-run and apply preflight **all** supported sources
and destinations before modifying anything. Symlinks/non-regular entries in
moved trees, destination collisions (including an existing `data/config`),
missing old `.env`/`config/agent.yaml`, and cross-filesystem runtime moves are
refused. Do not solve a collision by deleting data; review and merge manually.
Unexpected I/O failures during apply are not a filesystem-wide transaction:
restore from backup or inspect carefully before retrying.

The helper:

* Renames `.env`, optional `overrides.env`, `.secrets`, `.compose-secrets.yaml`,
  `.setup.lock` and `backups` into `data/`, preserving their contents and modes
  (except documented environment pointer updates).
* Copies only `config/agent.yaml` and optional `config/AGENT.md` to `data/config`,
  preserving file modes and leaving the old originals intact for Git/recovery.
  It does **not** copy unknown sensitive custom directories. Review and migrate
  custom TLS/config files, referenced files and integrations manually.
* Leaves existing `data/share`, `data/private` and `data/postgres` untouched.
  Credential volume names and settings are preserved.
* Rewrites existing `COMPOSE_FILE`, `SANDBOX_SHARED_ROOT_HOST` and
  `SANDBOX_DOCS_ROOT_HOST` assignments in `.env`/`overrides.env` for the new layout.
  Simple standard `*_FILE` pointers under the old `.secrets` directory move to
  `data/.secrets`. `/run/secrets/...`, unknown settings, inline credentials, and
  custom paths are preserved, not guessed. Review complex/quoted/commented
  credential pointers and **all custom absolute paths** manually.
* Archives the original `.env` and optional `overrides.env` verbatim under
  `data/backups/layout-transition-env/` before rewriting them (directory mode
  0700, copies mode 0600; an existing archive is a preflight collision).
  Existing `.secrets/state.json` is archived there verbatim as `state.json`.
  In the moved state file, **only the `pending` and `removed` lists** are changed:
  entries exactly equal to `AGENT_API_AUTH_TOKEN` or `ADMIN_COMMAND_SIGNING_KEY`
  are removed. All other list entries retain their order; credential-volume
  names, previous-volume fields, sudo flags and all other settings are preserved.
  If neither list needs filtering, the original state file bytes are preserved.
  Invalid JSON or non-string tracking lists are refused during preflight.
  Removes retired management/API environment assignments and their `_FILE`
  variants, `ASSISTANT_ACCEPT_V2_MIGRATION`, and the `api` Compose profile.
  Other profiles/settings survive. This replaces old setup compatibility
  filtering: current main no longer recognizes retired management credentials.
  Retired credential files, credential-volume contents, and old
  `data/private/tls/server.crt`/`server.key` remain unchanged for recovery;
  they are not mounted by the regenerated current overlay. After verifying
  migration, review and securely retire these unused secrets separately.

Do not invoke Compose using the moved overlay yet: it can still contain old
host paths. Only the new setup's save will regenerate it.

Now update your checkout to the verified current main using your normal Git
procedure. `config/agent.yaml` may be a locally modified tracked file: its copy
is now in `data/config/agent.yaml`; inspect the diff before restoring **only that
tracked file** to permit updating. Never use blanket `git clean`, `reset --hard`,
or discard unrelated changes to force the update. Keep the original Compose
project name in the environment so existing named volumes are reused.

Run the new root wrapper:

```sh
./setup.sh
```

Review the recovered configuration, save it, and **decline startup** until ready.
Saving regenerates absolute `COMPOSE_FILE` paths pointing at
`src/docker-compose.yml` and `data/.compose-secrets.yaml`, and the host roots
`data/share` and `docs`. It reads defaults from `src/config` and local files from
`data/config`. Verify your original database bind mount and exact credential
volume identity before intentionally starting any service. Preserve custom
settings that need manual reconciliation.

The old `.venv-setup` is deliberately **not relocated**: virtual environments
contain absolute paths. New setup creates `data/.venv-setup`. Once the new
installation is verified, manually remove the obsolete old virtual environment
and redundant old ignored local config copies, after checking backups. Keep
unknown original custom files until individually reviewed.

## Recovery and maintenance

Current main accepts only its current-schema baseline; it cannot safely run raw
V1 or old V2 databases. If anything fails, keep writers stopped. To roll back,
restore the database **and** runtime files/credential volumes as a consistent
backup set, return to the exact archived checkout, and verify before startup.
Checking out an old Git ref alone does not undo a database migration.

The bundle is self-contained: scripts read SQL relative to themselves, not the
checkout. `build_sql_bundle.py` is a maintainer-only AST extractor that freezes
`FEATURE_SCHEMA_SQL` (including `UPDATED_AT_SQL`) without importing app code.
It is not needed to run the extracted release. Tests remain only on the
archival branch and are excluded from the runtime release bundle:

```sh
python3 -m unittest discover -s transition/tests -v
TRANSITION_TEST_POSTGRES_IMAGE=postgres:16-alpine python3 -m unittest discover -s transition/tests -v
```

Integration tests create/remove only their uniquely named disposable PostgreSQL
container and its anonymous volumes. They cover V1 at the pinned tag, archived
V2, idempotency, concurrent serialization and late-failure rollback.
