import json
import logging
import math
import urllib.error
import urllib.request
from typing import Any, Optional

from .config import AppConfig
from .transport_security import open_https_request, open_internal_http_request, validate_embedding_url


LOGGER = logging.getLogger("assistant.embedding_client")


class EmbeddingClient:
    def __init__(self, config: AppConfig):
        self.config = config
        self.enabled = config.get_bool(
            "agent.embeddings.enabled",
            config.get_bool("agent.memory.embeddings.enabled", True),
        )
        self.base_url = str(
            config.get(
                "agent.embeddings.base_url",
                config.get("agent.memory.embeddings.base_url", "http://ollama:11434"),
            )
        ).rstrip("/")
        endpoint = validate_embedding_url(self.base_url)
        self._uses_internal_http = endpoint.scheme.lower() == "http"
        self.model = str(
            config.get(
                "agent.embeddings.model",
                config.get("agent.memory.embeddings.model", "embeddinggemma"),
            )
        )
        self.timeout_seconds = config.get_int(
            "agent.embeddings.timeout_seconds",
            config.get_int("agent.memory.embeddings.timeout_seconds", 20),
        )
        dimensions = config.get("agent.embeddings.dimensions", config.get("agent.memory.embeddings.dimensions"))
        try:
            self.dimensions: Optional[int] = int(dimensions) if dimensions not in (None, "") else None
        except (TypeError, ValueError):
            self.dimensions = None

    def embed(self, text: str) -> list[float]:
        if not self.enabled:
            raise RuntimeError("memory embeddings are disabled")
        clean_text = str(text or "").strip()
        if not clean_text:
            raise RuntimeError("embedding text is required")

        payload: dict[str, Any] = {
            "model": self.model,
            "input": clean_text,
        }
        if self.dimensions:
            payload["dimensions"] = self.dimensions
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            "%s/api/embed" % self.base_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            open_request = open_internal_http_request if self._uses_internal_http else open_https_request
            with open_request(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("embedding request failed with HTTP %s: %s" % (exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("embedding request failed: %s" % exc) from exc

        embeddings = data.get("embeddings") or []
        if not embeddings or not isinstance(embeddings[0], list):
            raise RuntimeError("embedding response did not include a vector")
        vector = [float(value) for value in embeddings[0]]
        if not vector or not all(math.isfinite(value) for value in vector) or not any(vector):
            raise RuntimeError("embedding response did not include a finite nonzero vector")
        if self.dimensions and len(vector) != self.dimensions:
            raise RuntimeError("embedding response dimensions do not match the requested dimensions")
        return vector

    def compatible(self, row: dict[str, Any], dimensions: int) -> bool:
        return (row.get("embedding_model") == self.model
                and row.get("embedding_dimensions") == dimensions
                and len(row.get("embedding") or []) == dimensions)
