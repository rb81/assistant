"""Provider-specific HTTP and message-format adapters for task-agent models."""
import base64
import json
import re
from urllib.parse import quote, urlsplit

from .transport_security import validate_https_url


PROVIDERS = ("auto", "openrouter", "openai", "anthropic", "gemini", "cloudflare", "cloudflare-gateway", "openai-compatible")
PROVIDER_CREDENTIALS = {
    "openrouter": "AGENT_LLM_OPENROUTER_API_KEY",
    "openai": "AGENT_LLM_OPENAI_API_KEY",
    "anthropic": "AGENT_LLM_ANTHROPIC_API_KEY",
    "gemini": "AGENT_LLM_GEMINI_API_KEY",
    "cloudflare": "AGENT_LLM_CLOUDFLARE_API_KEY",
    "cloudflare-gateway": "AGENT_LLM_CLOUDFLARE_API_KEY",
    "openai-compatible": "AGENT_PROVIDERS_OPENAI_COMPATIBLE_API_KEY",
}
PROVIDER_DEFAULT_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}


def provider_credential_name(provider, available):
    """Prefer a dedicated custom-endpoint key while preserving existing installations."""
    primary = PROVIDER_CREDENTIALS[provider]
    if provider != "openai-compatible":
        return primary
    names = (primary, "AGENT_LLM_OPENAI_API_KEY", "AGENT_LLM_OPENROUTER_API_KEY")
    return next((name for name in names if available(name)), primary)


def route_provider(config, route):
    """Resolve an explicitly selected route provider."""
    selected_path = "agent.model_routes.%s.provider" % route
    selected = str(config.get(selected_path, "") or "").strip().lower()
    if config.get("agent.model_routes") is None:
        base_url = str(config.get("agent.llm.base_url", PROVIDER_DEFAULT_URLS["openrouter"]) or "")
        provider = provider_for(config, base_url)
    else:
        if selected in {"", "inherit", "auto"}:
            raise ValueError("Select an explicit model provider for route %s" % route)
        if selected not in PROVIDER_CREDENTIALS:
            raise ValueError("Unsupported model provider for route %s: %s" % (route, selected))
        provider = selected
    if provider_is_disabled(config, provider):
        raise ValueError("Model route %s uses disabled provider %s" % (route, provider))
    return provider


def provider_is_disabled(config, provider):
    disabled = {item.strip().lower() for item in config.get_list("agent.providers.disabled")}
    enabled = config.get("agent.providers.enabled")
    if enabled is not None:
        return provider not in {item.strip().lower() for item in config.get_list("agent.providers.enabled")} or provider in disabled
    return provider in disabled


def cloudflare_gateway_id(config):
    value = str(config.get("agent.providers.cloudflare_gateway_id", "default") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ValueError("Cloudflare AI Gateway requires a valid gateway ID")
    return value


def provider_base_url(config, provider):
    """Return a validated endpoint for one provider without changing global route selection."""
    if provider in PROVIDER_DEFAULT_URLS:
        endpoint = str(config.get("agent.providers.%s_base_url" % provider) or PROVIDER_DEFAULT_URLS[provider]).rstrip("/")
        configured = str(config.get("agent.llm.base_url", "") or "").rstrip("/")
        if (config.get("agent.model_routes") is None
                and endpoint == PROVIDER_DEFAULT_URLS[provider] and configured
                and (urlsplit(configured).hostname or "").lower() == urlsplit(endpoint).hostname):
            endpoint = configured
        validate_provider_url(provider, endpoint)
        return endpoint
    if provider in {"cloudflare", "cloudflare-gateway"}:
        if provider == "cloudflare-gateway":
            cloudflare_gateway_id(config)
        account_id = str(config.get("agent.providers.cloudflare_account_id", "") or "").strip()
        template = str(config.get("agent.providers.cloudflare_base_url") or
                       "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1")
        template_path = urlsplit(template).path
        account_match = re.fullmatch(r"/client/v4/accounts/([^/]+)/ai/(?:v1|run)/?", template_path)
        if account_match and account_match.group(1) != "{account_id}":
            endpoint_account = account_match.group(1)
            if account_id and endpoint_account != account_id:
                raise ValueError("Cloudflare endpoint account ID does not match the configured account ID")
            account_id = endpoint_account
        if not account_id and config.get("agent.model_routes") is None:
            configured = str(config.get("agent.llm.base_url", "") or "").rstrip("/")
            parsed = urlsplit(configured)
            parts = parsed.path.strip("/").split("/")
            if (parsed.hostname or "").lower() == "api.cloudflare.com" and len(parts) >= 5:
                account_id = parts[3]
        if not account_id:
            raise ValueError("Cloudflare Workers AI requires an account ID")
        endpoint = template.replace("{account_id}", quote(account_id, safe="")).rstrip("/")
        parsed = urlsplit(endpoint)
        if re.fullmatch(r"/client/v4/accounts/[^/]+/ai/run", parsed.path):
            endpoint = parsed._replace(path=parsed.path.removesuffix("/run") + "/v1").geturl()
        validate_provider_url(provider, endpoint)
        return endpoint
    if provider == "openai-compatible":
        configured = str(config.get("agent.providers.openai_compatible_base_url", "") or "").strip()
        if not configured and config.get("agent.model_routes") is None:
            legacy = str(config.get("agent.llm.base_url", "") or "").rstrip("/")
            if provider_for(config, legacy) == "openai-compatible":
                configured = legacy
        if not configured:
            raise ValueError("OpenAI-compatible provider requires its HTTPS API URL")
        endpoint = configured.rstrip("/")
        validate_provider_url(provider, endpoint)
        return endpoint
    raise ValueError("Unsupported model provider: %s" % provider)


def validate_provider_url(provider, base_url):
    """Prevent a route from using one provider's policy with another provider's host."""
    try:
        validate_https_url(base_url, "Provider API URL")
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    host = (urlsplit(base_url).hostname or "").lower()
    native_hosts = {
        "openai": "api.openai.com",
        "anthropic": "api.anthropic.com",
        "gemini": "generativelanguage.googleapis.com",
        "cloudflare": "api.cloudflare.com",
        "cloudflare-gateway": "api.cloudflare.com",
    }
    if provider == "openrouter" and host != "openrouter.ai":
        raise ValueError("OpenRouter provider requires an https://openrouter.ai API URL")
    if provider in native_hosts and host != native_hosts[provider]:
        raise ValueError("%s provider requires the official %s API host" % (provider, native_hosts[provider]))
    if provider in {"cloudflare", "cloudflare-gateway"} and not re.fullmatch(
            r"/client/v4/accounts/[^/]+/ai/v1/?", urlsplit(base_url).path):
        raise ValueError(
            "Cloudflare model API URL must end in /client/v4/accounts/<account_id>/ai/v1 "
            "or /ai/run. Use the default endpoint template."
        )
    if provider == "openai-compatible" and host == "openrouter.ai":
        raise ValueError("OpenRouter URLs must use the OpenRouter provider policy")


def route_fallbacks(config, route, primary_provider):
    """Return ordered explicit provider:model candidates, including the legacy task fallback."""
    fallbacks = config.get_list("agent.model_routes.%s.fallbacks" % route)
    if fallbacks or route != "task" or config.get("agent.model_routes") is not None:
        return fallbacks
    legacy = str(config.get("agent.llm.fallback_model", "") or "").strip()
    return ["%s:%s" % (primary_provider, legacy)] if legacy else []


def parse_model_reference(reference):
    """Parse an explicit provider:model fallback without guessing a provider."""
    if not isinstance(reference, str) or ":" not in reference:
        raise ValueError("Fallback models must use provider:model identifiers")
    provider, model = reference.split(":", 1)
    provider, model = provider.strip().lower(), model.strip()
    if provider not in PROVIDER_CREDENTIALS or not model:
        raise ValueError("Fallback models must use a supported provider and non-empty model ID")
    return provider, model


def provider_for(config, base_url):
    configured = str(config.get("agent.llm.provider", "auto") or "auto").strip().lower()
    if configured not in PROVIDERS:
        raise ValueError("Unsupported model provider: %s" % configured)
    host = (urlsplit(base_url).hostname or "").lower()
    native_hosts = {
        "openai": "api.openai.com",
        "anthropic": "api.anthropic.com",
        "gemini": "generativelanguage.googleapis.com",
        "cloudflare": "api.cloudflare.com",
        "cloudflare-gateway": "api.cloudflare.com",
    }
    if configured != "auto":
        if host == "openrouter.ai" and configured != "openrouter":
            raise ValueError("OpenRouter URLs must use the OpenRouter provider policy")
        if configured == "openrouter" and host != "openrouter.ai":
            raise ValueError("OpenRouter provider requires an https://openrouter.ai API URL")
        if configured in native_hosts and host != native_hosts[configured]:
            raise ValueError("%s provider requires the official %s API host" % (configured, native_hosts[configured]))
        return configured
    if host == "openrouter.ai":
        return "openrouter"
    if host == "api.openai.com":
        return "openai"
    if host == "api.anthropic.com":
        return "anthropic"
    if host == "generativelanguage.googleapis.com":
        return "gemini"
    if host == "api.cloudflare.com":
        return "cloudflare"
    return "openai-compatible"


def _image_source(value):
    image = value.get("image_url")
    url = image.get("url") if isinstance(image, dict) else image
    if not isinstance(url, str):
        raise ValueError("Unsupported image content for the selected model provider")
    match = re.fullmatch(r"data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=]+)", url)
    if match:
        return {"media_type": match.group(1), "data": match.group(2)}
    if url.startswith("https://"):
        return {"url": url}
    raise ValueError("Only HTTPS image URLs or supported base64 image data are accepted")


def _anthropic_content(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ValueError("Unsupported message content for Claude")
    blocks = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Unsupported message content for Claude")
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            blocks.append({"type": "text", "text": item["text"]})
        elif item.get("type") == "image_url":
            image = _image_source(item)
            source = {"type": "url", "url": image["url"]} if "url" in image else {
                "type": "base64", "media_type": image["media_type"], "data": image["data"],
            }
            blocks.append({"type": "image", "source": source})
        else:
            raise ValueError("Unsupported message content for Claude")
    return blocks


def _tool_calls(message):
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list):
        raise ValueError("Invalid function calls in conversation")
    parsed = []
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ValueError("Invalid function call in conversation")
        arguments = function.get("arguments", "{}")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError as exc:
                raise ValueError("Invalid function arguments in conversation") from exc
        if not isinstance(arguments, dict):
            raise ValueError("Function arguments must be a JSON object")
        parsed.append((str(call.get("id") or ""), function["name"], arguments))
    return parsed


def _anthropic_request(payload):
    messages = []
    system = []
    for message in payload.get("messages", []):
        if not isinstance(message, dict):
            raise ValueError("Invalid message in conversation")
        role = message.get("role")
        content = message.get("content", "")
        if role == "system":
            if isinstance(content, str):
                system.append(content)
            elif isinstance(content, list):
                system.extend(item["text"] for item in content
                              if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str))
            else:
                raise ValueError("Unsupported system content for Claude")
        elif role == "tool":
            tool_id = message.get("tool_call_id")
            if not isinstance(tool_id, str) or not tool_id:
                raise ValueError("Tool result is missing its tool call ID")
            block = {"type": "tool_result", "tool_use_id": tool_id, "content": str(content)}
            if messages and messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
                messages[-1]["content"].append(block)
            else:
                messages.append({"role": "user", "content": [block]})
        elif role in ("user", "assistant"):
            blocks = _anthropic_content(content)
            if role == "assistant":
                if isinstance(blocks, str):
                    blocks = [{"type": "text", "text": blocks}] if blocks else []
                blocks.extend(
                    {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}
                    for tool_id, name, arguments in _tool_calls(message)
                )
            messages.append({"role": role, "content": blocks})
        else:
            raise ValueError("Unsupported message role for Claude")
    result = {
        "model": payload["model"],
        "max_tokens": payload["max_tokens"],
        "messages": messages,
    }
    if system:
        result["system"] = "\n\n".join(system)
    if payload.get("temperature") is not None:
        result["temperature"] = payload["temperature"]
    tools = []
    for item in payload.get("tools") or []:
        function = item.get("function") if isinstance(item, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ValueError("Invalid function tool definition")
        tools.append({
            "name": function["name"],
            "description": str(function.get("description") or ""),
            "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
        })
    if tools:
        result["tools"] = tools
        result["tool_choice"] = {"type": "auto"}
    return result


def _gemini_parts(content):
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}]
    if not isinstance(content, list):
        raise ValueError("Unsupported message content for Gemini")
    parts = []
    for item in content:
        if not isinstance(item, dict):
            raise ValueError("Unsupported message content for Gemini")
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append({"text": item["text"]})
        elif item.get("type") == "image_url":
            image = _image_source(item)
            if "url" in image:
                raise ValueError("Gemini Developer API requires uploaded image files; external image URLs are not supported here")
            parts.append({"inlineData": {"mimeType": image["media_type"], "data": image["data"]}})
        else:
            raise ValueError("Unsupported message content for Gemini")
    return parts


def _gemini_request(payload):
    contents = []
    system = []
    tool_names = {}
    for message in payload.get("messages", []):
        if not isinstance(message, dict):
            raise ValueError("Invalid message in conversation")
        role = message.get("role")
        if role == "system":
            system.extend(part["text"] for part in _gemini_parts(message.get("content", ""))
                          if isinstance(part.get("text"), str))
        elif role == "tool":
            tool_id = message.get("tool_call_id")
            name = tool_names.get(tool_id)
            if not name:
                raise ValueError("Tool result is missing a matching function call")
            try:
                response = json.loads(message.get("content", ""))
            except (TypeError, ValueError):
                response = {"result": str(message.get("content", ""))}
            if not isinstance(response, dict):
                response = {"result": response}
            part = {"functionResponse": {"name": name, "response": response}}
            if contents and contents[-1]["role"] == "user":
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})
        elif role in ("user", "assistant"):
            parts = _gemini_parts(message.get("content", ""))
            if role == "assistant":
                for tool_id, name, arguments in _tool_calls(message):
                    tool_names[tool_id] = name
                    parts.append({"functionCall": {"name": name, "args": arguments}})
            contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})
        else:
            raise ValueError("Unsupported message role for Gemini")
    request = {"contents": contents}
    if system:
        request["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
    generation = {}
    if payload.get("temperature") is not None:
        generation["temperature"] = payload["temperature"]
    if payload.get("max_tokens") is not None:
        generation["maxOutputTokens"] = payload["max_tokens"]
    if generation:
        request["generationConfig"] = generation
    declarations = []
    for item in payload.get("tools") or []:
        function = item.get("function") if isinstance(item, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ValueError("Invalid function tool definition")
        declaration = {"name": function["name"], "parameters": function.get("parameters") or {"type": "object"}}
        if function.get("description"):
            declaration["description"] = function["description"]
        declarations.append(declaration)
    if declarations:
        request["tools"] = [{"functionDeclarations": declarations}]
    return request


def build_request(provider, base_url, api_key, payload):
    """Build an authenticated request for one selected provider."""
    root = base_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    body = payload
    if provider in ("openrouter", "openai", "openai-compatible", "cloudflare", "cloudflare-gateway"):
        url = root + "/chat/completions"
        headers["Authorization"] = "Bearer " + api_key
        if provider in {"cloudflare", "cloudflare-gateway"}:
            headers.update({
                "cf-aig-collect-log": "false",
                "cf-aig-collect-log-payload": "false",
                "cf-aig-skip-cache": "true",
            })
        if provider == "openrouter":
            body = dict(payload)
        if provider == "openai" and payload.get("provider"):
            body = dict(payload)
        if provider == "openai" and payload.get("store") is None:
            body["store"] = False
    elif provider == "anthropic":
        url = root + ("/messages" if root.endswith("/v1") else "/v1/messages")
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        body = _anthropic_request(payload)
    elif provider == "gemini":
        if not isinstance(payload.get("model"), str) or not payload["model"]:
            raise ValueError("Gemini model identifier is required")
        prefix = root if root.endswith("/v1beta") else root + "/v1beta"
        model = quote(payload["model"].removeprefix("models/"), safe="-._")
        url = "%s/models/%s:generateContent" % (prefix, model)
        headers["x-goog-api-key"] = api_key
        body = _gemini_request(payload)
    else:
        raise ValueError("Unsupported model provider: %s" % provider)
    return url, headers, json.dumps(body, default=str).encode("utf-8")


def normalize_response(provider, data):
    """Normalize native Messages and Gemini responses to the task-agent chat schema."""
    if not isinstance(data, dict):
        raise ValueError("Model provider returned an invalid response")
    if provider in ("openrouter", "openai", "openai-compatible", "cloudflare", "cloudflare-gateway"):
        return data
    if provider == "anthropic":
        content = data.get("content")
        if not isinstance(content, list):
            raise ValueError("Claude returned an invalid message response")
        text = []
        calls = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text.append(block["text"])
            elif block.get("type") == "tool_use":
                calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {"name": block.get("name", ""), "arguments": json.dumps(block.get("input") or {})},
                })
        message = {"role": "assistant", "content": "".join(text) or None}
        if calls:
            message["tool_calls"] = calls
        reason = {"tool_use": "tool_calls", "max_tokens": "length"}.get(data.get("stop_reason"), "stop")
        usage = data.get("usage") or {}
        return {
            "id": data.get("id"), "model": data.get("model"),
            "choices": [{"index": 0, "message": message, "finish_reason": reason}],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0),
            },
        }
    if provider == "gemini":
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            raise ValueError("Gemini returned no candidate response")
        candidate = candidates[0]
        content = candidate.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            raise ValueError("Gemini returned an invalid candidate response")
        text = []
        calls = []
        for index, part in enumerate(parts):
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                text.append(part["text"])
            function = part.get("functionCall")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                calls.append({
                    "id": "gemini-call-%s" % index,
                    "type": "function",
                    "function": {"name": function["name"], "arguments": json.dumps(function.get("args") or {})},
                })
        message = {"role": "assistant", "content": "".join(text) or None}
        if calls:
            message["tool_calls"] = calls
        usage = data.get("usageMetadata") or {}
        finish = candidate.get("finishReason")
        reason = "tool_calls" if calls else "length" if finish == "MAX_TOKENS" else "stop"
        return {
            "id": data.get("responseId"), "model": data.get("modelVersion"),
            "choices": [{"index": 0, "message": message, "finish_reason": reason}],
            "usage": {
                "prompt_tokens": usage.get("promptTokenCount", 0),
                "completion_tokens": usage.get("candidatesTokenCount", 0),
                "total_tokens": usage.get("totalTokenCount", 0),
            },
        }
    raise ValueError("Unsupported model provider: %s" % provider)
