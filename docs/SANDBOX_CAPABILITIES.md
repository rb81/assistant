# Sandbox capabilities

This page describes the default image built from `src/sandbox/Dockerfile` and
`src/sandbox/requirements.txt`. Custom run images may differ.

Use `command_execute` for scripts and commands. Before executing a finished script
version, finish its code and helpers, call `script_approval_request` with exact
argv/workdir, purpose and complete `urls` host list for combined approval.
Execute that identical invocation only after approval. Changes require a new approval.

For iterative development, first call `development_session_request` with the
objective, permitted data/files, external effects, intended hosts and time/run
limits. After the admin approves that brief, call `script_development_review`
for each changed version or host set. The independent supervisor may approve
implementation changes only within the brief. Approved unchanged versions may
run repeatedly within the session's limits. Submit `script_approval_request`
when finished; this stops further development execution and asks for final
combined approval with supervisor comments. Never bypass rejection, expiry,
budgets or blocks. Explicitly enabled Script YOLO skips approvals and supervision,
but not sandbox restrictions or administrative blocks.

Keep task files in the shared
workspace (`/data/share` by default); only workspace changes persist between
commands. Pass an argument array, not a shell command string:

```json
{"command":["python","-c","print('hello')"],"workdir":".","timeout_seconds":30}
```

For shell syntax such as pipes, explicitly invoke `["sh","-c","..."]`.
Inline code also requires run approval. Approved code executes from a read-only
snapshot; use the workdir or explicit workspace paths for data and outputs,
not `__file__`. Once grants cover one execution, including failures, and are not
restored after an uncertain response. Do not retry an uncertain script dispatch.

The canonical harness documentation is mounted read-only at
`/data/share/.assistant/docs` in the agent, broker, and each command container.
Start with `.assistant/docs/README.md` and read relevant pages on demand.
The mount does not grant access to host source code, settings, credentials,
application networks, or service administration.

Do not search environment variables or secret files for API keys. Use dedicated
tools for authenticated integrations: `llm_service` handles model presets and
chat-compatible audio/image generation; `text_to_speech` synthesizes audio
with the admin-configured speech model and voice without exposing credentials.
Delivery remains in channel tools such as `telegram_send`. If a capability
is missing or fails, report the limitation rather than recreating it in scripts.

In default container mode, each command uses a new container. Packages installed
into its writable layer do not survive the command; install and use them in the
same call, or use a workspace-local environment. Default limits are 2 CPUs,
2 GiB memory, 512 processes and a 300-second timeout. See
[sandbox execution](sandbox.md) for configuration and isolation limits.

## Installed command-line tools

- `python` / `python3`
- `curl`
- `wget`
- `git`
- `jq`
- `file`
- `unzip`
- `dig` / `nslookup` from `dnsutils`
- `ping` (availability depends on container/kernel ICMP permissions; `NET_RAW`
  is dropped by default)

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

## Network access

Commands reach the internet only over HTTPS through an approval proxy.
Hosts must be authorized for that specific script version, including package
registries. There are no global host allows. Request the complete host list
with combined final approval or supervisor development review.
`network_access_request` is a compatibility entrypoint to these same flows.
Access covers every HTTPS path on that host, not just the supplied URL;
subdomains are not implicitly included. Redirects to another host need that
host approved too. The administrator chooses one execution or always for this
unchanged script version and host set. Check the `network` field of `command_execute`
results: blocked hosts are listed with reasons. Never use other hosts, mirrors,
IP addresses, DNS tricks or tools to get around a refusal, and do not request
hosts the administrator blocked. Plain HTTP and non-443 ports are not available.

## Web request guidance

Some sites reject command-line clients. Follow redirects for public URLs, use a
descriptive User-Agent, and inspect HTTP status codes. Browser-like headers do
not bypass authentication, JavaScript requirements or anti-bot controls.

Example with `curl`:

```sh
curl --fail-with-body --location --compressed --max-time 20 \
  --user-agent "Assistant URL check/1.0" \
  https://example.com/
```

Example with Python `requests` and BeautifulSoup:

```python
import requests
from bs4 import BeautifulSoup

headers = {"User-Agent": "Assistant URL check/1.0"}

response = requests.get("https://example.com/", headers=headers, timeout=20)
response.raise_for_status()
soup = BeautifulSoup(response.text, "lxml")
print(soup.get_text("\n", strip=True)[:4000])
```

Example main-content extraction with `trafilatura`:

```python
import requests
import trafilatura

response = requests.get(
    "https://example.com/",
    headers={"User-Agent": "Assistant URL check/1.0"},
    timeout=20,
)
response.raise_for_status()
text = trafilatura.extract(response.text, url=response.url)
print(text or "No main content extracted")
```

## Troubleshooting

- If `curl`, `wget`, or Python HTTP requests fail, inspect both stdout and stderr from `command_execute`.
- HTTP 403/406 can indicate access restrictions or client blocking; do not assume
  changing headers will resolve it.
- Direct DNS is intentionally unavailable in proxy mode; `dig`/`nslookup`
  failures are not evidence that the remote site is down. Check the command's
  network summary and HTTPS response instead.
- TLS/certificate issues may indicate a site-specific certificate problem or missing CA support.
- Some websites require JavaScript rendering. The default sandbox does not include a browser automation stack such as Playwright or Selenium.

For substantial research tasks, prefer `deep_research_request` because it uses the configured web search provider. Use `command_execute` for direct URL checks, custom scripts, downloads, and focused diagnostics.
