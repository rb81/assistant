"""Settings catalog derived from agent.yaml plus the agent.schema.yaml metadata sidecar.

agent.yaml is the single source of truth for which settings exist and their defaults.
Every leaf is overridable through an environment variable whose name is derived from its
path (agent.llm.model -> AGENT_LLM_MODEL) and whose type is inferred from its default.
There is no alias table: the sidecar only carries type overrides, the secret flag and
presentation metadata, and its keys must match agent.yaml paths.
"""
import logging
import os
from pathlib import Path
from typing import Any, Optional

import yaml

LOGGER = logging.getLogger(__name__)
PREFIX = "agent."

_CASTERS = {bool: bool, int: int, float: float, list: list}


def defaults_dir() -> Path:
    configured = os.getenv("ASSISTANT_DEFAULTS_DIR")
    if configured:
        return Path(configured)
    packaged = Path("/app/defaults")
    if (packaged / "agent.yaml").is_file():
        return packaged
    return Path(__file__).resolve().parents[3] / "config"


def _load(name: str) -> dict[str, Any]:
    path = defaults_dir() / name
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def leaves(node: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, Any]]:
    if isinstance(node, dict) and node:
        found: list[tuple[str, Any]] = []
        for key, value in node.items():
            found.extend(leaves(value, prefix + (str(key),)))
        return found
    return [(".".join(prefix), node)]


def derived_env_name(path: str) -> str:
    return path.upper().replace(".", "_")


def sidecar_types(sidecar: dict[str, Any]) -> dict[str, type]:
    names = {"bool": bool, "int": int, "float": float, "list": list, "str": str}
    return {path: names[meta["type"]] for path, meta in (sidecar.get("settings") or {}).items()
            if isinstance(meta, dict) and meta.get("type") in names}


def build_catalog() -> list[dict[str, Any]]:
    defaults, sidecar = _load("agent.yaml"), _load("agent.schema.yaml")
    metadata = sidecar.get("settings") or {}
    types = sidecar_types(sidecar)
    catalog = []
    for path, default in leaves(defaults):
        if not path.startswith(PREFIX):
            continue
        meta = metadata.get(path) or {}
        catalog.append({
            "path": path,
            "env": derived_env_name(path),
            "type": types.get(path) or _CASTERS.get(type(default), str),
            "default": default,
            "secret": bool(meta.get("secret")),
            "metadata": meta,
        })
    known = {item["path"] for item in catalog}
    for path in sorted(set(metadata) - known):
        LOGGER.info("agent.schema.yaml entry %s has no matching setting in agent.yaml; ignored", path)
    return catalog


_CATALOG: Optional[list[dict[str, Any]]] = None


def catalog() -> list[dict[str, Any]]:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = build_catalog()
    return _CATALOG


def catalog_by_env() -> dict[str, dict[str, Any]]:
    return {item["env"]: item for item in catalog()}


def env_overrides() -> list[tuple[str, str, Optional[type]]]:
    """Every setting as (environment name, path, type); secrets arrive through *_FILE."""
    return [(item["env"], item["path"], item["type"]) for item in catalog()]


def secret_settings() -> dict[str, str]:
    """Credential name -> agent.yaml path."""
    return {item["env"]: item["path"] for item in catalog() if item["secret"]}
