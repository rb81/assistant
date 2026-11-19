CREATE TABLE micro_skills (
    id text PRIMARY KEY,
    title text NOT NULL,
    description text NOT NULL,
    body text NOT NULL,
    revision bigint NOT NULL DEFAULT 1,
    overlay_for text,
    deleted_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX micro_skills_active_overlay
    ON micro_skills(overlay_for) WHERE overlay_for IS NOT NULL AND deleted_at IS NULL;

CREATE TABLE micro_skill_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    skill_id text NOT NULL REFERENCES micro_skills(id),
    revision bigint NOT NULL,
    action text NOT NULL CHECK (action IN ('create', 'edit', 'delete')),
    actor text NOT NULL,
    title text NOT NULL,
    description text NOT NULL,
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (skill_id, revision)
);

CREATE TABLE micro_skill_embeddings (
    skill_id text PRIMARY KEY,
    fingerprint text NOT NULL,
    embedding_model text NOT NULL,
    embedding_dimensions integer NOT NULL,
    embedding double precision[] NOT NULL
);
