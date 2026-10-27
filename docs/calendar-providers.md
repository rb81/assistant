# Calendar Providers

Assistant connects to calendar services directly via OAuth 2.0 APIs. This document covers how to obtain credentials for each supported provider.

## Supported providers

Zoom is a separate conferencing integration. `AGENT_TIMEZONE` controls implicit calendar and reminder times. Request-time context includes local and UTC clocks.

| Provider | Identifier | Required env vars |
|----------|-----------|-------------------|
| Google Calendar | `google` | `CALENDAR_GOOGLE_CLIENT_ID`, `CALENDAR_GOOGLE_CLIENT_SECRET`, `CALENDAR_GOOGLE_REFRESH_TOKEN` |
| Microsoft Outlook / Microsoft 365 | `outlook` | `CALENDAR_OUTLOOK_CLIENT_ID`, `CALENDAR_OUTLOOK_CLIENT_SECRET`, `CALENDAR_OUTLOOK_REFRESH_TOKEN`, `CALENDAR_OUTLOOK_TENANT_ID` |
| Zoho Calendar | `zoho` | `CALENDAR_ZOHO_CLIENT_ID`, `CALENDAR_ZOHO_CLIENT_SECRET`, `CALENDAR_ZOHO_REFRESH_TOKEN` |

Calendar tools stay disabled until `CALENDAR_ENABLED=true` and at least one provider's `*_CLIENT_ID` / `*_CLIENT_SECRET` / `*_REFRESH_TOKEN` are set. No edits to tracked defaults are required.

## Zoom

Set `ZOOM_ENABLED=true`, `ZOOM_AUTH_MODE=s2s`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`, and `ZOOM_ACCOUNT_ID` for an account-owned server-to-server app. `ZOOM_USER_ID` defaults to `me`. Alternatively use `ZOOM_AUTH_MODE=oauth` and `ZOOM_REFRESH_TOKEN` for a user-authorized app. Grant only meeting read/write scopes needed for the authorized host. Rotated refresh/access tokens persist in private database state, with serialized refresh and retry-after backoff.

Tools: `zoom_create_meeting`, `zoom_read_meeting`, `zoom_list_meetings`, `zoom_update_meeting`, `zoom_delete_meeting`, and admin-only `zoom_reconcile_meeting`. Writes require local managed IDs. Host start URLs are neither returned nor stored. Creation requires a stable idempotency key. After an uncertain timeout, verify the authorized host's Zoom meeting and reconcile its provider ID with the existing request; never use a new key to create a duplicate.

Create Zoom first, then pass its local `zoom_meeting_id` to `calendar_create_event`. Start and duration must match. The calendar tool automatically appends `\n\nZoom: <participant join URL>` to the supplied description, adds `zoom_meeting_id` to metadata, and records the relationship. Do not also insert the link into the creation description. Description updates replace the existing text without automatically reinserting the Zoom link, so preserve it when editing a linked event. Host start URLs are never added. Linked edits/deletions synchronize the other service; after a partial failure, retry the same update/deletion, not creation. Recurring Zoom series, joining, recordings, and transcription are not implemented. Live account validation requires configured credentials; mocked API tests and live database lifecycle tests do not prove an account's scopes/licensing.

## Multiple accounts per provider

To configure more than one account for a single provider, append a numeric suffix:

```env
# First Google account (no suffix or _1)
CALENDAR_GOOGLE_CLIENT_ID=...
CALENDAR_GOOGLE_CLIENT_SECRET=...
CALENDAR_GOOGLE_REFRESH_TOKEN=...

# Second Google account
CALENDAR_GOOGLE_2_CLIENT_ID=...
CALENDAR_GOOGLE_2_CLIENT_SECRET=...
CALENDAR_GOOGLE_2_REFRESH_TOKEN=...
CALENDAR_GOOGLE_2_CALENDAR_ID=work@group.calendar.google.com
```

The same suffix scheme works for Outlook and Zoho.

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

Run the following one-time flow from your local machine. Replace the placeholder values:

```bash
CLIENT_ID="your-client-id"
CLIENT_SECRET="your-client-secret"
SCOPE="https://www.googleapis.com/auth/calendar"

# Step 3a — Open this URL in a browser and authorize access
echo "https://accounts.google.com/o/oauth2/auth?client_id=${CLIENT_ID}&redirect_uri=urn:ietf:wg:oauth:2.0:oob&scope=${SCOPE}&response_type=code&access_type=offline&prompt=consent"

# Step 3b — After authorizing, paste the authorization code here
AUTH_CODE="paste-code-here"

# Step 3c — Exchange the code for tokens
curl -s -X POST https://oauth2.googleapis.com/token \
  -d "code=${AUTH_CODE}" \
  -d "client_id=${CLIENT_ID}" \
  -d "client_secret=${CLIENT_SECRET}" \
  -d "redirect_uri=urn:ietf:wg:oauth:2.0:oob" \
  -d "grant_type=authorization_code" | jq .
```

The response will contain `refresh_token`. Copy it.

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

1. Go to [portal.azure.com](https://portal.azure.com) → **Azure Active Directory → App registrations → New registration**.
2. Give it a name, select **Accounts in this organizational directory only** (or **Multitenant** if needed), and set the redirect URI to `http://localhost` (type: **Public client / native**).
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
# Copy the code= value from the URL

AUTH_CODE="paste-code-here"

# Step 4c — Exchange for tokens
curl -s -X POST "https://login.microsoftonline.com/${TENANT_ID}/oauth2/v2.0/token" \
  -d "code=${AUTH_CODE}" \
  -d "client_id=${CLIENT_ID}" \
  -d "client_secret=${CLIENT_SECRET}" \
  -d "redirect_uri=http://localhost" \
  -d "grant_type=authorization_code" \
  -d "scope=${SCOPE}" | jq .
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
SCOPE="ZohoCalendar.calendar.READ ZohoCalendar.event.ALL"

# Step 3a — Open this URL in a browser
echo "https://accounts.zoho.com/oauth/v2/auth?scope=${SCOPE}&client_id=${CLIENT_ID}&response_type=code&redirect_uri=http://localhost&access_type=offline"

# Step 3b — Copy the code= parameter from the redirect URL
AUTH_CODE="paste-code-here"

# Step 3c — Exchange for tokens
curl -s -X POST "https://accounts.zoho.com/oauth/v2/token" \
  -d "code=${AUTH_CODE}" \
  -d "client_id=${CLIENT_ID}" \
  -d "client_secret=${CLIENT_SECRET}" \
  -d "redirect_uri=http://localhost" \
  -d "grant_type=authorization_code" | jq .
```

Copy `refresh_token` from the response.

### Step 4 — Store the credentials

Enter the client ID, client secret and refresh token in `./setup.sh --section calendar` (stored as protected secrets), or update later with `./setup.sh secrets update NAME` (`CALENDAR_ZOHO_CLIENT_ID`, `CALENDAR_ZOHO_CLIENT_SECRET`, `CALENDAR_ZOHO_REFRESH_TOKEN`). Optional non-secret settings go to `data/.env` through the wizard:

```env
# Optional: target a specific calendar UID. If omitted, the integration uses "default",
# which may be empty if your events are stored in another Zoho calendar.
# CALENDAR_ZOHO_CALENDAR_ID=849d6badb4e04acc91860c43db0fb109

# Optional: override the API base with CALENDAR_ZOHO_API_BASE in
# data/overrides.env. Default: https://calendar.zoho.com/api/v1
# CALENDAR_ZOHO_API_BASE=https://calendar.zoho.eu/api/v1
```

> **Zoho data center regions** use different API base URLs. Use `zoho.eu` for the EU data center, `zoho.in` for India, etc.
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
| `CALENDAR_ENABLED` | `false` | Set `true` to expose calendar tools when provider credentials are present |
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
with `./setup.sh tools`. Calendar-provider requests continue to use
verified HTTPS; never use plain HTTP or disable certificate verification. The
response lists active providers and whether calendar tools are exposed.

---

## Security notes

- Refresh tokens grant long-lived access to your calendar. Store `data/.secrets/` securely and never commit it to source control.
- The assistant can only create, update, and delete events that it created itself — it cannot modify third-party events.
- Set `agent.calendar.policy.allow_read_event_details: false` (the default) to redact event titles and descriptions from free/busy results; the agent will only see time windows, not content.
