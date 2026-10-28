# Calendar Providers

Assistant connects to calendar services directly via OAuth 2.0 APIs. This document covers how to obtain credentials for each supported provider.

## Supported providers

Zoom is a separate conferencing integration. `AGENT_TIMEZONE` controls implicit calendar and reminder times. Request-time context includes local and UTC clocks.

| Provider | Identifier | Required env vars |
|----------|-----------|-------------------|
| Google Calendar | `google` | `CALENDAR_GOOGLE_CLIENT_ID`, `CALENDAR_GOOGLE_CLIENT_SECRET`, `CALENDAR_GOOGLE_REFRESH_TOKEN` |
| Microsoft Outlook / Microsoft 365 | `outlook` | `CALENDAR_OUTLOOK_CLIENT_ID`, `CALENDAR_OUTLOOK_CLIENT_SECRET`, `CALENDAR_OUTLOOK_REFRESH_TOKEN` |
| Zoho Calendar | `zoho` | `CALENDAR_ZOHO_CLIENT_ID`, `CALENDAR_ZOHO_CLIENT_SECRET`, `CALENDAR_ZOHO_REFRESH_TOKEN` |

Enable `CALENDAR_ENABLED=true` and supply all three credentials for each account.
The tool-exposure check only requires an unnumbered provider client ID; incomplete
credentials can expose tools without loading a usable provider, and numbered-only
accounts do not pass that check. No edits to tracked defaults are required.
Outlook's optional `CALENDAR_OUTLOOK_TENANT_ID` defaults to `common`; set it to
your directory's tenant ID for a single-tenant app.

## Zoom

Set `ZOOM_ENABLED=true`, `ZOOM_AUTH_MODE=s2s`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`, and `ZOOM_ACCOUNT_ID` for an account-owned server-to-server app. `ZOOM_USER_ID` defaults to `me`. Alternatively use `ZOOM_AUTH_MODE=oauth` and `ZOOM_REFRESH_TOKEN` for a user-authorized app. Grant only meeting read/write scopes needed for the authorized host. Rotated refresh/access tokens persist in private database state, with serialized refresh and retry-after backoff.

Tools: `zoom_create_meeting`, `zoom_read_meeting`, `zoom_list_meetings`, `zoom_update_meeting`, `zoom_delete_meeting`, and admin-only `zoom_reconcile_meeting`. Updates and deletions require local managed IDs. Host start URLs are neither returned nor stored. Creation requires a stable idempotency key. After an uncertain timeout, verify the authorized host's Zoom meeting and reconcile its provider ID with the existing request; never use a new key to create a duplicate.

Create Zoom first, then pass its local `zoom_meeting_id` to
`calendar_create_event`. Start and duration must match. Creation appends
`\n\nZoom: <participant join URL>` to the description and records the relationship;
do not insert the link yourself. Description updates replace the text without
reinserting the link, so preserve it when editing a linked event.

Linked edits/deletions synchronize the other service. After a partial failure,
retry the same update/deletion, not creation. Recurring Zoom series, joining,
recordings, and transcription are not implemented. Mocked API and database tests
do not validate a live account's scopes or licensing.

## Multiple accounts per provider

The first account has no suffix. Additional accounts use `_2` through `_9`
between the provider name and setting:

```env
# First Google account (no suffix; _1 is not supported)
CALENDAR_GOOGLE_CLIENT_ID=...
CALENDAR_GOOGLE_CLIENT_SECRET=...
CALENDAR_GOOGLE_REFRESH_TOKEN=...

# Second Google account
CALENDAR_GOOGLE_2_CLIENT_ID=...
CALENDAR_GOOGLE_2_CLIENT_SECRET=...
CALENDAR_GOOGLE_2_REFRESH_TOKEN=...
CALENDAR_GOOGLE_2_CALENDAR_ID=work@group.calendar.google.com
```

The same suffix scheme works for Outlook and Zoho. Provider identifiers become
`google_2`, `outlook_2`, etc. Numbered non-secret settings are environment-only;
there are no numbered YAML paths. The wizard configures the first account per
provider; manage additional credentials with `./setup.sh secrets update NAME`
and non-secret settings in `data/overrides.env`, then recreate affected services.

---

## Google Calendar

### Step 1 — Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a new project (or use an existing one).
2. In the left menu, go to **APIs & Services → Library**.
3. Search for **Google Calendar API** and click **Enable**.

### Step 2 — Create OAuth 2.0 credentials

1. Go to **APIs & Services → Credentials** and click **Create Credentials → OAuth client ID**.
2. Choose **Desktop app** as the application type.
3. Give it a name (e.g., "Assistant Calendar") and click **Create**.
4. Note the **Client ID** and **Client Secret** shown in the confirmation dialog.

> If this is your first time setting up OAuth on this project, you will also need to configure the **OAuth consent screen** (internal or external) and add the calendar scope `https://www.googleapis.com/auth/calendar` to the scopes list.

### Step 3 — Obtain a refresh token

Use a loopback redirect, not Google's retired out-of-band (`oob`) flow.
The OAuth examples below require Python 3, `curl` and `jq`. Keep token responses
private. Run this on the machine with your browser; replace the placeholders:

```bash
CLIENT_ID="your-client-id"
CLIENT_SECRET="your-client-secret"
SCOPE="https://www.googleapis.com/auth/calendar"
REDIRECT_URI="http://127.0.0.1:8765"

# Step 3a — Open this URL in a browser and authorize access
echo "https://accounts.google.com/o/oauth2/v2/auth?client_id=${CLIENT_ID}&redirect_uri=${REDIRECT_URI}&scope=${SCOPE}&response_type=code&access_type=offline&prompt=consent"

# Step 3b — The browser redirects to 127.0.0.1:8765 and may show a connection
# error because no local server is listening. Copy the full redirect URL.
# This extracts and URL-decodes its code parameter.
printf 'Paste redirect URL: ' >&2
AUTH_CODE="$(python3 -c 'from urllib.parse import urlparse, parse_qs; print(parse_qs(urlparse(input()).query)["code"][0])')"

# Step 3c — Exchange the code for tokens
curl --silent --show-error --fail-with-body https://oauth2.googleapis.com/token \
  --data-urlencode "code=${AUTH_CODE}" \
  --data-urlencode "client_id=${CLIENT_ID}" \
  --data-urlencode "client_secret=${CLIENT_SECRET}" \
  --data-urlencode "redirect_uri=${REDIRECT_URI}" \
  --data-urlencode "grant_type=authorization_code" | jq .
```

The response will contain `refresh_token`. Copy it.
External OAuth apps left in **Testing** can receive refresh tokens that expire
after seven days for Calendar scopes. Use an appropriate production consent
configuration for a persistent installation.

### Step 4 — Store the credentials

Enter them in `./setup.sh --section calendar` (stored as protected secrets, not in `data/.env`), or update later with `./setup.sh secrets update NAME`. The names are:

```text
CALENDAR_GOOGLE_CLIENT_ID
CALENDAR_GOOGLE_CLIENT_SECRET
CALENDAR_GOOGLE_REFRESH_TOKEN
```

The optional target calendar (defaults to `primary`) is the non-secret setting `CALENDAR_GOOGLE_CALENDAR_ID`, which the wizard writes to `data/.env`.

The `CalendarRouter` will automatically refresh the access token when it expires using the stored refresh token.

---

## Microsoft Outlook / Microsoft 365

### Step 1 — Register an application in Azure Entra ID

1. Go to [Microsoft Entra admin center](https://entra.microsoft.com) → **Entra ID → App registrations → New registration**.
2. Give it a name, select **Accounts in this organizational directory only** (or **Multitenant** if needed), and set the redirect URI to `http://localhost` (platform: **Web**, matching this client-secret flow).
3. After registration, note the **Application (client) ID** and the **Directory (tenant) ID**.

### Step 2 — Add API permissions

1. Go to **API permissions → Add a permission → Microsoft Graph**.
2. Choose **Delegated permissions** and add:
   - `Calendars.ReadWrite`
   - `offline_access`
3. Click **Grant admin consent** if your tenant requires it.

### Step 3 — Create a client secret

1. Go to **Certificates & secrets → New client secret**.
2. Set an expiry and click **Add**.
3. Copy the **Value** immediately — it is only shown once.

### Step 4 — Obtain a refresh token

```bash
TENANT_ID="your-tenant-id"
CLIENT_ID="your-client-id"
CLIENT_SECRET="your-client-secret"
SCOPE="https://graph.microsoft.com/Calendars.ReadWrite offline_access"

# Step 4a — Open this URL in a browser
echo "https://login.microsoftonline.com/${TENANT_ID}/oauth2/v2.0/authorize?client_id=${CLIENT_ID}&response_type=code&redirect_uri=http://localhost&scope=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${SCOPE}'))")&response_mode=query"

# Step 4b — After authorizing, the browser redirects to http://localhost?code=...
# A connection error is expected if no local server is listening.
# Copy the full redirect URL to extract and URL-decode its code parameter.

printf 'Paste redirect URL: ' >&2
AUTH_CODE="$(python3 -c 'from urllib.parse import urlparse, parse_qs; print(parse_qs(urlparse(input()).query)["code"][0])')"

# Step 4c — Exchange for tokens
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

Enter the client ID, client secret and refresh token in `./setup.sh --section calendar` (stored as protected secrets), or update later with `./setup.sh secrets update NAME` (`CALENDAR_OUTLOOK_CLIENT_ID`, `CALENDAR_OUTLOOK_CLIENT_SECRET`, `CALENDAR_OUTLOOK_REFRESH_TOKEN`). Non-secret settings go to `data/.env` through the wizard:

```env
CALENDAR_OUTLOOK_TENANT_ID=your-tenant-id

# Optional: target a specific calendar (defaults to the user's default calendar)
# CALENDAR_OUTLOOK_CALENDAR_ID=AAMkAGI2...
```

---

## Zoho Calendar

### Step 1 — Register a Zoho server-based application

1. Go to [api-console.zoho.com](https://api-console.zoho.com) and click **Add Client**.
2. Choose **Server-based Applications**.
3. Set **Authorized Redirect URIs** to `http://localhost`.
4. Note the **Client ID** and **Client Secret**.

### Step 2 — Add scopes

Use scopes that cover both calendar discovery and event writes. Recommended:

- `ZohoCalendar.calendar.READ`
- `ZohoCalendar.event.ALL`

If your integration only has calendar scope (for example `ZohoCalendar.calendar.ALL`) and not
event write scope, create/update calls will fail with `INVALID_OAUTHSCOPE`.

### Step 3 — Obtain a refresh token

```bash
CLIENT_ID="your-client-id"
CLIENT_SECRET="your-client-secret"
SCOPE="ZohoCalendar.calendar.READ,ZohoCalendar.event.ALL"

# Step 3a — Open this URL in a browser
echo "https://accounts.zoho.com/oauth/v2/auth?scope=${SCOPE}&client_id=${CLIENT_ID}&response_type=code&redirect_uri=http://localhost&access_type=offline"

# Step 3b — Copy the full redirect URL (a localhost connection error is expected).
printf 'Paste redirect URL: ' >&2
AUTH_CODE="$(python3 -c 'from urllib.parse import urlparse, parse_qs; print(parse_qs(urlparse(input()).query)["code"][0])')"

# Step 3c — Exchange for tokens
curl --silent --show-error --fail-with-body "https://accounts.zoho.com/oauth/v2/token" \
  --data-urlencode "code=${AUTH_CODE}" \
  --data-urlencode "client_id=${CLIENT_ID}" \
  --data-urlencode "client_secret=${CLIENT_SECRET}" \
  --data-urlencode "redirect_uri=http://localhost" \
  --data-urlencode "grant_type=authorization_code" | jq .
```

Copy `refresh_token` from the response.

### Step 4 — Store the credentials

Enter the client ID, client secret and refresh token in `./setup.sh --section calendar` (stored as protected secrets), or update later with `./setup.sh secrets update NAME` (`CALENDAR_ZOHO_CLIENT_ID`, `CALENDAR_ZOHO_CLIENT_SECRET`, `CALENDAR_ZOHO_REFRESH_TOKEN`). Optional non-secret settings go to `data/.env` through the wizard:

```env
# Optional: target a specific calendar UID. If omitted, the integration uses "default",
# which may be empty if your events are stored in another Zoho calendar.
# CALENDAR_ZOHO_CALENDAR_ID=849d6badb4e04acc91860c43db0fb109

# Optional: setup writes this to data/.env; manual overrides belong in
# data/overrides.env. Default: https://calendar.zoho.com/api/v1
# CALENDAR_ZOHO_API_BASE=https://calendar.zoho.eu/api/v1
```

> **Zoho data center regions** use different API and Accounts hosts. Use the
> matching Accounts host for authorization/token exchange above. However,
> runtime token refresh currently uses `accounts.zoho.com` regardless of
> `CALENDAR_ZOHO_API_BASE`. Setting a regional Calendar API (for example
> `https://calendar.zoho.eu/api/v1`) does not change that token endpoint;
> non-`.com` account refresh is not fully supported.
>
> **Zoho calendar UIDs**: Zoho API requests are calendar-UID specific (`/calendars/<calendar_uid>/...`). If reads appear empty,
> use the `calendar_list_calendars` tool to discover the correct UID, then set `CALENDAR_ZOHO_CALENDAR_ID` (or
> `agent.calendar.zoho.calendar_id` in `agent.yaml`).
>
> **OAuth scope changes**: If you change OAuth scopes after initial setup, regenerate the authorization code and refresh token.
> Existing refresh tokens keep their originally granted scopes.

---

## Common environment variables

These variables apply across all providers:

| Variable | Default | Description |
|----------|---------|-------------|
| `CALENDAR_ENABLED` | `false` | Enable the calendar tool-exposure check described above |
| `AGENT_TIMEZONE` | `UTC` | Authoritative timezone for implicit event times and all-day boundaries (e.g. `America/New_York`) |
| `CALENDAR_DEFAULT_PROVIDER` | first discovered | Which provider to use when the agent does not specify one |
| `CALENDAR_DEFAULT_ALERT_MINUTES` | `30` | Minutes before each event to set a reminder alert (0 to suppress) |

---

## Verifying configuration

Run `./setup.sh --section calendar`.
The optional read-only verification uses a fresh access token and checks both
calendar discovery and event access, without creating events or persisting the
test token. Verification failure does not prevent saving an offline draft, and
no credentials are automatically rotated.

Zoho `.com` accounts use `https://accounts.zoho.com/oauth/v2/token` and
`https://calendar.zoho.com/api/v1`. A persistent HTTP 401 after one token refresh is an authentication error,
not proof of an empty calendar. If it persists after fixing the route, verify the
client ID/secret and matching refresh token. `INVALID_OAUTHSCOPE` requires renewed
consent for `ZohoCalendar.calendar.READ` and `ZohoCalendar.event.ALL`: token refresh
cannot grant missing scopes. Use setup or `./setup.sh secrets update`
to store replacements, and explicitly activate/recreate affected consumers.

`calendar_list_busy` returns `status`, `complete`, and `provider_errors` alongside
the existing windows. Only `complete=true` verifies availability. With failed
providers, healthy-provider windows remain available but `status=degraded` and
`complete=false`; an empty list is not evidence that the user is free. With no
providers, `status=no_providers` and `complete=false`.

After adding credentials and activating the affected services, check tool status
with `./setup.sh tools`. Calendar-provider requests use verified HTTPS; never
use plain HTTP or disable certificate verification.

---

## Security notes

- Refresh tokens grant long-lived access to your calendar. Store `data/.secrets/` securely and never commit it to source control.
- The assistant can only create, update, and delete events that it created itself — it cannot modify third-party events.
- `agent.calendar.policy.allow_read_event_details: false` (the default) hides
  non-managed event details; busy-window titles become `Busy`. Assistant-managed
  events remain visible. Busy-window details for other events require both this
  setting and the call's `include_details=true`.
