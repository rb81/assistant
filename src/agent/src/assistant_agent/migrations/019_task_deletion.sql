ALTER TABLE jobs ADD COLUMN deleted_at timestamptz;

ALTER TABLE jobs DROP CONSTRAINT jobs_status_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_status_check
  CHECK (status IN ('queued', 'running', 'waiting', 'completed', 'failed',
                    'needs_review', 'cancelled', 'deleted'));
ALTER TABLE jobs ADD CONSTRAINT jobs_deleted_at_check
  CHECK ((status = 'deleted') = (deleted_at IS NOT NULL));

CREATE OR REPLACE FUNCTION workstream_record_object_change() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE before_data jsonb := '{}'::jsonb;
DECLARE after_data jsonb := '{}'::jsonb;
DECLARE changes_data jsonb;
DECLARE object_identifier bigint;
DECLARE object_kind text;
DECLARE object_key text;
DECLARE origin_id bigint;
DECLARE actor_job bigint;
DECLARE actor_stream bigint;
DECLARE event_identifier bigint;
DECLARE recipient record;
BEGIN
  IF TG_TABLE_NAME = 'jobs' AND TG_OP = 'UPDATE' THEN
    IF NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL THEN
      RETURN NEW;
    END IF;
  END IF;
  IF TG_OP <> 'INSERT' THEN before_data := to_jsonb(OLD); END IF;
  IF TG_OP <> 'DELETE' THEN after_data := to_jsonb(NEW); END IF;
  object_identifier := COALESCE(NULLIF(after_data->>'object_id', '')::bigint,
                                NULLIF(before_data->>'object_id', '')::bigint);
  SELECT o.kind, o.object_key, o.origin_workstream_id INTO object_kind, object_key, origin_id
    FROM workstream_objects o WHERE o.id = object_identifier;
  IF object_kind IS NULL THEN
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
  END IF;
  SELECT COALESCE(jsonb_object_agg(COALESCE(new_value.key, old_value.key),
      jsonb_build_object('before', old_value.value, 'after', new_value.value)), '{}'::jsonb)
    INTO changes_data
    FROM jsonb_each(before_data) old_value
    FULL JOIN jsonb_each(after_data) new_value ON new_value.key = old_value.key
    WHERE old_value.value IS DISTINCT FROM new_value.value
      AND COALESCE(new_value.key, old_value.key) NOT IN
        ('object_id', 'created_at', 'updated_at', 'last_accessed_at', 'embedding',
         'embedding_model', 'embedding_dimensions', 'embedding_updated_at', 'metadata');
  IF changes_data = '{}'::jsonb AND TG_OP = 'UPDATE' THEN
    IF (before_data - ARRAY['object_id', 'created_at', 'updated_at', 'last_accessed_at', 'embedding',
        'embedding_model', 'embedding_dimensions', 'embedding_updated_at'])
       IS DISTINCT FROM
       (after_data - ARRAY['object_id', 'created_at', 'updated_at', 'last_accessed_at', 'embedding',
        'embedding_model', 'embedding_dimensions', 'embedding_updated_at']) THEN
      UPDATE workstream_objects SET version = version + 1 WHERE id = object_identifier;
    END IF;
    RETURN NEW;
  END IF;
  actor_job := NULLIF(current_setting('assistant.job_id', true), '')::bigint;
  IF actor_job IS NULL AND TG_TABLE_NAME = 'jobs' THEN actor_job := NEW.id; END IF;
  actor_stream := NULLIF(current_setting('assistant.workstream_id', true), '')::bigint;
  IF actor_job IS NOT NULL THEN
    SELECT workstream_id INTO actor_stream FROM jobs WHERE id = actor_job;
  END IF;
  INSERT INTO workstream_object_events(object_id, object_kind, object_key, origin_workstream_id,
      actor_job_id, actor_workstream_id, event_type, changes, reason)
    VALUES (object_identifier, object_kind, object_key, origin_id, actor_job, actor_stream, TG_OP,
      changes_data, NULLIF(current_setting('assistant.change_reason', true), ''))
    RETURNING id INTO event_identifier;
  FOR recipient IN
    SELECT workstream_id FROM workstream_attachments WHERE object_id = object_identifier
  LOOP
    INSERT INTO workstream_context_events(workstream_id, object_event_id)
      VALUES (recipient.workstream_id, event_identifier) ON CONFLICT DO NOTHING;
    UPDATE jobs SET has_new_context = true
      WHERE workstream_id = recipient.workstream_id AND status = 'running'
        AND (actor_job IS NULL OR id <> actor_job);
  END LOOP;
  IF TG_OP = 'UPDATE' THEN
    UPDATE workstream_objects SET version = version + 1 WHERE id = object_identifier;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$$;

-- Dependent writers lock only the referenced job rows. KEY SHARE conflicts with deletion's FOR UPDATE
-- (taken before its blocker inventory) and returns the latest committed version, or a serialization
-- error under snapshot isolation. Taking request advisory locks here would invert the request-lock ->
-- row-lock order used by request handling while the writer already holds its own row lock.
-- Status changes are guarded only when inactive work becomes active: finishing, failing or moving
-- between active states never creates dependent work (active work already blocks deletion), and must
-- not wait on a request that holds the task row and resumes that work.
CREATE FUNCTION assert_task_not_deleted(task_ids bigint[]) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE reference record;
BEGIN
  FOR reference IN
    SELECT j.deleted_at, j.status FROM jobs j WHERE j.id = ANY(task_ids) ORDER BY j.id FOR KEY SHARE
  LOOP
    IF reference.deleted_at IS NOT NULL OR reference.status = 'deleted' THEN
      RAISE EXCEPTION 'cannot create dependent work for a deleted task';
    END IF;
  END LOOP;
END;
$$;

-- Every task whose deletion inventory would list these tasks as related work. Mirrors the
-- descendant walk in TaskDeletionService: parent_job_id, reminder-created jobs and retired project tasks.
CREATE FUNCTION task_lineage_ids(task_ids bigint[]) RETURNS bigint[]
LANGUAGE sql STABLE AS $$
  WITH RECURSIVE lineage(id) AS (
    SELECT ids.id FROM unnest(task_ids) AS ids(id) WHERE ids.id IS NOT NULL
    UNION
    SELECT up.id
    FROM lineage child
    CROSS JOIN LATERAL (
      SELECT (j.metadata->>'parent_job_id')::bigint FROM jobs j
      WHERE j.id = child.id AND j.metadata->>'parent_job_id' ~ '^[0-9]{1,18}$'
      UNION
      SELECT r.created_by_job_id FROM reminders r
      WHERE r.job_id = child.id AND r.created_by_job_id IS NOT NULL
      UNION
      SELECT root.id FROM jobs root
      CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(root.metadata->'retired_project_tasks') = 'array'
             THEN root.metadata->'retired_project_tasks' ELSE '[]'::jsonb END
      ) retired
      WHERE retired->>'id' = child.id::text OR retired->>'job_id' = child.id::text
    ) up(id)
  )
  SELECT COALESCE(array_agg(id ORDER BY id), '{}') FROM lineage
$$;

-- Dependent work locks its whole ancestor lineage, so it serializes with deletion of any ancestor,
-- not only its immediate parent. Locks are KEY SHARE only, taken in id order.
CREATE FUNCTION assert_task_lineage_not_deleted(task_ids bigint[]) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM assert_task_not_deleted(task_lineage_ids(task_ids));
END;
$$;

CREATE FUNCTION guard_job_task_deletion() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE parent_id bigint;
BEGIN
  IF TG_OP = 'UPDATE' AND OLD.deleted_at IS NOT NULL THEN
    -- Tombstones stay immutable except for referential cleanup and workstream-deletion rehoming.
    IF (to_jsonb(NEW) - ARRAY['workstream_id', 'trigger_email_id', 'trigger_request_id'])
         IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['workstream_id', 'trigger_email_id', 'trigger_request_id'])
       OR (NEW.trigger_email_id IS NOT NULL AND NEW.trigger_email_id IS DISTINCT FROM OLD.trigger_email_id)
       OR (NEW.trigger_request_id IS NOT NULL AND NEW.trigger_request_id IS DISTINCT FROM OLD.trigger_request_id)
       OR (NEW.workstream_id IS DISTINCT FROM OLD.workstream_id
           AND current_setting('assistant.workstream_cleanup', true) IS DISTINCT FROM 'yes') THEN
      RAISE EXCEPTION 'deleted tasks are immutable';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.metadata->>'parent_job_id' ~ '^[0-9]{1,18}$' THEN
    parent_id := (NEW.metadata->>'parent_job_id')::bigint;
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF parent_id IS NOT NULL THEN
      PERFORM assert_task_lineage_not_deleted(ARRAY[parent_id]);
    END IF;
  ELSIF NEW.metadata->>'parent_job_id' IS DISTINCT FROM OLD.metadata->>'parent_job_id'
      OR (OLD.status NOT IN ('queued', 'running', 'waiting', 'needs_review')
          AND NEW.status IN ('queued', 'running', 'waiting', 'needs_review')) THEN
    -- Reactivation of any task in a lineage (legacy children included) is new dependent work.
    PERFORM assert_task_lineage_not_deleted(ARRAY[NEW.id, parent_id]);
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER a_guard_job_task_deletion
  BEFORE INSERT OR UPDATE ON jobs
  FOR EACH ROW EXECUTE FUNCTION guard_job_task_deletion();

CREATE FUNCTION guard_research_task_deletion() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.original_job_id IS NOT NULL THEN
    IF TG_OP = 'INSERT' THEN
      PERFORM assert_task_lineage_not_deleted(ARRAY[NEW.original_job_id]);
    ELSIF NEW.original_job_id IS DISTINCT FROM OLD.original_job_id
        OR (OLD.status NOT IN ('queued', 'running', 'waiting_for_input')
            AND NEW.status IN ('queued', 'running', 'waiting_for_input')) THEN
      PERFORM assert_task_lineage_not_deleted(ARRAY[NEW.original_job_id]);
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER a_guard_research_task_deletion
  BEFORE INSERT OR UPDATE OF original_job_id, status ON deep_research_runs
  FOR EACH ROW EXECUTE FUNCTION guard_research_task_deletion();

CREATE FUNCTION guard_reminder_task_deletion() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT'
      OR NEW.created_by_job_id IS DISTINCT FROM OLD.created_by_job_id
      OR NEW.job_id IS DISTINCT FROM OLD.job_id
      OR (NEW.status IS DISTINCT FROM OLD.status AND NEW.status IN ('scheduled', 'queued')) THEN
    PERFORM assert_task_lineage_not_deleted(ARRAY[NEW.created_by_job_id, NEW.job_id]);
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER a_guard_reminder_task_deletion
  BEFORE INSERT OR UPDATE OF created_by_job_id, job_id, status ON reminders
  FOR EACH ROW EXECUTE FUNCTION guard_reminder_task_deletion();

-- Guidance writers may already hold job row locks, so this guard takes only the row lock that
-- deletion itself waits for; an instruction committed first remains a deletion blocker.
CREATE FUNCTION guard_supervisor_instruction_task_deletion() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM assert_task_not_deleted(ARRAY[NEW.job_id]);
  RETURN NEW;
END;
$$;

CREATE TRIGGER a_guard_supervisor_instruction_task_deletion
  BEFORE INSERT OR UPDATE OF job_id ON supervisor_instructions
  FOR EACH ROW EXECUTE FUNCTION guard_supervisor_instruction_task_deletion();

CREATE FUNCTION guard_task_log_deletion() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM assert_task_not_deleted(ARRAY[NEW.job_id]);
  RETURN NEW;
END;
$$;

CREATE TRIGGER a_guard_task_log_deletion
  BEFORE INSERT OR UPDATE OF job_id ON task_logs
  FOR EACH ROW EXECUTE FUNCTION guard_task_log_deletion();
