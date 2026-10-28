"""Dedicated OpenRouter speech synthesis; delivery belongs to channel tools."""
import io
import inspect
import re
import time
import urllib.error
import uuid
import wave
from email.message import Message
from http.client import HTTPException
from urllib.parse import urlencode

from .model_resources import ModelServiceError, ModelToolResources, ProviderRejected
from .model_service import OpenRouterModels
from .requests import TransactionDatabase
from .secret_store import read_secret
from .transport_security import open_https_request


MAX_AUDIO_BYTES = 32 * 1024 * 1024
MAX_TEXT_CHARS = 10000
ACCOUNTING_DELAYS = (1, 2, 3, 4, 5)


def configured(config):
    return config.get_bool("agent.tts.enabled", False) and bool(read_secret("OPENROUTER_API_KEY"))


def supports_instructions(model):
    return (model.startswith("openai/gpt-4o-mini-tts") or model.startswith("fish-audio/")
            or (model.startswith("google/gemini-") and "tts" in model))


def audio_formats(model):
    if model.startswith(("google/", "heygen/")):
        return ["wav"]
    if model.startswith("minimax/"):
        return ["mp3"]
    return ["mp3", "wav"]


class OpenRouterSpeech(OpenRouterModels):
    def synthesize(self, payload, record_generation):
        request = self.request("/audio/speech", payload)
        request.add_header("Accept", "audio/*")
        try:
            with open_https_request(request, timeout=180) as response:
                generation_id = response.headers.get("X-Generation-Id")
                if generation_id is not None:
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", generation_id):
                        raise ModelServiceError("OpenRouter returned an invalid speech generation ID")
                    record_generation(generation_id)
                data = response.read(MAX_AUDIO_BYTES + 1)
                if not data or len(data) > MAX_AUDIO_BYTES:
                    raise ModelServiceError("Speech output is empty or exceeds 32 MiB")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or int(length) != len(data)):
                    raise ModelServiceError("Speech output ended before its declared length")
                return data, response.headers.get("Content-Type", ""), generation_id
        except urllib.error.HTTPError as exc:
            if exc.code in {400, 401, 402, 403, 404, 413, 422, 429}:
                raise ProviderRejected("OpenRouter rejected speech (HTTP %s); check the model, voice, format, credits and permissions" % exc.code) from exc
            raise ModelServiceError("OpenRouter speech failed (HTTP %s); do not automatically retry" % exc.code) from exc
        except (urllib.error.URLError, OSError, HTTPException) as exc:
            raise ModelServiceError("OpenRouter speech interrupted; charge is uncertain, do not automatically retry") from exc

    def generation(self, generation_id):
        # Only the read-only accounting lookup is retried, never synthesis. OpenRouter
        # often publishes speech cost several seconds after the audio is returned.
        attempts = len(ACCOUNTING_DELAYS) + 1
        for attempt in range(attempts):
            try:
                with open_https_request(self.request("/generation?" + urlencode({"id": generation_id})), timeout=30) as response:
                    data = self.read_json(response).get("data")
                if not isinstance(data, dict) or data.get("id") != generation_id or data.get("api_type") != "tts":
                    raise ModelServiceError("OpenRouter returned invalid speech accounting")
                if data.get("total_cost") is not None:
                    return data
            except urllib.error.HTTPError as exc:
                if exc.code not in {404, 429, 500, 502, 503, 504}:
                    raise ModelServiceError("OpenRouter speech accounting failed (HTTP %s)" % exc.code) from exc
            except (urllib.error.URLError, OSError, HTTPException) as exc:
                raise ModelServiceError("OpenRouter speech accounting lookup interrupted") from exc
            if attempt < attempts - 1:
                time.sleep(ACCOUNTING_DELAYS[attempt])
        raise ModelServiceError("OpenRouter speech accounting is not available yet")


def playable_audio(data, content_type, audio_format):
    header = Message()
    header["Content-Type"] = content_type
    mime = header.get_content_type()
    if audio_format == "mp3":
        if mime != "audio/mpeg":
            raise ModelServiceError("Speech response is not the requested MP3 audio")
        return data, mime
    if mime != "audio/pcm":
        raise ModelServiceError("Speech response is not the requested PCM audio")
    try:
        rate = int(header.get_param("rate") or "")
        channels = int(header.get_param("channels") or "")
        if not 8000 <= rate <= 192000 or channels not in {1, 2} or len(data) % (2 * channels):
            raise ValueError
    except ValueError as exc:
        raise ModelServiceError("PCM audio requires valid rate/channel metadata and complete 16-bit frames") from exc
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(data)
    return buffer.getvalue(), "audio/wav"


class TextToSpeech(ModelToolResources):
    def __init__(self, runtime):
        super().__init__(runtime)
        self.client = OpenRouterSpeech(self.config)

    def run(self, action, **arguments):
        if not configured(self.config):
            raise ModelServiceError("Text-to-speech is disabled or OPENROUTER_API_KEY is missing")
        methods = {"list_models": self.list_models, "generate": self.generate, "reconcile": self.reconcile}
        if action not in methods:
            raise ModelServiceError("Unknown text-to-speech action")
        try:
            inspect.signature(methods[action]).bind(**arguments)
        except TypeError as exc:
            required = {"generate": "model, text and voice", "list_models": "no required arguments",
                        "reconcile": "no required arguments; optional request_id"}
            raise ModelServiceError("Invalid %s arguments: %s. Expected %s." % (action, exc, required[action])) from None
        return methods[action](**arguments)

    def models(self):
        return [model for model in self.client.models(zdr_only=self.config.get_bool("agent.tts.zdr_only", False))
                if "speech" in (model.get("architecture") or {}).get("output_modalities", [])]

    def list_models(self, query="", limit=20, offset=0):
        if not isinstance(query, str) or type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or offset < 0:
            raise ModelServiceError("query must be text; limit must be 1-50 and offset nonnegative")
        models = []
        for model in self.models():
            if query.lower() not in (model["id"] + " " + str(model.get("name") or "")).lower():
                continue
            models.append({
                **{key: model.get(key) for key in ("id", "name", "pricing", "supported_voices")},
                "description": str(model.get("description") or "")[:1000],
                "supports_instructions": supports_instructions(model["id"]),
                "audio_formats": audio_formats(model["id"]),
            })
        return {"models": models[offset:offset + limit], "total": len(models),
                "next_offset": offset + limit if offset + limit < len(models) else None,
                "zdr_only": self.config.get_bool("agent.tts.zdr_only", False)}

    def generate(self, model, text, voice, audio_format=None, instructions=None):
        if not isinstance(model, str) or not model:
            raise ModelServiceError("model must be an exact speech model slug")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise ModelServiceError("Speech text must contain 1-10000 characters; provider limits may be lower")
        if not isinstance(voice, str) or not voice.strip() or len(voice) > 200:
            raise ModelServiceError("voice must be an explicit nonempty identifier of at most 200 characters")
        metadata = next((item for item in self.models() if item["id"] == model), None)
        if metadata is None:
            raise ModelServiceError("Speech model is unavailable under the configured catalog policy; use list_models")
        voices = metadata.get("supported_voices")
        if isinstance(voices, list) and voices and voice not in voices:
            raise ModelServiceError("Selected voice is not supported; use list_models")
        formats = audio_formats(model)
        if audio_format is None:
            audio_format = formats[0]
        if audio_format not in formats:
            raise ModelServiceError("Requested speech audio_format is unsupported; use list_models")
        payload = {"model": model, "input": text, "voice": voice,
                   "response_format": "pcm" if audio_format == "wav" else "mp3"}
        if instructions is not None:
            if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 2000:
                raise ModelServiceError("instructions must contain 1-2000 characters")
            if not supports_instructions(model):
                raise ModelServiceError("Delivery instructions are not documented for this model; choose a supported model")
            payload["instructions"] = instructions
        if self.config.get_bool("agent.tts.zdr_only", False):
            payload["provider"] = {"zdr": True}
        call_id = self.reserve({"tool": "text_to_speech", "model": model, "voice": voice, "audio_format": audio_format})
        generation_id = None

        def record_generation(value):
            nonlocal generation_id
            generation_id = value
            self.db.execute("UPDATE model_service_calls SET generation_id = %s WHERE id = %s", (value, call_id))

        try:
            data, content_type, generation_id = self.client.synthesize(payload, record_generation)
        except ProviderRejected:
            self.settle(call_id, "failed", {"cost": 0}, generation_id)
            raise
        except (ModelServiceError, OSError) as exc:
            self.settle(call_id, "unknown", generation_id=generation_id)
            recovery = ("Use reconcile with this request_id." if generation_id else
                        "Ask the administrator to reconcile the charge; no generation ID was received.")
            raise ModelServiceError("Speech request %s has uncertain cost: %s. Do not repeat synthesis. %s" % (call_id, exc, recovery)) from exc
        self.settle(call_id, "unknown", generation_id=generation_id)
        accounting_error = None
        usage = None
        try:
            if not generation_id:
                raise ModelServiceError("OpenRouter omitted X-Generation-Id")
            usage = self.account(call_id, generation_id)
        except ModelServiceError as exc:
            accounting_error = exc
        try:
            data, mime = playable_audio(data, content_type, audio_format)
            output = self.save_media_bytes(data, mime, call_id)
        except (ModelServiceError, OSError) as exc:
            raise ModelServiceError("Speech request %s output could not be saved: %s. Do not repeat synthesis; check recorded accounting." % (call_id, exc)) from exc
        if accounting_error and not generation_id:
            raise ModelServiceError(
                "Speech saved at %s, but request %s has unresolved accounting: %s. "
                "Do not repeat synthesis. Ask the administrator to reconcile the charge; "
                "the provider omitted its generation ID." % (output["path"], call_id, accounting_error)
            ) from accounting_error
        if accounting_error:
            return {"request_id": call_id, "generation_id": generation_id, "model": model,
                    "files": [output], "usage": None, "accounting": "pending",
                    "note": ("Audio is ready to use. OpenRouter has not published the charge yet; it is "
                             "reconciled automatically before the next paid request. Do not repeat synthesis."),
                    "budget": self.budget()}
        return {"request_id": call_id, "generation_id": generation_id, "model": model,
                "files": [output], "usage": usage, "budget": self.budget()}

    def account(self, call_id, generation_id, *, conn=None, job_id=None):
        generation = self.client.generation(generation_id)
        usage = {"cost": generation["total_cost"]}
        for source, target in (("tokens_prompt", "prompt_tokens"), ("tokens_completion", "completion_tokens")):
            value = generation.get(source)
            if value is not None:
                if type(value) is not int or value < 0:
                    raise ModelServiceError("OpenRouter returned invalid speech token accounting")
                usage[target] = value
        usage["total_tokens"] = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        if self.settle(call_id, "completed", usage, generation_id, conn=conn) != "completed":
            raise ModelServiceError("OpenRouter omitted speech cost; request remains unresolved")
        database = self.db if conn is None else TransactionDatabase(conn)
        database.log_event(
            job_id if job_id is not None else self.runtime.job["id"], "model_service_response", tool_name="text_to_speech",
            output_data={"request_id": call_id, "generation_id": generation_id}, tokens_used=usage,
        )
        if conn is None:
            self.runtime.model_service_tokens += usage["total_tokens"]
        return usage

    def reconcile(self, request_id=None):
        if request_id is None:
            _, days = self.budget_settings()
            rows = self.db.fetch_all(
                """SELECT id FROM model_service_calls WHERE status = 'unknown'
                   AND preset->>'tool' = 'text_to_speech' AND generation_id IS NOT NULL
                   AND job_id IS NOT NULL
                   AND created_at > now() - (%s * interval '1 day')
                   ORDER BY created_at LIMIT 20""", (days,),
            )
            results = []
            for row in rows:
                try:
                    results.append(self.reconcile(str(row["id"])))
                except ModelServiceError as exc:
                    raise ModelServiceError("Speech accounting for request %s remains unresolved: %s. "
                                            "Do not repeat synthesis; retry reconcile later." % (row["id"], exc)) from exc
            return {"requests": results, "budget": self.budget()}
        try:
            identifier = str(uuid.UUID(request_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ModelServiceError("request_id must be a UUID returned by this tool") from exc
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT * FROM model_service_calls WHERE id = %s
                   AND preset->>'tool' = 'text_to_speech' FOR UPDATE""", (identifier,),
            ).fetchone()
            if not row:
                raise ModelServiceError("Speech request was not found")
            if row["status"] == "completed" and row["cost_usd"] is not None:
                return {"request_id": identifier, "status": "completed", "usage": row["usage"],
                        "already_reconciled": True}
            if row["status"] != "unknown" or not row["generation_id"]:
                raise ModelServiceError("Only unknown-cost speech requests with a generation ID can be reconciled")
            if row["job_id"] is None:
                raise ModelServiceError("Speech request has no owning job; ask the administrator to reconcile it")
            conn.execute("SELECT set_config('assistant.job_id', %s, true)", (str(row["job_id"]),))
            usage = self.account(identifier, row["generation_id"], conn=conn, job_id=row["job_id"])
        if row["job_id"] == self.runtime.job["id"]:
            self.runtime.model_service_tokens += usage["total_tokens"]
        return {"request_id": identifier, "status": "completed", "usage": usage, "budget": self.budget()}


TEXT_TO_SPEECH_TOOL = {
    "type": "function",
    "function": {
        "name": "text_to_speech",
        "description": (
            "Discover dedicated speech models and voices, synthesize text into a workspace audio file, "
            "or reconcile unresolved speech charges across jobs without generating again. No inherited context, "
            "presets, voice cloning or delivery. Authentication is internal."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list_models", "generate", "reconcile"]},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "offset": {"type": "integer", "minimum": 0},
                "model": {"type": "string", "description": "Exact speech model slug from list_models."},
                "text": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT_CHARS, "description": "Literal text to speak. Do not insert delivery directions into it."},
                "voice": {"type": "string", "description": "Explicit voice identifier; use the model's supported_voices."},
                "audio_format": {"type": "string", "enum": ["mp3", "wav"], "description": "Defaults to MP3, or WAV for PCM-only models. WAV wraps provider PCM using its rate/channel metadata."},
                "instructions": {"type": "string", "maxLength": 2000, "description": "Optional tone, pacing or emotion instructions, only for models advertising supports_instructions."},
                "request_id": {"type": "string", "description": "Optional local request UUID for reconcile. Omit to retry up to 20 eligible unresolved speech charges across jobs. Never supply model/text/voice for reconcile."},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}
