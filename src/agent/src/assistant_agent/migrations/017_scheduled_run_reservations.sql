ALTER TABLE sandbox_permission_grants
    ADD COLUMN IF NOT EXISTS reservation_id text;
