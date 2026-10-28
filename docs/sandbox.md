# Sandbox Command Execution

This document describes how Assistant executes shell commands safely.

## Components

- `ToolRuntime.command_execute` in `src/agent/src/assistant_agent/tools.py` —
  agent-facing tool.
- `src/sandbox/server.py` — broker API service.
- Per-command run containers — short-lived execution environments.

## Control Flow

1. Agent calls `command_execute` with an argv array and optional
   `timeout_seconds` and `workdir`.
2. `ToolRuntime.command_execute` validates input and shared-root workdir.
3. Request is sent to sandbox broker `POST /execute`.
4. Broker starts a fresh run container for the request.
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
- Allowed hosts are `agent.sandbox.network.allowed_hosts` (package registries
  by default) plus admin rules in `sandbox_network_rules`. Any block beats any
  allow. Rules are per host, never per URL path.
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

1. A command fails to reach a host; the proxy refuses it with HTTP 403 and
   `X-Sandbox-Egress: not_approved`, and the tool result explains this.
2. The agent calls `network_access_request` with the URLs and purpose. Already
   allowed hosts return immediately; admin-blocked hosts are reported and not
   requested again. Otherwise the job pauses (`needs_review`).
3. The admin gets a Telegram message and/or email:
   **Approve once** (this job), **Approve always** (all jobs), **Block once**,
   **Block always**, or **Cancel job**. On Telegram, "Add feedback first"
   attaches guidance; by email, reply with the decision on the first line and
   optional feedback below.
4. The job resumes with the decision and feedback as an administrator
   instruction.

Manage rules directly:

```bash
sudo docker compose --env-file data/.env exec task-agent python -m assistant_agent.network_access list
sudo docker compose --env-file data/.env exec task-agent python -m assistant_agent.network_access allow api.example.com
sudo docker compose --env-file data/.env exec task-agent python -m assistant_agent.network_access block '*.tracker.example' --note "no trackers"
sudo docker compose --env-file data/.env exec task-agent python -m assistant_agent.network_access remove api.example.com
```

Use `--job N` to scope `allow`, `block` and `remove` to one job. Setup factory
reset has a separate **Sandbox network approvals and blocks** category.

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

`command_execute` retries broker-request failures according to:

- `agent.sandbox.max_attempts`
- `agent.sandbox.retry_backoff_seconds`

It does **not** retry returned command results, including non-zero exits and
command timeouts. HTTP errors below 500 fail immediately; server errors and
transport failures are retried unless recognized as a host runtime
misconfiguration. A lost response can cause a command that already ran to be
executed again: retries do not guarantee exactly-once side effects.

Retry exhaustion raises `SandboxAttemptsExhausted` with attempt error details.
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
