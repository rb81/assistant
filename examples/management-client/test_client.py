"""Reference-client protocol tests use only local HTTP and fake API fixtures."""
import asyncio
import importlib.util
import json
from pathlib import Path
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("reference_client", Path(__file__).with_name("client.py"))
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class Handler(BaseHTTPRequestHandler):
    captured = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        Handler.captured = self.headers.get("Authorization")
        if self.path.endswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/v2/health")
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"api_version":2}')


class ReferenceClientTests(unittest.TestCase):
    def test_authenticated_loopback_reads_and_refused_redirects(self):
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            api = client.API(server.server_port, "fixture-token")
            self.assertEqual(api.request("health"), {"api_version": 2})
            self.assertEqual(Handler.captured, "Bearer fixture-token")
            with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
                api.request("redirect")
            with self.assertRaises(ValueError):
                api.request("https://other.example")
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_incomplete_acknowledgment_is_not_delivery_success(self):
        class Fake:
            def request(self, resource, body=None, key=None):
                if resource.endswith("/messages"):
                    return {"messages": [{"id": 1, "body": "fixture"}], "poll_interval_seconds": 1}
                return {"acknowledged": []}

        async def registered(*args):
            return {"client": {"id": "a" * 32}}

        with patch.object(client, "perform", registered), patch("builtins.print"):
            with self.assertRaisesRegex(RuntimeError, "Acknowledgment incomplete"):
                asyncio.run(client.inbox(Fake(), "a" * 32))


if __name__ == "__main__":
    unittest.main()
