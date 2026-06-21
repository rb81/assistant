# Memory, Notes, and Semantic Indexing

This document describes the durable memory system, entity-linked recall, and related semantic services.

## Architecture Overview

The memory system has two tiers:

1. **Durable Memories** (`agent_memories`) — high-signal, long-lived facts: decisions, agreements, preferences, operating rules, project context.
2. **Working Notes** (`agent_notes`) — shorter-lived working memory: meeting context, action items, pending follow-ups, relationship notes.

Both tiers support **entity-linked recall** — structural links to domain objects (contacts, projects, reminders, jobs) that enable deterministic retrieval independent of embedding similarity.

## Entity Registry

The **Entity Registry** is a first-class categorical organization system that automatically links all objects (memories, notes, reminders, projects, contacts, jobs, emails, calendar events) to high-level entities representing major areas, topics, or projects in the user's life.

### High-Level Entities vs. Entity-Linked Recall

The system has two complementary entity systems that serve different purposes:

1. **Entity Registry (`entities` + `entity_object_links`)** — High-level categorical groupings
   - Examples: "IntelliGulf", "Personal Finance", "Health & Fitness", "Family"
   - Purpose: Organize ALL objects into broad, meaningful categories
   - Implementation: Dedicated tables with junction links
   - Managed by: `EntityStore` + `EntityLinker` (LLM-powered auto-classification)

2. **Entity-Linked Recall (`linked_entities` JSONB)** — Domain object cross-references
   - Examples: Contact #42, Project #7, Reminder #88, Job #123
   - Purpose: Structural links between memories/notes and specific domain objects
   - Implementation: JSONB column with polymorphic URN-style references
   - Managed by: `EntityResolver` (deterministic resolution from context)

### How Entity Registry Works

When objects are created (memories, notes, reminders, projects, contacts), the system automatically:

1. Calls the configured mini model (memory steward model) to determine which high-level entities the object belongs to
2. Prefers linking to existing entities over creating new ones
3. Creates new entities only when nothing in the existing list fits
4. Links up to 3 entities per object (prefers 1-2)

**Design Principles:**
- **Best-effort**: Linking failures never block object creation
- **High-level**: Entities should be broad and meaningful, not granular ("Personal Finance" ✓, "Tuesday Tasks" ✗)
- **Prefer existing**: Always reuse entities rather than creating near-duplicates
- **Bidirectional**: Can query objects by entity OR entities by object

### Configuration

```yaml
agent:
  entities:
    enabled: true                  # Enable entity registry
    max_per_object: 3             # Max entities per object
    auto_link_on_create: true     # Auto-link when objects created
```

### API Endpoints

- `GET /api/entities` — List all entities with object counts
- `POST /api/entities` — Create entity
- `GET /api/entities/{id}` — Get entity details + linked objects
- `PUT /api/entities/{id}` — Update entity
- `DELETE /api/entities/{id}` — Delete with cascade (preview available)
- `POST /api/entities/{id}/merge` — Merge entity into another

### Cascade Deletion

The entity registry supports intelligent cascade deletion:
- **Exclusive objects** (linked ONLY to this entity) are physically deleted
- **Shared objects** (linked to multiple entities) are merely unlinked
- `GET /api/entities/{id}/delete-preview` shows what would happen before confirming

### When to Use Which System

- **Use Entity Registry** for: Broad categorization, cross-object organization, "show me everything related to IntelliGulf"
- **Use Entity-Linked Recall** for: Specific memory/note relationships, deterministic recall, "notes about Alice Chen"

## Entity-Linked Recall (Domain Objects)

### Problem

Pure semantic/embedding search fails when:
- The user says "reschedule the meeting" but the recall query doesn't match the note about the meeting.
- Two unrelated items share similar language (e.g., "project update" matches multiple projects).
- The agent confabulates connections between unrelated entities.

### Solution: Polymorphic Entity References

Memories and notes carry a `linked_entities` JSONB column containing an array of entity references:

```json
[
  {"type": "contact", "ref_id": 42, "label": "Alice Chen"},
  {"type": "project", "ref_id": 7, "label": "Q3 Marketing Campaign"}
]
```

Valid entity types: `contact`, `project`, `reminder`, `job`.

These are **polymorphic URN pointers** — there is no separate entities table. Each reference points to a row in the corresponding domain table (contacts, projects, reminders, jobs).

### Entity Resolution

`EntityResolver` (`entity_resolver.py`) resolves entities from task context during recall and consolidation. Resolution is **read-only** — it never creates entities.

Resolution pipeline (ordered by confidence):
1. **Deterministic**: sender email → contact lookup
2. **Deterministic**: thread_id → recent jobs
3. **Deterministic**: linked reminder (if present)
4. **Name matching**: capitalized names in text → contacts
5. **Project matching**: active project keywords in text
6. **Embedding fallback**: semantic contact search when <2 results

### Confidence-Tiered Recall Output

The recall system produces structured output distinguishing:

- **LINKED CONTEXT** — retrieved via structural entity links (high confidence, verified)
- **POSSIBLY RELATED** — retrieved via semantic similarity (uncertain, treat as hypothesis)
- **DRILL-DOWN** — suggested tools/queries for verification

The system prompt instructs the agent to treat POSSIBLY RELATED items as hypotheses requiring verification before action.

## Memory Steward

`MemorySteward` (`memory_manager.py`) runs synchronously in the task-agent flow.

### Recall Phase

Before prompt build, the steward:

1. Resolves entities from task context (sender, thread, reminder, text mentions).
2. Fetches entity-linked memories and notes via JSONB containment queries.
3. Performs semantic/keyword fallback search for additional candidates.
4. Formats structured recall output with confidence tiers.

### Consolidation Phase

After terminal/review outcomes, the steward:

1. Resolves entities from the completed task context.
2. Asks the LLM to extract durable memories with entity links.
3. Validates proposed entity links against resolved entities (rejects unverified links).
4. Stores memories with validated `linked_entities`.
5. Triggers mini-reflection if high-signal tools were used.

Allowed durable memory kinds:
- `decision`
- `agreement`
- `incident`
- `preference`
- `operating_rule`
- `project_context`

Steward intentionally rejects low-signal or transient details.

### Mini-Reflection

After jobs that use high-signal tools (calendar operations, email_send, contact_create, project_create), the steward automatically captures working knowledge as entity-linked notes.

High-signal tools: `calendar_create_event`, `calendar_update_event`, `calendar_delete_event`, `email_send`, `contact_create`, `project_create`.

Mini-reflection:
- Creates or updates notes with entity links.
- Captures decisions, commitments, and next steps.
- Marks resolved notes when their topic is concluded.
- Logs failures and parse errors at WARNING level for diagnostics.

Controlled by config: `agent.memory.steward.mini_reflection_enabled` (default: `true`).

### Conflict Detection and Resolution

When the Memory Steward proposes creating a new memory, it checks for existing memories of the same `kind` with high semantic similarity (>0.85 by default). If a conflict is detected, the existing memory is updated rather than creating a duplicate.

Conflict detection:
- Compares against active memories of the same kind
- Uses cosine similarity on embeddings
- Updates the best match when above threshold
- Logs conflict resolution events for audit

Controlled by:
- `agent.memory.steward.conflict_detection_enabled` (default: `true`)
- `agent.memory.steward.conflict_similarity_threshold` (default: `0.85`)

This prevents memory fragmentation and ensures the most current version of facts are preserved.

### Memory Lifecycle Management

Memories and notes have automated lifecycle maintenance to prevent unbounded growth:

**Memory Reaping** runs periodically (default: 24h) and expires stale memories:
- Targets low-importance memories (≤2 by default) not accessed in 90+ days
- Pinned memories are never reaped
- Sets `expires_at` rather than deleting (preserves audit trail)
- All reaping actions logged to `memory_events`

**Note Reaping** archives old working notes:
- Archives `active` notes not updated in 60+ days (configurable)
- Changes status to `archived` (preserves content/title/tags)
- Excludes already-archived or resolved notes
- Logs to `note_events` with reason and metadata

**Recency Boost** in semantic search gives slight priority to newer memories:
- Adds up to +0.1 to similarity scores based on age
- Max boost applied to memories within configured window (default: 365 days)
- Prevents old memories from completely dominating results
- Does not override strong semantic matches

Configuration:
- `agent.memory.steward.reap_after_days` (default: `90`)
- `agent.memory.steward.reap_max_importance` (default: `2`)
- `agent.memory.steward.reap_interval_hours` (default: `24`)
- `agent.memory.steward.note_reap_after_days` (default: `60`)
- `agent.embeddings.recency_boost_max` (default: `0.1`)
- `agent.embeddings.recency_boost_days` (default: `365`)

## Memory Store

Backed by:
- `agent_memories` (with `linked_entities` JSONB column)
- `memory_events`

Supports:
- keyword search,
- semantic search (when embeddings available),
- entity-linked search (JSONB containment queries),
- create/update/delete with event audit.

## Notes System

Notes are the agent's working memory — proactively created to track ongoing context.

Backed by:
- `agent_notes` (with `status` and `linked_entities` columns)
- `note_events`

### Note Lifecycle

Notes have a status lifecycle:
- `active` (default) — current, relevant working knowledge
- `resolved` — the noted matter has been handled
- `archived` — no longer relevant, kept for history

The agent marks notes `resolved` rather than deleting them, preserving audit trail. Additionally, stale `active` notes (not updated in 60+ days by default) are automatically archived through periodic maintenance.

### Entity-Linked Note Search

Notes can be searched by entity filter:
```
note_search(entity_filter={"type": "contact", "ref_id": 42})
```

This uses GIN-indexed JSONB containment queries for fast lookup.

## Context Search Window

Context search (used by the `context_search` tool and Memory Steward recall) defaults to a 90-day window (`agent.context.search_days`). This can be overridden:
- The `context_search` tool accepts `recent_only=true` to limit to 7 days
- Longer windows provide more coverage; shorter windows improve performance

## Why Contacts Are Separate

Contact details are intentionally excluded from memory stewardship and handled by dedicated contact tools/store (`contacts`).

This avoids contaminating durable behavioral memory with mutable contact records. However, contacts are **linked** to memories and notes via entity references.

## Anti-Confabulation Design

Several design choices prevent the agent from making erroneous assumptions:

1. **Read-only recall**: Entity resolution during recall never creates records. Typos and hallucinated names don't pollute the entity graph.
2. **Confidence tiers**: The agent sees which context is structurally verified vs. semantically guessed.
3. **Validated links on write**: During consolidation, only entity links matching resolved entities are persisted.
4. **System prompt guidance**: Explicit instructions to never assume connections not confirmed by recall.

## Database Schema

### Columns added to `agent_memories`:
- `linked_entities jsonb NOT NULL DEFAULT '[]'::jsonb`

### Columns added to `agent_notes`:
- `status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'resolved', 'archived'))`
- `linked_entities jsonb NOT NULL DEFAULT '[]'::jsonb`

### Indexes:
- `agent_memories_linked_entities_idx` — GIN index on `linked_entities`
- `agent_notes_linked_entities_idx` — GIN index on `linked_entities`

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
