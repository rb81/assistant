"""Provider model discovery using credentials held only by the management server."""
import copy
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import AppConfig
from .llm_providers import (
    PROVIDER_CREDENTIALS, provider_base_url, provider_credential_name, provider_for, provider_is_disabled,
)
from .openrouter_policy import SAFETY_RETENTION_WARNING, is_openrouter_url, require_zdr
from .secret_store import read_secret
from .transport_security import open_https_request, validate_https_url


MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_GATEWAY_CATALOG_BYTES = 64 * 1024 * 1024
MAX_MODELS = 1000
_cache_lock = threading.Lock()
_catalog_cache = {}


class ModelCatalogError(RuntimeError):
    """A provider model catalog could not be loaded safely."""


def _read_catalog(request, *, max_bytes=MAX_CATALOG_BYTES):
    try:
        with open_https_request(request, timeout=30) as response:
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise ModelCatalogError("Provider model discovery failed (HTTP %s)" % exc.code) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ModelCatalogError("Provider model discovery failed: %s" % exc) from exc
    if len(raw) > max_bytes:
        raise ModelCatalogError("Provider model catalog exceeds the %d MiB limit" % (max_bytes // (1024 * 1024)))
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
    elif provider == "cloudflare-gateway":
        rows = data.get("result")
        key = "model_id"
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
        if provider == "cloudflare-gateway":
            formats = row.get("request_formats")
            if row.get("task") != "Text Generation":
                continue
            if formats is not None and (not isinstance(formats, list)
                                        or not all(isinstance(item, str) for item in formats)):
                raise ModelCatalogError("Cloudflare returned invalid model request formats")
            if formats is not None and "chat-completions" not in formats:
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
    elif provider in {"cloudflare", "cloudflare-gateway"}:
        if (parsed.hostname or "").lower() != "api.cloudflare.com":
            raise ModelCatalogError("Cloudflare model discovery requires the official Cloudflare API host")
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 4 or parts[0] != "client" or parts[1] != "v4" or parts[2] != "accounts":
            raise ModelCatalogError("Cloudflare model discovery requires an account-specific Workers AI URL")
        account_id = parts[3]
        if not account_id or account_id in {".", ".."}:
            raise ModelCatalogError("Cloudflare account ID is missing from the Workers AI URL")
        path = ("models/search?page=1&per_page=1000" if provider == "cloudflare"
                else "catalog/models?page=1&per_page=100")
        url = "https://api.cloudflare.com/client/v4/accounts/%s/ai/%s" % (
            urllib.parse.quote(account_id, safe=""), path)
        headers["Authorization"] = "Bearer " + api_key
    else:
        raise ModelCatalogError("Model discovery is unavailable for the selected provider")
    endpoint = urllib.parse.urlsplit(url)
    validate_https_url(urllib.parse.urlunsplit(endpoint._replace(query="", fragment="")),
                       "Provider model discovery URL")
    return urllib.request.Request(url, headers=headers, method="GET")


def _gateway_models(request):
    rows = []
    for page in range(1, MAX_MODELS // 100 + 2):
        url = request.full_url.replace("page=1&per_page=100", "page=%d&per_page=100" % page)
        data = _read_catalog(urllib.request.Request(url, headers=dict(request.header_items()), method="GET"),
                             max_bytes=MAX_GATEWAY_CATALOG_BYTES)
        result, info = data.get("result"), data.get("result_info")
        if (data.get("success") is not True or not isinstance(result, list) or not isinstance(info, dict)
                or type(info.get("total_count")) is not int or info["total_count"] < 0
                or info["total_count"] > MAX_MODELS):
            raise ModelCatalogError("Cloudflare returned an invalid AI Gateway model catalog")
        if not all(isinstance(row, dict) for row in result):
            raise ModelCatalogError("Provider returned an invalid model entry")
        rows.extend({key: row.get(key) for key in ("model_id", "name", "displayName", "task", "request_formats")}
                    for row in result)
        if len(rows) > info["total_count"]:
            raise ModelCatalogError("Cloudflare returned an inconsistent AI Gateway model catalog")
        if len(rows) == info["total_count"]:
            return _models({"result": rows}, "cloudflare-gateway")
        if not result:
            raise ModelCatalogError("Cloudflare returned an incomplete AI Gateway model catalog")
        # Do not retain the previous page's model schemas while fetching the next.
        del data, result
    raise ModelCatalogError("Cloudflare AI Gateway model catalog exceeds the model limit")


def discover_models(config: AppConfig, provider_id=None, *, credential_reader=None):
    base_url = str(config.get("agent.llm.base_url", "")).rstrip("/")
    if provider_id:
        provider = str(provider_id).strip().lower()
        if provider not in PROVIDER_CREDENTIALS:
            raise ModelCatalogError("Unsupported model provider")
        if provider_is_disabled(config, provider):
            raise ModelCatalogError("Model discovery is disabled for this provider")
        try:
            base_url = provider_base_url(config, provider)
        except ValueError as exc:
            raise ModelCatalogError(str(exc)) from exc
    else:
        provider = provider_for(config, base_url)
    if provider == "openrouter":
        from .model_service import ModelServiceError, OpenRouterModels
        zdr = require_zdr(config)
        try:
            values = copy.deepcopy(config.values)
            llm = values.setdefault("agent", {}).setdefault("llm", {})
            llm["provider"] = "openrouter"
            llm["base_url"] = base_url
            models = OpenRouterModels(AppConfig(values), credential_reader=credential_reader).models(zdr_only=zdr)
        except ModelServiceError as exc:
            raise ModelCatalogError(str(exc)) from exc
        return {
            "provider": provider,
            "models": [{"id": model["id"], "name": model.get("name") or model["id"]} for model in models],
            "zdr_only": zdr,
            "warning": SAFETY_RETENTION_WARNING,
        }

    zdr = require_zdr(config)
    if zdr:
        if provider == "gemini":
            raise ModelCatalogError("Gemini Developer API does not provide strict Zero Data Retention")
        if provider not in {"openai", "anthropic", "cloudflare", "cloudflare-gateway"}:
            raise ModelCatalogError("Strict Zero Data Retention blocks this provider")
        if provider not in config.get_list("agent.llm.zdr_verified_providers"):
            raise ModelCatalogError("Verify the provider account and add it to the verified ZDR provider list")

    credential_reader = credential_reader or read_secret
    credential_name = provider_credential_name(provider, credential_reader)
    api_key = credential_reader(credential_name)
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
        models = _gateway_models(request) if provider == "cloudflare-gateway" else _models(_read_catalog(request), provider)
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
    elif provider == "cloudflare-gateway":
        warning = ("Stored default provider keys take precedence over Unified Billing. "
                   "Verify managed credentials, model eligibility, Gateway logging, caching, and Logpush settings.")
    elif zdr and provider in {"openai", "anthropic"}:
        warning = "Verify that this account and each selected model remain eligible for strict ZDR."
    return {"provider": provider, "models": models, "zdr_only": zdr, "warning": warning}
