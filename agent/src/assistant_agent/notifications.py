import hashlib
import logging
from typing import Any

from .config import AppConfig, agent_name
from .database import Database
from .tools import ToolRuntime
from .validation import admin_configured, smtp_configured


LOGGER = logging.getLogger("assistant.notifications")


def notify_admin_job_failure(
    db: Database,
    config: AppConfig,
    job: dict[str, Any],
    status: str,
    reason: str,
    source: str,
) -> None:
    """Email the configured admin once for a specific job failure reason."""
    job_id = int(job["id"])
    clean_status = str(status or job.get("status") or "unknown").strip() or "unknown"
    clean_reason = str(reason or "unknown failure").strip() or "unknown failure"
    clean_source = str(source or "system").strip() or "system"
    fingerprint = _fingerprint(job_id, clean_status, clean_reason, clean_source)

    try:
        if _already_sent(db, job_id, fingerprint, clean_status, clean_reason):
            return
        if not admin_configured(config) or not smtp_configured(config):
            db.log_event(
                job_id,
                "supervisor_note",
                output_data={
                    "notification": "admin_failure_email",
                    "sent": False,
                    "fingerprint": fingerprint,
                    "status": clean_status,
                    "reason": clean_reason,
                    "source": clean_source,
                    "skip_reason": "admin email or SMTP is not configured",
                },
            )
            return

        result = ToolRuntime(db, config, job).email_send(
            to=[config.get("agent.admin.email")],
            subject=_subject(config, job_id, clean_reason),
            body=_body(db, config, job, clean_status, clean_reason, clean_source),
            new_thread=True,
        )
        sent = result.get("status") == "sent"
        db.log_event(
            job_id,
            "supervisor_note",
            output_data={
                "notification": "admin_failure_email",
                "sent": sent,
                "fingerprint": fingerprint,
                "status": clean_status,
                "reason": clean_reason,
                "source": clean_source,
                "email_status": result.get("status"),
                "email_log_id": result.get("log_id"),
                "email_reason": result.get("reason"),
            },
        )
    except Exception:
        LOGGER.exception("failed to send admin failure notification for job %s", job_id)


def _already_sent(db: Database, job_id: int, fingerprint: str, status: str, reason: str) -> bool:
    row = db.fetch_one(
        """
        SELECT id
        FROM task_logs
        WHERE job_id = %s
          AND event_type = 'supervisor_note'
          AND output_data->>'notification' = 'admin_failure_email'
          AND output_data->>'sent' = 'true'
          AND (
            output_data->>'fingerprint' = %s
            OR (
              output_data->>'status' = %s
              AND output_data->>'reason' = %s
            )
          )
        LIMIT 1
        """,
        (job_id, fingerprint, status, reason),
    )
    return row is not None


def _fingerprint(job_id: int, status: str, reason: str, source: str) -> str:
    raw = "%s|%s|%s" % (job_id, status, reason)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _subject(config: AppConfig, job_id: int, reason: str) -> str:
    preview = " ".join(reason.split())
    if len(preview) > 90:
        preview = "%s..." % preview[:87]
    return "%s job #%s needs attention: %s" % (agent_name(config), job_id, preview)


def _fmt_count(value: int) -> str:
    return "{:,}".format(value)


def _body(db: Database, config: AppConfig, job: dict[str, Any], status: str, reason: str, source: str) -> str:
    latest = _latest_email(db, job)
    checkpoint = _latest_checkpoint(db, int(job["id"]))
    diag = _diagnostics(db, config, job)
    app_base_url = str(config.get("agent.app.base_url", "http://localhost:8000")).rstrip("/")
    factor = config.get_float("agent.supervisor.email_approval_increase_factor", 1.2)
    increase_pct = int(round((factor - 1.0) * 100))

    lines = [
        "%s reported a job that needs attention." % agent_name(config),
        "",
        "Job ID: %s" % job["id"],
        "Status: %s" % status,
        "Reason: %s" % reason,
        "Task summary: %s" % (job.get("task_summary") or ""),
        "Requester: %s" % (latest.get("from_address") or "unknown"),
        "Subject: %s" % (latest.get("subject") or ""),
    ]

    # Diagnostics section
    lines.extend([
        "",
        "─── Diagnostics ───────────────────────────────",
        "Stop reason: %s" % diag["explanation"],
        "",
        "Token usage:     %s / %s (%s%%)" % (
            _fmt_count(diag["usage"]["total_tokens"]),
            _fmt_count(diag["limits"]["max_tokens"]),
            diag["usage"]["token_pct"],
        ),
        "Iterations used: %s / %s (%s%%)" % (
            diag["usage"]["iterations_used"],
            diag["limits"]["max_iterations"],
            diag["usage"]["iteration_pct"],
        ),
        "API calls:       %s" % _fmt_count(diag["usage"]["api_calls"]),
    ])

    if checkpoint:
        lines.extend([
            "",
            "Checkpoint saved at iteration %s with %s tokens." % (
                checkpoint.get("iteration_count"),
                _fmt_count(int(checkpoint.get("token_count") or 0)),
            ),
        ])

    # Email-based approval instructions
    lines.extend([
        "",
        "─── Remote Approval ────────────────────────────",
        "Reply to this email to approve and continue the job.",
        "  • Your reply body will be passed to the agent as an instruction.",
        "  • Both limits will be increased by %s%%." % increase_pct,
        "  • Leave the body empty to approve with no additional instruction.",
        "",
        "Or review the job in the dashboard:",
        app_base_url,
    ])

    return "\n".join(lines)


def _latest_email(db: Database, job: dict[str, Any]) -> dict[str, Any]:
    emails = db.latest_thread_emails(job["thread_id"], limit=1)
    return emails[-1] if emails else {}


def _latest_checkpoint(db: Database, job_id: int) -> dict[str, Any]:
    row = db.fetch_one(
        """
        SELECT reason, iteration_count, token_count, created_at
        FROM agent_checkpoints
        WHERE job_id = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (job_id,),
    )
    return row or {}


def compute_review_diagnostics(db: Database, config: AppConfig, job: dict[str, Any]) -> dict[str, Any]:
    """Public interface for computing job review diagnostics."""
    return _diagnostics(db, config, job)


def _diagnostics(db: Database, config: AppConfig, job: dict[str, Any]) -> dict[str, Any]:
    """Compute human-readable diagnostics for a needs_review job."""
    job_id = int(job["id"])
    last_error = str(job.get("last_error") or "").strip()

    base_max_iterations = config.get_int("agent.limits.max_iterations_per_task", 50)
    base_max_tokens = config.get_int("agent.limits.max_tokens_per_task", 1000000)

    override = job.get("metadata") or {}
    if isinstance(override, dict):
        override = override.get("admin_review_override") or {}
    if not isinstance(override, dict):
        override = {}

    effective_max_iterations = int(override.get("max_iterations_per_task") or base_max_iterations)
    effective_max_tokens = int(override.get("max_tokens_per_task") or base_max_tokens)

    # Count actual LLM call iterations
    iter_row = db.fetch_one(
        "SELECT COUNT(*) AS count FROM task_logs WHERE job_id = %s AND event_type = 'llm_request'",
        (job_id,),
    )
    iterations_used = int((iter_row or {}).get("count") or 0)

    # Token and API call usage (inline query).
    # All JSONB field casts are guarded by a regex pattern to prevent Postgres cast
    # errors when token fields contain non-numeric or unexpected values.  This mirrors
    # the production-safe usage_number_sql / usage_total_tokens_sql pattern used in api.py.
    _num_pat = r"^-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?$"
    usage_row = db.fetch_one(
        r"""
        WITH usage_logs AS (
          SELECT tl.tokens_used
          FROM task_logs tl
          WHERE tl.job_id = %%s AND tl.tokens_used IS NOT NULL
          UNION ALL
          SELECT dre.tokens_used
          FROM deep_research_events dre
          JOIN deep_research_runs drr ON drr.id = dre.run_id
          WHERE drr.original_job_id = %%s AND dre.tokens_used IS NOT NULL
        )
        SELECT
          COUNT(*) AS api_call_count,
          COALESCE(SUM(
            CASE
              WHEN tokens_used->>'total_tokens' ~ '%(pat)s'
                THEN (tokens_used->>'total_tokens')::double precision
              WHEN (tokens_used->>'prompt_tokens'    ~ '%(pat)s'
                 OR tokens_used->>'completion_tokens' ~ '%(pat)s')
                THEN
                  CASE WHEN tokens_used->>'prompt_tokens' ~ '%(pat)s'
                       THEN (tokens_used->>'prompt_tokens')::double precision ELSE 0 END
                + CASE WHEN tokens_used->>'completion_tokens' ~ '%(pat)s'
                       THEN (tokens_used->>'completion_tokens')::double precision ELSE 0 END
              ELSE
                  CASE WHEN tokens_used->>'input_tokens' ~ '%(pat)s'
                       THEN (tokens_used->>'input_tokens')::double precision ELSE 0 END
                + CASE WHEN tokens_used->>'output_tokens' ~ '%(pat)s'
                       THEN (tokens_used->>'output_tokens')::double precision ELSE 0 END
            END
          ), 0)::bigint AS total_tokens
        FROM usage_logs
        """ % {"pat": _num_pat},
        (job_id, job_id),
    )
    total_tokens = int((usage_row or {}).get("total_tokens") or 0)
    api_calls = int((usage_row or {}).get("api_call_count") or 0)

    token_pct = round(100.0 * total_tokens / effective_max_tokens, 1) if effective_max_tokens else 0.0
    iteration_pct = round(100.0 * iterations_used / effective_max_iterations, 1) if effective_max_iterations else 0.0

    # Human-readable stop reason
    reason_lower = last_error.lower()
    if "token budget exceeded" in reason_lower:
        stop_reason = "token_budget"
        explanation = "Token budget exceeded — used %s of %s tokens (%s%%)" % (
            _fmt_count(total_tokens), _fmt_count(effective_max_tokens), token_pct
        )
    elif "max iterations reached" in reason_lower:
        stop_reason = "max_iterations"
        explanation = "Max iterations reached — completed %s of %s iterations (%s%%)" % (
            iterations_used, effective_max_iterations, iteration_pct
        )
    elif "no task log progress" in reason_lower:
        stop_reason = "stalled"
        explanation = "Job stalled — no activity recorded: %s" % last_error
    elif "failed three times" in reason_lower:
        stop_reason = "repeated_failure"
        explanation = "Repeated tool failure: %s" % last_error
    elif "limit increase request" in reason_lower:
        # Matches both legacy "limit increase requested: …" format and the current
        # task_agent.py format "agent limit increase request (resource): …"
        stop_reason = "agent_limit_request"
        clean = last_error
        for prefix in ("agent limit increase request", "limit increase requested"):
            idx = clean.lower().find(prefix)
            if idx != -1:
                after = clean[idx + len(prefix):].lstrip(" ():").strip()
                if after:
                    clean = after
                break
        explanation = "Agent requested limit increase: %s" % clean
    elif last_error:
        stop_reason = "other"
        explanation = last_error
    else:
        stop_reason = "unknown"
        explanation = "No specific reason recorded"

    return {
        "stop_reason": stop_reason,
        "explanation": explanation,
        "limits": {
            "max_iterations": effective_max_iterations,
            "max_tokens": effective_max_tokens,
            "base_max_iterations": base_max_iterations,
            "base_max_tokens": base_max_tokens,
        },
        "usage": {
            "iterations_used": iterations_used,
            "total_tokens": total_tokens,
            "api_calls": api_calls,
            "token_pct": token_pct,
            "iteration_pct": iteration_pct,
        },
    }
