"""Entity linker — LLM-powered automatic entity resolution and linking.

When any object is created or edited (memory, note, reminder, project, etc.),
this module calls the configured mini model to determine which high-level
entities the object should be linked to, or whether a new entity should be created.

Design principles:
- Max 3 entities per object (prefer fewer, broader groupings)
- Always prefer existing entities over creating new ones
- Entities should be high-level and meaningful, not granular
- Best-effort: failures never block object creation
"""

import json
import logging
from typing import Any, Optional

from .config import AppConfig
from .database import Database
from .entity_store import EntityStore
from .llm_client import LlmClient


LOGGER = logging.getLogger("assistant.entity_linker")


ENTITY_RESOLUTION_SYSTEM_PROMPT = """You are an entity classifier for a personal assistant system.

Your job: given an object (memory, note, reminder, project, etc.), determine which HIGH-LEVEL ENTITIES it belongs to.

Entities represent major areas, projects, people/organizations, or recurring topics in the user's life. Examples:
- "IntelliGulf" (an AI startup project)
- "Personal Finance" (budgeting, investments, banking)
- "Health & Fitness" (exercise, diet, medical)
- "Company X Partnership" (ongoing business relationship)
- "Home Renovation" (a major life project)

RULES:
1. Return 1-3 entities maximum. Prefer 1-2. Only use 3 if genuinely necessary.
2. ALWAYS prefer linking to an EXISTING entity from the provided list. Only suggest creating a new entity when NOTHING in the existing list fits.
3. Entities must be HIGH-LEVEL and SPECIFIC:
   - GOOD: "IntelliGulf", "Personal Finance", "Career Development", "Family"
   - BAD: "Meeting Notes", "Emails", "Admin Tasks", "Tuesday", "Follow-ups", "Miscellaneous", "General"
4. Do NOT create near-duplicates of existing entities. If "Personal" exists, do not create "Personal Tasks" or "Personal Work" — use "Personal".
5. If the object is truly generic/ephemeral and doesn't fit any meaningful category, return an empty list.
6. New entity descriptions should be 1 sentence explaining what the entity covers.

Return JSON only:
{"entities": [{"id": <existing_entity_id_or_null>, "name": "<entity_name>", "description": "<only if new, else empty string>"}]}

If nothing fits: {"entities": []}
"""


class EntityLinker:
    """Resolves and links objects to entities using the configured mini model."""

    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.store = EntityStore(db)

    def enabled(self) -> bool:
        return self.config.get_bool("agent.entities.enabled", True)

    def max_per_object(self) -> int:
        return self.config.get_int("agent.entities.max_per_object", 3)

    def _get_llm(self) -> LlmClient:
        """Get LLM client using the memory steward model (mini model)."""
        model = self.config.get("agent.memory.steward.model")
        timeout = self.config.get_int("agent.memory.steward.timeout_seconds", 45)
        return LlmClient(
            self.config,
            model=model or None,
            temperature=0.1,
            max_tokens=300,
            timeout_seconds=timeout,
        )

    def link_object(
        self,
        object_type: str,
        object_id: int,
        content_summary: str,
        linked_by: str = "agent",
    ) -> list[dict[str, Any]]:
        """Resolve entities for an object and create links.

        Args:
            object_type: Type of object (memory, note, reminder, etc.)
            object_id: ID of the object
            content_summary: Text summary of the object for LLM classification
            linked_by: Who triggered the linking

        Returns:
            List of entity dicts that were linked
        """
        if not self.enabled():
            return []

        if not content_summary or not content_summary.strip():
            return []

        try:
            resolved = self._resolve_entities(content_summary)
            if not resolved:
                return []

            # Create any new entities and collect IDs
            entity_ids: list[int] = []
            linked_entities: list[dict[str, Any]] = []

            for item in resolved[: self.max_per_object()]:
                entity_id = item.get("id")

                if entity_id is not None:
                    # Verify it exists
                    entity = self.store.get(int(entity_id))
                    if entity:
                        entity_ids.append(int(entity_id))
                        linked_entities.append(entity)
                else:
                    # Create new entity
                    name = str(item.get("name") or "").strip()
                    description = str(item.get("description") or "").strip()
                    if not name:
                        continue
                    try:
                        entity = self.store.create(
                            name=name,
                            description=description,
                            created_by=linked_by,
                        )
                        entity_ids.append(int(entity["id"]))
                        linked_entities.append(entity)
                    except ValueError as exc:
                        # Name collision — try to find existing
                        LOGGER.debug("entity creation failed (%s), looking up by name", exc)
                        existing = self.store.get_by_name(name)
                        if existing:
                            entity_ids.append(int(existing["id"]))
                            linked_entities.append(existing)

            # Set the links (replaces existing links for this object)
            if entity_ids:
                self.store.set_object_entities(object_type, object_id, entity_ids, linked_by)

            return linked_entities

        except Exception as exc:
            LOGGER.warning(
                "entity linking failed for %s/%s (best-effort, continuing): %s",
                object_type, object_id, exc,
            )
            return []

    def _resolve_entities(self, content_summary: str) -> list[dict[str, Any]]:
        """Call LLM to resolve which entities this content belongs to."""
        existing_entities = self.store.get_all_names()

        user_message = json.dumps({
            "existing_entities": [
                {"id": e["id"], "name": e["name"], "description": e["description"]}
                for e in existing_entities
            ],
            "object_content": content_summary[:2000],
        }, default=str)

        llm = self._get_llm()
        response = llm.chat(
            [
                {"role": "system", "content": ENTITY_RESOLUTION_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            [],
        )

        content = str(response["choices"][0]["message"].get("content") or "{}").strip()

        # Parse JSON response
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            # Try to extract JSON from markdown code blocks
            if "```" in content:
                start = content.find("{")
                end = content.rfind("}") + 1
                if start >= 0 and end > start:
                    try:
                        parsed = json.loads(content[start:end])
                    except json.JSONDecodeError:
                        LOGGER.debug("entity linker returned non-JSON: %s", content[:200])
                        return []
                else:
                    return []
            else:
                LOGGER.debug("entity linker returned non-JSON: %s", content[:200])
                return []

        entities = parsed.get("entities") if isinstance(parsed, dict) else []
        if not isinstance(entities, list):
            return []

        # Validate and normalize
        results: list[dict[str, Any]] = []
        for item in entities:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            entity_id = item.get("id")
            if entity_id is not None:
                try:
                    entity_id = int(entity_id)
                except (TypeError, ValueError):
                    entity_id = None
            results.append({
                "id": entity_id,
                "name": name,
                "description": str(item.get("description") or "").strip(),
            })

        return results[:self.max_per_object()]

    def build_content_summary(
        self,
        title: str = "",
        content: str = "",
        tags: Optional[list[str]] = None,
        extra: Optional[dict[str, str]] = None,
    ) -> str:
        """Build a content summary string for entity resolution.

        Combines title, content snippet, tags, and any extra fields into
        a concise representation for the LLM.
        """
        parts: list[str] = []
        if title:
            parts.append("Title: %s" % title.strip())
        if tags:
            parts.append("Tags: %s" % ", ".join(str(t) for t in tags[:10]))
        if extra:
            for key, value in extra.items():
                if value and value.strip():
                    parts.append("%s: %s" % (key.capitalize(), value.strip()[:200]))
        if content:
            # Include first ~500 chars of content
            snippet = content.strip()[:500]
            parts.append("Content: %s" % snippet)
        return "\n".join(parts)
