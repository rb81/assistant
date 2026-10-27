import logging
import os
from typing import Optional

from .config import AppConfig


LOGGER = logging.getLogger("assistant.filesystem_permissions")
DEFAULT_SHARED_FILE_UMASK = "0002"


def parse_umask(value: str) -> int:
    clean = str(value or "").strip()
    if clean.startswith("0o"):
        clean = clean[2:]
    if not clean:
        clean = DEFAULT_SHARED_FILE_UMASK
    try:
        parsed = int(clean, 8)
    except ValueError as exc:
        raise ValueError("umask must be an octal value such as 0002 or 0022") from exc
    if parsed < 0 or parsed > 0o777:
        raise ValueError("umask must be between 0000 and 0777")
    return parsed


def apply_shared_file_umask(config: Optional[AppConfig] = None) -> int:
    """Apply the process umask used for files created in the shared workspace.

    The process umask is global, so this intentionally affects all files created
    by the agent process. Writable paths are constrained to the shared
    workspace for file tools and sandbox command execution.
    """
    raw_value = config.get("agent.filesystem.shared_file_umask", DEFAULT_SHARED_FILE_UMASK) if config else DEFAULT_SHARED_FILE_UMASK
    umask = parse_umask(raw_value)
    previous = os.umask(umask)
    LOGGER.info("applied shared file umask", extra={"umask": "%04o" % umask, "previous_umask": "%04o" % previous})
    return umask
