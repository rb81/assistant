"""Per-run HTTPS egress proxy for sandbox commands.

The sandbox broker starts one of these next to every command container. The
command container sits on an internal network whose only other member is this
proxy, so every outbound connection must go through it. Policy arrives in the
EGRESS_POLICY environment variable as {"allow": [...], "block": [...]}; each
entry is an exact host or a "*.example.com" suffix pattern. Decisions are
printed to stdout as JSON lines that the broker returns to the agent.
"""
import asyncio
import ipaddress
import json
import os
import socket
import sys
from typing import Optional


LISTEN_PORT = int(os.getenv("EGRESS_PROXY_PORT", "3128"))
HEAD_LIMIT = 16384
HEAD_TIMEOUT = 15
CONNECT_TIMEOUT = 10
IDLE_TIMEOUT = int(os.getenv("EGRESS_PROXY_IDLE_TIMEOUT", "300"))
MAX_CONNECTIONS = int(os.getenv("EGRESS_PROXY_MAX_CONNECTIONS", "64"))
MAX_EVENTS = 200

REASON_TEXT = {
    "not_approved": "this host is not approved for sandbox network access",
    "blocked_by_admin": "the administrator blocked this host",
    "plain_http": "only HTTPS is allowed through the sandbox proxy",
    "port_not_allowed": "only port 443 is allowed through the sandbox proxy",
    "ip_literal": "connect by hostname, not IP address",
    "invalid_host": "the requested host name is invalid",
    "private_address": "the host resolves to a private or internal address",
    "dns_failed": "the host name could not be resolved",
    "sni_mismatch": "the TLS server name did not match the approved host",
    "not_tls": "only TLS traffic is allowed through the sandbox proxy",
    "upstream_failed": "the remote host refused or did not answer the connection",
}


def normalize_host(value: str) -> str:
    host = str(value or "").strip().rstrip(".").lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host:
        raise ValueError("empty host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise LookupError("ip literal")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid host") from exc
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or any(
        not label or len(label) > 63 or not all(c.isalnum() or c == "-" for c in label)
        or label.startswith("-") or label.endswith("-") for label in labels
    ):
        raise ValueError("invalid host")
    return host


def matches(pattern: str, host: str) -> bool:
    pattern = str(pattern or "").strip().lower().rstrip(".")
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        return host.endswith(pattern[1:])
    return host == pattern


class Policy:
    def __init__(self, allow: list[str], block: list[str]):
        self.allow = [str(item) for item in allow]
        self.block = [str(item) for item in block]

    @classmethod
    def from_env(cls) -> "Policy":
        data = json.loads(os.getenv("EGRESS_POLICY") or "{}")
        return cls(list(data.get("allow") or []), list(data.get("block") or []))

    def decide(self, host: str) -> Optional[str]:
        if any(matches(pattern, host) for pattern in self.block):
            return "blocked_by_admin"
        if any(matches(pattern, host) for pattern in self.allow):
            return None
        return "not_approved"


class EventLog:
    def __init__(self) -> None:
        self.seen: set[tuple[str, str, str]] = set()

    def record(self, host: str, decision: str, reason: str = "") -> None:
        key = (host, decision, reason)
        if key in self.seen or len(self.seen) >= MAX_EVENTS:
            return
        self.seen.add(key)
        print(json.dumps({"event": "egress", "host": host, "decision": decision, "reason": reason}), flush=True)


def server_name_from_client_hello(handshake: bytes) -> Optional[str]:
    """Return the SNI host from a TLS ClientHello handshake message, or None."""
    try:
        if handshake[0] != 1:
            return None
        index = 4 + 2 + 32
        index += 1 + handshake[index]
        index += 2 + int.from_bytes(handshake[index:index + 2], "big")
        index += 1 + handshake[index]
        end = index + 2 + int.from_bytes(handshake[index:index + 2], "big")
        index += 2
        while index + 4 <= end:
            kind = int.from_bytes(handshake[index:index + 2], "big")
            size = int.from_bytes(handshake[index + 2:index + 4], "big")
            data = handshake[index + 4:index + 4 + size]
            index += 4 + size
            if kind != 0:
                continue
            names_end = 2 + int.from_bytes(data[0:2], "big")
            cursor = 2
            while cursor + 3 <= names_end:
                name_type = data[cursor]
                length = int.from_bytes(data[cursor + 1:cursor + 3], "big")
                name = data[cursor + 3:cursor + 3 + length]
                cursor += 3 + length
                if name_type == 0:
                    return name.decode("ascii").rstrip(".").lower()
            return None
    except (IndexError, UnicodeDecodeError):
        return None
    return None


async def read_client_hello(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """Read whole TLS records until the first handshake message is complete."""
    raw = b""
    handshake = b""
    while True:
        header = await reader.readexactly(5)
        if header[0] != 0x16:
            raise ValueError("not_tls")
        length = int.from_bytes(header[3:5], "big")
        if length == 0 or length > 16384:
            raise ValueError("not_tls")
        body = await reader.readexactly(length)
        raw += header + body
        handshake += body
        if len(handshake) >= 4 and len(handshake) >= 4 + int.from_bytes(handshake[1:4], "big"):
            return raw, handshake
        if len(raw) > 65536:
            raise ValueError("not_tls")


async def resolve_public(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM), CONNECT_TIMEOUT)
    except (OSError, asyncio.TimeoutError) as exc:
        raise ValueError("dns_failed") from exc
    addresses = []
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global:
            raise ValueError("private_address")
        if str(address) not in addresses:
            addresses.append(str(address))
    if not addresses:
        raise ValueError("dns_failed")
    return addresses


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), IDLE_TIMEOUT)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


class Proxy:
    def __init__(self, policy: Policy):
        self.policy = policy
        self.events = EventLog()
        self.slots = asyncio.Semaphore(MAX_CONNECTIONS)

    async def reject(self, writer: asyncio.StreamWriter, host: str, reason: str, status: str = "403 Forbidden") -> None:
        self.events.record(host, "blocked", reason)
        hint = ""
        if reason == "not_approved":
            hint = " Request access with the network_access_request tool, then retry."
        message = "Sandbox egress proxy blocked %s: %s.%s\n" % (host or "request", REASON_TEXT.get(reason, reason), hint)
        body = message.encode("utf-8")
        writer.write(
            ("HTTP/1.1 %s\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nX-Sandbox-Egress: %s\r\n"
             "Connection: close\r\n\r\n" % (status, len(body), reason)).encode("ascii") + body
        )
        try:
            await writer.drain()
        except OSError:
            pass

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with self.slots:
            try:
                await self.serve(reader, writer)
            except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError, asyncio.LimitOverrunError, ValueError):
                pass
            finally:
                try:
                    writer.close()
                except OSError:
                    pass

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        if len(head) > HEAD_LIMIT:
            await self.reject(writer, "", "invalid_host", "431 Request Header Fields Too Large")
            return
        parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
        if len(parts) != 3:
            await self.reject(writer, "", "invalid_host", "400 Bad Request")
            return
        method, target, _ = parts
        if method.upper() != "CONNECT":
            host = target.split("://", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1].split(":", 1)[0]
            await self.reject(writer, host.lower(), "plain_http")
            return
        raw_host, _, port = target.rpartition(":")
        try:
            host = normalize_host(raw_host)
        except LookupError:
            await self.reject(writer, raw_host.lower(), "ip_literal")
            return
        except ValueError:
            await self.reject(writer, raw_host.lower()[:255], "invalid_host", "400 Bad Request")
            return
        if port != "443":
            await self.reject(writer, host, "port_not_allowed")
            return
        reason = self.policy.decide(host)
        if reason:
            await self.reject(writer, host, reason)
            return
        try:
            addresses = await resolve_public(host)
        except ValueError as exc:
            await self.reject(writer, host, str(exc), "502 Bad Gateway")
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        try:
            initial, handshake = await asyncio.wait_for(read_client_hello(reader), HEAD_TIMEOUT)
        except ValueError:
            self.events.record(host, "blocked", "not_tls")
            return
        if server_name_from_client_hello(handshake) != host:
            self.events.record(host, "blocked", "sni_mismatch")
            return
        upstream = None
        for address in addresses:
            try:
                upstream = await asyncio.wait_for(asyncio.open_connection(address, 443), CONNECT_TIMEOUT)
                break
            except (OSError, asyncio.TimeoutError):
                continue
        if upstream is None:
            self.events.record(host, "blocked", "upstream_failed")
            return
        upstream_reader, upstream_writer = upstream
        self.events.record(host, "allowed")
        upstream_writer.write(initial)
        await upstream_writer.drain()
        await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))


async def main() -> None:
    proxy = Proxy(Policy.from_env())
    server = await asyncio.start_server(proxy.handle, "0.0.0.0", LISTEN_PORT, limit=HEAD_LIMIT * 2)
    print(json.dumps({"event": "ready", "port": LISTEN_PORT}), flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
