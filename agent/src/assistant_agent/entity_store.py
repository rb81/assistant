"""Entity registry — CRUD operations for high-level entity groupings.

Entities are user/agent-defined categories (projects, topics, areas of life)
that any object in the system can be linked to via the entity_object_links
junction table. This module handles creation, listing, linking/unlinking,
and cascade deletion with preview.
"""

import logging
from typing import Any, Optional

from .database import Database


LOGGER = logging.getLogger("assistant.entity_store")

VALID_OBJECT_TYPES = frozenset([
    "memory", "note", "reminder", "project", "project_task",
    "job", "contact", "email", "calendar_event",
])

# Maps object_type to (table_name, id_column) for cascade deletion
OBJECT_TYPE_TABLE_MAP: dict[str, tuple[str, str]] = {
    "memory": ("agent_memories", "id"),
    "note": ("agent_notes", "id"),
    "reminder": ("reminders", "id"),
    "project": ("projects", "id"),
    "project_task": ("project_tasks", "id"),
    "job": ("jobs", "id"),
    "contact": ("contacts", "id"),
    "email": ("emails", "id"),
    "calendar_event": ("calendar_managed_events", "assistant_id"),
}


class EntityStore:
    def __init__(self, db: Database):
        self.db = db

    # -------------------------------------------------------------------------
    # Entity CRUD
    # -------------------------------------------------------------------------

    def list_all(self) -> list[dict[str, Any]]:
        """List all entities with object counts per type."""
        entities = self.db.fetch_all(
            """
            SELECT id, name, description, created_by, created_at, updated_at
            FROM entities
            ORDER BY name ASC
            """
        )
        if not entities:
            return []

        # Fetch counts per entity
        counts = self.db.fetch_all(
            """
            SELECT entity_id, object_type, count(*) AS cnt
            FROM entity_object_links
            GROUP BY entity_id, object_type
            """
        )
        count_map: dict[int, dict[str, int]] = {}
        for row in counts:
            entity_id = int(row["entity_id"])
            if entity_id not in count_map:
                count_map[entity_id] = {}
            count_map[entity_id][row["object_type"]] = int(row["cnt"])

        results = []
        for entity in entities:
            entity_id = int(entity["id"])
            obj_counts = count_map.get(entity_id, {})
            results.append({
                "id": entity["id"],
                "name": entity["name"],
                "description": entity["description"],
                "created_by": entity["created_by"],
                "created_at": entity["created_at"],
                "updated_at": entity["updated_at"],
                "object_counts": obj_counts,
                "total_objects": sum(obj_counts.values()),
            })
        return results

    def get(self, entity_id: int) -> Optional[dict[str, Any]]:
        """Get a single entity by ID."""
        return self.db.fetch_one(
            "SELECT id, name, description, created_by, created_at, updated_at FROM entities WHERE id = %s",
            (entity_id,),
        )

    def get_by_name(self, name: str) -> Optional[dict[str, Any]]:
        """Get a single entity by exact name (case-insensitive)."""
        return self.db.fetch_one(
            "SELECT id, name, description, created_by, created_at, updated_at FROM entities WHERE lower(name) = lower(%s)",
            (name.strip(),),
        )

    def create(self, name: str, description: str = "", created_by: str = "system") -> dict[str, Any]:
        """Create a new entity. Raises ValueError if name already exists."""
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("entity name is required")
        if len(clean_name) > 200:
            clean_name = clean_name[:200]

        clean_desc = str(description or "").strip()[:1000]

        existing = self.get_by_name(clean_name)
        if existing:
            raise ValueError("entity '%s' already exists (id=%s)" % (existing["name"], existing["id"]))

        row = self.db.fetch_one(
            """
            INSERT INTO entities(name, description, created_by)
            VALUES (%s, %s, %s)
            RETURNING id, name, description, created_by, created_at, updated_at
            """,
            (clean_name, clean_desc, created_by),
        )
        LOGGER.info("created entity id=%s name=%r by=%s", row["id"], row["name"], created_by)
        return row

    def update(self, entity_id: int, name: Optional[str] = None, description: Optional[str] = None) -> dict[str, Any]:
        """Update entity name and/or description."""
        existing = self.get(entity_id)
        if existing is None:
            raise ValueError("entity not found")

        if name is not None:
            clean_name = str(name).strip()
            if not clean_name:
                raise ValueError("entity name cannot be empty")
            if len(clean_name) > 200:
                clean_name = clean_name[:200]
            # Check uniqueness
            dupe = self.get_by_name(clean_name)
            if dupe and int(dupe["id"]) != entity_id:
                raise ValueError("entity name '%s' already exists" % clean_name)
        else:
            clean_name = None

        clean_desc = str(description).strip()[:1000] if description is not None else None

        row = self.db.fetch_one(
            """
            UPDATE entities
            SET name = COALESCE(%s, name),
                description = COALESCE(%s, description)
            WHERE id = %s
            RETURNING id, name, description, created_by, created_at, updated_at
            """,
            (clean_name, clean_desc, entity_id),
        )
        return row

    # -------------------------------------------------------------------------
    # Object linking
    # -------------------------------------------------------------------------

    def link_object(
        self,
        entity_id: int,
        object_type: str,
        object_id: int,
        linked_by: str = "agent",
    ) -> Optional[dict[str, Any]]:
        """Link an object to an entity. Returns the link row or None if already exists."""
        if object_type not in VALID_OBJECT_TYPES:
            raise ValueError("invalid object_type: %s" % object_type)

        row = self.db.fetch_one(
            """
            INSERT INTO entity_object_links(entity_id, object_type, object_id, linked_by)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (entity_id, object_type, object_id) DO NOTHING
            RETURNING id, entity_id, object_type, object_id, linked_by, created_at
            """,
            (entity_id, object_type, object_id, linked_by),
        )
        return row

    def unlink_object(self, entity_id: int, object_type: str, object_id: int) -> bool:
        """Remove a link between an entity and an object. Returns True if removed."""
        result = self.db.fetch_one(
            """
            DELETE FROM entity_object_links
            WHERE entity_id = %s AND object_type = %s AND object_id = %s
            RETURNING id
            """,
            (entity_id, object_type, object_id),
        )
        return result is not None

    def set_object_entities(
        self,
        object_type: str,
        object_id: int,
        entity_ids: list[int],
        linked_by: str = "agent",
    ) -> None:
        """Replace all entity links for a given object with the provided set."""
        if object_type not in VALID_OBJECT_TYPES:
            raise ValueError("invalid object_type: %s" % object_type)

        # Remove existing links for this object
        self.db.execute(
            "DELETE FROM entity_object_links WHERE object_type = %s AND object_id = %s",
            (object_type, object_id),
        )
        # Insert new links
        for eid in entity_ids[:3]:  # Max 3 entities per object
            self.db.execute(
                """
                INSERT INTO entity_object_links(entity_id, object_type, object_id, linked_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (eid, object_type, object_id, linked_by),
            )

    def get_entities_for_object(self, object_type: str, object_id: int) -> list[dict[str, Any]]:
        """Get all entities linked to a specific object."""
        return self.db.fetch_all(
            """
            SELECT e.id, e.name, e.description, eol.linked_by, eol.created_at AS linked_at
            FROM entity_object_links eol
            JOIN entities e ON e.id = eol.entity_id
            WHERE eol.object_type = %s AND eol.object_id = %s
            ORDER BY e.name ASC
            """,
            (object_type, object_id),
        )

    def get_objects_for_entity(self, entity_id: int, object_type: Optional[str] = None) -> list[dict[str, Any]]:
        """Get all objects linked to an entity, optionally filtered by type."""
        if object_type:
            return self.db.fetch_all(
                """
                SELECT id, entity_id, object_type, object_id, linked_by, created_at
                FROM entity_object_links
                WHERE entity_id = %s AND object_type = %s
                ORDER BY created_at DESC
                """,
                (entity_id, object_type),
            )
        return self.db.fetch_all(
            """
            SELECT id, entity_id, object_type, object_id, linked_by, created_at
            FROM entity_object_links
            WHERE entity_id = %s
            ORDER BY object_type, created_at DESC
            """,
            (entity_id,),
        )

    # -------------------------------------------------------------------------
    # Cascade delete with preview
    # -------------------------------------------------------------------------

    def delete_preview(self, entity_id: int) -> dict[str, Any]:
        """Preview what would happen if an entity is deleted.

        Returns counts of objects that would be deleted (exclusive) vs unlinked (shared).
        """
        entity = self.get(entity_id)
        if entity is None:
            raise ValueError("entity not found")

        links = self.get_objects_for_entity(entity_id)

        will_delete: dict[str, list[int]] = {}
        will_unlink: list[dict[str, Any]] = []

        for link in links:
            obj_type = link["object_type"]
            obj_id = int(link["object_id"])

            # Check if this object is linked to other entities too
            other_entities = self.db.fetch_all(
                """
                SELECT e.id, e.name
                FROM entity_object_links eol
                JOIN entities e ON e.id = eol.entity_id
                WHERE eol.object_type = %s AND eol.object_id = %s AND eol.entity_id != %s
                """,
                (obj_type, obj_id, entity_id),
            )

            if other_entities:
                will_unlink.append({
                    "object_type": obj_type,
                    "object_id": obj_id,
                    "other_entities": [{"id": e["id"], "name": e["name"]} for e in other_entities],
                })
            else:
                if obj_type not in will_delete:
                    will_delete[obj_type] = []
                will_delete[obj_type].append(obj_id)

        delete_counts = {k: len(v) for k, v in will_delete.items()}
        total_delete = sum(delete_counts.values())
        total_unlink = len(will_unlink)

        warning = ""
        if total_unlink > 0:
            warning = (
                "%d object(s) are shared with other entities and will only be "
                "unlinked from this entity, not deleted." % total_unlink
            )

        return {
            "entity": {"id": entity["id"], "name": entity["name"], "description": entity["description"]},
            "will_delete": delete_counts,
            "will_delete_ids": will_delete,
            "total_deletions": total_delete,
            "will_unlink_only": will_unlink,
            "total_unlinks": total_unlink,
            "warning": warning,
        }

    def delete_cascade(self, entity_id: int) -> dict[str, Any]:
        """Delete an entity and cascade-delete exclusive objects.

        Objects linked ONLY to this entity are physically deleted.
        Objects linked to other entities are merely unlinked.
        """
        preview = self.delete_preview(entity_id)

        deleted_objects: dict[str, int] = {}
        unlinked_objects: int = 0

        # Delete exclusive objects from their source tables
        for obj_type, obj_ids in preview["will_delete_ids"].items():
            if not obj_ids:
                continue
            table_info = OBJECT_TYPE_TABLE_MAP.get(obj_type)
            if not table_info:
                LOGGER.warning("no table mapping for object_type=%s, skipping deletion", obj_type)
                continue

            table_name, id_col = table_info
            # Delete in batches
            for obj_id in obj_ids:
                self.db.execute(
                    "DELETE FROM %s WHERE %s = %%s" % (table_name, id_col),
                    (obj_id,),
                )
            deleted_objects[obj_type] = len(obj_ids)

        # Count unlinked (these will be handled by CASCADE on entity_object_links
        # when we delete the entity itself)
        unlinked_objects = preview["total_unlinks"]

        # Delete the entity (CASCADE removes all entity_object_links rows)
        self.db.execute("DELETE FROM entities WHERE id = %s", (entity_id,))

        LOGGER.info(
            "deleted entity id=%s name=%r — deleted %s exclusive objects, unlinked %s shared objects",
            entity_id,
            preview["entity"]["name"],
            sum(deleted_objects.values()),
            unlinked_objects,
        )

        return {
            "entity": preview["entity"],
            "deleted_objects": deleted_objects,
            "unlinked_objects": unlinked_objects,
        }

    # -------------------------------------------------------------------------
    # Merge
    # -------------------------------------------------------------------------

    def merge(self, source_entity_id: int, target_entity_id: int) -> dict[str, Any]:
        """Merge source entity into target: re-link all objects, then delete source."""
        source = self.get(source_entity_id)
        target = self.get(target_entity_id)
        if source is None:
            raise ValueError("source entity not found")
        if target is None:
            raise ValueError("target entity not found")
        if source_entity_id == target_entity_id:
            raise ValueError("cannot merge entity into itself")

        # Move all links from source to target (skip conflicts)
        self.db.execute(
            """
            UPDATE entity_object_links
            SET entity_id = %s
            WHERE entity_id = %s
              AND (object_type, object_id) NOT IN (
                SELECT object_type, object_id
                FROM entity_object_links
                WHERE entity_id = %s
              )
            """,
            (target_entity_id, source_entity_id, target_entity_id),
        )

        # Delete remaining source links (duplicates that couldn't move)
        self.db.execute(
            "DELETE FROM entity_object_links WHERE entity_id = %s",
            (source_entity_id,),
        )

        # Delete source entity
        self.db.execute("DELETE FROM entities WHERE id = %s", (source_entity_id,))

        LOGGER.info(
            "merged entity id=%s (%r) into id=%s (%r)",
            source_entity_id, source["name"],
            target_entity_id, target["name"],
        )

        return {
            "merged": {"id": source["id"], "name": source["name"]},
            "into": {"id": target["id"], "name": target["name"]},
        }

    # -------------------------------------------------------------------------
    # Utility for entity linker
    # -------------------------------------------------------------------------

    def get_all_names(self) -> list[dict[str, Any]]:
        """Get minimal entity list for LLM prompt (id, name, description)."""
        return self.db.fetch_all(
            "SELECT id, name, description FROM entities ORDER BY name ASC"
        )
