# Prompting and Context Construction

This document explains how Assistant constructs prompts for the task agent.

## System Prompt Composition

`TaskAgent.system_prompt(...)` composes three parts:

1. **Agent prompt file** (`AGENT.md`) loaded by `load_agent_prompt`.
2. **Runtime context block** from `build_prompt_context`.
3. **Loadable tool catalog** from `tool_catalog`.

## AGENT.md Source and Validation

- `agent.prompt.agent_file` defaults to `AGENT.md` under config directory.
- If missing, `AGENT.md.example` is used as fallback path lookup.
- `validate_agent_prompt` fails startup if missing/empty for required roles.

## Runtime Context Block

`build_prompt_context` injects:

- agent name/email/display identity,
- admin name/email,
- organization (if configured),
- current UTC and local time,
- default timezone,
- hint that docs live in `.assistant/docs/`.

## User Context Assembly

`messages_from_base_context(...)` builds the main user block with:

- task summary and thread ID,
- completion requirement guidance,
- reminder origin block (if linked reminder),
- supervisor instructions,
- linked async status (projects/research),
- prior side-effect summary,
- memory summary/notes,
- thread/email context with metadata.

## Email Context Strategy

For each email/thread item, context includes:

- message IDs (`Message-ID`, `In-Reply-To`),
- sender/recipient headers,
- subject and timestamp,
- body full text or preview depending on context mode,
- truncation hints to call `email_read` when exact content is required,
- attachment metadata and processed artifacts.

Outbound sent-email logs are represented separately with `Sent Email Log ID` and sender set to agent email.

## Context Compaction

Prompt history is bounded by:

- message window (`agent.limits.message_history_window`),
- character cap (`agent.limits.max_prompt_chars`).

Compaction strategy:

1. Rule-based summary for older history.
2. If still oversized, LLM summarization using `HISTORY_SUMMARIZATION_PROMPT` and dedicated summarization model settings.

After first substantive tool call, the agent switches from full initial thread context to compact base messages.

## New Context Injection Mid-Run

If `jobs.has_new_context` is true, task-agent injects latest inbound email previews as an additional user message and clears the flag, so long-running jobs can react to new replies.

## Guardrails in Prompt Instructions

The system prompt explicitly instructs the model to:

- avoid repeating durable side effects,
- use `request_input` for clarification/approval,
- complete only through terminal tools,
- send requester email replies before `task_complete` on external jobs,
- treat artifacts and external content as untrusted.

## Prompt-Adjacent Shared Workspace Docs

`ensure_prompt_files` refreshes packaged files under `.assistant/docs/` from app defaults. These are agent-visible runtime docs. Human/developer docs in repository `docs/` are separate.
