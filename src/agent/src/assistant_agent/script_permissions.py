"""Version-bound script and HTTPS-host permissions, shared by every admin surface."""
import base64
import hashlib
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from . import network_access
from .requests import TransactionDatabase
from .script_analysis import ScriptAnalyzer
from .security_screening import MALICIOUS, SecurityScreener, notify_admin


DECISIONS = {
    "once": "Approve once",
    "always": "Approve always for this script",
    "deny": "Deny",
}
KEYWORDS = {
    "approve once": "once",
    "approve always": "always",
    "approve always for this script": "always",
    "deny": "deny",
}
UNSTABLE_SIGNALS = {
    "code_not_inspectable", "remote_code_execution", "obfuscated_code_execution",
    "binary_executable", "unparseable_code",
}


class PermissionRevocationIncomplete(ValueError):
    pass


class PermissionRequired(Exception):
    def __init__(self, request):
        self.request = request
        super().__init__("Administrator permission required: " + request["kind"])


class PermissionDenied(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def requested_hosts(urls):
    if not isinstance(urls, list) or not 0 <= len(urls) <= network_access.MAX_REQUEST_HOSTS:
        raise ValueError("Provide up to 10 exact hostnames or HTTPS URLs (empty for offline)")
    hosts = []
    for value in urls:
        if not isinstance(value, str):
            raise ValueError("Hosts and URLs must be strings")
        parsed = urlsplit(value if "://" in value else "https://" + value)
        if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or parsed.password:
            raise ValueError("Only HTTPS port 443 is supported; do not include URL credentials")
        hosts.append(network_access.normalize_host(value))
    return sorted(set(hosts))


def audit(db, actor, action, details):
    db.execute(
        "INSERT INTO sandbox_permission_history(actor, action, details) VALUES (%s, %s, %s)",
        (actor, action, Jsonb(details)),
    )


def inspect(runtime, command, workdir=None, purpose="Script execution", environment=""):
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise ValueError("command must be a non-empty argv array of strings")
    directory = runtime.resolve_path(workdir) if workdir else runtime.shared_root
    if not directory.is_dir():
        raise ValueError("workdir must be an existing workspace directory")
    analyzer = ScriptAnalyzer(
        runtime.shared_root, directory, runtime.config.get_list("agent.security.scripts.allowed_domains"),
        runtime.config.get_int("agent.security.scripts.max_files", 20),
    )
    manifest = analyzer.analyze(command)
    if manifest is None:
        return None
    state = manifest.state()
    state["code_arguments"] = {}
    if manifest.entry:
        for index, arg in enumerate(command):
            candidate = Path(os.path.abspath(directory / arg))
            if candidate == runtime.shared_root / manifest.entry:
                state["code_arguments"][str(index)] = manifest.entry
                break
    relative_workdir = str(directory.relative_to(runtime.shared_root))
    fingerprint = digest({
        **manifest.fingerprint_material(), "workdir": relative_workdir, "environment": environment,
        "security_policy": runtime.config.get("agent.security", {}),
    })
    entry = manifest.entry or "inline:" + digest({"command": command, "workdir": relative_workdir})
    # Unknown dependency graphs never earn a reusable grant.
    reusable = (
        not (manifest.hard_signals & UNSTABLE_SIGNALS)
        and not manifest.installs
        and manifest.language in {"python", "shell", "javascript"}
        and not manifest.risky_calls.get("code_execution")
        and not manifest.risky_calls.get("process")
        and not (manifest.language == "shell" and manifest.commands - {
            "echo", "printf", "cat", "head", "tail", "wc", "sort", "uniq", "cut", "tr",
            "grep", "sed", "awk", "jq", "curl", "wget", "mkdir", "cp", "mv", "rm",
            "touch", "date", "test", "[", "true", "false", "sleep", "exit", "set", "read",
        })
        and not (manifest.language == "javascript" and manifest.imports["third_party"])
        and not any("dynamic import" in note for note in manifest.notes)
        and Path(command[0]).name not in {"env", "timeout", "nice", "nohup", "time", "stdbuf", "xargs", "exec", "command"}
    )
    with runtime.db.connect() as conn:
        db = TransactionDatabase(conn)
        script = db.fetch_one(
            """INSERT INTO sandbox_scripts(entry) VALUES (%s)
               ON CONFLICT (entry) DO UPDATE SET entry = EXCLUDED.entry RETURNING *""", (entry,),
        )
        version = db.fetch_one(
            """INSERT INTO sandbox_script_versions(script_id, fingerprint, manifest, purpose, reusable)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (fingerprint) DO UPDATE SET fingerprint = EXCLUDED.fingerprint RETURNING *""",
            (script["id"], fingerprint, Jsonb({**state, "workdir": relative_workdir, "environment": environment}),
             str(purpose).strip()[:1000], reusable),
        )
    return {**version, "entry": entry, "blocked": script["blocked"]}


def grants(db, version_id, job_id, reservation_id=None):
    if job_id is not None and reservation_id is None:
        job = db.fetch_one("SELECT metadata FROM jobs WHERE id = %s", (job_id,))
        reservation_id = ((job or {}).get("metadata") or {}).get("scheduled_script", {}).get("reservation_id")
    return db.fetch_all(
        """SELECT * FROM sandbox_permission_grants WHERE consumed_at IS NULL
           AND (version_id = %s OR scope = 'global')
           AND (scope <> 'once' OR (reservation_id IS NULL AND job_id = %s)
                OR reservation_id = %s)
           AND (reservation_id IS NULL OR reservation_id = %s) ORDER BY id""",
        (version_id, job_id, reservation_id, reservation_id),
    )


def run_status(db, version, job_id, reservation_id=None):
    script = db.fetch_one("SELECT blocked FROM sandbox_scripts WHERE id = %s", (version["script_id"],))
    if not script or script["blocked"]:
        return "blocked"
    rows = [row for row in grants(db, version["id"], job_id, reservation_id) if row["kind"] == "run"]
    if any(row["decision"] == "block" for row in rows):
        return "denied"
    return "approved" if any(row["decision"] == "allow" for row in rows) else "not_approved"


def host_policy(db, config, version_id, job_id, reservation_id=None):
    legacy = network_access.policy(db, config, job_id)
    rows = [row for row in grants(db, version_id, job_id, reservation_id) if row["kind"] == "host"]
    return {
        "allow": sorted({row["target"] for row in rows if row["decision"] == "allow" and row["version_id"] == version_id}),
        "block": sorted(set(legacy["block"] + [row["target"] for row in rows if row["decision"] == "block"])),
    }


def standing_approval(db, config, version, hosts):
    """Whether this exact version may run in any job, unattended, with exactly these hosts."""
    if run_status(db, version, None) != "approved":
        return False
    policy = host_policy(db, config, version["id"], None)
    if any(network_access.decide(policy, host) == "blocked" for host in hosts):
        return False
    return set(policy["allow"]) == set(hosts)


def check_blocks(runtime, version, hosts, db=None):
    if not runtime.job or not runtime.job.get("id"):
        raise ValueError("Script permissions require a task job")
    db = db or runtime.db
    status = run_status(db, version, runtime.job["id"])
    if status in {"blocked", "denied"}:
        raise PermissionDenied("This script is blocked or denied. Do not retry or work around the decision.")
    policy = host_policy(db, runtime.config, version["id"], runtime.job["id"])
    blocked = [host for host in hosts if network_access.decide(policy, host) == "blocked"]
    if blocked:
        raise PermissionDenied("Administrator blocked: %s. Do not request or retry these hosts." % ", ".join(blocked))


def final_request(runtime, version, hosts, purpose, scheduled=None):
    """Request combined approval, using the reminder's recurrence to choose its scope."""
    from . import development_sessions
    from .development_supervisor import SupervisorError, review

    check_blocks(runtime, version, hosts)
    if runtime.config.get_bool("agent.security.script_yolo", False):
        return {"status": "approved", "mode": "script_yolo", "version_id": version["id"]}
    approved_here = (runtime.job.get("metadata") or {}).get("approved_script") or {}
    if approved_here.get("version_id") == version["id"] and sorted(approved_here.get("hosts") or []) != hosts:
        # Models re-typing an approved call can drop or add hosts; never turn that into a new admin request.
        raise PermissionDenied(
            "Version #%s was just approved in this task with exactly these hosts: %s. Repeat the call with exactly "
            "that host list. If the script truly needs different hosts, change the script (which creates a new "
            "version) and request approval again." % (version["id"], ", ".join(approved_here.get("hosts") or []) or "none")
        )
    recurring = scheduled and bool(scheduled.get("recurrence"))
    if recurring and not version["reusable"]:
        raise PermissionDenied(
            "This script cannot be scheduled recurrently: its executable dependencies cannot be pinned, so it can only "
            "receive one-time approval. Restructure it (pinned local helpers; no package installs, subprocesses "
            "or dynamic code execution) and schedule it again."
        )
    analysis = version["manifest"]["manifest"]
    if not hosts and analysis["risky_calls"].get("network") and analysis["network"]["hosts"]:
        raise PermissionDenied(
            "This script uses networking but the approval host list is empty. "
            "Call script_approval_request with the complete intended HTTPS hosts in urls; "
            "static URL mentions are evidence, not automatically authorized destinations."
        )
    session = development_sessions.current(runtime, version)
    status = run_status(runtime.db, version, runtime.job["id"])
    policy = host_policy(runtime.db, runtime.config, version["id"], runtime.job["id"])
    approved = (standing_approval(runtime.db, runtime.config, version, hosts) if recurring else
                status == "approved" and set(hosts) == set(policy["allow"]))
    if not session and approved:
        return {"status": "approved", "version_id": version["id"], "fingerprint": version["fingerprint"]}
    if session:
        # Closing authorization before model review prevents testing while final approval is pending.
        with runtime.db.connect() as conn:
            db = TransactionDatabase(conn)
            db.execute("SELECT id FROM sandbox_scripts WHERE id = %s FOR UPDATE", (version["script_id"],))
            db.execute(
                "UPDATE sandbox_development_sessions SET status = 'finalizing' WHERE id = %s AND status = 'active'",
                (session["id"],),
            )
    finding = screen(runtime, version)
    if finding.get("level") == MALICIOUS:
        raise PermissionDenied("Security screening classified this version as malicious; execution is blocked.")
    if session:
        try:
            _, supervisor = development_sessions.review_version(runtime, version, hosts, final=True)
        except PermissionDenied as exc:
            supervisor = {"decision": "escalate", "comments": str(exc)}
    else:
        try:
            context = getattr(runtime, "sandbox_review_context", None)
            supervisor = review(runtime.config, None, {**version, "purpose": purpose}, hosts, final=True,
                                **({"sandbox_context": context} if context is not None else {}))
        except SupervisorError as exc:
            supervisor = {"decision": "unavailable", "comments": str(exc)}
        audit(runtime.db, "supervisor", "final-review", {
            "version_id": version["id"], "job_id": runtime.job["id"], "hosts": hosts, **supervisor,
        })
    pending = {
        "id": uuid.uuid4().hex, "kind": "final", "version_id": version["id"],
        "fingerprint": version["fingerprint"], "targets": hosts, "purpose": purpose,
        "screening": finding, "reusable": version["reusable"], "supervisor": supervisor,
        "session_id": session["id"] if session else None,
    }
    if scheduled:
        pending["scheduled"] = scheduled
    binding = (runtime.job.get("metadata") or {}).get("scheduled_script")
    if binding and not scheduled:
        pending["scheduled_drift"] = {
            "reminder_id": binding.get("reminder_id"), "version_id": binding.get("version_id"),
            "hosts": binding.get("hosts") or [],
        }
    raise PermissionRequired(pending)


def screen(runtime, version):
    screener = SecurityScreener(runtime.config)
    profile = digest({
        "decision_model": runtime.config.get("agent.security.decision_model", {}),
        "scripts": runtime.config.get("agent.security.scripts", {}),
    })
    if not screener.enabled("scripts"):
        return {"level": "not_configured", "summary": "Decision-model screening is not configured.", "profile": profile}
    with runtime.db.connect() as conn:
        db = TransactionDatabase(conn)
        current = db.fetch_one("SELECT screening FROM sandbox_script_versions WHERE id = %s FOR UPDATE",
                               (version["id"],))
        if not current:
            raise ValueError("Script version is no longer available")
        previous = current.get("screening") or {}
        if previous.get("profile") == profile and not previous.get("error"):
            return previous
        state = version["manifest"]
        signals = tuple(state["manifest"]["hard_signals"])
        result = screener.screen("scripts", state, signals)
        finding = {
            "level": result.level, "summary": result.summary(), "error": result.error, "profile": profile,
        }
        db.execute("UPDATE sandbox_script_versions SET screening = %s WHERE id = %s",
                   (Jsonb(finding), version["id"]))
        audit(db, "screening", "screen", {"version_id": version["id"], **finding})
    if finding["level"] == MALICIOUS:
        notify_admin(runtime.db, runtime.config, "Assistant blocked script version #%s" % version["id"],
                     "Script version %s (%s) was blocked.\n%s" %
                     (version["id"], version["fingerprint"], finding["summary"]))
    return finding


def request(runtime, version, kind, targets=None, purpose=""):
    if not runtime.job or not runtime.job.get("id"):
        raise ValueError("Script permissions require a task job")
    if kind not in {"run", "host"}:
        raise ValueError("Unknown script permission kind")
    return final_request(runtime, version, targets or [], purpose or version["purpose"])


def approval_payload(job):
    pending = (job.get("metadata") or {}).get("permission_request")
    if not pending:
        return None
    return {"kind": "permission", "job_id": job["id"], "request_id": pending["id"],
            "permission_type": pending["kind"], "reusable": pending["reusable"],
            "version_id": pending["version_id"],
            **({"scheduled": True, "recurring": bool(pending["scheduled"].get("recurrence"))}
               if pending.get("scheduled") else {})}


def summary(db, job):
    pending = job["metadata"]["permission_request"]
    version = db.fetch_one(
        """SELECT v.*, s.entry FROM sandbox_script_versions v JOIN sandbox_scripts s ON s.id = v.script_id
           WHERE v.id = %s""", (pending["version_id"],),
    )
    if not version:
        raise ValueError("Script version is no longer available")
    entry = version["entry"]
    label = "Inline command" if entry.startswith("inline:") else entry
    scheduled = pending.get("scheduled")
    lines = [
        "Scheduled script approval" if scheduled else
        {"host": "Website access requested", "development": "Development session requested",
         "final": "Script and website access approval"}.get(pending["kind"], "Script run requested"),
        "",
        "Script: " + label[:180],
        "Job #%s | Version #%s" % (job["id"], version["id"]),
        "",
        "Why: " + " ".join(pending["purpose"].split())[:360],
    ]
    if scheduled:
        lines += ["Schedule: %s; first run %s%s." % (
            " ".join(str(scheduled.get("title") or "").split())[:120], scheduled.get("run_at") or "unknown",
            ", " + scheduled["recurrence"] if scheduled.get("recurrence") else " (one time)")]
    drift = pending.get("scheduled_drift")
    if drift:
        lines += ["", "Warning: requested during a scheduled run of reminder #%s, whose approved script is "
                  "version #%s with hosts: %s. This request differs from what was scheduled." % (
                      drift.get("reminder_id"), drift.get("version_id"), ", ".join(drift.get("hosts") or []) or "none")]
    if pending["kind"] == "development":
        brief = pending["brief"]
        lines += ["", "Data/files: " + brief["data_access"], "External effects: " + brief["effects"],
                  "Limits: %s minutes; %s executions." % (brief["minutes"], brief["max_runs"]),
                  "Related host changes delegated to supervisor: " + ("yes" if brief["allow_host_changes"] else "no"),
                  "Live testing can have irreversible effects before final approval."]
    if pending.get("supervisor"):
        result = pending["supervisor"]
        conclusion = {
            "approve": "Recommends approval", "reject": "Recommends denial",
            "escalate": "Needs administrator judgment", "unavailable": "Review unavailable",
            "not_reviewed": "Not reviewed (older request; request a fresh assessment)",
        }.get(result["decision"], result["decision"])
        lines += ["", "Supervisor: " + conclusion, result["comments"]]
        if result["decision"] == "unavailable":
            lines += ["No supervisor assessment was completed. Review the script manually before approving."]
    lines += ["", "HTTPS hosts (all paths; subdomains excluded):",
              *(["- " + host for host in pending["targets"]] or ["None (offline)."])]
    if pending["kind"] != "development":
        finding = pending["screening"]
        screening = (
            "Unavailable; manual review required." if finding.get("error") else {
                "not_malicious": "No concerns flagged.",
                "possibly_malicious": "Potential risks flagged; review the supervisor's comments and script.",
                "malicious": "Malicious code detected; execution blocked.",
                "not_configured": "Not configured.",
            }.get(finding.get("level"), " ".join(finding.get("summary", "").split())[:240])
        )
        if screening:
            lines += ["", "Security screening: " + screening]
        if scheduled and scheduled.get("recurrence"):
            lines += ["", "Always allow this version: required so scheduled runs can execute unattended with these "
                      "hosts, until revoked. Changes need new approval.",
                      "One-run approval is not offered for scheduled scripts."]
        elif scheduled:
            lines += ["", "Approve this scheduled run: one execution reserved for this reminder, with these hosts. "
                      "Other runs cannot consume it. Changes need new approval."]
        else:
            lines += ["", "Approve one run: one execution with the listed hosts.",
                      "Always allow this version: repeat runs with these hosts, until revoked. Changes need new approval."]
            if not version["reusable"]:
                lines[-1] = "One-time approval only: executable dependencies cannot be pinned."
    lines += ["\nDetails:", "SHA-256: " + version["fingerprint"],
              "Purpose: " + pending["purpose"],
              "Screening: " + json.dumps(pending["screening"], sort_keys=True),
              "HTTPS hosts: " + ", ".join(pending["targets"]),
              json.dumps(version["manifest"], indent=2, sort_keys=True)]
    return "\n".join(lines)


def send_approval(db, config, job, suffix=""):
    from .email_approvals import EmailApprovals
    payload = approval_payload(job)
    if not payload:
        raise ValueError("No script permission request is pending")
    version = db.fetch_one("SELECT manifest, fingerprint FROM sandbox_script_versions WHERE id = %s", (payload["version_id"],))
    if not version:
        raise ValueError("Script version is no longer available")
    pending = job["metadata"]["permission_request"]
    return EmailApprovals(db, config).request(
        "permission:%s:%s%s" % (job["id"], payload["request_id"], suffix),
        payload, summary(db, job), ttl_minutes=60,
        email_subject="Assistant approval: " + ("development session" if payload["permission_type"] == "development" else "script and hosts"),
        details_attachment=("script-version-%s.txt" % payload["version_id"],
                            (source_text(version) + "\n\n" + json.dumps({**version["manifest"], "fingerprint": version["fingerprint"],
                                        "requested_hosts": pending["targets"], "purpose": pending["purpose"],
                                        "screening": pending["screening"], "supervisor": pending.get("supervisor")},
                                       indent=2, sort_keys=True)).encode()),
    )


def parse_decision(text):
    lines = str(text or "").strip().splitlines()
    decision_line = " ".join(lines[0].lower().strip(".!: ").split()) if lines else ""
    decision = KEYWORDS.get(decision_line)
    if decision is None and " - " in decision_line:
        decision = KEYWORDS.get(decision_line.partition(" - ")[0])
    if not decision:
        raise ValueError("First line must be APPROVE ONCE, APPROVE ALWAYS, or DENY.")
    return decision, "\n".join(lines[1:]).strip()


def apply_decision(db, payload, decision, feedback="", actor="admin"):
    if decision not in DECISIONS:
        raise ValueError("Unknown permission decision")
    with db.connect() as conn:
        tx = TransactionDatabase(conn)
        script = tx.fetch_one(
            """SELECT s.id FROM sandbox_scripts s JOIN sandbox_script_versions v ON v.script_id = s.id
               WHERE v.id = %s FOR UPDATE OF s""", (payload["version_id"],),
        )
        if not script:
            raise ValueError("This script version is unavailable.")
        job = tx.fetch_one(
            """SELECT * FROM jobs WHERE id = %s AND status = 'needs_review'
               AND metadata->'permission_request'->>'id' = %s FOR UPDATE""",
            (payload["job_id"], payload["request_id"]),
        )
        if not job:
            raise ValueError("This request was already handled or the task changed.")
        pending = job["metadata"]["permission_request"]
        version = tx.fetch_one(
            """SELECT v.*, s.blocked FROM sandbox_script_versions v JOIN sandbox_scripts s ON s.id = v.script_id
               WHERE v.id = %s AND s.id = %s FOR UPDATE OF v""", (pending["version_id"], script["id"]),
        )
        if not version or version["blocked"] or version["fingerprint"] != pending["fingerprint"]:
            raise ValueError("This script version is unavailable or blocked.")
        if pending["kind"] == "final" and pending.get("session_id"):
            session = tx.fetch_one("SELECT status FROM sandbox_development_sessions WHERE id = %s FOR UPDATE",
                                   (pending["session_id"],))
            if not session or session["status"] not in {"pending", "finalizing"}:
                raise ValueError("The development session was closed or revoked. This approval is no longer valid.")
        if pending["kind"] == "development":
            from .development_sessions import approve_plan
            approve_plan(tx, pending, decision)
        if decision == "always" and not version["reusable"]:
            raise ValueError("This script needs one-time approval because its executable dependencies cannot be pinned.")
        if decision == "once" and (pending.get("scheduled") or {}).get("recurrence"):
            raise ValueError("Scheduled scripts need Always allow or Deny; a one-run approval would not cover the schedule.")
        if decision == "always" and pending.get("scheduled") and not pending["scheduled"].get("recurrence"):
            raise ValueError("One-off schedules need Approve once or Deny, not standing approval.")
        scope = {"once": "once", "always": "script", "deny": "once"}[decision]
        if pending["kind"] == "final":
            tx.execute(
                """DELETE FROM sandbox_permission_grants WHERE version_id = %s AND decision = 'allow'
                   AND reservation_id IS NULL""", (version["id"],),
            )
        targets = (
            [("run", ""), *[("host", host) for host in pending["targets"]]]
            if pending["kind"] == "final" else
            [("host", host) for host in pending["targets"]] if pending["kind"] == "host" else
            [] if pending["kind"] == "development" and decision != "deny" else [("run", "")]
        )
        for kind, target in targets:
            tx.execute(
                """INSERT INTO sandbox_permission_grants
                   (version_id, job_id, kind, target, scope, decision, created_by, note)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (version["id"], job["id"] if scope == "once" else None,
                 kind, target, scope, "block" if decision == "deny" else "allow", actor, feedback),
            )
        if pending["kind"] == "final" and pending.get("session_id"):
            tx.execute("UPDATE sandbox_development_sessions SET status = 'closed' WHERE id = %s", (pending["session_id"],))
        replay = pending.get("replay") if decision != "deny" and pending.get("scheduled") else None
        instruction = (
            "Administrator %s for script version #%s (%s). %s" %
            (DECISIONS[decision], version["id"], pending["fingerprint"],
             "Do not repeat or work around this denied request." if decision == "deny" else
             "Continue within the approved development brief; request supervisor review before running each version."
             if pending["kind"] == "development" else
             "The system is saving the schedule from your original %s call; its result follows. Do not repeat "
             "that call, and do not run the script now unless asked." % replay["tool"] if replay else
             "Retry the identical %s call to save the schedule; do not run the script now unless asked."
             % pending["scheduled"].get("tool", "reminder_create") if pending.get("scheduled") else
             "Execute this unchanged version with its approved hosts.")
        )
        if feedback:
            instruction += "\nAdministrator feedback: " + feedback
        tx.execute("INSERT INTO supervisor_instructions(job_id, instruction, created_by) VALUES (%s, %s, %s)",
                   (job["id"], instruction, actor))
        resume = {}
        if pending["kind"] == "final" and decision != "deny":
            resume["approved_script"] = {"version_id": version["id"], "hosts": sorted(pending["targets"])}
        if replay:
            resume["replay_tool_call"] = replay
        tx.execute(
            """UPDATE jobs SET status = 'queued', run_at = now(), has_new_context = true,
               metadata = (metadata - 'permission_request') || %s, locked_at = NULL, locked_by = NULL,
               last_error = NULL, updated_at = now() WHERE id = %s""", (Jsonb(resume), job["id"]),
        )
        audit(tx, actor, decision, {**{key: value for key, value in pending.items() if key != "replay"},
                                    "job_id": job["id"], "feedback": feedback})
    return "%s. Job #%s resumed; execution limits unchanged." % (DECISIONS[decision], payload["job_id"])


def source_text(version):
    return "\n\n".join("=== %s ===\n%s" % (item["path"], item["source"]) for item in version["manifest"]["files"])


def execution(runtime, version):
    """Atomically consume all temporary grants before a single non-retried dispatch."""
    bundle = {}
    for path, expected in {
        item["path"]: item["sha256"] for item in version["manifest"]["files"]
    }.items():
        if path.startswith("<"):
            continue
        source = runtime.resolve_path(path).read_bytes()
        if hashlib.sha256(source).hexdigest() != expected:
            raise PermissionDenied("Script version changed; request approval again before execution.")
        bundle[path] = base64.b64encode(source).decode("ascii")
    with runtime.db.connect() as conn:
        tx = TransactionDatabase(conn)
        tx.execute("SELECT id FROM sandbox_scripts WHERE id = %s FOR UPDATE", (version["script_id"],))
        tx.execute("SELECT id FROM sandbox_script_versions WHERE id = %s FOR UPDATE", (version["id"],))
        check_blocks(runtime, version, [], db=tx)
        current = host_policy(tx, runtime.config, version["id"], runtime.job["id"])
        if runtime.config.get_bool("agent.security.script_yolo", False):
            current["allow"] = ["*"]
            audit(tx, "runtime", "script-yolo-execute", {"version_id": version["id"], "job_id": runtime.job["id"]})
            return bundle, current
        from . import development_sessions
        session = development_sessions.current(runtime, version, tx)
        if session:
            session, hosts = development_sessions.consume(runtime, version, tx)
            current["allow"] = hosts
            current["expires_at"] = session["expires_at"].isoformat()
            return bundle, current
        if run_status(tx, version, runtime.job["id"]) != "approved":
            raise PermissionDenied("Run approval is missing, consumed, denied or revoked. Request approval first.")
        rows = grants(tx, version["id"], runtime.job["id"])
        run_once = [row["id"] for row in rows if row["kind"] == "run" and row["scope"] == "once" and row["decision"] == "allow"]
        permanent_run = any(row["kind"] == "run" and row["scope"] == "script" and row["decision"] == "allow" for row in rows)
        consume = [row["id"] for row in rows if row["kind"] == "host" and row["scope"] == "once" and row["decision"] == "allow"]
        if not permanent_run and run_once:
            consume.append(run_once[0])
        if consume:
            tx.execute("UPDATE sandbox_permission_grants SET consumed_at = now() WHERE id = ANY(%s)", (consume,))
        audit(tx, "runtime", "execute", {"version_id": version["id"], "job_id": runtime.job["id"], "grants": consume})
    return bundle, current


def management(db, config, args, confirmed=False, actor="admin", *, page=None, query=""):
    action = args[0] if args else "scripts"
    if action == "scripts":
        source = """FROM sandbox_scripts s
                    LEFT JOIN sandbox_script_versions v ON v.script_id = s.id"""
        parameters = ()
        if query:
            source += " WHERE s.entry ILIKE %s"
            parameters = ("%" + query + "%",)
        rows = db.fetch_all(
            """SELECT s.id, s.entry, s.blocked, v.id AS version_id, v.fingerprint, v.reusable,
               EXISTS (SELECT 1 FROM sandbox_permission_grants g WHERE g.version_id = v.id
                   AND g.kind = 'run' AND g.decision = 'allow' AND g.scope = 'script') AS approved_always,
               EXISTS (SELECT 1 FROM sandbox_permission_grants g WHERE g.version_id = v.id
                   AND g.kind = 'run' AND g.decision = 'allow' AND g.scope = 'once'
                   AND g.consumed_at IS NULL) AS has_once_grant,
               v.purpose, v.created_at """ + source +
            " ORDER BY s.id DESC, v.id DESC LIMIT %s OFFSET %s",
            (*parameters, 100 if page is None else 8, 0 if page is None else page * 8),
        )
        if page is None:
            return rows
        total = db.fetch_one("SELECT count(*) AS total " + source, parameters)["total"]
        return {"rows": rows, "total": total}
    if action == "hosts":
        return {
            "defaults": [],
            "legacy_rules": db.fetch_all("SELECT * FROM sandbox_network_rules ORDER BY id DESC LIMIT 100"),
            "grants": db.fetch_all("SELECT * FROM sandbox_permission_grants WHERE kind = 'host' AND consumed_at IS NULL ORDER BY id DESC LIMIT 100"),
        }
    if action == "history":
        return db.fetch_all("SELECT * FROM sandbox_permission_history ORDER BY id DESC LIMIT 100")
    if action == "show" and len(args) == 2:
        version = db.fetch_one("SELECT * FROM sandbox_script_versions WHERE id = %s", (int(args[1]),))
        if not version:
            raise ValueError("Script version not found")
        return {"version": version, "grants": db.fetch_all(
            "SELECT * FROM sandbox_permission_grants WHERE version_id = %s ORDER BY id", (version["id"],))}
    if not confirmed:
        raise ValueError("Permission changes require confirmation (--yes in the local CLI).")
    with db.connect() as conn:
        tx = TransactionDatabase(conn)
        if action in {"block-script", "unblock-script", "revoke-script"} and len(args) == 2:
            script_id = int(args[1])
            script = tx.fetch_one("SELECT id FROM sandbox_scripts WHERE id = %s FOR UPDATE", (script_id,))
            if not script:
                raise ValueError("Script not found")
            if action == "revoke-script":
                tx.execute(
                    """DELETE FROM sandbox_permission_grants WHERE version_id IN
                       (SELECT id FROM sandbox_script_versions WHERE script_id = %s)""", (script_id,))
            else:
                tx.execute("UPDATE sandbox_scripts SET blocked = %s WHERE id = %s", (action == "block-script", script_id))
            if action != "unblock-script":
                tx.execute(
                    """UPDATE sandbox_development_sessions SET status = 'revoked'
                       WHERE script_id = %s AND status IN ('pending', 'active', 'finalizing')""", (script_id,))
                affected = tx.fetch_all(
                    """SELECT id FROM jobs WHERE status = 'needs_review'
                       AND metadata->'permission_request'->>'version_id' IN
                       (SELECT id::text FROM sandbox_script_versions WHERE script_id = %s) FOR UPDATE""", (script_id,))
                for job in affected:
                    tx.execute(
                        """UPDATE jobs SET metadata = metadata - 'permission_request', status = 'queued',
                           run_at = now(), has_new_context = true, locked_at = NULL, locked_by = NULL
                           WHERE id = %s""", (job["id"],))
                    tx.execute(
                        "INSERT INTO supervisor_instructions(job_id, instruction, created_by) VALUES (%s, %s, %s)",
                        (job["id"], "Administrator revoked or blocked this script. Do not retry pending approvals or work around this decision.", actor),
                    )
        elif action in {"block-host", "unblock-host"} and len(args) == 2:
            host = network_access.normalize_pattern(args[1])
            tx.execute("DELETE FROM sandbox_permission_grants WHERE scope = 'global' AND kind = 'host' AND target = %s AND decision = 'block'", (host,))
            if action == "block-host":
                tx.execute(
                    """INSERT INTO sandbox_permission_grants(kind, target, scope, decision, created_by)
                       VALUES ('host', %s, 'global', 'block', %s)""", (host, actor))
        elif action == "revoke-grant" and len(args) == 2:
            changed = tx.fetch_one("DELETE FROM sandbox_permission_grants WHERE id = %s RETURNING id", (int(args[1]),))
            if not changed:
                raise ValueError("Grant not found")
        elif action == "remove-legacy" and len(args) == 2:
            changed = tx.fetch_one("DELETE FROM sandbox_network_rules WHERE id = %s RETURNING id", (int(args[1]),))
            if not changed:
                raise ValueError("Legacy rule not found")
        else:
            raise ValueError("Unknown permission operation")
        audit(tx, actor, action, {"arguments": args[1:]})
    from .tools import ToolRuntime
    from .tools import ToolError
    try:
        result = ToolRuntime(db, config, {}).sandbox_control("POST", "/permissions/revoke")
    except ToolError as exc:
        raise PermissionRevocationIncomplete(
            "Permissions were saved, but active sandbox runs could not be stopped. "
            "Stop the sandbox service before relying on revocation. %s" % exc) from exc
    return {"status": "updated", "action": action, "active_runs": result}
