"""Selective deletion utility; setup's factory reset rebuilds PostgreSQL entirely."""
import argparse
import json
import os
import shutil
import sys
import tarfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Category:
    title: str
    tables: tuple = ()
    paths: tuple = ()
    requires: tuple = ()
    reason: str = ""
    notes: str = ""


CATEGORIES = {
    "jobs": Category(
        "Jobs, task logs and activity history",
        tables=("manual_events", "supervisor_instructions", "agent_checkpoints", "task_logs", "deep_research_events",
                "deep_research_runs", "dropbox_transfers", "jobs"),
        requires=("reminders",), reason="a queued reminder waits on its job and would never run again",
        notes="Includes deep research runs and Dropbox transfer history."),
    "emails": Category(
        "Received messages, thread summaries and attachments",
        tables=("processed_artifacts", "request_messages", "thread_summaries", "emails"), paths=("artifacts_raw", "artifacts_processed"),
        requires=("jobs",), reason="jobs reply to and quote the email that started them",
        notes="Messages stay in the mailbox; see the mailbox position option."),
    "memories": Category("Memories and memory history", tables=("memory_events", "agent_memories")),
    "reminders": Category("Reminders (including recurring ones)", tables=("reminders",)),
    "calendar": Category(
        "Calendar and Zoom records the assistant created",
        tables=("zoom_managed_meetings", "calendar_event_audit", "calendar_managed_events"),
        notes="Events and meetings stay in your calendar/Zoom; the assistant just stops treating them as its own."),
    "outbound": Category("Sent-message logs", tables=("channel_deliveries", "outbound_email_logs"), notes="Also resets the hourly sending limit."),
    "contacts": Category("Contacts", tables=("contacts",)),
    "model_service": Category(
        "Model presets and model-tool spending history",
        tables=("model_service_calls", "model_presets"),
        notes="Also resets the model tool budget. Generated media stays in the workspace unless workspace files are selected."),
    "workspace_index": Category(
        "Workspace search index and converted-document archive",
        tables=("workspace_file_chunks", "workspace_files", "workspace_document_conversions"), paths=("source_archive",),
        notes="Rebuilt automatically from the remaining workspace files."),
    "network_rules": Category(
        "Sandbox network approvals and blocks", tables=("sandbox_network_rules",),
        notes="Package registries in agent.yaml stay allowed."),
    "caches": Category("Tool-result cache and workspace trash", paths=("tool_cache", "trash")),
    "workspace_files": Category(
        "ALL workspace files in data/share (keeps .assistant/docs)",
        paths=("workspace",), requires=("workspace_index", "caches"),
        reason="the index, cache and trash live in or describe data/share"),
    "runtime_state": Category(
        "Runtime state: cached provider tokens, rate-limit backoffs, pending admin confirmations",
        notes="Provider access tokens are fetched again automatically."),
    "workstreams": Category(
        "All workstreams, ownership and deletion history",
        tables=("provider_mutations", "deleted_thread_references", "telegram_workstream_selections", "workstream_task_notifications",
                "workstream_context_events",
                "workstream_object_events", "workstream_deletions", "workstream_attachments", "workstream_threads",
                "workstream_files", "workstream_objects", "workstreams"),
        requires=("emails", "memories", "reminders", "calendar", "outbound", "contacts", "workspace_files", "runtime_state"),
        reason="workstreams own or retain all task data",
        notes="Also removes all task data; does not delete remote provider objects."),
}

def expand(selected):
    """Return (all categories to delete in CATEGORIES order, {added category: reason it was added})."""
    unknown = set(selected) - CATEGORIES.keys()
    if unknown:
        raise ValueError("unknown categories: " + ", ".join(sorted(unknown)))
    chosen, reasons, frontier = set(selected), {}, list(selected)
    while frontier:
        category = CATEGORIES[frontier.pop()]
        for required in category.requires:
            if required not in chosen:
                chosen.add(required)
                reasons[required] = "required by \"" + category.title + "\": " + category.reason
                frontier.append(required)
    return [key for key in CATEGORIES if key in chosen], reasons


@dataclass
class Layout:
    shared_root: Path
    artifacts_raw: Path
    artifacts_processed: Path
    tool_cache: Path
    source_archive: Path
    trash: Path
    docs: Path = field(init=False)

    def __post_init__(self):
        self.docs = self.shared_root / ".assistant/docs"
        for name in ("artifacts_processed", "tool_cache", "source_archive", "trash", "docs"):
            if not inside(getattr(self, name), self.shared_root) or getattr(self, name) == self.shared_root:
                raise ValueError(name + " must be inside the shared workspace root")

    @classmethod
    def from_config(cls, config):
        shared = Path(os.path.normpath(str(config.get("agent.filesystem.shared_root", "/data/share") or "/data/share")))

        def under_share(path, default):
            value = Path(str(config.get(path, default) or default))
            return Path(os.path.normpath(value if value.is_absolute() else shared / value))

        return cls(shared_root=shared,
                   artifacts_raw=Path(os.path.normpath(str(config.get("agent.artifacts.raw_root", "/data/private/artifacts") or "/data/private/artifacts"))),
                   artifacts_processed=under_share("agent.artifacts.processed_root", "processed"),
                   tool_cache=under_share("agent.tool_result_cache.root", ".assistant/cache/tool-results"),
                   source_archive=under_share("agent.workspace.source_archive_root", ".assistant/archive/source-documents"),
                   trash=under_share("agent.filesystem.trash_directory", str(shared / ".trash")))


def inside(path, root):
    return path == root or root in path.parents


def path_plan(layout, categories):
    """Return [(directory whose contents are deleted, paths inside it to keep)] for the categories."""
    plan = []
    for key in categories:
        for name in CATEGORIES[key].paths:
            if name == "workspace":
                keep = {layout.docs} | ({layout.artifacts_processed} if "emails" not in categories else set())
                plan.append((layout.shared_root, keep))
            else:
                plan.append((getattr(layout, name), set()))
    return plan


def targets(directory, keep):
    """Yield top-level entries to delete under directory without following symlinks, descending only toward kept paths."""
    if directory.is_symlink() or not directory.is_dir():
        return
    with os.scandir(directory) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            path = Path(entry.path)
            if path in keep:
                continue
            if entry.is_dir(follow_symlinks=False) and any(path in kept.parents for kept in keep):
                yield from targets(path, keep)
            else:
                yield path


def all_targets(layout, categories):
    seen = []
    for directory, keep in path_plan(layout, categories):
        for path in targets(directory, keep):
            if not any(inside(path, earlier) for earlier in seen):
                seen.append(path)
    return seen


def archive(layout, categories, output):
    count = 0
    with tarfile.open(fileobj=output, mode="w|gz") as bundle:
        for path in all_targets(layout, categories):
            bundle.add(str(path), arcname=str(path).lstrip("/"), recursive=True)
            count += 1
    return count


def delete_files(layout, categories):
    removed, errors = 0, []
    for path in all_targets(layout, categories):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append("%s: %s" % (path, exc.strerror or type(exc).__name__))
    return removed, errors


def reset_database(connection, categories, keep_mailbox_position=True):
    """Delete selected rows in one transaction and verify the tables are empty before committing."""
    counts = {}
    with connection.transaction(), connection.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'")
        existing = {row[0] if isinstance(row, tuple) else row["table_name"] for row in cur.fetchall()}
        tables = [table for key in categories for table in CATEGORIES[key].tables if table in existing]
        if "workstreams" in categories:
            cur.execute("SELECT set_config('assistant.workstream_cleanup', 'yes', true)")
        for table in tables:
            # Plain DELETE honours ON DELETE SET NULL; TRUNCATE ... CASCADE would empty kept tables too.
            cur.execute('DELETE FROM "%s"' % table)
            counts[table] = max(cur.rowcount or 0, 0)
        if "runtime_state" in categories:
            cur.execute("DELETE FROM runtime_state WHERE NOT (%s AND key LIKE 'imap:%%:last_uid')", (keep_mailbox_position,))
            counts["runtime_state"] = max(cur.rowcount or 0, 0)
        elif not keep_mailbox_position:
            cur.execute("DELETE FROM runtime_state WHERE key LIKE 'imap:%:last_uid'")
            counts["runtime_state"] = max(cur.rowcount or 0, 0)
        if "memories" in categories:
            cur.execute("""INSERT INTO runtime_state(key, value) VALUES ('memory_revision', %s::jsonb)
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                        (json.dumps({"revision": str(uuid.uuid4()), "reason": "factory reset"}),))
        for table in tables:
            cur.execute('SELECT count(*) FROM "%s"' % table)
            row = cur.fetchone()
            if (row[0] if isinstance(row, tuple) else row["count"]) != 0:
                raise RuntimeError(table + " is not empty after reset; nothing was committed")
    return counts


def main():
    parser = argparse.ArgumentParser(description="Selective data deletion utility; not the full setup factory reset")
    parser.add_argument("action", choices=("archive", "apply"))
    parser.add_argument("--categories", required=True, help="comma-separated category keys")
    parser.add_argument("--discard-mailbox-position", action="store_true")
    options = parser.parse_args()
    requested = [item for item in options.categories.split(",") if item]
    categories, added = expand(requested)
    if added:
        raise SystemExit("Refusing incomplete selection; setup must include: " + ", ".join(sorted(added)))
    from .config import database_url, load_config

    config = load_config()
    layout = Layout.from_config(config)
    if options.action == "archive":
        count = archive(layout, categories, sys.stdout.buffer)
        print("Archived %d top-level paths" % count, file=sys.stderr)
        return
    import psycopg

    with psycopg.connect(database_url(config)) as connection:
        counts = reset_database(connection, categories, keep_mailbox_position=not options.discard_mailbox_position)
    removed, errors = delete_files(layout, categories)
    print(json.dumps({"deleted_rows": counts, "deleted_paths": removed, "file_errors": errors}, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
