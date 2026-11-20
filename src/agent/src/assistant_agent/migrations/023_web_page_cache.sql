CREATE TABLE web_page_cache (
  job_id bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  requested_url text NOT NULL,
  final_url text NOT NULL,
  title text NOT NULL,
  markdown text NOT NULL,
  review_fingerprint text,
  review_summary text,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, requested_url)
);

CREATE INDEX web_page_cache_fetched_at ON web_page_cache (fetched_at);
