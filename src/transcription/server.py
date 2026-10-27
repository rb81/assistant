import asyncio
import array
import hashlib
import hmac
import json
import os
import signal
import subprocess
import tempfile
import wave
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request


def secret_token() -> str:
    pointer = os.environ.get("TRANSCRIPTION_API_TOKEN_FILE")
    value = Path(pointer).read_text().strip() if pointer else os.environ.get("TRANSCRIPTION_API_TOKEN", "")
    if len(value) < 32:
        raise RuntimeError("transcription requires a secret token of at least 32 characters")
    return value


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
capacity = asyncio.Semaphore(1)
MODEL = Path(os.environ.get("WHISPER_MODEL_PATH", "/models/ggml-small.bin"))
MAX_BYTES = 20 * 1024 * 1024
MAX_SECONDS = 600


def run_process(arguments: list[str], timeout: int) -> None:
    with subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, start_new_session=True) as process:
        try:
            result = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise ValueError("audio processing exceeded its time limit") from None
        if result:
            raise ValueError("audio decoding or transcription failed")


def transcribe_file(source: Path, directory: Path, language: str) -> dict:
    waveform = directory / "audio.wav"
    run_process(["ffmpeg", "-nostdin", "-v", "error", "-threads", "1",
                 "-protocol_whitelist", "file,pipe", "-i", str(source), "-map", "0:a:0",
                 "-t", str(MAX_SECONDS + 1), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                 "-fs", "20000000", str(waveform)], 45)
    with wave.open(str(waveform), "rb") as audio:
        duration = audio.getnframes() / audio.getframerate()
        samples = array.array("h", audio.readframes(audio.getnframes()))
    if not 0.1 <= duration <= MAX_SECONDS:
        raise ValueError("audio must contain between 0.1 and 600 seconds")
    if not samples or sum(sample * sample for sample in samples) / len(samples) < 1024:
        raise ValueError("no audible speech was detected")
    output = directory / "transcript"
    threads = min(max(int(os.environ.get("WHISPER_THREADS", "2")), 1), 4)
    run_process(["whisper-cli", "-m", str(MODEL), "-f", str(waveform), "-l", language,
                 "-t", str(threads), "-ng", "-oj", "-of", str(output)], 300)
    result_path = output.with_suffix(".json")
    if not result_path.is_file() or result_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("transcription did not produce a bounded result")
    result = json.loads(result_path.read_text())
    segments = result.get("transcription") or []
    text = " ".join(str(segment.get("text") or "").strip() for segment in segments).strip()
    if not text or len(text) > 100000:
        raise ValueError("no usable speech was detected; please repeat or type the request")
    return {"text": text, "language": (result.get("result") or {}).get("language", language),
            "model": MODEL.name, "engine": "whisper.cpp", "duration_seconds": duration,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}


@app.get("/health")
def health():
    secret_token()
    if not MODEL.is_file():
        raise HTTPException(503, "transcription model is not provisioned")
    return {"status": "ready"}


@app.post("/transcribe")
async def transcribe(request: Request, language: str = "auto"):
    if not hmac.compare_digest(request.headers.get("x-transcription-token", ""), secret_token()):
        raise HTTPException(401, "unauthorized")
    if language not in ("auto", "en", "ar"):
        raise HTTPException(400, "language must be auto, en or ar")
    if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
        raise HTTPException(415, "send audio bytes as application/octet-stream")
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        raise HTTPException(400, "invalid content length") from None
    if declared < 0 or declared > MAX_BYTES:
        raise HTTPException(413, "audio exceeds the byte limit")
    if capacity.locked():
        raise HTTPException(429, "transcription is busy", headers={"Retry-After": "10"})
    async with capacity:
        with tempfile.TemporaryDirectory(prefix="voice-", dir="/tmp") as temporary:
            directory = Path(temporary)
            source = directory / "input"
            size = 0
            with source.open("xb") as handle:
                try:
                    async with asyncio.timeout(30):
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > MAX_BYTES:
                                raise HTTPException(413, "audio exceeds the byte limit")
                            handle.write(chunk)
                except TimeoutError:
                    raise HTTPException(408, "audio upload exceeded its time limit") from None
            if not size:
                raise HTTPException(400, "empty audio")
            try:
                return await asyncio.to_thread(transcribe_file, source, directory, language)
            except (ValueError, OSError, json.JSONDecodeError):
                raise HTTPException(422, "Cannot transcribe this audio; repeat it or send text") from None