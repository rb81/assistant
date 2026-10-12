import hashlib
import os
import urllib.request
from pathlib import Path


MODEL_SHA256 = "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin"
MODEL_SIZE = 487601967


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def main():
    token_path = os.environ.get("TRANSCRIPTION_API_TOKEN_FILE")
    if token_path:
        token = Path(token_path).read_text().strip()
        if len(token) < 32:
            raise RuntimeError("invalid transcription token")
        temporary_token = Path("/runtime/token.new")
        temporary_token.write_text(token)
        os.chmod(temporary_token, 0o400)
        os.chown(temporary_token, 10001, 10001)
        temporary_token.replace("/runtime/token")
    os.chown("/models", 0, 0)
    target = Path("/models/ggml-small.bin")
    if target.is_file() and digest(target) == MODEL_SHA256:
        return
    temporary = target.with_suffix(".download")
    try:
        size = 0
        with urllib.request.urlopen(MODEL_URL, timeout=60) as response, temporary.open("wb") as handle:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MODEL_SIZE:
                    raise RuntimeError("model download exceeded expected size")
                handle.write(chunk)
        if size != MODEL_SIZE or digest(temporary) != MODEL_SHA256:
            raise RuntimeError("model checksum verification failed")
        os.chmod(temporary, 0o644)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()