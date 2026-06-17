import hashlib
import logging
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from psycopg.types.json import Jsonb

from .config import AppConfig
from .database import Database, json_safe
from .document_text import DocumentTextExtractor, UnsupportedDocumentError
from .embedding_client import EmbeddingClient
from .memory_store import cosine_similarity
from .threading import safe_filename


LOGGER = logging.getLogger("assistant.workspace_index")


@dataclass
class NormalizationResult:
    path: Path
    converted: bool = False
    archived_path: Optional[Path] = None
    error: Optional[str] = None


class WorkspaceNormalizer:
    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.shared_root = Path(config.get("agent.filesystem.shared_root", "/data/share")).resolve()
        self.extractor = DocumentTextExtractor(config)

    def enabled(self) -> bool:
        return self.config.get_bool("agent.workspace.normalize_documents", True)

    def archive_root(self) -> Path:
        value = Path(str(self.config.get("agent.workspace.source_archive_root", ".assistant/archive/source-documents") or ".assistant/archive/source-documents"))
        root = value.resolve() if value.is_absolute() else (self.shared_root / value).resolve()
        if root != self.shared_root and self.shared_root not in root.parents:
            raise RuntimeError("workspace source archive root must stay under shared root")
        return root

    def path_is_or_is_under(self, path: Path, parent: Path) -> bool:
        resolved = path.resolve()
        resolved_parent = parent.resolve()
        return resolved == resolved_parent or resolved_parent in resolved.parents

    def is_archive_path(self, path: Path) -> bool:
        return self.path_is_or_is_under(path, self.archive_root())

    def normalize_path(self, path: Path, source: str = "workspace") -> NormalizationResult:
        return NormalizationResult(path=path)

    def convert_to_markdown(
        self,
        path: Path,
        source: str = "workspace",
        destination_path: Optional[Path] = None,
    ) -> NormalizationResult:
        if not path.exists() or not path.is_file() or self.is_archive_path(path):
            return NormalizationResult(path=path)
        max_bytes = self.config.get_int("agent.workspace.max_conversion_bytes", 25 * 1024 * 1024)
        size_bytes = path.stat().st_size
        if size_bytes > max_bytes:
            error = "document exceeds workspace conversion size limit"
            self.record_conversion(path, None, None, "skipped", source, error=error)
            return NormalizationResult(path=path, error=error)

        original_sha = file_sha256(path)
        self.record_conversion(
            original_path=path,
            markdown_path=None,
            archived_path=None,
            status="pending",
            source=source,
            original_sha256=original_sha,
            metadata={"original_size_bytes": size_bytes},
        )
        try:
            markdown = self.extractor.convert_to_markdown(path)
            markdown_path = destination_path.resolve() if destination_path else self.available_markdown_path(path)
            markdown_path.parent.mkdir(parents=True, exist_ok=True)
            markdown_path.write_text(markdown, encoding="utf-8")
            self.record_conversion(
                original_path=path,
                markdown_path=markdown_path,
                archived_path=None,
                status="ready",
                source=source,
                original_sha256=original_sha,
                markdown_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
                metadata={"original_size_bytes": size_bytes},
            )
            return NormalizationResult(path=markdown_path, converted=True)
        except Exception as exc:
            LOGGER.warning("workspace document conversion failed for %s: %s", path, exc)
            self.record_conversion(path, None, None, "failed", source, original_sha256=original_sha, error=str(exc))
            return NormalizationResult(path=path, error=str(exc))

    def available_markdown_path(self, source_path: Path) -> Path:
        target = source_path.with_suffix(".md")
        if not target.exists():
            return target
        stem = source_path.with_suffix("").name
        parent = source_path.parent
        index = 1
        while True:
            candidate = parent / ("%s-%s.md" % (stem, index))
            if not candidate.exists():
                return candidate
            index += 1

    def available_archive_path(self, source_path: Path, sha256: str) -> Path:
        try:
            relative = source_path.relative_to(self.shared_root)
        except ValueError:
            relative = Path(source_path.name)
        date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
        stem = safe_filename(str(relative.with_suffix("")))[:160] or safe_filename(source_path.stem) or "document"
        suffix = source_path.suffix.lower()
        base = "%s-%s%s" % (sha256[:16], stem, suffix)
        candidate = self.archive_root() / date_part / base
        index = 1
        while candidate.exists():
            candidate = self.archive_root() / date_part / ("%s-%s-%s%s" % (sha256[:16], stem, index, suffix))
            index += 1
        return candidate

    def record_conversion(
        self,
        original_path: Path,
        markdown_path: Optional[Path],
        archived_path: Optional[Path],
        status: str,
        source: str,
        original_sha256: Optional[str] = None,
        markdown_sha256: Optional[str] = None,
        error: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        try:
            self.db.execute(
                """
                INSERT INTO workspace_document_conversions(
                  original_relative_path,
                  markdown_relative_path,
                  archived_relative_path,
                  original_sha256,
                  markdown_sha256,
                  status,
                  source,
                  error,
                  metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    relative_to(self.shared_root, original_path),
                    relative_to(self.shared_root, markdown_path) if markdown_path else None,
                    relative_to(self.shared_root, archived_path) if archived_path else None,
                    original_sha256,
                    markdown_sha256,
                    status,
                    source,
                    error,
                    Jsonb(json_safe(metadata or {})),
                ),
            )
        except Exception as exc:
            LOGGER.debug("could not record workspace document conversion: %s", exc)


class WorkspaceIndex:
    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.shared_root = Path(config.get("agent.filesystem.shared_root", "/data/share")).resolve()
        self.extractor = DocumentTextExtractor(config)
        self.embedding_client = EmbeddingClient(config)
        self.normalizer = WorkspaceNormalizer(db, config)

    def enabled(self) -> bool:
        return self.config.get_bool("agent.workspace.index.enabled", True)

    def hidden_roots(self) -> list[Path]:
        paths = [
            self.shared_root / ".cache" / "docs",
            self.shared_root / ".trash",
            self.normalizer.archive_root(),
        ]
        cache_root = Path(str(self.config.get("agent.tool_result_cache.root", ".assistant/cache/tool-results") or ".assistant/cache/tool-results"))
        paths.append(cache_root.resolve() if cache_root.is_absolute() else (self.shared_root / cache_root).resolve())
        return [path.resolve() for path in paths]

    def path_is_or_is_under(self, path: Path, parent: Path) -> bool:
        resolved = path.resolve()
        resolved_parent = parent.resolve()
        return resolved == resolved_parent or resolved_parent in resolved.parents

    def should_skip_path(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved != self.shared_root and self.shared_root not in resolved.parents:
            return True
        return any(self.path_is_or_is_under(resolved, root) for root in self.hidden_roots())

    def index_path_best_effort(self, path: Path, source: str = "workspace") -> Optional[dict[str, Any]]:
        try:
            return self.index_path(path, source=source)
        except Exception as exc:
            LOGGER.debug("workspace index update failed for %s: %s", path, exc)
            return None

    def index_tree_best_effort(self, path: Path, source: str = "workspace") -> list[dict[str, Any]]:
        try:
            return self.index_tree(path, source=source)
        except Exception as exc:
            LOGGER.debug("workspace tree index update failed for %s: %s", path, exc)
            return []

    def mark_deleted_best_effort(self, path: Path) -> None:
        try:
            self.mark_deleted(path)
        except Exception as exc:
            LOGGER.debug("workspace index delete marker failed for %s: %s", path, exc)

    def mark_deleted_prefix_best_effort(self, path: Path) -> None:
        try:
            self.mark_deleted_prefix(path)
        except Exception as exc:
            LOGGER.debug("workspace index prefix delete marker failed for %s: %s", path, exc)

    def index_path(self, path: Path, source: str = "workspace") -> Optional[dict[str, Any]]:
        if not self.enabled():
            return None
        path = path.resolve()
        if not path.exists():
            self.mark_deleted(path)
            return None
        if path.is_dir():
            rows = self.index_tree(path, source=source)
            return rows[0] if rows else None
        if self.should_skip_path(path):
            return None

        superseded = self.current_superseded_row(path)
        if superseded:
            return superseded

        return self.index_text_path(path)

    def current_superseded_row(self, path: Path) -> Optional[dict[str, Any]]:
        try:
            relative = relative_to(self.shared_root, path)
        except ValueError:
            return None
        row = self.db.fetch_one(
            """
            SELECT *
            FROM workspace_files
            WHERE relative_path = %s
            """,
            (relative,),
        )
        if not row or row.get("index_status") != "superseded":
            return None
        try:
            if row.get("content_sha256") == file_sha256(path):
                return row
        except OSError:
            return row
        return None

    def index_text_path(self, path: Path) -> dict[str, Any]:
        stat = path.stat()
        relative = relative_to(self.shared_root, path)
        sha = file_sha256(path)
        existing = self.db.fetch_one(
            """
            SELECT *
            FROM workspace_files
            WHERE relative_path = %s
            """,
            (relative,),
        )
        if (
            existing
            and existing.get("content_sha256") == sha
            and str(existing.get("mtime_ns") or "") == str(stat.st_mtime_ns)
            and existing.get("index_status") == "indexed"
        ):
            return existing

        try:
            text, extractor = self.extractor.extract_text(path)
        except UnsupportedDocumentError as exc:
            row = self.upsert_file(path, sha, stat, "unsupported", str(exc), {"extractor": "none"})
            self.delete_chunks(row["id"])
            return row
        except Exception as exc:
            row = self.upsert_file(path, sha, stat, "error", str(exc), {"extractor": "error"})
            self.delete_chunks(row["id"])
            return row

        row = self.upsert_file(path, sha, stat, "pending", None, {"extractor": extractor})
        self.delete_chunks(row["id"])
        chunks = chunk_text(text, max_chars=self.config.get_int("agent.workspace.index.chunk_chars", 3500))
        embedding_error = None
        for index, chunk in enumerate(chunks):
            embedding = None
            embedding_model = None
            embedding_dimensions = None
            embedding_updated_at = None
            if not embedding_error and self.embedding_client.enabled:
                try:
                    embedding = self.embedding_client.embed(chunk["content"])
                    embedding_model = self.embedding_client.model
                    embedding_dimensions = len(embedding)
                    embedding_updated_at = datetime.now(timezone.utc)
                except Exception as exc:
                    embedding_error = str(exc)
                    LOGGER.warning("workspace chunk embedding failed for %s: %s", path, exc)
            self.insert_chunk(
                file_id=row["id"],
                chunk_index=index,
                chunk=chunk,
                embedding=embedding,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
                embedding_updated_at=embedding_updated_at,
            )
        status = "embedding_failed" if embedding_error else "indexed"
        final = self.upsert_file(
            path,
            sha,
            stat,
            status,
            embedding_error,
            {"extractor": extractor, "chunk_count": len(chunks), "embedding_model": self.embedding_client.model if self.embedding_client.enabled else None},
            indexed=True,
        )
        return final

    def upsert_file(
        self,
        path: Path,
        sha: str,
        stat: Any,
        status: str,
        error: Optional[str],
        metadata: dict[str, Any],
        indexed: bool = False,
    ) -> dict[str, Any]:
        relative = relative_to(self.shared_root, path)
        mime_type = mimetypes.guess_type(path.name)[0]
        return self.db.fetch_one(
            """
            INSERT INTO workspace_files(
              relative_path,
              size_bytes,
              mtime_ns,
              content_sha256,
              mime_type,
              extension,
              index_status,
              error,
              metadata,
              indexed_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() ELSE NULL END)
            ON CONFLICT (relative_path)
            DO UPDATE SET
              size_bytes = EXCLUDED.size_bytes,
              mtime_ns = EXCLUDED.mtime_ns,
              content_sha256 = EXCLUDED.content_sha256,
              mime_type = EXCLUDED.mime_type,
              extension = EXCLUDED.extension,
              index_status = EXCLUDED.index_status,
              error = EXCLUDED.error,
              metadata = EXCLUDED.metadata,
              indexed_at = CASE WHEN %s THEN now() ELSE workspace_files.indexed_at END,
              updated_at = now()
            RETURNING *
            """,
            (
                relative,
                stat.st_size,
                str(stat.st_mtime_ns),
                sha,
                mime_type,
                path.suffix.lower(),
                status,
                error,
                Jsonb(json_safe(metadata)),
                indexed,
                indexed,
            ),
        )

    def delete_chunks(self, file_id: int) -> None:
        self.db.execute("DELETE FROM workspace_file_chunks WHERE file_id = %s", (file_id,))

    def insert_chunk(
        self,
        file_id: int,
        chunk_index: int,
        chunk: dict[str, Any],
        embedding: Optional[list[float]],
        embedding_model: Optional[str],
        embedding_dimensions: Optional[int],
        embedding_updated_at: Optional[datetime],
    ) -> None:
        self.db.execute(
            """
            INSERT INTO workspace_file_chunks(
              file_id,
              chunk_index,
              content,
              start_line,
              end_line,
              content_sha256,
              embedding,
              embedding_model,
              embedding_dimensions,
              embedding_updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                file_id,
                chunk_index,
                chunk["content"],
                chunk["start_line"],
                chunk["end_line"],
                hashlib.sha256(chunk["content"].encode("utf-8")).hexdigest(),
                embedding,
                embedding_model,
                embedding_dimensions,
                embedding_updated_at,
            ),
        )

    def mark_deleted(self, path: Path) -> None:
        try:
            relative = relative_to(self.shared_root, path)
        except ValueError:
            return
        row = self.db.fetch_one("SELECT id FROM workspace_files WHERE relative_path = %s", (relative,))
        if row:
            self.db.execute("DELETE FROM workspace_file_chunks WHERE file_id = %s", (row["id"],))
            self.db.execute(
                """
                UPDATE workspace_files
                SET index_status = 'deleted', error = NULL, updated_at = now()
                WHERE id = %s
                """,
                (row["id"],),
            )

    def mark_superseded(self, path: Path, superseded_by: Path) -> None:
        try:
            relative = relative_to(self.shared_root, path)
            superseded_by_relative = relative_to(self.shared_root, superseded_by)
        except ValueError:
            return
        row = self.db.fetch_one("SELECT id FROM workspace_files WHERE relative_path = %s", (relative,))
        if row:
            self.db.execute("DELETE FROM workspace_file_chunks WHERE file_id = %s", (row["id"],))
            self.db.execute(
                """
                UPDATE workspace_files
                SET index_status = 'superseded',
                    error = NULL,
                    metadata = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (Jsonb(json_safe({"superseded_by": superseded_by_relative})), row["id"]),
            )

    def mark_deleted_prefix(self, path: Path) -> None:
        try:
            relative = relative_to(self.shared_root, path).rstrip("/")
        except ValueError:
            return
        rows = self.db.fetch_all(
            """
            SELECT id
            FROM workspace_files
            WHERE relative_path = %s OR relative_path LIKE %s
            """,
            (relative, relative + "/%"),
        )
        for row in rows:
            self.db.execute("DELETE FROM workspace_file_chunks WHERE file_id = %s", (row["id"],))
        self.db.execute(
            """
            UPDATE workspace_files
            SET index_status = 'deleted', error = NULL, updated_at = now()
            WHERE relative_path = %s OR relative_path LIKE %s
            """,
            (relative, relative + "/%"),
        )

    def index_tree(self, path: Path, source: str = "workspace") -> list[dict[str, Any]]:
        if not path.exists():
            self.mark_deleted_prefix(path)
            return []
        rows = []
        for item in sorted(path.rglob("*") if path.is_dir() else [path], key=lambda value: str(value)):
            if item.is_dir() or self.should_skip_path(item):
                continue
            row = self.index_path(item, source=source)
            if row:
                rows.append(row)
        return rows

    def run_once(self) -> int:
        if not self.enabled() or not self.shared_root.exists():
            return 0
        seen: set[str] = set()
        count = 0
        for item in sorted(self.shared_root.rglob("*"), key=lambda value: str(value.relative_to(self.shared_root))):
            if item.is_dir() or self.should_skip_path(item):
                continue
            row = self.index_path(item, source="scan")
            if row:
                seen.add(str(row["relative_path"]))
                count += 1
        rows = self.db.fetch_all("SELECT relative_path FROM workspace_files WHERE index_status <> 'deleted'")
        for row in rows:
            relative = str(row["relative_path"])
            path = (self.shared_root / relative).resolve()
            if relative not in seen and not path.exists():
                self.mark_deleted(path)
        return count

    def run_forever(self, should_stop) -> None:
        interval = self.config.get_int("agent.workspace.index.poll_interval_seconds", 60)
        while not should_stop():
            try:
                count = self.run_once()
                LOGGER.info("workspace indexer processed %s file(s)", count)
            except Exception:
                LOGGER.exception("workspace indexer loop failed")
            should_stop(interval)

    def search(self, query: str, directory: str = ".", limit: int = 10) -> list[dict[str, Any]]:
        clean_query = str(query or "").strip()
        if not clean_query:
            raise ValueError("query is required")
        max_rows = min(max(int(limit or 10), 1), 50)
        root = (self.shared_root / directory).resolve() if not Path(directory).is_absolute() else Path(directory).resolve()
        if root != self.shared_root and self.shared_root not in root.parents:
            raise ValueError("directory must stay under shared root")
        prefix = "" if root == self.shared_root else str(root.relative_to(self.shared_root)).rstrip("/") + "/"

        results: dict[tuple[str, int], dict[str, Any]] = {}
        try:
            query_embedding = self.embedding_client.embed(clean_query)
        except Exception as exc:
            LOGGER.warning("workspace semantic search falling back to keyword only: %s", exc)
            query_embedding = []

        if query_embedding:
            candidate_limit = self.config.get_int("agent.workspace.index.candidate_limit", 3000)
            rows = self.db.fetch_all(
                """
                SELECT c.*, f.relative_path, f.size_bytes, f.updated_at AS file_updated_at
                FROM workspace_file_chunks c
                JOIN workspace_files f ON f.id = c.file_id
                WHERE c.embedding IS NOT NULL
                  AND f.index_status IN ('indexed', 'embedding_failed')
                  AND (%s = '' OR f.relative_path LIKE %s)
                ORDER BY f.updated_at DESC, c.chunk_index ASC
                LIMIT %s
                """,
                (prefix, prefix + "%", min(max(candidate_limit, 10), 10000)),
            )
            for row in rows:
                score = cosine_similarity(query_embedding, row.get("embedding") or [])
                if score is None:
                    continue
                key = (str(row["relative_path"]), int(row["chunk_index"]))
                results[key] = search_result(row, clean_query, score=score, match_type="semantic")

        keyword_rows = self.db.fetch_all(
            """
            SELECT c.*, f.relative_path, f.size_bytes, f.updated_at AS file_updated_at
            FROM workspace_file_chunks c
            JOIN workspace_files f ON f.id = c.file_id
            WHERE f.index_status IN ('indexed', 'embedding_failed')
              AND (%s = '' OR f.relative_path LIKE %s)
              AND (c.content ILIKE %s OR f.relative_path ILIKE %s)
            ORDER BY f.updated_at DESC, c.chunk_index ASC
            LIMIT %s
            """,
            (prefix, prefix + "%", "%%%s%%" % clean_query, "%%%s%%" % clean_query, max_rows),
        )
        for row in keyword_rows:
            key = (str(row["relative_path"]), int(row["chunk_index"]))
            results.setdefault(key, search_result(row, clean_query, score=0.0, match_type="keyword"))

        ordered = list(results.values())
        ordered.sort(key=lambda item: (float(item.get("score") or 0.0), str(item.get("file_updated_at") or "")), reverse=True)
        return ordered[:max_rows]


def relative_to(root: Path, path: Optional[Path]) -> str:
    if path is None:
        return ""
    return str(path.resolve().relative_to(root.resolve()))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def chunk_text(text: str, max_chars: int = 3500) -> list[dict[str, Any]]:
    clean_text = str(text or "")
    if not clean_text.strip():
        return []
    limit = min(max(int(max_chars or 3500), 500), 20000)
    chunks: list[dict[str, Any]] = []
    current: list[str] = []
    current_len = 0
    start_line = 1
    line_no = 0
    for raw_line in clean_text.splitlines():
        line_no += 1
        line = raw_line.rstrip()
        projected = current_len + len(line) + 1
        if current and projected > limit:
            chunks.append({"content": "\n".join(current).strip(), "start_line": start_line, "end_line": line_no - 1})
            current = []
            current_len = 0
            start_line = line_no
        if len(line) > limit:
            for start in range(0, len(line), limit):
                part = line[start : start + limit]
                if current:
                    chunks.append({"content": "\n".join(current).strip(), "start_line": start_line, "end_line": line_no - 1})
                    current = []
                    current_len = 0
                chunks.append({"content": part, "start_line": line_no, "end_line": line_no})
            start_line = line_no + 1
            continue
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append({"content": "\n".join(current).strip(), "start_line": start_line, "end_line": line_no})
    return [chunk for chunk in chunks if chunk["content"]]


def search_result(row: dict[str, Any], query: str, score: float, match_type: str) -> dict[str, Any]:
    content = str(row.get("content") or "")
    return {
        "relative_path": row.get("relative_path"),
        "chunk_index": row.get("chunk_index"),
        "start_line": row.get("start_line"),
        "end_line": row.get("end_line"),
        "score": score,
        "match_type": match_type,
        "snippet": snippet(content, query),
        "size_bytes": row.get("size_bytes"),
        "file_updated_at": row.get("file_updated_at"),
    }


def snippet(content: str, query: str, max_chars: int = 700) -> str:
    text = " ".join(str(content or "").split())
    if len(text) <= max_chars:
        return text
    needle = str(query or "").strip().lower()
    start = 0
    if needle:
        found = text.lower().find(needle)
        if found >= 0:
            start = max(found - max_chars // 3, 0)
    end = min(start + max_chars, len(text))
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return "%s%s%s" % (prefix, text[start:end].strip(), suffix)
