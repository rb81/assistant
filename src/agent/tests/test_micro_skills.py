import json
import time
from unittest.mock import Mock, patch

import pytest
from psycopg.types.json import Jsonb

from assistant_agent.config import AppConfig
from assistant_agent.micro_skills import Skill, SkillContext, SkillStore, core_skills
from assistant_agent.task_agent import TaskAgent, system_prompt
from assistant_agent.tools import CORE_TOOL_NAMES, HELP_TOOL_NAMES, LOADABLE_TOOL_NAMES, TERMINAL_TOOL_NAMES
from test_network_access import db  # noqa: F401


def test_catalog_classifies_each_task_tool_and_keeps_feature_text_out_of_base_prompt(tmp_path, monkeypatch):
    shipped = core_skills()
    bound = [name for skill in shipped.values() for name in skill.tools]
    assert len(set(bound)) == len(bound)
    assert set(bound) == (LOADABLE_TOOL_NAMES | CORE_TOOL_NAMES) - TERMINAL_TOOL_NAMES - HELP_TOOL_NAMES
    (tmp_path / "AGENT.md").write_text("Use a concise style.")
    monkeypatch.setenv("ASSISTANT_CONFIG_DIR", str(tmp_path))
    prompt = system_prompt(AppConfig({"agent": {"skills": {"enabled": True}}}))
    assert "Use a concise style." in prompt
    assert "Never search for, read or print API keys" in prompt
    for skill in shipped.values():
        assert skill.body not in prompt
    assert "Model preset tool catalog policy" not in prompt
    assert "Text-to-speech (admin-configured)" not in prompt


def test_semantic_intent_selects_only_available_current_skills():
    db = Mock()
    config = AppConfig({"agent": {"embeddings": {"enabled": True}}})
    store = SkillStore(db, config)
    catalog = core_skills()
    eligible = store.visible(catalog, {"email_send", "telegram_send"}, set(), admin=False)
    db.fetch_all.return_value = [
        {"skill_id": skill_id, "fingerprint": eligible[skill_id].fingerprint(),
         "embedding_model": "embeddinggemma", "embedding_dimensions": 2, "embedding": [1.0, 0.0]}
        for skill_id in ("EMAIL", "TELEGRAM")
    ]
    with patch("assistant_agent.micro_skills.EmbeddingClient.embed", return_value=[1.0, 0.0]) as embed:
        result = store.search("Please reach out to my coworker", eligible)
        embed.assert_called_once()
    assert {row["id"] for row in result} == {"EMAIL", "TELEGRAM"}
    assert all("body" not in row for row in result)


def test_skill_index_embeds_metadata_and_invalidates_changed_title(db):
    store = SkillStore(db, AppConfig({"agent": {"embeddings": {"enabled": True}}}))
    created = store.mutate("create", "NOTE.STYLE", actor="admin", title="Concise notes",
                           description="Reduce clutter", body="Keep this body out of embeddings.")
    with patch("assistant_agent.micro_skills.EmbeddingClient.embed", return_value=[1.0, 0.0]) as embed:
        deadline = time.monotonic() + 10
        while store.refresh_one(deadline=deadline):
            pass
        assert any("Concise notes" in call.args[0] for call in embed.call_args_list)
        assert all("Keep this body" not in call.args[0] for call in embed.call_args_list)
    assert db.fetch_one("SELECT fingerprint FROM micro_skill_embeddings WHERE skill_id = 'NOTE.STYLE'")[
        "fingerprint"] == store.catalog()["NOTE.STYLE"].fingerprint()
    store.mutate("edit", "NOTE.STYLE", actor="admin", revision=created["revision"],
                 title="Daily notes", description="Summarize updates", body="Write three short lines.")
    with patch("assistant_agent.micro_skills.EmbeddingClient.embed") as embed:
        updated = store.catalog()["NOTE.STYLE"]
        assert store.search("Keep a daily log", {updated.id: updated})[0]["id"] == updated.id
        embed.assert_not_called()


def test_bound_guidance_is_required_before_dispatch_and_recalled_after_success():
    db = Mock()
    agent = TaskAgent(db, AppConfig({"agent": {"skills": {"enabled": True}}}))
    state = SkillContext()
    catalog = core_skills()
    job = {"id": 13, "_skill_context": state, "_skill_lifecycle": catalog}
    runtime = Mock()
    runtime.run.side_effect = [{"status": "uncertain"}, {"status": "sent"}]
    history = []
    with patch("assistant_agent.task_agent.available_function_names", return_value={"email_send"}):
        run = lambda: agent.handle_tool_call(job, history, runtime, "c1", "email_send", {}, {"email_send"})
        run()
        runtime.run.assert_not_called()
        assert json.loads(history[-1]["content"])["status"] == "not_executed"
        active = state.messages(catalog, [], admin=False)["content"]
        assert active.count(catalog["EMAIL"].body) == 1
        run()
        assert "EMAIL" in state.requested
        run()
        assert "EMAIL" not in state.requested
        recalled = state.messages(catalog, [], admin=False)["content"]
        assert "Recall with skill_load" in recalled
        assert catalog["EMAIL"].body not in recalled


def test_resume_revalidates_revisions_and_does_not_inject_checkpoint_bodies():
    state = SkillContext()
    catalog = core_skills()
    custom = Skill("MY.SKILL", "My custom skill", "Example", "old skill body", "1")
    catalog = {**catalog, custom.id: custom}
    state.activate(custom, "manual")
    state.activate(catalog["FILES"], "tool")
    state.activate(catalog["TELEGRAM"], "event")
    saved = state.reference(catalog)
    assert "old skill body" not in str(saved)
    restored = SkillContext()
    restored.restore(saved, core_skills())
    assert restored.requested == {"FILES": "tool"}


def test_preferences_load_with_parent_without_becoming_a_discoverable_skill():
    agent = TaskAgent(Mock(), AppConfig({"agent": {"skills": {"enabled": True}, "embeddings": {"enabled": False}}}))
    preference = Skill("EMAIL.LEGACY", "Signature", "Unique match", "Include the personal signoff.", "1",
                       overlay_for="EMAIL")
    catalog = {**core_skills(), preference.id: preference}
    state = SkillContext()
    job = {"id": 13, "_skill_context": state, "_skill_lifecycle": catalog}
    history = []

    def call(name, arguments):
        agent.handle_skill_call(job, history, "test", name, arguments)
        return json.loads(history[-1]["content"])

    with patch("assistant_agent.task_agent.available_function_names", return_value={"email_send"}), \
            patch.object(agent, "skill_requester_is_admin", return_value=True) as is_admin:
        assert [card["id"] for card in call("skill_list", {})["skills"]] == ["EMAIL"]
        assert call("skill_search", {"query": preference.description}) == {"skills": []}
        assert call("skill_load", {"id": "EMAIL"})["status"] == "queued"
        assert call("skill_load", {"id": preference.id})["skill"]["id"] == "EMAIL"
        prompt = state.messages(catalog, [], admin=True)["content"]
        assert prompt.count(preference.body) == 1
        assert preference.id not in prompt
        assert call("skill_load", {"id": preference.id})["status"] == "already_loaded"
        is_admin.return_value = False
        assert "error" in call("skill_load", {"id": preference.id})
        assert preference.body not in state.messages(catalog, [], admin=False)["content"]


def test_locked_core_stale_revisions_and_deleted_overlay(db):
    db.execute("DROP TABLE micro_skill_events, micro_skill_embeddings, micro_skills")
    db.execute("DELETE FROM schema_migrations WHERE filename = '021_micro_skills.sql'")
    assert db.run_migrations() == ["021_micro_skills.sql"]
    assert db.run_migrations() == []
    store = SkillStore(db, AppConfig({}))
    values = {"title": "Email preferences", "description": "Use a preferred email style.", "body": "Use short salutations."}
    with pytest.raises(ValueError, match="locked"):
        store.mutate("edit", "EMAIL", revision="1", actor="admin", **values)
    created = store.mutate("create", "EMAIL.PREFERENCES", actor="admin", overlay_for="EMAIL", **values)
    with pytest.raises(ValueError, match="revision has changed"):
        store.mutate("delete", created["id"], actor="admin", revision="9")
    state = SkillContext()
    catalog = store.catalog()
    state.activate(catalog["EMAIL"], "tool")
    assert values["body"] in state.messages(catalog, [], admin=True)["content"]
    assert values["body"] not in state.messages(catalog, [], admin=False)["content"]
    store.mutate("delete", created["id"], actor="admin", revision=created["revision"])
    assert created["id"] not in store.catalog()
    assert db.fetch_one("SELECT action FROM micro_skill_events ORDER BY id DESC LIMIT 1")["action"] == "delete"
    restored = store.mutate("create", created["id"], actor="admin", overlay_for="EMAIL", **values)
    assert int(restored["revision"]) > int(created["revision"])


def test_assistant_publishing_requires_configured_and_current_trusted_request(db):
    agent = TaskAgent(db, AppConfig({"agent": {"skills": {"enabled": True, "allow_assistant_writes": True}}}))
    saved_job = db.create_manual_job("Skill publishing", "Publish a writing skill")
    job = {"id": saved_job["id"], "_skill_context": SkillContext(), "_skill_lifecycle": core_skills(),
           "metadata": {"source_channel": "client", "reply_route": {"channel": "client", "client_id": "trusted"},
                        "reply_request_id": 8}}
    history = []
    request = {"action": "create", "id": "WRITING", "title": "Writing",
               "description": "Review writing", "body": "Give concrete suggestions."}
    db.execute("UPDATE jobs SET status = 'running', locked_by = %s, metadata = %s WHERE id = %s",
               (agent.locked_by, Jsonb({**job["metadata"], "reply_request_id": 9}), job["id"]))
    with patch("assistant_agent.task_agent.available_function_names", return_value=set()):
        agent.handle_skill_call(job, history, "one", "skill_save", request)
        assert "WRITING" not in agent.skill_store.catalog()
        assert "request changed" in json.loads(history[-1]["content"])["error"]
        db.execute("UPDATE jobs SET metadata = %s WHERE id = %s", (Jsonb(job["metadata"]), job["id"]))
        agent.handle_skill_call(job, history, "two", "skill_save", request)
        assert agent.skill_store.catalog()["WRITING"].body == request["body"]
        agent.handle_skill_call(job, history, "three", "skill_save", {"action": "delete", "id": "WRITING"})
        assert "cannot delete" in json.loads(history[-1]["content"])["error"]
    assert not TaskAgent(db, AppConfig({"agent": {"skills": {"enabled": True}}})).can_save_skill(job)
