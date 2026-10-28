ALTER TABLE workstreams DROP CONSTRAINT workstreams_status_check;
ALTER TABLE workstreams ADD CONSTRAINT workstreams_status_check
    CHECK (status IN ('active', 'archived', 'deleting'));
