CREATE TABLE IF NOT EXISTS chat_sessions (
  id         bigserial PRIMARY KEY,
  title      text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_messages (
  id          bigserial PRIMARY KEY,
  session_id  bigint NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  role        text NOT NULL CHECK (role IN ('user', 'assistant')),
  kind        text NOT NULL DEFAULT 'chat' CHECK (kind IN ('chat', 'job_ref')),
  content     text NOT NULL DEFAULT '',
  job_id      bigint REFERENCES jobs(id) ON DELETE SET NULL,
  tokens_used jsonb,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chat_messages_session_idx ON chat_messages(session_id, id);
