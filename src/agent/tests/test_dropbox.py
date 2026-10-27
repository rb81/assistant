import errno
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx

from assistant_agent.config import AppConfig
from assistant_agent.dropbox_provider import DropboxClient, DropboxError, authorization, content_hash, exchange_code, remote_path


class DropboxProviderTest(unittest.TestCase):
    def setUp(self):
        self.config = AppConfig({"agent": {"dropbox": {"enabled": True, "client_id": "client", "refresh_token": "secret-canary"}}})
        self.client = DropboxClient(self.config)
        self.client.access_token = "access"
        self.client.expires_at = float("inf")

    def test_paths_reject_traversal_ids_urls_and_other_folders(self):
        for path in ["/Inbox/../Outbox/file", "/Inbox//file", "/Inbox/./file", "/Inbox2/file", "/Outbox/file", "id:abc", "https://example.com/file", "/Inbox/a\\b", "/Inbox/a\n"]:
            with self.subTest(path=path), self.assertRaises(DropboxError):
                remote_path(path, "Inbox")
        self.assertEqual(remote_path("/inbox/report.pdf", "Inbox"), "/Inbox/report.pdf")

    def test_oauth_is_offline_pkce_and_exchange_is_redacted(self):
        url, verifier = authorization("client")
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["token_access_type"], ["offline"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertNotIn(verifier, url)
        response = httpx.Response(400, json={"error": "secret-canary"})
        with patch("httpx.post", return_value=response) as send, self.assertRaises(DropboxError) as caught:
            exchange_code("client", "code-canary", verifier)
        self.assertNotIn("canary", str(caught.exception))
        self.assertEqual(send.call_args.kwargs["data"]["client_id"], "client")
        self.assertEqual(send.call_args.kwargs["data"]["code_verifier"], verifier)
        self.assertFalse(send.call_args.kwargs["follow_redirects"])

    def test_refresh_is_cached_and_invalid_token_is_redacted(self):
        self.client.expires_at = 0
        with patch("httpx.post", return_value=httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})) as send:
            self.assertEqual(self.client.token(), "fresh")
            self.assertEqual(self.client.token(), "fresh")
            self.assertEqual(send.call_count, 1)
        with patch("httpx.post", side_effect=httpx.ConnectError("secret-canary")), self.assertRaises(DropboxError) as caught:
            self.client.token(force=True)
        self.assertNotIn("secret-canary", str(caught.exception))

    def test_cursor_is_bound_to_folder_and_connection(self):
        cursor = self.client.cursor("Inbox", "provider-cursor")
        self.assertEqual(self.client.decode_cursor("Inbox", cursor), "provider-cursor")
        for folder, value in [("Outbox", cursor), ("Inbox", "garbage"), ("Inbox", cursor[:-4] + "AAAA")]:
            with self.assertRaises(DropboxError):
                self.client.decode_cursor(folder, value)

    def test_metadata_cannot_escape_or_change_requested_path(self):
        for path in ["/Outbox/file.txt", "/Inbox/other.txt"]:
            with patch.object(self.client, "request", return_value={".tag": "file", "id": "id:1", "rev": "rev", "size": 0, "path_display": path}), self.assertRaises(DropboxError):
                self.client.metadata("/Inbox/file.txt")

    def test_delete_requires_current_listed_revision_and_deletes_by_file_id(self):
        current = {"id": "id:1", "rev": "r1", "path": "/Inbox/file.txt", "size": 5}
        with patch.object(self.client, "metadata", return_value=current), patch.object(self.client, "request", return_value={}) as request:
            result = self.client.delete_file("/Inbox/file.txt", "r1", "Inbox")
        self.assertEqual(result, {"deleted": True, "path": "/Inbox/file.txt", "revision": "r1"})
        request.assert_called_once_with("files/delete_v2", {"path": "id:1", "parent_rev": "r1"})
        for folder, path in [("Other", "/Other/file.txt"), ("Inbox", "/Outbox/file.txt")]:
            with self.subTest(folder=folder), self.assertRaises(DropboxError):
                self.client.delete_file(path, "r1", folder)
        with patch.object(self.client, "metadata", return_value={**current, "rev": "r2"}), patch.object(self.client, "request") as request:
            with self.assertRaisesRegex(DropboxError, "changed"):
                self.client.delete_file("/Inbox/file.txt", "r1", "Inbox")
        request.assert_not_called()

    def test_delete_reports_missing_files_without_deleting(self):
        with patch.object(self.client, "metadata", return_value=None), patch.object(self.client, "request") as request:
            result = self.client.delete_file("/Outbox/file.txt", "r1", "Outbox")
        self.assertEqual(result, {"deleted": False, "path": "/Outbox/file.txt", "already_missing": True})
        request.assert_not_called()

    def test_rate_limit_is_persisted_and_prevents_retry(self):
        self.client.db = MagicMock()
        self.client.db.get_state.return_value = {}
        with patch("httpx.post", return_value=httpx.Response(429, headers={"Retry-After": "90"})), self.assertRaisesRegex(DropboxError, "90"):
            self.client.request("files/list_folder", {"path": "/Inbox"})
        self.client.db.get_state.return_value = self.client.db.set_state.call_args.args[1]
        with patch("httpx.post") as send, self.assertRaises(DropboxError):
            self.client.request("files/list_folder", {"path": "/Inbox"})
        send.assert_not_called()

    def test_download_verifies_revision_hash_size_and_cleans_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "download.txt"
            body = b"hello"
            digest = hashlib.sha256(hashlib.sha256(body).digest()).hexdigest()
            metadata = {"id": "id:1", "rev": "r1", "path": "/Inbox/file.txt", "size": 5, "content_hash": digest}
            remote = {"path_display": metadata["path"], **metadata}
            response = httpx.Response(200, headers={"Dropbox-API-Result": json.dumps(remote)}, content=body)
            with patch("httpx.stream") as stream:
                stream.return_value.__enter__.return_value = response
                self.client.download(metadata, target)
            self.assertEqual(target.read_bytes(), body)
            self.assertEqual(content_hash(target), digest)
            target.unlink()
            with patch("httpx.stream") as stream:
                stream.return_value.__enter__.return_value = httpx.Response(200, headers={"Dropbox-API-Result": json.dumps({".tag": "file", **remote})}, content=body)
                self.client.download(metadata, target)
            self.assertEqual(target.read_bytes(), body)
            target.unlink()
            for data, size in [(b"too many", 5), (b"less", 5), (b"world", 5), (b"hello", 4)]:
                remote["size"] = size
                with patch("httpx.stream") as stream, self.assertRaises(DropboxError):
                    stream.return_value.__enter__.return_value = httpx.Response(200, headers={"Dropbox-API-Result": json.dumps(remote)}, content=data)
                    self.client.download(metadata, target)
                self.assertFalse(target.exists())

    def test_unknown_endpoint_and_disabled_configuration_are_rejected(self):
        with self.assertRaises(DropboxError):
            self.client.request("https://evil.example", {})
        with self.assertRaises(DropboxError):
            DropboxClient(AppConfig({}))

    def test_failed_download_does_not_remove_existing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing.txt"
            target.write_bytes(b"preserve")
            metadata = {"id": "id:1", "rev": "r1", "path": "/Inbox/file.txt", "size": 5, "content_hash": "hash"}
            remote = {".tag": "file", "path_display": metadata["path"], **metadata}
            with patch("httpx.stream") as stream, self.assertRaises(DropboxError):
                stream.return_value.__enter__.return_value = httpx.Response(200, headers={"Dropbox-API-Result": json.dumps(remote)}, content=b"hello")
                self.client.download(metadata, target)
            self.assertEqual(target.read_bytes(), b"preserve")

    def test_missing_folder_listing_is_read_only_and_empty(self):
        with patch.object(self.client, "request", return_value=None) as request:
            result = self.client.list_files("Outbox")
        self.assertTrue(result["folder_missing"])
        self.assertEqual(result["files"], [])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], "files/list_folder")

    def test_upload_conflict_reconciliation_never_overwrites_or_reuploads(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "result.txt"
            source.write_bytes(b"hello")
            existing = {"id": "id:1", "rev": "r1", "path": "/Outbox/job-1/result.txt", "size": 5, "content_hash": content_hash(source)}
            with patch.object(self.client, "metadata", return_value=existing), patch("httpx.post") as send:
                self.assertEqual(self.client.upload(source, existing["path"]), existing)
            send.assert_not_called()
            existing["content_hash"] = "different"
            with patch.object(self.client, "metadata", return_value=existing), patch("httpx.post") as send, self.assertRaises(DropboxError):
                self.client.upload(source, existing["path"])
            send.assert_not_called()

    def test_upload_uses_strict_add_mode_and_timeout_is_uncertain(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "result.txt"
            source.write_bytes(b"hello")
            metadata = {"id": "id:1", "rev": "r1", "path_display": "/Outbox/job-1/result.txt", "size": 5, "content_hash": content_hash(source)}
            with patch.object(self.client, "metadata", return_value=None), patch.object(self.client, "request", return_value={}), patch("httpx.post", return_value=httpx.Response(200, json=metadata)) as send:
                result = self.client.upload(source, "/Outbox/job-1/result.txt")
            self.assertEqual(result["path"], metadata["path_display"])
            for field in ("id", "rev", "size", "content_hash"):
                self.assertEqual(result[field], metadata[field])
            arguments = json.loads(send.call_args.kwargs["headers"]["Dropbox-API-Arg"])
            self.assertEqual(arguments["mode"], "add")
            self.assertTrue(arguments["strict_conflict"])
            self.assertFalse(arguments["autorename"])
            self.assertFalse(send.call_args.kwargs["follow_redirects"])
            with patch.object(self.client, "metadata", return_value=None), patch.object(self.client, "request", return_value={}), patch("httpx.post", return_value=httpx.Response(200, json={".tag": "file", **metadata})):
                self.assertEqual(self.client.upload(source, "/Outbox/job-1/result.txt"), result)
            with patch.object(self.client, "metadata", return_value=None), patch.object(self.client, "request", return_value={}), patch("httpx.post", side_effect=httpx.ReadTimeout("token-canary")), self.assertRaises(DropboxError) as caught:
                self.client.upload(source, "/Outbox/job-1/result.txt")
            self.assertTrue(caught.exception.uncertain)
            self.assertNotIn("canary", str(caught.exception))

    def test_listing_is_bounded_and_pagination_cannot_switch_folders(self):
        entry = {".tag": "file", "id": "id:1", "rev": "r1", "size": 1, "path_display": "/Inbox/file.txt"}
        with patch.object(self.client, "request", return_value={"entries": [entry], "cursor": "cursor", "has_more": True}) as request:
            result = self.client.list_files()
            self.assertEqual(request.call_args.args[1]["limit"], 100)
            self.client.list_files("Inbox", result["cursor"])
            self.assertEqual(request.call_args.args[0], "files/list_folder/continue")
            self.assertEqual(request.call_args.args[1], {"cursor": "cursor"})
            with self.assertRaises(DropboxError):
                self.client.list_files("Outbox", result["cursor"])

    def test_lookup_requires_tagged_files_and_listing_excludes_nonfiles(self):
        metadata = {"id": "id:1", "rev": "r1", "size": 5, "path_display": "/Inbox/file.txt"}
        entries = [metadata] + [{".tag": tag, **metadata} for tag in ("folder", "deleted", "unknown", None)]
        for entry in entries:
            with self.subTest(entry=entry), patch.object(self.client, "request", return_value=entry), self.assertRaisesRegex(DropboxError, "not a regular file"):
                self.client.metadata("/Inbox/file.txt")
        entries.append({".tag": "file", **metadata})
        with patch.object(self.client, "request", return_value={"entries": entries, "has_more": False}):
            result = self.client.list_files("Inbox")
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["files"][0]["path"], "/Inbox/file.txt")

    def test_successful_upload_with_invalid_metadata_remains_uncertain(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "result.txt"
            source.write_bytes(b"hello")
            metadata = {"id": "id:1", "rev": "r1", "size": 5, "path_display": "/Outbox/job-1/result.txt", "content_hash": content_hash(source)}
            invalid = [None, [], "secret-canary"]
            invalid += [{**metadata, ".tag": tag} for tag in ("folder", "deleted", "unknown", None, [])]
            invalid += [{**metadata, field: value} for field, value in (
                ("id", None), ("rev", None), ("size", -1), ("size", "5"),
                ("content_hash", None), ("content_hash", "different"),
                ("path_display", "/Inbox/file.txt"), ("path_display", "/Outbox/job-1/other.txt"),
            )]
            responses = [httpx.Response(200, json=payload) for payload in invalid]
            responses.append(httpx.Response(200, content=b"secret-canary"))
            for response in responses:
                with self.subTest(response=response.content), patch.object(self.client, "metadata", return_value=None), patch.object(self.client, "request", return_value={}), patch("httpx.post", return_value=response), self.assertRaises(DropboxError) as caught:
                    self.client.upload(source, "/Outbox/job-1/result.txt")
                self.assertTrue(caught.exception.uncertain)
                self.assertNotIn("canary", str(caught.exception))

    def test_upload_http_rejections_keep_their_outcome_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "result.txt"
            source.write_bytes(b"hello")
            for status_code in (403, 409, 429, 503):
                with self.subTest(status=status_code), patch.object(self.client, "metadata", return_value=None), patch.object(self.client, "request", return_value={}), patch("httpx.post", return_value=httpx.Response(status_code, json={"error": "secret-canary"})), self.assertRaises(DropboxError) as caught:
                    self.client.upload(source, "/Outbox/job-1/result.txt")
                self.assertEqual(caught.exception.uncertain, status_code >= 500)
                self.assertNotIn("canary", str(caught.exception))

    def test_download_rejects_invalid_concrete_metadata_without_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "download.txt"
            digest = hashlib.sha256(hashlib.sha256(b"hello").digest()).hexdigest()
            metadata = {"id": "id:1", "rev": "r1", "size": 5, "path": "/Inbox/file.txt", "content_hash": digest}
            remote = {**metadata, "path_display": metadata["path"]}
            invalid = [None, [], {**remote, "id": None}, {**remote, "rev": "changed"},
                       {**remote, "size": -1}, {**remote, "path_display": "/Outbox/file.txt"},
                       {**remote, "path_display": "/Inbox/other.txt"}, {**remote, "content_hash": "different"}]
            invalid += [{**remote, ".tag": tag} for tag in ("folder", "deleted", "unknown", None, [])]
            for payload in invalid:
                with self.subTest(payload=payload), patch("httpx.stream") as stream, self.assertRaises(DropboxError):
                    stream.return_value.__enter__.return_value = httpx.Response(200, headers={"Dropbox-API-Result": json.dumps(payload)}, content=b"hello")
                    self.client.download(metadata, target)
                self.assertFalse(target.exists())


class DropboxTransferTest(unittest.TestCase):
    def setUp(self):
        from assistant_agent.dropbox_files import DropboxFiles
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.shared = root / "share"
        self.shared.mkdir()
        self.config = AppConfig({"agent": {"dropbox": {"enabled": True, "allow_exports": True, "client_id": "client", "refresh_token": "refresh"},
                                         "filesystem": {"shared_root": str(self.shared), "require_mount": False},
                                         "artifacts": {"raw_root": str(root / "private")}}})
        self.db = MagicMock()
        self.db.fetch_one.return_value = None
        self.service = DropboxFiles(self.db, self.config, {"id": 1, "thread_id": "thread"})
        self.service.client = MagicMock(identity="connection", max_bytes=100)
        self.service.scanner = MagicMock()
        self.service.scanner.scan_file.return_value = {"status": "clean"}
        self.service.scanner.is_supported_attachment.return_value = True
        self.service.scanner.extractor.convert_to_markdown.return_value = "converted content"
        self.metadata = {"id": "id:1", "rev": "r1", "path": "/Inbox/file.txt", "size": 5,
                         "content_hash": hashlib.sha256(hashlib.sha256(b"hello").digest()).hexdigest()}
        self.service.client.metadata.return_value = self.metadata
        self.service.client.download.side_effect = lambda metadata, path: path.write_bytes(b"hello")

    def test_import_publishes_only_after_clean_scan_and_conversion(self):
        with patch("assistant_agent.dropbox_files.WorkspaceIndex") as index:
            result = self.service.import_file("/Inbox/file.txt", "r1")
        self.assertEqual((self.shared / result["path"]).read_bytes(), b"hello")
        self.assertEqual((self.shared / result["markdown_path"]).read_text(), "converted content")
        self.assertTrue(result["untrusted"])
        index.return_value.index_path_best_effort.assert_called_once()
        self.assertFalse(list(self.service.private_root.iterdir()))

    def test_import_parses_untagged_download_metadata(self):
        self.service.client = DropboxClient(self.config, self.db)
        self.service.client.access_token = "access"
        self.service.client.expires_at = float("inf")
        self.db.get_state.return_value = {}
        remote = {**self.metadata, "path_display": self.metadata["path"]}
        with patch("assistant_agent.dropbox_files.WorkspaceIndex") as index, patch("httpx.post", return_value=httpx.Response(200, json={".tag": "file", **remote})), patch("httpx.stream") as stream:
            stream.return_value.__enter__.return_value = httpx.Response(200, headers={"Dropbox-API-Result": json.dumps(remote)}, content=b"hello")
            result = self.service.import_file("/Inbox/file.txt", "r1")
        self.assertEqual((self.shared / result["path"]).read_bytes(), b"hello")
        self.assertEqual((self.shared / result["markdown_path"]).read_text(), "converted content")
        self.assertIn("status = 'ready'", self.db.execute.call_args.args[0])
        self.assertEqual(self.service.scanner.scan_file.call_count, 2)
        index.return_value.index_path_best_effort.assert_called_once()
        self.assertFalse(list(self.service.private_root.iterdir()))

    def test_all_nonclean_scans_block_publication_and_indexing(self):
        for scan_status in ["infected", "error", "skipped", "pending"]:
            self.service.scanner.scan_file.return_value = {"status": scan_status, "allow_conversion": True}
            with patch("assistant_agent.dropbox_files.WorkspaceIndex") as index, self.assertRaises(DropboxError):
                self.service.import_file("/Inbox/file.txt", "r1")
            self.assertFalse(list(self.shared.iterdir()))
            index.assert_not_called()

    def test_conversion_failure_revision_and_size_block_import(self):
        self.service.scanner.extractor.convert_to_markdown.side_effect = ValueError("secret-canary")
        with self.assertRaises(DropboxError) as caught:
            self.service.import_file("/Inbox/file.txt", "r1")
        self.assertNotIn("canary", str(caught.exception))
        self.assertFalse(list(self.shared.iterdir()))
        with self.assertRaises(DropboxError):
            self.service.import_file("/Inbox/file.txt", "wrong-revision")
        self.metadata["size"] = 101
        with self.assertRaises(DropboxError):
            self.service.import_file("/Inbox/file.txt", "r1")

    def test_export_scans_snapshot_and_never_uploads_changed_source(self):
        source = self.shared / "result.txt"
        source.write_bytes(b"hello")
        self.service.scanner.scan_file.side_effect = lambda snapshot: (source.write_bytes(b"changed"), {"status": "clean"})[1]
        captured = []
        def upload(snapshot, destination):
            captured.append(snapshot.read_bytes())
            return {**self.metadata, "path": destination}
        self.service.client.upload.side_effect = upload
        result = self.service.export_file("result.txt")
        self.assertEqual(captured, [b"hello"])
        self.assertEqual(result["remote_path"], "/Outbox/job-1/result.txt")
        self.assertFalse(list(self.service.private_root.iterdir()))

    def test_export_parses_untagged_upload_and_reconciles_without_reupload(self):
        source = self.shared / "result.txt"
        source.write_bytes(b"hello")
        self.service.client = DropboxClient(self.config, self.db)
        self.service.client.access_token = "access"
        self.service.client.expires_at = float("inf")
        self.db.get_state.return_value = {}
        remote = {"id": "id:1", "rev": "r1", "size": 5, "content_hash": content_hash(source), "path_display": "/Outbox/job-1/result.txt"}
        responses = [
            httpx.Response(409, json={"error": {".tag": "path", "path": {".tag": "not_found"}}}),
            httpx.Response(200, json={"metadata": {".tag": "folder", "path_display": "/Outbox"}}),
            httpx.Response(200, json={"metadata": {".tag": "folder", "path_display": "/Outbox/job-1"}}),
            httpx.Response(200, json=remote),
            httpx.Response(200, json={".tag": "file", **remote}),
        ]
        with patch("httpx.post", side_effect=responses) as send:
            result = self.service.export_file("result.txt")
            self.db.fetch_one.return_value = {"status": "ready", "content_hash": remote["content_hash"]}
            self.assertEqual(self.service.export_file("result.txt"), result)
        self.assertEqual(result, {"remote_path": remote["path_display"], "revision": "r1", "size_bytes": 5, "scan_status": "clean"})
        uploads = [call for call in send.call_args_list if call.args[0].endswith("files/upload")]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(send.call_count, len(responses))
        self.assertIn("status = 'ready'", self.db.execute.call_args.args[0])
        self.assertFalse(list(self.service.private_root.iterdir()))

    def test_export_invalid_success_response_reconciles_without_reupload(self):
        source = self.shared / "result.txt"
        source.write_bytes(b"hello")
        self.service.client = DropboxClient(self.config, self.db)
        self.service.client.access_token = "access"
        self.service.client.expires_at = float("inf")
        self.db.get_state.return_value = {}
        remote = {"id": "id:1", "rev": "r1", "size": 5, "content_hash": content_hash(source), "path_display": "/Outbox/job-1/result.txt"}
        with patch.object(self.service.client, "request", return_value={}), patch.object(self.service.client, "metadata", side_effect=[None, self.service.client.validate_metadata({".tag": "file", **remote}, "Outbox")]), patch("httpx.post", return_value=httpx.Response(200, json={**remote, "rev": None})) as send:
            with self.assertLogs("assistant.dropbox", level="ERROR"), self.assertRaises(DropboxError) as caught:
                self.service.export_file("result.txt")
            self.assertTrue(caught.exception.uncertain)
            self.assertEqual(self.db.execute.call_args.args[1][0], "uncertain")
            self.db.fetch_one.return_value = {"status": "uncertain", "content_hash": remote["content_hash"]}
            result = self.service.export_file("result.txt")
        self.assertEqual(result["remote_path"], remote["path_display"])
        send.assert_called_once()
        self.assertIn("status = 'ready'", self.db.execute.call_args.args[0])
        self.assertFalse(list(self.service.private_root.iterdir()))

    def test_export_staging_failures_are_actionable_and_redacted(self):
        (self.shared / "result.txt").write_bytes(b"hello")
        for operation in ("mkdir", "temporary_directory"):
            for error_number in (errno.EROFS, errno.EACCES, errno.EPERM):
                failure = OSError(error_number, "secret-canary")
                target = patch.object(Path, "mkdir", side_effect=failure) if operation == "mkdir" else patch("assistant_agent.dropbox_files.tempfile.TemporaryDirectory", side_effect=failure)
                with self.subTest(operation=operation, errno=error_number), target, self.assertLogs("assistant.dropbox", level="ERROR") as logs:
                    with self.assertRaises(DropboxError) as caught:
                        self.service.export_file("result.txt")
                self.assertIn("private staging is not writable", str(caught.exception))
                self.assertFalse(caught.exception.uncertain)
                self.assertEqual(self.db.execute.call_args.args[1][0], "failed")
                self.assertIn("stage=staging", " ".join(logs.output))
                self.assertIn("errno=%s" % error_number, " ".join(logs.output))
                self.assertNotIn("canary", str(caught.exception) + " ".join(logs.output) + str(self.db.execute.call_args))
        self.service.scanner.scan_file.assert_not_called()
        self.service.client.upload.assert_not_called()

    def test_export_staging_failure_preserves_previous_uncertainty(self):
        source = self.shared / "result.txt"
        source.write_bytes(b"hello")
        for previous_status in ("uncertain", "pending"):
            self.db.fetch_one.return_value = {"status": previous_status, "content_hash": content_hash(source)}
            with self.subTest(status=previous_status), patch.object(Path, "mkdir", side_effect=OSError(errno.EROFS, "secret-canary")), self.assertLogs("assistant.dropbox", level="ERROR"):
                with self.assertRaises(DropboxError) as caught:
                    self.service.export_file("result.txt")
            self.assertTrue(caught.exception.uncertain)
            self.assertEqual(self.db.execute.call_args.args[1][0], "uncertain")
        self.service.client.upload.assert_not_called()

    def test_export_unexpected_failures_report_stage_and_upload_uncertainty(self):
        (self.shared / "result.txt").write_bytes(b"hello")
        self.service.client.upload.return_value = {**self.metadata, "path": "/Outbox/job-1/result.txt"}
        for stage, target, method, uncertain in (
            ("scan", self.service.scanner, "scan_file", False),
            ("upload", self.service.client, "upload", True),
            ("finalization", self.service, "ready", True),
        ):
            with self.subTest(stage=stage), patch.object(target, method, side_effect=RuntimeError("secret-canary")), self.assertLogs("assistant.dropbox", level="ERROR") as logs:
                with self.assertRaises(DropboxError) as caught:
                    self.service.export_file("result.txt")
            self.assertIn(stage, str(caught.exception))
            self.assertEqual(caught.exception.uncertain, uncertain)
            self.assertEqual(self.db.execute.call_args.args[1][0], "uncertain" if uncertain else "failed")
            self.assertIn("stage=%s" % stage, " ".join(logs.output))
            self.assertIn("exception=RuntimeError", " ".join(logs.output))
            self.assertNotIn("canary", str(caught.exception) + " ".join(logs.output) + str(self.db.execute.call_args))
            self.assertFalse(list(self.service.private_root.iterdir()))

    def test_export_cleanup_failure_preserves_uploaded_outcome(self):
        (self.shared / "result.txt").write_bytes(b"hello")
        self.service.client.upload.return_value = {**self.metadata, "path": "/Outbox/job-1/result.txt"}
        original_exit = tempfile.TemporaryDirectory.__exit__

        def fail_after_cleanup(directory, *args):
            original_exit(directory, *args)
            raise PermissionError(errno.EACCES, "secret-canary")

        with patch.object(tempfile.TemporaryDirectory, "__exit__", fail_after_cleanup), self.assertLogs("assistant.dropbox", level="ERROR") as logs:
            with self.assertRaises(DropboxError) as caught:
                self.service.export_file("result.txt")
        self.assertIn("cleanup", str(caught.exception))
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(self.db.execute.call_args.args[1][0], "uncertain")
        self.service.client.upload.assert_called_once()
        self.assertFalse(list(self.service.private_root.iterdir()))
        self.assertNotIn("canary", str(caught.exception) + " ".join(logs.output))

    def test_export_failure_recording_cannot_mask_primary_error(self):
        (self.shared / "result.txt").write_bytes(b"hello")
        self.service.client.upload.side_effect = DropboxError("Dropbox rate limited; retry later")
        with patch.object(self.service, "failed", side_effect=RuntimeError("database-secret-canary")), self.assertLogs("assistant.dropbox", level="ERROR") as logs:
            with self.assertRaises(DropboxError) as caught:
                self.service.export_file("result.txt")
        self.assertEqual(str(caught.exception), "Dropbox rate limited; retry later")
        self.assertFalse(caught.exception.uncertain)
        self.assertIn("stage=failure_recording", " ".join(logs.output))
        self.assertNotIn("canary", " ".join(logs.output))

    def test_export_blocks_outside_internal_symlinks_oversize_and_nonclean(self):
        source = self.shared / "result.txt"
        source.write_bytes(b"hello")
        internal = self.shared / ".assistant"
        internal.mkdir()
        (internal / "secret.txt").write_bytes(b"secret")
        (self.shared / "link.txt").symlink_to(source)
        for path in ["../private/file", ".assistant/secret.txt", "link.txt", "."]:
            with self.subTest(path=path), self.assertRaises(DropboxError):
                self.service.export_file(path)
        self.service.client.max_bytes = 4
        with self.assertRaises(DropboxError):
            self.service.export_file("result.txt")
        self.service.client.max_bytes = 100
        self.service.scanner.scan_file.return_value = {"status": "error"}
        with self.assertRaises(DropboxError):
            self.service.export_file("result.txt")
        self.service.client.upload.assert_not_called()

    def test_delivery_permission_and_tool_availability(self):
        from assistant_agent.tools import DROPBOX_TOOL_NAMES, LOADABLE_TOOL_NAMES, available_function_names
        self.assertTrue(DROPBOX_TOOL_NAMES <= LOADABLE_TOOL_NAMES)
        self.assertTrue(DROPBOX_TOOL_NAMES <= available_function_names(self.config))
        self.config.values["agent"]["dropbox"]["allow_exports"] = False
        self.assertNotIn("dropbox_export", available_function_names(self.config))
        with self.assertRaises(DropboxError):
            self.service.export_file("result.txt")

    def test_repeat_import_checks_integrity_and_reconciles_interrupted_publication(self):
        with patch("assistant_agent.dropbox_files.WorkspaceIndex"):
            first = self.service.import_file("/Inbox/file.txt", "r1")
            self.db.fetch_one.return_value = {"status": "ready", "markdown_sha256": hashlib.sha256(b"converted content").hexdigest()}
            self.service.client.download.reset_mock()
            self.assertEqual(self.service.import_file("/Inbox/file.txt", "r1"), first)
            self.service.client.download.assert_not_called()
            self.db.fetch_one.return_value = {"status": "pending"}
            self.assertEqual(self.service.import_file("/Inbox/file.txt", "r1"), first)
            self.service.client.download.assert_called_once()

    def test_import_rejects_tampered_destination_without_overwrite(self):
        with patch("assistant_agent.dropbox_files.WorkspaceIndex"):
            result = self.service.import_file("/Inbox/file.txt", "r1")
            (self.shared / result["markdown_path"]).write_text("tampered")
            with self.assertRaises(DropboxError):
                self.service.import_file("/Inbox/file.txt", "r1")
            self.assertEqual((self.shared / result["markdown_path"]).read_text(), "tampered")

    def test_export_rejects_internal_custom_roots_credentials_and_unauthorized_names(self):
        cache = self.shared / "cache"
        cache.mkdir()
        (cache / "result.txt").write_text("private")
        self.config.values["agent"]["tool_result_cache"] = {"root": str(cache)}
        for name in ["POSTGRES_PASSWORD", "AGENT.md", "agent.yaml", "private.pem"]:
            (self.shared / name).write_text("private")
            with self.assertRaises(DropboxError):
                self.service.export_file(name)
        with self.assertRaises(DropboxError):
            self.service.export_file("cache/result.txt")
        source = self.shared / "result.txt"
        source.write_bytes(b"hello")
        for filename in ["../outside", "/absolute", ".hidden", "a/b", ""]:
            with self.assertRaises(DropboxError):
                self.service.export_file("result.txt", filename)
        self.service.client.upload.assert_not_called()

    def test_export_retry_binds_destination_to_original_bytes_and_records_uncertainty(self):
        source = self.shared / "result.txt"
        source.write_bytes(b"hello")
        self.service.client.upload.side_effect = DropboxError("uncertain", uncertain=True)
        with self.assertRaises(DropboxError):
            self.service.export_file("result.txt")
        self.assertEqual(self.db.execute.call_args.args[1][0], "uncertain")
        self.db.fetch_one.return_value = {"content_hash": content_hash(source), "status": "uncertain"}
        source.write_bytes(b"changed")
        self.service.client.upload.reset_mock()
        with self.assertRaises(DropboxError):
            self.service.export_file("result.txt")
        self.service.client.upload.assert_not_called()

    def test_runtime_dispatch_and_disabled_export(self):
        from assistant_agent.tools import ToolError, ToolRuntime
        runtime = ToolRuntime(self.db, self.config, {"id": 1})
        runtime._dropbox_service = self.service
        self.service.client.list_files.return_value = {"files": []}
        self.assertEqual(runtime.run("dropbox_list", {"folder": "Inbox"}), {"files": []})
        self.service.client.delete_file.return_value = {"deleted": True}
        self.assertEqual(runtime.run("dropbox_delete", {"folder": "Inbox", "path": "/Inbox/file.txt", "revision": "r1"}), {"deleted": True})
        self.service.client.delete_file.assert_called_once_with("/Inbox/file.txt", "r1", "Inbox")
        self.config.values["agent"]["dropbox"]["allow_exports"] = False
        with self.assertRaises(ToolError):
            runtime.run("dropbox_export", {"path": "result.txt"})

    def test_export_blocks_ancestor_symlink_swap_after_validation(self):
        folder = self.shared / "reports"
        folder.mkdir()
        (folder / "result.txt").write_bytes(b"hello")
        private = Path(self.directory.name) / "private-documents"
        private.mkdir()
        (private / "result.txt").write_bytes(b"secret-canary")
        original_open = os.open
        def swap(path, flags, *args, **kwargs):
            descriptor = original_open(path, flags, *args, **kwargs)
            if path == self.service.shared_root:
                folder.rename(self.shared / "old-reports")
                folder.symlink_to(private, target_is_directory=True)
            return descriptor
        with patch("assistant_agent.dropbox_files.os.open", side_effect=swap), self.assertRaises(DropboxError):
            self.service.export_file("reports/result.txt")
        self.service.client.upload.assert_not_called()


class DropboxSetupTest(unittest.TestCase):
    def test_launcher_detects_missing_oauth_http_dependency_before_setup(self):
        repository = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "src/scripts").mkdir(parents=True)
            (root / "data/.venv-setup/bin").mkdir(parents=True)
            shutil.copyfile(repository / "scripts/setup.sh", root / "src/scripts/setup.sh")
            executable = root / "data/.venv-setup/bin/python"
            executable.write_text('#!/bin/sh\ncase "$*" in *httpx*) exit 1 ;; esac\nexit 0\n')
            executable.chmod(0o755)
            result = subprocess.run(["sh", str(root / "src/scripts/setup.sh")], input="n\n", text=True, capture_output=True,
                                    env={**os.environ, "PYTHON": str(executable)}, check=True)
            self.assertIn("Install/update the isolated setup dependencies?", result.stdout)
            self.assertFalse((root / ".env").exists())

    def test_dropbox_section_cancel_does_not_save_connection_or_consent(self):
        import argparse
        import questionary
        from assistant_agent.setup import Installation, interactive
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.values = {"AGENT_LLM_MODEL": "model", "AGENT_LLM_BASE_URL": "https://openrouter.ai/api/v1"}
            instance.secret_values = {"POSTGRES_PASSWORD": "database-password", "OPENROUTER_API_KEY": "model-key"}
            instance.save()
            paths = [Path(root) / "data" / name for name in (".env", ".compose-secrets.yaml", ".secrets/state.json")]
            before = {path: path.read_bytes() for path in paths}
            responses = [False, False, True, True, "client", "authorization-code", True, "26214400", False, "cancel"]
            with patch("assistant_agent.setup.shutil.which", return_value="/usr/bin/docker"), patch.object(questionary.Question, "unsafe_ask", side_effect=responses), patch("assistant_agent.dropbox_provider.exchange_code", return_value="refresh-canary"):
                interactive(argparse.Namespace(root=Path(root), command=None, section="dropbox"))
            for path, data in before.items():
                self.assertEqual(path.read_bytes(), data)
            self.assertNotIn("DROPBOX_REFRESH_TOKEN", Installation(root).secret_values)
            self.assertFalse((Path(root) / "data/.secrets/DROPBOX_REFRESH_TOKEN").exists())

    def test_validation_requires_connection_and_bounds_file_limit(self):
        from assistant_agent.setup import Installation, SetupError, validate_draft
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.values = {"AGENT_LLM_MODEL": "model", "AGENT_LLM_BASE_URL": "https://openrouter.ai/api/v1", "DROPBOX_ENABLED": "true"}
            instance.secret_values = {"POSTGRES_PASSWORD": "database-password", "OPENROUTER_API_KEY": "model-key"}
            with self.assertRaisesRegex(SetupError, "DROPBOX_CLIENT_ID"):
                validate_draft(instance)
            instance.secret_values.update({"DROPBOX_CLIENT_ID": "client", "DROPBOX_REFRESH_TOKEN": "refresh"})
            validate_draft(instance)
            for limit in ["0", "invalid", "157286401"]:
                instance.values["DROPBOX_MAX_FILE_BYTES"] = limit
                with self.assertRaisesRegex(SetupError, "DROPBOX_MAX_FILE_BYTES"):
                    validate_draft(instance)

    def test_setup_stages_pkce_credentials_and_consent_without_writing_files(self):
        from assistant_agent.setup import Installation, prompt_dropbox
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            answers = iter(["client", "authorization-code", "26214400"])
            console = MagicMock()
            with patch("assistant_agent.dropbox_provider.exchange_code", return_value="refresh-canary") as exchange:
                prompt_dropbox(instance, lambda question: next(answers), MagicMock(side_effect=[True, True, True, False]), console)
            self.assertEqual(instance.secret_values["DROPBOX_REFRESH_TOKEN"], "refresh-canary")
            self.assertEqual(instance.values["DROPBOX_ALLOW_EXPORTS"], "true")
            self.assertEqual(exchange.call_args.args[:2], ("client", "authorization-code"))
            self.assertFalse((Path(root) / ".env").exists())
            self.assertNotIn("refresh-canary", str(console.mock_calls))
            instance.save()
            reloaded = Installation(root)
            self.assertEqual(reloaded.secret_values, instance.secret_values)
            self.assertEqual((Path(root) / "data/.secrets/DROPBOX_REFRESH_TOKEN").stat().st_mode & 0o777, 0o600)
            self.assertIn("DROPBOX_REFRESH_TOKEN", reloaded.overlay()["secrets"])

    def test_disable_keeps_or_removes_connection_and_stops_mounting_secrets(self):
        from assistant_agent.setup import Installation, prompt_dropbox
        for remove in [False, True]:
            with tempfile.TemporaryDirectory() as root:
                instance = Installation(root)
                instance.values = {"DROPBOX_ENABLED": "true", "DROPBOX_ALLOW_EXPORTS": "true"}
                instance.secret_values = {"DROPBOX_CLIENT_ID": "client", "DROPBOX_REFRESH_TOKEN": "refresh"}
                instance.save()
                prompt_dropbox(instance, MagicMock(), MagicMock(side_effect=[False, remove]), MagicMock())
                self.assertNotIn("DROPBOX_REFRESH_TOKEN", instance.overlay()["secrets"])
                instance.save()
                reloaded = Installation(root)
                self.assertEqual(reloaded.values["DROPBOX_ALLOW_EXPORTS"], "false")
                self.assertEqual("DROPBOX_REFRESH_TOKEN" in reloaded.secret_values, not remove)

    def test_failed_exchange_preserves_saved_credentials_and_env(self):
        from assistant_agent.setup import Installation, SetupError, prompt_dropbox
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.secret_values = {"DROPBOX_CLIENT_ID": "old-client", "DROPBOX_REFRESH_TOKEN": "old-refresh"}
            instance.save()
            original = instance.env_original
            answers = iter(["Reconnect", "new-client", "code"])
            with patch("assistant_agent.dropbox_provider.exchange_code", side_effect=DropboxError("authorization failed")), self.assertRaises(SetupError):
                prompt_dropbox(instance, lambda question: next(answers), MagicMock(side_effect=[True, True]), MagicMock())
            self.assertEqual(instance.secret_values["DROPBOX_REFRESH_TOKEN"], "old-refresh")
            self.assertEqual(Installation(root).env_original, original)

    def test_read_only_verification_and_config_file_secrets(self):
        from assistant_agent.config import load_config
        from assistant_agent.setup import Installation, prompt_dropbox
        with tempfile.TemporaryDirectory() as root:
            instance = Installation(root)
            instance.secret_values = {"DROPBOX_CLIENT_ID": "client", "DROPBOX_REFRESH_TOKEN": "refresh"}
            answers = iter(["Keep existing", "26214400"])
            with patch.object(DropboxClient, "token", return_value="access") as token, patch.object(DropboxClient, "request") as request:
                prompt_dropbox(instance, lambda question: next(answers), MagicMock(side_effect=[True, True, True]), MagicMock())
            token.assert_called_once_with(force=True)
            request.assert_not_called()
            instance.save()
            with patch.dict(os.environ, {"AGENT_CONFIG": str(Path(root) / "missing.yaml"), "DROPBOX_ENABLED": "true", "DROPBOX_ALLOW_EXPORTS": "true",
                                        "DROPBOX_CLIENT_ID_FILE": str(Path(root) / "data/.secrets/DROPBOX_CLIENT_ID"),
                                        "DROPBOX_REFRESH_TOKEN_FILE": str(Path(root) / "data/.secrets/DROPBOX_REFRESH_TOKEN")}, clear=True):
                config = load_config()
            self.assertEqual(config.get("agent.dropbox.refresh_token"), "refresh")
            self.assertTrue(config.get_bool("agent.dropbox.allow_exports"))


@unittest.skipUnless(os.getenv("ASSISTANT_TEST_DATABASE_URL"), "requires disposable assistant_test database")
class DropboxDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import make_conninfo
        from assistant_agent.database import Database
        cls.original_db = Database(os.environ["ASSISTANT_TEST_DATABASE_URL"])
        cls.schema = "test_" + uuid.uuid4().hex
        with cls.original_db.connect() as conn:
            if conn.info.dbname != "assistant_test":
                raise RuntimeError("integration tests require a disposable database named assistant_test")
            conn.execute("CREATE SCHEMA " + cls.schema)
        cls.db = Database(make_conninfo(os.environ["ASSISTANT_TEST_DATABASE_URL"], options="-c search_path=" + cls.schema))
        cls.db.execute(Path(__file__).resolve().parents[2].joinpath("database/schema.sql").read_text())
        cls.db.ensure_feature_schema()
        cls.db.ensure_feature_schema()
        cls.db.run_migrations()

    @classmethod
    def tearDownClass(cls):
        cls.original_db.execute("DROP SCHEMA " + cls.schema + " CASCADE")

    def service(self, root):
        from assistant_agent.dropbox_files import DropboxFiles
        job = self.db.fetch_one("INSERT INTO jobs(thread_id) VALUES (%s) RETURNING *", ("dropbox-" + uuid.uuid4().hex,))
        config = AppConfig({"agent": {"dropbox": {"enabled": True, "allow_exports": True, "client_id": "client", "refresh_token": "refresh"},
                                      "filesystem": {"shared_root": str(Path(root) / "share")}, "artifacts": {"raw_root": str(Path(root) / "private")},
                                      "workspace": {"index": {"enabled": False}}}})
        service = DropboxFiles(self.db, config, job)
        service.shared_root.mkdir()
        service.scanner = MagicMock()
        service.scanner.scan_file.return_value = {"status": "clean"}
        service.scanner.is_supported_attachment.return_value = True
        service.scanner.extractor.convert_to_markdown.return_value = "document"
        service.client = MagicMock(identity="test-connection", max_bytes=100)
        metadata = {"id": "id:1", "rev": "r1", "path": "/Inbox/file.txt", "size": 5,
                    "content_hash": hashlib.sha256(hashlib.sha256(b"hello").digest()).hexdigest()}
        service.client.metadata.return_value = metadata
        service.client.download.side_effect = lambda metadata, path: path.write_bytes(b"hello")
        return service, metadata

    def test_import_and_export_persist_retry_identity_and_keep_workstream_ownership(self):
        with tempfile.TemporaryDirectory() as root:
            service, metadata = self.service(root)
            result = service.import_file("/Inbox/file.txt", "r1")
            service.client.download.reset_mock()
            self.assertEqual(service.import_file("/Inbox/file.txt", "r1"), result)
            service.client.download.assert_not_called()
            imported = self.db.fetch_one("SELECT * FROM dropbox_transfers WHERE job_id = %s", (service.job["id"],))
            self.assertEqual(imported["status"], "ready")
            self.assertEqual(imported["scan_status"], "clean")
            self.assertEqual(imported["conversion_status"], "ready")
            source = service.shared_root / "result.txt"
            source.write_bytes(b"hello")
            service.client.upload.side_effect = DropboxError("uncertain", uncertain=True)
            with self.assertRaises(DropboxError):
                service.export_file("result.txt")
            row = self.db.fetch_one("SELECT * FROM dropbox_transfers WHERE job_id = %s AND direction = 'export'", (service.job["id"],))
            self.assertEqual(row["status"], "uncertain")
            service.client.upload.side_effect = None
            service.client.upload.return_value = {**metadata, "path": "/Outbox/job-%s/result.txt" % service.job["id"]}
            service.export_file("result.txt")
            saved = self.db.fetch_one("SELECT * FROM dropbox_transfers WHERE request_key = %s", (row["request_key"],))
            self.assertEqual(saved["status"], "ready")
            self.assertEqual(saved["local_path"], str(source))
            self.assertEqual(saved["content_hash"], content_hash(source))
            self.db.execute("DELETE FROM jobs WHERE id = %s", (service.job["id"],))
            self.assertEqual(self.db.fetch_all("SELECT * FROM dropbox_transfers WHERE job_id = %s", (service.job["id"],)), [])
            retained = self.db.fetch_one("SELECT * FROM dropbox_transfers WHERE request_key = %s", (row["request_key"],))
            self.assertIsNone(retained["job_id"])
            self.assertIsNotNone(retained["object_id"])
            self.assertTrue(source.exists())

    def test_export_staging_failure_persists_failed_without_upload(self):
        with tempfile.TemporaryDirectory() as root:
            service, metadata = self.service(root)
            (service.shared_root / "result.txt").write_bytes(b"hello")
            with patch.object(Path, "mkdir", side_effect=OSError(errno.EROFS, "secret-canary")), self.assertLogs("assistant.dropbox", level="ERROR"):
                with self.assertRaises(DropboxError) as caught:
                    service.export_file("result.txt")
            row = self.db.fetch_one("SELECT * FROM dropbox_transfers WHERE job_id = %s AND direction = 'export'", (service.job["id"],))
            self.assertEqual(row["status"], "failed")
            self.assertIn("private staging is not writable", row["error"])
            self.assertNotIn("canary", row["error"])
            self.assertFalse(caught.exception.uncertain)
            self.assertIsNone(row["content_hash"])
            service.scanner.scan_file.assert_not_called()
            service.client.upload.assert_not_called()

    def test_failure_states_and_email_ownership_remain_separate(self):
        with tempfile.TemporaryDirectory() as root:
            service, metadata = self.service(root)
            service.scanner.scan_file.return_value = {"status": "infected", "allow_conversion": False}
            with self.assertRaises(DropboxError):
                service.import_file("/Inbox/file.txt", "r1")
            row = self.db.fetch_one("SELECT * FROM dropbox_transfers WHERE job_id = %s", (service.job["id"],))
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["scan_status"], "infected")
            self.assertIsNone(row["local_path"])
            self.assertFalse(list(service.shared_root.iterdir()))
            ownership = self.db.fetch_one("SELECT is_nullable FROM information_schema.columns WHERE table_schema = %s AND table_name = 'processed_artifacts' AND column_name = 'email_id'", (self.schema,))
            self.assertEqual(ownership["is_nullable"], "YES")
            self.assertEqual(self.db.fetch_one("SELECT count(*) AS count FROM emails")["count"], 0)