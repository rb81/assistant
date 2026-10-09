import os
import re
from pathlib import Path


SECRET_NAMES = frozenset({
    "POSTGRES_PASSWORD", "DATABASE_URL", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
    "IMAP_PASSWORD", "SMTP_PASSWORD", "ADMIN_COMMAND_SIGNING_KEY", "AGENT_API_AUTH_TOKEN",
    "SANDBOX_API_TOKEN",
    "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET", "ZOOM_ACCOUNT_ID", "ZOOM_REFRESH_TOKEN",
    "DROPBOX_CLIENT_ID", "DROPBOX_REFRESH_TOKEN",
    *{f"CALENDAR_{provider}_{field}" for provider in ("GOOGLE", "OUTLOOK", "ZOHO")
      for field in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN")},
})


def is_secret_name(name: str) -> bool:
    return name in SECRET_NAMES or bool(re.fullmatch(
        r"CALENDAR_(GOOGLE|OUTLOOK|ZOHO)_[2-9]_(CLIENT_ID|CLIENT_SECRET|REFRESH_TOKEN)", name
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