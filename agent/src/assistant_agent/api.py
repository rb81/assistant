import hashlib
import io
import json
import logging
import mimetypes
import shlex
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Union
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

from . import chat_responder
from .chat_store import ChatStore
from .config import agent_email, app_display_name, database_url, load_config, message_id_domain
from .contact_store import ContactStore
from .database import Database, json_safe
from .entity_store import EntityStore
from .document_text import DocumentTextExtractor
from .memory_store import MEMORY_COLUMNS, MemoryStore
from .notifications import compute_review_diagnostics
from .note_store import NOTE_COLUMNS, NoteStore, UNSET as NOTE_UNSET
from .threading import safe_filename
from .time_utils import local_datetime_iso, parse_datetime, recurrence_anchor_day
from .tools import ToolError, ToolRuntime
from .ui_pages import UI_ROOT, render_ui_page as render_configured_ui_page
from .validation import tool_status
from .workspace_index import WorkspaceIndex


config = load_config()
LOGGER = logging.getLogger("assistant.api")
docs_url = "/docs" if config.get_bool("agent.api.docs_enabled", False) else None
redoc_url = "/redoc" if config.get_bool("agent.api.docs_enabled", False) else None
openapi_url = "/openapi.json" if config.get_bool("agent.api.openapi_enabled", False) else None
app = FastAPI(
    title=app_display_name(config),
    docs_url=docs_url,
    redoc_url=redoc_url,
    openapi_url=openapi_url,
)
if (UI_ROOT / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(UI_ROOT / "assets")), name="assets")
db = Database(database_url(config))
db.ensure_feature_schema()


class ManualJobRequest(BaseModel):
    subject: str = Field(..., min_length=1, max_length=240)
    body: str = Field(..., min_length=1)
    from_address: str = "dashboard@local"


class InstructionRequest(BaseModel):
    instruction: str = Field(..., min_length=1)


class JobReviewOverrideRequest(BaseModel):
    instruction: Optional[str] = Field(None, max_length=10000)
    max_iterations_per_task: Optional[int] = Field(None, ge=1, le=10000)
    max_tokens_per_task: Optional[int] = Field(None, ge=1, le=100000000)
    requeue: bool = True


class MemoryCreateRequest(BaseModel):
    content: str = Field(..., min_length=1)
    tags: list[str] = Field(default_factory=list)
    scope: str = "global"
    kind: str = "project_context"
    importance: int = Field(3, ge=1, le=5)
    confidence: float = Field(0.7, ge=0.0, le=1.0)
    expires_at: Optional[str] = None
    pinned: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryUpdateRequest(BaseModel):
    content: Optional[str] = None
    tags: Optional[list[str]] = None
    scope: Optional[str] = None
    kind: Optional[str] = None
    importance: Optional[int] = Field(None, ge=1, le=5)
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    expires_at: Optional[str] = None
    pinned: Optional[bool] = None
    metadata: Optional[dict[str, Any]] = None


class NoteCreateRequest(BaseModel):
    content: str = Field(..., min_length=1)
    title: Optional[str] = Field(None, max_length=240)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NoteUpdateRequest(BaseModel):
    content: Optional[str] = None
    title: Optional[str] = Field(None, max_length=240)
    tags: Optional[list[str]] = None
    metadata: Optional[dict[str, Any]] = None


class ContactCreateRequest(BaseModel):
    first_name: str = Field("", max_length=120)
    last_name: str = Field("", max_length=120)
    email_address: str = Field("", max_length=320)
    company: str = Field("", max_length=240)
    title: str = Field("", max_length=240)
    notes: str = Field("", max_length=10000)


class ContactUpdateRequest(BaseModel):
    first_name: Optional[str] = Field(None, max_length=120)
    last_name: Optional[str] = Field(None, max_length=120)
    email_address: Optional[str] = Field(None, max_length=320)
    company: Optional[str] = Field(None, max_length=240)
    title: Optional[str] = Field(None, max_length=240)
    notes: Optional[str] = Field(None, max_length=10000)


class ReminderCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=240)
    task: str = Field(..., min_length=1, max_length=20000)
    run_at: str = Field(..., min_length=1, max_length=120)
    priority: int = 0
    recurrence_unit: Optional[str] = Field(None, max_length=20)
    recurrence_interval: Optional[int] = Field(None, ge=1, le=10000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReminderUpdateRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=240)
    task: Optional[str] = Field(None, min_length=1, max_length=20000)
    run_at: Optional[str] = Field(None, min_length=1, max_length=120)
    priority: Optional[int] = None
    recurrence_unit: Optional[str] = Field(None, max_length=20)
    recurrence_interval: Optional[int] = Field(None, ge=1, le=10000)
    metadata: Optional[dict[str, Any]] = None


class WorkspaceFileWriteRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=2000)
    content: str = ""
    expected_mtime_ns: Optional[Union[int, str]] = None
    create_only: bool = False


class WorkspaceDraftSnapshotRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=2000)
    content: str = ""


class WorkspaceFolderCreateRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=2000)


class WorkspacePathOperationRequest(BaseModel):
    source_path: str = Field(..., min_length=1, max_length=2000)
    destination_path: str = Field(..., min_length=1, max_length=2000)


class WorkspaceConvertRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=2000)
    output_format: str = Field(..., min_length=1, max_length=40)
    delete_original: bool = False


class WorkspaceArchiveRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=2000)
    destination_path: Optional[str] = Field(None, min_length=1, max_length=2000)


class WorkspaceExtractRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=2000)
    destination_folder: Optional[str] = Field(None, min_length=1, max_length=2000)


class WorkspaceJobRequest(BaseModel):
    message: str = Field(..., min_length=1)
    active_path: Optional[str] = Field(None, max_length=2000)
    include_active_file: bool = False
    include_file_content: bool = False
    file_content: Optional[str] = None


class WorkspaceScriptRunRequest(BaseModel):
    path: Optional[str] = Field(None, max_length=2000)
    command: list[str] = Field(default_factory=list)
    workdir: Optional[str] = Field(None, max_length=2000)
    timeout_seconds: Optional[int] = Field(None, ge=1, le=3600)


class ChatMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=20000)


USAGE_NUMBER_PATTERN = r"^-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?$"
REMINDER_STATUSES = {"scheduled", "queued", "completed", "failed", "cancelled"}
PROJECT_STATUSES = {"queued", "running", "completed", "failed", "cancelled"}
RECURRENCE_UNITS = {"hour", "day", "week", "month"}
RECURRENCE_UNIT_ALIASES = {
    "hours": "hour",
    "daily": "day",
    "days": "day",
    "weekly": "week",
    "weeks": "week",
    "monthly": "month",
    "months": "month",
}


def usage_number_sql(alias: str, key: str) -> str:
    field = "%s.tokens_used->>'%s'" % (alias, key)
    return "CASE WHEN %s ~ '%s' THEN (%s)::double precision ELSE 0::double precision END" % (
        field,
        USAGE_NUMBER_PATTERN,
        field,
    )


def usage_total_tokens_sql(alias: str) -> str:
    total = "%s.tokens_used->>'total_tokens'" % alias
    prompt = usage_number_sql(alias, "prompt_tokens")
    completion = usage_number_sql(alias, "completion_tokens")
    input_tokens = usage_number_sql(alias, "input_tokens")
    output_tokens = usage_number_sql(alias, "output_tokens")
    return """
    CASE
      WHEN %(total)s ~ '%(pattern)s' THEN (%(total)s)::double precision
      WHEN (%(prompt)s + %(completion)s) > 0 THEN %(prompt)s + %(completion)s
      ELSE %(input_tokens)s + %(output_tokens)s
    END
    """ % {
        "total": total,
        "pattern": USAGE_NUMBER_PATTERN,
        "prompt": prompt,
        "completion": completion,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


JOB_COST_JOIN = """
LEFT JOIN (
  SELECT usage_logs.job_id, SUM(usage_logs.cost) AS cost_total
  FROM (
    SELECT
      tl.job_id,
      %s AS cost
    FROM task_logs tl
    WHERE tl.tokens_used IS NOT NULL

    UNION ALL

    SELECT
      drr.original_job_id AS job_id,
      %s AS cost
    FROM deep_research_events dre
    JOIN deep_research_runs drr ON drr.id = dre.run_id
    WHERE dre.tokens_used IS NOT NULL
  ) usage_logs
  GROUP BY usage_logs.job_id
) jc ON jc.job_id = j.id
""" % (usage_number_sql("tl", "cost"), usage_number_sql("dre", "cost"))


JOB_LIST_SELECT = """
SELECT
  j.*,
  COALESCE(jc.cost_total, 0)::double precision AS cost_total,
  te.from_address AS trigger_from_address,
  te.subject AS trigger_subject,
  te.received_at AS trigger_received_at,
  (
    SELECT COUNT(*)
    FROM emails ce
    WHERE ce.thread_id = j.thread_id
      AND (j.completed_at IS NULL OR ce.created_at <= j.completed_at)
  ) AS context_email_count
FROM jobs j
LEFT JOIN emails te ON te.id = j.trigger_email_id
%s
""" % JOB_COST_JOIN


CANCELLABLE_JOB_STATUSES = {"queued", "running", "waiting", "needs_review"}
REQUEUEABLE_JOB_STATUSES = {"failed", "cancelled", "waiting", "needs_review", "completed"}

WORKSPACE_BINARY_EXTENSIONS = {
    ".7z",
    ".avif",
    ".bmp",
    ".doc",
    ".docx",
    ".dmg",
    ".eot",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp3",
    ".mp4",
    ".ods",
    ".odt",
    ".otf",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".tar",
    ".tif",
    ".tiff",
    ".ttf",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".xls",
    ".xlsm",
    ".xlsx",
    ".zip",
}

WORKSPACE_TEXT_EXTENSIONS = {
    ".bash",
    ".c",
    ".conf",
    ".cpp",
    ".cs",
    ".css",
    ".csv",
    ".env",
    ".fish",
    ".go",
    ".h",
    ".hpp",
    ".htm",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".less",
    ".log",
    ".markdown",
    ".mjs",
    ".md",
    ".mdown",
    ".mkdn",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sass",
    ".scss",
    ".sh",
    ".sql",
    ".ts",
    ".tsx",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
    ".zsh",
}

WORKSPACE_TEXT_FILENAMES = {
    ".dockerignore",
    ".env",
    ".gitignore",
    "dockerfile",
    "makefile",
    "readme",
}


def job_context_cutoff(job: dict[str, Any]) -> Any:
    if job.get("completed_at") and job.get("status") in {"completed", "failed", "cancelled"}:
        return job["completed_at"]
    return None


def job_actions(job: dict[str, Any]) -> dict[str, bool]:
    status = str(job.get("status") or "")
    return {
        "can_cancel": status in CANCELLABLE_JOB_STATUSES,
        "can_requeue": status in REQUEUEABLE_JOB_STATUSES,
        "can_review_override": status == "needs_review",
    }


def model_fields_set(model: BaseModel) -> set[str]:
    return set(getattr(model, "model_fields_set", getattr(model, "__fields_set__", set())))


def memory_store() -> MemoryStore:
    return MemoryStore(db, config)


def contact_store() -> ContactStore:
    return ContactStore(db)


def chat_store() -> ChatStore:
    return ChatStore(db)


def sse_pack(event: dict[str, Any]) -> str:
    return "data: %s\n\n" % json.dumps(event, default=str)


def chat_escalation_body(user_message: str, transcript: str) -> str:
    if not transcript:
        return user_message
    return "%s\n\nConversation so far:\n%s" % (user_message, transcript)


def chat_message_events(
    session: dict[str, Any],
    history: list[dict[str, Any]],
    user_message: str,
) -> Iterator[dict[str, Any]]:
    """Drive chat_responder for one turn, persisting rows and yielding client-facing events."""
    store = chat_store()
    accumulated = ""
    try:
        for event in chat_responder.generate_reply_events(config, history, user_message):
            kind = event.get("type")
            if kind == "delta":
                accumulated += event["text"]
                yield {"type": "delta", "text": event["text"]}
            elif kind == "escalated":
                transcript = chat_responder.condense_transcript(history + [{"role": "user", "content": user_message}])
                job_request = WorkspaceJobRequest(message=chat_escalation_body(user_message, transcript))
                job = create_workspace_job(
                    job_request,
                    source="chat_escalation",
                    subject_override="Chat: %s" % event["task_summary"][:80],
                    extra_metadata={"chat_session_id": session["id"]},
                )
                ack = "On it — I'll work on this now."
                store.create_message(session["id"], "assistant", kind="job_ref", content=ack, job_id=job["id"])
                yield {"type": "escalated", "job_id": job["id"], "text": ack}
                return
            elif kind == "error":
                yield {"type": "error", "message": event["message"]}
                return
            elif kind == "done":
                store.create_message(
                    session["id"], "assistant", kind="chat", content=accumulated,
                    tokens_used=event.get("usage"),
                )
                yield {"type": "done"}
                return
    except Exception as exc:
        LOGGER.exception("chat stream failed for session %s", session["id"])
        yield {"type": "error", "message": str(exc)}


def clean_status_filter(value: Optional[str], allowed: set[str], label: str) -> Optional[str]:
    clean = str(value or "").strip().lower()
    if not clean:
        return None
    if clean not in allowed:
        raise HTTPException(status_code=400, detail="invalid %s status" % label)
    return clean


def float_value(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


def int_value(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def usage_cost_summary() -> dict[str, Any]:
    task_cost = usage_number_sql("tl", "cost")
    research_cost = usage_number_sql("dre", "cost")
    chat_cost = usage_number_sql("cm", "cost")
    row = db.fetch_one(
        """
        WITH log_costs AS (
          SELECT
            tl.job_id,
            tl.created_at,
            %(task_cost)s AS cost
          FROM task_logs tl
          WHERE tl.tokens_used IS NOT NULL

          UNION ALL

          SELECT
            drr.original_job_id AS job_id,
            dre.created_at,
            %(research_cost)s AS cost
          FROM deep_research_events dre
          JOIN deep_research_runs drr ON drr.id = dre.run_id
          WHERE dre.tokens_used IS NOT NULL

          UNION ALL

          SELECT
            NULL::bigint AS job_id,
            cm.created_at,
            %(chat_cost)s AS cost
          FROM chat_messages cm
          WHERE cm.tokens_used IS NOT NULL
        ),
        job_costs AS (
          SELECT
            j.id,
            COALESCE(SUM(lc.cost), 0)::double precision AS cost
          FROM jobs j
          LEFT JOIN log_costs lc ON lc.job_id = j.id
          GROUP BY j.id
        )
        SELECT
          COALESCE((SELECT SUM(cost) FROM log_costs), 0)::double precision AS lifetime_total,
          COALESCE(
            (SELECT SUM(cost) FROM log_costs WHERE created_at >= date_trunc('month', now())),
            0
          )::double precision AS month_total,
          COALESCE((SELECT AVG(cost) FROM job_costs), 0)::double precision AS average_per_job,
          (SELECT COUNT(*) FROM job_costs) AS job_count,
          (SELECT COUNT(*) FROM job_costs WHERE cost > 0) AS charged_job_count,
          (SELECT COUNT(*) FROM log_costs) AS api_call_count
        """
        % {"task_cost": task_cost, "research_cost": research_cost, "chat_cost": chat_cost}
    )
    return {
        "currency": "USD",
        "lifetime_total": float_value(row.get("lifetime_total") if row else 0),
        "month_total": float_value(row.get("month_total") if row else 0),
        "average_per_job": float_value(row.get("average_per_job") if row else 0),
        "job_count": int_value(row.get("job_count") if row else 0),
        "charged_job_count": int_value(row.get("charged_job_count") if row else 0),
        "api_call_count": int_value(row.get("api_call_count") if row else 0),
    }


def job_usage_summary(job_id: int) -> dict[str, Any]:
    task_cost = usage_number_sql("tl", "cost")
    task_total_tokens = usage_total_tokens_sql("tl")
    research_cost = usage_number_sql("dre", "cost")
    research_total_tokens = usage_total_tokens_sql("dre")
    row = db.fetch_one(
        """
        WITH usage_logs AS (
          SELECT
            tl.created_at,
            %(task_cost)s AS cost,
            %(task_total_tokens)s AS total_tokens
          FROM task_logs tl
          WHERE tl.job_id = %%s
            AND tl.tokens_used IS NOT NULL

          UNION ALL

          SELECT
            dre.created_at,
            %(research_cost)s AS cost,
            %(research_total_tokens)s AS total_tokens
          FROM deep_research_events dre
          JOIN deep_research_runs drr ON drr.id = dre.run_id
          WHERE drr.original_job_id = %%s
            AND dre.tokens_used IS NOT NULL
        )
        SELECT
          COALESCE(SUM(cost), 0)::double precision AS cost_total,
          COUNT(*) AS api_call_count,
          COALESCE(SUM(total_tokens), 0)::double precision AS total_tokens,
          MIN(created_at) AS first_usage_at,
          MAX(created_at) AS last_usage_at
        FROM usage_logs
        """
        % {
            "task_cost": task_cost,
            "task_total_tokens": task_total_tokens,
            "research_cost": research_cost,
            "research_total_tokens": research_total_tokens,
        },
        (job_id, job_id),
    )
    return {
        "currency": "USD",
        "cost_total": float_value(row.get("cost_total") if row else 0),
        "api_call_count": int_value(row.get("api_call_count") if row else 0),
        "total_tokens": int(float_value(row.get("total_tokens") if row else 0)),
        "first_usage_at": row.get("first_usage_at") if row else None,
        "last_usage_at": row.get("last_usage_at") if row else None,
    }


def clean_recurrence_unit(value: Optional[str]) -> Optional[str]:
    clean = str(value or "").strip().lower()
    clean = RECURRENCE_UNIT_ALIASES.get(clean, clean)
    if clean in ("", "none"):
        return None
    if clean not in RECURRENCE_UNITS:
        raise ValueError("recurrence_unit must be hour, day, week, month, or none")
    return clean


def clean_recurrence_interval(value: Optional[int]) -> int:
    if value is None:
        return 1
    try:
        interval = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("recurrence_interval must be an integer") from exc
    if interval < 1:
        raise ValueError("recurrence_interval must be greater than zero")
    return interval


def recurrence_fields(run_at: datetime, unit: Optional[str], interval: Optional[int]) -> tuple[Optional[str], Optional[int], Optional[int]]:
    clean_unit = clean_recurrence_unit(unit)
    if clean_unit is None:
        if interval is not None:
            raise ValueError("recurrence_unit is required when recurrence_interval is provided")
        return None, None, None
    clean_interval = clean_recurrence_interval(interval)
    return clean_unit, clean_interval, recurrence_anchor_day(run_at, clean_unit, config)


def reminder_public(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    result = dict(row)
    if result.get("run_at"):
        result["run_at_local"] = local_datetime_iso(result["run_at"], config)
    return result


def raise_reminder_error(exc: ValueError) -> None:
    raise HTTPException(status_code=400, detail=str(exc)) from exc


def workspace_runtime() -> ToolRuntime:
    return ToolRuntime(db, config, {"id": 0, "thread_id": "dashboard"}, allow_cache_reads=False)


def normalize_and_index_workspace_path(path: Path, source: str = "dashboard", inline_convert_documents: bool = False) -> Path:
    try:
        index = WorkspaceIndex(db, config)
        canonical = path.resolve()
        if index.enabled():
            if canonical.is_dir():
                index.index_tree_best_effort(canonical, source=source)
            else:
                index.index_path_best_effort(canonical, source=source)
        return canonical
    except Exception:
        return path.resolve()


def raise_workspace_error(exc: Exception) -> None:
    message = str(exc)
    if isinstance(exc, FileNotFoundError):
        raise HTTPException(status_code=404, detail="path not found") from exc
    raise HTTPException(status_code=400, detail=message) from exc


def workspace_latest_conversion_rows(relative_paths: list[str]) -> dict[str, dict[str, Any]]:
    clean_paths = sorted({str(path) for path in relative_paths if str(path or "").strip()})
    if not clean_paths:
        return {}
    rows = db.fetch_all(
        """
        SELECT DISTINCT ON (original_relative_path)
          id,
          original_relative_path,
          markdown_relative_path,
          archived_relative_path,
          status,
          error,
          created_at,
          updated_at
        FROM workspace_document_conversions
        WHERE original_relative_path = ANY(%s)
        ORDER BY original_relative_path, created_at DESC, id DESC
        """,
        (clean_paths,),
    )
    return {str(row["original_relative_path"]): row for row in rows}


def workspace_latest_conversion_row(relative_path: str) -> Optional[dict[str, Any]]:
    return workspace_latest_conversion_rows([relative_path]).get(relative_path)


def workspace_file_conversion_metadata(path: Path, conversion_row: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    if path.is_dir() or not DocumentTextExtractor(config).is_convertible_document(path):
        return None
    status = str((conversion_row or {}).get("status") or "").strip()
    if status in {"pending", "ready"} and path.exists():
        status = ""
    if status and status not in {"pending", "ready", "failed", "skipped"}:
        status = ""
    return {
        "convertible": True,
        "status": status or None,
        "pending": status == "pending",
        "markdown_relative_path": (conversion_row or {}).get("markdown_relative_path"),
        "archived_relative_path": (conversion_row or {}).get("archived_relative_path"),
        "error": (conversion_row or {}).get("error"),
    }


def workspace_file_metadata(path: Path, conversion_row: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    stat = path.stat()
    shared_root = Path(config.get("agent.filesystem.shared_root", "/data/share")).resolve()
    metadata = {
        "name": path.name,
        "relative_path": str(path.relative_to(shared_root)),
        "is_dir": path.is_dir(),
        "size_bytes": stat.st_size,
        "mtime_ns": str(stat.st_mtime_ns),
    }
    conversion = workspace_file_conversion_metadata(path, conversion_row)
    if conversion:
        metadata["conversion"] = conversion
        metadata["conversion_status"] = conversion["status"]
        metadata["conversion_pending"] = conversion["pending"]
    return metadata


def workspace_directory_version(runtime: ToolRuntime, max_entries: int = 10000) -> dict[str, Any]:
    root = runtime.shared_root
    digest = hashlib.sha256()
    count = 0
    total_size = 0
    latest_mtime_ns = 0
    truncated = False
    if not root.exists():
        return {
            "version": "",
            "entry_count": 0,
            "total_size_bytes": 0,
            "latest_mtime_ns": "0",
            "truncated": False,
        }

    for item in sorted(root.rglob("*"), key=lambda value: str(value.relative_to(root))):
        try:
            resolved = item.resolve()
            if runtime.is_tool_cache_path(resolved):
                continue
            if runtime.is_source_archive_path(resolved):
                continue
            stat = item.stat()
            relative = str(item.relative_to(root))
            if relative == ".cache" or relative.startswith(".cache/"):
                continue
        except OSError:
            continue
        count += 1
        if count > max_entries:
            truncated = True
            break
        is_dir = item.is_dir()
        size = 0 if is_dir else stat.st_size
        total_size += size
        latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
        digest.update(relative.encode("utf-8", errors="replace"))
        digest.update(b"\0d" if is_dir else b"\0f")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\n")

    return {
        "version": digest.hexdigest(),
        "entry_count": min(count, max_entries),
        "total_size_bytes": total_size,
        "latest_mtime_ns": str(latest_mtime_ns),
        "truncated": truncated,
    }


def workspace_file_is_text_candidate(path: Path) -> bool:
    return DocumentTextExtractor(config).is_text_candidate(path)


def read_workspace_text_file(runtime: ToolRuntime, target: Path, max_bytes: Optional[int], offset: int) -> dict[str, Any]:
    if not target.is_file():
        raise ToolError("path is not a file")
    if not workspace_file_is_text_candidate(target):
        raise HTTPException(status_code=415, detail="unsupported file type for workspace editor")
    runtime.check_cache_read_allowed(target)
    configured_limit = config.get_int("agent.filesystem.max_read_bytes", 102400)
    try:
        requested_limit = int(max_bytes) if max_bytes is not None else configured_limit
    except (TypeError, ValueError):
        requested_limit = configured_limit
    limit = min(max(requested_limit, 1), configured_limit)
    try:
        clean_offset = max(int(offset or 0), 0)
    except (TypeError, ValueError):
        clean_offset = 0
    size_bytes = target.stat().st_size
    with target.open("rb") as handle:
        handle.seek(min(clean_offset, size_bytes))
        data = handle.read(limit)
    if b"\x00" in data:
        raise HTTPException(status_code=415, detail="unsupported binary file")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=415, detail="unsupported binary file") from exc
    next_offset = min(clean_offset + len(data), size_bytes)
    return {
        "path": str(target),
        "relative_path": str(target.relative_to(runtime.shared_root)),
        "content": content,
        "offset": clean_offset,
        "bytes_read": len(data),
        "next_offset": next_offset,
        "size_bytes": size_bytes,
        "truncated": next_offset < size_bytes,
    }


def workspace_draft_snapshot_identity(source_path: Path, shared_root: Path) -> tuple[str, str, str]:
    relative_source = source_path.relative_to(shared_root)
    suffix = source_path.suffix or ".txt"
    source_stem = safe_filename(str(relative_source.with_suffix("")))[:140]
    source_hash = hashlib.sha256(str(relative_source).encode("utf-8")).hexdigest()[:12]
    return source_stem, source_hash, suffix


def workspace_draft_snapshot_path(source_path: Path, shared_root: Path) -> Path:
    source_stem, source_hash, suffix = workspace_draft_snapshot_identity(source_path, shared_root)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return shared_root / ".cache" / "docs" / f"{timestamp}-{source_stem}-{source_hash}{suffix}"


def workspace_latest_draft(source_path: Path, shared_root: Path) -> Optional[Path]:
    draft_dir = shared_root / ".cache" / "docs"
    if not draft_dir.is_dir():
        return None
    source_stem, source_hash, suffix = workspace_draft_snapshot_identity(source_path, shared_root)
    matching = [
        path
        for path in draft_dir.iterdir()
        if path.is_file() and path.name.endswith(f"-{source_stem}-{source_hash}{suffix}")
    ]
    return max(matching, default=None, key=lambda path: path.name)


def workspace_matching_drafts(source_path: Path, shared_root: Path) -> list[Path]:
    draft_dir = shared_root / ".cache" / "docs"
    if not draft_dir.is_dir():
        return []
    source_stem, source_hash, suffix = workspace_draft_snapshot_identity(source_path, shared_root)
    return [
        path
        for path in draft_dir.iterdir()
        if path.is_file() and path.name.endswith(f"-{source_stem}-{source_hash}{suffix}")
    ]


def content_disposition(filename: str, disposition: str = "attachment") -> str:
    safe = filename.replace("\\", "_").replace("/", "_") or "download"
    return f"{disposition}; filename*=UTF-8''{quote(safe)}"


def zip_relative_name(path: Path, source: Path) -> str:
    if path == source:
        return path.name
    return str(Path(source.name) / path.relative_to(source)).replace("\\", "/")


def write_zip(source: Path, archive: zipfile.ZipFile) -> None:
    if source.is_dir():
        for item in sorted(source.rglob("*")):
            if item.is_dir():
                continue
            archive.write(item, zip_relative_name(item, source))
    else:
        archive.write(source, source.name)


def workspace_zip_bytes(source: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        write_zip(source, archive)
    return buffer.getvalue()


def workspace_default_script_command(path: str) -> list[str]:
    name = Path(path).name
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return ["python", name]
    if suffix in {".sh", ".bash"}:
        return ["bash", name]
    if suffix == ".zsh":
        return ["zsh", name]
    if suffix in {".js", ".mjs", ".cjs"}:
        return ["node", name]
    if suffix == ".rb":
        return ["ruby", name]
    if suffix == ".php":
        return ["php", name]
    return []


def workspace_script_transcript_path(runtime: ToolRuntime, source_path: Optional[str]) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = safe_filename(source_path or "script-run")[:120] or "script-run"
    return runtime.shared_root / "script-runs" / f"{timestamp}-{label}.md"


def markdown_fence(value: Any) -> str:
    text = "" if value is None else str(value)
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}text\n{text}\n{fence}"


def workspace_script_transcript(
    *,
    command: list[str],
    workdir: str,
    result: dict[str, Any],
    started_at: datetime,
    finished_at: datetime,
    source_path: Optional[str],
    error: Optional[str],
) -> str:
    command_line = " ".join(shlex.quote(part) for part in command)
    duration_ms = result.get("duration_ms")
    metadata = [
        "# Script Run",
        "",
        f"- Command: `{command_line}`",
        f"- Workdir: `{workdir or '.'}`",
        f"- Source file: `{source_path}`" if source_path else "- Source file: none",
        f"- Started: `{started_at.isoformat()}`",
        f"- Finished: `{finished_at.isoformat()}`",
        f"- Duration: `{duration_ms} ms`" if duration_ms is not None else "- Duration: unknown",
        f"- Exit code: `{result.get('exit_code')}`",
        f"- Timed out: `{bool(result.get('timed_out'))}`",
        f"- Sandbox isolation: `{result.get('isolation_mode') or 'unknown'}`",
    ]
    if result.get("run_id"):
        metadata.append(f"- Sandbox run id: `{result.get('run_id')}`")
    if result.get("image"):
        metadata.append(f"- Sandbox image: `{result.get('image')}`")
    if error:
        metadata.append(f"- Error: `{error}`")
    return "\n".join(
        [
            *metadata,
            "",
            "## stdout",
            "",
            markdown_fence(result.get("stdout", "")),
            "",
            "## stderr",
            "",
            markdown_fence(result.get("stderr", "")),
            "",
        ]
    )


def safe_zip_destination(root: Path, member_name: str) -> Path:
    member = Path(member_name)
    if member.is_absolute() or any(part in ("", ".", "..") for part in member.parts):
        raise HTTPException(status_code=400, detail="zip contains unsafe paths")
    destination = (root / member).resolve()
    root_resolved = root.resolve()
    if destination != root_resolved and root_resolved not in destination.parents:
        raise HTTPException(status_code=400, detail="zip contains unsafe paths")
    return destination


def workspace_job_metadata(
    request: WorkspaceJobRequest,
    parent_job_id: Optional[int] = None,
    source: str = "workspace",
    extra_metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "source": source,
        "workspace_message": request.message,
        "workspace_active_path": request.active_path or "",
        "workspace_include_active_file": request.include_active_file,
        "workspace_include_file_content": request.include_file_content,
    }
    if parent_job_id is not None:
        metadata["parent_job_id"] = parent_job_id
    if extra_metadata:
        metadata.update(extra_metadata)
    return metadata


def workspace_job_body(request: WorkspaceJobRequest) -> str:
    parts = [request.message.strip()]
    if request.include_active_file and request.active_path:
        parts.append(f"\nActive workspace file: {request.active_path}")
    if request.include_file_content and request.active_path and request.file_content:
        if len(request.file_content) <= 25000:
            parts.append(f"\nActive file content:\n\n```\n{request.file_content}\n```")
        else:
            parts.append("\nThe active file is large. Use file_read on the active path instead of relying on pasted content.")
    return "\n".join(parts)


def create_workspace_job(
    request: WorkspaceJobRequest,
    parent_job_id: Optional[int] = None,
    thread_id: Optional[str] = None,
    source: str = "workspace",
    subject_override: Optional[str] = None,
    extra_metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    subject = subject_override or (f"Workspace: {request.active_path}" if request.active_path else "Workspace chat")
    job = db.create_manual_job(
        subject,
        workspace_job_body(request),
        "workspace@local",
        agent_address=agent_email(config),
        message_domain=message_id_domain(config),
        thread_id=thread_id,
    )
    metadata = workspace_job_metadata(request, parent_job_id=parent_job_id, source=source, extra_metadata=extra_metadata)
    db.execute(
        "UPDATE jobs SET metadata = metadata || %s, updated_at = now() WHERE id = %s",
        (Jsonb(json_safe(metadata)), job["id"]),
    )
    updated = db.fetch_one("SELECT * FROM jobs WHERE id = %s", (job["id"],))
    return updated or job


def render_ui_page(filename: str) -> str:
    try:
        return render_configured_ui_page(filename, config)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="ui page not found") from exc


def memory_row(memory_id: int) -> Optional[dict[str, Any]]:
    return db.fetch_one(
        f"""
        SELECT {MEMORY_COLUMNS},
               (expires_at IS NOT NULL AND expires_at <= now()) AS expired
        FROM agent_memories
        WHERE id = %s
        """,
        (memory_id,),
    )


def memory_events(memory_id: int, limit: int = 50) -> list[dict[str, Any]]:
    return db.fetch_all(
        """
        SELECT *
        FROM memory_events
        WHERE memory_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT %s
        """,
        (memory_id, min(max(limit, 1), 200)),
    )


def note_store() -> NoteStore:
    return NoteStore(db, config)


def note_row(note_id: int) -> Optional[dict[str, Any]]:
    return db.fetch_one(f"SELECT {NOTE_COLUMNS} FROM agent_notes WHERE id = %s", (note_id,))


def note_events(note_id: int, limit: int = 50) -> list[dict[str, Any]]:
    return db.fetch_all(
        """
        SELECT *
        FROM note_events
        WHERE note_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT %s
        """,
        (note_id, min(max(limit, 1), 200)),
    )


def raise_memory_error(exc: ValueError) -> None:
    message = str(exc)
    if "not found" in message:
        raise HTTPException(status_code=404, detail=message) from exc
    raise HTTPException(status_code=400, detail=message) from exc


def raise_contact_error(exc: ValueError) -> None:
    message = str(exc)
    if "not found" in message:
        raise HTTPException(status_code=404, detail=message) from exc
    if "already exists" in message:
        raise HTTPException(status_code=409, detail=message) from exc
    raise HTTPException(status_code=400, detail=message) from exc


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    if not config.get_bool("agent.api.dashboard_enabled", True):
        raise HTTPException(status_code=404, detail="not found")
    return render_ui_page("admin.html")


@app.get("/admin", response_class=HTMLResponse)
def admin_index() -> str:
    if not config.get_bool("agent.api.dashboard_enabled", True):
        raise HTTPException(status_code=404, detail="not found")
    return render_ui_page("admin.html")


@app.get("/workspace", response_class=HTMLResponse)
def workspace_index() -> str:
    if not config.get_bool("agent.api.workspace_enabled", True):
        raise HTTPException(status_code=404, detail="not found")
    return render_ui_page("workspace.html")


@app.get("/chat", response_class=HTMLResponse)
def chat_index() -> str:
    if not config.get_bool("agent.api.workspace_enabled", True):
        raise HTTPException(status_code=404, detail="not found")
    return render_ui_page("chat.html")


@app.get("/manifest.webmanifest", include_in_schema=False)
def chat_manifest() -> Response:
    if not config.get_bool("agent.api.workspace_enabled", True):
        raise HTTPException(status_code=404, detail="not found")
    content = (UI_ROOT / "manifest.webmanifest").read_text(encoding="utf-8")
    content = content.replace("__APP_TITLE__", app_display_name(config))
    return Response(content, media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def chat_service_worker() -> FileResponse:
    if not config.get_bool("agent.api.workspace_enabled", True):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(UI_ROOT / "sw.js", media_type="text/javascript")


@app.post("/api/chat/sessions/{session_id}/messages")
def chat_send_message(session_id: str, request: ChatMessageRequest) -> StreamingResponse:
    if not config.get_bool("agent.api.workspace_enabled", True):
        raise HTTPException(status_code=404, detail="not found")
    store = chat_store()
    limit = config.get_int("agent.chat.rate_limit_per_minute", 20)
    if store.count_recent_user_messages(60) >= limit:
        raise HTTPException(status_code=429, detail="slow down — too many messages, try again in a minute")

    is_new = session_id == "new"
    if is_new:
        session = store.create_session(store.title_from_message(request.message))
    else:
        try:
            numeric_id = int(session_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="chat session not found")
        session = store.get_session(numeric_id)
        if session is None:
            raise HTTPException(status_code=404, detail="chat session not found")

    max_history = config.get_int("agent.chat.max_history_messages", 20)
    history = store.recent_messages(session["id"], max_history)
    if history:
        latest = history[-1]
        if latest.get("kind") == "job_ref" and latest.get("job_id"):
            job = db.fetch_one("SELECT status FROM jobs WHERE id = %s", (latest["job_id"],))
            if job and job["status"] in {"queued", "running", "waiting"}:
                raise HTTPException(status_code=409, detail="a job is still processing for this session")

    user_message = request.message.strip()
    store.create_message(session["id"], "user", kind="chat", content=user_message)

    def stream() -> Iterator[str]:
        if is_new:
            yield sse_pack({"type": "session", "session_id": session["id"]})
        for event in chat_message_events(session, history, user_message):
            yield sse_pack(event)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/chat/sessions")
def chat_sessions(limit: int = 50) -> dict[str, Any]:
    clean_limit = min(max(limit, 1), 100)
    return {"sessions": chat_store().list_sessions(clean_limit)}


@app.get("/api/chat/sessions/{session_id}/messages")
def chat_session_messages(session_id: int) -> dict[str, Any]:
    store = chat_store()
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="chat session not found")
    return {"session": session, "messages": store.list_messages(session_id)}


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    rows = db.fetch_all("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status ORDER BY status")
    projects = db.fetch_all("SELECT status, COUNT(*) AS count FROM projects GROUP BY status ORDER BY status")
    reminders = db.fetch_all("SELECT status, COUNT(*) AS count FROM reminders GROUP BY status ORDER BY status")
    deep_research = db.fetch_all("SELECT status, COUNT(*) AS count FROM deep_research_runs GROUP BY status ORDER BY status")
    emails = db.fetch_one("SELECT COUNT(*) AS count FROM emails")
    logs = db.fetch_one("SELECT COUNT(*) AS count FROM task_logs")
    contacts = db.fetch_one("SELECT COUNT(*) AS count FROM contacts")
    memories = db.fetch_one(
        """
        SELECT
          COUNT(*) AS count,
          COUNT(*) FILTER (WHERE expires_at IS NULL OR expires_at > now()) AS active_count,
          COUNT(*) FILTER (WHERE expires_at IS NOT NULL AND expires_at <= now()) AS expired_count
        FROM agent_memories
        """
    )
    return {
        "jobs": rows,
        "projects": projects,
        "reminders": reminders,
        "deep_research": deep_research,
        "costs": usage_cost_summary(),
        "email_count": emails["count"],
        "log_count": logs["count"],
        "contact_count": contacts["count"],
        "memory_count": memories["count"],
        "active_memory_count": memories["active_count"],
        "expired_memory_count": memories["expired_count"],
    }


@app.get("/api/config/status")
def config_status() -> dict[str, Any]:
    return tool_status(config)


@app.get("/api/workspace/tree")
def workspace_tree(path: str = ".", max_entries: int = 500, recursive: bool = False) -> dict[str, Any]:
    try:
        result = workspace_runtime().file_list(path=path, recursive=recursive, max_entries=max_entries)
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)
    conversion_rows = workspace_latest_conversion_rows([str(item["relative_path"]) for item in result["entries"]])
    entries = [
        workspace_file_metadata(Path(item["path"]), conversion_rows.get(str(item["relative_path"])))
        for item in result["entries"]
    ]
    return {"root": path, "entries": entries, "truncated": result["truncated"]}


@app.get("/api/workspace/version")
def workspace_version(max_entries: int = 10000) -> dict[str, Any]:
    runtime = workspace_runtime()
    clean_limit = min(max(max_entries, 100), 50000)
    try:
        return workspace_directory_version(runtime, max_entries=clean_limit)
    except OSError as exc:
        raise_workspace_error(exc)


@app.get("/api/workspace/metadata")
def workspace_metadata(path: str) -> dict[str, Any]:
    runtime = workspace_runtime()
    try:
        target = runtime.resolve_path(path)
        if not target.exists():
            raise HTTPException(status_code=404, detail="path not found")
        if runtime.is_tool_cache_path(target):
            runtime.check_cache_read_allowed(target)
        relative = str(target.relative_to(runtime.shared_root))
        return {"item": workspace_file_metadata(target, workspace_latest_conversion_row(relative))}
    except HTTPException:
        raise
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)


@app.get("/api/workspace/file")
def workspace_file(path: str, max_bytes: Optional[int] = None, offset: int = 0) -> dict[str, Any]:
    runtime = workspace_runtime()
    try:
        target = runtime.resolve_path(path)
        result = read_workspace_text_file(runtime, target, max_bytes, offset)
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)
    result["mtime_ns"] = str(target.stat().st_mtime_ns)
    return result


@app.put("/api/workspace/file")
def workspace_file_write(request: WorkspaceFileWriteRequest) -> dict[str, Any]:
    runtime = workspace_runtime()
    try:
        target = runtime.resolve_path(request.path)
        if request.create_only and target.exists():
            raise HTTPException(status_code=409, detail="file already exists")
        if (
            request.expected_mtime_ns is not None
            and target.exists()
            and str(target.stat().st_mtime_ns) != str(request.expected_mtime_ns)
        ):
            raise HTTPException(status_code=409, detail="file changed since it was opened")
        result = runtime.file_write(path=request.path, content=request.content)
        canonical = runtime.resolve_path(result.get("relative_path") or request.path)
        return {"file": workspace_file_metadata(canonical)}
    except HTTPException:
        raise
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)


@app.post("/api/workspace/drafts")
def workspace_draft_snapshot(request: WorkspaceDraftSnapshotRequest) -> dict[str, Any]:
    runtime = workspace_runtime()
    try:
        source = runtime.resolve_path(request.path)
        destination = workspace_draft_snapshot_path(source, runtime.shared_root)
        runtime.check_write_allowed(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(request.content, encoding="utf-8")
        return {"draft": workspace_file_metadata(destination)}
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)


@app.get("/api/workspace/drafts/latest")
def workspace_draft_latest(path: str) -> dict[str, Any]:
    runtime = workspace_runtime()
    try:
        source = runtime.resolve_path(path)
        draft = workspace_latest_draft(source, runtime.shared_root)
        if draft is None:
            return {"draft": None}
        return {
            "draft": {
                **workspace_file_metadata(draft),
                "content": draft.read_text(encoding="utf-8", errors="replace"),
            }
        }
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)


@app.delete("/api/workspace/drafts")
def workspace_draft_delete(path: str) -> dict[str, Any]:
    runtime = workspace_runtime()
    try:
        source = runtime.resolve_path(path)
        deleted = 0
        for draft in workspace_matching_drafts(source, runtime.shared_root):
            runtime.check_delete_allowed(draft)
            draft.unlink()
            deleted += 1
        return {"deleted": deleted}
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)


@app.post("/api/workspace/folders")
def workspace_folder_create(request: WorkspaceFolderCreateRequest) -> dict[str, Any]:
    runtime = workspace_runtime()
    try:
        target = runtime.resolve_path(request.path)
        runtime.check_write_allowed(target)
        target.mkdir(parents=True, exist_ok=True)
        return {"folder": workspace_file_metadata(target)}
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)


@app.patch("/api/workspace/path")
def workspace_path_move(request: WorkspacePathOperationRequest) -> dict[str, Any]:
    runtime = workspace_runtime()
    try:
        result = runtime.file_move(source_path=request.source_path, destination_path=request.destination_path)
        target = runtime.resolve_path(result["relative_path"])
        return {"item": workspace_file_metadata(target)}
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)


@app.post("/api/workspace/copy")
def workspace_path_copy(request: WorkspacePathOperationRequest) -> dict[str, Any]:
    runtime = workspace_runtime()
    try:
        result = runtime.file_copy(source_path=request.source_path, destination_path=request.destination_path)
        target = runtime.resolve_path(result["relative_path"])
        return {"item": workspace_file_metadata(target)}
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)


@app.post("/api/workspace/convert")
def workspace_file_convert(request: WorkspaceConvertRequest) -> dict[str, Any]:
    runtime = workspace_runtime()
    try:
        result = runtime.file_convert(
            path=request.path,
            output_format=request.output_format,
            delete_original=request.delete_original,
        )
        target = runtime.resolve_path(result["relative_path"])
        response: dict[str, Any] = {
            "file": workspace_file_metadata(target),
            "conversion": result,
        }
        if result.get("deleted_original"):
            response["deleted_original"] = result["deleted_original"]
        return response
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)


@app.put("/api/workspace/upload")
async def workspace_upload(path: str, request: Request) -> dict[str, Any]:
    runtime = workspace_runtime()
    # Default 500 MB; set to 0 in config to disable the limit.
    max_bytes = config.get_int("agent.api.max_upload_bytes", 524288000)
    try:
        target = runtime.resolve_path(path)
        if target.exists():
            raise HTTPException(status_code=409, detail="destination already exists")
        runtime.check_write_allowed(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            bytes_received = 0
            with target.open("wb") as handle:
                async for chunk in request.stream():
                    if chunk:
                        bytes_received += len(chunk)
                        if max_bytes > 0 and bytes_received > max_bytes:
                            raise HTTPException(
                                status_code=413,
                                detail="upload exceeds maximum allowed size of %d bytes" % max_bytes,
                            )
                        handle.write(chunk)
        except Exception:
            if target.exists():
                target.unlink()
            raise
        canonical = normalize_and_index_workspace_path(target, source="upload")
        return {"file": workspace_file_metadata(canonical)}
    except HTTPException:
        raise
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)


@app.get("/api/workspace/download")
def workspace_download(path: str) -> StreamingResponse:
    runtime = workspace_runtime()
    try:
        target = runtime.resolve_path(path)
        if not target.exists():
            raise HTTPException(status_code=404, detail="path not found")
        runtime.check_cache_read_allowed(target)
        if target.is_dir():
            filename = f"{target.name or 'workspace'}.zip"
            payload = workspace_zip_bytes(target)
            media_type = "application/zip"
        else:
            filename = target.name
            payload = target.read_bytes()
            media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return StreamingResponse(
            io.BytesIO(payload),
            media_type=media_type,
            headers={"Content-Disposition": content_disposition(filename)},
        )
    except HTTPException:
        raise
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)


@app.post("/api/workspace/zip")
def workspace_zip(request: WorkspaceArchiveRequest) -> dict[str, Any]:
    runtime = workspace_runtime()
    try:
        source = runtime.resolve_path(request.path)
        if not source.exists():
            raise HTTPException(status_code=404, detail="path not found")
        runtime.check_cache_read_allowed(source)
        destination_path = request.destination_path or f"{request.path.rstrip('/')}.zip"
        destination = runtime.resolve_path(destination_path)
        if destination.exists():
            raise HTTPException(status_code=409, detail="destination already exists")
        runtime.check_write_allowed(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            write_zip(source, archive)
        normalize_and_index_workspace_path(destination, source="dashboard")
        return {"file": workspace_file_metadata(destination)}
    except HTTPException:
        raise
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)


@app.post("/api/workspace/unzip")
def workspace_unzip(request: WorkspaceExtractRequest) -> dict[str, Any]:
    runtime = workspace_runtime()
    try:
        source = runtime.resolve_path(request.path)
        if not source.is_file() or source.suffix.lower() != ".zip":
            raise HTTPException(status_code=400, detail="path is not a zip file")
        runtime.check_cache_read_allowed(source)
        destination_root = runtime.resolve_path(request.destination_folder or str(Path(request.path).with_suffix("")))
        if destination_root.exists() and any(destination_root.iterdir()):
            raise HTTPException(status_code=409, detail="destination folder is not empty")
        runtime.check_write_allowed(destination_root)
        destination_root.mkdir(parents=True, exist_ok=True)
        extracted: list[dict[str, Any]] = []
        with zipfile.ZipFile(source, "r") as archive:
            for member in archive.infolist():
                destination = safe_zip_destination(destination_root, member.filename)
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                runtime.check_write_allowed(destination)
                if destination.exists():
                    raise HTTPException(status_code=409, detail="zip extraction would overwrite an existing file")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as src, destination.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                canonical = normalize_and_index_workspace_path(destination, source="upload")
                extracted.append(workspace_file_metadata(canonical))
        return {"folder": workspace_file_metadata(destination_root), "files": extracted}
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="invalid zip file") from exc
    except HTTPException:
        raise
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)


@app.delete("/api/workspace/file")
def workspace_file_delete(path: str) -> dict[str, Any]:
    runtime = workspace_runtime()
    try:
        target = runtime.resolve_path(path)
        if runtime.path_is_or_is_under(target, runtime.trash_dir) and target.resolve() != runtime.trash_dir:
            runtime.check_delete_allowed(target)
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            return {"deleted_path": str(target), "permanent": True}
        return runtime.file_delete(path=path)
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)


@app.get("/api/workspace/search")
def workspace_search(q: str, directory: str = ".", max_results: int = 100, include_dirs: bool = True) -> dict[str, Any]:
    runtime = workspace_runtime()
    shared_root = Path(config.get("agent.filesystem.shared_root", "/data/share")).resolve()
    try:
        result = runtime.file_search(pattern=q, directory=directory, max_results=max_results, include_dirs=include_dirs)
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)
    matches = []
    entries = []
    paths = []
    for match in result["matches"]:
        path = Path(match)
        try:
            relative = str(path.relative_to(shared_root))
        except ValueError:
            continue
        matches.append(relative)
        paths.append((path, relative))
    conversion_rows = workspace_latest_conversion_rows([relative for _path, relative in paths])
    for path, relative in paths:
        entries.append(workspace_file_metadata(path, conversion_rows.get(relative)))
    return {"matches": matches, "entries": entries, "truncated": result["truncated"]}


@app.get("/api/workspace/semantic-search")
def workspace_semantic_search(q: str, directory: str = ".", max_results: int = 10) -> dict[str, Any]:
    try:
        rows = WorkspaceIndex(db, config).search(query=q, directory=directory, limit=max_results)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"matches": rows}


@app.get("/api/workspace/jobs")
def workspace_jobs(limit: int = 50) -> dict[str, Any]:
    clean_limit = min(max(limit, 1), 100)
    rows = db.fetch_all(
        JOB_LIST_SELECT
        + """
        WHERE j.metadata->>'source' = 'workspace'
        ORDER BY j.created_at DESC
        LIMIT %s
        """,
        (clean_limit,),
    )
    return {"jobs": rows}


@app.post("/api/workspace/jobs")
def workspace_job_create(request: WorkspaceJobRequest) -> dict[str, Any]:
    return {"job": create_workspace_job(request)}


@app.get("/api/workspace/jobs/{job_id}")
def workspace_job_detail(job_id: int) -> dict[str, Any]:
    job = db.fetch_one("SELECT * FROM jobs WHERE id = %s AND metadata->>'source' = 'workspace'", (job_id,))
    if job is None:
        raise HTTPException(status_code=404, detail="workspace job not found")
    detail = job_detail(job_id)
    return detail


@app.post("/api/workspace/jobs/{job_id}/messages")
def workspace_job_message(job_id: int, request: WorkspaceJobRequest) -> dict[str, Any]:
    parent = db.fetch_one("SELECT * FROM jobs WHERE id = %s AND metadata->>'source' = 'workspace'", (job_id,))
    if parent is None:
        raise HTTPException(status_code=404, detail="workspace job not found")
    if parent["status"] in {"queued", "running", "waiting"}:
        raise HTTPException(status_code=409, detail="job is still processing")
    return {"job": create_workspace_job(request, parent_job_id=job_id, thread_id=parent.get("thread_id"))}


@app.post("/api/workspace/script-runs")
def workspace_script_run(request: WorkspaceScriptRunRequest) -> dict[str, Any]:
    runtime = workspace_runtime()
    source_path = request.path.strip() if request.path else None
    try:
        if source_path:
            source = runtime.resolve_path(source_path)
            if not source.is_file():
                raise HTTPException(status_code=400, detail="script path must be a file")
            workdir = request.workdir or str(source.parent.relative_to(runtime.shared_root))
        else:
            workdir = request.workdir or "."
        resolved_workdir = runtime.resolve_path(workdir)
        if not resolved_workdir.is_dir():
            raise HTTPException(status_code=400, detail="workdir is not a directory")
    except HTTPException:
        raise
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)

    command = [str(part).strip() for part in request.command if str(part).strip()]
    if not command and source_path:
        command = workspace_default_script_command(source_path)
    if not command:
        raise HTTPException(status_code=400, detail="command is required")

    started_at = datetime.now(timezone.utc)
    error: Optional[str] = None
    try:
        result = runtime.command_execute(command, timeout_seconds=request.timeout_seconds, workdir=workdir)
    except (ToolError, OSError, TypeError, ValueError) as exc:
        error = str(exc)
        result = {
            "exit_code": None,
            "stdout": "",
            "stderr": error,
            "duration_ms": int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000),
            "timed_out": False,
            "isolation_mode": None,
        }
    finished_at = datetime.now(timezone.utc)

    try:
        destination = workspace_script_transcript_path(runtime, source_path or " ".join(command))
        runtime.check_write_allowed(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            workspace_script_transcript(
                command=command,
                workdir=workdir,
                result=result,
                started_at=started_at,
                finished_at=finished_at,
                source_path=source_path,
                error=error,
            ),
            encoding="utf-8",
        )
    except (OSError, ToolError) as exc:
        raise_workspace_error(exc)

    return {
        "file": workspace_file_metadata(destination),
        "result": result,
        "command": command,
        "workdir": workdir,
        "error": error,
    }


@app.get("/api/jobs")
def jobs(status: Optional[str] = None, limit: int = 50) -> dict[str, Any]:
    limit = min(max(limit, 1), 200)
    if status:
        rows = db.fetch_all(
            JOB_LIST_SELECT
            + """
            WHERE status = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (status, limit),
        )
    else:
        rows = db.fetch_all(JOB_LIST_SELECT + "ORDER BY created_at DESC LIMIT %s", (limit,))
    return {"jobs": rows}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: int) -> dict[str, Any]:
    job = db.fetch_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    cutoff = job_context_cutoff(job)
    trigger_email = None
    if job.get("trigger_email_id"):
        trigger_email = db.fetch_one("SELECT * FROM emails WHERE id = %s", (job["trigger_email_id"],))
    emails = db.latest_thread_emails(job["thread_id"], limit=20, through=cutoff)
    thread_messages = db.latest_thread_messages(job["thread_id"], limit=20, through=cutoff)
    for item in thread_messages:
        if item.get("context_type") == "outbound_email":
            item["from_address"] = agent_email(config)
    logs = db.fetch_all(
        "SELECT * FROM task_logs WHERE job_id = %s ORDER BY sequence ASC",
        (job_id,),
    )
    return {
        "job": job,
        "usage": job_usage_summary(job_id),
        "trigger_email": trigger_email,
        "thread_messages": thread_messages,
        "thread_emails": emails,
        "emails": emails,
        "logs": logs,
        "actions": job_actions(job),
        "review_defaults": {
            "max_iterations_per_task": config.get_int("agent.limits.max_iterations_per_task", 50),
            "max_tokens_per_task": config.get_int("agent.limits.max_tokens_per_task", 1000000),
        },
        "review_diagnostics": compute_review_diagnostics(db, config, job) if job.get("status") == "needs_review" else None,
    }


@app.post("/api/jobs")
def create_job(request: ManualJobRequest) -> dict[str, Any]:
    job = db.create_manual_job(
        request.subject,
        request.body,
        request.from_address,
        agent_address=agent_email(config),
        message_domain=message_id_domain(config),
    )
    return {"job": job}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int) -> dict[str, str]:
    row = db.fetch_one("SELECT id, status FROM jobs WHERE id = %s", (job_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not job_actions(row)["can_cancel"]:
        raise HTTPException(status_code=409, detail="job cannot be cancelled from its current status")
    db.update_job_status(job_id, "cancelled", last_error="cancelled from dashboard")
    return {"status": "cancelled"}


@app.post("/api/jobs/{job_id}/requeue")
def requeue_job(job_id: int) -> dict[str, str]:
    row = db.fetch_one("SELECT id, status FROM jobs WHERE id = %s", (job_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not job_actions(row)["can_requeue"]:
        raise HTTPException(status_code=409, detail="job cannot be requeued from its current status")
    db.execute(
        """
        UPDATE jobs
        SET status = 'queued',
            run_at = now(),
            locked_at = NULL,
            locked_by = NULL,
            last_error = NULL,
            updated_at = now()
        WHERE id = %s
        """,
        (job_id,),
    )
    db.log_event(job_id, "status_change", output_data={"status": "queued", "reason": "requeued from dashboard"})
    return {"status": "queued"}


@app.post("/api/jobs/{job_id}/review-override")
def review_override_job(job_id: int, request: JobReviewOverrideRequest) -> dict[str, str]:
    row = db.fetch_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    if row["status"] != "needs_review":
        raise HTTPException(status_code=409, detail="job is not waiting for admin review")

    instruction = str(request.instruction or "").strip()
    override: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": "dashboard",
        "reason": row.get("last_error") or "",
    }
    if request.max_iterations_per_task is not None:
        override["max_iterations_per_task"] = request.max_iterations_per_task
    if request.max_tokens_per_task is not None:
        override["max_tokens_per_task"] = request.max_tokens_per_task
    if instruction:
        override["instruction"] = instruction

    db.execute(
        """
        UPDATE jobs
        SET metadata = metadata || %s,
            updated_at = now()
        WHERE id = %s
        """,
        (Jsonb(json_safe({"admin_review_override": override})), job_id),
    )
    if instruction:
        db.execute(
            "INSERT INTO supervisor_instructions(job_id, instruction, created_by) VALUES (%s, %s, %s)",
            (job_id, instruction, "dashboard"),
        )
        db.log_event(job_id, "supervisor_note", output_data={"instruction": instruction, "created_by": "dashboard"})

    if request.requeue:
        db.execute(
            """
            UPDATE jobs
            SET status = 'queued',
                run_at = now(),
                locked_at = NULL,
                locked_by = NULL,
                last_error = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (job_id,),
        )
        db.log_event(
            job_id,
            "status_change",
            output_data={"status": "queued", "reason": "admin review override", "override": override},
        )
        return {"status": "queued"}

    db.log_event(job_id, "admin_review", output_data={"status": "needs_review", "override": override})
    return {"status": "needs_review"}


@app.get("/api/jobs/{job_id}/poll")
def job_poll(job_id: int, after_sequence: int = 0) -> dict[str, Any]:
    job = db.fetch_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    after = max(int(after_sequence), 0)
    new_logs = db.fetch_all(
        "SELECT * FROM task_logs WHERE job_id = %s AND sequence > %s ORDER BY sequence ASC LIMIT 200",
        (job_id, after),
    )
    job_subset = {
        k: job[k]
        for k in ("id", "status", "last_error", "task_summary", "updated_at", "locked_at", "attempts", "completed_at", "metadata")
        if k in job
    }
    return {
        "job": job_subset,
        "usage": job_usage_summary(job_id),
        "actions": job_actions(job),
        "new_logs": new_logs,
    }


@app.post("/api/jobs/{job_id}/instructions")
def add_instruction(job_id: int, request: InstructionRequest) -> dict[str, str]:
    row = db.fetch_one("SELECT id, status FROM jobs WHERE id = %s", (job_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    db.execute(
        "INSERT INTO supervisor_instructions(job_id, instruction, created_by) VALUES (%s, %s, %s)",
        (job_id, request.instruction, "dashboard"),
    )
    db.log_event(job_id, "supervisor_note", output_data={"instruction": request.instruction, "created_by": "dashboard"})
    
    # Automatically requeue jobs in terminal states so the instruction can be processed
    if row["status"] in ("completed", "failed", "cancelled"):
        db.execute(
            """
            UPDATE jobs
            SET status = 'queued',
                run_at = now(),
                locked_at = NULL,
                locked_by = NULL,
                last_error = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (job_id,),
        )
        db.log_event(job_id, "status_change", output_data={"status": "queued", "reason": "requeued by admin instruction"})
        return {"status": "requeued"}
    
    return {"status": "created"}


@app.delete("/api/jobs/{job_id}")
def erase_job(job_id: int) -> dict[str, Any]:
    try:
        summary = db.erase_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"erased": summary}


@app.get("/api/memories")
def memories(
    query: Optional[str] = None,
    tag: Optional[str] = None,
    scope: Optional[str] = None,
    kind: Optional[str] = None,
    pinned: Optional[bool] = None,
    include_expired: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    limit = min(max(limit, 1), 200)
    store = memory_store()
    filters: list[str] = []
    params: list[Any] = []
    clean_query = str(query or "").strip()
    clean_tag = str(tag or "").strip()
    clean_scope = str(scope or "").strip()
    clean_kind = str(kind or "").strip()
    if not include_expired:
        filters.append(store.active_filter())
    if clean_query:
        pattern = "%%%s%%" % clean_query
        filters.append(
            """
            (content ILIKE %s
             OR scope ILIKE %s
             OR kind ILIKE %s
             OR EXISTS (
               SELECT 1
               FROM unnest(tags) AS memory_tag(tag)
               WHERE memory_tag.tag ILIKE %s
             ))
            """
        )
        params.extend([pattern, pattern, pattern, pattern])
    if clean_tag:
        tags = store.clean_tags([clean_tag])
        if tags:
            filters.append("tags && %s")
            params.append(tags)
    if clean_scope:
        filters.append("scope = %s")
        params.append(store.clean_text_field(clean_scope, "global"))
    if clean_kind:
        filters.append("kind = %s")
        params.append(store.clean_text_field(clean_kind, "fact"))
    if pinned is not None:
        filters.append("pinned = %s")
        params.append(bool(pinned))
    where = "WHERE %s" % " AND ".join(filters) if filters else ""
    params.append(limit)
    rows = db.fetch_all(
        f"""
        SELECT {MEMORY_COLUMNS},
               (expires_at IS NOT NULL AND expires_at <= now()) AS expired
        FROM agent_memories
        {where}
        ORDER BY pinned DESC, importance DESC, updated_at DESC, created_at DESC
        LIMIT %s
        """,
        tuple(params),
    )
    return {"memories": rows}


@app.get("/api/memories/{memory_id}")
def memory_detail(memory_id: int) -> dict[str, Any]:
    row = memory_row(memory_id)
    if row is None:
        raise HTTPException(status_code=404, detail="memory not found")
    source_job = None
    if row.get("source_job_id"):
        source_job = db.fetch_one(
            "SELECT id, status, task_summary, created_at, updated_at FROM jobs WHERE id = %s",
            (row["source_job_id"],),
        )
    return {"memory": row, "events": memory_events(memory_id), "source_job": source_job}


@app.post("/api/memories")
def create_memory(request: MemoryCreateRequest) -> dict[str, Any]:
    try:
        row = memory_store().create(
            content=request.content,
            tags=request.tags,
            scope=request.scope,
            kind=request.kind,
            importance=request.importance,
            confidence=request.confidence,
            expires_at=request.expires_at,
            pinned=request.pinned,
            metadata=request.metadata,
            actor="dashboard",
        )
    except ValueError as exc:
        raise_memory_error(exc)
    return {"memory": memory_row(row["id"]) or row}


@app.patch("/api/memories/{memory_id}")
def update_memory(memory_id: int, request: MemoryUpdateRequest) -> dict[str, Any]:
    fields = model_fields_set(request)
    if not fields:
        raise HTTPException(status_code=400, detail="at least one memory field must be provided")
    if "content" in fields and not str(request.content or "").strip():
        raise HTTPException(status_code=400, detail="memory content cannot be empty")
    kwargs: dict[str, Any] = {}
    for name in ("content", "tags", "scope", "kind", "importance", "confidence", "pinned", "metadata"):
        if name in fields:
            kwargs[name] = getattr(request, name)
    if "expires_at" in fields:
        kwargs["expires_at"] = request.expires_at
    try:
        row = memory_store().update(memory_id=memory_id, actor="dashboard", include_expired=True, **kwargs)
    except ValueError as exc:
        raise_memory_error(exc)
    return {"memory": memory_row(row["id"]) or row}


@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: int) -> dict[str, Any]:
    try:
        row = memory_store().delete(
            memory_id=memory_id,
            reason="deleted from dashboard",
            actor="dashboard",
            include_expired=True,
        )
    except ValueError as exc:
        raise_memory_error(exc)
    return {"deleted": row}


@app.get("/api/notes")
def notes(query: Optional[str] = None, tag: Optional[str] = None, limit: int = 100) -> dict[str, Any]:
    store = note_store()
    tags = [tag] if tag else None
    rows = store.semantic_search(query=query or "", tags=tags, limit=min(max(limit, 1), 200))
    return {"notes": [store.public_search_row(row, query=query or "") for row in rows]}


@app.get("/api/notes/{note_id}")
def note_detail(note_id: int) -> dict[str, Any]:
    row = note_row(note_id)
    if row is None:
        raise HTTPException(status_code=404, detail="note not found")
    source_job = None
    if row.get("source_job_id"):
        source_job = db.fetch_one(
            "SELECT id, status, task_summary, created_at, updated_at FROM jobs WHERE id = %s",
            (row["source_job_id"],),
        )
    return {"note": row, "events": note_events(note_id), "source_job": source_job}


@app.post("/api/notes")
def create_note(request: NoteCreateRequest) -> dict[str, Any]:
    try:
        row = note_store().create(
            content=request.content,
            title=request.title,
            tags=request.tags,
            metadata=request.metadata,
            actor="dashboard",
        )
    except ValueError as exc:
        raise_memory_error(exc)
    return {"note": note_row(row["id"]) or row}


@app.patch("/api/notes/{note_id}")
def update_note(note_id: int, request: NoteUpdateRequest) -> dict[str, Any]:
    fields = model_fields_set(request)
    if not fields:
        raise HTTPException(status_code=400, detail="at least one note field must be provided")
    if "content" in fields and not str(request.content or "").strip():
        raise HTTPException(status_code=400, detail="note content cannot be empty")
    kwargs: dict[str, Any] = {}
    for name in ("content", "title", "tags"):
        if name in fields:
            kwargs[name] = getattr(request, name)
    if "metadata" in fields:
        kwargs["metadata"] = request.metadata
    else:
        kwargs["metadata"] = NOTE_UNSET
    try:
        row = note_store().update(note_id=note_id, actor="dashboard", **kwargs)
    except ValueError as exc:
        raise_memory_error(exc)
    return {"note": note_row(row["id"]) or row}


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: int) -> dict[str, Any]:
    try:
        row = note_store().delete(note_id=note_id, reason="deleted from dashboard", actor="dashboard")
    except ValueError as exc:
        raise_memory_error(exc)
    return {"deleted": row}


@app.get("/api/contacts")
def contacts(query: Optional[str] = None, limit: int = 100) -> dict[str, Any]:
    try:
        rows = contact_store().search(query=query or "", limit=limit)
    except ValueError as exc:
        raise_contact_error(exc)
    return {"contacts": rows}


@app.get("/api/contacts/{contact_id}")
def contact_detail(contact_id: int) -> dict[str, Any]:
    row = contact_store().get(contact_id)
    if row is None:
        raise HTTPException(status_code=404, detail="contact not found")
    return {"contact": row}


@app.post("/api/contacts")
def create_contact(request: ContactCreateRequest) -> dict[str, Any]:
    fields = {
        "first_name": request.first_name,
        "last_name": request.last_name,
        "email_address": request.email_address,
        "company": request.company,
        "title": request.title,
        "notes": request.notes,
    }
    try:
        row = contact_store().create(fields, source="dashboard")
    except ValueError as exc:
        raise_contact_error(exc)
    return {"contact": row}


@app.patch("/api/contacts/{contact_id}")
def update_contact(contact_id: int, request: ContactUpdateRequest) -> dict[str, Any]:
    fields = model_fields_set(request)
    if not fields:
        raise HTTPException(status_code=400, detail="at least one contact field must be provided")
    updates = {
        name: getattr(request, name)
        for name in ("first_name", "last_name", "email_address", "company", "title", "notes")
        if name in fields
    }
    try:
        row = contact_store().update(contact_id, updates)
    except ValueError as exc:
        raise_contact_error(exc)
    return {"contact": row}


@app.delete("/api/contacts/{contact_id}")
def delete_contact(contact_id: int) -> dict[str, Any]:
    try:
        row = contact_store().delete(contact_id)
    except ValueError as exc:
        raise_contact_error(exc)
    return {"deleted": row}


@app.get("/api/reminders")
def reminders(status: Optional[str] = None, limit: int = 100) -> dict[str, Any]:
    limit = min(max(limit, 1), 200)
    clean_status = clean_status_filter(status, REMINDER_STATUSES, "reminder")
    if clean_status:
        rows = db.fetch_all(
            """
            SELECT *
            FROM reminders
            WHERE status = %s
            ORDER BY
              CASE WHEN status IN ('scheduled', 'queued') THEN 0 ELSE 1 END,
              run_at ASC,
              id DESC
            LIMIT %s
            """,
            (clean_status, limit),
        )
    else:
        rows = db.fetch_all(
            """
            SELECT *
            FROM reminders
            ORDER BY
              CASE WHEN status IN ('scheduled', 'queued') THEN 0 ELSE 1 END,
              run_at ASC,
              id DESC
            LIMIT %s
            """,
            (limit,),
        )
    return {"reminders": [reminder_public(row) for row in rows]}


@app.get("/api/reminders/{reminder_id}")
def reminder_detail(reminder_id: int) -> dict[str, Any]:
    row = db.fetch_one("SELECT * FROM reminders WHERE id = %s", (reminder_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    created_by_job = None
    if row.get("created_by_job_id"):
        created_by_job = db.fetch_one(
            "SELECT id, status, task_summary, created_at, updated_at FROM jobs WHERE id = %s",
            (row["created_by_job_id"],),
        )
    linked_job = None
    if row.get("job_id"):
        linked_job = db.fetch_one(
            "SELECT id, status, task_summary, created_at, updated_at, completed_at, last_error FROM jobs WHERE id = %s",
            (row["job_id"],),
        )
    return {"reminder": reminder_public(row), "created_by_job": created_by_job, "job": linked_job}


@app.post("/api/reminders")
def create_reminder(request: ReminderCreateRequest) -> dict[str, Any]:
    try:
        run_at = parse_datetime(request.run_at, config)
        recurrence_unit, recurrence_interval, recurrence_anchor = recurrence_fields(
            run_at,
            request.recurrence_unit,
            request.recurrence_interval,
        )
    except ValueError as exc:
        raise_reminder_error(exc)
    row = db.fetch_one(
        """
        INSERT INTO reminders(
          title,
          task,
          run_at,
          priority,
          recurrence_unit,
          recurrence_interval,
          recurrence_anchor_day,
          created_by,
          metadata
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'dashboard', %s)
        RETURNING *
        """,
        (
            request.title.strip(),
            request.task.strip(),
            run_at,
            int(request.priority or 0),
            recurrence_unit,
            recurrence_interval,
            recurrence_anchor,
            Jsonb(json_safe(request.metadata or {})),
        ),
    )
    if row is None:
        raise HTTPException(status_code=500, detail="reminder was not created")
    db.execute(
        "INSERT INTO manual_events(event_type, payload) VALUES (%s, %s)",
        ("dashboard_reminder_created", Jsonb(json_safe({"reminder_id": row["id"]}))),
    )
    return {"reminder": reminder_public(row)}


@app.patch("/api/reminders/{reminder_id}")
def update_reminder(reminder_id: int, request: ReminderUpdateRequest) -> dict[str, Any]:
    fields = model_fields_set(request)
    if not fields:
        raise HTTPException(status_code=400, detail="at least one reminder field must be provided")
    existing = db.fetch_one("SELECT * FROM reminders WHERE id = %s", (reminder_id,))
    if existing is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    if existing["status"] != "scheduled":
        raise HTTPException(status_code=409, detail="only scheduled reminders can be edited")

    try:
        next_run_at = parse_datetime(request.run_at, config) if "run_at" in fields else existing["run_at"]
        next_recurrence_unit = existing.get("recurrence_unit")
        next_recurrence_interval = existing.get("recurrence_interval")
        next_recurrence_anchor = existing.get("recurrence_anchor_day")
        if "recurrence_unit" in fields:
            next_recurrence_unit = clean_recurrence_unit(request.recurrence_unit)
            if next_recurrence_unit is None:
                next_recurrence_interval = None
                next_recurrence_anchor = None
            else:
                next_recurrence_interval = clean_recurrence_interval(
                    request.recurrence_interval if "recurrence_interval" in fields else next_recurrence_interval
                )
                next_recurrence_anchor = recurrence_anchor_day(next_run_at, next_recurrence_unit, config)
        elif "recurrence_interval" in fields:
            if not next_recurrence_unit:
                raise ValueError("recurrence_unit is required when recurrence_interval is provided")
            next_recurrence_interval = clean_recurrence_interval(request.recurrence_interval)
            next_recurrence_anchor = recurrence_anchor_day(next_run_at, next_recurrence_unit, config)
        elif "run_at" in fields and next_recurrence_unit:
            next_recurrence_anchor = recurrence_anchor_day(next_run_at, next_recurrence_unit, config)
    except ValueError as exc:
        raise_reminder_error(exc)

    row = db.fetch_one(
        """
        UPDATE reminders
        SET title = %s,
            task = %s,
            run_at = %s,
            priority = %s,
            recurrence_unit = %s,
            recurrence_interval = %s,
            recurrence_anchor_day = %s,
            metadata = %s,
            updated_at = now()
        WHERE id = %s
          AND status = 'scheduled'
        RETURNING *
        """,
        (
            request.title.strip() if "title" in fields and request.title is not None else existing["title"],
            request.task.strip() if "task" in fields and request.task is not None else existing["task"],
            next_run_at,
            int(request.priority) if "priority" in fields and request.priority is not None else existing["priority"],
            next_recurrence_unit,
            next_recurrence_interval,
            next_recurrence_anchor,
            Jsonb(json_safe(request.metadata if "metadata" in fields and request.metadata is not None else existing.get("metadata") or {})),
            reminder_id,
        ),
    )
    if row is None:
        raise HTTPException(status_code=409, detail="reminder could not be updated")
    return {"reminder": reminder_public(row)}


@app.delete("/api/reminders/{reminder_id}")
def delete_reminder(reminder_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM reminders WHERE id = %s FOR UPDATE", (reminder_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="reminder not found")
            cancelled_job_ids: list[int] = []
            if row.get("job_id") and row.get("status") in {"scheduled", "queued"}:
                cur.execute(
                    """
                    UPDATE jobs
                    SET status = 'cancelled',
                        last_error = 'linked reminder was deleted from dashboard',
                        completed_at = now(),
                        locked_at = NULL,
                        locked_by = NULL,
                        updated_at = now()
                    WHERE id = %s
                      AND status = ANY(%s::text[])
                    RETURNING id
                    """,
                    (row["job_id"], sorted(CANCELLABLE_JOB_STATUSES)),
                )
                cancelled_job_ids = [int(item["id"]) for item in cur.fetchall()]
            cur.execute("DELETE FROM reminders WHERE id = %s RETURNING *", (reminder_id,))
            deleted = cur.fetchone()
            cur.execute(
                "INSERT INTO manual_events(event_type, payload) VALUES (%s, %s)",
                (
                    "dashboard_reminder_deleted",
                    Jsonb(json_safe({"reminder_id": reminder_id, "cancelled_job_ids": cancelled_job_ids})),
                ),
            )
    return {"deleted": reminder_public(deleted), "cancelled_job_ids": cancelled_job_ids}


@app.get("/api/projects")
def projects(status: Optional[str] = None, limit: int = 100) -> dict[str, Any]:
    limit = min(max(limit, 1), 200)
    clean_status = clean_status_filter(status, PROJECT_STATUSES, "project")
    if clean_status:
        rows = db.fetch_all(
            """
            SELECT
              p.*,
              oj.status AS original_job_status,
              oj.task_summary AS original_job_summary,
              COUNT(pt.id) AS task_count,
              COUNT(pt.id) FILTER (WHERE pt.status = 'completed') AS completed_task_count,
              COUNT(pt.id) FILTER (WHERE pt.status IN ('failed', 'cancelled')) AS failed_task_count
            FROM projects p
            LEFT JOIN jobs oj ON oj.id = p.original_job_id
            LEFT JOIN project_tasks pt ON pt.project_id = p.id
            WHERE p.status = %s
            GROUP BY p.id, oj.id, oj.status, oj.task_summary
            ORDER BY p.created_at DESC
            LIMIT %s
            """,
            (clean_status, limit),
        )
    else:
        rows = db.fetch_all(
            """
            SELECT
              p.*,
              oj.status AS original_job_status,
              oj.task_summary AS original_job_summary,
              COUNT(pt.id) AS task_count,
              COUNT(pt.id) FILTER (WHERE pt.status = 'completed') AS completed_task_count,
              COUNT(pt.id) FILTER (WHERE pt.status IN ('failed', 'cancelled')) AS failed_task_count
            FROM projects p
            LEFT JOIN jobs oj ON oj.id = p.original_job_id
            LEFT JOIN project_tasks pt ON pt.project_id = p.id
            GROUP BY p.id, oj.id, oj.status, oj.task_summary
            ORDER BY p.created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
    return {"projects": rows}


@app.get("/api/projects/{project_id}")
def project_detail(project_id: int) -> dict[str, Any]:
    project = db.fetch_one(
        """
        SELECT
          p.*,
          oj.status AS original_job_status,
          oj.task_summary AS original_job_summary,
          oj.created_at AS original_job_created_at,
          oj.updated_at AS original_job_updated_at
        FROM projects p
        LEFT JOIN jobs oj ON oj.id = p.original_job_id
        WHERE p.id = %s
        """,
        (project_id,),
    )
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    tasks = db.fetch_all(
        """
        SELECT
          pt.*,
          j.status AS job_status,
          j.task_summary AS job_summary,
          j.last_error AS job_last_error,
          j.created_at AS job_created_at,
          j.updated_at AS job_updated_at,
          j.completed_at AS job_completed_at,
          COALESCE(jc.cost_total, 0)::double precision AS job_cost_total
        FROM project_tasks pt
        LEFT JOIN jobs j ON j.id = pt.job_id
        %s
        WHERE pt.project_id = %%s
        ORDER BY pt.sequence ASC
        """
        % JOB_COST_JOIN,
        (project_id,),
    )
    return {"project": project, "tasks": tasks}


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM projects WHERE id = %s FOR UPDATE", (project_id,))
            project = cur.fetchone()
            if project is None:
                raise HTTPException(status_code=404, detail="project not found")
            cur.execute("SELECT * FROM project_tasks WHERE project_id = %s FOR UPDATE", (project_id,))
            tasks = list(cur.fetchall())
            task_ids = [int(task["id"]) for task in tasks]
            job_ids = [int(task["job_id"]) for task in tasks if task.get("job_id")]
            cancelled_job_ids: list[int] = []
            if job_ids:
                cur.execute(
                    """
                    UPDATE jobs
                    SET status = 'cancelled',
                        last_error = 'linked project was deleted from dashboard',
                        completed_at = now(),
                        locked_at = NULL,
                        locked_by = NULL,
                        updated_at = now()
                    WHERE id = ANY(%s::bigint[])
                      AND status = ANY(%s::text[])
                    RETURNING id
                    """,
                    (job_ids, sorted(CANCELLABLE_JOB_STATUSES)),
                )
                cancelled_job_ids = [int(item["id"]) for item in cur.fetchall()]
            instruction = "\n".join(
                [
                    "Project #%s was deleted from the dashboard before completion." % project_id,
                    "",
                    "Project title: %s" % project["title"],
                    "Cancelled linked job IDs: %s" % (", ".join(str(value) for value in cancelled_job_ids) or "none"),
                    "",
                    "Continue the original user task without this project, or explain that the project was deleted if the task cannot continue.",
                ]
            )
            cur.execute(
                """
                UPDATE jobs
                SET status = CASE WHEN status IN ('needs_review', 'waiting') THEN 'queued' ELSE status END,
                    run_at = CASE WHEN status IN ('needs_review', 'waiting') THEN now() ELSE run_at END,
                    has_new_context = true,
                    last_error = CASE WHEN status IN ('queued', 'needs_review', 'waiting') THEN NULL ELSE last_error END,
                    locked_at = CASE WHEN status IN ('needs_review', 'waiting') THEN NULL ELSE locked_at END,
                    locked_by = CASE WHEN status IN ('needs_review', 'waiting') THEN NULL ELSE locked_by END,
                    updated_at = now()
                WHERE id = %s
                  AND status NOT IN ('completed', 'failed', 'cancelled')
                RETURNING id
                """,
                (project["original_job_id"],),
            )
            original_job_updated = cur.fetchone() is not None
            if original_job_updated:
                cur.execute(
                    "INSERT INTO supervisor_instructions(job_id, instruction, created_by) VALUES (%s, %s, %s)",
                    (project["original_job_id"], instruction, "dashboard"),
                )
                cur.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM task_logs WHERE job_id = %s",
                    (project["original_job_id"],),
                )
                sequence = cur.fetchone()["next_sequence"]
                cur.execute(
                    """
                    INSERT INTO task_logs(job_id, sequence, event_type, output_data)
                    VALUES (%s, %s, 'status_change', %s)
                    """,
                    (
                        project["original_job_id"],
                        sequence,
                        Jsonb(
                            json_safe(
                                {
                                    "status": "queued",
                                    "reason": "project deleted from dashboard",
                                    "project_id": project_id,
                                    "cancelled_job_ids": cancelled_job_ids,
                                }
                            )
                        ),
                    ),
                )
            cur.execute("DELETE FROM projects WHERE id = %s RETURNING *", (project_id,))
            deleted = cur.fetchone()
            cur.execute(
                "INSERT INTO manual_events(event_type, payload) VALUES (%s, %s)",
                (
                    "dashboard_project_deleted",
                    Jsonb(
                        json_safe(
                            {
                                "project_id": project_id,
                                "original_job_id": project["original_job_id"],
                                "original_job_updated": original_job_updated,
                                "project_task_ids": task_ids,
                                "cancelled_job_ids": cancelled_job_ids,
                            }
                        )
                    ),
                ),
            )
    return {
        "deleted": deleted,
        "deleted_task_ids": task_ids,
        "cancelled_job_ids": cancelled_job_ids,
        "original_job_updated": original_job_updated,
    }


@app.get("/api/emails")
def emails(limit: int = 50) -> dict[str, Any]:
    limit = min(max(limit, 1), 200)
    rows = db.fetch_all(
        """
        SELECT id, message_id, thread_id, from_address, subject, received_at, is_actionable
        FROM emails
        ORDER BY received_at DESC, id DESC
        LIMIT %s
        """,
        (limit,),
    )
    return {"emails": rows}


# ---------------------------------------------------------------------------
# Entity Registry API
# ---------------------------------------------------------------------------


class EntityCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=1000)


class EntityUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)


class EntityMergeRequest(BaseModel):
    target_entity_id: int = Field(..., ge=1)


def entity_store() -> EntityStore:
    return EntityStore(db)


def raise_entity_error(exc: ValueError) -> None:
    message = str(exc)
    if "not found" in message:
        raise HTTPException(status_code=404, detail=message) from exc
    if "already exists" in message:
        raise HTTPException(status_code=409, detail=message) from exc
    raise HTTPException(status_code=400, detail=message) from exc


@app.get("/api/entities")
def entities_list() -> dict[str, Any]:
    rows = entity_store().list_all()
    return {"entities": rows}


@app.post("/api/entities")
def entity_create(request: EntityCreateRequest) -> dict[str, Any]:
    try:
        row = entity_store().create(
            name=request.name,
            description=request.description,
            created_by="dashboard",
        )
    except ValueError as exc:
        raise_entity_error(exc)
    return {"entity": row}


@app.get("/api/entities/{entity_id}")
def entity_detail(entity_id: int) -> dict[str, Any]:
    store = entity_store()
    entity = store.get(entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="entity not found")
    objects = store.get_objects_for_entity(entity_id)
    return {"entity": entity, "objects": objects}


@app.put("/api/entities/{entity_id}")
def entity_update(entity_id: int, request: EntityUpdateRequest) -> dict[str, Any]:
    fields = model_fields_set(request)
    if not fields:
        raise HTTPException(status_code=400, detail="at least one field must be provided")
    try:
        row = entity_store().update(
            entity_id=entity_id,
            name=request.name if "name" in fields else None,
            description=request.description if "description" in fields else None,
        )
    except ValueError as exc:
        raise_entity_error(exc)
    return {"entity": row}


@app.get("/api/entities/{entity_id}/delete-preview")
def entity_delete_preview(entity_id: int) -> dict[str, Any]:
    try:
        preview = entity_store().delete_preview(entity_id)
    except ValueError as exc:
        raise_entity_error(exc)
    return preview


@app.delete("/api/entities/{entity_id}")
def entity_delete(entity_id: int) -> dict[str, Any]:
    try:
        result = entity_store().delete_cascade(entity_id)
    except ValueError as exc:
        raise_entity_error(exc)
    return result


@app.post("/api/entities/{entity_id}/merge")
def entity_merge(entity_id: int, request: EntityMergeRequest) -> dict[str, Any]:
    try:
        result = entity_store().merge(
            source_entity_id=entity_id,
            target_entity_id=request.target_entity_id,
        )
    except ValueError as exc:
        raise_entity_error(exc)
    return result

