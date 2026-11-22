import unittest
from unittest.mock import patch

from assistant_agent import settings_schema as schema
from assistant_agent.config import ENV_OVERRIDES


class SettingsSchemaTests(unittest.TestCase):
    def test_every_yaml_leaf_is_overridable_with_unique_env_names(self):
        catalog = schema.build_catalog()
        mapped = {path for _, path, _ in ENV_OVERRIDES}
        self.assertEqual({item["path"] for item in catalog} - mapped, set())
        names = [name for name, _, _ in ENV_OVERRIDES]
        duplicated = {name for name in names if names.count(name) > 1}
        self.assertEqual(duplicated, set())

    def test_orphan_sidecar_entries_are_ignored_with_info_notice(self):
        real = schema._load

        def load(name):
            data = real(name)
            if name == "agent.schema.yaml":
                data = {**data, "settings": {**data.get("settings", {}), "agent.no_such_setting": {"type": "int"}}}
            return data
        with patch.object(schema, "_load", load), self.assertLogs(schema.LOGGER, "INFO") as logs:
            catalog = schema.build_catalog()
        self.assertIn("agent.no_such_setting", " ".join(logs.output))
        self.assertNotIn("agent.no_such_setting", {item["path"] for item in catalog})

    def test_blank_defaults_have_declared_types_in_sidecar(self):
        types = schema.sidecar_types(schema._load("agent.schema.yaml"))
        for item in schema.build_catalog():
            if item["default"] is None and item["env"] not in {"AGENT_IDENTITY_NAME", "AGENT_IDENTITY_EMAIL"}:
                self.assertIn(item["type"], (str, int, float), item["path"])
        self.assertIs(types["agent.embeddings.dimensions"], int)

    def test_model_subtrees_include_new_leaves_and_reject_overlapping_sections(self):
        real = schema._load

        def load(name):
            data = real(name)
            if name == "agent.yaml":
                data["agent"]["llm"]["new_sampling_setting"] = 0.5
            return data
        with patch.object(schema, "_load", load):
            fields = {item["env"]: item for item in schema.build_catalog()}
        self.assertEqual(fields["AGENT_LLM_NEW_SAMPLING_SETTING"]["metadata"]["model_section"], "task")
        sidecar = real("agent.schema.yaml")
        sidecar["model_sections"].append({"id": "duplicate", "paths": ["agent.llm.model"]})
        with patch.object(schema, "_load", side_effect=lambda name: sidecar if name == "agent.schema.yaml" else real(name)):
            with self.assertRaisesRegex(ValueError, "multiple overview sections"):
                schema.build_catalog()


if __name__ == "__main__":
    unittest.main()
