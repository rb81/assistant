"""Provider model discovery using credentials held only by the management server."""
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import AppConfig
from .llm_providers import PROVIDER_CREDENTIALS, provider_for
from .openrouter_policy import is_openrouter_url, require_zdr
from .secret_store import read_secret
from .transport_security import open_https_request, validate_https_url


MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_MODELS = 1000
_cache_lock = threading.Lock()
_catalog_cache = {}


class ModelCatalogError(RuntimeError):
    """A provider model catalog could not be loaded safely."""


def _read_catalog(request):
    try:
        with open_https_request(request, timeout=30) as response:
            raw = response.read(MAX_CATALOG_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ModelCatalogError("Provider model discovery failed (HTTP %s)" % exc.code) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ModelCatalogError("Provider model discovery failed: %s" % exc) from exc
    if len(raw) > MAX_CATALOG_BYTES:
        raise ModelCatalogError("Provider model catalog exceeds the 2 MiB limit")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ModelCatalogError("Provider returned invalid model catalog JSON") from exc
    if not isinstance(data, dict):
        raise ModelCatalogError("Provider returned an invalid model catalog")
    return data


def _models(data, provider):
    if provider in {"openai", "openai-compatible", "anthropic"}:
        rows = data.get("data")
        key = "id"
    elif provider == "gemini":
        rows = data.get("models")
        key = "name"
    elif provider == "cloudflare":
        rows = data.get("result")
        key = "name"
    else:
        raise ModelCatalogError("Unsupported provider model catalog")
    if not isinstance(rows, list) or len(rows) > MAX_MODELS:
        raise ModelCatalogError("Provider returned an invalid model list")
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ModelCatalogError("Provider returned an invalid model entry")
        identifier = row.get(key)
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ModelCatalogError("Provider returned an invalid model identifier")
        if provider == "gemini":
            methods = row.get("supportedGenerationMethods")
            if not isinstance(methods, list) or not all(isinstance(item, str) for item in methods):
                raise ModelCatalogError("Gemini returned invalid model capabilities")
            if "generateContent" not in methods:
                continue
        if provider == "cloudflare" and not identifier.startswith("@cf/"):
            continue
        seen.add(identifier)
        result.append({"id": identifier, "name": str(row.get("displayName") or row.get("name") or identifier)})
    return sorted(result, key=lambda model: (model["name"].casefold(), model["id"]))


def _request(provider, base_url, api_key):
    parsed = validate_https_url(base_url, "Model API base URL")
    root = base_url.rstrip("/")
    headers = {"Accept": "application/json"}
    if provider in {"openai", "openai-compatible"}:
        if not is_openrouter_url(root):
            headers["Authorization"] = "Bearer " + api_key
        url = root + ("/models" if root.endswith("/v1") else "/v1/models")
    elif provider == "anthropic":
        url = root + ("/models?limit=1000" if root.endswith("/v1") else "/v1/models?limit=1000")
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    elif provider == "gemini":
        url = root + ("/models?pageSize=1000" if root.endswith("/v1beta") else "/v1beta/models?pageSize=1000")
        headers["x-goog-api-key"] = api_key
    elif provider == "cloudflare":
        if (parsed.hostname or "").lower() != "api.cloudflare.com":
            raise ModelCatalogError("Cloudflare model discovery requires the official Cloudflare API host")
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 4 or parts[0] != "client" or parts[1] != "v4" or parts[2] != "accounts":
            raise ModelCatalogError("Cloudflare model discovery requires an account-specific Workers AI URL")
        account_id = parts[3]
        if not account_id or account_id in {".", ".."}:
            raise ModelCatalogError("Cloudflare account ID is missing from the Workers AI URL")
        url = (
            "https://api.cloudflare.com/client/v4/accounts/%s/ai/models/search?page=1&per_page=1000"
            % urllib.parse.quote(account_id, safe="")
        )
        headers["Authorization"] = "Bearer " + api_key
    else:
        raise ModelCatalogError("Model discovery is unavailable for the selected provider")
    endpoint = urllib.parse.urlsplit(url)
    validate_https_url(urllib.parse.urlunsplit(endpoint._replace(query="", fragment="")),
                       "Provider model discovery URL")
    return urllib.request.Request(url, headers=headers, method="GET")


def discover_models(config: AppConfig):
    base_url = str(config.get("agent.llm.base_url", "")).rstrip("/")
    provider = provider_for(config, base_url)
    if provider == "openrouter":
        from .model_service import ModelServiceError, OpenRouterModels
        zdr = require_zdr(config)
        try:
            models = OpenRouterModels(config).models(zdr_only=zdr)
        except ModelServiceError as exc:
            raise ModelCatalogError(str(exc)) from exc
        return {
            "provider": provider,
            "models": [{"id": model["id"], "name": model.get("name") or model["id"]} for model in models],
            "zdr_only": zdr,
            "warning": "OpenRouter Safety Retention endpoints require a separate account policy or guardrail.",
        }

    zdr = require_zdr(config)
    if zdr:
        if provider == "gemini":
            raise ModelCatalogError("Gemini Developer API does not provide strict Zero Data Retention")
        if provider not in {"openai", "anthropic", "cloudflare"}:
            raise ModelCatalogError("Strict Zero Data Retention blocks this provider")
        if provider not in config.get_list("agent.llm.zdr_verified_providers"):
            raise ModelCatalogError("Verify the provider account and add it to the verified ZDR provider list")

    credential_name = PROVIDER_CREDENTIALS[provider]
    api_key = read_secret(credential_name)
    if provider == "openai-compatible" and not api_key:
        api_key = read_secret("AGENT_LLM_OPENROUTER_API_KEY")
    if not api_key:
        raise ModelCatalogError("%s is required for model discovery" % credential_name)

    key = (provider, base_url, hashlib.sha256(api_key.encode()).hexdigest())
    with _cache_lock:
        cached = _catalog_cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            models = cached[1]
        else:
            models = None
    if models is None:
        try:
            request = _request(provider, base_url, api_key)
        except ModelCatalogError:
            raise
        except RuntimeError as exc:
            raise ModelCatalogError(str(exc)) from exc
        models = _models(_read_catalog(request), provider)
        with _cache_lock:
            if len(_catalog_cache) >= 64:
                oldest = min(_catalog_cache, key=lambda item: _catalog_cache[item][0])
                del _catalog_cache[oldest]
            _catalog_cache[key] = (time.monotonic(), models)
    if zdr:
        approved = set(config.get_list("agent.llm.zdr_verified_models"))
        models = [model for model in models if provider + ":" + model["id"] in approved]
    warning = None
    if provider == "cloudflare":
        warning = "Verify AI Gateway logging, caching, and Logpush settings; request headers do not disable Logpush."
    elif zdr and provider in {"openai", "anthropic"}:
        warning = "Verify that this account and each selected model remain eligible for strict ZDR."
    return {"provider": provider, "models": models, "zdr_only": zdr, "warning": warning}
