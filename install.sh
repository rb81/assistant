#!/bin/sh
set -eu

fail() { printf '%s\n' "$*" >&2; exit 1; }
print_wrapped() {
    width=${COLUMNS:-80}
    if [ -t 1 ] && command -v tput >/dev/null 2>&1; then
        terminal_width=$(tput cols 2>/dev/null || :)
        case "$terminal_width" in ''|*[!0-9]*) ;; *) width=$terminal_width ;; esac
    fi
    case "$width" in ''|*[!0-9]*) width=80 ;; esac
    [ "$width" -ge 20 ] || width=20
    printf '%s\n' "$1" | fold -s -w "$width"
}
case "$(uname -s)" in Darwin|Linux) ;; *) fail 'Assistant currently supports macOS and Linux.' ;; esac
for tool in curl tar docker; do
    command -v "$tool" >/dev/null 2>&1 || fail "Install $tool first, then rerun the installer."
done
docker info >/dev/null 2>&1 || fail 'Start Docker and grant this account Docker access first. The installer will not change host permissions.'
docker compose version >/dev/null || fail 'Docker Compose is required.'

DESTINATION=${ASSISTANT_INSTALL_DIR:-"$HOME/.local/share/assistant"}
REF=${ASSISTANT_VERSION:-main}
case "$REF" in ''|-*|*[!A-Za-z0-9._/-]*) fail 'Invalid Assistant version/ref.' ;; esac
case "$DESTINATION" in /*) ;; *) fail 'ASSISTANT_INSTALL_DIR must be an absolute path.' ;; esac
test ! -L "$DESTINATION" || fail 'The installation directory must not be a symlink.'
LINK="$HOME/.local/bin/assistant"
if [ -e "$LINK" ] || [ -L "$LINK" ]; then
    test -L "$LINK" && [ "$(readlink "$LINK")" = "$DESTINATION/run.sh" ] \
        || fail "$LINK already belongs to another installation."
fi
mkdir -p "$(dirname -- "$DESTINATION")"
LOCK="$DESTINATION.install-lock"
(umask 077; mkdir "$LOCK") 2>/dev/null || fail "An installation is in progress, or a stale lock exists: $LOCK"
WORK=
START_PID=
cleanup() {
    if [ -n "$WORK" ]; then
        rm -f "$WORK/source.tar.gz" "$WORK/paths" "$WORK/types"
        rmdir "$WORK" 2>/dev/null || printf 'Temporary installation files remain at %s\n' "$WORK" >&2
    fi
    rmdir "$LOCK"
}
terminate_tree() {
    for child in $(ps -ax -o pid= -o ppid= | awk -v parent="$1" '$2 == parent { print $1 }'); do
        terminate_tree "$child"
    done
    kill "$1" 2>/dev/null || :
}
stop_start() {
    if [ -n "$START_PID" ]; then
        terminate_tree "$START_PID"
        wait "$START_PID" 2>/dev/null || :
    fi
    exit 1
}
trap cleanup EXIT
trap stop_start HUP INT TERM

if [ -e "$DESTINATION" ]; then
    test -f "$DESTINATION/src/scripts/server.sh" || fail 'Destination already exists and is not a packaged Assistant installation. Use a new directory; existing data was not changed.'
    print_wrapped 'Existing installation detected. Its source and data will be preserved; this is not an implicit update.'
else
    umask 077
    WORK=$(mktemp -d "${TMPDIR:-/tmp}/assistant-install.XXXXXXXX")
    ARCHIVE="$WORK/source.tar.gz"
    print_wrapped "Downloading Assistant $REF..."
    curl --proto '=https' --tlsv1.2 -fLSs --retry 3 --max-filesize 52428800 \
        "https://github.com/rb81/assistant/archive/$REF.tar.gz" -o "$ARCHIVE"
    tar -tzf "$ARCHIVE" > "$WORK/paths"
    if grep -E '(^/|(^|/)\.\.(/|$))' "$WORK/paths" >/dev/null; then
        fail 'Archive contains unsafe paths.'
    fi
    tar -tvzf "$ARCHIVE" > "$WORK/types"
    if grep -E '^[lh]' "$WORK/types" >/dev/null; then
        fail 'Archive links are not supported.'
    fi
    mkdir "$WORK/installation"
    tar -xzf "$ARCHIVE" --strip-components=1 -C "$WORK/installation"
    test -f "$WORK/installation/src/scripts/server.sh" || fail 'Selected version does not contain the packaged server. No existing installation was changed.'
    mkdir -p "$(dirname -- "$DESTINATION")"
    mv "$WORK/installation" "$DESTINATION"
    rm -f "$WORK/paths" "$WORK/types"
fi

for migration in "$DESTINATION"/migrations/*.sh; do
    [ -f "$migration" ] || continue
    print_wrapped "Checking deployment migration: $(basename "$migration")"
    sh "$migration" "$DESTINATION/data" || fail "Migration failed: $migration"
done

print_wrapped 'Starting Assistant services; the first startup may take several minutes.'
print_wrapped 'Build progress, timed startup stages, and pending health checks appear below.'
sh "$DESTINATION/run.sh" server start </dev/null &
START_PID=$!
if wait "$START_PID"; then
    START_STATUS=0
else
    START_STATUS=$?
fi
START_PID=
[ "$START_STATUS" -eq 0 ] || fail 'Assistant could not be started. Review the Docker diagnostics above; installation is incomplete.'
mkdir -p "$HOME/.local/bin"
if [ ! -L "$LINK" ]; then
    ln -s "$DESTINATION/run.sh" "$LINK"
fi
printf '\n'
print_wrapped 'Assistant is installed and its management API is ready.'
print_wrapped 'Complete setup in the containerized TUI:'
printf '  %s\n' "\"$LINK\""
print_wrapped 'Or connect with Assistant for macOS: https://github.com/rb81/assistant-client'
print_wrapped 'SSH administrators can use assistant connect through the installed launcher.'
