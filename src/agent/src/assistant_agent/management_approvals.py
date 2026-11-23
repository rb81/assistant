"""Safe management-facing review and decision logic for pending approvals.

The public reference generated here is an unguessable locator, not an
authorization credential. Callers must authenticate the administrator before
listing or deciding approvals.
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from typing import Any

from .approval_channels import channels
from .email_approvals import EmailApprovals
from .management_errors import ApprovalApplicationIncomplete
from .requests import TransactionDatabase


LOGGER = logging.getLogger("assistant.management_approvals")
_APPROVAL_KEY = re.compile(r"^email-approval:([a-f0-9]{64})$")
_REFERENCE_KEY = "management-approval-ref:"
_MAX_LIST_LIMIT = 100
_MAX_LIST_OFFSET = 1_000_000
_SUPPORTED_KINDS = {"permission", "network", "review", "command", "email", "screened_email"}
_PERMISSION_DECISIONS = {
    "approve_once": "Approve once",
    "approve_always": "Approve always",
    "deny": "Deny",
}
_NETWORK_DECISIONS = {
    "block_once": "BLOCK ONCE",
    "block_always": "BLOCK ALWAYS",
    "cancel_job": "CANCEL JOB",
}


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _configured_admin(config) -> str:
    admin = str(config.get("agent.admin.email") or "").strip().lower()
    if not admin and channels(config) not in {"telegram", "client"}:
        raise ValueError("Admin email is not configured.")
    return admin


def _transport_redact(value: Any) -> tuple[Any, bool]:
    try:
        from .admin_commands import redact_secrets

        redacted = redact_secrets(value)
        return redacted, _canonical(redacted) != _canonical(value)
    except Exception:
        fallback = {"content_withheld": True}
        return fallback, True


def _expires(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _stored_action(db, config, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Render a reviewable action and its current domain version."""
    kind = payload.get("kind")
    if kind == "command":
        action = {
            "type": "admin_command",
            "subject": payload.get("subject", ""),
            "body": payload.get("body", ""),
            "effect": "Execute this exact administrator command.",
            "decision_options": ["approve", "deny"],
        }
        return action, {}

    if kind in {"email", "screened_email"}:
        action = _email_action(payload)
        return action, {}

    if kind == "permission":
        import assistant_agent.script_permissions as script_permissions

        db.fetch_one(
            """SELECT s.id FROM sandbox_scripts s JOIN sandbox_script_versions v ON v.script_id = s.id
               WHERE v.id = %s FOR UPDATE OF s""",
            (payload.get("version_id"),),
        )
        job = _current_job(db, payload, "permission_request")
        pending = job["metadata"]["permission_request"]
        version = db.fetch_one(
            """SELECT v.*, s.entry, s.blocked FROM sandbox_script_versions v
               JOIN sandbox_scripts s ON s.id = v.script_id WHERE v.id = %s FOR UPDATE OF v""",
            (payload.get("version_id"),),
        )
        if not version or version["fingerprint"] != pending.get("fingerprint"):
            raise ValueError("This script permission request is no longer reviewable.")
        if int(payload.get("version_id", -1)) != int(pending.get("version_id", -2)):
            raise ValueError("This script permission request has changed.")
        scheduled = pending.get("scheduled") or {}
        if version["blocked"]:
            decision_options = []
        elif pending.get("kind") == "development" or not pending.get("reusable"):
            decision_options = ["approve_once", "deny"]
        elif scheduled.get("recurrence"):
            decision_options = ["approve_always", "deny"]
        elif scheduled:
            decision_options = ["approve_once", "deny"]
        else:
            decision_options = ["approve_once", "approve_always", "deny"]
        action = {
            "type": "script_permission",
            "effect": "Grant the selected permission for the exact script version and requested HTTPS hosts.",
            "job": _job_description(job),
            "request": _json_value(pending),
            "script": {
                "entry": version["entry"],
                "blocked": version["blocked"],
                "version_id": version["id"],
                "fingerprint": version["fingerprint"],
                "purpose": version["purpose"],
                "reusable": version["reusable"],
                "manifest": _json_value(version["manifest"]),
                "source": script_permissions.source_text(version),
            },
            "decision_options": decision_options,
        }
        return action, {"job_updated_at": _json_value(job["updated_at"])}

    if kind == "network":
        job = _current_job(db, payload, "network_request")
        request = job["metadata"]["network_request"]
        if request.get("id") != payload.get("request_id"):
            raise ValueError("This network request has changed.")
        action = {
            "type": "network_access",
            "effect": "Apply the selected block or cancellation decision to these requested hosts.",
            "job": _job_description(job),
            "request": _json_value(request),
            "decision_options": ["block_once", "block_always", "cancel_job"],
        }
        return action, {"job_updated_at": _json_value(job["updated_at"])}

    if kind == "review":
        job = db.fetch_one(
            """SELECT * FROM jobs WHERE id = %s AND status = 'needs_review'
               AND updated_at = %s FOR UPDATE""",
            (payload.get("job_id"), payload.get("version")),
        )
        if not job:
            raise ValueError("This task has changed and needs a fresh review.")
        metadata = job.get("metadata") or {}
        if metadata.get("permission_request") or metadata.get("network_request"):
            raise ValueError("This task now has a specific permission request; request a fresh approval.")
        blocked = metadata.get("security_pending_action") or {}
        tool_call = None
        if blocked.get("tool"):
            tool_call = db.fetch_one(
                """SELECT tool_name, input_data FROM task_logs
                   WHERE job_id = %s AND event_type = 'tool_call' AND tool_name = %s
                   ORDER BY sequence DESC LIMIT 1""",
                (job["id"], blocked["tool"]),
            )
            if not tool_call or not isinstance(tool_call.get("input_data"), dict):
                raise ValueError("The exact paused action inputs are no longer available for review.")
        current_override = metadata.get("admin_review_override") or {}
        base_iterations = (
            current_override.get("max_iterations_per_task")
            or config.get_int("agent.limits.max_iterations_per_task", 50)
        )
        base_tokens = (
            current_override.get("max_tokens_per_task")
            or config.get_int("agent.limits.max_tokens_per_task", 1000000)
        )
        factor = config.get_float("agent.supervisor.email_approval_increase_factor", 1.2)
        proposed_iterations = max(1, int(base_iterations * factor))
        proposed_tokens = max(1, int(base_tokens * factor))
        if blocked.get("fingerprint"):
            proposed_iterations, proposed_tokens = base_iterations, base_tokens
        action = {
            "type": "task_review",
            "effect": (
                "Resume the existing task with the exact paused action reviewed below."
                if blocked else
                "Resume the existing task; the model may determine subsequent actions after it resumes."
            ),
            "job": _job_description(job),
            "paused_reason": job.get("last_error") or "",
            "security_action": _json_value(blocked) if blocked else None,
            "tool_call": _json_value(tool_call) if tool_call else None,
            "execution_limits": {
                "current": {
                    "max_iterations_per_task": base_iterations,
                    "max_tokens_per_task": base_tokens,
                },
                "after_approval": {
                    "max_iterations_per_task": proposed_iterations,
                    "max_tokens_per_task": proposed_tokens,
                },
            },
            "decision_options": ["approve", "deny"],
        }
        return action, {"job_updated_at": _json_value(job["updated_at"])}

    raise ValueError("Unsupported approval kind")


def _job_description(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "status": job["status"],
        "task_summary": job.get("task_summary") or "",
        "title": (job.get("metadata") or {}).get("title") or "",
    }


def _current_job(db, payload: dict[str, Any], request_key: str) -> dict[str, Any]:
    job = db.fetch_one("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (payload.get("job_id"),))
    if not job or job["status"] != "needs_review":
        raise ValueError("This task is no longer awaiting this approval.")
    request = (job.get("metadata") or {}).get(request_key) or {}
    if request.get("id") != payload.get("request_id"):
        raise ValueError("This approval request is no longer current.")
    if payload.get("request_fingerprint"):
        from .approval_binding import request_fingerprint

        if not hmac.compare_digest(str(payload["request_fingerprint"]), request_fingerprint(request)):
            raise ValueError("This approval request has changed.")
    if payload.get("kind") == "permission":
        if int(request.get("version_id", -1)) != int(payload.get("version_id", -2)):
            raise ValueError("This script permission request has changed.")
        if request.get("kind") != payload.get("permission_type"):
            raise ValueError("This script permission request has changed.")
    return job


def _decode_part(part: Any) -> str:
    data = part.get_payload(decode=True)
    if data is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _email_action(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = base64.b64decode(payload["raw"], validate=True)
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("The stored email approval cannot be decoded safely.") from exc

    headers = [{"name": str(name), "value": str(value)} for name, value in message.items()]
    parts = []
    for part in message.walk():
        if part.is_multipart():
            continue
        content = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        item: dict[str, Any] = {
            "content_type": part.get_content_type(),
            "filename": str(filename) if filename else None,
            "size_bytes": len(content),
            "headers": [{"name": str(name), "value": str(value)} for name, value in part.items()],
        }
        if filename:
            item["content_base64"] = base64.b64encode(content).decode("ascii")
            if part.get_content_maintype() == "text":
                item["content"] = _decode_part(part)
        elif part.get_content_maintype() == "text":
            item["content"] = _decode_part(part)
        else:
            item["content_base64"] = base64.b64encode(content).decode("ascii")
        parts.append(item)

    action_type = "screened_email_release" if payload.get("kind") == "screened_email" else "email_ingestion"
    from .email_review import review_email

    return {
        "type": action_type,
        "effect": (
            "Release this quarantined email for processing."
            if payload.get("kind") == "screened_email" else
            "Ingest this email and process it as an administrator-originated request."
        ),
        "folder": payload.get("folder", ""),
        "thread_id": payload.get("thread_id", ""),
        "existing_job": _json_value(payload.get("job")),
        "review": payload.get("review") or review_email(payload),
        "headers": headers,
        "parts": parts,
        "decision_options": ["approve", "deny"],
    }


def _valid_pending(row: dict[str, Any], admin: str) -> tuple[str, dict[str, Any]]:
    match = _APPROVAL_KEY.fullmatch(row["key"])
    if not match:
        raise ValueError("Invalid pending approval record")
    digest = match.group(1)
    pending = row["value"]
    if not isinstance(pending, dict) or pending.get("admin") != admin:
        raise ValueError("Invalid pending approval record")
    otp_hash = pending.get("otp_hash")
    if not isinstance(otp_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", otp_hash):
        raise ValueError("Invalid pending approval record")
    reviewed_material = dict(pending)
    reviewed_material.pop("otp_hash", None)
    expected = hashlib.sha256(_canonical(reviewed_material).encode("utf-8")).hexdigest()
    if not hmac.compare_digest(digest, expected):
        raise ValueError("Approval content has changed. Request a fresh approval.")
    if not isinstance(pending.get("payload"), dict):
        raise ValueError("Invalid pending approval record")
    return digest, pending


def _public_kind(value: Any) -> str:
    return value if isinstance(value, str) and value in _SUPPORTED_KINDS else "unknown"


def _non_actionable(
    row: dict[str, Any], kind: str, reason_code: str, reason: str,
    action: Any = None,
) -> dict[str, Any]:
    expires_at = None
    pending = row.get("value")
    if isinstance(pending, dict) and pending.get("expires"):
        try:
            expires_at = _expires(pending["expires"]).isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
    return {
        "approval_id": None,
        "version": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "kind": kind,
        "expires_at": expires_at,
        "status": "non_actionable",
        "actionable": False,
        "reason_code": reason_code,
        "reason": reason,
        "action": action,
    }


def _unavailable_reason(exc: ValueError) -> tuple[str, str]:
    message = str(exc).lower()
    if "unsupported approval kind" in message:
        return "unsupported_kind", "This approval kind is not supported by management."
    if "exact paused action inputs" in message:
        return "missing_action_inputs", "The exact paused action inputs are unavailable; request a fresh review."
    if "expired" in message:
        return "expired", "This approval has expired; request a fresh approval."
    if "content has changed" in message or "invalid pending" in message:
        return "invalid_record", "The approval record is malformed or its content binding is invalid."
    if any(fragment in message for fragment in ("changed", "no longer", "unavailable", "stale")):
        return "stale_action", "The underlying task or request changed or is no longer reviewable."
    if "decode" in message:
        return "invalid_email", "The stored email content cannot be rendered safely."
    return "invalid_record", "The approval record is malformed or its content binding is invalid."


def _delete_references(db, approval_key: str) -> None:
    db.execute(
        "DELETE FROM runtime_state WHERE key LIKE %s AND value->>'approval_key' = %s",
        (_REFERENCE_KEY + "%", approval_key),
    )


def _reference_for(
    db, approval_key: str, version: str, action: dict[str, Any], domain_version: dict[str, Any],
    expires: str,
) -> str:
    """Reuse the single reference for an unchanged action instead of growing rows per poll."""
    action_fingerprint = _fingerprint(action)
    with db.connect() as conn:
        tx = TransactionDatabase(conn)
        tx.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("management-approval:" + approval_key,))
        approval = tx.fetch_one(
            "SELECT updated_at FROM runtime_state WHERE key = %s FOR UPDATE",
            (approval_key,),
        )
        if not approval or approval["updated_at"].isoformat() != version:
            return ""
        candidates = tx.fetch_all(
            """SELECT key, value FROM runtime_state
               WHERE key LIKE %s AND value->>'approval_key' = %s
                 AND value->>'approval_version' = %s
                 AND (value->>'expires')::timestamptz > now()
               ORDER BY key""",
            (_REFERENCE_KEY + "%", approval_key, version),
        )
        for candidate in candidates:
            value = candidate["value"]
            if (value.get("action_fingerprint") == action_fingerprint
                    and value.get("domain_version") == domain_version
                    and value.get("actionable") is True):
                tx.execute(
                    """DELETE FROM runtime_state WHERE key LIKE %s
                       AND value->>'approval_key' = %s AND key <> %s""",
                    (_REFERENCE_KEY + "%", approval_key, candidate["key"]),
                )
                return candidate["key"].removeprefix(_REFERENCE_KEY)
        tx.execute(
            "DELETE FROM runtime_state WHERE key LIKE %s AND value->>'approval_key' = %s",
            (_REFERENCE_KEY + "%", approval_key),
        )
        approval_id = uuid.uuid4().hex
        tx.set_state(_REFERENCE_KEY + approval_id, {
            "approval_key": approval_key,
            "approval_version": version,
            "action_fingerprint": action_fingerprint,
            "domain_version": domain_version,
            "actionable": True,
            "expires": expires,
        })
        return approval_id


def list_approvals(
    db, config, *, limit: int = 50, offset: int = 0,
) -> list[dict[str, Any]]:
    """Return one bounded, deterministically ordered page of approval reviews.

    The caller must enforce management administrator authorization. Returned
    data never includes the email OTP, its hash, or the approval content digest.
    Order is stable by approval-state update time and then its internal key.
    """
    limit = max(1, min(int(limit), _MAX_LIST_LIMIT))
    offset = int(offset)
    if not 0 <= offset <= _MAX_LIST_OFFSET:
        raise ValueError("Approval offset must be between 0 and %s." % _MAX_LIST_OFFSET)
    admin = _configured_admin(config)

    db.execute(
        """DELETE FROM runtime_state reference
           WHERE reference.key LIKE %s AND (
             (reference.value->>'expires')::timestamptz < now()
             OR NOT EXISTS (
               SELECT 1 FROM runtime_state approval
               WHERE approval.key = reference.value->>'approval_key'
             )
           )""",
        (_REFERENCE_KEY + "%",),
    )
    rows = db.fetch_all(
        """SELECT approval.key, approval.value, approval.updated_at, source.value AS source_value
           FROM runtime_state approval
           LEFT JOIN runtime_state source ON source.key = approval.value->>'source_key'
           WHERE approval.key LIKE 'email-approval:%%'
             AND (approval.value->>'admin' = %s OR approval.value->>'admin' IS NULL)
           ORDER BY approval.updated_at, approval.key LIMIT %s OFFSET %s""",
        (admin, limit + 1, offset),
    )
    rows = rows[:limit]
    result = []
    for row in rows:
        pending = row.get("value")
        kind = _public_kind(pending.get("payload", {}).get("kind")) if isinstance(pending, dict) and isinstance(
            pending.get("payload"), dict
        ) else "unknown"
        version = row["updated_at"].isoformat()
        expires = None
        try:
            if not isinstance(pending, dict):
                raise ValueError("Invalid pending approval record")
            expires = _expires(pending.get("expires"))
            if expires <= datetime.now(timezone.utc):
                result.append(_non_actionable(
                    row, kind, "expired", "This approval has expired; request a fresh approval.",
                ))
                _delete_references(db, row["key"])
                continue
            source = row.get("source_value")
            if not isinstance(source, dict):
                result.append(_non_actionable(
                    row, kind, "delivery_unavailable",
                    "The approval delivery record is missing; request a fresh approval.",
                ))
                _delete_references(db, row["key"])
                continue
            source_status = source.get("status")
            if source_status not in {"sent", "delivery_uncertain", "client_queued"}:
                reason = (
                    "Approval delivery is still in progress; refresh the list before deciding."
                    if source_status == "sending" else
                    "This approval is no longer pending; request a fresh approval."
                )
                result.append(_non_actionable(row, kind, "delivery_unavailable", reason))
                _delete_references(db, row["key"])
                continue
            digest, pending = _valid_pending(row, admin)
            action, domain_version = _stored_action(db, config, pending["payload"])
            safe_action, action_was_redacted = _transport_redact(action)
            if action_was_redacted:
                if isinstance(safe_action, dict):
                    safe_action["decision_options"] = []
                entry = _non_actionable(
                    row, kind, "redacted_details",
                    "Sensitive approval details were redacted from the management response; no decision is allowed. "
                    "Review through the configured approval channel.",
                    safe_action,
                )
                result.append(entry)
                _delete_references(db, row["key"])
                continue
            expires_at = expires.isoformat()
            approval_id = _reference_for(
                db, row["key"], version, action, domain_version, expires_at,
            )
            if not approval_id:
                result.append(_non_actionable(
                    row, kind, "stale_action",
                    "The approval changed while it was being listed; refresh the list.",
                ))
                _delete_references(db, row["key"])
                continue
            entry = {
                "approval_id": approval_id,
                "version": version,
                "kind": kind,
                "expires_at": expires_at,
                "status": "pending",
                "actionable": True,
                "action": action,
            }
            safe_entry, entry_was_redacted = _transport_redact(entry)
            if entry_was_redacted:
                safe_action = safe_entry.get("action") if isinstance(safe_entry, dict) else None
                if isinstance(safe_action, dict):
                    safe_action["decision_options"] = []
                result.append(_non_actionable(
                    row, kind, "redacted_details",
                    "Sensitive approval details were redacted from the management response; no decision is allowed. "
                    "Review through the configured approval channel.",
                    safe_action,
                ))
                _delete_references(db, row["key"])
            else:
                result.append(safe_entry)
        except (TypeError, ValueError, OverflowError) as exc:
            reason_code, reason = _unavailable_reason(exc) if isinstance(exc, ValueError) else (
                "invalid_record", "The approval record is malformed and cannot be reviewed safely.",
            )
            result.append(_non_actionable(row, kind, reason_code, reason))
            _delete_references(db, row["key"])
    return result


def _decision_instruction(kind: str, decision: str, instruction: str, *, channel_feedback: bool = False) -> tuple[str, bool]:
    instruction = str(instruction or "").strip()
    if kind == "permission":
        item = _PERMISSION_DECISIONS.get(decision)
        if not item:
            raise ValueError("Permission decisions must be approve_once, approve_always, or deny.")
        if decision == "deny" and instruction and not channel_feedback:
            raise ValueError("Feedback is not accepted with a denied permission request.")
        return item + (("\n" + instruction) if instruction else ""), False
    if kind == "network":
        item = _NETWORK_DECISIONS.get(decision)
        if not item:
            raise ValueError("Network decisions must be block_once, block_always, or cancel_job.")
        if decision == "cancel_job" and instruction and not channel_feedback:
            raise ValueError("Feedback is not accepted when cancelling a network request.")
        return item + (("\n" + instruction) if instruction else ""), False
    if decision not in {"approve", "deny"}:
        raise ValueError("This approval supports only approve or deny.")
    if decision == "deny" and instruction:
        raise ValueError("Feedback is not accepted with a denied approval.")
    return instruction, decision == "deny"


def _apply_decision(tx, config, digest, pending, action, decision, instruction, *, actor):
    """Consume and apply database actions, or record external work, in one transaction."""
    if decision not in action.get("decision_options", []):
        raise ValueError("This decision is not available for the reviewed approval.")
    instruction_text, denied = _decision_instruction(
        pending["payload"]["kind"], decision, instruction, channel_feedback=actor != "admin",
    )
    kind = pending["payload"]["kind"]
    consumed = EmailApprovals(tx, config)._consume(
        digest, pending["otp_hash"], denied=denied or (kind == "permission" and decision == "deny"),
    )
    _delete_references(tx, "email-approval:" + digest)
    if not denied and kind in {"email", "screened_email"}:
        from .approved_email_execution import enqueue

        return enqueue(tx, digest, pending, instruction_text), None
    if kind in {"permission", "network", "review"} or denied:
        from .approval_actions import apply_approval, deny_approval

        result = (deny_approval(tx, consumed, actor=actor) if denied else
                  apply_approval(tx, config, consumed, instruction_text, actor=actor))
        return result, None
    return None, (consumed, instruction_text, actor)


def _apply_external(db, config, external):
    if external is None:
        return None
    from .approval_actions import apply_approval

    consumed, instruction, actor = external
    try:
        return apply_approval(db, config, consumed, instruction, actor=actor)
    except Exception as exc:
        LOGGER.error("Approved action failed after consumption (%s); outcome may be partial", type(exc).__name__)
        raise ApprovalApplicationIncomplete() from None


def decide_channel_approval(db, config, digest, decision, instruction="", *, actor,
                            otp_hash=None, callback_id=None, sender=None):
    """Apply a channel-authenticated decision using the same domain transaction."""
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Invalid approval reference.")
    if not isinstance(instruction, str) or len(instruction) > 20_000:
        raise ValueError("Instruction must be text no longer than 20000 characters.")
    if actor == "admin-email":
        if not isinstance(otp_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", otp_hash):
            raise ValueError("Invalid approval credential.")
    elif actor == "telegram-admin":
        from .approval_channels import telegram_admins

        if (channels(config) not in {"telegram", "both"} or sender not in telegram_admins(config)
                or not isinstance(callback_id, str) or not re.fullmatch(r"[a-f0-9]{24}", callback_id)):
            raise ValueError("Administrator permission is required.")
    else:
        raise ValueError("Invalid approval channel.")
    guidance_unused = False
    with db.connect() as conn:
        tx = TransactionDatabase(conn)
        row = tx.fetch_one(
            "SELECT key, value, updated_at FROM runtime_state WHERE key = %s FOR UPDATE",
            ("email-approval:" + digest,),
        )
        if not row:
            raise ValueError("Approval is expired, invalid, or already used. Send a fresh request.")
        _, pending = _valid_pending(row, _configured_admin(config))
        if (pending["payload"].get("kind") in {"permission", "network"}
                and not pending["payload"].get("request_fingerprint")):
            raise ValueError("This older approval cannot be safely bound to its reviewed details. Request a fresh approval.")
        if _expires(pending.get("expires")) <= datetime.now(timezone.utc):
            raise ValueError("This approval has expired. Request a fresh approval.")
        if otp_hash is not None and not hmac.compare_digest(pending["otp_hash"], otp_hash):
            raise ValueError("Approval is expired, invalid, or already used. Send a fresh request.")
        if callback_id is not None and (
            pending.get("callback_id") != callback_id or sender not in pending.get("telegram_admin_ids", [])
        ):
            raise ValueError("Administrator permission is required.")
        source = tx.fetch_one(
            "SELECT value FROM runtime_state WHERE key = %s FOR UPDATE", (pending.get("source_key"),),
        )
        if not source or source["value"].get("status") not in {"sent", "delivery_uncertain", "client_queued"}:
            raise ValueError("This approval request is still being delivered or is no longer available.")
        if actor == "admin-email" and channels(config) == "telegram" and not source["value"].get("email_fallback"):
            raise ValueError("Email approval fallback was not attempted.")
        if actor == "admin-email" and decision is None:
            kind = pending["payload"]["kind"]
            if kind == "permission":
                from .script_permissions import parse_decision

                choice, instruction = parse_decision(instruction)
                decision = {"once": "approve_once", "always": "approve_always", "deny": "deny"}[choice]
            elif kind == "network":
                from .network_access import parse_email_decision

                choice, instruction = parse_email_decision(instruction)
                decision = {"block": "block_once", "blockall": "block_always", "cancel": "cancel_job"}.get(choice)
                if decision is None:
                    raise ValueError("Start the reply with BLOCK ONCE, BLOCK ALWAYS or CANCEL JOB.")
            else:
                decision = "approve"
                if kind == "command" and instruction:
                    words = pending["payload"].get("subject", "").split()
                    if len(words) < 2 or words[1] not in {"review", "instruct"}:
                        instruction = ""
                        guidance_unused = True
        action, _ = _stored_action(tx, config, pending["payload"])
        result, external = _apply_decision(
            tx, config, digest, pending, action, decision, instruction, actor=actor,
        )
    result = result if external is None else _apply_external(db, config, external)
    if guidance_unused:
        result += ("\n\nAdditional text was not applied: this command has no LLM job. "
                   "Use /admin instruct JOB_ID for job guidance.")
    return result


def decide_approval(
    db, config, approval_id: str, version: str, decision: str, instruction: str = "",
) -> str:
    """Consume one approval only if its reviewed version and action are unchanged."""
    if not isinstance(approval_id, str) or not re.fullmatch(r"[a-f0-9]{32}", approval_id):
        raise ValueError("Invalid approval reference.")
    if not isinstance(version, str) or not version:
        raise ValueError("The reviewed approval version is required.")
    if not isinstance(instruction, str) or len(instruction) > 20_000:
        raise ValueError("Instruction must be text no longer than 20000 characters.")
    admin = _configured_admin(config)

    result = None
    external_apply = None
    with db.connect() as conn:
        tx = TransactionDatabase(conn)
        reference = tx.fetch_one(
            "SELECT key, value FROM runtime_state WHERE key = %s",
            (_REFERENCE_KEY + approval_id,),
        )
        if not reference:
            raise ValueError("This approval reference is expired, stale, or already used.")
        reviewed = reference["value"]
        if reviewed.get("actionable") is not True:
            raise ValueError("This approval is non-actionable and cannot be decided through management.")
        if reviewed.get("approval_version") != version:
            raise ValueError("The approval changed after it was reviewed. Refresh the approval list.")
        if _expires(reviewed.get("expires")) <= datetime.now(timezone.utc):
            raise ValueError("This approval has expired. Request a fresh approval.")

        approval_key = reviewed.get("approval_key")
        approval = tx.fetch_one(
            "SELECT key, value, updated_at FROM runtime_state WHERE key = %s FOR UPDATE",
            (approval_key,),
        )
        reference = tx.fetch_one(
            "SELECT key, value FROM runtime_state WHERE key = %s FOR UPDATE",
            (_REFERENCE_KEY + approval_id,),
        )
        if not reference or reference["value"] != reviewed:
            raise ValueError("This approval reference is expired, stale, or already used.")
        if not approval or approval["updated_at"].isoformat() != version:
            raise ValueError("The approval changed after it was reviewed. Refresh the approval list.")
        digest, pending = _valid_pending(approval, admin)
        source = tx.fetch_one(
            "SELECT value FROM runtime_state WHERE key = %s FOR UPDATE",
            (pending.get("source_key"),),
        )
        if not source or source["value"].get("status") not in {"sent", "delivery_uncertain", "client_queued"}:
            raise ValueError("This approval request is still being delivered or is no longer available.")
        action, domain_version = _stored_action(tx, config, pending["payload"])
        if (_fingerprint(action) != reviewed.get("action_fingerprint")
                or domain_version != reviewed.get("domain_version")):
            raise ValueError("The action changed after it was reviewed. Refresh the approval list.")
        result, external_apply = _apply_decision(
            tx, config, digest, pending, action, decision, instruction, actor="admin",
        )
    return result if external_apply is None else _apply_external(db, config, external_apply)
