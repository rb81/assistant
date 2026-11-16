#!/bin/sh
# One-off migration: older installations used hand-picked setting and credential names.
# Settings are now named after their agent.yaml path (agent.email.smtp.host -> AGENT_EMAIL_SMTP_HOST),
# live in data/overrides.env, and credentials are stored under the same derived names.
# install.sh runs every migrations/*.sh it finds; delete this file once old installations are gone.
#
# Usage: 001-derived-config-names.sh DATA_DIR
set -eu

DATA=${1:?usage: 001-derived-config-names.sh DATA_DIR}
MARKER="$DATA/.migrations/001-derived-config-names"

[ -d "$DATA" ] || exit 0
[ -f "$DATA/.env" ] || [ -f "$DATA/overrides.env" ] || [ -d "$DATA/.secrets" ] || exit 0
[ ! -f "$MARKER" ] || exit 0

new_name() {
    case "$1" in
        ADMIN_EMAIL) echo AGENT_ADMIN_EMAIL ;;
        ADMIN_NAME) echo AGENT_ADMIN_NAME ;;
        AGENT_APPROVAL_CHANNEL) echo AGENT_APPROVALS_CHANNEL ;;
        AGENT_EMAIL) echo AGENT_IDENTITY_EMAIL ;;
        AGENT_IMAP_POLL_INTERVAL) echo AGENT_EMAIL_IMAP_POLL_INTERVAL_SECONDS ;;
        AGENT_MAX_EMAILS_PER_HOUR) echo AGENT_LIMITS_MAX_EMAILS_PER_HOUR ;;
        AGENT_NAME) echo AGENT_IDENTITY_NAME ;;
        AGENT_SANDBOX_ALLOWED_HOSTS) echo AGENT_SANDBOX_NETWORK_ALLOWED_HOSTS ;;
        AGENT_SEARCH_CONTEXT_SIZE) echo AGENT_SEARCH_SEARCH_CONTEXT_SIZE ;;
        AGENT_SECURITY_DECISION_APPROVAL_TTL_MINUTES) echo AGENT_SECURITY_DECISION_MODEL_APPROVAL_TTL_MINUTES ;;
        AGENT_SECURITY_DECISION_ENABLED) echo AGENT_SECURITY_DECISION_MODEL_ENABLED ;;
        AGENT_SECURITY_DECISION_FLAG_THRESHOLD) echo AGENT_SECURITY_DECISION_MODEL_FLAG_THRESHOLD ;;
        AGENT_SECURITY_DECISION_MALICIOUS_THRESHOLD) echo AGENT_SECURITY_DECISION_MODEL_MALICIOUS_THRESHOLD ;;
        AGENT_SECURITY_DECISION_MAX_FIELD_CHARS) echo AGENT_SECURITY_DECISION_MODEL_MAX_FIELD_CHARS ;;
        AGENT_SECURITY_DECISION_MODEL) echo AGENT_SECURITY_DECISION_MODEL_MODEL ;;
        AGENT_SECURITY_DECISION_ON_ERROR) echo AGENT_SECURITY_DECISION_MODEL_ON_ERROR ;;
        AGENT_SECURITY_DECISION_POSSIBLY_MALICIOUS_THRESHOLD) echo AGENT_SECURITY_DECISION_MODEL_POSSIBLY_MALICIOUS_THRESHOLD ;;
        AGENT_SECURITY_DECISION_PROVIDER) echo AGENT_SECURITY_DECISION_MODEL_PROVIDER ;;
        AGENT_SECURITY_DECISION_TIMEOUT_SECONDS) echo AGENT_SECURITY_DECISION_MODEL_TIMEOUT_SECONDS ;;
        AGENT_SECURITY_DECISION_URL) echo AGENT_SECURITY_DECISION_MODEL_URL ;;
        AGENT_SECURITY_EMAIL_AUTH_ENABLED) echo AGENT_SECURITY_EMAIL_AUTHENTICATION_ENABLED ;;
        AGENT_SECURITY_SCREEN_OUTBOUND_EMAIL) echo AGENT_SECURITY_DECISION_MODEL_SCREEN_OUTBOUND_EMAIL ;;
        AGENT_SECURITY_SCREEN_SCRIPTS) echo AGENT_SECURITY_DECISION_MODEL_SCREEN_SCRIPTS ;;
        AGENT_SECURITY_SCRIPT_ALLOWED_DOMAINS) echo AGENT_SECURITY_SCRIPTS_ALLOWED_DOMAINS ;;
        AGENT_SECURITY_SCRIPT_MAX_FILES) echo AGENT_SECURITY_SCRIPTS_MAX_FILES ;;
        AGENT_TIMEZONE) echo AGENT_APP_TIMEZONE ;;
        CALENDAR_DEFAULT_ALERT_MINUTES|CALENDAR_DEFAULT_PROVIDER|CALENDAR_ENABLED|CALENDAR_TIMEZONE) echo "AGENT_$1" ;;
        CALENDAR_GOOGLE_*|CALENDAR_OUTLOOK_*|CALENDAR_ZOHO_*) echo "AGENT_$1" ;;
        DATABASE_URL) echo AGENT_DATABASE_URL ;;
        DROPBOX_*) echo "AGENT_$1" ;;
        EMAIL_ACTIONABLE_SENDERS|EMAIL_ALLOWED_RECIPIENT_DOMAINS) echo "AGENT_$1" ;;
        IMAP_*) echo "AGENT_EMAIL_$1" ;;
        OPENAI_API_KEY) echo AGENT_LLM_OPENAI_API_KEY ;;
        OPENROUTER_API_KEY) echo AGENT_LLM_OPENROUTER_API_KEY ;;
        ORG_*) echo "AGENT_$1" ;;
        POSTGRES_PASSWORD) echo AGENT_DATABASE_PASSWORD ;;
        SANDBOX_API_TOKEN|SANDBOX_REVOKE_TOKEN) echo "AGENT_$1" ;;
        SECURITY_DECISION_API_KEY) echo AGENT_SECURITY_DECISION_MODEL_API_KEY ;;
        SMTP_*) echo "AGENT_EMAIL_$1" ;;
        TELEGRAM_*|TRANSCRIPTION_*|ZOOM_*) echo "AGENT_$1" ;;
        *) echo "$1" ;;
    esac
}

is_old_secret() {
    case "$1" in
        POSTGRES_PASSWORD|DATABASE_URL|OPENROUTER_API_KEY|OPENAI_API_KEY|IMAP_PASSWORD|SMTP_PASSWORD) return 0 ;;
        SANDBOX_API_TOKEN|SANDBOX_REVOKE_TOKEN|SECURITY_DECISION_API_KEY|TELEGRAM_BOT_TOKEN|TRANSCRIPTION_API_TOKEN) return 0 ;;
        ZOOM_CLIENT_ID|ZOOM_CLIENT_SECRET|ZOOM_ACCOUNT_ID|ZOOM_REFRESH_TOKEN|DROPBOX_CLIENT_ID|DROPBOX_REFRESH_TOKEN) return 0 ;;
        CALENDAR_GOOGLE_CLIENT_ID|CALENDAR_GOOGLE_CLIENT_SECRET|CALENDAR_GOOGLE_REFRESH_TOKEN) return 0 ;;
        CALENDAR_OUTLOOK_CLIENT_ID|CALENDAR_OUTLOOK_CLIENT_SECRET|CALENDAR_OUTLOOK_REFRESH_TOKEN) return 0 ;;
        CALENDAR_ZOHO_CLIENT_ID|CALENDAR_ZOHO_CLIENT_SECRET|CALENDAR_ZOHO_REFRESH_TOKEN) return 0 ;;
        CALENDAR_GOOGLE_[2-9]_CLIENT_ID|CALENDAR_GOOGLE_[2-9]_CLIENT_SECRET|CALENDAR_GOOGLE_[2-9]_REFRESH_TOKEN) return 0 ;;
        CALENDAR_OUTLOOK_[2-9]_CLIENT_ID|CALENDAR_OUTLOOK_[2-9]_CLIENT_SECRET|CALENDAR_OUTLOOK_[2-9]_REFRESH_TOKEN) return 0 ;;
        CALENDAR_ZOHO_[2-9]_CLIENT_ID|CALENDAR_ZOHO_[2-9]_CLIENT_SECRET|CALENDAR_ZOHO_[2-9]_REFRESH_TOKEN) return 0 ;;
    esac
    return 1
}

VOLUME=
if [ -f "$DATA/.secrets/state.json" ]; then
    VOLUME=$(sed -n 's/.*"credential_volume": *"\(assistant-credentials-[a-f0-9]\{16\}\)".*/\1/p' "$DATA/.secrets/state.json")
    if [ -z "$VOLUME" ] && grep -Eq '"credential_volume": *"[^"]' "$DATA/.secrets/state.json"; then
        echo "Unrecognized credential volume name in data/.secrets/state.json; nothing was changed." >&2
        exit 1
    fi
fi
if [ -n "$VOLUME" ]; then
    docker volume inspect "$VOLUME" >/dev/null 2>&1 || { echo "Credential volume $VOLUME is missing; nothing was changed." >&2; exit 1; }
fi

umask 077
BACKUP="$DATA/backups/config-names-$(date +%Y%m%d%H%M%S)"
mkdir -p "$BACKUP"
for file in .env overrides.env .secrets/state.json .compose-secrets.yaml; do
    if [ -f "$DATA/$file" ]; then
        mkdir -p "$BACKUP/$(dirname "$file")"
        cp -p "$DATA/$file" "$BACKUP/$file"
    fi
done
[ ! -d "$DATA/.secrets" ] || cp -pR "$DATA/.secrets" "$BACKUP/secrets-files"

TMP=$(mktemp -d "${TMPDIR:-/tmp}/assistant-migrate.XXXXXXXX")
trap 'rm -rf "$TMP"' EXIT
: > "$TMP/env.new"
: > "$TMP/moved"
: > "$TMP/existing"
mkdir "$TMP/incoming"
# Inline credentials go to the store the installation actually uses.
if [ -n "$VOLUME" ]; then SECRETS="$TMP/incoming"; else SECRETS="$DATA/.secrets"; fi

# Settings leave .env for overrides.env; credentials leave it for the credential store.
# Values are copied untouched so quoting is preserved.
if [ -f "$DATA/.env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ''|'#'*) echo "$line" >> "$TMP/env.new"; continue ;;
            *=*) ;;
            *) echo "$line" >> "$TMP/env.new"; continue ;;
        esac
        old=${line%%=*}
        value=${line#*=}
        key=$(new_name "$old")
        if is_old_secret "$old"; then
            mkdir -p "$SECRETS"
            if [ ! -e "$SECRETS/$key" ]; then
                case "$value" in
                    \'*\') value=${value#\'}; value=${value%\'} ;;
                    \"*\") value=${value#\"}; value=${value%\"} ;;
                esac
                printf '%s' "$value" > "$SECRETS/$key"
            fi
            continue
        fi
        case "$key" in
            AGENT_*) echo "$key=$value" >> "$TMP/moved" ;;
            *) echo "$key=$value" >> "$TMP/env.new" ;;
        esac
    done < "$DATA/.env"
fi

# Existing overrides win over anything that used to live in .env.
if [ -f "$DATA/overrides.env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            '# '[A-Z]*=*) commented=${line#\# }; echo "# $(new_name "${commented%%=*}")=${commented#*=}" >> "$TMP/existing" ;;
            ''|'#'*) echo "$line" >> "$TMP/existing" ;;
            *=*) echo "$(new_name "${line%%=*}")=${line#*=}" >> "$TMP/existing" ;;
            *) echo "$line" >> "$TMP/existing" ;;
        esac
    done < "$DATA/overrides.env"
fi
while IFS= read -r line; do
    grep -q "^${line%%=*}=" "$TMP/existing" || echo "$line" >> "$TMP/existing"
done < "$TMP/moved"

# Credentials kept in a Docker volume are renamed in place by a disposable, network-isolated container.
# Old values win over anything already stored under the new name; per-service files are rebuilt and
# grants are recomputed on the next server start. Runs before any host file changes so a failure is retryable.
if [ -n "$VOLUME" ]; then
    # Input framing: "R OLD NEW" rename lines, then "S NAME BYTES" headers each followed by the raw value.
    {
        for old in POSTGRES_PASSWORD DATABASE_URL OPENROUTER_API_KEY OPENAI_API_KEY IMAP_PASSWORD SMTP_PASSWORD \
            SANDBOX_API_TOKEN SANDBOX_REVOKE_TOKEN SECURITY_DECISION_API_KEY TELEGRAM_BOT_TOKEN TRANSCRIPTION_API_TOKEN \
            ZOOM_CLIENT_ID ZOOM_CLIENT_SECRET ZOOM_ACCOUNT_ID ZOOM_REFRESH_TOKEN DROPBOX_CLIENT_ID DROPBOX_REFRESH_TOKEN; do
            echo "R $old $(new_name "$old")"
        done
        for provider in GOOGLE OUTLOOK ZOHO; do
            for field in CLIENT_ID CLIENT_SECRET REFRESH_TOKEN; do
                echo "R CALENDAR_${provider}_$field AGENT_CALENDAR_${provider}_$field"
                for n in 2 3 4 5 6 7 8 9; do
                    echo "R CALENDAR_${provider}_${n}_$field AGENT_CALENDAR_${provider}_${n}_$field"
                done
            done
        done
        for path in "$TMP/incoming"/*; do
            [ -f "$path" ] || continue
            echo "S $(basename "$path") $(wc -c < "$path" | tr -d ' ')"
            cat "$path"
        done
    } | docker run --rm -i --log-driver none --network none --read-only --cap-drop ALL \
        --security-opt no-new-privileges:true \
        --mount "type=volume,source=$VOLUME,target=/credentials" \
        python:3.12-slim python -c '
import fcntl, json, os, sys, tempfile
from pathlib import Path
renames, incoming = {}, {}
stream = sys.stdin.buffer
while header := stream.readline().decode():
    kind, name, other = header.split()
    if kind == "R":
        renames[name] = other
    else:
        incoming[name] = stream.read(int(other)).decode()
root = Path("/credentials")
def atomic(path, content):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, "w") as output:
        output.write(content); output.flush(); os.fsync(output.fileno())
    os.replace(temporary, path)
with (root / "lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    store = root / "store.json"
    recovery = root / "recovery.json"
    # An interrupted write left its intended record behind; it supersedes store.json.
    source = recovery if recovery.exists() else store
    record = json.loads(source.read_text()) if source.exists() else {}
    values = dict(record.get("values", {}))
    for old, new in renames.items():
        if old in values:
            values[new] = values.pop(old)
    for name, value in incoming.items():
        values.setdefault(name, value)
    grants = {service: sorted({renames.get(name, name) for name in names} & set(values))
              for service, names in record.get("grants", {}).items()}
    services = root / "services"
    services.mkdir(mode=0o700, exist_ok=True)
    for service in set(grants) | {path.name for path in services.iterdir()}:
        directory = services / service
        directory.mkdir(mode=0o700, exist_ok=True)
        for name in grants.get(service, []):
            atomic(directory / name, values[name])
        for path in directory.iterdir():
            if path.name not in grants.get(service, []):
                path.unlink()
    atomic(store, json.dumps({"values": values, "grants": grants}))
    if recovery.exists():
        recovery.unlink()
' || { echo "Could not rename credentials in Docker volume $VOLUME; nothing on the host was changed." >&2; exit 1; }
fi

[ ! -f "$DATA/.env" ] || cat "$TMP/env.new" > "$DATA/.env"
if [ -s "$TMP/existing" ]; then
    cat "$TMP/existing" > "$DATA/overrides.env"
fi

# Credential files, and the setup state that names them.
names=
if [ -d "$DATA/.secrets" ]; then
    for path in "$DATA/.secrets"/*; do
        [ -f "$path" ] || continue
        old=$(basename "$path")
        is_old_secret "$old" || continue
        names="$names $old"
        # The pre-migration credential is authoritative over anything generated under the new name.
        mv -f "$path" "$DATA/.secrets/$(new_name "$old")"
    done
fi
if [ -f "$DATA/.secrets/state.json" ]; then
    expression=
    for old in POSTGRES_PASSWORD DATABASE_URL OPENROUTER_API_KEY OPENAI_API_KEY IMAP_PASSWORD SMTP_PASSWORD \
        SANDBOX_API_TOKEN SANDBOX_REVOKE_TOKEN SECURITY_DECISION_API_KEY TELEGRAM_BOT_TOKEN TRANSCRIPTION_API_TOKEN \
        ZOOM_CLIENT_ID ZOOM_CLIENT_SECRET ZOOM_ACCOUNT_ID ZOOM_REFRESH_TOKEN DROPBOX_CLIENT_ID DROPBOX_REFRESH_TOKEN \
        $names $(for p in GOOGLE OUTLOOK ZOHO; do for f in CLIENT_ID CLIENT_SECRET REFRESH_TOKEN; do echo "CALENDAR_${p}_$f"; done; done); do
        expression="$expression -e s/\"$old\"/\"$(new_name "$old")\"/g"
    done
    # shellcheck disable=SC2086
    sed $expression "$DATA/.secrets/state.json" > "$TMP/state.json"
    cat "$TMP/state.json" > "$DATA/.secrets/state.json"
fi

# The next server start regenerates the Compose credential overlay from the renamed files.
rm -f "$DATA/.compose-secrets.yaml"
mkdir -p "$DATA/.migrations"
: > "$MARKER"
echo "Migrated configuration names (backup: $BACKUP)."
