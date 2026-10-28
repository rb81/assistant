import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from psycopg.conninfo import make_conninfo

from assistant_agent import network_access
from assistant_agent.config import AppConfig
from assistant_agent.database import Database


def test_normalize_host_accepts_urls_and_rejects_ip_literals():
    assert network_access.normalize_host("https://API.Example.com:8443/v1?q=1") == "api.example.com"
    assert network_access.normalize_host("bücher.de") == "xn--bcher-kva.de"
    for bad in ("https://10.0.0.1/x", "[::1]", "localhost", "exa mple.com", ""):
        with pytest.raises(ValueError):
            network_access.normalize_host(bad)


def test_block_wins_and_wildcards_match_subdomains_only():
    current = {"allow": ["*.example.com", "api.other.com"], "block": ["bad.example.com"]}
    assert network_access.decide(current, "cdn.example.com") == "allowed"
    assert network_access.decide(current, "example.com") == "not_approved"
    assert network_access.decide(current, "bad.example.com") == "blocked"
    assert network_access.decide(current, "evilexample.com") == "not_approved"


def test_summarize_events_tells_agent_how_to_proceed():
    events = [
        {"host": "pypi.org", "decision": "allowed"},
        {"host": "api.new.com", "decision": "blocked", "reason": "not_approved"},
        {"host": "api.new.com", "decision": "blocked", "reason": "not_approved"},
        {"host": "evil.com", "decision": "blocked", "reason": "blocked_by_admin"},
    ]
    summary = network_access.summarize_events(events, "proxy")
    assert summary["allowed_hosts"] == ["pypi.org"]
    assert [item["host"] for item in summary["blocked"]] == ["api.new.com", "evil.com"]
    assert "network_access_request" in summary["hint"] and "Do not request or retry" in summary["hint"]
    assert network_access.summarize_events([], "open") is None
    assert network_access.summarize_events([], "none")["egress"] == "none"


def test_parse_email_decision():
    assert network_access.parse_email_decision("Approve once.\nOnly the docs API") == ("once", "Only the docs API")
    assert network_access.parse_email_decision("BLOCK  ALWAYS") == ("blockall", "")
    assert network_access.parse_email_decision("cancel job\nnot needed") == ("cancel", "not needed")
    assert network_access.parse_email_decision("sure go ahead")[0] is None


@pytest.fixture
def db():
    url = os.environ.get("ASSISTANT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires disposable assistant_test database")
    original = Database(url)
    schema = "test_" + uuid.uuid4().hex
    with original.connect() as conn:
        if conn.info.dbname != "assistant_test":
            raise RuntimeError("network access tests require a disposable assistant_test database")
        conn.execute("CREATE SCHEMA " + schema)
    database = Database(make_conninfo(url, options="-c search_path=" + schema))
    try:
        database.execute(Path(__file__).resolve().parents[2].joinpath("database/schema.sql").read_text())
        database.ensure_feature_schema()
        database.run_migrations()
        yield database
    finally:
        original.execute("DROP SCHEMA " + schema + " CASCADE")


CONFIG = AppConfig({"agent": {"sandbox": {"network": {"allowed_hosts": ["pypi.org"]}}}})


def paused_job(db, urls):
    job = db.create_manual_job("Network test", "Fetch data")
    result = network_access.create_request(db, CONFIG, job, urls, "fetch the dataset")
    assert result["status"] == "pending"
    network_access.store_pending_request(db, job["id"], result["request"])
    db.execute("UPDATE jobs SET status = 'needs_review' WHERE id = %s", (job["id"],))
    job = db.fetch_one("SELECT * FROM jobs WHERE id = %s", (job["id"],))
    return job, network_access.approval_payload(job)


def test_create_request_skips_already_allowed_hosts(db):
    job = db.create_manual_job("Network test", "Fetch data")
    result = network_access.create_request(db, CONFIG, job, ["https://pypi.org/simple/x"], "install")
    assert result == {"allowed": ["pypi.org"], "blocked": [], "status": "no_approval_needed"}


def test_approve_once_scopes_to_job_and_resumes_with_instruction(db):
    job, payload = paused_job(db, ["https://api.data.com/v1", "api.data.com"])
    message = network_access.apply_decision(db, payload, "once", "only the v1 endpoint", "telegram-admin")
    assert "api.data.com" in message and "resumed" in message
    current = db.fetch_one("SELECT status, metadata FROM jobs WHERE id = %s", (job["id"],))
    assert current["status"] == "queued" and "network_request" not in current["metadata"]
    instruction = db.fetch_one("SELECT instruction FROM supervisor_instructions WHERE job_id = %s", (job["id"],))
    assert "Approve once" in instruction["instruction"] and "only the v1 endpoint" in instruction["instruction"]
    assert "api.data.com" in network_access.policy(db, CONFIG, job["id"])["allow"]
    other = db.create_manual_job("Other", "Other")
    assert "api.data.com" not in network_access.policy(db, CONFIG, other["id"])["allow"]
    with pytest.raises(ValueError, match="already handled"):
        network_access.apply_decision(db, payload, "always")


def test_block_always_is_global_and_later_requests_report_it(db):
    job, payload = paused_job(db, ["evil.example"])
    network_access.apply_decision(db, payload, "blockall")
    other = db.create_manual_job("Other", "Other")
    result = network_access.create_request(db, CONFIG, other, ["https://evil.example/x"], "again")
    assert result["status"] == "no_approval_needed" and result["blocked"] == ["evil.example"]
    assert "Do not retry" in result["blocked_message"]


def test_cancel_job_stops_task(db):
    job, payload = paused_job(db, ["api.data.com"])
    assert network_access.apply_decision(db, payload, "cancel", "not needed") == "Job #%s cancelled." % job["id"]
    current = db.fetch_one("SELECT status, last_error FROM jobs WHERE id = %s", (job["id"],))
    assert current["status"] == "cancelled" and "not needed" in current["last_error"]
    assert not db.fetch_all("SELECT 1 FROM sandbox_network_rules")


def test_send_approval_uses_network_payload():
    job = {"id": 7, "task_summary": "Fetch", "metadata": {"network_request": {
        "id": "abc", "hosts": ["api.data.com"], "urls": ["https://api.data.com/x"], "purpose": "data"}}}
    with patch("assistant_agent.email_approvals.EmailApprovals") as approvals:
        approvals.return_value.request.return_value = True
        assert network_access.send_approval(MagicMock(), CONFIG, job)
    source, payload, summary = approvals.return_value.request.call_args.args
    assert payload == {"kind": "network", "job_id": 7, "request_id": "abc"}
    assert "api.data.com" in summary and "Approve once" in summary
