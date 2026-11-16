"""Bounded source-configuration editing; credentials never belong in YAML backups."""
import math
import re
from datetime import datetime, timezone

import yaml

from . import settings_schema
from .config import AppConfig
from .setup import SetupError, read_regular


BACKUP_PATTERN = re.compile(r"agent\.yaml\.bk_[0-9]{8}_[0-9]{6}_[0-9]{6}")
MAX_YAML_BYTES = 512 * 1024
BACKUP_LIMIT = 3


def validate_yaml(text):
    """Validate the existing schema, preserving comments in the submitted text."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_YAML_BYTES:
        raise SetupError("agent.yaml must be UTF-8 text no larger than 512 KiB.")
    try:
        if any(isinstance(event, yaml.events.AliasEvent) for event in yaml.parse(text)):
            raise SetupError("YAML aliases are not supported in client-edited source configuration.")
        values = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError):
        raise SetupError("agent.yaml must contain valid YAML.") from None
    if not isinstance(values, dict) or set(values) != {"agent"} or not isinstance(values["agent"], dict):
        raise SetupError("agent.yaml must contain an agent mapping.")
    catalog = {item["path"]: item for item in settings_schema.catalog()}
    leaves = {}
    pending = [("", values)]
    while pending:
        path, value = pending.pop()
        if path in catalog or not isinstance(value, dict) or not value:
            leaves[path] = value
        else:
            if len(path) > 1024 or len(pending) > 4096:
                raise SetupError("agent.yaml contains excessive nesting or recursive aliases.")
            if any(not isinstance(key, str) for key in value):
                raise SetupError("Setting names must be strings.")
            pending.extend(((path + "." if path else "") + key, child) for key, child in value.items())
    unknown = set(leaves) - set(catalog)
    missing = set(catalog) - set(leaves)
    if unknown or missing:
        raise SetupError("Keep the existing agent.yaml setting paths; use a server upgrade to change its schema.")
    for path, value in leaves.items():
        spec = catalog[path]
        if spec["secret"] and value not in (None, ""):
            raise SetupError("Store credentials in Settings, not agent.yaml: " + path)
        if value is None:
            continue
        expected = dict if isinstance(spec["default"], dict) else spec["type"]
        valid = (
            type(value) is expected or
            expected is float and type(value) is int
        )
        if not valid:
            raise SetupError("Invalid value type for " + path)
        if expected is float and not math.isfinite(value):
            raise SetupError("Use a finite number for " + path)
    return AppConfig(values)


def backups(directory):
    """Return only the three most recent regular, fixed-name source backups."""
    names = sorted(
        (path.name for path in directory.iterdir() if BACKUP_PATTERN.fullmatch(path.name)),
        reverse=True,
    )
    return [name for name in names if read_regular(directory / name) is not None][:BACKUP_LIMIT]


def backup_writes(directory, previous):
    """Prepare backup rotation in the same rollback-capable transaction as the save."""
    name = "agent.yaml.bk_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    writes = {directory / name: previous}
    existing = sorted(
        (path for path in directory.iterdir() if BACKUP_PATTERN.fullmatch(path.name)),
        reverse=True,
    )
    for path in existing[BACKUP_LIMIT - 1:]:
        read_regular(path)
        writes[path] = None
    return writes
