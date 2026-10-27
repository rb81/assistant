import errno
import hashlib
import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path

from .artifact_processor import ArtifactProcessor
from .dropbox_provider import DropboxClient, DropboxError, content_hash
from .secret_store import SECRET_NAMES
from .threading import safe_filename
from .workspace_index import WorkspaceIndex
from .workstream_files import WorkstreamFileTracker


LOGGER = logging.getLogger("assistant.dropbox")


class DropboxFiles:
    def __init__(self, db, config, job):
        self.db, self.config, self.job = db, config, job
        self.workstream_files = WorkstreamFileTracker(db, config, job.get("id"))
        self.client = DropboxClient(config, db)
        self.scanner = ArtifactProcessor(db, config)
        self.shared_root = Path(config.get("agent.filesystem.shared_root", "/data/share")).resolve()
        self.private_root = (Path(config.get("agent.artifacts.raw_root", "/data/private/artifacts/raw")) / "dropbox").resolve()
        if self.private_root == self.shared_root or self.shared_root in self.private_root.parents:
            raise DropboxError("Dropbox quarantine must be outside the shared workspace")
        if not isinstance(job.get("id"), int) or job["id"] < 1:
            raise DropboxError("Dropbox transfers require a persisted task")

    def scan(self, path, key=None):
        result = self.scanner.scan_file(path)
        if key:
            status = result.get("status", "error")
            status = status if status in {"clean", "infected", "error", "skipped", "pending"} else "error"
            self.db.execute("UPDATE dropbox_transfers SET scan_status = %s, updated_at = now() WHERE request_key = %s", (status, key))
        if result.get("status") != "clean":
            raise DropboxError("Dropbox transfer blocked: malware scan was not clean (%s)" % result.get("status", "error"))

    def key(self, direction, identity):
        return hashlib.sha256((self.client.identity + ":" + str(self.job["id"]) + ":" + direction + ":" + identity).encode()).hexdigest()

    def begin(self, key, direction, remote_path, remote_id=None, revision=None):
        self.db.execute("""INSERT INTO dropbox_transfers(request_key, job_id, thread_id, direction, remote_path, remote_id, revision)
                           VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (request_key) DO NOTHING""",
                        (key, self.job["id"], self.job.get("thread_id"), direction, remote_path, remote_id, revision))
        return self.db.fetch_one("SELECT * FROM dropbox_transfers WHERE request_key = %s", (key,))

    def ready(self, key, metadata, local_path, markdown_path=None, source_path=None):
        digest = hashlib.sha256(local_path.read_bytes()).hexdigest()
        markdown_digest = hashlib.sha256(markdown_path.read_bytes()).hexdigest() if markdown_path else None
        self.db.execute("""UPDATE dropbox_transfers SET status = 'ready', scan_status = 'clean', conversion_status = %s,
                           remote_id = %s, revision = %s, local_path = %s, markdown_path = %s, local_sha256 = %s,
                           markdown_sha256 = %s, content_hash = %s, size_bytes = %s, error = NULL, updated_at = now() WHERE request_key = %s""",
                        ("ready" if markdown_path else "not_applicable", metadata["id"], metadata["rev"], str(source_path or local_path),
                         str(markdown_path) if markdown_path else None, digest, markdown_digest, metadata["content_hash"], metadata["size"], key))

    def failed(self, key, message, uncertain=False):
        self.db.execute("UPDATE dropbox_transfers SET status = %s, error = %s, updated_at = now() WHERE request_key = %s",
                        ("uncertain" if uncertain else "failed", message, key))

    def log_export_failure(self, key, stage, exception):
        sqlstate = getattr(exception, "sqlstate", None)
        if not isinstance(sqlstate, str) or len(sqlstate) != 5 or not sqlstate.isascii() or not sqlstate.isalnum():
            sqlstate = None
        LOGGER.error("Dropbox export failed: job_id=%s request_key=%s stage=%s exception=%s errno=%s sqlstate=%s",
                     self.job["id"], key, stage, type(exception).__name__,
                     exception.errno if isinstance(exception, OSError) else None, sqlstate)

    def public_import(self, path, markdown, metadata):
        return {"path": str(path.relative_to(self.shared_root)), "markdown_path": str(markdown.relative_to(self.shared_root)),
                "source": "dropbox", "remote_path": metadata["path"], "revision": metadata["rev"], "scan_status": "clean", "untrusted": True}

    def import_file(self, path, revision):
        metadata = self.client.metadata(path)
        if not isinstance(revision, str) or not revision or metadata["rev"] != revision:
            raise DropboxError("Dropbox file revision changed; list again before importing")
        name = safe_filename(Path(metadata["path"]).name)
        if not self.scanner.is_supported_attachment(Path(name).suffix.lower()) or Path(name).suffix.lower() in {".zip", ".tar", ".gz"}:
            raise DropboxError("unsupported Dropbox document extension")
        if metadata["size"] > self.client.max_bytes:
            raise DropboxError("Dropbox file exceeds transfer limit")
        key = self.key("import", metadata["id"] + ":" + revision)
        parent = self.shared_root / "imports" / "dropbox" / ("job-%s" % self.job["id"])
        destination = parent / key
        if not destination.resolve().is_relative_to(self.shared_root) or destination.resolve() != destination:
            raise DropboxError("Dropbox import directory must not contain symlinks")
        original, markdown = destination / "original" / name, destination / "content.md"
        with self.db.connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("dropbox:" + key,))
            row = self.begin(key, "import", metadata["path"], metadata["id"], revision)
            if row and row["status"] == "ready" and original.is_file() and markdown.is_file():
                if content_hash(original) == metadata["content_hash"] and hashlib.sha256(markdown.read_bytes()).hexdigest() == row.get("markdown_sha256"):
                    self.scan(original, key)
                    self.scan(markdown, key)
                    self.workstream_files.track_file(original)
                    self.workstream_files.track_file(markdown)
                    WorkspaceIndex(self.db, self.config).index_path_best_effort(markdown, source="dropbox")
                    return self.public_import(original, markdown, metadata)
            try:
                self.private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tempfile.TemporaryDirectory(prefix="import-", dir=self.private_root) as staging:
                    source = Path(staging) / name
                    self.client.download(metadata, source)
                    self.scan(source, key)
                    try:
                        text = self.scanner.extractor.convert_to_markdown(source)
                    except Exception:
                        self.db.execute("UPDATE dropbox_transfers SET conversion_status = 'failed', updated_at = now() WHERE request_key = %s", (key,))
                        raise DropboxError("Dropbox document conversion failed; no file published") from None
                    if len(text.encode("utf-8")) > self.client.max_bytes:
                        raise DropboxError("converted Dropbox document exceeds transfer limit")
                    converted = Path(staging) / "converted.md"
                    converted.write_text(text, encoding="utf-8")
                    self.scan(converted, key)
                    parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists():
                        if not original.is_file() or not markdown.is_file() or content_hash(original) != metadata["content_hash"] or markdown.read_bytes() != converted.read_bytes():
                            raise DropboxError("Dropbox import destination changed; remove the old import before retrying")
                    else:
                        self.workstream_files.register_path(original)
                        self.workstream_files.register_path(markdown)
                        with tempfile.TemporaryDirectory(prefix=".publish-", dir=parent) as publication:
                            bundle = Path(publication) / "bundle"
                            (bundle / "original").mkdir(parents=True)
                            shutil.copyfile(source, bundle / "original" / name)
                            shutil.copyfile(converted, bundle / "content.md")
                            os.rename(bundle, destination)
                    self.workstream_files.track_file(original)
                    self.workstream_files.track_file(markdown)
                self.ready(key, metadata, original, markdown)
                WorkspaceIndex(self.db, self.config).index_path_best_effort(markdown, source="dropbox")
                return self.public_import(original, markdown, metadata)
            except DropboxError as exc:
                self.failed(key, str(exc))
                raise
            except Exception:
                self.failed(key, "Dropbox import failed")
                raise DropboxError("Dropbox import failed; retry the same import") from None

    def open_source(self, relative):
        directory = os.open(self.shared_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for segment in relative.parts[:-1]:
                child = os.open(segment, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            return os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)

    def export_file(self, path, filename=None):
        if not self.config.get_bool("agent.dropbox.allow_exports", False):
            raise DropboxError("Dropbox Outbox delivery is not authorized; enable it in setup")
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.shared_root / candidate
        source = candidate.resolve()
        if not source.is_relative_to(self.shared_root) or source == self.shared_root:
            raise DropboxError("Dropbox exports must stay inside the shared workspace")
        relative = source.relative_to(self.shared_root)
        if any(part.startswith(".") for part in relative.parts) or candidate.absolute() != source:
            raise DropboxError("Dropbox exports cannot use hidden, internal or symlink paths")
        if source.name.upper() in SECRET_NAMES or source.name.casefold() in {"agent.md", "agent.yaml", "docker-compose.yml"} or source.suffix.lower() in {".pem", ".key"}:
            raise DropboxError("Dropbox exports cannot use credential or assistant configuration files")
        configured_prompt = Path(str(self.config.get("agent.prompt.agent_file", "AGENT.md")))
        if not configured_prompt.is_absolute():
            configured_prompt = self.shared_root / configured_prompt
        if source == configured_prompt.resolve():
            raise DropboxError("Dropbox exports cannot use assistant instructions")
        for protected in ("agent.tool_result_cache.root", "agent.workspace.source_archive_root", "agent.filesystem.trash_directory"):
            value = self.config.get(protected)
            if value:
                protected_path = Path(value)
                if not protected_path.is_absolute():
                    protected_path = self.shared_root / protected_path
                if source.is_relative_to(protected_path.resolve()):
                    raise DropboxError("Dropbox exports cannot use internal storage")
        if not source.is_file():
            raise DropboxError("Dropbox export requires a regular workspace file")
        name = filename if filename is not None else source.name
        if not isinstance(name, str) or not name or name.startswith(".") or safe_filename(name) != name or Path(name).name != name:
            raise DropboxError("Dropbox output filename must be a plain safe filename")
        destination = "/Outbox/job-%s/%s" % (self.job["id"], name)
        key = self.key("export", destination.casefold())
        with self.db.connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("dropbox:" + key,))
            row = self.begin(key, "export", destination)
            stage = "staging"
            uncertain = bool(row and (row.get("status") == "uncertain" or
                                     (row.get("status") == "pending" and row.get("content_hash"))))
            try:
                self.private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tempfile.TemporaryDirectory(prefix="export-", dir=self.private_root) as staging:
                    stage = "snapshot"
                    snapshot = Path(staging) / name
                    descriptor = self.open_source(relative)
                    with os.fdopen(descriptor, "rb") as handle, snapshot.open("xb") as output:
                        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                            raise DropboxError("Dropbox export requires a regular workspace file")
                        total = 0
                        while chunk := handle.read(65536):
                            total += len(chunk)
                            if total > self.client.max_bytes:
                                raise DropboxError("Dropbox file exceeds transfer limit")
                            output.write(chunk)
                    digest = content_hash(snapshot)
                    if row and row.get("content_hash") and row["content_hash"] != digest:
                        raise DropboxError("Dropbox output belongs to different bytes; choose a different filename")
                    stage = "scan"
                    self.scan(snapshot, key)
                    stage = "persistence"
                    self.db.execute("UPDATE dropbox_transfers SET content_hash = %s, status = 'pending', scan_status = 'clean', updated_at = now() WHERE request_key = %s", (digest, key))
                    stage = "upload"
                    uncertain = True
                    metadata = self.client.upload(snapshot, destination)
                    stage = "finalization"
                    self.ready(key, metadata, snapshot, source_path=source)
                    result = {"remote_path": metadata["path"], "revision": metadata["rev"], "size_bytes": metadata["size"], "scan_status": "clean"}
                    stage = "cleanup"
                return result
            except Exception as exc:
                self.log_export_failure(key, stage, exc)
                if isinstance(exc, DropboxError):
                    message, uncertain = str(exc), exc.uncertain
                elif stage == "staging" and isinstance(exc, OSError) and exc.errno in {errno.EROFS, errno.EACCES, errno.EPERM}:
                    message = "Dropbox export private staging is not writable; configure a writable private Dropbox mount for task-agent"
                else:
                    message = "Dropbox export failed during %s; %s" % (
                        stage, "retry the same export to reconcile" if uncertain else "check task-agent configuration and logs")
                try:
                    self.failed(key, message, uncertain=uncertain)
                except Exception as recording_error:
                    self.log_export_failure(key, "failure_recording", recording_error)
                if isinstance(exc, DropboxError):
                    raise
                raise DropboxError(message, uncertain=uncertain) from None