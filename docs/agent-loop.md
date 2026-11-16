# Agent Loop and Job Lifecycle

This page summarizes task-agent job execution, state transitions, and pause/resume
behavior.

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

Workers claim due queued jobs in active workstreams with PostgreSQL row locking
(`FOR UPDATE SKIP LOCKED`), ordered by priority and then creation time. Task-agent
excludes research-worker jobs. Claiming records the worker lock and increments
`attempts`.

This prevents simultaneous claims, not duplicate external effects across retries.
See [concurrent job processing](concurrent-job-processing.md).

## Task-Agent Execution (`TaskAgent.process_job`)

For each claimed job, task-agent:

1. Initializes the model client and tool runtime.
2. Builds context from the thread, pending instructions, reminder, memory recall,
   research status, prior side effects, processed artifacts, and related
   workstream or system-task activity.
3. Builds the prompt and enters the bounded model/tool loop.

Each iteration:

- verifies the worker still owns the job;
- injects new requester context when flagged, deferring stale tool calls;
- refreshes context and clears in-memory history if the memory revision changes;
- calls the model, logs usage, executes tool calls and persists results;
- stops on a terminal condition.

## Tool Loading Model

- Agent starts with available **core tools** (`task_complete`, `task_failed`,
  `request_input`, and `email_send`) plus `get_tool_specs`; `email_send` requires
  SMTP configuration.
- Other tools are lazy-loaded by calling `get_tool_specs` with tool names from the runtime catalog.
- Guardrail text is returned with loaded tool groups.

## Completion and Terminal Rules

`task_complete` requires a non-empty `response`. For automatically routed jobs, including Telegram reply routes, it delivers the
response through `ReplyRouter` before completing; failed or uncertain delivery
blocks completion.
For legacy external-email jobs, a sent reply to the latest external sender must
already exist in `outbound_email_logs` (To or Cc).

Automatically routed completion also checks for new requester context before
delivery and before the final status update. New context keeps the job active;
already completed side effects must not be repeated.

`request_input` asks for missing information; it is never an approval.
Requester questions always use the job's reply route, even when the requester is
the admin, and set `metadata.waiting_for_input=true`. An admin-directed question
also uses the requester route when the requester is the admin; otherwise, admin
questions and undeliverable requester questions go to the admin by Telegram
(reply or tap **Reply**) and/or email (reply to the question or use
`/admin instruct JOB_ID`), and set `metadata.waiting_for_admin=true`. The job
moves to `waiting`; an answer resumes it as a supervisor instruction without
changing permissions or limits. If no admin channel can deliver the question,
it moves to `needs_review`.

Pauses preserve working memory: the runtime checkpoints the conversation and
loaded tools (`metadata.resume_checkpoint_id`, `resume_tools`). On resume, new
replies and admin instructions are appended. Guidance received during execution
is injected at the next step. Prior-action context records questions, guidance,
commands and script approval requests.

Tools enforce approval gates. Call the gated tool
(`development_session_request`, `script_approval_request`, or
`reminder_create(execution="script")`) directly. For ungated irreversible
actions or actions affecting others, ask first if the request did not clearly
authorize them.

`task_failed` marks the job `failed` and notifies the admin; it does not requeue it.
Unhandled processing exceptions requeue while attempts remain, otherwise move
the job to `needs_review`. Rate-limit exceptions apply the configured
`agent.limits.rate_limit_backoff_seconds` delay before retry.

Security screening can also pause a tool call for admin approval in
`needs_review`. Approval permits retrying the identical call, not changed arguments.

Scripts have a separate permission workflow even when screening is disabled:
`script_approval_request` registers the finished version, then
`network_access_request` requests any missing hosts for the identical invocation.
Missing grants pause the job in `needs_review` with `metadata.permission_request`.
An authenticated decision records the grant or denial, optional feedback, and
queues the job without increasing its budgets. A generic resume is not a grant.
Once grants are consumed before dispatch; registered script dispatches are not
automatically retried after an uncertain result. See
[script permissions](sandbox.md#script-versions-and-run-permissions).

## Async Requests

For an async request such as `deep_research_request`, task-agent:

- moves the job to `waiting` and logs the transition;
- sends a status update through the reply route, or email when available;
- consolidates memory; and
- resumes the parent job when the linked work completes.

## Budget and Context Controls

- Iteration cap: `agent.limits.max_iterations_per_task`.
- Token cap: `agent.limits.max_tokens_per_task`, including nested `llm_service` usage.
- Near the cap, task-agent adds `request_limit_increase`.
- Prompt size is bounded by a character budget and conversation compaction.

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
