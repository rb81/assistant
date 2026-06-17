import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda handle: {}))
psycopg_module = types.ModuleType("psycopg")
psycopg_module.connect = lambda *args, **kwargs: None
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


class FakeMarkdownIt:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def enable(self, *args: Any, **kwargs: Any) -> "FakeMarkdownIt":
        return self

    def render(self, value: str) -> str:
        return value


markdown_module.MarkdownIt = FakeMarkdownIt
sys.modules.setdefault("markdown_it", markdown_module)
notifications_module = types.ModuleType("assistant_agent.notifications")
notifications_module.notify_admin_job_failure = lambda *args, **kwargs: None
sys.modules.setdefault("assistant_agent.notifications", notifications_module)

from assistant_agent.config import AppConfig
from assistant_agent.contact_store import CONTACT_FIELDS
from assistant_agent.deep_research import research_tools
from assistant_agent.task_agent import TaskAgent
from assistant_agent.tool_result_cache import ToolResultCache
from assistant_agent.tools import LOADABLE_TOOL_NAMES, ToolError, ToolRuntime, available_function_names, tool_catalog, tool_name


def search_config(temp_dir: str) -> AppConfig:
    return AppConfig(
        {
            "agent": {
                "llm": {"base_url": "https://openrouter.ai/api/v1", "model": "openai/gpt-4.1-mini"},
                "search": {"enabled": True, "max_results": 5, "max_total_results": 10},
                "filesystem": {"shared_root": temp_dir, "require_mount": False},
                "tool_result_cache": {"enabled": True, "root": ".cache/tool-results", "min_bytes": 100},
                "projects": {"enabled": False},
                "deep_research": {"enabled": True},
            }
        }
    )


def calendar_enabled_config(temp_dir: str) -> AppConfig:
    return AppConfig(
        {
            "agent": {
                "calendar": {
                    "enabled": True,
                    "store": {"vdir_path": str(Path(temp_dir) / "calendar" / "vdir"), "default_calendar": "main"},
                    "sync": {"command": [], "before_read": False, "before_write": False, "after_write": False},
                },
                "filesystem": {"shared_root": temp_dir, "require_mount": False},
                "tool_result_cache": {"enabled": True, "root": ".cache/tool-results", "min_bytes": 100},
                "projects": {"enabled": False},
                "deep_research": {"enabled": False},
            }
        }
    )


class FakeReminderDatabase:
    def __init__(self) -> None:
        self.reminders: list[dict[str, Any]] = []
        self.next_id = 1

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        if "metadata->>'idempotency_key'" in sql:
            job_id, key = params
            for reminder in self.reminders:
                if (
                    reminder["created_by_job_id"] == job_id
                    and reminder["status"] in {"scheduled", "queued"}
                    and (reminder.get("metadata") or {}).get("idempotency_key") == key
                ):
                    return reminder
            return None
        if "AND title = %s" in sql:
            job_id, title, task, run_at, priority, unit, interval, anchor_day = params
            for reminder in self.reminders:
                if (
                    reminder["created_by_job_id"] == job_id
                    and reminder["status"] in {"scheduled", "queued"}
                    and reminder["title"] == title
                    and reminder["task"] == task
                    and reminder["run_at"] == run_at
                    and reminder["priority"] == priority
                    and (reminder.get("recurrence_unit") or "") == (unit or "")
                    and (reminder.get("recurrence_interval") or 0) == (interval or 0)
                    and (reminder.get("recurrence_anchor_day") or 0) == (anchor_day or 0)
                ):
                    return reminder
            return None
        if "INSERT INTO reminders" in sql:
            (
                title,
                task,
                run_at,
                priority,
                recurrence_unit,
                recurrence_interval,
                recurrence_anchor_day,
                created_by,
                created_by_job_id,
                metadata,
            ) = params
            reminder = {
                "id": self.next_id,
                "title": title,
                "task": task,
                "run_at": run_at,
                "status": "scheduled",
                "priority": priority,
                "recurrence_unit": recurrence_unit,
                "recurrence_interval": recurrence_interval,
                "recurrence_anchor_day": recurrence_anchor_day,
                "created_by": created_by,
                "created_by_job_id": created_by_job_id,
                "metadata": metadata,
            }
            self.next_id += 1
            self.reminders.append(reminder)
            return reminder
        return None


class FakeCursor:
    def __init__(self) -> None:
        self.run: dict[str, Any] | None = None

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        if "INSERT INTO deep_research_runs" in sql:
            self.run = {
                "id": 91,
                "original_job_id": params[0],
                "original_thread_id": params[1],
                "title": params[2],
                "research_question": params[3],
                "instructions": params[4],
                "priority": params[5],
                "max_tool_calls": params[6],
                "metadata": params[7],
            }

    def fetchone(self) -> dict[str, Any]:
        if self.run is None:
            raise AssertionError("no run inserted")
        return self.run


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_obj = FakeCursor()

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self.cursor_obj


class FakeResearchDatabase:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    def connect(self) -> FakeConnection:
        return self.connection


class FakeContactDatabase:
    def __init__(self) -> None:
        self.contacts: list[dict[str, Any]] = []
        self.next_id = 1

    def row(self, values: dict[str, Any]) -> dict[str, Any]:
        row = {
            "id": self.next_id,
            "first_name": "",
            "last_name": "",
            "email_address": "",
            "company": "",
            "title": "",
            "notes": "",
            "source": "agent",
            "created_at": "2026-06-07T00:00:00Z",
            "updated_at": "2026-06-07T00:00:00Z",
        }
        row.update(values)
        return row

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT") and "FROM contacts WHERE id = %s" in normalized:
            contact_id = int(params[0])
            return next((contact for contact in self.contacts if int(contact["id"]) == contact_id), None)
        if normalized.startswith("SELECT") and "FROM contacts WHERE lower(email_address)" in normalized:
            email = str(params[0]).lower()
            return next((contact for contact in self.contacts if str(contact["email_address"]).lower() == email), None)
        if normalized.startswith("INSERT INTO contacts"):
            first_name, last_name, email_address, company, title, notes, source = params
            contact = self.row(
                {
                    "first_name": first_name,
                    "last_name": last_name,
                    "email_address": email_address,
                    "company": company,
                    "title": title,
                    "notes": notes,
                    "source": source,
                }
            )
            self.next_id += 1
            self.contacts.append(contact)
            return contact
        if normalized.startswith("UPDATE contacts"):
            contact_id = int(params[-1])
            contact = next((item for item in self.contacts if int(item["id"]) == contact_id), None)
            if contact is None:
                return None
            update_names = [name for name in CONTACT_FIELDS if "%s = %%s" % name in sql]
            for name, value in zip(update_names, params[:-1]):
                contact[name] = value
            contact["updated_at"] = "2026-06-07T01:00:00Z"
            return contact
        if normalized.startswith("DELETE FROM contacts"):
            contact_id = int(params[0])
            for index, contact in enumerate(self.contacts):
                if int(contact["id"]) == contact_id:
                    return self.contacts.pop(index)
            return None
        return None

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if len(params) == 1:
            return list(self.contacts)[: int(params[0])]
        pattern = str(params[0]).strip("%").lower()
        limit = int(params[-1])
        matches = []
        for contact in self.contacts:
            haystack = " ".join(str(contact.get(name) or "") for name in CONTACT_FIELDS).lower()
            if pattern in haystack:
                matches.append(contact)
        return matches[:limit]


class ToolWebSearchAndReminderTest(unittest.TestCase):
    def test_contact_tools_are_loadable_without_optional_services(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = AppConfig({"agent": {"filesystem": {"shared_root": temp_dir, "require_mount": False}}})
            available = available_function_names(config, {"id": 7, "thread_id": "thread"})

            self.assertIn("contact_create", available)
            self.assertIn("contact_search", tool_catalog(config, {"id": 7, "thread_id": "thread"}))

            agent = TaskAgent.__new__(TaskAgent)
            agent.config = config
            enabled: set[str] = set()
            result = agent.load_tool_specs(
                {"id": 7, "thread_id": "thread"},
                {"tools": ["contact_search", "contact_read", "contact_create", "contact_update", "contact_delete"]},
                enabled,
            )

            self.assertEqual(
                result["loaded"],
                ["contact_create", "contact_delete", "contact_read", "contact_search", "contact_update"],
            )
            self.assertIn("contact_delete", enabled)

    def test_calendar_tools_are_loadable_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = calendar_enabled_config(temp_dir)
            available = available_function_names(config, {"id": 7, "thread_id": "thread"})

            self.assertIn("calendar_list_busy", available)
            self.assertIn("calendar_create_event", tool_catalog(config, {"id": 7, "thread_id": "thread"}))

            agent = TaskAgent.__new__(TaskAgent)
            agent.config = config
            enabled: set[str] = set()
            result = agent.load_tool_specs(
                {"id": 7, "thread_id": "thread"},
                {"tools": ["calendar_list_busy", "calendar_create_event", "calendar_update_event", "calendar_delete_event"]},
                enabled,
            )

            self.assertEqual(
                result["loaded"],
                ["calendar_create_event", "calendar_delete_event", "calendar_list_busy", "calendar_update_event"],
            )
            self.assertIn("calendar_delete_event", enabled)

    def test_contact_tools_create_search_update_and_delete_contacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = ToolRuntime(
                FakeContactDatabase(),  # type: ignore[arg-type]
                search_config(temp_dir),
                {"id": 12, "thread_id": "thread-1"},
            )

            created = runtime.contact_create(
                first_name="Jane",
                last_name="Doe",
                email_address="JANE@EXAMPLE.COM",
                company="Example Co",
                title="CFO",
                notes="Finance contact.",
            )["contact"]

            self.assertEqual(created["source"], "agent")
            self.assertEqual(created["email_address"], "jane@example.com")
            self.assertEqual(runtime.contact_search("finance")["contacts"][0]["id"], created["id"])

            updated = runtime.contact_update(created["id"], company="Example Holdings", notes="Primary billing contact.")["contact"]

            self.assertEqual(updated["company"], "Example Holdings")
            self.assertEqual(runtime.contact_read(created["id"])["contact"]["notes"], "Primary billing contact.")
            self.assertEqual(runtime.contact_delete(created["id"])["deleted"]["id"], created["id"])
            with self.assertRaisesRegex(ToolError, "contact not found"):
                runtime.contact_read(created["id"])

    def test_web_search_tool_is_loadable_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            config = search_config(temp_dir)
            self.assertIn("web_search", available_function_names(config, {"id": 7, "thread_id": "thread"}))
            self.assertIn("web_search", tool_catalog(config, {"id": 7, "thread_id": "thread"}))

            agent = TaskAgent.__new__(TaskAgent)
            agent.config = config
            enabled = set()
            result = agent.load_tool_specs({"id": 7, "thread_id": "thread"}, {"tools": ["web_search"]}, enabled)

            self.assertEqual(result["loaded"], ["web_search"])
            self.assertIn("web_search", enabled)

    def test_openrouter_server_tool_is_not_loadable_directly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            config = search_config(temp_dir)
            agent = TaskAgent.__new__(TaskAgent)
            agent.config = config
            enabled = set()

            result = agent.load_tool_specs({"id": 7, "thread_id": "thread"}, {"tools": ["openrouter:web_search"]}, enabled)

            self.assertNotIn("openrouter:web_search", LOADABLE_TOOL_NAMES)
            self.assertEqual(result["loaded"], [])
            self.assertEqual(result["invalid"], ["openrouter:web_search"])

    def test_tool_name_falls_back_to_server_tool_type(self) -> None:
        self.assertEqual(tool_name({"type": "openrouter:web_search"}), "openrouter:web_search")

    def test_deep_research_tools_include_web_search_function_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            tools = research_tools(search_config(temp_dir))
            names = [tool_name(item) for item in tools]

            self.assertIn("web_search", names)
            self.assertNotIn("openrouter:web_search", names)

    def test_deep_research_request_accepts_question_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            runtime = ToolRuntime(
                FakeResearchDatabase(),  # type: ignore[arg-type]
                search_config(temp_dir),
                {"id": 12, "thread_id": "thread-1"},
            )

            result = runtime.deep_research_request(question="What changed today?", title="Daily research")

            self.assertEqual(result["deep_research_run"]["research_question"], "What changed today?")
            self.assertEqual(result["deep_research_run"]["title"], "Daily research")

    def test_reminder_create_exact_duplicate_reuses_existing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = ToolRuntime(
                FakeReminderDatabase(),  # type: ignore[arg-type]
                search_config(temp_dir),
                {"id": 12, "thread_id": "thread-1"},
            )

            first = runtime.reminder_create(
                title="Daily summary",
                task="Send a summary",
                run_at="2026-06-08T05:00:00Z",
                recurrence_unit="day",
                recurrence_interval=1,
            )
            second = runtime.reminder_create(
                title="Daily summary",
                task="Send a summary",
                run_at="2026-06-08T05:00:00Z",
                recurrence_unit="day",
                recurrence_interval=1,
            )

            self.assertEqual(first["reminder"]["id"], second["reminder"]["id"])
            self.assertTrue(second["idempotent_reuse"])

    def test_reminder_create_idempotency_key_controls_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = ToolRuntime(
                FakeReminderDatabase(),  # type: ignore[arg-type]
                search_config(temp_dir),
                {"id": 12, "thread_id": "thread-1"},
            )

            first = runtime.reminder_create("Summary A", "Send A", "2026-06-08T05:00:00Z", idempotency_key="daily-news")
            second = runtime.reminder_create("Summary B", "Send B", "2026-06-09T05:00:00Z", idempotency_key="daily-news")
            third = runtime.reminder_create("Summary B", "Send B", "2026-06-09T05:00:00Z", idempotency_key="daily-news-b")

            self.assertEqual(first["reminder"]["id"], second["reminder"]["id"])
            self.assertTrue(second["idempotent_reuse"])
            self.assertNotEqual(first["reminder"]["id"], third["reminder"]["id"])

    def test_file_search_excludes_tool_cache_but_explicit_read_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            visible = root / "notes.txt"
            visible.write_text("visible", encoding="utf-8")
            cache_file = root / ".cache" / "tool-results" / "job-12" / "cached.json"
            cache_file.parent.mkdir(parents=True)
            cache_file.write_text("cached", encoding="utf-8")
            runtime = ToolRuntime(
                FakeReminderDatabase(),  # type: ignore[arg-type]
                search_config(temp_dir),
                {"id": 12, "thread_id": "thread-1"},
            )

            listed = runtime.file_list(".", recursive=True)
            search = runtime.file_search("cached", directory=".")
            read = runtime.file_read(".cache/tool-results/job-12/cached.json")

            self.assertTrue(any(item["relative_path"] == "notes.txt" for item in listed["entries"]))
            self.assertFalse(any(".cache/tool-results" in item["relative_path"] for item in listed["entries"]))
            self.assertEqual(search["matches"], [])
            self.assertEqual(read["content"], "cached")

    def test_file_delete_refuses_protected_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            assistant_file = root / ".assistant" / "config.json"
            assistant_file.parent.mkdir(parents=True)
            assistant_file.write_text("{}", encoding="utf-8")
            runtime = ToolRuntime(
                FakeReminderDatabase(),  # type: ignore[arg-type]
                search_config(temp_dir),
                {"id": 12, "thread_id": "thread-1"},
            )

            # Test protected paths cannot be deleted
            protected_paths = {
                ".": "protected paths cannot be deleted",
                ".assistant": "protected paths cannot be deleted",
            }
            for path, expected_error in protected_paths.items():
                with self.subTest(path=path):
                    with self.assertRaisesRegex(ToolError, expected_error):
                        runtime.file_delete(path)

            # Test .assistant directory contents are read-only
            with self.assertRaisesRegex(ToolError, "read-only to agent file tools"):
                runtime.file_delete(".assistant/config.json")

    def test_file_move_and_copy_use_workspace_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "docs" / "note.md"
            source.parent.mkdir(parents=True)
            source.write_text("draft", encoding="utf-8")
            runtime = ToolRuntime(
                FakeReminderDatabase(),  # type: ignore[arg-type]
                search_config(temp_dir),
                {"id": 12, "thread_id": "thread-1"},
            )

            moved = runtime.file_move("docs/note.md", "archive/note.md")
            copied = runtime.file_copy("archive/note.md", "archive/note-copy.md")

            self.assertFalse(source.exists())
            self.assertEqual(moved["relative_path"], "archive/note.md")
            self.assertEqual(copied["relative_path"], "archive/note-copy.md")
            self.assertEqual((root / "archive" / "note.md").read_text(encoding="utf-8"), "draft")
            self.assertEqual((root / "archive" / "note-copy.md").read_text(encoding="utf-8"), "draft")

    def test_file_move_and_copy_refuse_unsafe_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "docs" / "note.md"
            source.parent.mkdir(parents=True)
            source.write_text("draft", encoding="utf-8")
            existing = root / "docs" / "existing.md"
            existing.write_text("existing", encoding="utf-8")
            assistant_file = root / ".assistant" / "config.json"
            assistant_file.parent.mkdir(parents=True)
            assistant_file.write_text("{}", encoding="utf-8")
            runtime = ToolRuntime(
                FakeReminderDatabase(),  # type: ignore[arg-type]
                search_config(temp_dir),
                {"id": 12, "thread_id": "thread-1"},
            )

            with self.assertRaisesRegex(ToolError, "destination already exists"):
                runtime.file_move("docs/note.md", "docs/existing.md")
            with self.assertRaisesRegex(ToolError, "read-only"):
                runtime.file_move(".assistant/config.json", "config.json")
            with self.assertRaisesRegex(ToolError, "read-only"):
                runtime.file_copy("docs/note.md", ".assistant/note.md")

    def test_assistant_directory_is_read_only_to_file_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            assistant_file = root / ".assistant" / "config.json"
            assistant_file.parent.mkdir(parents=True)
            assistant_file.write_text("{}", encoding="utf-8")
            runtime = ToolRuntime(
                FakeReminderDatabase(),  # type: ignore[arg-type]
                search_config(temp_dir),
                {"id": 12, "thread_id": "thread-1"},
            )

            with self.assertRaisesRegex(ToolError, "read-only"):
                runtime.file_write(".assistant/config.json", "new")
            with self.assertRaisesRegex(ToolError, "read-only"):
                runtime.file_append(".assistant/config.json", "new")
            with self.assertRaisesRegex(ToolError, "read-only"):
                runtime.file_delete(".assistant/config.json")

    def test_cache_file_reads_are_not_cached_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = search_config(temp_dir)
            cache = ToolResultCache(config)
            cache_file = Path(temp_dir) / ".cache" / "tool-results" / "job-12" / "cached.json"
            cache_file.parent.mkdir(parents=True)
            cache_file.write_text("x" * 5000, encoding="utf-8")
            result = {
                "path": str(cache_file),
                "relative_path": ".cache/tool-results/job-12/cached.json",
                "content": "x" * 5000,
                "bytes_read": 5000,
            }

            cached = cache.cache_result(12, "file_read", result)

            self.assertNotIn("cached_output_path", cached)


if __name__ == "__main__":
    unittest.main()
