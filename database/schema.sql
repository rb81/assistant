CREATE TABLE emails (
  id bigserial PRIMARY KEY,
  message_id text UNIQUE NOT NULL,
  in_reply_to text,
  references_header text[] DEFAULT ARRAY[]::text[],
  thread_id text NOT NULL,
  from_address text NOT NULL,
  to_addresses text[] NOT NULL DEFAULT ARRAY[]::text[],
  cc_addresses text[] NOT NULL DEFAULT ARRAY[]::text[],
  subject text,
  body_text text,
  body_html text,
  attachments jsonb NOT NULL DEFAULT '[]'::jsonb,
  received_at timestamptz NOT NULL,
  downloaded_at timestamptz NOT NULL DEFAULT now(),
  folder text NOT NULL DEFAULT 'INBOX',
  is_actionable boolean NOT NULL DEFAULT false,
  embedding double precision[],
  embedding_model text,
  embedding_dimensions int,
  embedding_updated_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX emails_thread_received_idx ON emails(thread_id, received_at);
CREATE INDEX emails_received_idx ON emails(received_at);
CREATE INDEX emails_from_address_idx ON emails(from_address);

CREATE TABLE processed_artifacts (
  id bigserial PRIMARY KEY,
  email_id bigint NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
  thread_id text NOT NULL,
  source_type text NOT NULL
    CHECK (source_type IN ('attachment', 'youtube_url')),
  source_label text NOT NULL,
  source_uri text,
  original_filename text,
  content_type text,
  raw_path text,
  raw_sha256 text,
  raw_size_bytes bigint,
  scan_status text NOT NULL DEFAULT 'pending'
    CHECK (scan_status IN ('pending', 'clean', 'infected', 'error', 'skipped', 'not_applicable')),
  scan_engine text,
  scan_result text,
  conversion_status text NOT NULL DEFAULT 'pending'
    CHECK (conversion_status IN ('pending', 'ready', 'unsupported', 'failed', 'skipped')),
  markdown_path text,
  markdown_sha256 text,
  markdown_size_bytes bigint,
  error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX processed_artifacts_email_idx ON processed_artifacts(email_id, id);
CREATE INDEX processed_artifacts_thread_idx ON processed_artifacts(thread_id, id);
CREATE INDEX processed_artifacts_status_idx ON processed_artifacts(conversion_status, scan_status);

CREATE TABLE runtime_state (
  key text PRIMARY KEY,
  value jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
  id bigserial PRIMARY KEY,
  thread_id text NOT NULL,
  trigger_email_id bigint REFERENCES emails(id) ON DELETE SET NULL,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'waiting', 'completed', 'failed', 'needs_review', 'cancelled')),
  priority int NOT NULL DEFAULT 0,
  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 3,
  run_at timestamptz NOT NULL DEFAULT now(),
  locked_at timestamptz,
  locked_by text,
  has_new_context boolean NOT NULL DEFAULT false,
  task_summary text,
  embedding double precision[],
  embedding_model text,
  embedding_dimensions int,
  embedding_updated_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  last_error text
);

CREATE INDEX jobs_status_run_at_idx ON jobs(status, run_at);
CREATE INDEX jobs_thread_id_idx ON jobs(thread_id);
CREATE INDEX jobs_trigger_email_idx ON jobs(trigger_email_id);
CREATE UNIQUE INDEX jobs_one_open_per_thread_idx
  ON jobs(thread_id)
  WHERE status IN ('queued', 'running', 'waiting', 'needs_review');

CREATE TABLE agent_memories (
  id bigserial PRIMARY KEY,
  content text NOT NULL,
  tags text[] NOT NULL DEFAULT ARRAY[]::text[],
  scope text NOT NULL DEFAULT 'global',
  kind text NOT NULL DEFAULT 'fact',
  importance int NOT NULL DEFAULT 3,
  confidence double precision NOT NULL DEFAULT 0.7,
  expires_at timestamptz,
  pinned boolean NOT NULL DEFAULT false,
  linked_entities jsonb NOT NULL DEFAULT '[]'::jsonb,
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

CREATE INDEX agent_memories_created_idx ON agent_memories(created_at DESC);
CREATE INDEX agent_memories_tags_idx ON agent_memories USING GIN(tags);
CREATE INDEX agent_memories_scope_kind_idx ON agent_memories(scope, kind);
CREATE INDEX agent_memories_pinned_idx ON agent_memories(pinned, importance DESC);
CREATE INDEX agent_memories_linked_entities_idx ON agent_memories USING GIN(linked_entities);

CREATE TABLE memory_events (
  id bigserial PRIMARY KEY,
  memory_id bigint,
  job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  actor text NOT NULL DEFAULT 'system',
  event_type text NOT NULL,
  input_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  output_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX memory_events_memory_created_idx ON memory_events(memory_id, created_at DESC);
CREATE INDEX memory_events_job_created_idx ON memory_events(job_id, created_at DESC);

CREATE TABLE agent_notes (
  id bigserial PRIMARY KEY,
  title text NOT NULL DEFAULT 'Untitled note',
  content text NOT NULL,
  tags text[] NOT NULL DEFAULT ARRAY[]::text[],
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'resolved', 'archived')),
  linked_entities jsonb NOT NULL DEFAULT '[]'::jsonb,
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

CREATE INDEX agent_notes_created_idx ON agent_notes(created_at DESC);
CREATE INDEX agent_notes_tags_idx ON agent_notes USING GIN(tags);
CREATE INDEX agent_notes_updated_idx ON agent_notes(updated_at DESC);
CREATE INDEX agent_notes_status_idx ON agent_notes(status);
CREATE INDEX agent_notes_linked_entities_idx ON agent_notes USING GIN(linked_entities);

CREATE TABLE note_events (
  id bigserial PRIMARY KEY,
  note_id bigint,
  job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  actor text NOT NULL DEFAULT 'system',
  event_type text NOT NULL,
  input_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  output_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX note_events_note_created_idx ON note_events(note_id, created_at DESC);
CREATE INDEX note_events_job_created_idx ON note_events(job_id, created_at DESC);

CREATE TABLE contacts (
  id bigserial PRIMARY KEY,
  first_name text NOT NULL DEFAULT '',
  last_name text NOT NULL DEFAULT '',
  email_address text NOT NULL DEFAULT '',
  company text NOT NULL DEFAULT '',
  title text NOT NULL DEFAULT '',
  notes text NOT NULL DEFAULT '',
  source text NOT NULL DEFAULT 'agent'
    CONSTRAINT contacts_source_check CHECK (source IN ('dashboard', 'agent')),
  embedding double precision[],
  embedding_model text,
  embedding_dimensions int,
  embedding_updated_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT contacts_not_blank_check CHECK (
    first_name <> ''
    OR last_name <> ''
    OR email_address <> ''
    OR company <> ''
    OR title <> ''
    OR notes <> ''
  )
);

CREATE UNIQUE INDEX contacts_email_unique_idx
  ON contacts(lower(email_address))
  WHERE email_address <> '';
CREATE INDEX contacts_name_idx ON contacts(last_name, first_name);
CREATE INDEX contacts_company_idx ON contacts(company);
CREATE INDEX contacts_updated_idx ON contacts(updated_at DESC);

CREATE TABLE workspace_files (
  id bigserial PRIMARY KEY,
  relative_path text NOT NULL UNIQUE,
  size_bytes bigint NOT NULL DEFAULT 0,
  mtime_ns text NOT NULL DEFAULT '',
  content_sha256 text NOT NULL DEFAULT '',
  mime_type text,
  extension text NOT NULL DEFAULT '',
  index_status text NOT NULL DEFAULT 'pending'
    CHECK (index_status IN ('pending', 'indexed', 'embedding_failed', 'unsupported', 'error', 'deleted', 'superseded')),
  error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  indexed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX workspace_files_status_idx ON workspace_files(index_status, updated_at DESC);
CREATE INDEX workspace_files_extension_idx ON workspace_files(extension);

CREATE TABLE workspace_file_chunks (
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

CREATE INDEX workspace_file_chunks_file_idx ON workspace_file_chunks(file_id, chunk_index);

CREATE TABLE workspace_document_conversions (
  id bigserial PRIMARY KEY,
  original_relative_path text NOT NULL,
  markdown_relative_path text,
  archived_relative_path text,
  original_sha256 text,
  markdown_sha256 text,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'ready', 'failed', 'skipped')),
  source text NOT NULL DEFAULT 'workspace',
  error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX workspace_document_conversions_original_idx ON workspace_document_conversions(original_relative_path, created_at DESC);
CREATE INDEX workspace_document_conversions_markdown_idx ON workspace_document_conversions(markdown_relative_path, created_at DESC);

CREATE TABLE reminders (
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
  CONSTRAINT reminders_recurrence_check CHECK (
    (recurrence_unit IS NULL AND recurrence_interval IS NULL AND recurrence_anchor_day IS NULL)
    OR (
      recurrence_unit IN ('hour', 'day', 'week', 'month')
      AND recurrence_interval IS NOT NULL
      AND recurrence_interval > 0
      AND (recurrence_anchor_day IS NULL OR recurrence_anchor_day BETWEEN 1 AND 31)
    )
  ),
  created_by text NOT NULL DEFAULT 'agent',
  created_by_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  embedding double precision[],
  embedding_model text,
  embedding_dimensions int,
  embedding_updated_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  queued_at timestamptz,
  completed_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX reminders_status_run_at_idx ON reminders(status, run_at);
CREATE INDEX reminders_job_id_idx ON reminders(job_id);

CREATE TABLE calendar_managed_events (
  assistant_id text PRIMARY KEY,
  uid text NOT NULL UNIQUE,
  calendar_name text NOT NULL,
  relative_path text NOT NULL,
  summary text NOT NULL DEFAULT '',
  starts_at timestamptz NOT NULL,
  ends_at timestamptz NOT NULL,
  file_hash text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'deleted')),
  created_by_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  updated_by_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  deleted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX calendar_managed_events_status_starts_idx ON calendar_managed_events(status, starts_at);
CREATE INDEX calendar_managed_events_job_idx ON calendar_managed_events(created_by_job_id, updated_by_job_id);

CREATE TABLE calendar_event_audit (
  id bigserial PRIMARY KEY,
  assistant_id text NOT NULL REFERENCES calendar_managed_events(assistant_id) ON DELETE CASCADE,
  job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  action text NOT NULL
    CHECK (action IN ('created', 'updated', 'deleted')),
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX calendar_event_audit_event_created_idx ON calendar_event_audit(assistant_id, created_at DESC);
CREATE INDEX calendar_event_audit_job_created_idx ON calendar_event_audit(job_id, created_at DESC);

CREATE TABLE projects (
  id bigserial PRIMARY KEY,
  original_job_id bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  original_thread_id text NOT NULL,
  title text NOT NULL,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
  priority int NOT NULL DEFAULT 0,
  run_at timestamptz NOT NULL DEFAULT now(),
  locked_at timestamptz,
  locked_by text,
  result_summary text,
  embedding double precision[],
  embedding_model text,
  embedding_dimensions int,
  embedding_updated_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  completed_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX projects_status_run_at_idx ON projects(status, run_at);
CREATE INDEX projects_original_job_idx ON projects(original_job_id);

CREATE TABLE project_tasks (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  sequence int NOT NULL,
  title text NOT NULL,
  task text NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'queued', 'running', 'completed', 'failed', 'cancelled')),
  job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  priority int NOT NULL DEFAULT 0,
  result_summary text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  queued_at timestamptz,
  completed_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, sequence)
);

CREATE INDEX project_tasks_project_sequence_idx ON project_tasks(project_id, sequence);
CREATE INDEX project_tasks_job_idx ON project_tasks(job_id);
CREATE INDEX project_tasks_status_idx ON project_tasks(status);

CREATE TABLE deep_research_runs (
  id bigserial PRIMARY KEY,
  original_job_id bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  original_thread_id text NOT NULL,
  title text NOT NULL,
  research_question text NOT NULL,
  instructions text,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'waiting_for_input', 'completed', 'failed', 'cancelled')),
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

CREATE INDEX deep_research_runs_status_run_at_idx ON deep_research_runs(status, run_at);
CREATE INDEX deep_research_runs_original_job_idx ON deep_research_runs(original_job_id);

CREATE TABLE deep_research_events (
  id bigserial PRIMARY KEY,
  run_id bigint NOT NULL REFERENCES deep_research_runs(id) ON DELETE CASCADE,
  sequence int NOT NULL,
  event_type text NOT NULL
    CHECK (event_type IN (
      'llm_request',
      'llm_response',
      'tool_call',
      'tool_result',
      'search_request',
      'search_result',
      'error',
      'status_change'
    )),
  tool_name text,
  input_data jsonb,
  output_data jsonb,
  tokens_used jsonb,
  duration_ms int,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (run_id, sequence)
);

CREATE INDEX deep_research_events_run_sequence_idx ON deep_research_events(run_id, sequence);
CREATE INDEX deep_research_events_run_created_idx ON deep_research_events(run_id, created_at);

CREATE TABLE task_logs (
  id bigserial PRIMARY KEY,
  job_id bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  sequence int NOT NULL,
  event_type text NOT NULL
    CHECK (event_type IN (
      'llm_request',
      'llm_response',
      'tool_call',
      'tool_result',
      'error',
      'timeout',
      'status_change',
      'supervisor_note'
    )),
  tool_name text,
  tool_action text,
  input_data jsonb,
  output_data jsonb,
  tokens_used jsonb,
  duration_ms int,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (job_id, sequence)
);

CREATE INDEX task_logs_job_sequence_idx ON task_logs(job_id, sequence);
CREATE INDEX task_logs_job_created_idx ON task_logs(job_id, created_at);

CREATE TABLE thread_summaries (
  thread_id text PRIMARY KEY,
  summary text NOT NULL,
  email_count int NOT NULL,
  last_email_id bigint NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent_checkpoints (
  id bigserial PRIMARY KEY,
  job_id bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  message_history jsonb NOT NULL,
  iteration_count int NOT NULL DEFAULT 0,
  token_count int NOT NULL DEFAULT 0,
  reason text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX agent_checkpoints_job_created_idx ON agent_checkpoints(job_id, created_at DESC);

CREATE TABLE supervisor_instructions (
  id bigserial PRIMARY KEY,
  job_id bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  instruction text NOT NULL,
  created_by text NOT NULL DEFAULT 'supervisor',
  consumed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX supervisor_instructions_pending_idx
  ON supervisor_instructions(job_id, created_at)
  WHERE consumed_at IS NULL;

CREATE TABLE outbound_email_logs (
  id bigserial PRIMARY KEY,
  job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  to_addresses text[] NOT NULL,
  cc_addresses text[] NOT NULL DEFAULT ARRAY[]::text[],
  subject text NOT NULL,
  body_text text NOT NULL,
  in_reply_to text,
  attachments jsonb NOT NULL DEFAULT '[]'::jsonb,
  provider_message_id text,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'sent', 'failed', 'blocked')),
  blocked_reason text,
  embedding double precision[],
  embedding_model text,
  embedding_dimensions int,
  embedding_updated_at timestamptz,
  sent_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX outbound_email_logs_job_idx ON outbound_email_logs(job_id);
CREATE INDEX outbound_email_logs_created_idx ON outbound_email_logs(created_at);
CREATE INDEX outbound_email_logs_provider_message_idx ON outbound_email_logs(provider_message_id);

CREATE TABLE manual_events (
  id bigserial PRIMARY KEY,
  event_type text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX manual_events_created_idx ON manual_events(created_at);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER jobs_set_updated_at
BEFORE UPDATE ON jobs
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER processed_artifacts_set_updated_at
BEFORE UPDATE ON processed_artifacts
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER agent_memories_set_updated_at
BEFORE UPDATE ON agent_memories
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER agent_notes_set_updated_at
BEFORE UPDATE ON agent_notes
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER contacts_set_updated_at
BEFORE UPDATE ON contacts
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER reminders_set_updated_at
BEFORE UPDATE ON reminders
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER calendar_managed_events_set_updated_at
BEFORE UPDATE ON calendar_managed_events
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER projects_set_updated_at
BEFORE UPDATE ON projects
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER project_tasks_set_updated_at
BEFORE UPDATE ON project_tasks
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER deep_research_runs_set_updated_at
BEFORE UPDATE ON deep_research_runs
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER thread_summaries_set_updated_at
BEFORE UPDATE ON thread_summaries
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER runtime_state_set_updated_at
BEFORE UPDATE ON runtime_state
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER workspace_files_set_updated_at
BEFORE UPDATE ON workspace_files
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER workspace_document_conversions_set_updated_at
BEFORE UPDATE ON workspace_document_conversions
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();
