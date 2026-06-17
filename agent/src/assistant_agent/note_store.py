import logging
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg.types.json import Jsonb

from .config import AppConfig
from .database import Database, json_safe
from .embedding_client import EmbeddingClient
from .memory_store import cosine_similarity


LOGGER = logging.getLogger("assistant.note_store")
UNSET = object()


NOTE_COLUMNS = """
id,
title,
content,
tags,
status,
linked_entities,
embedding_model,
embedding_dimensions,
embedding_updated_at,
source_job_id,
metadata,
last_accessed_at,
created_at,
updated_at
"""


class NoteStore:
    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.embedding_client = EmbeddingClient(config)

    def clean_tags(self, tags: Optional[list[str]]) -> list[str]:
        cleaned = []
        for tag in tags or []:
            value = str(tag).strip().lower()
            if not value or value in cleaned:
                continue
            cleaned.append(value[:64])
            if len(cleaned) >= 20:
                break
        return cleaned

    def clean_title(self, value: Optional[str], content: str = "") -> str:
        text = str(value or "").strip()
        if text:
            return text[:240]
        first_line = str(content or "").strip().splitlines()[0:1]
        return (first_line[0].strip()[:120] if first_line else "Untitled note") or "Untitled note"

    def embedding_text(self, title: str, content: str, tags: list[str]) -> str:
        parts = []
        if title:
            parts.append("Title: %s" % title)
        if tags:
            parts.append("Tags: %s" % ", ".join(tags))
        parts.append(str(content or ""))
        return "\n\n".join(parts).strip()

    def embedding_for(
        self,
        title: str,
        content: str,
        tags: list[str],
    ) -> tuple[Optional[list[float]], Optional[str], Optional[int], Optional[datetime]]:
        if not self.embedding_client.enabled:
            return None, None, None, None
        try:
            embedding = self.embedding_client.embed(self.embedding_text(title, content, tags))
        except Exception as exc:
            LOGGER.warning("note embedding failed: %s", exc)
            return None, None, None, None
        return embedding, self.embedding_client.model, len(embedding), datetime.now(timezone.utc)

    def public_search_row(self, row: dict[str, Any], query: str = "") -> dict[str, Any]:
        content = str(row.get("content") or "")
        result = {key: row.get(key) for key in ("id", "title", "tags", "status", "linked_entities", "metadata", "created_at", "updated_at", "last_accessed_at")}
        if "score" in row:
            result["score"] = row["score"]
        result["snippet"] = snippet(content, query)
        return result

    def log_event(
        self,
        event_type: str,
        actor: str,
        note_id: Optional[int] = None,
        job_id: Optional[int] = None,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO note_events(note_id, job_id, actor, event_type, input_data, output_data)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                note_id,
                job_id,
                actor,
                event_type,
                Jsonb(json_safe(input_data or {})),
                Jsonb(json_safe(output_data or {})),
            ),
        )

    def get(self, note_id: int) -> Optional[dict[str, Any]]:
        return self.db.fetch_one(f"SELECT {NOTE_COLUMNS} FROM agent_notes WHERE id = %s", (note_id,))

    def touch(self, note_ids: list[int]) -> None:
        for note_id in note_ids:
            self.db.execute("UPDATE agent_notes SET last_accessed_at = now() WHERE id = %s", (note_id,))

    def read(self, note_id: int, job_id: Optional[int] = None, actor: str = "system") -> dict[str, Any]:
        row = self.get(note_id)
        if row is None:
            raise ValueError("note not found")
        self.touch([note_id])
        self.log_event("read", actor, note_id=note_id, job_id=job_id, output_data={"note_id": note_id})
        return row

    def create(
        self,
        content: str,
        title: Optional[str] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        source_job_id: Optional[int] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        text = str(content or "").strip()
        if not text:
            raise ValueError("note content is required")
        clean_tags = self.clean_tags(tags)
        clean_title = self.clean_title(title, text)
        embedding, embedding_model, embedding_dimensions, embedding_updated_at = self.embedding_for(clean_title, text, clean_tags)
        row = self.db.fetch_one(
            f"""
            INSERT INTO agent_notes(
              title,
              content,
              tags,
              embedding,
              embedding_model,
              embedding_dimensions,
              embedding_updated_at,
              source_job_id,
              metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {NOTE_COLUMNS}
            """,
            (
                clean_title,
                text,
                clean_tags,
                embedding,
                embedding_model,
                embedding_dimensions,
                embedding_updated_at,
                source_job_id,
                Jsonb(json_safe(metadata or {})),
            ),
        )
        self.log_event(
            "create",
            actor,
            note_id=row["id"],
            job_id=source_job_id,
            input_data={"title": title, "tags": tags},
            output_data={"note": {key: row.get(key) for key in ("id", "title", "tags")}},
        )
        return row

    def update(
        self,
        note_id: int,
        content: Optional[str] = None,
        title: Optional[str] = None,
        tags: Optional[list[str]] = None,
        metadata: Any = UNSET,
        job_id: Optional[int] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        existing = self.get(note_id)
        if existing is None:
            raise ValueError("note not found")
        has_content = content is not None
        next_content = str(content).strip() if has_content else str(existing.get("content") or "")
        if has_content and not next_content:
            raise ValueError("note content cannot be empty")
        has_title = title is not None
        next_title = self.clean_title(title, next_content) if has_title else str(existing.get("title") or "Untitled note")
        has_tags = tags is not None
        next_tags = self.clean_tags(tags) if has_tags else list(existing.get("tags") or [])
        should_reembed = has_content or has_title or has_tags
        embedding = None
        embedding_model = None
        embedding_dimensions = None
        embedding_updated_at = None
        if should_reembed:
            embedding, embedding_model, embedding_dimensions, embedding_updated_at = self.embedding_for(next_title, next_content, next_tags)
        has_metadata = metadata is not UNSET
        row = self.db.fetch_one(
            f"""
            UPDATE agent_notes
            SET title = COALESCE(%s, title),
                content = COALESCE(%s, content),
                tags = COALESCE(%s, tags),
                metadata = CASE WHEN %s THEN %s ELSE metadata END,
                embedding = CASE WHEN %s THEN %s ELSE embedding END,
                embedding_model = CASE WHEN %s THEN %s ELSE embedding_model END,
                embedding_dimensions = CASE WHEN %s THEN %s ELSE embedding_dimensions END,
                embedding_updated_at = CASE WHEN %s THEN %s ELSE embedding_updated_at END,
                updated_at = now()
            WHERE id = %s
            RETURNING {NOTE_COLUMNS}
            """,
            (
                next_title if has_title else None,
                next_content if has_content else None,
                next_tags if has_tags else None,
                has_metadata,
                Jsonb(json_safe(metadata)) if has_metadata else None,
                should_reembed,
                embedding,
                should_reembed,
                embedding_model,
                should_reembed,
                embedding_dimensions,
                should_reembed,
                embedding_updated_at,
                note_id,
            ),
        )
        if row is None:
            raise ValueError("note not found")
        self.log_event(
            "update",
            actor,
            note_id=note_id,
            job_id=job_id,
            input_data={
                "title": title,
                "content": content,
                "tags": tags,
                "metadata": None if metadata is UNSET else metadata,
            },
            output_data={"before": {key: existing.get(key) for key in ("id", "title", "tags")}, "after": {key: row.get(key) for key in ("id", "title", "tags")}},
        )
        return row

    def delete(self, note_id: int, reason: str = "", job_id: Optional[int] = None, actor: str = "system") -> dict[str, Any]:
        existing = self.get(note_id)
        if existing is None:
            raise ValueError("note not found")
        row = self.db.fetch_one("DELETE FROM agent_notes WHERE id = %s RETURNING id, title, tags", (note_id,))
        self.log_event("delete", actor, note_id=note_id, job_id=job_id, input_data={"reason": reason}, output_data={"deleted": row})
        return row

    def keyword_search(self, query: str = "", tags: Optional[list[str]] = None, limit: int = 10) -> list[dict[str, Any]]:
        max_rows = min(max(int(limit or 10), 1), 100)
        clean_query = str(query or "").strip()
        clean_tags = self.clean_tags(tags)
        params: list[Any] = []
        filters: list[str] = ["status != 'archived'"]
        if clean_query:
            pattern = "%%%s%%" % clean_query
            filters.append(
                """
                (title ILIKE %s
                 OR content ILIKE %s
                 OR EXISTS (
                   SELECT 1
                   FROM unnest(tags) AS note_tag(tag)
                   WHERE note_tag.tag ILIKE %s
                 ))
                """
            )
            params.extend([pattern, pattern, pattern])
        if clean_tags:
            filters.append("tags && %s")
            params.append(clean_tags)
        params.append(max_rows)
        where = "WHERE %s" % " AND ".join(filters)
        rows = self.db.fetch_all(
            f"""
            SELECT {NOTE_COLUMNS}
            FROM agent_notes
            {where}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )
        self.touch([row["id"] for row in rows])
        return rows

    def semantic_search(self, query: str, tags: Optional[list[str]] = None, limit: int = 10) -> list[dict[str, Any]]:
        clean_query = str(query or "").strip()
        if not clean_query:
            return self.keyword_search("", tags=tags, limit=limit)
        try:
            query_embedding = self.embedding_client.embed(clean_query)
        except Exception as exc:
            LOGGER.warning("semantic note search falling back to keyword search: %s", exc)
            return self.keyword_search(clean_query, tags=tags, limit=limit)

        clean_tags = self.clean_tags(tags)
        params: list[Any] = []
        filters = ["embedding IS NOT NULL", "status != 'archived'"]
        if clean_tags:
            filters.append("tags && %s")
            params.append(clean_tags)
        candidate_limit = self.config.get_int("agent.notes.embeddings.candidate_limit", 1000)
        params.append(min(max(candidate_limit, 10), 5000))
        rows = self.db.fetch_all(
            f"""
            SELECT {NOTE_COLUMNS}, embedding
            FROM agent_notes
            WHERE {" AND ".join(filters)}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )
        scored = []
        for row in rows:
            score = cosine_similarity(query_embedding, row.get("embedding") or [])
            if score is None:
                continue
            clean_row = dict(row)
            clean_row.pop("embedding", None)
            clean_row["score"] = score
            scored.append(clean_row)
        scored.sort(key=lambda item: item.get("score") or 0.0, reverse=True)
        result = scored[: min(max(int(limit or 10), 1), 100)]
        if not result:
            return self.keyword_search(clean_query, tags=tags, limit=limit)
        self.touch([row["id"] for row in result])
        return result


    def reap_stale_notes(self) -> dict[str, Any]:
        """Archive active notes older than the configured threshold.
        
        Does NOT delete — changes status to 'archived' to preserve audit trail.
        """
        stale_days = self.config.get_int("agent.memory.steward.note_reap_after_days", 60)

        rows = self.db.fetch_all(
            """
            SELECT id, title, linked_entities, source_job_id, created_at, updated_at
            FROM agent_notes
            WHERE status = 'active'
              AND updated_at < now() - interval '1 day' * %s
            ORDER BY updated_at ASC
            LIMIT 100
            """,
            (stale_days,),
        )

        archived_ids = []
        for row in rows:
            self.db.execute(
                "UPDATE agent_notes SET status = 'archived', updated_at = now() WHERE id = %s AND status = 'active'",
                (row["id"],),
            )
            self.log_event(
                "archive",
                actor="note-maintenance",
                note_id=row["id"],
                input_data={"reason": "stale", "age_days": stale_days,
                            "linked_entities": row.get("linked_entities")},
            )
            archived_ids.append(row["id"])

        return {"archived_count": len(archived_ids), "archived_ids": archived_ids}


def snippet(content: str, query: str = "", max_chars: int = 500) -> str:
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
