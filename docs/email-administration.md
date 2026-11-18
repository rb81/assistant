# Administration and Approvals

Use `/admin` for deterministic administration; commands and errors bypass chat
and embedding models. Ordinary requests still go through the assistant.

## Approval channels

Choose **Approval delivery** in Settings under **Safety & Approvals > Approvals**:
`email`, `telegram`, `both`, or `client`. The shipped default is `email`.
Use `client` for Management API review without requiring an email/Telegram
approval channel. Client IDs are delivery identities, not authentication;
review still requires the instance's administrative API credential.

In client mode, the server queues a notice for a registered client; inspect and
decide pending approvals through the API/client UI. Registration and live polling
are separate. If no client is registered, notice delivery fails explicitly;
inspect pending approvals rather than assuming an action ran. See
[client delivery](building-clients.md#client-channels-polling-and-fallback).

The administrator's Telegram identity reuses the verified
`AGENT_TELEGRAM_RECIPIENTS` mapping for `AGENT_ADMIN_EMAIL`; that numeric ID
must also be in `AGENT_TELEGRAM_ALLOWED_USER_IDS`. No separate admin-ID setting
exists. Other mappings, the general allowlist, and `/start` do not grant admin
rights. Configure `AGENT_APPROVALS_CHANNEL` to select the channel.

Telegram requests use private-chat buttons. Review the summary and **Details**
before deciding. In `both` mode, email and Telegram share one single-use
approval; confirming either invalidates the other. If Telegram delivery fails
or times out, the same approval is sent to the admin email even when Telegram
was selected. Successful Telegram delivery does not send an extra email.
Fallback needs SMTP; email replies also need email intake and its downloader.
Delivery alone never authorizes an action.

Script requests offer **Approve once**, **Approve always**, and **Deny**.
Website requests also support a grant for this script version or all approved
scripts. **Once** permits one execution, not the whole job. Provide feedback
before deciding if useful. Email replies must quote the original request and
start with `APPROVE ONCE`, `APPROVE ALWAYS`, `APPROVE ALWAYS FOR ALL SCRIPTS`
(websites only), or `DENY`; full script manifests are attached.
See [network access approvals](sandbox.md#network-access-approvals).

Telegram `/permissions` shows versions, host grants, legacy rules, and approval
history. In the terminal interface, open **Activity → Script permissions** to
inspect scripts and revoke or block them. Permission changes stop active sandbox runs. Use
`/menu` → **Needs attention** for paused tasks and its **Approval inbox** for
pending confirmations, including held inbound mail. Expired buttons require a
fresh approval; expiry does not delete the job.

## Email authentication

Configure a dedicated mailbox and `AGENT_ADMIN_EMAIL` in `./run.sh`. Signing
keys are not used; `Authentication-Results` never authorize an action. The
recommended [admin sender authentication](security.md#admin-sender-authentication-spfdkim)
check is enabled by default and quarantines mail claiming the admin address
when both DKIM and SPF fail. This check can reject mail but cannot grant
approval. Disable it in **Security** only if the provider cannot pass either
check.

An explicit `/admin` command always requires approval. Ordinary admin task
emails need routine confirmation only for a new conversation when
**Confirm new admin conversations** is enabled (default on:
`agent.security.confirm_new_admin_conversations` /
`AGENT_SECURITY_CONFIRM_NEW_ADMIN_CONVERSATIONS`). A reply matched to stored
mail, or a continuation in an existing conversation, does not need this
routine confirmation if the sender participated in or received assistant mail
on that conversation. Subject prefixes and unknown reply headers do not prove
history. Turning the setting off allows fresh admin tasks without routine
confirmation. Sender authentication and enabled decision-model screening still
apply; suspicious content can still be held. Follow-ups add context, not admin
authority, script/host permission, limit increases, or safety-hold clearance.

For a fresh admin task email, Telegram can show the message with **Confirm &
start**. This processes the held request without a second confirmation screen
and grants no script permissions.

When email approvals are selected, the system sends a new, unthreaded email
only to `AGENT_ADMIN_EMAIL`. It contains a random OTP, a SHA-256 request hash,
the action summary, and a ten-minute expiry. Job-review notifications have
their own confirmation. Reply to the original email and retain its OTP/hash;
subject lines alone do not authenticate. Text above the quoted confirmation
can guide `task`, `review`, or `instruct` actions, but not other deterministic
commands. Signatures and quoted history are excluded from that guidance.

The configured admin sender and a valid one-use OTP/hash are required. The
server executes the stored action and original content, not a changed subject
or reply thread. Expired, mismatched, changed-target, automated, or duplicate
approvals fail closed. Use `/admin review JOB_ID` to request a fresh review
after expiry. Email approval replies missing the original OTP/hash are
quarantined, not treated as new admin tasks. If a task has no deliverable
requester address, it is paused before admin confirmation is generated.

The OTP proves access to the mailbox; the hash binds the stored request, not a
second authentication factor. Anyone given the confirmation can use it, so do
not forward it. Tokens are stored hashed and never sent to models. Delivery is
limited to ten new confirmations per minute. A delivery failure does not repeat
an approved action; inspect local state before issuing a new request.

Plain administrator questions are recorded as sent messages, so a reply can
provide ordinary task guidance without another routine confirmation.
Version-bound clarification emails that include an OTP/hash can be answered by
retaining the original message. Neither grants script or host permissions.
Approving a job review resumes it and records the transition without a separate
acknowledgment email; explicit `/admin` commands email their results.

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

Put memory content, review guidance, or `instruct` text in the message body
(plain text preferred). `requeue` retries a failed, cancelled, waiting,
needs-review, or completed job. `instruct` adds supervisor guidance and
requeues a finished job or one waiting on a question. Neither clears a safety
hold. Use `approve` to accept a suggested memory; use `update` to correct an
existing fact.

Every command sent by email requires the OTP approval above, including
read-only commands. In the local CLI, state-changing commands that request
confirmation need `--yes`. Logs use stable sequence cursors. Structured
secrets and configured secret values are redacted. Memory embeddings refresh
through ordinary workers, not inline in admin commands.

## Recovery CLI

Run these examples from the installation root after setup:

```bash
docker compose --env-file data/.env exec task-agent python -m assistant_agent.admin_commands '/admin status'
docker compose --env-file data/.env exec task-agent python -m assistant_agent.admin_commands '/admin memory suggestions'
docker compose --env-file data/.env exec task-agent python -m assistant_agent.admin_commands '/admin requeue 123' --yes
```

`data/.env` supplies the generated Compose file list, including the credential
overlay. CLI access uses local OS/container authorization and does not require
models or mailbox connectivity. `--yes` confirms locally. For ordinary
administration, use **Activity** in `./run.sh`; `./run.sh tools` opens Services,
not the retired Tools menu.

## Memory And Deletion

Unified memory tracks evidence, provenance, tags, importance/confidence,
pinning, and optional expiry. Working context defaults to fourteen days. Related
facts become suggestions rather than similarity-only overwrites. Recall ranks
exact/keyword matches and compatible-model semantic matches; low-similarity
candidates are excluded.

Deleting memory removes its rows and embeddings, scrubs controlled database
logs/caches, invalidates dependent checkpoints, and leaves a content-free
receipt. Active jobs refresh context after a revision. Source emails and
documents remain and may cause the fact to be relearned.

Deletion cannot retract already-sent messages or model requests, remote
provider retention, backups, or external Docker/syslog copies. Legacy paraphrases
without provenance may not be attributable. Deleting a memory does not erase
every historical occurrence of its underlying fact.

## Security screening approvals

If [decision-model screening](security.md#email-and-action-screening) holds an
inbound email, it is quarantined and the admin receives the usual OTP
confirmation. The default expiry is 24 hours
(`agent.security.decision_model.approval_ttl_minutes`). Approval processes the
original email once, with reply text added as an admin note; it does not
authenticate the sender or grant standing approval. Later screening receives
the prior approval only as message-scoped context.

A held outbound-email call pauses the job as `needs_review`; review permits
exactly that call once without increasing execution budgets. Script findings
are cached per version, security profile, and task context. Reusable admin run
approval bypasses repeated model checks for that version; a once approval
permits one execution. Email YOLO does not bypass script permissions; the separate Script YOLO setting
explicitly disables those checks. See [YOLO modes](security.md#yolo-mode).
