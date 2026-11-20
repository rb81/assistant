import io
import json
import os
import shutil
import subprocess
import urllib.error
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

from assistant_agent import development_supervisor, exa_search, main, web_pages
from assistant_agent.config import AppConfig
from assistant_agent.deep_research import research_tools
from assistant_agent.llm_client import LlmClient
from assistant_agent.tool_result_cache import ToolResultCache
from assistant_agent.tools import SecurityApprovalRequired, ToolError, ToolRuntime, available_tools


def settings(tmp_path, *, search=None, fetch=None):
    return AppConfig({"agent": {
        "filesystem": {"shared_root": str(tmp_path), "require_mount": False},
        "search": search or {},
        "web_fetch": fetch or {},
    }})


def http_response(value, *, sse=False):
    json_text = json.dumps(value)
    response = io.BytesIO((f"event: message\ndata: {json_text}\n\n" if sse else json_text).encode())
    response.headers = Message()
    response.headers["Content-Type"] = "text/event-stream" if sse else "application/json"
    return response


@pytest.mark.parametrize("quota", [402, 429])
def test_exa_limit_retries_one_query_keyless_and_keeps_secret(monkeypatch, quota):
    monkeypatch.setenv(exa_search.EXA_SECRET, "stored-key")
    seen = []
    result = {"results": [{"url": "https://example.org/reference", "title": "Reference", "highlights": ["An excerpt"]}]}

    def transport(request, **_):
        body = json.loads(request.data)
        seen.append((request.full_url, request.get_header("X-api-key"), body))
        if len(seen) == 1:
            raise urllib.error.HTTPError(request.full_url, quota, "limited", {}, io.BytesIO())
        if len(seen) == 2:
            return http_response({"jsonrpc": "2.0", "id": 1, "result": {
                "content": [{"type": "text", "text": json.dumps(result)}],
            }}, sse=True)
        return http_response(result)

    monkeypatch.setattr(exa_search, "open_https_request", transport)
    kwargs = {"limit": 1, "context": "low", "allowed_domains": ["example.org"], "excluded_domains": [], "timeout": 1}
    mode, rows = exa_search.search("query", **kwargs)
    assert mode == "mcp" and rows[0]["excerpt"] == "An excerpt"
    assert exa_search.search("another query", **kwargs)[0] == "api"
    assert [entry[1] for entry in seen] == ["stored-key", None, "stored-key"]
    assert seen[0][2]["query"] == seen[1][2]["params"]["arguments"]["query"]
    assert seen[1][2]["params"]["arguments"]["includeDomains"] == ["example.org"]


def test_search_fallback_only_for_outage_not_empty_results(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LLM_OPENROUTER_API_KEY", "test-key")
    config = settings(tmp_path, search={"primary_provider": "exa", "exa_enabled": True})
    runtime = ToolRuntime(Mock(), config, {"id": 1})
    with patch.object(runtime, "_openrouter_web_search", return_value={"search_provider": "openrouter"}) as hosted:
        with patch("assistant_agent.tools.exa_search", side_effect=exa_search.SearchUnavailable("offline")):
            assert runtime.web_search("question")["search_provider"] == "openrouter"
        with patch("assistant_agent.tools.exa_search", return_value=("mcp", [])):
            assert runtime.web_search("question")["results"] == []
        hosted.assert_called_once()


def test_only_explicit_openrouter_search_adds_the_hosted_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LLM_OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_LLM_OPENAI_API_KEY", "task-key")
    config = settings(tmp_path, search={"engine": "native", "model": "perplexity/sonar"})
    config.values["agent"]["llm"] = {"base_url": "https://models.example.org/v1"}
    job = {"id": 1}
    requests = []
    endpoints = []

    def transport(request, **_):
        requests.append(json.loads(request.data))
        endpoints.append((request.full_url, request.get_header("Authorization")))
        return http_response({"choices": [{"message": {"content": "Search answer"}}]})

    monkeypatch.setattr("assistant_agent.llm_client.open_https_request", transport)
    messages = [{"role": "user", "content": "A current question"}]
    client = LlmClient(config)
    client.chat(messages, available_tools(config, job, {"web_search"}))
    client.chat(messages, research_tools(config))
    ToolRuntime(Mock(), config, job).web_search("question", max_results=3)
    assert endpoints[:2] == [("https://models.example.org/v1/chat/completions", "Bearer task-key")] * 2
    assert endpoints[2] == ("https://openrouter.ai/api/v1/chat/completions", "Bearer test-key")
    for payload in requests[:2]:
        assert "plugins" not in payload
        assert any(tool.get("function", {}).get("name") == "web_search" for tool in payload["tools"])
        assert all(tool["type"] != "openrouter:web_search" for tool in payload["tools"])
    assert requests[2]["tools"] == [{"type": "openrouter:web_search", "parameters": {
        "engine": "native", "max_results": 3, "max_total_results": 15,
    }}]


def test_scraper_auth_rendered_markdown_and_redirect(tmp_path, monkeypatch):
    monkeypatch.setenv(web_pages.SCRAPER_SECRET, "worker-token")
    config = settings(tmp_path, fetch={"enabled": True, "base_url": "https://scraper.example.test"})

    def transport(request, **_):
        assert request.full_url == "https://scraper.example.test/crawl"
        assert request.get_header("Authorization") == "Bearer worker-token"
        assert json.loads(request.data)["urls"] == ["https://example.org/start"]
        return http_response({"results": [{
            "success": True, "status_code": 302, "redirected_status_code": 200,
            "redirected_url": "https://example.org/final", "metadata": {"title": "Title"},
            "markdown": {"fit_markdown": "# Rendered", "raw_markdown": "Navigation, footer"},
        }]})

    monkeypatch.setattr(web_pages, "open_https_request", transport)
    assert web_pages.scrape(config, "https://example.org/start") == ("https://example.org/final", "Title", "# Rendered")
    for url in ("file:///tmp/page", "http://127.0.0.1", "http://[::1]", "https://user@example.org", "https://example.org:8080"):
        with pytest.raises(web_pages.WebFetchError):
            web_pages.public_url(url)


def test_flagged_page_only_released_after_exact_approval_and_paged_from_cache(tmp_path, monkeypatch):
    url = "https://example.org/page"
    content = "# Page\n" + ("text " * 90)
    db = Mock()
    config = settings(tmp_path, fetch={"enabled": True, "inline_chars": 250})
    job = {"id": 4, "metadata": {}}
    scrape = Mock(return_value=(url, "Title", content))
    review = Mock(return_value={"decision": "review", "summary": "A page", "concern": "Possible malicious instruction"})
    monkeypatch.setattr(web_pages, "scrape", scrape)
    monkeypatch.setattr(development_supervisor, "review_web_page", review)
    cache = {}

    def fetch(sql, params):
        if sql.startswith("UPDATE jobs"):
            assert params == (4,)
            return {"id": 4}
        assert params[:2] == (4, url)
        if sql.startswith("INSERT"):
            _, _, final, title, body, fingerprint, summary = params
            cache.update(final_url=final, title=title, markdown=body, review_fingerprint=fingerprint,
                         review_summary=summary, fetched_at=datetime.now(timezone.utc))
        return cache or None

    db.fetch_one.side_effect = fetch
    runtime = ToolRuntime(db, config, job)
    with pytest.raises(SecurityApprovalRequired, match="Possible malicious instruction") as exc:
        runtime.web_fetch(url)
    with pytest.raises(SecurityApprovalRequired):
        runtime.web_fetch(url, offset=250)
    with pytest.raises(SecurityApprovalRequired) as search_exc:
        runtime.web_fetch(url, query="text")
    assert search_exc.value.fingerprint == exc.value.fingerprint
    job["metadata"]["security_approved_actions"] = [exc.value.fingerprint]
    job["metadata"]["approved_web_page"] = {"arguments": {"url": url}, "tool_call_id": "initial"}
    messages = []
    runtime.replay_approved_web_page(messages)
    result = json.loads(messages[-1]["content"])
    assert messages[-1]["role"] == "tool"
    assert result["markdown"] == content[:250]
    assert result["content_warning"] == web_pages.CAUTION
    assert "approved_web_page" not in job["metadata"]
    replay_count = len(messages)
    runtime.replay_approved_web_page(messages)
    assert len(messages) == replay_count
    assert runtime.web_fetch(**result["next_read"]["arguments"])["markdown"] == content[250:]
    assert "markdown" not in ToolResultCache(config).redact_result("web_fetch", result)
    scrape.assert_called_once()
    review.assert_called_once_with(config, url, "Title", content)


def test_cleanup_tool_caches_cleans_files_and_expired_pages(tmp_path):
    db = Mock()
    config = settings(tmp_path)
    with patch("assistant_agent.main.ToolResultCache") as cache, \
         patch("assistant_agent.web_pages.cleanup_expired") as cleanup_pages:
        cache.return_value.cleanup_expired.return_value = 3
        cache.return_value.retention_days.return_value = 7
        assert main.cleanup_tool_caches(db, config) == 3
    cache.return_value.cleanup_expired.assert_called_once_with()
    cleanup_pages.assert_called_once_with(db, 7)


def test_approved_web_page_replay_requires_claiming_the_persistent_marker(tmp_path):
    db = Mock()
    db.fetch_one.return_value = None
    job = {"id": 4, "metadata": {"approved_web_page": {
        "arguments": {"url": "https://example.org/page"}, "tool_call_id": "initial",
    }}}
    runtime = ToolRuntime(db, settings(tmp_path), job)
    messages = []

    runtime.replay_approved_web_page(messages)

    assert messages == []
    assert "approved_web_page" not in job["metadata"]
    assert "metadata ? 'approved_web_page'" in db.fetch_one.call_args.args[0]


def test_managed_reader_uses_fixed_private_endpoint_and_its_own_token(tmp_path, monkeypatch):
    url = "https://example.org/page"
    config = settings(tmp_path, fetch={"enabled": True})
    monkeypatch.setenv(web_pages.MANAGED_SECRET, "managed-test-token")
    monkeypatch.setenv(web_pages.SCRAPER_SECRET, "external-test-token")

    def request(request, *, timeout):
        assert request.full_url == "http://web-reader-gateway:8080/crawl"
        assert request.get_header("Authorization") == "Bearer managed-test-token"
        assert json.loads(request.data) == {"url": url, "timeout_seconds": 35}
        assert timeout > 35
        return http_response({"results": [{"success": True, "url": url, "markdown": "Rendered"}]})

    transport = Mock(side_effect=request)
    monkeypatch.setattr(web_pages, "open_internal_http_request", transport)
    assert not web_pages.configuration_errors(config)
    assert web_pages.scrape(config, url) == (url, "", "Rendered")
    with pytest.raises(web_pages.WebFetchError, match="requires HTTPS"):
        web_pages.scrape(config, "http://example.org/page")
    transport.assert_called_once()

    config.values["agent"]["web_fetch"]["base_url"] = "http://web-reader-gateway:8080"
    assert web_pages.configuration_errors(config)
    with pytest.raises(web_pages.WebFetchError, match="not configured correctly"):
        web_pages.scrape(config, url)
    transport.assert_called_once()


def test_page_search_jumps_to_ranked_passages_and_reuses_snapshot(tmp_path, monkeypatch):
    url = "https://example.org/page"
    content = ("Renewal is possible. " + "filler " * 95 + "\n"
               "The renewal deadline is 31 October.\n" + " ".join(["filler"] * 130))
    config = settings(tmp_path, fetch={"enabled": True, "inline_chars": 250})
    db = Mock()
    page = {"markdown": content, "final_url": url, "title": "Title", "review_fingerprint": None,
            "fetched_at": datetime.now(timezone.utc)}
    db.fetch_one.side_effect = [None, page, page, page]
    scrape = Mock(return_value=(url, "Title", content))
    review = Mock(return_value={"decision": "allow"})
    monkeypatch.setattr(web_pages, "scrape", scrape)
    monkeypatch.setattr(development_supervisor, "review_web_page", review)
    runtime = ToolRuntime(db, config, {"id": 3})

    result = runtime.web_fetch(url, query="RENEWAL deadline")
    passages = result["passages"]
    assert "31 October" in passages[0]["markdown"]
    assert 1 < len(passages) <= 3
    assert sum(len(p["markdown"]) for p in passages) <= 250
    assert result["content_warning"] == web_pages.CAUTION
    assert "markdown" not in result
    assert "passages" not in ToolResultCache(config).redact_result("web_fetch", result)
    read = passages[0]["read"]
    assert read["arguments"]["offset"] > 0
    assert runtime.web_fetch(**read["arguments"])["markdown"].startswith(passages[0]["markdown"])
    assert runtime.web_fetch(url, query="renew")["passages"] == []  # Whole words only.
    scrape.assert_called_once()
    review.assert_called_once()
    db.fetch_one.assert_called()
    assert all(call.args[1][:2] == (3, url) for call in db.fetch_one.call_args_list)


@pytest.mark.skipif(os.environ.get("ASSISTANT_TEST_MANAGED_BROWSER") != "1", reason="requires Docker and public HTTPS")
def test_managed_browser_renders_javascript_and_exits_between_requests(tmp_path):
    from assistant_agent.setup import Installation

    source = Path(__file__).resolve().parents[2]
    target = tmp_path / "src"
    for folder in ("config", "sandbox", "web_reader"):
        shutil.copytree(source / folder, target / folder)
    shutil.copyfile(source / "docker-compose.yml", target / "docker-compose.yml")
    installation = Installation(tmp_path)
    installation.values["AGENT_WEB_FETCH_ENABLED"] = "true"
    installation.save()
    installation = Installation(tmp_path)
    prefix = installation.compose_prefix()
    project = "assistant-web-reader-test-" + uuid4().hex[:8]

    def compose(*args, input=None, check=True):
        return subprocess.run(
            ["docker", *prefix, "--project-name", project, "--profile", "web-reader", *args],
            input=input, text=True, capture_output=True, check=check, timeout=600,
        )

    try:
        started = compose("up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "45",
                          "web-reader", "web-reader-gateway", check=False)
        assert not started.returncode, started.stdout + started.stderr
        probe = """
import json, sys, urllib.error, urllib.request
origin = "http://127.0.0.1:8080/crawl"
def read(url, token):
    data = json.dumps({"url": url, "timeout_seconds": 35}).encode()
    request = urllib.request.Request(origin, data=data, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)
token = sys.stdin.read()
url = "https://quotes.toscrape.com/js/"
assert read(url, "invalid")[0] == 401
status, body = read(url, token)
assert status == 200 and body["results"][0]["success"], (status, body)
assert "by Albert Einstein" in body["results"][0]["markdown"]
"""
        result = compose("exec", "-T", "web-reader-gateway", "python", "-c", probe,
                         input=installation.secret_values[web_pages.MANAGED_SECRET], check=False)
        assert not result.returncode, result.stdout + result.stderr + compose("logs", "--tail", "20").stdout
        idle = """
import socket
from pathlib import Path
assert not any("Uid:\\t10001\\t" in p.read_text() for p in Path("/proc").glob("[0-9]*/status"))
assert not list(Path("/tmp").iterdir())
try:
    with socket.create_connection(("1.1.1.1", 443), timeout=2):
        raise AssertionError("Browser container can bypass the HTTPS gateway")
except OSError:
    pass
"""
        compose("exec", "-T", "web-reader", "python", "-c", idle)

        installation.values["AGENT_WEB_FETCH_ENABLED"] = "false"
        installation.save()
        inactive = Installation(tmp_path)
        with patch.object(inactive, "compose", side_effect=lambda args: compose(*args).stdout):
            inactive.stop_inactive_web_reader()
        assert not compose("ps", "--status", "running", "--services").stdout.strip()
    finally:
        compose("down", "--rmi", "local")


@pytest.mark.parametrize(("kwargs", "error"), [
    ({"query": "?! "}, "at least one word"),
    ({"query": "q" * 201}, "at most 200"),
    ({"query": "word", "offset": 1}, "without an offset"),
])
def test_invalid_page_search_does_not_access_the_cache_or_worker(tmp_path, kwargs, error):
    db = Mock()
    config = settings(tmp_path, fetch={"enabled": True})
    with pytest.raises(ToolError, match=error):
        ToolRuntime(db, config, {"id": 3}).web_fetch("https://example.org/page", **kwargs)
    db.fetch_one.assert_not_called()


def test_supervisor_failure_does_not_cache_or_release_page(tmp_path, monkeypatch):
    url = "https://example.org"
    config = settings(tmp_path, fetch={"enabled": True})
    db = Mock()
    db.fetch_one.return_value = None
    monkeypatch.setattr(web_pages, "scrape", Mock(return_value=(url, "Title", "A page")))
    monkeypatch.setattr(development_supervisor, "review_web_page", Mock(side_effect=development_supervisor.SupervisorError()))
    with pytest.raises(ToolError, match="no content was released"):
        ToolRuntime(db, config, {"id": 3}).web_fetch(url)
    db.fetch_one.assert_called_once()
