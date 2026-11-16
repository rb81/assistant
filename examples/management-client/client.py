"""Small standard-library Management API client; no token persistence or shell RPC."""
import argparse
import asyncio
import contextlib
import json
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
import uuid


MAX_RESPONSE = 2 * 1024 * 1024


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class API:
    def __init__(self, port, token):
        self.base = f"http://127.0.0.1:{port}/v2/"
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())

    def request(self, resource, body=None, key=None):
        if not re.fullmatch(r"[a-zA-Z0-9/_-]+", resource):
            raise ValueError("Use a fixed relative API resource")
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if key is not None:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(self.base + resource, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=45) as response:
                content = response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise RuntimeError(f"Management HTTP {status}; inspect server state") from None
        if len(content) > MAX_RESPONSE:
            raise ValueError("Management response exceeds the 2 MiB example limit")
        return json.loads(content)


async def stop(process):
    if process is None or process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


async def discovery(executable, arguments):
    process = await asyncio.create_subprocess_exec(
        executable, *arguments, stdin=subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=subprocess.DEVNULL,
    )

    async def capture():
        content = bytearray()
        while True:
            chunk = await process.stdout.read(4097 - len(content))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > 4096:
                raise ValueError("Oversized installed-launcher handoff")
        if await process.wait() != 0:
            raise RuntimeError("Discovery failed; check SSH trust/authentication, launcher and Docker access")
        return json.loads(content)

    try:
        result = await asyncio.wait_for(capture(), timeout=20)
    finally:
        await stop(process)
    if (
        result.get("api_version") != 1
        or result.get("host") != "127.0.0.1"
        or not isinstance(result.get("port"), int)
        or not 1 <= result["port"] <= 65535
        or not isinstance(result.get("token"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", result["token"])
    ):
        raise ValueError("Invalid installed-launcher handoff")
    return result


def reserve_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def perform(api, payload):
    """No automatic resubmission; keep the key/accepted ID visible for reconciliation."""
    key = str(uuid.uuid4())
    print(f"Submission key: {key}", file=sys.stderr)
    operation = await asyncio.to_thread(api.request, "operations", payload, key)
    identifier = operation.get("id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier):
        raise ValueError("Invalid accepted operation ID; inspect server operations")
    print(f"Accepted operation: {identifier}", file=sys.stderr)
    deadline = asyncio.get_running_loop().time() + 300
    while operation.get("state") in {"queued", "running"}:
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Operation {identifier} still pending; inspect before retrying")
        await asyncio.sleep(1)
        operation = await asyncio.to_thread(api.request, f"operations/{identifier}")
    if operation.get("state") != "succeeded":
        raise RuntimeError(f"Operation {identifier} ended as {operation.get('state')}; inspect before retrying")
    return operation.get("result")


async def inbox(api, identifier):
    result = await perform(api, {
        "operation": "client.register", "client_id": identifier,
        "name": "Reference client", "client_type": "custom",
        "confirmed": True,
    })
    if not isinstance(result, dict) or result.get("client", {}).get("id") != identifier:
        raise ValueError("Server registration returned a different client ID")
    delivered = set()
    while True:
        response = await asyncio.to_thread(api.request, f"clients/{identifier}/messages")
        interval = response.get("poll_interval_seconds")
        if not isinstance(interval, int) or interval <= 0 or not isinstance(response.get("messages"), list):
            raise ValueError("Incompatible client poll response")
        ids = []
        for message in response["messages"]:
            if not isinstance(message.get("id"), int) or message["id"] <= 0:
                raise ValueError("Incompatible message identity")
            ids.append(message["id"])
            if message["id"] not in delivered:
                print(json.dumps(message, ensure_ascii=False))
        sys.stdout.flush()
        if ids:
            acknowledgment = await asyncio.to_thread(
                api.request, f"clients/{identifier}/acknowledgments", {"message_ids": ids},
            )
            if set(acknowledgment.get("acknowledged", [])) != set(ids):
                raise RuntimeError("Acknowledgment incomplete; messages may be offered again")
            delivered.update(ids)
        await asyncio.sleep(interval)


async def main(args):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,254}", args.ssh):
        raise ValueError("Invalid SSH destination")
    if not 1 <= args.port <= 65535:
        raise ValueError("Invalid SSH port")
    executable = shutil.which("ssh")
    if executable is None:
        raise RuntimeError("Install OpenSSH before running this example")
    options = [
        "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10", "-p", str(args.port),
    ]
    handoff = await discovery(
        executable, options + ["--", args.ssh, 'exec "$HOME/.local/bin/assistant" connect'],
    )
    port = reserve_port()
    process = await asyncio.create_subprocess_exec(
        executable, *options, "-N", "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
        "-L", f"127.0.0.1:{port}:127.0.0.1:{handoff['port']}", "--", args.ssh,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        api = API(port, handoff["token"])
        for attempt in range(40):
            if process.returncode is not None:
                raise RuntimeError("Owned SSH tunnel exited before readiness")
            try:
                health = await asyncio.to_thread(api.request, "health")
                break
            except urllib.error.URLError:
                if attempt == 39:
                    raise RuntimeError("Management API did not become reachable") from None
                await asyncio.sleep(0.25)
        if (
            health.get("api_version") != 2 or health.get("read_only") is not False
            or not health.get("instance_id")
            or "configuration.read" not in health.get("capabilities", [])
        ):
            raise ValueError("Writable Management API v2 is required")
        if args.expected_instance and health["instance_id"] != args.expected_instance:
            raise ValueError("Installation identity changed; verify before continuing")
        if args.command == "inbox":
            if not re.fullmatch(r"[a-f0-9]{32}", args.client_id):
                raise ValueError("Client ID must be 32 lowercase hex characters")
            await inbox(api, args.client_id)
        else:
            result = health if args.command == "health" else await asyncio.to_thread(api.request, "domains")
            print(json.dumps(result, indent=2))
    finally:
        await stop(process)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--ssh", required=True, help="Previously trusted SSH alias or user@host")
    result.add_argument("--port", type=int, default=22)
    result.add_argument("--expected-instance", help="Reject a different Management API instance ID")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("health")
    commands.add_parser("domains")
    polling = commands.add_parser("inbox")
    polling.add_argument("--client-id", required=True, help="Retain this non-secret ID between runs")
    return result


if __name__ == "__main__":
    try:
        asyncio.run(main(parser().parse_args()))
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print(f"Client failed: {error}", file=sys.stderr)
        sys.exit(1)
