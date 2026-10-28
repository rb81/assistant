CREATE TABLE sandbox_network_rules (
  id bigserial PRIMARY KEY,
  host text NOT NULL CHECK (host = lower(host) AND length(host) BETWEEN 1 AND 260),
  decision text NOT NULL CHECK (decision IN ('allow', 'block')),
  job_id bigint REFERENCES jobs(id) ON DELETE CASCADE,
  note text,
  created_by text NOT NULL DEFAULT 'admin',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX sandbox_network_rules_global_host_idx ON sandbox_network_rules(host) WHERE job_id IS NULL;
CREATE UNIQUE INDEX sandbox_network_rules_job_host_idx ON sandbox_network_rules(job_id, host) WHERE job_id IS NOT NULL;
