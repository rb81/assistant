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
This feature is off by default. Deploy a self-hosted
[Crawl4AI](https://docs.crawl4ai.com/core/self-hosting/) server with the v0.9
Docker API and its authentication and public-egress controls enabled. For
example, use a pinned release of `unclecode/crawl4ai`, set
`CRAWL4AI_API_TOKEN` on that host, and place a private TLS reverse proxy in front
of it. Allow the application workers to reach `POST /crawl` only. Do not expose
the raw crawler port or mount Assistant credentials, the Docker socket, or
shared workspace into the scraper. On the crawler host, block access to
private networks and metadata services at the network layer as well as in
Crawl4AI; those restrictions must cover redirects and browser subrequests.
Do not grant generic access to the crawler from untrusted clients.

On the isolated crawler host, the upstream container can listen on loopback
for the reverse proxy. Set `CRAWL4AI_API_TOKEN` securely in the shell or through
your container secret manager before starting it:

```bash
docker run -d --name assistant-web-reader --restart unless-stopped \
  --shm-size=1g -p 127.0.0.1:11235:11235 -e CRAWL4AI_API_TOKEN \
  unclecode/crawl4ai:0.9.0
```

Keep the pinned release current with upstream security patches. Do not enable
deep crawling or give the crawler any LLM credentials; the Assistant handles
all model review after extraction.

In **Models → Web page reader**, save the proxy's HTTPS origin and the token,
enable the reader, and apply the settings. The origin is a trusted service
configuration value; the page URL always travels in the request body and
never changes the scraper destination. This is separate from network access
or approvals for Assistant scripts.

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
