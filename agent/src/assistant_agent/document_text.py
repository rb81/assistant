from pathlib import Path
from typing import Any, Optional

from .config import AppConfig


CONVERTIBLE_DOCUMENT_EXTENSIONS = {
    ".doc",
    ".docx",
    ".odp",
    ".ods",
    ".odt",
    ".pdf",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsm",
    ".xlsx",
}

TEXT_EXTENSIONS = {
    ".bash",
    ".c",
    ".conf",
    ".cpp",
    ".cs",
    ".css",
    ".csv",
    ".env",
    ".fish",
    ".go",
    ".h",
    ".hpp",
    ".htm",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".less",
    ".log",
    ".markdown",
    ".mjs",
    ".md",
    ".mdown",
    ".mkdn",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sass",
    ".scss",
    ".sh",
    ".sql",
    ".ts",
    ".tsx",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
    ".zsh",
}

TEXT_FILENAMES = {
    ".dockerignore",
    ".env",
    ".gitignore",
    "dockerfile",
    "makefile",
    "readme",
}

BINARY_EXTENSIONS = {
    ".7z",
    ".avif",
    ".bmp",
    ".dmg",
    ".eot",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp3",
    ".mp4",
    ".otf",
    ".png",
    ".rar",
    ".tar",
    ".tif",
    ".tiff",
    ".ttf",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".zip",
}


class UnsupportedDocumentError(ValueError):
    pass


class DocumentTextExtractor:
    def __init__(self, config: AppConfig):
        self.config = config
        self._markitdown = None

    def convertible_extensions(self) -> set[str]:
        configured = self.config.get_list("agent.workspace.convertible_document_extensions")
        if not configured:
            return set(CONVERTIBLE_DOCUMENT_EXTENSIONS)
        return {item if item.startswith(".") else ".%s" % item for item in configured}

    def text_extensions(self) -> set[str]:
        configured = self.config.get_list("agent.workspace.text_extensions")
        if not configured:
            return set(TEXT_EXTENSIONS)
        return {item if item.startswith(".") else ".%s" % item for item in configured}

    def is_convertible_document(self, path: Path) -> bool:
        return path.suffix.lower() in self.convertible_extensions()

    def is_text_candidate(self, path: Path) -> bool:
        suffix = path.suffix.lower()
        name = path.name.lower()
        if suffix in BINARY_EXTENSIONS or suffix in self.convertible_extensions():
            return False
        if suffix in self.text_extensions() or name in TEXT_FILENAMES:
            return True
        return suffix == ""

    def markitdown(self) -> Any:
        if self._markitdown is None:
            from markitdown import MarkItDown

            self._markitdown = MarkItDown(enable_plugins=False)
        return self._markitdown

    def result_text(self, result: Any) -> str:
        for attribute in ("text_content", "markdown"):
            value = getattr(result, attribute, None)
            if isinstance(value, str):
                return value
        return str(result)

    def convert_to_markdown(self, path: Path, extension: Optional[str] = None) -> str:
        suffix = (extension or path.suffix).lower()
        with path.open("rb") as handle:
            result = self.markitdown().convert_stream(handle, file_extension=suffix)
        return self.result_text(result)

    def extract_text(self, path: Path) -> tuple[str, str]:
        if self.is_text_candidate(path):
            data = path.read_bytes()
            if b"\x00" in data:
                raise UnsupportedDocumentError("unsupported binary file")
            return data.decode("utf-8"), "text"
        if self.is_convertible_document(path):
            return self.convert_to_markdown(path), "markitdown"
        raise UnsupportedDocumentError("unsupported file extension: %s" % (path.suffix.lower() or "<none>"))
