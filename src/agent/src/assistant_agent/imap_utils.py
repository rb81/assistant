from typing import Any


def imap_status_ok(status: Any) -> bool:
    if isinstance(status, bytes):
        text = status.decode("ascii", errors="ignore")
    else:
        text = str(status)
    return text.strip().upper() == "OK"


def imap_mailbox_arg(folder: str) -> str:
    text = str(folder or "").strip()
    if not text:
        return '""'
    if text.startswith('"') and text.endswith('"'):
        return text
    if not any(character in text for character in (' ', "\t", "\r", "\n", '"', "\\")) and text.upper() != "NIL":
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % escaped
