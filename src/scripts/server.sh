#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
case "$ROOT" in *','*|*'
'*) printf '%s\n' 'Installation paths cannot contain commas or newlines.' >&2; exit 1 ;; esac
command -v docker >/dev/null 2>&1 || {
    printf '%s\n' 'Install and start Docker with Compose, then run this command again.' >&2
    exit 1
}
docker info >/dev/null 2>&1 || {
    printf '%s\n' 'Docker is not accessible. Start it and grant this account Docker access; no sudo changes were made.' >&2
    exit 1
}
docker compose version >/dev/null
SOCKET=${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}
case "$SOCKET" in
    unix://*) SOCKET=${SOCKET#unix://} ;;
    *) printf '%s\n' 'Use a local Docker context; remote daemon bind mounts would refer to the wrong host.' >&2; exit 1 ;;
esac
test -S "$SOCKET" || { printf '%s\n' 'The selected Docker socket is unavailable.' >&2; exit 1; }
test ! -L "$ROOT/data" || { printf '%s\n' 'Runtime data cannot be a symlink.' >&2; exit 1; }
umask 077
mkdir -p "$ROOT/data"
export ASSISTANT_INSTALL_ROOT="$ROOT"
export ASSISTANT_DOCKER_SOCKET="$SOCKET"
export ASSISTANT_UID="$(id -u)" ASSISTANT_GID="$(id -g)"
IMAGE=${ASSISTANT_MANAGEMENT_IMAGE:-assistant-management:local}

if [ "${1:-}" = connect ]; then
    test "$#" -eq 1 || { printf '%s\n' 'Usage: assistant connect' >&2; exit 1; }
    exec docker compose --project-directory "$ROOT/src" --env-file "$ROOT/data/.env" \
        -f "$ROOT/src/docker-compose.yml" exec -T management \
        python -m assistant_agent.management_server connection
fi

if [ "$IMAGE" = assistant-management:local ]; then
    if [ "${1:-}" = server ] && [ "${2:-start}" = start ]; then
        printf '%s\n' 'Building the management image (the first build may take several minutes)...'
        docker build --progress=plain -t "$IMAGE" -f "$ROOT/src/management/Dockerfile" "$ROOT"
    else
        docker build -q -t "$IMAGE" -f "$ROOT/src/management/Dockerfile" "$ROOT" >/dev/null
    fi
elif ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker pull "$IMAGE"
fi
export ASSISTANT_MANAGEMENT_IMAGE="$IMAGE"
SOCKET_GROUP=$(stat -c '%g' "$SOCKET" 2>/dev/null || stat -f '%g' "$SOCKET")
NETWORK=bridge
if [ "${1:-}" != server ] && ! { [ "${1:-}" = management ] && [ "${2:-}" = serve ]; } \
        && ! { [ "${1:-}" = secrets ] && [ "${2:-}" = sync ]; }; then
    API_ID=$(docker compose --project-directory "$ROOT/src" --env-file "$ROOT/data/.env" \
        -f "$ROOT/src/docker-compose.yml" ps -q management)
    test -n "$API_ID" || { printf '%s\n' 'Start the server with assistant server start first.' >&2; exit 1; }
    NETWORK="container:$API_ID"
fi

admin() {
    if [ -t 0 ] && [ -t 1 ]; then
        docker run --rm -it --network "$NETWORK" --user "$ASSISTANT_UID:$ASSISTANT_GID" --group-add 0 --group-add "$SOCKET_GROUP" \
            --mount "type=bind,source=$ROOT,target=$ROOT" \
            --mount "type=bind,source=$SOCKET,target=/var/run/docker.sock" \
            -e ASSISTANT_CONTAINER_ADMIN=1 -e ASSISTANT_INSTALL_ROOT -e ASSISTANT_DOCKER_SOCKET \
            -e ASSISTANT_MANAGEMENT_IMAGE -e ASSISTANT_UID -e ASSISTANT_GID -e HOME=/tmp -e TERM \
            "$IMAGE" python -m "$@"
    else
        docker run --rm -i --network "$NETWORK" --user "$ASSISTANT_UID:$ASSISTANT_GID" --group-add 0 --group-add "$SOCKET_GROUP" \
            --mount "type=bind,source=$ROOT,target=$ROOT" \
            --mount "type=bind,source=$SOCKET,target=/var/run/docker.sock" \
            -e ASSISTANT_CONTAINER_ADMIN=1 -e ASSISTANT_INSTALL_ROOT -e ASSISTANT_DOCKER_SOCKET \
            -e ASSISTANT_MANAGEMENT_IMAGE -e ASSISTANT_UID -e ASSISTANT_GID -e HOME=/tmp \
            "$IMAGE" python -m "$@"
    fi
}

api_client() {
    set -- python -m assistant_agent.setup --root "$ROOT" "$@"
    if [ -t 0 ] && [ -t 1 ]; then
        set -- -t "$IMAGE" "$@"
    else
        set -- "$IMAGE" "$@"
    fi
    docker run --rm -i --network "$NETWORK" --user "$ASSISTANT_UID:$ASSISTANT_GID" \
        --read-only --cap-drop ALL --security-opt no-new-privileges:true \
        --tmpfs /tmp:rw,nosuid,nodev,size=16m \
        --mount "type=bind,source=$ROOT/data/.management-token,target=/run/assistant/management-token,readonly" \
        -e ASSISTANT_CONTAINER_ADMIN=1 \
        -e ASSISTANT_MANAGEMENT_URL=http://127.0.0.1:8765 \
        -e ASSISTANT_MANAGEMENT_TOKEN_FILE=/run/assistant/management-token \
        -e HOME=/tmp -e TERM "$@"
}

if [ "${1:-}" = server ]; then
    shift
    if [ "$#" -eq 0 ]; then set -- start; fi
    admin assistant_agent.server_control --root "$ROOT" "$@"
elif [ "${1:-}" = management ] && [ "${2:-}" = serve ]; then
    test "$#" -eq 2 || { printf '%s\n' 'Set ASSISTANT_MANAGEMENT_PORT in data/.env; management serve takes no flags in container mode.' >&2; exit 1; }
    admin assistant_agent.server_control --root "$ROOT" start
elif [ "${1:-}" = secrets ] && [ "${2:-}" = sync ]; then
    admin assistant_agent.setup --root "$ROOT" "$@"
else
    api_client "$@"
fi
