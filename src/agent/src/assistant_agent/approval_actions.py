"""Deterministic execution of a consumed, version-bound approval."""
import base64


def apply_approval(db, config, payload, instruction=""):
    from .admin_commands import AdminCommands
    from .email_ingest import EmailDownloader
    kind = payload["kind"]
    if kind == "permission":
        from .script_permissions import apply_decision, parse_decision
        decision, feedback = parse_decision(instruction)
        return apply_decision(db, payload, decision, feedback)
    if kind == "network":
        from .network_access import apply_decision, parse_email_decision
        decision, feedback = parse_email_decision(instruction)
        if decision is None:
            raise ValueError("Start the reply with APPROVE ONCE, APPROVE ALWAYS, BLOCK ONCE, BLOCK ALWAYS or CANCEL JOB.")
        return apply_decision(db, payload, decision, feedback, "admin")
    downloader = EmailDownloader(db, config)
    if kind == "review":
        job = db.fetch_one(
            "SELECT * FROM jobs WHERE id = %s AND status = 'needs_review' AND updated_at = %s",
            (payload["job_id"], payload["version"]),
        )
        if not job:
            raise ValueError("This task has changed. Request a fresh approval from its status card.")
        downloader.apply_email_review_override(job, 0, instruction)
        return "Task resumed."
    if kind == "command":
        body = payload["body"]
        words = payload["subject"].split()
        if instruction:
            if len(words) >= 2 and words[1] in {"review", "instruct"}:
                body = (body + "\n\n" + instruction).strip()
            else:
                raise ValueError("This command cannot accept additional guidance")
        return AdminCommands(db, config).execute(payload["subject"], body, confirmed=True)
    if kind == "screened_email":
        downloader.ingest_raw(base64.b64decode(payload["raw"]), payload["folder"],
                              approved_thread_id=payload["thread_id"], security_approved=instruction)
        return "Email released for processing."
    if kind == "email":
        current = db.fetch_one(
            "SELECT * FROM jobs WHERE thread_id = %s AND status IN ('queued', 'running', 'waiting', 'needs_review')",
            (payload["thread_id"],),
        )
        snapshot = {"id": current["id"], "review_version": str(current["updated_at"])
                    if current["status"] == "needs_review" else None} if current else None
        if snapshot != payload["job"]:
            raise ValueError("This task has changed. Send a fresh request.")
        downloader.ingest_raw(
            base64.b64decode(payload["raw"]), payload["folder"], approved_instruction=instruction,
            approved_thread_id=payload["thread_id"],
            approved_review=current if current and current["status"] == "needs_review" else None,
        )
        return "Email approved for processing."
    raise ValueError("Unknown approval action")
