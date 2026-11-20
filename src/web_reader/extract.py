import asyncio
import contextlib
import json
import sys

from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright
from trafilatura import extract


async def crawl(url, timeout):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            proxy={"server": "http://web-reader-gateway:3128"},
            args=["--proxy-bypass-list=<-loopback>", "--disable-quic"],
        )
        try:
            page = await browser.new_page(accept_downloads=False, service_workers="block")
            response = await page.goto(url, wait_until="domcontentloaded", timeout=max(500, (timeout - 3) * 1000))
            if not response or not response.ok:
                return {"results": [{"success": False}]}
            # Give client-side rendering a brief chance to settle without waiting on analytics indefinitely.
            with contextlib.suppress(BrowserTimeout):
                await page.wait_for_load_state("networkidle", timeout=1500)
            final_url = page.url
            title = (await page.title())[:300]
            html = await page.content()
        finally:
            await browser.close()
    if len(html) > 6 * 1024 * 1024:
        return {"results": [{"success": False}]}
    text = extract(html, url=final_url, output_format="markdown", include_links=True,
                   include_comments=False) or ""
    # Preserve the client's fail-closed review limit without copying a huge renderer result.
    return {"results": [{
        "success": True,
        "status_code": response.status,
        "url": final_url,
        "metadata": {"title": title},
        "markdown": text.strip().replace("\x00", "")[:48001],
    }]}


if __name__ == "__main__":
    with contextlib.redirect_stdout(sys.stderr):
        result = asyncio.run(crawl(sys.argv[1], int(sys.argv[2])))
    json.dump(result, sys.stdout)
