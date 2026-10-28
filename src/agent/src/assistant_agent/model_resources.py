"""Shared spending admission and workspace output handling for hosted model tools."""
import base64
import binascii
import uuid
from decimal import Decimal, InvalidOperation

from psycopg.types.json import Jsonb


MEDIA_TYPES = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg", "audio/flac": ".flac", "audio/pcm": ".pcm",
}


class ModelServiceError(ValueError):
    pass


class ProviderRejected(ModelServiceError):
    """The provider rejected the request before generation."""


def validate_media(data, mime):
    signatures = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
        "audio/wav": data.startswith(b"RIFF") and data[8:12] == b"WAVE",
        "audio/x-wav": data.startswith(b"RIFF") and data[8:12] == b"WAVE",
        "audio/flac": data.startswith(b"fLaC"),
        "audio/ogg": data.startswith(b"OggS"),
        "audio/mpeg": data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 255 and data[1] & 224 == 224),
        "application/pdf": data.startswith(b"%PDF-"),
    }
    if mime in signatures and not signatures[mime]:
        raise ModelServiceError("Media bytes do not match MIME type %s" % mime)


class ModelToolResources:
    def __init__(self, runtime):
        self.runtime = runtime
        self.db = runtime.db
        self.config = runtime.config

    def budget_settings(self):
        try:
            amount = Decimal(str(self.config.get("agent.model_service.budget_usd", 5)))
            days = self.config.get("agent.model_service.budget_period_days", 30)
            if not amount.is_finite() or amount <= 0 or type(days) is not int or not 1 <= days <= 365:
                raise ValueError
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ModelServiceError("Model service budget must be positive and period must be 1-365 days") from exc
        return amount, days

    def budget(self, conn=None):
        amount, days = self.budget_settings()
        sql = """SELECT COALESCE(sum(cost_usd), 0) AS spent,
                        count(*) FILTER (WHERE status IN ('pending', 'unknown')) AS unresolved
                 FROM model_service_calls WHERE created_at > now() - (%s * interval '1 day')"""
        row = conn.execute(sql, (days,)).fetchone() if conn is not None else self.db.fetch_one(sql, (days,))
        return {"limit_usd": float(amount), "period_days": days, "spent_usd": float(row["spent"]),
                "unresolved_requests": row["unresolved"],
                "remaining_usd": float(max(Decimal(0), amount - row["spent"]))}

    def reserve(self, preset):
        if self.budget()["unresolved_requests"]:
            from .tts import TextToSpeech
            TextToSpeech(self.runtime).reconcile()
        call_id = str(uuid.uuid4())
        with self.db.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('assistant_model_service_budget'))")
            budget = self.budget(conn)
            if budget["unresolved_requests"]:
                raise ModelServiceError(
                    "Model service has a pending or unknown-cost request. "
                    "Eligible speech charges are retried automatically on the next request. "
                    "Pending requests, non-speech charges or missing generation IDs require administrator reconciliation; "
                    "changing models cannot bypass this block."
                )
            if budget["remaining_usd"] <= 0:
                raise ModelServiceError("Model service budget exhausted for the rolling period")
            conn.execute(
                "INSERT INTO model_service_calls(id, job_id, preset) VALUES (%s, %s, %s)",
                (call_id, self.runtime.job.get("id"), Jsonb(preset)),
            )
        return call_id

    def settle(self, call_id, status, usage=None, generation_id=None, *, conn=None):
        database = conn if conn is not None else self.db
        cost = None
        if usage is not None and usage.get("cost") is not None:
            try:
                cost = Decimal(str(usage["cost"]))
                if not cost.is_finite() or cost < 0:
                    raise ValueError
            except (InvalidOperation, TypeError, ValueError) as exc:
                database.execute("UPDATE model_service_calls SET status = 'unknown' WHERE id = %s", (call_id,))
                raise ModelServiceError("OpenRouter returned an invalid cost; request %s remains unresolved" % call_id) from exc
        if status == "completed" and cost is None:
            status = "unknown"
        database.execute(
            "UPDATE model_service_calls SET status = %s, cost_usd = %s, usage = %s, generation_id = %s WHERE id = %s",
            (status, cost, Jsonb(usage) if usage is not None else None, generation_id, call_id),
        )
        return status

    def save_media(self, encoded, mime, call_id, index):
        if mime not in MEDIA_TYPES or not isinstance(encoded, str):
            raise ModelServiceError("Unsupported media MIME type; cost already recorded")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ModelServiceError("Invalid base64 media output; cost already recorded") from exc
        return self.save_media_bytes(data, mime, call_id, index)

    def save_media_bytes(self, data, mime, call_id, index=0):
        if mime not in MEDIA_TYPES:
            raise ModelServiceError("Unsupported media MIME type")
        if not data or len(data) > 32 * 1024 * 1024:
            raise ModelServiceError("Media output is empty or exceeds 32 MiB")
        validate_media(data, mime)
        value = "outputs/job-%s/%s-%s%s" % (self.runtime.job["id"], call_id, index, MEDIA_TYPES[mime])
        self.runtime.reject_symlink_path(value)
        path = self.runtime.resolve_path(value)
        self.runtime.check_write_allowed(path)
        self.runtime.workstream_files.register_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(data)
        self.runtime.workstream_files.track_file(path)
        self.runtime.normalize_and_index_path(path, source="model_service")
        return {"path": str(path.relative_to(self.runtime.shared_root)), "mime_type": mime, "bytes": len(data)}
