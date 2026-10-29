"""Admin-authorized, bounded development; model judgments never enlarge the brief."""
import uuid
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from . import script_permissions as permissions
from .requests import TransactionDatabase


def current(runtime, version, db=None):
    if not runtime.job or not runtime.job.get("id"):
        raise ValueError("Development requires a task job")
    return (db or runtime.db).fetch_one(
        """SELECT * FROM sandbox_development_sessions
           WHERE job_id = %s AND script_id = %s
           AND status IN ('pending', 'active', 'finalizing') ORDER BY id DESC LIMIT 1""",
        (runtime.job["id"], version["script_id"]),
    )


def require_active(session):
    if not session or session["status"] != "active":
        raise permissions.PermissionDenied("No active development session. Request administrator approval.")
    if session["expires_at"] <= datetime.now(timezone.utc):
        raise permissions.PermissionDenied("Development session expired. Submit final approval or request a new session.")
    if session["runs"] >= session["max_runs"]:
        raise permissions.PermissionDenied("Development execution budget exhausted. Submit final approval.")


def start(runtime, version, objective, data_access, effects, hosts, minutes, max_runs, allow_host_changes=False):
    if not runtime.job or not runtime.job.get("id"):
        raise ValueError("Development requires a task job")
    permissions.check_blocks(runtime, version, hosts)
    if runtime.config.get_bool("agent.security.script_yolo", False):
        return {"status": "approved", "mode": "script_yolo", "message": "Supervision and approvals are disabled."}
    if not version["reusable"]:
        raise ValueError("Development requires pinnable executable dependencies; use final one-time approval instead.")
    if not all(isinstance(value, str) and value.strip() for value in (objective, data_access, effects)):
        raise ValueError("Describe the objective, permitted data/files and external effects explicitly (use 'none' if absent)")
    for label, value, limit in (
        ("minutes", minutes, runtime.config.get_int("agent.security.development.max_minutes", 30)),
        ("max_runs", max_runs, runtime.config.get_int("agent.security.development.max_runs", 20)),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= limit:
            raise ValueError("%s must be between 1 and %s" % (label, limit))
    if not isinstance(allow_host_changes, bool):
        raise ValueError("allow_host_changes must be a boolean")
    brief = {
        "objective": objective.strip(), "data_access": data_access.strip(), "effects": effects.strip(),
        "allowed_hosts": hosts, "allow_host_changes": allow_host_changes,
        "minutes": minutes, "max_runs": max_runs,
        "workdir": version["manifest"]["workdir"], "entry": version["entry"],
    }
    with runtime.db.connect() as conn:
        db = TransactionDatabase(conn)
        db.execute("SELECT id FROM sandbox_scripts WHERE id = %s FOR UPDATE", (version["script_id"],))
        existing = current(runtime, version, db)
        if existing and existing["status"] == "active" and existing["expires_at"] <= datetime.now(timezone.utc):
            db.execute("UPDATE sandbox_development_sessions SET status = 'closed' WHERE id = %s", (existing["id"],))
            existing = None
        if existing:
            if existing["brief"] != brief:
                raise ValueError("An open session already exists. Finalize or revoke it before changing the approved brief.")
            if existing["status"] == "active":
                require_active(existing)
                return {"status": "approved", "session_id": existing["id"], "expires_at": existing["expires_at"].isoformat()}
            if existing["status"] == "finalizing":
                raise permissions.PermissionDenied("Development is closed while final approval is pending.")
            session = existing
        else:
            session = db.fetch_one(
                """INSERT INTO sandbox_development_sessions
                   (job_id, script_id, initial_version_id, brief, max_runs) VALUES (%s, %s, %s, %s, %s) RETURNING *""",
                (runtime.job["id"], version["script_id"], version["id"], Jsonb(brief), max_runs),
            )
            permissions.audit(db, "agent", "development-request", {"session_id": session["id"], "brief": brief})
    finding = permissions.screen(runtime, version)
    if finding.get("level") == "malicious":
        raise permissions.PermissionDenied("Security screening blocked this development script.")
    raise permissions.PermissionRequired({
        "id": uuid.uuid4().hex, "kind": "development", "version_id": version["id"],
        "fingerprint": version["fingerprint"], "targets": hosts, "purpose": objective,
        "screening": finding, "reusable": False, "session_id": session["id"], "brief": brief,
    })


def review_version(runtime, version, hosts, final=False):
    """Review a complete immutable version and its exact host policy, with no tool access."""
    from .development_supervisor import SupervisorError, review

    session = current(runtime, version)
    if not session or session["status"] not in ({"active", "finalizing"} if final else {"active"}):
        raise permissions.PermissionDenied("An approved development session is required.")
    if not final:
        require_active(session)
    if not version["reusable"]:
        raise permissions.PermissionDenied("This version has unpinnable dependencies; submit it for final admin review.")
    if version["manifest"]["workdir"] != session["brief"]["workdir"]:
        raise permissions.PermissionDenied("Development workdir differs from the approved brief.")
    permissions.check_blocks(runtime, version, hosts)
    extra = set(hosts) - set(session["brief"]["allowed_hosts"])
    if extra and not session["brief"]["allow_host_changes"]:
        raise permissions.PermissionDenied("New hosts require an amended brief or final administrator approval: " + ", ".join(sorted(extra)))
    previous = runtime.db.fetch_one(
        """SELECT * FROM sandbox_development_reviews
           WHERE session_id = %s AND version_id = %s AND final = %s""",
        (session["id"], version["id"], final),
    )
    if previous and previous["hosts"] == hosts and previous["review"]["decision"] == "approve":
        return session, previous["review"]
    finding = permissions.screen(runtime, version)
    if finding.get("level") == "malicious":
        raise permissions.PermissionDenied("Security screening classified this version as malicious.")
    try:
        result = review(runtime.config, session["brief"], version, hosts, final=final)
    except SupervisorError as exc:
        permissions.audit(runtime.db, "supervisor", "development-error", {"session_id": session["id"], "error": str(exc)})
        raise permissions.PermissionDenied("Supervisor unavailable; execution paused: %s" % exc) from exc
    runtime.db.execute(
        """INSERT INTO sandbox_development_reviews(session_id, version_id, hosts, final, review)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (session_id, version_id, final) DO UPDATE
           SET hosts = EXCLUDED.hosts, review = EXCLUDED.review, created_at = now()""",
        (session["id"], version["id"], Jsonb(hosts), final, Jsonb(result)),
    )
    permissions.audit(runtime.db, "supervisor", "development-review", {
        "session_id": session["id"], "version_id": version["id"], "hosts": hosts, "final": final, **result,
    })
    return session, result


def authorize(runtime, version, hosts):
    session, result = review_version(runtime, version, hosts)
    require_active(current(runtime, version))
    if result["decision"] != "approve":
        raise permissions.PermissionDenied("Supervisor %s: %s. Revise within the brief or submit final admin review." %
                                          (result["decision"], result["comments"]))
    return {"status": "approved", "session_id": session["id"], "version_id": version["id"],
            "hosts": hosts, "supervisor": result, "expires_at": session["expires_at"].isoformat()}


def consume(runtime, version, db):
    """Called under the script/version locks, immediately before dispatch."""
    session = current(runtime, version, db)
    require_active(session)
    row = db.fetch_one(
        """SELECT * FROM sandbox_development_reviews
           WHERE session_id = %s AND version_id = %s AND final = false""", (session["id"], version["id"]),
    )
    if not row or row["review"]["decision"] != "approve":
        raise permissions.PermissionDenied("This exact version needs supervisor approval before execution.")
    permissions.check_blocks(runtime, version, row["hosts"], db=db)
    db.execute("UPDATE sandbox_development_sessions SET runs = runs + 1 WHERE id = %s", (session["id"],))
    permissions.audit(db, "runtime", "development-execute", {
        "session_id": session["id"], "version_id": version["id"], "run": session["runs"] + 1, "hosts": row["hosts"],
    })
    return session, row["hosts"]


def approve_plan(db, pending, decision):
    session = db.fetch_one("SELECT * FROM sandbox_development_sessions WHERE id = %s FOR UPDATE", (pending["session_id"],))
    if not session or session["status"] != "pending" or session["brief"] != pending["brief"]:
        raise ValueError("Development brief is no longer pending")
    if decision not in {"once", "deny"}:
        raise ValueError("Development plans accept approval for this session or denial only.")
    expires = datetime.now(timezone.utc) + timedelta(minutes=session["brief"]["minutes"])
    db.execute("UPDATE sandbox_development_sessions SET status = %s, expires_at = %s WHERE id = %s",
               ("active" if decision == "once" else "denied", expires, session["id"]))
