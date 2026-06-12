"""
Tests for:
  1. _diagnostics() stop-reason classifier - all known error patterns
  2. _diagnostics() SQL token-format robustness (malformed/missing fields degrade gracefully)
  3. email-based admin approval override via EmailDownloader.apply_email_review_override()
  4. request_limit_increase tool handler status-transition (TaskAgent)
  5. /api/jobs/{id}/poll endpoint shape (lightweight integration)
"""

import os
import sys
import types
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub heavy dependencies that aren't available without a Docker/venv runtime
# ---------------------------------------------------------------------------
sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda handle: {}))

# Set DATABASE_URL to bypass POSTGRES_PASSWORD check in api.py module initialization
os.environ.setdefault("DATABASE_URL", "postgresql://stub:stub@localhost/stub")

psycopg_module = types.ModuleType("psycopg")
# MagicMock returns a context manager that supports with statement and has cursor() method
mock_cursor = MagicMock()
mock_cursor.__enter__.return_value = mock_cursor
mock_cursor.__exit__.return_value = None

mock_conn = MagicMock()
mock_conn.__enter__.return_value = mock_conn
mock_conn.__exit__.return_value = None
mock_conn.cursor.return_value = mock_cursor

psycopg_module.connect = MagicMock(return_value=mock_conn)
psycopg_module.Connection = object
sys.modules.setdefault("psycopg", psycopg_module)

rows_module = types.ModuleType("psycopg.rows")
rows_module.dict_row = object()
sys.modules.setdefault("psycopg.rows", rows_module)

json_module = types.ModuleType("psycopg.types.json")
json_module.Jsonb = lambda value: value
sys.modules.setdefault("psycopg.types", types.ModuleType("psycopg.types"))
sys.modules.setdefault("psycopg.types.json", json_module)

markdown_module = types.ModuleType("markdown_it")


class _FakeMd:
    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

    def enable(self, *a: Any, **kw: Any) -> "_FakeMd":
        return self

    def render(self, v: str) -> str:
        return v


markdown_module.MarkdownIt = _FakeMd
sys.modules.setdefault("markdown_it", markdown_module)

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from assistant_agent.config import AppConfig  # noqa: E402
from assistant_agent.notifications import _diagnostics, compute_review_diagnostics  # noqa: E402
from assistant_agent.email_ingest import EmailDownloader  # noqa: E402


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

def _config(**limits: Any) -> AppConfig:
    """Build a minimal AppConfig with optional overrides inside agent.limits."""
    merged: dict[str, Any] = {
        "max_iterations_per_task": 50,
        "max_tokens_per_task": 1_000_000,
    }
    merged.update(limits)
    return AppConfig({"agent": {"limits": merged, "supervisor": {}}})


class _StubDb:
    """Minimal database stub for _diagnostics().

    iter_count  -- number returned by the llm_request COUNT query
    usage_row   -- row returned by the token-usage CTE query
    """

    def __init__(
        self,
        iter_count: int = 0,
        usage_row: Optional[dict[str, Any]] = None,
    ) -> None:
        self._iter_count = iter_count
        self._usage_row = usage_row or {"api_call_count": 0, "total_tokens": 0}
        self.queries: list[str] = []

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        self.queries.append(sql)
        if "COUNT(*) AS count" in sql and "llm_request" in sql:
            return {"count": self._iter_count}
        if "WITH usage_logs" in sql:
            return self._usage_row
        # checkpoint query (used by _body / _latest_checkpoint)
        if "agent_checkpoints" in sql:
            return None
        return None

    def latest_thread_emails(self, thread_id: str, limit: int = 1) -> list[dict[str, Any]]:
        return []

    def log_event(self, *args: Any, **kwargs: Any) -> None:
        pass

    def execute(self, *args: Any, **kwargs: Any) -> None:
        pass

    def fetch_all(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []


def _job(last_error: str = "", metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {
        "id": 42,
        "thread_id": "thread-1",
        "last_error": last_error,
        "metadata": metadata,
        "task_summary": "test job",
    }


# ===========================================================================
# 1. Stop-reason classifier tests
# ===========================================================================

class StopReasonClassifierTest(unittest.TestCase):
    def _diag(self, last_error: str, **limit_overrides: Any) -> dict[str, Any]:
        db = _StubDb(iter_count=5, usage_row={"api_call_count": 5, "total_tokens": 500_000})
        return _diagnostics(db, _config(**limit_overrides), _job(last_error=last_error))

    # --- token_budget -------------------------------------------------------
    def test_token_budget_stop_reason(self) -> None:
        d = self._diag("token budget exceeded")
        self.assertEqual(d["stop_reason"], "token_budget")
        self.assertIn("Token budget exceeded", d["explanation"])

    def test_token_budget_case_insensitive(self) -> None:
        d = self._diag("TOKEN BUDGET EXCEEDED")
        self.assertEqual(d["stop_reason"], "token_budget")

    # --- max_iterations -----------------------------------------------------
    def test_max_iterations_stop_reason(self) -> None:
        d = self._diag("max iterations reached")
        self.assertEqual(d["stop_reason"], "max_iterations")
        self.assertIn("Max iterations reached", d["explanation"])

    # --- stalled ------------------------------------------------------------
    def test_stalled_stop_reason(self) -> None:
        d = self._diag("no task log progress detected after 3 polls")
        self.assertEqual(d["stop_reason"], "stalled")

    # --- repeated_failure ---------------------------------------------------
    def test_repeated_failure_stop_reason(self) -> None:
        d = self._diag("tool sandbox_exec failed three times consecutively")
        self.assertEqual(d["stop_reason"], "repeated_failure")

    # --- agent_limit_request (new task_agent.py format) ---------------------
    def test_agent_limit_request_new_format(self) -> None:
        """task_agent.py writes: 'agent limit increase request (tokens): reason text'"""
        d = self._diag("agent limit increase request (tokens): Need more tokens to finish analysis")
        self.assertEqual(d["stop_reason"], "agent_limit_request")
        self.assertIn("Need more tokens", d["explanation"])
        self.assertIn("Agent requested limit increase", d["explanation"])

    def test_agent_limit_request_both_resource(self) -> None:
        d = self._diag("agent limit increase request (both): Lots of work remaining")
        self.assertEqual(d["stop_reason"], "agent_limit_request")

    def test_agent_limit_request_legacy_format(self) -> None:
        """The legacy classifier used 'limit increase requested' phrasing."""
        d = self._diag("limit increase requested: need 20 more iterations")
        self.assertEqual(d["stop_reason"], "agent_limit_request")
        self.assertIn("need 20 more iterations", d["explanation"])

    # --- other / unknown ----------------------------------------------------
    def test_other_stop_reason_for_arbitrary_error(self) -> None:
        d = self._diag("some unexpected error occurred")
        self.assertEqual(d["stop_reason"], "other")
        self.assertIn("some unexpected error", d["explanation"])

    def test_unknown_stop_reason_when_no_error(self) -> None:
        d = self._diag("")
        self.assertEqual(d["stop_reason"], "unknown")

    # --- percentage calculations --------------------------------------------
    def test_token_percentage_calculation(self) -> None:
        db = _StubDb(iter_count=20, usage_row={"api_call_count": 20, "total_tokens": 800_000})
        d = _diagnostics(db, _config(max_tokens_per_task=1_000_000), _job(last_error="token budget exceeded"))
        self.assertEqual(d["usage"]["token_pct"], 80.0)
        self.assertEqual(d["usage"]["total_tokens"], 800_000)

    def test_iteration_percentage_calculation(self) -> None:
        db = _StubDb(iter_count=40, usage_row={"api_call_count": 40, "total_tokens": 0})
        d = _diagnostics(db, _config(max_iterations_per_task=50), _job(last_error="max iterations reached"))
        self.assertEqual(d["usage"]["iteration_pct"], 80.0)
        self.assertEqual(d["usage"]["iterations_used"], 40)

    def test_zero_max_does_not_divide_by_zero(self) -> None:
        """Pathological config with 0 limits must not raise ZeroDivisionError."""
        db = _StubDb(usage_row={"api_call_count": 1, "total_tokens": 100})
        # Force 0 limit through the override path
        job = _job(metadata={"admin_review_override": {"max_iterations_per_task": 0, "max_tokens_per_task": 0}})
        d = _diagnostics(db, _config(), job)
        self.assertEqual(d["usage"]["token_pct"], 0.0)
        self.assertEqual(d["usage"]["iteration_pct"], 0.0)


# ===========================================================================
# 2. Token-field robustness – the DB stub simulates various malformed payloads
#    by returning what the PostgreSQL CTE returns (already aggregated).
#    We test that _diagnostics() handles None, 0, and large values gracefully.
# ===========================================================================

class DiagnosticsRobustnessTest(unittest.TestCase):
    def test_null_usage_row_returns_zeros(self) -> None:
        """If the usage CTE returns None (empty table), no exception is raised."""
        db = _StubDb(iter_count=0, usage_row=None)
        d = _diagnostics(db, _config(), _job())
        self.assertEqual(d["usage"]["total_tokens"], 0)
        self.assertEqual(d["usage"]["api_calls"], 0)

    def test_zero_total_tokens_in_usage_row(self) -> None:
        db = _StubDb(usage_row={"api_call_count": 5, "total_tokens": 0})
        d = _diagnostics(db, _config(), _job())
        self.assertEqual(d["usage"]["total_tokens"], 0)
        self.assertEqual(d["usage"]["api_calls"], 5)

    def test_large_token_count(self) -> None:
        db = _StubDb(usage_row={"api_call_count": 300, "total_tokens": 9_999_999})
        d = _diagnostics(db, _config(max_tokens_per_task=10_000_000), _job())
        self.assertAlmostEqual(d["usage"]["token_pct"], 100.0, delta=0.1)

    def test_compute_review_diagnostics_public_alias(self) -> None:
        """compute_review_diagnostics is just a public alias for _diagnostics."""
        db = _StubDb()
        job = _job()
        d1 = _diagnostics(db, _config(), job)
        d2 = compute_review_diagnostics(db, _config(), job)  # type: ignore[arg-type]
        self.assertEqual(d1["stop_reason"], d2["stop_reason"])

    def test_metadata_override_limits_are_respected(self) -> None:
        """When an admin override exists in metadata, diagnostics use the new limits."""
        db = _StubDb(
            iter_count=55,
            usage_row={"api_call_count": 55, "total_tokens": 600_000},
        )
        job = _job(
            last_error="max iterations reached",
            metadata={"admin_review_override": {"max_iterations_per_task": 60, "max_tokens_per_task": 1_200_000}},
        )
        d = _diagnostics(db, _config(max_iterations_per_task=50, max_tokens_per_task=1_000_000), job)
        self.assertEqual(d["limits"]["max_iterations"], 60)
        self.assertEqual(d["limits"]["max_tokens"], 1_200_000)
        # pct should be relative to overridden limit
        self.assertAlmostEqual(d["usage"]["iteration_pct"], round(100.0 * 55 / 60, 1), delta=0.01)

    def test_non_dict_metadata_falls_back_gracefully(self) -> None:
        """Malformed metadata (e.g. a string) must not crash."""
        db = _StubDb()
        job = _job(metadata="bad-metadata")  # type: ignore[arg-type]
        d = _diagnostics(db, _config(), job)
        self.assertEqual(d["limits"]["max_iterations"], 50)  # falls back to config default


# ===========================================================================
# 3. Email-based admin approval override
# ===========================================================================

class _ApprovalQueueDb:
    """Database stub that tracks apply_email_review_override interactions."""

    def __init__(self, job: Optional[dict[str, Any]] = None) -> None:
        self._job = job
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.logged: list[tuple[Any, ...]] = []

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        if "FROM deep_research_runs" in sql:
            return None
        if "FROM jobs" in sql:
            return self._job
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executed.append((sql, params))

    def log_event(self, *args: Any, **kwargs: Any) -> None:
        self.logged.append((args, kwargs))

    def latest_thread_emails(self, *args: Any, **kwargs: Any) -> list:
        return []


def _make_downloader(db: Any, extra_config: Optional[dict[str, Any]] = None) -> EmailDownloader:
    cfg: dict[str, Any] = {
        "agent": {
            "supervisor": {
                "admin_email": "admin@example.com",
                "email_approval_increase_factor": 1.2,
            },
            "notifications": {},
            "limits": {"max_iterations_per_task": 50, "max_tokens_per_task": 1_000_000},
            "filesystem": {"shared_root": "/tmp"},
            "artifacts": {"raw_root": "/tmp"},
        }
    }
    if extra_config:
        cfg["agent"].update(extra_config)
    downloader = EmailDownloader.__new__(EmailDownloader)
    downloader.db = db  # type: ignore[assignment]
    downloader.config = AppConfig(cfg)
    return downloader


class EmailApprovalOverrideTest(unittest.TestCase):
    def test_is_admin_sender_matches_plain_admin_email(self) -> None:
        # from_addresses in the ingest pipeline are plain email addresses
        # (already parsed from headers), not RFC 2822 display-name format.
        d = _make_downloader(_ApprovalQueueDb())
        self.assertTrue(d.is_admin_sender(["admin@example.com"]))

    def test_is_admin_sender_matches_multiple_senders(self) -> None:
        d = _make_downloader(_ApprovalQueueDb())
        self.assertTrue(d.is_admin_sender(["other@example.com", "admin@example.com"]))

    def test_is_admin_sender_case_insensitive(self) -> None:
        d = _make_downloader(_ApprovalQueueDb())
        self.assertTrue(d.is_admin_sender(["ADMIN@EXAMPLE.COM"]))

    def test_is_admin_sender_rejects_unknown(self) -> None:
        d = _make_downloader(_ApprovalQueueDb())
        self.assertFalse(d.is_admin_sender(["other@example.com"]))

    def test_is_admin_sender_returns_false_when_no_admin_configured(self) -> None:
        downloader = EmailDownloader.__new__(EmailDownloader)
        downloader.db = _ApprovalQueueDb()  # type: ignore[assignment]
        downloader.config = AppConfig({"agent": {"supervisor": {}, "notifications": {}}})
        self.assertFalse(downloader.is_admin_sender(["admin@example.com"]))

    def test_apply_email_review_override_requeues_job(self) -> None:
        job = {
            "id": 99,
            "status": "needs_review",
            "metadata": {},
            "last_error": "token budget exceeded",
            "thread_id": "thread-1",
        }
        db = _ApprovalQueueDb(job=job)
        d = _make_downloader(db)

        d.apply_email_review_override(job, trigger_email_id=7, body_text="")

        update_sqls = [sql for sql, _ in db.executed if "UPDATE jobs" in sql]
        self.assertTrue(len(update_sqls) >= 1, "Expected at least one UPDATE jobs statement")
        combined = " ".join(sql for sql, _ in db.executed)
        self.assertIn("status = 'queued'", combined)

    def test_apply_email_review_override_increases_limits(self) -> None:
        job = {
            "id": 99,
            "status": "needs_review",
            "metadata": {},
            "last_error": "max iterations reached",
            "thread_id": "thread-1",
        }
        db = _ApprovalQueueDb(job=job)
        d = _make_downloader(db)
        d.apply_email_review_override(job, trigger_email_id=7, body_text="")

        # The override dict is passed as first param to execute() for the UPDATE
        update_params = [params for sql, params in db.executed if "UPDATE jobs" in sql]
        self.assertTrue(len(update_params) >= 1)
        override_payload = update_params[0][0]  # Jsonb({...})
        # The Jsonb stub just returns the raw dict
        self.assertIn("admin_review_override", override_payload)
        override = override_payload["admin_review_override"]
        # Factor 1.2 × default 50 = 60; 1.2 × 1_000_000 = 1_200_000
        self.assertEqual(override["max_iterations_per_task"], 60)
        self.assertEqual(override["max_tokens_per_task"], 1_200_000)

    def test_apply_email_review_override_compounds_existing_override(self) -> None:
        """A second email approval should stack on top of an existing override."""
        job = {
            "id": 100,
            "status": "needs_review",
            "metadata": {"admin_review_override": {"max_iterations_per_task": 60, "max_tokens_per_task": 1_200_000}},
            "last_error": "max iterations reached",
            "thread_id": "thread-2",
        }
        db = _ApprovalQueueDb(job=job)
        d = _make_downloader(db)
        d.apply_email_review_override(job, trigger_email_id=8, body_text="")

        update_params = [params for sql, params in db.executed if "UPDATE jobs" in sql]
        override = update_params[0][0]["admin_review_override"]
        # 1.2 × 60 = 72; 1.2 × 1_200_000 = 1_440_000
        self.assertEqual(override["max_iterations_per_task"], 72)
        self.assertEqual(override["max_tokens_per_task"], 1_440_000)

    def test_apply_email_review_override_inserts_instruction_when_body_present(self) -> None:
        job = {
            "id": 101,
            "status": "needs_review",
            "metadata": {},
            "last_error": "max iterations reached",
            "thread_id": "thread-3",
        }
        db = _ApprovalQueueDb(job=job)
        d = _make_downloader(db)
        d.apply_email_review_override(job, trigger_email_id=9, body_text="Please finish the summary section.")

        instruction_sqls = [sql for sql, _ in db.executed if "supervisor_instructions" in sql]
        self.assertTrue(len(instruction_sqls) == 1, "Expected INSERT into supervisor_instructions")

    def test_apply_email_review_override_skips_instruction_insert_for_empty_body(self) -> None:
        job = {
            "id": 102,
            "status": "needs_review",
            "metadata": {},
            "last_error": "max iterations reached",
            "thread_id": "thread-4",
        }
        db = _ApprovalQueueDb(job=job)
        d = _make_downloader(db)
        d.apply_email_review_override(job, trigger_email_id=10, body_text="  ")  # whitespace only

        instruction_sqls = [sql for sql, _ in db.executed if "supervisor_instructions" in sql]
        self.assertEqual(len(instruction_sqls), 0, "Should not insert empty instruction")

    def test_queue_or_update_job_triggers_override_for_admin_sender(self) -> None:
        job = {
            "id": 99,
            "status": "needs_review",
            "metadata": {},
            "last_error": "max iterations reached",
        }
        db = _ApprovalQueueDb(job=job)
        d = _make_downloader(db)

        # Patch apply_email_review_override so we can assert it was called
        called: list[dict[str, Any]] = []

        def _fake_override(j: dict, trigger_email_id: int, body_text: str) -> None:
            called.append({"job": j, "trigger_email_id": trigger_email_id, "body_text": body_text})

        d.apply_email_review_override = _fake_override  # type: ignore[method-assign]

        d.queue_or_update_job(
            thread_id="thread-1",
            subject="Re: Task",
            trigger_email_id=20,
            from_addresses=["admin@example.com"],
            body_text="Proceed with additional research.",
        )

        self.assertEqual(len(called), 1)
        self.assertEqual(called[0]["trigger_email_id"], 20)
        self.assertEqual(called[0]["body_text"], "Proceed with additional research.")

    def test_queue_or_update_job_does_not_override_for_non_admin_sender(self) -> None:
        job = {
            "id": 99,
            "status": "needs_review",
            "metadata": {},
            "last_error": "max iterations reached",
        }
        db = _ApprovalQueueDb(job=job)
        d = _make_downloader(db)

        called: list[bool] = []
        d.apply_email_review_override = lambda *a, **kw: called.append(True)  # type: ignore[method-assign]

        d.queue_or_update_job(
            thread_id="thread-1",
            subject="Re: Task",
            trigger_email_id=21,
            from_addresses=["user@example.com"],  # not admin
            body_text="Can you do more?",
        )

        self.assertEqual(len(called), 0, "Should not call override for non-admin sender")


# ===========================================================================
# 4. request_limit_increase tool handler (TaskAgent) – unit check on the
#    last_error string format to ensure the classifier round-trip works.
# ===========================================================================

class RequestLimitIncreaseFormatTest(unittest.TestCase):
    """Verify the error string written by task_agent.py is correctly classified
    by notifications.py without needing to run the full task loop."""

    def _classify(self, last_error: str) -> str:
        db = _StubDb()
        return _diagnostics(db, _config(), _job(last_error=last_error))["stop_reason"]

    def test_tokens_resource_classified_correctly(self) -> None:
        last_error = "agent limit increase request (tokens): Running out of token budget midway through analysis"
        self.assertEqual(self._classify(last_error), "agent_limit_request")

    def test_iterations_resource_classified_correctly(self) -> None:
        last_error = "agent limit increase request (iterations): Many steps still pending"
        self.assertEqual(self._classify(last_error), "agent_limit_request")

    def test_both_resource_classified_correctly(self) -> None:
        last_error = "agent limit increase request (both): Comprehensive research required"
        self.assertEqual(self._classify(last_error), "agent_limit_request")

    def test_explanation_strips_prefix_cleanly(self) -> None:
        last_error = "agent limit increase request (tokens): Need X tokens for Y"
        db = _StubDb()
        d = _diagnostics(db, _config(), _job(last_error=last_error))
        self.assertNotIn("agent limit increase request", d["explanation"])
        self.assertIn("Need X tokens for Y", d["explanation"])


# ===========================================================================
# 5. /api/jobs/{id}/poll endpoint shape
#    Lightweight check: build a minimal FastAPI test client to call the
#    new endpoint without a live DB.
# ===========================================================================

class PollEndpointShapeTest(unittest.TestCase):
    """Verify the poll endpoint returns the expected top-level keys.

    We monkey-patch the module-level `db` and `config` used by api.py so no
    real database or config file is needed.
    """

    def test_poll_endpoint_returns_expected_keys(self) -> None:
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi[testclient] not available in this environment")

        import assistant_agent.api as api_module  # noqa: PLC0415

        # Stub the module-level db and config
        stub_db = _PollStubDb()
        api_module.db = stub_db  # type: ignore[attr-defined]
        api_module.config = AppConfig({"agent": {"limits": {}}})  # type: ignore[attr-defined]

        client = TestClient(api_module.app, raise_server_exceptions=True)
        resp = client.get("/api/jobs/1/poll?after_sequence=0")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        for key in ("job", "usage", "actions", "new_logs"):
            self.assertIn(key, body, "Missing key '%s' in poll response" % key)

    def test_poll_endpoint_returns_404_for_missing_job(self) -> None:
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi[testclient] not available in this environment")

        import assistant_agent.api as api_module  # noqa: PLC0415

        stub_db = _PollStubDb(job=None)
        api_module.db = stub_db  # type: ignore[attr-defined]
        api_module.config = AppConfig({"agent": {"limits": {}}})  # type: ignore[attr-defined]

        client = TestClient(api_module.app, raise_server_exceptions=False)
        resp = client.get("/api/jobs/999/poll")
        self.assertEqual(resp.status_code, 404)


class _PollStubDb:
    def __init__(self, job: Optional[dict[str, Any]] = None) -> None:
        self._job: Optional[dict[str, Any]] = job if job is not None else {
            "id": 1,
            "status": "running",
            "last_error": None,
            "task_summary": "test",
            "updated_at": None,
            "locked_at": None,
            "attempts": 0,
            "completed_at": None,
            "metadata": {},
            "thread_id": "thread-1",
        }

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        if "FROM jobs" in sql:
            return self._job
        return {"count": 0, "total_tokens": 0, "api_call_count": 0, "cost_total": 0.0}

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return []

    def log_event(self, *args: Any, **kwargs: Any) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
