#!/bin/sh
set -eu

fail() { printf '%s\n' "$*" >&2; exit 1; }
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
mkdir "$LOCK" 2>/dev/null || fail "An installation is in progress, or a stale lock exists: $LOCK"
WORK=
cleanup() {
    if [ -n "$WORK" ]; then
        rm -f "$WORK/source.tar.gz" "$WORK/paths" "$WORK/types"
        rmdir "$WORK" 2>/dev/null || printf 'Temporary installation files remain at %s\n' "$WORK" >&2
    fi
    rmdir "$LOCK"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

if [ -e "$DESTINATION" ]; then
    test -f "$DESTINATION/src/scripts/server.sh" || fail 'Destination already exists and is not a packaged Assistant installation. Use a new directory; existing data was not changed.'
    printf '%s\n' 'Existing installation detected. Preserving its source and data; this is not an implicit update.'
else
    umask 077
    WORK=$(mktemp -d "${TMPDIR:-/tmp}/assistant-install.XXXXXXXX")
    ARCHIVE="$WORK/source.tar.gz"
    printf 'Downloading Assistant %s...\n' "$REF"
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
    sh "$migration" "$DESTINATION/data" || fail "Migration failed: $migration"
done

sh "$DESTINATION/run.sh" server start </dev/null
mkdir -p "$HOME/.local/bin"
if [ ! -L "$LINK" ]; then
    ln -s "$DESTINATION/run.sh" "$LINK"
fi
printf '\n%s\n' 'Assistant is installed and its management API starts with Docker.'
printf '%s\n' 'Complete setup in the containerized TUI:'
printf '  %s\n' "\"$LINK\""
printf '%s\n' 'Or connect with Assistant for macOS: https://github.com/rb81/assistant-client'
printf '%s\n' 'SSH administrators can use assistant connect through the installed launcher; no separate pairing is required.'
