import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import AppConfig
from .document_text import DocumentTextExtractor


class FileConversionError(ValueError):
    pass


@dataclass
class FileConversionResult:
    source_path: Path
    output_path: Path
    output_format: str
    engine: str
    size_bytes: int


OUTPUT_EXTENSIONS = {
    "markdown": ".md",
    "html": ".html",
    "pdf": ".pdf",
    "docx": ".docx",
}

OUTPUT_ALIASES = {
    "md": "markdown",
    "markdown": "markdown",
    "html": "html",
    "htm": "html",
    "pdf": "pdf",
    "doc": "docx",
    "docx": "docx",
    "office": "docx",
    "word": "docx",
}

MARKDOWN_INPUT_EXTENSIONS = {".md", ".markdown", ".mdown", ".mkdn"}
HTML_INPUT_EXTENSIONS = {".html", ".htm"}
PANDOC_OUTPUT_FORMATS = {
    "markdown": "gfm",
    "html": "html5",
    "docx": "docx",
}


class FileConversionService:
    def __init__(self, config: AppConfig, shared_root: Path):
        self.config = config
        self.shared_root = shared_root.resolve()
        self.extractor = DocumentTextExtractor(config)

    def convert(
        self,
        source_path: Path,
        output_format: str,
        destination_path: Optional[Path] = None,
    ) -> FileConversionResult:
        source = source_path.resolve()
        if not source.is_file():
            raise FileConversionError("source path is not a file")
        self.check_under_shared_root(source)
        clean_format = self.clean_output_format(output_format)
        self.check_size(source)
        destination = destination_path.resolve() if destination_path else self.available_output_path(source, clean_format)
        self.check_under_shared_root(destination)
        if destination.exists():
            raise FileConversionError("destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if clean_format == "markdown":
            return self.convert_to_markdown(source, destination)
        return self.convert_with_pandoc(source, clean_format, destination)

    def clean_output_format(self, value: str) -> str:
        clean = str(value or "").strip().lower().lstrip(".")
        result = OUTPUT_ALIASES.get(clean)
        if not result:
            raise FileConversionError("output_format must be markdown, html, pdf, or docx")
        return result

    def check_under_shared_root(self, path: Path) -> None:
        resolved = path.resolve()
        if resolved != self.shared_root and self.shared_root not in resolved.parents:
            raise FileConversionError("path must stay under shared root")

    def check_size(self, path: Path) -> None:
        max_bytes = self.config.get_int("agent.conversion.max_input_bytes", 25 * 1024 * 1024)
        if path.stat().st_size > max_bytes:
            raise FileConversionError("source file exceeds conversion size limit")

    def available_output_path(self, source: Path, output_format: str) -> Path:
        suffix = OUTPUT_EXTENSIONS[output_format]
        candidate = source.with_suffix(suffix)
        if not candidate.exists() and candidate.resolve() != source.resolve():
            return candidate
        stem = source.with_suffix("").name or source.name
        index = 1
        while True:
            candidate = source.parent / ("%s-%s%s" % (stem, index, suffix))
            if not candidate.exists() and candidate.resolve() != source.resolve():
                return candidate
            index += 1

    def convert_to_markdown(self, source: Path, destination: Path) -> FileConversionResult:
        if self.extractor.is_convertible_document(source):
            content = self.extractor.convert_to_markdown(source)
            engine = "markitdown"
        elif self.extractor.is_text_candidate(source):
            if source.suffix.lower() in {".md", ".markdown", ".mdown", ".mkdn"}:
                content = source.read_text(encoding="utf-8", errors="replace")
                engine = "copy"
            else:
                return self.convert_with_pandoc(source, "markdown", destination)
        else:
            raise FileConversionError("unsupported source file for Markdown conversion")
        self.write_text_atomically(destination, content)
        return FileConversionResult(
            source_path=source,
            output_path=destination,
            output_format="markdown",
            engine=engine,
            size_bytes=destination.stat().st_size,
        )

    def convert_with_pandoc(self, source: Path, output_format: str, destination: Path) -> FileConversionResult:
        pandoc = self.pandoc_path()
        with tempfile.TemporaryDirectory() as temp_dir:
            pandoc_source = source
            generated_markdown = False
            if self.extractor.is_convertible_document(source):
                markdown = self.extractor.convert_to_markdown(source)
                pandoc_source = Path(temp_dir) / "source.md"
                pandoc_source.write_text(markdown, encoding="utf-8")
                generated_markdown = True
            temp_output = self.temp_output_path(destination)
            try:
                command = [
                    pandoc,
                    str(pandoc_source),
                    "--from",
                    self.pandoc_input_format(pandoc_source, generated_markdown=generated_markdown),
                ]
                # For PDF, omit --to and let pandoc infer from output extension
                # This is more reliable with weasyprint
                if output_format != "pdf":
                    command.extend(["--to", PANDOC_OUTPUT_FORMATS[output_format]])
                command.extend(["-o", str(temp_output)])
                if output_format in {"html", "pdf"}:
                    command.append("--standalone")
                if output_format == "pdf":
                    pdf_engine = str(self.config.get("agent.conversion.pdf_engine", "weasyprint") or "").strip()
                    if pdf_engine:
                        command.append("--pdf-engine=%s" % pdf_engine)
                self.run_pandoc(command)
                temp_output.replace(destination)
            except Exception:
                if temp_output.exists():
                    temp_output.unlink()
                raise
        return FileConversionResult(
            source_path=source,
            output_path=destination,
            output_format=output_format,
            engine="pandoc",
            size_bytes=destination.stat().st_size,
        )

    def pandoc_input_format(self, source: Path, generated_markdown: bool = False) -> str:
        if generated_markdown:
            return "gfm"
        suffix = source.suffix.lower()
        if suffix in MARKDOWN_INPUT_EXTENSIONS:
            return "gfm"
        if suffix in HTML_INPUT_EXTENSIONS:
            return "html"
        if suffix in {".csv", ".tsv"}:
            return "csv"
        return "gfm"

    def pandoc_path(self) -> str:
        configured = str(self.config.get("agent.conversion.pandoc_path", "pandoc") or "pandoc")
        candidate = Path(configured)
        if candidate.is_absolute() and candidate.exists():
            return str(candidate)
        resolved = shutil.which(configured)
        if not resolved:
            raise FileConversionError("pandoc is not installed or not on PATH")
        return resolved

    def run_pandoc(self, command: list[str]) -> None:
        timeout = self.config.get_int("agent.conversion.timeout_seconds", 120)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise FileConversionError("pandoc conversion timed out") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "pandoc conversion failed").strip()
            raise FileConversionError(detail[:1000])

    def temp_output_path(self, destination: Path) -> Path:
        suffix = destination.suffix or ".tmp"
        handle = tempfile.NamedTemporaryFile(
            prefix=".%s." % destination.stem,
            suffix=suffix,
            dir=str(destination.parent),
            delete=False,
        )
        handle.close()
        return Path(handle.name)

    def write_text_atomically(self, destination: Path, content: str) -> None:
        temp_output = self.temp_output_path(destination)
        try:
            temp_output.write_text(content, encoding="utf-8")
            temp_output.replace(destination)
        except Exception:
            if temp_output.exists():
                temp_output.unlink()
            raise
