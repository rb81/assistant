# Web access

The Assistant searches only when it calls `web_search`. In **Models → Web search providers**,
enable OpenRouter, direct Exa, or both. The preferred provider goes first.
If it cannot serve the request, the other enabled, available provider is tried
without interrupting the task. An empty successful search is not an outage.
The Web search switch still disables the tool for all providers.

Existing installations keep OpenRouter enabled with their current engine
and limits; there is no configuration migration. OpenRouter's engine setting
only controls its hosted search tool and does not change the direct Exa backend.
Without an Exa key, direct Exa uses [Exa's free, rate-limited MCP
search](https://exa.ai/auth.md). Optionally save an Exa key to use its Search API.
If the key hits a rate or budget limit, the Assistant retries that query
through keyless MCP. It does not delete the key; the next query uses it again.
The direct Exa backend only receives search queries and search options.

## Read a known public page

The optional `web_fetch` tool reads an exact public URL as rendered,
boilerplate-filtered Markdown. It does not depend on either search provider.
### Managed browser (default)

In **Models → Web page reader**, turn on the reader, save, and apply. Leave the
external origin blank. Assistant provisions the browser and its private
credential; you do not need an origin, reverse proxy, or API key. The first
apply downloads and builds the browser image and can take several minutes.
Web search does not need the browser and works with it disabled.

The managed reader requires [Docker Engine 28 or newer](https://docs.docker.com/engine/release-notes/28/)
for isolated bridge networking. On an older engine, upgrade Docker or use an
external reader.

The managed reader accepts public **HTTPS** hostnames on the standard port. Its
browser has no direct route to the internet or the application network:
all browser connections go through the public HTTPS egress proxy, including
redirects and subrequests. Neither service publishes a host port. The reader
does not mount the workspace, database credentials, or Docker socket.

An uncached read starts a fresh Chromium subprocess, converts its rendered HTML
to Markdown with Trafilatura, and deletes its temporary browser profile. No
Chromium process stays running between reads;
only the lightweight HTTP listener and gateway stay up. The reader runs one
scrape at a time and reports busy if another request arrives. Disable the
reader and apply to stop both containers. Switching to an external origin
also stops them.

### External Crawl4AI (optional)

To supply your own browser server, configure **External Crawl4AI HTTPS origin**
and **External Crawl4AI API token** under **Advanced → External reader & limits**
and keep the reader enabled. Use an origin such as `https://crawler.example.com`
without a path. Assistant sends a bearer-authenticated `POST /crawl` and
verifies the TLS certificate against the trust store of the Assistant workers.
Existing nonempty origins continue to select the external reader after upgrade.
Clear the origin and apply to switch back to the managed browser.

Deploy [Crawl4AI](https://docs.crawl4ai.com/core/self-hosting/) with its v0.9
Docker API. On an isolated crawler host, set `CRAWL4AI_API_TOKEN` securely,
bind the container to loopback, and put a private TLS reverse proxy in front:

```bash
docker run -d --name assistant-web-reader --restart unless-stopped \
  --shm-size=1g -p 127.0.0.1:11235:11235 -e CRAWL4AI_API_TOKEN \
  unclecode/crawl4ai:0.9.0
```

Allow only the Assistant workers to reach `POST /crawl` through the proxy.
Block private networks and cloud metadata at the network layer and in Crawl4AI
for navigations, redirects, and subrequests. Don't expose the raw port or
mount Assistant credentials, its workspace, or the Docker socket. Leave deep
crawling disabled, and keep the pinned image current with upstream patches.

### Review and cache

The same model used for development script supervision reviews the extracted
Markdown before the task model sees it. The review sends the page to the
configured supervisor model provider even though the scraper is self-hosted.
Ordinary technical content does not require approval. If the supervisor flags
a page, the task or deep-research run pauses and the configured admin channel
receives a summary and the concern. On approval, the task gets the same cached
content, not a fresh download. Approval permits reading, not following the
page's instructions. If the supervisor cannot complete its review, no content
is released.

Pages are cached per task. The result includes a `next_read` reference for long
pages; it reads another bounded chunk from the same cache entry without
refetching. To jump to a specific detail, pass a short `query` to `web_fetch`
instead of an `offset`. It returns up to three excerpts, preferring phrase
matches and then excerpts containing more of the query words. Each excerpt has
a `read` reference for more context. This is a local keyword search, not another
model call. A first read still fetches and reviews the full page before returning
any excerpts. No matches means no matching words were found; try different
keywords or read in order.

Content above the full-review ceiling is rejected, not partially released. The
cache uses the tool result cache's retention setting after the task ends and is
removed when its task is deleted.
