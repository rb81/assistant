#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

usage() {
  cat <<'EOF'
Usage: scripts/reset-databases.sh [options]

Reset Assistant's PostgreSQL database to an empty schema without deleting files.

Options:
  -y, --yes, --force  Skip the confirmation prompt.
  --no-restart        Do not restart app services that were running before reset.
  --discard-imap-position
                      Clear IMAP checkpoints too. Old mailbox messages may be
                      downloaded again when the downloader restarts.
  --discard-calendar-sync
                      Clear local calendar sync files and vdirsyncer status too.
                      Remote calendar data is not deleted.
  -h, --help          Show this help text.

This script does not delete data/share, data/ollama, or config files. It preserves
local calendar sync files unless --discard-calendar-sync is provided.
By default it preserves only IMAP last-UID checkpoints so old mailbox messages
are not immediately re-imported into the fresh database.
EOF
}

assume_yes=0
restart_services=1
preserve_imap_position=1
discard_calendar_sync=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    -y|--yes|--force)
      assume_yes=1
      ;;
    --no-restart)
      restart_services=0
      ;;
    --discard-imap-position)
      preserve_imap_position=0
      ;;
    --discard-calendar-sync)
      discard_calendar_sync=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [ ! -f database/schema.sql ]; then
  echo "database/schema.sql was not found. Run this script from the Assistant repository." >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  compose_cmd=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose_cmd=(docker-compose)
else
  echo "Docker Compose is required: install Docker with 'docker compose' or docker-compose." >&2
  exit 1
fi

writer_services=(
  agent-api
  downloader
  task-agent
  reminder-scheduler
  project-scheduler
  deep-research-agent
  supervisor
  heartbeat
)

running_services="$("${compose_cmd[@]}" ps --services --filter status=running 2>/dev/null || true)"

is_running() {
  local service="$1"
  printf '%s\n' "$running_services" | grep -qx "$service"
}

postgres_was_running=0
if is_running postgres; then
  postgres_was_running=1
fi

running_writers=()
for service in "${writer_services[@]}"; do
  if is_running "$service"; then
    running_writers+=("$service")
  fi
done

if [ "$assume_yes" -ne 1 ]; then
  cat <<'EOF'
This will permanently erase every PostgreSQL record in the Assistant Compose database:
emails, jobs, logs, memories, reminders, projects, research runs, and runtime state.

It will NOT delete shared workspace files, config files, attachments on disk,
local calendar sync files, or the Ollama model cache unless the matching discard
flag was provided.
EOF
  printf 'Type RESET to continue: '
  read -r confirmation
  if [ "$confirmation" != "RESET" ]; then
    echo "Cancelled."
    exit 0
  fi
fi

if [ "${#running_writers[@]}" -gt 0 ]; then
  echo "Stopping database writer services: ${running_writers[*]}"
  "${compose_cmd[@]}" stop "${running_writers[@]}"
fi

echo "Starting postgres if needed..."
"${compose_cmd[@]}" up -d postgres

echo "Waiting for postgres to accept connections..."
attempts=0
until "${compose_cmd[@]}" exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1'; do
  attempts=$((attempts + 1))
  if [ "$attempts" -ge 60 ]; then
    echo "Postgres did not become ready within 120 seconds." >&2
    exit 1
  fi
  sleep 2
done

saved_imap_state=""
if [ "$preserve_imap_position" -eq 1 ]; then
  echo "Saving IMAP downloader checkpoints..."
  saved_imap_state="$("${compose_cmd[@]}" exec -T postgres sh -c '
    set -eu
    export PGPASSWORD="${POSTGRES_PASSWORD:-}"
    psql -v ON_ERROR_STOP=1 -At -F "$(printf "\t")" -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<'"'"'SQL'"'"' 2>/dev/null || true
SELECT key, value::text
FROM runtime_state
WHERE key LIKE '"'"'imap:%:last_uid'"'"'
ORDER BY key;
SQL
  ')"
  if [ -n "$saved_imap_state" ]; then
    checkpoint_count="$(printf '%s\n' "$saved_imap_state" | sed '/^$/d' | wc -l | tr -d ' ')"
    echo "Saved ${checkpoint_count} IMAP checkpoint(s)."
  else
    echo "No IMAP checkpoints found."
  fi
fi

echo "Dropping and recreating the public schema..."
"${compose_cmd[@]}" exec -T postgres sh -c '
  set -eu
  export PGPASSWORD="${POSTGRES_PASSWORD:-}"
  psql -v ON_ERROR_STOP=1 -v app_user="$POSTGRES_USER" -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<'"'"'SQL'"'"'
DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;
GRANT ALL ON SCHEMA public TO :"app_user";
GRANT ALL ON SCHEMA public TO public;
SQL
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /docker-entrypoint-initdb.d/001_schema.sql
'

if [ -n "$saved_imap_state" ]; then
  echo "Restoring IMAP downloader checkpoints..."
  while IFS="$(printf '\t')" read -r state_key state_value; do
    if [ -z "$state_key" ]; then
      continue
    fi
    "${compose_cmd[@]}" exec -T -e STATE_KEY="$state_key" -e STATE_VALUE="$state_value" postgres sh -c '
      set -eu
      export PGPASSWORD="${POSTGRES_PASSWORD:-}"
      psql -v ON_ERROR_STOP=1 -v state_key="$STATE_KEY" -v state_value="$STATE_VALUE" -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<'"'"'SQL'"'"'
INSERT INTO runtime_state(key, value, updated_at)
VALUES (:'"'"'state_key'"'"', :'"'"'state_value'"'"'::jsonb, now())
ON CONFLICT (key)
DO UPDATE SET value = EXCLUDED.value, updated_at = now();
SQL
    '
  done <<< "$saved_imap_state"
fi

if [ "$discard_calendar_sync" -eq 1 ]; then
  echo "Clearing local calendar sync files..."
  rm -rf data/private/calendar/vdir data/private/calendar/status
  mkdir -p data/private/calendar/vdir/default data/private/calendar/status
fi

echo "Verifying user data tables are empty..."
non_empty_counts="$("${compose_cmd[@]}" exec -T postgres sh -c '
  set -eu
  export PGPASSWORD="${POSTGRES_PASSWORD:-}"
  psql -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<'"'"'SQL'"'"'
SELECT table_name || '"'"'='"'"' || row_count
FROM (
  SELECT '"'"'agent_checkpoints'"'"' AS table_name, count(*) AS row_count FROM agent_checkpoints
  UNION ALL SELECT '"'"'agent_memories'"'"', count(*) FROM agent_memories
  UNION ALL SELECT '"'"'calendar_event_audit'"'"', count(*) FROM calendar_event_audit
  UNION ALL SELECT '"'"'calendar_managed_events'"'"', count(*) FROM calendar_managed_events
  UNION ALL SELECT '"'"'deep_research_events'"'"', count(*) FROM deep_research_events
  UNION ALL SELECT '"'"'deep_research_runs'"'"', count(*) FROM deep_research_runs
  UNION ALL SELECT '"'"'emails'"'"', count(*) FROM emails
  UNION ALL SELECT '"'"'jobs'"'"', count(*) FROM jobs
  UNION ALL SELECT '"'"'manual_events'"'"', count(*) FROM manual_events
  UNION ALL SELECT '"'"'memory_events'"'"', count(*) FROM memory_events
  UNION ALL SELECT '"'"'outbound_email_logs'"'"', count(*) FROM outbound_email_logs
  UNION ALL SELECT '"'"'project_tasks'"'"', count(*) FROM project_tasks
  UNION ALL SELECT '"'"'projects'"'"', count(*) FROM projects
  UNION ALL SELECT '"'"'reminders'"'"', count(*) FROM reminders
  UNION ALL SELECT '"'"'supervisor_instructions'"'"', count(*) FROM supervisor_instructions
  UNION ALL SELECT '"'"'task_logs'"'"', count(*) FROM task_logs
  UNION ALL SELECT '"'"'thread_summaries'"'"', count(*) FROM thread_summaries
) counts
WHERE row_count > 0
ORDER BY table_name;
SQL
')"
if [ -n "$non_empty_counts" ]; then
  echo "Reset verification failed; these tables still contain rows:" >&2
  printf '%s\n' "$non_empty_counts" >&2
  exit 1
fi

if [ "$restart_services" -eq 1 ] && [ "${#running_writers[@]}" -gt 0 ]; then
  echo "Restarting previously running database writer services: ${running_writers[*]}"
  "${compose_cmd[@]}" up -d "${running_writers[@]}"
elif [ "$postgres_was_running" -eq 0 ] && [ "${#running_writers[@]}" -eq 0 ]; then
  echo "Stopping postgres because it was not running before this reset."
  "${compose_cmd[@]}" stop postgres
fi

if [ "$discard_calendar_sync" -eq 1 ]; then
  echo "Database reset complete. Shared files were left untouched. Local calendar sync files were cleared."
else
  echo "Database reset complete. Shared files and local calendar sync files were left untouched."
fi
