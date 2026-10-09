# Email-First Administration

Browser administration and shared editing are removed. `/admin` subjects are handled before artifact processing or assistant job creation. All commands and errors bypass chat/embedding models. Ordinary tasks still use the assistant. Private attachments, malware scanning, conversion, sandbox files, and emailed deliverables remain supported. Native reminders are unchanged; CalDAV is deferred.

## Authentication

Run `./scripts/setup.sh` to configure the mailbox and signing key. The signing
key is a Docker secret mounted only into the downloader, not an inline `.env`
value. Use `./scripts/setup.sh secrets update ADMIN_COMMAND_SIGNING_KEY` to replace
it; clients must adopt the replacement after activation.

Set `ADMIN_EMAIL` and one of:

- `ADMIN_TRUSTED_AUTHSERV_IDS`: exact receiving-provider authority IDs, comma-separated. Only the first Authentication-Results header is considered, with an aligned DMARC pass. Use this only when the receiver strips forged headers bearing its identity and the sender's provider enforces the exact authorized mailbox address. DMARC authenticates a domain, not an individual mailbox; other authorized signers of that domain remain inside this trust boundary.
- `ADMIN_COMMAND_SIGNING_KEY`: an HMAC signing key outside model-accessible files. Signed messages include `X-Assistant-Admin-Timestamp` (Unix seconds) and `X-Assistant-Admin-Signature` (SHA-256 HMAC hex). Sign the UTF-8 Message-ID, decoded subject, extracted plain-text body, and timestamp joined with newlines. Signatures expire in five minutes.

Unverified commands are ignored, never sent to the assistant. Only one configured From address is accepted. Bulk/automated messages are rejected. Replies go only to the configured admin, never arbitrary Reply-To. Message-ID claims prevent duplicate execution; the rate limit is ten commands per minute. Failed reply delivery does not blindly repeat executed commands; use the CLI to inspect state and send a fresh appropriate command.

## Commands

```text
/admin
/admin status
/admin jobs --cursor 0 --limit 25
/admin jobs --recent --limit 10
/admin logs 123 --cursor 0 --limit 25
/admin review 123
/admin cancel 123
/admin requeue 123
/admin instruct 123
/admin memory list
/admin memory search --query "meeting preference"
/admin memory suggestions
/admin memory show 7
/admin memory create
/admin memory update 7
/admin memory approve 7
/admin memory reject 7
/admin memory pin 7
/admin memory unpin 7
/admin memory expire 7
/admin memory delete 7
/admin reminders
/admin reminder-cancel 9
/admin confirm TOKEN
```

Put memory text, review instructions or `instruct` instructions in the plain-text body. `requeue` retries a failed, cancelled, waiting, needs-review or completed job; `instruct` adds a supervisor instruction and requeues a finished job so it is processed. Approval accepts a suggested new fact; use explicit update for a correction to an existing fact. Destructive actions require one-use, admin/action/object-bound confirmations valid for ten minutes. Logs use stable sequence cursors and return the next command. Structured secrets and configured secret values are redacted. Memory embeddings are refreshed by ordinary workers, never inline in commands.

## Recovery CLI

```bash
docker compose exec task-agent python -m assistant_agent.admin_commands '/admin status'
docker compose exec task-agent python -m assistant_agent.admin_commands '/admin memory suggestions'
docker compose exec task-agent python -m assistant_agent.admin_commands '/admin requeue 123' --yes
```

The CLI uses local OS/container authorization and the same service without models or mailbox dependencies. `--yes` replaces the email confirmation token for destructive commands. `./scripts/setup.sh tools` wraps these commands in menus.

## Memory And Deletion

Unified memory carries evidence, provenance, tags, importance/confidence, pinning, and optional expiry. Working context defaults to fourteen days. Related facts become suggestions, not similarity-only overwrites. Recall ranks exact/keyword matches and compatible-model semantic matches; low-similarity candidates are excluded.

Deletion removes memory/embedding rows, scrubs historical versions from controlled database logs and caches, invalidates dependent checkpoints, and leaves a content-free receipt. Active jobs refresh their context after a revision change. Source emails/documents remain and can legitimately cause relearning.

Already-sent emails/model requests, remote provider retention, backups, and external Docker/syslog copies cannot be retracted by this service. Legacy paraphrases lacking provenance cannot always be attributed reliably. Deleting a memory is not erasing every historical occurrence of its underlying fact.

## Upgrade

Stop old workers/project scheduling, back up PostgreSQL, and export project history before upgrading. Do not mix old binaries with the new schema. Migration 005 preserves note content/audit/lifecycle metadata as memories and useful entity labels as tags. Project/task history is retained on parent jobs; waiting parents receive explicit retirement instructions, while admin-review states remain paused. Existing child jobs remain ordinary jobs; unstarted project tasks are not queued. Retired tables are removed and feature bootstrap does not recreate them.

The historical base schema retains compatibility definitions consumed and retired by ordered migrations before workers operate. Backup restore is the rollback procedure. Migrations apply at startup; this change does not migrate an existing running deployment automatically.

## Optional HTTPS API

The Compose `api` profile is opt-in. Run the wizard's HTTPS API section to import
a trusted hostname-valid certificate/key and generate/configure a bearer token.
The token is mounted as a Docker secret. TLS files are protected under
`data/private/tls` and mounted read-only at `/run/assistant/tls`.

Start with `docker compose --profile api up -d agent-api`. Requests require `Authorization: Bearer TOKEN`. Do not disable client certificate verification. Browser/workspace/project/note/entity routes do not exist. Backend ports stay unpublished. Docker daemon access is separate: local OS-controlled Unix socket, SSH, or mutual TLS; never unauthenticated TCP. Private same-host container protocols are not exposed application access.