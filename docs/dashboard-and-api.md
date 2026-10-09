# HTTPS API

The browser dashboard/shared editor and workspace/project/note/entity routes are
removed. Use [email administration](email-administration.md) and its local recovery
CLI for operator workflows. The optional retained JSON API is intended for clients,
not a replacement browser UI.

## Setup and authentication

Run `./scripts/setup.sh`, choose the HTTPS API section, and import a hostname-valid
certificate/key or generate a localhost-only development pair. The wizard selects
the `api` Compose profile, configures file-backed bearer credentials, and mounts
the protected TLS files. Start from the repository directory:

```bash
docker compose up -d --build
```

Every request, including `/health`, requires HTTPS and
`Authorization: Bearer TOKEN`. HTTP requests are rejected with 426; missing/invalid
credentials receive 401. Keep credentials out of command history/logs. Configure
the token through your client's protected credential mechanism. Explicitly trust
a development certificate rather than disabling certificate verification.

Host publishing defaults to `127.0.0.1:8000`. Backend ports remain private. API
docs (`/docs`, `/redoc`) and OpenAPI (`/openapi.json`) remain disabled by default;
they are authenticated if enabled.

## Retained endpoints

- `GET /health`: service health.
- `GET /api/config/status`: computed runtime/tool availability, not secret values.
- `GET /api/jobs` and `GET /api/jobs/{job_id}`: job listing and details.
- `POST /api/jobs`: create a manual job with `subject` and `body`.
- `POST /api/jobs/{job_id}/cancel` or `/requeue`: operator state changes.
- `POST /api/jobs/{job_id}/instructions`: append supervisor guidance.
- `POST /api/jobs/{job_id}/review-override`: review limits/instructions/requeue.
- `GET /api/jobs/{job_id}/poll`: incremental events.
- `DELETE /api/jobs/{job_id}`: erase controlled local job records.
- `/api/memories`, `/api/contacts`, `/api/reminders`: retained management APIs.
- `GET /api/emails`: stored email listing.

Job creation example payload:

```json
{"subject": "Summarize this file", "body": "Read report.md and provide a concise summary"}
```

Review overrides accept optional `instruction`, `max_iterations_per_task`,
`max_tokens_per_task`, and `requeue`. These are explicit operator interventions.

Erasure does not cancel external Zoom meetings, retract sent emails or delete
provider/backups data. See [retention limits](email-administration.md#memory-and-deletion).
See [the upgrade guide](upgrading.md) before replacing a legacy API client.