import logging
import json
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from assistant_agent.config import AppConfig
from assistant_agent.database import Database
from assistant_agent.requests import IncomingRequest, RequestService
from assistant_agent.telegram import TelegramClient, TelegramDelivery, TelegramError, TelegramURLFilter, telegram_destination, telegram_message_parts
from assistant_agent.telegram_ingest import RequestPreprocessor, TelegramReceiver, message_files
from assistant_agent.tools import ToolRuntime, available_function_names
from assistant_agent.task_agent import TaskAgent
from assistant_agent.replies import reminder_delivery
from assistant_agent.reminders import ReminderScheduler


def config():
    return AppConfig({"agent": {"telegram": {"enabled": True, "bot_token": "test-token",
        "allowed_user_ids": ["123"], "recipients": {"user@example.com": 123}}}})


def test_email_to_telegram_identity_and_tool_gate():
    job = {"metadata": {"reply_route": {"channel": "email", "to": "user@example.com"}}}
    assert telegram_destination(config(), job) == "123"
    assert "telegram_send" in available_function_names(config())
    assert "telegram_send" not in available_function_names(AppConfig({}))
    with pytest.raises(TelegramError):
        telegram_destination(config(), job, "stranger@example.com")


def test_reminder_defaults_to_initiating_channel_and_honors_explicit_channel():
    values = config()
    values.values["agent"]["email"] = {"smtp_host": "smtp.example.net", "smtp_from": "agent@example.net"}
    job = {"metadata": {"reply_route": {"channel": "email", "to": "user@example.com"}}}
    policy = reminder_delivery(values, job)
    assert policy["primary"]["channel"] == "email"
    assert policy["primary"]["to"] == "user@example.com"
    assert reminder_delivery(values, job, "email")["primary"]["channel"] == "email"
    assert reminder_delivery(values, job, "telegram")["fallback"] == {}


def test_token_url_redaction():
    for path in ("bot123:secret/sendMessage", "file/bot123:secret/photos/a.jpg"):
        record = logging.LogRecord("httpx", 20, "", 0, "POST https://api.telegram.org/" + path, (), None)
        assert TelegramURLFilter().filter(record)
        assert "secret" not in record.getMessage()


def test_unknown_sender_never_reaches_intake():
    receiver = TelegramReceiver.__new__(TelegramReceiver)
    receiver.config, receiver.db = config(), MagicMock()
    receiver.ingest({"message": {"from": {"id": 456}, "chat": {"id": 456, "type": "private"}, "text": "run a tool"}})
    receiver.db.fetch_one.assert_not_called()


def test_forwarded_group_task_selector_is_rejected():
    receiver = TelegramReceiver.__new__(TelegramReceiver)
    receiver.config, receiver.db = config(), MagicMock()
    receiver.select_task({"from": {"id": 123}, "data": "task:1:2", "message": {"chat": {"type": "group", "id": -123}}})
    receiver.db.fetch_one.assert_not_called()


def test_expired_selector_answers_the_user_without_requeueing():
    receiver = TelegramReceiver.__new__(TelegramReceiver)
    receiver.config, receiver.db, receiver.client = config(), MagicMock(), MagicMock()
    receiver.db.fetch_one.return_value = None
    receiver.select_task({"id": "callback", "from": {"id": 123}, "data": "task:1:2",
                          "message": {"chat": {"type": "private", "id": 123}}})
    receiver.client.call.assert_called_once()
    assert receiver.client.call.call_args.args[0] == "answerCallbackQuery"
    receiver.db.execute.assert_not_called()


def test_voice_and_photo_descriptors():
    assert message_files({"voice": {"file_id": "v"}})[0]["voice"]
    result = message_files({"photo": [{"file_id": "small", "width": 1, "height": 1},
                                     {"file_id": "large", "width": 10, "height": 10}]})
    assert result[0]["file_id"] == "large"


def test_markdown_is_converted_to_telegram_html():
    parts = telegram_message_parts("# Summary\n\n**Bold** and *italic* and ~~removed~~ with `a < b`.\n\n[Site](https://example.com/?a=1&b=2)\n\n- First\n- Second\n\n3. Third\n4. Fourth\n\n> Quoted\n\n```python\nprint('<hello>')\n```\n\n<script>literal</script>")
    assert len(parts) == 1
    markup, plain = parts[0]
    root = ElementTree.fromstring("<root>" + markup + "</root>")
    assert set(element.tag for element in root.iter()) <= {"root", "b", "i", "s", "code", "pre", "a", "blockquote"}
    assert "<b>Summary</b>" in markup and "<b>Bold</b>" in markup
    assert "<i>italic</i>" in markup and "<s>removed</s>" in markup
    assert '<a href="https://example.com/?a=1&amp;b=2">Site</a>' in markup
    assert "<code>a &lt; b</code>" in markup
    assert '<pre><code class="language-python">' in markup
    assert "&lt;script&gt;literal&lt;/script&gt;" in markup
    assert "- First\n- Second" in plain and "3. Third\n4. Fourth" in plain
    assert "".join(root.itertext()) == plain


@pytest.mark.parametrize("markdown", ["**" + "<&\U0001f600" * 1800 + "**", "```\n" + "<&" * 2500 + "\n```", "[" + "link" * 1200 + "](https://example.com)"])
def test_long_formatted_messages_keep_balanced_tags_and_text(markdown):
    parts = telegram_message_parts(markdown)
    assert len(parts) > 1
    for markup, plain in parts:
        root = ElementTree.fromstring("<root>" + markup + "</root>")
        assert "".join(root.itertext()) == plain
        assert 0 < len(plain.encode("utf-16-le")) // 2 <= 2000
    expected = markdown[2:-2] if markdown.startswith("**") else markdown[4:-4] if markdown.startswith("```") else "link" * 1200
    assert "".join(plain for _, plain in parts) == expected


def test_task_status_is_opt_in():
    database = MagicMock()
    delivery = TelegramDelivery(database, config())
    delivery.client = MagicMock()
    delivery.update_status({"id": 7, "metadata": {"reply_route": {"channel": "telegram", "chat_id": "123"}}}, "Completed")
    database.fetch_one.assert_not_called()
    delivery.client.call.assert_not_called()


def test_uncertain_send_is_not_replayed():
    database = MagicMock()
    database.fetch_one.side_effect = [None, {"id": 1, "status": "uncertain", "provider_message_ids": []}]
    delivery = TelegramDelivery(database, config())
    delivery.client = MagicMock()
    result = delivery.send({"id": 7}, "result", destination="123")
    assert result["status"] == "uncertain"
    delivery.client.call.assert_not_called()


def test_transport_errors_do_not_expose_token():
    client = TelegramClient(config())
    with patch("assistant_agent.telegram.httpx.Client") as http:
        http.return_value.__enter__.return_value.post.side_effect = ValueError("https://api.telegram.org/bottest-token")
        with pytest.raises(TelegramError) as error:
            client.call("sendMessage", {}, sending=True)
    assert error.value.uncertain
    assert "test-token" not in str(error.value)


def test_hardened_compose_configuration():
    import yaml
    root = Path(__file__).resolve().parents[2]
    values = yaml.safe_load((root / "docker-compose.yml").read_text())
    service = values["services"]["transcription"]
    assert service["user"] == "10001:10001"
    assert service["read_only"] and service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert "ports" not in service
    assert service["networks"] == ["transcription_net"]
    assert values["networks"]["transcription_net"]["internal"]
    assert service["pids_limit"] <= 64
    if shutil.which("docker"):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").touch()
            result = subprocess.run(["docker", "compose", "--project-directory", directory,
                "-f", str(root / "docker-compose.yml"), "--profile", "voice", "--profile", "telegram",
                "config", "--quiet"], capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr


@pytest.fixture
def database():
    url = os.environ.get("TEST_CHANNEL_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_CHANNEL_DATABASE_URL to a disposable assistant_channel_test database")
    with psycopg.connect(url) as connection:
        assert connection.info.dbname == "assistant_channel_test", "refusing to reset a non-test database"
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        schema = Path(__file__).resolve().parents[2] / "database/schema.sql"
        connection.execute(schema.read_text())
    database = Database(url)
    database.ensure_feature_schema()
    yield database


def migrate(database):
    migration_dir = Path(__file__).resolve().parents[1] / "src/assistant_agent/migrations"
    sources = [migration_dir / name for name in (
        "006_zoom_meetings.sql", "007_request_channels.sql",
        "008_workstreams.sql", "009_workstream_threads.sql",
    )]
    with tempfile.TemporaryDirectory() as directory:
        for source in sources:
            shutil.copy2(source, directory)
        expected = [source.name for source in sources]
        assert database.run_migrations(Path(directory)) == expected
        assert database.run_migrations(Path(directory)) == []


def test_upgrade_preserves_every_past_task_state(database):
    database.execute("ALTER TABLE jobs DROP COLUMN trigger_request_id; ALTER TABLE processed_artifacts DROP COLUMN request_id; DROP TABLE channel_deliveries; DROP TABLE request_messages")
    expected = []
    for status in ("queued", "running", "waiting", "needs_review", "completed", "failed", "cancelled"):
        email = database.fetch_one("""INSERT INTO emails(message_id, thread_id, from_address, received_at)
            VALUES (%s, %s, 'user@example.com', now()) RETURNING *""", (status, status))
        job = database.fetch_one("INSERT INTO jobs(thread_id, trigger_email_id, status, metadata) VALUES (%s, %s, %s, '{\"keep\":true}') RETURNING *", (status, email["id"], status))
        database.execute("INSERT INTO processed_artifacts(email_id, thread_id, source_type, source_label) VALUES (%s, %s, 'attachment', 'legacy.pdf')", (email["id"], status))
        expected.append((job["id"], status))
    local = database.fetch_one("INSERT INTO emails(message_id, thread_id, from_address, received_at) VALUES ('reminder', 'reminder', 'reminder@local', now()) RETURNING id")
    database.execute("INSERT INTO jobs(thread_id, status) VALUES ('reminder', 'completed')")
    migrate(database)
    rows = database.fetch_all("SELECT * FROM jobs ORDER BY id")
    assert [(row["id"], row["status"]) for row in rows[:-1]] == expected
    assert all(row["trigger_request_id"] and row["metadata"]["keep"] for row in rows[:-1])
    assert rows[-1]["trigger_request_id"]
    assert rows[-1]["metadata"]["reply_route"] == {}
    assert all(row["request_id"] for row in database.fetch_all("SELECT * FROM processed_artifacts"))
    assert database.fetch_one("SELECT id FROM request_messages WHERE email_id = %s", (local["id"],))
    before = database.fetch_one("SELECT count(*) AS total FROM jobs")["total"]
    RequestService(database).receive_request(IncomingRequest("email", "completed", "completed", "user@example.com",
        "completed", "old request", reply_route={"channel": "email", "to": "user@example.com"}))
    assert database.fetch_one("SELECT count(*) AS total FROM jobs")["total"] == before


def test_request_replay_and_explicit_followup(database):
    migrate(database)
    service = RequestService(database)
    request = IncomingRequest("telegram", "9:123:1", "9:123", "123", "task-1", "hello",
                              reply_route={"channel": "telegram", "chat_id": "123"})
    first = service.receive_request(request)
    job = database.fetch_one("SELECT * FROM jobs WHERE thread_id = 'task-1'")
    database.update_job_status(job["id"], "completed")
    assert service.receive_request(request)["id"] == first["id"]
    assert database.fetch_one("SELECT count(*) AS total FROM jobs")["total"] == 1
    service.receive_request(IncomingRequest("telegram", "9:123:2", "9:123", "123", "task-1", "followup", reply_route=request.reply_route))
    assert database.fetch_one("SELECT count(*) AS total FROM jobs")["total"] == 2


def test_preprocessor_gathers_a_request_before_activation(database):
    migrate(database)
    service = RequestService(database)
    for identifier in ("first", "second"):
        service.receive_request(IncomingRequest("telegram", identifier, "9:123", "123", "group", identifier,
                                reply_route={"channel": "telegram", "chat_id": "123"}), queue=False)
    processor = RequestPreprocessor(database, config())
    assert processor.run_once()
    assert database.fetch_one("SELECT count(*) AS total FROM jobs")["total"] == 0
    assert processor.run_once()
    assert database.fetch_one("SELECT count(*) AS total FROM jobs")["total"] == 1
    assert not processor.run_once()


def test_text_intake_does_not_wait_for_other_voice_tasks(database):
    migrate(database)
    service = RequestService(database)
    service.receive_request(IncomingRequest("telegram", "voice", "9:123", "123", "voice-task", "",
        attachments=[{"file_id": "voice", "filename": "voice.ogg"}],
        reply_route={"channel": "telegram", "chat_id": "123"}), queue=False)
    service.receive_request(IncomingRequest("telegram", "text", "9:123", "123", "text-task", "hello",
        reply_route={"channel": "telegram", "chat_id": "123"}), queue=False)
    receiver = TelegramReceiver.__new__(TelegramReceiver)
    receiver.db, receiver.config = database, config()
    receiver.activate_text_requests()
    assert database.fetch_one("SELECT thread_id FROM jobs")["thread_id"] == "text-task"


def test_formatted_delivery_preserves_plain_history_and_reply(database):
    migrate(database)
    job = database.fetch_one("INSERT INTO jobs(thread_id) VALUES ('formatted-reply') RETURNING *")
    delivery = TelegramDelivery(database, config())
    delivery.client = MagicMock()
    delivery.client.call.return_value = {"message_id": 81}
    assert delivery.send(job, "**Hello** & `world`", destination="123", reply_to=17)["status"] == "sent"
    method, payload = delivery.client.call.call_args.args
    assert method == "sendMessage"
    assert payload["parse_mode"] == "HTML"
    assert payload["text"] == "<b>Hello</b> &amp; <code>world</code>"
    assert payload["reply_parameters"]["message_id"] == 17
    assert database.fetch_one("SELECT body_text FROM channel_deliveries")["body_text"] == "Hello & world"
    assert delivery.send(job, "**Hello** & `world`", destination="123", reply_to=17)["status"] == "sent"
    assert delivery.client.call.call_count == 1


def test_explicit_task_status_updates_one_message(database):
    migrate(database)
    values = config()
    values.values["agent"]["telegram"]["show_task_status"] = True
    job = database.fetch_one("INSERT INTO jobs(thread_id) VALUES ('status-reply') RETURNING *")
    job["task_summary"] = "**literal** <status>"
    job["metadata"] = {"reply_route": {"channel": "telegram", "chat_id": "123"}}
    delivery = TelegramDelivery(database, values)
    delivery.client = MagicMock()
    delivery.client.call.return_value = {"message_id": 82}
    delivery.update_status(job, "Working")
    delivery.update_status(job, "Completed")
    delivery.update_status(job, "Completed")
    assert [call.args[0] for call in delivery.client.call.call_args_list] == ["sendMessage", "editMessageText"]
    assert delivery.client.call.call_args.args[1]["message_id"] == 82
    assert delivery.client.call.call_args.args[1]["text"].endswith("**literal** &lt;status&gt;")
    assert delivery.client.call.call_args.args[1]["parse_mode"] == "HTML"


def test_partial_delivery_retries_only_failed_part(database):
    migrate(database)
    job = database.fetch_one("INSERT INTO jobs(thread_id) VALUES ('delivery') RETURNING *")
    delivery = TelegramDelivery(database, config())
    delivery.client = MagicMock()
    delivery.client.call.side_effect = [{"message_id": 41}, TelegramError("definite rejection"), {"message_id": 42}]
    assert delivery.send(job, "a" * 2500, destination="123")["status"] == "failed"
    result = delivery.send(job, "a" * 2500, destination="123")
    assert result["status"] == "sent" and result["message_ids"] == [41, 42]
    assert delivery.client.call.call_count == 3
    assert delivery.send(job, "a" * 2500, destination="123")["status"] == "sent"
    assert delivery.client.call.call_count == 3


def test_outgoing_file_is_labeled_and_replies_to_its_task(database):
    migrate(database)
    job = database.fetch_one("INSERT INTO jobs(thread_id) VALUES ('file-reply') RETURNING *")
    delivery = TelegramDelivery(database, config())
    delivery.client = MagicMock()
    delivery.client.call.return_value = {"message_id": 91}
    result = delivery.send(job, "", destination="123", reply_to=17,
        attachments=[{"data": b"report", "filename": "report.txt", "content_type": "text/plain", "metadata": {"size_bytes": 6}}])
    assert result["status"] == "sent"
    method, payload = delivery.client.call.call_args.args
    assert method == "sendDocument" and payload["caption"] == "report.txt"
    assert json.loads(payload["reply_parameters"])["message_id"] == 17


def test_uncertain_delivery_blocks_changed_content_replay(database):
    migrate(database)
    job = database.fetch_one("INSERT INTO jobs(thread_id) VALUES ('uncertain') RETURNING *")
    delivery = TelegramDelivery(database, config())
    delivery.client = MagicMock()
    delivery.client.call.side_effect = TelegramError("timeout", uncertain=True)
    assert delivery.send(job, "first", destination="123")["status"] == "uncertain"
    assert delivery.send(job, "different wording", destination="123")["status"] == "uncertain"
    assert delivery.client.call.call_count == 1


def test_simple_recurring_reminder_delivers_without_agent(database):
    migrate(database)
    reminder = database.fetch_one("""INSERT INTO reminders(title, task, run_at, recurrence_unit,
        recurrence_interval, metadata) VALUES ('Water', 'Drink water', now(), 'day', 1,
        '{"delivery":{"primary":{"channel":"telegram","chat_id":"123"}},"notification_only":true}')
        RETURNING *""")
    scheduler = ReminderScheduler(database, config())
    with patch("assistant_agent.telegram.TelegramClient.call", return_value={"message_id": 77}) as api:
        assert scheduler.run_once() == 2
        assert api.call_count == 1
        assert scheduler.run_once() == 1
        assert api.call_count == 1
    result = database.fetch_one("SELECT * FROM reminders WHERE id = %s", (reminder["id"],))
    assert result["status"] == "scheduled" and result["run_at"] > reminder["run_at"]
    job = database.fetch_one("SELECT * FROM jobs")
    assert job["status"] == "completed" and job["metadata"]["final_response"] == "Drink water"


def test_concurrent_request_and_agent_events_have_unique_sequences(database):
    migrate(database)
    job = database.fetch_one("INSERT INTO jobs(thread_id) VALUES ('parallel-logs') RETURNING id")
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(database.log_event, job["id"], "status_change", output_data={"index": index})
                   for index in range(40)]
        for future in futures:
            future.result()
    rows = database.fetch_all("SELECT sequence FROM task_logs WHERE job_id = %s ORDER BY sequence", (job["id"],))
    assert [row["sequence"] for row in rows] == list(range(1, 41))


def test_shared_request_delivers_and_atomically_completes(database):
    migrate(database)
    RequestService(database).receive_request(IncomingRequest("telegram", "final", "9:123", "123", "final-task", "hello",
        reply_route={"channel": "telegram", "chat_id": "123"}))
    job = database.claim_job("test-worker")
    agent = TaskAgent.__new__(TaskAgent)
    agent.db, agent.config, agent.locked_by = database, config(), "test-worker"
    agent.memory_steward = MagicMock()
    with patch("assistant_agent.telegram.TelegramClient.call", return_value={"message_id": 77}):
        assert agent.handle_tool_call(job, [], ToolRuntime(database, agent.config, job), "call", "task_complete",
                                      {"summary": "Answered", "response": "Hello"}, set())
    current = database.fetch_one("SELECT * FROM jobs WHERE id = %s", (job["id"],))
    assert current["status"] == "completed" and current["metadata"]["final_response"] == "Hello"


def test_followup_during_consolidation_is_not_lost(database):
    migrate(database)
    service = RequestService(database)
    route = {"channel": "telegram", "chat_id": "123"}
    service.receive_request(IncomingRequest("telegram", "before", "9:123", "123", "race-task", "first", reply_route=route))
    job = database.claim_job("test-worker")
    agent = TaskAgent.__new__(TaskAgent)
    agent.db, agent.config, agent.locked_by = database, config(), "test-worker"
    agent.memory_steward = MagicMock()
    agent.memory_steward.consolidate.side_effect = lambda *args: service.receive_request(
        IncomingRequest("telegram", "during", "9:123", "123", "race-task", "followup", reply_route=route))
    history = []
    with patch("assistant_agent.telegram.TelegramClient.call", return_value={"message_id": 77}):
        assert not agent.handle_tool_call(job, history, ToolRuntime(database, agent.config, job), "call", "task_complete",
                                         {"summary": "Answered", "response": "First answer"}, set())
    assert database.fetch_one("SELECT status FROM jobs WHERE id = %s", (job["id"],))["status"] == "running"
    assert any("followup" in item.get("content", "") for item in history)


@pytest.mark.parametrize("media_group", [None, "album"])
def test_task_thread_handles_optional_media_group(database, media_group):
    migrate(database)
    receiver = TelegramReceiver.__new__(TelegramReceiver)
    receiver.db, receiver.config, receiver.bot_id = database, config(), "9"
    message = {"chat": {"id": 123}, "message_id": 2}
    if media_group is not None:
        message["media_group_id"] = media_group
    assert receiver.task_thread(message, "9:123", "123") == ("telegram:9:123:2", [])
    RequestService(database).receive_request(IncomingRequest(
        "telegram", "9:123:1", "9:123", "123", "existing-task", "first",
        reply_route={"channel": "telegram", "chat_id": "123"},
        metadata={"media_group_id": media_group}), queue=False)
    assert receiver.task_thread(message, "9:123", "123") == ("existing-task", [])
    database.execute("UPDATE request_messages SET created_at = now() - interval '1 minute'")
    expected = "existing-task" if media_group is not None else "telegram:9:123:2"
    assert receiver.task_thread(message, "9:123", "123") == (expected, [])


def test_telegram_selection_persists_and_explicit_replies_do_not_switch_it(database):
    migrate(database)
    service = RequestService(database)
    route = {"channel": "telegram", "chat_id": "123"}
    first = service.receive_request(IncomingRequest(
        "telegram", "9:123:10", "9:123", "123", "stream-one", "First",
        reply_route=route,
    ), queue=False)
    second = service.receive_request(IncomingRequest(
        "telegram", "9:123:20", "9:123", "123", "stream-two", "Second",
        reply_route=route,
    ), queue=False)
    receiver = TelegramReceiver.__new__(TelegramReceiver)
    receiver.db, receiver.config, receiver.bot_id = database, config(), "9"
    receiver.set_selection("123", second["workstream_id"])

    restarted_receiver = TelegramReceiver.__new__(TelegramReceiver)
    restarted_receiver.db, restarted_receiver.config, restarted_receiver.bot_id = database, config(), "9"
    assert restarted_receiver.task_thread(
        {"chat": {"id": 123}, "message_id": 21}, "9:123", "123",
    ) == ("stream-two", [])
    assert restarted_receiver.task_thread({
        "chat": {"id": 123}, "message_id": 22,
        "reply_to_message": {"message_id": 10},
    }, "9:123", "123") == ("stream-one", [])
    assert restarted_receiver.selected_workstream("123")["workstream_id"] == second["workstream_id"]

    receiver.client = MagicMock()
    receiver.select_task({
        "id": "callback", "from": {"id": 123}, "data": "ws:%s" % first["workstream_id"],
        "message": {"chat": {"type": "private", "id": 123}},
    })
    assert receiver.selected_workstream("123")["workstream_id"] == first["workstream_id"]
    receiver.client.call.assert_called_once_with(
        "answerCallbackQuery", {"callback_query_id": "callback", "text": "Workstream selected."},
    )


def test_new_telegram_workstream_uses_temporary_title_and_persists_selection(database):
    migrate(database)
    receiver = TelegramReceiver.__new__(TelegramReceiver)
    receiver.db, receiver.config, receiver.bot_id, receiver.client = database, config(), "9", MagicMock()
    receiver.ingest({
        "update_id": 1,
        "message": {"from": {"id": 123}, "chat": {"id": 123, "type": "private"},
                    "message_id": 30, "date": 1_700_000_000, "text": "/new Plan dinner"},
    })
    request = database.fetch_one("SELECT * FROM request_messages WHERE external_id = '9:123:30'")
    stream = database.fetch_one("SELECT * FROM workstreams WHERE id = %s", (request["workstream_id"],))
    selection = receiver.selected_workstream("123")
    assert stream["title"] == "New workstream" and stream["name_status"] == "temporary"
    assert selection["workstream_id"] == stream["id"]
    for index in (31, 32):
        RequestService(database).receive_request(IncomingRequest(
            "telegram", "9:123:%s" % index, "9:123", "123", request["thread_id"], "Follow-up",
        ), queue=False)
    assert database.fetch_one("SELECT telegram_message_count, name_status FROM workstreams WHERE id = %s",
                              (stream["id"],)) == {"telegram_message_count": 3, "name_status": "pending"}
    agent = TaskAgent.__new__(TaskAgent)
    agent.db, agent.config = database, config()
    llm = MagicMock()
    llm.chat.return_value = {"choices": [{"message": {"content": "Dinner Plans"}}]}
    agent.name_pending_workstream({"id": 900, "workstream_id": stream["id"]}, llm)
    agent.name_pending_workstream({"id": 901, "workstream_id": stream["id"]}, llm)
    named = database.fetch_one("SELECT title, name_status FROM workstreams WHERE id = %s", (stream["id"],))
    assert named == {"title": "Dinner Plans", "name_status": "named"}
    llm.chat.assert_called_once()


def test_clarification_resumes_the_matching_task(database):
    migrate(database)
    service = RequestService(database)
    route = {"channel": "telegram", "chat_id": "123"}
    service.receive_request(IncomingRequest("telegram", "question", "9:123", "123", "question-task", "plan", reply_route=route))
    job = database.claim_job("test-worker")
    agent = TaskAgent.__new__(TaskAgent)
    agent.db, agent.config, agent.locked_by = database, config(), "test-worker"
    agent.memory_steward = MagicMock()
    with patch("assistant_agent.telegram.TelegramClient.call", return_value={"message_id": 77}):
        assert agent.handle_request_input(job, [], ToolRuntime(database, agent.config, job), "request_input",
                                         {"recipient": "requester", "question": "Which date?"}, "call")
    receiver = TelegramReceiver.__new__(TelegramReceiver)
    receiver.db, receiver.config, receiver.bot_id = database, config(), "9"
    thread, choices = receiver.task_thread({"chat": {"id": 123}, "message_id": 2}, "9:123", "123")
    assert thread == "question-task" and choices == []
    service.receive_request(IncomingRequest("telegram", "answer", "9:123", "123", thread, "Tomorrow", reply_route=route))
    assert database.fetch_one("SELECT status FROM jobs WHERE id = %s", (job["id"],))["status"] == "queued"


def test_new_context_defers_all_remaining_old_tool_calls(database):
    migrate(database)
    service = RequestService(database)
    route = {"channel": "telegram", "chat_id": "123"}
    service.receive_request(IncomingRequest("telegram", "original", "9:123", "123", "defer", "plan", reply_route=route))
    job = database.claim_job("test-worker")
    service.receive_request(IncomingRequest("telegram", "correction", "9:123", "123", "defer", "Use the new date", reply_route=route))
    agent = TaskAgent.__new__(TaskAgent)
    agent.db = database
    messages = []
    assert agent.defer_stale_tool_calls(job, messages, [{"id": "one", "function": {"name": "calendar_create_event"}},
                                                     {"id": "two", "function": {"name": "email_send"}}])
    assert [item["tool_call_id"] for item in messages if item["role"] == "tool"] == ["one", "two"]
    assert any("new date" in item.get("content", "") for item in messages)