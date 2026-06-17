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

Workers claim queued jobs with PostgreSQL row locking (`FOR UPDATE SKIP LOCKED`), which allows multiple task-agent workers without duplicate processing.

## Task-Agent Execution (`TaskAgent.process_job`)

For each claimed job:

1. Initialize LLM client and tool runtime.
2. Build base context from:
   - latest thread emails/messages,
   - pending supervisor instructions,
   - linked reminder (if any),
   - memory recall summary,
   - async project/research status,
   - prior side-effect summary,
   - processed artifact manifests.
3. Build prompt messages (`system` + `user` context block).
4. Enter bounded iteration loop.

On each iteration:

- optionally inject new inbound email context if `has_new_context` is set,
- prepare active tool set,
- call model,
- log request/response and token usage,
- execute returned tool calls,
- persist tool results,
- stop on terminal conditions.

## Tool Loading Model

- Agent starts with **core tools** (`task_complete`, `task_failed`, `request_input`, and `email_send`) plus `get_tool_specs`.
- Other tools are lazy-loaded by calling `get_tool_specs` with tool names from the runtime catalog.
- Guardrail text is returned with loaded tool groups.

## Completion and Terminal Rules

`task_complete` is blocked unless:

- `response` is non-empty, and
- for external requester jobs, a sent `email_send` exists to the latest external sender.

`request_input` sends clarification to requester/admin and moves job to `needs_review`.

`task_failed` transitions job to failed/retry logic.

## Async Requests

If tool call is async-request type (`project_create`, `deep_research_request`):

- job is moved to `waiting`,
- status-change event is logged,
- optional status email is sent to original sender,
- memory consolidation runs,
- parent job resumes later when linked async work completes.

## Budget and Context Controls

- Iteration cap: `agent.limits.max_iterations_per_task`
- Token cap: `agent.limits.max_tokens_per_task`
- Near-limit mode adds `request_limit_increase` tool.
- Prompt-size safety uses character budget and conversation compaction.

If token/iteration limits are exceeded:

- checkpoint is created in `agent_checkpoints`,
- job moves to `needs_review`,
- admin is notified,
- Memory Steward consolidation runs.

## Checkpoint and Review Resume

When admin applies review override in job metadata (`admin_review_override`), task-agent resumes from latest checkpointed message history and appends an admin resume note with override budget and instruction.

## Supervisor Behavior

`Supervisor` continuously reviews active jobs and flags:

- stalled running jobs,
- over-duration running jobs,
- repeated unresolved tool failures,
- failure-like `needs_review` states.

Flagged jobs are moved to `needs_review` and admin notification is sent.

## Heartbeat Behavior

`Heartbeat` is a rule-based monitor (no LLM calls) that:

- flags stale running jobs,
- notifies failed/review jobs (excluding expected wait states),
- flags stalled deep-research runs,
- flags stale projects,
- runs periodic memory maintenance (reaping stale memories/notes),
- records health checks in `manual_events`.

Memory maintenance runs on a configurable interval (default: 24h) and reaps low-importance stale memories and archives old active notes.
