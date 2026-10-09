import json
import uuid
from typing import Any

from psycopg.types.json import Jsonb

from .tool_result_cache import ToolResultCache


def memory_contents(value: Any) -> set[str]:
    contents = set()
    if isinstance(value, dict):
        if isinstance(value.get("content"), str) and value["content"]:
            contents.add(value["content"])
        for item in value.values():
            contents.update(memory_contents(item))
    elif isinstance(value, list):
        for item in value:
            contents.update(memory_contents(item))
    return contents


def redact_memory(value: Any, memory_id: int, contents: set[str], content_field: bool = False) -> Any:
    if isinstance(value, str):
        for content in sorted(contents, key=len, reverse=True):
            if len(content) >= 16 or (content_field and value == content):
                value = value.replace(content, "[deleted memory]")
        return value
    if isinstance(value, list):
        return [redact_memory(item, memory_id, contents) for item in value]
    if isinstance(value, dict):
        references = value.get("memory_ids")
        if value.get("memory_id") == memory_id or (isinstance(references, list) and memory_id in references):
            if value.get("role"):
                return {"role": value["role"], "content": "[deleted memory context]"}
            return {"redacted_memory_id": memory_id}
        return {redact_memory(key, memory_id, contents): redact_memory(item, memory_id, contents, key in {"content", "content_preview"}) for key, item in value.items()}
    return value


def erase_memory(db, config, memory_id: int, actor: str, job_id=None, include_expired=False) -> dict[str, Any]:
    redacted_records = 0
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, content, expires_at FROM agent_memories WHERE id = %s AND (%s OR expires_at IS NULL OR expires_at > now()) FOR UPDATE", (memory_id, include_expired))
            existing = cur.fetchone()
            if existing is None:
                raise ValueError("memory not found")
            contents = {existing["content"]}
            cur.execute("SELECT input_data, output_data FROM memory_events WHERE memory_id = %s", (memory_id,))
            for event in cur.fetchall():
                contents.update(memory_contents(event))
            for table, fields in (
                ("memory_events", ("input_data", "output_data")),
                ("task_logs", ("input_data", "output_data")),
                ("deep_research_events", ("input_data", "output_data")),
                ("agent_checkpoints", ("message_history",)),
                ("thread_summaries", ("summary",)),
                ("jobs", ("task_summary", "final_response", "metadata")),
            ):
                cur.execute("SELECT to_regclass(%s) AS relation", (table,))
                if not cur.fetchone()["relation"]:
                    continue
                identifier = "thread_id" if table == "thread_summaries" else "id"
                with conn.cursor(name="erase_%s" % table) as reader:
                    reader.execute("SELECT * FROM %s" % table)
                    for record in reader:
                        changes = {}
                        for field in fields:
                            original = record.get(field)
                            cleaned = redact_memory(original, memory_id, contents)
                            if cleaned != original:
                                if field == "message_history":
                                    cleaned = []
                                changes[field] = Jsonb(cleaned) if isinstance(cleaned, (list, dict)) else cleaned
                        if not changes:
                            continue
                        cur.execute("UPDATE %s SET %s WHERE %s = %%s" % (table, ", ".join("%s = %%s" % field for field in changes), identifier), (*changes.values(), record[identifier]))
                        redacted_records += 1
            cache = ToolResultCache(config)
            if cache.root.exists():
                for path in cache.root.rglob("*.json"):
                    if path.is_symlink() or not path.resolve().is_relative_to(cache.root):
                        continue
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if redact_memory(payload, memory_id, contents) != payload:
                        path.unlink()
                        redacted_records += 1
            cur.execute("DELETE FROM memory_events WHERE memory_id = %s", (memory_id,))
            cur.execute("DELETE FROM agent_memories WHERE id = %s", (memory_id,))
            receipt = {"id": memory_id, "deleted": True, "redacted_records": redacted_records}
            cur.execute("INSERT INTO memory_events(memory_id, job_id, actor, event_type, input_data, output_data) VALUES (%s, %s, %s, 'delete', '{}'::jsonb, %s)", (memory_id, job_id, actor, Jsonb(receipt)))
            cur.execute("INSERT INTO runtime_state(key, value) VALUES ('memory_revision', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()", (Jsonb({"deleted_memory_id": memory_id, "revision": str(uuid.uuid4())}),))
    return receipt