import json
import os
import urllib.request
import unittest
from unittest.mock import MagicMock, patch

from assistant_agent.config import AppConfig
from assistant_agent.embedding_client import EmbeddingClient
from assistant_agent.embedding_maintenance import EmbeddingMaintenance, SOURCES
from assistant_agent.context_store import cosine_similarity


class EmbeddingMaintenanceTest(unittest.TestCase):
    def service(self):
        db = MagicMock()
        db.connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = {"locked": True}
        db.fetch_one.return_value = {"count": 0}
        return EmbeddingMaintenance(db, AppConfig({"agent": {"embeddings": {"model": "test-model", "dimensions": 2}}}))

    def test_rebuild_uses_seven_active_sources_and_stable_text_builders(self):
        self.assertEqual({source[0] for source in SOURCES}, {"agent_memories", "workspace_file_chunks", "jobs", "reminders", "outbound_email_logs", "emails", "contacts"})
        samples = [{"content": "knowledge"}, {"content": "chunk"}, {"task_summary": "job", "metadata": {"final_response": "done"}},
                   {"title": "reminder", "task": "call"}, {"to_addresses": ["a@example.com"], "subject": "outbound", "body_text": "body"},
                   {"from_address": "a@example.com", "subject": "inbound", "body_text": "body"}, {"first_name": "Person", "notes": "detail"}]
        for source, row in zip(SOURCES, samples):
            self.assertTrue(source[2](row))

    def test_interrupted_rebuild_resumes_without_clearing_vectors(self):
        service = self.service()
        rows = [{"id": 1, "content": "first", "embedding_model": "old", "embedding_dimensions": 2},
                {"id": 2, "content": "second", "embedding_model": "old", "embedding_dimensions": 2}]
        service.db.fetch_all.side_effect = [rows]
        with patch.object(service, "probe"), patch.object(service.client, "embed", side_effect=[[1.0, 0.0], RuntimeError("offline")]):
            with self.assertRaises(RuntimeError):
                service.reindex(2)
        updates = [call for call in service.db.fetch_one.call_args_list if call.args[0].startswith("UPDATE")]
        self.assertEqual(len(updates), 1)
        self.assertNotIn("embedding = NULL", updates[0].args[0])
        self.assertIn("content IS NOT DISTINCT FROM %s", updates[0].args[0])
        self.assertNotIn("updated_at =", updates[0].args[0].replace("embedding_updated_at =", ""))
        service.db.fetch_all.side_effect = [[rows[1]], [], *[[] for source in SOURCES[1:]]]
        with patch.object(service, "probe"), patch.object(service.client, "embed", return_value=[0.0, 1.0]):
            result = service.reindex(2)
        self.assertEqual(result["refreshed"], 1)
        self.assertTrue(result["complete"])

    def test_concurrent_source_change_is_not_overwritten(self):
        service = self.service()
        service.db.fetch_all.side_effect = [[{"id": 1, "content": "original"}], [], *[[] for source in SOURCES[1:]]]
        service.db.fetch_one.side_effect = [None, *[{"count": 1} for source in SOURCES]]
        with patch.object(service, "probe"), patch.object(service.client, "embed", return_value=[1.0, 0.0]):
            result = service.reindex()
        self.assertEqual(result["refreshed"], 0)
        self.assertEqual(result["conflicts"], 1)
        self.assertFalse(result["complete"])

    def test_already_current_rows_are_a_noop_and_duplicate_runs_are_blocked(self):
        service = self.service()
        service.db.fetch_all.return_value = []
        with patch.object(service, "probe"), patch.object(service.client, "embed") as embed:
            self.assertTrue(service.reindex()["complete"])
            embed.assert_not_called()
        service.db.connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = {"locked": False}
        with patch.object(service, "probe"), self.assertRaisesRegex(RuntimeError, "already running"):
            service.reindex()

    def test_embedding_response_rejects_nonfinite_zero_and_wrong_dimension_vectors(self):
        client = EmbeddingClient(AppConfig({"agent": {"embeddings": {"dimensions": 2}}}))
        response = MagicMock()
        for vector in ([float("nan"), 1], [0, 0], [1]):
            response.__enter__.return_value.read.return_value = json.dumps({"embeddings": [vector]}).encode()
            with patch("assistant_agent.embedding_client.open_internal_http_request", return_value=response), self.assertRaises(RuntimeError):
                client.embed("test")

    def test_embedding_client_allows_only_managed_http_ollama(self):
        self.assertTrue(EmbeddingClient(AppConfig({}))._uses_internal_http)
        with self.assertRaisesRegex(RuntimeError, "must use HTTPS"):
            EmbeddingClient(AppConfig({"agent": {"embeddings": {"base_url": "http://embedding.example"}}}))
        client = EmbeddingClient(AppConfig({"agent": {"embeddings": {"base_url": "https://embedding.example/v1"}}}))
        self.assertFalse(client._uses_internal_http)

    def test_local_embedding_http_bypasses_proxies_and_refuses_redirects(self):
        from assistant_agent.transport_security import open_internal_http_request

        response = object()
        request = urllib.request.Request("http://ollama:11434/api/embed")
        with patch("assistant_agent.transport_security.urllib.request.build_opener") as build_opener:
            build_opener.return_value.open.return_value = response
            self.assertIs(open_internal_http_request(request, timeout=2), response)

        handlers = build_opener.call_args.args
        self.assertTrue(any(isinstance(handler, urllib.request.ProxyHandler) for handler in handlers))
        self.assertTrue(any(isinstance(handler, urllib.request.HTTPHandler) for handler in handlers))
        redirect_handler = next(handler for handler in handlers if handler.__class__.__name__ == "_NoRedirectHandler")
        self.assertIsNone(redirect_handler.redirect_request(request, None, 302, "Found", {}, "http://external.example"))


@unittest.skipUnless(os.getenv("ASSISTANT_TEST_EMBEDDING_URL"), "requires isolated Ollama with embeddinggemma downloaded")
class EmbeddingGemmaSmokeTest(unittest.TestCase):
    def test_real_model_dimensions_retrieval_and_long_input(self):
        config = AppConfig({"agent": {"embeddings": {"model": "embeddinggemma", "base_url": os.environ["ASSISTANT_TEST_EMBEDDING_URL"], "timeout_seconds": 120}}})
        service = EmbeddingMaintenance(MagicMock(), config)
        self.assertEqual(service.probe()["dimensions"], 768)
        query = service.client.embed("When is my dentist appointment?")
        relevant = service.client.embed("Your dentist appointment is on Friday morning at 10 AM.")
        unrelated = service.client.embed("The quarterly financial report shows a rise in operating revenue.")
        self.assertGreater(cosine_similarity(query, relevant), cosine_similarity(query, unrelated))
        self.assertEqual(len(service.client.embed("Meeting agenda and project updates. " * 2000)), 768)


if __name__ == "__main__":
    unittest.main()