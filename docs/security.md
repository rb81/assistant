# Security Guide

Assistant is designed to run on a **private, trusted network** — typically your home server, a local VM, or a private VPS that is not directly reachable from the public internet. This document explains the security model, its inherent limitations, and what you must do if you choose to expose it beyond a private network.

---

## ⚠️ Critical: This System Has No Built-In Authentication

**Every API endpoint, the admin dashboard (`/admin`), and the workspace UI (`/workspace`) are completely unauthenticated.** Anyone who can reach the server's IP and port can:

- Create, cancel, and delete jobs
- Read and write files in the shared workspace
- Browse and edit memories, notes, and contacts
- Trigger sandboxed command execution
- Access all email, calendar, and configuration data

This is an intentional design trade-off for simplicity in private deployments. It means **network-level access control is your only protection**.

---

## Deployment Model

Assistant is designed to be accessed by one person (you) on a network you control. There are three typical deployment scenarios:

### ✅ Local machine (fully safe)
Run the stack on your own laptop or desktop. The default `127.0.0.1` binding means only your machine can reach the dashboard. This is the safest default.

### ✅ Private home/office network
Run on a home server or NAS. Bind to the server's LAN IP (e.g., `192.168.1.50`). Only devices on your LAN can reach the dashboard. This is safe if you trust all devices on your network.

### ⚠️ Public server (VPS, cloud VM) — requires additional protection
If your server has a public IP, you **must** put the dashboard behind a reverse proxy with authentication before it is safe to operate. See [If You Must Expose This Publicly](#if-you-must-expose-this-publicly) below.

---

## Network Binding

### Docker compose publishing (the primary control)

The `agent-api` service in `docker-compose.yml` publishes its port like this:

```yaml
ports:
  - "${AGENT_API_PUBLISHED_HOST:-127.0.0.1}:${AGENT_API_PUBLISHED_PORT:-8000}:8000"
```

The default `AGENT_API_PUBLISHED_HOST=127.0.0.1` means Docker will only accept connections on the loopback interface — the dashboard is not reachable from other machines. This is the safest default.

**To allow LAN access**, set in `.env`:

```env
AGENT_API_PUBLISHED_HOST=192.168.1.50  # your server's LAN IP
```

**Never set `AGENT_API_PUBLISHED_HOST=0.0.0.0`** on a server with a public IP unless you have a firewall rule or reverse proxy blocking external access.

### Internal bind host

Inside the container, the API binds to `0.0.0.0` so that Docker's port-forwarding can route traffic to it. **This does not expose the service externally** — what's reachable from outside is controlled entirely by the `AGENT_API_PUBLISHED_HOST` setting on the Docker host (see above), which still defaults to `127.0.0.1`. You can override the internal bind address in `config/agent.yaml` or via `AGENT_API_BIND_HOST`, but there is normally no reason to do so.

### Public bind guard

As a safety net, the API **refuses to start** if `AGENT_API_BIND_HOST` resolves to `0.0.0.0` unless you explicitly acknowledge this by setting:

```env
AGENT_API_ALLOW_PUBLIC_BIND=true
```

Or in `config/agent.yaml`:

```yaml
agent:
  api:
    allow_public_bind: true
```

This prevents a casual user from accidentally exposing all unauthenticated endpoints on every network interface. In the standard Docker Compose setup the internal bind is `0.0.0.0` (so Docker port-forwarding works), and the override is already set in the container environment. If you run the API outside Docker and forget to set `AGENT_API_BIND_HOST` to `127.0.0.1`, this guard will catch the mistake.

---

## Database Password

The `.env.example` file contains a placeholder password that **must be changed** before deploying:

**Do this before starting the stack:**

```bash
cp .env.example .env
# Generate a strong password, for example:
openssl rand -base64 32
# Set it in .env:
POSTGRES_PASSWORD=<your-generated-password>
```

The application will log a startup warning if it detects that `POSTGRES_PASSWORD` is still set to the placeholder value. It will still start — but you should treat this warning as urgent.

The database port is **not published** outside the Docker network by default, so the password primarily protects against container-to-container or volume-level compromise. Still, using the default placeholder is bad practice.

---

## Dashboard and Workspace Visibility

By default, the `/admin` dashboard and `/workspace` UI are enabled. For deployments where you want to expose only the API (e.g., behind an application that calls the API programmatically), you can disable the browser UIs:

In `.env`:

```env
AGENT_API_DASHBOARD_ENABLED=false
AGENT_API_WORKSPACE_ENABLED=false
```

When set to `false`, those routes return HTTP 404. The underlying API endpoints (e.g., `/api/jobs`, `/api/workspace/file`) remain available.

Note: disabling the UI does **not** provide authentication — it only prevents the browser interface from loading. API endpoints are still unauthenticated. True protection requires either not publishing the port or using a reverse proxy with authentication.

---

## Upload Size Limit

The `PUT /api/workspace/upload` endpoint enforces a configurable maximum upload size. The default is **500 MB**. Large uploads beyond this limit are rejected with HTTP 413.

To change the limit, set in `.env`:

```env
AGENT_API_MAX_UPLOAD_BYTES=104857600   # 100 MB
```

Or set `agent.api.max_upload_bytes` in `config/agent.yaml`.

---

## API Documentation Exposure

The FastAPI interactive docs (`/docs`, `/redoc`) and the OpenAPI schema (`/openapi.json`) are **disabled by default**. They expose a full enumeration of all API endpoints and make it trivial for anyone with network access to discover the API surface.

To enable them for local development, set in `.env`:

```env
AGENT_API_DOCS_ENABLED=true
AGENT_API_OPENAPI_ENABLED=true
```

Disable them again before connecting the server to any network you do not fully control.

---

## Configuration Status Endpoint

`GET /api/config/status` returns the configured LLM model name, LLM base URL, admin email address, SMTP status, sandbox URL, embedding URL, and other operational parameters. This endpoint requires no authentication and is intentionally open on private deployments where it is useful for verifying your setup.

If the server is reachable from a network you do not fully control, be aware that this endpoint leaks configuration details to anyone who can reach it. The same network-level controls that protect the rest of the API apply here — see [Network Binding](#network-binding) and [If You Must Expose This Publicly](#if-you-must-expose-this-publicly).

---

## Docker Socket in the Sandbox Service

The `sandbox` service in `docker-compose.yml` mounts the Docker socket:

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
```

**Why it's there:** The sandbox broker creates fresh Docker containers to isolate each command execution. Mounting the socket is the mechanism by which the broker can spin up and tear down these ephemeral run containers. It is architecturally necessary for the container isolation mode.

**The risk:** A process inside the sandbox broker container that can write to the Docker socket has full control of the Docker daemon on the host — it can create privileged containers, mount host paths, etc. The sandbox broker is hardened (read-only filesystem, all capabilities dropped, no new privileges), but this is a high-privilege mount that you should be aware of.

**Mitigations in place:**
- The sandbox runs read-only with a `tmpfs` at `/tmp`
- `security_opt: no-new-privileges:true` is set
- All Linux capabilities are dropped from the broker (`cap_drop: ALL`)
- The broker is on `sandbox_net` only — it cannot reach `app_net` directly
- Per-command run containers drop `NET_RAW` and are resource-capped

**If you want to eliminate the Docker socket exposure:** Disable sandbox command execution entirely by setting `agent.sandbox.enabled: false` in `config/agent.yaml`. This removes the `command_execute` tool from the agent but everything else continues to work.

---

## If You Must Expose This Publicly

If you need to access the dashboard from outside your private network (e.g., from your phone over the internet), the recommended approach is:

### Option 1: Reverse proxy with HTTP Basic Auth (minimum viable)

Use nginx or Caddy in front of the agent with HTTP Basic Auth:

**Nginx example:**

```nginx
server {
    listen 443 ssl;
    server_name assistant.yourdomain.com;

    # TLS config here (certbot/Let's Encrypt)

    # Password-protect everything
    auth_basic "Assistant";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

Generate the password file:
```bash
htpasswd -c /etc/nginx/.htpasswd yourusername
```

**Caddy example:**

```caddy
assistant.yourdomain.com {
    basicauth /* {
        yourusername <hashed-password>
    }
    reverse_proxy 127.0.0.1:8000
}
```

### Option 2: VPN access only

Keep `AGENT_API_PUBLISHED_HOST=127.0.0.1` (or a LAN IP) and access remotely via WireGuard or Tailscale. This is the most secure option — the dashboard is never on the public internet.

### Option 3: Keep the dashboard private; expose only selected API routes

If you have an application that calls specific API endpoints, proxy only those through nginx with auth, and keep the dashboard (`/admin`, `/workspace`) behind a separate, more restricted location block — or disable them entirely with `AGENT_API_DASHBOARD_ENABLED=false`.

### Checklist for public deployment

- [ ] Reverse proxy with HTTPS and authentication in front of the stack
- [ ] `AGENT_API_PUBLISHED_HOST=127.0.0.1` (proxy connects internally)
- [ ] `AGENT_API_DOCS_ENABLED` and `AGENT_API_OPENAPI_ENABLED` are not set to `true` (they are off by default)
- [ ] `POSTGRES_PASSWORD` is a strong, unique password (not the placeholder)
- [ ] `EMAIL_ACTIONABLE_SENDERS` is locked down to only your address
- [ ] `EMAIL_ALLOWED_RECIPIENT_DOMAINS` is locked down to your domains
- [ ] Consider `AGENT_API_DASHBOARD_ENABLED=false` and `AGENT_API_WORKSPACE_ENABLED=false` if you don't need the browser UI from the public internet
- [ ] Firewall rules block direct access to port 8000 from external IPs

---

## Responsible Disclosure

If you discover a security vulnerability in this project, please open a GitHub issue or reach out privately before disclosing publicly. This is an open-source project without a formal security response team — issues will be addressed on a best-effort basis.
