# Concurrent job processing

Each `task-agent` container runs one worker. Set `TASK_AGENT_WORKERS` in
`data/.env`; Compose uses it as the service replica count:

```env
TASK_AGENT_WORKERS=3
```

```bash
docker compose --env-file data/.env up -d --build task-agent
docker compose --env-file data/.env ps task-agent
```

Workers atomically claim due queued jobs in active workstreams through PostgreSQL
row locking (`FOR UPDATE SKIP LOCKED`), highest priority first and oldest first
within a priority. Research-worker jobs use their own worker and queue.

A partial unique index allows only one open job per thread, but different
threads in the same workstream may run concurrently. Database claims prevent two
workers claiming the same queued job at once; they do not guarantee exactly-once
external effects after retries or coordinate arbitrary shared-file writes.

Versioned object update/delete tools require `expected_version` from the latest
read. A stale version is rejected; refresh the object before retrying rather
than overwriting another worker's changes.

More workers increase database connections, CPU/memory use, and model-provider
requests; choose a count appropriate to host capacity and provider limits. See
[architecture](architecture.md) for service roles.
