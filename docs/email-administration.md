# Email-First Administration

Browser administration and shared editing are removed. `/admin` subjects are handled before artifact processing or assistant job creation. All commands and errors bypass chat/embedding models. Ordinary tasks still use the assistant. Private attachments, malware scanning, conversion, sandbox files, and emailed deliverables remain supported. Native reminders are unchanged; CalDAV is deferred.

## Authentication

Run `./scripts/setup.sh` to configure the dedicated assistant mailbox and
`ADMIN_EMAIL`. Signing keys and trusted Authentication-Results settings are no
longer used.

An `/admin` request or actionable task email from the configured admin is held
pending approval. The assistant sends a **new, independent email** only to
`ADMIN_EMAIL`, without threading headers or inherited Reply-To. It contains a
random OTP, a SHA-256 request hash, an action summary and a ten-minute expiry.
Job review notifications include their own confirmation.

Reply normally, retaining the original confirmation (plain text or HTML).
The OTP and hash may remain in quoted text. A valid reply from the configured
admin consumes the approval once; the stored action and original content are
used, never a changed subject or reply thread. Changed review targets, expired,
mismatched, automated and duplicate approvals fail closed and are logged.
Send a fresh request after expiry; `/admin review JOB_ID` requests another review.

Text above the quoted confirmation is additional LLM guidance for a task,
`review`, or `instruct` command. Signatures, quoted history and approval
credentials are excluded. No new text means approval without extra guidance.
Other `/admin` commands remain deterministic and do not apply extra LLM guidance;
their responses explain how to use `/admin instruct JOB_ID` instead.

The random OTP proves possession of the email; the hash binds the stored
request, not a second authentication factor. From alone never authorizes an
action. This trusts the admin mailbox and anyone to whom its confirmation is
disclosed. Do not forward confirmations. Tokens are stored hashed and never sent
to models. Delivery is limited to ten new confirmations per minute, with
Message-ID deduplication. Failed result delivery does not repeat an approved
action; inspect state using the CLI before sending a fresh request.

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
```

Put memory text, review instructions or `instruct` instructions in the plain-text body. `requeue` retries a failed, cancelled, waiting, needs-review or completed job; `instruct` adds a supervisor instruction and requeues a finished job so it is processed. Approval accepts a suggested new fact; use explicit update for a correction to an existing fact. Destructive actions require one-use, admin/action/object-bound confirmations valid for ten minutes. Logs use stable sequence cursors and return the next command. Structured secrets and configured secret values are redacted. Memory embeddings are refreshed by ordinary workers, never inline in commands.

## Recovery CLI

```bash
docker compose exec task-agent python -m assistant_agent.admin_commands '/admin status'
docker compose exec task-agent python -m assistant_agent.admin_commands '/admin memory suggestions'
docker compose exec task-agent python -m assistant_agent.admin_commands '/admin requeue 123' --yes
```

The CLI uses local OS/container authorization and the same service without models or mailbox dependencies. `--yes` confirms destructive commands locally. `./scripts/setup.sh tools` wraps these commands in menus.

## Memory And Deletion

Unified memory carries evidence, provenance, tags, importance/confidence, pinning, and optional expiry. Working context defaults to fourteen days. Related facts become suggestions, not similarity-only overwrites. Recall ranks exact/keyword matches and compatible-model semantic matches; low-similarity candidates are excluded.

Deletion removes memory/embedding rows, scrubs historical versions from controlled database logs and caches, invalidates dependent checkpoints, and leaves a content-free receipt. Active jobs refresh their context after a revision change. Source emails/documents remain and can legitimately cause relearning.

Already-sent emails/model requests, remote provider retention, backups, and external Docker/syslog copies cannot be retracted by this service. Legacy paraphrases lacking provenance cannot always be attributed reliably. Deleting a memory is not erasing every historical occurrence of its underlying fact.

## Upgrade

Stop old workers/project scheduling, back up PostgreSQL, and export project history before upgrading. Do not mix old binaries with the new schema. Migration 005 preserves note content/audit/lifecycle metadata as memories and useful entity labels as tags. Project/task history is retained on parent jobs; waiting parents receive explicit retirement instructions, while admin-review states remain paused. Existing child jobs remain ordinary jobs; unstarted project tasks are not queued. Retired tables are removed and feature bootstrap does not recreate them.

The historical base schema retains compatibility definitions consumed and retired by ordered migrations before workers operate. Backup restore is the rollback procedure. Migrations apply at startup; this change does not migrate an existing running deployment automatically.
