"""Sandbox network access: host rules, per-run policy and admin decisions.

Sandbox commands reach the internet only through a per-run HTTPS proxy. The
proxy sees host names (CONNECT + TLS SNI), not full URLs, so every rule and
approval is host-level. Precedence: any matching block wins, then any
matching allow (job-scoped, global, or configured default); otherwise denied.
"""
import argparse
import ipaddress
import json
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb


DEFAULT_ALLOWED_HOSTS = ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org")
MAX_REQUEST_HOSTS = 10
APPROVAL_TTL_MINUTES = 60
DECISIONS = {
    "once": "Approve once",
    "always": "Approve always",
    "block": "Block once",
    "blockall": "Block always",
    "cancel": "Cancel job",
}
EMAIL_KEYWORDS = {
    "approve once": "once",
    "approve always": "always",
    "block once": "block",
    "block always": "blockall",
    "cancel job": "cancel",
    "cancel": "cancel",
}
REASON_TEXT = {
    "not_approved": "not approved yet",
    "blocked_by_admin": "blocked by the administrator",
    "plain_http": "plain HTTP is not allowed; use HTTPS",
    "port_not_allowed": "only port 443 is allowed",
    "ip_literal": "IP addresses are not allowed; use a host name",
    "invalid_host": "invalid host name",
    "private_address": "resolves to a private or internal address",
    "dns_failed": "host name could not be resolved",
    "sni_mismatch": "TLS server name did not match the approved host",
    "not_tls": "non-TLS traffic is not allowed",
    "upstream_failed": "remote host did not accept the connection",
}


def normalize_host(value: str) -> str:
    """Return the lowercase ASCII host for a URL or bare host name."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("empty host")
    parsed = urlsplit(text if "://" in text else "https://" + text)
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise ValueError("%r has no host name" % text)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("%s is an IP address; sandbox access is granted by host name" % host)
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("%r is not a valid host name" % text) from exc
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or any(
        not label or len(label) > 63 or not all(c.isalnum() or c == "-" for c in label)
        or label.startswith("-") or label.endswith("-") for label in labels
    ):
        raise ValueError("%r is not a valid host name" % text)
    return host


def normalize_pattern(value: str) -> str:
    """Admin/config patterns: an exact host or '*.example.com' for its subdomains."""
    text = str(value or "").strip().lower()
    if text.startswith("*."):
        return "*." + normalize_host(text[2:])
    return normalize_host(text)


def matches(pattern: str, host: str) -> bool:
    if pattern.startswith("*."):
        return host.endswith(pattern[1:])
    return host == pattern


def default_hosts(config) -> list[str]:
    configured = config.get("agent.sandbox.network.allowed_hosts", list(DEFAULT_ALLOWED_HOSTS))
    if isinstance(configured, str):
        configured = [item for item in configured.split(",")]
    hosts = []
    for item in configured or []:
        try:
            hosts.append(normalize_pattern(item))
        except ValueError:
            continue
    return hosts


def policy(db, config, job_id: Optional[int]) -> dict[str, list[str]]:
    rows = [] if db is None else db.fetch_all(
        "SELECT host, decision FROM sandbox_network_rules WHERE job_id IS NULL OR job_id = %s ORDER BY id",
        (job_id,),
    )
    allow = default_hosts(config) + [row["host"] for row in rows if row["decision"] == "allow"]
    block = [row["host"] for row in rows if row["decision"] == "block"]
    return {"allow": sorted(set(allow)), "block": sorted(set(block))}


def decide(current: dict[str, list[str]], host: str) -> str:
    if any(matches(pattern, host) for pattern in current["block"]):
        return "blocked"
    if any(matches(pattern, host) for pattern in current["allow"]):
        return "allowed"
    return "not_approved"


def summarize_events(events: list[dict[str, str]], egress: Optional[str]) -> Optional[dict[str, Any]]:
    """Turn proxy events into feedback the agent can act on."""
    if egress == "none":
        return {"egress": "none", "message": "This command ran without network access."}
    if egress != "proxy":
        return None
    allowed = sorted({item["host"] for item in events if item.get("decision") == "allowed"})
    blocked = []
    seen = set()
    for item in events:
        if item.get("decision") != "blocked":
            continue
        key = (item.get("host"), item.get("reason"))
        if key in seen:
            continue
        seen.add(key)
        blocked.append({"host": item.get("host") or "", "reason": item.get("reason") or "",
                        "detail": REASON_TEXT.get(item.get("reason") or "", item.get("reason") or "")})
    result: dict[str, Any] = {"egress": "proxy", "allowed_hosts": allowed}
    if blocked:
        result["blocked"] = blocked
        hints = []
        if any(item["reason"] == "not_approved" for item in blocked):
            hints.append(
                "Network access was denied for hosts that are not approved. If this access is essential, call "
                "network_access_request with the URLs and a clear purpose, then retry after the administrator decides. "
                "Do not try other hosts, mirrors, IP addresses or tools to get around the proxy."
            )
        if any(item["reason"] == "blocked_by_admin" for item in blocked):
            hints.append("Some hosts were blocked by the administrator. Do not request or retry them; "
                         "continue without them or ask the administrator with request_input.")
        result["hint"] = " ".join(hints) or "Some connections were refused by the sandbox proxy; see blocked."
    return result


def request_summary(job: dict[str, Any], request: dict[str, Any]) -> str:
    lines = [
        "Network access request: job #%s" % job["id"],
        "",
        "The agent wants sandbox commands to reach:",
        *["  • %s" % host for host in request["hosts"]],
        "",
        "Purpose: %s" % request.get("purpose", ""),
        "",
        "Approve once = these hosts for this job only. Approve always = all future jobs.",
        "Block once / Block always = refuse for this job / permanently. Cancel job = stop the task.",
        "Access is granted per host (all HTTPS paths on it), not per URL.",
        "",
        "Details:",
        "Task: %s" % (job.get("task_summary") or ""),
        "URLs given by the agent:",
        *["  %s" % url for url in request.get("urls", [])],
    ]
    return "\n".join(lines)


def approval_payload(job: dict[str, Any]) -> Optional[dict[str, Any]]:
    request = (job.get("metadata") or {}).get("network_request")
    if not request or not request.get("id"):
        return None
    return {"kind": "network", "job_id": int(job["id"]), "request_id": str(request["id"])}


def send_approval(db, config, job: dict[str, Any], source_suffix: str = "") -> bool:
    from .email_approvals import EmailApprovals
    payload = approval_payload(job)
    if payload is None:
        raise ValueError("Task has no pending network access request")
    request = job["metadata"]["network_request"]
    return EmailApprovals(db, config).request(
        "network:%s:%s%s" % (payload["job_id"], payload["request_id"], source_suffix),
        payload,
        request_summary(job, request),
        ttl_minutes=APPROVAL_TTL_MINUTES,
        email_subject="Assistant network access request (job #%s)" % payload["job_id"],
    )


def create_request(db, config, job: dict[str, Any], urls: Any, purpose: Any) -> dict[str, Any]:
    """Validate an agent request. Returns {'status': ...} and stores a pending request when needed."""
    if not isinstance(urls, list) or not urls:
        raise ValueError("urls must be a non-empty list of URLs or host names")
    purpose = str(purpose or "").strip()
    if not purpose:
        raise ValueError("purpose is required: explain what the access is for")
    hosts: list[str] = []
    for url in urls[:MAX_REQUEST_HOSTS * 3]:
        host = normalize_host(str(url))
        if host not in hosts:
            hosts.append(host)
    if len(hosts) > MAX_REQUEST_HOSTS:
        raise ValueError("request at most %s hosts at a time" % MAX_REQUEST_HOSTS)
    current = policy(db, config, int(job["id"]))
    decisions = {host: decide(current, host) for host in hosts}
    allowed = [host for host, value in decisions.items() if value == "allowed"]
    blocked = [host for host, value in decisions.items() if value == "blocked"]
    pending = [host for host, value in decisions.items() if value == "not_approved"]
    result: dict[str, Any] = {"allowed": allowed, "blocked": blocked}
    if blocked:
        result["blocked_message"] = ("The administrator blocked these hosts. Do not retry or work around this; "
                                     "continue without them or ask the administrator with request_input.")
    if not pending:
        result["status"] = "no_approval_needed"
        return result
    request = {
        "id": str(uuid.uuid4()),
        "hosts": pending,
        "urls": [str(url)[:500] for url in urls[:MAX_REQUEST_HOSTS * 3]],
        "purpose": purpose[:1000],
    }
    result.update({"status": "pending", "requested": pending, "request": request})
    return result


def apply_decision(db, payload: dict[str, Any], decision: str, feedback: str = "", actor: str = "admin") -> str:
    from .requests import TransactionDatabase
    if decision not in DECISIONS:
        raise ValueError("Unknown network decision")
    if decision in {"once", "always"}:
        raise ValueError("Network-only approvals are retired. Request combined script and host approval with script_approval_request.")
    feedback = str(feedback or "").strip()
    with db.connect() as conn:
        database = TransactionDatabase(conn)
        job = database.fetch_one(
            """SELECT id, metadata FROM jobs WHERE id = %s AND status = 'needs_review'
               AND metadata->'network_request'->>'id' = %s FOR UPDATE""",
            (payload["job_id"], payload["request_id"]),
        )
        if not job:
            raise ValueError("This network request was already handled or the task changed.")
        request = job["metadata"]["network_request"]
        hosts = list(request.get("hosts") or [])
        label = DECISIONS[decision]
        note = "%s by %s: %s" % (label, actor, request.get("purpose", ""))[:1000]
        if decision == "block":
            for host in hosts:
                database.execute(
                    """INSERT INTO sandbox_network_rules(host, decision, job_id, note, created_by)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (job_id, host) WHERE job_id IS NOT NULL
                       DO UPDATE SET decision = EXCLUDED.decision, note = EXCLUDED.note,
                                     created_by = EXCLUDED.created_by, created_at = now()""",
                    (host, "block", job["id"], note, actor),
                )
        elif decision == "blockall":
            for host in hosts:
                database.execute(
                    """INSERT INTO sandbox_network_rules(host, decision, job_id, note, created_by)
                       VALUES (%s, %s, NULL, %s, %s)
                       ON CONFLICT (host) WHERE job_id IS NULL
                       DO UPDATE SET decision = EXCLUDED.decision, note = EXCLUDED.note,
                                     created_by = EXCLUDED.created_by, created_at = now()""",
                    (host, "block", note, actor),
                )
        if decision == "cancel":
            database.execute(
                """UPDATE jobs SET status = 'cancelled', completed_at = now(), locked_at = NULL, locked_by = NULL,
                   last_error = %s, metadata = metadata - 'network_request', updated_at = now() WHERE id = %s""",
                ("Cancelled by admin during network access review" + (": " + feedback if feedback else ""), job["id"]),
            )
        else:
            instruction = (
                "Administrator decision on your network access request (%s) for %s: %s. %s" % (
                    request.get("id"), ", ".join(hosts), label,
                    "Do not access these hosts or try to work around the block; continue without them "
                    "or report what cannot be done.",
                )
            )
            if feedback:
                instruction += "\nAdministrator feedback: " + feedback
            database.execute(
                "INSERT INTO supervisor_instructions(job_id, instruction, created_by) VALUES (%s, %s, %s)",
                (job["id"], instruction, actor),
            )
            database.execute(
                """UPDATE jobs SET status = 'queued', run_at = now(), has_new_context = true,
                   locked_at = NULL, locked_by = NULL, last_error = NULL,
                   metadata = metadata - 'network_request', updated_at = now() WHERE id = %s""",
                (job["id"],),
            )
        database.log_event(job["id"], "status_change", output_data={
            "status": "cancelled" if decision == "cancel" else "queued",
            "reason": "network access decision", "decision": decision,
            "hosts": hosts, "request_id": request.get("id"), "feedback": feedback,
        })
    if decision == "cancel":
        return "Job #%s cancelled." % payload["job_id"]
    return "%s: %s. Job #%s resumed." % (DECISIONS[decision], ", ".join(hosts), payload["job_id"])


def parse_email_decision(text: str) -> tuple[Optional[str], str]:
    lines = str(text or "").strip().splitlines()
    if not lines:
        return None, ""
    first = " ".join(lines[0].strip().strip(".!:").lower().split())
    decision = EMAIL_KEYWORDS.get(first)
    if decision is None:
        return None, "\n".join(lines).strip()
    return decision, "\n".join(lines[1:]).strip()


def store_pending_request(db, job_id: int, request: dict[str, Any]) -> None:
    db.execute("UPDATE jobs SET metadata = metadata || %s WHERE id = %s",
               (Jsonb({"network_request": request}), job_id))


def main(argv: Optional[list[str]] = None) -> None:
    from .config import database_url, load_config
    from .database import Database

    parser = argparse.ArgumentParser(prog="python -m assistant_agent.network_access",
                                     description="Manage sandbox network access rules.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="show inactive legacy allows and stored blocks")
    for name in ("allow", "block"):
        command = commands.add_parser(name, help="%s a host for all jobs (or one job with --job)" % name)
        command.add_argument("host", help="host name or *.example.com")
        command.add_argument("--job", type=int)
        command.add_argument("--note", default="")
    remove = commands.add_parser("remove", help="delete a rule")
    remove.add_argument("host")
    remove.add_argument("--job", type=int)
    args = parser.parse_args(argv)
    if args.command == "allow":
        parser.error("Global/job host allows are retired; request combined version-bound script and host approval.")
    config = load_config()
    db = Database(database_url(config))
    if args.command == "list":
        print(json.dumps({
            "inactive_legacy_defaults": default_hosts(config),
            "rules": [
                {key: (str(value) if key == "created_at" else value) for key, value in row.items()}
                for row in db.fetch_all(
                    "SELECT host, decision, job_id, note, created_by, created_at FROM sandbox_network_rules ORDER BY job_id NULLS FIRST, host"
                )
            ],
        }, indent=2))
        return
    host = normalize_pattern(args.host)
    if args.command == "remove":
        db.execute("DELETE FROM sandbox_network_rules WHERE host = %s AND job_id IS NOT DISTINCT FROM %s", (host, args.job))
        print("removed %s%s" % (host, " for job #%s" % args.job if args.job else ""))
        return
    conflict = "(job_id, host) WHERE job_id IS NOT NULL" if args.job else "(host) WHERE job_id IS NULL"
    db.execute(
        "INSERT INTO sandbox_network_rules(host, decision, job_id, note, created_by) VALUES (%s, %s, %s, %s, 'admin-cli') "
        "ON CONFLICT " + conflict + " DO UPDATE SET decision = EXCLUDED.decision, note = EXCLUDED.note, "
        "created_by = EXCLUDED.created_by, created_at = now()",
        (host, args.command, args.job, args.note or None),
    )
    print("%s %s%s" % (args.command, host, " for job #%s" % args.job if args.job else " for all jobs"))


if __name__ == "__main__":
    main()
