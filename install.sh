#!/bin/sh
set -eu

BLUE= GREEN= RED= RESET=
if [ -t 1 ] && [ "${TERM:-dumb}" != dumb ] && [ "${NO_COLOR+x}" != x ]; then
    BLUE=$(printf '\033[1;34m'); GREEN=$(printf '\033[1;32m')
    RED=$(printf '\033[1;31m'); RESET=$(printf '\033[0m')
fi
fail() { printf '\n%sError:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }
stage() { CURRENT=$2; printf '\n%s==> [%s/4]%s %s\n' "$BLUE" "$1" "$RESET" "$2"; }
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
setting() { sed -n "s/^$1=//p" "$2"; }
regular() { test ! -L "$1" && { test ! -e "$1" || test -f "$1"; }; }
revision_ok() {
    test "${#1}" -eq 40 && ! printf '%s' "$1" | grep '[^a-f0-9]' >/dev/null
}
resolve_link() {
    resolved=$1; depth=0
    while [ -L "$resolved" ]; do
        depth=$((depth + 1)); [ "$depth" -lt 20 ] || fail 'Launcher symlink loop.'
        base=$(CDPATH= cd -- "$(dirname -- "$resolved")" && pwd -P)
        resolved=$(readlink "$resolved")
        case "$resolved" in /*) ;; *) resolved=$base/$resolved ;; esac
    done
    base=$(CDPATH= cd -- "$(dirname -- "$resolved")" && pwd -P)
    resolved=$base/$(basename -- "$resolved")
}
terminate_tree() {
    for child in $(ps -ax -o pid= -o ppid= | awk -v parent="$1" '$2 == parent { print $1 }'); do
        terminate_tree "$child"
    done
    kill "$1" 2>/dev/null || :
}
stop_start() {
    if [ -n "$START_PID" ]; then terminate_tree "$START_PID"; wait "$START_PID" 2>/dev/null || :; fi
    exit 1
}
run_logged() {
    CURRENT=$1; shift
    "$@" </dev/null >>"$LOG" 2>&1 & START_PID=$!
    ELAPSED=0
    while kill -0 "$START_PID" 2>/dev/null; do
        sleep 1; ELAPSED=$((ELAPSED + 1))
        if [ $((ELAPSED % 15)) -eq 0 ]; then print_wrapped "Still working: $CURRENT (${ELAPSED}s). Details are being saved."; fi
    done
    if wait "$START_PID"; then command_status=0; else command_status=$?; fi
    START_PID=
    return "$command_status"
}
finish() {
    status=$1
    trap - EXIT
    if [ -n "$LOG_PID" ]; then kill "$LOG_PID" 2>/dev/null || :; wait "$LOG_PID" 2>/dev/null || :; fi
    if [ "$status" -ne 0 ]; then
        printf '\nInstallation is incomplete during: %s.\nDetails: %s\n' "$CURRENT" "$LOG" >&2
        if [ -f "$PENDING" ]; then
            printf 'Update recovery information: %s\nPrevious source: %s\n' "$PENDING" "$BACKUP" >&2
            printf 'Data was not rolled back. Do not downgrade across database migrations.\n' >&2
        fi
    fi
    if [ -n "$WORK" ] && [ ! -f "$PENDING" ]; then
        # WORK is exclusively this invocation's mktemp directory, never the installation.
        case "$WORK" in "$DESTINATION".update.*) rm -rf -- "$WORK" ;; esac
    fi
    rmdir "$LOCK"
    exit "$status"
}
source_manifest() {
    while IFS= read -r entry; do
        case "$entry" in ''|.|..|*/*|*[!A-Za-z0-9._-]*|data|.git) fail 'Invalid source manifest entry.' ;; esac
    done < "$2"
    (
        cd -- "$1"
        while IFS= read -r entry; do
            find "./$entry" -name __pycache__ -prune -o -type f -exec sh -c '
                for file do
                    cksum "$file"
                    if [ -x "$file" ]; then printf "executable %s\n" "$file"; fi
                done
            ' sh {} +
            find "./$entry" -type l -print
        done < "$2"
    ) | LC_ALL=C sort
}
check_checkout() {
    if [ "$GIT_CHECKOUT" = 1 ]; then
        test -z "$(git -C "$DESTINATION" status --porcelain --untracked-files=normal)" \
            || fail 'The source checkout has local changes. Commit or preserve them before updating; nothing was reset.'
    elif [ -f "$DESTINATION/data/.installation-files" ]; then
        source_manifest "$DESTINATION" "$DESTINATION/data/.installation-entries" > "$WORK/current-files"
        cmp -s "$WORK/current-files" "$DESTINATION/data/.installation-files" \
            || fail 'Installed source was modified. Preserve those changes before updating; nothing was replaced.'
    elif [ "$EXISTING" = 1 ] && [ "${ASSISTANT_ADOPT:-0}" != 1 ]; then
        fail 'This installation has no source manifest. Back up/review its source, then use ASSISTANT_ADOPT=1 once; data is preserved. ASSISTANT_UPDATE=0 repairs without updating source.'
    fi
}
source_git() {
    if [ "$GIT_CHECKOUT" = 1 ]; then
        git -C "$fetch_repository" "$@"
    else
        git --git-dir="$fetch_repository" "$@"
    fi
}
download_source() {
    mkdir "$WORK/release"
    if [ "$GIT_CHECKOUT" = 1 ]; then
        fetch_repository=$DESTINATION
    elif [ "${SOURCE#github:}" != "$SOURCE" ]; then
        repository=${SOURCE#github:}
        case "$repository" in */*) ;; *) fail 'Use github:OWNER/REPOSITORY as the source.' ;; esac
        case "$repository" in *[!A-Za-z0-9._/-]*|*..*|/*|*/|*/*/*) fail 'Invalid GitHub source.' ;; esac
        if revision_ok "$REF"; then
            REVISION=$REF
        else
            run_logged 'resolving the update revision' curl --proto '=https' --tlsv1.2 -fLSs --retry 3 --get \
                --data-urlencode "sha=$REF" --data 'per_page=1' \
                "https://api.github.com/repos/$repository/commits" -o "$WORK/commit.json" \
                || fail 'Could not resolve the configured source revision. The running installation was not changed.'
            REVISION=$(sed -n 's/^ *"sha": *"\([a-f0-9]\{40\}\)".*/\1/p' "$WORK/commit.json" | head -n 1)
        fi
        revision_ok "$REVISION" || fail 'The source did not return an exact commit revision.'
        run_logged 'downloading source' curl --proto '=https' --tlsv1.2 -fLSs --retry 3 --max-filesize 52428800 \
            "https://github.com/$repository/archive/$REVISION.tar.gz" -o "$WORK/source.tar.gz" \
            || fail 'Source download failed. The running installation was not changed.'
        tar -tzf "$WORK/source.tar.gz" > "$WORK/paths"
        tar -tvzf "$WORK/source.tar.gz" > "$WORK/types"
        archive_strip=1
        fetch_repository=
    else
        command -v git >/dev/null 2>&1 || fail 'Git is required for the configured non-GitHub source.'
        git init -q --bare "$WORK/repository"
        fetch_repository=$WORK/repository
    fi
    if [ -n "$fetch_repository" ]; then
        git_source=$SOURCE
        case "$SOURCE" in github:*) git_source=https://github.com/${SOURCE#github:}.git ;; esac
        run_logged 'fetching source' source_git fetch --no-tags "$git_source" "$REF" \
            || fail 'Could not fetch the configured source. The running installation was not changed.'
        REVISION=$(source_git rev-parse --verify 'FETCH_HEAD^{commit}')
        if [ "$GIT_CHECKOUT" = 1 ]; then
            git -C "$DESTINATION" merge-base --is-ancestor HEAD "$REVISION" \
                || fail 'The deployment source diverges from this checkout. Review it explicitly; the installer never force-resets Git history.'
        fi
        source_git archive --format=tar.gz --output="$WORK/source.tar.gz" "$REVISION"
        tar -tzf "$WORK/source.tar.gz" > "$WORK/paths"
        tar -tvzf "$WORK/source.tar.gz" > "$WORK/types"
        archive_strip=0
    fi
    if grep -E '(^/|(^|/)\.\.(/|$)|[^A-Za-z0-9._/-])' "$WORK/paths" >/dev/null \
            || grep -Ev '^[-d]' "$WORK/types" >/dev/null; then
        fail 'Source archive contains unsupported paths or links.'
    fi
    (umask 022; tar -xzf "$WORK/source.tar.gz" --strip-components="$archive_strip" -C "$WORK/release")
    test -f "$WORK/release/src/scripts/server.sh" && test -f "$WORK/release/run.sh" \
        && test -f "$WORK/release/src/docker-compose.yml" || fail 'Source is not a packaged Assistant release.'
    : > "$WORK/entries"
    for entry in "$WORK/release"/* "$WORK/release"/.[!.]* "$WORK/release"/..?*; do
        [ -e "$entry" ] || continue
        name=$(basename -- "$entry")
        case "$name" in data|.git|.env|.secrets|backups|.installation*) fail 'Source archive contains reserved installation paths.' ;; esac
        printf '%s\n' "$name" >> "$WORK/entries"
    done
    source_manifest "$WORK/release" "$WORK/entries" > "$WORK/files"
}
build_update() {
    CURRENT='building update images'
    print_wrapped 'Building update images before stopping the current installation.'
    case "${ASSISTANT_MANAGEMENT_IMAGE:-assistant-management:local}" in
        assistant-management:local|assistant-management:update-*) ;;
        *) fail 'Automatic updates require the locally built management image. Use ASSISTANT_UPDATE=0 to reconcile an externally managed image.' ;;
    esac
    installation_key=$(printf '%s' "$DESTINATION" | cksum | cut -d ' ' -f 1)
    image=assistant-management:update-$installation_key-$REVISION
    export ASSISTANT_MANAGEMENT_IMAGE="$image"
    run_logged 'building the management image' docker build --progress=plain --build-arg "ASSISTANT_REVISION=$REVISION" \
        -t "$image" -f "$WORK/release/src/management/Dockerfile" "$WORK/release" \
        || fail 'Building the management image failed. The running installation was not stopped.'
    if [ -f "$DESTINATION/data/.env" ] && [ -f "$DESTINATION/data/.secrets/state.json" ] \
            && grep -Eq '"setup_complete"[[:space:]]*:[[:space:]]*true' "$DESTINATION/data/.secrets/state.json"; then
        ln -s "$DESTINATION/data" "$WORK/release/data"
        run_logged 'building configured service images' candidate_compose "$WORK/release" build \
            || fail 'Building update services failed. The running installation was not stopped.'
        rm "$WORK/release/data"
    fi
}
candidate_compose() {
    compose_source=$1; shift
    if [ -f "$DESTINATION/data/.compose-secrets.yaml" ]; then
        docker compose --project-directory "$compose_source/src" --env-file "$DESTINATION/data/.env" \
            -f "$compose_source/src/docker-compose.yml" -f "$DESTINATION/data/.compose-secrets.yaml" "$@"
    else
        docker compose --project-directory "$compose_source/src" --env-file "$DESTINATION/data/.env" \
            -f "$compose_source/src/docker-compose.yml" "$@"
    fi
}
activate_update() {
    check_checkout
    if [ "$GIT_CHECKOUT" = 0 ] && [ "$EXISTING" = 1 ]; then
        if [ -f "$DESTINATION/data/.installation-entries" ]; then
            cp "$DESTINATION/data/.installation-entries" "$WORK/old-entries"
        else
            { printf '%s\n' src docs migrations run.sh install.sh .dockerignore .gitignore README.md LICENSE; cat "$WORK/entries"; } \
                | LC_ALL=C sort -u > "$WORK/old-entries"
        fi
        while IFS= read -r entry; do
            if [ -e "$DESTINATION/$entry" ] || [ -L "$DESTINATION/$entry" ]; then
                grep -Fx -- "$entry" "$WORK/old-entries" >/dev/null \
                    || fail "An unrelated file blocks the update: $entry. No services were stopped."
            fi
        done < "$WORK/entries"
    fi
    BACKUP=
    unchanged=0
    if [ "$GIT_CHECKOUT" = 1 ] && [ "$(git -C "$DESTINATION" rev-parse HEAD)" = "$REVISION" ]; then
        unchanged=1
    elif [ -f "$DESTINATION/data/.installation-files" ] \
            && cmp -s "$WORK/files" "$DESTINATION/data/.installation-files"; then
        unchanged=1
    fi
    if [ "$unchanged" = 1 ] && [ -f "$DESTINATION/data/.installation" ]; then
        BACKUP=$(setting previous "$DESTINATION/data/.installation")
    elif [ "$EXISTING" = 1 ] && [ "$GIT_CHECKOUT" = 0 ]; then
        BACKUP=$(mktemp -d "$DESTINATION.previous.XXXXXXXX")
    fi
    {
        printf 'source=%s\nref=%s\nrevision=%s\nimage=%s\nprevious=%s\nwork=%s\n' \
            "$SOURCE" "$REF" "$REVISION" "$ASSISTANT_MANAGEMENT_IMAGE" "$BACKUP" "$WORK"
    } > "$PENDING"
    cp "$WORK/files" "$DESTINATION/data/.install-target-files"
    cp "$WORK/entries" "$DESTINATION/data/.install-target-entries"
    if [ "$unchanged" = 1 ]; then
        print_wrapped 'Source is already current; reconciling services without a source cutover.'
        return
    fi
    CURRENT='stopping services for source activation'
    if [ -f "$DESTINATION/data/.env" ]; then
        run_logged 'stopping services for source activation' candidate_compose "$DESTINATION" stop \
            || fail 'Could not stop the old installation. Review service state before retrying.'
    fi
    CURRENT='activating updated source'
    if [ "$GIT_CHECKOUT" = 1 ]; then
        git -C "$DESTINATION" rev-parse HEAD > "$DESTINATION/data/.install-previous-revision"
        git -C "$DESTINATION" merge --ff-only --no-overwrite-ignore "$REVISION" >>"$LOG" 2>&1 \
            || fail 'Could not activate the fetched revision. Source was not reset.'
    else
        if [ "$EXISTING" = 1 ]; then
            while IFS= read -r entry; do
                [ ! -e "$DESTINATION/$entry" ] || mv "$DESTINATION/$entry" "$BACKUP/$entry"
            done < "$WORK/old-entries"
        fi
        while IFS= read -r entry; do
            test ! -e "$DESTINATION/$entry" && test ! -L "$DESTINATION/$entry" \
                || fail "An unrelated file blocks source activation: $entry. Previous source was retained."
            mv "$WORK/release/$entry" "$DESTINATION/$entry"
        done < "$WORK/entries"
    fi
}

# Parse the whole installer before execution: an update may replace this script.
install_main() {
    printf '\n%sAssistant%s\n' "$BLUE" "$RESET"
    stage 1 'Checking requirements'
    case "$(uname -s)" in Darwin|Linux) ;; *) fail 'Assistant currently supports macOS and Linux.' ;; esac
    for tool in curl tar docker; do command -v "$tool" >/dev/null 2>&1 || fail "Install $tool first."; done
    docker info >/dev/null 2>&1 || fail 'Start Docker and grant this account Docker access first. Host permissions were not changed.'
    docker compose version >/dev/null || fail 'Docker Compose is required.'
    UPDATE=${ASSISTANT_UPDATE:-1}
    case "$UPDATE" in 0|1) ;; *) fail 'ASSISTANT_UPDATE must be 0 or 1.' ;; esac
    LINK=$HOME/.local/bin/assistant
    if [ -n "${ASSISTANT_INSTALL_DIR:-}" ]; then
        DESTINATION=$ASSISTANT_INSTALL_DIR
    elif [ -L "$LINK" ]; then
        resolve_link "$LINK"; DESTINATION=$(dirname -- "$resolved")
    elif [ -f "$0" ]; then
        resolve_link "$0"
        script_root=$(CDPATH= cd -- "$(dirname -- "$resolved")" && pwd -P)
        if [ -f "$script_root/src/scripts/server.sh" ]; then DESTINATION=$script_root; else DESTINATION=$HOME/.local/share/assistant; fi
    else
        DESTINATION=$HOME/.local/share/assistant
    fi
    case "$DESTINATION" in /*) ;; *) fail 'ASSISTANT_INSTALL_DIR must be an absolute path.' ;; esac
    while [ "$DESTINATION" != / ] && [ "${DESTINATION%/}" != "$DESTINATION" ]; do DESTINATION=${DESTINATION%/}; done
    test ! -L "$DESTINATION" || fail 'The installation directory must not be a symlink.'
    mkdir -p "$(dirname -- "$DESTINATION")"
    parent=$(CDPATH= cd -- "$(dirname -- "$DESTINATION")" && pwd -P)
    DESTINATION=$parent/$(basename -- "$DESTINATION")
    if [ -e "$LINK" ] || [ -L "$LINK" ]; then
        test -L "$LINK" || fail 'The assistant launcher belongs to another installation.'
        resolve_link "$LINK"
        test "$resolved" = "$DESTINATION/run.sh" || fail 'The assistant launcher belongs to another installation.'
    fi
    EXISTING=0
    if [ -e "$DESTINATION" ]; then
        if [ -f "$DESTINATION/src/scripts/server.sh" ]; then
            EXISTING=1
        elif regular "$DESTINATION/data/.install-initializing" \
                && [ -f "$DESTINATION/data/.install-initializing" ] \
                && [ "$(cat "$DESTINATION/data/.install-initializing")" = assistant-initial-install ]; then
            print_wrapped 'Resuming an incomplete first installation.'
        else
            fail 'Destination is not a packaged Assistant installation.'
        fi
    fi
    test "$EXISTING" = 1 || test "$UPDATE" = 1 || fail 'There is no installation to repair.'
    test ! -L "$DESTINATION/data" && test ! -L "$DESTINATION/data/logs" || fail 'Runtime data and logs must not be symlinks.'
    umask 077
    LOCK=$DESTINATION.install-lock
    mkdir "$LOCK" 2>/dev/null || fail "Another installer is running, or a stale lock exists: $LOCK"
    LOG=$DESTINATION/data/logs
    PENDING=$DESTINATION/data/.install-pending
    WORK= BACKUP= START_PID= LOG_PID=
    trap 'finish $?' EXIT
    trap stop_start HUP INT TERM
    mkdir -p "$DESTINATION/data/logs"
    if [ "$EXISTING" = 0 ]; then printf '%s\n' assistant-initial-install > "$DESTINATION/data/.install-initializing"; fi
    LOG=$(mktemp "$DESTINATION/data/logs/install.XXXXXXXX")
    print_wrapped "Installation: $DESTINATION"
    print_wrapped "Detailed startup log: $LOG"
    if [ "${ASSISTANT_VERBOSE:-0}" = 1 ]; then tail -f "$LOG" & LOG_PID=$!; fi
    for metadata in .installation .installation-files .installation-entries .install-pending .install-target-files .install-target-entries .install-previous-revision; do
        regular "$DESTINATION/data/$metadata" || fail 'Installation metadata must be regular files.'
    done
    GIT_CHECKOUT=0
    if [ -e "$DESTINATION/.git" ]; then
        command -v git >/dev/null 2>&1 || fail 'Git is required to update this checkout.'
        GIT_CHECKOUT=1
    fi
    SOURCE=${ASSISTANT_SOURCE:-}
    REF=${ASSISTANT_VERSION:-}
    REVISION=
    if [ -f "$PENDING" ]; then
        SOURCE=$(setting source "$PENDING"); REF=$(setting ref "$PENDING"); REVISION=$(setting revision "$PENDING")
        revision_ok "$REVISION" && test -n "$SOURCE" && test -n "$REF" || fail 'Invalid pending update metadata.'
        WORK=$(setting work "$PENDING")
        case "$WORK" in
            "$DESTINATION".update.*)
                suffix=${WORK#"$DESTINATION".update.}
                case "$suffix" in ''|*[!A-Za-z0-9]*) WORK=; fail 'Invalid update recovery directory.' ;; esac ;;
            *) WORK=; fail 'Invalid update recovery directory.' ;;
        esac
        export ASSISTANT_MANAGEMENT_IMAGE="$(setting image "$PENDING")"
        UPDATE=0
        print_wrapped "Finishing interrupted update: $REVISION"
        if [ "$GIT_CHECKOUT" = 1 ]; then
            test "$(git -C "$DESTINATION" rev-parse HEAD)" = "$REVISION" || fail 'The pending Git revision was not activated. Review recovery information before restarting.'
        fi
        BACKUP=$(setting previous "$PENDING")
        verify_work=$(mktemp "$DESTINATION/data/.verify.XXXXXXXX")
        source_manifest "$DESTINATION" "$DESTINATION/data/.install-target-entries" > "$verify_work"
        cmp -s "$verify_work" "$DESTINATION/data/.install-target-files" \
            || fail 'Source activation was interrupted. Restore the retained previous source or complete activation before restarting; data was preserved.'
        rm "$verify_work"
    elif [ -f "$DESTINATION/data/.installation" ]; then
        [ -n "$SOURCE" ] || SOURCE=$(setting source "$DESTINATION/data/.installation")
        [ -n "$REF" ] || REF=$(setting ref "$DESTINATION/data/.installation")
        REVISION=$(setting revision "$DESTINATION/data/.installation")
        if [ -z "${ASSISTANT_MANAGEMENT_IMAGE:-}" ]; then
            ASSISTANT_MANAGEMENT_IMAGE=$(setting image "$DESTINATION/data/.installation")
            export ASSISTANT_MANAGEMENT_IMAGE
        fi
        revision_ok "$REVISION" && test -n "$SOURCE" && test -n "$REF" || fail 'Installation source metadata is incomplete or invalid.'
    fi
    if [ -z "$SOURCE" ] && [ "$GIT_CHECKOUT" = 1 ] && [ "$UPDATE" = 1 ]; then
        upstream=$(git -C "$DESTINATION" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null) \
            || fail 'This checkout needs an update source. Set ASSISTANT_SOURCE to its repository and ASSISTANT_VERSION to its deployment branch once.'
        remote=${upstream%%/*}
        SOURCE=$(git -C "$DESTINATION" remote get-url "$remote")
        case "$SOURCE" in /*|*:*) ;; *) SOURCE=$(CDPATH= cd -- "$DESTINATION/$SOURCE" && pwd -P) ;; esac
        [ -n "$REF" ] || REF=${upstream#*/}
    fi
    SOURCE=${SOURCE:-github:rb81/assistant}
    REF=${REF:-main}
    case "$SOURCE" in ''|-*|*'
'*|*://*@*) fail 'Invalid source; do not embed credentials in source URLs.' ;; esac
    case "$SOURCE" in /*|*:*) ;; *) SOURCE=$(CDPATH= cd -- "$SOURCE" && pwd -P) ;; esac
    case "$REF" in ''|-*|*[!A-Za-z0-9._/-]*) fail 'Invalid Assistant version/ref.' ;; esac
    export ASSISTANT_INSTALL_ROOT="$DESTINATION"
    export ASSISTANT_INSTALL_DIAGNOSTICS=1
    export ASSISTANT_UID="$(id -u)" ASSISTANT_GID="$(id -g)"
    unset COMPOSE_FILE COMPOSE_PROFILES COMPOSE_PROJECT_NAME
    if [ "$UPDATE" = 1 ]; then
        stage 2 'Downloading update'
        if [ -n "$REVISION" ]; then print_wrapped "Current revision: $REVISION"; fi
        WORK=$(mktemp -d "$DESTINATION.update.XXXXXXXX")
        check_checkout
        download_source
        print_wrapped "Requested revision: $REVISION"
        export ASSISTANT_REVISION="$REVISION"
        stage 3 'Preparing services'
        build_update
        activate_update
    else
        stage 2 'Repairing installation'
        print_wrapped 'Using existing source without downloading a new revision. Data is preserved.'
        if [ -z "$REVISION" ] && [ "$GIT_CHECKOUT" = 1 ]; then REVISION=$(git -C "$DESTINATION" rev-parse HEAD); fi
        export ASSISTANT_REVISION="${REVISION:-unknown}"
        stage 3 'Preparing services'
    fi
    CURRENT='deployment migrations'
    for migration in "$DESTINATION"/migrations/*.sh; do
        [ -f "$migration" ] || continue
        print_wrapped "Checking deployment migration: $(basename "$migration")"
        sh "$migration" "$DESTINATION/data" >>"$LOG" 2>&1 || fail 'A deployment migration failed; data was not rolled back.'
    done
    CURRENT='starting services and verifying readiness'
    print_wrapped 'Starting services and verifying readiness. This may take several minutes.'
    if ! run_logged 'starting services and verifying readiness' sh "$DESTINATION/run.sh" server start; then
        print_wrapped 'Startup stopped. Detailed diagnostics are in the protected log.'
        fail 'Assistant installation is incomplete. Fix the reported problem, then rerun the installer.'
    fi
    CURRENT='verifying the running API revision and capabilities'
    run_logged 'verifying the running API revision and capabilities' sh "$DESTINATION/run.sh" server verify \
        || fail 'The running API did not pass update verification. Check the protected log; the update was not marked successful.'
    if [ -f "$PENDING" ]; then
        cp "$DESTINATION/data/.install-target-files" "$DESTINATION/data/.installation-files"
        cp "$DESTINATION/data/.install-target-entries" "$DESTINATION/data/.installation-entries"
        mv "$PENDING" "$DESTINATION/data/.installation"
        rm "$DESTINATION/data/.install-target-files" "$DESTINATION/data/.install-target-entries"
    fi
    regular "$DESTINATION/data/.install-initializing" || fail 'Initial installation marker must be a regular file.'
    rm -f "$DESTINATION/data/.install-initializing"
    mkdir -p "$HOME/.local/bin"
    if [ ! -L "$LINK" ]; then ln -s "$DESTINATION/run.sh" "$LINK"; fi
    stage 4 'Installation complete'
    if grep -F 'Assistant and its management API are running.' "$LOG" >/dev/null; then
        printf '%sReady.%s\n' "$GREEN" "$RESET"
        print_wrapped 'Assistant and its management API are running. Open Assistant for macOS and reconnect.'
    else
        printf '%sInstalled.%s\n' "$GREEN" "$RESET"
        print_wrapped 'The management API is ready; assistant setup is still required.'
        print_wrapped 'Next: open guided setup, review your settings, then choose Save & start.'
        printf '\n  "%s"\n' "$LINK"
    fi
    if [ -n "$REVISION" ]; then print_wrapped "Installed revision: $REVISION"; fi
    if [ -d "$BACKUP" ]; then print_wrapped "Previous source retained: $BACKUP"; fi
    print_wrapped 'Startup diagnostics may contain private information. Review them before sharing.'
}
install_main "$@"
