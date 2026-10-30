"""Scheduled scripts: approved when scheduled, re-verified before every unattended run.

A reminder with ``execution: script`` stores a binding to one exact script version and
host set. Execution requires a standing grant or a one-run grant reserved for
this one-off reminder, which the sandbox enforces at run time.
"""
import json
import logging
import uuid
from datetime import datetime, timezone

from . import script_permissions as permissions


LOGGER = logging.getLogger("assistant.scheduled_scripts")
CHECK_STATE = "scheduled-script-check:%s"
SWEEP_STATE = "scheduled-script-sweep"
_process_swept = False


def describe_recurrence(unit, interval):
    if not unit:
        return ""
    interval = int(interval or 1)
    return "every %s %s%s" % (interval, unit, "" if interval == 1 else "s")


def normalize(script):
    if not isinstance(script, dict):
        raise ValueError("script must be an object with command, optional workdir, urls and optional purpose")
    command = script.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        raise ValueError("script.command must be a non-empty argv array of strings")
    urls = script.get("urls")
    if urls is None:
        raise ValueError("script.urls is required: the complete intended HTTPS host list, or [] for offline scripts")
    workdir = script.get("workdir")
    if workdir is not None and not isinstance(workdir, str):
        raise ValueError("script.workdir must be a string")
    return {"command": command, "workdir": workdir or None, "urls": urls,
            "purpose": str(script.get("purpose") or "").strip()}


def bind(runtime, script, schedule, db):
    """Bind standing permission, or exclusively reserve a one-off execution."""
    spec = normalize(script)
    purpose = spec["purpose"] or "Scheduled run: %s" % schedule.get("title", "reminder")
    health = runtime.sandbox_control("GET", "/health")
    version = permissions.inspect(runtime, spec["command"], spec["workdir"], purpose, health["execution_identity"])
    if version is None:
        raise ValueError("Scheduled scripts must be workspace scripts or explicit interpreter invocations that can be inspected.")
    hosts = permissions.requested_hosts(spec["urls"])
    result = permissions.final_request(runtime, version, hosts, purpose, scheduled=schedule)
    if not schedule.get("recurrence") and not result.get("mode"):
        db.execute("SELECT id FROM sandbox_scripts WHERE id = %s FOR UPDATE", (version["script_id"],))
        if not permissions.standing_approval(db, runtime.config, version, hosts):
            permissions.check_blocks(runtime, version, hosts, db=db)
            if (permissions.run_status(db, version, runtime.job["id"]) != "approved"
                    or set(permissions.host_policy(db, runtime.config, version["id"],
                                                   runtime.job["id"])["allow"]) != set(hosts)):
                raise permissions.PermissionDenied("One-run approval is missing or was consumed; request approval again.")
            reservation_id = uuid.uuid4().hex
            rows = permissions.grants(db, version["id"], runtime.job["id"])
            ids = [row["id"] for row in rows if row["scope"] == "once" and row["decision"] == "allow"]
            db.execute("UPDATE sandbox_permission_grants SET reservation_id = %s WHERE id = ANY(%s)",
                       (reservation_id, ids))
            permissions.audit(db, "runtime", "reserve-scheduled-run",
                              {"version_id": version["id"], "job_id": runtime.job["id"],
                               "reservation_id": reservation_id, "grants": ids})
            result["reservation_id"] = reservation_id
    return {
        "command": spec["command"], "workdir": spec["workdir"], "hosts": hosts, "purpose": purpose,
        "version_id": version["id"], "script_id": version["script_id"], "fingerprint": version["fingerprint"],
        "approved_at": datetime.now(timezone.utc).isoformat(),
        **({"mode": result["mode"]} if result.get("mode") else {}),
        **({"reservation_id": result["reservation_id"]} if result.get("reservation_id") else {}),
    }


def reservation_approval(db, config, version, hosts, reservation_id):
    if permissions.run_status(db, version, None, reservation_id) != "approved":
        return False
    rows = permissions.grants(db, version["id"], None, reservation_id)
    if not any(row["reservation_id"] == reservation_id and row["kind"] == "run" for row in rows):
        return False
    policy = permissions.host_policy(db, config, version["id"], None, reservation_id)
    return set(policy["allow"]) == set(hosts) and not any(
        permissions.network_access.decide(policy, host) == "blocked" for host in hosts
    )


def grant_problem(db, config, binding):
    """Database-only check used by the scheduler, which has no workspace or sandbox access."""
    if config.get_bool("agent.security.script_yolo", False):
        return None
    version = db.fetch_one(
        """SELECT v.*, s.blocked FROM sandbox_script_versions v JOIN sandbox_scripts s ON s.id = v.script_id
           WHERE v.id = %s""", (binding.get("version_id"),),
    )
    if not version:
        return "the approved script version no longer exists"
    if version["blocked"]:
        return "the script is blocked"
    hosts = binding.get("hosts") or []
    if binding.get("reservation_id"):
        if not reservation_approval(db, config, version, hosts, binding["reservation_id"]):
            return "its reserved one-run approval was consumed, revoked or denied, or its hosts changed or were blocked"
    elif not permissions.standing_approval(db, config, version, hosts):
        return "its standing approval was revoked or denied, or its approved hosts changed or were blocked"
    return None


def verify(runtime, binding):
    """Full check: grants plus a fresh approval identity for code and invocation.

    Raises ToolError when the sandbox cannot be reached, so callers can retry later.
    """
    problem = grant_problem(runtime.db, runtime.config, binding)
    if problem or runtime.config.get_bool("agent.security.script_yolo", False):
        return problem
    health = runtime.sandbox_control("GET", "/health")
    try:
        version = permissions.inspect(runtime, binding["command"], binding.get("workdir"),
                                      binding.get("purpose") or "Scheduled run", health["execution_identity"])
    except (OSError, ValueError) as exc:
        return "the script can no longer be inspected (%s)" % exc
    if version is None:
        return "the command is no longer an inspectable script"
    if version["id"] != binding["version_id"]:
        return "the script, its helpers, its invocation or the approval-policy revision changed since approval"
    return None


def recorded_problem(db, reminder):
    binding = (reminder.get("metadata") or {}).get("script") or {}
    state = db.get_state(CHECK_STATE % reminder["id"]) or {}
    if state.get("problem") and state.get("version_id") == binding.get("version_id"):
        return state["problem"]
    return None


def due_problem(db, config, reminder):
    binding = (reminder.get("metadata") or {}).get("script")
    if not binding:
        return None
    return grant_problem(db, config, binding) or recorded_problem(db, reminder)


def instructions(binding):
    return "\n".join([
        "Scheduled script (approved for unattended runs, version #%s):" % binding["version_id"],
        "- Command: " + json.dumps(binding["command"]),
        "- Workdir: " + (binding.get("workdir") or "(workspace root)"),
        "- HTTPS hosts: " + (", ".join(binding.get("hosts") or []) or "none (offline)"),
        "Run exactly this with command_execute, using the same command and workdir. Do not edit the script, "
        "its helpers, the command or the workdir, and do not start a development session: any change "
        "invalidates the approval. If execution is refused, call task_failed with the reason and "
        "ask the administrator to refresh this reminder's script approval; do not request new script "
        "approvals from this scheduled run.",
    ])


def alert(db, config, subject, body):
    """Best-effort administrator alert through the configured approval channels."""
    from .approval_channels import channels, telegram_admins
    try:
        mode = channels(config)
    except ValueError:
        mode = "email"
    sent = False
    if mode in {"telegram", "both"}:
        try:
            from .telegram import TelegramClient
            client = TelegramClient(config)
            for admin in telegram_admins(config):
                client.call("sendMessage", {"chat_id": admin, "text": (subject + "\n\n" + body)[:3900]}, sending=True)
                sent = True
        except Exception as exc:
            LOGGER.warning("scheduled script Telegram alert failed (%s)", type(exc).__name__)
    if mode in {"email", "both"} or not sent:
        from .security_screening import notify_admin
        sent = notify_admin(db, config, subject, body) or sent
    return sent


def alert_body(reminder, problem, consequence):
    binding = (reminder.get("metadata") or {}).get("script") or {}
    return (
        "Reminder #%s \"%s\" runs a scheduled script that can no longer run unattended: %s.\n\n"
        "Command: %s\nApproved version: #%s\n\n%s\n\n"
        "To fix it, ask the assistant to refresh the script approval for reminder #%s "
        "(reminder_update with the script), or cancel the reminder."
    ) % (reminder["id"], reminder.get("title", ""), problem, json.dumps(binding.get("command") or []),
         binding.get("version_id"), consequence, reminder["id"])


def record(db, config, reminder, problem):
    """Store the latest full-check result; alert only when a new problem appears."""
    key = CHECK_STATE % reminder["id"]
    if not problem:
        db.execute("DELETE FROM runtime_state WHERE key = %s", (key,))
        return False
    binding = reminder["metadata"]["script"]
    previous = db.get_state(key) or {}
    db.set_state(key, {"problem": problem, "version_id": binding.get("version_id"),
                       "checked_at": datetime.now(timezone.utc).isoformat()})
    if previous.get("problem") == problem and previous.get("version_id") == binding.get("version_id"):
        return False
    alert(db, config, "Assistant: scheduled script needs attention (reminder #%s)" % reminder["id"],
          alert_body(reminder, problem, "The next run will be skipped unless the approval is refreshed."))
    return True


def sweep(db, config, force=False):
    """Re-verify every scheduled script; runs in the task agent, which can inspect workspace code."""
    from .tools import ToolError, ToolRuntime
    from .validation import sandbox_configured
    if not sandbox_configured(config):
        return 0
    interval = max(1, config.get_int("agent.reminders.script_check_interval_minutes", 360))
    if force:
        db.set_state(SWEEP_STATE, {"forced": True})
    elif not db.fetch_one(
        """INSERT INTO runtime_state(key, value) VALUES (%s, '{}'::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
           WHERE runtime_state.updated_at < now() - make_interval(mins => %s) RETURNING key""",
        (SWEEP_STATE, interval),
    ):
        return 0
    rows = db.fetch_all(
        "SELECT * FROM reminders WHERE status = 'scheduled' AND metadata ? 'script' ORDER BY run_at, id"
    )
    if not rows:
        return 0
    runtime = ToolRuntime(db, config, {})
    checked = 0
    for reminder in rows:
        try:
            problem = verify(runtime, reminder["metadata"]["script"])
        except ToolError as exc:
            LOGGER.warning("scheduled script check postponed; sandbox unavailable: %s", exc)
            break
        record(db, config, reminder, problem)
        checked += 1
    return checked


def maybe_sweep(db, config):
    """Sweep once per process start (catching upgrades) and then at the configured interval."""
    global _process_swept
    force = not _process_swept
    _process_swept = True
    try:
        return sweep(db, config, force=force)
    except Exception:
        LOGGER.exception("scheduled script check failed")
        return 0
