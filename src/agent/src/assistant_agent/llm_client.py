import logging
import os
import json
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from .config import AppConfig, app_display_name, app_referer_url
from .prompt_context import refresh_request_context
from .secret_store import read_secret
from .transport_security import open_https_request, validate_https_url
from .openrouter_policy import apply_routing_policy, is_openrouter_url, require_zdr_eligibility
from .llm_providers import PROVIDER_CREDENTIALS, build_request, normalize_response, provider_for


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
        allow_fallback: bool = True,
        base_url: Optional[str] = None,
        request_observer: Optional[Callable[[dict[str, Any]], None]] = None,
    ):
        self.config = config
        self.base_url = str(base_url or config.get("agent.llm.base_url", "https://openrouter.ai/api/v1")).rstrip("/")
        validate_https_url(self.base_url, "Model API base URL")
        self.provider = provider_for(config, self.base_url)
        if self.provider == "openrouter" and not is_openrouter_url(self.base_url):
            raise ValueError("OpenRouter provider requires an https://openrouter.ai API URL")
        credential_name = PROVIDER_CREDENTIALS[self.provider]
        api_key = read_secret(credential_name)
        if self.provider == "openai-compatible" and not api_key:
            api_key = read_secret("AGENT_LLM_OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("%s is required for the selected model provider" % credential_name)
        self.api_key = api_key
        self.model = model or config.get("agent.llm.model", "anthropic/claude-sonnet-4.6")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds or 180
        self.allow_fallback = allow_fallback
        self.request_observer = request_observer

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        """Send a chat completion request, retrying with the fallback model on transient errors."""
        try:
            return self._chat_with_model(self.model, messages, tools)
        except _TransientLlmError as exc:
            fallback = str(self.config.get("agent.llm.fallback_model") or "").strip()
            if not self.allow_fallback or not fallback or fallback == self.model:
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
            "messages": [{key: value for key, value in message.items() if key != "memory_ids"} for message in refresh_request_context(messages, self.config)],
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
        if self.provider == "openrouter":
            apply_routing_policy(self.config, self.base_url, payload)
        require_zdr_eligibility(self.config, self.provider, model)
        if self.request_observer is not None:
            self.request_observer(payload)
        url, provider_headers, body = build_request(self.provider, self.base_url, self.api_key, payload)
        headers = {
            "Content-Type": "application/json",
            **provider_headers,
        }
        if self.provider == "openrouter":
            headers.update({
                "HTTP-Referer": app_referer_url(self.config),
                "X-Title": app_display_name(self.config),
            })
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with open_https_request(request, timeout=self.timeout_seconds) as response:
                return normalize_response(self.provider, json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            msg = "LLM request failed with HTTP %s: %s" % (exc.code, detail)
            if exc.code in {400, 404} and is_openrouter_url(self.base_url) and self.config.get_list("agent.llm.allowed_providers"):
                msg += " Check the allowed OpenRouter providers, model availability, and ZDR settings."
            if exc.code in _RETRYABLE_HTTP_CODES:
                raise _TransientLlmError(msg) from exc
            raise RuntimeError(msg) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("LLM request failed: %s" % exc) from exc


class _TransientLlmError(Exception):
    """Raised for HTTP errors that are transient (rate limit, overload) and worth retrying."""
