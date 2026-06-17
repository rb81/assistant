# Sandbox Command Execution

This document describes how Assistant executes shell commands safely.

## Components

- `assistant_agent.tools:command_execute` — agent-facing tool.
- `sandbox/server.py` — broker API service.
- Per-command run containers — short-lived execution environments.

## Control Flow

1. Agent calls `command_execute(command, timeout_seconds, workdir)`.
2. `ToolRuntime.command_execute` validates input and shared-root workdir.
3. Request is sent to sandbox broker `POST /execute`.
4. Broker starts a fresh run container for the request.
5. Broker captures stdout/stderr/exit status.
6. Broker removes run container (and per-run network).
7. Result is returned to task-agent and logged.

## Safety Properties

- Commands do **not** run in task-agent container.
- Paths are constrained under shared root.
- Broker can enforce runtime limits (CPU, memory, PID, timeout).
- Broker container is hardened (`read_only`, dropped caps, `no-new-privileges`).
- Run containers mount shared workspace only (no app internals).

## Network Isolation

- Broker is attached to internal sandbox control network.
- Run containers are attached to per-run bridge networks.
- Default design isolates run containers from application service networks.

## Isolation Modes

### `SANDBOX_ISOLATION_MODE=container` (default, production)

This is the default and intended production mode. Each command runs in a fresh, short-lived Docker container that is fully isolated from the host and the agent. Resource limits (CPU, memory, PIDs), capability drops, network isolation, and per-run bridge networks are all enforced by Docker. The Docker socket must be mounted into the sandbox broker container and `SANDBOX_RUN_IMAGE` must be set.

`container` is the default in both `docker-compose.yml` and `sandbox/server.py`. You do not need to set anything to use it.

### `SANDBOX_ISOLATION_MODE=process` (development only)

⚠️ **Process mode is unsuitable for production.** It is provided as a fallback for environments where Docker is unavailable (e.g., bare-metal local development).

To enable it, set in `.env`:

```env
SANDBOX_ISOLATION_MODE=process
```

In process mode, commands are executed directly via `subprocess.run` on the host where the sandbox broker is running. The only restriction enforced is that the working directory must stay under the configured shared root. There are **no container-level protections**:

- No memory, CPU, or PID limits
- No capability drops or seccomp profiles
- No filesystem isolation beyond the workdir check
- No network isolation — the process inherits the broker's network context
- Any command the broker process is permitted to run can be executed

Do not use process mode if the agent can receive instructions from untrusted input, or on any server where the consequences of arbitrary command execution are unacceptable.

---

## Retry and Failure Semantics

`command_execute` retries broker-request failures according to:

- `agent.sandbox.max_attempts`
- `agent.sandbox.retry_backoff_seconds`

It does **not** retry normal non-zero command exits automatically.

Failure classes include:

- transient broker/network/timeout errors,
- HTTP non-retriable errors,
- host runtime misconfiguration (e.g., invalid runtime like missing `runsc`).

Retry exhaustion raises `SandboxAttemptsExhausted` and is surfaced with attempt error details.

## Configuration Highlights

From app config (`config/agent.yaml`):

- `agent.sandbox.enabled`
- `agent.sandbox.base_url`
- `agent.sandbox.default_timeout_seconds`
- `agent.sandbox.max_attempts`
- `agent.sandbox.retry_backoff_seconds`

From Compose env/runtime defaults:

- run image,
- isolation mode,
- resource caps,
- runtime selection (`runsc` optional override).

## Operational Note

Sandbox provides process isolation, but production should additionally enforce host-level egress policy (private-range blocking / metadata endpoint blocking) for run containers.
