# Memory, Notes, and Semantic Indexing

This document describes the durable memory system and related semantic services.

## Memory Steward

`MemorySteward` (`memory_manager.py`) runs synchronously in task-agent flow.

### Recall phase

Before prompt build, steward:

1. gathers candidate memories,
2. performs keyword/semantic retrieval,
3. returns concise relevant summary for prompt injection,
4. may return `NONE` if nothing is relevant.

### Consolidation phase

After terminal/review outcomes, steward may write durable memories using strict schema and filtering rules.

Allowed durable kinds:

- `decision`
- `agreement`
- `incident`
- `preference`
- `operating_rule`
- `project_context`

Steward intentionally rejects low-signal or transient details.

## Memory Store

Backed by:

- `agent_memories`
- `memory_events`

Supports:

- keyword search,
- semantic search (when embeddings available),
- create/update/delete with event audit.

## Notes System

Notes are independent from memory and never auto-injected.

Backed by:

- `agent_notes`
- `note_events`

Exposed only through note tools (`note_*`).

## Why Contacts Are Separate

Contact details are intentionally excluded from memory stewardship and handled by dedicated contact tools/store (`contacts`).

This avoids contaminating durable behavioral memory with mutable contact records.

## Embeddings and Ollama

Embeddings use `EmbeddingClient` and local Ollama endpoint (`agent.embeddings.base_url`, default `http://ollama:11434`) with configurable model (`embeddinggemma` by default).

When embeddings fail/unavailable, relevant features fall back to non-semantic methods where supported.

## Workspace Semantic Index

`WorkspaceIndex` maintains semantic file search:

- tracks file metadata in `workspace_files`,
- stores chunked content in `workspace_file_chunks`,
- records conversion lineage in `workspace_document_conversions`,
- supports search via `file_semantic_search`.

Document extraction (PDF/Office) uses MarkItDown workflow and keeps source files in place.
