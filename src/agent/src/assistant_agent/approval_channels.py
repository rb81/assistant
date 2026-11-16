"""Approval delivery policy and verified administrator identities.

Setup imports this module with only setup dependencies installed, so runtime
modules (Telegram delivery, database) are imported inside functions.
"""


def channels(config):
    value = str(config.get("agent.approvals.channel", "email")).strip().lower()
    if value not in {"email", "telegram", "both", "client"}:
        raise ValueError("Approval channel must be email, telegram, both or client")
    return value


def telegram_admins(config):
    from .telegram_config import telegram_identities
    admin = str(config.get("agent.admin.email") or "").strip().lower()
    if not admin:
        return []
    identifier = telegram_identities(config).get(admin)
    if identifier is None:
        return []
    value = str(identifier)
    allowed = config.get_list("agent.telegram.allowed_user_ids")
    if not value.isdigit() or int(value) <= 0 or value not in allowed:
        raise ValueError("The admin email's Telegram mapping must be a positive user ID on the Telegram allowlist")
    return [value]


def approvals_configured(config):
    from .telegram_config import telegram_configured
    from .validation import smtp_configured
    mode = channels(config)
    if mode == "client":
        return True
    if mode in {"telegram", "both"}:
        if not telegram_admins(config):
            return False
        if not telegram_configured(config) and not smtp_configured(config):
            return False
    if mode in {"email", "both"} and (not config.get("agent.admin.email") or not smtp_configured(config)):
        return False
    return True


def review_label(job):
    return str((job.get("metadata") or {}).get("title") or job.get("task_summary") or "Task")[:80]


def review_summary(job, reason):
    title = review_label(job)
    metadata = job.get("metadata") or {}
    if metadata.get("security_pending_action"):
        return ("%s\n\nA safety check paused an action. "
                "Approve once permits only the exact action below without increasing execution limits. "
                "Deny cancels the paused task; completed actions are not undone.\n"
                "Job #%s\n\nDetails:\n%s" % (title, job["id"], reason))
    return ("%s\n\nThis task needs your attention. Resume continues existing work with additional "
            "execution allowance. Deny cancels the task; completed actions are not undone.\n"
            "Job #%s\n\nDetails:\n%s" % (title, job["id"], reason))
