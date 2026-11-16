import os
import re
from pathlib import Path

from .settings_schema import secret_settings


# Credentials are the agent.yaml settings flagged `secret: true` in agent.schema.yaml.
SECRET_NAMES = frozenset(secret_settings())


def is_secret_name(name: str) -> bool:
    return name in SECRET_NAMES or bool(re.fullmatch(
        r"AGENT_CALENDAR_(GOOGLE|OUTLOOK|ZOHO)_[2-9]_(CLIENT_ID|CLIENT_SECRET|REFRESH_TOKEN)", name
    ))


def read_secret(name: str, default: str | None = None) -> str | None:
    inline = os.getenv(name)
    pointer = os.getenv(name + "_FILE")
    if pointer is None:
        return inline if inline else default
    if inline:
        raise RuntimeError(f"Set only {name} or {name}_FILE, not both")
    try:
        path = Path(pointer)
        if not pointer or not path.is_file() or path.stat().st_size > 1048576:
            raise ValueError
        value = path.read_text(encoding="utf-8")
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
        if not value or "\x00" in value:
            raise ValueError
        return value
    except (OSError, ValueError):
        raise RuntimeError(f"{name}_FILE must reference a readable, nonempty UTF-8 secret file") from None


def configured_secret_names() -> list[str]:
    names = {key.removesuffix("_FILE") for key in os.environ if is_secret_name(key.removesuffix("_FILE"))}
    return sorted(names)