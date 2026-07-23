import sys
import types
import unittest
from typing import Optional

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda handle: {}))

from assistant_agent.config import AppConfig
from assistant_agent.ui_pages import render_ui_page


class UiPageRenderTest(unittest.TestCase):
    def config(
        self,
        docs_enabled: Optional[bool] = None,
        openapi_enabled: Optional[bool] = None,
    ) -> AppConfig:
        agent: dict[str, object] = {"app": {"name": "assistant"}}
        api: dict[str, bool] = {}
        if docs_enabled is not None:
            api["docs_enabled"] = docs_enabled
        if openapi_enabled is not None:
            api["openapi_enabled"] = openapi_enabled
        if api:
            agent["api"] = api
        return AppConfig({"agent": agent})

    def test_docs_link_renders_when_enabled(self) -> None:
        html = render_ui_page("admin.html", self.config(docs_enabled=True, openapi_enabled=True))

        self.assertIn('href="/docs"', html)
        self.assertNotIn("__API_DOCS_LINK__", html)

    def test_docs_link_is_hidden_when_docs_disabled(self) -> None:
        for filename in ("admin.html", "workspace.html"):
            with self.subTest(filename=filename):
                html = render_ui_page(filename, self.config(docs_enabled=False))

                self.assertNotIn('href="/docs"', html)
                self.assertNotIn("__API_DOCS_LINK__", html)

    def test_docs_link_is_hidden_when_openapi_disabled(self) -> None:
        html = render_ui_page("admin.html", self.config(docs_enabled=True, openapi_enabled=False))

        self.assertNotIn('href="/docs"', html)

    def test_chat_page_renders_title_and_pwa_links(self) -> None:
        html = render_ui_page("chat.html", self.config())

        self.assertNotIn("__APP_TITLE__", html)
        self.assertIn('id="chat-root"', html)
        self.assertIn('rel="manifest"', html)
        self.assertIn("/assets/chat.bundle.js", html)

    def test_dashboard_renders_cost_reminders_and_projects_views(self) -> None:
        html = render_ui_page("admin.html", self.config())

        self.assertIn('id="dashboard-summary"', html)
        self.assertIn('id="reminders-view-button"', html)
        self.assertIn('id="projects-view-button"', html)
        self.assertIn('id="reminder-status-filter"', html)
        self.assertIn('id="project-status-filter"', html)

    def test_workspace_renders_upload_progress(self) -> None:
        html = render_ui_page("workspace.html", self.config())

        self.assertIn('id="upload-progress"', html)
        self.assertIn('id="upload-progress-cancel"', html)
        self.assertIn('id="upload-progress-count"', html)
        self.assertIn('id="upload-progress-fill"', html)


if __name__ == "__main__":
    unittest.main()
