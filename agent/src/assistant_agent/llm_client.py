import logging
import os
import json
import urllib.error
import urllib.request
from typing import Any, Optional

from .config import AppConfig, app_display_name, app_referer_url


LOGGER = logging.getLogger("assistant.llm_client")

# HTTP status codes that are transient and worth retrying with the fallback model.
_RETRYABLE_HTTP_CODES = {429, 503, 529}


class LlmClient:
    def __init__(
        self,
        config: AppConfig,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
    ):
        self.config = config
        self.base_url = str(config.get("agent.llm.base_url", "https://openrouter.ai/api/v1")).rstrip("/")
        if "openrouter.ai" in self.base_url.lower():
            api_key = os.getenv("OPENROUTER_API_KEY")
        else:
            api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("model API key is required for task-agent")
        self.api_key = api_key
        self.model = model or config.get("agent.llm.model", "openai/gpt-4.1")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds or 180

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        """Send a chat completion request, retrying with the fallback model on transient errors."""
        try:
            return self._chat_with_model(self.model, messages, tools)
        except _TransientLlmError as exc:
            fallback = str(self.config.get("agent.llm.fallback_model") or "").strip()
            if not fallback or fallback == self.model:
                raise RuntimeError(str(exc)) from exc
            LOGGER.warning(
                "primary model %r returned transient error (%s); retrying with fallback model %r",
                self.model,
                exc,
                fallback,
            )
            try:
                return self._chat_with_model(fallback, messages, tools)
            except _TransientLlmError as fallback_exc:
                raise RuntimeError(str(fallback_exc)) from fallback_exc

    def _chat_with_model(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": self.temperature
            if self.temperature is not None
            else self.config.get_float("agent.llm.temperature", 0.2),
            "max_tokens": self.max_tokens
            if self.max_tokens is not None
            else self.config.get_int("agent.llm.max_tokens_per_call", 4096),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        body = json.dumps(payload, default=str).encode("utf-8")
        request = urllib.request.Request(
            "%s/chat/completions" % self.base_url,
            data=body,
            headers={
                "Authorization": "Bearer %s" % self.api_key,
                "Content-Type": "application/json",
                "HTTP-Referer": app_referer_url(self.config),
                "X-Title": app_display_name(self.config),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            msg = "LLM request failed with HTTP %s: %s" % (exc.code, detail)
            if exc.code in _RETRYABLE_HTTP_CODES:
                raise _TransientLlmError(msg) from exc
            raise RuntimeError(msg) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("LLM request failed: %s" % exc) from exc


class _TransientLlmError(Exception):
    """Raised for HTTP errors that are transient (rate limit, overload) and worth retrying."""
