import json
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Any, Optional

from psycopg.types.json import Jsonb

from .artifact_processor import public_artifact_manifest, public_attachment_metadata
from .config import AppConfig, agent_name, worker_id
from .database import Database, json_safe
from .llm_client import LlmClient
from .notifications import notify_admin_job_failure
from .polling import poll_interval_seconds, run_poll_loop
from .tool_result_cache import ToolResultCache
from .tools import EMAIL_TOOL_NAMES, FILE_TOOL_NAMES, FUNCTION_TOOLS, WEB_SEARCH_TOOL_NAMES, ToolError, ToolRuntime, tool_name
from .validation import admin_configured, search_configured, shared_root_status, smtp_configured


LOGGER = logging.getLogger("assistant.deep_research")


RESEARCH_SYSTEM_PROMPT_TEMPLATE = """You are %(agent_name)s's constrained deep research agent.

Rules:
- Work only with the provided tools.
- Use web_search when current/live web evidence is needed.
- email_search only searches stored emails; it is not web search.
- For latest/current/news tasks, call web_search before relying on cached files or prior notes unless the requester explicitly says not to.
- Do not claim web search is unavailable unless web_search returns an explicit error.
- Save durable findings, source notes, and deliverables with File tools under the shared workspace when useful.
- For substantial result sets, write findings to files incrementally instead of keeping all details in the conversation.
- Prefer concise, targeted searches. After each search pass, assess whether the research objective is sufficiently answered before searching again.
- If requester guidance is needed, call research_request_human_input with a concise question.
- When the research is sufficient, call research_complete with a clear summary and any output file paths.
- If the research cannot be completed, call research_failed with the blocking reason.
- Keep source attributions in saved notes or summaries when search results support claims.
"""


def compact_json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=True)


def research_system_prompt(config: AppConfig) -> str:
    return RESEARCH_SYSTEM_PROMPT_TEMPLATE % {"agent_name": agent_name(config)}


def truncate_text(value: Any, limit: int = 6000) -> str:
    text = value if isinstance(value, str) else compact_json(value)
    if len(text) <= limit:
        return text
    return "%s..." % text[:limit]


class DeepResearchAgent:
    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.locked_by = worker_id("deep-research-agent")
        self.tool_result_cache = ToolResultCache(config)

    def run_forever(self, stop_requested) -> None:
        interval = poll_interval_seconds(self.config, "agent.deep_research.poll_interval_seconds")
        run_poll_loop(
            stop_requested,
            self.run_once,
            interval,
            should_sleep=lambda worked: not worked,
            logger=LOGGER,
            error_message="deep research loop failed",
        )

    def run_once(self) -> bool:
        if not self.config.get_bool("agent.deep_research.enabled", True):
            return False
        run = self.claim_run()
        if run is None:
            return False
        LOGGER.info("claimed deep research run %s", run["id"])
        try:
            self.process_run(run)
        except Exception as exc:
            LOGGER.exception("deep research run %s failed during processing", run["id"])
            self.handle_run_exception(run, str(exc))
        return True

    def claim_run(self) -> Optional[dict[str, Any]]:
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH picked AS (
                      SELECT id
                      FROM deep_research_runs
                      WHERE status = 'queued'
                        AND run_at <= now()
                      ORDER BY priority DESC, created_at ASC
                      FOR UPDATE SKIP LOCKED
                      LIMIT 1
                    )
                    UPDATE deep_research_runs
                    SET status = 'running',
                        locked_at = now(),
                        locked_by = %s,
                        attempts = attempts + 1,
                        updated_at = now()
                    WHERE id IN (SELECT id FROM picked)
                    RETURNING *
                    """,
                    (self.locked_by,),
                )
                return cur.fetchone()

    def process_run(self, run: dict[str, Any]) -> None:
        if not search_configured(self.config):
            self.fail_run(run, "Deep research requires OpenRouter web search configuration.")
            return
        original_job = self.db.fetch_one("SELECT * FROM jobs WHERE id = %s", (run["original_job_id"],))
        if original_job is None:
            self.update_run_status(run["id"], "failed", "original job not found")
            return

        llm = LlmClient(
            self.config,
            model=self.config.get("agent.deep_research.model") or self.config.get("agent.llm.model"),
            timeout_seconds=self.config.get_int("agent.deep_research.timeout_seconds", 180),
        )
        messages = self.build_messages(run, original_job)
        tools = research_tools(self.config)
        max_iterations = self.config.get_int("agent.deep_research.max_iterations", 15)

        for _iteration in range(max_iterations):
            current_run = self.current_run(run["id"])
            if current_run is None or current_run["status"] != "running":
                return
            self.log_event(
                run["id"],
                "llm_request",
                input_data={"message_count": len(messages), "tool_names": [tool_name(item) for item in tools]},
            )
            started = datetime.now(timezone.utc)
            response = llm.chat(messages, tools)
            duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            choice = response["choices"][0]
            response_message = choice["message"]
            token_data = response.get("usage") or {}
            messages.append(response_message)
            self.log_event(
                run["id"],
                "llm_response",
                output_data=response_message,
                tokens_used=token_data,
                duration_ms=duration_ms,
            )
            self.prune_tool_results(messages)

            tool_calls = response_message.get("tool_calls") or []
            if not tool_calls:
                messages.append(
                    {
                        "role": "user",
                        "content": "Continue by calling a tool. If more evidence is required and budget remains, use web_search. Otherwise call exactly one terminal tool: research_complete, research_failed, or research_request_human_input.",
                    }
                )
                continue

            for tool_call in tool_calls:
                terminal = self.handle_tool_call(run, original_job, messages, tool_call)
                if terminal:
                    return

        self.fail_run(run, "deep research max iterations reached")

    def build_messages(self, run: dict[str, Any], original_job: dict[str, Any]) -> list[dict[str, Any]]:
        emails = self.db.latest_thread_emails(run["original_thread_id"], limit=12)
        events = self.db.fetch_all(
            """
            SELECT event_type, tool_name, input_data, output_data, created_at
            FROM deep_research_events
            WHERE run_id = %s
              AND event_type IN ('tool_call', 'tool_result', 'search_result', 'status_change', 'error')
            ORDER BY sequence DESC
            LIMIT 30
            """,
            (run["id"],),
        )
        events = list(reversed(events))
        content_lines = [
            "Deep research run ID: %s" % run["id"],
            "Original job ID: %s" % original_job["id"],
            "Original thread ID: %s" % run["original_thread_id"],
            "Research question:",
            run["research_question"],
        ]
        if run.get("instructions"):
            content_lines.extend(["", "Additional instructions:", run["instructions"]])
        artifact_rows = self.db.processed_artifacts_for_thread(run["original_thread_id"], limit=100)
        artifacts_by_email: dict[int, list[dict[str, Any]]] = {}
        for artifact in artifact_rows:
            artifacts_by_email.setdefault(int(artifact["email_id"]), []).append(public_artifact_manifest(artifact))
        content_lines.extend(
            [
                "",
                "Tool budget: %s nonterminal tool calls maximum; %s already used."
                % (run.get("max_tool_calls") or 40, run.get("tool_call_count") or 0),
                "",
                "Original email context:",
            ]
        )
        for item in emails:
            content_lines.append(
                "\nEmail ID: %s\nMessage-ID: %s\nFrom: %s\nSubject: %s\nReceived: %s\nBody:\n%s"
                % (
                    item["id"],
                    item["message_id"],
                    item["from_address"],
                    item.get("subject") or "",
                    item["received_at"],
                    truncate_text(item.get("body_text") or item.get("body_html") or "", 5000),
                )
            )
            if item.get("attachments"):
                content_lines.append("Attachments: %s" % compact_json([public_attachment_metadata(value) for value in item["attachments"]]))
            if artifacts_by_email.get(int(item["id"])):
                content_lines.append("Processed artifacts: %s" % compact_json(artifacts_by_email[int(item["id"])]))
        if events:
            content_lines.extend(["", "Prior research events:"])
            for event in events:
                content_lines.append(
                    "- %s %s input=%s output=%s"
                    % (
                        event["event_type"],
                        event.get("tool_name") or "",
                        truncate_text(event.get("input_data") or {}, 1200),
                        truncate_text(event.get("output_data") or {}, 2500),
                    )
                )
        return [
            {"role": "system", "content": research_system_prompt(self.config)},
            {"role": "user", "content": "\n".join(content_lines)},
        ]

    def handle_tool_call(
        self,
        run: dict[str, Any],
        original_job: dict[str, Any],
        messages: list[dict[str, Any]],
        tool_call: dict[str, Any],
    ) -> bool:
        name = tool_call["function"]["name"]
        try:
            arguments = json.loads(tool_call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        self.log_event(run["id"], "tool_call", tool_name=name, input_data=arguments)

        if name == "research_complete":
            summary = str(arguments.get("summary") or "Research completed.").strip()
            result_data = {
                "output_files": arguments.get("output_files") or [],
                "data": arguments.get("data") or {},
            }
            self.complete_run(run, summary, result_data)
            self.log_event(run["id"], "tool_result", tool_name=name, output_data={"summary": summary, "result_data": result_data})
            return True

        if name == "research_failed":
            reason = str(arguments.get("reason") or "Research failed.").strip()
            self.fail_run(run, reason)
            self.log_event(run["id"], "tool_result", tool_name=name, output_data={"reason": reason})
            return True

        if name == "research_request_human_input":
            question = str(arguments.get("question") or "Research guidance is required.").strip()
            self.email_original_sender_for_input(run, original_job, question)
            self.pause_for_input(run, question)
            self.log_event(run["id"], "tool_result", tool_name=name, output_data={"question": question})
            return True

        current_run = self.current_run(run["id"]) or run
        if int(current_run.get("tool_call_count") or 0) >= int(current_run.get("max_tool_calls") or 40):
            result = {"error": "deep research tool call budget exceeded; call research_complete or research_failed"}
            self.log_event(run["id"], "error", tool_name=name, input_data=arguments, output_data=result)
        else:
            runtime = ResearchToolRuntime(self.db, self.config, current_run, original_job, self)
            try:
                started = datetime.now(timezone.utc)
                result = runtime.run(name, arguments)
                duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            except (ToolError, TypeError, ValueError) as exc:
                result = {"error": str(exc)}
                duration_ms = None
                self.log_event(run["id"], "error", tool_name=name, input_data=arguments, output_data=result)
            else:
                result = self.prepare_tool_result_for_model(original_job["id"], name, result)
                self.increment_tool_count(run["id"])
                self.log_event(
                    run["id"],
                    "tool_result",
                    tool_name=name,
                    output_data=self.redact_tool_result_for_storage(name, result),
                    duration_ms=duration_ms,
                )

        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "name": name,
                "content": compact_json(result),
            }
        )
        return False

    def prepare_tool_result_for_model(self, job_id: int, name: str, result: dict[str, Any]) -> dict[str, Any]:
        return self.tool_result_cache.cache_result(job_id, name, result)

    def redact_tool_result_for_storage(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        return self.tool_result_cache.redact_result(name, result)

    def prune_tool_results(self, messages: list[dict[str, Any]]) -> None:
        for message in messages:
            if message.get("role") != "tool":
                continue
            name = str(message.get("name") or "")
            try:
                result = json.loads(str(message.get("content") or "{}"))
            except json.JSONDecodeError:
                result = {"content": str(message.get("content") or "")}
            message["content"] = compact_json(self.redact_tool_result_for_storage(name, result))

    def complete_run(self, run: dict[str, Any], summary: str, result_data: dict[str, Any]) -> None:
        rich_result_data = dict(result_data or {})
        rich_result_data.setdefault("research_context", self.result_context(run["id"]))
        self.db.execute(
            """
            UPDATE deep_research_runs
            SET status = 'completed',
                result_summary = %s,
                result_data = %s,
                completed_at = now(),
                last_error = NULL,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (summary, Jsonb(json_safe(rich_result_data)), run["id"]),
        )
        self.log_event(run["id"], "status_change", output_data={"status": "completed", "summary": summary})
        self.notify_original_job(run, "completed", summary, rich_result_data)

    def result_context(self, run_id: int) -> dict[str, Any]:
        rows = self.db.fetch_all(
            """
            SELECT event_type, tool_name, input_data, output_data, created_at
            FROM deep_research_events
            WHERE run_id = %s
              AND event_type IN ('search_result', 'tool_result')
            ORDER BY sequence DESC
            LIMIT 16
            """,
            (run_id,),
        )
        search_results = []
        tool_results = []
        for row in reversed(rows):
            output = row.get("output_data") or {}
            if row["event_type"] == "search_result":
                search_results.append(
                    {
                        "queries": output.get("queries") or output.get("clean_queries") or [],
                        "iteration": output.get("iteration"),
                        "content": truncate_text(output.get("content") or output, 3000),
                        "created_at": row["created_at"],
                    }
                )
            elif row.get("tool_name") in ("file_write", "file_append", "file_read", "file_list", "file_search"):
                tool_results.append(
                    {
                        "tool_name": row.get("tool_name"),
                        "output": truncate_text(output, 1500),
                        "created_at": row["created_at"],
                    }
                )
        return {"search_results": search_results[-8:], "file_tool_results": tool_results[-8:]}

    def fail_run(self, run: dict[str, Any], reason: str) -> None:
        self.db.execute(
            """
            UPDATE deep_research_runs
            SET status = 'failed',
                result_summary = %s,
                completed_at = now(),
                last_error = %s,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (reason, reason, run["id"]),
        )
        self.log_event(run["id"], "status_change", output_data={"status": "failed", "reason": reason})
        self.notify_original_job(run, "failed", reason, {})
        original_job = self.db.fetch_one("SELECT * FROM jobs WHERE id = %s", (run["original_job_id"],))
        if original_job:
            notify_admin_job_failure(
                self.db,
                self.config,
                original_job,
                "failed",
                "Deep research run #%s failed: %s" % (run["id"], reason),
                "deep-research-agent",
            )

    def pause_for_input(self, run: dict[str, Any], question: str) -> None:
        self.db.execute(
            """
            UPDATE deep_research_runs
            SET status = 'waiting_for_input',
                waiting_since = now(),
                run_at = %s,
                last_error = %s,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (datetime.now(timezone.utc) + timedelta(hours=24), question, run["id"]),
        )
        self.log_event(run["id"], "status_change", output_data={"status": "waiting_for_input", "question": question})

    def notify_original_job(
        self,
        run: dict[str, Any],
        status: str,
        summary: str,
        result_data: dict[str, Any],
    ) -> None:
        instruction = "\n".join(
            [
                "Deep research run #%s has %s." % (run["id"], status),
                "",
                "Research question:",
                run["research_question"],
                "",
                "Summary:",
                summary,
                "",
                "Result data:",
                compact_json(result_data or {}),
                "",
                "Continue the original task using these research results. Reply to the user when the original task is complete.",
            ]
        )
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO supervisor_instructions(job_id, instruction, created_by) VALUES (%s, %s, %s)",
                    (run["original_job_id"], instruction, "deep-research-agent"),
                )
                cur.execute(
                    """
                    UPDATE jobs
                    SET status = CASE WHEN status IN ('needs_review', 'waiting') THEN 'queued' ELSE status END,
                        run_at = CASE WHEN status IN ('needs_review', 'waiting') THEN now() ELSE run_at END,
                        has_new_context = true,
                        last_error = CASE WHEN status IN ('queued', 'needs_review', 'waiting') THEN NULL ELSE last_error END,
                        locked_at = CASE WHEN status IN ('needs_review', 'waiting') THEN NULL ELSE locked_at END,
                        locked_by = CASE WHEN status IN ('needs_review', 'waiting') THEN NULL ELSE locked_by END,
                        updated_at = now()
                    WHERE id = %s
                      AND status NOT IN ('completed', 'failed', 'cancelled')
                    RETURNING id
                    """,
                    (run["original_job_id"],),
                )
                updated = cur.fetchone()
                if updated:
                    self.log_job_event(
                        cur,
                        run["original_job_id"],
                        "status_change",
                        output_data={"status": "queued", "reason": "deep research %s" % status, "deep_research_run_id": run["id"]},
                    )

    def email_original_sender_for_input(self, run: dict[str, Any], original_job: dict[str, Any], question: str) -> None:
        tool_runtime = ToolRuntime(self.db, self.config, original_job)
        emails = self.db.latest_thread_emails(run["original_thread_id"], limit=1)
        latest = emails[-1] if emails else {}
        recipient = latest.get("from_address")
        parsed_recipient = parseaddr(str(recipient or ""))[1] or str(recipient or "")
        if smtp_configured(self.config) and parsed_recipient and not parsed_recipient.endswith("@local"):
            subject = latest.get("subject") or "Research guidance needed"
            if not subject.lower().startswith("re:"):
                subject = "Re: %s" % subject
            result = tool_runtime.email_send(
                to=[recipient],
                subject=subject,
                body=question,
                in_reply_to=latest.get("message_id"),
            )
            self.db.log_event(original_job["id"], "tool_result", tool_name="email_send", output_data=result)
            return

        if not admin_configured(self.config) or not smtp_configured(self.config):
            self.db.log_event(
                original_job["id"],
                "supervisor_note",
                output_data={"reason": "research guidance email not sent because SMTP or admin email is not configured"},
            )
            return
        admin_email = self.config.get("agent.admin.email")
        result = tool_runtime.email_send(
            to=[admin_email],
            subject="%s research guidance needed for run #%s" % (agent_name(self.config), run["id"]),
            body="\n\n".join(["%s needs guidance for a deep research run." % agent_name(self.config), "Question:", question]),
            in_reply_to=latest.get("message_id"),
        )
        self.db.log_event(original_job["id"], "tool_result", tool_name="email_send", output_data=result)

    def handle_run_exception(self, run: dict[str, Any], reason: str) -> None:
        attempts = int(run.get("attempts") or 1)
        max_attempts = int(run.get("max_attempts") or 3)
        if attempts >= max_attempts:
            self.fail_run(run, reason)
            return
        self.db.execute(
            """
            UPDATE deep_research_runs
            SET status = 'queued',
                run_at = %s,
                last_error = %s,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (datetime.now(timezone.utc) + timedelta(minutes=1), reason, run["id"]),
        )
        self.log_event(run["id"], "error", output_data={"reason": reason, "retry": True})

    def update_run_status(self, run_id: int, status: str, last_error: Optional[str] = None) -> None:
        self.db.execute(
            """
            UPDATE deep_research_runs
            SET status = %s,
                last_error = %s,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (status, last_error, run_id),
        )
        self.log_event(run_id, "status_change", output_data={"status": status, "last_error": last_error})

    def current_run(self, run_id: int) -> Optional[dict[str, Any]]:
        return self.db.fetch_one("SELECT * FROM deep_research_runs WHERE id = %s", (run_id,))

    def increment_tool_count(self, run_id: int) -> None:
        self.db.execute("UPDATE deep_research_runs SET tool_call_count = tool_call_count + 1, updated_at = now() WHERE id = %s", (run_id,))

    def log_event(
        self,
        run_id: int,
        event_type: str,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
        tool_name: Optional[str] = None,
        tokens_used: Optional[dict[str, Any]] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM deep_research_events WHERE run_id = %s", (run_id,))
                sequence = cur.fetchone()["next_sequence"]
                cur.execute(
                    """
                    INSERT INTO deep_research_events(
                      run_id,
                      sequence,
                      event_type,
                      tool_name,
                      input_data,
                      output_data,
                      tokens_used,
                      duration_ms
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        sequence,
                        event_type,
                        tool_name,
                        Jsonb(json_safe(input_data)) if input_data is not None else None,
                        Jsonb(json_safe(output_data)) if output_data is not None else None,
                        Jsonb(json_safe(tokens_used)) if tokens_used is not None else None,
                        duration_ms,
                    ),
                )

    def log_job_event(
        self,
        cur,
        job_id: int,
        event_type: str,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
        tool_name: Optional[str] = None,
    ) -> None:
        cur.execute("SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM task_logs WHERE job_id = %s", (job_id,))
        sequence = cur.fetchone()["next_sequence"]
        cur.execute(
            """
            INSERT INTO task_logs(job_id, sequence, event_type, tool_name, input_data, output_data)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                job_id,
                sequence,
                event_type,
                tool_name,
                Jsonb(json_safe(input_data)) if input_data is not None else None,
                Jsonb(json_safe(output_data)) if output_data is not None else None,
            ),
        )


class ResearchToolRuntime:
    def __init__(
        self,
        db: Database,
        config: AppConfig,
        run: dict[str, Any],
        original_job: dict[str, Any],
        agent: DeepResearchAgent,
    ):
        self.db = db
        self.config = config
        self.run_data = run
        self.original_job = original_job
        self.agent = agent
        self.tool_runtime = ToolRuntime(
            db,
            config,
            original_job,
            cache_read_job_ids=self.cache_read_job_ids(),
            search_model_override=config.get("agent.deep_research.search_model") or None,
        )

    def run(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name in FILE_TOOL_NAMES or name in EMAIL_TOOL_NAMES or name in WEB_SEARCH_TOOL_NAMES:
            return self.tool_runtime.run(name, arguments)
        raise ToolError("unknown research tool: %s" % name)

    def cache_read_job_ids(self) -> set[int]:
        ids = {int(self.original_job["id"])}
        try:
            rows = self.db.fetch_all(
                """
                SELECT id
                FROM jobs
                WHERE thread_id = %s
                ORDER BY id DESC
                LIMIT 50
                """,
                (self.original_job["thread_id"],),
            )
        except Exception:
            return ids
        for row in rows:
            try:
                ids.add(int(row["id"]))
            except (KeyError, TypeError, ValueError):
                continue
        return ids

RESEARCH_COMPLETE_TOOL = {
    "type": "function",
    "function": {
        "name": "research_complete",
        "description": "Mark the deep research run complete and return findings to the original job.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "output_files": {"type": "array", "items": {"type": "string"}},
                "data": {"type": "object"},
            },
            "required": ["summary"],
        },
    },
}

RESEARCH_FAILED_TOOL = {
    "type": "function",
    "function": {
        "name": "research_failed",
        "description": "Mark the deep research run failed when it cannot continue.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
}

RESEARCH_REQUEST_INPUT_TOOL = {
    "type": "function",
    "function": {
        "name": "research_request_human_input",
        "description": "Email the original sender for research guidance and pause the research run.",
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
}


def research_tools(config: AppConfig) -> list[dict[str, Any]]:
    names = {"email_search", "email_read"}
    if smtp_configured(config):
        names.add("email_send")
    if shared_root_status(config)["available"]:
        names.update(FILE_TOOL_NAMES)
    if search_configured(config):
        names.update(WEB_SEARCH_TOOL_NAMES)
    tools = [tool for tool in FUNCTION_TOOLS if tool_name(tool) in names]
    tools.extend([RESEARCH_COMPLETE_TOOL, RESEARCH_FAILED_TOOL, RESEARCH_REQUEST_INPUT_TOOL])
    return tools
