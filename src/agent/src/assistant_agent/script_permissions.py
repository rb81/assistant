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
    "global": "Approve always for all approved scripts",
    "deny": "Deny",
}
KEYWORDS = {
    "approve once": "once",
    "approve always": "always",
    "approve always for this script": "always",
    "approve always for all scripts": "global",
    "approve always for all approved scripts": "global",
    "deny": "deny",
}
UNSTABLE_SIGNALS = {
    "code_not_inspectable", "remote_code_execution", "obfuscated_code_execution",
    "binary_executable", "unparseable_code",
}


class PermissionRequired(Exception):
    def __init__(self, request):
        self.request = request
        super().__init__("Administrator permission required: " + request["kind"])


class PermissionDenied(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def requested_hosts(urls):
    if not isinstance(urls, list) or not 1 <= len(urls) <= network_access.MAX_REQUEST_HOSTS:
        raise ValueError("Provide 1 to 10 exact hostnames or HTTPS URLs")
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


def grants(db, version_id, job_id):
    return db.fetch_all(
        """SELECT * FROM sandbox_permission_grants WHERE consumed_at IS NULL
           AND (version_id = %s OR scope = 'global')
           AND (scope <> 'once' OR job_id = %s) ORDER BY id""", (version_id, job_id),
    )


def run_status(db, version, job_id):
    script = db.fetch_one("SELECT blocked FROM sandbox_scripts WHERE id = %s", (version["script_id"],))
    if not script or script["blocked"]:
        return "blocked"
    rows = [row for row in grants(db, version["id"], job_id) if row["kind"] == "run"]
    if any(row["decision"] == "block" for row in rows):
        return "denied"
    return "approved" if any(row["decision"] == "allow" for row in rows) else "not_approved"


def host_policy(db, config, version_id, job_id):
    legacy = network_access.policy(db, config, job_id)
    rows = [row for row in grants(db, version_id, job_id) if row["kind"] == "host"]
    return {
        "allow": sorted(set(legacy["allow"] + [row["target"] for row in rows if row["decision"] == "allow"])),
        "block": sorted(set(legacy["block"] + [row["target"] for row in rows if row["decision"] == "block"])),
    }


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
    status = run_status(runtime.db, version, runtime.job["id"])
    if status in {"blocked", "denied"}:
        raise PermissionDenied("This script is blocked or denied. Do not retry or work around the decision.")
    if kind == "run" and status == "approved":
        return {"status": "approved", "version_id": version["id"], "fingerprint": version["fingerprint"]}
    if kind == "host" and status != "approved":
        return {"status": "script_approval_required", "message": "Call script_approval_request first."}
    finding = screen(runtime, version) if kind == "run" else (version.get("screening") or {})
    if finding.get("level") == MALICIOUS:
        raise PermissionDenied("Security screening classified this version as malicious; execution is blocked.")
    targets = targets or []
    if kind == "host":
        current = host_policy(runtime.db, runtime.config, version["id"], runtime.job["id"])
        decisions = {host: network_access.decide(current, host) for host in targets}
        blocked = [host for host, value in decisions.items() if value == "blocked"]
        if blocked:
            raise PermissionDenied("Administrator blocked: %s. Do not request or retry these hosts." % ", ".join(blocked))
        targets = [host for host, value in decisions.items() if value != "allowed"]
        if not targets:
            return {"status": "approved", "version_id": version["id"]}
    pending = {
        "id": uuid.uuid4().hex, "kind": kind, "version_id": version["id"],
        "fingerprint": version["fingerprint"], "targets": targets,
        "purpose": str(purpose or version["purpose"])[:1000], "screening": finding,
        "reusable": version["reusable"],
    }
    raise PermissionRequired(pending)


def approval_payload(job):
    pending = (job.get("metadata") or {}).get("permission_request")
    if not pending:
        return None
    return {"kind": "permission", "job_id": job["id"], "request_id": pending["id"],
            "permission_type": pending["kind"], "reusable": pending["reusable"],
            "version_id": pending["version_id"]}


def summary(db, job):
    pending = job["metadata"]["permission_request"]
    version = db.fetch_one(
        """SELECT v.*, s.entry FROM sandbox_script_versions v JOIN sandbox_scripts s ON s.id = v.script_id
           WHERE v.id = %s""", (pending["version_id"],),
    )
    if not version:
        raise ValueError("Script version is no longer available")
    lines = [
        "Script %s permission: job #%s" % (pending["kind"], job["id"]),
        "Script #%s: %s" % (version["script_id"], version["entry"]),
        "Version #%s | SHA-256 %s" % (version["id"], version["fingerprint"]),
        "Purpose: " + pending["purpose"],
    ]
    if pending["kind"] == "host":
        lines += ["HTTPS hosts: " + ", ".join(pending["targets"]),
                  "All HTTPS paths on these exact hosts; no implicit subdomains.",
                  "Once = next execution. Always = this version. All scripts = all approved scripts."]
    else:
        lines += ["Once = one execution. Always = this version and exact invocation.",
                  pending["screening"].get("summary", "Screening not configured.")]
        if not version["reusable"]:
            lines.append("Always is unavailable: the executable dependency graph cannot be pinned safely.")
    lines += ["\nDetails:", json.dumps(version["manifest"], indent=2, sort_keys=True)]
    return "\n".join(lines)


def send_approval(db, config, job, suffix=""):
    from .email_approvals import EmailApprovals
    payload = approval_payload(job)
    if not payload:
        raise ValueError("No script permission request is pending")
    version = db.fetch_one("SELECT manifest FROM sandbox_script_versions WHERE id = %s", (payload["version_id"],))
    if not version:
        raise ValueError("Script version is no longer available")
    return EmailApprovals(db, config).request(
        "permission:%s:%s%s" % (job["id"], payload["request_id"], suffix),
        payload, summary(db, job), ttl_minutes=60,
        details_attachment=("script-version-%s.json" % payload["version_id"],
                            json.dumps(version["manifest"], indent=2, sort_keys=True).encode()),
    )


def parse_decision(text):
    lines = str(text or "").strip().splitlines()
    decision = KEYWORDS.get(" ".join(lines[0].lower().strip(".!: ").split())) if lines else None
    if not decision:
        raise ValueError("First line must be APPROVE ONCE, APPROVE ALWAYS, APPROVE ALWAYS FOR ALL SCRIPTS, or DENY.")
    return decision, "\n".join(lines[1:]).strip()


def apply_decision(db, payload, decision, feedback="", actor="admin"):
    if decision not in DECISIONS:
        raise ValueError("Unknown permission decision")
    with db.connect() as conn:
        tx = TransactionDatabase(conn)
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
               WHERE v.id = %s FOR UPDATE OF v, s""", (pending["version_id"],),
        )
        if not version or version["blocked"] or version["fingerprint"] != pending["fingerprint"]:
            raise ValueError("This script version is unavailable or blocked.")
        if decision == "global" and pending["kind"] != "host":
            raise ValueError("Run approval cannot apply to all scripts")
        if decision == "always" and not version["reusable"]:
            raise ValueError("This script needs one-time approval because its executable dependencies cannot be pinned.")
        scope = {"once": "once", "always": "script", "global": "global", "deny": "once"}[decision]
        for target in pending["targets"] if pending["kind"] == "host" else [""]:
            tx.execute(
                """INSERT INTO sandbox_permission_grants
                   (version_id, job_id, kind, target, scope, decision, created_by, note)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (None if scope == "global" else version["id"], job["id"] if scope == "once" else None,
                 pending["kind"], target, scope, "block" if decision == "deny" else "allow", actor, feedback),
            )
        instruction = (
            "Administrator %s for script version #%s (%s). %s" %
            (DECISIONS[decision], version["id"], pending["fingerprint"],
             "Do not repeat or work around this denied request." if decision == "deny" else
             "Continue with this unchanged version; request required hosts before execution.")
        )
        if feedback:
            instruction += "\nAdministrator feedback: " + feedback
        tx.execute("INSERT INTO supervisor_instructions(job_id, instruction, created_by) VALUES (%s, %s, %s)",
                   (job["id"], instruction, actor))
        tx.execute(
            """UPDATE jobs SET status = 'queued', run_at = now(), has_new_context = true,
               metadata = metadata - 'permission_request', locked_at = NULL, locked_by = NULL,
               last_error = NULL, updated_at = now() WHERE id = %s""", (job["id"],),
        )
        audit(tx, actor, decision, {**pending, "job_id": job["id"], "feedback": feedback})
    return "%s. Job #%s resumed; execution limits unchanged." % (DECISIONS[decision], payload["job_id"])


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
        if run_status(tx, version, runtime.job["id"]) != "approved":
            raise PermissionDenied("Run approval is missing, consumed, denied or revoked. Request approval first.")
        current = host_policy(tx, runtime.config, version["id"], runtime.job["id"])
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


def management(db, config, args, confirmed=False, actor="admin"):
    action = args[0] if args else "scripts"
    if action == "scripts":
        return db.fetch_all(
            """SELECT s.id, s.entry, s.blocked, v.id AS version_id, v.fingerprint, v.reusable,
               EXISTS (SELECT 1 FROM sandbox_permission_grants g WHERE g.version_id = v.id
                   AND g.kind = 'run' AND g.decision = 'allow' AND g.scope = 'script') AS approved_always,
               EXISTS (SELECT 1 FROM sandbox_permission_grants g WHERE g.version_id = v.id
                   AND g.kind = 'run' AND g.decision = 'allow' AND g.scope = 'once'
                   AND g.consumed_at IS NULL) AS has_once_grant,
               v.purpose, v.created_at FROM sandbox_scripts s
               LEFT JOIN sandbox_script_versions v ON v.script_id = s.id ORDER BY s.id DESC, v.id DESC LIMIT 100"""
        )
    if action == "hosts":
        return {
            "defaults": network_access.default_hosts(config),
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
        raise ValueError("Permissions were saved, but active sandbox runs could not be stopped. "
                         "Stop the sandbox service before relying on revocation. %s" % exc) from exc
    return {"status": "updated", "action": action, "active_runs": result}
