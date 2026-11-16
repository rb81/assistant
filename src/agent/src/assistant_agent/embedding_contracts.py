"""Embedding source names shared with the lightweight management boundary."""
from enum import StrEnum


class EmbeddingTable(StrEnum):
    MEMORIES = "agent_memories"
    WORKSPACE = "workspace_file_chunks"
    JOBS = "jobs"
    REMINDERS = "reminders"
    OUTBOUND_EMAIL = "outbound_email_logs"
    EMAIL = "emails"
    CONTACTS = "contacts"
