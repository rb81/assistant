CREATE OR REPLACE FUNCTION invalidate_source_embedding() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  field_name text;
  before_data jsonb := to_jsonb(OLD);
  after_data jsonb := to_jsonb(NEW);
  changed boolean := false;
BEGIN
  FOREACH field_name IN ARRAY TG_ARGV LOOP
    IF field_name = 'metadata' THEN
      changed := changed OR (before_data->'metadata'->'final_response'
                          IS DISTINCT FROM after_data->'metadata'->'final_response');
    ELSE
      changed := changed OR (before_data->field_name IS DISTINCT FROM after_data->field_name);
    END IF;
  END LOOP;
  IF changed AND NEW.embedding IS NOT DISTINCT FROM OLD.embedding THEN
    NEW.embedding := NULL;
    NEW.embedding_model := NULL;
    NEW.embedding_dimensions := NULL;
    NEW.embedding_updated_at := NULL;
  END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER invalidate_embedding BEFORE UPDATE ON agent_memories
  FOR EACH ROW EXECUTE FUNCTION invalidate_source_embedding('content');
CREATE OR REPLACE TRIGGER invalidate_embedding BEFORE UPDATE ON workspace_file_chunks
  FOR EACH ROW EXECUTE FUNCTION invalidate_source_embedding('content');
CREATE OR REPLACE TRIGGER invalidate_embedding BEFORE UPDATE ON jobs
  FOR EACH ROW EXECUTE FUNCTION invalidate_source_embedding('task_summary', 'metadata');
CREATE OR REPLACE TRIGGER invalidate_embedding BEFORE UPDATE ON reminders
  FOR EACH ROW EXECUTE FUNCTION invalidate_source_embedding('title', 'task');
CREATE OR REPLACE TRIGGER invalidate_embedding BEFORE UPDATE ON outbound_email_logs
  FOR EACH ROW EXECUTE FUNCTION invalidate_source_embedding('to_addresses', 'subject', 'body_text');
CREATE OR REPLACE TRIGGER invalidate_embedding BEFORE UPDATE ON emails
  FOR EACH ROW EXECUTE FUNCTION invalidate_source_embedding('from_address', 'subject', 'body_text', 'body_html');
CREATE OR REPLACE TRIGGER invalidate_embedding BEFORE UPDATE ON contacts
  FOR EACH ROW EXECUTE FUNCTION invalidate_source_embedding('first_name', 'last_name', 'email_address', 'company', 'title', 'notes');

CREATE OR REPLACE FUNCTION notify_embedding_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.embedding IS NULL THEN
    PERFORM pg_notify('assistant_embeddings', '');
  END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER notify_embedding AFTER INSERT OR UPDATE ON agent_memories
  FOR EACH ROW EXECUTE FUNCTION notify_embedding_change();
CREATE OR REPLACE TRIGGER notify_embedding AFTER INSERT OR UPDATE ON workspace_file_chunks
  FOR EACH ROW EXECUTE FUNCTION notify_embedding_change();
CREATE OR REPLACE TRIGGER notify_embedding AFTER INSERT OR UPDATE ON jobs
  FOR EACH ROW EXECUTE FUNCTION notify_embedding_change();
CREATE OR REPLACE TRIGGER notify_embedding AFTER INSERT OR UPDATE ON reminders
  FOR EACH ROW EXECUTE FUNCTION notify_embedding_change();
CREATE OR REPLACE TRIGGER notify_embedding AFTER INSERT OR UPDATE ON outbound_email_logs
  FOR EACH ROW EXECUTE FUNCTION notify_embedding_change();
CREATE OR REPLACE TRIGGER notify_embedding AFTER INSERT OR UPDATE ON emails
  FOR EACH ROW EXECUTE FUNCTION notify_embedding_change();
CREATE OR REPLACE TRIGGER notify_embedding AFTER INSERT OR UPDATE ON contacts
  FOR EACH ROW EXECUTE FUNCTION notify_embedding_change();
