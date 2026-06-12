import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda handle: {}))
psycopg_module = types.ModuleType("psycopg")
psycopg_module.connect = lambda *args, **kwargs: None
psycopg_module.Connection = object
sys.modules.setdefault("psycopg", psycopg_module)
rows_module = types.ModuleType("psycopg.rows")
rows_module.dict_row = object()
sys.modules.setdefault("psycopg.rows", rows_module)
json_module = types.ModuleType("psycopg.types.json")
json_module.Jsonb = lambda value: value
sys.modules.setdefault("psycopg.types", types.ModuleType("psycopg.types"))
sys.modules.setdefault("psycopg.types.json", json_module)
markdown_module = types.ModuleType("markdown_it")


class FakeMarkdownIt:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def enable(self, *args: Any, **kwargs: Any) -> "FakeMarkdownIt":
        return self

    def render(self, value: str) -> str:
        return value


markdown_module.MarkdownIt = FakeMarkdownIt
sys.modules.setdefault("markdown_it", markdown_module)

from assistant_agent.config import AppConfig
from assistant_agent.tools import ToolRuntime, available_function_names, tool_catalog
from assistant_agent.workspace_index import WorkspaceIndex, WorkspaceNormalizer


def workspace_config(temp_dir: str, embeddings_enabled: bool = True) -> AppConfig:
    return AppConfig(
        {
            "agent": {
                "filesystem": {"shared_root": temp_dir, "require_mount": False},
                "tool_result_cache": {"root": ".cache/tool-results"},
                "embeddings": {"enabled": embeddings_enabled, "model": "test-embed"},
                "workspace": {
                    "normalize_documents": True,
                    "source_archive_root": ".assistant/archive/source-documents",
                    "index": {"enabled": True, "chunk_chars": 1200, "candidate_limit": 100},
                },
                "projects": {"enabled": False},
                "deep_research": {"enabled": False},
            }
        }
    )


class FakeNoteDatabase:
    def __init__(self) -> None:
        self.notes: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.next_id = 1

    def row(self, values: dict[str, Any]) -> dict[str, Any]:
        row = {
            "id": self.next_id,
            "title": "Untitled note",
            "content": "",
            "tags": [],
            "embedding": None,
            "embedding_model": None,
            "embedding_dimensions": None,
            "embedding_updated_at": None,
            "source_job_id": None,
            "metadata": {},
            "last_accessed_at": None,
            "created_at": "2026-06-09T00:00:00Z",
            "updated_at": "2026-06-09T00:00:00Z",
        }
        row.update(values)
        return row

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        normalized = " ".join(sql.split())
        if normalized.startswith("INSERT INTO agent_notes"):
            title, content, tags, embedding, embedding_model, embedding_dimensions, embedding_updated_at, source_job_id, metadata = params
            row = self.row(
                {
                    "title": title,
                    "content": content,
                    "tags": tags,
                    "embedding": embedding,
                    "embedding_model": embedding_model,
                    "embedding_dimensions": embedding_dimensions,
                    "embedding_updated_at": embedding_updated_at,
                    "source_job_id": source_job_id,
                    "metadata": metadata,
                }
            )
            self.next_id += 1
            self.notes.append(row)
            return row
        if "FROM agent_notes WHERE id = %s" in normalized:
            note_id = int(params[0])
            return next((note for note in self.notes if int(note["id"]) == note_id), None)
        if normalized.startswith("UPDATE agent_notes"):
            note_id = int(params[-1])
            note = next((item for item in self.notes if int(item["id"]) == note_id), None)
            if note is None:
                return None
            title, content, tags, has_metadata, metadata = params[:5]
            if title is not None:
                note["title"] = title
            if content is not None:
                note["content"] = content
            if tags is not None:
                note["tags"] = tags
            if has_metadata:
                note["metadata"] = metadata
            note["updated_at"] = "2026-06-09T01:00:00Z"
            return note
        if normalized.startswith("DELETE FROM agent_notes"):
            note_id = int(params[0])
            for index, note in enumerate(self.notes):
                if int(note["id"]) == note_id:
                    return self.notes.pop(index)
            return None
        return None

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        normalized = " ".join(sql.split())
        if "FROM agent_notes" not in normalized:
            return []
        if "embedding IS NOT NULL" in normalized:
            return [dict(note) for note in self.notes if note.get("embedding") is not None]
        query = ""
        if params and isinstance(params[0], str):
            query = params[0].strip("%").lower()
        rows = []
        for note in self.notes:
            haystack = "%s %s %s" % (note["title"], note["content"], " ".join(note["tags"]))
            if not query or query in haystack.lower():
                rows.append(dict(note))
        return rows

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        normalized = " ".join(sql.split())
        if normalized.startswith("INSERT INTO note_events"):
            self.events.append({"note_id": params[0], "event_type": params[3]})
        if normalized.startswith("UPDATE agent_notes SET last_accessed_at"):
            note_id = int(params[0])
            for note in self.notes:
                if int(note["id"]) == note_id:
                    note["last_accessed_at"] = "touched"


class FakeConversionDatabase:
    def __init__(self) -> None:
        self.conversions: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        if "INSERT INTO workspace_document_conversions" in sql:
            self.conversions.append(params)


class FakeWorkspaceDatabase:
    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.chunks: list[dict[str, Any]] = []
        self.next_file_id = 1

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        normalized = " ".join(sql.split())
        if "FROM workspace_files WHERE relative_path = %s" in normalized and normalized.startswith("SELECT *"):
            return self.files.get(str(params[0]))
        if "INSERT INTO workspace_files" in normalized:
            relative = str(params[0])
            row = self.files.get(relative)
            if row is None:
                row = {"id": self.next_file_id, "relative_path": relative, "created_at": "2026-06-09T00:00:00Z"}
                self.next_file_id += 1
            row.update(
                {
                    "size_bytes": params[1],
                    "mtime_ns": params[2],
                    "content_sha256": params[3],
                    "mime_type": params[4],
                    "extension": params[5],
                    "index_status": params[6],
                    "error": params[7],
                    "metadata": params[8],
                    "updated_at": "2026-06-09T01:00:00Z",
                }
            )
            self.files[relative] = row
            return row
        if "SELECT id FROM workspace_files WHERE relative_path = %s" in normalized:
            row = self.files.get(str(params[0]))
            return {"id": row["id"]} if row else None
        return None

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT relative_path FROM workspace_files"):
            return [{"relative_path": path} for path, row in self.files.items() if row.get("index_status") != "deleted"]
        if "FROM workspace_file_chunks c JOIN workspace_files f" in normalized:
            rows = []
            keyword = ""
            if "ILIKE" in normalized:
                keyword = str(params[2]).strip("%").lower()
            for chunk in self.chunks:
                file_row = next((item for item in self.files.values() if item["id"] == chunk["file_id"]), None)
                if not file_row or file_row.get("index_status") not in {"indexed", "embedding_failed"}:
                    continue
                if "c.embedding IS NOT NULL" in normalized and chunk.get("embedding") is None:
                    continue
                if keyword and keyword not in chunk["content"].lower() and keyword not in file_row["relative_path"].lower():
                    continue
                rows.append({**chunk, "relative_path": file_row["relative_path"], "size_bytes": file_row["size_bytes"], "file_updated_at": file_row["updated_at"]})
            return rows
        return []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        normalized = " ".join(sql.split())
        if normalized.startswith("DELETE FROM workspace_file_chunks"):
            file_id = int(params[0])
            self.chunks = [chunk for chunk in self.chunks if int(chunk["file_id"]) != file_id]
        elif normalized.startswith("INSERT INTO workspace_file_chunks"):
            (
                file_id,
                chunk_index,
                content,
                start_line,
                end_line,
                content_sha256,
                embedding,
                embedding_model,
                embedding_dimensions,
                embedding_updated_at,
            ) = params
            self.chunks.append(
                {
                    "file_id": file_id,
                    "chunk_index": chunk_index,
                    "content": content,
                    "start_line": start_line,
                    "end_line": end_line,
                    "content_sha256": content_sha256,
                    "embedding": embedding,
                    "embedding_model": embedding_model,
                    "embedding_dimensions": embedding_dimensions,
                    "embedding_updated_at": embedding_updated_at,
                }
            )
        elif normalized.startswith("UPDATE workspace_files SET index_status = 'deleted'"):
            file_id = int(params[0])
            for row in self.files.values():
                if int(row["id"]) == file_id:
                    row["index_status"] = "deleted"
        elif normalized.startswith("UPDATE workspace_files SET index_status = 'superseded'"):
            file_id = int(params[1])
            for row in self.files.values():
                if int(row["id"]) == file_id:
                    row["index_status"] = "superseded"
                    row["metadata"] = params[0]


class NotesAndWorkspaceIndexTest(unittest.TestCase):
    def test_note_tools_are_loadable_and_do_not_replace_memory_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = workspace_config(temp_dir)
            available = available_function_names(config, {"id": 7, "thread_id": "thread"})

            self.assertIn("note_create", available)
            self.assertIn("note_search", tool_catalog(config, {"id": 7, "thread_id": "thread"}))

    def test_note_tool_crud_uses_explicit_search_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = workspace_config(temp_dir)
            db = FakeNoteDatabase()
            runtime = ToolRuntime(db, config, {"id": 42, "thread_id": "thread-1"})  # type: ignore[arg-type]

            with patch("assistant_agent.embedding_client.EmbeddingClient.embed", return_value=[1.0, 0.0]):
                created = runtime.note_create("Investigate the workspace index after saving files.", title="Workspace index", tags=["workspace"])
                found = runtime.note_search("saving files")
                read = runtime.note_read(created["note"]["id"])
                updated = runtime.note_update(created["note"]["id"], title="Workspace index notes")
                deleted = runtime.note_delete(created["note"]["id"])

            self.assertEqual(created["note"]["title"], "Workspace index")
            self.assertEqual(found["notes"][0]["id"], created["note"]["id"])
            self.assertIn("workspace index", read["note"]["content"])
            self.assertEqual(updated["note"]["title"], "Workspace index notes")
            self.assertEqual(deleted["deleted"]["id"], created["note"]["id"])

    def test_document_converter_creates_markdown_without_archiving_original(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "docs" / "brief.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"%PDF fake")
            db = FakeConversionDatabase()
            normalizer = WorkspaceNormalizer(db, workspace_config(temp_dir))  # type: ignore[arg-type]

            with patch("assistant_agent.document_text.DocumentTextExtractor.convert_to_markdown", return_value="# Brief\n\nConverted."):
                result = normalizer.convert_to_markdown(source, source="upload")

            self.assertTrue(result.converted)
            self.assertEqual(result.path.relative_to(root), Path("docs/brief.md"))
            self.assertTrue(source.exists())
            self.assertEqual(result.path.read_text(encoding="utf-8"), "# Brief\n\nConverted.")
            self.assertIsNone(result.archived_path)
            self.assertTrue(db.conversions)
            self.assertEqual([row[5] for row in db.conversions], ["pending", "ready"])

    def test_file_tool_index_hook_indexes_pdf_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "upload.pdf"
            source.write_bytes(b"%PDF fake")
            db = FakeWorkspaceDatabase()
            runtime = ToolRuntime(
                db,  # type: ignore[arg-type]
                workspace_config(temp_dir, embeddings_enabled=False),
                {"id": 42, "thread_id": "thread-1"},
            )

            with patch("assistant_agent.document_text.DocumentTextExtractor.convert_to_markdown", return_value="Converted PDF text"):
                result = runtime.normalize_and_index_path(source, source="upload")

            self.assertEqual(result, source.resolve())
            self.assertTrue(source.exists())
            self.assertFalse((root / "upload.md").exists())
            self.assertEqual(db.files["upload.pdf"]["index_status"], "indexed")
            self.assertEqual(db.chunks[0]["content"], "Converted PDF text")

    def test_file_convert_to_markdown_supersedes_original_index_without_deleting_original(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "upload.pdf"
            source.write_bytes(b"%PDF fake")
            db = FakeWorkspaceDatabase()
            runtime = ToolRuntime(
                db,  # type: ignore[arg-type]
                workspace_config(temp_dir, embeddings_enabled=False),
                {"id": 42, "thread_id": "thread-1"},
            )

            with patch("assistant_agent.document_text.DocumentTextExtractor.convert_to_markdown", return_value="# Converted"):
                result = runtime.file_convert("upload.pdf", "markdown")

            self.assertEqual(result["relative_path"], "upload.md")
            self.assertTrue(source.exists())
            self.assertEqual((root / "upload.md").read_text(encoding="utf-8"), "# Converted")
            self.assertEqual(db.files["upload.pdf"]["index_status"], "superseded")
            self.assertEqual(db.files["upload.md"]["index_status"], "indexed")

    def test_file_convert_uses_pandoc_and_appends_duplicate_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "note.md"
            source.write_text("# Note", encoding="utf-8")
            (root / "note.html").write_text("existing", encoding="utf-8")
            db = FakeWorkspaceDatabase()
            runtime = ToolRuntime(
                db,  # type: ignore[arg-type]
                workspace_config(temp_dir, embeddings_enabled=False),
                {"id": 42, "thread_id": "thread-1"},
            )

            def fake_run(command: list[str], **_: Any) -> Any:
                output = Path(command[command.index("-o") + 1])
                self.assertEqual(output.suffix, ".html")
                output.write_text("<h1>Note</h1>", encoding="utf-8")
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("assistant_agent.file_conversion.shutil.which", return_value="/usr/bin/pandoc"), patch(
                "assistant_agent.file_conversion.subprocess.run",
                side_effect=fake_run,
            ) as run:
                result = runtime.file_convert("note.md", "html")

            command = run.call_args[0][0]
            self.assertEqual(result["relative_path"], "note-1.html")
            self.assertEqual((root / "note-1.html").read_text(encoding="utf-8"), "<h1>Note</h1>")
            self.assertEqual(command[0], "/usr/bin/pandoc")
            self.assertEqual(command[command.index("--from") + 1], "gfm")
            self.assertEqual(command[command.index("--to") + 1], "html5")
            self.assertEqual(db.files["note-1.html"]["index_status"], "indexed")

    def test_workspace_index_embeds_chunks_and_semantic_search_returns_snippets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "docs" / "plan.md"
            path.parent.mkdir(parents=True)
            path.write_text("Launch checklist\n\nVerify save-as updates the embedding index.", encoding="utf-8")
            db = FakeWorkspaceDatabase()
            index = WorkspaceIndex(db, workspace_config(temp_dir))  # type: ignore[arg-type]

            with patch("assistant_agent.embedding_client.EmbeddingClient.embed", return_value=[1.0, 0.0]):
                row = index.index_path(path)
                matches = index.search("embedding index", limit=5)

            self.assertEqual(row["index_status"], "indexed")
            self.assertEqual(db.chunks[0]["embedding"], [1.0, 0.0])
            self.assertEqual(matches[0]["relative_path"], "docs/plan.md")
            self.assertIn("embedding index", matches[0]["snippet"].lower())


if __name__ == "__main__":
    unittest.main()
