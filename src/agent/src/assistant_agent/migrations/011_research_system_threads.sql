ALTER TABLE deep_research_runs
  ADD COLUMN execution_job_id bigint UNIQUE REFERENCES jobs(id) ON DELETE SET NULL;

CREATE FUNCTION workstream_research_execution() RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE TRIGGER workstream_research_execution
  BEFORE INSERT OR UPDATE OF status ON deep_research_runs
  FOR EACH ROW EXECUTE FUNCTION workstream_research_execution();

CREATE FUNCTION workstream_research_execution_status() RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE TRIGGER workstream_research_execution_status
  AFTER INSERT OR UPDATE OF status ON deep_research_runs
  FOR EACH ROW EXECUTE FUNCTION workstream_research_execution_status();

UPDATE deep_research_runs SET status = status WHERE original_job_id IS NOT NULL;

CREATE FUNCTION workstream_research_job_control() RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE TRIGGER workstream_research_job_control
  AFTER UPDATE OF status ON jobs
  FOR EACH ROW EXECUTE FUNCTION workstream_research_job_control();
