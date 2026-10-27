#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/../.."

SERVICE="${OLLAMA_SERVICE:-ollama}"

docker compose --env-file data/.env pull "$SERVICE"
docker compose --env-file data/.env up -d --no-deps "$SERVICE"

attempts=0
until docker compose --env-file data/.env exec -T "$SERVICE" ollama list >/dev/null 2>&1; do
  attempts=$((attempts + 1))
  if [ "$attempts" -ge 60 ]; then
    echo "Ollama did not become ready within 120 seconds" >&2
    exit 1
  fi
  sleep 2
done

if [ -n "${MEMORY_EMBEDDING_MODEL:-}" ]; then
  printf '%s\n' 'MEMORY_EMBEDDING_MODEL is deprecated; configure AGENT_EMBEDDINGS_MODEL in setup instead.' >&2
fi
docker compose --env-file data/.env run --rm --no-deps -e "OLLAMA_HOST=http://$SERVICE:11434" ollama-model-puller
