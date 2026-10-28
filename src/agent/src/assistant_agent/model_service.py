import base64
import binascii
import json
import math
import mimetypes
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg.types.json import Jsonb

from .config import app_display_name, app_referer_url
from .secret_store import read_secret
from .transport_security import open_https_request


BASE_URL = "https://openrouter.ai/api/v1"
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_PROMPT_BYTES = 200000
MAX_OUTPUT_TOKENS = 16384
_catalog_lock = threading.Lock()
_catalog: dict[bool, tuple[float, list[dict[str, Any]]]] = {}
MEDIA_TYPES = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg", "audio/flac": ".flac", "audio/pcm": ".pcm",
}
AUDIO_FORMATS = {"wav", "mp3", "aiff", "aac", "ogg", "flac", "m4a", "webm", "pcm16", "pcm24"}


class ModelServiceError(ValueError):
    pass


class ProviderRejected(ModelServiceError):
    """The provider rejected the request before generation."""


def validate_media(data, mime):
    signatures = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
        "audio/wav": data.startswith(b"RIFF") and data[8:12] == b"WAVE",
        "audio/x-wav": data.startswith(b"RIFF") and data[8:12] == b"WAVE",
        "audio/flac": data.startswith(b"fLaC"),
        "audio/ogg": data.startswith(b"OggS"),
        "audio/mpeg": data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 255 and data[1] & 224 == 224),
        "application/pdf": data.startswith(b"%PDF-"),
    }
    if mime in signatures and not signatures[mime]:
        raise ModelServiceError("Media bytes do not match MIME type %s" % mime)


def configured(config) -> bool:
    return config.get_bool("agent.model_service.enabled", False) and bool(read_secret("OPENROUTER_API_KEY"))


class OpenRouterModels:
    def __init__(self, config):
        self.config = config

    def request(self, path, payload=None):
        key = read_secret("OPENROUTER_API_KEY")
        if not key:
            raise ModelServiceError("OPENROUTER_API_KEY is required for the model service")
        return urllib.request.Request(
            BASE_URL + path,
            data=json.dumps(payload, allow_nan=False).encode() if payload is not None else None,
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "HTTP-Referer": app_referer_url(self.config),
                "X-Title": app_display_name(self.config),
            },
            method="POST" if payload is not None else "GET",
        )

    def models(self):
        zdr_only = self.config.get_bool("agent.model_service.zdr_only", False)
        with _catalog_lock:
            cached = _catalog.get(zdr_only)
            if cached and time.monotonic() - cached[0] < 300:
                return cached[1]
            path = "/models?output_modalities=all"
            if zdr_only:
                path += "&zdr=true"
            try:
                with open_https_request(self.request(path), timeout=30) as response:
                    data = self.read_json(response)
            except urllib.error.HTTPError as exc:
                raise ModelServiceError("OpenRouter model discovery failed (HTTP %s)" % exc.code) from exc
            except (urllib.error.URLError, OSError) as exc:
                raise ModelServiceError("OpenRouter model discovery failed: %s" % exc) from exc
            models = data.get("data")
            if not isinstance(models, list) or (not models and not zdr_only) or any(
                not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"]
                for model in models
            ):
                raise ModelServiceError("OpenRouter returned an invalid model catalog")
            _catalog[zdr_only] = (time.monotonic(), models)
            return models

    @staticmethod
    def read_json(response):
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ModelServiceError("OpenRouter response exceeds the 64 MiB limit")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ModelServiceError("OpenRouter returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ModelServiceError("OpenRouter response must be an object")
        return data

    def complete(self, payload):
        try:
            with open_https_request(self.request("/chat/completions", payload), timeout=180) as response:
                if payload.get("stream"):
                    return self.read_stream(response)
                return self.read_json(response)
        except urllib.error.HTTPError as exc:
            # Do not echo provider error bodies: they may contain prompts or base64 files.
            if exc.code in {400, 401, 402, 403, 404, 413, 422, 429}:
                raise ProviderRejected("OpenRouter rejected the request (HTTP %s); check capabilities, credits and permissions" % exc.code) from exc
            raise ModelServiceError("OpenRouter generation failed (HTTP %s); do not automatically retry" % exc.code) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ModelServiceError("OpenRouter request interrupted; charge is uncertain, do not automatically retry: %s" % exc) from exc

    @staticmethod
    def read_stream(response):
        text, audio, transcript = [], [], []
        result: dict[str, Any] = {}
        size = 0
        done = False
        while True:
            line = response.readline(MAX_RESPONSE_BYTES + 1)
            if not line:
                break
            size += len(line)
            if size > MAX_RESPONSE_BYTES:
                raise ModelServiceError("OpenRouter stream exceeds the 64 MiB limit")
            if not line.startswith(b"data:"):
                continue
            value = line[5:].strip()
            if value == b"[DONE]":
                done = True
                break
            try:
                chunk = json.loads(value)
            except (ValueError, UnicodeError) as exc:
                raise ModelServiceError("OpenRouter returned an invalid audio stream") from exc
            if not isinstance(chunk, dict):
                raise ModelServiceError("OpenRouter returned an invalid stream chunk")
            if chunk.get("error"):
                raise ModelServiceError("OpenRouter reported a streaming generation error")
            for key in ("id", "model", "usage"):
                if chunk.get(key) is not None:
                    result[key] = chunk[key]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    if not isinstance(delta["content"], str):
                        raise ModelServiceError("Unsupported streaming content format")
                    text.append(delta["content"])
                part = delta.get("audio") or {}
                if part.get("data"):
                    audio.append(part["data"])
                if part.get("transcript"):
                    transcript.append(part["transcript"])
                if choice.get("finish_reason"):
                    result["finish_reason"] = choice["finish_reason"]
        if not done:
            raise ModelServiceError("OpenRouter stream ended before completion; do not automatically retry")
        result["choices"] = [{
            "finish_reason": result.pop("finish_reason", None),
            "message": {"content": "".join(text), "audio": {
                "data": "".join(audio), "transcript": "".join(transcript),
            }},
        }]
        return result


class ModelService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.db = runtime.db
        self.config = runtime.config
        self.client = OpenRouterModels(self.config)

    def run(self, action, **arguments):
        if not configured(self.config):
            raise ModelServiceError("Model service is disabled or OPENROUTER_API_KEY is missing")
        methods = {
            "query_models": self.query_models, "add_preset": self.add_preset,
            "list_presets": self.list_presets, "delete_preset": self.delete_preset,
            "use_preset": self.use_preset,
        }
        if action not in methods:
            raise ModelServiceError("Unknown model service action")
        return methods[action](**arguments)

    def model(self, slug):
        for model in self.client.models():
            if model["id"] == slug:
                return model
        policy = " under the configured ZDR-only policy" if self.config.get_bool("agent.model_service.zdr_only", False) else ""
        raise ModelServiceError("Model is not in the current OpenRouter catalog%s; use query_models" % policy)

    def query_models(self, query="", input_modalities=None, output_modalities=None,
                     supported_parameters=None, limit=20, offset=0):
        if not isinstance(query, str) or type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or offset < 0:
            raise ModelServiceError("query must be text; limit must be 1-50 and offset nonnegative")
        for value in (input_modalities, output_modalities, supported_parameters):
            if value is not None and (not isinstance(value, list) or any(not isinstance(item, str) for item in value)):
                raise ModelServiceError("Model filters must be arrays of strings")
        matches = []
        for model in self.client.models():
            architecture = model.get("architecture") or {}
            if query.lower() not in (model["id"] + " " + str(model.get("name", "")) + " " + str(model.get("description", ""))).lower():
                continue
            if not set(input_modalities or []).issubset(architecture.get("input_modalities") or []):
                continue
            if not set(output_modalities or []).issubset(architecture.get("output_modalities") or []):
                continue
            if not set(supported_parameters or []).issubset(model.get("supported_parameters") or []):
                continue
            matches.append({
                key: model.get(key) for key in (
                    "id", "name", "architecture", "context_length", "pricing",
                    "supported_parameters", "supported_voices", "expiration_date",
                )
            })
            matches[-1]["description"] = str(model.get("description") or "")[:1000]
        return {"models": matches[offset:offset + limit], "total": len(matches),
                "next_offset": offset + limit if offset + limit < len(matches) else None}

    @staticmethod
    def preset_name(name):
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
            raise ModelServiceError("Preset name must be 1-64 letters, digits, underscores or hyphens")
        return name

    def add_preset(self, name, model, system_prompt=None):
        name = self.preset_name(name)
        if not isinstance(model, str):
            raise ModelServiceError("model must be an exact model slug")
        metadata = self.model(model)
        if "text" not in (metadata.get("architecture") or {}).get("output_modalities", []):
            raise ModelServiceError("Only chat-compatible text/image/audio models are supported")
        if system_prompt is not None and (not isinstance(system_prompt, str) or len(system_prompt.encode()) > 32000):
            raise ModelServiceError("System prompt must be text of at most 32000 bytes")
        with self.db.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('assistant_model_presets'))")
            if conn.execute("SELECT count(*) AS count FROM model_presets").fetchone()["count"] >= 100:
                raise ModelServiceError("Preset limit (100) reached; delete unused presets")
            row = conn.execute(
                """INSERT INTO model_presets(name, model, system_prompt) VALUES (%s, %s, %s)
                   ON CONFLICT (name) DO NOTHING RETURNING name, model, system_prompt""",
                (name, model, system_prompt),
            ).fetchone()
            if row is None:
                raise ModelServiceError("Preset already exists; delete it before recreating")
        return {"preset": row}

    def list_presets(self, name=None):
        if name is not None:
            self.preset_name(name)
        rows = self.db.fetch_all(
            "SELECT name, model, system_prompt FROM model_presets WHERE (%s::text IS NULL OR name = %s) ORDER BY name",
            (name, name),
        )
        return {"presets": rows, "budget": self.budget()}

    def delete_preset(self, name):
        row = self.db.fetch_one("DELETE FROM model_presets WHERE name = %s RETURNING name", (self.preset_name(name),))
        if row is None:
            raise ModelServiceError("Preset does not exist")
        return {"deleted": row["name"]}

    def budget_settings(self):
        try:
            amount = Decimal(str(self.config.get("agent.model_service.budget_usd", 5)))
            days = self.config.get("agent.model_service.budget_period_days", 30)
            if not amount.is_finite() or amount <= 0 or type(days) is not int or not 1 <= days <= 365:
                raise ValueError
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ModelServiceError("Model service budget must be positive and period must be 1-365 days") from exc
        return amount, days

    def budget(self, conn=None):
        amount, days = self.budget_settings()
        sql = """SELECT COALESCE(sum(cost_usd), 0) AS spent,
                        count(*) FILTER (WHERE status IN ('pending', 'unknown')) AS unresolved
                 FROM model_service_calls WHERE created_at > now() - (%s * interval '1 day')"""
        row = conn.execute(sql, (days,)).fetchone() if conn is not None else self.db.fetch_one(sql, (days,))
        return {"limit_usd": float(amount), "period_days": days, "spent_usd": float(row["spent"]),
                "unresolved_requests": row["unresolved"],
                "remaining_usd": float(max(Decimal(0), amount - row["spent"]))}

    def reserve(self, preset):
        call_id = str(uuid.uuid4())
        with self.db.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('assistant_model_service_budget'))")
            budget = self.budget(conn)
            if budget["unresolved_requests"]:
                raise ModelServiceError("Model service has a pending or unknown-cost request; wait for completion or ask the administrator to reconcile model_service_calls")
            if budget["remaining_usd"] <= 0:
                raise ModelServiceError("Model service budget exhausted for the rolling period")
            conn.execute(
                "INSERT INTO model_service_calls(id, job_id, preset) VALUES (%s, %s, %s)",
                (call_id, self.runtime.job.get("id"), Jsonb(preset)),
            )
        return call_id

    def settle(self, call_id, status, usage=None, generation_id=None):
        cost = None
        if usage is not None and usage.get("cost") is not None:
            try:
                cost = Decimal(str(usage["cost"]))
                if not cost.is_finite() or cost < 0:
                    raise ValueError
            except (InvalidOperation, TypeError, ValueError) as exc:
                self.db.execute("UPDATE model_service_calls SET status = 'unknown' WHERE id = %s", (call_id,))
                raise ModelServiceError("OpenRouter returned an invalid cost; request %s remains unresolved" % call_id) from exc
        if status == "completed" and cost is None:
            status = "unknown"
        self.db.execute(
            "UPDATE model_service_calls SET status = %s, cost_usd = %s, usage = %s, generation_id = %s WHERE id = %s",
            (status, cost, Jsonb(usage) if usage is not None else None, generation_id, call_id),
        )
        return status

    def messages(self, payload, preset):
        if not isinstance(payload, dict) or set(payload) != {"messages"}:
            raise ModelServiceError("payload must contain only a messages array")
        messages = payload["messages"]
        if not isinstance(messages, list) or not 1 <= len(messages) <= 100:
            raise ModelServiceError("Provide 1-100 prompt messages")
        clean = []
        if preset.get("system_prompt"):
            clean.append({"role": "system", "content": preset["system_prompt"]})
        for message in messages:
            if (not isinstance(message, dict) or set(message) != {"role", "content"}
                    or message["role"] not in {"user", "assistant"} or not isinstance(message["content"], str)):
                raise ModelServiceError("Prompt messages must have a user/assistant role and text content; use the preset for system instructions and files for attachments")
            clean.append(dict(message))
        if clean[-1]["role"] != "user":
            raise ModelServiceError("The final prompt message must be from the user")
        if len(json.dumps(clean).encode()) > MAX_PROMPT_BYTES:
            raise ModelServiceError("Prompt exceeds 200000 bytes")
        return clean

    def attach_files(self, messages, files, model):
        if files is None:
            return
        if not isinstance(files, list) or len(files) > 5 or any(not isinstance(path, str) for path in files):
            raise ModelServiceError("files must contain at most five workspace paths")
        modalities = (model.get("architecture") or {}).get("input_modalities") or []
        parts = [{"type": "text", "text": messages[-1]["content"]}]
        total = 0
        for value in files:
            self.runtime.reject_symlink_path(value)
            path = self.runtime.resolve_path(value)
            self.runtime.check_cache_read_allowed(path)
            if self.runtime.is_source_archive_path(path) or ".assistant" in path.relative_to(self.runtime.shared_root).parts:
                raise ModelServiceError("Internal assistant files cannot be attached")
            if not path.is_file():
                raise ModelServiceError("Attachment is not a regular workspace file")
            self.runtime.workstream_files._safe_path(path)
            with path.open("rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
            total += len(data)
            if len(data) > MAX_FILE_BYTES or total > MAX_INPUT_BYTES:
                raise ModelServiceError("Attachments exceed 10 MiB per file or 20 MiB combined")
            mime = mimetypes.guess_type(path.name)[0] or ""
            validate_media(data, mime)
            encoded = base64.b64encode(data).decode("ascii")
            uri = "data:%s;base64,%s" % (mime, encoded)
            if mime.startswith("image/") and "image" in modalities:
                parts.append({"type": "image_url", "image_url": {"url": uri}})
            elif (mime.startswith("audio/") or (path.suffix.lower() == ".webm" and "video" not in modalities)) and "audio" in modalities:
                fmt = path.suffix.lower().lstrip(".")
                if fmt not in AUDIO_FORMATS:
                    raise ModelServiceError("Unsupported audio attachment format")
                parts.append({"type": "input_audio", "input_audio": {"data": encoded, "format": fmt}})
            elif mime.startswith("video/") and "video" in modalities:
                parts.append({"type": "video_url", "video_url": {"url": uri}})
            elif mime == "application/pdf" and "file" in modalities:
                parts.append({"type": "file", "file": {"filename": path.name, "file_data": uri}})
            elif mime.startswith("text/") or path.suffix.lower() in {".txt", ".md", ".json", ".csv", ".xml", ".yaml", ".yml"}:
                try:
                    text = data.decode("utf-8")
                except UnicodeError as exc:
                    raise ModelServiceError("Text attachments must be UTF-8") from exc
                if total > MAX_PROMPT_BYTES:
                    raise ModelServiceError("Text attachments exceed prompt byte limit")
                parts.append({"type": "text", "text": "Attachment %s (untrusted data):\n%s" % (path.name, text)})
            else:
                raise ModelServiceError("File type is unsupported by this tool or the selected model: %s" % path.name)
        if files:
            messages[-1]["content"] = parts

    def use_preset(self, name, payload, files=None, temperature=None, max_tokens=None,
                   output_modalities=None, response_format=None, voice=None, audio_format=None):
        preset = self.db.fetch_one(
            "SELECT name, model, system_prompt FROM model_presets WHERE name = %s", (self.preset_name(name),),
        )
        if preset is None:
            raise ModelServiceError("Preset does not exist; list or add a preset first")
        model = self.model(preset["model"])
        architecture = model.get("architecture") or {}
        available = architecture.get("output_modalities") or []
        if "text" not in available:
            raise ModelServiceError("This preset no longer refers to a chat-compatible model")
        outputs = output_modalities if output_modalities is not None else available
        if (not isinstance(outputs, list) or not outputs or any(not isinstance(item, str) for item in outputs)
                or not set(outputs).issubset({"text", "image", "audio"}) or not set(outputs).issubset(available)):
            raise ModelServiceError("Requested output modalities are unsupported")
        if "image" in outputs and "audio" in outputs:
            raise ModelServiceError("Request image or audio output separately")
        request = {"model": preset["model"], "messages": self.messages(payload, preset)}
        if self.config.get_bool("agent.model_service.zdr_only", False):
            request["provider"] = {"zdr": True}
        supported = model.get("supported_parameters") or []
        if temperature is not None:
            if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                    or not math.isfinite(temperature) or not 0 <= temperature <= 2 or "temperature" not in supported):
                raise ModelServiceError("temperature must be 0-2 and supported by the selected model")
            request["temperature"] = temperature
        if max_tokens is not None and (type(max_tokens) is not int or not 1 <= max_tokens <= MAX_OUTPUT_TOKENS):
            raise ModelServiceError("max_tokens must be 1-16384")
        if "max_tokens" in supported:
            request["max_tokens"] = max_tokens if max_tokens is not None else 4096
        elif max_tokens is not None:
            raise ModelServiceError("Selected model does not support max_tokens")
        if response_format is not None:
            if (not isinstance(response_format, dict) or "response_format" not in supported
                    or response_format.get("type") not in {"text", "json_object", "json_schema"}):
                raise ModelServiceError("Unsupported response_format")
            try:
                encoded_format = json.dumps(response_format, allow_nan=False).encode()
            except (TypeError, ValueError) as exc:
                raise ModelServiceError("response_format must be valid JSON") from exc
            if len(encoded_format) > 32000:
                raise ModelServiceError("response_format exceeds 32000 bytes")
            if response_format["type"] == "json_schema" and not isinstance(response_format.get("json_schema"), dict):
                raise ModelServiceError("json_schema response_format requires a json_schema object")
            if response_format["type"] == "json_schema" and "structured_outputs" not in supported:
                raise ModelServiceError("Selected model does not advertise structured_outputs")
            request["response_format"] = response_format
        if set(available) != {"text"}:
            request["modalities"] = outputs
        if "audio" in outputs:
            if audio_format is None:
                audio_format = "wav"
            if audio_format not in {"wav", "mp3", "flac", "opus", "pcm16"}:
                raise ModelServiceError("Unsupported audio_format")
            request["audio"] = {"format": audio_format}
            if voice is not None:
                if not isinstance(voice, str) or not voice.strip() or len(voice) > 100:
                    raise ModelServiceError("voice must be a nonempty identifier")
                voices = model.get("supported_voices")
                if voices and isinstance(voices, list) and all(isinstance(item, str) for item in voices) and voice not in voices:
                    raise ModelServiceError("Selected voice is not supported")
                request["audio"]["voice"] = voice
            request["stream"] = True
            request["stream_options"] = {"include_usage": True}
        elif voice is not None or audio_format is not None:
            raise ModelServiceError("voice/audio_format require audio output")
        self.attach_files(request["messages"], files, model)
        call_id = self.reserve(preset)
        try:
            response = self.client.complete(request)
        except ProviderRejected:
            self.settle(call_id, "failed", {"cost": 0})
            raise
        except (ModelServiceError, ValueError, OSError) as exc:
            self.settle(call_id, "unknown")
            raise ModelServiceError("Request %s has uncertain cost: %s" % (call_id, exc)) from exc
        usage = response.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        status = self.settle(call_id, "completed", usage, response.get("id"))
        tokens = usage.get("total_tokens")
        if tokens is None:
            tokens = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
        self.runtime.model_service_tokens += max(0, int(tokens or 0))
        self.db.log_event(
            self.runtime.job["id"], "model_service_response", tool_name="llm_service",
            output_data={"request_id": call_id, "preset": preset, "generation_id": response.get("id")},
            tokens_used=usage,
        )
        if status == "unknown":
            raise ModelServiceError("OpenRouter omitted usage.cost; request %s recorded with unknown cost and further spending blocked" % call_id)
        if response.get("error") or not response.get("choices"):
            raise ModelServiceError("OpenRouter returned no reply; request %s cost was recorded" % call_id)
        choice = response["choices"][0]
        if choice.get("error") or choice.get("finish_reason") == "error":
            raise ModelServiceError("OpenRouter reported a generation error; request %s cost was recorded" % call_id)
        result = self.save_reply(choice.get("message") or {}, call_id, audio_format)
        result.update({
            "request_id": call_id, "model": response.get("model") or preset["model"],
            "finish_reason": choice.get("finish_reason"), "usage": usage, "budget": self.budget(),
        })
        return result

    def save_reply(self, message, call_id, audio_format):
        texts, outputs = [], []
        media = []
        content = message.get("content")
        if isinstance(content, str):
            if content:
                texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    raise ModelServiceError("Unsupported reply content format; cost already recorded")
                if part.get("type") == "text":
                    texts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    media.append((part.get("image_url") or {}).get("url"))
                else:
                    raise ModelServiceError("Unsupported reply content type; cost already recorded")
        elif content is not None:
            raise ModelServiceError("Unsupported reply content format; cost already recorded")
        for image in message.get("images") or []:
            media.append((image.get("image_url") or {}).get("url"))
        for uri in media:
            if not isinstance(uri, str) or not uri.startswith("data:") or ";base64," not in uri:
                raise ModelServiceError("Only inline base64 media output is supported; cost already recorded")
            header, data = uri.split(";base64,", 1)
            outputs.append(self.save_media(data, header[5:], call_id, len(outputs)))
        audio = message.get("audio") or {}
        if audio.get("transcript") and audio["transcript"] not in texts:
            texts.append(audio["transcript"])
        if audio.get("data"):
            mime = {"mp3": "audio/mpeg", "opus": "audio/ogg", "pcm16": "audio/pcm"}.get(audio_format, "audio/" + (audio_format or "wav"))
            outputs.append(self.save_media(audio["data"], mime, call_id, len(outputs)))
        if not texts and not outputs:
            raise ModelServiceError("OpenRouter returned an empty or unsupported reply; cost already recorded")
        return {"content": "\n".join(texts), "files": outputs}

    def save_media(self, encoded, mime, call_id, index):
        if mime not in MEDIA_TYPES or not isinstance(encoded, str):
            raise ModelServiceError("Unsupported media MIME type; cost already recorded")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ModelServiceError("Invalid base64 media output; cost already recorded") from exc
        if not data or len(data) > 32 * 1024 * 1024:
            raise ModelServiceError("Media output is empty or exceeds 32 MiB")
        validate_media(data, mime)
        value = "outputs/job-%s/%s-%s%s" % (self.runtime.job["id"], call_id, index, MEDIA_TYPES[mime])
        self.runtime.reject_symlink_path(value)
        path = self.runtime.resolve_path(value)
        self.runtime.check_write_allowed(path)
        self.runtime.workstream_files.register_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(data)
        self.runtime.workstream_files.track_file(path)
        self.runtime.normalize_and_index_path(path, source="model_service")
        return {"path": str(path.relative_to(self.runtime.shared_root)), "mime_type": mime, "bytes": len(data)}


MODEL_SERVICE_TOOL = {
    "type": "function",
    "function": {
        "name": "llm_service",
        "description": (
            "Discover OpenRouter models, manage reusable local presets, and call presets for isolated "
            "text, structured JSON, image or audio generation/understanding. No inherited context or tools. "
            "Media outputs are saved to the job workspace; inspect them yourself or via a vision preset."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["query_models", "add_preset", "list_presets", "delete_preset", "use_preset"]},
                "name": {"type": "string", "description": "Preset name for add/delete/use; optional exact filter for list."},
                "model": {"type": "string", "description": "Exact model slug for add_preset."},
                "system_prompt": {"type": "string", "description": "Optional persistent system prompt for add_preset."},
                "query": {"type": "string", "description": "Model catalog text search."},
                "input_modalities": {"type": "array", "items": {"type": "string"}},
                "output_modalities": {"type": "array", "items": {"type": "string"}, "description": "Catalog filter, or requested outputs for use_preset; defaults to model outputs."},
                "supported_parameters": {"type": "array", "items": {"type": "string"}, "description": "Catalog filter."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "offset": {"type": "integer", "minimum": 0},
                "payload": {
                    "type": "object",
                    "properties": {"messages": {
                        "type": "array", "minItems": 1, "maxItems": 100,
                        "items": {"type": "object", "properties": {
                            "role": {"type": "string", "enum": ["user", "assistant"]},
                            "content": {"type": "string"},
                        }, "required": ["role", "content"], "additionalProperties": False},
                    }},
                    "required": ["messages"], "additionalProperties": False,
                },
                "files": {"type": "array", "maxItems": 5, "items": {"type": "string"}, "description": "Workspace paths attached to final user message, up to 10 MiB each/20 MiB total."},
                "temperature": {"type": "number", "minimum": 0, "maximum": 2},
                "max_tokens": {"type": "integer", "minimum": 1, "maximum": 16384},
                "response_format": {"type": "object", "description": "OpenRouter response_format, where supported (text, json_object or json_schema)."},
                "voice": {"type": "string", "description": "Audio voice identifier; provide when the model requires one."},
                "audio_format": {"type": "string", "enum": ["wav", "mp3", "flac", "opus", "pcm16"]},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}
