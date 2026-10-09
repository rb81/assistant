import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .config import AppConfig, agent_name
from .context_store import ContextStore
from .database import Database
from .llm_client import LlmClient
from .memory_store import MemoryStore, cosine_similarity


LOGGER = logging.getLogger("assistant.memory_manager")
ALLOWED_MEMORY_KINDS = {"decision", "agreement", "incident", "preference", "operating_rule", "project_context", "working_context"}
KIND_ALIASES = {
    "operating rule": "operating_rule",
    "operating-rule": "operating_rule",
    "project context": "project_context",
    "project-context": "project_context",
}

# High-signal tool names that trigger mini-reflection after job completion


RECALL_SYSTEM_PROMPT = 'You are a context steward. Return concise relevant recall with memory IDs and source evidence.\nDistinguish known source-backed facts from possibly related search results. Similarity or an exact word does not prove a relationship.\nUse only genuinely useful memories and source context for this request; do not guess, include secrets, or repeat routine bookkeeping.\nGive priority to explicit user preferences and standing rules when relevant. Return exactly NONE if nothing is useful.\nInclude drill-down IDs for memory_search, job_read, contact_read, or context_search when verification is needed.'


CONSOLIDATION_SYSTEM_PROMPT_TEMPLATE = 'You are a strict memory steward for %(agent_name)s. Return JSON only:\n{"memories":[{"content":"...","tags":["..."],"scope":"global","kind":"decision|agreement|incident|preference|operating_rule|project_context|working_context","importance":1,"confidence":0.9,"why_future_relevant":"...","evidence":"...","explicit_user_requested":false,"expires_at":null}]}\nStore atomic, concise, significant future-useful facts with source evidence. Prefer no memories to noise.\nAllowed kinds: decisions, agreements, significant incidents, durable preferences, operating rules, background to ongoing real-world work (project_context), and temporary commitments or pending follow-ups (working_context).\nWorking context expires by default. Include dates when relevant. Separate explicit user memories from uncertain inferred suggestions.\nNever conflate related facts or infer a correction from similarity. Do not store secrets, contact records, public trivia, speculation, routine successful-action logs, or copied source text.\nReturn {"memories":[]} when nothing needs remembering.'




class MemorySteward:
    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.store = MemoryStore(db, config)

    def enabled(self) -> bool:
        return self.config.get_bool("agent.memory.steward.enabled", True)

    def mode(self) -> str:
        return str(self.config.get("agent.memory.steward.mode", "best_effort") or "best_effort").strip().lower()

    def recall(
        self,
        job: dict[str, Any],
        emails: list[dict[str, Any]],
        reminder: Optional[dict[str, Any]],
        instructions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not self.enabled():
            return {"summary": ""}
        try:
            resolution = None

            # Phase 2: Entity-linked fetch (structural, deterministic)
            linked_memories = []
            linked_notes = []

            # Phase 3: Semantic/keyword fallback (existing behavior)
            memory_candidates = self.retrieve_memory_candidates(job, emails, reminder, instructions)
            context_candidates = self.retrieve_context_candidates(job, emails, reminder, instructions)

            # Phase 4: Combine and format as structured recall
            all_linked = linked_memories + linked_notes
            all_semantic = memory_candidates + context_candidates

            if not all_linked and not all_semantic:
                return {"summary": ""}

            summary = self.format_structured_recall(
                job, emails, reminder, instructions,
                resolution, linked_memories, linked_notes,
                memory_candidates, context_candidates,
            )

            if summary.strip().upper() == "NONE":
                return {"summary": ""}

            # Touch accessed memories
            accessed_ids = [row["id"] for row in linked_memories + memory_candidates if row.get("id")]
            if accessed_ids:
                self.store.touch(accessed_ids)

            return {
                "summary": summary.strip(),
                "memory_ids": accessed_ids,

            }
        except Exception as exc:
            error_str = str(exc)
            self.log(job["id"], "recall_failed", {"error": error_str})
            if self.mode() == "required":
                raise
            LOGGER.warning(
                "memory steward recall failed for job %s (mode=%s): %s",
                job["id"],
                self.mode(),
                error_str,
            )
            return {"summary": ""}



    def format_structured_recall(
        self,
        job: dict[str, Any],
        emails: list[dict[str, Any]],
        reminder: Optional[dict[str, Any]],
        instructions: list[dict[str, Any]],
        resolution: Any,
        linked_memories: list[dict[str, Any]],
        linked_notes: list[dict[str, Any]],
        semantic_memories: list[dict[str, Any]],
        semantic_context: list[dict[str, Any]],
    ) -> str:
        """Format recall as structured text with confidence tiers.

        Uses LLM to synthesize candidates into a concise structured output.
        """
        llm = self.llm(max_tokens=self.config.get_int("agent.memory.steward.max_tokens_per_call", 1200))

        task = self.task_query(job, emails, reminder, instructions)
        payload: dict[str, Any] = {
            "task": task,
            "resolved_entities": [entity.to_dict() for entity in []],
        }

        if linked_memories:
            payload["linked_memories"] = [compact_memory(row) for row in linked_memories]
        if linked_notes:
            payload["linked_notes"] = [compact_note(row) for row in linked_notes]
        if semantic_memories:
            # Exclude any already in linked set
            linked_ids = {int(row["id"]) for row in linked_memories}
            extra = [compact_memory(row) for row in semantic_memories if int(row["id"]) not in linked_ids]
            if extra:
                payload["semantic_memory_candidates"] = extra[:8]
        if semantic_context:
            # Annotate candidates where the query term appears literally in their text —
            # this gives the recall LLM a strong signal that these are genuine matches
            # rather than superficial embedding similarity.
            key_terms = _extract_key_terms(task)
            annotated = []
            for item in semantic_context[:10]:
                entry = dict(item)
                if key_terms:
                    snippet_lower = str(item.get("snippet") or "").lower()
                    entry["exact_term_match"] = any(term in snippet_lower for term in key_terms)
                annotated.append(entry)
            payload["semantic_context_candidates"] = annotated

        response = llm.chat(
            [
                {"role": "system", "content": RECALL_SYSTEM_PROMPT},
                {"role": "user", "content": compact_json(payload)},
            ],
            [],
        )
        return str(response["choices"][0]["message"].get("content") or "").strip()

    def retrieve_memory_candidates(
        self,
        job: dict[str, Any],
        emails: list[dict[str, Any]],
        reminder: Optional[dict[str, Any]],
        instructions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Retrieve candidates from durable agent_memories (original behavior)."""
        query = self.task_query(job, emails, reminder, instructions)
        limit = self.config.get_int("agent.memory.steward.max_injected_memories", 8)
        seen: dict[int, dict[str, Any]] = {}
        for row in self.store.semantic_search(query, limit=max(limit, 4)):
            seen[int(row["id"])] = row
        for row in self.store.keyword_search(query[:500], limit=max(limit, 4)):
            seen[int(row["id"])] = row
        rows = list(seen.values())
        rows.sort(key=lambda item: (float(item.get("score") or 1.0), bool(item.get("pinned")), int(item.get("importance") or 0)), reverse=True)
        return rows[: max(limit, 0)]

    def retrieve_context_candidates(
        self,
        job: dict[str, Any],
        emails: list[dict[str, Any]],
        reminder: Optional[dict[str, Any]],
        instructions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Retrieve candidates from all other sources via ContextStore."""
        if not self.config.get_bool("agent.context.enabled", True):
            return []
        query = self.task_query(job, emails, reminder, instructions)
        max_candidates = self.config.get_int("agent.context.steward_max_candidates", 10)
        try:
            context_store = ContextStore(self.db, self.config)
            return context_store.search_for_steward(query, max_candidates=max_candidates)
        except Exception as exc:
            LOGGER.warning("context store search failed during recall: %s", exc)
            return []

    def consolidate(self, job: dict[str, Any], messages: list[dict[str, Any]], outcome: str, summary: str) -> None:
        if not self.enabled():
            return
        try:
            # Resolve entities for this job to provide to consolidation LLM
            emails = self.db.latest_thread_emails(job["thread_id"], limit=5)
            reminder = self.db.fetch_one("SELECT * FROM reminders WHERE job_id = %s", (job["id"],))
            resolution = None

            memories = self.memories_to_store(job, messages, outcome, summary, resolution)
            max_writes = self.config.get_int("agent.memory.steward.max_writes_per_job", 3)
            min_importance = self.config.get_int("agent.memory.steward.min_importance", 4)
            min_confidence = self.config.get_float("agent.memory.steward.min_confidence", 0.55)
            created = []
            rejected = []
            for item in memories:
                if len(created) >= max(max_writes, 0):
                    break
                normalized, reason = normalize_memory_candidate(
                    item,
                    min_importance=min_importance,
                    min_confidence=min_confidence,
                )
                if normalized is None:
                    rejected.append(compact_rejected_memory(item, reason))
                    continue
                content = normalized["content"]
                if normalized["kind"] == "working_context" and not normalized.get("expires_at"):
                    normalized["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=self.config.get_int("agent.memory.working_context_days", 14))).isoformat()
                if self.duplicate_exists(content):
                    rejected.append(compact_rejected_memory(item, "duplicate"))
                    continue

                # Check for conflicts before creating
                conflicts = self._detect_conflicts(content, normalized["kind"])
                # Validate and attach entity links

                row = self.store.create(
                    content=content,
                    tags=normalized["tags"],
                    scope=normalized["scope"],
                    kind=normalized["kind"],
                    importance=normalized["importance"],
                    confidence=normalized["confidence"],
                    expires_at=normalized.get("expires_at"),
                    metadata={
                        "state": "suggested" if conflicts else "active",
                        "related_memory_ids": [match["id"] for match in conflicts],
                        "source": "single_shot_consolidation",
                        "outcome": outcome,
                        "category": normalized["kind"],
                        "why_future_relevant": normalized.get("why_future_relevant"),
                        "evidence": normalized.get("evidence"),
                        "explicit_user_requested": normalized.get("explicit_user_requested", False),
                    },
                    source_job_id=job["id"],
                    actor="memory-steward",
                )

                # Store entity links on the memory

                created.append(row["id"])

            self.log(
                job["id"],
                "consolidation_complete",
                {
                    "created_memory_ids": created,
                    "candidate_count": len(memories),
                    "rejected": rejected[:20],
                    "min_importance": min_importance,
                    "min_confidence": min_confidence,
                    "entities_resolved": len([]),
                },
            )
        except Exception as exc:
            self.log(job["id"], "consolidation_failed", {"error": str(exc)})
            if self.mode() == "required":
                raise

    def memories_to_store(
        self,
        job: dict[str, Any],
        messages: list[dict[str, Any]],
        outcome: str,
        summary: str,
        resolution: Any,
    ) -> list[dict[str, Any]]:
        llm = self.llm(max_tokens=self.config.get_int("agent.memory.steward.max_tokens_per_call", 1000))
        transcript = compact_json(messages[-16:])
        max_bytes = self.config.get_int("agent.memory.steward.max_transcript_bytes", 12000)
        if len(transcript) > max_bytes:
            transcript = "%s...[truncated]" % transcript[:max_bytes]
        response = llm.chat(
            [
                {"role": "system", "content": consolidation_system_prompt(self.config)},
                {
                    "role": "user",
                    "content": compact_json(
                        {
                            "job": compact_job(job),
                            "outcome": outcome,
                            "summary": summary,
                            "resolved_entities": [entity.to_dict() for entity in []],
                            "recent_transcript": transcript,
                            "current_utc_time": datetime.now(timezone.utc).isoformat(),
                        }
                    ),
                },
            ],
            [],
        )
        content = str(response["choices"][0]["message"].get("content") or "{}").strip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            self.log(job["id"], "consolidation_non_json", {"content": content[:1000]})
            return []
        memories = parsed.get("memories") if isinstance(parsed, dict) else []
        return [item for item in memories if isinstance(item, dict)] if isinstance(memories, list) else []





    def _detect_conflicts(self, content: str, kind: str) -> list[dict[str, Any]]:
        """Find existing memories of the same kind that are semantically similar."""
        if not self.config.get_bool("agent.memory.steward.conflict_detection_enabled", True):
            return []
        if not self.store.embedding_client.enabled:
            return []
        try:
            query_embedding = self.store.embedding_client.embed(content)
        except Exception:
            return []
        rows = self.db.fetch_all(
            """
            SELECT id, content, kind, importance, embedding
            FROM agent_memories
            WHERE kind = %s
              AND (expires_at IS NULL OR expires_at > now())
                            AND COALESCE(metadata->>'state', 'active') = 'active'
              AND embedding IS NOT NULL
            ORDER BY importance DESC, updated_at DESC
            LIMIT 100
            """,
            (kind,),
        )
        threshold = self.config.get_float("agent.memory.steward.conflict_similarity_threshold", 0.85)
        conflicts = []
        for row in rows:
            score = cosine_similarity(query_embedding, row.get("embedding") or [])
            if score is not None and score > threshold:
                conflicts.append({"id": row["id"], "content": row["content"], "similarity": score})
        return sorted(conflicts, key=lambda match: match["similarity"], reverse=True)

    def reap_stale_memories(self) -> dict[str, Any]:
        """Soft-expire low-importance memories that haven't been accessed recently.

        Does NOT delete — sets expires_at = now() so they stop appearing in active results.
        Logs all reaping decisions to memory_events for auditability.
        """
        stale_days = self.config.get_int("agent.memory.steward.reap_after_days", 90)
        min_importance = self.config.get_int("agent.memory.steward.reap_max_importance", 2)

        rows = self.db.fetch_all(
            """
            SELECT id, content, kind, importance, last_accessed_at, created_at
            FROM agent_memories
            WHERE (expires_at IS NULL OR expires_at > now())
              AND importance <= %s
              AND pinned = false
              AND last_accessed_at IS NOT NULL
              AND last_accessed_at < now() - (%s * interval '1 day')
            """,
            (min_importance, stale_days),
        )
        never_accessed = self.db.fetch_all(
            """
            SELECT id, content, kind, importance, last_accessed_at, created_at
            FROM agent_memories
            WHERE (expires_at IS NULL OR expires_at > now())
              AND importance <= %s
              AND pinned = false
              AND last_accessed_at IS NULL
              AND created_at < now() - (%s * interval '1 day')
            """,
            (min_importance, stale_days),
        )
        all_stale = rows + never_accessed
        reaped_ids = []
        for row in all_stale:
            self.db.execute(
                "UPDATE agent_memories SET expires_at = now() WHERE id = %s",
                (row["id"],),
            )
            self.store.log_event(
                "reap",
                actor="memory-maintenance",
                memory_id=row["id"],
                input_data={"reason": "stale", "importance": row["importance"],
                            "last_accessed_at": str(row.get("last_accessed_at"))},
            )
            reaped_ids.append(row["id"])
        return {"reaped_count": len(reaped_ids), "reaped_ids": reaped_ids}



    def duplicate_exists(self, content: str) -> bool:
        rows = self.store.keyword_search(content[:200], limit=5)
        clean = content.strip().lower()
        return any(str(row.get("content") or "").strip().lower() == clean for row in rows)

    def llm(self, max_tokens: int) -> LlmClient:
        model = str(self.config.get("agent.memory.steward.model", "openai/gpt-4.1-mini"))
        return LlmClient(
            self.config,
            model=model,
            temperature=0.0,
            max_tokens=max_tokens,
            timeout_seconds=self.config.get_int("agent.memory.steward.timeout_seconds", 45),
        )

    def task_query(
        self,
        job: dict[str, Any],
        emails: list[dict[str, Any]],
        reminder: Optional[dict[str, Any]],
        instructions: list[dict[str, Any]],
    ) -> str:
        parts = [str(job.get("task_summary") or "")]
        if reminder:
            parts.extend([str(reminder.get("title") or ""), str(reminder.get("task") or "")])
        parts.extend(str(item.get("instruction") or "") for item in instructions)
        for email in emails[-3:]:
            body = str(email.get("body_text") or email.get("body_html") or "")
            parts.extend([str(email.get("subject") or ""), body[:1500]])
        return "\n".join(part for part in parts if part.strip()).strip()

    def log(self, job_id: int, action: str, payload: dict[str, Any]) -> None:
        try:
            self.db.log_event(job_id, "supervisor_note", output_data={"source": "memory_steward", "action": action, "payload": payload})
        except Exception:
            LOGGER.exception("failed to log memory steward event")


def compact_json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=True)


def consolidation_system_prompt(config: AppConfig) -> str:
    return CONSOLIDATION_SYSTEM_PROMPT_TEMPLATE % {"agent_name": agent_name(config)}




def compact_job(job: dict[str, Any]) -> dict[str, Any]:
    return {"id": job.get("id"), "thread_id": job.get("thread_id"), "task_summary": job.get("task_summary"), "created_at": job.get("created_at")}


def compact_memory(memory: dict[str, Any]) -> dict[str, Any]:
    content = str(memory.get("content") or "")
    if len(content) > 800:
        content = "%s...[truncated]" % content[:800]
    return {
        "id": memory.get("id"),
        "content": content,
        "tags": memory.get("tags") or [],
        "scope": memory.get("scope"),
        "kind": memory.get("kind"),
        "importance": memory.get("importance"),
        "confidence": memory.get("confidence"),
        "pinned": memory.get("pinned"),
        "linked_entities": memory.get("linked_entities") or [],
        "updated_at": memory.get("updated_at"),
    }




def normalize_memory_candidate(
    item: dict[str, Any],
    *,
    min_importance: int = 4,
    min_confidence: float = 0.55,
) -> tuple[Optional[dict[str, Any]], str]:
    content = str(item.get("content") or "").strip()
    if not content:
        return None, "empty_content"

    kind = normalize_memory_kind(item.get("kind"))
    if kind not in ALLOWED_MEMORY_KINDS:
        return None, "invalid_kind:%s" % (kind or "<empty>")

    explicit_user_requested = clean_bool(item.get("explicit_user_requested"))
    importance = parse_int(item.get("importance"))
    if importance is None:
        importance = min_importance if explicit_user_requested else 0
    importance = min(max(importance, 1), 5)
    if importance < min_importance and not explicit_user_requested:
        return None, "low_importance:%s" % importance

    confidence = parse_float(item.get("confidence"))
    if confidence is None:
        confidence = 0.7
    confidence = min(max(confidence, 0.0), 1.0)
    if confidence < min_confidence and not explicit_user_requested:
        return None, "low_confidence:%s" % confidence
    if not explicit_user_requested and (not item.get("evidence") or not item.get("why_future_relevant")):
        return None, "missing_evidence_or_future_relevance"

    tags = []
    for tag in item.get("tags") or []:
        clean_tag = str(tag).strip().lower()
        if clean_tag and clean_tag not in tags:
            tags.append(clean_tag[:64])
        if len(tags) >= 20:
            break

    scope = str(item.get("scope") or "global").strip().lower()[:64] or "global"
    return (
        {
            "content": content,
            "tags": tags,
            "scope": scope,
            "kind": kind,
            "importance": importance,
            "confidence": confidence,
            "why_future_relevant": clean_optional_text(item.get("why_future_relevant"), 500),
            "evidence": clean_optional_text(item.get("evidence"), 500),
            "explicit_user_requested": explicit_user_requested,
            "expires_at": item.get("expires_at"),
        },
        "",
    )


def normalize_memory_kind(value: Any) -> str:
    raw = str(value or "").strip().lower()
    raw = KIND_ALIASES.get(raw, raw)
    return raw.replace("-", "_").replace(" ", "_")[:64]


def clean_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def parse_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clean_optional_text(value: Any, limit: int) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:limit]


def compact_rejected_memory(item: dict[str, Any], reason: str) -> dict[str, Any]:
    content = str(item.get("content") or "")
    return {
        "reason": reason,
        "kind": item.get("kind"),
        "importance": item.get("importance"),
        "confidence": item.get("confidence"),
        "content_preview": content[:180],
    }


# Stopwords that should never be treated as meaningful search key terms.
_KEY_TERM_STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "from", "have", "been",
    "will", "your", "about", "just", "more", "also", "very", "some",
    "task", "please", "reply", "email", "send", "check", "find", "look",
    "you", "are", "can", "what", "when", "where", "how", "who", "why",
    "was", "has", "had", "not", "but", "they", "then", "than", "him",
    "her", "its", "our", "all", "any", "did", "let", "get", "set",
}


def _extract_key_terms(task_text: str) -> list[str]:
    """Extract significant lowercase terms from a task string for exact-match annotation.

    Returns a list of unique words that are:
    - 4+ characters long
    - Not in the stopword list
    - Appear in the first 500 characters of the task text (focused on the subject/intent)

    Used to annotate semantic context candidates with an ``exact_term_match`` flag
    so the recall LLM can distinguish genuine name/entity matches from noise.
    """
    import re
    text = task_text[:500].lower()
    tokens = re.findall(r"[a-z][a-z0-9]{2,}", text)  # 3+ char alphanumeric words starting with letter
    seen: list[str] = []
    for token in tokens:
        if token in _KEY_TERM_STOPWORDS:
            continue
        if token not in seen:
            seen.append(token)
        if len(seen) >= 8:
            break
    return seen
