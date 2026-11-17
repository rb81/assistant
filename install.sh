#!/bin/sh
set -eu

BLUE= GREEN= RED= RESET=
if [ -t 1 ] && [ "${TERM:-dumb}" != dumb ] && [ "${NO_COLOR+x}" != x ]; then
    BLUE=$(printf '\033[1;34m')
    GREEN=$(printf '\033[1;32m')
    RED=$(printf '\033[1;31m')
    RESET=$(printf '\033[0m')
fi
fail() { printf '\n%sError:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }
stage() { printf '\n%s==> [%s/4]%s %s\n' "$BLUE" "$1" "$RESET" "$2"; }
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
printf '\n%sAssistant%s\n' "$BLUE" "$RESET"
stage 1 'Checking requirements'
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
LOG_PID=
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
    if [ -n "$LOG_PID" ]; then
        kill "$LOG_PID" 2>/dev/null || :
        wait "$LOG_PID" 2>/dev/null || :
    fi
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
    stage 2 'Existing installation'
    print_wrapped 'Your source and data are preserved. This does not upgrade Assistant.'
else
    umask 077
    WORK=$(mktemp -d "${TMPDIR:-/tmp}/assistant-install.XXXXXXXX")
    ARCHIVE="$WORK/source.tar.gz"
    stage 2 "Downloading Assistant $REF"
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

stage 3 'Preparing services'
print_wrapped 'The first startup may take several minutes. Your existing data will be preserved.'
test ! -L "$DESTINATION/data" || fail 'The data directory must not be a symlink.'
test ! -L "$DESTINATION/data/logs" || fail 'The log directory must not be a symlink.'
umask 077
mkdir -p "$DESTINATION/data/logs"
LOG=$(mktemp "$DESTINATION/data/logs/install.XXXXXXXX")
print_wrapped "Detailed startup log: $LOG"
sh "$DESTINATION/run.sh" server start </dev/null >"$LOG" 2>&1 &
START_PID=$!
if [ "${ASSISTANT_VERBOSE:-0}" = 1 ]; then
    tail -f "$LOG" &
    LOG_PID=$!
fi
ELAPSED=0
while kill -0 "$START_PID" 2>/dev/null; do
    sleep 1
    ELAPSED=$((ELAPSED + 1))
    if [ $((ELAPSED % 15)) -eq 0 ]; then
        print_wrapped "Still preparing services (${ELAPSED}s). Progress and diagnostics are being saved."
    fi
done
if wait "$START_PID"; then
    START_STATUS=0
else
    START_STATUS=$?
fi
START_PID=
if [ -n "$LOG_PID" ]; then
    kill "$LOG_PID" 2>/dev/null || :
    wait "$LOG_PID" 2>/dev/null || :
    LOG_PID=
fi
if [ "$START_STATUS" -ne 0 ]; then
    print_wrapped 'Startup stopped. The latest diagnostics are:'
    tail -n 12 "$LOG" >&2
    fail "Assistant installation is incomplete. Full details: $LOG. Fix the reported problem, then rerun the installer."
fi
mkdir -p "$HOME/.local/bin"
if [ ! -L "$LINK" ]; then
    ln -s "$DESTINATION/run.sh" "$LINK"
fi
stage 4 'Installation complete'
if grep -F 'Assistant and its management API are running.' "$LOG" >/dev/null; then
    printf '%sReady.%s\n' "$GREEN" "$RESET"
    print_wrapped 'Assistant and its management API are running.'
    print_wrapped 'Open Assistant for macOS and connect to this installation to start chatting.'
else
    printf '%sInstalled.%s\n' "$GREEN" "$RESET"
    print_wrapped 'The management API is ready; assistant setup is still required.'
    print_wrapped 'Next: open guided setup, review your settings, then choose Save & start.'
    printf '\n  "%s"\n' "$LINK"
fi
print_wrapped 'Startup diagnostics may contain private information. Review them before sharing.'
