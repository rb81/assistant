# SANDBOX_CAPABILITIES

This file describes common tools and Python libraries available in the sandbox command environment.

Use `command_execute` for scripts and shell commands that should run in the sandbox. The sandbox shared workspace is the only supported working area for file reads/writes.

## Installed command-line tools

- `python` / `python3`
- `curl`
- `wget`
- `git`
- `jq`
- `file`
- `unzip`
- `dig` / `nslookup` from `dnsutils`
- `ping`

## Installed Python web/research libraries

- `requests`
- `httpx`
- `beautifulsoup4` (`bs4`)
- `lxml`
- `html5lib`
- `trafilatura`
- `readability-lxml`
- `markdownify`
- `python-dateutil`

## Web request guidance

Many websites reject bare command-line clients or bare Python requests. When checking a website, prefer browser-like request headers and follow redirects.

Example with `curl`:

```sh
curl -L --compressed \
  -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36" \
  -H "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8" \
  -H "Accept-Language: en-US,en;q=0.9" \
  https://example.com/
```

Example with Python `requests` and BeautifulSoup:

```python
import requests
from bs4 import BeautifulSoup

headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

response = requests.get("https://example.com/", headers=headers, timeout=20)
response.raise_for_status()
soup = BeautifulSoup(response.text, "lxml")
print(soup.get_text("\n", strip=True)[:4000])
```

Example main-content extraction with `trafilatura`:

```python
import requests
import trafilatura

headers = {"User-Agent": "Mozilla/5.0"}
html = requests.get("https://example.com/", headers=headers, timeout=20).text
text = trafilatura.extract(html, url="https://example.com/")
print(text or "No main content extracted")
```

## Troubleshooting

- If `curl`, `wget`, or Python HTTP requests fail, inspect both stdout and stderr from `command_execute`.
- HTTP 403/406 can mean the site is blocking non-browser clients; retry with browser-like headers.
- DNS issues can be checked with `dig example.com` or `nslookup example.com`.
- TLS/certificate issues may indicate a site-specific certificate problem or missing CA support.
- Some websites require JavaScript rendering. The default sandbox does not include a browser automation stack such as Playwright or Selenium.

For substantial research tasks, prefer `deep_research_request` because it uses the configured web search provider. Use `command_execute` for direct URL checks, custom scripts, downloads, and focused diagnostics.
