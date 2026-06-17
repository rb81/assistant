import hashlib
import html
import logging
import re
import socket
import struct
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from psycopg.types.json import Jsonb

from .config import AppConfig
from .database import Database, json_safe
from .document_text import DocumentTextExtractor
from .threading import safe_filename, safe_message_segment


LOGGER = logging.getLogger("assistant.artifacts")

DEFAULT_ATTACHMENT_EXTENSIONS = {
    ".csv",
    ".docx",
    ".epub",
    ".htm",
    ".html",
    ".json",
    ".md",
    ".pdf",
    ".pptx",
    ".txt",
    ".xls",
    ".xlsx",
    ".xml",
}
URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def public_attachment_metadata(attachment: dict[str, Any]) -> dict[str, Any]:
    return {
        key: attachment.get(key)
        for key in ("filename", "content_type", "size_bytes", "sha256")
        if attachment.get(key) not in (None, "")
    }


def public_artifact_manifest(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        "artifact_id": row.get("id"),
        "email_id": row.get("email_id"),
        "source": row.get("source_type"),
        "label": row.get("source_label"),
        "original_filename": row.get("original_filename"),
        "content_type": row.get("content_type"),
        "source_uri": row.get("source_uri"),
        "scan_status": row.get("scan_status"),
        "conversion_status": row.get("conversion_status"),
        "markdown_path": row.get("markdown_path"),
        "markdown_size_bytes": row.get("markdown_size_bytes"),
        "error": row.get("error"),
    }
    return {key: value for key, value in result.items() if value not in (None, "", [])}


def canonical_youtube_url(value: str) -> Optional[str]:
    candidate = html.unescape(str(value or "").strip()).rstrip(").,;]'\"")
    if not candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        return None
    host = parsed.netloc.lower().split(":", 1)[0]
    video_id = ""
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif parsed.path.startswith("/shorts/"):
            video_id = parsed.path.split("/", 3)[2]
        elif parsed.path.startswith("/embed/"):
            video_id = parsed.path.split("/", 3)[2]
    if not YOUTUBE_ID_RE.match(video_id or ""):
        return None
    return "https://www.youtube.com/watch?v=%s" % video_id


def extract_youtube_urls(*bodies: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for body in bodies:
        for match in URL_RE.findall(html.unescape(body or "")):
            canonical = canonical_youtube_url(match)
            if canonical and canonical not in seen:
                seen.add(canonical)
                urls.append(canonical)
    return urls


class ArtifactProcessor:
    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.shared_root = Path(config.get("agent.filesystem.shared_root", "/data/share")).resolve()
        self.processed_root = self.resolve_processed_root()
        self.extractor = DocumentTextExtractor(config)

    def enabled(self) -> bool:
        return self.config.get_bool("agent.artifacts.enabled", True)

    def process_email(
        self,
        email_row: dict[str, Any],
        attachments: list[dict[str, Any]],
        body_text: str,
        body_html: str,
    ) -> list[dict[str, Any]]:
        if not self.enabled():
            return []
        artifacts: list[dict[str, Any]] = []
        for index, attachment in enumerate(attachments, start=1):
            try:
                artifacts.append(self.process_attachment(email_row, attachment, index))
            except Exception as exc:
                LOGGER.exception("attachment artifact processing failed")
                artifacts.append(
                    self.create_artifact(
                        email_row,
                        source_type="attachment",
                        source_label=str(attachment.get("filename") or "attachment-%s" % index),
                        original_filename=attachment.get("filename"),
                        content_type=attachment.get("content_type"),
                        raw_path=attachment.get("path"),
                        raw_sha256=attachment.get("sha256"),
                        raw_size_bytes=attachment.get("size_bytes"),
                        scan_status="error",
                        scan_result=str(exc),
                        conversion_status="failed",
                        error=str(exc),
                        metadata={"attachment_index": index},
                    )
                )
        for index, url in enumerate(extract_youtube_urls(body_text, body_html), start=1):
            try:
                artifacts.append(self.process_youtube_url(email_row, url, index))
            except Exception as exc:
                LOGGER.exception("youtube artifact processing failed")
                artifacts.append(
                    self.create_artifact(
                        email_row,
                        source_type="youtube_url",
                        source_label=url,
                        source_uri=url,
                        scan_status="not_applicable",
                        conversion_status="failed",
                        error=str(exc),
                        metadata={"url_index": index},
                    )
                )
        return artifacts

    def process_attachment(self, email_row: dict[str, Any], attachment: dict[str, Any], index: int) -> dict[str, Any]:
        raw_path = Path(str(attachment.get("path") or ""))
        filename = str(attachment.get("filename") or "attachment-%s" % index)
        extension = raw_path.suffix.lower() or Path(filename).suffix.lower()
        artifact = self.create_artifact(
            email_row,
            source_type="attachment",
            source_label=filename,
            original_filename=filename,
            content_type=attachment.get("content_type"),
            raw_path=str(raw_path),
            raw_sha256=attachment.get("sha256"),
            raw_size_bytes=attachment.get("size_bytes"),
            scan_status="pending",
            conversion_status="pending",
            metadata={"attachment_index": index, "extension": extension},
        )
        if not raw_path.is_file():
            return self.update_artifact(
                artifact["id"],
                scan_status="error",
                scan_result="attachment file is missing",
                conversion_status="failed",
                error="attachment file is missing",
            )

        scan = self.scan_file(raw_path)
        if scan["status"] != "clean" and not scan.get("allow_conversion"):
            conversion_status = "skipped" if scan["status"] in ("infected", "error") else "pending"
            error = scan.get("result") if scan["status"] in ("infected", "error") else None
            return self.update_artifact(
                artifact["id"],
                scan_status=scan["status"],
                scan_engine=scan.get("engine"),
                scan_result=scan.get("result"),
                conversion_status=conversion_status,
                error=error,
            )

        if not self.is_supported_attachment(extension):
            return self.update_artifact(
                artifact["id"],
                scan_status=scan["status"],
                scan_engine=scan.get("engine"),
                scan_result=scan.get("result"),
                conversion_status="unsupported",
                error="unsupported attachment extension: %s" % (extension or "<none>"),
            )

        max_bytes = self.config.get_int("agent.artifacts.max_attachment_bytes", 25 * 1024 * 1024)
        if raw_path.stat().st_size > max_bytes:
            return self.update_artifact(
                artifact["id"],
                scan_status=scan["status"],
                scan_engine=scan.get("engine"),
                scan_result=scan.get("result"),
                conversion_status="skipped",
                error="attachment exceeds artifact conversion size limit",
            )

        try:
            with raw_path.open("rb") as handle:
                result = self.markitdown().convert_stream(handle, file_extension=extension)
            markdown = self.result_text(result)
            markdown_path = self.write_markdown(email_row, filename, markdown)
            return self.update_artifact(
                artifact["id"],
                scan_status=scan["status"],
                scan_engine=scan.get("engine"),
                scan_result=scan.get("result"),
                conversion_status="ready",
                markdown_path=markdown_path,
                markdown_sha256=sha256_text(markdown),
                markdown_size_bytes=len(markdown.encode("utf-8")),
                error=None,
            )
        except Exception as exc:
            return self.update_artifact(
                artifact["id"],
                scan_status=scan["status"],
                scan_engine=scan.get("engine"),
                scan_result=scan.get("result"),
                conversion_status="failed",
                error=str(exc),
            )

    def process_youtube_url(self, email_row: dict[str, Any], url: str, index: int) -> dict[str, Any]:
        artifact = self.create_artifact(
            email_row,
            source_type="youtube_url",
            source_label=url,
            source_uri=url,
            scan_status="not_applicable",
            conversion_status="pending",
            metadata={"url_index": index},
        )
        try:
            result = self.markitdown().convert(url)
            markdown = self.result_text(result)
            video_id = parse_qs(urlparse(url).query).get("v", ["youtube"])[0]
            markdown_path = self.write_markdown(email_row, "youtube-%s" % video_id, markdown)
            return self.update_artifact(
                artifact["id"],
                conversion_status="ready",
                markdown_path=markdown_path,
                markdown_sha256=sha256_text(markdown),
                markdown_size_bytes=len(markdown.encode("utf-8")),
                error=None,
            )
        except Exception as exc:
            return self.update_artifact(artifact["id"], conversion_status="failed", error=str(exc))

    def create_artifact(
        self,
        email_row: dict[str, Any],
        source_type: str,
        source_label: str,
        source_uri: Optional[str] = None,
        original_filename: Optional[str] = None,
        content_type: Optional[str] = None,
        raw_path: Optional[str] = None,
        raw_sha256: Optional[str] = None,
        raw_size_bytes: Optional[int] = None,
        scan_status: str = "pending",
        scan_engine: Optional[str] = None,
        scan_result: Optional[str] = None,
        conversion_status: str = "pending",
        markdown_path: Optional[str] = None,
        markdown_sha256: Optional[str] = None,
        markdown_size_bytes: Optional[int] = None,
        error: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return self.db.fetch_one(
            """
            INSERT INTO processed_artifacts(
              email_id,
              thread_id,
              source_type,
              source_label,
              source_uri,
              original_filename,
              content_type,
              raw_path,
              raw_sha256,
              raw_size_bytes,
              scan_status,
              scan_engine,
              scan_result,
              conversion_status,
              markdown_path,
              markdown_sha256,
              markdown_size_bytes,
              error,
              metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                email_row["id"],
                email_row["thread_id"],
                source_type,
                source_label,
                source_uri,
                original_filename,
                content_type,
                raw_path,
                raw_sha256,
                raw_size_bytes,
                scan_status,
                scan_engine,
                scan_result,
                conversion_status,
                markdown_path,
                markdown_sha256,
                markdown_size_bytes,
                error,
                Jsonb(json_safe(metadata or {})),
            ),
        )

    def update_artifact(self, artifact_id: int, **values: Any) -> dict[str, Any]:
        allowed = {
            "scan_status",
            "scan_engine",
            "scan_result",
            "conversion_status",
            "markdown_path",
            "markdown_sha256",
            "markdown_size_bytes",
            "error",
        }
        assignments = []
        params: list[Any] = []
        for key, value in values.items():
            if key not in allowed:
                continue
            assignments.append("%s = %%s" % key)
            params.append(value)
        if not assignments:
            row = self.db.fetch_one("SELECT * FROM processed_artifacts WHERE id = %s", (artifact_id,))
            if row is None:
                raise RuntimeError("artifact not found")
            return row
        params.append(artifact_id)
        return self.db.fetch_one(
            "UPDATE processed_artifacts SET %s, updated_at = now() WHERE id = %%s RETURNING *" % ", ".join(assignments),
            tuple(params),
        )

    def scan_file(self, path: Path) -> dict[str, Any]:
        if not self.config.get_bool("agent.artifacts.clamav.enabled", True):
            return {"status": "skipped", "engine": "clamav", "result": "scan disabled by configuration", "allow_conversion": True}
        host = str(self.config.get("agent.artifacts.clamav.host", "clamav") or "clamav")
        port = self.config.get_int("agent.artifacts.clamav.port", 3310)
        timeout = self.config.get_int("agent.artifacts.clamav.timeout_seconds", 30)
        required = self.config.get_bool("agent.artifacts.clamav.required", True)
        try:
            response = self.clamd_instream(path, host, port, timeout)
        except Exception as exc:
            return {"status": "error", "engine": "clamav", "result": str(exc), "allow_conversion": not required}
        if " FOUND" in response:
            return {"status": "infected", "engine": "clamav", "result": response, "allow_conversion": False}
        if response.endswith("OK") or response.endswith(": OK"):
            return {"status": "clean", "engine": "clamav", "result": response, "allow_conversion": True}
        return {"status": "error", "engine": "clamav", "result": response, "allow_conversion": not required}

    def clamd_instream(self, path: Path, host: str, port: int, timeout: int) -> str:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"zINSTREAM\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    sock.sendall(struct.pack("!I", len(chunk)))
                    sock.sendall(chunk)
            sock.sendall(struct.pack("!I", 0))
            chunks = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
                if b"\0" in data or b"\n" in data:
                    break
        return b"".join(chunks).decode("utf-8", errors="replace").strip("\0\r\n ")

    def markitdown(self) -> Any:
        return self.extractor.markitdown()

    def result_text(self, result: Any) -> str:
        return self.extractor.result_text(result)

    def is_supported_attachment(self, extension: str) -> bool:
        configured = self.config.get_list("agent.artifacts.allowed_attachment_extensions")
        allowed = {item if item.startswith(".") else ".%s" % item for item in configured} if configured else DEFAULT_ATTACHMENT_EXTENSIONS
        return extension.lower() in allowed

    def resolve_processed_root(self) -> Path:
        value = Path(str(self.config.get("agent.artifacts.processed_root", "processed") or "processed"))
        root = value.resolve() if value.is_absolute() else (self.shared_root / value).resolve()
        if root != self.shared_root and self.shared_root not in root.parents:
            raise RuntimeError("artifact processed_root must stay under shared root")
        return root

    def write_markdown(self, email_row: dict[str, Any], source_name: str, markdown: str) -> str:
        target_dir = self.processed_root / "email" / safe_message_segment(str(email_row["message_id"]))
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = safe_filename(Path(source_name).stem or "artifact")
        target = target_dir / ("%s.md" % stem)
        suffix = 1
        while target.exists():
            target = target_dir / ("%s-%s.md" % (stem, suffix))
            suffix += 1
        target.write_text(markdown, encoding="utf-8")
        return str(target.resolve().relative_to(self.shared_root))
