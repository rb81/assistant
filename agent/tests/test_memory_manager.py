import unittest
import sys
import types

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

from assistant_agent.memory_manager import normalize_memory_candidate


class MemoryCandidateFilterTest(unittest.TestCase):
    def test_rejects_contact_category_even_when_important(self) -> None:
        normalized, reason = normalize_memory_candidate(
            {
                "content": "Store Jane Doe as the finance contact.",
                "kind": "contact",
                "importance": 5,
                "confidence": 1.0,
                "explicit_user_requested": True,
            }
        )

        self.assertIsNone(normalized)
        self.assertEqual(reason, "invalid_kind:contact")

    def test_rejects_low_importance_noise(self) -> None:
        normalized, reason = normalize_memory_candidate(
            {
                "content": "The agent sent a routine status email.",
                "kind": "project_context",
                "importance": 2,
                "confidence": 0.9,
            }
        )

        self.assertIsNone(normalized)
        self.assertEqual(reason, "low_importance:2")

    def test_accepts_high_signal_decision_with_metadata(self) -> None:
        normalized, reason = normalize_memory_candidate(
            {
                "content": "On 2026-06-07, User decided memory curation should prioritize dashboard review before merge automation.",
                "tags": ["Memory", "Decision", "Memory"],
                "scope": "Global",
                "kind": "decision",
                "importance": 5,
                "confidence": 0.95,
                "why_future_relevant": "Guides the next memory-management implementation sequence.",
                "evidence": "The user asked to proceed with dashboard UI first.",
            }
        )

        self.assertEqual(reason, "")
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["kind"], "decision")
        self.assertEqual(normalized["scope"], "global")
        self.assertEqual(normalized["tags"], ["memory", "decision"])
        self.assertEqual(normalized["importance"], 5)
        self.assertEqual(normalized["confidence"], 0.95)
        self.assertFalse(normalized["explicit_user_requested"])

    def test_explicit_user_memory_can_bypass_importance_threshold(self) -> None:
        normalized, reason = normalize_memory_candidate(
            {
                "content": "User prefers memory-management changes to be explained before implementation.",
                "kind": "preference",
                "importance": 3,
                "confidence": 0.9,
                "explicit_user_requested": True,
            }
        )

        self.assertEqual(reason, "")
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["importance"], 3)
        self.assertTrue(normalized["explicit_user_requested"])


if __name__ == "__main__":
    unittest.main()
