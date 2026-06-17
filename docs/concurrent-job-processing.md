# Concurrent Job Processing

The assistant supports running multiple task agent workers in parallel to process jobs concurrently.

## Configuration

Set the `TASK_AGENT_WORKERS` environment variable to control the number of concurrent workers:

```bash
# In your .env file
TASK_AGENT_WORKERS=3
```

**Default:** 1 (single worker, synchronous processing)

**Recommended values:**
- **2-4 workers:** Moderate concurrency for typical workloads
- **5-8 workers:** Heavy workloads with many simultaneous jobs
- **1 worker:** Development or low-traffic environments

## How It Works

Multiple task-agent containers run simultaneously, each polling the database for available jobs. The existing job claiming mechanism ensures each job is processed by only one worker:

1. Each worker polls for `queued` jobs
2. Workers atomically claim jobs using database transactions
3. Jobs are processed independently across workers
4. No code changes required - the architecture already supports this

## Applying Changes

After updating `TASK_AGENT_WORKERS` in your `.env` file:

```bash
# Rebuild and restart the task-agent service
docker compose up -d --scale task-agent=${TASK_AGENT_WORKERS}

# Or simply restart all services
docker compose down
docker compose up -d
```

## Monitoring

You can verify the number of running workers:

```bash
docker compose ps task-agent
```

Each worker will appear as a separate container (e.g., `task-agent-1`, `task-agent-2`, etc.).

## Resource Considerations

- Each worker consumes CPU, memory, and database connections
- Monitor system resources when scaling workers
- Each worker needs access to Ollama, Sandbox, and PostgreSQL
- Consider your API rate limits when running multiple workers

## Limitations

- All workers share the same volumes and configuration
- High worker counts may increase database connection pressure
- Calendar sync and other shared resources require coordination (already handled by the application)
