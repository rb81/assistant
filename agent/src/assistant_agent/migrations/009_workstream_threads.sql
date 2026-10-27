ALTER TABLE workstream_threads
  ADD COLUMN role text NOT NULL DEFAULT 'main' CHECK (role IN ('main', 'system'));

ALTER TABLE workstreams
  ADD COLUMN main_thread_id text,
  ADD COLUMN origin_channel text NOT NULL DEFAULT 'local'
    CHECK (origin_channel IN ('telegram', 'email', 'local')),
  ADD COLUMN name_status text NOT NULL DEFAULT 'named'
    CHECK (name_status IN ('named', 'temporary', 'pending', 'naming', 'failed')),
  ADD COLUMN name_attempts integer NOT NULL DEFAULT 0,
  ADD COLUMN name_attempted_at timestamptz,
  ADD COLUMN telegram_message_count integer NOT NULL DEFAULT 0;

UPDATE workstreams w
SET main_thread_id = (
  SELECT wt.thread_id
  FROM workstream_threads wt
  LEFT JOIN jobs j ON j.thread_id = wt.thread_id
  LEFT JOIN emails e ON e.thread_id = wt.thread_id
  WHERE wt.workstream_id = w.id
  ORDER BY COALESCE(j.created_at, e.created_at, w.created_at), wt.thread_id
  LIMIT 1
);
UPDATE workstreams w
SET origin_channel = COALESCE(
  (SELECT r.channel FROM request_messages r
   JOIN workstream_threads t ON t.thread_id = r.thread_id
   WHERE t.workstream_id = w.id AND r.channel IN ('telegram', 'email')
   ORDER BY r.received_at, r.id LIMIT 1),
  (SELECT 'email' FROM emails e
   JOIN workstream_threads t ON t.thread_id = e.thread_id
   WHERE t.workstream_id = w.id LIMIT 1),
  'local'
);

INSERT INTO workstream_threads(thread_id, workstream_id, role)
SELECT 'workstream:' || w.id::text, w.id, 'main'
FROM workstreams w
WHERE NOT EXISTS (SELECT 1 FROM workstream_threads t WHERE t.workstream_id = w.id);
UPDATE workstreams SET main_thread_id = 'workstream:' || id::text WHERE main_thread_id IS NULL;
ALTER TABLE workstreams ALTER COLUMN main_thread_id SET NOT NULL;
CREATE UNIQUE INDEX workstreams_main_thread_idx ON workstreams(main_thread_id);
ALTER TABLE workstreams ADD CONSTRAINT workstreams_main_thread_fk
  FOREIGN KEY (main_thread_id) REFERENCES workstream_threads(thread_id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE telegram_workstream_selections (
  bot_id text NOT NULL,
  sender_id text NOT NULL,
  workstream_id bigint,
  unavailable_workstream_id bigint,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (bot_id, sender_id),
  CHECK (workstream_id IS NULL OR unavailable_workstream_id IS NULL)
);

CREATE TABLE deleted_thread_references (
  reference_hash text PRIMARY KEY,
  deleted_workstream_id bigint NOT NULL,
  deleted_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE workstream_object_events (
  id bigserial PRIMARY KEY,
  object_id bigint NOT NULL,
  object_kind text NOT NULL,
  object_key text NOT NULL,
  origin_workstream_id bigint NOT NULL,
  actor_job_id bigint,
  actor_workstream_id bigint,
  event_type text NOT NULL CHECK (event_type IN ('INSERT', 'UPDATE', 'DELETE')),
  changes jsonb NOT NULL,
  reason text,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE workstream_objects ADD COLUMN version bigint NOT NULL DEFAULT 1;
CREATE INDEX workstream_object_events_object_idx ON workstream_object_events(object_id, created_at DESC);
CREATE INDEX workstream_object_events_actor_idx ON workstream_object_events(actor_workstream_id, created_at DESC);

CREATE TABLE workstream_context_events (
  id bigserial PRIMARY KEY,
  workstream_id bigint NOT NULL REFERENCES workstreams(id) ON DELETE CASCADE,
  object_event_id bigint NOT NULL REFERENCES workstream_object_events(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(workstream_id, object_event_id)
);
CREATE INDEX workstream_context_events_stream_idx ON workstream_context_events(workstream_id, created_at DESC);

CREATE TABLE workstream_task_notifications (
  id bigserial PRIMARY KEY,
  workstream_id bigint NOT NULL REFERENCES workstreams(id) ON DELETE CASCADE,
  source_job_id bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  source_thread_id text NOT NULL,
  notification_key text NOT NULL,
  notification_type text NOT NULL CHECK (notification_type IN ('input_requested', 'completed')),
  summary text NOT NULL,
  content text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(source_job_id, notification_key)
);
CREATE INDEX workstream_task_notifications_stream_idx
  ON workstream_task_notifications(workstream_id, created_at DESC);

CREATE TABLE provider_mutations (
  id bigserial PRIMARY KEY,
  provider_name text NOT NULL,
  resource_type text NOT NULL CHECK (resource_type IN ('calendar_event', 'zoom_meeting')),
  resource_key text NOT NULL,
  operation text NOT NULL CHECK (operation IN ('CREATE', 'UPDATE', 'DELETE')),
  idempotency_key text NOT NULL UNIQUE,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'uncertain', 'completed', 'failed')),
  before_state jsonb NOT NULL DEFAULT '{}'::jsonb,
  desired_state jsonb NOT NULL DEFAULT '{}'::jsonb,
  result_state jsonb NOT NULL DEFAULT '{}'::jsonb,
  attempts integer NOT NULL DEFAULT 0,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);
CREATE INDEX provider_mutations_resource_idx
  ON provider_mutations(resource_type, resource_key, created_at DESC);

CREATE OR REPLACE FUNCTION ensure_workstream(task_thread text, label text DEFAULT '', parent bigint DEFAULT NULL)
RETURNS bigint AS $$
DECLARE owner_id bigint;
DECLARE role_value text := CASE WHEN current_setting('assistant.thread_role', true) = 'system' THEN 'system' ELSE 'main' END;
DECLARE telegram_new boolean := current_setting('assistant.telegram_new_workstream', true) = 'yes';
DECLARE channel_value text := NULLIF(current_setting('assistant.workstream_channel', true), '');
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('workstream:' || task_thread));
  SELECT workstream_id INTO owner_id FROM workstream_threads WHERE thread_id = task_thread;
  IF owner_id IS NULL THEN
    IF parent IS NOT NULL AND NOT EXISTS (SELECT 1 FROM workstreams WHERE id = parent AND status = 'active') THEN
      RAISE EXCEPTION 'parent workstream is unavailable';
    END IF;
    IF parent IS NOT NULL AND role_value = 'system' THEN
      owner_id := parent;
      INSERT INTO workstream_threads(thread_id, workstream_id, role) VALUES (task_thread, owner_id, 'system');
    ELSE
      IF channel_value IS NULL THEN
        SELECT channel INTO channel_value FROM request_messages
          WHERE thread_id = task_thread AND channel IN ('telegram', 'email')
          ORDER BY id LIMIT 1;
      END IF;
      INSERT INTO workstreams(title, parent_id, name_status, main_thread_id)
        VALUES (COALESCE(NULLIF(label, ''),
                         NULLIF(current_setting('assistant.telegram_workstream_title', true), ''),
                         task_thread), parent,
                CASE WHEN telegram_new THEN 'temporary' ELSE 'named' END, task_thread)
        RETURNING id INTO owner_id;
      UPDATE workstreams SET origin_channel = COALESCE(channel_value, 'local') WHERE id = owner_id;
      INSERT INTO workstream_threads(thread_id, workstream_id, role) VALUES (task_thread, owner_id, role_value);
    END IF;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM workstreams WHERE id = owner_id AND status = 'active') THEN
    RAISE EXCEPTION 'workstream is being deleted';
  END IF;
  RETURN owner_id;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION workstream_record_object_change() RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION workstream_check_object_version() RETURNS trigger AS $$
DECLARE expected_version bigint := NULLIF(current_setting('assistant.expected_object_version', true), '')::bigint;
DECLARE expected_kind text := NULLIF(current_setting('assistant.expected_object_kind', true), '');
DECLARE expected_key text := NULLIF(current_setting('assistant.expected_object_key', true), '');
DECLARE object_identifier bigint := CASE WHEN TG_OP = 'DELETE' THEN OLD.object_id ELSE NEW.object_id END;
DECLARE current_version bigint;
DECLARE current_kind text;
DECLARE current_key text;
BEGIN
  IF expected_version IS NULL THEN
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
  END IF;
  SELECT version, kind, object_key INTO current_version, current_kind, current_key
    FROM workstream_objects WHERE id = object_identifier FOR UPDATE;
  IF current_kind IS DISTINCT FROM expected_kind OR current_key IS DISTINCT FROM expected_key THEN
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
  END IF;
  IF current_setting('assistant.expected_object_checked', true) IS DISTINCT FROM 'yes' THEN
    IF current_version IS DISTINCT FROM expected_version THEN
      RAISE EXCEPTION 'object changed since it was read; refresh it before updating';
    END IF;
    PERFORM set_config('assistant.expected_object_checked', 'yes', true);
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY['jobs', 'agent_memories', 'contacts', 'reminders', 'deep_research_runs',
    'calendar_managed_events', 'zoom_managed_meetings', 'dropbox_transfers', 'processed_artifacts', 'workstream_files']
  LOOP
    EXECUTE format('CREATE TRIGGER workstream_object_version BEFORE UPDATE OR DELETE ON %I '
      'FOR EACH ROW EXECUTE FUNCTION workstream_check_object_version()', table_name);
    EXECUTE format('CREATE TRIGGER workstream_object_audit AFTER INSERT OR UPDATE OR DELETE ON %I '
      'FOR EACH ROW EXECUTE FUNCTION workstream_record_object_change()', table_name);
  END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION workstream_register_object() RETURNS trigger AS $$
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
  IF owner_id IS NOT NULL AND (TG_OP = 'INSERT' OR OLD.object_id IS DISTINCT FROM NEW.object_id) THEN
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

CREATE OR REPLACE FUNCTION workstream_count_telegram_message() RETURNS trigger AS $$
DECLARE stream record;
BEGIN
  IF NEW.channel <> 'telegram' THEN
    RETURN NEW;
  END IF;
  SELECT w.id, w.main_thread_id, w.name_status
    INTO stream
    FROM workstreams w
    JOIN workstream_threads t ON t.workstream_id = w.id
    WHERE t.thread_id = NEW.thread_id;
  IF NOT FOUND OR stream.main_thread_id <> NEW.thread_id THEN
    RETURN NEW;
  END IF;
  UPDATE workstreams
    SET telegram_message_count = telegram_message_count + 1,
        name_status = CASE
          WHEN name_status = 'temporary' AND telegram_message_count + 1 >= 3 THEN 'pending'
          ELSE name_status
        END
    WHERE id = stream.id;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER telegram_workstream_message_count
  AFTER INSERT ON request_messages
  FOR EACH ROW EXECUTE FUNCTION workstream_count_telegram_message();
