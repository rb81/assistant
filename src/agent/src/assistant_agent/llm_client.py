import logging
import os
import json
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from .config import AppConfig, app_display_name, app_referer_url
from .prompt_context import refresh_request_context
from .secret_store import read_secret
from .reasoning import configure as configure_reasoning
from .transport_security import open_https_request, validate_https_url
from .openrouter_policy import apply_routing_policy, is_openrouter_url, require_zdr_eligibility
from .llm_providers import (
    build_request, normalize_response, parse_model_reference,
    provider_base_url, provider_credential_name, provider_for, provider_is_disabled, route_fallbacks, route_provider,
    validate_provider_url,
)


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
        route: Optional[str] = None,
    ):
        self.config = config
        self.route = route or ("task" if config.get("agent.model_routes") is not None else None)
        if self.route:
            self.provider = route_provider(config, self.route)
            route_url = provider_base_url(config, self.provider)
        else:
            legacy_url = str(config.get("agent.llm.base_url", "https://openrouter.ai/api/v1"))
            if not base_url:
                validate_https_url(legacy_url, "Model API base URL")
            self.provider = provider_for(config, legacy_url)
            if provider_is_disabled(config, self.provider):
                raise ValueError("Model provider %s is disabled" % self.provider)
            route_url = base_url or provider_base_url(config, self.provider)
        self.base_url = str(base_url or route_url).rstrip("/")
        validate_https_url(self.base_url, "Model API base URL")
        validate_provider_url(self.provider, self.base_url)
        self.api_key = self._api_key(self.provider)
        if config.get("agent.model_routes") is not None:
            self.model = config.get("agent.llm.model") if self.route == "task" and model is None else model
            if not isinstance(self.model, str) or not self.model.strip():
                raise ValueError("Select a model for the %s route" % self.route)
        else:
            self.model = model or config.get("agent.llm.model", "anthropic/claude-sonnet-4.6")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds or 180
        self.allow_fallback = allow_fallback
        self.request_observer = request_observer

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        """Send a request and try only explicitly configured candidates after transient failures."""
        try:
            return self._chat_with_model(self.model, messages, tools)
        except _TransientLlmError as exc:
            if not self.allow_fallback:
                raise LlmRequestError(str(exc), status_code=exc.status_code) from exc
            fallback_specs = (
                route_fallbacks(self.config, self.route, self.provider)
                if self.route else route_fallbacks(self.config, "task", self.provider)
            )
            last_error = exc
            for fallback_spec in fallback_specs:
                provider, fallback_model = parse_model_reference(fallback_spec)
                if provider == self.provider and fallback_model == self.model:
                    continue
                if provider_is_disabled(self.config, provider):
                    raise RuntimeError("Fallback route uses disabled provider %s" % provider)
                fallback_url = provider_base_url(self.config, provider)
                validate_https_url(fallback_url, "Fallback model API URL")
                validate_provider_url(provider, fallback_url)
                api_key = self._api_key(provider)
                LOGGER.warning(
                    "primary model %r returned transient error (%s); retrying with %s model %r",
                    self.model, last_error, provider, fallback_model,
                )
                try:
                    return self._chat_with_model(
                        fallback_model, messages, tools, provider=provider,
                        base_url=fallback_url, api_key=api_key,
                    )
                except _TransientLlmError as fallback_exc:
                    last_error = fallback_exc
            raise LlmRequestError(str(last_error), status_code=last_error.status_code) from last_error

    @staticmethod
    def _api_key(provider: str) -> str:
        credential_name = provider_credential_name(provider, read_secret)
        api_key = read_secret(credential_name)
        if not api_key:
            raise RuntimeError("%s is required for the selected model provider" % credential_name)
        return api_key

    def _chat_with_model(
        self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
        *, provider: Optional[str] = None, base_url: Optional[str] = None, api_key: Optional[str] = None,
    ) -> dict[str, Any]:
        provider = provider or self.provider
        base_url = base_url or self.base_url
        api_key = api_key or self.api_key
        payload = {
            "model": model,
            "messages": [{key: value for key, value in message.items() if key != "memory_ids"} for message in refresh_request_context(messages, self.config)],
            "temperature": self.temperature
            if self.temperature is not None
            else self.config.get_float("agent.llm.temperature", 0.2),
            "max_tokens": self.max_tokens
            if self.max_tokens is not None
            else self.config.get_int("agent.llm.max_tokens_per_call", 8192),
        }
        reasoning = configure_reasoning(self.config, provider, model, self.route, payload)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if provider == "openrouter":
            apply_routing_policy(self.config, base_url, payload)
        require_zdr_eligibility(self.config, provider, model)
        if self.request_observer is not None:
            self.request_observer(payload)
        url, provider_headers, body = build_request(provider, base_url, api_key, payload)
        headers = {
            "Content-Type": "application/json",
            **provider_headers,
        }
        if provider == "cloudflare-gateway":
            from .llm_providers import cloudflare_gateway_id
            headers["cf-aig-gateway-id"] = cloudflare_gateway_id(self.config)
        if provider == "openrouter":
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
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise LlmRequestError("Model response exceeded the 8 MiB safety limit; request charge is uncertain")
                result = normalize_response(provider, json.loads(body.decode("utf-8")))
                result["_request_metadata"] = {
                    "provider": provider, "requested_model": self.model, "attempted_model": model,
                    "fallback": model != self.model or provider != self.provider, "actual_model": result.get("model"),
                    "route": self.route, "output_token_cap": payload["max_tokens"], "reasoning": reasoning,
                }
                return result
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            msg = "LLM request failed with HTTP %s: %s" % (exc.code, detail)
            if exc.code in {400, 404} and is_openrouter_url(base_url) and self.config.get_list("agent.llm.allowed_providers"):
                msg += " Check the allowed OpenRouter providers, model availability, and ZDR settings."
            if exc.code in _RETRYABLE_HTTP_CODES:
                raise _TransientLlmError(msg, status_code=exc.code) from exc
            raise LlmRequestError(msg, status_code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise LlmRequestError("LLM transport failed; charge may be uncertain: %s" % exc) from exc
        except (TimeoutError, OSError) as exc:
            raise LlmRequestError("LLM transport interrupted; charge may be uncertain: %s" % exc) from exc


class LlmRequestError(RuntimeError):
    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class _TransientLlmError(LlmRequestError):
    """Raised for HTTP errors that are transient (rate limit, overload) and worth retrying."""
