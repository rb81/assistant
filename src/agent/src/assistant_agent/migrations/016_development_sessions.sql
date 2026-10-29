CREATE TABLE sandbox_development_sessions (
    id bigserial PRIMARY KEY,
    job_id bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    script_id bigint NOT NULL REFERENCES sandbox_scripts(id) ON DELETE CASCADE,
    initial_version_id bigint NOT NULL REFERENCES sandbox_script_versions(id),
    brief jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'finalizing', 'closed', 'denied', 'revoked')),
    expires_at timestamptz,
    max_runs integer NOT NULL CHECK (max_runs > 0),
    runs integer NOT NULL DEFAULT 0 CHECK (runs >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX sandbox_development_session_open
    ON sandbox_development_sessions(job_id, script_id)
    WHERE status IN ('pending', 'active', 'finalizing');
CREATE TABLE sandbox_development_reviews (
    session_id bigint NOT NULL REFERENCES sandbox_development_sessions(id) ON DELETE CASCADE,
    version_id bigint NOT NULL REFERENCES sandbox_script_versions(id) ON DELETE CASCADE,
    hosts jsonb NOT NULL,
    final boolean NOT NULL DEFAULT false,
    review jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, version_id, final)
);

-- Retain a migration audit, but never let old global grants authorize scripts.
INSERT INTO sandbox_permission_history(actor, action, details)
    SELECT 'migration', 'retire-global-host-allow', to_jsonb(g)
    FROM sandbox_permission_grants g
    WHERE kind = 'host' AND scope = 'global' AND decision = 'allow';
DELETE FROM sandbox_permission_grants
    WHERE kind = 'host' AND scope = 'global' AND decision = 'allow';
ALTER TABLE sandbox_permission_grants ADD CONSTRAINT sandbox_no_global_allow
    CHECK (scope <> 'global' OR decision = 'block');
