CREATE UNIQUE INDEX IF NOT EXISTS memories_legacy_note_idx
  ON agent_memories((metadata->>'legacy_note_id')) WHERE metadata ? 'legacy_note_id';

DO $$
BEGIN
  IF to_regclass('entity_object_links') IS NOT NULL THEN
    UPDATE agent_memories m SET tags = ARRAY(
      SELECT DISTINCT lower(tag) FROM unnest(m.tags || ARRAY(
        SELECT e.name FROM entity_object_links l JOIN entities e ON e.id = l.entity_id
        WHERE l.object_type = 'memory' AND l.object_id = m.id
      )) AS tag ORDER BY lower(tag)
    );
    UPDATE agent_notes n SET tags = ARRAY(
      SELECT DISTINCT lower(tag) FROM unnest(n.tags || ARRAY(
        SELECT e.name FROM entity_object_links l JOIN entities e ON e.id = l.entity_id
        WHERE l.object_type = 'note' AND l.object_id = n.id
      )) AS tag ORDER BY lower(tag)
    );
  END IF;
  IF to_regclass('agent_notes') IS NOT NULL THEN
    INSERT INTO agent_memories(content, tags, scope, kind, importance, confidence,
      expires_at, pinned, source_job_id, metadata, created_at, updated_at)
    SELECT n.content, n.tags, 'global', 'working_context', 3, 0.7,
      CASE WHEN COALESCE(to_jsonb(n)->>'status', 'active') = 'active' THEN now() + interval '14 days' ELSE now() END,
      false, n.source_job_id,
      n.metadata || jsonb_build_object('legacy_note_id', n.id, 'title', n.title,
        'source', 'legacy_note', 'source_refs', COALESCE(to_jsonb(n)->'linked_entities', '[]'::jsonb),
        'legacy_status', COALESCE(to_jsonb(n)->>'status', 'active'), 'embedding_pending', true),
      n.created_at, n.updated_at
    FROM agent_notes n
    WHERE NOT EXISTS (SELECT 1 FROM agent_memories m WHERE m.metadata->>'legacy_note_id' = n.id::text);
    IF to_regclass('note_events') IS NOT NULL THEN
      INSERT INTO memory_events(memory_id, job_id, actor, event_type, input_data, output_data, created_at)
      SELECT m.id, e.job_id, e.actor, e.event_type, e.input_data, e.output_data, e.created_at
      FROM note_events e JOIN agent_memories m ON m.metadata->>'legacy_note_id' = e.note_id::text;
    END IF;
  END IF;
  IF to_regclass('projects') IS NOT NULL THEN
    UPDATE jobs j SET metadata = j.metadata || jsonb_build_object('retired_project', to_jsonb(p),
      'retired_project_tasks', COALESCE((SELECT jsonb_agg(to_jsonb(t) ORDER BY t.sequence) FROM project_tasks t WHERE t.project_id = p.id), '[]'::jsonb))
    FROM projects p WHERE j.id = p.original_job_id;
    INSERT INTO supervisor_instructions(job_id, instruction, created_by)
    SELECT p.original_job_id,
      'Projects has been retired. Continue this request through ordinary long-chain execution. Do not create delegated project tasks. Inspect retired_project_tasks in the job metadata: existing child jobs remain independent jobs; verify their status with job_read and never duplicate their effects. Unstarted tasks were not queued.',
      'feature-retirement'
    FROM projects p JOIN jobs j ON j.id = p.original_job_id
    WHERE p.status IN ('queued', 'running') AND j.status = 'waiting';
    UPDATE jobs j SET status = 'queued', run_at = now(), locked_at = NULL, locked_by = NULL,
      has_new_context = true, updated_at = now()
    FROM projects p WHERE j.id = p.original_job_id AND p.status IN ('queued', 'running') AND j.status = 'waiting';
    UPDATE jobs SET metadata = (metadata - 'project_id') || jsonb_build_object('retired_project_id', metadata->'project_id')
    WHERE metadata ? 'project_id';
  END IF;
END $$;

UPDATE agent_memories SET metadata = metadata || jsonb_build_object('source_refs', linked_entities)
WHERE linked_entities <> '[]'::jsonb;
ALTER TABLE agent_memories DROP COLUMN IF EXISTS linked_entities;
DROP TABLE IF EXISTS entity_object_links, entities, note_events, agent_notes, project_tasks, projects;