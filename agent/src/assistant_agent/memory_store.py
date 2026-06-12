import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg.types.json import Jsonb

from .config import AppConfig
from .database import Database, json_safe
from .embedding_client import EmbeddingClient
from .time_utils import parse_datetime


LOGGER = logging.getLogger("assistant.memory_store")
UNSET = object()


MEMORY_COLUMNS = """
id,
content,
tags,
scope,
kind,
importance,
confidence,
expires_at,
pinned,
embedding_model,
embedding_dimensions,
embedding_updated_at,
source_job_id,
metadata,
last_accessed_at,
created_at,
updated_at
"""


class MemoryStore:
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

    def clean_text_field(self, value: Optional[str], default: str, max_length: int = 64) -> str:
        text = str(value or default).strip().lower()
        return (text or default)[:max_length]

    def clamp_importance(self, value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = 3
        return min(max(number, 1), 5)

    def clamp_confidence(self, value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 0.7
        return min(max(number, 0.0), 1.0)

    def parse_optional_datetime(self, value: Optional[str]) -> Optional[datetime]:
        if value in (None, ""):
            return None
        return parse_datetime(str(value), self.config)

    def embedding_for(self, content: str) -> tuple[Optional[list[float]], Optional[str], Optional[int], Optional[datetime]]:
        if not self.embedding_client.enabled:
            return None, None, None, None
        try:
            embedding = self.embedding_client.embed(content)
        except Exception as exc:
            LOGGER.warning("memory embedding failed: %s", exc)
            return None, None, None, None
        return embedding, self.embedding_client.model, len(embedding), datetime.now(timezone.utc)

    def log_event(
        self,
        event_type: str,
        actor: str,
        memory_id: Optional[int] = None,
        job_id: Optional[int] = None,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO memory_events(memory_id, job_id, actor, event_type, input_data, output_data)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                memory_id,
                job_id,
                actor,
                event_type,
                Jsonb(json_safe(input_data or {})),
                Jsonb(json_safe(output_data or {})),
            ),
        )

    def active_filter(self) -> str:
        return "(expires_at IS NULL OR expires_at > now())"

    def get(self, memory_id: int, include_expired: bool = False) -> Optional[dict[str, Any]]:
        filters = ["id = %s"]
        if not include_expired:
            filters.append(self.active_filter())
        return self.db.fetch_one(
            f"SELECT {MEMORY_COLUMNS} FROM agent_memories WHERE {' AND '.join(filters)}",
            (memory_id,),
        )

    def touch(self, memory_ids: list[int]) -> None:
        if not memory_ids:
            return
        for memory_id in memory_ids:
            self.db.execute("UPDATE agent_memories SET last_accessed_at = now() WHERE id = %s", (memory_id,))

    def create(
        self,
        content: str,
        tags: Optional[list[str]] = None,
        scope: str = "global",
        kind: str = "fact",
        importance: int = 3,
        confidence: float = 0.7,
        expires_at: Optional[str] = None,
        pinned: bool = False,
        metadata: Optional[dict[str, Any]] = None,
        source_job_id: Optional[int] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        text = str(content or "").strip()
        if not text:
            raise ValueError("memory content is required")
        embedding, embedding_model, embedding_dimensions, embedding_updated_at = self.embedding_for(text)
        row = self.db.fetch_one(
            f"""
            INSERT INTO agent_memories(
              content,
              tags,
              scope,
              kind,
              importance,
              confidence,
              expires_at,
              pinned,
              embedding,
              embedding_model,
              embedding_dimensions,
              embedding_updated_at,
              source_job_id,
              metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {MEMORY_COLUMNS}
            """,
            (
                text,
                self.clean_tags(tags),
                self.clean_text_field(scope, "global"),
                self.clean_text_field(kind, "fact"),
                self.clamp_importance(importance),
                self.clamp_confidence(confidence),
                self.parse_optional_datetime(expires_at),
                bool(pinned),
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
            memory_id=row["id"],
            job_id=source_job_id,
            input_data={"content": text, "tags": tags, "scope": scope, "kind": kind},
            output_data={"memory": row},
        )
        return row

    def update(
        self,
        memory_id: int,
        content: Optional[str] = None,
        tags: Optional[list[str]] = None,
        scope: Optional[str] = None,
        kind: Optional[str] = None,
        importance: Optional[int] = None,
        confidence: Optional[float] = None,
        expires_at: Any = UNSET,
        pinned: Optional[bool] = None,
        metadata: Optional[dict[str, Any]] = None,
        job_id: Optional[int] = None,
        actor: str = "system",
        include_expired: bool = False,
    ) -> dict[str, Any]:
        existing = self.get(memory_id, include_expired=include_expired)
        if existing is None:
            raise ValueError("memory not found")
        has_content = content is not None
        next_content = str(content).strip() if has_content else None
        if has_content and not next_content:
            raise ValueError("memory content cannot be empty")
        embedding = None
        embedding_model = None
        embedding_dimensions = None
        embedding_updated_at = None
        if has_content:
            embedding, embedding_model, embedding_dimensions, embedding_updated_at = self.embedding_for(next_content or "")
        has_expires_at = expires_at is not UNSET
        next_expires_at = self.parse_optional_datetime(expires_at) if has_expires_at else None
        row = self.db.fetch_one(
            f"""
            UPDATE agent_memories
            SET content = COALESCE(%s, content),
                tags = COALESCE(%s, tags),
                scope = COALESCE(%s, scope),
                kind = COALESCE(%s, kind),
                importance = COALESCE(%s, importance),
                confidence = COALESCE(%s, confidence),
                expires_at = CASE WHEN %s THEN %s ELSE expires_at END,
                pinned = COALESCE(%s, pinned),
                metadata = COALESCE(%s, metadata),
                embedding = CASE WHEN %s THEN %s ELSE embedding END,
                embedding_model = CASE WHEN %s THEN %s ELSE embedding_model END,
                embedding_dimensions = CASE WHEN %s THEN %s ELSE embedding_dimensions END,
                embedding_updated_at = CASE WHEN %s THEN %s ELSE embedding_updated_at END,
                updated_at = now()
            WHERE id = %s
            RETURNING {MEMORY_COLUMNS}
            """,
            (
                next_content,
                self.clean_tags(tags) if tags is not None else None,
                self.clean_text_field(scope, "global") if scope is not None else None,
                self.clean_text_field(kind, "fact") if kind is not None else None,
                self.clamp_importance(importance) if importance is not None else None,
                self.clamp_confidence(confidence) if confidence is not None else None,
                has_expires_at,
                next_expires_at,
                bool(pinned) if pinned is not None else None,
                Jsonb(json_safe(metadata)) if metadata is not None else None,
                has_content,
                embedding,
                has_content,
                embedding_model,
                has_content,
                embedding_dimensions,
                has_content,
                embedding_updated_at,
                memory_id,
            ),
        )
        if row is None:
            raise ValueError("memory not found")
        self.log_event(
            "update",
            actor,
            memory_id=memory_id,
            job_id=job_id,
            input_data={
                "content": content,
                "tags": tags,
                "scope": scope,
                "kind": kind,
                "importance": importance,
                "confidence": confidence,
                "expires_at": None if expires_at is UNSET else expires_at,
                "pinned": pinned,
                "metadata": metadata,
            },
            output_data={"before": existing, "after": row},
        )
        return row

    def delete(
        self,
        memory_id: int,
        reason: str = "",
        job_id: Optional[int] = None,
        actor: str = "system",
        include_expired: bool = False,
    ) -> dict[str, Any]:
        existing = self.get(memory_id, include_expired=include_expired)
        if existing is None:
            raise ValueError("memory not found")
        row = self.db.fetch_one(
            "DELETE FROM agent_memories WHERE id = %s RETURNING id, content, tags, scope, kind",
            (memory_id,),
        )
        self.log_event(
            "delete",
            actor,
            memory_id=memory_id,
            job_id=job_id,
            input_data={"reason": reason},
            output_data={"deleted": row, "before": existing},
        )
        return row

    def keyword_search(
        self,
        query: str = "",
        tags: Optional[list[str]] = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        max_rows = min(max(int(limit or 10), 1), 100)
        clean_query = str(query or "").strip()
        clean_tags = self.clean_tags(tags)
        params: list[Any] = []
        filters = [self.active_filter()]
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
        if clean_tags:
            filters.append("tags && %s")
            params.append(clean_tags)
        params.append(max_rows)
        rows = self.db.fetch_all(
            f"""
            SELECT {MEMORY_COLUMNS}
            FROM agent_memories
            WHERE {" AND ".join(filters)}
            ORDER BY pinned DESC, importance DESC, updated_at DESC, created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )
        self.touch([row["id"] for row in rows])
        return rows

    def semantic_search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        clean_query = str(query or "").strip()
        if not clean_query:
            return self.keyword_search("", limit=limit)
        try:
            query_embedding = self.embedding_client.embed(clean_query)
        except Exception as exc:
            LOGGER.warning("semantic memory search falling back to keyword search: %s", exc)
            return self.keyword_search(clean_query, limit=limit)

        candidate_limit = self.config.get_int("agent.memory.embeddings.candidate_limit", 500)
        rows = self.db.fetch_all(
            f"""
            SELECT {MEMORY_COLUMNS}, embedding
            FROM agent_memories
            WHERE embedding IS NOT NULL
              AND {self.active_filter()}
            ORDER BY pinned DESC, importance DESC, updated_at DESC, created_at DESC
            LIMIT %s
            """,
            (min(max(candidate_limit, 10), 2000),),
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
        scored.sort(key=lambda item: (item.get("score") or 0.0, item.get("importance") or 0), reverse=True)
        result = scored[: min(max(int(limit or 10), 1), 100)]
        self.touch([row["id"] for row in result])
        if not result:
            return self.keyword_search(clean_query, limit=limit)
        return result


def cosine_similarity(left: list[float], right: list[float]) -> Optional[float]:
    if not left or not right or len(left) != len(right):
        return None
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for left_value, right_value in zip(left, right):
        dot += float(left_value) * float(right_value)
        left_norm += float(left_value) * float(left_value)
        right_norm += float(right_value) * float(right_value)
    if left_norm <= 0 or right_norm <= 0:
        return None
    return dot / (math.sqrt(left_norm) * math.sqrt(right_norm))
