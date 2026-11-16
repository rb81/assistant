"""Separate a new email reply from the quoted history mail clients append beneath it."""

import re

_ATTRIBUTION = re.compile(r"On\b[^\n]{0,300}?\bwrote:\s*$", re.IGNORECASE)
_ORIGINAL = re.compile(r"-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE)
_HEADER = re.compile(r"(From|Sent|Date|To|Subject):\s", re.IGNORECASE)


def _history_starts(lines, index):
    line = lines[index].strip()
    # Attributions such as "On Mon, 3 Mar 2025 at 10:00, Name <a@b> wrote:" may wrap over several lines.
    for span in range(1, 5):
        joined = " ".join(item.strip() for item in lines[index:index + span])
        if _ATTRIBUTION.fullmatch(joined):
            return True
    if _ORIGINAL.fullmatch(line):
        return True
    # Outlook-style header blocks: From: followed closely by Sent:/Date:.
    if line.lower().startswith("from:") and any(
            _HEADER.match(item.strip()) and not item.strip().lower().startswith("from:")
            for item in lines[index + 1:index + 4]):
        return True
    # A trailing block of ">" lines is history; interleaved quotes are part of the reply.
    remaining = [item.strip() for item in lines[index:] if item.strip()]
    return line.startswith(">") and all(item.startswith(">") for item in remaining)


def strip_quoted_history(text):
    """Return only the new part of an email body, or the whole body when nothing new remains."""
    if not isinstance(text, str) or not text:
        return text
    lines = text.splitlines()
    for index in range(len(lines)):
        if _history_starts(lines, index):
            kept = lines[:index]
            # Outlook draws a rule above its header block; it belongs to the history.
            while kept and (not kept[-1].strip() or set(kept[-1].strip()) <= set("_-")):
                kept.pop()
            reply = "\n".join(kept)
            return reply if reply.strip() else text
    return text
