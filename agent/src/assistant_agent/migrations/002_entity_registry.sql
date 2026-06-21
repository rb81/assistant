-- Migration: Entity Registry
-- Adds a first-class entity registry for high-level grouping of all data objects.
-- Entities are user/agent-defined categories (projects, topics, areas of life)
-- that any object in the system can be linked to.

-- =============================================================================
-- 1. Entity Registry table
-- =============================================================================

CREATE TABLE IF NOT EXISTS entities (
  id bigserial PRIMARY KEY,
  name text NOT NULL,
  description text NOT NULL DEFAULT '',
  created_by text NOT NULL DEFAULT 'system',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT entities_name_unique UNIQUE(name)
);

CREATE TRIGGER entities_set_updated_at
BEFORE UPDATE ON entities
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- 2. Junction table: links entities to any object in the system
-- =============================================================================

CREATE TABLE IF NOT EXISTS entity_object_links (
  id bigserial PRIMARY KEY,
  entity_id bigint NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  object_type text NOT NULL CHECK (object_type IN (
    'memory', 'note', 'reminder', 'project', 'project_task',
    'job', 'contact', 'email', 'calendar_event'
  )),
  object_id bigint NOT NULL,
  linked_by text NOT NULL DEFAULT 'agent',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(entity_id, object_type, object_id)
);

CREATE INDEX IF NOT EXISTS entity_object_links_entity_idx
  ON entity_object_links(entity_id, object_type);
CREATE INDEX IF NOT EXISTS entity_object_links_object_idx
  ON entity_object_links(object_type, object_id);
