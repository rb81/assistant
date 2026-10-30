# Sandbox Command Execution

This document describes how Assistant executes shell commands safely.

## Components

- `ToolRuntime.command_execute` in `src/agent/src/assistant_agent/tools.py` —
  agent-facing tool.
- `src/sandbox/server.py` — broker API service.
- Per-command run containers — short-lived execution environments.

## Control Flow

1. For iterative work, the agent calls `development_session_request` with a
   development brief, initial script, hosts and limits. After the admin approves,
   `script_development_review` reviews each complete version and host set against
   that immutable brief. Alternatively, the agent requests final approval directly.
2. `script_approval_request` closes further development execution and submits the
   final script and hosts together, including the supervisor's final comments.
   Direct requests without a development session also receive a tool-free
   supervisor assessment, without treating the agent's purpose as an approved brief.
3. `ToolRuntime.command_execute` validates input, authorization and shared-root workdir.
   Missing approval returns an explicit error directing the agent to the development
   or final approval tool; execution cannot generate an incomplete implicit approval.
4. Request is sent to sandbox broker `POST /execute`.
5. Broker starts a fresh run container. Approved code is copied into a private
   read-only volume at `/approved-code`; script arguments reference that snapshot.
   The working directory and data outputs remain in the writable shared workspace.
6. Broker captures stdout/stderr/exit status.
7. Broker attempts to remove the run container and per-run network, including
   after errors or timeouts.
8. Result is returned to task-agent and logged.

## Safety Properties

- Commands do **not** run in task-agent container.
- The working directory must resolve under the shared root. This is not a
  filesystem-access filter on the command itself.
- Broker can enforce runtime limits (CPU, memory, PID, timeout).
- Broker container is hardened (`read_only`, dropped caps, `no-new-privileges`).
- Broker requests use HTTPS with hostname verification and a service-scoped bearer token.
- Run containers execute as root by default and mount the writable shared
  workspace (no host documentation, app
  credential mounts or Docker socket). Other container-local paths remain
  accessible; do not put secrets in workspace files.

## Runtime help and command environment

For functionality, capabilities, limitations, architecture and tool usage, use
`help_search` first. Use `help_topic` with a returned `topic_id` and documentation
revision to retrieve a section. `runtime_capabilities` reports enabled tools,
live sandbox modes and resource limits without exposing credentials.
Help is chunked from documentation bundled with the running application image;
idle indexing caches vectors using the configured embedding service. Retrieval
uses semantic similarity, with an explicit keyword fallback if embeddings are
disabled, pending or unavailable. Rebuilding updates the help content.
Host documentation is not mounted
into the workspace, broker or command containers. Help is reference material,
not permission to change host configuration or bypass tool policies.

The default image is defined by `src/sandbox/Dockerfile` and
`src/sandbox/requirements.txt`; custom images may differ. It includes:

- Commands: `python` / `python3`, `curl`, `wget`, `git`, `jq`, `file`, `unzip`,
  `dig` / `nslookup` and `ping` (subject to dropped `NET_RAW` capability).
- Python libraries: `requests`, `httpx`, `beautifulsoup4` (`bs4`), `lxml`,
  `html5lib`, `trafilatura`, `readability-lxml`, `markdownify`, `python-dateutil`.

Each command starts a fresh container. Packages installed in its writable layer
do not persist; install and use them in the same call or use a workspace-local
environment. Only workspace changes persist. Default limits are 2 CPUs, 2 GiB
memory, 512 processes and 300 seconds. The default image has no browser automation
stack such as Playwright or Selenium.

Application credentials are unavailable to scripts. Use dedicated integration
tools, including `llm_service`, `text_to_speech` and channel delivery tools,
rather than searching for secrets or recreating authenticated integrations.
Prefer `deep_research_request` for substantial research and `command_execute`
for custom scripts, direct URL checks, downloads and focused diagnostics.

## Calling the tool

```json
{"command":["python","script.py"],"workdir":"reports","timeout_seconds":60}
```

Relative workdirs resolve under `/data/share` by default and must already exist.
The example runs `/data/share/reports/script.py`. Shell expansion, redirection
and pipelines require an explicit shell, for example
`{"command":["sh","-c","printf 'hello\\n' | wc -l"]}`.

Results include `stdout`, `stderr`, `exit_code`, `timed_out`, `duration_ms`,
isolation metadata and the request-attempt count. Tool-result caching may replace
large output with previews and a workspace cache path. A command timeout is
returned as `timed_out=true`, not retried as a broker failure.

### Script versions and run permissions

Finished scripts and their exact HTTPS host list require one combined
**Approve one run**, **Always allow this version**, or **Deny** decision, unless explicitly
enabled Script YOLO bypasses approvals. Optional feedback accompanies any
decision. A denial does not cancel the job or authorize another attempt at the
same action. The agent should continue without that capability or report the blocker.

The approval card shows the purpose, supervisor recommendation and comments, exact
hosts, and a plain-language screening result. Code, hashes, raw screening data and
version details are attachments, not inline JSON. Telegram's **View script & details**
opens these documents. Email replies still use `APPROVE ONCE`, `APPROVE ALWAYS`, or
`DENY`. A supervisor outage is shown explicitly and requires manual administrator
review; a model recommendation never grants execution permission by itself.

Persistent approval identities include SHA-256 hashes of the entry file and
analysed local code dependencies, exact command arguments, workdir, and an
explicit approval-policy revision. Changes require new approval or a fresh
supervisor review inside an active development session. Final host grants are
version-bound and apply only to the exact approved host set.
Do not use symlinks for code paths.

Always approval is available only for dependency graphs that can be pinned.
Uninspectable/dynamic code, package installs, wrapped interpreter commands and
other unsupported dependency graphs require one-time approval. Static analysis
is not a complete proof of script behaviour. Data inputs are not frozen.

Use the workdir for data paths and outputs, not `__file__`: it refers to the
read-only code snapshot. Python local packages and static JavaScript relative
imports are included in the bundle. Inline Python and local module execution
resolve code from the snapshot rather than the mutable workdir.

Once means **one execution**, including subprocesses and connections. Grants
are consumed before dispatch and are not restored on failure or a lost response.
Approved script dispatches are never automatically retried. Approval does not
increase task budgets. Version-bound executions require container isolation and
proxy egress (or network mode `none`), not process/open-network modes.

### Supervised development sessions

The brief specifies objective/outputs, permitted data/files, external effects,
exact intended hosts, duration and execution budget. The admin may explicitly
delegate related public host changes to the supervisor; the default is no
host changes. Such delegation never permits new data recipients or effects.
Live tests can produce irreversible effects before final approval.

The supervisor is a separate, tool-free model call with the original approved
brief and the complete code/dependency manifest, not agent summaries. It returns
approve, reject with comments, or escalate. Implementation changes may remain
within scope; the supervisor cannot rewrite the brief. Uncertainty, malformed
output and provider failure never authorize execution. Static analysis and model
judgment are best effort, not proof of behavior or a filesystem-access boundary.
Commands still have access to the writable shared workspace; use test data and
dedicated test accounts rather than assuming development creates a private copy.

The supervisor reviews authorization, not QA, performance or feature completeness.
Intermediate implementations and small technical fixes may remain within the brief.
Trusted broker modes are supplied separately from source so the supervisor does
not require scripts to duplicate enforced proxy controls or disable the required
proxy. Scope expansion, new recipients and unauthorized effects remain grounds
for rejection or escalation. Identical reviews (including rejections) are reused.

Each approved executable fingerprint and exact host set may run repeatedly
within that job's session. Changes require review. Runs consume budget atomically,
including failed attempts; dispatch is never automatically retried. The broker
rejects expired dispatches and terminates running commands at session expiry.
Unpinnable dependency graphs require final one-time admin approval instead.

Configuration under `agent.security.development`:

```yaml
model: ""
max_minutes: 30
max_runs: 20
```

Leave `model` blank to use the configured agent provider/model through a separate invocation.
To choose a different model, enter its OpenRouter ID, such as
`openai/gpt-sol-6.1`; this uses `OPENROUTER_API_KEY` and needs no preset.
Presets and the literal `default` are not accepted as model IDs.
Configure this in **`./run.sh` → Settings → Safety & screening → Development supervisor model ID**,
or run `./run.sh --plain --section security`.
The environment override is `AGENT_SECURITY_DEVELOPMENT_MODEL`; an empty value
also clears a model configured in YAML. This replaces the earlier
`model_preset` / `AGENT_SECURITY_DEVELOPMENT_MODEL_PRESET` setting; re-enter any
previous override as a model ID in the new field.
Requested session limits cannot exceed these
ceilings. Expiry starts when the admin approves, not when the agent requests.

Final submission stops further development execution. The admin sees the
supervisor's final decision/comments and the full exact host list; an escalation
is clearly labeled, not presented as supervisor approval. Email includes the full
code in the body and attachment. Telegram offers **Review Script**. Final approval
is bound to the executable fingerprint and selected hosts; a denial grants no
ongoing permission and cannot undo development-time effects.
Task completion is refused while development is pending, active or finalizing;
submit the final approval or explicitly fail the task rather than silently leaving
development without a final decision. Networking scripts with detected destinations
cannot submit an empty final host list. No hosts are automatically granted from
source-code URL mentions.

## Network Isolation

- Broker is attached to internal sandbox control network.
- With the default `SANDBOX_RUN_NETWORK_MODE=per_run_bridge` and
  `SANDBOX_EGRESS_MODE=proxy`, each command gets an *internal* network shared
  only with a per-run egress proxy sidecar. The proxy is the only way out:
  - HTTPS `CONNECT` to port 443 only; plain HTTP, other ports, IP literals and
    hosts resolving to private/internal addresses are refused.
  - The TLS server name (SNI) must match the approved host.
  - DNS from the run container goes to a sink address, so DNS cannot be used
    to exfiltrate data.
  - The policy (allowed and blocked hosts) is fixed when the command starts.
- Allowed hosts come only from the authorized development version or final
  approved script version. There are no global host allows or package-registry
  exceptions. Any matching block beats any allow. New requests
  use exact hosts, never implicit wildcards. Rules cover all HTTPS paths on the
  named host, not individual URLs or repositories.
- `command_execute` results include a `network` summary listing allowed and
  blocked hosts with reasons and what to do next.
- `SANDBOX_EGRESS_MODE=open` restores direct internet from per-run bridges
  (no proxy). The `bridge` and `existing` network modes bypass the proxy and
  keep direct internet; `none` has no network.
- Run containers do not join application service networks. The Docker host's
  bridge gateway may still be reachable; firewall host services as before.
- The proxy checks host names (CONNECT and SNI), not encrypted HTTP content. An
  approved host on a shared CDN could in principle relay to other sites behind
  the same CDN (domain fronting). Approve hosts you trust, not just URLs.

### Proxy-compatible scripts and troubleshooting

Keep the injected `HTTPS_PROXY` / `HTTP_PROXY` settings enabled. In Python
`requests`, `Session.trust_env = False` disables them unless you explicitly
configure the proxy. Direct DNS lookups, IP pinning and direct TLS sockets fail
in default proxy mode; `dig` / `nslookup` failures do not mean a site is down.
For certificate inspections, use a proxy-compatible CONNECT client. The proxy
enforces public-address restrictions; scripts need not duplicate those guards.

Inspect stdout, stderr, HTTP status and the command's network summary. A script
returning zero when every request failed has not passed testing. Use timeouts,
a descriptive User-Agent and redirects only to authorized hosts. HTTP 403/406,
authentication, JavaScript requirements and anti-bot controls are not necessarily
fixed by browser-like headers. Never use mirrors, IP addresses, DNS tricks or
other tools to circumvent a denial.

Routine image rebuilds, system upgrades and configuration changes do not
invalidate unchanged script approvals. Administrator-installed runtime and
dependency updates are trusted maintenance; they can change script behaviour
without another approval. Each execution is separately authorized against the
current sandbox image and isolation settings; a change between authorization
and dispatch is rejected. Current blocks, revocations and runtime restrictions
still apply. Incompatible approval semantics require an explicit bump to
`APPROVAL_REVISION` in `script_permissions.py`.

Request the complete intended hosts: an empty list means offline, not
inheritance from an earlier version. Preserve the exact approved interpreter,
arguments and workdir for scheduled runs.

On upgrade, legacy versions are adopted lazily after inspecting the current
code, invocation and workdir. The existing version ID, approval hash, grants,
denials, one-run reservations and reminder bindings are retained; permissions
are never copied or merged. Unapproved duplicate versions from earlier rebuild
checks do not displace the approved version. If multiple identical legacy
versions have permission or reference history, automatic adoption stops and
requires administrator reconciliation. A changed script is never adopted as
an older approved version. The stored legacy manifest's environment remains
historical evidence, not the identity used to authorize the next execution.

### Scheduled scripts

Recurring or future script work is scheduled with
`reminder_create(execution="script", script={command, workdir?, urls, purpose})`.
The script must already exist and be final: the reminder is not saved until
the exact version and complete host set have suitable approval.
For recurring reminders, the request offers **Allow recurring runs of this
version** or **Deny**; one-run approval is refused because it would not cover
future runs. For one-off reminders, it offers **Approve this scheduled run**
or **Deny**. The one-run grant is reserved exclusively for that reminder:
neither the creating job nor another reminder can consume it. Existing standing
approval can cover either kind of reminder without another prompt.
After approval, the system saves the schedule by replaying the original
`reminder_create` (or `reminder_update`) call exactly as submitted; the agent
does not re-type it. Within the same task, a later request for an
already-approved version with a different host list is refused with the
approved hosts instead of sending the administrator a new request. Changing the script, argv,
workdir or hosts of an existing schedule uses `reminder_update(script=...)`,
which needs the same approval. Converting a one-off reminder to recurring
also checks for standing approval, even when the script is unchanged.

Use one script reminder per scheduled task: its task text runs the approved
script and then does any follow-up (searches, summaries, emails). Inside a
development session, `reminder_create(execution="script")` is itself the final
approval request, so no separate `script_approval_request` is needed. The agent
should go straight to the gated tool; approval is never requested in
conversation.

The reminder stores the bound version, fingerprint and hosts. Each due run gets
the exact command to execute and job metadata marking it as a scheduled run:

- Development sessions are unavailable in scheduled runs.
- Any new approval request raised by a scheduled run is flagged to the admin as
  drift from the approved schedule.
- Before queuing, the scheduler checks that the standing or reserved grant is still active and
  hosts are neither revoked nor blocked. A failed check skips the occurrence
  (recurring reminders advance; one-time reminders fail) and alerts the admin.
- When idle, the agent re-inspects all scheduled scripts at startup and every
  `agent.reminders.script_check_interval_minutes` (default 360). Source, helper,
  invocation or approval-policy revision changes are recorded and alerted once,
  and block future occurrences until the schedule is re-approved. Routine
  rebuilds do not trigger alerts. A successful check also clears an older
  image-drift warning without deleting approval history.

### Network access approvals

`script_approval_request` includes `urls` (up to ten exact hosts or HTTPS URLs),
the exact `command` and optional `workdir`. Offline scripts use an empty list.
`network_access_request` is a compatibility entrypoint: it asks the supervisor
during development, or requests combined final script+host approval otherwise.
Blocked hosts are refused without another approval request.

Choices: **Approve once** (next execution), **Approve always** (this exact
version and host set), or **Deny**. **Add feedback** collects optional guidance.
Development briefs offer approval for that session or denial, never always.
Email replies use `APPROVE ONCE`, `APPROVE ALWAYS`, or `DENY`
on the first line, followed by optional feedback. Both channels resolve the same
one-use request when delivery is configured as `both`.

Manage permissions in Telegram **/permissions** (also **/menu → Permissions**), or
**./run.sh tools → Script and website permissions**. Both use deterministic
operations, not a model. Inspect scripts, stored code/version diffs, host scopes,
pending requests and history; revoke grants or block/unblock scripts and hosts.
Email approvals attach the full code manifest for inspection.

```bash
sudo docker compose --env-file data/.env exec task-agent python -m assistant_agent.admin_commands '/admin permissions scripts'
sudo docker compose --env-file data/.env exec task-agent python -m assistant_agent.admin_commands '/admin permissions hosts'
sudo docker compose --env-file data/.env exec task-agent python -m assistant_agent.admin_commands '/admin permissions block-host api.example.com' --yes
sudo docker compose --env-file data/.env exec task-agent python -m assistant_agent.admin_commands '/admin permissions revoke-script 1' --yes
```

Permission changes stop active sandbox runs and invalidate in-flight policy
snapshots; completed external actions cannot be undone. If the broker cannot
be reached, changes remain saved and the UI explicitly reports that active runs
could not be stopped. Stop the sandbox service before relying on revocation.
Administrator ingress services receive only a derived **revoke-only** token,
never the sandbox execution token.

Existing `sandbox_network_rules` remain visible under **Legacy rules**, but their
allows and configured registry defaults no longer authorize sandbox traffic.
Their blocks still apply. Migration retires previous global host allow grants
with an audit record; it does not convert them into script grants.
Old network-only approval replies and CLI allow operations are refused explicitly;
request a fresh combined version-bound approval instead.
Revoking or blocking a script revokes its development sessions and invalidates
pending approvals as well as preventing use of its host permissions.
Factory reset has a
separate **Sandbox script and network approvals and blocks** category.

For upgrades, save setup again to generate the revoke-only credential and
updated Compose secret grants, then rebuild/recreate the agent and sandbox
services. Runtime database migration preserves existing rules.

## Upgrade and acceptance checklist

Back up local state before upgrading. From the repository root, run `./run.sh`,
review **Advanced Settings → Approvals and names**, select `email`, `telegram`,
or `both`, and save. Telegram requires a configured, verified administrator;
email requires the admin address and working mail delivery.

Rebuild and recreate the stack using the generated Compose configuration:

```bash
docker compose --env-file data/.env up -d --build
docker compose --env-file data/.env ps
docker compose --env-file data/.env logs --tail=100 task-agent sandbox
```

Confirm services are healthy and migrations complete before testing. Do not use
database reset or remove volumes to activate permissions. Existing legacy rules
are preserved: inspect **Permissions → Legacy rules** for broad or job-wide
access you no longer want.

Use a harmless script and non-sensitive data for these checks:

| Check | Expected result |
|---|---|
| New script | Combined script/host approval arrives through the configured channel(s), even with screening disabled. |
| Approve once | One execution succeeds; a second needs approval. Failure does not restore the grant. |
| Approve always | The unchanged invocation can run again, including in another job, without another run prompt. |
| Change source, a local helper, or argv | A new version needs approval; old script-specific host grants do not apply. |
| Request `https://example.com/feed.rss` | The request clearly grants `example.com`, including other HTTPS paths, not just `/feed.rss`; subdomains remain separate. |
| Always for this script | Another version does not inherit the host grant. No global allow is offered. |
| Development brief | Admin approves objective, data/effects, hosts and time/run limits once. |
| Changed development version | Supervisor reviews full code; rejection/escalation cannot execute. |
| Expired or exhausted session | New execution is blocked; expiry also stops active commands. |
| Final submission | Further development execution stops; final comments and hosts accompany combined approval. |
| Deny with feedback | The job receives the decision/guidance; the same denied request is not repeatedly prompted or bypassed. |
| Delivery set to both | Either channel resolves the request; the other channel cannot approve it a second time. |
| Block a host or script | Future matching execution/access is refused; all active sandbox runs are stopped. |
| Revoke a reusable grant | Future use requires approval again unless another applicable grant remains. |
| Schedule a recurring script | `reminder_create(execution="script")` requires standing approval before saving; Approve once is refused. |
| Schedule a one-off script | One execution is reserved for that reminder and consumed on dispatch, even if execution fails. |
| Change or revoke a scheduled script | The next occurrence is skipped with an admin alert instead of prompting at run time. |

Always may be unavailable for an uninspectable dependency graph; use once rather
than treating that as a UI failure. Existing version-bound grants can already
allow a host, so inspect that script's host list before testing a missing-host
prompt. Removing a block does not remove a separate allow, and revoking one grant
does not remove other applicable grants.

For screening tests, enable script screening and approve an unchanged version:
successful findings are cached, reusable approval bypasses repeat checks, and
changed code or policy requires a fresh context. Screening errors are not cached
as success; malicious findings cannot be overridden by ordinary run approval.

If revocation reports that permissions were saved but active runs could not be
stopped, stop the sandbox service before relying on the change. Revocation cannot
undo completed writes or external requests. See [network access approvals](#network-access-approvals)
for management commands and credential boundaries.

### Script YOLO

`agent.security.script_yolo` defaults to false and is separate from the existing
possibly-malicious-email setting. Enabling it explicitly bypasses script, host
and supervisor approvals, allowing any public HTTPS hostname through the proxy.
Explicit script/host blocks, private-address/SNI checks, sandbox isolation and
resource limits remain. Setup requires a typed confirmation and logs a startup
warning. Disabling it restores the approval flow; it does not create reusable
script or host grants.

## Isolation Modes

### `SANDBOX_ISOLATION_MODE=container` (default, production)

This is the default and intended production mode. Each command runs in a fresh,
short-lived Docker container separated from the agent's application networks. The
runner executes as root for package installation; its writable container layer is
removed after execution, but changes to the shared workspace and outbound data
transfer persist. Resource limits, capability drops, network isolation, and
per-run bridge networks are enforced by Docker. Runners drop `NET_RAW` by
default, not all capabilities; the broker drops all capabilities. The broker has
the Docker socket mounted and therefore retains root-equivalent daemon authority; these
containers are not fully isolated from host compromise. The run image must
exist in the daemon; the broker does not pull it automatically.

`container` is the default in both `src/docker-compose.yml` and
`src/sandbox/server.py`. You do not need to set anything to use it.

### `SANDBOX_ISOLATION_MODE=process` (development only)

⚠️ **Process mode is unsuitable for production.** It is provided as a fallback for environments where Docker is unavailable (e.g., bare-metal local development).

To enable it, set it in `data/.env`:

```env
SANDBOX_ISOLATION_MODE=process
```

In process mode, commands run via `subprocess.run` in the broker's own environment
(inside the broker container when using Compose, or on the host if launched
there directly). The workdir check and command timeout remain, but there is no
separate per-command container:

- No per-command memory, CPU, PID, capability or seccomp configuration
- No filesystem isolation from the broker, including its credentials and Docker
  socket when mounted
- No network isolation — the process inherits the broker's network context
- Commands inherit the broker's permissions, mounts and resource limits

Do not use process mode if the agent can receive instructions from untrusted input, or on any server where the consequences of arbitrary command execution are unacceptable.

---

## Retry and Failure Semantics

For ordinary non-script utilities, `command_execute` retries broker-request failures according to:

- `agent.sandbox.max_attempts`
- `agent.sandbox.retry_backoff_seconds`

It does **not** retry returned command results, including non-zero exits and
command timeouts. HTTP errors below 500 fail immediately; server errors and
transport failures are retried unless recognized as a host runtime
misconfiguration. A lost response can cause a command that already ran to be
executed again: retries do not guarantee exactly-once side effects.

Retry exhaustion raises `SandboxAttemptsExhausted` with attempt error details.
A registered script always uses one dispatch attempt, regardless of these settings.
A missing Docker runtime such as `runsc` raises
`SandboxHostConfigurationError` without retrying.

## Configuration Highlights

Edit application settings in `data/config/agent.yaml`; repository defaults are
in `src/config/agent.yaml`:

- `agent.sandbox.enabled`
- `agent.sandbox.base_url`
- `agent.limits.tool_timeout_command_seconds`
- `agent.sandbox.max_attempts`
- `agent.sandbox.retry_backoff_seconds`
- `agent.sandbox.network.allowed_hosts` (`AGENT_SANDBOX_ALLOWED_HOSTS`)
- `agent.reminders.script_check_interval_minutes` (scheduled script re-inspection, default 360)

Compose reads these settings from `data/.env`:

- `SANDBOX_IMAGE` (used for both broker and run image)
- `SANDBOX_ISOLATION_MODE`, `SANDBOX_RUN_NETWORK_MODE`, `SANDBOX_EGRESS_MODE`, `SANDBOX_RUN_CAP_DROP`
- `SANDBOX_RUN_CPUS`, `SANDBOX_RUN_MEMORY_BYTES`, `SANDBOX_RUN_PIDS_LIMIT`
- `SANDBOX_SHARED_ROOT_HOST` (generated by setup)

The broker also supports `SANDBOX_RUN_RUNTIME` (for example `runsc`),
`SANDBOX_RUN_USER`, `SANDBOX_RUN_NETWORK` and timeout overrides. The supplied
Compose file does not forward these variables: add them to the broker's
`environment` with a Compose override, or set them when running the broker
directly. The default Docker runtime is used unless overridden. Broker default
and maximum command timeouts are both 300 seconds; increasing the agent limit
alone does not increase the broker maximum.

## Operational Note

See [security](security.md#sandbox-docker-socket) for Docker-daemon trust limits.
