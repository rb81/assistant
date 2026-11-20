# Memory System and Semantic Indexing

Assistant keeps durable and short-lived working context in one memory store.

## Unified memories

Memories live in `agent_memories`, with an audit trail in `memory_events`. There
are two kinds of content:

- **Durable facts**: decisions, agreements, incidents, preferences, operating
  rules and project context. They carry evidence and provenance.
- **Working context** (`working_context`): short-lived context such as meeting
  details or follow-ups. It expires after `agent.memory.working_context_days`
  (default 14 days) when written by the steward without an explicit expiry.
  Pinning protects against stale-memory reaping; it does not clear an existing
  expiry or prevent explicit expiration/deletion.

When semantic conflict detection finds a similar active memory of the same kind,
the steward stores the new fact as a reviewable **suggestion**, rather than
overwriting the existing memory. Exact duplicates are skipped. Administrators
review, correct, pin, expire and delete memories with email `/admin memory`
commands, including controlled erasure of logged copies. Sources may be learned
again afterwards. See [Email Administration](email-administration.md)
for the commands and [security](security.md) for deletion limits.

Memory supports keyword search and, when embeddings are available, semantic
search. With embeddings, a keyword hit is returned only if it contains the full
query phrase, enough of the query's words as whole words
(`agent.memory.keyword_min_match`, default 0.5), or is also semantically similar
(`min_similarity`). Results report `similarity`, `keyword_match` and `match`;
`confidence` is how certain the memory was when stored, not relevance. Contact details live in the dedicated `contacts` store and contact tools,
so mutable records do not contaminate durable behavior.

## Memory Steward

`MemorySteward` (`memory_manager.py`) runs synchronously in the task-agent flow.

**Recall.** Before the prompt is built it retrieves memory candidates using
keyword and semantic search, prioritizing the current workstream and retaining
cross-workstream provenance. `max_injected_memories` (default 8) bounds the
selected memory candidates; the synthesis currently includes at most eight.
It also searches other context sources and uses the configured steward model
to synthesize a concise recall summary. Only memories the summary cites
(`memory #ID`) are marked accessed; every candidate shown to the model is still
recorded in `memory_ids` for erasure. Recording access updates
`last_accessed_at` without changing `updated_at`, so reads do not inflate recency.

**Consolidation.** At completion, failure, review, and asynchronous-wait paths,
the steward asks the configured model to propose memories, subject to
`min_importance`, `min_confidence`, `max_memory_chars` and `max_writes_per_job`. Inferred memories
must include evidence and why they are relevant to the future; routine action
logs and low-signal details are rejected. Proposals longer than
`max_memory_chars` (default 500) are rejected as non-atomic, and reminder or task
configuration should be referenced by ID rather than copied. Failures are ignored in the default
`best_effort` mode; `mode: required` propagates recall/consolidation errors to
the task-agent's exception handling.

Configuration (`agent.memory.steward.*`): `enabled`, `mode`, `model`, `timeout_seconds`,
`max_tokens_per_call`, `max_injected_memories`, `max_writes_per_job`,
`min_importance`, `min_confidence`, `max_memory_chars`, `conflict_detection_enabled` and
`conflict_similarity_threshold` (default 0.85). Use the **Memory** settings
for automatic stewardship and the injected-memory limit.

### Lifecycle

Reaping runs periodically (default every 24 hours) and expires stale,
low-importance memories (importance at most 2, not accessed in 90 days). Pinned
memories are never reaped. For never-accessed memories, age is measured from
creation. Reaping sets `expires_at` rather than deleting, and is logged to
`memory_events`. Semantic ranking adds a small recency boost (up to +0.1,
decreasing to zero over 365 days) based on the memory's update time.

Settings: `agent.memory.steward.reap_after_days`, `reap_max_importance`,
`reap_interval_hours`, `agent.memory.embeddings.min_similarity`,
`recency_boost_max` and `recency_boost_days`.

## Context Search Window

`context_search` and steward context recall search memories, jobs, reminders,
inbound/outbound email, contacts, and non-email requests. Request messages use
keyword search even when other sources use embeddings. Workspace chunks are
searched separately through `file_semantic_search`.

`agent.context.search_days` defaults to 90 days in both shipped configuration and
code. This window applies to jobs, reminders, email, and
request messages, not memories or contacts. `recent_only=true` uses 7 days for
the time-windowed sources; memory expiry still governs eligible memories.

## Embeddings and Ollama

Embeddings use `EmbeddingClient` and the Ollama endpoint
(`agent.embeddings.base_url`, default `http://ollama:11434`) with configurable
model (`embeddinggemma` by default).

When embeddings fail or are unavailable, relevant features fall back to
non-semantic methods where supported.

Use `./run.sh --section embeddings` to configure the model, endpoint,
timeout, and output dimensions. The maintenance probe requires Ollama 0.11.10+
for `embeddinggemma` and expects 768 dimensions unless configured otherwise.
Long inputs may be truncated by Ollama; source text limits and workspace
chunking still apply. Encoding uses raw text without query/document prefixes;
changing that convention would also require a consistent vector rebuild.

Changing model or dimensions requires rebuilding existing vectors. Memory,
context, and workspace retrieval filter out vectors with a different model,
incompatible dimensions, or missing model metadata.
Keyword fallback remains available, but semantic recall can be incomplete until
the rebuild finishes. No source records are deleted to change embedding models.

The `workspace-indexer` refreshes missing or incompatible vectors for memories,
workspace chunks, jobs, reminders, inbound/outbound email, and contacts.
PostgreSQL notifications wake it after committed changes; a one-second poll
recovers missed notifications. File discovery uses the workspace scan interval
(60 seconds by default).

Bulk embedding and workspace scans run only while task and research workers are
idle. Database advisory locks coordinate workers and manual rebuilds. Background
passes allow five seconds and at most 10 records per source. Refresh also runs
when workspace file indexing is disabled; `workspace-index-cron` and `all` roles
also run idle refreshes.

Before claiming queued task/research work, a worker drains pending vectors for up
to 60 seconds. If indexing remains incomplete, the task proceeds with a warning
and a `timeout` event (`reason=embedding_index_incomplete`), using keyword-only
retrieval for that run. It does not treat an incomplete index as current. Pending
work is retried later; changes made during a task wait for the next idle/startup
pass.

Memory writes and workspace chunks enqueue source vectors rather than calling
Ollama inline. Semantic queries still embed on demand unless the run is in
keyword-only mode.

Indexed-field changes invalidate vectors; unrelated status or metadata updates
do not. Failures are retried without blocking other records. Expired memories
and deleted or superseded workspace files are excluded.

To deploy automatic indexing on an existing installation, rebuild and recreate
the services from the repository root:

```sh
docker compose --env-file data/.env up -d --build
docker compose --env-file data/.env logs --tail=100 workspace-indexer
```

Services apply the embedding-invalidation/notification migration at startup.
Pending records are picked up automatically; manual reindexing is unnecessary.

### Inspecting and rebuilding vectors

After saving embedding settings, build the agent image and run from the
repository root:

```sh
docker compose --env-file data/.env build task-agent
docker compose --env-file data/.env run --rm --no-deps --entrypoint python task-agent -m assistant_agent.embedding_maintenance status
docker compose --env-file data/.env run --rm --no-deps --entrypoint python task-agent -m assistant_agent.embedding_maintenance probe
docker compose --env-file data/.env run --rm --no-deps --entrypoint python task-agent -m assistant_agent.embedding_maintenance reindex --batch-size 50
```

`status` lists stale counts for the seven embedding-backed tables and exits
nonzero while replacements remain pending. `probe` checks the endpoint and
dimensions. `reindex` replaces only missing or incompatible vectors, preserving
source data, IDs, and content timestamps; expired memories and deleted or
superseded workspace files are excluded. It applies the embedding-only timestamp
safeguard, not unrelated pending schema changes.

PostgreSQL is required for status/reindex; Ollama and the model are required for
probe/reindex. Failed runs retain completed replacements, so rerun to resume.
Concurrent source edits/deletions are not overwritten; incomplete rebuilds exit
nonzero. Recreate enabled embedding consumers after changing settings so they
receive the new environment. Rebuild does not enable workers.

## Workspace Semantic Index

`WorkspaceIndex` maintains semantic file search:

- tracks file metadata in `workspace_files`,
- stores chunked content in `workspace_file_chunks`,
- records conversion lineage in `workspace_document_conversions`,
- supports search via `file_semantic_search`.

Document extraction (PDF/Office) uses MarkItDown and keeps source files in place.
Indexing extracts text without automatically replacing documents with Markdown;
explicit conversion records derivative files and conversion lineage.
