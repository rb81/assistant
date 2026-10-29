# Sandbox Command Execution

This document describes how Assistant executes shell commands safely.

## Components

- `ToolRuntime.command_execute` in `src/agent/src/assistant_agent/tools.py` —
  agent-facing tool.
- `src/sandbox/server.py` — broker API service.
- Per-command run containers — short-lived execution environments.

## Control Flow

1. Agent finishes a script and calls `script_approval_request` with its exact
   argv, purpose and optional workdir, then `network_access_request` for needed
   HTTPS hosts with the same argv/workdir. Both may pause for administrator approval.
2. `ToolRuntime.command_execute` validates input and shared-root workdir.
3. Request is sent to sandbox broker `POST /execute`.
4. Broker starts a fresh run container. Approved code is copied into a private
   read-only volume at `/approved-code`; script arguments reference that snapshot.
   The working directory and data outputs remain in the writable shared workspace.
5. Broker captures stdout/stderr/exit status.
6. Broker attempts to remove the run container and per-run network, including
   after errors or timeouts.
7. Result is returned to task-agent and logged.

## Safety Properties

- Commands do **not** run in task-agent container.
- The working directory must resolve under the shared root. This is not a
  filesystem-access filter on the command itself.
- Broker can enforce runtime limits (CPU, memory, PID, timeout).
- Broker container is hardened (`read_only`, dropped caps, `no-new-privileges`).
- Broker requests use HTTPS with hostname verification and a service-scoped bearer token.
- Run containers execute as root by default and mount the writable shared
  workspace plus read-only documentation at `.assistant/docs` (no app
  credential mounts or Docker socket). Other container-local paths remain
  accessible; do not put secrets in workspace files.

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

Scripts require explicit **Approve once**, **Approve always**, or **Deny** even
when decision-model screening is disabled. Optional feedback accompanies any
decision. A denial does not cancel the job or authorize another attempt at the
same action. The agent should continue without that capability or report the blocker.

Versions include SHA-256 hashes of the entry file and analysed local code
dependencies, exact command arguments, workdir, sandbox image/runtime identity,
and security policy. Changes require new approval and invalidate script-specific
host grants. Global host grants remain global, but never authorize execution.
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
- Allowed hosts are configured defaults (package registries), explicitly retained
  legacy rules, and grants for this approved script version or all approved
  scripts. Any matching block beats any allow, including defaults. New requests
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

### Network access approvals

After run approval, the agent requests required URLs or exact hostnames with
`network_access_request`, supplying the identical `command` and `workdir`.
Allowed hosts return `approved`; blocked hosts are refused without another
approval request. Otherwise the job pauses (`needs_review`).

Choices: **Approve once** (next execution), **Always for this script** (this
version), **Always for all approved scripts**, or **Deny**. Telegram's **Always...**
opens the scope selector; the broad all-scripts grant needs a confirmation.
**Add feedback** collects optional guidance before deciding. Email replies use
`APPROVE ONCE`, `APPROVE ALWAYS`, `APPROVE ALWAYS FOR ALL SCRIPTS`, or `DENY`
on the first line, followed by optional feedback. Both channels resolve the same
one-use request when delivery is configured as `both`.

Manage permissions in Telegram **/list → Permissions**, or
**./setup.sh tools → Script and website permissions**. Both use deterministic
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

Existing `sandbox_network_rules` retain their original job/global scope and
appear under **Legacy rules**; they are not silently converted to script grants.
The old network CLI remains available for legacy rules. Factory reset has a
separate **Sandbox script and network approvals and blocks** category.

For upgrades, save setup again to generate the revoke-only credential and
updated Compose secret grants, then rebuild/recreate the agent and sandbox
services. Runtime database migration preserves existing rules.

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

Compose reads these settings from `data/.env`:

- `SANDBOX_IMAGE` (used for both broker and run image)
- `SANDBOX_ISOLATION_MODE`, `SANDBOX_RUN_NETWORK_MODE`, `SANDBOX_EGRESS_MODE`, `SANDBOX_RUN_CAP_DROP`
- `SANDBOX_RUN_CPUS`, `SANDBOX_RUN_MEMORY_BYTES`, `SANDBOX_RUN_PIDS_LIMIT`
- `SANDBOX_SHARED_ROOT_HOST`, `SANDBOX_DOCS_ROOT_HOST` (generated by setup)

The broker also supports `SANDBOX_RUN_RUNTIME` (for example `runsc`),
`SANDBOX_RUN_USER`, `SANDBOX_RUN_NETWORK` and timeout overrides. The supplied
Compose file does not forward these variables: add them to the broker's
`environment` with a Compose override, or set them when running the broker
directly. The default Docker runtime is used unless overridden. Broker default
and maximum command timeouts are both 300 seconds; increasing the agent limit
alone does not increase the broker maximum.

## Operational Note

See [sandbox capabilities](SANDBOX_CAPABILITIES.md) for installed tools and
[security](security.md#sandbox-docker-socket) for Docker-daemon trust limits.
