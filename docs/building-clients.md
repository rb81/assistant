# Building independent Management API clients

Build desktop, CLI, or TUI clients against the shared Management API, not private
Python methods. The protocol is independent of the
[first-party macOS app](https://github.com/rb81/assistant-client).

The [API reference](management-api.md) lists routes and operations.
Canonical contracts live in
`src/agent/src/assistant_agent/management_contracts.py`; domain argument schemas
come from `management_domains.py`. Start with the [runnable example](#runnable-reference-client).

## Connection and authentication

The default host-published API is `127.0.0.1:8765`. Do not publicly expose it.
Every public route requires `Authorization: Bearer TOKEN`, including health and
schema discovery. Loopback and SSH do not replace API authentication.

For remote clients:

1. Authenticate using the user's SSH configuration/agent.
2. Verify host trust; never silently disable strict host-key checking.
3. Invoke the fixed command `exec "$HOME/.local/bin/assistant" connect`.
4. Read its bounded JSON handoff: `api_version`, `host`, `port`, `token`.
5. Require discovery version **1**, host **127.0.0.1**, a valid port and token.
6. Forward a dynamically allocated local loopback port to that remote loopback
   port using an owned SSH process with `ExitOnForwardFailure=yes`.
7. Perform an authenticated `/v2/health` request and verify protocol version,
   expected installation identity, and required capabilities.

Discovery version 1 is not the writable API version: `/v2/health` returns API
version **2**. Use the returned `instance_id` as installation identity, not a
hash of the token or just the SSH host. Token rotation must not create another
installation identity. A changed identity requires explicit administrator review.

The SSH account must be authorized to administer the installation, not merely
able to log in. Tokens must not appear in command arguments, URLs, logs, profile
JSON, or committed examples. Rediscover the current token on reconnect; if a client
persists it, use an appropriate OS credential store.

Disable credential-bearing redirects, unnecessary proxies/cookies/caching, and
arbitrary URL forwarding. Bind tunnels to loopback only and terminate only owned
processes. Closing a client must not stop the remote platform.

The present API uses an instance-wide administrative token. A client-channel ID
is not a credential and does not grant per-device permissions or revocation.
Do not invent an additional pairing ceremony for SSH-authorized administrators.

## Discover contracts and capabilities

`GET /v1/schema` returns authenticated OpenAPI for the public API versions.
Generate language-specific models from that schema where useful and validate
wire responses at runtime. Pin a tested schema fixture in client tests so builds
do not require a live production server.

`GET /v2/domains` returns typed domain actions with access (`read`/`write`),
argument JSON Schema, and context requirements. Use this catalog to drive an
advanced editor or discover supported operations. Queries use `POST /v2/query`;
domain mutations use the `domain.mutate` operation.

Check required capabilities at connection time. There is no published
server/client release compatibility matrix yet: API version alone does not
guarantee every feature exists. Disable unsupported features with an explanation,
not an empty result. Schema availability does not prove provider readiness.

For a unified model-settings view, require `configuration.model-settings` and
read `GET /v2/configuration/model-settings`. Render its server-owned sections,
including disabled features and advanced fields. Match its revision to the
current editing catalog; never silently merge a newer snapshot into a draft.
Use the supplied display values and provenance for inheritance/provider defaults,
not a client-maintained model list. The response describes saved configuration
only. Link each field's `group` back to its existing editor and preserve the
normal preview/save/activation workflow.

Settings fields carry `metadata.section` for provider-specific blocks. Keep these
headings rather than deriving provider headings from model-role metadata.
Native provider endpoint fields include their defaults in the normal settings
snapshot; do not make users enter a shared URL to select a native provider.

Render `metadata.selection` values `single`, `multiple`, and `ordered` as
selection controls. Local options and display labels are in `choices` and
`choice_labels`. When `choices_source` is present, require
`configuration.choices` and load
`GET /v2/configuration/choices?key=<setting-key>`. Model fields may supply
`&provider=<provider-id>` so a draft provider selection loads the right list
before the route is saved. The response has `key`, `choices`, `choice_labels`,
and `warnings`; show discovery failures and retain unavailable saved entries
without offering them as new selections. Ordered selections must expose
reordering. The wire value remains a comma-separated string for compatibility;
that is not a suitable editing control.

The direct-provider model-attestation list enumerates model IDs even before
verification. This does not relax inference enforcement or prove eligibility.
Provider credentials must be saved before authenticated discovery can use them.

## Reads, errors, and bounded data

Use v2 for new clients. v1 reads remain a compatibility surface for older clients.
Common resource groups include configuration, credentials/status, services/logs,
records, task messages/summary, approvals/permissions, projects/files, clients,
domains, and operations.

Record queries use `page` and `query`; approvals use `limit`/`offset`; client history
uses `before_id`; file content uses byte `offset`/`limit`. Different resource groups
have different pagination contracts. Do not treat one page as the complete dataset.

Use both server bounds and client response limits. File content uses base64
chunks of at most 1 MiB. Configuration credentials are redacted, but logs,
conversations, and files may still contain private material. Diagnostic export
requires administrator review.

HTTP 401 means failed authentication; 422 means a rejected request contract;
409 reports a management/setup conflict; 503 indicates executor unavailability
or bounded-read capacity. Error bodies are `{"error": {"message": ...}}`: show the
message. Curated problems (for example Docker being unavailable) are explained
directly; unexpected failures return a generic message with a `Server log
reference` to find the details in the management logs. A 404 for a documented
resource usually means the server predates it; check `health.capabilities`.
Update server source first, then rebuild/restart it; `assistant server start`
alone does not download new source.
Retry safe reads/acknowledgments with bounded backoff and jitter, not a busy loop.
Cancelled requests (for example when a view closes) are not errors to display. Invalid JSON or incompatible schemas
are errors, not empty successful responses.

Use `GET /v2/conversations?channel=chat` for chat history and
`channel=reminder` for scheduled runs. Scheduled runs are read-only; direct
users to a new chat instead of offering a reply on the run.

## Mutations and recovery

Submit confirmed typed mutations through `POST /v2/operations` with an
`Idempotency-Key` (8-128 allowed identifier characters; a random UUID is suitable).
Use one key per immutable submission intent, not a new key for each network retry.
Never reuse a key for a different payload.

HTTP **202 means accepted**, not completed. Record the returned operation `id`;
poll `GET /v2/operations/{id}` until its state is:

| State | Meaning |
|---|---|
| `queued`, `running` | Still pending; inspect `progress`. |
| `succeeded` | Verified operation completion under that contract. |
| `failed` | Failure; partial effects can still exist. Inspect `error` and state. |
| `interrupted` | Completion was not established after interruption. Inspect before retrying. |

The operation journal stores metadata, not credential values, instructions,
message bodies, or file bytes. The last 500 statuses are retained. Evicted keys
remain protected from re-execution for at least 30 days, but their results may
no longer be recoverable. Do not automatically replay requests after that window.

Persist non-secret submission keys and accepted operation IDs if you need crash
recovery. If the submission response is lost, preserve an **unknown outcome**
and inspect existing operations/resources before resubmission. Do not persist
secret request bodies merely to build an automatic retry queue.

Control-plane restarts can disconnect the API before verification completes.
Reconnect through discovery, verify identity, and resume operation inspection.
Never report a successful restart solely because the original connection closed.

Configuration saves additionally require:

1. Read `/v2/configuration/catalog` and page-specific
   `/v2/configuration/settings?group=...`; preserve and compare their `revision`.
   Load `/v2/configuration/instructions` only when editing standing instructions.
2. Send only draft changes to `/v2/configuration/preview`.
3. Display validation problems and the exact change preview.
4. Submit `configuration.save` with the same draft/revision, returned `preview_id`,
   and explicit confirmation.
5. Activate affected services separately, or explicitly choose reviewed `apply: true`.

Ordinary settings must not fetch source files or saved credential values.
Source YAML can still be edited by legacy operator clients with `agent_yaml` in the same preview/save contract.
The API retains three previous source versions and exposes their fixed-name
contents for loading into a new draft. Changed source with `apply: true` rebuilds
application images; unchanged source does not. See the [API reference](management-api.md)
for backup rotation and executor access requirements.

Editing a draft invalidates its prior confirmation. External edits require a new
revision/preview, not overwriting the server. Read-only installation settings
remain read-only. Credentials are replace/remove workflows, not secret read-back.

Approval decisions require the displayed opaque reference and version. Do not
extract OTPs or approve changed/expired/redacted reviews. Task/project deletions
require current server previews/digests and exact contract confirmations.
For email, show `action.review` by default and offer original message text
separately. Label AI-generated summaries and preserve the original as the
authority. Show `executions` states separately from the decision; queued is not
completed, and uncertain outcomes require reconciliation. Older servers may
omit these fields.
Database reset requires the literal `RESET` confirmation.

## Client channels, polling, and fallback

Generate a non-secret 32-character lowercase hex client ID and retain it per
installation. Register it through `client.register` with the chosen name,
client type, and fallback order. Reuse that ID after reconnect/token rotation.

Poll `/v2/clients/{id}/messages` at the returned `poll_interval_seconds`.
Polling renews availability; registration alone does not. Keep one sequential
message poller per channel. Do not let long-running operations or file transfers
block its availability heartbeat.

Deduplicate by message ID, durably accept messages into the client (or render
them successfully for an output-only client), then POST
`message_ids` to `/v2/clients/{id}/acknowledgments`. Acknowledgment means delivery,
not a human read receipt. A failed acknowledgment is not success; allow reoffers
and retry safely. A GUI should acknowledge background receipt without waiting
for the person to open a conversation; human unread markers remain separate.
Do not acknowledge before successful local acceptance or rendering/output.

Use `/history` for retained delivery states and older pages; do not download all
history on every heartbeat. Stop polling when the client truly exits. An
unavailable client or unacknowledged delivery falls back according to server
settings (normally Telegram, then email), not another arbitrary registered client.
Absent fallback configuration retains the message for later polling.

For routing diagnostics, run `sh examples/diagnose-routing.sh` from a server
checkout with the task-agent container running. Optionally append a positive
task ID. The script uses a read-only database snapshot and reports delivery
timing, channels, status, and approval-fallback flags, excluding message bodies,
addresses, credentials, and approval tokens. Set `ASSISTANT_COMPOSE_PROJECT`
for a non-default Compose project, or `ASSISTANT_DIAGNOSTIC_CONTAINER` to select
its task-agent container explicitly.

External delivery can be `uncertain`. An administrator can use
`client.delivery-resolve` to mark it sent or return it to the client. Returning it
can duplicate an external delivery, so require explicit review.

## Workspace containment and extensions

Project-file reads require membership in the selected project. Workspace domain
operations are restricted to permitted workspace paths, not arbitrary host files.
File writes/uploads require a clean malware scan; updates require the current
digest where specified. Chunked reads/writes do not imply transactional,
resumable large uploads. Display incomplete/partial transfer outcomes.

Extend shared server contracts/domain logic before adding unsupported client
features. Backup, restore, server upgrades, per-device auth, and unrestricted
log/resource metrics must not be claimed just because an advanced console exists.
Never add a generic shell/SQL/Docker RPC as a shortcut.

## Runnable reference client

The standard-library Python example has no first-party UI dependency:

```sh
python3 examples/management-client/client.py --ssh your-server-alias health
python3 examples/management-client/client.py --ssh your-server-alias domains
python3 examples/management-client/client.py --ssh your-server-alias inbox \
  --client-id 0123456789abcdef0123456789abcdef
```

Verify SSH trust/authentication first. The example discovers credentials,
owns/cleans up its tunnel, checks v2 health, and uses bounded authenticated reads.
Inbox mode registers/polls the chosen channel, prints messages, flushes output,
then acknowledges successful delivery. Keep its client ID between runs.
It intentionally provides no automatic mutation replay or token persistence.

Test custom clients with isolated fixtures: trust/auth failures, incompatible
schemas, lost submission responses, restart recovery, duplicate deliveries,
acknowledgment failure, stale configuration previews, file conflicts, and provider
failure. The static API audit inventories contracts; it does not verify your client.
