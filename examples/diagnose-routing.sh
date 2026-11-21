#!/bin/sh
# Read-only routing metadata; no message bodies, addresses, approval tokens, or credentials.
set -eu

if [ "$#" -gt 1 ]; then
    printf '%s\n' 'Usage: sh diagnose-routing.sh [positive-task-id]' >&2
    exit 2
fi
case "${1:-}" in
    '') ;;
    *[!0-9]*|0) printf '%s\n' 'Task ID must be a positive integer.' >&2; exit 2 ;;
esac

container=${ASSISTANT_DIAGNOSTIC_CONTAINER:-}
if [ -z "$container" ]; then
    candidates=$(docker ps -q \
        --filter "label=com.docker.compose.project=${ASSISTANT_COMPOSE_PROJECT:-assistant}" \
        --filter label=com.docker.compose.service=task-agent)
    count=$(printf '%s\n' "$candidates" | awk 'NF { n++ } END { print n+0 }')
    if [ "$count" -ne 1 ]; then
        printf '%s\n' 'Could not identify exactly one running task-agent. Set ASSISTANT_DIAGNOSTIC_CONTAINER to its container ID.' >&2
        exit 1
    fi
    container=$candidates
fi

docker exec -i "$container" python - "${1:-}" <<'PY'
import json
import sys
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from assistant_agent.config import database_url, load_config

task_id = int(sys.argv[1]) if sys.argv[1] else None
if task_id is not None and task_id <= 0:
    raise SystemExit("Task ID must be a positive integer.")

config = load_config()
settings = {
    key: config.get(key) for key in (
        "agent.approvals.channel",
        "agent.client.poll_interval_seconds",
        "agent.client.availability_timeout_seconds",
        "agent.client.delivery_timeout_seconds",
        "agent.client.reconciliation_interval_seconds",
        "agent.heartbeat.admin_digest_interval_hours",
    )
}

with psycopg.connect(database_url(config), row_factory=dict_row, connect_timeout=10) as connection:
    connection.read_only = True
    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    connection.execute("SET LOCAL statement_timeout = '10s'")
    report = {"captured_at": datetime.now(timezone.utc).isoformat(), "settings": settings}
    if task_id is None:
        report["recent_tasks"] = connection.execute("""
            SELECT id, status, created_at, updated_at, completed_at,
                   metadata->'reply_route'->>'channel' AS reply_channel
            FROM jobs ORDER BY created_at DESC, id DESC LIMIT 20
        """).fetchall()
    else:
        task = connection.execute("""
            SELECT id, status, created_at, updated_at, completed_at,
                   metadata->'reply_route'->>'channel' AS reply_channel,
                   metadata->>'auto_reply' AS auto_reply,
                   last_error IS NOT NULL AS has_error
            FROM jobs WHERE id = %s
        """, (task_id,)).fetchone()
        if task is None:
            raise SystemExit("Task not found.")
        report["task"] = task
        report["clients"] = connection.execute("""
            SELECT c.kind, c.fallback_channels, c.last_poll_at, c.disabled
            FROM client_channels c JOIN jobs j ON j.id = %s
            WHERE c.id = j.metadata->'reply_route'->>'client_id' OR EXISTS (
                SELECT 1 FROM channel_deliveries d
                WHERE d.job_id = j.id AND d.channel = 'client' AND d.destination = c.id
            ) LIMIT 10
        """, (task_id,)).fetchall()
        report["requests"] = connection.execute("""
            SELECT r.id, r.channel, r.received_at,
                   r.reply_route->>'channel' AS reply_channel
            FROM request_messages r JOIN jobs j ON j.thread_id = r.thread_id
            WHERE j.id = %s ORDER BY r.received_at, r.id LIMIT 100
        """, (task_id,)).fetchall()
        report["deliveries"] = connection.execute("""
            SELECT id, request_id, channel, purpose, status, created_at, offered_at,
                   sent_at, fallback_channel, char_length(body_text) AS body_characters,
                   error IS NOT NULL AS has_error
            FROM channel_deliveries WHERE job_id = %s ORDER BY id LIMIT 100
        """, (task_id,)).fetchall()
        report["thread_deliveries"] = connection.execute("""
            SELECT d.job_id, d.channel, d.purpose, d.status, d.created_at, d.sent_at,
                   d.fallback_channel, char_length(d.body_text) AS body_characters
            FROM channel_deliveries d JOIN jobs j ON j.id = d.job_id
            WHERE j.thread_id = (SELECT thread_id FROM jobs WHERE id = %s)
            ORDER BY d.created_at DESC, d.id DESC LIMIT 30
        """, (task_id,)).fetchall()
        report["emails"] = connection.execute("""
            SELECT id, status, created_at, sent_at,
                   char_length(body_text) AS body_characters,
                   blocked_reason IS NOT NULL AS blocked
            FROM outbound_email_logs WHERE job_id = %s ORDER BY id LIMIT 100
        """, (task_id,)).fetchall()
        report["events"] = connection.execute("""
            SELECT event_type, tool_name, created_at,
                   CASE WHEN tool_name = 'task_complete' AND event_type = 'error' THEN
                       CASE
                           WHEN output_data->>'error' LIKE 'Cannot complete with an unfinished development session%%'
                               THEN 'unfinished_development_session'
                           WHEN output_data->>'error' LIKE 'Cannot complete yet: the latest external requester%%'
                               THEN 'requester_email_not_sent'
                           WHEN output_data->>'error' LIKE 'task_complete requires a non-empty response%%'
                               THEN 'missing_final_response'
                           WHEN output_data->>'error' LIKE 'New requester context arrived%%'
                               THEN 'new_requester_context'
                           WHEN output_data->>'error' LIKE 'Final reply was not accepted%%'
                               THEN 'final_delivery_not_accepted'
                           ELSE 'other_completion_error'
                       END
                   END AS completion_blocker,
                   CASE WHEN output_data->>'notification' IN (
                       'admin_failure_email', 'script_permission'
                   ) THEN output_data->>'notification' END AS notification,
                   CASE output_data->>'sent'
                       WHEN 'true' THEN true WHEN 'false' THEN false
                   END AS notification_sent
            FROM task_logs WHERE job_id = %s
              AND (event_type IN ('error', 'timeout', 'status_change', 'supervisor_note')
                   OR tool_name IN ('task_complete', 'task_failed', 'request_input',
                                    'script_approval_request', 'script_development_review',
                                    'development_session_request'))
            ORDER BY created_at DESC, id DESC LIMIT 100
        """, (task_id,)).fetchall()
        report["retained_approvals"] = connection.execute("""
            SELECT a.updated_at, a.value->>'channel' AS approval_channel,
                   a.value->'payload'->>'kind' AS kind,
                   a.value->'payload'->>'permission_type' AS permission_type,
                   a.value->>'expires' AS expires,
                   s.value->>'status' AS delivery_status,
                   s.value->>'email_fallback' = 'true' AS email_fallback
            FROM runtime_state a LEFT JOIN runtime_state s ON s.key = a.value->>'source_key'
            WHERE a.key LIKE 'email-approval:%%'
              AND a.value->'payload'->>'job_id' = %s::text
            ORDER BY a.updated_at DESC LIMIT 20
        """, (task_id,)).fetchall()
    print(json.dumps(report, default=str, indent=2))
PY
