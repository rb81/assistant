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


if __name__ == "__main__":
    unittest.main()
