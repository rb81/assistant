import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .config import AppConfig, agent_name
from .context_store import ContextStore
from .database import Database
from .llm_client import LlmClient
from .memory_store import MemoryStore


LOGGER = logging.getLogger("assistant.memory_manager")
ALLOWED_MEMORY_KINDS = {"decision", "agreement", "incident", "preference", "operating_rule", "project_context"}
KIND_ALIASES = {
    "operating rule": "operating_rule",
    "operating-rule": "operating_rule",
    "project context": "project_context",
    "project-context": "project_context",
}


RECALL_SYSTEM_PROMPT = """You are a context steward for an autonomous task agent.

Given the current task and candidate context (from memories, past jobs, reminders, emails, notes, projects, and contacts), return a concise paragraph of directly relevant context.

Include references to specific object types and IDs so the agent can drill down if needed (e.g. "Job #123", "Reminder #42", "Email #215").

If no candidate context is useful for the current task, return exactly: NONE
Do not include secrets. Do not include one-off or weakly related facts.
"""


CONSOLIDATION_SYSTEM_PROMPT_TEMPLATE = """You are a strict durable-memory steward for an autonomous task agent.

Store a memory only when it is significant enough to change how %(agent_name)s should handle a future task.

Return JSON only, with this shape:
{"memories":[{"content":"...","tags":["..."],"scope":"global","kind":"decision|agreement|incident|preference|operating_rule|project_context","importance":1-5,"confidence":0.0-1.0,"why_future_relevant":"...","evidence":"...","explicit_user_requested":false,"expires_at":null}]}

Allowed kinds:
- decision: a durable choice the user/admin made.
- agreement: a commitment, approval, accepted plan, or standing arrangement.
- incident: a notable failure, safety issue, production issue, or repeated problem that should affect future behavior.
- preference: a stable user/admin preference.
- operating_rule: a durable process rule or instruction for how %(agent_name)s should work.
- project_context: durable background for an ongoing project or important long-running effort.

The storage bar is high. Prefer returning no memories.

Do store:
- Important decisions, agreements, incidents, corrections, durable preferences, standing operating rules, or important project context.
- User-requested memories, but only if they fit an allowed kind and are safe to store.

Do not store:
- Routine task summaries, successful completion notes, or "%(agent_name)s did X" bookkeeping.
- One-off implementation details, temporary statuses, transient bugs already fixed, copied source text, or facts already preserved in logs.
- Random details from an email, web page, file, or tool result unless they create durable future operating context.
- External public facts, speculation, inferred personality traits, or weakly relevant observations.
- Contact records, email addresses, phone numbers, mailing addresses, recipient routing rules, or contact-management facts. Contacts will be handled by a separate tool.
- Secrets, passwords, private keys, tokens, payment details, or sensitive credentials.

Content requirements:
- Make each memory atomic and concise.
- Include dates in content when the timing matters.
- Explain future relevance in why_future_relevant.
- Point to the evidence in evidence using a short paraphrase, not a long quote.

Return {"memories":[]} when nothing should be remembered.
"""


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
            return {"summary": "", "notes": "memory disabled"}
        try:
            memory_candidates = self.retrieve_memory_candidates(job, emails, reminder, instructions)
            context_candidates = self.retrieve_context_candidates(job, emails, reminder, instructions)
            all_candidates = memory_candidates + context_candidates
            if not all_candidates:
                return {"summary": "", "notes": "no candidate context"}
            summary = self.summarize_candidates(job, emails, reminder, instructions, memory_candidates, context_candidates)
            if summary.strip().upper() == "NONE":
                return {"summary": "", "notes": "no relevant context"}
            self.store.touch([row["id"] for row in memory_candidates])
            return {"summary": summary.strip(), "notes": "context summary from %s candidate(s)" % len(all_candidates)}
        except Exception as exc:
            self.log(job["id"], "recall_failed", {"error": str(exc)})
            if self.mode() == "required":
                raise
            return {"summary": "", "notes": "context recall failed: %s" % exc}

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
        rows.sort(key=lambda item: (bool(item.get("pinned")), int(item.get("importance") or 0), str(item.get("updated_at") or item.get("created_at") or "")), reverse=True)
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

    def summarize_candidates(
        self,
        job: dict[str, Any],
        emails: list[dict[str, Any]],
        reminder: Optional[dict[str, Any]],
        instructions: list[dict[str, Any]],
        memory_candidates: list[dict[str, Any]],
        context_candidates: list[dict[str, Any]],
    ) -> str:
        llm = self.llm(max_tokens=self.config.get_int("agent.memory.steward.max_tokens_per_call", 800))
        payload: dict[str, Any] = {
            "task": self.task_query(job, emails, reminder, instructions),
        }
        if memory_candidates:
            payload["durable_memories"] = [compact_memory(row) for row in memory_candidates]
        if context_candidates:
            payload["recent_context"] = context_candidates[:15]
        response = llm.chat(
            [
                {"role": "system", "content": RECALL_SYSTEM_PROMPT},
                {"role": "user", "content": compact_json(payload)},
            ],
            [],
        )
        return str(response["choices"][0]["message"].get("content") or "").strip()

    def consolidate(self, job: dict[str, Any], messages: list[dict[str, Any]], outcome: str, summary: str) -> None:
        if not self.enabled():
            return
        try:
            memories = self.memories_to_store(job, messages, outcome, summary)
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
                if self.duplicate_exists(content):
                    rejected.append(compact_rejected_memory(item, "duplicate"))
                    continue
                row = self.store.create(
                    content=content,
                    tags=normalized["tags"],
                    scope=normalized["scope"],
                    kind=normalized["kind"],
                    importance=normalized["importance"],
                    confidence=normalized["confidence"],
                    expires_at=normalized.get("expires_at"),
                    metadata={
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
                },
            )
        except Exception as exc:
            self.log(job["id"], "consolidation_failed", {"error": str(exc)})
            if self.mode() == "required":
                raise

    def memories_to_store(self, job: dict[str, Any], messages: list[dict[str, Any]], outcome: str, summary: str) -> list[dict[str, Any]]:
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
