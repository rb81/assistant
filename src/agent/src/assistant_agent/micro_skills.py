import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from psycopg.errors import UniqueViolation

from .context_store import cosine_similarity
from .embedding_client import EmbeddingClient, keyword_only


LOGGER = logging.getLogger(__name__)
SKILL_DIR = Path(__file__).with_name("skills")
MAX_BODY_CHARS = 6000
MAX_ACTIVE_CHARS = 24000
MAX_IDLE_TURNS = 2
MAX_QUERY_CHARS = 1600
SEARCH_MIN_SIMILARITY = 0.45
SUGGEST_MIN_SIMILARITY = 0.65
SKILL_ID = re.compile(r"[A-Z][A-Z0-9]*(?:[._-][A-Z0-9]+)*")
SKILL_DISCOVERY_TOOLS = [
    {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required,
                       "additionalProperties": False},
    }}
    for name, description, properties, required in (
        ("skill_list", "List available skill descriptions, without loading their guidance.",
         {"offset": {"type": "integer", "minimum": 0}}, []),
        ("skill_search", "Find available skills by workflow or intent.",
         {"query": {"type": "string"}}, ["query"]),
        ("skill_load", "Load a skill's guidance before planning or using a feature. Applicable operator preferences are included automatically.",
         {"id": {"type": "string"}}, ["id"]),
    )
]
SKILL_WRITE_TOOL = {"type": "function", "function": {
    "name": "skill_save",
    "description": "Publish a new or revised reusable custom skill following explicit administrator feedback. "
                   "To edit attached preferences, use the preference_record metadata from skill_list. "
                   "Never persist secrets or information from third parties. Cannot change or delete locked skills.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "edit"]},
            "id": {"type": "string"}, "revision": {"type": "string"},
            "title": {"type": "string"}, "description": {"type": "string"},
            "body": {"type": "string"}, "overlay_for": {"type": "string"},
        },
        "required": ["action", "id", "title", "description", "body"],
        "additionalProperties": False,
    },
}}


@dataclass(frozen=True)
class Skill:
    id: str
    title: str
    description: str
    body: str
    revision: str
    tools: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    locked: bool = False
    overlay_for: str | None = None

    def card(self):
        return {
            "id": self.id, "title": self.title, "description": self.description,
            "revision": self.revision, "locked": self.locked, "overlay_for": self.overlay_for,
        }

    def document(self):
        return {**self.card(), "body": self.body, "tools": list(self.tools), "events": list(self.events)}

    def index_text(self):
        return "\n".join((self.id, self.title, self.description, *self.examples))

    def fingerprint(self):
        return hashlib.sha256(self.index_text().encode()).hexdigest()


def preferences_by_parent(catalog: dict[str, Skill]) -> dict[str, Skill]:
    return {skill.overlay_for: skill for skill in catalog.values() if skill.overlay_for}


@lru_cache(maxsize=1)
def core_skills() -> dict[str, Skill]:
    result = {}
    for path in sorted(SKILL_DIR.glob("*.skill")):
        raw = path.read_text(encoding="utf-8")
        header, separator, body = raw.partition("\n\n")
        if not separator or not body.strip() or len(body.strip()) > MAX_BODY_CHARS or not SKILL_ID.fullmatch(path.stem):
            raise RuntimeError(f"Invalid shipped skill: {path.name}")
        details = json.loads(header)
        result[path.stem] = Skill(
            id=path.stem, title=details["title"], description=details["description"], body=body.strip(),
            revision=hashlib.sha256(raw.encode()).hexdigest()[:16], locked=True,
            tools=tuple(details.get("tools", ())), events=tuple(details.get("events", ())),
            examples=tuple(details.get("examples", ())),
        )
    if not result:
        raise RuntimeError("The shipped skill catalog is empty")
    return result


def terms(text: str) -> set[str]:
    return set(re.findall(r"\b[^\W_]{3,}\b", text.casefold())) - {
        "and", "can", "for", "from", "how", "the", "this", "that", "with", "you",
    }


def recommendation_keyword_match(query: str, query_terms: set[str], skill: Skill) -> bool:
    for name in (skill.id, skill.title):
        phrase = re.findall(r"[^\W_]+", name.casefold())
        if phrase and re.search(r"\b" + r"[\W_]+".join(map(re.escape, phrase)) + r"\b", query.casefold()):
            return True
    meaningful = query_terms - {"last", "next"}
    return any(len(meaningful & terms(field)) >= 2 for field in (skill.description, *skill.examples))


def enabled(config) -> bool:
    return config.get_bool("agent.skills.enabled", True)


class SkillStore:
    def __init__(self, db, config):
        self.db = db
        self.config = config

    def catalog(self) -> dict[str, Skill]:
        result = dict(core_skills())
        rows = self.db.fetch_all(
            "SELECT id, title, description, body, revision, overlay_for FROM micro_skills WHERE deleted_at IS NULL",
        )
        for row in rows:
            skill = Skill(**{**row, "revision": str(row["revision"])})
            result[skill.id] = skill
        return result

    def visible(self, catalog, available_tools, events, *, admin):
        return {
            skill.id: skill for skill in catalog.values()
            if not skill.overlay_for and (
                (skill.locked and (not (skill.tools or skill.events)
                                   or set(skill.tools) & available_tools or set(skill.events) & events))
                or (not skill.locked and admin)
            )
        }

    def search(self, query, candidates, *, limit=3, proactive=False):
        query = query.strip()[:MAX_QUERY_CHARS]
        if not query or not candidates:
            return []
        query_terms = terms(query)
        lexical = {
            skill.id: len(query_terms & terms(skill.index_text())) / max(len(query_terms), 1)
            for skill in candidates.values()
            if not proactive or recommendation_keyword_match(query, query_terms, skill)
        }
        minimum_similarity = SUGGEST_MIN_SIMILARITY if proactive else SEARCH_MIN_SIMILARITY
        semantic = {}
        if self.config.get_bool("agent.embeddings.enabled", True) and not keyword_only.get():
            client = EmbeddingClient(self.config)
            client.timeout_seconds = min(client.timeout_seconds, 2)
            rows = self.db.fetch_all(
                """SELECT skill_id, fingerprint, embedding, embedding_model, embedding_dimensions
                   FROM micro_skill_embeddings WHERE skill_id = ANY(%s) AND embedding_model = %s""",
                (list(candidates), client.model),
            )
            current = [row for row in rows if row["fingerprint"] == candidates[row["skill_id"]].fingerprint()
                       and (not client.dimensions or row["embedding_dimensions"] == client.dimensions)]
            if current:
                try:
                    vector = client.embed(query)
                except (RuntimeError, ValueError, OSError):
                    LOGGER.warning("Skill query embedding unavailable; using keyword retrieval")
                else:
                    for row in current:
                        if client.compatible(row, len(vector)):
                            score = cosine_similarity(vector, row["embedding"])
                            if score is not None and score >= minimum_similarity:
                                semantic[row["skill_id"]] = score
        matches = [skill_id for skill_id in candidates if lexical.get(skill_id, 0) > 0 or skill_id in semantic]
        matches.sort(key=lambda skill_id: (-(semantic.get(skill_id, 0) + 0.35 * lexical.get(skill_id, 0)), skill_id))
        return [candidates[skill_id].card() for skill_id in matches[:max(1, min(8, limit))]]

    def refresh_one(self, *, deadline) -> bool:
        if not self.config.get_bool("agent.embeddings.enabled", True):
            return False
        catalog = self.catalog()
        client = EmbeddingClient(self.config)
        done = self.db.fetch_all(
            "SELECT skill_id, fingerprint, embedding_dimensions FROM micro_skill_embeddings WHERE embedding_model = %s",
            (client.model,),
        )
        current = {row["skill_id"]: row["fingerprint"] for row in done
                   if not client.dimensions or row["embedding_dimensions"] == client.dimensions}
        for skill in catalog.values():
            if skill.overlay_for or current.get(skill.id) == skill.fingerprint():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            client.timeout_seconds = min(client.timeout_seconds, remaining)
            try:
                vector = client.embed(skill.index_text())
            except (RuntimeError, ValueError, OSError):
                LOGGER.warning("Skill index embedding unavailable; will retry")
                return False
            self.db.execute(
                """INSERT INTO micro_skill_embeddings VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (skill_id) DO UPDATE SET fingerprint = EXCLUDED.fingerprint,
                       embedding_model = EXCLUDED.embedding_model, embedding_dimensions = EXCLUDED.embedding_dimensions,
                       embedding = EXCLUDED.embedding""",
                (skill.id, skill.fingerprint(), client.model, len(vector), vector),
            )
            return True
        return False

    def mutate(self, action, skill_id, *, actor, revision=None, title=None, description=None, body=None, overlay_for=None):
        if not isinstance(skill_id, str) or len(skill_id) > 64 or not SKILL_ID.fullmatch(skill_id):
            raise ValueError("Skill ID must use uppercase letters, digits and internal separators.")
        if skill_id in core_skills():
            raise ValueError("Shipped skills are locked. Edit a preference overlay instead.")
        if action in {"create", "edit"}:
            if any(not isinstance(value, str) or not value.strip() or "\0" in value or len(value) > limit
                   for value, limit in ((title, 100), (description, 500), (body, MAX_BODY_CHARS))):
                raise ValueError("Provide a title, description and body within the skill size limits.")
            if overlay_for is not None and (not isinstance(overlay_for, str) or overlay_for not in core_skills()):
                raise ValueError("A preference overlay must target a shipped skill.")
            if action == "edit" and overlay_for is not None:
                raise ValueError("An existing skill cannot be rebound to a different overlay target.")
        if action != "create" and (not isinstance(revision, str) or not revision.isascii() or not revision.isdigit()
                                   or len(revision) > 18 or int(revision) < 1):
            raise ValueError("A current skill revision is required.")
        with self.db.connect() as conn:
            if action == "create":
                try:
                    result = conn.execute(
                        """INSERT INTO micro_skills (id, title, description, body, overlay_for)
                           VALUES (%s, %s, %s, %s, %s)
                           ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title,
                               description = EXCLUDED.description, body = EXCLUDED.body, overlay_for = EXCLUDED.overlay_for,
                               deleted_at = NULL, revision = micro_skills.revision + 1, updated_at = now()
                           WHERE micro_skills.deleted_at IS NOT NULL RETURNING *""",
                        (skill_id, title.strip(), description.strip(), body.strip(), overlay_for),
                    ).fetchone()
                except UniqueViolation as exc:
                    if exc.diag.constraint_name != "micro_skills_active_overlay":
                        raise
                    raise ValueError("An active preference overlay already exists for this skill.") from exc
            elif action == "edit":
                result = conn.execute(
                    """UPDATE micro_skills SET title = %s, description = %s, body = %s,
                           revision = revision + 1, updated_at = now()
                       WHERE id = %s AND revision = %s AND deleted_at IS NULL RETURNING *""",
                    (title.strip(), description.strip(), body.strip(), skill_id, int(revision)),
                ).fetchone()
            elif action == "delete":
                result = conn.execute(
                    """UPDATE micro_skills SET revision = revision + 1, deleted_at = now(), updated_at = now()
                       WHERE id = %s AND revision = %s AND deleted_at IS NULL RETURNING *""",
                    (skill_id, int(revision)),
                ).fetchone()
                if result:
                    conn.execute("DELETE FROM micro_skill_embeddings WHERE skill_id = %s", (skill_id,))
            else:
                raise ValueError("Unknown skill operation.")
            if not result:
                raise ValueError("The skill or overlay exists already, or its revision has changed. Refresh and retry.")
            conn.execute(
                """INSERT INTO micro_skill_events (skill_id, revision, action, actor, title, description, body)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (skill_id, result["revision"], action, actor, result["title"], result["description"], result["body"]),
            )
        return {"id": skill_id, "revision": str(result["revision"]), "status": action}


class SkillContext:
    def __init__(self):
        self.requested: dict[str, str] = {}
        self.visible: set[tuple[str, str]] = set()
        self.recalled: set[str] = set()
        self.idle_turns: dict[str, int] = {}
        self.cards: list[dict] = []
        self.query: str | None = None

    def activate(self, skill: Skill, reason: str) -> bool:
        self.idle_turns.pop(skill.id, None)
        if skill.id in self.requested:
            if reason == "event":
                self.requested[skill.id] = reason
            elif reason == "manual" and self.requested[skill.id] != "event":
                self.requested[skill.id] = reason
            return False
        self.requested[skill.id] = reason
        return True

    def restore(self, record, catalog):
        for skill_id, revision, reason in record.get("active", []):
            skill = catalog.get(skill_id)
            if skill and skill.revision == revision and reason in {"manual", "tool"}:
                self.activate(skill, reason)
        self.recalled.update(skill_id for skill_id in record.get("recalled", []) if skill_id in catalog)
        for skill_id, revision, idle in record.get("idle", []):
            skill = catalog.get(skill_id)
            if (skill and skill.revision == revision and skill_id in self.requested
                    and type(idle) is int and 0 <= idle <= MAX_IDLE_TURNS):
                self.idle_turns[skill_id] = idle

    def reference(self, catalog):
        return {
            "active": [[skill_id, catalog[skill_id].revision, reason]
                       for skill_id, reason in self.requested.items() if skill_id in catalog and reason != "event"],
            "recalled": sorted(self.recalled),
            "idle": [[skill_id, catalog[skill_id].revision, idle]
                     for skill_id, idle in self.idle_turns.items()
                     if skill_id in catalog and self.requested.get(skill_id) in {"manual", "tool"}],
        }

    def used(self, name, catalog):
        for skill in catalog.values():
            if name in skill.tools:
                self.touch(skill.id)

    def touch(self, skill_id):
        if self.requested.get(skill_id) in {"tool", "manual"}:
            self.idle_turns[skill_id] = 0

    def consume(self, skill_id):
        self.idle_turns.pop(skill_id, None)
        if self.requested.get(skill_id) in {"tool", "manual"}:
            del self.requested[skill_id]
            self.recalled.add(skill_id)

    def prioritize(self, skill):
        self.idle_turns.pop(skill.id, None)
        reason = self.requested.get(skill.id, "tool")
        self.requested = {skill.id: reason, **self.requested}

    def messages(self, catalog, cards, *, admin, runtime=None, char_limit=MAX_ACTIVE_CHARS):
        self.visible.clear()
        for skill_id, reason in list(self.requested.items()):
            skill = catalog.get(skill_id)
            if not skill or not skill.tools or reason == "event":
                self.idle_turns.pop(skill_id, None)
                continue
            idle = self.idle_turns.get(skill_id, 0)
            if idle >= MAX_IDLE_TURNS:
                self.consume(skill_id)
            else:
                self.idle_turns[skill_id] = idle + 1
        self.idle_turns = {skill_id: idle for skill_id, idle in self.idle_turns.items()
                           if skill_id in self.requested}
        preferences = preferences_by_parent(catalog) if admin else {}
        sections = []
        size = 0
        for skill_id in sorted(self.requested, key=lambda skill_id: self.requested[skill_id] != "event"):
            skill = catalog.get(skill_id)
            if not skill:
                continue
            heading = f"\n## {skill.id} (revision {skill.revision})\n"
            block = heading + skill.body
            if runtime and skill.id in runtime:
                block += "\n\n" + runtime[skill.id]
            if preference := preferences.get(skill.id):
                block += f"\n\nOperator preferences:\n{preference.body}"
            cost = len(json.dumps(block, ensure_ascii=True))
            if size + cost > char_limit:
                continue
            sections.append(block)
            size += cost
            self.visible.add((skill.id, skill.revision))
        advisory = [f"- {card['id']}: {card['description']}" for card in cards
                    if not any(skill_id == card["id"] for skill_id, _ in self.visible)]
        if advisory:
            block = "\nSuggested skills (optional; use skill_load if useful):\n" + "\n".join(advisory)
            cost = len(json.dumps(block, ensure_ascii=True))
            if size + cost <= char_limit:
                sections.append(block)
                size += cost
        recalled = sorted((self.recalled - self.requested.keys()) & catalog.keys())
        if recalled:
            block = "\nPreviously used. Recall with skill_load if needed: " + ", ".join(recalled)
            if size + len(json.dumps(block, ensure_ascii=True)) <= char_limit:
                sections.append(block)
        return {"role": "system", "content": "\n".join(sections)} if sections else None
