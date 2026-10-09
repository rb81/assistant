CREATE TABLE IF NOT EXISTS zoom_managed_meetings (
  id bigserial PRIMARY KEY,
  request_key text NOT NULL UNIQUE,
  request_payload jsonb NOT NULL,
  provider_meeting_id text UNIQUE,
  title text NOT NULL,
  starts_at timestamptz NOT NULL,
  duration_minutes int NOT NULL CHECK (duration_minutes BETWEEN 1 AND 1440),
  join_url text,
  status text NOT NULL DEFAULT 'creating' CHECK (status IN ('creating', 'active', 'failed', 'uncertain', 'deleted')),
  calendar_event_id text REFERENCES calendar_managed_events(assistant_id) ON DELETE SET NULL,
  created_by_job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now()
);