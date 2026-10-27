import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .config import AppConfig


LOGGER = logging.getLogger("assistant.tool_result_cache")
TEXT_OUTPUT_FIELDS = ("stdout", "stderr", "content")
ALWAYS_CACHE_TOOLS = {"command_execute"}
SOURCE_REPLAY_TOOLS = {"email_read"}
CACHE_METADATA_PREFIX = "cached_output_"


def compact_json(value: Any) -> str:
    return json.dumps(json_safe(value), default=str, ensure_ascii=True, sort_keys=True)


def json_safe(value: Any) -> Any:
    return sanitize_json_value(json.loads(json.dumps(value, default=str)))


def sanitize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "[NUL]")
    if isinstance(value, list):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, dict):
        return {sanitize_json_value(key): sanitize_json_value(item) for key, item in value.items()}
    return value


class ToolResultCache:
    def __init__(self, config: AppConfig):
        self.config = config
        self.shared_root = Path(config.get("agent.filesystem.shared_root", "/data/share")).resolve()
        self.root = self._cache_root()

    def enabled(self) -> bool:
        return self.config.get_bool("agent.tool_result_cache.enabled", True)

    def min_bytes(self) -> int:
        return self.config.get_int("agent.tool_result_cache.min_bytes", 4096)

    def retention_days(self) -> int:
        return self.config.get_int("agent.tool_result_cache.retention_days", 7)

    def cache_result(self, job_id: int, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        if not self.should_cache(tool_name, result):
            return result
        try:
            return self._write_cache(job_id, tool_name, result)
        except Exception:
            LOGGER.exception("failed to cache tool result", extra={"job_id": job_id, "tool_name": tool_name})
            return result

    def should_cache(self, tool_name: str, result: dict[str, Any]) -> bool:
        if not self.enabled() or not isinstance(result, dict):
            return False
        if result.get("cached_output_path"):
            return False
        if self.is_cache_file_read(tool_name, result):
            return False
        if tool_name in SOURCE_REPLAY_TOOLS:
            return False
        if tool_name in ALWAYS_CACHE_TOOLS:
            return True
        return len(compact_json(result).encode("utf-8")) >= self.min_bytes()

    def redact_result(self, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict):
            return result
        redacted = dict(result)
        if tool_name == "email_read":
            redacted = self._redact_email_read_result(redacted)
        cached_path = str(redacted.get("cached_output_path") or "").strip()
        for field in TEXT_OUTPUT_FIELDS:
            value = redacted.get(field)
            if not isinstance(value, str):
                continue
            if not value:
                continue
            field_bytes = len(value.encode("utf-8"))
            if not cached_path and field_bytes <= self.min_bytes():
                continue
            redacted["%s_preview" % field] = self._preview(value)
            redacted[field] = self._omitted_message(field, cached_path)
            redacted["%s_omitted" % field] = True
            redacted["%s_bytes" % field] = field_bytes
        if cached_path and tool_name != "email_read":
            redacted = self._redact_cached_nested_values(redacted, cached_path)
            redacted["cached_output_available"] = True
        elif cached_path:
            redacted["cached_output_available"] = True
        return redacted

    def _redact_email_read_result(self, result: dict[str, Any]) -> dict[str, Any]:
        email = result.get("email")
        if not isinstance(email, dict):
            return result
        redacted = dict(result)
        redacted_email = dict(email)
        email_id = redacted_email.get("id") or redacted_email.get("email_id")
        for field in ("body_text", "body_html"):
            value = redacted_email.get(field)
            if not isinstance(value, str) or not value:
                continue
            redacted_email[field] = "[%s omitted from persisted context; call email_read with email_id %s if needed]" % (
                field,
                email_id,
            )
            redacted_email["%s_omitted" % field] = True
            redacted_email["%s_bytes" % field] = len(value.encode("utf-8"))
        if email_id:
            redacted_email["body_recall"] = "call email_read with email_id %s" % email_id
        redacted["email"] = redacted_email
        return redacted

    def cleanup_expired(self, now: Optional[datetime] = None) -> int:
        if not self.enabled():
            return 0
        retention = self.retention_days()
        if retention < 1:
            return 0
        current_time = now or datetime.now(timezone.utc)
        cutoff = current_time - timedelta(days=retention)
        removed = 0
        if not self.root.exists():
            return 0
        for path in self.root.rglob("*.json"):
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            except OSError:
                continue
            if modified >= cutoff:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                LOGGER.warning("could not remove expired tool result cache file", extra={"path": str(path)})
        self._remove_empty_dirs()
        return removed

    def _write_cache(self, job_id: int, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        created_at = datetime.now(timezone.utc)
        result_payload = json_safe(result)
        result_bytes = compact_json(result_payload).encode("utf-8")
        result_sha256 = hashlib.sha256(result_bytes).hexdigest()
        job_dir = self.root / ("job-%s" % int(job_id))
        job_dir.mkdir(parents=True, exist_ok=True)
        filename = "%s-%s-%s-%s.json" % (
            created_at.strftime("%Y%m%dT%H%M%S%fZ"),
            self._safe_tool_name(tool_name),
            result_sha256[:12],
            uuid.uuid4().hex[:8],
        )
        target = job_dir / filename
        cache_payload = {
            "job_id": int(job_id),
            "tool_name": tool_name,
            "created_at": created_at.isoformat(),
            "result_sha256": result_sha256,
            "result_size_bytes": len(result_bytes),
            "result": result_payload,
        }
        target.write_text(json.dumps(cache_payload, ensure_ascii=True, indent=2, default=str), encoding="utf-8")
        cached = dict(result)
        cached.update(
            {
                "cached_output_path": str(target),
                "cached_output_relative_path": str(target.relative_to(self.shared_root)),
                "cached_output_sha256": result_sha256,
                "cached_output_size_bytes": len(result_bytes),
                "cached_output_created_at": created_at.isoformat(),
            }
        )
        return cached

    def _cache_root(self) -> Path:
        configured = str(self.config.get("agent.tool_result_cache.root", ".assistant/cache/tool-results") or "").strip()
        path = Path(configured) if configured else Path(".assistant/cache/tool-results")
        if not path.is_absolute():
            path = self.shared_root / path
        resolved = path.resolve()
        if resolved != self.shared_root and self.shared_root not in resolved.parents:
            LOGGER.warning("tool result cache root must stay under shared root; falling back to default", extra={"configured": configured})
            return (self.shared_root / ".assistant/cache/tool-results").resolve()
        return resolved

    def _omitted_message(self, field: str, cached_path: str) -> str:
        if cached_path:
            return "[%s omitted from context; read cached_output_path with file_read if needed]" % field
        return "[%s omitted from persisted context]" % field

    def _preview(self, value: str, limit: int = 500) -> str:
        text = str(value or "")
        if len(text) <= limit:
            return text
        return "%s..." % text[:limit]

    def is_cache_file_read(self, tool_name: str, result: dict[str, Any]) -> bool:
        if tool_name != "file_read":
            return False
        path_value = str(result.get("path") or "").strip()
        if not path_value:
            return False
        try:
            path = Path(path_value).resolve()
        except OSError:
            return False
        return path == self.root or self.root in path.parents

    def _remove_empty_dirs(self) -> None:
        for path in sorted((item for item in self.root.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
            try:
                path.rmdir()
            except OSError:
                pass

    def _safe_tool_name(self, tool_name: str) -> str:
        clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(tool_name or "tool")).strip("-")
        return clean[:80] or "tool"

    def _redact_cached_nested_values(self, result: dict[str, Any], cached_path: str) -> dict[str, Any]:
        redacted = dict(result)
        for key, value in list(redacted.items()):
            if key.startswith(CACHE_METADATA_PREFIX) or key in TEXT_OUTPUT_FIELDS:
                continue
            if key.endswith("_omitted") or key.endswith("_bytes"):
                continue
            if isinstance(value, (dict, list)):
                redacted[key] = self._omitted_message(key, cached_path)
                redacted["%s_omitted" % key] = True
            elif isinstance(value, str) and len(value.encode("utf-8")) > 1000:
                redacted[key] = self._omitted_message(key, cached_path)
                redacted["%s_omitted" % key] = True
                redacted["%s_bytes" % key] = len(value.encode("utf-8"))
        return redacted
