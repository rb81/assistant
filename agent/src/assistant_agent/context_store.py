"""Unified semantic context search across all agent data sources.

Provides a single search interface that queries jobs, reminders, outbound emails,
inbound emails, contacts, notes, projects, and memories — returning compact results
with source type and ID for drill-down via existing tools.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

from .config import AppConfig
from .database import Database
from .embedding_client import EmbeddingClient


LOGGER = logging.getLogger("assistant.context_store")

# Maximum characters of body text to use for embedding generation
DEFAULT_EMBEDDING_TEXT_LIMIT = 1500


class ContextStore:
    """Unified semantic search across all agent data sources."""

    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.embedding_client = EmbeddingClient(config)

    def search_days(self) -> int:
        return self.config.get_int("agent.context.search_days", 30)

    def max_results_per_source(self) -> int:
        return self.config.get_int("agent.context.max_results_per_source", 5)

    def embedding_text_limit(self) -> int:
        return self.config.get_int("agent.context.embedding_text_limit", DEFAULT_EMBEDDING_TEXT_LIMIT)

    def search(self, query: str, limit: int = 10, search_days: Optional[int] = None) -> list[dict[str, Any]]:
        """Search all sources semantically. Falls back to keyword if embedding fails."""
        clean_query = str(query or "").strip()
        if not clean_query:
            return []

        query_embedding = self._embed_query(clean_query)
        if query_embedding is not None:
            return self._semantic_search(clean_query, query_embedding, limit, search_days=search_days)
        return self._keyword_search(clean_query, limit, search_days=search_days)

    def search_for_steward(self, query: str, max_candidates: int = 15) -> list[dict[str, Any]]:
        """Search all sources for the memory steward's recall phase."""
        return self.search(query, limit=max_candidates)

    def _embed_query(self, text: str) -> Optional[list[float]]:
        if not self.embedding_client.enabled:
            return None
        try:
            return self.embedding_client.embed(text)
        except Exception as exc:
            LOGGER.warning("context search embedding failed, falling back to keyword: %s", exc)
            return None

    def _semantic_search(self, query: str, query_embedding: list[float], limit: int, search_days: Optional[int] = None) -> list[dict[str, Any]]:
        """Search across all sources using cosine similarity."""
        days = search_days if search_days is not None else self.search_days()
        per_source = self.max_results_per_source()
        all_results: list[dict[str, Any]] = []

        # Search each source
        all_results.extend(self._search_memories_semantic(query, query_embedding, per_source))
        all_results.extend(self._search_jobs_semantic(query, query_embedding, per_source, days))
        all_results.extend(self._search_reminders_semantic(query, query_embedding, per_source, days))
        all_results.extend(self._search_outbound_emails_semantic(query, query_embedding, per_source, days))
        all_results.extend(self._search_inbound_emails_semantic(query, query_embedding, per_source, days))
        all_results.extend(self._search_notes_semantic(query, query_embedding, per_source))
        all_results.extend(self._search_projects_semantic(query, query_embedding, per_source, days))
        all_results.extend(self._search_contacts_semantic(query, query_embedding, per_source))

        # Sort by score descending and return top results
        all_results.sort(key=lambda r: r.get("score", 0.0), reverse=True)
        return all_results[:limit]

    def _keyword_search(self, query: str, limit: int, search_days: Optional[int] = None) -> list[dict[str, Any]]:
        """Fallback keyword search across all sources."""
        days = search_days if search_days is not None else self.search_days()
        per_source = self.max_results_per_source()
        all_results: list[dict[str, Any]] = []

        all_results.extend(self._search_memories_keyword(query, per_source))
        all_results.extend(self._search_jobs_keyword(query, per_source, days))
        all_results.extend(self._search_reminders_keyword(query, per_source, days))
        all_results.extend(self._search_outbound_emails_keyword(query, per_source, days))
        all_results.extend(self._search_inbound_emails_keyword(query, per_source, days))
        all_results.extend(self._search_notes_keyword(query, per_source))
        all_results.extend(self._search_projects_keyword(query, per_source, days))
        all_results.extend(self._search_contacts_keyword(query, per_source))

        return all_results[:limit]

    # -------------------------------------------------------------------------
    # Semantic search helpers (per source)
    # -------------------------------------------------------------------------

    def _search_memories_semantic(self, query: str, query_embedding: list[float], limit: int) -> list[dict[str, Any]]:
        rows = self.db.fetch_all(
            """
            SELECT id, content, tags, scope, kind, importance, embedding, created_at, updated_at
            FROM agent_memories
            WHERE embedding IS NOT NULL
              AND (expires_at IS NULL OR expires_at > now())
            ORDER BY pinned DESC, importance DESC, updated_at DESC
            LIMIT %s
            """,
            (min(limit * 20, 200),),
        )
        return self._score_and_format(rows, query, query_embedding, "memory", limit, self._format_memory,
                                      text_fields=["content"])

    def _search_jobs_semantic(self, query: str, query_embedding: list[float], limit: int, days: int) -> list[dict[str, Any]]:
        rows = self.db.fetch_all(
            """
            SELECT id, task_summary, status, metadata, embedding, created_at, completed_at
            FROM jobs
            WHERE embedding IS NOT NULL
              AND created_at > now() - interval '%s days'
            ORDER BY created_at DESC
            LIMIT %s
            """ % (int(days), min(limit * 20, 200)),
        )
        return self._score_and_format(rows, query, query_embedding, "job", limit, self._format_job,
                                      text_fields=["task_summary"])

    def _search_reminders_semantic(self, query: str, query_embedding: list[float], limit: int, days: int) -> list[dict[str, Any]]:
        rows = self.db.fetch_all(
            """
            SELECT id, title, task, status, run_at, recurrence_unit, embedding, created_at, updated_at
            FROM reminders
            WHERE embedding IS NOT NULL
              AND (status = 'scheduled' OR created_at > now() - interval '%s days')
            ORDER BY run_at DESC
            LIMIT %s
            """ % (int(days), min(limit * 20, 200)),
        )
        return self._score_and_format(rows, query, query_embedding, "reminder", limit, self._format_reminder,
                                      text_fields=["title", "task"])

    def _search_outbound_emails_semantic(self, query: str, query_embedding: list[float], limit: int, days: int) -> list[dict[str, Any]]:
        rows = self.db.fetch_all(
            """
            SELECT id, to_addresses, subject, status, embedding, created_at, sent_at
            FROM outbound_email_logs
            WHERE embedding IS NOT NULL
              AND created_at > now() - interval '%s days'
              AND status = 'sent'
            ORDER BY created_at DESC
            LIMIT %s
            """ % (int(days), min(limit * 20, 200)),
        )
        return self._score_and_format(rows, query, query_embedding, "outbound_email", limit, self._format_outbound_email,
                                      text_fields=["subject"])

    def _search_inbound_emails_semantic(self, query: str, query_embedding: list[float], limit: int, days: int) -> list[dict[str, Any]]:
        rows = self.db.fetch_all(
            """
            SELECT id, from_address, to_addresses, subject, thread_id, embedding, received_at
            FROM emails
            WHERE embedding IS NOT NULL
              AND received_at > now() - interval '%s days'
            ORDER BY received_at DESC
            LIMIT %s
            """ % (int(days), min(limit * 20, 200)),
        )
        return self._score_and_format(rows, query, query_embedding, "inbound_email", limit, self._format_inbound_email,
                                      text_fields=["from_address", "subject"])

    def _search_notes_semantic(self, query: str, query_embedding: list[float], limit: int) -> list[dict[str, Any]]:
        rows = self.db.fetch_all(
            """
            SELECT id, title, content, tags, embedding, created_at, updated_at
            FROM agent_notes
            WHERE embedding IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (min(limit * 20, 200),),
        )
        return self._score_and_format(rows, query, query_embedding, "note", limit, self._format_note,
                                      text_fields=["title", "content"])

    def _search_projects_semantic(self, query: str, query_embedding: list[float], limit: int, days: int) -> list[dict[str, Any]]:
        rows = self.db.fetch_all(
            """
            SELECT id, title, status, result_summary, embedding, created_at, completed_at
            FROM projects
            WHERE embedding IS NOT NULL
              AND created_at > now() - interval '%s days'
            ORDER BY created_at DESC
            LIMIT %s
            """ % (int(days), min(limit * 20, 200)),
        )
        return self._score_and_format(rows, query, query_embedding, "project", limit, self._format_project,
                                      text_fields=["title", "result_summary"])

    def _search_contacts_semantic(self, query: str, query_embedding: list[float], limit: int) -> list[dict[str, Any]]:
        rows = self.db.fetch_all(
            """
            SELECT id, first_name, last_name, email_address, company, title, notes, embedding, updated_at
            FROM contacts
            WHERE embedding IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (min(limit * 20, 200),),
        )
        return self._score_and_format(rows, query, query_embedding, "contact", limit, self._format_contact,
                                      text_fields=["first_name", "last_name", "email_address", "company", "notes"])

    # -------------------------------------------------------------------------
    # Keyword search helpers (per source)
    # -------------------------------------------------------------------------

    def _search_memories_keyword(self, query: str, limit: int) -> list[dict[str, Any]]:
        pattern = "%%%s%%" % query
        rows = self.db.fetch_all(
            """
            SELECT id, content, tags, scope, kind, importance, created_at, updated_at
            FROM agent_memories
            WHERE (expires_at IS NULL OR expires_at > now())
              AND (content ILIKE %s OR scope ILIKE %s OR kind ILIKE %s)
            ORDER BY pinned DESC, importance DESC, updated_at DESC
            LIMIT %s
            """,
            (pattern, pattern, pattern, limit),
        )
        return [self._format_memory(row, None) for row in rows]

    def _search_jobs_keyword(self, query: str, limit: int, days: int) -> list[dict[str, Any]]:
        pattern = "%%%s%%" % query
        rows = self.db.fetch_all(
            """
            SELECT id, task_summary, status, metadata, created_at, completed_at
            FROM jobs
            WHERE created_at > now() - interval '%s days'
              AND (task_summary ILIKE %s OR metadata::text ILIKE %s)
            ORDER BY created_at DESC
            LIMIT %s
            """ % (int(days), "%s", "%s", "%s"),
            (pattern, pattern, limit),
        )
        return [self._format_job(row, None) for row in rows]

    def _search_reminders_keyword(self, query: str, limit: int, days: int) -> list[dict[str, Any]]:
        pattern = "%%%s%%" % query
        rows = self.db.fetch_all(
            """
            SELECT id, title, task, status, run_at, recurrence_unit, created_at, updated_at
            FROM reminders
            WHERE (status = 'scheduled' OR created_at > now() - interval '%s days')
              AND (title ILIKE %s OR task ILIKE %s)
            ORDER BY run_at DESC
            LIMIT %s
            """ % (int(days), "%s", "%s", "%s"),
            (pattern, pattern, limit),
        )
        return [self._format_reminder(row, None) for row in rows]

    def _search_outbound_emails_keyword(self, query: str, limit: int, days: int) -> list[dict[str, Any]]:
        pattern = "%%%s%%" % query
        rows = self.db.fetch_all(
            """
            SELECT id, to_addresses, subject, status, created_at, sent_at
            FROM outbound_email_logs
            WHERE created_at > now() - interval '%s days'
              AND status = 'sent'
              AND (subject ILIKE %s OR to_addresses::text ILIKE %s)
            ORDER BY created_at DESC
            LIMIT %s
            """ % (int(days), "%s", "%s", "%s"),
            (pattern, pattern, limit),
        )
        return [self._format_outbound_email(row, None) for row in rows]

    def _search_inbound_emails_keyword(self, query: str, limit: int, days: int) -> list[dict[str, Any]]:
        pattern = "%%%s%%" % query
        rows = self.db.fetch_all(
            """
            SELECT id, from_address, to_addresses, subject, thread_id, received_at
            FROM emails
            WHERE received_at > now() - interval '%s days'
              AND (subject ILIKE %s OR from_address ILIKE %s OR body_text ILIKE %s)
            ORDER BY received_at DESC
            LIMIT %s
            """ % (int(days), "%s", "%s", "%s", "%s"),
            (pattern, pattern, pattern, limit),
        )
        return [self._format_inbound_email(row, None) for row in rows]

    def _search_notes_keyword(self, query: str, limit: int) -> list[dict[str, Any]]:
        pattern = "%%%s%%" % query
        rows = self.db.fetch_all(
            """
            SELECT id, title, content, tags, created_at, updated_at
            FROM agent_notes
            WHERE title ILIKE %s OR content ILIKE %s
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (pattern, pattern, limit),
        )
        return [self._format_note(row, None) for row in rows]

    def _search_projects_keyword(self, query: str, limit: int, days: int) -> list[dict[str, Any]]:
        pattern = "%%%s%%" % query
        rows = self.db.fetch_all(
            """
            SELECT id, title, status, result_summary, created_at, completed_at
            FROM projects
            WHERE created_at > now() - interval '%s days'
              AND (title ILIKE %s OR result_summary ILIKE %s)
            ORDER BY created_at DESC
            LIMIT %s
            """ % (int(days), "%s", "%s", "%s"),
            (pattern, pattern, limit),
        )
        return [self._format_project(row, None) for row in rows]

    def _search_contacts_keyword(self, query: str, limit: int) -> list[dict[str, Any]]:
        pattern = "%%%s%%" % query
        rows = self.db.fetch_all(
            """
            SELECT id, first_name, last_name, email_address, company, title, notes, updated_at
            FROM contacts
            WHERE first_name ILIKE %s
              OR last_name ILIKE %s
              OR email_address ILIKE %s
              OR company ILIKE %s
              OR notes ILIKE %s
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (pattern, pattern, pattern, pattern, pattern, limit),
        )
        return [self._format_contact(row, None) for row in rows]

    # -------------------------------------------------------------------------
    # Scoring and formatting
    # -------------------------------------------------------------------------

    def _score_and_format(
        self,
        rows: list[dict[str, Any]],
        query: str,
        query_embedding: list[float],
        source: str,
        limit: int,
        formatter,
        text_fields: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Score rows by cosine similarity, boosting results that contain the query term literally.

        Minimum threshold is 0.20 (meaningfully above noise). Results where the query
        term appears verbatim in the row text receive a +0.25 boost, ensuring literal
        matches always surface above results that merely look similar in embedding space.
        """
        min_score = self.config.get_float("agent.context.min_score", 0.20)
        keyword_boost = self.config.get_float("agent.context.keyword_boost", 0.25)
        clean_query_lower = query.strip().lower() if query else ""
        scored = []
        for row in rows:
            embedding = row.get("embedding")
            if not embedding:
                continue
            score = cosine_similarity(query_embedding, embedding)
            if score is None:
                continue
            # Apply keyword boost when the query term appears literally in any text field
            if clean_query_lower and text_fields:
                for field in text_fields:
                    field_value = str(row.get(field) or "").lower()
                    if clean_query_lower in field_value:
                        score = score + keyword_boost
                        break
            if score < min_score:
                continue
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [formatter(row, score) for score, row in scored[:limit]]

    # -------------------------------------------------------------------------
    # Result formatters — produce compact results for the agent
    # -------------------------------------------------------------------------

    def _format_memory(self, row: dict[str, Any], score: Optional[float]) -> dict[str, Any]:
        content = str(row.get("content") or "")
        return {
            "source": "memory",
            "id": row["id"],
            "snippet": content[:200],
            "score": score,
            "kind": row.get("kind"),
            "importance": row.get("importance"),
            "tags": row.get("tags") or [],
            "updated_at": str(row.get("updated_at") or row.get("created_at") or ""),
        }

    def _format_job(self, row: dict[str, Any], score: Optional[float]) -> dict[str, Any]:
        summary = str(row.get("task_summary") or "")
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        final_response = str(metadata.get("final_response") or "")[:100]
        snippet = summary
        if final_response:
            snippet = "%s → %s" % (summary, final_response)
        return {
            "source": "job",
            "id": row["id"],
            "snippet": snippet[:250],
            "score": score,
            "status": row.get("status"),
            "created_at": str(row.get("created_at") or ""),
            "completed_at": str(row.get("completed_at") or ""),
        }

    def _format_reminder(self, row: dict[str, Any], score: Optional[float]) -> dict[str, Any]:
        title = str(row.get("title") or "")
        task = str(row.get("task") or "")
        snippet = "%s: %s" % (title, task[:150]) if task else title
        return {
            "source": "reminder",
            "id": row["id"],
            "snippet": snippet[:250],
            "score": score,
            "status": row.get("status"),
            "run_at": str(row.get("run_at") or ""),
            "recurrence": row.get("recurrence_unit"),
        }

    def _format_outbound_email(self, row: dict[str, Any], score: Optional[float]) -> dict[str, Any]:
        to_addrs = row.get("to_addresses") or []
        subject = str(row.get("subject") or "")
        recipients = ", ".join(str(addr) for addr in to_addrs[:3])
        snippet = "To: %s — %s" % (recipients, subject)
        return {
            "source": "outbound_email",
            "id": row["id"],
            "snippet": snippet[:250],
            "score": score,
            "sent_at": str(row.get("sent_at") or row.get("created_at") or ""),
        }

    def _format_inbound_email(self, row: dict[str, Any], score: Optional[float]) -> dict[str, Any]:
        from_addr = str(row.get("from_address") or "")
        subject = str(row.get("subject") or "")
        snippet = "From: %s — %s" % (from_addr, subject)
        return {
            "source": "inbound_email",
            "id": row["id"],
            "snippet": snippet[:250],
            "score": score,
            "thread_id": row.get("thread_id"),
            "received_at": str(row.get("received_at") or ""),
        }

    def _format_note(self, row: dict[str, Any], score: Optional[float]) -> dict[str, Any]:
        title = str(row.get("title") or "")
        content = str(row.get("content") or "")
        snippet = "%s: %s" % (title, content[:150]) if content else title
        return {
            "source": "note",
            "id": row["id"],
            "snippet": snippet[:250],
            "score": score,
            "tags": row.get("tags") or [],
            "updated_at": str(row.get("updated_at") or row.get("created_at") or ""),
        }

    def _format_project(self, row: dict[str, Any], score: Optional[float]) -> dict[str, Any]:
        title = str(row.get("title") or "")
        result = str(row.get("result_summary") or "")[:100]
        snippet = "%s → %s" % (title, result) if result else title
        return {
            "source": "project",
            "id": row["id"],
            "snippet": snippet[:250],
            "score": score,
            "status": row.get("status"),
            "created_at": str(row.get("created_at") or ""),
        }

    def _format_contact(self, row: dict[str, Any], score: Optional[float]) -> dict[str, Any]:
        name_parts = [str(row.get("first_name") or ""), str(row.get("last_name") or "")]
        name = " ".join(part for part in name_parts if part.strip()).strip()
        email_addr = str(row.get("email_address") or "")
        company = str(row.get("company") or "")
        parts = [part for part in [name, email_addr, company] if part]
        snippet = " — ".join(parts)
        return {
            "source": "contact",
            "id": row["id"],
            "snippet": snippet[:250],
            "score": score,
        }


# -------------------------------------------------------------------------
# Embedding text builders — used when generating embeddings for each source
# -------------------------------------------------------------------------


def job_embedding_text(row: dict[str, Any]) -> str:
    """Build embedding text for a job record."""
    parts = []
    summary = str(row.get("task_summary") or "").strip()
    if summary:
        parts.append(summary)
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    final_response = str(metadata.get("final_response") or "").strip()
    if final_response:
        parts.append(final_response[:DEFAULT_EMBEDDING_TEXT_LIMIT])
    return "\n\n".join(parts).strip()


def reminder_embedding_text(row: dict[str, Any]) -> str:
    """Build embedding text for a reminder record."""
    title = str(row.get("title") or "").strip()
    task = str(row.get("task") or "").strip()
    parts = []
    if title:
        parts.append("Reminder: %s" % title)
    if task:
        parts.append(task[:DEFAULT_EMBEDDING_TEXT_LIMIT])
    return "\n\n".join(parts).strip()


def outbound_email_embedding_text(row: dict[str, Any]) -> str:
    """Build embedding text for an outbound email log record."""
    to_addrs = row.get("to_addresses") or []
    recipients = ", ".join(str(addr) for addr in to_addrs[:5])
    subject = str(row.get("subject") or "").strip()
    body = str(row.get("body_text") or "").strip()
    parts = []
    if recipients:
        parts.append("To: %s" % recipients)
    if subject:
        parts.append("Subject: %s" % subject)
    if body:
        parts.append(body[:DEFAULT_EMBEDDING_TEXT_LIMIT])
    return "\n".join(parts).strip()


def inbound_email_embedding_text(row: dict[str, Any]) -> str:
    """Build embedding text for an inbound email record."""
    from_addr = str(row.get("from_address") or "").strip()
    subject = str(row.get("subject") or "").strip()
    body = str(row.get("body_text") or row.get("body_html") or "").strip()
    parts = []
    if from_addr:
        parts.append("From: %s" % from_addr)
    if subject:
        parts.append("Subject: %s" % subject)
    if body:
        parts.append(body[:DEFAULT_EMBEDDING_TEXT_LIMIT])
    return "\n".join(parts).strip()


def contact_embedding_text(row: dict[str, Any]) -> str:
    """Build embedding text for a contact record."""
    parts = []
    name_parts = [str(row.get("first_name") or ""), str(row.get("last_name") or "")]
    name = " ".join(part for part in name_parts if part.strip()).strip()
    if name:
        parts.append("Name: %s" % name)
    email_addr = str(row.get("email_address") or "").strip()
    if email_addr:
        parts.append("Email: %s" % email_addr)
    company = str(row.get("company") or "").strip()
    if company:
        parts.append("Company: %s" % company)
    title = str(row.get("title") or "").strip()
    if title:
        parts.append("Title: %s" % title)
    notes = str(row.get("notes") or "").strip()
    if notes:
        parts.append(notes[:DEFAULT_EMBEDDING_TEXT_LIMIT])
    return "\n".join(parts).strip()


def project_embedding_text(row: dict[str, Any]) -> str:
    """Build embedding text for a project record."""
    title = str(row.get("title") or "").strip()
    result = str(row.get("result_summary") or "").strip()
    parts = []
    if title:
        parts.append("Project: %s" % title)
    if result:
        parts.append(result[:DEFAULT_EMBEDDING_TEXT_LIMIT])
    return "\n\n".join(parts).strip()


# -------------------------------------------------------------------------
# Embedding generation utility
# -------------------------------------------------------------------------


def generate_embedding(
    embedding_client: EmbeddingClient,
    text: str,
) -> tuple[Optional[list[float]], Optional[str], Optional[int], Optional[datetime]]:
    """Generate an embedding for the given text. Returns (embedding, model, dimensions, timestamp) or all Nones."""
    if not embedding_client.enabled:
        return None, None, None, None
    clean = text.strip()
    if not clean:
        return None, None, None, None
    try:
        embedding = embedding_client.embed(clean)
    except Exception as exc:
        LOGGER.warning("context embedding failed: %s", exc)
        return None, None, None, None
    return embedding, embedding_client.model, len(embedding), datetime.now(timezone.utc)


# -------------------------------------------------------------------------
# Cosine similarity (same algorithm as memory_store but avoids circular import)
# -------------------------------------------------------------------------


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
