#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
PYTHON=${PYTHON:-python3}
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    printf '%s\n' 'Python 3.12 or newer is required. Set PYTHON to its executable.' >&2
    exit 1
fi
VENV="$ROOT/data/.venv-setup"
if [ ! -x "$VENV/bin/python" ]; then
    printf '%s\n' 'Create an isolated setup environment and install the setup dependencies? [y/N]'
    read -r choice
    case "$choice" in y|Y|yes|YES) ;; *) exit 0 ;; esac
    "$PYTHON" -m venv "$VENV"
fi
if ! "$VENV/bin/python" -c 'import yaml, dotenv, questionary, rich, httpx' 2>/dev/null; then
    printf '%s\n' 'Install/update the isolated setup dependencies? [y/N]'
    read -r choice
    case "$choice" in y|Y|yes|YES) ;; *) exit 0 ;; esac
    "$VENV/bin/python" -m pip install -r "$ROOT/src/agent/requirements-setup.txt"
fi
PYTHONPATH="$ROOT/src/agent/src${PYTHONPATH:+:$PYTHONPATH}" exec "$VENV/bin/python" -m assistant_agent.setup --root "$ROOT" "$@"