import re
from email.utils import getaddresses
from typing import Optional


REFERENCE_RE = re.compile(r"<[^>]+>")
SUBJECT_PREFIX_RE = re.compile(r"^\s*((re|fw|fwd):\s*)+", re.IGNORECASE)


def normalize_subject(subject: Optional[str]) -> str:
    if not subject:
        return ""
    normalized = SUBJECT_PREFIX_RE.sub("", subject).strip().lower()
    return re.sub(r"\s+", " ", normalized)


def parse_reference_header(value: Optional[str]) -> list[str]:
    if not value:
        return []
    matches = REFERENCE_RE.findall(value)
    if matches:
        return matches
    return [part.strip() for part in value.split() if part.strip()]


def parse_addresses(value: Optional[str]) -> list[str]:
    if not value:
        return []
    addresses = []
    for _name, address in getaddresses([value]):
        if address:
            addresses.append(address.lower())
    return addresses


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned or "attachment"


def safe_message_segment(message_id: str) -> str:
    return safe_filename(message_id.strip("<>"))[:120]
