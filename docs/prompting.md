# Prompting and context

The task agent composes its prompt from built-in operating rules,
runtime identity and time, operator-maintained personality and preferences in
`data/config/AGENT.md`, and only the feature guidance needed for the task.
The configured prompt filename is the `agent.prompt.agent_file`
setting (`AGENT_PROMPT_AGENT_FILE` in `data/overrides.env`).
Setup can initialize the local prompt from `src/config/AGENT.md.example`.

The filename resolves relative to `ASSISTANT_CONFIG_DIR` (`/app/config`, the
read-only mount of `data/config/`). If it is absent, the loader tries the
same filename with `.example` appended. Startup rejects a missing or empty
prompt. Task-agent reads at most `agent.prompt.max_context_file_bytes` bytes
(65536 by default).

The runtime context includes configured agent/admin identity, organization
information when present, current time, timezone, and help-tool guidance.
Model preset and speech policy appear with their respective skills.
Documentation is retrieved through `help_search` and `help_topic`,
not a shared-workspace documentation directory.

## Contextual skills

| Workflow | Locked skills and activation |
|---|---|
| Communication and identity | `EMAIL`, `TELEGRAM`, and `CONTACTS`; email and Telegram reply routes also activate their own guidance. |
| Scheduling and meetings | `CALENDAR`, `ZOOM`, and `REMINDER.SCHEDULE` for their tool families. Notification-only reminders still do not invoke a model. |
| Media and models | `MODEL.PRESETS` and `TTS`, each with its own live configuration detail. |
| Workspace and automation | `FILES`, `DROPBOX`, `WORKSTREAMS`, and `SCRIPTS`; approved scheduled script runs activate `SCRIPTS` without asking again for approval. |
| Recall and research | `MEMORY`, `CONTEXT`, `WEB.SEARCH`, and `DEEP.RESEARCH`. The dedicated research worker shares applicable locked guidance but keeps its narrower tool set. |

`skill_list` and `skill_search` return candidate descriptions; `skill_load`
activates a body. Preferences aren't separate candidates: they load
automatically with their parent skill on authenticated administrator requests.
The configured embedding service indexes titles, descriptions,
and example intents, not instruction bodies. The query uses a bounded excerpt
of the latest request or scheduled task, not earlier messages or a quoted
email thread. Only configure an embedding service you trust with that data.
Proactive suggestions require a stronger semantic match than explicit skill
search. Keyword fallback accepts an exact skill name or multiple terms from
the same description or example, rather than a single incidental word.
Recommendations never authorize a tool or external action.

`get_tool_specs` activates bound guidance for the following model turn. A
tool call made without its required guidance is not executed. Dispatching a
tool keeps bound guidance active across related model turns, including
failed or uncertain attempts. After two model turns without using its tools,
the guidance is replaced by a recall reference. Event-bound guidance remains
active, and manually loaded custom guidance without bound tools does not expire.
The prompt budget and current skill revision still determine whether guidance
is visible; retention never bypasses the skill gate.

When a skill gate defers a call, remaining calls in that batch also receive
`not_executed` replies and individual activity-log entries. These are
recoverable guidance deferrals, not network or provider errors. Checkpoints
carry revision references and idle-turn ages instead of copying skill bodies.
Existing approval, retry, and delivery checks still apply in code.

Use **Settings > Assistant > Skills** in the macOS app or TUI to inspect skills.
Open a locked skill to add, edit, or remove its single **Preferences** entry.
Write both what the Assistant should do and when in the instructions, followed
by any content it should use. For example, under Email: “Use this signature for
outbound emails:” followed by your signature. You don't need an ID, title, or
description for preferences. Existing preferences are preserved. Standalone
custom skills still use a title and description for discovery and load only for
authenticated administrator requests. Direct Assistant publishing is off by default
(`AGENT_SKILLS_ALLOW_ASSISTANT_WRITES`); enabling it never authorizes deletion.
Deleting an editable revision removes it from the active catalog, not the
audit history. Do not put credentials in any skill or overlay.

Upgrades leave existing `AGENT.md` files unchanged. Compare yours with the
new example and remove duplicated operational prose only after review.
`AGENT_SKILLS_ENABLED=false` disables contextual loading for rollback.

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
