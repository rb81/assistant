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
search. Contact details live in the dedicated `contacts` store and contact tools,
so mutable records do not contaminate durable behavior.

## Memory Steward

`MemorySteward` (`memory_manager.py`) runs synchronously in the task-agent flow.

**Recall.** Before the prompt is built it retrieves memory candidates using
keyword and semantic search, prioritizing the current workstream and retaining
cross-workstream provenance. `max_injected_memories` (default 8) bounds the
selected memory candidates; the synthesis currently includes at most eight.
It also searches other context sources and uses the configured steward model
to synthesize a concise recall summary.

**Consolidation.** At completion, failure, review, and asynchronous-wait paths,
the steward asks the configured model to propose memories, subject to
`min_importance`, `min_confidence` and `max_writes_per_job`. Inferred memories
must include evidence and why they are relevant to the future; routine action
logs and low-signal details are rejected. Failures are ignored in the default
`best_effort` mode; `mode: required` propagates recall/consolidation errors to
the task-agent's exception handling.

Configuration (`agent.memory.steward.*`): `enabled`, `mode`, `model`, `timeout_seconds`,
`max_tokens_per_call`, `max_injected_memories`, `max_writes_per_job`,
`min_importance`, `min_confidence`, `conflict_detection_enabled` and
`conflict_similarity_threshold` (default 0.85). The wizard's Advanced Settings
cover automatic stewardship and the injected-memory limit.

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

The shipped configuration sets `agent.context.search_days` to 90 (the code
fallback when absent is 30). This window applies to jobs, reminders, email, and
request messages, not memories or contacts. `recent_only=true` uses 7 days for
the time-windowed sources; memory expiry still governs eligible memories.

## Embeddings and Ollama

Embeddings use `EmbeddingClient` and the Ollama endpoint
(`agent.embeddings.base_url`, default `http://ollama:11434`) with configurable
model (`embeddinggemma` by default).

When embeddings fail or are unavailable, relevant features fall back to
non-semantic methods where supported.

Use `./setup.sh --section embeddings` to configure the model, endpoint,
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

The `workspace-indexer` service automatically refreshes missing or incompatible
vectors across all seven sources: memories, workspace chunks, jobs, reminders,
outbound emails, inbound emails, and contacts. PostgreSQL sends a content-free
notification after changes commit, waking the worker without waiting for a full
workspace scan. Missing/stale vectors in the source tables are the durable work
queue; a one-second recovery poll handles missed notifications. File discovery
still uses the workspace scan interval (60 seconds by default).

Bulk embedding generation and workspace scans run only while task and research
workers are idle. A shared/exclusive database advisory lock coordinates every
worker replica and manual rebuild. Background embedding passes have a five-second
budget and process at most 10 records per source. Refresh still runs when
workspace file indexing is disabled. `workspace-index-cron` and `all` also perform
idle-only refreshes.

Before claiming queued task/research work, a worker drains pending vectors with
a 60-second startup budget. It then keeps an execution lock until processing
ends, preventing background indexing from competing with active work. A waiting
task takes priority over another idle pass; a manual rebuild yields between
records when a task is waiting. The handoff waits for any in-flight background
operation to release its lock rather than overlapping it with task execution.

If indexing cannot catch up within the startup budget, the task proceeds with a
logged warning, a `timeout` task event with reason `embedding_index_incomplete`, and keyword-only
retrieval for that run. It does not silently treat an incomplete index as current.
Pending records remain eligible for retry when execution finishes. New changes
that arrive during active execution are also indexed at the next idle/startup
handoff, not concurrently with the running task.

Memory writes and workspace chunk creation defer source embeddings to this
worker instead of invoking Ollama inline. Normal semantic searches still generate
a query embedding on demand: this is required to compare a question with the
stored vectors and is not bulk indexing. Timed-out/degraded runs skip even these
query calls and use existing keyword-search paths.

Changes to indexed source fields invalidate the old vector, including a job's
final response. Unrelated status or metadata updates do not invalidate vectors.
Failures are logged and retried on later passes; a failed record does not prevent
other records from being indexed. Expired memories and deleted/superseded
workspace files are excluded. Automatic refresh and manual rebuilds share a
database advisory lock, and neither overwrites concurrent source edits.

To deploy automatic indexing on an existing installation, rebuild and recreate
the services from the repository root:

```sh
sudo docker compose --env-file data/.env up -d --build
sudo docker compose --env-file data/.env logs --tail=100 workspace-indexer
```

Services apply the embedding-invalidation/notification migration at startup. Existing pending
records are picked up automatically; manual reindexing is not required.

### Inspecting and rebuilding vectors

After saving embedding settings, build the agent image and run from the
repository root:

```sh
docker compose --env-file data/.env build task-agent
docker compose --env-file data/.env run --rm --no-deps --entrypoint python task-agent -m assistant_agent.embedding_maintenance status
docker compose --env-file data/.env run --rm --no-deps --entrypoint python task-agent -m assistant_agent.embedding_maintenance probe
docker compose --env-file data/.env run --rm --no-deps --entrypoint python task-agent -m assistant_agent.embedding_maintenance reindex --batch-size 50
```

`status` lists stale counts for seven embedding-backed tables (memory, workspace
chunks, jobs, reminders, outbound email, inbound email, and contacts) and exits
nonzero while replacements remain pending. `probe` verifies the configured
endpoint and vector dimensions. `reindex` replaces only missing
or incompatible vectors, preserving source data, IDs and content timestamps;
expired memories and deleted/superseded workspace files are excluded. PostgreSQL
receives the embedding-only timestamp safeguard before rebuilding; this command
does not run unrelated pending schema changes.

PostgreSQL must be running for status/reindex; Ollama must be running and the
model available for probe/reindex. Failed runs retain completed replacements: rerun the same
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

Document extraction (PDF/Office) uses MarkItDown and keeps source files in place.
Indexing extracts text without automatically replacing documents with Markdown;
explicit conversion records derivative files and conversion lineage.
