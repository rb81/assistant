# Calendar Providers

Configure calendar accounts in `./run.sh --section calendar`. Assistant uses
OAuth 2.0 APIs directly. Zoom is a separate conferencing integration.
`AGENT_APP_TIMEZONE` sets the timezone for implicit calendar and reminder times.

## Supported providers

| Provider | ID | Required credentials |
|---|---|---|
| Google Calendar | `google` | `AGENT_CALENDAR_GOOGLE_CLIENT_ID`, `_CLIENT_SECRET`, `_REFRESH_TOKEN` |
| Outlook / Microsoft 365 | `outlook` | `AGENT_CALENDAR_OUTLOOK_CLIENT_ID`, `_CLIENT_SECRET`, `_REFRESH_TOKEN` |
| Zoho Calendar | `zoho` | `AGENT_CALENDAR_ZOHO_CLIENT_ID`, `_CLIENT_SECRET`, `_REFRESH_TOKEN` |

Enable `AGENT_CALENDAR_ENABLED=true` and provide all three credentials for each
account. Tool exposure checks for an unnumbered provider client ID; incomplete
credentials may expose tools without a usable provider, and numbered-only
accounts do not pass that check. Outlook's optional
`AGENT_CALENDAR_OUTLOOK_TENANT_ID` defaults to `common`; use your directory's
tenant ID for a single-tenant app.

## Zoom

For an account-owned server-to-server app, set
`AGENT_ZOOM_ENABLED=true`, `AGENT_ZOOM_AUTH_MODE=s2s`,
`AGENT_ZOOM_CLIENT_ID`, `AGENT_ZOOM_CLIENT_SECRET`, and
`AGENT_ZOOM_ACCOUNT_ID`. `AGENT_ZOOM_USER_ID` defaults to `me`. For a
user-authorized app, use `AGENT_ZOOM_AUTH_MODE=oauth` and
`AGENT_ZOOM_REFRESH_TOKEN` instead. Grant only the meeting read/write scopes
needed for the authorized host. Rotated tokens are persisted in private database
state; refresh is serialized and rate-limit backoff is retained.

Available tools: `zoom_create_meeting`, `zoom_read_meeting`,
`zoom_list_meetings`, `zoom_update_meeting`, `zoom_delete_meeting`, and
admin-only `zoom_reconcile_meeting`. Updates and deletions require local managed
IDs. Host start URLs are neither returned nor stored.

To link a meeting, create the Zoom meeting first and pass its local
`zoom_meeting_id` to `calendar_create_event` with the same start and duration.
The calendar tool appends the participant join URL and records the relationship;
do not add the link yourself. Description updates replace the text without
reinserting the link, so preserve it when editing. Linked updates/deletions
synchronize the other service.

Creation requires a stable idempotency key. After an uncertain timeout, verify
the authorized host's Zoom meeting and reconcile its provider ID with the
existing local request; never create a duplicate with a new key. After partial
failure, retry the same update/deletion, not creation. Recurring series,
joining, recordings, and transcription are not implemented. Mocked tests do not
validate live-account scopes or licensing.

## Multiple accounts per provider

The first account has no suffix (`_1` is unsupported); additional accounts use
`_2` through `_9`:

```env
AGENT_CALENDAR_GOOGLE_CLIENT_ID=...
AGENT_CALENDAR_GOOGLE_CLIENT_SECRET=...
AGENT_CALENDAR_GOOGLE_REFRESH_TOKEN=...

AGENT_CALENDAR_GOOGLE_2_CLIENT_ID=...
AGENT_CALENDAR_GOOGLE_2_CLIENT_SECRET=...
AGENT_CALENDAR_GOOGLE_2_REFRESH_TOKEN=...
AGENT_CALENDAR_GOOGLE_2_CALENDAR_ID=work@group.calendar.google.com
```

The same suffix scheme applies to Outlook and Zoho; provider IDs become
`google_2`, `outlook_2`, and so on. The wizard configures the first account per
provider. Numbered credentials use `./run.sh secrets update NAME`; numbered
non-secret values go in `data/overrides.env`. Recreate affected services after
changes.

---

## Google Calendar

### Step 1 — Create a Google Cloud project

In [Google Cloud Console](https://console.cloud.google.com), create or select a
project, open **APIs & Services → Library**, and enable **Google Calendar API**.

### Step 2 — Create OAuth 2.0 credentials

Under **APIs & Services → Credentials**, create an OAuth client ID for a
**Desktop app** and save its client ID and secret. Configure the consent screen
and add `https://www.googleapis.com/auth/calendar` when prompted.

### Step 3 — Obtain a refresh token

Use the loopback redirect flow below; Google's retired out-of-band (`oob`) flow
does not work. Run this on a machine with a browser, Python 3, `curl`, and `jq`.
Keep token responses private.

```bash
CLIENT_ID="your-client-id"
CLIENT_SECRET="your-client-secret"
SCOPE="https://www.googleapis.com/auth/calendar"
REDIRECT_URI="http://127.0.0.1:8765"

echo "https://accounts.google.com/o/oauth2/v2/auth?client_id=${CLIENT_ID}&redirect_uri=${REDIRECT_URI}&scope=${SCOPE}&response_type=code&access_type=offline&prompt=consent"

# Authorize in the browser, then paste its full redirect URL.
printf 'Paste redirect URL: ' >&2
AUTH_CODE="$(python3 -c 'from urllib.parse import urlparse, parse_qs; print(parse_qs(urlparse(input()).query)["code"][0])')"

curl --silent --show-error --fail-with-body https://oauth2.googleapis.com/token \
  --data-urlencode "code=${AUTH_CODE}" \
  --data-urlencode "client_id=${CLIENT_ID}" \
  --data-urlencode "client_secret=${CLIENT_SECRET}" \
  --data-urlencode "redirect_uri=${REDIRECT_URI}" \
  --data-urlencode "grant_type=authorization_code" | jq .
```

Copy `refresh_token` from the response. An external OAuth app left in **Testing**
may issue Calendar refresh tokens that expire after seven days; use a suitable
production consent configuration for a persistent installation.

### Step 4 — Store the credentials

Enter credentials in `./run.sh --section calendar` or later with
`./run.sh secrets update NAME`:
`AGENT_CALENDAR_GOOGLE_CLIENT_ID`,
`AGENT_CALENDAR_GOOGLE_CLIENT_SECRET`, and
`AGENT_CALENDAR_GOOGLE_REFRESH_TOKEN`. The optional
`AGENT_CALENDAR_GOOGLE_CALENDAR_ID` defaults to `primary` and is a non-secret
setting. Runtime refreshes access tokens as needed.

---

## Microsoft Outlook / Microsoft 365

### Step 1 — Register an application in Azure Entra ID

In [Microsoft Entra admin center](https://entra.microsoft.com), register an app
under **Entra ID → App registrations**. Select single-tenant or multitenant
access as required. Add `http://localhost` as a **Web** redirect URI and note
the application (client) ID and directory (tenant) ID.

### Step 2 — Add API permissions

Under **API permissions**, add Microsoft Graph delegated permissions
`Calendars.ReadWrite` and `offline_access`. Grant admin consent if required by
your tenant.

### Step 3 — Create a client secret

Create a secret under **Certificates & secrets** and copy its **Value**
immediately; it is displayed only once.

### Step 4 — Obtain a refresh token

```bash
TENANT_ID="your-tenant-id"
CLIENT_ID="your-client-id"
CLIENT_SECRET="your-client-secret"
SCOPE="https://graph.microsoft.com/Calendars.ReadWrite offline_access"

echo "https://login.microsoftonline.com/${TENANT_ID}/oauth2/v2.0/authorize?client_id=${CLIENT_ID}&response_type=code&redirect_uri=http://localhost&scope=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${SCOPE}'))")&response_mode=query"

# Authorize, then paste the full localhost redirect URL.
printf 'Paste redirect URL: ' >&2
AUTH_CODE="$(python3 -c 'from urllib.parse import urlparse, parse_qs; print(parse_qs(urlparse(input()).query)["code"][0])')"

curl --silent --show-error --fail-with-body "https://login.microsoftonline.com/${TENANT_ID}/oauth2/v2.0/token" \
  --data-urlencode "code=${AUTH_CODE}" \
  --data-urlencode "client_id=${CLIENT_ID}" \
  --data-urlencode "client_secret=${CLIENT_SECRET}" \
  --data-urlencode "redirect_uri=http://localhost" \
  --data-urlencode "grant_type=authorization_code" \
  --data-urlencode "scope=${SCOPE}" | jq .
```

Copy `refresh_token` from the response.

### Step 5 — Store the credentials

Use `./run.sh --section calendar` or
`./run.sh secrets update AGENT_CALENDAR_OUTLOOK_CLIENT_ID`,
`..._CLIENT_SECRET`, and `..._REFRESH_TOKEN`. The optional non-secret
`AGENT_CALENDAR_OUTLOOK_TENANT_ID` selects the tenant; the calendar ID defaults
to the user's default calendar and may be set with
`AGENT_CALENDAR_OUTLOOK_CALENDAR_ID`.

---

## Zoho Calendar

### Step 1 — Register a Zoho server-based application

At [Zoho API Console](https://api-console.zoho.com), add a
**Server-based Application**, set the redirect URI to `http://localhost`, and
save the client ID and secret.

### Step 2 — Add scopes

Grant both calendar discovery and event writes:
`ZohoCalendar.calendar.READ` and `ZohoCalendar.event.ALL`. Calendar-only scope
can allow reads but makes writes fail with `INVALID_OAUTHSCOPE`.

### Step 3 — Obtain a refresh token

```bash
CLIENT_ID="your-client-id"
CLIENT_SECRET="your-client-secret"
SCOPE="ZohoCalendar.calendar.READ,ZohoCalendar.event.ALL"

echo "https://accounts.zoho.com/oauth/v2/auth?scope=${SCOPE}&client_id=${CLIENT_ID}&response_type=code&redirect_uri=http://localhost&access_type=offline"

# Authorize, then paste the full localhost redirect URL.
printf 'Paste redirect URL: ' >&2
AUTH_CODE="$(python3 -c 'from urllib.parse import urlparse, parse_qs; print(parse_qs(urlparse(input()).query)["code"][0])')"

curl --silent --show-error --fail-with-body "https://accounts.zoho.com/oauth/v2/token" \
  --data-urlencode "code=${AUTH_CODE}" \
  --data-urlencode "client_id=${CLIENT_ID}" \
  --data-urlencode "client_secret=${CLIENT_SECRET}" \
  --data-urlencode "redirect_uri=http://localhost" \
  --data-urlencode "grant_type=authorization_code" | jq .
```

Copy `refresh_token` from the response.

### Step 4 — Store the credentials

Use `./run.sh --section calendar` or
`./run.sh secrets update AGENT_CALENDAR_ZOHO_CLIENT_ID`,
`..._CLIENT_SECRET`, and `..._REFRESH_TOKEN`.
`AGENT_CALENDAR_ZOHO_CALENDAR_ID` optionally selects a calendar UID; if reads
are empty, use `calendar_list_calendars` to find the correct UID. The API base
defaults to `https://calendar.zoho.com/api/v1`.

> **Data centers:** Use the Accounts host matching your Zoho region during
> authorization. Runtime refresh currently uses `accounts.zoho.com` regardless
> of `AGENT_CALENDAR_ZOHO_API_BASE`; non-`.com` account refresh is not fully
> supported. Setting a regional Calendar API URL does not change the token
> endpoint.
>
> **Scopes:** If you change OAuth scopes, obtain consent and a new refresh token;
> an existing token retains its original grants.

---

## Common environment variables

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_CALENDAR_ENABLED` | `false` | Enables the calendar tool-exposure check |
| `AGENT_APP_TIMEZONE` | `UTC` | Timezone for implicit event times and all-day boundaries |
| `AGENT_CALENDAR_DEFAULT_PROVIDER` | First discovered | Provider used when a request does not specify one |
| `AGENT_CALENDAR_DEFAULT_ALERT_MINUTES` | `30` | Reminder lead time; `0` suppresses alerts |

## Verifying configuration

Run `./run.sh --section calendar`, save, and apply changes to affected services.
The current management forms validate the draft, not live provider access.
Check services with `./run.sh management services`, then request a harmless
calendar availability read to verify the configured account and scopes.
The older host wizard's optional credential probe is not part of this interface.

A persistent Zoho HTTP 401 after one refresh is an authentication error, not
proof that the calendar is empty. Check the regional endpoint and credentials.
`INVALID_OAUTHSCOPE` requires new consent for
`ZohoCalendar.calendar.READ` and `ZohoCalendar.event.ALL`; refreshing cannot add
scopes.

`calendar_list_busy` reports `status`, `complete`, and `provider_errors` with
its windows. Treat availability as reliable only when `complete=true`; a
degraded or empty result does not mean the user is free. Calendar requests use
verified HTTPS; do not use plain HTTP or disable certificate checks.

## Security notes

- Refresh tokens grant long-lived calendar access. Protect `data/.secrets/` and
  never commit it.
- The assistant can modify only events it manages; it cannot change third-party
  events.
- `agent.calendar.policy.allow_read_event_details` defaults to `false` and hides
  details of unmanaged events. Busy-window titles are shown as `Busy`; details
  require this setting and the call's `include_details=true`.
