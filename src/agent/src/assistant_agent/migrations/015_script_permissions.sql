CREATE TABLE sandbox_scripts (
    id bigserial PRIMARY KEY,
    entry text NOT NULL UNIQUE,
    blocked boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE sandbox_script_versions (
    id bigserial PRIMARY KEY,
    script_id bigint NOT NULL REFERENCES sandbox_scripts(id) ON DELETE CASCADE,
    fingerprint text NOT NULL UNIQUE,
    manifest jsonb NOT NULL,
    purpose text NOT NULL,
    reusable boolean NOT NULL,
    screening jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE sandbox_permission_grants (
    id bigserial PRIMARY KEY,
    version_id bigint REFERENCES sandbox_script_versions(id) ON DELETE CASCADE,
    job_id bigint REFERENCES jobs(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('run', 'host')),
    target text NOT NULL DEFAULT '',
    scope text NOT NULL CHECK (scope IN ('once', 'script', 'global')),
    decision text NOT NULL CHECK (decision IN ('allow', 'block')),
    created_by text NOT NULL,
    note text NOT NULL DEFAULT '',
    consumed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (scope <> 'once' OR (job_id IS NOT NULL AND version_id IS NOT NULL)),
    CHECK (scope <> 'script' OR version_id IS NOT NULL),
    CHECK (kind <> 'run' OR (version_id IS NOT NULL AND scope <> 'global'))
);
CREATE INDEX sandbox_permission_grants_lookup ON sandbox_permission_grants(version_id, job_id, kind);
CREATE TABLE sandbox_permission_history (
    id bigserial PRIMARY KEY,
    actor text NOT NULL,
    action text NOT NULL,
    details jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
