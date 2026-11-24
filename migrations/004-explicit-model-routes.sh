#!/bin/sh
set -eu

DATA=${1:?usage: 004-explicit-model-routes.sh DATA_DIR}
[ -d "$DATA" ] || exit 0
[ ! -f "$DATA/.migrations/004-explicit-model-routes" ] || exit 0
DATA=$(cd "$DATA" && pwd)
ROOT=$(dirname -- "$DATA")
HERE=$(cd "$(dirname -- "$0")" && pwd)
IMAGE=${ASSISTANT_MANAGEMENT_IMAGE:-assistant-management:local}

if [ "$IMAGE" = assistant-management:local ]; then
    docker build -q -t "$IMAGE" -f "$ROOT/src/management/Dockerfile" "$ROOT" >/dev/null
elif ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker pull "$IMAGE" >/dev/null
fi

docker run --rm --log-driver none --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --user "$(id -u):$(id -g)" \
    -e ASSISTANT_DEFAULTS_DIR=/app/defaults \
    --mount "type=bind,source=$DATA,target=/data" \
    --mount "type=bind,source=$HERE/004-explicit-model-routes.py,target=/migration.py,readonly" \
    "$IMAGE" python /migration.py /data
