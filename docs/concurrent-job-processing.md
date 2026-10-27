# Concurrent job processing

The `task-agent` service can run multiple workers. Set the Compose deployment
setting `TASK_AGENT_WORKERS` in `data/.env`, then recreate the service:

```env
TASK_AGENT_WORKERS=3
```

```bash
docker compose --env-file data/.env up -d --build --scale task-agent=3
docker compose --env-file data/.env ps task-agent
```

Workers claim queued jobs through PostgreSQL row locking. More workers increase
database connections, CPU/memory use, and model-provider requests; choose a
count appropriate to host capacity and provider limits. See
[architecture](architecture.md) for service roles.
