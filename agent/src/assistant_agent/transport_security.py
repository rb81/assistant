from __future__ import annotations

import ssl
import urllib.request
from typing import Any
from urllib.parse import SplitResult, urlsplit


def validate_https_url(value: str, label: str) -> SplitResult:
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        parsed = None
    if (
        parsed is None
        or parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("%s must be a valid HTTPS URL without embedded credentials" % label)
    return parsed


def validate_embedding_url(value: str) -> SplitResult:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        parsed = None
        port = None
    if parsed is not None and parsed.scheme.lower() == "https":
        return validate_https_url(value, "Embedding API URL")
    if (
        parsed is not None
        and parsed.scheme.lower() == "http"
        and parsed.hostname == "ollama"
        and port == 11434
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and parsed.path in ("", "/")
    ):
        return parsed
    raise RuntimeError("Embedding API URL must use HTTPS except for the managed local Ollama endpoint")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_https_request(request: urllib.request.Request, timeout: float, context: ssl.SSLContext | None = None) -> Any:
    tls_context = context or ssl.create_default_context()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=tls_context),
        _NoRedirectHandler(),
    )
    return opener.open(request, timeout=timeout)


def open_internal_http_request(request: urllib.request.Request, timeout: float) -> Any:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPHandler(),
        _NoRedirectHandler(),
    )
    return opener.open(request, timeout=timeout)