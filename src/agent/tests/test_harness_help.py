from unittest.mock import MagicMock, patch

import pytest

from assistant_agent import harness_help
from assistant_agent.config import AppConfig
from assistant_agent.embedding_client import keyword_only
from assistant_agent.tools import ToolRuntime, available_function_names
from assistant_agent.workspace_index import WorkspaceIndex


def test_help_chunks_by_heading_bounds_output_and_rejects_stale_topics(tmp_path):
    page = tmp_path / "sandbox.md"
    page.write_text("# Sandbox\nIntro\n\n## Proxy networking\nHTTPS_PROXY must remain enabled.\n" + "x" * 7000)
    revision, topics = harness_help.index(tmp_path)
    assert all(len(item["content"]) <= 3000 for item in topics.values())
    assert sum(len(line) for item in topics.values() for line in item["content"].splitlines()
               if set(line) == {"x"}) == 7000
    with patch.object(harness_help, "index", return_value=(revision, topics)):
        result = harness_help.search("proxy networking", limit=1)
        assert result["source"] == "running_build"
        section = result["results"][0]
        assert section["heading"] == "Proxy networking"
        assert "HTTPS_PROXY" in section["content"]
        assert harness_help.topic(section["topic_id"], revision)["content"] == section["content"]
        with pytest.raises(ValueError, match="revision changed"):
            harness_help.topic(section["topic_id"], "old")
        with pytest.raises(ValueError, match="Unknown help topic"):
            harness_help.topic("../../secrets", revision)
        assert harness_help.search("unfindableword")["results"] == []
    page.write_text("# Sandbox\nUpdated\n")
    harness_help.index.cache_clear()
    assert harness_help.index(tmp_path)[0] != revision


def test_help_missing_bundle_and_invalid_queries_fail_explicitly(tmp_path):
    with pytest.raises(ValueError, match="rebuild"):
        harness_help.index(tmp_path)
    for query, limit in (("", 5), ("x", 0), ("x", True), ("x" * 501, 5)):
        with pytest.raises(ValueError):
            harness_help.search(query, limit)


def test_live_capabilities_exposes_only_whitelisted_broker_fields(tmp_path):
    config = AppConfig({"agent": {"filesystem": {"shared_root": str(tmp_path), "require_mount": False}}})
    runtime = ToolRuntime(None, config, {"id": 1})
    with patch("assistant_agent.harness_help.index", return_value=("revision", {})), \
            patch("assistant_agent.tools.sandbox_configured", return_value=True), \
            patch.object(runtime, "sandbox_control", return_value={
                "egress": "proxy", "max_timeout_seconds": 300, "api_token": "must-not-leak",
            }):
        result = runtime.runtime_capabilities()
    assert result["sandbox"] == {"egress": "proxy", "max_timeout_seconds": 300}
    assert "must-not-leak" not in str(result)
    assert {"help_search", "help_topic", "runtime_capabilities"} <= available_function_names(config)


@pytest.fixture
def semantic_help(tmp_path):
    (tmp_path / "sandbox.md").write_text("## Proxy\nHTTPS_PROXY must remain enabled.\n")
    (tmp_path / "calendar.md").write_text("## Calendar\nCreate an event.\n")
    revision, topics = harness_help.index(tmp_path)
    config = AppConfig({"agent": {"embeddings": {"model": "test", "dimensions": 2}}})
    db = MagicMock()
    cached = {}
    db.fetch_all.side_effect = lambda sql, params: [
        {"key": key, "value": cached[key]} for key in params[0] if key in cached
    ]
    db.set_state.side_effect = lambda key, value: cached.update({key: value})
    db.execute.side_effect = lambda sql, params: cached.pop(params[0], None)
    with patch.object(harness_help, "index", return_value=(revision, topics)), \
            patch("assistant_agent.harness_help.EmbeddingClient.embed") as embed:
        embed.side_effect = lambda text: [0.0, 1.0] if "Calendar" in text else [1.0, 0.0]
        yield db, config, embed, topics


def test_help_semantic_search_matches_without_keywords_and_reuses_persisted_vectors(semantic_help):
    db, config, embed, topics = semantic_help
    assert harness_help.refresh_embeddings(db, config, deadline=float("inf")) == len(topics)
    embed.reset_mock()
    result = harness_help.search("outbound connectivity", limit=1, db=db, config=config)
    assert result["search_mode"] == "semantic"
    assert result["results"][0]["source"] == "sandbox.md"
    assert result["results"][0]["score"] == 1
    embed.assert_called_once_with("outbound connectivity")
    embed.reset_mock()
    assert harness_help.refresh_embeddings(db, config, deadline=float("inf")) == 0
    embed.assert_not_called()
    db.set_state.assert_called()


def test_help_cache_refreshes_changed_content_and_model_without_rebuilding_unchanged_chunks(semantic_help):
    db, config, embed, topics = semantic_help
    harness_help.refresh_embeddings(db, config, deadline=float("inf"))
    embed.reset_mock()
    changed = {key: dict(item) for key, item in topics.items()}
    next(iter(changed.values()))["content"] += "Updated instructions."
    with patch.object(harness_help, "index", return_value=("new-revision", changed)):
        assert harness_help.refresh_embeddings(db, config, deadline=float("inf")) == 1
        assert embed.call_count == 1
        embed.reset_mock()
        other_model = AppConfig({"agent": {"embeddings": {"model": "new", "dimensions": 2}}})
        assert harness_help.search("Proxy", db=db, config=other_model)["fallback_reason"] == "help_embeddings_pending"
        embed.assert_not_called()
        assert harness_help.refresh_embeddings(db, other_model, deadline=float("inf")) == len(topics)


def test_help_embeddings_disabled_or_pending_never_embed_during_search(semantic_help):
    db, config, embed, _ = semantic_help
    assert harness_help.search("Proxy", db=db, config=config)["fallback_reason"] == "help_embeddings_pending"
    disabled = AppConfig({"agent": {"embeddings": {"enabled": False}}})
    result = harness_help.search("Proxy", db=db, config=disabled)
    assert result["search_mode"] == "keyword"
    assert result["results"][0]["source"] == "sandbox.md"
    assert result["fallback_reason"] == "embeddings_disabled"
    assert harness_help.refresh_embeddings(db, disabled, deadline=float("inf")) == 0
    token = keyword_only.set(True)
    try:
        assert harness_help.search("Proxy", db=db, config=config)["fallback_reason"] == "keyword_only_run"
    finally:
        keyword_only.reset(token)
    embed.assert_not_called()


def test_help_embedding_failure_logs_explicit_keyword_fallback(semantic_help, caplog):
    db, config, embed, _ = semantic_help
    harness_help.refresh_embeddings(db, config, deadline=float("inf"))
    embed.side_effect = RuntimeError("Embedding provider unavailable")
    result = harness_help.search("Proxy", db=db, config=config)
    assert result["search_mode"] == "keyword"
    assert result["fallback_reason"] == "embedding_unavailable"
    assert result["results"][0]["source"] == "sandbox.md"
    assert "Embedding provider unavailable" in caplog.text


def test_help_query_dimension_drift_invalidates_cache_for_automatic_refresh(semantic_help, caplog):
    db, config, embed, topics = semantic_help
    config.values["agent"]["embeddings"].pop("dimensions")
    harness_help.refresh_embeddings(db, config, deadline=float("inf"))
    embed.side_effect = lambda text: [1.0, 0.0, 0.0]
    result = harness_help.search("Proxy", db=db, config=config)
    assert result["search_mode"] == "keyword"
    assert "incompatible with the query" in caplog.text
    assert db.execute.call_count == len(topics)
    assert harness_help.refresh_embeddings(db, config, deadline=float("inf")) == len(topics)
    assert harness_help.search("Proxy", db=db, config=config)["search_mode"] == "semantic"


def test_help_refresh_resumes_saved_progress_after_failure_and_respects_deadline(semantic_help):
    db, config, embed, topics = semantic_help
    assert harness_help.refresh_embeddings(db, config, deadline=0) == 0
    embed.assert_not_called()
    embed.side_effect = [[1.0, 0.0], RuntimeError("offline")]
    with pytest.raises(RuntimeError, match="offline"):
        harness_help.refresh_embeddings(db, config, deadline=float("inf"))
    embed.side_effect = lambda text: [1.0, 0.0]
    assert harness_help.refresh_embeddings(db, config, deadline=float("inf")) == len(topics) - 1


def test_idle_refresh_indexes_help_even_when_workspace_file_indexing_is_disabled(semantic_help):
    db, _, embed, topics = semantic_help
    config = AppConfig({"agent": {"workspace": {"index": {"enabled": False}},
                                "embeddings": {"model": "test", "dimensions": 2}}})
    indexer = WorkspaceIndex(db, config)
    with patch.object(indexer.embedding_maintenance, "refresh_pending", return_value={"refreshed": 0}):
        indexer.refresh_embeddings()
    assert embed.call_count == len(topics)
