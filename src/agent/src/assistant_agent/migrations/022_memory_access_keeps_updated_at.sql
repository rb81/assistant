-- Reading a memory (last_accessed_at) is not an edit and must not bump updated_at,
-- which drives recency ranking.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF to_jsonb(NEW) ? 'embedding'
         AND NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at
         AND (to_jsonb(NEW) - ARRAY['embedding', 'embedding_model', 'embedding_dimensions', 'embedding_updated_at', 'metadata', 'last_accessed_at'])
                 = (to_jsonb(OLD) - ARRAY['embedding', 'embedding_model', 'embedding_dimensions', 'embedding_updated_at', 'metadata', 'last_accessed_at'])
         AND (COALESCE(to_jsonb(NEW)->'metadata', '{}'::jsonb) - 'embedding_pending')
                 = (COALESCE(to_jsonb(OLD)->'metadata', '{}'::jsonb) - 'embedding_pending') THEN
        RETURN NEW;
    END IF;
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;
