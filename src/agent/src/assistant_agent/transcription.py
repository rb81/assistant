from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import AppConfig


class TranscriptionError(ValueError):
    pass


def transcribe_audio(config: AppConfig, path: Path) -> dict:
    if not config.get_bool("agent.transcription.enabled", False):
        raise TranscriptionError("Voice transcription is disabled; please send text")
    base_url = str(config.get("agent.transcription.base_url", "http://transcription:8080")).rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"transcription", "localhost", "127.0.0.1"}):
        raise TranscriptionError("Transcription requires HTTPS or the managed internal service")
    token = str(config.get("agent.transcription.api_token") or "")
    if len(token) < 32:
        raise TranscriptionError("Transcription credentials are not configured")
    if path.stat().st_size > 20 * 1024 * 1024:
        raise TranscriptionError("Voice file exceeds the transcription byte limit")
    try:
        with path.open("rb") as handle, httpx.Client(timeout=360, follow_redirects=False) as client:
            response = client.post(base_url + "/transcribe", content=handle,
                                   headers={"x-transcription-token": token, "content-type": "application/octet-stream"},
                                   params={"language": config.get("agent.transcription.language", "auto")})
        if response.status_code in (429, 502, 503, 504):
            raise TranscriptionError("Transcription is temporarily unavailable")
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, TranscriptionError):
            raise
        raise TranscriptionError("Cannot transcribe this audio; repeat it or send text") from None
    if not isinstance(result, dict) or not str(result.get("text") or "").strip():
        raise TranscriptionError("No usable speech was detected; repeat it or send text")
    return result