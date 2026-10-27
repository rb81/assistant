# Memory System and Semantic Indexing

Assistant keeps durable and short-lived working context in one memory store.

## Unified memories

Memories live in `agent_memories`, with an audit trail in `memory_events`. There
are two kinds of content:

- **Durable facts**: decisions, agreements, incidents, preferences, operating
  rules and project context. They carry evidence and provenance.
- **Working context** (`working_context`): short-lived context such as meeting
  details or follow-ups. It expires after `agent.memory.working_context_days`
  (default 14 days). Pinned memories never expire.

Similar facts become reviewable **suggestions** rather than silently overwriting
an existing memory. Administrators review, correct, pin, expire and delete
memories with email `/admin memory` commands, including controlled erasure of
logged copies. Sources
may be learned again afterwards. See [Email Administration](email-administration.md)
for the commands and [security](security.md) for deletion limits.

Memory supports keyword search and, when embeddings are available, semantic
search. Contact details live in the dedicated `contacts` store and contact tools,
so mutable records do not contaminate durable behavior.

## Memory Steward

`MemorySteward` (`memory_manager.py`) runs synchronously in the task-agent flow.

**Recall.** Before the prompt is built it selects relevant memories (up to
`max_injected_memories`, default 8) using keyword and semantic search.

**Consolidation.** After a job reaches a terminal or review outcome, the steward
asks the configured model to propose durable memories, subject to
`min_importance`, `min_confidence` and `max_writes_per_job`. Inferred memories
must include evidence and why they are relevant to the future; transient or
low-signal details are rejected. Failures are best-effort and never fail the job.

Configuration (`agent.memory.steward.*`): `enabled`, `model`, `timeout_seconds`,
`max_tokens_per_call`, `max_injected_memories`, `max_writes_per_job`,
`min_importance`, `min_confidence`, `conflict_detection_enabled` and
`conflict_similarity_threshold` (default 0.85). The wizard's Advanced Settings
cover automatic stewardship and the injected-memory limit.

### Lifecycle

Reaping runs periodically (default every 24 hours) and expires stale,
low-importance memories (importance at most 2, not accessed in 90 days). Pinned
memories are never reaped. Reaping sets `expires_at` rather than deleting, and is
logged to `memory_events`. A small recency boost (up to +0.1 within 365 days)
keeps old memories from dominating semantic results without overriding strong
matches.

Settings: `agent.memory.steward.reap_after_days`, `reap_max_importance`,
`reap_interval_hours`, `agent.memory.embeddings.min_similarity`,
`recency_boost_max` and `recency_boost_days`.

## Context Search Window

Context search (used by the `context_search` tool and steward recall) defaults to
a 90-day window (`agent.context.search_days`). The `context_search` tool accepts
`recent_only=true` to limit to 7 days. Longer windows give more coverage; shorter
ones improve performance.

## Embeddings and Ollama

Embeddings use `EmbeddingClient` and local Ollama endpoint (`agent.embeddings.base_url`, default `http://ollama:11434`) with configurable model (`embeddinggemma` by default).

When embeddings fail/unavailable, relevant features fall back to non-semantic methods where supported.

Use `./setup.sh --section embeddings` to configure the model, endpoint,
timeout, and output dimensions. Ollama's `embeddinggemma` is the 300M text model,
not EmbeddingGemma 2. It needs Ollama 0.11.10+, supports 2,048 input tokens and
768-dimensional output by default, with 512/256/128 reduced dimensions available.
Long inputs may be truncated by Ollama; existing source text limits and workspace
chunking remain unchanged. Current encoding preserves the existing raw-text
convention rather than silently introducing query/document prefixes. Google's
[model card](https://huggingface.co/google/embeddinggemma-300m) recommends those
prefixes for optimized retrieval; adopting them would require rebuilding even
same-model vectors with a consistently versioned encoding profile.

Changing model or dimensions requires rebuilding existing vectors. Search never
compares different models, incompatible dimensions, or unknown model metadata.
Keyword fallback remains available, but semantic recall can be incomplete until
the rebuild finishes. No source records are deleted to change embedding models.

After deploying the updated agent image, inspect or rebuild from the repository:

```sh
docker compose --env-file data/.env build task-agent
docker compose --env-file data/.env run --rm --no-deps --entrypoint python task-agent -m assistant_agent.embedding_maintenance status
docker compose --env-file data/.env run --rm --no-deps --entrypoint python task-agent -m assistant_agent.embedding_maintenance probe
docker compose --env-file data/.env run --rm --no-deps --entrypoint python task-agent -m assistant_agent.embedding_maintenance reindex --batch-size 50
```

`status` lists stale counts for all seven searchable sources. `probe` verifies
the configured endpoint and vector dimensions. `reindex` replaces only missing
or incompatible vectors, preserving source data, IDs and content timestamps;
expired memories and deleted/superseded workspace files are excluded. PostgreSQL
receives the embedding-only timestamp safeguard before rebuilding; this command
does not run unrelated pending schema changes. PostgreSQL
must be running for status/reindex; Ollama must be running and the model available
for probe/reindex. Failed runs retain completed replacements: rerun the same
command to resume. Concurrent source edits/deletions are not overwritten, and an
unfinished rebuild exits nonzero. Recreate all enabled embedding consumers after
saving settings, including `task-agent` when enabled, rather than merely restarting
containers with their old environment. The rebuild does not enable workers itself.

## Workspace Semantic Index

`WorkspaceIndex` maintains semantic file search:

- tracks file metadata in `workspace_files`,
- stores chunked content in `workspace_file_chunks`,
- records conversion lineage in `workspace_document_conversions`,
- supports search via `file_semantic_search`.

Document extraction (PDF/Office) uses MarkItDown workflow and keeps source files in place.
