# Email-First Administration

`/admin` subjects are handled before artifact processing or assistant job
creation. Commands and errors bypass chat/embedding models. Ordinary tasks use
the assistant; administration uses deterministic commands and local setup tools.

## Authentication

Run `./setup.sh` to configure the dedicated assistant mailbox and
`ADMIN_EMAIL`. Signing keys are no longer used, and Authentication-Results never
authorize an action. The optional [admin sender authentication](security.md#admin-sender-authentication-spfdkim)
check quarantines mail that claims your address but fails both DKIM and SPF. It can
only reject mail; it never replaces the OTP.

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

Put memory text, review instructions or `instruct` instructions in the message
body (plain text preferred). `requeue` retries a failed, cancelled, waiting,
needs-review or completed job; `instruct` adds a supervisor instruction and
requeues a finished job. Memory `approve` accepts a suggested new fact; use
`update` to correct an existing fact.

Every emailed command requires the OTP approval described above, including
read-only commands. In the local CLI, state-changing commands that request
confirmation require `--yes`. Logs use stable sequence cursors and return the
next command. Structured secrets and configured secret values are redacted.
Memory embeddings are refreshed by ordinary workers, never inline in commands.

## Recovery CLI

```bash
docker compose --env-file data/.env exec task-agent python -m assistant_agent.admin_commands '/admin status'
docker compose --env-file data/.env exec task-agent python -m assistant_agent.admin_commands '/admin memory suggestions'
docker compose --env-file data/.env exec task-agent python -m assistant_agent.admin_commands '/admin requeue 123' --yes
```

Run these examples from the repository root after saving setup. The generated
`data/.env` supplies `COMPOSE_FILE`, including the protected-secret overlay.
The CLI uses local OS/container authorization and the same service without
models or mailbox dependencies. `--yes` confirms commands locally.
`./setup.sh tools` wraps these commands in menus.

## Memory And Deletion

Unified memory carries evidence, provenance, tags, importance/confidence, pinning, and optional expiry. Working context defaults to fourteen days. Related facts become suggestions, not similarity-only overwrites. Recall ranks exact/keyword matches and compatible-model semantic matches; low-similarity candidates are excluded.

Deletion removes memory/embedding rows, scrubs historical versions from controlled database logs and caches, invalidates dependent checkpoints, and leaves a content-free receipt. Active jobs refresh their context after a revision change. Source emails/documents remain and can legitimately cause relearning.

Already-sent emails/model requests, remote provider retention, backups, and external Docker/syslog copies cannot be retracted by this service. Legacy paraphrases lacking provenance cannot always be attributed reliably. Deleting a memory is not erasing every historical occurrence of its underlying fact.

## Security screening approvals

When [decision-model screening](security.md#email-and-action-screening) holds an
inbound email, the message is moved to the quarantine folder, and the admin
receives the usual OTP confirmation. That confirmation expires after 24 hours by
default (`approval_ttl_minutes`). Approving processes the original email once,
with any reply text added as an admin note. It does not mark the job as
admin-authenticated. A held tool call (outbound email or script) pauses the job
as `needs_review`. Approving that review allows exactly the same call once; for
scripts, only while the file contents are unchanged. With YOLO mode on, these
items proceed and you are notified instead.
