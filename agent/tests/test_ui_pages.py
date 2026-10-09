import sys
import types
import unittest
from unittest.mock import patch
from typing import Optional

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda handle: {}))

from assistant_agent.config import AppConfig
from assistant_agent.admin_commands import HELP


class EmailAdministrationSurfaceTest(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {"DATABASE_URL": "postgresql://stub@localhost/stub?sslmode=verify-full&sslrootcert=%2Fdev%2Fnull"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_help_covers_core_admin_workflows(self):
        for command in ("logs", "jobs", "memory", "reminders", "review"):
            self.assertIn("/admin " + command, HELP)

    def test_browser_and_retired_feature_routes_are_absent(self):
        from assistant_agent.api import app
        routes = [getattr(route, "path", "") for route in app.routes]
        for route in routes:
            self.assertNotIn(route, ("/", "/admin", "/workspace", "/assets"))
            self.assertFalse(route.startswith(("/api/projects", "/api/notes", "/api/entities", "/api/workspace")))

    def test_api_requires_tls_and_authentication(self):
        from fastapi.testclient import TestClient
        import assistant_agent.api as api
        with patch.object(api, "config", AppConfig({"agent": {"api": {"auth_token": "test-token"}}})):
            self.assertEqual(TestClient(api.app).get("/health").status_code, 426)
            self.assertEqual(TestClient(api.app, base_url="https://testserver").get("/health").status_code, 401)
            self.assertEqual(TestClient(api.app, base_url="https://testserver", headers={"Authorization": "Bearer wrong"}).get("/health").status_code, 401)
            self.assertEqual(TestClient(api.app, base_url="https://testserver", headers={"Authorization": "Bearer test-token"}).get("/health").status_code, 200)
            malformed = TestClient(api.app, base_url="https://testserver").get("/health", headers=[(b"authorization", b"Bearer \xff")])
            self.assertEqual(malformed.status_code, 401)


if __name__ == "__main__":
    unittest.main()
