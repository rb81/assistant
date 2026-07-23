import logging
from typing import Any, Optional

from psycopg.types.json import Jsonb

from .database import Database, json_safe


LOGGER = logging.getLogger("assistant.chat_store")


class ChatStore:
    def __init__(self, db: Database):
        self.db = db

    def create_session(self, title: str) -> dict[str, Any]:
        return self.db.fetch_one(
            "INSERT INTO chat_sessions(title) VALUES (%s) RETURNING *",
            (title,),
        )

    def get_session(self, session_id: int) -> Optional[dict[str, Any]]:
        return self.db.fetch_one("SELECT * FROM chat_sessions WHERE id = %s", (session_id,))

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """
            SELECT
              s.*,
              lm.content AS last_message_content,
              lm.role AS last_message_role,
              lm.kind AS last_message_kind,
              lm.job_id AS last_message_job_id,
              lm.created_at AS last_message_at,
              j.status AS last_job_status,
              j.metadata AS last_job_metadata,
              j.last_error AS last_job_last_error
            FROM chat_sessions s
            LEFT JOIN LATERAL (
              SELECT * FROM chat_messages cm WHERE cm.session_id = s.id ORDER BY cm.id DESC LIMIT 1
            ) lm ON true
            LEFT JOIN jobs j ON j.id = lm.job_id
            ORDER BY COALESCE(lm.created_at, s.created_at) DESC
            LIMIT %s
            """,
            (limit,),
        )

    def recent_messages(self, session_id: int, limit: int) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """
            SELECT * FROM (
              SELECT * FROM chat_messages WHERE session_id = %s ORDER BY id DESC LIMIT %s
            ) recent
            ORDER BY id ASC
            """,
            (session_id, limit),
        )

    def list_messages(self, session_id: int) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """
            SELECT
              cm.*,
              j.status AS job_status,
              j.metadata AS job_metadata,
              j.last_error AS job_last_error
            FROM chat_messages cm
            LEFT JOIN jobs j ON j.id = cm.job_id
            WHERE cm.session_id = %s
            ORDER BY cm.id ASC
            """,
            (session_id,),
        )

    def create_message(
        self,
        session_id: int,
        role: str,
        kind: str = "chat",
        content: str = "",
        job_id: Optional[int] = None,
        tokens_used: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        row = self.db.fetch_one(
            """
            INSERT INTO chat_messages(session_id, role, kind, content, job_id, tokens_used)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                session_id,
                role,
                kind,
                content,
                job_id,
                Jsonb(json_safe(tokens_used)) if tokens_used is not None else None,
            ),
        )
        self.db.execute("UPDATE chat_sessions SET updated_at = now() WHERE id = %s", (session_id,))
        return row

    def count_recent_user_messages(self, window_seconds: int) -> int:
        row = self.db.fetch_one(
            "SELECT COUNT(*) AS count FROM chat_messages WHERE role = 'user' AND created_at >= now() - (%s || ' seconds')::interval",
            (window_seconds,),
        )
        return int(row["count"]) if row else 0

    def title_from_message(self, message: str) -> str:
        text = " ".join(str(message or "").split()).strip()
        if not text:
            return "New chat"
        return text[:63].rstrip() + "…" if len(text) > 64 else text
