from types import SimpleNamespace
from unittest.mock import patch

import pytest

from assistant_agent.client_channels import ClientChannels
from assistant_agent.config import AppConfig
from assistant_agent.management_operator import execute
from assistant_agent.replies import ReplyRouter
from test_network_access import db  # noqa: F401


def client_task(db, *, fallbacks=None):
    config = AppConfig({})
    channels = ClientChannels(db, config)
    client_id = "a" * 32
    channels.register(client_id, "Desktop", "desktop", fallbacks or [])
    channels.poll(client_id)
    result = execute(db, config, {"kind": "task.start", "body": "Question",
                                 "request_key": "client-test", "client_id": client_id, "confirmed": True})
    job = db.fetch_one("SELECT * FROM jobs WHERE id = %s", (result["task_id"],))
    runtime = SimpleNamespace(db=db, config=config, job=job)
    return channels, client_id, runtime


@pytest.mark.parametrize("setting", [
    "availability_timeout_seconds", "delivery_timeout_seconds", "poll_interval_seconds",
    "reconciliation_interval_seconds", "retry_interval_seconds", "claim_timeout_seconds",
])
def test_channel_timings_must_be_positive(setting):
    with pytest.raises(ValueError, match="positive"):
        ClientChannels(None, AppConfig({"agent": {"client": {setting: 0}}}))


def test_polling_updates_availability_and_acknowledges_only_offered_owned_messages(db):
    channels, identifier, runtime = client_task(db)
    result = ReplyRouter(runtime).send("Answer")
    assert result["status"] == "queued"
    assert channels.acknowledge(identifier, [result["log_id"]]) == {"acknowledged": []}
    assert channels.poll(identifier)["messages"][0]["body"] == "Answer"
    assert channels.poll(identifier)["messages"][0]["id"] == result["log_id"]
    other = "b" * 32
    channels.register(other, "Another client", "custom", [])
    assert channels.acknowledge(other, [result["log_id"]]) == {"acknowledged": []}
    assert channels.acknowledge(identifier, [result["log_id"]]) == {"acknowledged": [result["log_id"]]}
    assert channels.poll(identifier)["messages"] == []
    assert channels.client(identifier)["available"]
    assert ReplyRouter(runtime).send("Answer")["status"] == "sent"


@pytest.mark.parametrize("expired", ["last_poll_at", "offered_at"])
def test_offline_or_unacknowledged_reply_falls_back_once(db, expired):
    channels, identifier, runtime = client_task(db, fallbacks=["email"])
    runtime.config.values["agent"] = {
        "admin": {"email": "admin@example.com"},
        "email": {"smtp_host": "smtp.test.invalid", "smtp_username": "user", "smtp_password": "password"},
    }
    result = ReplyRouter(runtime).send("Answer")
    channels.poll(identifier)
    table, predicate = ("client_channels", "id = %s") if expired == "last_poll_at" else (
        "channel_deliveries", "destination = %s",
    )
    db.execute(f"UPDATE {table} SET {expired} = now() - interval '2 minutes' WHERE {predicate}", (identifier,))
    with patch("assistant_agent.replies.ReplyRouter.email", return_value={"status": "sent"}) as send:
        assert channels.reconcile()["processed"] == 1
        assert channels.reconcile()["processed"] == 0
        send.assert_called_once()
    row = db.fetch_one("SELECT status, fallback_channel FROM channel_deliveries WHERE id = %s", (result["log_id"],))
    assert row == {"status": "sent", "fallback_channel": "email"}
    assert channels.poll(identifier)["messages"] == []


def test_missing_fallback_keeps_reply_pollable_and_uncertain_delivery_is_not_retried(db):
    channels, identifier, runtime = client_task(db, fallbacks=["email"])
    result = ReplyRouter(runtime).send("Answer")
    channels.unregister(identifier)
    assert channels.reconcile()["processed"] == 1
    assert db.fetch_one("SELECT status FROM channel_deliveries WHERE id = %s", (result["log_id"],))["status"] == "pending"
    channels.register(identifier, "Desktop", "desktop", ["email"])
    assert channels.poll(identifier)["messages"][0]["body"] == "Answer"
    runtime.config.values["agent"] = {
        "admin": {"email": "admin@example.com"},
        "email": {"smtp_host": "smtp.test.invalid", "smtp_username": "user", "smtp_password": "password"},
    }
    db.execute("UPDATE channel_deliveries SET available_at = now(), offered_at = now() - interval '2 minutes'")
    with patch("assistant_agent.replies.ReplyRouter.email", return_value={"status": "uncertain"}) as send:
        channels.reconcile()
        channels.reconcile()
        send.assert_called_once()
    assert db.fetch_one("SELECT status FROM channel_deliveries WHERE id = %s", (result["log_id"],))["status"] == "uncertain"


@pytest.mark.parametrize("during_send", [False, True])
def test_smtp_disconnect_after_send_is_not_retried(db, during_send):
    import smtplib

    channels, identifier, runtime = client_task(db, fallbacks=["email"])
    runtime.config.values["agent"] = {
        "admin": {"email": "admin@example.com"},
        "email": {"smtp_host": "smtp.test.invalid", "smtp_username": "assistant@example.com",
                  "smtp_password": "password", "smtp_from": "assistant@example.com"},
    }
    delivery = ReplyRouter(runtime).send("Answer")
    channels.unregister(identifier)
    with patch("assistant_agent.tools.smtplib.SMTP") as smtp:
        if during_send:
            smtp.return_value.__enter__.return_value.send_message.side_effect = smtplib.SMTPServerDisconnected("Lost response")
        else:
            smtp.side_effect = OSError("Connection unavailable")
        assert channels.reconcile()["processed"] == 1
        assert channels.reconcile()["processed"] == 0
        smtp.assert_called_once()
    row = db.fetch_one("SELECT status FROM channel_deliveries WHERE id = %s", (delivery["log_id"],))
    assert row["status"] == ("uncertain" if during_send else "pending")
    routed = db.fetch_one("SELECT status FROM channel_deliveries WHERE channel = 'email'")
    assert routed["status"] == ("uncertain" if during_send else "failed")


@pytest.mark.parametrize("outcome", ["accepted", "lost_response", "auth_rejected"])
def test_email_fallback_over_local_tls_smtp(db, tmp_path, outcome):
    import shutil
    import socketserver
    import ssl
    import subprocess
    from threading import Thread

    if not shutil.which("openssl"):
        pytest.skip("requires OpenSSL to create a disposable test certificate")
    key, cert = tmp_path / "smtp.key", tmp_path / "smtp.crt"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
        "-addext", "subjectAltName=IP:127.0.0.1",
    ], check=True, capture_output=True)
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert, key)
    client_tls = ssl.create_default_context(cafile=str(cert))
    received = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            connection = self.request
            connection.settimeout(10)

            def line():
                data = b""
                while not data.endswith(b"\n"):
                    chunk = connection.recv(1)
                    if not chunk:
                        return b""
                    data += chunk
                return data

            connection.sendall(b"220 localhost test SMTP\r\n")
            try:
                while command := line():
                    verb = command.split(None, 1)[0].upper()
                    if verb in {b"EHLO", b"HELO"}:
                        connection.sendall(b"250-localhost\r\n250-STARTTLS\r\n250 AUTH PLAIN LOGIN\r\n")
                    elif verb == b"STARTTLS":
                        connection.sendall(b"220 Ready for TLS\r\n")
                        connection = server_tls.wrap_socket(connection, server_side=True)
                    elif verb == b"AUTH":
                        connection.sendall(b"535 Authentication rejected\r\n" if outcome == "auth_rejected"
                                           else b"235 Authentication successful\r\n")
                    elif verb in {b"MAIL", b"RCPT"}:
                        connection.sendall(b"250 OK\r\n")
                    elif verb == b"DATA":
                        connection.sendall(b"354 End data with a dot\r\n")
                        data = []
                        while True:
                            value = line()
                            if not value or value == b".\r\n":
                                break
                            data.append(value)
                        received.append(b"".join(data))
                        if outcome == "lost_response":
                            return
                        connection.sendall(b"250 Accepted\r\n")
                    elif verb == b"QUIT":
                        connection.sendall(b"221 Bye\r\n")
                        return
                    else:
                        connection.sendall(b"500 Unsupported command\r\n")
            finally:
                connection.close()

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            channels, identifier, runtime = client_task(db, fallbacks=["email"])
            runtime.config.values["agent"] = {
                "admin": {"email": "admin@example.com"},
                "email": {"smtp_host": "127.0.0.1", "smtp_port": server.server_address[1],
                          "smtp_username": "assistant@example.com", "smtp_password": "synthetic",
                          "smtp_from": "assistant@example.com"},
            }
            delivery = ReplyRouter(runtime).send("Synthetic TLS fallback")
            channels.unregister(identifier)
            with patch("assistant_agent.tools.ssl.create_default_context", return_value=client_tls):
                assert channels.reconcile()["processed"] == 1
                assert channels.reconcile()["processed"] == 0
            status = db.fetch_one("SELECT status FROM channel_deliveries WHERE id = %s", (delivery["log_id"],))
            assert status["status"] == {
                "accepted": "sent", "lost_response": "uncertain", "auth_rejected": "pending",
            }[outcome]
            assert len(received) == (0 if outcome == "auth_rejected" else 1)
            if received:
                assert b"Synthetic TLS fallback" in received[0]
            if outcome == "auth_rejected":
                outcome = "accepted"
                db.execute("UPDATE channel_deliveries SET available_at = now() WHERE id = %s", (delivery["log_id"],))
                with patch("assistant_agent.tools.ssl.create_default_context", return_value=client_tls):
                    assert channels.reconcile()["processed"] == 1
                    assert channels.reconcile()["processed"] == 0
                assert db.fetch_one("SELECT status FROM channel_deliveries WHERE id = %s",
                                    (delivery["log_id"],))["status"] == "sent"
                assert len(received) == 1
        finally:
            server.shutdown()
            thread.join(timeout=10)


@pytest.mark.parametrize("partial", [False, True])
def test_telegram_failure_falls_back_only_if_no_parts_were_sent(db, partial):
    channels, identifier, runtime = client_task(db, fallbacks=["telegram", "email"])
    runtime.config.values["agent"] = {
        "admin": {"email": "admin@example.com"},
        "email": {"smtp_host": "smtp.test.invalid", "smtp_username": "user", "smtp_password": "password"},
    }
    delivery = ReplyRouter(runtime).send("Answer")
    channels.unregister(identifier)
    with patch("assistant_agent.telegram.telegram_configured", return_value=True), \
            patch("assistant_agent.telegram.telegram_destination", return_value="123"), \
            patch("assistant_agent.telegram.TelegramDelivery") as telegram, \
            patch("assistant_agent.replies.ReplyRouter.email", return_value={"status": "sent"}) as email:
        telegram.return_value.send.return_value = {"status": "failed", "message_ids": [99] if partial else []}
        assert channels.reconcile()["processed"] == 1
        assert email.call_count == (0 if partial else 1)
    row = db.fetch_one("SELECT status, fallback_channel FROM channel_deliveries WHERE id = %s", (delivery["log_id"],))
    assert row == {"status": "uncertain" if partial else "sent", "fallback_channel": "telegram" if partial else "email"}


@pytest.mark.parametrize("failure", ["rejected", "server_error", "lost_response"])
def test_telegram_transport_failure_controls_fallback_and_retries(db, failure):
    import json
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    from assistant_agent.telegram import TelegramClient

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if failure == "lost_response":
                self.connection.shutdown(2)
                return
            code = 401 if failure == "rejected" else 503
            body = json.dumps({"ok": False, "error_code": code}).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            channels, identifier, runtime = client_task(db, fallbacks=["telegram", "email"])
            runtime.config.values["agent"] = {
                "admin": {"email": "admin@example.com"},
                "telegram": {"enabled": True, "bot_token": "synthetic",
                             "allowed_user_ids": ["123"], "recipients": {"admin@example.com": "123"}},
                "email": {"smtp_host": "smtp.test.invalid", "smtp_username": "user", "smtp_password": "password"},
            }
            delivery = ReplyRouter(runtime).send("Synthetic provider failure")
            channels.unregister(identifier)
            original_init = TelegramClient.__init__

            def local_client(client, config):
                original_init(client, config)
                client.base_url = f"http://127.0.0.1:{server.server_port}/bot"

            with patch.object(TelegramClient, "__init__", local_client), \
                    patch("assistant_agent.replies.ReplyRouter.email", return_value={"status": "sent"}) as email:
                assert channels.reconcile()["processed"] == 1
                assert channels.reconcile()["processed"] == 0
                assert email.call_count == (1 if failure == "rejected" else 0)
            row = db.fetch_one(
                "SELECT status, fallback_channel FROM channel_deliveries WHERE id = %s", (delivery["log_id"],),
            )
            assert row == {
                "status": "sent" if failure == "rejected" else "uncertain",
                "fallback_channel": "email" if failure == "rejected" else "telegram",
            }
            telegram = db.fetch_one("SELECT status FROM channel_deliveries WHERE channel = 'telegram'")
            assert telegram["status"] == ("failed" if failure == "rejected" else "uncertain")
            assert len(requests) == 1
        finally:
            server.shutdown()
            thread.join(timeout=10)


def test_fallback_claim_prevents_concurrent_sends_and_late_acknowledgment(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    channels, identifier, runtime = client_task(db, fallbacks=["email"])
    runtime.config.values["agent"] = {
        "admin": {"email": "admin@example.com"},
        "email": {"smtp_host": "smtp.test.invalid", "smtp_username": "user", "smtp_password": "password"},
    }
    delivery = ReplyRouter(runtime).send("Answer")
    channels.poll(identifier)
    db.execute("UPDATE channel_deliveries SET offered_at = now() - interval '2 minutes' WHERE id = %s",
               (delivery["log_id"],))
    claimed, release = Event(), Event()

    def send(*args, **kwargs):
        claimed.set()
        assert release.wait(10)
        return {"status": "sent"}

    with patch("assistant_agent.replies.ReplyRouter.email", side_effect=send) as email, \
            ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(channels.reconcile)
        try:
            assert claimed.wait(10)
            assert channels.reconcile()["processed"] == 0
            assert channels.acknowledge(identifier, [delivery["log_id"]]) == {"acknowledged": []}
            assert channels.poll(identifier)["messages"] == []
        finally:
            release.set()
        assert future.result(timeout=10)["processed"] == 1
        email.assert_called_once()


def test_non_task_notice_falls_back_and_client_failure_notification_is_not_silently_skipped(db):
    from assistant_agent.notifications import notify_admin_job_failure
    channels, identifier, runtime = client_task(db, fallbacks=["email"])
    with patch("assistant_agent.notifications._body", return_value="Task failed"):
        notify_admin_job_failure(db, runtime.config, runtime.job, "failed", "Failure", "test")
        notify_admin_job_failure(db, runtime.config, runtime.job, "failed", "Failure", "test")
    messages = channels.poll(identifier)["messages"]
    assert [(message["purpose"], message["body"]) for message in messages] == [("failure_notice", "Task failed")]
    channels.acknowledge(identifier, [messages[0]["id"]])
    notice = channels.notice("Review approval in Management", "approval", "notice:test")
    runtime.config.values["agent"] = {
        "admin": {"email": "admin@example.com"},
        "email": {"smtp_host": "smtp.test.invalid", "smtp_username": "user", "smtp_password": "password"},
    }
    channels.unregister(identifier)
    with patch("assistant_agent.email_approvals.send_admin_email") as send:
        channels.reconcile()
        send.assert_called_once()
    assert db.fetch_one("SELECT status FROM channel_deliveries WHERE id = %s", (notice["log_id"],))["status"] == "sent"


@pytest.mark.parametrize("partial_failure", [False, True])
def test_telegram_notice_preserves_long_text_and_partial_delivery_requires_review(db, partial_failure):
    from assistant_agent.telegram import TelegramError

    channels, identifier, runtime = client_task(db, fallbacks=["telegram", "email"])
    runtime.config.values["agent"] = {
        "admin": {"email": "admin@example.com"},
        "email": {"smtp_host": "smtp.test.invalid", "smtp_username": "user", "smtp_password": "password"},
    }
    text = "Notice " + "\U0001f600" * 4100
    notice = channels.notice(text, "approval", "notice:long")
    channels.unregister(identifier)
    with patch("assistant_agent.telegram_config.telegram_configured", return_value=True), \
            patch("assistant_agent.approval_channels.telegram_admins", return_value=["123"]), \
            patch("assistant_agent.telegram.TelegramClient") as telegram, \
            patch("assistant_agent.email_approvals.send_admin_email") as email:
        send = telegram.return_value.call
        if partial_failure:
            send.side_effect = [None, TelegramError("Rejected")]
        assert channels.reconcile()["processed"] == 1
        assert channels.reconcile()["processed"] == 0
        email.assert_not_called()
    row = db.fetch_one("SELECT status, fallback_channel FROM channel_deliveries WHERE id = %s", (notice["log_id"],))
    assert row == {"status": "uncertain" if partial_failure else "sent", "fallback_channel": "telegram"}
    parts = [call.args[1]["text"] for call in send.call_args_list]
    assert all(len(part.encode("utf-16-le")) // 2 <= 4096 for part in parts)
    if not partial_failure:
        assert "".join(parts) == text


def test_history_survives_acknowledgment_and_uncertain_delivery_can_be_resolved(db):
    channels, identifier, runtime = client_task(db)
    first = ReplyRouter(runtime).send("Answer")
    channels.poll(identifier)
    channels.acknowledge(identifier, [first["log_id"]])
    notice = channels.notice("Review", "approval", "notice:history")
    db.execute("UPDATE channel_deliveries SET status = 'uncertain', error = 'Unknown outcome' WHERE id = %s",
               (notice["log_id"],))
    history = channels.history(identifier)["messages"]
    assert [(item["body"], item["status"]) for item in history] == [
        ("Review", "uncertain"), ("Answer", "sent"),
    ]
    assert [item["body"] for item in channels.history(identifier, notice["log_id"])["messages"]] == ["Answer"]
    channels.resolve(identifier, notice["log_id"], "return-to-client")
    assert channels.poll(identifier)["messages"][0]["id"] == notice["log_id"]
    with pytest.raises(ValueError, match="not awaiting"):
        channels.resolve(identifier, first["log_id"], "mark-sent")


def test_uncertain_delivery_marked_sent_is_not_offered_or_retried(db):
    channels, identifier, runtime = client_task(db, fallbacks=["email"])
    delivery = ReplyRouter(runtime).send("Answer")
    db.execute("UPDATE channel_deliveries SET status = 'uncertain', error = 'Unknown outcome' WHERE id = %s",
               (delivery["log_id"],))
    assert channels.resolve(identifier, delivery["log_id"], "mark-sent")["delivery"]["status"] == "sent"
    assert channels.poll(identifier)["messages"] == []
    assert channels.reconcile()["processed"] == 0
    item = channels.history(identifier)["messages"][0]
    assert item["status"] == "sent"
    assert item["sent_at"] is not None
    assert item["error"] is None


def test_notice_keeps_original_client_when_another_is_available(db):
    channels, identifier, runtime = client_task(db)
    other = "b" * 32
    channels.register(other, "Active", "desktop", [])
    channels.poll(other)
    db.execute("UPDATE client_channels SET last_poll_at = now() - interval '2 minutes' WHERE id = %s", (identifier,))
    notice = channels.notice("Review", "approval", "notice:available", job_id=runtime.job["id"])
    assert channels.poll(other)["messages"] == []
    assert db.fetch_one("SELECT destination FROM channel_deliveries WHERE id = %s", (notice["log_id"],))[
        "destination"] == identifier


def test_history_cursor_pages_without_duplicates(db):
    channels, identifier, _ = client_task(db)
    db.execute(
        """INSERT INTO channel_deliveries(channel, destination, purpose, request_key, body_text)
           SELECT 'client', %s, 'notice', 'history-page:' || n, n::text FROM generate_series(1, 101) n""",
        (identifier,),
    )
    first = channels.history(identifier)
    second = channels.history(identifier, first["next_before_id"])
    ids = [row["id"] for row in first["messages"] + second["messages"]]
    assert len(first["messages"]) == 100
    assert len(ids) == len(set(ids)) == 101
    assert second["next_before_id"] is None


def test_registration_can_reuse_a_stable_client_id(db):
    identifier = "c" * 32
    request = {"kind": "client.register", "client_id": identifier, "name": "Desktop",
               "client_type": "desktop", "fallback_channels": ["telegram", "email"],
               "request_key": "first-operation"}
    assert execute(db, AppConfig({}), request)["client"]["id"] == identifier
    request["request_key"] = "second-operation"
    assert execute(db, AppConfig({}), request)["client"]["id"] == identifier
    assert len(ClientChannels(db, AppConfig({})).list()["clients"]) == 1


def test_task_start_selects_workstream_and_only_its_attached_files(db, tmp_path):
    from assistant_agent.workstream_files import WorkstreamFileTracker

    root = tmp_path / "share"
    root.mkdir()
    path = root / "report.txt"
    path.write_text("A report")
    config = AppConfig({"agent": {"filesystem": {"shared_root": str(root)}}})
    identifier = "d" * 32
    ClientChannels(db, config).register(identifier, "Desktop", "desktop", [])
    project = db.fetch_one("SELECT ensure_workstream('test-project', 'Project') AS id")["id"]
    other = db.fetch_one("SELECT ensure_workstream('other-project', 'Other') AS id")["id"]
    WorkstreamFileTracker(db.for_workstream(project), config, None).track_file(path)
    file_id = db.fetch_one("SELECT id FROM workstream_files WHERE path = %s", (str(path),))["id"]
    request = {"kind": "task.start", "body": "Review this", "request_key": "project-task",
               "client_id": identifier, "workstream_id": project, "attachment_file_ids": [file_id],
               "confirmed": True}
    result = execute(db, config, request)
    assert result["workstream_id"] == project
    message = db.fetch_one("SELECT attachments FROM request_messages WHERE id = %s", (result["request_id"],))
    assert message["attachments"][0]["path"] == "report.txt"
    reply = execute(db, config, {"kind": "task.reply", "body": "Another look", "request_key": "project-reply",
                                 "client_id": identifier, "task_id": result["task_id"],
                                 "attachment_file_ids": [file_id], "confirmed": True})
    assert reply["workstream_id"] == project
    assert db.fetch_one("SELECT attachments FROM request_messages WHERE id = %s", (reply["request_id"],))[
        "attachments"][0]["file_id"] == file_id
    with pytest.raises(ValueError, match="not available"):
        execute(db, config, {**request, "request_key": "other-task", "workstream_id": other})
    assert db.fetch_one("SELECT id FROM request_messages WHERE external_id = 'other-task'") is None
    with pytest.raises(ValueError, match="Select a workstream"):
        execute(db, config, {**request, "request_key": "unscoped-task", "workstream_id": None})
