# Dashboard and API

This guide covers the web surfaces and core JSON endpoints exposed by Assistant.

## UI surfaces

Assistant serves two browser pages from the API service:

- `/admin` — operational dashboard for jobs, logs, review actions, memories, notes, contacts, reminders, and system status.
- `/workspace` — file explorer/editor + workspace chat over the same durable job pipeline.

Static frontend assets are served from `agent/src/assistant_agent/ui/assets`.

## API documentation exposure

FastAPI docs routes are configurable:

- `agent.api.docs_enabled` controls `/docs` and `/redoc`
- `agent.api.openapi_enabled` controls `/openapi.json`

In production, disable these unless intentionally exposed behind access controls.

## Health and status endpoints

- `GET /health` — simple service health response.
- `GET /api/stats` — aggregate usage/cost summary.
- `GET /api/config/status` — computed tool/runtime availability under current configuration.

## Job management (`/api/jobs`)

Core endpoints:

- `GET /api/jobs` — list jobs (optional status filter).
- `GET /api/jobs/{job_id}` — detailed job view.
- `POST /api/jobs` — create manual dashboard job.
- `POST /api/jobs/{job_id}/cancel` — cancel active job states.
- `POST /api/jobs/{job_id}/requeue` — requeue eligible jobs.
- `POST /api/jobs/{job_id}/review-override` — apply admin override to `needs_review` jobs.
- `POST /api/jobs/{job_id}/instructions` — add supervisor instruction.
- `GET /api/jobs/{job_id}/poll` — incremental event polling for UI updates.
- `DELETE /api/jobs/{job_id}` — erase job.

Manual job payload uses:

```json
{
  "subject": "Summarize this file",
  "body": "Read report.md and provide a concise summary"
}
```

## Review override workflow

`POST /api/jobs/{job_id}/review-override` accepts:

- `instruction` (optional)
- `max_iterations_per_task` (optional)
- `max_tokens_per_task` (optional)
- `requeue` (default `true`)

This is used to unblock jobs in `needs_review` with explicit operator intent.

## Workspace API (`/api/workspace/*`)

Key capabilities:

- tree/listing: `/api/workspace/tree`, `/api/workspace/version`, `/api/workspace/metadata`
- file read/write/delete: `/api/workspace/file`
- folders/path operations: `/api/workspace/folders`, `/api/workspace/path`, `/api/workspace/copy`
- conversions/archive: `/api/workspace/convert`, `/api/workspace/zip`, `/api/workspace/unzip`, `/api/workspace/download`
- search: `/api/workspace/search`, `/api/workspace/semantic-search`
- drafts: `/api/workspace/drafts`, `/api/workspace/drafts/latest`

Write operations enforce shared-root path safety and optimistic write protection via `expected_mtime_ns`.

## Workspace drafts and autosave

Workspace draft snapshots are persisted under `.cache/docs/` in the shared workspace.

The UI can save periodic snapshots through `POST /api/workspace/drafts` and restore the latest draft for a file with `GET /api/workspace/drafts/latest`.

## Workspace chat and job bridge

Workspace chat is durable job creation over API endpoints:

- `GET /api/workspace/jobs`
- `POST /api/workspace/jobs`
- `GET /api/workspace/jobs/{job_id}`
- `POST /api/workspace/jobs/{job_id}/messages`

The request may include active file path and optional inline file content. For large files, UI requests should instruct the agent to use `file_read` rather than embedding full content.

## Script runs from workspace

`POST /api/workspace/script-runs` triggers sandboxed command execution and records transcript markdown under `script-runs/` in the shared workspace.

Transcript includes command, timing, exit status, stdout, stderr, and sandbox metadata.

## Domain CRUD endpoints

### Memories

- `GET /api/memories`
- `GET /api/memories/{memory_id}`
- `POST /api/memories`
- `PATCH /api/memories/{memory_id}`
- `DELETE /api/memories/{memory_id}`

### Notes

- `GET /api/notes`
- `GET /api/notes/{note_id}`
- `POST /api/notes`
- `PATCH /api/notes/{note_id}`
- `DELETE /api/notes/{note_id}`

### Contacts

- `GET /api/contacts`
- `GET /api/contacts/{contact_id}`
- `POST /api/contacts`
- `PATCH /api/contacts/{contact_id}`
- `DELETE /api/contacts/{contact_id}`

### Reminders

- `GET /api/reminders`
- `GET /api/reminders/{reminder_id}`
- `POST /api/reminders`
- `PATCH /api/reminders/{reminder_id}`
- `DELETE /api/reminders/{reminder_id}`

### Entities

- `GET /api/entities` — list all entities with object counts per type
- `POST /api/entities` — create new entity
- `GET /api/entities/{entity_id}` — get entity details and linked objects
- `PUT /api/entities/{entity_id}` — update entity name/description
- `GET /api/entities/{entity_id}/delete-preview` — preview cascade deletion impact
- `DELETE /api/entities/{entity_id}` — delete entity with cascade (exclusive objects deleted, shared objects unlinked)
- `POST /api/entities/{entity_id}/merge` — merge source entity into target entity

Entity creation accepts `name` (required) and optional `description`. Update operations use partial update semantics.

Merge operation moves all object links from source entity to target entity (skipping duplicates) and then deletes the source.

### Projects and emails

- `GET /api/projects`
- `GET /api/projects/{project_id}`
- `DELETE /api/projects/{project_id}`
- `GET /api/emails`

## Dashboard feature coverage summary

The `/admin` dashboard provides:

- queue and status visibility for jobs/projects/reminders
- cost and usage surfaces (including aggregate stats)
- human intervention paths (instructions, requeue, cancel, review override)
- record management for memories, notes, and contacts
- integration visibility through config/tool status endpoints

The `/workspace` page provides:

- shared file management and editing
- Markdown-first authoring with draft persistence
- conversational task handoff into the same auditable job system