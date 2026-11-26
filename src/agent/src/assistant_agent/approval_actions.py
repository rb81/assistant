"""Deterministic execution of a consumed, version-bound approval."""


def deny_approval(db, payload, *, actor="admin"):
    if payload["kind"] == "review":
        job = db.fetch_one(
            """UPDATE jobs SET status = 'cancelled', locked_at = NULL, locked_by = NULL, updated_at = now()
               WHERE id = %s AND status = 'needs_review' AND updated_at = %s RETURNING id""",
            (payload["job_id"], payload["version"]),
        )
        if not job:
            raise ValueError("This task has changed. Request a fresh approval from its status card.")
        db.log_event(payload["job_id"], "status_change", output_data={"reason": "%s denied approval" % actor})
        return "Request denied. Task cancelled; completed actions were not undone."
    if payload["kind"] in {"email", "screened_email", "quarantined_email", "command"}:
        return "Request denied. Nothing was approved or executed."
    raise ValueError("Unknown approval action")


def apply_approval(db, config, payload, instruction="", *, actor="admin"):
    from .admin_commands import AdminCommands
    kind = payload["kind"]
    if kind == "permission":
        from .script_permissions import apply_decision, parse_decision
        decision, feedback = parse_decision(instruction)
        return apply_decision(db, payload, decision, feedback, actor)
    if kind == "network":
        from .network_access import apply_decision, parse_email_decision
        decision, feedback = parse_email_decision(instruction)
        if decision is None:
            raise ValueError("Start the reply with APPROVE ONCE, APPROVE ALWAYS, BLOCK ONCE, BLOCK ALWAYS or CANCEL JOB.")
        return apply_decision(db, payload, decision, feedback, actor)
    if kind == "review":
        from .email_ingest import EmailDownloader

        job = db.fetch_one(
            "SELECT * FROM jobs WHERE id = %s AND status = 'needs_review' AND updated_at = %s",
            (payload["job_id"], payload["version"]),
        )
        if not job:
            raise ValueError("This task has changed. Request a fresh approval from its status card.")
        EmailDownloader(db, config).apply_email_review_override(job, 0, instruction)
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
    raise ValueError("Unknown approval action")
