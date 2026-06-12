# Advanced Configuration

This guide is for users who want to go beyond the Quick Start defaults — tuning models, limits, email behavior, integrations, and deployment topology.

For the basics, see the [Quick Start in the README](../README.md#-quick-start).

---

## Configuration Layers

Assistant has two configuration layers that work together:

| File | What belongs here | Git status |
|------|-------------------|------------|
| `.env` | Secrets, credentials, identity, deployment-specific overrides | Gitignored |
| `config/agent.yaml` | All operational defaults (models, limits, timeouts, feature flags) | Tracked |

Override precedence: `.env` > `config/agent.yaml` > code defaults.

Most users only need to fill in `.env`. Edit `config/agent.yaml` only when you want to change operational behavior like LLM models, iteration limits, or polling intervals. Your local changes to that file will show as modifications in Git — that's expected.

Full reference: [docs/configuration.md](configuration.md)

---

## LLM Model Selection

Override the default models in `.env`:

```env
AGENT_LLM_MODEL=anthropic/claude-sonnet-4.6
AGENT_LLM_FALLBACK_MODEL=openai/gpt-5.4
```

Or edit `config/agent.yaml` directly for more granular control:

```yaml
agent:
  llm:
    model: anthropic/claude-sonnet-4.6
    fallback_model: openai/gpt-5.4
    temperature: 0.2
    max_tokens_per_call: 4096
  memory:
    steward:
      model: openai/gpt-4.1-mini
  supervisor:
    review_model: openai/gpt-4.1-mini
  search:
    model: openai/gpt-4.1-mini
  deep_research:
    search_model: perplexity/sonar-pro
```

Different subsystems can use different (cheaper) models for cost efficiency. See [docs/openrouter.md](openrouter.md) for the full OpenRouter integration guide.

---

## Job Limits and Cost Controls

In `config/agent.yaml`:

```yaml
agent:
  limits:
    max_iterations_per_task: 50       # Steps before a job pauses for review
    max_tokens_per_task: 1000000      # Token budget per job
    max_daily_cost_usd: 10.00         # Daily spend cap (agent stops when hit)
    max_emails_per_hour: 10           # Outbound email rate limit
```

Or via `.env`:

```env
AGENT_MAX_DAILY_COST_USD=5.00
AGENT_MAX_EMAILS_PER_HOUR=5
```

---

## Email: Full Configuration

> **Important:** The agent requires its own **dedicated email address and mailbox** (e.g., `assistant@yourdomain.com`). Create a separate account for it with your email provider and point both IMAP and SMTP credentials at that account. **Do not give the agent access to your personal email.** You communicate with the agent by sending email *to* its address; responses and notifications are delivered to your `ADMIN_EMAIL`.

For email-driven operation (IMAP polling), add to `.env`:

```env
IMAP_HOST=imap.example.com
IMAP_PORT=993
IMAP_USERNAME=assistant@example.com
IMAP_PASSWORD=your-password
IMAP_FOLDER=INBOX
IMAP_ARCHIVE_FOLDER=Archive
IMAP_SENT_FOLDER=Sent

SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=assistant@example.com
SMTP_PASSWORD=your-password
SMTP_FROM=assistant@example.com
```

**Safety guardrails** (set these before enabling email):

```env
EMAIL_ACTIONABLE_SENDERS=you@example.com        # Only these addresses can create new jobs
EMAIL_ALLOWED_RECIPIENT_DOMAINS=example.com     # Agent can only email these domains
ORG_INTERNAL_EMAIL_DOMAINS=example.com          # These domains don't get disclosure footers
```

If `EMAIL_ACTIONABLE_SENDERS` is blank, no inbound email creates new jobs — the agent operates in dashboard-only mode. If `EMAIL_ALLOWED_RECIPIENT_DOMAINS` is blank, only your `ADMIN_EMAIL` can receive outbound mail.

See [docs/email-and-artifacts.md](email-and-artifacts.md) for the full email pipeline guide.

---

## Agent Identity and Organization

```env
AGENT_NAME=Ada
AGENT_EMAIL=ada@example.com

ADMIN_NAME=Your Name
ADMIN_EMAIL=you@example.com

ORG_NAME=Example Inc
ORG_SECURITY_EMAIL=security@example.com
ORG_INTERNAL_EMAIL_DOMAINS=example.com
```

`ORG_NAME` and `ORG_SECURITY_EMAIL` appear in autonomous-agent disclosure footers added to outbound emails sent to external recipients.

---

## Enabling the Browser UI

The dashboard and workspace UIs are disabled by default. To enable them for local use:

```env
AGENT_API_DASHBOARD_ENABLED=true    # Enables /admin
AGENT_API_WORKSPACE_ENABLED=true    # Enables /workspace
```

The port binds to `127.0.0.1` by default, so these are only reachable from your local machine. If you want LAN access:

```env
AGENT_API_PUBLISHED_HOST=192.168.1.50   # Your server's LAN IP
```

**Never set `AGENT_API_PUBLISHED_HOST=0.0.0.0` on a server with a public IP** without a reverse proxy with authentication in front of it. See [docs/security.md](security.md) for the full guide including nginx and Caddy examples.

---

## API Documentation

Interactive API docs (`/docs`, `/redoc`) and the OpenAPI schema (`/openapi.json`) are disabled by default. To enable locally for development:

```env
AGENT_API_DOCS_ENABLED=true
AGENT_API_OPENAPI_ENABLED=true
```

Disable them before connecting to any network you don't fully control.

---

## Sandbox (Script Execution)

The sandbox lets the agent write and run shell scripts in isolated Docker containers. It is enabled by default.

To disable it (removes the `command_execute` tool entirely):

```yaml
# config/agent.yaml
agent:
  sandbox:
    enabled: false
```

The sandbox mounts the Docker socket, which is a high-privilege operation. See [docs/security.md](security.md#docker-socket-in-the-sandbox-service) and [docs/sandbox.md](sandbox.md) for details on the isolation model.

---

## Scaling: Multiple Task-Agent Workers

To process jobs concurrently, set in `.env`:

```env
TASK_AGENT_WORKERS=3
```

See [docs/concurrent-job-processing.md](concurrent-job-processing.md) for scaling considerations.

---

## Calendar Integration

Calendar access is disabled by default. To enable:

1. Set in `.env`:

```env
CALENDAR_ENABLED=true
CALDAV_URL=https://your-caldav-provider/.well-known/caldav
CALDAV_USERNAME=you@example.com
CALDAV_PASSWORD=your-password
CALENDAR_TIMEZONE=America/New_York
```

2. Enable in `config/agent.yaml`:

```yaml
agent:
  calendar:
    enabled: true
    timezone: America/New_York
    policy:
      allow_read_event_details: false   # Set true to let the agent read event contents
```

See [docs/configuration.md](configuration.md#calendar-sync) for the full CalDAV setup.

---

## Memory and Embeddings

The Memory Steward uses a local Ollama model for embeddings. It starts automatically as part of the Docker Compose stack and pulls the embedding model on first run.

Tuning options in `config/agent.yaml`:

```yaml
agent:
  memory:
    recent_prompt_limit: 10
    steward:
      enabled: true
      max_injected_memories: 8
      min_importance: 4             # 1–10 scale; only memories above this are stored
      min_confidence: 0.55
  embeddings:
    enabled: true
    model: embeddinggemma            # The Ollama model used for semantic search
```

See [docs/memory-system.md](memory-system.md) for the full memory architecture.

---

## Agent Personality: `config/AGENT.md`

The agent's behavior, tone, hard rules, and domain-specific instructions are controlled by `config/AGENT.md`. This file is required — startup fails if it's missing or empty.

```bash
cp config/AGENT.md.example config/AGENT.md
# Edit to give your agent a name, role, and operating rules
```

The example file contains a ready-to-use prompt for an Executive Assistant persona with hard rules for outbound email, calendar, and irreversible actions. Edit it freely for your use case.

---

## Persisted Data

All runtime state lives under `./data`:

```
./data/postgres        — PostgreSQL database
./data/share           — Shared workspace (visible to agent and UI)
./data/private/        — Inbound artifacts, calendar vdir
./data/ollama          — Ollama model cache
./data/clamav          — ClamAV virus definitions
```

To back up or migrate, stop the stack (`docker compose down`) and copy `./data`. The `.env` and `config/agent.yaml` files travel with it.

---

## Further Reading

- [docs/configuration.md](configuration.md) — full `.env` and `config/agent.yaml` reference
- [docs/security.md](security.md) — threat model, hardening checklist, reverse proxy examples
- [docs/docker-setup.md](docker-setup.md) — Compose topology, networks, and volume mounts
- [docs/running-assistant.md](running-assistant.md) — local and server deployment guide
- [docs/architecture.md](architecture.md) — service topology and data flow
- [docs/tools-reference.md](tools-reference.md) — all agent tools grouped by capability
- [docs/extending-tools.md](extending-tools.md) — adding custom tools
