import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .secret_store import read_secret
from .transport_security import open_https_request


EXA_REST = "https://api.exa.ai/search"
EXA_MCP = "https://mcp.exa.ai/mcp?tools=web_search_advanced_exa"
EXA_SECRET = "AGENT_SEARCH_EXA_API_KEY"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
EXCERPT_LIMITS = {"low": 1000, "medium": 2000, "high": 4000}


class SearchUnavailable(RuntimeError):
    """The configured search backend could not serve the query."""


class _ExaQuotaExceeded(SearchUnavailable):
    pass


def _request(url, payload, timeout, *, key=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "Assistant-Web-Search/1.0"}
    if key:
        headers["x-api-key"] = key
    if url == EXA_MCP:
        headers["Accept"] = "application/json, text/event-stream"
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with open_https_request(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise SearchUnavailable("Exa returned an oversized response")
            text = body.decode("utf-8")
            if response.headers.get_content_type() == "text/event-stream":
                for event in text.replace("\r\n", "\n").split("\n\n"):
                    data = "\n".join(line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:"))
                    if data:
                        message = json.loads(data)
                        if isinstance(message, dict) and message.get("id") == payload.get("id"):
                            return message
                raise SearchUnavailable("Exa MCP did not return a search response")
            return json.loads(text)
    except urllib.error.HTTPError as exc:
        exc.close()
        if key and exc.code in (402, 429):
            raise _ExaQuotaExceeded("Exa API rate limit or budget reached") from exc
        raise SearchUnavailable("Exa search failed (HTTP %s)" % exc.code) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SearchUnavailable("Exa search connection or response failed") from exc


def _keyless_search(arguments, timeout, excerpt_limit):
    message = _request(EXA_MCP, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "web_search_advanced_exa", "arguments": {
            **arguments, "enableHighlights": True, "highlightsMaxCharacters": excerpt_limit,
        }},
    }, timeout)
    result = message.get("result") if isinstance(message, dict) and message.get("id") == 1 else None
    if not isinstance(result, dict) or result.get("isError"):
        raise SearchUnavailable("Exa keyless search could not serve the request")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                try:
                    return json.loads(item["text"])
                except json.JSONDecodeError as exc:
                    raise SearchUnavailable("Exa MCP returned an invalid search response") from exc
    raise SearchUnavailable("Exa MCP did not return search results")


def search(query, *, limit, context, allowed_domains, excluded_domains, timeout):
    excerpt_limit = EXCERPT_LIMITS.get(context, EXCERPT_LIMITS["medium"])
    args = {"query": query, "numResults": min(limit, 100), "type": "lite"}
    if allowed_domains:
        args["includeDomains"] = allowed_domains
    if excluded_domains:
        args["excludeDomains"] = excluded_domains
    try:
        key = read_secret(EXA_SECRET)
    except RuntimeError as exc:
        raise SearchUnavailable("Exa search credential is unreadable") from exc
    mode = "mcp"
    if key:
        try:
            payload = _request(EXA_REST, {
                **args, "contents": {"highlights": {"maxCharacters": excerpt_limit}},
            }, timeout, key=key)
            mode = "api"
        except _ExaQuotaExceeded:
            payload = _keyless_search(args, timeout, excerpt_limit)
    else:
        payload = _keyless_search(args, timeout, excerpt_limit)
    raw_results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(raw_results, list):
        raise SearchUnavailable("Exa returned an invalid results list")
    results = []
    seen = set()
    for item in raw_results:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        url = item["url"]
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or url in seen:
            continue
        seen.add(url)
        highlights = item.get("highlights")
        excerpt = "\n".join(part for part in highlights if isinstance(part, str)) if isinstance(highlights, list) else ""
        results.append({
            "url": url, "title": item.get("title"), "published_date": item.get("publishedDate"),
            "excerpt": excerpt[:excerpt_limit],
        })
        if len(results) >= args["numResults"]:
            break
    return mode, results
