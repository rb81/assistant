import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from assistant_agent.config import AppConfig
from assistant_agent.database import Database
from assistant_agent.request_router import RequestRouter, RoutingDecision
from assistant_agent.requests import IncomingRequest


STREAM = {"id": 7, "title": "Trip planning", "main_thread_id": "trip"}


def completion(content=None, calls=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message}]}


def search_call(query="trip dates", name="context_search"):
    return {"id": "search", "type": "function",
            "function": {"name": name, "arguments": json.dumps({"query": query})}}


def incoming(channel="telegram", body="Plan the trip"):
    return IncomingRequest(channel, "message-1", "conversation-1", "123", "fresh", body,
                           attachments=[{"filename": "itinerary.pdf", "content_type": "application/pdf"}])


@pytest.fixture
def router():
    db = MagicMock(spec=Database)
    db.fetch_one.return_value = {**STREAM, "workstream_id": 7}
    db.fetch_all.return_value = []
    db.latest_thread_messages.return_value = [{"body_text": "Which dates?", "direction": "outbound"}]
    return RequestRouter(db, AppConfig({}))


def decide(router, action, **kwargs):
    llm = MagicMock()
    llm.chat.side_effect = [completion(calls=[search_call()]), completion(json.dumps(action))]
    with patch("assistant_agent.request_router.LlmClient", return_value=llm), patch(
        "assistant_agent.request_router.TransactionDatabase", return_value=router.db
    ), patch.object(router, "permitted_workstreams", return_value=[STREAM]), patch(
        "assistant_agent.request_router.ContextStore.search_workstreams", return_value=[STREAM]
    ):
        result = router.decide(incoming(), kwargs.get("reply_thread_id"), kwargs.get("reply_context"),
                               False, time.monotonic() + 20)
    return result, llm


def test_standalone_input_can_continue_without_negative_thread_hints(router):
    result, llm = decide(router, {"action": "continue", "workstream_id": 7, "reason": "Same trip"})
    assert result.thread_id == "trip" and not result.fallback
    messages, tools = llm.chat.call_args.args
    payload = json.loads(messages[1]["content"])
    assert set(payload) == {"message", "channel", "attachments"}
    assert payload["message"] == "Plan the trip"
    assert [tool["function"]["name"] for tool in tools] == ["context_search"]


@pytest.mark.parametrize("action,expected", [("new", "fresh"), ("continue", "trip-subtask")])
def test_reply_context_is_weighted_but_can_be_overridden(router, action, expected):
    result, llm = decide(router, {"action": action, "workstream_id": 7, "reason": "Different intent" if action == "new" else "Answers question"},
                         reply_thread_id="trip-subtask", reply_context={"text": "Which dates?"})
    assert result.thread_id == expected
    messages = llm.chat.call_args.args[0]
    payload = json.loads(messages[1]["content"])
    assert payload["reply_context"]["text"] == "Which dates?"
    assert payload["reply_workstream"]["workstream_id"] == 7
    assert payload["reply_workstream"]["recent_exchanges"][0]["body"] == "Which dates?"
    assert "extra weight" in messages[0]["content"] and "not a rule" in messages[0]["content"]


@pytest.mark.parametrize("decision", [
    [], None, "new",
    {"action": "continue", "workstream_id": 99, "reason": "Guess"},
    {"action": "continue", "workstream_id": "7", "reason": "Wrong type"},
    {"action": "continue", "workstream_id": True, "reason": "Wrong type"},
    {"action": "merge", "workstream_id": 7, "reason": "Unsupported"},
    {"action": "new"},
])
def test_invalid_decisions_are_not_applied(router, decision):
    with pytest.raises(ValueError):
        decide(router, decision)
    router.db.execute.assert_not_called()


@pytest.mark.parametrize("channel,reply,available,expected", [
    ("email", "trip", True, "trip"),
    ("telegram", "trip", True, "trip"),
    ("email", None, True, "fresh"),
    ("telegram", None, True, "fresh"),
    ("email", "trip", False, "fresh"),
    ("telegram", "trip", False, "fresh"),
])
def test_unavailable_router_uses_only_known_reply_associations(router, channel, reply, available, expected):
    router.db.fetch_one.return_value = {"workstream_id": 7} if available else None
    with patch.object(router, "decide", side_effect=RuntimeError("model unavailable")):
        result = router.route(incoming(channel), reply_thread_id=reply)
    assert result.thread_id == expected and result.fallback
    assert result.metadata()["routing"]["fallback"] is True
    router.db.execute.assert_not_called()


def test_timeout_returns_fallback_and_ignores_late_decision(router):
    router.config = AppConfig({"agent": {"routing": {"timeout_seconds": 1}}})
    release, finished = threading.Event(), threading.Event()

    def late(*args):
        release.wait(5)
        finished.set()
        return RoutingDecision("trip", "continue", "Late", workstream_id=7)

    try:
        with patch.object(router, "decide", side_effect=late):
            started = time.monotonic()
            result = router.route(incoming())
        assert result.thread_id == "fresh" and result.fallback
        assert time.monotonic() - started < 2
    finally:
        release.set()
        assert finished.wait(2)
    assert result.thread_id == "fresh"
    router.db.execute.assert_not_called()


def test_busy_router_does_not_enqueue_more_background_work(router):
    with patch("assistant_agent.request_router._slots") as slots, patch(
        "assistant_agent.request_router._workers"
    ) as workers:
        slots.acquire.return_value = False
        assert router.route(incoming()).fallback
    workers.submit.assert_not_called()


def test_deleted_destination_falls_back_before_assignment(router):
    router.db.fetch_one.return_value = None
    with patch.object(router, "decide", return_value=RoutingDecision("trip", "continue", "Related", workstream_id=7)):
        result = router.route(incoming())
    assert result.thread_id == "fresh" and result.fallback


def test_router_cannot_call_mutating_tools(router):
    llm = MagicMock()
    llm.chat.return_value = completion(calls=[search_call(name="workstream_attach")])
    with patch("assistant_agent.request_router.LlmClient", return_value=llm), patch(
        "assistant_agent.request_router.TransactionDatabase", return_value=router.db
    ), patch.object(router, "permitted_workstreams", return_value=[STREAM]):
        with pytest.raises(ValueError, match="unsupported tool"):
            router.decide(incoming(), None, None, False, time.monotonic() + 20)
    router.db.execute.assert_not_called()


def test_reply_target_is_supplied_even_without_prior_sender_history(router):
    llm = MagicMock()
    llm.chat.side_effect = [completion(calls=[search_call()]),
                           completion('{"action":"continue","workstream_id":7,"reason":"Reply"}')]
    with patch("assistant_agent.request_router.LlmClient", return_value=llm), patch(
        "assistant_agent.request_router.TransactionDatabase", return_value=router.db
    ), patch.object(router, "permitted_workstreams", return_value=[]), patch(
        "assistant_agent.request_router.ContextStore.search_workstreams", return_value=[]
    ):
        result = router.decide(incoming(body="Yes"), "trip", {"text": "Which dates?"}, False,
                               time.monotonic() + 20)
    assert result.thread_id == "trip"
    assert json.loads(llm.chat.call_args.args[0][1]["content"])["reply_workstream"]["workstream_id"] == 7


@pytest.mark.parametrize("arguments", ["[]", "null", '"trip"'])
def test_malformed_search_arguments_trigger_fallback(router, arguments):
    llm = MagicMock()
    call = search_call()
    call["function"]["arguments"] = arguments
    llm.chat.return_value = completion(calls=[call])
    with patch("assistant_agent.request_router.LlmClient", return_value=llm), patch(
        "assistant_agent.request_router.TransactionDatabase", return_value=router.db
    ), patch.object(router, "permitted_workstreams", return_value=[STREAM]):
        result = router.route(incoming())
    assert result.fallback and result.thread_id == "fresh"


def test_router_requires_search_even_if_model_immediately_decides(router):
    llm = MagicMock()
    llm.chat.return_value = completion('{"action":"new","reason":"New"}')
    with patch("assistant_agent.request_router.LlmClient", return_value=llm), patch(
        "assistant_agent.request_router.TransactionDatabase", return_value=router.db
    ), patch.object(router, "permitted_workstreams", return_value=[STREAM]):
        with pytest.raises(ValueError, match="search budget"):
            router.decide(incoming(), None, None, False, time.monotonic() + 20)
