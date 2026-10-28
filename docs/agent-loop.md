# Agent Loop and Job Lifecycle

This document explains how jobs move through the system and how the task agent executes work.

## Job Statuses

`jobs.status` can be:

- `queued`
- `running`
- `waiting`
- `completed`
- `failed`
- `needs_review`
- `cancelled`

## Claiming and Concurrency

Workers atomically claim due queued jobs in active workstreams with PostgreSQL
row locking (`FOR UPDATE SKIP LOCKED`), ordered by priority descending, then
creation time ascending. Task-agent excludes jobs assigned to the research
worker. The claim records the worker lock and increments `attempts`.

This prevents simultaneous claims, not duplicate external effects across retries.
See [concurrent job processing](concurrent-job-processing.md).

## Task-Agent Execution (`TaskAgent.process_job`)

For each claimed job:

1. Initialize LLM client and tool runtime.
2. Build base context from:
   - latest thread emails/messages,
   - pending supervisor instructions,
   - linked reminder (if any),
   - memory recall summary,
   - async research status,
   - prior side-effect summary,
   - processed artifact manifests,
   - main-conversation context for system tasks, recent system-task outcomes for
     the main conversation, and related workstream change events.
3. Build prompt messages (`system` + `user` context block).
4. Enter bounded iteration loop.

On each iteration:

- verify the worker still owns the running job,
- inject new request context if `has_new_context` is set, deferring stale tool calls,
- rebuild recall/context and clear in-memory history if `memory_revision` changes,
- prepare active tool set,
- call model,
- log request/response and token usage,
- execute returned tool calls,
- persist tool results,
- stop on terminal conditions.

## Tool Loading Model

- Agent starts with available **core tools** (`task_complete`, `task_failed`,
  `request_input`, and `email_send`) plus `get_tool_specs`; `email_send` requires
  SMTP configuration.
- Other tools are lazy-loaded by calling `get_tool_specs` with tool names from the runtime catalog.
- Guardrail text is returned with loaded tool groups.

## Completion and Terminal Rules

`task_complete` requires a non-empty `response`. For jobs with automatic reply
routing (and Telegram reply routes), it delivers that response through
`ReplyRouter` before completing; failed or uncertain delivery blocks completion.
For legacy external-email jobs, a sent reply to the latest external sender must
already exist in `outbound_email_logs` (To or Cc).

Automatically routed completion also checks for new requester context before
delivery and before the final status update. New context keeps the job active;
already completed side effects must not be repeated.

`request_input` sends clarification to the requester or admin. Requester input
on an automatically routed job moves it to `waiting` with
`metadata.waiting_for_input=true`; other input requests move it to `needs_review`.
Requester questions use the job's reply route when available.

`task_failed` marks the job `failed` and notifies the admin; it does not requeue it.
Unhandled processing exceptions requeue while attempts remain, otherwise move
the job to `needs_review`. Rate-limit exceptions apply the configured
`agent.limits.rate_limit_backoff_seconds` delay before retry.

Security screening can also pause a tool call for admin approval in
`needs_review`. Approval permits retrying the identical call, not changed arguments.

## Async Requests

If tool call is async-request type (`deep_research_request`):

- job is moved to `waiting`,
- status-change event is logged,
- a status update is sent through the reply route, or by email when available,
- memory consolidation runs,
- parent job resumes later when linked async work completes.

## Budget and Context Controls

- Iteration cap: `agent.limits.max_iterations_per_task`
- Token cap: `agent.limits.max_tokens_per_task`, including nested `llm_service` usage
- Near-limit mode adds `request_limit_increase` tool.
- Prompt-size safety uses character budget and conversation compaction.

If token/iteration limits are exceeded:

- checkpoint is created in `agent_checkpoints`,
- job moves to `needs_review`,
- admin is notified,
- Memory Steward consolidation runs.

## Checkpoint and Review Resume

When an admin requeues a job with `metadata.admin_review_override`, task-agent
loads the latest valid checkpointed message history, if one exists, and appends
an admin resume note with the override budget and instruction. Without a valid
checkpoint, it builds fresh context.

## Supervisor Behavior

`Supervisor` is rule-based. It reviews running and review jobs, skips `waiting`
jobs and research-worker jobs, and detects:

- stalled running jobs,
- over-duration running jobs,
- repeated unresolved tool failures,
- failure-like `needs_review` states.

Flagged running jobs move to `needs_review` and release their locks. Existing
failure-like review states receive deduplicated admin notifications; expected
input waits are excluded.

## Heartbeat Behavior

`Heartbeat` is a rule-based monitor (no LLM calls) that:

- flags stale running jobs,
- notifies failed/review jobs (excluding expected wait states),
- flags stalled deep-research runs,
- runs periodic memory maintenance,
- records health checks in `manual_events`.

Memory maintenance reaps eligible low-importance stale memories on its configured
interval.
