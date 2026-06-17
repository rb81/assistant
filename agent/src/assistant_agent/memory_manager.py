import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .config import AppConfig, agent_name
from .context_store import ContextStore
from .database import Database
from .entity_resolver import EntityRef, EntityResolver, ResolutionResult
from .llm_client import LlmClient
from .memory_store import MemoryStore, cosine_similarity


LOGGER = logging.getLogger("assistant.memory_manager")
ALLOWED_MEMORY_KINDS = {"decision", "agreement", "incident", "preference", "operating_rule", "project_context"}
KIND_ALIASES = {
    "operating rule": "operating_rule",
    "operating-rule": "operating_rule",
    "project context": "project_context",
    "project-context": "project_context",
}

# High-signal tool names that trigger mini-reflection after job completion
HIGH_SIGNAL_TOOLS = {
    "calendar_create_event",
    "calendar_update_event",
    "calendar_delete_event",
    "email_send",
    "contact_create",
    "project_create",
}


RECALL_SYSTEM_PROMPT = """You are a context steward for an autonomous task agent.

Given the current task, resolved entities, and candidate context (from memories, past jobs, reminders, emails, notes, projects, and contacts), return a concise structured recall.

Format your response as follows:

LINKED CONTEXT (verified — structurally connected to this task):
- [type] #[id]: [one-line summary of what's relevant] [entity label in brackets]

POSSIBLY RELATED (found by similarity — agent should verify before acting):
- [type] #[id]: [one-line summary] [reason for uncertainty]

DRILL-DOWN: Use note_read, job_read, or context_search to verify uncertain items. Do not guess contents from titles.

Rules:
- Only include items that are genuinely useful for the current task.
- Mark items as LINKED if they are structurally connected via entity links.
- Mark items as POSSIBLY RELATED if found only by text similarity.
- semantic_context_candidates items with "exact_term_match": true contain the query term literally in their text — treat these as high-confidence factual matches. Give them extra weight; include unless clearly off-topic.
- semantic_context_candidates items with "exact_term_match": false were found by embedding similarity — judge them on content relevance to the task, not the flag. Many genuinely useful context items will have this flag as false.
- If no candidate context is useful, return exactly: NONE
- Do not include secrets. Do not include one-off or weakly related facts.
- Be concise — a few lines per section maximum.
"""


CONSOLIDATION_SYSTEM_PROMPT_TEMPLATE = """You are a strict durable-memory steward for an autonomous task agent.

Store a memory only when it is significant enough to change how %(agent_name)s should handle a future task.

Return JSON only, with this shape:
{"memories":[{"content":"...","tags":["..."],"scope":"global","kind":"decision|agreement|incident|preference|operating_rule|project_context","importance":1-5,"confidence":0.0-1.0,"why_future_relevant":"...","evidence":"...","explicit_user_requested":false,"expires_at":null,"entity_links":[{"type":"contact|project|thread|job|reminder","ref_id":"..."}]}]}

Allowed kinds:
- decision: a durable choice the user/admin made.
- agreement: a commitment, approval, accepted plan, or standing arrangement.
- incident: a notable failure, safety issue, production issue, or repeated problem that should affect future behavior.
- preference: a stable user/admin preference.
- operating_rule: a durable process rule or instruction for how %(agent_name)s should work.
- project_context: durable background for an ongoing project or important long-running effort.

Entity linking rules:
- Link memories to entities that are DIRECTLY relevant (the person, project, or thread the memory is about).
- Do NOT link tangentially mentioned entities. Only link if the memory would be needed when working on that entity in the future.
- Use the resolved_entities provided to identify valid entity refs.

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


MINI_REFLECTION_SYSTEM_PROMPT_TEMPLATE = """You are a working-memory steward for an autonomous task agent named %(agent_name)s.

After completing a task that involved significant actions (calendar changes, external emails, new contacts, or projects), you review what happened and decide what working knowledge to capture for future recall.

Given the job summary, outcome, and resolved entities, return JSON:
{"notes":[{"title":"...","content":"...","tags":["..."],"status":"active","linked_entities":[{"type":"contact|project|thread|job|reminder","ref_id":"..."}],"action":"create|update","update_note_id":null}]}

Rules:
- Capture working knowledge that will help %(agent_name)s recall context in future interactions with the same people/projects.
- Examples: meeting details, commitments made, pending follow-ups, important dates, relationship context.
- Link each note to the specific entities it's about (people, projects, threads).
- Keep notes atomic (one topic per note) and concise.
- If an existing note should be updated rather than creating a new one, use action:"update" with the update_note_id.
- Do NOT create notes for routine actions that are already in logs.
- Do NOT create notes for one-off tasks with no future relevance.
- Return {"notes":[]} if nothing is worth noting.
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
            # Phase 1: Resolve entities from task context (read-only)
            resolver = EntityResolver(self.db, self.config)
            resolution = resolver.resolve_from_task(job, emails, reminder, instructions)

            # Phase 2: Entity-linked fetch (structural, deterministic)
            linked_memories = self.fetch_memories_by_entity_links(resolution.entities)
            linked_notes = self.fetch_notes_by_entity_links(resolution.entities)

            # Phase 3: Semantic/keyword fallback (existing behavior)
            memory_candidates = self.retrieve_memory_candidates(job, emails, reminder, instructions)
            context_candidates = self.retrieve_context_candidates(job, emails, reminder, instructions)

            # Phase 4: Combine and format as structured recall
            all_linked = linked_memories + linked_notes
            all_semantic = memory_candidates + context_candidates

            if not all_linked and not all_semantic:
                return {"summary": "", "notes": "no candidate context"}

            summary = self.format_structured_recall(
                job, emails, reminder, instructions,
                resolution, linked_memories, linked_notes,
                memory_candidates, context_candidates,
            )

            if summary.strip().upper() == "NONE":
                return {"summary": "", "notes": "no relevant context"}

            # Touch accessed memories
            accessed_ids = [row["id"] for row in linked_memories + memory_candidates if row.get("id")]
            if accessed_ids:
                self.store.touch(accessed_ids)

            return {
                "summary": summary.strip(),
                "notes": "structured recall from %s linked + %s semantic candidate(s), %s entities resolved" % (
                    len(all_linked), len(all_semantic), len(resolution.entities)
                ),
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
            return {"summary": "", "notes": "context recall failed: %s" % exc}

    def fetch_memories_by_entity_links(self, entities: list[EntityRef]) -> list[dict[str, Any]]:
        """Fetch memories linked to resolved entities via JSONB containment."""
        if not entities:
            return []
        seen: dict[int, dict[str, Any]] = {}
        limit = self.config.get_int("agent.memory.steward.max_injected_memories", 8)
        for entity in entities:
            link_filter = json.dumps([entity.link_dict()])
            rows = self.db.fetch_all(
                """
                SELECT id, content, tags, scope, kind, importance, confidence, pinned,
                       linked_entities, updated_at, created_at
                FROM agent_memories
                WHERE linked_entities @> %s::jsonb
                  AND (expires_at IS NULL OR expires_at > now())
                ORDER BY pinned DESC, importance DESC, updated_at DESC
                LIMIT %s
                """,
                (link_filter, limit),
            )
            for row in rows:
                seen[int(row["id"])] = row
        results = list(seen.values())
        results.sort(key=lambda item: (bool(item.get("pinned")), int(item.get("importance") or 0)), reverse=True)
        return results[:limit]

    def fetch_notes_by_entity_links(self, entities: list[EntityRef]) -> list[dict[str, Any]]:
        """Fetch active notes linked to resolved entities."""
        if not entities:
            return []
        seen: dict[int, dict[str, Any]] = {}
        limit = self.config.get_int("agent.memory.steward.max_injected_notes", 6)
        for entity in entities:
            link_filter = json.dumps([entity.link_dict()])
            rows = self.db.fetch_all(
                """
                SELECT id, title, content, tags, status, linked_entities,
                       updated_at, created_at
                FROM agent_notes
                WHERE linked_entities @> %s::jsonb
                  AND status = 'active'
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (link_filter, limit),
            )
            for row in rows:
                seen[int(row["id"])] = row
        results = list(seen.values())
        results.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return results[:limit]

    def format_structured_recall(
        self,
        job: dict[str, Any],
        emails: list[dict[str, Any]],
        reminder: Optional[dict[str, Any]],
        instructions: list[dict[str, Any]],
        resolution: ResolutionResult,
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
            "resolved_entities": [entity.to_dict() for entity in resolution.entities],
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

    def consolidate(self, job: dict[str, Any], messages: list[dict[str, Any]], outcome: str, summary: str) -> None:
        if not self.enabled():
            return
        try:
            # Resolve entities for this job to provide to consolidation LLM
            emails = self.db.latest_thread_emails(job["thread_id"], limit=5)
            reminder = self.db.fetch_one("SELECT * FROM reminders WHERE job_id = %s", (job["id"],))
            resolver = EntityResolver(self.db, self.config)
            resolution = resolver.resolve_from_task(job, emails, reminder, [])

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
                if self.duplicate_exists(content):
                    rejected.append(compact_rejected_memory(item, "duplicate"))
                    continue

                # Check for conflicts before creating
                conflicts = self._detect_conflicts(content, normalized["kind"])
                if conflicts:
                    # Update existing memory instead of creating duplicate
                    best_match = conflicts[0]
                    entity_links = self._validate_entity_links(
                        item.get("entity_links") or [],
                        resolution.entities,
                    )
                    self.store.update(
                        memory_id=best_match["id"],
                        content=content,
                        tags=normalized["tags"],
                        importance=normalized["importance"],
                        confidence=normalized["confidence"],
                        job_id=job["id"],
                        actor="memory-steward-conflict-resolve",
                    )
                    if entity_links:
                        self._set_memory_entity_links(best_match["id"], entity_links)
                    self.log(job["id"], "memory_conflict_resolved", {
                        "existing_id": best_match["id"],
                        "similarity": best_match["similarity"],
                        "action": "updated_existing",
                    })
                    created.append(best_match["id"])
                    continue

                # Validate and attach entity links
                entity_links = self._validate_entity_links(
                    item.get("entity_links") or [],
                    resolution.entities,
                )

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

                # Store entity links on the memory
                if entity_links:
                    self._set_memory_entity_links(row["id"], entity_links)

                created.append(row["id"])

            # Run mini-reflection if high-signal tools were used
            self._maybe_mini_reflect(job, messages, outcome, summary, resolution)

            self.log(
                job["id"],
                "consolidation_complete",
                {
                    "created_memory_ids": created,
                    "candidate_count": len(memories),
                    "rejected": rejected[:20],
                    "min_importance": min_importance,
                    "min_confidence": min_confidence,
                    "entities_resolved": len(resolution.entities),
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
        resolution: ResolutionResult,
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
                            "resolved_entities": [entity.to_dict() for entity in resolution.entities],
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

    def _validate_entity_links(
        self,
        raw_links: list[Any],
        resolved_entities: list[EntityRef],
    ) -> list[dict[str, Any]]:
        """Validate entity links from LLM output against actually resolved entities."""
        if not raw_links or not isinstance(raw_links, list):
            return []
        # Build a map from (type, str_ref_id) to actual entity for type normalization
        entity_map = {(entity.type, str(entity.ref_id)): entity for entity in resolved_entities}
        validated = []
        for link in raw_links:
            if not isinstance(link, dict):
                continue
            link_type = str(link.get("type") or "").strip()
            link_ref_id = link.get("ref_id")
            if not link_type or link_ref_id is None:
                continue
            # Only accept links that match resolved entities, use entity's actual ref_id for correct type
            key = (link_type, str(link_ref_id))
            if key in entity_map:
                validated.append({"type": link_type, "ref_id": entity_map[key].ref_id})
        return validated

    def _set_memory_entity_links(self, memory_id: int, links: list[dict[str, Any]]) -> None:
        """Update the linked_entities column on a memory."""
        from psycopg.types.json import Jsonb
        self.db.execute(
            "UPDATE agent_memories SET linked_entities = %s WHERE id = %s",
            (Jsonb(links), memory_id),
        )

    def _maybe_mini_reflect(
        self,
        job: dict[str, Any],
        messages: list[dict[str, Any]],
        outcome: str,
        summary: str,
        resolution: ResolutionResult,
    ) -> None:
        """Run mini-reflection if high-signal tools were used in this job."""
        if not self.config.get_bool("agent.memory.steward.mini_reflection_enabled", True):
            return
        if outcome not in ("completed", "needs_review"):
            return

        # Check if any high-signal tools were used
        used_tools: set[str] = set()
        for message in messages:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    func = call.get("function") or {}
                    name = str(func.get("name") or "")
                    if name in HIGH_SIGNAL_TOOLS:
                        used_tools.add(name)
        if not used_tools:
            return

        try:
            self._run_mini_reflection(job, messages, outcome, summary, resolution, used_tools)
        except Exception as exc:
            LOGGER.warning("mini-reflection failed for job %s: %s", job["id"], exc)

    def _run_mini_reflection(
        self,
        job: dict[str, Any],
        messages: list[dict[str, Any]],
        outcome: str,
        summary: str,
        resolution: ResolutionResult,
        used_tools: set[str],
    ) -> None:
        """Execute mini-reflection to capture working knowledge as notes."""
        from .note_store import NoteStore

        llm = self.llm(max_tokens=self.config.get_int("agent.memory.steward.max_tokens_per_call", 1200))
        transcript = compact_json(messages[-12:])
        max_bytes = self.config.get_int("agent.memory.steward.max_transcript_bytes", 10000)
        if len(transcript) > max_bytes:
            transcript = "%s...[truncated]" % transcript[:max_bytes]

        # Check for existing notes linked to these entities (to suggest updates)
        existing_notes: list[dict[str, Any]] = []
        for entity in resolution.entities:
            link_filter = json.dumps([entity.link_dict()])
            rows = self.db.fetch_all(
                """
                SELECT id, title, tags, status, linked_entities
                FROM agent_notes
                WHERE linked_entities @> %s::jsonb
                  AND status = 'active'
                ORDER BY updated_at DESC
                LIMIT 5
                """,
                (link_filter,),
            )
            for row in rows:
                if not any(note["id"] == row["id"] for note in existing_notes):
                    existing_notes.append(row)

        response = llm.chat(
            [
                {"role": "system", "content": mini_reflection_system_prompt(self.config)},
                {
                    "role": "user",
                    "content": compact_json({
                        "job": compact_job(job),
                        "outcome": outcome,
                        "summary": summary,
                        "high_signal_tools_used": list(used_tools),
                        "resolved_entities": [entity.to_dict() for entity in resolution.entities],
                        "existing_notes_for_entities": [
                            {"id": note["id"], "title": note.get("title"), "tags": note.get("tags")}
                            for note in existing_notes[:10]
                        ],
                        "recent_transcript": transcript,
                    }),
                },
            ],
            [],
        )

        content = str(response["choices"][0]["message"].get("content") or "{}").strip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            LOGGER.warning(
                "mini-reflection JSON parse failed for job %s: %s",
                job["id"], content[:500],
            )
            self.log(job["id"], "mini_reflection_parse_failed", {
                "content_preview": content[:1000],
                "high_signal_tools": list(used_tools),
            })
            return

        notes = parsed.get("notes") if isinstance(parsed, dict) else []
        if not isinstance(notes, list):
            return

        note_store = NoteStore(self.db, self.config)
        max_notes = self.config.get_int("agent.memory.steward.max_notes_per_reflection", 3)
        created_count = 0

        for item in notes:
            if not isinstance(item, dict) or created_count >= max_notes:
                break
            action = str(item.get("action") or "create").strip()
            title = str(item.get("title") or "").strip()
            note_content = str(item.get("content") or "").strip()
            if not note_content:
                continue

            # Validate entity links
            raw_links = item.get("linked_entities") or []
            entity_links = self._validate_entity_links(raw_links, resolution.entities)

            tags = [str(tag).strip().lower()[:64] for tag in (item.get("tags") or []) if str(tag).strip()][:10]

            if action == "update" and item.get("update_note_id"):
                try:
                    note_store.update(
                        note_id=int(item["update_note_id"]),
                        content=note_content,
                        title=title or None,
                        tags=tags or None,
                        job_id=job["id"],
                        actor="mini-reflection",
                    )
                    # Update entity links
                    if entity_links:
                        from psycopg.types.json import Jsonb
                        self.db.execute(
                            "UPDATE agent_notes SET linked_entities = %s WHERE id = %s",
                            (Jsonb(entity_links), int(item["update_note_id"])),
                        )
                    created_count += 1
                except Exception as exc:
                    LOGGER.warning("mini-reflection note update failed for job %s: %s", job["id"], exc)
            else:
                try:
                    row = note_store.create(
                        content=note_content,
                        title=title or None,
                        tags=tags or None,
                        source_job_id=job["id"],
                        actor="mini-reflection",
                    )
                    # Set entity links
                    if entity_links:
                        from psycopg.types.json import Jsonb
                        self.db.execute(
                            "UPDATE agent_notes SET linked_entities = %s WHERE id = %s",
                            (Jsonb(entity_links), row["id"]),
                        )
                    created_count += 1
                except Exception as exc:
                    LOGGER.warning("mini-reflection note create failed for job %s: %s", job["id"], exc)

        if created_count > 0:
            self.log(job["id"], "mini_reflection_complete", {
                "notes_written": created_count,
                "high_signal_tools": list(used_tools),
            })
        elif notes:  # LLM proposed notes but none were written
            self.log(job["id"], "mini_reflection_no_notes_written", {
                "proposed_count": len(notes),
                "high_signal_tools": list(used_tools),
            })

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
        return conflicts

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
              AND last_accessed_at < now() - interval '%s days'
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
              AND created_at < now() - interval '%s days'
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


def mini_reflection_system_prompt(config: AppConfig) -> str:
    return MINI_REFLECTION_SYSTEM_PROMPT_TEMPLATE % {"agent_name": agent_name(config)}


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


def compact_note(note: dict[str, Any]) -> dict[str, Any]:
    content = str(note.get("content") or "")
    snippet = content[:300] if len(content) > 300 else content
    return {
        "id": note.get("id"),
        "title": note.get("title"),
        "snippet": snippet,
        "tags": note.get("tags") or [],
        "status": note.get("status") or "active",
        "linked_entities": note.get("linked_entities") or [],
        "updated_at": note.get("updated_at"),
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
