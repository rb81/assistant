"""Bounded local retrieval over documentation shipped with the running build."""
import hashlib
import json
import logging
import math
import re
import time
from functools import lru_cache
from pathlib import Path

from psycopg.types.json import Jsonb

from .context_store import cosine_similarity
from .embedding_client import EmbeddingClient, keyword_only
from .workspace_index import chunk_text


DOCS_ROOT = Path("/app/docs")
LOGGER = logging.getLogger("assistant.harness_help")


@lru_cache(maxsize=1)
def index(root=DOCS_ROOT):
    pages = sorted(root.rglob("*.md"))
    if not pages:
        raise ValueError("Bundled harness documentation is unavailable; rebuild the application image.")
    digest = hashlib.sha256()
    topics = {}
    for path in pages:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            continue
        source = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        digest.update(source.encode() + b"\0" + text.encode() + b"\0")
        heading = source
        start = 1
        lines = []
        fenced = False

        def add_section():
            for number, chunk in enumerate(chunk_text("\n".join(lines), max_chars=3000)):
                line = start + chunk["start_line"] - 1
                topic = "%s:%s:%s" % (source, line, number)
                topics[topic] = {
                    "topic_id": topic, "source": source, "heading": heading,
                    "start_line": line, "end_line": start + chunk["end_line"] - 1,
                    "content": chunk["content"],
                }

        for number, line in enumerate(text.splitlines(), 1):
            if line.startswith(("```", "~~~")):
                fenced = not fenced
            if not fenced and re.match(r"^#{1,6} ", line):
                add_section()
                lines = []
                heading = line.lstrip("# ").strip()
                start = number
            lines.append(line)
        add_section()
    return digest.hexdigest(), topics


def embedding_records(db, client, topics):
    profile = json.dumps([client.base_url, client.model, client.dimensions])
    keys = {
        identifier: "harness-help-embedding:" + hashlib.sha256(
            (profile + "\0" + item["source"] + "\0" + item["heading"] + "\0" + item["content"]).encode()
        ).hexdigest()
        for identifier, item in topics.items()
    }
    rows = db.fetch_all("SELECT key, value FROM runtime_state WHERE key = ANY(%s)", (list(keys.values()),))
    cached = {row["key"]: row["value"] for row in rows}
    return keys, cached


def refresh_embeddings(db, config, *, deadline):
    """Populate a resumable cache during idle indexing, never during help searches."""
    client = EmbeddingClient(config)
    if not client.enabled or keyword_only.get():
        return 0
    _, topics = index()
    keys, cached = embedding_records(db, client, topics)
    refreshed = 0
    for identifier, item in topics.items():
        if time.monotonic() >= deadline:
            break
        previous = cached.get(keys[identifier])
        if previous and client.compatible(previous, client.dimensions or previous["embedding_dimensions"]):
            continue
        client.timeout_seconds = min(client.timeout_seconds, max(0.01, deadline - time.monotonic()))
        vector = client.embed(item["source"] + "\n" + item["heading"] + "\n" + item["content"])
        db.set_state(keys[identifier], {
            "embedding": vector, "embedding_model": client.model, "embedding_dimensions": len(vector),
        })
        refreshed += 1
    return refreshed


def search(query, limit=5, *, db=None, config=None):
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise ValueError("Provide a help query between 1 and 500 characters.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
        raise ValueError("Help limit must be between 1 and 8.")
    revision, topics = index()
    fallback = "embeddings_not_configured"
    if config is not None and db is not None:
        client = EmbeddingClient(config)
        fallback = "embeddings_disabled" if not client.enabled else "keyword_only_run"
        if client.enabled and not keyword_only.get():
            keys, cached = embedding_records(db, client, topics)
            fallback = "help_embeddings_pending"
            if all(key in cached for key in keys.values()):
                try:
                    query_vector = client.embed(query)
                    semantic = []
                    invalid = []
                    for identifier, item in topics.items():
                        row = cached[keys[identifier]]
                        score = (cosine_similarity(query_vector, row["embedding"])
                                 if client.compatible(row, len(query_vector)) else None)
                        if score is None or not math.isfinite(score):
                            invalid.append((keys[identifier], row))
                            continue
                        semantic.append((score, item))
                    if invalid:
                        for key, row in invalid:
                            db.execute("DELETE FROM runtime_state WHERE key = %s AND value = %s", (key, Jsonb(row)))
                        raise ValueError("Help embeddings are invalid or incompatible with the query; scheduled for refresh.")
                    semantic.sort(key=lambda item: (-item[0], item[1]["topic_id"]))
                    return {"documentation_revision": revision, "source": "running_build",
                            "search_mode": "semantic",
                            "results": [{**item, "score": score} for score, item in semantic[:limit]]}
                except (RuntimeError, ValueError, OSError) as exc:
                    LOGGER.warning("Help semantic search falling back to keywords: %s", exc)
                    fallback = "embedding_unavailable"
    words = set(re.findall(r"[a-z0-9_]+", query.lower()))
    ranked = []
    for topic in topics.values():
        title = (topic["source"] + " " + topic["heading"]).lower()
        body = topic["content"].lower()
        score = sum(4 * (word in title) + (word in body) for word in words)
        if score:
            ranked.append((score, topic))
    ranked.sort(key=lambda item: (-item[0], item[1]["topic_id"]))
    return {"documentation_revision": revision, "source": "running_build",
            "search_mode": "keyword", "fallback_reason": fallback,
            "results": [topic for _, topic in ranked[:limit]]}


def topic(topic_id, revision):
    current, topics = index()
    if revision != current:
        raise ValueError("Documentation revision changed; repeat help_search.")
    if topic_id not in topics:
        raise ValueError("Unknown help topic; use a topic_id returned by help_search.")
    return {"documentation_revision": current, **topics[topic_id]}
