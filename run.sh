#!/bin/sh
set -eu

SOURCE=$0
while [ -L "$SOURCE" ]; do
    BASE=$(CDPATH= cd -- "$(dirname -- "$SOURCE")" && pwd)
    SOURCE=$(readlink "$SOURCE")
    case "$SOURCE" in /*) ;; *) SOURCE=$BASE/$SOURCE ;; esac
done
ROOT=$(CDPATH= cd -- "$(dirname -- "$SOURCE")" && pwd)
if [ "${ASSISTANT_HOST_ADMIN:-0}" = 1 ]; then
    exec "$ROOT/src/scripts/run.sh" "$@"
fi
exec sh "$ROOT/src/scripts/server.sh" "$@"
