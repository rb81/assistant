CREATE TABLE client_channels (
  id text PRIMARY KEY CHECK (id ~ '^[a-f0-9]{32}$'),
  name text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('desktop', 'cli', 'tui', 'custom')),
  fallback_channels jsonb NOT NULL DEFAULT '["telegram", "email"]'::jsonb,
  last_poll_at timestamptz,
  disabled boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE channel_deliveries ADD COLUMN offered_at timestamptz;
ALTER TABLE channel_deliveries ADD COLUMN fallback_channel text;
CREATE INDEX client_pending_deliveries ON channel_deliveries(destination, id)
  WHERE channel = 'client' AND status = 'pending';
ALTER TABLE workstreams DROP CONSTRAINT workstreams_origin_channel_check;
ALTER TABLE workstreams ADD CONSTRAINT workstreams_origin_channel_check
  CHECK (origin_channel IN ('telegram', 'email', 'local', 'client'));
