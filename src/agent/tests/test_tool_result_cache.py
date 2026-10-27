import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

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

from assistant_agent.config import AppConfig
from assistant_agent.tool_result_cache import ToolResultCache


class ToolResultCacheTest(unittest.TestCase):
    def test_command_result_is_cached_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = AppConfig(
                {
                    "agent": {
                        "filesystem": {"shared_root": temp_dir},
                        "tool_result_cache": {
                            "enabled": True,
                            "root": ".cache/tool-results",
                            "min_bytes": 4096,
                            "retention_days": 7,
                        },
                    }
                }
            )
            cache = ToolResultCache(config)
            result = {"stdout": "x" * 5000, "stderr": "", "exit_code": 0, "timed_out": False}

            cached = cache.cache_result(57, "command_execute", result)
            cached_path = Path(cached["cached_output_path"])

            self.assertTrue(cached_path.exists())
            self.assertTrue(cached["cached_output_relative_path"].startswith(".cache/tool-results/job-57/"))
            self.assertEqual(cached["stdout"], "x" * 5000)

            stored = json.loads(cached_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["result"]["stdout"], "x" * 5000)

            redacted = cache.redact_result("command_execute", cached)
            self.assertNotIn("x" * 50, redacted["stdout"])
            self.assertTrue(redacted["stdout_omitted"])
            self.assertEqual(redacted["stdout_bytes"], 5000)
            self.assertEqual(redacted["exit_code"], 0)
            self.assertEqual(redacted["cached_output_path"], str(cached_path))

    def test_cached_nested_result_is_reduced_to_cache_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = AppConfig(
                {
                    "agent": {
                        "filesystem": {"shared_root": temp_dir},
                        "tool_result_cache": {"enabled": True, "root": ".cache/tool-results", "min_bytes": 10},
                    }
                }
            )
            cache = ToolResultCache(config)
            result = {"items": [{"body": "x" * 1000}], "count": 1}

            cached = cache.cache_result(3, "project_status", result)
            redacted = cache.redact_result("project_status", cached)

            self.assertEqual(redacted["count"], 1)
            self.assertTrue(redacted["items_omitted"])
            self.assertIn("cached_output_path", redacted)

    def test_email_read_is_replayable_and_body_is_redacted_without_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = AppConfig(
                {
                    "agent": {
                        "filesystem": {"shared_root": temp_dir},
                        "tool_result_cache": {"enabled": True, "root": ".cache/tool-results", "min_bytes": 10},
                    }
                }
            )
            cache = ToolResultCache(config)
            result = {"email": {"id": 42, "subject": "Hello", "body_text": "sensitive body"}, "processed_artifacts": []}

            cached = cache.cache_result(3, "email_read", result)
            redacted = cache.redact_result("email_read", cached)

            self.assertNotIn("cached_output_path", cached)
            self.assertNotIn("sensitive body", redacted["email"]["body_text"])
            self.assertTrue(redacted["email"]["body_text_omitted"])
            self.assertEqual(redacted["email"]["body_text_bytes"], len("sensitive body"))
            self.assertEqual(redacted["email"]["body_recall"], "call email_read with email_id 42")


if __name__ == "__main__":
    unittest.main()
