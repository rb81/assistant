import logging
import json
from concurrent.futures import ThreadPoolExecutor
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
from assistant_agent.telegram import TelegramClient, TelegramDelivery, TelegramError, TelegramURLFilter, telegram_destination
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


def test_reminder_prefers_linked_telegram_and_honors_explicit_email():
    values = config()
    values.values["agent"]["email"] = {"smtp_host": "smtp.example.net", "smtp_from": "agent@example.net"}
    job = {"metadata": {"reply_route": {"channel": "email", "to": "user@example.com"}}}
    policy = reminder_delivery(values, job)
    assert policy["primary"]["channel"] == "telegram"
    assert policy["fallback"]["to"] == "user@example.com"
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
    yield Database(url)


def migrate(database):
    source = Path(__file__).resolve().parents[1] / "src/assistant_agent/migrations/007_request_channels.sql"
    with tempfile.TemporaryDirectory() as directory:
        shutil.copy2(source, directory)
        assert database.run_migrations(Path(directory)) == [source.name]
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
    assert method == "sendDocument" and "Task #%s" % job["id"] in payload["caption"]
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