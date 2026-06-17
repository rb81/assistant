"""Entity resolution for associative memory recall.

Resolves fuzzy mentions in task context to canonical domain object references
(contacts, projects, reminders, threads). This module is strictly read-only —
it never creates entities. Creation happens during consolidation or reflection.

Entity references use polymorphic URNs:
  {"type": "contact", "ref_id": 42, "label": "John Smith (VP Sales, Apple)"}
  {"type": "project", "ref_id": 7, "label": "Q3 Planning (running)"}
  {"type": "thread", "ref_id": "abc123", "label": "RE: Budget approval"}
  {"type": "reminder", "ref_id": 15, "label": "Follow up with John (scheduled)"}
"""

import logging
import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Any, Optional


from .config import AppConfig
from .database import Database
from .embedding_client import EmbeddingClient


LOGGER = logging.getLogger("assistant.entity_resolver")


@dataclass
class EntityRef:
    """A resolved reference to an existing domain object."""

    type: str  # "contact", "project", "thread", "reminder"
    ref_id: Any  # int for contacts/projects/reminders, str for threads
    label: str  # Human-readable label for prompt display
    confidence: float = 1.0  # 1.0 = deterministic match, <1.0 = fuzzy/semantic

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "ref_id": self.ref_id, "label": self.label, "confidence": self.confidence}

    def link_dict(self) -> dict[str, Any]:
        """Minimal dict for storage in linked_entities column."""
        return {"type": self.type, "ref_id": self.ref_id}


@dataclass
class ResolutionResult:
    """Result of entity resolution for a task."""

    entities: list[EntityRef] = field(default_factory=list)
    # Mentions that could not be resolved (for logging/debugging)
    unresolved_mentions: list[str] = field(default_factory=list)


class EntityResolver:
    """Resolves fuzzy entity mentions to canonical domain object references.

    Resolution is strictly read-only and never creates new records.
    Strategy (ordered by cost and certainty):
      1. Deterministic extraction (sender email → contact, thread_id → jobs)
      2. Name/alias matching (names in text → contacts, project titles → projects)
      3. Embedding fallback (vector similarity for ambiguous mentions)
    """

    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.embedding_client = EmbeddingClient(config)

    def resolve_from_task(
        self,
        job: dict[str, Any],
        emails: list[dict[str, Any]],
        reminder: Optional[dict[str, Any]],
        instructions: list[dict[str, Any]],
    ) -> ResolutionResult:
        """Extract and resolve entity references from task context.

        This is the main entry point called during the recall phase.
        Returns resolved entities — never creates anything.
        """
        result = ResolutionResult()
        seen_keys: set[str] = set()

        # 1. Deterministic: sender email → contact
        for email in emails:
            address = self._parse_email_address(email.get("from_address"))
            if address:
                contact = self._contact_by_email(address)
                if contact:
                    ref = self._contact_ref(contact)
                    key = "contact:%s" % ref.ref_id
                    if key not in seen_keys:
                        seen_keys.add(key)
                        result.entities.append(ref)

        # 2. Deterministic: thread_id → recent jobs in same thread
        thread_id = job.get("thread_id")
        if thread_id:
            ref = self._thread_ref(thread_id)
            if ref:
                key = "thread:%s" % ref.ref_id
                if key not in seen_keys:
                    seen_keys.add(key)
                    result.entities.append(ref)

        # 3. Deterministic: linked reminder
        if reminder:
            ref = self._reminder_ref(reminder)
            key = "reminder:%s" % ref.ref_id
            if key not in seen_keys:
                seen_keys.add(key)
                result.entities.append(ref)

        # 4. Name matching: extract person names from task text → contacts
        task_text = self._build_task_text(job, emails, reminder, instructions)
        name_contacts = self._contacts_by_name_mentions(task_text, exclude_ids={
            ref.ref_id for ref in result.entities if ref.type == "contact"
        })
        for contact in name_contacts:
            ref = self._contact_ref(contact)
            key = "contact:%s" % ref.ref_id
            if key not in seen_keys:
                seen_keys.add(key)
                ref.confidence = 0.85  # name match, not email match
                result.entities.append(ref)

        # 5. Project title matching: keywords in task → active projects
        project_refs = self._projects_by_text_match(task_text, limit=3)
        for ref in project_refs:
            key = "project:%s" % ref.ref_id
            if key not in seen_keys:
                seen_keys.add(key)
                result.entities.append(ref)

        # 6. Embedding fallback: if we have < 2 structural results, try semantic
        if len(result.entities) < 2 and self.embedding_client.enabled:
            semantic_refs = self._semantic_contact_search(task_text, exclude_ids={
                ref.ref_id for ref in result.entities if ref.type == "contact"
            })
            for ref in semantic_refs:
                key = "contact:%s" % ref.ref_id
                if key not in seen_keys:
                    seen_keys.add(key)
                    result.entities.append(ref)

        return result

    def hydrate_entity(self, ref: EntityRef) -> dict[str, Any]:
        """Fetch current data from the source table for a given entity ref.

        Returns enriched metadata for prompt display.
        """
        if ref.type == "contact":
            return self._hydrate_contact(ref.ref_id)
        if ref.type == "project":
            return self._hydrate_project(ref.ref_id)
        if ref.type == "reminder":
            return self._hydrate_reminder(ref.ref_id)
        if ref.type == "thread":
            return self._hydrate_thread(ref.ref_id)
        return {"type": ref.type, "ref_id": ref.ref_id, "label": ref.label}

    # -------------------------------------------------------------------------
    # Deterministic resolution helpers
    # -------------------------------------------------------------------------

    def _contact_by_email(self, email_address: str) -> Optional[dict[str, Any]]:
        if not email_address:
            return None
        return self.db.fetch_one(
            "SELECT * FROM contacts WHERE lower(email_address) = %s LIMIT 1",
            (email_address.lower(),),
        )

    def _contact_ref(self, contact: dict[str, Any]) -> EntityRef:
        name_parts = [str(contact.get("first_name") or ""), str(contact.get("last_name") or "")]
        name = " ".join(part for part in name_parts if part.strip()).strip()
        company = str(contact.get("company") or "").strip()
        title = str(contact.get("title") or "").strip()
        label_parts = [name]
        if title and company:
            label_parts.append("(%s, %s)" % (title, company))
        elif company:
            label_parts.append("(%s)" % company)
        elif title:
            label_parts.append("(%s)" % title)
        return EntityRef(type="contact", ref_id=int(contact["id"]), label=" ".join(label_parts).strip())

    def _thread_ref(self, thread_id: str) -> Optional[EntityRef]:
        """Create a thread ref from recent jobs in this thread."""
        row = self.db.fetch_one(
            """
            SELECT id, task_summary, status, completed_at
            FROM jobs
            WHERE thread_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (thread_id,),
        )
        if not row:
            return None
        summary = str(row.get("task_summary") or "").strip()[:100]
        if not summary:
            return None
        return EntityRef(type="thread", ref_id=thread_id, label=summary)

    def _reminder_ref(self, reminder: dict[str, Any]) -> EntityRef:
        title = str(reminder.get("title") or "").strip()
        status = str(reminder.get("status") or "").strip()
        label = "%s (%s)" % (title, status) if status else title
        return EntityRef(type="reminder", ref_id=int(reminder["id"]), label=label)

    # -------------------------------------------------------------------------
    # Name/keyword matching
    # -------------------------------------------------------------------------

    def _contacts_by_name_mentions(
        self,
        text: str,
        exclude_ids: set[Any],
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Find contacts whose names appear in the task text.

        Tries multi-word capitalized names first (highest confidence), then
        falls back to single capitalized tokens (filtered against a stoplist
        of common English words to avoid false positives).
        """
        if not text.strip():
            return []

        results: list[dict[str, Any]] = []
        seen_ids: set[int] = set(int(item) for item in exclude_ids if item is not None)

        # --- Phase 1: multi-word capitalized names (e.g. "Zevae Zaheer") ---
        multi_name_pattern = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
        potential_multi = multi_name_pattern.findall(text)
        for name in potential_multi[:10]:
            parts = name.strip().split()
            if len(parts) < 2:
                continue
            rows = self.db.fetch_all(
                """
                SELECT * FROM contacts
                WHERE (lower(first_name) = %s AND lower(last_name) = %s)
                   OR (lower(first_name) || ' ' || lower(last_name)) = %s
                LIMIT 2
                """,
                (parts[0].lower(), parts[-1].lower(), name.lower()),
            )
            for row in rows:
                contact_id = int(row["id"])
                if contact_id not in seen_ids:
                    seen_ids.add(contact_id)
                    results.append(row)
                    if len(results) >= limit:
                        return results

        if len(results) >= limit:
            return results

        # --- Phase 2: single capitalized tokens ---
        # Catches unusual proper names like "Zevae" that appear alone in text.
        # The DB query itself acts as the filter — if nothing matches in contacts,
        # the token is silently skipped. No static stoplist needed.
        single_name_pattern = re.compile(r"\b([A-Z][a-z]{2,})\b")
        potential_single = single_name_pattern.findall(text)
        already_in_multi = {
            part
            for name in potential_multi
            for part in name.split()
        }
        # Cap single-token lookups to avoid excessive DB queries
        single_token_limit = 5
        single_token_count = 0
        for token in potential_single:
            if single_token_count >= single_token_limit:
                break
            # Skip tokens already matched as part of a multi-word name
            if token in already_in_multi:
                continue
            single_token_count += 1
            rows = self.db.fetch_all(
                """
                SELECT * FROM contacts
                WHERE lower(first_name) = %s
                LIMIT 2
                """,
                (token.lower(),),
            )
            for row in rows:
                contact_id = int(row["id"])
                if contact_id not in seen_ids:
                    seen_ids.add(contact_id)
                    results.append(row)
                    if len(results) >= limit:
                        return results

        return results

    def _projects_by_text_match(self, text: str, limit: int = 3) -> list[EntityRef]:
        """Find active projects whose titles appear in the task text."""
        if not text.strip():
            return []
        # Get active projects and check if their title words appear in text
        rows = self.db.fetch_all(
            """
            SELECT id, title, status
            FROM projects
            WHERE status IN ('queued', 'running')
            ORDER BY updated_at DESC
            LIMIT 20
            """,
        )
        text_lower = text.lower()
        results: list[EntityRef] = []
        for row in rows:
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            # Check if significant words from the project title appear in the text
            title_words = [word for word in title.lower().split() if len(word) > 3]
            if not title_words:
                continue
            match_count = sum(1 for word in title_words if word in text_lower)
            if match_count >= max(len(title_words) // 2, 1):
                label = "%s (status: %s)" % (title, row.get("status") or "unknown")
                results.append(EntityRef(
                    type="project",
                    ref_id=int(row["id"]),
                    label=label,
                    confidence=0.75,
                ))
                if len(results) >= limit:
                    break
        return results

    # -------------------------------------------------------------------------
    # Semantic fallback
    # -------------------------------------------------------------------------

    def _semantic_contact_search(
        self,
        text: str,
        exclude_ids: set[Any],
        limit: int = 2,
    ) -> list[EntityRef]:
        """Last-resort: embed the task text and find similar contacts."""
        if not text.strip():
            return []
        try:
            query_embedding = self.embedding_client.embed(text[:1000])
        except Exception as exc:
            LOGGER.debug("semantic contact search embedding failed: %s", exc)
            return []

        rows = self.db.fetch_all(
            """
            SELECT id, first_name, last_name, company, title, embedding
            FROM contacts
            WHERE embedding IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT 100
            """,
        )

        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            if int(row["id"]) in exclude_ids:
                continue
            embedding = row.get("embedding")
            if not embedding:
                continue
            score = _cosine_similarity(query_embedding, embedding)
            if score is not None and score > 0.4:
                scored.append((score, row))

        scored.sort(key=lambda item: item[0], reverse=True)
        results: list[EntityRef] = []
        for score, row in scored[:limit]:
            ref = self._contact_ref(row)
            ref.confidence = min(score, 0.7)  # cap confidence for semantic matches
            results.append(ref)
        return results

    # -------------------------------------------------------------------------
    # Hydration (fetch current state from source tables)
    # -------------------------------------------------------------------------

    def _hydrate_contact(self, ref_id: int) -> dict[str, Any]:
        row = self.db.fetch_one("SELECT * FROM contacts WHERE id = %s", (ref_id,))
        if not row:
            return {"type": "contact", "ref_id": ref_id, "label": "[deleted contact]"}
        name_parts = [str(row.get("first_name") or ""), str(row.get("last_name") or "")]
        name = " ".join(part for part in name_parts if part.strip()).strip()
        return {
            "type": "contact",
            "ref_id": ref_id,
            "name": name,
            "email": row.get("email_address") or "",
            "company": row.get("company") or "",
            "title": row.get("title") or "",
        }

    def _hydrate_project(self, ref_id: int) -> dict[str, Any]:
        row = self.db.fetch_one("SELECT id, title, status, result_summary FROM projects WHERE id = %s", (ref_id,))
        if not row:
            return {"type": "project", "ref_id": ref_id, "label": "[deleted project]"}
        return {
            "type": "project",
            "ref_id": ref_id,
            "title": row.get("title") or "",
            "status": row.get("status") or "",
            "summary": (str(row.get("result_summary") or "")[:100]).strip() or None,
        }

    def _hydrate_reminder(self, ref_id: int) -> dict[str, Any]:
        row = self.db.fetch_one("SELECT id, title, task, status, run_at FROM reminders WHERE id = %s", (ref_id,))
        if not row:
            return {"type": "reminder", "ref_id": ref_id, "label": "[deleted reminder]"}
        return {
            "type": "reminder",
            "ref_id": ref_id,
            "title": row.get("title") or "",
            "status": row.get("status") or "",
            "run_at": str(row.get("run_at") or ""),
        }

    def _hydrate_thread(self, ref_id: str) -> dict[str, Any]:
        row = self.db.fetch_one(
            """
            SELECT id, task_summary, status, completed_at
            FROM jobs
            WHERE thread_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (ref_id,),
        )
        if not row:
            return {"type": "thread", "ref_id": ref_id, "label": "[no jobs in thread]"}
        return {
            "type": "thread",
            "ref_id": ref_id,
            "latest_job_id": row.get("id"),
            "task_summary": str(row.get("task_summary") or "")[:150],
            "status": row.get("status") or "",
        }

    # -------------------------------------------------------------------------
    # Utility
    # -------------------------------------------------------------------------

    def _parse_email_address(self, value: Any) -> str:
        raw = str(value or "").strip()
        parsed = parseaddr(raw)[1]
        address = (parsed or raw).lower().strip()
        if address.endswith("@local"):
            return ""
        return address

    def _build_task_text(
        self,
        job: dict[str, Any],
        emails: list[dict[str, Any]],
        reminder: Optional[dict[str, Any]],
        instructions: list[dict[str, Any]],
    ) -> str:
        """Build a combined text from the task for name/keyword extraction."""
        parts = [str(job.get("task_summary") or "")]
        if reminder:
            parts.extend([str(reminder.get("title") or ""), str(reminder.get("task") or "")])
        parts.extend(str(item.get("instruction") or "") for item in instructions)
        for email in emails[-3:]:
            parts.extend([
                str(email.get("subject") or ""),
                str(email.get("body_text") or email.get("body_html") or "")[:2000],
            ])
        return "\n".join(part for part in parts if part.strip()).strip()


def _cosine_similarity(left: list[float], right: list[float]) -> Optional[float]:
    """Compute cosine similarity between two vectors."""
    import math

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
