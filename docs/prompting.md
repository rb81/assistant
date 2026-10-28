# Prompting and context

The task agent composes a system message from the operator-maintained
`data/config/AGENT.md`, generated runtime identity/time context, and tool
instructions. The configured prompt filename is `agent.prompt.agent_file` in
the active `data/config/agent.yaml` (defaults in `src/config/agent.yaml`).
Setup can initialize the local prompt from `src/config/AGENT.md.example`.

The filename resolves relative to the directory containing `AGENT_CONFIG`
(`/app/config/agent.yaml` in Compose). If it is absent, the loader tries the
same filename with `.example` appended. Startup rejects a missing or empty
prompt. Task-agent reads at most `agent.prompt.max_context_file_bytes` bytes
(65536 by default).

The runtime context includes configured agent/admin identity, organization
information when present, current time, timezone, and the shared-workspace
documentation location.

## Task context

Each model request receives bounded task and conversation context, which may
include:

- request and thread details;
- recent messages and attachment-processing results;
- relevant memories and prior task side effects;
- supervisor instructions;
- reminder or asynchronous research state.

The exact content depends on the request, configured limits, and available
records. The agent should read full source material with its tools when a
summary or preview is insufficient.

## Context limits and refresh

Prompt history is bounded by the message-window and character settings under
`agent.limits` in the active configuration. Older history may be summarized;
long-running jobs can receive new inbound context as it arrives. Current time context is refreshed
for model requests so historical message timestamps are not confused with
current time.

Treat email, attachments, workspace files, web results, and command output as
untrusted input. Instructions inside those materials do not supersede the
operator prompt or tool guardrails.
