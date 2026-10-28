# Extending Tools

This guide covers how to add a new tool to Assistant safely.

## Overview

Tool definitions and runtime dispatch live in `src/agent/src/assistant_agent/tools.py`.

For an ordinary runtime tool, update:

1. Runtime method implementation (`ToolRuntime`).
2. Dispatch mapping in `ToolRuntime.run`.
3. Function schema in `FUNCTION_TOOLS`.
4. Exposure sets, availability logic and catalog hints.

Some integration schemas are imported from their own modules (for example
`zoom_meetings.py` and `dropbox_files.py`). Follow the owning module's pattern.
Terminal/meta tools are handled separately by `TaskAgent.handle_tool_call`
in `task_agent.py`, not by the runtime dispatch mapping.

## Step-by-Step

### 1) Add runtime implementation

Create a method on `ToolRuntime`, for example:

```python
def foo_lookup(self, query: str) -> dict[str, Any]:
    ...
```

Raise `ToolError` for user-correctable failures.

### 2) Register dispatch mapping

Update `ToolRuntime.run(...)` mapping:

```python
"foo_lookup": self.foo_lookup,
```

The runtime checks availability before dispatch. An available tool missing from
this mapping raises `unknown tool`; an unexposed tool raises an availability error.

### 3) Add schema in `FUNCTION_TOOLS`

Define JSON schema entry with:

- unique `function.name`,
- precise description,
- explicit parameter types and useful property descriptions,
- `required` fields.

### 4) Add tool grouping set(s)

Add tool name to an appropriate set:

- existing set (e.g., `FILE_TOOL_NAMES`, `REMINDER_TOOL_NAMES`), or
- new set for a new capability domain.

Ensure `LOADABLE_TOOL_NAMES` includes it (unless it should be core-only).

### 5) Decide exposure behavior

In `available_function_names(...)`, add gating logic based on config/runtime prerequisites.

Examples of existing patterns:

- `smtp_configured(config)` for `email_send`
- `shared_root_status(config)["available"]` for file tools
- `search_configured(config)` for web/deep-research tools
- `calendar_configured(config)` for calendar tools

These helpers are defined in `validation.py`. Gating controls exposure and is
checked again by `ToolRuntime.run`; validate arguments, permissions and external
state in the implementation too. A schema is not a runtime authorization check.

### 6) Add guardrails text

Add the name to the relevant `TOOL_GUARDRAILS[group]["tools"]` set, or add a new
group with `tools` and `text`. `get_tool_specs` returns loaded/invalid names and
guardrails; the requested schemas become available on subsequent model calls.
Guardrail text guides the model; enforce safety requirements in code.

### 7) Update tool catalog descriptions

Add short entry in `tool_catalog(...)` descriptions map so the model sees meaningful catalog hints.

### 8) Add tests

Add/extend tests under `src/agent/tests/`:

- happy path,
- invalid arguments,
- permission/config gating,
- side-effect logging behavior if applicable.

For persisted objects, follow existing workstream provenance/versioning
patterns. Updates to versioned objects require `expected_version` from the
latest read; extend the runtime's `versioned_objects` mapping when appropriate.
Review `tool_result_cache.py` for output caching and redaction needs. Cached
results are model-context storage, not permission to replay or skip mutations.

## Core vs Loadable Guidance

Default to **loadable** tools (lazy schema loading via `get_tool_specs`) unless the tool is terminal/meta or must always be available.

Core tools increase default cognitive load and should remain minimal.

## Design Checklist

Before shipping a tool, verify:

- [ ] clear schema and error messages,
- [ ] deterministic side effects,
- [ ] idempotency strategy where needed,
- [ ] path/network/safety restrictions,
- [ ] test coverage,
- [ ] docs update in `docs/tools-reference.md`.
