#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

SERVICE="${OLLAMA_SERVICE:-ollama}"

docker compose pull "$SERVICE"
docker compose up -d --no-deps "$SERVICE"

attempts=0
until docker compose exec -T "$SERVICE" ollama list >/dev/null 2>&1; do
  attempts=$((attempts + 1))
  if [ "$attempts" -ge 60 ]; then
    echo "Ollama did not become ready within 120 seconds" >&2
    exit 1
  fi
  sleep 2
done

docker compose exec -T "$SERVICE" sh -c 'ollama pull "${MEMORY_EMBEDDING_MODEL:-embeddinggemma}"'
