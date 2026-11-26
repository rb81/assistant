ALTER TABLE task_logs DROP CONSTRAINT task_logs_event_type_check;
ALTER TABLE task_logs ADD CONSTRAINT task_logs_event_type_check CHECK (
  event_type IN ('llm_request', 'llm_response', 'model_service_request',
                'model_service_response', 'tool_call', 'tool_result', 'error',
                'timeout', 'status_change', 'supervisor_note')
);
