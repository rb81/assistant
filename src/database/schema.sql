SET check_function_bodies = false;

CREATE FUNCTION ensure_workstream(task_thread text, label text DEFAULT ''::text, parent bigint DEFAULT NULL::bigint) RETURNS bigint
    LANGUAGE plpgsql
    AS $$
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
$$;

CREATE FUNCTION link_email_artifact_request() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  IF NEW.request_id IS NULL AND NEW.email_id IS NOT NULL THEN
    SELECT id INTO NEW.request_id FROM request_messages WHERE email_id = NEW.email_id;
  END IF;
  RETURN NEW;
END;
$$;

CREATE FUNCTION mirror_email_request() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  INSERT INTO request_messages(channel, external_id, conversation_id, sender_id, thread_id,
                               email_id, body_text, attachments, reply_route, received_at)
  VALUES (CASE WHEN NEW.from_address LIKE '%@local' THEN 'local' ELSE 'email' END,
          NEW.message_id, NEW.thread_id, NEW.from_address, NEW.thread_id, NEW.id,
          COALESCE(NEW.body_text, ''), NEW.attachments,
          CASE WHEN NEW.from_address LIKE '%@local' THEN '{}'::jsonb
               ELSE jsonb_build_object('channel', 'email', 'to', NEW.from_address,
                                      'in_reply_to', NEW.message_id, 'subject', COALESCE(NEW.subject, 'Reply')) END,
          NEW.received_at)
  ON CONFLICT (email_id) DO UPDATE SET body_text = EXCLUDED.body_text,
    attachments = EXCLUDED.attachments, thread_id = EXCLUDED.thread_id, reply_route = EXCLUDED.reply_route;
  RETURN NEW;
END;
$$;

CREATE FUNCTION set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF to_jsonb(NEW) ? 'embedding'
         AND NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at
         AND (to_jsonb(NEW) - ARRAY['embedding', 'embedding_model', 'embedding_dimensions', 'embedding_updated_at', 'metadata'])
                 = (to_jsonb(OLD) - ARRAY['embedding', 'embedding_model', 'embedding_dimensions', 'embedding_updated_at', 'metadata'])
         AND (COALESCE(to_jsonb(NEW)->'metadata', '{}'::jsonb) - 'embedding_pending')
                 = (COALESCE(to_jsonb(OLD)->'metadata', '{}'::jsonb) - 'embedding_pending') THEN
        RETURN NEW;
    END IF;
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE FUNCTION workstream_attachment_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
$$;

CREATE FUNCTION workstream_bind_owner() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$
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
$_$;

CREATE FUNCTION workstream_check_object_version() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
$$;

CREATE FUNCTION workstream_count_telegram_message() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
$$;

CREATE FUNCTION workstream_delete_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  IF current_setting('assistant.workstream_cleanup', true) IS DISTINCT FROM 'yes' THEN
    RAISE EXCEPTION 'use the confirmed workstream deletion workflow';
  END IF;
  RETURN OLD;
END;
$$;

CREATE FUNCTION workstream_forget_object() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  DELETE FROM workstream_objects WHERE id = OLD.object_id;
  RETURN OLD;
END;
$$;

CREATE FUNCTION workstream_immutable_origin() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  IF NEW.origin_workstream_id <> OLD.origin_workstream_id OR NEW.kind <> OLD.kind OR NEW.object_key <> OLD.object_key THEN
    RAISE EXCEPTION 'object provenance is immutable';
  END IF;
  RETURN NEW;
END;
$$;

CREATE FUNCTION workstream_object_delete_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  PERFORM pg_advisory_xact_lock_shared(hashtext('workstream-maintenance'));
  IF EXISTS (SELECT 1 FROM workstream_objects WHERE id = OLD.object_id AND deleting)
      AND current_setting('assistant.workstream_cleanup', true) IS DISTINCT FROM 'yes' THEN
    RAISE EXCEPTION 'object deletion is pending; resume the confirmed deletion';
  END IF;
  RETURN OLD;
END;
$$;

CREATE FUNCTION workstream_parent_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
$$;

CREATE FUNCTION workstream_record_object_change() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
$$;

CREATE FUNCTION workstream_register_object() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
$$;

CREATE FUNCTION workstream_remove_unmaterialized_objects() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$
BEGIN
  EXECUTE format('DELETE FROM workstream_objects o WHERE o.kind = $1 AND NOT EXISTS (SELECT 1 FROM %I t WHERE t.object_id = o.id)', TG_TABLE_NAME)
    USING TG_ARGV[0];
  RETURN NULL;
END;
$_$;

CREATE FUNCTION workstream_research_execution() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE source_job jobs%ROWTYPE;
DECLARE execution_thread text;
BEGIN
  IF NEW.execution_job_id IS NULL AND NEW.original_job_id IS NOT NULL THEN
    SELECT * INTO source_job FROM jobs WHERE id = NEW.original_job_id;
    execution_thread := 'research:' || NEW.id::text;
    INSERT INTO workstream_threads(thread_id, workstream_id, role)
      VALUES (execution_thread, source_job.workstream_id, 'system');
    INSERT INTO jobs(thread_id, task_summary, status, metadata)
      VALUES (execution_thread, NEW.title, 'waiting',
        (source_job.metadata - ARRAY['pending_final_response', 'final_response', 'final_response_at'])
        || jsonb_build_object('worker', 'deep-research-agent', 'research_run_id', NEW.id,
                              'waiting_for_input', false))
      RETURNING id INTO NEW.execution_job_id;
    INSERT INTO workstream_attachments(workstream_id, object_id)
      SELECT a.workstream_id, j.object_id
      FROM workstream_attachments a
      JOIN jobs j ON j.id = NEW.execution_job_id
      WHERE a.object_id = NEW.object_id
      ON CONFLICT DO NOTHING;
  END IF;
  RETURN NEW;
END;
$$;

CREATE FUNCTION workstream_research_execution_status() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE execution_status text;
BEGIN
  IF NEW.execution_job_id IS NOT NULL THEN
    execution_status := CASE
      WHEN NEW.status = 'running' THEN 'running'
      WHEN NEW.status IN ('completed', 'failed', 'cancelled') THEN NEW.status
      ELSE 'waiting'
    END;
    UPDATE jobs SET status = execution_status,
      run_at = NEW.run_at, locked_at = NEW.locked_at, locked_by = NEW.locked_by,
      last_error = NEW.last_error, completed_at = NEW.completed_at,
      metadata = metadata || jsonb_build_object('waiting_for_input', NEW.status = 'waiting_for_input')
      WHERE id = NEW.execution_job_id;
  END IF;
  RETURN NEW;
END;
$$;

CREATE FUNCTION workstream_research_job_control() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE research_run deep_research_runs%ROWTYPE;
BEGIN
  IF NEW.metadata->>'worker' IS DISTINCT FROM 'deep-research-agent'
      OR NEW.status IS NOT DISTINCT FROM OLD.status THEN
    RETURN NEW;
  END IF;
  IF NEW.status = 'queued' THEN
    UPDATE deep_research_runs SET status = 'queued', run_at = now(),
      waiting_since = NULL, last_error = NULL, completed_at = NULL,
      locked_at = NULL, locked_by = NULL
      WHERE execution_job_id = NEW.id;
  ELSIF NEW.status = 'cancelled' THEN
    UPDATE deep_research_runs SET status = 'cancelled', completed_at = now(),
      last_error = NEW.last_error, locked_at = NULL, locked_by = NULL
      WHERE execution_job_id = NEW.id AND status NOT IN ('completed', 'failed', 'cancelled')
      RETURNING * INTO research_run;
    IF FOUND THEN
      INSERT INTO workstream_task_notifications(
        workstream_id, source_job_id, source_thread_id, notification_key,
        notification_type, summary, content
      ) VALUES (NEW.workstream_id, NEW.id, NEW.thread_id, 'final', 'completed',
                'Research cancelled: ' || research_run.title,
                COALESCE(NEW.last_error, 'Research was cancelled.'))
        ON CONFLICT (source_job_id, notification_key) DO NOTHING;
      UPDATE jobs SET status = CASE WHEN status = 'waiting' THEN 'queued' ELSE status END,
        run_at = CASE WHEN status = 'waiting' THEN now() ELSE run_at END,
        has_new_context = true
        WHERE id = research_run.original_job_id AND status NOT IN ('completed', 'failed', 'cancelled');
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

CREATE TABLE agent_checkpoints (
    id bigint NOT NULL,
    job_id bigint NOT NULL,
    message_history jsonb NOT NULL,
    iteration_count integer DEFAULT 0 NOT NULL,
    token_count integer DEFAULT 0 NOT NULL,
    reason text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE agent_checkpoints_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE agent_checkpoints_id_seq OWNED BY agent_checkpoints.id;

CREATE TABLE agent_memories (
    id bigint NOT NULL,
    content text NOT NULL,
    tags text[] DEFAULT ARRAY[]::text[] NOT NULL,
    scope text DEFAULT 'global'::text NOT NULL,
    kind text DEFAULT 'fact'::text NOT NULL,
    importance integer DEFAULT 3 NOT NULL,
    confidence double precision DEFAULT 0.7 NOT NULL,
    expires_at timestamp with time zone,
    pinned boolean DEFAULT false NOT NULL,
    embedding double precision[],
    embedding_model text,
    embedding_dimensions integer,
    embedding_updated_at timestamp with time zone,
    source_job_id bigint,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    last_accessed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    object_id bigint NOT NULL
);

CREATE SEQUENCE agent_memories_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE agent_memories_id_seq OWNED BY agent_memories.id;

CREATE TABLE calendar_event_audit (
    id bigint NOT NULL,
    assistant_id text NOT NULL,
    job_id bigint,
    action text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT calendar_event_audit_action_check CHECK ((action = ANY (ARRAY['created'::text, 'updated'::text, 'deleted'::text])))
);

CREATE SEQUENCE calendar_event_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE calendar_event_audit_id_seq OWNED BY calendar_event_audit.id;

CREATE TABLE calendar_managed_events (
    assistant_id text NOT NULL,
    provider_name text NOT NULL,
    provider_event_id text NOT NULL,
    calendar_id text NOT NULL,
    calendar_name text DEFAULT ''::text NOT NULL,
    summary text DEFAULT ''::text NOT NULL,
    starts_at timestamp with time zone NOT NULL,
    ends_at timestamp with time zone NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_by_job_id bigint,
    updated_by_job_id bigint,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    object_id bigint NOT NULL,
    CONSTRAINT calendar_managed_events_status_check CHECK ((status = ANY (ARRAY['active'::text, 'deleted'::text])))
);

CREATE TABLE channel_deliveries (
    id bigint NOT NULL,
    job_id bigint,
    request_id bigint,
    channel text NOT NULL,
    destination text NOT NULL,
    purpose text DEFAULT 'message'::text NOT NULL,
    request_key text NOT NULL,
    body_text text DEFAULT ''::text NOT NULL,
    attachments jsonb DEFAULT '[]'::jsonb NOT NULL,
    provider_message_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    error text,
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    sent_at timestamp with time zone,
    workstream_id bigint NOT NULL,
    CONSTRAINT channel_deliveries_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'sending'::text, 'sent'::text, 'failed'::text, 'uncertain'::text])))
);

CREATE SEQUENCE channel_deliveries_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE channel_deliveries_id_seq OWNED BY channel_deliveries.id;

CREATE TABLE contacts (
    id bigint NOT NULL,
    first_name text DEFAULT ''::text NOT NULL,
    last_name text DEFAULT ''::text NOT NULL,
    email_address text DEFAULT ''::text NOT NULL,
    company text DEFAULT ''::text NOT NULL,
    title text DEFAULT ''::text NOT NULL,
    notes text DEFAULT ''::text NOT NULL,
    source text DEFAULT 'agent'::text NOT NULL,
    embedding double precision[],
    embedding_model text,
    embedding_dimensions integer,
    embedding_updated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    object_id bigint NOT NULL,
    CONSTRAINT contacts_not_blank_check CHECK (((first_name <> ''::text) OR (last_name <> ''::text) OR (email_address <> ''::text) OR (company <> ''::text) OR (title <> ''::text) OR (notes <> ''::text))),
    CONSTRAINT contacts_source_check CHECK ((source = ANY (ARRAY['dashboard'::text, 'agent'::text])))
);

CREATE SEQUENCE contacts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE contacts_id_seq OWNED BY contacts.id;

CREATE TABLE deep_research_events (
    id bigint NOT NULL,
    run_id bigint NOT NULL,
    sequence integer NOT NULL,
    event_type text NOT NULL,
    tool_name text,
    input_data jsonb,
    output_data jsonb,
    tokens_used jsonb,
    duration_ms integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT deep_research_events_event_type_check CHECK ((event_type = ANY (ARRAY['llm_request'::text, 'llm_response'::text, 'tool_call'::text, 'tool_result'::text, 'search_request'::text, 'search_result'::text, 'error'::text, 'status_change'::text])))
);

CREATE SEQUENCE deep_research_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE deep_research_events_id_seq OWNED BY deep_research_events.id;

CREATE TABLE deep_research_runs (
    id bigint NOT NULL,
    original_job_id bigint,
    original_thread_id text NOT NULL,
    title text NOT NULL,
    research_question text NOT NULL,
    instructions text,
    status text DEFAULT 'queued'::text NOT NULL,
    priority integer DEFAULT 0 NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    max_attempts integer DEFAULT 3 NOT NULL,
    run_at timestamp with time zone DEFAULT now() NOT NULL,
    locked_at timestamp with time zone,
    locked_by text,
    tool_call_count integer DEFAULT 0 NOT NULL,
    max_tool_calls integer DEFAULT 40 NOT NULL,
    waiting_since timestamp with time zone,
    result_summary text,
    result_data jsonb DEFAULT '{}'::jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    completed_at timestamp with time zone,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    object_id bigint NOT NULL,
    execution_job_id bigint,
    CONSTRAINT deep_research_runs_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'waiting_for_input'::text, 'completed'::text, 'failed'::text, 'cancelled'::text])))
);

CREATE SEQUENCE deep_research_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE deep_research_runs_id_seq OWNED BY deep_research_runs.id;

CREATE TABLE deleted_thread_references (
    reference_hash text NOT NULL,
    deleted_workstream_id bigint NOT NULL,
    deleted_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE dropbox_transfers (
    request_key text NOT NULL,
    job_id bigint,
    thread_id text,
    direction text NOT NULL,
    remote_path text NOT NULL,
    remote_id text,
    revision text,
    local_path text,
    markdown_path text,
    local_sha256 text,
    markdown_sha256 text,
    content_hash text,
    size_bytes bigint,
    status text DEFAULT 'pending'::text NOT NULL,
    scan_status text DEFAULT 'pending'::text NOT NULL,
    conversion_status text DEFAULT 'pending'::text NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    object_id bigint NOT NULL,
    CONSTRAINT dropbox_transfers_conversion_status_check CHECK ((conversion_status = ANY (ARRAY['pending'::text, 'ready'::text, 'failed'::text, 'not_applicable'::text]))),
    CONSTRAINT dropbox_transfers_direction_check CHECK ((direction = ANY (ARRAY['import'::text, 'export'::text]))),
    CONSTRAINT dropbox_transfers_scan_status_check CHECK ((scan_status = ANY (ARRAY['pending'::text, 'clean'::text, 'infected'::text, 'error'::text, 'skipped'::text]))),
    CONSTRAINT dropbox_transfers_size_bytes_check CHECK ((size_bytes >= 0)),
    CONSTRAINT dropbox_transfers_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'ready'::text, 'failed'::text, 'uncertain'::text])))
);

CREATE TABLE emails (
    id bigint NOT NULL,
    message_id text NOT NULL,
    in_reply_to text,
    references_header text[] DEFAULT ARRAY[]::text[],
    thread_id text NOT NULL,
    from_address text NOT NULL,
    to_addresses text[] DEFAULT ARRAY[]::text[] NOT NULL,
    cc_addresses text[] DEFAULT ARRAY[]::text[] NOT NULL,
    subject text,
    body_text text,
    body_html text,
    attachments jsonb DEFAULT '[]'::jsonb NOT NULL,
    received_at timestamp with time zone NOT NULL,
    downloaded_at timestamp with time zone DEFAULT now() NOT NULL,
    folder text DEFAULT 'INBOX'::text NOT NULL,
    is_actionable boolean DEFAULT false NOT NULL,
    embedding double precision[],
    embedding_model text,
    embedding_dimensions integer,
    embedding_updated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    workstream_id bigint NOT NULL
);

CREATE SEQUENCE emails_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE emails_id_seq OWNED BY emails.id;

CREATE TABLE jobs (
    id bigint NOT NULL,
    thread_id text NOT NULL,
    trigger_email_id bigint,
    status text DEFAULT 'queued'::text NOT NULL,
    priority integer DEFAULT 0 NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    max_attempts integer DEFAULT 3 NOT NULL,
    run_at timestamp with time zone DEFAULT now() NOT NULL,
    locked_at timestamp with time zone,
    locked_by text,
    has_new_context boolean DEFAULT false NOT NULL,
    task_summary text,
    embedding double precision[],
    embedding_model text,
    embedding_dimensions integer,
    embedding_updated_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    last_error text,
    trigger_request_id bigint,
    workstream_id bigint NOT NULL,
    object_id bigint NOT NULL,
    CONSTRAINT jobs_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'waiting'::text, 'completed'::text, 'failed'::text, 'needs_review'::text, 'cancelled'::text])))
);

CREATE SEQUENCE jobs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE jobs_id_seq OWNED BY jobs.id;

CREATE TABLE manual_events (
    id bigint NOT NULL,
    event_type text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE manual_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE manual_events_id_seq OWNED BY manual_events.id;

CREATE TABLE memory_events (
    id bigint NOT NULL,
    memory_id bigint,
    job_id bigint,
    actor text DEFAULT 'system'::text NOT NULL,
    event_type text NOT NULL,
    input_data jsonb DEFAULT '{}'::jsonb NOT NULL,
    output_data jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE memory_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE memory_events_id_seq OWNED BY memory_events.id;

CREATE TABLE model_presets (
    name text NOT NULL,
    model text NOT NULL,
    system_prompt text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE model_service_calls (
    id uuid NOT NULL,
    job_id bigint,
    preset jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    cost_usd numeric,
    usage jsonb,
    generation_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT model_service_calls_cost_usd_check CHECK ((cost_usd >= (0)::numeric)),
    CONSTRAINT model_service_calls_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'completed'::text, 'failed'::text, 'unknown'::text])))
);

CREATE TABLE outbound_email_logs (
    id bigint NOT NULL,
    job_id bigint,
    to_addresses text[] NOT NULL,
    cc_addresses text[] DEFAULT ARRAY[]::text[] NOT NULL,
    subject text NOT NULL,
    body_text text NOT NULL,
    in_reply_to text,
    attachments jsonb DEFAULT '[]'::jsonb NOT NULL,
    provider_message_id text,
    status text DEFAULT 'pending'::text NOT NULL,
    blocked_reason text,
    embedding double precision[],
    embedding_model text,
    embedding_dimensions integer,
    embedding_updated_at timestamp with time zone,
    sent_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    workstream_id bigint NOT NULL,
    CONSTRAINT outbound_email_logs_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'sent'::text, 'failed'::text, 'blocked'::text])))
);

CREATE SEQUENCE outbound_email_logs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE outbound_email_logs_id_seq OWNED BY outbound_email_logs.id;

CREATE TABLE processed_artifacts (
    id bigint NOT NULL,
    email_id bigint,
    thread_id text NOT NULL,
    source_type text NOT NULL,
    source_label text NOT NULL,
    source_uri text,
    original_filename text,
    content_type text,
    raw_path text,
    raw_sha256 text,
    raw_size_bytes bigint,
    scan_status text DEFAULT 'pending'::text NOT NULL,
    scan_engine text,
    scan_result text,
    conversion_status text DEFAULT 'pending'::text NOT NULL,
    markdown_path text,
    markdown_sha256 text,
    markdown_size_bytes bigint,
    error text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    request_id bigint,
    object_id bigint NOT NULL,
    CONSTRAINT processed_artifacts_conversion_status_check CHECK ((conversion_status = ANY (ARRAY['pending'::text, 'ready'::text, 'unsupported'::text, 'failed'::text, 'skipped'::text]))),
    CONSTRAINT processed_artifacts_scan_status_check CHECK ((scan_status = ANY (ARRAY['pending'::text, 'clean'::text, 'infected'::text, 'error'::text, 'skipped'::text, 'not_applicable'::text]))),
    CONSTRAINT processed_artifacts_source_type_check CHECK ((source_type = ANY (ARRAY['attachment'::text, 'youtube_url'::text])))
);

CREATE SEQUENCE processed_artifacts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE processed_artifacts_id_seq OWNED BY processed_artifacts.id;

CREATE TABLE provider_mutations (
    id bigint NOT NULL,
    provider_name text NOT NULL,
    resource_type text NOT NULL,
    resource_key text NOT NULL,
    operation text NOT NULL,
    idempotency_key text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    before_state jsonb DEFAULT '{}'::jsonb NOT NULL,
    desired_state jsonb DEFAULT '{}'::jsonb NOT NULL,
    result_state jsonb DEFAULT '{}'::jsonb NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT provider_mutations_operation_check CHECK ((operation = ANY (ARRAY['CREATE'::text, 'UPDATE'::text, 'DELETE'::text]))),
    CONSTRAINT provider_mutations_resource_type_check CHECK ((resource_type = ANY (ARRAY['calendar_event'::text, 'zoom_meeting'::text]))),
    CONSTRAINT provider_mutations_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'uncertain'::text, 'completed'::text, 'failed'::text])))
);

CREATE SEQUENCE provider_mutations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE provider_mutations_id_seq OWNED BY provider_mutations.id;

CREATE TABLE reminders (
    id bigint NOT NULL,
    title text NOT NULL,
    task text NOT NULL,
    run_at timestamp with time zone NOT NULL,
    status text DEFAULT 'scheduled'::text NOT NULL,
    priority integer DEFAULT 0 NOT NULL,
    recurrence_unit text,
    recurrence_interval integer,
    recurrence_anchor_day integer,
    created_by text DEFAULT 'agent'::text NOT NULL,
    created_by_job_id bigint,
    job_id bigint,
    embedding double precision[],
    embedding_model text,
    embedding_dimensions integer,
    embedding_updated_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    queued_at timestamp with time zone,
    completed_at timestamp with time zone,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    object_id bigint NOT NULL,
    CONSTRAINT reminders_recurrence_check CHECK ((((recurrence_unit IS NULL) AND (recurrence_interval IS NULL) AND (recurrence_anchor_day IS NULL)) OR ((recurrence_unit = ANY (ARRAY['hour'::text, 'day'::text, 'week'::text, 'month'::text])) AND (recurrence_interval IS NOT NULL) AND (recurrence_interval > 0) AND ((recurrence_anchor_day IS NULL) OR ((recurrence_anchor_day >= 1) AND (recurrence_anchor_day <= 31)))))),
    CONSTRAINT reminders_status_check CHECK ((status = ANY (ARRAY['scheduled'::text, 'queued'::text, 'completed'::text, 'failed'::text, 'cancelled'::text])))
);

CREATE SEQUENCE reminders_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE reminders_id_seq OWNED BY reminders.id;

CREATE TABLE request_messages (
    id bigint NOT NULL,
    channel text NOT NULL,
    external_id text NOT NULL,
    conversation_id text NOT NULL,
    sender_id text NOT NULL,
    thread_id text NOT NULL,
    email_id bigint,
    body_text text DEFAULT ''::text NOT NULL,
    attachments jsonb DEFAULT '[]'::jsonb NOT NULL,
    reply_route jsonb DEFAULT '{}'::jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'ready'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    locked_at timestamp with time zone,
    last_error text,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    workstream_id bigint NOT NULL,
    CONSTRAINT request_messages_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'processing'::text, 'ready'::text, 'failed'::text])))
);

CREATE SEQUENCE request_messages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE request_messages_id_seq OWNED BY request_messages.id;

CREATE TABLE runtime_state (
    key text NOT NULL,
    value jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE schema_migrations (
    filename text NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE supervisor_instructions (
    id bigint NOT NULL,
    job_id bigint NOT NULL,
    instruction text NOT NULL,
    created_by text DEFAULT 'supervisor'::text NOT NULL,
    consumed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE supervisor_instructions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE supervisor_instructions_id_seq OWNED BY supervisor_instructions.id;

CREATE TABLE task_logs (
    id bigint NOT NULL,
    job_id bigint NOT NULL,
    sequence integer NOT NULL,
    event_type text NOT NULL,
    tool_name text,
    tool_action text,
    input_data jsonb,
    output_data jsonb,
    tokens_used jsonb,
    duration_ms integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT task_logs_event_type_check CHECK ((event_type = ANY (ARRAY['llm_request'::text, 'llm_response'::text, 'model_service_response'::text, 'tool_call'::text, 'tool_result'::text, 'error'::text, 'timeout'::text, 'status_change'::text, 'supervisor_note'::text])))
);

CREATE SEQUENCE task_logs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE task_logs_id_seq OWNED BY task_logs.id;

CREATE TABLE telegram_workstream_selections (
    bot_id text NOT NULL,
    sender_id text NOT NULL,
    workstream_id bigint,
    unavailable_workstream_id bigint,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT telegram_workstream_selections_check CHECK (((workstream_id IS NULL) OR (unavailable_workstream_id IS NULL)))
);

CREATE TABLE thread_summaries (
    thread_id text NOT NULL,
    summary text NOT NULL,
    email_count integer NOT NULL,
    last_email_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    workstream_id bigint NOT NULL
);

CREATE TABLE workspace_document_conversions (
    id bigint NOT NULL,
    original_relative_path text NOT NULL,
    markdown_relative_path text,
    archived_relative_path text,
    original_sha256 text,
    markdown_sha256 text,
    status text DEFAULT 'pending'::text NOT NULL,
    source text DEFAULT 'workspace'::text NOT NULL,
    error text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT workspace_document_conversions_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'ready'::text, 'failed'::text, 'skipped'::text])))
);

CREATE SEQUENCE workspace_document_conversions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE workspace_document_conversions_id_seq OWNED BY workspace_document_conversions.id;

CREATE TABLE workspace_file_chunks (
    id bigint NOT NULL,
    file_id bigint NOT NULL,
    chunk_index integer NOT NULL,
    content text NOT NULL,
    start_line integer,
    end_line integer,
    content_sha256 text DEFAULT ''::text NOT NULL,
    embedding double precision[],
    embedding_model text,
    embedding_dimensions integer,
    embedding_updated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE workspace_file_chunks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE workspace_file_chunks_id_seq OWNED BY workspace_file_chunks.id;

CREATE TABLE workspace_files (
    id bigint NOT NULL,
    relative_path text NOT NULL,
    size_bytes bigint DEFAULT 0 NOT NULL,
    mtime_ns text DEFAULT ''::text NOT NULL,
    content_sha256 text DEFAULT ''::text NOT NULL,
    mime_type text,
    extension text DEFAULT ''::text NOT NULL,
    index_status text DEFAULT 'pending'::text NOT NULL,
    error text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    indexed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT workspace_files_index_status_check CHECK ((index_status = ANY (ARRAY['pending'::text, 'indexed'::text, 'embedding_failed'::text, 'unsupported'::text, 'error'::text, 'deleted'::text, 'superseded'::text])))
);

CREATE SEQUENCE workspace_files_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE workspace_files_id_seq OWNED BY workspace_files.id;

CREATE TABLE workstream_attachments (
    workstream_id bigint NOT NULL,
    object_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE workstream_context_events (
    id bigint NOT NULL,
    workstream_id bigint NOT NULL,
    object_event_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE workstream_context_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE workstream_context_events_id_seq OWNED BY workstream_context_events.id;

CREATE TABLE workstream_deletions (
    id bigint NOT NULL,
    workstream_ids bigint[] NOT NULL,
    object_ids bigint[] NOT NULL,
    mode text NOT NULL,
    manifest jsonb NOT NULL,
    completed_steps jsonb DEFAULT '[]'::jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT workstream_deletions_mode_check CHECK ((mode = ANY (ARRAY['preserve_shared'::text, 'delete_objects'::text, 'connected'::text]))),
    CONSTRAINT workstream_deletions_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'failed'::text, 'completed'::text])))
);

CREATE SEQUENCE workstream_deletions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE workstream_deletions_id_seq OWNED BY workstream_deletions.id;

CREATE TABLE workstream_files (
    id bigint NOT NULL,
    path text NOT NULL,
    sha256 text,
    source_job_id bigint,
    object_id bigint NOT NULL
);

CREATE SEQUENCE workstream_files_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE workstream_files_id_seq OWNED BY workstream_files.id;

CREATE TABLE workstream_object_events (
    id bigint NOT NULL,
    object_id bigint NOT NULL,
    object_kind text NOT NULL,
    object_key text NOT NULL,
    origin_workstream_id bigint NOT NULL,
    actor_job_id bigint,
    actor_workstream_id bigint,
    event_type text NOT NULL,
    changes jsonb NOT NULL,
    reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT workstream_object_events_event_type_check CHECK ((event_type = ANY (ARRAY['INSERT'::text, 'UPDATE'::text, 'DELETE'::text])))
);

CREATE SEQUENCE workstream_object_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE workstream_object_events_id_seq OWNED BY workstream_object_events.id;

CREATE TABLE workstream_objects (
    id bigint NOT NULL,
    kind text NOT NULL,
    object_key text NOT NULL,
    origin_workstream_id bigint NOT NULL,
    deleting boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    version bigint DEFAULT 1 NOT NULL
);

CREATE SEQUENCE workstream_objects_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE workstream_objects_id_seq OWNED BY workstream_objects.id;

CREATE TABLE workstream_task_notifications (
    id bigint NOT NULL,
    workstream_id bigint NOT NULL,
    source_job_id bigint NOT NULL,
    source_thread_id text NOT NULL,
    notification_key text NOT NULL,
    notification_type text NOT NULL,
    summary text NOT NULL,
    content text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT workstream_task_notifications_notification_type_check CHECK ((notification_type = ANY (ARRAY['input_requested'::text, 'completed'::text])))
);

CREATE SEQUENCE workstream_task_notifications_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE workstream_task_notifications_id_seq OWNED BY workstream_task_notifications.id;

CREATE TABLE workstream_threads (
    thread_id text NOT NULL,
    workstream_id bigint NOT NULL,
    role text DEFAULT 'main'::text NOT NULL,
    CONSTRAINT workstream_threads_role_check CHECK ((role = ANY (ARRAY['main'::text, 'system'::text])))
);

CREATE TABLE workstreams (
    id bigint NOT NULL,
    parent_id bigint,
    title text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    main_thread_id text NOT NULL,
    origin_channel text DEFAULT 'local'::text NOT NULL,
    name_status text DEFAULT 'named'::text NOT NULL,
    name_attempts integer DEFAULT 0 NOT NULL,
    name_attempted_at timestamp with time zone,
    telegram_message_count integer DEFAULT 0 NOT NULL,
    CONSTRAINT workstreams_check CHECK (((parent_id IS NULL) OR (parent_id <> id))),
    CONSTRAINT workstreams_name_status_check CHECK ((name_status = ANY (ARRAY['named'::text, 'temporary'::text, 'pending'::text, 'naming'::text, 'failed'::text]))),
    CONSTRAINT workstreams_origin_channel_check CHECK ((origin_channel = ANY (ARRAY['telegram'::text, 'email'::text, 'local'::text]))),
    CONSTRAINT workstreams_status_check CHECK ((status = ANY (ARRAY['active'::text, 'deleting'::text])))
);

CREATE SEQUENCE workstreams_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE workstreams_id_seq OWNED BY workstreams.id;

CREATE TABLE zoom_managed_meetings (
    id bigint NOT NULL,
    request_key text NOT NULL,
    request_payload jsonb NOT NULL,
    provider_meeting_id text,
    title text NOT NULL,
    starts_at timestamp with time zone NOT NULL,
    duration_minutes integer NOT NULL,
    join_url text,
    status text DEFAULT 'creating'::text NOT NULL,
    calendar_event_id text,
    created_by_job_id bigint,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    object_id bigint NOT NULL,
    CONSTRAINT zoom_managed_meetings_duration_minutes_check CHECK (((duration_minutes >= 1) AND (duration_minutes <= 1440))),
    CONSTRAINT zoom_managed_meetings_status_check CHECK ((status = ANY (ARRAY['creating'::text, 'active'::text, 'failed'::text, 'uncertain'::text, 'deleted'::text])))
);

CREATE SEQUENCE zoom_managed_meetings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE zoom_managed_meetings_id_seq OWNED BY zoom_managed_meetings.id;

ALTER TABLE ONLY agent_checkpoints ALTER COLUMN id SET DEFAULT nextval('agent_checkpoints_id_seq'::regclass);

ALTER TABLE ONLY agent_memories ALTER COLUMN id SET DEFAULT nextval('agent_memories_id_seq'::regclass);

ALTER TABLE ONLY calendar_event_audit ALTER COLUMN id SET DEFAULT nextval('calendar_event_audit_id_seq'::regclass);

ALTER TABLE ONLY channel_deliveries ALTER COLUMN id SET DEFAULT nextval('channel_deliveries_id_seq'::regclass);

ALTER TABLE ONLY contacts ALTER COLUMN id SET DEFAULT nextval('contacts_id_seq'::regclass);

ALTER TABLE ONLY deep_research_events ALTER COLUMN id SET DEFAULT nextval('deep_research_events_id_seq'::regclass);

ALTER TABLE ONLY deep_research_runs ALTER COLUMN id SET DEFAULT nextval('deep_research_runs_id_seq'::regclass);

ALTER TABLE ONLY emails ALTER COLUMN id SET DEFAULT nextval('emails_id_seq'::regclass);

ALTER TABLE ONLY jobs ALTER COLUMN id SET DEFAULT nextval('jobs_id_seq'::regclass);

ALTER TABLE ONLY manual_events ALTER COLUMN id SET DEFAULT nextval('manual_events_id_seq'::regclass);

ALTER TABLE ONLY memory_events ALTER COLUMN id SET DEFAULT nextval('memory_events_id_seq'::regclass);

ALTER TABLE ONLY outbound_email_logs ALTER COLUMN id SET DEFAULT nextval('outbound_email_logs_id_seq'::regclass);

ALTER TABLE ONLY processed_artifacts ALTER COLUMN id SET DEFAULT nextval('processed_artifacts_id_seq'::regclass);

ALTER TABLE ONLY provider_mutations ALTER COLUMN id SET DEFAULT nextval('provider_mutations_id_seq'::regclass);

ALTER TABLE ONLY reminders ALTER COLUMN id SET DEFAULT nextval('reminders_id_seq'::regclass);

ALTER TABLE ONLY request_messages ALTER COLUMN id SET DEFAULT nextval('request_messages_id_seq'::regclass);

ALTER TABLE ONLY supervisor_instructions ALTER COLUMN id SET DEFAULT nextval('supervisor_instructions_id_seq'::regclass);

ALTER TABLE ONLY task_logs ALTER COLUMN id SET DEFAULT nextval('task_logs_id_seq'::regclass);

ALTER TABLE ONLY workspace_document_conversions ALTER COLUMN id SET DEFAULT nextval('workspace_document_conversions_id_seq'::regclass);

ALTER TABLE ONLY workspace_file_chunks ALTER COLUMN id SET DEFAULT nextval('workspace_file_chunks_id_seq'::regclass);

ALTER TABLE ONLY workspace_files ALTER COLUMN id SET DEFAULT nextval('workspace_files_id_seq'::regclass);

ALTER TABLE ONLY workstream_context_events ALTER COLUMN id SET DEFAULT nextval('workstream_context_events_id_seq'::regclass);

ALTER TABLE ONLY workstream_deletions ALTER COLUMN id SET DEFAULT nextval('workstream_deletions_id_seq'::regclass);

ALTER TABLE ONLY workstream_files ALTER COLUMN id SET DEFAULT nextval('workstream_files_id_seq'::regclass);

ALTER TABLE ONLY workstream_object_events ALTER COLUMN id SET DEFAULT nextval('workstream_object_events_id_seq'::regclass);

ALTER TABLE ONLY workstream_objects ALTER COLUMN id SET DEFAULT nextval('workstream_objects_id_seq'::regclass);

ALTER TABLE ONLY workstream_task_notifications ALTER COLUMN id SET DEFAULT nextval('workstream_task_notifications_id_seq'::regclass);

ALTER TABLE ONLY workstreams ALTER COLUMN id SET DEFAULT nextval('workstreams_id_seq'::regclass);

ALTER TABLE ONLY zoom_managed_meetings ALTER COLUMN id SET DEFAULT nextval('zoom_managed_meetings_id_seq'::regclass);

ALTER TABLE ONLY agent_checkpoints
    ADD CONSTRAINT agent_checkpoints_pkey PRIMARY KEY (id);

ALTER TABLE ONLY agent_memories
    ADD CONSTRAINT agent_memories_object_id_key UNIQUE (object_id);

ALTER TABLE ONLY agent_memories
    ADD CONSTRAINT agent_memories_pkey PRIMARY KEY (id);

ALTER TABLE ONLY calendar_event_audit
    ADD CONSTRAINT calendar_event_audit_pkey PRIMARY KEY (id);

ALTER TABLE ONLY calendar_managed_events
    ADD CONSTRAINT calendar_managed_events_object_id_key UNIQUE (object_id);

ALTER TABLE ONLY calendar_managed_events
    ADD CONSTRAINT calendar_managed_events_pkey PRIMARY KEY (assistant_id);

ALTER TABLE ONLY channel_deliveries
    ADD CONSTRAINT channel_deliveries_pkey PRIMARY KEY (id);

ALTER TABLE ONLY channel_deliveries
    ADD CONSTRAINT channel_deliveries_request_key_key UNIQUE (request_key);

ALTER TABLE ONLY contacts
    ADD CONSTRAINT contacts_object_id_key UNIQUE (object_id);

ALTER TABLE ONLY contacts
    ADD CONSTRAINT contacts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY deep_research_events
    ADD CONSTRAINT deep_research_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY deep_research_events
    ADD CONSTRAINT deep_research_events_run_id_sequence_key UNIQUE (run_id, sequence);

ALTER TABLE ONLY deep_research_runs
    ADD CONSTRAINT deep_research_runs_execution_job_id_key UNIQUE (execution_job_id);

ALTER TABLE ONLY deep_research_runs
    ADD CONSTRAINT deep_research_runs_object_id_key UNIQUE (object_id);

ALTER TABLE ONLY deep_research_runs
    ADD CONSTRAINT deep_research_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY deleted_thread_references
    ADD CONSTRAINT deleted_thread_references_pkey PRIMARY KEY (reference_hash);

ALTER TABLE ONLY dropbox_transfers
    ADD CONSTRAINT dropbox_transfers_object_id_key UNIQUE (object_id);

ALTER TABLE ONLY dropbox_transfers
    ADD CONSTRAINT dropbox_transfers_pkey PRIMARY KEY (request_key);

ALTER TABLE ONLY emails
    ADD CONSTRAINT emails_message_id_key UNIQUE (message_id);

ALTER TABLE ONLY emails
    ADD CONSTRAINT emails_pkey PRIMARY KEY (id);

ALTER TABLE ONLY jobs
    ADD CONSTRAINT jobs_object_id_key UNIQUE (object_id);

ALTER TABLE ONLY jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY manual_events
    ADD CONSTRAINT manual_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY memory_events
    ADD CONSTRAINT memory_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY model_presets
    ADD CONSTRAINT model_presets_pkey PRIMARY KEY (name);

ALTER TABLE ONLY model_service_calls
    ADD CONSTRAINT model_service_calls_pkey PRIMARY KEY (id);

ALTER TABLE ONLY outbound_email_logs
    ADD CONSTRAINT outbound_email_logs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY processed_artifacts
    ADD CONSTRAINT processed_artifacts_object_id_key UNIQUE (object_id);

ALTER TABLE ONLY processed_artifacts
    ADD CONSTRAINT processed_artifacts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY provider_mutations
    ADD CONSTRAINT provider_mutations_idempotency_key_key UNIQUE (idempotency_key);

ALTER TABLE ONLY provider_mutations
    ADD CONSTRAINT provider_mutations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY reminders
    ADD CONSTRAINT reminders_object_id_key UNIQUE (object_id);

ALTER TABLE ONLY reminders
    ADD CONSTRAINT reminders_pkey PRIMARY KEY (id);

ALTER TABLE ONLY request_messages
    ADD CONSTRAINT request_messages_channel_external_id_key UNIQUE (channel, external_id);

ALTER TABLE ONLY request_messages
    ADD CONSTRAINT request_messages_email_id_key UNIQUE (email_id);

ALTER TABLE ONLY request_messages
    ADD CONSTRAINT request_messages_pkey PRIMARY KEY (id);

ALTER TABLE ONLY runtime_state
    ADD CONSTRAINT runtime_state_pkey PRIMARY KEY (key);

ALTER TABLE ONLY schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (filename);

ALTER TABLE ONLY supervisor_instructions
    ADD CONSTRAINT supervisor_instructions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY task_logs
    ADD CONSTRAINT task_logs_job_id_sequence_key UNIQUE (job_id, sequence);

ALTER TABLE ONLY task_logs
    ADD CONSTRAINT task_logs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY telegram_workstream_selections
    ADD CONSTRAINT telegram_workstream_selections_pkey PRIMARY KEY (bot_id, sender_id);

ALTER TABLE ONLY thread_summaries
    ADD CONSTRAINT thread_summaries_pkey PRIMARY KEY (thread_id);

ALTER TABLE ONLY workspace_document_conversions
    ADD CONSTRAINT workspace_document_conversions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY workspace_file_chunks
    ADD CONSTRAINT workspace_file_chunks_file_id_chunk_index_key UNIQUE (file_id, chunk_index);

ALTER TABLE ONLY workspace_file_chunks
    ADD CONSTRAINT workspace_file_chunks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY workspace_files
    ADD CONSTRAINT workspace_files_pkey PRIMARY KEY (id);

ALTER TABLE ONLY workspace_files
    ADD CONSTRAINT workspace_files_relative_path_key UNIQUE (relative_path);

ALTER TABLE ONLY workstream_attachments
    ADD CONSTRAINT workstream_attachments_pkey PRIMARY KEY (workstream_id, object_id);

ALTER TABLE ONLY workstream_context_events
    ADD CONSTRAINT workstream_context_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY workstream_context_events
    ADD CONSTRAINT workstream_context_events_workstream_id_object_event_id_key UNIQUE (workstream_id, object_event_id);

ALTER TABLE ONLY workstream_deletions
    ADD CONSTRAINT workstream_deletions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY workstream_files
    ADD CONSTRAINT workstream_files_object_id_key UNIQUE (object_id);

ALTER TABLE ONLY workstream_files
    ADD CONSTRAINT workstream_files_path_key UNIQUE (path);

ALTER TABLE ONLY workstream_files
    ADD CONSTRAINT workstream_files_pkey PRIMARY KEY (id);

ALTER TABLE ONLY workstream_object_events
    ADD CONSTRAINT workstream_object_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY workstream_objects
    ADD CONSTRAINT workstream_objects_kind_object_key_key UNIQUE (kind, object_key);

ALTER TABLE ONLY workstream_objects
    ADD CONSTRAINT workstream_objects_pkey PRIMARY KEY (id);

ALTER TABLE ONLY workstream_task_notifications
    ADD CONSTRAINT workstream_task_notifications_pkey PRIMARY KEY (id);

ALTER TABLE ONLY workstream_task_notifications
    ADD CONSTRAINT workstream_task_notifications_source_job_id_notification_ke_key UNIQUE (source_job_id, notification_key);

ALTER TABLE ONLY workstream_threads
    ADD CONSTRAINT workstream_threads_pkey PRIMARY KEY (thread_id);

ALTER TABLE ONLY workstreams
    ADD CONSTRAINT workstreams_pkey PRIMARY KEY (id);

ALTER TABLE ONLY zoom_managed_meetings
    ADD CONSTRAINT zoom_managed_meetings_object_id_key UNIQUE (object_id);

ALTER TABLE ONLY zoom_managed_meetings
    ADD CONSTRAINT zoom_managed_meetings_pkey PRIMARY KEY (id);

ALTER TABLE ONLY zoom_managed_meetings
    ADD CONSTRAINT zoom_managed_meetings_provider_meeting_id_key UNIQUE (provider_meeting_id);

ALTER TABLE ONLY zoom_managed_meetings
    ADD CONSTRAINT zoom_managed_meetings_request_key_key UNIQUE (request_key);

CREATE INDEX agent_checkpoints_job_created_idx ON agent_checkpoints USING btree (job_id, created_at DESC);

CREATE INDEX agent_memories_created_idx ON agent_memories USING btree (created_at DESC);

CREATE INDEX agent_memories_pinned_idx ON agent_memories USING btree (pinned, importance DESC);

CREATE INDEX agent_memories_scope_kind_idx ON agent_memories USING btree (scope, kind);

CREATE INDEX agent_memories_tags_idx ON agent_memories USING gin (tags);

CREATE INDEX calendar_event_audit_event_created_idx ON calendar_event_audit USING btree (assistant_id, created_at DESC);

CREATE INDEX calendar_event_audit_job_created_idx ON calendar_event_audit USING btree (job_id, created_at DESC);

CREATE INDEX calendar_managed_events_job_idx ON calendar_managed_events USING btree (created_by_job_id, updated_by_job_id);

CREATE INDEX calendar_managed_events_provider_idx ON calendar_managed_events USING btree (provider_name, provider_event_id);

CREATE INDEX calendar_managed_events_status_starts_idx ON calendar_managed_events USING btree (status, starts_at);

CREATE INDEX channel_deliveries_job_idx ON channel_deliveries USING btree (job_id, status);

CREATE INDEX channel_deliveries_reply_idx ON channel_deliveries USING btree (channel, destination);

CREATE INDEX channel_deliveries_workstream_id_idx ON channel_deliveries USING btree (workstream_id);

CREATE INDEX contacts_company_idx ON contacts USING btree (company);

CREATE UNIQUE INDEX contacts_email_unique_idx ON contacts USING btree (lower(email_address)) WHERE (email_address <> ''::text);

CREATE INDEX contacts_name_idx ON contacts USING btree (last_name, first_name);

CREATE INDEX contacts_updated_idx ON contacts USING btree (updated_at DESC);

CREATE INDEX deep_research_events_run_created_idx ON deep_research_events USING btree (run_id, created_at);

CREATE INDEX deep_research_events_run_sequence_idx ON deep_research_events USING btree (run_id, sequence);

CREATE INDEX deep_research_runs_original_job_idx ON deep_research_runs USING btree (original_job_id);

CREATE INDEX deep_research_runs_status_run_at_idx ON deep_research_runs USING btree (status, run_at);

CREATE INDEX dropbox_transfers_job_idx ON dropbox_transfers USING btree (job_id);

CREATE INDEX emails_from_address_idx ON emails USING btree (from_address);

CREATE INDEX emails_received_idx ON emails USING btree (received_at);

CREATE INDEX emails_thread_received_idx ON emails USING btree (thread_id, received_at);

CREATE INDEX emails_workstream_id_idx ON emails USING btree (workstream_id);

CREATE UNIQUE INDEX jobs_one_open_per_thread_idx ON jobs USING btree (thread_id) WHERE (status = ANY (ARRAY['queued'::text, 'running'::text, 'waiting'::text, 'needs_review'::text]));

CREATE INDEX jobs_status_run_at_idx ON jobs USING btree (status, run_at);

CREATE INDEX jobs_thread_id_idx ON jobs USING btree (thread_id);

CREATE INDEX jobs_trigger_email_idx ON jobs USING btree (trigger_email_id);

CREATE INDEX jobs_workstream_id_idx ON jobs USING btree (workstream_id);

CREATE INDEX manual_events_created_idx ON manual_events USING btree (created_at);

CREATE INDEX memory_events_job_created_idx ON memory_events USING btree (job_id, created_at DESC);

CREATE INDEX memory_events_memory_created_idx ON memory_events USING btree (memory_id, created_at DESC);

CREATE INDEX model_service_calls_created_at_idx ON model_service_calls USING btree (created_at);

CREATE INDEX outbound_email_logs_created_idx ON outbound_email_logs USING btree (created_at);

CREATE INDEX outbound_email_logs_job_idx ON outbound_email_logs USING btree (job_id);

CREATE INDEX outbound_email_logs_provider_message_idx ON outbound_email_logs USING btree (provider_message_id);

CREATE INDEX outbound_email_logs_workstream_id_idx ON outbound_email_logs USING btree (workstream_id);

CREATE INDEX processed_artifacts_email_idx ON processed_artifacts USING btree (email_id, id);

CREATE INDEX processed_artifacts_request_idx ON processed_artifacts USING btree (request_id, id);

CREATE INDEX processed_artifacts_status_idx ON processed_artifacts USING btree (conversion_status, scan_status);

CREATE INDEX processed_artifacts_thread_idx ON processed_artifacts USING btree (thread_id, id);

CREATE INDEX provider_mutations_resource_idx ON provider_mutations USING btree (resource_type, resource_key, created_at DESC);

CREATE INDEX reminders_job_id_idx ON reminders USING btree (job_id);

CREATE INDEX reminders_status_run_at_idx ON reminders USING btree (status, run_at);

CREATE INDEX request_messages_pending_idx ON request_messages USING btree (status, available_at);

CREATE INDEX request_messages_thread_idx ON request_messages USING btree (thread_id, received_at, id);

CREATE INDEX request_messages_workstream_id_idx ON request_messages USING btree (workstream_id);

CREATE INDEX supervisor_instructions_pending_idx ON supervisor_instructions USING btree (job_id, created_at) WHERE (consumed_at IS NULL);

CREATE INDEX task_logs_job_created_idx ON task_logs USING btree (job_id, created_at);

CREATE INDEX task_logs_job_sequence_idx ON task_logs USING btree (job_id, sequence);

CREATE INDEX thread_summaries_workstream_id_idx ON thread_summaries USING btree (workstream_id);

CREATE INDEX workspace_document_conversions_markdown_idx ON workspace_document_conversions USING btree (markdown_relative_path, created_at DESC);

CREATE INDEX workspace_document_conversions_original_idx ON workspace_document_conversions USING btree (original_relative_path, created_at DESC);

CREATE INDEX workspace_file_chunks_file_idx ON workspace_file_chunks USING btree (file_id, chunk_index);

CREATE INDEX workspace_files_extension_idx ON workspace_files USING btree (extension);

CREATE INDEX workspace_files_status_idx ON workspace_files USING btree (index_status, updated_at DESC);

CREATE INDEX workstream_attachments_object_idx ON workstream_attachments USING btree (object_id);

CREATE INDEX workstream_context_events_stream_idx ON workstream_context_events USING btree (workstream_id, created_at DESC);

CREATE INDEX workstream_object_events_actor_idx ON workstream_object_events USING btree (actor_workstream_id, created_at DESC);

CREATE INDEX workstream_object_events_object_idx ON workstream_object_events USING btree (object_id, created_at DESC);

CREATE INDEX workstream_task_notifications_stream_idx ON workstream_task_notifications USING btree (workstream_id, created_at DESC);

CREATE INDEX workstream_threads_owner_idx ON workstream_threads USING btree (workstream_id);

CREATE UNIQUE INDEX workstreams_main_thread_idx ON workstreams USING btree (main_thread_id);

CREATE INDEX workstreams_parent_idx ON workstreams USING btree (parent_id);

CREATE TRIGGER agent_memories_set_updated_at BEFORE UPDATE ON agent_memories FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER artifacts_request_link BEFORE INSERT ON processed_artifacts FOR EACH ROW EXECUTE FUNCTION link_email_artifact_request();

CREATE TRIGGER attachment_guard BEFORE INSERT OR UPDATE ON workstream_attachments FOR EACH ROW EXECUTE FUNCTION workstream_attachment_guard();

CREATE TRIGGER calendar_managed_events_set_updated_at BEFORE UPDATE ON calendar_managed_events FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER confirmed_deletion BEFORE DELETE ON workstreams FOR EACH ROW EXECUTE FUNCTION workstream_delete_guard();

CREATE TRIGGER contacts_set_updated_at BEFORE UPDATE ON contacts FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER deep_research_runs_set_updated_at BEFORE UPDATE ON deep_research_runs FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER emails_request_mirror AFTER INSERT OR UPDATE OF body_text, attachments, thread_id ON emails FOR EACH ROW EXECUTE FUNCTION mirror_email_request();

CREATE TRIGGER immutable_origin BEFORE UPDATE ON workstream_objects FOR EACH ROW EXECUTE FUNCTION workstream_immutable_origin();

CREATE TRIGGER immutable_parent BEFORE INSERT OR UPDATE ON workstreams FOR EACH ROW EXECUTE FUNCTION workstream_parent_guard();

CREATE TRIGGER jobs_set_updated_at BEFORE UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER processed_artifacts_set_updated_at BEFORE UPDATE ON processed_artifacts FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER reminders_set_updated_at BEFORE UPDATE ON reminders FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER runtime_state_set_updated_at BEFORE UPDATE ON runtime_state FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER telegram_workstream_message_count AFTER INSERT ON request_messages FOR EACH ROW EXECUTE FUNCTION workstream_count_telegram_message();

CREATE TRIGGER thread_summaries_set_updated_at BEFORE UPDATE ON thread_summaries FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER workspace_document_conversions_set_updated_at BEFORE UPDATE ON workspace_document_conversions FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER workspace_files_set_updated_at BEFORE UPDATE ON workspace_files FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER workstream_delete_pending BEFORE DELETE ON agent_memories FOR EACH ROW EXECUTE FUNCTION workstream_object_delete_guard();

CREATE TRIGGER workstream_delete_pending BEFORE DELETE ON calendar_managed_events FOR EACH ROW EXECUTE FUNCTION workstream_object_delete_guard();

CREATE TRIGGER workstream_delete_pending BEFORE DELETE ON contacts FOR EACH ROW EXECUTE FUNCTION workstream_object_delete_guard();

CREATE TRIGGER workstream_delete_pending BEFORE DELETE ON deep_research_runs FOR EACH ROW EXECUTE FUNCTION workstream_object_delete_guard();

CREATE TRIGGER workstream_delete_pending BEFORE DELETE ON dropbox_transfers FOR EACH ROW EXECUTE FUNCTION workstream_object_delete_guard();

CREATE TRIGGER workstream_delete_pending BEFORE DELETE ON jobs FOR EACH ROW EXECUTE FUNCTION workstream_object_delete_guard();

CREATE TRIGGER workstream_delete_pending BEFORE DELETE ON processed_artifacts FOR EACH ROW EXECUTE FUNCTION workstream_object_delete_guard();

CREATE TRIGGER workstream_delete_pending BEFORE DELETE ON reminders FOR EACH ROW EXECUTE FUNCTION workstream_object_delete_guard();

CREATE TRIGGER workstream_delete_pending BEFORE DELETE ON workstream_files FOR EACH ROW EXECUTE FUNCTION workstream_object_delete_guard();

CREATE TRIGGER workstream_delete_pending BEFORE DELETE ON zoom_managed_meetings FOR EACH ROW EXECUTE FUNCTION workstream_object_delete_guard();

CREATE TRIGGER workstream_insert_cleanup AFTER INSERT ON agent_memories FOR EACH STATEMENT EXECUTE FUNCTION workstream_remove_unmaterialized_objects('memory');

CREATE TRIGGER workstream_insert_cleanup AFTER INSERT ON calendar_managed_events FOR EACH STATEMENT EXECUTE FUNCTION workstream_remove_unmaterialized_objects('calendar_event');

CREATE TRIGGER workstream_insert_cleanup AFTER INSERT ON contacts FOR EACH STATEMENT EXECUTE FUNCTION workstream_remove_unmaterialized_objects('contact');

CREATE TRIGGER workstream_insert_cleanup AFTER INSERT ON deep_research_runs FOR EACH STATEMENT EXECUTE FUNCTION workstream_remove_unmaterialized_objects('research');

CREATE TRIGGER workstream_insert_cleanup AFTER INSERT ON dropbox_transfers FOR EACH STATEMENT EXECUTE FUNCTION workstream_remove_unmaterialized_objects('dropbox_transfer');

CREATE TRIGGER workstream_insert_cleanup AFTER INSERT ON jobs FOR EACH STATEMENT EXECUTE FUNCTION workstream_remove_unmaterialized_objects('job');

CREATE TRIGGER workstream_insert_cleanup AFTER INSERT ON processed_artifacts FOR EACH STATEMENT EXECUTE FUNCTION workstream_remove_unmaterialized_objects('artifact');

CREATE TRIGGER workstream_insert_cleanup AFTER INSERT ON reminders FOR EACH STATEMENT EXECUTE FUNCTION workstream_remove_unmaterialized_objects('reminder');

CREATE TRIGGER workstream_insert_cleanup AFTER INSERT ON workstream_files FOR EACH STATEMENT EXECUTE FUNCTION workstream_remove_unmaterialized_objects('file');

CREATE TRIGGER workstream_insert_cleanup AFTER INSERT ON zoom_managed_meetings FOR EACH STATEMENT EXECUTE FUNCTION workstream_remove_unmaterialized_objects('zoom_meeting');

CREATE TRIGGER workstream_object_audit AFTER INSERT OR DELETE OR UPDATE ON agent_memories FOR EACH ROW EXECUTE FUNCTION workstream_record_object_change();

CREATE TRIGGER workstream_object_audit AFTER INSERT OR DELETE OR UPDATE ON calendar_managed_events FOR EACH ROW EXECUTE FUNCTION workstream_record_object_change();

CREATE TRIGGER workstream_object_audit AFTER INSERT OR DELETE OR UPDATE ON contacts FOR EACH ROW EXECUTE FUNCTION workstream_record_object_change();

CREATE TRIGGER workstream_object_audit AFTER INSERT OR DELETE OR UPDATE ON deep_research_runs FOR EACH ROW EXECUTE FUNCTION workstream_record_object_change();

CREATE TRIGGER workstream_object_audit AFTER INSERT OR DELETE OR UPDATE ON dropbox_transfers FOR EACH ROW EXECUTE FUNCTION workstream_record_object_change();

CREATE TRIGGER workstream_object_audit AFTER INSERT OR DELETE OR UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION workstream_record_object_change();

CREATE TRIGGER workstream_object_audit AFTER INSERT OR DELETE OR UPDATE ON processed_artifacts FOR EACH ROW EXECUTE FUNCTION workstream_record_object_change();

CREATE TRIGGER workstream_object_audit AFTER INSERT OR DELETE OR UPDATE ON reminders FOR EACH ROW EXECUTE FUNCTION workstream_record_object_change();

CREATE TRIGGER workstream_object_audit AFTER INSERT OR DELETE OR UPDATE ON workstream_files FOR EACH ROW EXECUTE FUNCTION workstream_record_object_change();

CREATE TRIGGER workstream_object_audit AFTER INSERT OR DELETE OR UPDATE ON zoom_managed_meetings FOR EACH ROW EXECUTE FUNCTION workstream_record_object_change();

CREATE TRIGGER workstream_object_removed AFTER DELETE ON agent_memories FOR EACH ROW EXECUTE FUNCTION workstream_forget_object();

CREATE TRIGGER workstream_object_removed AFTER DELETE ON calendar_managed_events FOR EACH ROW EXECUTE FUNCTION workstream_forget_object();

CREATE TRIGGER workstream_object_removed AFTER DELETE ON contacts FOR EACH ROW EXECUTE FUNCTION workstream_forget_object();

CREATE TRIGGER workstream_object_removed AFTER DELETE ON deep_research_runs FOR EACH ROW EXECUTE FUNCTION workstream_forget_object();

CREATE TRIGGER workstream_object_removed AFTER DELETE ON dropbox_transfers FOR EACH ROW EXECUTE FUNCTION workstream_forget_object();

CREATE TRIGGER workstream_object_removed AFTER DELETE ON jobs FOR EACH ROW EXECUTE FUNCTION workstream_forget_object();

CREATE TRIGGER workstream_object_removed AFTER DELETE ON processed_artifacts FOR EACH ROW EXECUTE FUNCTION workstream_forget_object();

CREATE TRIGGER workstream_object_removed AFTER DELETE ON reminders FOR EACH ROW EXECUTE FUNCTION workstream_forget_object();

CREATE TRIGGER workstream_object_removed AFTER DELETE ON workstream_files FOR EACH ROW EXECUTE FUNCTION workstream_forget_object();

CREATE TRIGGER workstream_object_removed AFTER DELETE ON zoom_managed_meetings FOR EACH ROW EXECUTE FUNCTION workstream_forget_object();

CREATE TRIGGER workstream_object_version BEFORE DELETE OR UPDATE ON agent_memories FOR EACH ROW EXECUTE FUNCTION workstream_check_object_version();

CREATE TRIGGER workstream_object_version BEFORE DELETE OR UPDATE ON calendar_managed_events FOR EACH ROW EXECUTE FUNCTION workstream_check_object_version();

CREATE TRIGGER workstream_object_version BEFORE DELETE OR UPDATE ON contacts FOR EACH ROW EXECUTE FUNCTION workstream_check_object_version();

CREATE TRIGGER workstream_object_version BEFORE DELETE OR UPDATE ON deep_research_runs FOR EACH ROW EXECUTE FUNCTION workstream_check_object_version();

CREATE TRIGGER workstream_object_version BEFORE DELETE OR UPDATE ON dropbox_transfers FOR EACH ROW EXECUTE FUNCTION workstream_check_object_version();

CREATE TRIGGER workstream_object_version BEFORE DELETE OR UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION workstream_check_object_version();

CREATE TRIGGER workstream_object_version BEFORE DELETE OR UPDATE ON processed_artifacts FOR EACH ROW EXECUTE FUNCTION workstream_check_object_version();

CREATE TRIGGER workstream_object_version BEFORE DELETE OR UPDATE ON reminders FOR EACH ROW EXECUTE FUNCTION workstream_check_object_version();

CREATE TRIGGER workstream_object_version BEFORE DELETE OR UPDATE ON workstream_files FOR EACH ROW EXECUTE FUNCTION workstream_check_object_version();

CREATE TRIGGER workstream_object_version BEFORE DELETE OR UPDATE ON zoom_managed_meetings FOR EACH ROW EXECUTE FUNCTION workstream_check_object_version();

CREATE TRIGGER workstream_owner BEFORE INSERT OR UPDATE ON channel_deliveries FOR EACH ROW EXECUTE FUNCTION workstream_bind_owner();

CREATE TRIGGER workstream_owner BEFORE INSERT OR UPDATE ON emails FOR EACH ROW EXECUTE FUNCTION workstream_bind_owner();

CREATE TRIGGER workstream_owner BEFORE INSERT OR UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION workstream_bind_owner();

CREATE TRIGGER workstream_owner BEFORE INSERT OR UPDATE ON outbound_email_logs FOR EACH ROW EXECUTE FUNCTION workstream_bind_owner();

CREATE TRIGGER workstream_owner BEFORE INSERT OR UPDATE ON request_messages FOR EACH ROW EXECUTE FUNCTION workstream_bind_owner();

CREATE TRIGGER workstream_owner BEFORE INSERT OR UPDATE ON thread_summaries FOR EACH ROW EXECUTE FUNCTION workstream_bind_owner();

CREATE TRIGGER workstream_research_execution BEFORE INSERT OR UPDATE OF status ON deep_research_runs FOR EACH ROW EXECUTE FUNCTION workstream_research_execution();

CREATE TRIGGER workstream_research_execution_status AFTER INSERT OR UPDATE OF status ON deep_research_runs FOR EACH ROW EXECUTE FUNCTION workstream_research_execution_status();

CREATE TRIGGER workstream_research_job_control AFTER UPDATE OF status ON jobs FOR EACH ROW EXECUTE FUNCTION workstream_research_job_control();

CREATE TRIGGER zz_workstream_object BEFORE INSERT OR UPDATE ON agent_memories FOR EACH ROW EXECUTE FUNCTION workstream_register_object('memory', 'id');

CREATE TRIGGER zz_workstream_object BEFORE INSERT OR UPDATE ON calendar_managed_events FOR EACH ROW EXECUTE FUNCTION workstream_register_object('calendar_event', 'assistant_id');

CREATE TRIGGER zz_workstream_object BEFORE INSERT OR UPDATE ON contacts FOR EACH ROW EXECUTE FUNCTION workstream_register_object('contact', 'id');

CREATE TRIGGER zz_workstream_object BEFORE INSERT OR UPDATE ON deep_research_runs FOR EACH ROW EXECUTE FUNCTION workstream_register_object('research', 'id');

CREATE TRIGGER zz_workstream_object BEFORE INSERT OR UPDATE ON dropbox_transfers FOR EACH ROW EXECUTE FUNCTION workstream_register_object('dropbox_transfer', 'request_key');

CREATE TRIGGER zz_workstream_object BEFORE INSERT OR UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION workstream_register_object('job', 'id');

CREATE TRIGGER zz_workstream_object BEFORE INSERT OR UPDATE ON processed_artifacts FOR EACH ROW EXECUTE FUNCTION workstream_register_object('artifact', 'id');

CREATE TRIGGER zz_workstream_object BEFORE INSERT OR UPDATE ON reminders FOR EACH ROW EXECUTE FUNCTION workstream_register_object('reminder', 'id');

CREATE TRIGGER zz_workstream_object BEFORE INSERT OR UPDATE ON workstream_files FOR EACH ROW EXECUTE FUNCTION workstream_register_object('file', 'id');

CREATE TRIGGER zz_workstream_object BEFORE INSERT OR UPDATE ON zoom_managed_meetings FOR EACH ROW EXECUTE FUNCTION workstream_register_object('zoom_meeting', 'id');

ALTER TABLE ONLY agent_checkpoints
    ADD CONSTRAINT agent_checkpoints_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE;

ALTER TABLE ONLY agent_memories
    ADD CONSTRAINT agent_memories_object_id_fkey FOREIGN KEY (object_id) REFERENCES workstream_objects(id) ON DELETE CASCADE;

ALTER TABLE ONLY agent_memories
    ADD CONSTRAINT agent_memories_source_job_id_fkey FOREIGN KEY (source_job_id) REFERENCES jobs(id) ON DELETE SET NULL;

ALTER TABLE ONLY calendar_event_audit
    ADD CONSTRAINT calendar_event_audit_assistant_id_fkey FOREIGN KEY (assistant_id) REFERENCES calendar_managed_events(assistant_id) ON DELETE CASCADE;

ALTER TABLE ONLY calendar_event_audit
    ADD CONSTRAINT calendar_event_audit_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL;

ALTER TABLE ONLY calendar_managed_events
    ADD CONSTRAINT calendar_managed_events_created_by_job_id_fkey FOREIGN KEY (created_by_job_id) REFERENCES jobs(id) ON DELETE SET NULL;

ALTER TABLE ONLY calendar_managed_events
    ADD CONSTRAINT calendar_managed_events_object_id_fkey FOREIGN KEY (object_id) REFERENCES workstream_objects(id) ON DELETE CASCADE;

ALTER TABLE ONLY calendar_managed_events
    ADD CONSTRAINT calendar_managed_events_updated_by_job_id_fkey FOREIGN KEY (updated_by_job_id) REFERENCES jobs(id) ON DELETE SET NULL;

ALTER TABLE ONLY channel_deliveries
    ADD CONSTRAINT channel_deliveries_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE;

ALTER TABLE ONLY channel_deliveries
    ADD CONSTRAINT channel_deliveries_request_id_fkey FOREIGN KEY (request_id) REFERENCES request_messages(id) ON DELETE CASCADE;

ALTER TABLE ONLY channel_deliveries
    ADD CONSTRAINT channel_deliveries_workstream_id_fkey FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE;

ALTER TABLE ONLY contacts
    ADD CONSTRAINT contacts_object_id_fkey FOREIGN KEY (object_id) REFERENCES workstream_objects(id) ON DELETE CASCADE;

ALTER TABLE ONLY deep_research_events
    ADD CONSTRAINT deep_research_events_run_id_fkey FOREIGN KEY (run_id) REFERENCES deep_research_runs(id) ON DELETE CASCADE;

ALTER TABLE ONLY deep_research_runs
    ADD CONSTRAINT deep_research_runs_execution_job_id_fkey FOREIGN KEY (execution_job_id) REFERENCES jobs(id) ON DELETE SET NULL;

ALTER TABLE ONLY deep_research_runs
    ADD CONSTRAINT deep_research_runs_object_id_fkey FOREIGN KEY (object_id) REFERENCES workstream_objects(id) ON DELETE CASCADE;

ALTER TABLE ONLY deep_research_runs
    ADD CONSTRAINT deep_research_runs_original_job_id_fkey FOREIGN KEY (original_job_id) REFERENCES jobs(id) ON DELETE SET NULL;

ALTER TABLE ONLY dropbox_transfers
    ADD CONSTRAINT dropbox_transfers_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL;

ALTER TABLE ONLY dropbox_transfers
    ADD CONSTRAINT dropbox_transfers_object_id_fkey FOREIGN KEY (object_id) REFERENCES workstream_objects(id) ON DELETE CASCADE;

ALTER TABLE ONLY emails
    ADD CONSTRAINT emails_workstream_id_fkey FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE;

ALTER TABLE ONLY jobs
    ADD CONSTRAINT jobs_object_id_fkey FOREIGN KEY (object_id) REFERENCES workstream_objects(id) ON DELETE CASCADE;

ALTER TABLE ONLY jobs
    ADD CONSTRAINT jobs_trigger_email_id_fkey FOREIGN KEY (trigger_email_id) REFERENCES emails(id) ON DELETE SET NULL;

ALTER TABLE ONLY jobs
    ADD CONSTRAINT jobs_trigger_request_id_fkey FOREIGN KEY (trigger_request_id) REFERENCES request_messages(id) ON DELETE SET NULL;

ALTER TABLE ONLY jobs
    ADD CONSTRAINT jobs_workstream_id_fkey FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE;

ALTER TABLE ONLY memory_events
    ADD CONSTRAINT memory_events_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL;

ALTER TABLE ONLY model_service_calls
    ADD CONSTRAINT model_service_calls_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL;

ALTER TABLE ONLY outbound_email_logs
    ADD CONSTRAINT outbound_email_logs_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL;

ALTER TABLE ONLY outbound_email_logs
    ADD CONSTRAINT outbound_email_logs_workstream_id_fkey FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE;

ALTER TABLE ONLY processed_artifacts
    ADD CONSTRAINT processed_artifacts_email_id_fkey FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE SET NULL;

ALTER TABLE ONLY processed_artifacts
    ADD CONSTRAINT processed_artifacts_object_id_fkey FOREIGN KEY (object_id) REFERENCES workstream_objects(id) ON DELETE CASCADE;

ALTER TABLE ONLY processed_artifacts
    ADD CONSTRAINT processed_artifacts_request_id_fkey FOREIGN KEY (request_id) REFERENCES request_messages(id) ON DELETE SET NULL;

ALTER TABLE ONLY reminders
    ADD CONSTRAINT reminders_created_by_job_id_fkey FOREIGN KEY (created_by_job_id) REFERENCES jobs(id) ON DELETE SET NULL;

ALTER TABLE ONLY reminders
    ADD CONSTRAINT reminders_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL;

ALTER TABLE ONLY reminders
    ADD CONSTRAINT reminders_object_id_fkey FOREIGN KEY (object_id) REFERENCES workstream_objects(id) ON DELETE CASCADE;

ALTER TABLE ONLY request_messages
    ADD CONSTRAINT request_messages_email_id_fkey FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE CASCADE;

ALTER TABLE ONLY request_messages
    ADD CONSTRAINT request_messages_workstream_id_fkey FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE;

ALTER TABLE ONLY supervisor_instructions
    ADD CONSTRAINT supervisor_instructions_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE;

ALTER TABLE ONLY task_logs
    ADD CONSTRAINT task_logs_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE;

ALTER TABLE ONLY thread_summaries
    ADD CONSTRAINT thread_summaries_last_email_id_fkey FOREIGN KEY (last_email_id) REFERENCES emails(id) ON DELETE CASCADE;

ALTER TABLE ONLY thread_summaries
    ADD CONSTRAINT thread_summaries_workstream_id_fkey FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE;

ALTER TABLE ONLY workspace_file_chunks
    ADD CONSTRAINT workspace_file_chunks_file_id_fkey FOREIGN KEY (file_id) REFERENCES workspace_files(id) ON DELETE CASCADE;

ALTER TABLE ONLY workstream_attachments
    ADD CONSTRAINT workstream_attachments_object_id_fkey FOREIGN KEY (object_id) REFERENCES workstream_objects(id) ON DELETE CASCADE;

ALTER TABLE ONLY workstream_attachments
    ADD CONSTRAINT workstream_attachments_workstream_id_fkey FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE;

ALTER TABLE ONLY workstream_context_events
    ADD CONSTRAINT workstream_context_events_object_event_id_fkey FOREIGN KEY (object_event_id) REFERENCES workstream_object_events(id) ON DELETE CASCADE;

ALTER TABLE ONLY workstream_context_events
    ADD CONSTRAINT workstream_context_events_workstream_id_fkey FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE;

ALTER TABLE ONLY workstream_files
    ADD CONSTRAINT workstream_files_object_id_fkey FOREIGN KEY (object_id) REFERENCES workstream_objects(id) ON DELETE CASCADE;

ALTER TABLE ONLY workstream_files
    ADD CONSTRAINT workstream_files_source_job_id_fkey FOREIGN KEY (source_job_id) REFERENCES jobs(id) ON DELETE SET NULL;

ALTER TABLE ONLY workstream_task_notifications
    ADD CONSTRAINT workstream_task_notifications_source_job_id_fkey FOREIGN KEY (source_job_id) REFERENCES jobs(id) ON DELETE CASCADE;

ALTER TABLE ONLY workstream_task_notifications
    ADD CONSTRAINT workstream_task_notifications_workstream_id_fkey FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE;

ALTER TABLE ONLY workstream_threads
    ADD CONSTRAINT workstream_threads_workstream_id_fkey FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE;

ALTER TABLE ONLY workstreams
    ADD CONSTRAINT workstreams_main_thread_fk FOREIGN KEY (main_thread_id) REFERENCES workstream_threads(thread_id) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE ONLY workstreams
    ADD CONSTRAINT workstreams_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES workstreams(id) ON DELETE CASCADE;

ALTER TABLE ONLY zoom_managed_meetings
    ADD CONSTRAINT zoom_managed_meetings_calendar_event_id_fkey FOREIGN KEY (calendar_event_id) REFERENCES calendar_managed_events(assistant_id) ON DELETE SET NULL;

ALTER TABLE ONLY zoom_managed_meetings
    ADD CONSTRAINT zoom_managed_meetings_created_by_job_id_fkey FOREIGN KEY (created_by_job_id) REFERENCES jobs(id) ON DELETE SET NULL;

ALTER TABLE ONLY zoom_managed_meetings
    ADD CONSTRAINT zoom_managed_meetings_object_id_fkey FOREIGN KEY (object_id) REFERENCES workstream_objects(id) ON DELETE CASCADE;

INSERT INTO schema_migrations(filename) VALUES
  ('000_current_baseline'),
  ('006_zoom_meetings.sql'),
  ('007_request_channels.sql'),
  ('008_workstreams.sql'),
  ('009_workstream_threads.sql'),
  ('010_model_service.sql'),
  ('011_research_system_threads.sql');

SET check_function_bodies = true;
