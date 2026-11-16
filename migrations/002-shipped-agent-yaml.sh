#!/bin/sh
# One-off migration: older installations copied agent.yaml into data/config/ at install time, so later builds
# never saw new or changed defaults. Settings now come only from the shipped agent.yaml plus data/overrides.env.
# This keeps deliberate edits from the old copy as overrides, backs the copy up, and removes it.
# install.sh runs every migrations/*.sh it finds; delete this file (and its .py helper) once old installations are gone.
#
# Usage: 002-shipped-agent-yaml.sh DATA_DIR
set -eu

DATA=${1:?usage: 002-shipped-agent-yaml.sh DATA_DIR}
[ -f "$DATA/config/agent.yaml" ] || exit 0

DATA=$(cd "$DATA" && pwd)
ROOT=$(dirname -- "$DATA")
HERE=$(cd "$(dirname -- "$0")" && pwd)
IMAGE=${ASSISTANT_MANAGEMENT_IMAGE:-assistant-management:local}

# The helper needs this build's catalog and PyYAML, which the management image provides.
if [ "$IMAGE" = assistant-management:local ]; then
    printf '%s\n' 'Building the management image to migrate data/config/agent.yaml...'
    docker build -q -t "$IMAGE" -f "$ROOT/src/management/Dockerfile" "$ROOT" >/dev/null
elif ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker pull "$IMAGE" >/dev/null
fi

docker run --rm --log-driver none --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --user "$(id -u):$(id -g)" \
    -e ASSISTANT_DEFAULTS_DIR=/app/defaults \
    --mount "type=bind,source=$DATA,target=/data" \
    --mount "type=bind,source=$HERE/002-shipped-agent-yaml.py,target=/migration.py,readonly" \
    "$IMAGE" python /migration.py /data
