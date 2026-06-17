from html import escape as html_escape
from pathlib import Path

from .config import AppConfig, app_display_name


UI_ROOT = Path(__file__).with_name("ui")

API_DOCS_LINK = """
        <a class="topbar-icon-link" href="/docs" title="API Docs" aria-label="API Docs">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M0 1.75C0 .784.784 0 1.75 0h12.5C15.216 0 16 .784 16 1.75v12.5A1.75 1.75 0 0 1 14.25 16H1.75A1.75 1.75 0 0 1 0 14.25Zm1.75-.25a.25.25 0 0 0-.25.25v12.5c0 .138.112.25.25.25h12.5a.25.25 0 0 0 .25-.25V1.75a.25.25 0 0 0-.25-.25Z"/><path d="M3.5 6.75A.75.75 0 0 1 4.25 6h7a.75.75 0 0 1 0 1.5h-7a.75.75 0 0 1-.75-.75Zm0 2.5a.75.75 0 0 1 .75-.75h4a.75.75 0 0 1 0 1.5h-4a.75.75 0 0 1-.75-.75Z"/></svg>
        </a>"""


def api_docs_link(config: AppConfig) -> str:
    docs_available = config.get_bool("agent.api.docs_enabled", False) and config.get_bool(
        "agent.api.openapi_enabled", False
    )
    return API_DOCS_LINK if docs_available else ""


def render_ui_page(filename: str, config: AppConfig, ui_root: Path = UI_ROOT) -> str:
    page = ui_root / filename
    if not page.is_file():
        raise FileNotFoundError(str(page))
    return (
        page.read_text(encoding="utf-8")
        .replace("__APP_TITLE__", html_escape(app_display_name(config)))
        .replace("__API_DOCS_LINK__", api_docs_link(config))
    )
