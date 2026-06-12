# Extending Tools

This guide covers how to add a new tool to Assistant safely.

## Overview

Tool definitions and runtime dispatch live in `agent/src/assistant_agent/tools.py`.

You add a tool in three places:

1. Runtime method implementation (`ToolRuntime`).
2. Function schema in `FUNCTION_TOOLS`.
3. Exposure sets / availability logic.

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

Without this mapping, runtime will throw `unknown tool`.

### 3) Add schema in `FUNCTION_TOOLS`

Define JSON schema entry with:

- unique `function.name`,
- precise description,
- strict parameter types,
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

### 6) Add guardrails text

If the tool belongs to a sensitive domain, extend `TOOL_GUARDRAILS` with group text so `get_tool_specs` returns operational constraints with schemas.

### 7) Update tool catalog descriptions

Add short entry in `tool_catalog(...)` descriptions map so the model sees meaningful catalog hints.

### 8) Add tests

Add/extend tests under `agent/tests/`:

- happy path,
- invalid arguments,
- permission/config gating,
- side-effect logging behavior if applicable.

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
