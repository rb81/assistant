CREATE TABLE workstreams (
  id bigserial PRIMARY KEY,
  parent_id bigint REFERENCES workstreams(id) ON DELETE CASCADE,
  title text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleting')),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (parent_id IS NULL OR parent_id <> id)
);
CREATE INDEX workstreams_parent_idx ON workstreams(parent_id);

CREATE TABLE workstream_threads (
  thread_id text PRIMARY KEY,
  workstream_id bigint NOT NULL REFERENCES workstreams(id) ON DELETE CASCADE
);
CREATE INDEX workstream_threads_owner_idx ON workstream_threads(workstream_id);

CREATE TABLE workstream_objects (
  id bigserial PRIMARY KEY,
  kind text NOT NULL,
  object_key text NOT NULL,
  -- Keep the immutable identifier even after the originating workstream is erased.
  origin_workstream_id bigint NOT NULL,
  deleting boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(kind, object_key)
);
CREATE TABLE workstream_attachments (
  workstream_id bigint NOT NULL REFERENCES workstreams(id) ON DELETE CASCADE,
  object_id bigint NOT NULL REFERENCES workstream_objects(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(workstream_id, object_id)
);
CREATE INDEX workstream_attachments_object_idx ON workstream_attachments(object_id);

CREATE TABLE workstream_files (
  id bigserial PRIMARY KEY,
  path text NOT NULL UNIQUE,
  sha256 text,
  source_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL
);

CREATE TABLE workstream_deletions (
  id bigserial PRIMARY KEY,
  workstream_ids bigint[] NOT NULL,
  object_ids bigint[] NOT NULL,
  mode text NOT NULL CHECK (mode IN ('preserve_shared', 'delete_objects', 'connected')),
  manifest jsonb NOT NULL,
  completed_steps jsonb NOT NULL DEFAULT '[]'::jsonb,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'failed', 'completed')),
  error text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION ensure_workstream(task_thread text, label text DEFAULT '', parent bigint DEFAULT NULL)
RETURNS bigint AS $$
DECLARE owner_id bigint;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('workstream:' || task_thread));
  SELECT workstream_id INTO owner_id FROM workstream_threads WHERE thread_id = task_thread;
  IF owner_id IS NULL THEN
    IF parent IS NOT NULL AND NOT EXISTS (SELECT 1 FROM workstreams WHERE id = parent AND status = 'active') THEN
      RAISE EXCEPTION 'parent workstream is unavailable';
    END IF;
    INSERT INTO workstreams(title, parent_id)
      VALUES (COALESCE(NULLIF(label, ''), task_thread), parent) RETURNING id INTO owner_id;
    INSERT INTO workstream_threads VALUES (task_thread, owner_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM workstreams WHERE id = owner_id AND status = 'active') THEN
    RAISE EXCEPTION 'workstream is being deleted';
  END IF;
  RETURN owner_id;
END;
$$ LANGUAGE plpgsql;

-- Existing thread identities are evidence; unrelated legacy objects are not.
SELECT ensure_workstream(thread_id) FROM (
  SELECT thread_id FROM emails UNION SELECT thread_id FROM jobs UNION
  SELECT thread_id FROM request_messages UNION SELECT thread_id FROM processed_artifacts
) t;
SELECT ensure_workstream('system:unattributed', 'Unattributed legacy and administrative data');

CREATE FUNCTION workstream_bind_owner() RETURNS trigger AS $$
DECLARE data jsonb := to_jsonb(NEW); owner_id bigint; source_id bigint; parent_hint bigint;
BEGIN
  PERFORM pg_advisory_xact_lock_shared(hashtext('workstream-maintenance'));
  owner_id := NULLIF(current_setting('assistant.workstream_id', true), '')::bigint;
  IF TG_OP = 'UPDATE' AND NEW.workstream_id IS DISTINCT FROM OLD.workstream_id
      AND current_setting('assistant.workstream_cleanup', true) IS DISTINCT FROM 'yes' THEN
    RAISE EXCEPTION 'message/execution ownership cannot be reassigned';
  END IF;
  IF data->>'thread_id' IS NOT NULL THEN
    IF TG_OP = 'UPDATE' AND data->>'thread_id' IS DISTINCT FROM to_jsonb(OLD)->>'thread_id' THEN
      IF TG_TABLE_NAME <> 'request_messages' OR COALESCE(data->'metadata'->>'queued', 'false') = 'true' THEN
        RAISE EXCEPTION 'only unqueued requests may select a different workstream';
      END IF;
      NEW.workstream_id := NULL;
    END IF;
    IF NEW.workstream_id IS NULL THEN
      parent_hint := NULLIF(current_setting('assistant.parent_workstream_id', true), '')::bigint;
      IF parent_hint IS NULL AND NULLIF(current_setting('assistant.job_id', true), '') IS NOT NULL THEN
        SELECT workstream_id INTO parent_hint FROM jobs
          WHERE id = current_setting('assistant.job_id')::bigint;
      END IF;
      IF parent_hint IS NULL AND TG_TABLE_NAME = 'jobs' AND data->'metadata'->>'parent_job_id' ~ '^[0-9]+$' THEN
        SELECT workstream_id INTO parent_hint FROM jobs WHERE id = (data->'metadata'->>'parent_job_id')::bigint;
      END IF;
      NEW.workstream_id := ensure_workstream(data->>'thread_id', COALESCE(data->>'subject', data->>'task_summary', ''),
        parent_hint);
    END IF;
  ELSIF NEW.workstream_id IS NULL THEN
    source_id := NULLIF(data->>'job_id', '')::bigint;
    IF source_id IS NOT NULL THEN
      SELECT workstream_id INTO owner_id FROM jobs WHERE id = source_id;
    END IF;
    IF owner_id IS NULL AND data->>'request_id' IS NOT NULL THEN
      SELECT workstream_id INTO owner_id FROM request_messages WHERE id = (data->>'request_id')::bigint;
    END IF;
    NEW.workstream_id := COALESCE(owner_id, ensure_workstream('system:unattributed'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM workstreams WHERE id = NEW.workstream_id AND
      (status = 'active' OR current_setting('assistant.workstream_cleanup', true) = 'yes')) THEN
    RAISE EXCEPTION 'workstream is unavailable';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY['emails', 'request_messages', 'jobs', 'outbound_email_logs', 'channel_deliveries', 'thread_summaries']
  LOOP
    EXECUTE format('ALTER TABLE %I ADD COLUMN workstream_id bigint REFERENCES workstreams(id) ON DELETE CASCADE', table_name);
    EXECUTE format('CREATE TRIGGER workstream_owner BEFORE INSERT OR UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION workstream_bind_owner()', table_name);
    EXECUTE format('UPDATE %I SET workstream_id = NULL', table_name);
    EXECUTE format('ALTER TABLE %I ALTER COLUMN workstream_id SET NOT NULL', table_name);
    EXECUTE format('CREATE INDEX ON %I(workstream_id)', table_name);
  END LOOP;
END;
$$;

CREATE FUNCTION workstream_register_object() RETURNS trigger AS $$
DECLARE data jsonb := to_jsonb(NEW); owner_id bigint; source_id bigint; key_value text;
BEGIN
  PERFORM pg_advisory_xact_lock_shared(hashtext('workstream-maintenance'));
  IF TG_OP = 'UPDATE' AND NEW.object_id IS DISTINCT FROM OLD.object_id THEN
    RAISE EXCEPTION 'object identity is immutable';
  END IF;
  IF NEW.object_id IS NOT NULL AND EXISTS (SELECT 1 FROM workstream_objects WHERE id = NEW.object_id AND deleting)
      AND current_setting('assistant.workstream_cleanup', true) IS DISTINCT FROM 'yes' THEN
    RAISE EXCEPTION 'object deletion is pending';
  END IF;
  owner_id := NULLIF(current_setting('assistant.workstream_id', true), '')::bigint;
  source_id := NULLIF(current_setting('assistant.job_id', true), '')::bigint;
  IF source_id IS NULL AND (TG_OP = 'INSERT' OR NEW.object_id IS NULL OR
      data->>'source_job_id' IS DISTINCT FROM to_jsonb(OLD)->>'source_job_id') THEN
    source_id := COALESCE(NULLIF(data->>'source_job_id', '')::bigint, NULLIF(data->>'created_by_job_id', '')::bigint,
      NULLIF(data->>'original_job_id', '')::bigint, NULLIF(data->>'job_id', '')::bigint);
  END IF;
  IF source_id IS NOT NULL THEN
    SELECT workstream_id INTO owner_id FROM jobs WHERE id = source_id;
    IF owner_id IS NULL THEN RAISE EXCEPTION 'source job is unavailable'; END IF;
  END IF;
  IF TG_TABLE_NAME = 'jobs' THEN owner_id := NEW.workstream_id; END IF;
  IF owner_id IS NULL AND NEW.object_id IS NULL AND data->>'thread_id' IS NOT NULL THEN
    owner_id := ensure_workstream(data->>'thread_id');
  END IF;
  IF NEW.object_id IS NULL THEN
    owner_id := COALESCE(owner_id, ensure_workstream('system:unattributed'));
    key_value := data->>TG_ARGV[1];
    INSERT INTO workstream_objects(kind, object_key, origin_workstream_id)
      VALUES (TG_ARGV[0], key_value, owner_id)
      ON CONFLICT(kind, object_key) DO UPDATE SET kind = EXCLUDED.kind RETURNING id INTO NEW.object_id;
  END IF;
  IF owner_id IS NOT NULL THEN
    INSERT INTO workstream_attachments(workstream_id, object_id) VALUES (owner_id, NEW.object_id)
      ON CONFLICT DO NOTHING;
    IF NOT EXISTS (SELECT 1 FROM workstreams WHERE id = owner_id AND
        (status = 'active' OR current_setting('assistant.workstream_cleanup', true) = 'yes')) THEN
      RAISE EXCEPTION 'workstream is unavailable';
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION workstream_forget_object() RETURNS trigger AS $$
BEGIN
  DELETE FROM workstream_objects WHERE id = OLD.object_id;
  RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION workstream_remove_unmaterialized_objects() RETURNS trigger AS $$
BEGIN
  EXECUTE format('DELETE FROM workstream_objects o WHERE o.kind = $1 AND NOT EXISTS (SELECT 1 FROM %I t WHERE t.object_id = o.id)', TG_TABLE_NAME)
    USING TG_ARGV[0];
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE spec text[]; table_name text;
BEGIN
  FOREACH spec SLICE 1 IN ARRAY ARRAY[
    ['jobs', 'job', 'id'], ['agent_memories', 'memory', 'id'], ['contacts', 'contact', 'id'],
    ['reminders', 'reminder', 'id'], ['deep_research_runs', 'research', 'id'],
    ['calendar_managed_events', 'calendar_event', 'assistant_id'],
    ['zoom_managed_meetings', 'zoom_meeting', 'id'],
    ['dropbox_transfers', 'dropbox_transfer', 'request_key'],
    ['processed_artifacts', 'artifact', 'id'], ['workstream_files', 'file', 'id']
  ]
  LOOP
    table_name := spec[1];
    EXECUTE format('ALTER TABLE %I ADD COLUMN object_id bigint UNIQUE REFERENCES workstream_objects(id) ON DELETE CASCADE', table_name);
    EXECUTE format('CREATE TRIGGER zz_workstream_object BEFORE INSERT OR UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION workstream_register_object(%L, %L)', table_name, spec[2], spec[3]);
    EXECUTE format('CREATE TRIGGER workstream_object_removed AFTER DELETE ON %I FOR EACH ROW EXECUTE FUNCTION workstream_forget_object()', table_name);
    EXECUTE format('CREATE TRIGGER workstream_insert_cleanup AFTER INSERT ON %I FOR EACH STATEMENT EXECUTE FUNCTION workstream_remove_unmaterialized_objects(%L)', table_name, spec[2]);
    EXECUTE format('UPDATE %I SET object_id = NULL', table_name);
    EXECUTE format('ALTER TABLE %I ALTER COLUMN object_id SET NOT NULL', table_name);
  END LOOP;
END;
$$;

ALTER TABLE dropbox_transfers DROP CONSTRAINT dropbox_transfers_job_id_fkey;
ALTER TABLE dropbox_transfers ALTER COLUMN job_id DROP NOT NULL;
ALTER TABLE dropbox_transfers ADD FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL;
ALTER TABLE deep_research_runs DROP CONSTRAINT deep_research_runs_original_job_id_fkey;
ALTER TABLE deep_research_runs ALTER COLUMN original_job_id DROP NOT NULL;
ALTER TABLE deep_research_runs ADD FOREIGN KEY (original_job_id) REFERENCES jobs(id) ON DELETE SET NULL;
ALTER TABLE processed_artifacts DROP CONSTRAINT processed_artifacts_source_required;
ALTER TABLE processed_artifacts DROP CONSTRAINT processed_artifacts_email_id_fkey;
ALTER TABLE processed_artifacts DROP CONSTRAINT processed_artifacts_request_id_fkey;
ALTER TABLE processed_artifacts ADD FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE SET NULL;
ALTER TABLE processed_artifacts ADD FOREIGN KEY (request_id) REFERENCES request_messages(id) ON DELETE SET NULL;

CREATE FUNCTION workstream_attachment_guard() RETURNS trigger AS $$
BEGIN
  PERFORM pg_advisory_xact_lock_shared(hashtext('workstream-maintenance'));
  IF NOT EXISTS (SELECT 1 FROM workstreams WHERE id = NEW.workstream_id AND status = 'active')
      AND current_setting('assistant.workstream_cleanup', true) IS DISTINCT FROM 'yes' THEN
    RAISE EXCEPTION 'workstream is unavailable';
  END IF;
  IF EXISTS (SELECT 1 FROM workstream_objects WHERE id = NEW.object_id AND deleting)
      AND current_setting('assistant.workstream_cleanup', true) IS DISTINCT FROM 'yes' THEN
    RAISE EXCEPTION 'object deletion is pending';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER attachment_guard BEFORE INSERT OR UPDATE ON workstream_attachments
  FOR EACH ROW EXECUTE FUNCTION workstream_attachment_guard();

CREATE FUNCTION workstream_immutable_origin() RETURNS trigger AS $$
BEGIN
  IF NEW.origin_workstream_id <> OLD.origin_workstream_id OR NEW.kind <> OLD.kind OR NEW.object_key <> OLD.object_key THEN
    RAISE EXCEPTION 'object provenance is immutable';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER immutable_origin BEFORE UPDATE ON workstream_objects
  FOR EACH ROW EXECUTE FUNCTION workstream_immutable_origin();

CREATE FUNCTION workstream_delete_guard() RETURNS trigger AS $$
BEGIN
  IF current_setting('assistant.workstream_cleanup', true) IS DISTINCT FROM 'yes' THEN
    RAISE EXCEPTION 'use the confirmed workstream deletion workflow';
  END IF;
  RETURN OLD;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER confirmed_deletion BEFORE DELETE ON workstreams
  FOR EACH ROW EXECUTE FUNCTION workstream_delete_guard();

-- Backfill only unambiguous, older execution parents from existing structural links.
DO $$
DECLARE link record;
BEGIN
  FOR link IN
    WITH candidates AS (
      SELECT child.workstream_id AS child_id, parent.workstream_id AS parent_id
      FROM jobs child JOIN jobs parent ON parent.id::text = child.metadata->>'parent_job_id'
      WHERE parent.id < child.id
      UNION
      SELECT child.workstream_id, parent.workstream_id FROM reminders r
      JOIN jobs child ON child.id = r.job_id JOIN jobs parent ON parent.id = r.created_by_job_id
      WHERE parent.id < child.id
      UNION
      SELECT child.workstream_id, parent.workstream_id FROM jobs parent
      CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(parent.metadata->'retired_project_tasks') = 'array'
        THEN parent.metadata->'retired_project_tasks' ELSE '[]'::jsonb END) task
      JOIN jobs child ON child.id::text = task->>'job_id'
      WHERE parent.id < child.id
    )
    SELECT child_id, min(parent_id) AS parent_id FROM candidates WHERE child_id <> parent_id
    GROUP BY child_id HAVING count(DISTINCT parent_id) = 1
  LOOP
    IF NOT EXISTS (
      WITH RECURSIVE ancestors AS (
        SELECT id, parent_id FROM workstreams WHERE id = link.parent_id
        UNION SELECT w.id, w.parent_id FROM workstreams w JOIN ancestors a ON w.id = a.parent_id
      ) SELECT 1 FROM ancestors WHERE id = link.child_id
    ) THEN
      UPDATE workstreams SET parent_id = link.parent_id WHERE id = link.child_id AND parent_id IS NULL;
    END IF;
  END LOOP;
END;
$$;

CREATE FUNCTION workstream_parent_guard() RETURNS trigger AS $$
BEGIN
  PERFORM pg_advisory_xact_lock_shared(hashtext('workstream-maintenance'));
  IF TG_OP = 'UPDATE' AND NEW.parent_id IS DISTINCT FROM OLD.parent_id THEN
    RAISE EXCEPTION 'workstream ancestry is immutable';
  END IF;
  IF TG_OP = 'INSERT' AND NEW.parent_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM workstreams WHERE id = NEW.parent_id AND status = 'active'
  ) THEN RAISE EXCEPTION 'parent workstream is unavailable'; END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER immutable_parent BEFORE INSERT OR UPDATE ON workstreams
  FOR EACH ROW EXECUTE FUNCTION workstream_parent_guard();

CREATE FUNCTION workstream_object_delete_guard() RETURNS trigger AS $$
BEGIN
  PERFORM pg_advisory_xact_lock_shared(hashtext('workstream-maintenance'));
  IF EXISTS (SELECT 1 FROM workstream_objects WHERE id = OLD.object_id AND deleting)
      AND current_setting('assistant.workstream_cleanup', true) IS DISTINCT FROM 'yes' THEN
    RAISE EXCEPTION 'object deletion is pending; resume the confirmed deletion';
  END IF;
  RETURN OLD;
END;
$$ LANGUAGE plpgsql;
DO $$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY['jobs', 'agent_memories', 'contacts', 'reminders', 'deep_research_runs',
    'calendar_managed_events', 'zoom_managed_meetings', 'dropbox_transfers', 'processed_artifacts', 'workstream_files']
  LOOP
    EXECUTE format('CREATE TRIGGER workstream_delete_pending BEFORE DELETE ON %I FOR EACH ROW EXECUTE FUNCTION workstream_object_delete_guard()', table_name);
  END LOOP;
END;
$$;
