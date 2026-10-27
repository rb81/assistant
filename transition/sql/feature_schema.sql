
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS trigger_email_id bigint;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'jobs_trigger_email_id_fkey'
      AND conrelid = 'jobs'::regclass
  ) THEN
    ALTER TABLE jobs
    ADD CONSTRAINT jobs_trigger_email_id_fkey
    FOREIGN KEY (trigger_email_id) REFERENCES emails(id) ON DELETE SET NULL;
  END IF;
END $$;
UPDATE jobs j
SET trigger_email_id = (
  SELECT e.id
  FROM emails e
  WHERE e.thread_id = j.thread_id
    AND e.created_at <= j.created_at
  ORDER BY e.created_at DESC, e.id DESC
  LIMIT 1
)
WHERE j.trigger_email_id IS NULL
  AND EXISTS (
    SELECT 1
    FROM emails e
    WHERE e.thread_id = j.thread_id
      AND e.created_at <= j.created_at
  );
CREATE INDEX IF NOT EXISTS jobs_trigger_email_idx ON jobs(trigger_email_id);
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_status_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_status_check CHECK (status IN ('queued', 'running', 'waiting', 'completed', 'failed', 'needs_review', 'cancelled'));
DROP INDEX IF EXISTS jobs_one_open_per_thread_idx;
CREATE UNIQUE INDEX jobs_one_open_per_thread_idx
  ON jobs(thread_id)
  WHERE status IN ('queued', 'running', 'waiting', 'needs_review');
UPDATE jobs
SET status = 'waiting', updated_at = now()
WHERE status = 'needs_review'
  AND (
    last_error LIKE 'Project #% is running; waiting for project completion.%'
    OR last_error LIKE 'Deep research run #% is running; waiting for research completion.%'
    OR last_error = 'Async request is running.'
  );

CREATE TABLE IF NOT EXISTS processed_artifacts (
  id bigserial PRIMARY KEY,
  email_id bigint NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
  thread_id text NOT NULL,
  source_type text NOT NULL,
  source_label text NOT NULL,
  source_uri text,
  original_filename text,
  content_type text,
  raw_path text,
  raw_sha256 text,
  raw_size_bytes bigint,
  scan_status text NOT NULL DEFAULT 'pending',
  scan_engine text,
  scan_result text,
  conversion_status text NOT NULL DEFAULT 'pending',
  markdown_path text,
  markdown_sha256 text,
  markdown_size_bytes bigint,
  error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE processed_artifacts DROP CONSTRAINT IF EXISTS processed_artifacts_source_type_check;
ALTER TABLE processed_artifacts ADD CONSTRAINT processed_artifacts_source_type_check CHECK (source_type IN ('attachment', 'youtube_url'));
ALTER TABLE processed_artifacts DROP CONSTRAINT IF EXISTS processed_artifacts_scan_status_check;
ALTER TABLE processed_artifacts ADD CONSTRAINT processed_artifacts_scan_status_check CHECK (scan_status IN ('pending', 'clean', 'infected', 'error', 'skipped', 'not_applicable'));
ALTER TABLE processed_artifacts DROP CONSTRAINT IF EXISTS processed_artifacts_conversion_status_check;
ALTER TABLE processed_artifacts ADD CONSTRAINT processed_artifacts_conversion_status_check CHECK (conversion_status IN ('pending', 'ready', 'unsupported', 'failed', 'skipped'));

CREATE INDEX IF NOT EXISTS processed_artifacts_email_idx ON processed_artifacts(email_id, id);
CREATE INDEX IF NOT EXISTS processed_artifacts_thread_idx ON processed_artifacts(thread_id, id);
CREATE INDEX IF NOT EXISTS processed_artifacts_status_idx ON processed_artifacts(conversion_status, scan_status);

CREATE TABLE IF NOT EXISTS dropbox_transfers (
    request_key text PRIMARY KEY,
    job_id bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    thread_id text,
    direction text NOT NULL CHECK (direction IN ('import', 'export')),
    remote_path text NOT NULL,
    remote_id text,
    revision text,
    local_path text,
    markdown_path text,
    local_sha256 text,
    markdown_sha256 text,
    content_hash text,
    size_bytes bigint CHECK (size_bytes >= 0),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready', 'failed', 'uncertain')),
    scan_status text NOT NULL DEFAULT 'pending' CHECK (scan_status IN ('pending', 'clean', 'infected', 'error', 'skipped')),
    conversion_status text NOT NULL DEFAULT 'pending' CHECK (conversion_status IN ('pending', 'ready', 'failed', 'not_applicable')),
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dropbox_transfers_job_idx ON dropbox_transfers(job_id);

CREATE TABLE IF NOT EXISTS agent_memories (
  id bigserial PRIMARY KEY,
  content text NOT NULL,
  tags text[] NOT NULL DEFAULT ARRAY[]::text[],
  scope text NOT NULL DEFAULT 'global',
  kind text NOT NULL DEFAULT 'fact',
  importance int NOT NULL DEFAULT 3,
  confidence double precision NOT NULL DEFAULT 0.7,
  expires_at timestamptz,
  pinned boolean NOT NULL DEFAULT false,
  embedding double precision[],
  embedding_model text,
  embedding_dimensions int,
  embedding_updated_at timestamptz,
  source_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_accessed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT 'global';
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'fact';
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS importance int NOT NULL DEFAULT 3;
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS confidence double precision NOT NULL DEFAULT 0.7;
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS expires_at timestamptz;
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT false;
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS embedding double precision[];
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS embedding_model text;
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS embedding_dimensions int;
ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;

CREATE INDEX IF NOT EXISTS agent_memories_created_idx ON agent_memories(created_at DESC);
CREATE INDEX IF NOT EXISTS agent_memories_tags_idx ON agent_memories USING GIN(tags);
CREATE INDEX IF NOT EXISTS agent_memories_scope_kind_idx ON agent_memories(scope, kind);
CREATE INDEX IF NOT EXISTS agent_memories_pinned_idx ON agent_memories(pinned, importance DESC);

CREATE TABLE IF NOT EXISTS memory_events (
  id bigserial PRIMARY KEY,
  memory_id bigint,
  job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  actor text NOT NULL DEFAULT 'system',
  event_type text NOT NULL,
  input_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  output_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE memory_events DROP CONSTRAINT IF EXISTS memory_events_memory_id_fkey;

CREATE INDEX IF NOT EXISTS memory_events_memory_created_idx ON memory_events(memory_id, created_at DESC);
CREATE INDEX IF NOT EXISTS memory_events_job_created_idx ON memory_events(job_id, created_at DESC);
























CREATE TABLE IF NOT EXISTS contacts (
  id bigserial PRIMARY KEY,
  first_name text NOT NULL DEFAULT '',
  last_name text NOT NULL DEFAULT '',
  email_address text NOT NULL DEFAULT '',
  company text NOT NULL DEFAULT '',
  title text NOT NULL DEFAULT '',
  notes text NOT NULL DEFAULT '',
  source text NOT NULL DEFAULT 'agent',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE contacts ADD COLUMN IF NOT EXISTS first_name text NOT NULL DEFAULT '';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS last_name text NOT NULL DEFAULT '';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS email_address text NOT NULL DEFAULT '';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS company text NOT NULL DEFAULT '';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS title text NOT NULL DEFAULT '';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS notes text NOT NULL DEFAULT '';
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'agent';
ALTER TABLE contacts DROP CONSTRAINT IF EXISTS contacts_source_check;
ALTER TABLE contacts ADD CONSTRAINT contacts_source_check CHECK (source IN ('dashboard', 'agent'));
ALTER TABLE contacts DROP CONSTRAINT IF EXISTS contacts_not_blank_check;
ALTER TABLE contacts ADD CONSTRAINT contacts_not_blank_check CHECK (
  first_name <> ''
  OR last_name <> ''
  OR email_address <> ''
  OR company <> ''
  OR title <> ''
  OR notes <> ''
);

CREATE UNIQUE INDEX IF NOT EXISTS contacts_email_unique_idx
  ON contacts(lower(email_address))
  WHERE email_address <> '';
CREATE INDEX IF NOT EXISTS contacts_name_idx ON contacts(last_name, first_name);
CREATE INDEX IF NOT EXISTS contacts_company_idx ON contacts(company);
CREATE INDEX IF NOT EXISTS contacts_updated_idx ON contacts(updated_at DESC);

CREATE TABLE IF NOT EXISTS workspace_files (
  id bigserial PRIMARY KEY,
  relative_path text NOT NULL UNIQUE,
  size_bytes bigint NOT NULL DEFAULT 0,
  mtime_ns text NOT NULL DEFAULT '',
  content_sha256 text NOT NULL DEFAULT '',
  mime_type text,
  extension text NOT NULL DEFAULT '',
  index_status text NOT NULL DEFAULT 'pending',
  error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  indexed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS size_bytes bigint NOT NULL DEFAULT 0;
ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS mtime_ns text NOT NULL DEFAULT '';
ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS content_sha256 text NOT NULL DEFAULT '';
ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS mime_type text;
ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS extension text NOT NULL DEFAULT '';
ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS index_status text NOT NULL DEFAULT 'pending';
ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS error text;
ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE workspace_files ADD COLUMN IF NOT EXISTS indexed_at timestamptz;
ALTER TABLE workspace_files DROP CONSTRAINT IF EXISTS workspace_files_index_status_check;
ALTER TABLE workspace_files ADD CONSTRAINT workspace_files_index_status_check CHECK (
  index_status IN ('pending', 'indexed', 'embedding_failed', 'unsupported', 'error', 'deleted', 'superseded')
);

CREATE INDEX IF NOT EXISTS workspace_files_status_idx ON workspace_files(index_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS workspace_files_extension_idx ON workspace_files(extension);

CREATE TABLE IF NOT EXISTS workspace_file_chunks (
  id bigserial PRIMARY KEY,
  file_id bigint NOT NULL REFERENCES workspace_files(id) ON DELETE CASCADE,
  chunk_index int NOT NULL,
  content text NOT NULL,
  start_line int,
  end_line int,
  content_sha256 text NOT NULL DEFAULT '',
  embedding double precision[],
  embedding_model text,
  embedding_dimensions int,
  embedding_updated_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(file_id, chunk_index)
);

ALTER TABLE workspace_file_chunks ADD COLUMN IF NOT EXISTS start_line int;
ALTER TABLE workspace_file_chunks ADD COLUMN IF NOT EXISTS end_line int;
ALTER TABLE workspace_file_chunks ADD COLUMN IF NOT EXISTS content_sha256 text NOT NULL DEFAULT '';
ALTER TABLE workspace_file_chunks ADD COLUMN IF NOT EXISTS embedding double precision[];
ALTER TABLE workspace_file_chunks ADD COLUMN IF NOT EXISTS embedding_model text;
ALTER TABLE workspace_file_chunks ADD COLUMN IF NOT EXISTS embedding_dimensions int;
ALTER TABLE workspace_file_chunks ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;

CREATE INDEX IF NOT EXISTS workspace_file_chunks_file_idx ON workspace_file_chunks(file_id, chunk_index);

CREATE TABLE IF NOT EXISTS workspace_document_conversions (
  id bigserial PRIMARY KEY,
  original_relative_path text NOT NULL,
  markdown_relative_path text,
  archived_relative_path text,
  original_sha256 text,
  markdown_sha256 text,
  status text NOT NULL DEFAULT 'pending',
  source text NOT NULL DEFAULT 'workspace',
  error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE workspace_document_conversions ADD COLUMN IF NOT EXISTS markdown_relative_path text;
ALTER TABLE workspace_document_conversions ADD COLUMN IF NOT EXISTS archived_relative_path text;
ALTER TABLE workspace_document_conversions ADD COLUMN IF NOT EXISTS original_sha256 text;
ALTER TABLE workspace_document_conversions ADD COLUMN IF NOT EXISTS markdown_sha256 text;
ALTER TABLE workspace_document_conversions ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'pending';
ALTER TABLE workspace_document_conversions ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'workspace';
ALTER TABLE workspace_document_conversions ADD COLUMN IF NOT EXISTS error text;
ALTER TABLE workspace_document_conversions ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE workspace_document_conversions DROP CONSTRAINT IF EXISTS workspace_document_conversions_status_check;
ALTER TABLE workspace_document_conversions ADD CONSTRAINT workspace_document_conversions_status_check CHECK (
  status IN ('pending', 'ready', 'failed', 'skipped')
);

CREATE INDEX IF NOT EXISTS workspace_document_conversions_original_idx ON workspace_document_conversions(original_relative_path, created_at DESC);
CREATE INDEX IF NOT EXISTS workspace_document_conversions_markdown_idx ON workspace_document_conversions(markdown_relative_path, created_at DESC);

CREATE TABLE IF NOT EXISTS reminders (
  id bigserial PRIMARY KEY,
  title text NOT NULL,
  task text NOT NULL,
  run_at timestamptz NOT NULL,
  status text NOT NULL DEFAULT 'scheduled'
    CHECK (status IN ('scheduled', 'queued', 'completed', 'failed', 'cancelled')),
  priority int NOT NULL DEFAULT 0,
  recurrence_unit text,
  recurrence_interval int,
  recurrence_anchor_day int,
  created_by text NOT NULL DEFAULT 'agent',
  created_by_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  queued_at timestamptz,
  completed_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE reminders ADD COLUMN IF NOT EXISTS recurrence_unit text;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS recurrence_interval int;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS recurrence_anchor_day int;
ALTER TABLE reminders DROP CONSTRAINT IF EXISTS reminders_recurrence_check;
ALTER TABLE reminders ADD CONSTRAINT reminders_recurrence_check CHECK (
  (recurrence_unit IS NULL AND recurrence_interval IS NULL AND recurrence_anchor_day IS NULL)
  OR (
    recurrence_unit IN ('hour', 'day', 'week', 'month')
    AND recurrence_interval IS NOT NULL
    AND recurrence_interval > 0
    AND (recurrence_anchor_day IS NULL OR recurrence_anchor_day BETWEEN 1 AND 31)
  )
);

CREATE INDEX IF NOT EXISTS reminders_status_run_at_idx ON reminders(status, run_at);
CREATE INDEX IF NOT EXISTS reminders_job_id_idx ON reminders(job_id);

CREATE TABLE IF NOT EXISTS calendar_managed_events (
  assistant_id text PRIMARY KEY,
  provider_name text NOT NULL,
  provider_event_id text NOT NULL,
  calendar_id text NOT NULL,
  calendar_name text NOT NULL DEFAULT '',
  summary text NOT NULL DEFAULT '',
  starts_at timestamptz NOT NULL,
  ends_at timestamptz NOT NULL,
  status text NOT NULL DEFAULT 'active',
  created_by_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  updated_by_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  deleted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS provider_name text;
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS provider_event_id text;
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS calendar_id text;
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS calendar_name text NOT NULL DEFAULT '';
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS summary text NOT NULL DEFAULT '';
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS starts_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS ends_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active';
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS created_by_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL;
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS updated_by_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL;
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE calendar_managed_events ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE calendar_managed_events DROP CONSTRAINT IF EXISTS calendar_managed_events_status_check;
ALTER TABLE calendar_managed_events ADD CONSTRAINT calendar_managed_events_status_check CHECK (status IN ('active', 'deleted'));

DROP INDEX IF EXISTS calendar_managed_events_uid_unique_idx;
CREATE INDEX IF NOT EXISTS calendar_managed_events_status_starts_idx ON calendar_managed_events(status, starts_at);
CREATE INDEX IF NOT EXISTS calendar_managed_events_provider_idx ON calendar_managed_events(provider_name, provider_event_id);
CREATE INDEX IF NOT EXISTS calendar_managed_events_job_idx ON calendar_managed_events(created_by_job_id, updated_by_job_id);


CREATE TABLE IF NOT EXISTS calendar_event_audit (
  id bigserial PRIMARY KEY,
  assistant_id text NOT NULL REFERENCES calendar_managed_events(assistant_id) ON DELETE CASCADE,
  job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  action text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE calendar_event_audit DROP CONSTRAINT IF EXISTS calendar_event_audit_action_check;
ALTER TABLE calendar_event_audit ADD CONSTRAINT calendar_event_audit_action_check CHECK (action IN ('created', 'updated', 'deleted'));
CREATE INDEX IF NOT EXISTS calendar_event_audit_event_created_idx ON calendar_event_audit(assistant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS calendar_event_audit_job_created_idx ON calendar_event_audit(job_id, created_at DESC);
















CREATE INDEX IF NOT EXISTS outbound_email_logs_provider_message_idx ON outbound_email_logs(provider_message_id);

















CREATE TABLE IF NOT EXISTS deep_research_runs (
  id bigserial PRIMARY KEY,
  original_job_id bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  original_thread_id text NOT NULL,
  title text NOT NULL,
  research_question text NOT NULL,
  instructions text,
  status text NOT NULL DEFAULT 'queued',
  priority int NOT NULL DEFAULT 0,
  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 3,
  run_at timestamptz NOT NULL DEFAULT now(),
  locked_at timestamptz,
  locked_by text,
  tool_call_count int NOT NULL DEFAULT 0,
  max_tool_calls int NOT NULL DEFAULT 40,
  waiting_since timestamptz,
  result_summary text,
  result_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  completed_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE deep_research_runs DROP CONSTRAINT IF EXISTS deep_research_runs_status_check;
ALTER TABLE deep_research_runs ADD CONSTRAINT deep_research_runs_status_check CHECK (status IN ('queued', 'running', 'waiting_for_input', 'completed', 'failed', 'cancelled'));
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS instructions text;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS priority int NOT NULL DEFAULT 0;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS attempts int NOT NULL DEFAULT 0;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS max_attempts int NOT NULL DEFAULT 3;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS run_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS locked_at timestamptz;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS locked_by text;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS tool_call_count int NOT NULL DEFAULT 0;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS max_tool_calls int NOT NULL DEFAULT 40;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS waiting_since timestamptz;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS result_summary text;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS result_data jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS completed_at timestamptz;
ALTER TABLE deep_research_runs ADD COLUMN IF NOT EXISTS last_error text;

CREATE INDEX IF NOT EXISTS deep_research_runs_status_run_at_idx ON deep_research_runs(status, run_at);
CREATE INDEX IF NOT EXISTS deep_research_runs_original_job_idx ON deep_research_runs(original_job_id);

CREATE TABLE IF NOT EXISTS deep_research_events (
  id bigserial PRIMARY KEY,
  run_id bigint NOT NULL REFERENCES deep_research_runs(id) ON DELETE CASCADE,
  sequence int NOT NULL,
  event_type text NOT NULL,
  tool_name text,
  input_data jsonb,
  output_data jsonb,
  tokens_used jsonb,
  duration_ms int,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (run_id, sequence)
);

ALTER TABLE deep_research_events DROP CONSTRAINT IF EXISTS deep_research_events_event_type_check;
ALTER TABLE deep_research_events ADD CONSTRAINT deep_research_events_event_type_check CHECK (
  event_type IN ('llm_request', 'llm_response', 'tool_call', 'tool_result', 'search_request', 'search_result', 'error', 'status_change')
);

CREATE INDEX IF NOT EXISTS deep_research_events_run_sequence_idx ON deep_research_events(run_id, sequence);
CREATE INDEX IF NOT EXISTS deep_research_events_run_created_idx ON deep_research_events(run_id, created_at);


CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;


DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'processed_artifacts_set_updated_at') THEN
    CREATE TRIGGER processed_artifacts_set_updated_at
    BEFORE UPDATE ON processed_artifacts
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'agent_memories_set_updated_at') THEN
    CREATE TRIGGER agent_memories_set_updated_at
    BEFORE UPDATE ON agent_memories
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;


  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'contacts_set_updated_at') THEN
    CREATE TRIGGER contacts_set_updated_at
    BEFORE UPDATE ON contacts
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'reminders_set_updated_at') THEN
    CREATE TRIGGER reminders_set_updated_at
    BEFORE UPDATE ON reminders
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'calendar_managed_events_set_updated_at') THEN
    CREATE TRIGGER calendar_managed_events_set_updated_at
    BEFORE UPDATE ON calendar_managed_events
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;



  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'deep_research_runs_set_updated_at') THEN
    CREATE TRIGGER deep_research_runs_set_updated_at
    BEFORE UPDATE ON deep_research_runs
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'workspace_files_set_updated_at') THEN
    CREATE TRIGGER workspace_files_set_updated_at
    BEFORE UPDATE ON workspace_files
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'workspace_document_conversions_set_updated_at') THEN
    CREATE TRIGGER workspace_document_conversions_set_updated_at
    BEFORE UPDATE ON workspace_document_conversions
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;
END $$;

-- Context search embedding columns
ALTER TABLE jobs
  ADD COLUMN IF NOT EXISTS embedding double precision[],
  ADD COLUMN IF NOT EXISTS embedding_model text,
  ADD COLUMN IF NOT EXISTS embedding_dimensions int,
  ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;

ALTER TABLE reminders
  ADD COLUMN IF NOT EXISTS embedding double precision[],
  ADD COLUMN IF NOT EXISTS embedding_model text,
  ADD COLUMN IF NOT EXISTS embedding_dimensions int,
  ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;

ALTER TABLE outbound_email_logs
  ADD COLUMN IF NOT EXISTS embedding double precision[],
  ADD COLUMN IF NOT EXISTS embedding_model text,
  ADD COLUMN IF NOT EXISTS embedding_dimensions int,
  ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;

ALTER TABLE emails
  ADD COLUMN IF NOT EXISTS embedding double precision[],
  ADD COLUMN IF NOT EXISTS embedding_model text,
  ADD COLUMN IF NOT EXISTS embedding_dimensions int,
  ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;

ALTER TABLE contacts
  ADD COLUMN IF NOT EXISTS embedding double precision[],
  ADD COLUMN IF NOT EXISTS embedding_model text,
  ADD COLUMN IF NOT EXISTS embedding_dimensions int,
  ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;
