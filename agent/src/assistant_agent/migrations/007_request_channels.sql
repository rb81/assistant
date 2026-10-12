CREATE TABLE IF NOT EXISTS request_messages (
  id bigserial PRIMARY KEY,
  channel text NOT NULL,
  external_id text NOT NULL,
  conversation_id text NOT NULL,
  sender_id text NOT NULL,
  thread_id text NOT NULL,
  email_id bigint UNIQUE REFERENCES emails(id) ON DELETE CASCADE,
  body_text text NOT NULL DEFAULT '',
  attachments jsonb NOT NULL DEFAULT '[]'::jsonb,
  reply_route jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'ready' CHECK (status IN ('pending', 'processing', 'ready', 'failed')),
  attempts int NOT NULL DEFAULT 0,
  available_at timestamptz NOT NULL DEFAULT now(),
  locked_at timestamptz,
  last_error text,
  received_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(channel, external_id)
);
CREATE INDEX IF NOT EXISTS request_messages_thread_idx ON request_messages(thread_id, received_at, id);
CREATE INDEX IF NOT EXISTS request_messages_pending_idx ON request_messages(status, available_at);

CREATE TABLE IF NOT EXISTS channel_deliveries (
  id bigserial PRIMARY KEY,
  job_id bigint REFERENCES jobs(id) ON DELETE CASCADE,
  request_id bigint REFERENCES request_messages(id) ON DELETE CASCADE,
  channel text NOT NULL,
  destination text NOT NULL,
  purpose text NOT NULL DEFAULT 'message',
  request_key text UNIQUE NOT NULL,
  body_text text NOT NULL DEFAULT '',
  attachments jsonb NOT NULL DEFAULT '[]'::jsonb,
  provider_message_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'uncertain')),
  error text,
  available_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz
);
CREATE INDEX IF NOT EXISTS channel_deliveries_job_idx ON channel_deliveries(job_id, status);
CREATE INDEX IF NOT EXISTS channel_deliveries_reply_idx ON channel_deliveries(channel, destination);

INSERT INTO request_messages(channel, external_id, conversation_id, sender_id, thread_id,
                             email_id, body_text, attachments, reply_route, received_at, created_at)
SELECT CASE WHEN from_address LIKE '%@local' THEN 'local' ELSE 'email' END,
       message_id, thread_id, from_address, thread_id, id, COALESCE(body_text, ''), attachments,
       CASE WHEN from_address LIKE '%@local' THEN '{}'::jsonb
            ELSE jsonb_build_object('channel', 'email', 'to', from_address,
                                    'in_reply_to', message_id, 'subject', COALESCE(subject, 'Reply')) END,
       received_at, created_at
FROM emails
ON CONFLICT DO NOTHING;
UPDATE request_messages SET metadata = metadata || '{"queued":true}'::jsonb WHERE email_id IS NOT NULL;

ALTER TABLE processed_artifacts ADD COLUMN IF NOT EXISTS request_id bigint REFERENCES request_messages(id) ON DELETE CASCADE;
UPDATE processed_artifacts a SET request_id = r.id
FROM request_messages r WHERE r.email_id = a.email_id AND a.request_id IS NULL;
ALTER TABLE processed_artifacts ALTER COLUMN email_id DROP NOT NULL;
ALTER TABLE processed_artifacts ADD CONSTRAINT processed_artifacts_source_required
  CHECK (email_id IS NOT NULL OR request_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS processed_artifacts_request_idx ON processed_artifacts(request_id, id);

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS trigger_request_id bigint REFERENCES request_messages(id) ON DELETE SET NULL;
UPDATE jobs j SET trigger_request_id = r.id
FROM request_messages r WHERE r.email_id = j.trigger_email_id AND j.trigger_request_id IS NULL;
UPDATE jobs j SET trigger_request_id = (
  SELECT r.id FROM request_messages r WHERE r.thread_id = j.thread_id ORDER BY r.id LIMIT 1
) WHERE j.trigger_request_id IS NULL;
UPDATE jobs j SET metadata = j.metadata || jsonb_build_object('source_channel', r.channel, 'reply_route', r.reply_route)
FROM request_messages r
WHERE r.id = j.trigger_request_id AND NOT (j.metadata ? 'source_channel');

INSERT INTO channel_deliveries(job_id, channel, destination, purpose, request_key, body_text, attachments,
                              provider_message_ids, status, error, created_at, sent_at)
SELECT job_id, 'email', array_to_string(to_addresses, ','), 'legacy', 'email-log:' || id,
       COALESCE(body_text, ''), attachments,
       CASE WHEN provider_message_id IS NULL THEN '[]'::jsonb ELSE jsonb_build_array(provider_message_id) END,
       CASE WHEN status = 'sent' THEN 'sent' WHEN status = 'pending' THEN 'uncertain' ELSE 'failed' END,
       blocked_reason, created_at, sent_at
FROM outbound_email_logs ON CONFLICT (request_key) DO NOTHING;

CREATE OR REPLACE FUNCTION mirror_email_request() RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS emails_request_mirror ON emails;
CREATE TRIGGER emails_request_mirror AFTER INSERT OR UPDATE OF body_text, attachments, thread_id ON emails
  FOR EACH ROW EXECUTE FUNCTION mirror_email_request();

CREATE OR REPLACE FUNCTION link_email_artifact_request() RETURNS trigger AS $$
BEGIN
  IF NEW.request_id IS NULL AND NEW.email_id IS NOT NULL THEN
    SELECT id INTO NEW.request_id FROM request_messages WHERE email_id = NEW.email_id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS artifacts_request_link ON processed_artifacts;
CREATE TRIGGER artifacts_request_link BEFORE INSERT ON processed_artifacts
  FOR EACH ROW EXECUTE FUNCTION link_email_artifact_request();