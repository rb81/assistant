# Visual Workspace UI

The browser UI now has two surfaces:

- `/admin`: technical control room for jobs, memories, contacts, logs, and queue actions.
- `/workspace`: shared office workspace for files, Markdown editing, and agent chat.

Both routes share static assets under `agent/src/assistant_agent/ui/assets`.

## Editor

The workspace editor uses Milkdown Kit through the local frontend build in `agent/frontend`.

- Markdown remains the canonical stored format.
- The default Markdown editing mode is Milkdown WYSIWYG.
- The Source toggle exposes raw Markdown when needed.
- Preview renders the current Markdown without changing the file.
- The save path uses `/api/workspace/file` with `expected_mtime_ns` conflict detection.
- New unsaved files can be started from the file explorer and saved into a selected workspace folder.
- Save As uses create-only writes so it does not overwrite an existing file by accident.
- Uploaded, copied, moved, or extracted PDF and Office documents stay in place. The background workspace indexer extracts text with MarkItDown for search, and users can explicitly convert files to Markdown, HTML, PDF, or DOCX from the file context menu.
- The active folder and selected file are persisted in browser state and reflected in the workspace URL.
- Unsaved editor changes are snapshotted every 30 seconds through `/api/workspace/drafts` into `.cache/docs/` under the shared workspace root; drafts are restored as unsaved editor state when newer than the source file.

## File Explorer

The workspace sidebar supports common file-manager actions while staying under the configured shared workspace root.

- Toolbar actions create new files and folders in the active folder.
- Right-click context menus support opening, renaming, moving, duplicating, copying paths, and moving files or folders to trash.
- Move and duplicate use `/api/workspace/path` and `/api/workspace/copy`, backed by the same runtime safety checks as agent file tools.
- Folder pickers use recursive `/api/workspace/tree` listings.

Do not load Milkdown from a CDN. Docker builds the browser bundle in the `ui-build` stage and copies the compiled assets into the Python image. Local frontend source lives under `agent/frontend`; generated workspace bundles are ignored by Git.

## Context Sharing

Workspace chat submits normal durable jobs through `POST /api/jobs`. The chat body includes active-file context when selected:

- active file path
- active file content when small enough
- an instruction to use file tools when the file is large

This keeps agent work auditable in the existing job system while giving the workspace a conversational interface.
