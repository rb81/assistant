import asyncio

from egress_proxy import HEAD_LIMIT, Policy, Proxy, pipe


async def forward(reader, writer):
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection("web-reader", 8080), timeout=3,
        )
    except (OSError, TimeoutError):
        body = b"Managed browser service is unavailable"
        writer.write(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: "
                     + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
        try:
            await writer.drain()
        finally:
            writer.close()
        return
    await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))


async def main():
    proxy = Proxy(Policy(["*"], []))
    egress = await asyncio.start_server(proxy.handle, "0.0.0.0", 3128, limit=HEAD_LIMIT * 2)
    control = await asyncio.start_server(forward, "0.0.0.0", 8080)
    async with egress, control:
        await asyncio.gather(egress.serve_forever(), control.serve_forever())


if __name__ == "__main__":
    asyncio.run(main())
