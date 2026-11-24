"""Detect repeated unchanged reads without treating discovery as useful progress."""
import hashlib
import json
from collections import deque


READ_TOOLS = {
    "job_list", "job_read", "job_search", "reminder_list", "reminder_read",
    "context_search", "memory_search", "file_read", "file_list", "web_fetch", "web_search",
    "workstream_list", "workstream_read", "calendar_list_events", "help_search", "help_topic",
}


class ToolProgress:
    def __init__(self, limit=3):
        self.limit = max(2, limit)
        self.recent = deque(maxlen=max(16, self.limit * 4))
        self.should_stop = False

    def observe(self, name, arguments, result):
        self.should_stop = False
        if result.get("status") == "not_executed":
            return ""
        failed = bool(result.get("error"))
        if name not in READ_TOOLS and not failed:
            return ""
        stable = {key: value for key, value in result.items()
                  if not key.startswith("cached_output_") and key != "retrieval"}
        digest = hashlib.sha256(json.dumps(
            [name, arguments, stable], sort_keys=True, default=str, ensure_ascii=True,
        ).encode()).hexdigest()
        self.recent.append(digest)
        count = self.recent.count(digest)
        self.should_stop = count > self.limit
        if count >= self.limit:
            return "%s returned unchanged %s %s times in recent execution" % (
                name, "failures" if failed else "evidence", count,
            )
        return ""
