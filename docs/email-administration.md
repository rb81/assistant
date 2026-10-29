# Administration and Approvals

`/admin` subjects are handled before artifact processing or assistant job
creation. Commands and errors bypass chat/embedding models. Ordinary tasks use
the assistant; administration uses deterministic commands and local setup tools.

## Approval channels

In setup, open **Advanced Settings → Approvals and names**. Choose `email`,
`telegram`, or `both`; existing installations keep `email`. Telegram-only is
recommended when Telegram is configured. The administrator's Telegram identity is
the existing verified `TELEGRAM_RECIPIENTS` mapping for `ADMIN_EMAIL`; its numeric
ID must also be allowlisted. No separate administrator-ID setting is needed.
Other recipient mappings, the general allowlist, and `/start` do not grant admin
rights. Choose the delivery channel with `AGENT_APPROVAL_CHANNEL`.

Telegram approvals use private-chat buttons instead of copied codes. Review the
summary, open **Details** for the exact action, then confirm. In `both` mode the
email and Telegram buttons consume the same one-use record: confirming either
invalidates the other. There is no silent fallback to an unselected channel.
Both selected channels are attempted; partial or uncertain delivery is reported
and the original record is preserved rather than blindly resent.

Script run requests offer **Approve once**, **Approve always**, and **Deny**.
Website requests additionally allow choosing always for this script version or
all approved scripts. Once means one execution, not the entire job. On Telegram,
use **Add feedback** before deciding and **Always...** to choose website scope.
By email, reply quoting the original request with `APPROVE ONCE`, `APPROVE ALWAYS`,
`APPROVE ALWAYS FOR ALL SCRIPTS` (websites only), or `DENY` on the first line;
optional feedback follows. Full script manifests are attached for review.
See
[network access approvals](sandbox.md#network-access-approvals).

Telegram **/list → Permissions** and setup **Tools → Script and website
permissions** list registered versions, host grants, legacy rules and approval
history, and support revocation and blocking. Changes also stop active sandbox
runs; failures to reach the broker are reported explicitly.

Use `/list` → **Needs attention** for paused tasks and its **Approval inbox** for
pending confirmations, including held inbound emails. Expired buttons offer a fresh
confirmation. Expiry never deletes the job: its status card can request a current
approval even after the old confirmation is no longer retained.

## Email authentication

Run `./setup.sh` to configure the dedicated assistant mailbox and
`ADMIN_EMAIL`. Signing keys are no longer used, and Authentication-Results never
authorize an action. The optional [admin sender authentication](security.md#admin-sender-authentication-spfdkim)
check quarantines mail that claims your address but fails both DKIM and SPF. It can
only reject mail; it never replaces the OTP.

An `/admin` request or actionable task email from the configured admin is held
pending approval. When email approvals are selected, the assistant sends a
**new, independent email** only to
`ADMIN_EMAIL`, without threading headers or inherited Reply-To. It contains a
random OTP, a SHA-256 request hash, an action summary and a ten-minute expiry.
Job review notifications include their own confirmation.
Admin-input questions also include the one-use OTP/hash in the initial email,
bound to the paused job's version. Replying with that original message answers
and resumes the job directly, without increasing limits or granting permissions;
no second confirmation is requested. A clarification is not a safety approval.
Requester clarifications addressed to the admin mailbox use this same
authenticated flow, rather than a code-less question followed by another challenge.
If a task has no deliverable requester address, it is paused before its
administrator confirmation is generated.

Reply normally, retaining the original confirmation (plain text or HTML).
The OTP and hash may remain in quoted text. A valid reply from the configured
admin consumes the approval once; the stored action and original content are
used, never a changed subject or reply thread. Changed review targets, expired,
mismatched, automated and duplicate approvals fail closed and are logged.
Send a fresh request after expiry; `/admin review JOB_ID` requests another review.
Approving a job review resumes the job and records the transition without a
separate acknowledgment email. Explicit `/admin` commands still email their results.
Approval-email replies missing the original OTP/hash are quarantined and logged,
not treated as fresh admin tasks. Keep the original confirmation in your reply;
subject lines alone never authenticate an approval.

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
/admin permissions scripts
/admin permissions hosts
/admin permissions show 1
/admin permissions block-script 1
/admin permissions revoke-script 1
/admin permissions block-host api.example.com
/admin permissions history
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
sudo docker compose --env-file data/.env exec task-agent python -m assistant_agent.admin_commands '/admin status'
sudo docker compose --env-file data/.env exec task-agent python -m assistant_agent.admin_commands '/admin memory suggestions'
sudo docker compose --env-file data/.env exec task-agent python -m assistant_agent.admin_commands '/admin requeue 123' --yes
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
admin-authenticated. A held outbound-email call pauses the job as `needs_review`; review permits
exactly that call once without increasing execution budgets. Script findings
are cached per version/security profile. An administrator's reusable run approval
bypasses repeated model checks for that version; a once approval permits one
execution only. Script run permissions remain mandatory even with YOLO mode on.
