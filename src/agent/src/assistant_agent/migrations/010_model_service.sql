CREATE TABLE model_presets (
  name text PRIMARY KEY,
  model text NOT NULL,
  system_prompt text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE model_service_calls (
  id uuid PRIMARY KEY,
  job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  preset jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'completed', 'failed', 'unknown')),
  cost_usd numeric CHECK (cost_usd >= 0),
  usage jsonb,
  generation_id text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX model_service_calls_created_at_idx ON model_service_calls(created_at);

ALTER TABLE task_logs DROP CONSTRAINT task_logs_event_type_check;
ALTER TABLE task_logs ADD CONSTRAINT task_logs_event_type_check CHECK (
  event_type IN ('llm_request', 'llm_response', 'model_service_response', 'tool_call',
                'tool_result', 'error', 'timeout', 'status_change', 'supervisor_note')
);
