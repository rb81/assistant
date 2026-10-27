-- Migration: Entity-Linked Associative Memory
-- Adds linked_entities to memories and notes, adds status lifecycle to notes.
-- Entities are polymorphic URN pointers (type + ref_id) referencing existing domain tables.

-- =============================================================================
-- 1. Add linked_entities to agent_memories
-- =============================================================================

ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS linked_entities jsonb NOT NULL DEFAULT '[]'::jsonb;
CREATE INDEX IF NOT EXISTS agent_memories_linked_entities_idx ON agent_memories USING GIN(linked_entities);

-- =============================================================================
-- 2. Add status and linked_entities to agent_notes
-- =============================================================================

ALTER TABLE agent_notes ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active';

DO $$ BEGIN
  ALTER TABLE agent_notes ADD CONSTRAINT agent_notes_status_check
    CHECK (status IN ('active', 'resolved', 'archived'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE agent_notes ADD COLUMN IF NOT EXISTS linked_entities jsonb NOT NULL DEFAULT '[]'::jsonb;
CREATE INDEX IF NOT EXISTS agent_notes_linked_entities_idx ON agent_notes USING GIN(linked_entities);
CREATE INDEX IF NOT EXISTS agent_notes_status_idx ON agent_notes(status);

-- =============================================================================
-- 3. Helper function for entity-linked queries
-- =============================================================================
-- Query pattern: find notes/memories linked to a specific entity
-- Example: SELECT * FROM agent_notes WHERE linked_entities @> '[{"type":"contact","ref_id":42}]'::jsonb;
--
-- For text-based ref_ids (e.g. thread_id):
-- SELECT * FROM agent_notes WHERE linked_entities @> '[{"type":"thread","ref_id":"abc123"}]'::jsonb;
