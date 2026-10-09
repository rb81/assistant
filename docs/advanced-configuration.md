# Advanced Configuration

This guide is for users who want to go beyond the Quick Start defaults — tuning models, limits, email behavior, integrations, and deployment topology.

For the basics, see the [Quick Start in the README](../README.md#-quick-start).

---

## Configuration Layers

Assistant has several configuration layers that work together. Start with the setup wizard (`./scripts/setup.sh`); it writes the first three for you.

| File | What belongs here | Git status |
|------|-------------------|------------|
| `.secrets/` | Credentials (`./scripts/setup.sh secrets update`) | Gitignored |
| `.env` | Non-secret settings written by the wizard, plus Compose variables such as `TASK_AGENT_WORKERS` | Gitignored |
| `overrides.env` | Optional, hand-edited expert overrides for settings the wizard does not manage; never rewritten by setup | Gitignored |
| `config/agent.yaml` | All operational defaults (models, limits, timeouts, feature flags) | Tracked |

Override precedence: `docker-compose.yml` `environment:` > `overrides.env` > `.env` > `config/agent.yaml` > code defaults.

**Every setting in `config/agent.yaml` can be overridden by an environment variable.** Use the wizard for the settings it manages (including `--section advanced`), and `overrides.env` for everything else listed in [environment overrides](env-overrides.md). You can edit `config/agent.yaml` directly, but overrides keep your customizations separate from the tracked defaults.

### Important: Prefer overrides to editing `agent.yaml`

**Git updates overwrite `config/agent.yaml` with the upstream version.** If you edit this file directly, your changes will be lost when you pull updates. Use the wizard or `overrides.env` for customizations to:

- Keep your settings safe through updates
- Separate your configuration from tracked defaults
- Make deployments portable and reproducible

If you've already edited `agent.yaml`, migrate those changes to `overrides.env` (or the wizard) before pulling future updates. Do not put credentials in `overrides.env`.

How to create and use `overrides.env`: see [Manual overrides file](configuration.md#manual-overrides-file). Full references: [docs/configuration.md](configuration.md) | [docs/env-overrides.md](env-overrides.md)

---

## LLM Model Selection

Choose models with `./scripts/setup.sh --section models`, which writes these `.env` settings:

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
    max_emails_per_hour: 10           # Outbound email rate limit
```

Or use `./scripts/setup.sh --section advanced` (these are the `.env` settings it writes):

```env
AGENT_LIMITS_MAX_TOKENS_PER_TASK=500000
AGENT_MAX_EMAILS_PER_HOUR=5
```

There is no application-enforced daily monetary spending cap. Configure spending
limits with your model provider; per-task token and iteration limits are enforced
by the agent.

---

## Email: Full Configuration

> **Important:** The agent requires its own **dedicated email address and mailbox** (e.g., `assistant@yourdomain.com`). Create a separate account for it with your email provider and point both IMAP and SMTP credentials at that account. **Do not give the agent access to your personal email.** You communicate with the agent by sending email *to* its address; responses and notifications are delivered to your `ADMIN_EMAIL`.

Configure email with `./scripts/setup.sh --section email`. Credentials are stored as protected secrets, not in `.env`. For reference, the non-secret settings it writes to `.env` are:

```env
IMAP_HOST=imap.example.com
IMAP_PORT=993
IMAP_USERNAME=assistant@example.com
IMAP_FOLDER=INBOX
IMAP_ARCHIVE_FOLDER=Archive
IMAP_SENT_FOLDER=Sent

SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=assistant@example.com
SMTP_FROM=assistant@example.com
```

The IMAP and SMTP passwords are entered in the wizard (or later via `./scripts/setup.sh secrets update IMAP_PASSWORD`).

**Safety guardrails** (set these before enabling email):

```env
EMAIL_ACTIONABLE_SENDERS=you@example.com        # Only these addresses can create new jobs
EMAIL_ALLOWED_RECIPIENT_DOMAINS=example.com     # Agent can only email these domains
ORG_INTERNAL_EMAIL_DOMAINS=example.com          # These domains don't get disclosure footers
```

If `EMAIL_ACTIONABLE_SENDERS` is blank, inbound email does not create new jobs. Jobs can still be submitted through the optional authenticated API when enabled. If `EMAIL_ALLOWED_RECIPIENT_DOMAINS` is blank, only your `ADMIN_EMAIL` can receive outbound mail.

See [docs/email-and-artifacts.md](email-and-artifacts.md) for the full email pipeline guide.

---

## Agent Identity and Organization

Normally set via the wizard's identity and email sections; these are the resulting `.env` values:

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

## Optional API Access

The browser dashboard, workspace UI and workspace-upload routes have been retired.
Use email administration or the optional authenticated HTTPS API for supported
operations. Workspace-file indexing remains active for agent search; it is not
a browser-UI setting and is separate from embedding model configuration.

The API is published only on host loopback and requires HTTPS plus a bearer
token. For remote administration, use an SSH tunnel; managed Compose does not
publish the API on a LAN interface. See [docs/security.md](security.md) for the
transport and certificate requirements.

---

## API Documentation

Interactive API docs (`/docs`, `/redoc`) and the OpenAPI schema (`/openapi.json`) are disabled by default. To enable them locally for development, use the API section of `./scripts/setup.sh` (they are wizard-managed `.env` settings, not `overrides.env` ones):

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

Calendar access is disabled by default. Enable it with `./scripts/setup.sh --section calendar`, which stores OAuth credentials as protected secrets and writes the non-secret switches to `.env`. Resulting settings:

```env
CALENDAR_ENABLED=true

# Optional: set the default timezone for event creation
AGENT_TIMEZONE=America/New_York
```

Provider credentials (`CALENDAR_GOOGLE_CLIENT_ID`, `CALENDAR_GOOGLE_CLIENT_SECRET`, `CALENDAR_GOOGLE_REFRESH_TOKEN`, and the Outlook/Zoho equivalents) are managed through the wizard or `./scripts/setup.sh secrets update`; see [docs/calendar-providers.md](calendar-providers.md) for how to obtain them. Calendar tools are activated when `CALENDAR_ENABLED=true` and at least one provider's credentials are present. No YAML editing is needed.

Optional tuning (wizard-managed `.env` settings):

```env
CALENDAR_DEFAULT_PROVIDER=google          # Which provider to use when not specified per-call
CALENDAR_DEFAULT_ALERT_MINUTES=30         # Reminder alert before each created event (0 to disable)
```

Or set them in `config/agent.yaml`:

```yaml
agent:
  calendar:
    enabled: true
    timezone: America/New_York
    default_provider: google
    default_alert_minutes: 30
    policy:
      allow_read_event_details: false   # Set true to let the agent read event titles/descriptions
```

See [docs/calendar-providers.md](calendar-providers.md) for step-by-step OAuth setup for Google Calendar, Microsoft Outlook, and Zoho Calendar.

---

## Memory and Embeddings

The Memory Steward uses a local Ollama model for embeddings. It starts automatically as part of the Docker Compose stack and pulls the embedding model on first run.

Tuning options in `config/agent.yaml`:

```yaml
agent:
  memory:
    steward:
      enabled: true
      max_injected_memories: 8
      min_importance: 4                    # 1–5 scale; only memories above this are stored
      min_confidence: 0.55
      reap_after_days: 90                  # Expire stale low-importance memories
      reap_max_importance: 2               # Max importance level to reap (≤2)
      reap_interval_hours: 24              # How often to run memory maintenance
      conflict_detection_enabled: true     # Prevent duplicate memories
      conflict_similarity_threshold: 0.85  # Similarity threshold for conflict detection
  embeddings:
    enabled: true
    model: embeddinggemma                  # The Ollama model used for semantic search
    recency_boost_max: 0.1                 # Max score boost for recent memories
    recency_boost_days: 365                # Days within which to apply recency boost
  context:
    search_days: 90                        # Default search window for context search
```

**Memory lifecycle management** prevents unbounded growth:
- Low-importance memories (≤2) not accessed in 90+ days are expired
- Active notes not updated in 60+ days are archived
- Conflict detection prevents duplicate memories through semantic similarity matching
- Recency boost (up to +0.1) gives slight priority to newer memories in search results

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
./data/share           — Shared workspace (agent and sandbox files; no browser UI)
./data/private/        — Inbound artifacts and private storage
./data/ollama          — Ollama model cache
./data/clamav          — ClamAV virus definitions
```

To back up or migrate, stop the stack (`docker compose down`) and copy `./data`. The `.env` and `config/agent.yaml` files travel with it.

---

## Further Reading

- [docs/configuration.md](configuration.md) — full `.env` and `config/agent.yaml` reference
- [docs/calendar-providers.md](calendar-providers.md) — OAuth setup for Google, Outlook, and Zoho calendar providers
- [docs/security.md](security.md) — threat model, transport requirements, and hardening checklist
- [docs/docker-setup.md](docker-setup.md) — Compose topology, networks, and volume mounts
- [docs/running-assistant.md](running-assistant.md) — local and server deployment guide
- [docs/architecture.md](architecture.md) — service topology and data flow
- [docs/tools-reference.md](tools-reference.md) — all agent tools grouped by capability
- [docs/extending-tools.md](extending-tools.md) — adding custom tools
