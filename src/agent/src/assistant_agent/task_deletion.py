"""Safe task-execution deletion that retains the conversation and project object."""
import hashlib
import json

from psycopg.types.json import Jsonb

from .database import json_safe


TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
ACTIVE_STATUSES = ("queued", "running", "waiting", "needs_review")
DELETED_TASK_MARKER = "[Deleted task]"


class TaskDeletionError(ValueError):
    pass


class DeletedTaskError(TaskDeletionError):
    def __init__(self, marker):
        self.marker = marker
        super().__init__("task was deleted; its conversation is preserved and read-only")


class TaskDeletionService:
    """Preview and delete task execution data without erasing its conversation."""

    def __init__(self, db):
        self.db = db

    def get_tombstone(self, task_id):
        if type(task_id) is not int or task_id <= 0:
            raise TaskDeletionError("invalid task identifier")
        task = self.db.fetch_one(
            """SELECT id, status, deleted_at, thread_id, workstream_id, created_at,
                      updated_at, completed_at
               FROM jobs WHERE id = %s""",
            (task_id,),
        )
        if task is None:
            raise TaskDeletionError("task not found")
        if task["deleted_at"] is None and task["status"] != "deleted":
            return None
        return json_safe({
            "id": task["id"],
            "task_id": task["id"],
            "status": "deleted",
            "deleted": True,
            "task_summary": DELETED_TASK_MARKER,
            "marker": DELETED_TASK_MARKER,
            "deleted_at": task["deleted_at"],
            "thread_id": task["thread_id"],
            "workstream_id": task["workstream_id"],
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
            "completed_at": task["completed_at"],
        })

    def assert_operable(self, task_id, *task_ids):
        """Reject mutations/replies; call inside their transaction so its locks span the action.

        Pass every task an action references in one call so locks are taken in canonical order.
        """
        with self.db.connect() as connection:
            tasks = self._lock_tasks(connection, (task_id,) + task_ids)
            for identifier, task in sorted(tasks.items()):
                if task["deleted_at"] is not None or task["status"] == "deleted":
                    raise DeletedTaskError(self.get_tombstone(identifier))

    def _lock_tasks(self, connection, task_ids, *, exclusive=False):
        """Lock in the system-wide order: request threads, maintenance, task-deletion keys, job rows."""
        ids = sorted(set(task_ids))
        if not ids or any(type(identifier) is not int or identifier <= 0 for identifier in ids):
            raise TaskDeletionError("invalid task identifier")
        # Unlocked read only discovers which request locks to take; the locked re-read below verifies it.
        threads = connection.execute(
            """SELECT DISTINCT thread_id COLLATE "C" AS thread_id FROM jobs
               WHERE id = ANY(%s::bigint[]) ORDER BY 1""",
            (ids,),
        ).fetchall()
        for row in threads:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("request:" + row["thread_id"],))
        connection.execute("SELECT pg_advisory_xact_lock_shared(hashtext('workstream-maintenance'))")
        task_lock = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
        for identifier in ids:
            connection.execute("SELECT " + task_lock + "(hashtext(%s))", ("task-deletion:" + str(identifier),))
        rows = connection.execute(
            """SELECT j.id, j.thread_id, j.workstream_id, j.object_id, j.status, j.updated_at,
                      j.deleted_at, j.locked_at, j.locked_by,
                      o.deleting AS object_deleting,
                      EXISTS (SELECT 1 FROM workstreams w
                              WHERE w.status = 'deleting'
                                AND (w.id = j.workstream_id OR w.id IN (
                                  SELECT a.workstream_id FROM workstream_attachments a
                                  WHERE a.object_id = j.object_id))) AS project_deleting
               FROM jobs j JOIN workstream_objects o ON o.id = j.object_id
               WHERE j.id = ANY(%s::bigint[]) ORDER BY j.id"""
            + (" FOR UPDATE OF j" if exclusive else " FOR KEY SHARE OF j"),
            (ids,),
        ).fetchall()
        if len(rows) != len(ids):
            raise TaskDeletionError("task not found")
        locked_threads = {row["thread_id"] for row in threads}
        tasks = {}
        for row in rows:
            if row["thread_id"] not in locked_threads:
                raise TaskDeletionError("task changed while it was being locked; retry")
            if row["object_deleting"] or row["project_deleting"]:
                raise TaskDeletionError("task project is unavailable or being deleted")
            tasks[row["id"]] = row
        return tasks

    def _locked_task(self, connection, task_id, *, for_update=False):
        return self._lock_tasks(connection, (task_id,), exclusive=for_update)[task_id]

    def _inventory(self, connection, task):
        task_id = task["id"]
        related_rows = connection.execute(
            """
            WITH RECURSIVE related(id) AS (
              SELECT %s::bigint
              UNION
              SELECT next.id
              FROM related parent
              CROSS JOIN LATERAL (
                SELECT child.id FROM jobs child
                WHERE child.metadata->>'parent_job_id' = parent.id::text
                UNION
                SELECT reminder.job_id FROM reminders reminder
                WHERE reminder.created_by_job_id = parent.id AND reminder.job_id IS NOT NULL
                UNION
                SELECT child.id
                FROM jobs root
                CROSS JOIN LATERAL jsonb_array_elements(
                  CASE WHEN jsonb_typeof(root.metadata->'retired_project_tasks') = 'array'
                       THEN root.metadata->'retired_project_tasks' ELSE '[]'::jsonb END
                ) retired
                JOIN jobs child ON child.id::text = retired->>'id'
                                  OR child.id::text = retired->>'job_id'
                WHERE root.id = parent.id
              ) next
            )
            SELECT id FROM related ORDER BY id
            """,
            (task_id,),
        )
        related_ids = [row["id"] for row in related_rows.fetchall()]
        blockers = []
        if task["status"] not in TERMINAL_STATUSES and task["deleted_at"] is None:
            blockers.append({"kind": "task_status", "status": task["status"]})
        if task["locked_at"] is not None or task["locked_by"] is not None:
            blockers.append({"kind": "task_lease"})
        related_jobs = connection.execute(
            "SELECT id, status FROM jobs WHERE id = ANY(%s::bigint[]) ORDER BY id",
            (related_ids,),
        ).fetchall()
        for row in related_jobs:
            if row["id"] != task_id and row["status"] in ACTIVE_STATUSES:
                blockers.append({"kind": "dependent_task", "task_id": row["id"], "status": row["status"]})
        for row in connection.execute(
            """SELECT DISTINCT j.id, j.status FROM jobs j
               JOIN workstream_attachments a ON a.workstream_id = j.workstream_id
               WHERE a.object_id = %s AND j.id <> %s AND j.status = ANY(%s)
               ORDER BY j.id""",
            (task["object_id"], task_id, list(ACTIVE_STATUSES)),
        ).fetchall():
            blockers.append({"kind": "shared_project_active_task", "task_id": row["id"], "status": row["status"]})
        for row in connection.execute(
            """SELECT id, status FROM jobs
               WHERE thread_id = %s AND id <> %s AND status = ANY(%s)
               ORDER BY id""",
            (task["thread_id"], task_id, list(ACTIVE_STATUSES)),
        ).fetchall():
            blockers.append({"kind": "active_conversation_task", "task_id": row["id"], "status": row["status"]})
        for row in connection.execute(
            """SELECT id, status FROM deep_research_runs
               WHERE (original_job_id = ANY(%s::bigint[]) OR execution_job_id = ANY(%s::bigint[]))
                 AND status IN ('queued', 'running', 'waiting_for_input')
               ORDER BY id""",
            (related_ids, related_ids),
        ).fetchall():
            blockers.append({"kind": "active_research", "research_id": row["id"], "status": row["status"]})
        for row in connection.execute(
            """SELECT id, status FROM reminders
               WHERE (created_by_job_id = ANY(%s::bigint[]) OR job_id = ANY(%s::bigint[]))
                 AND status IN ('scheduled', 'queued')
               ORDER BY id""",
            (related_ids, related_ids),
        ).fetchall():
            blockers.append({"kind": "pending_reminder", "reminder_id": row["id"], "status": row["status"]})
        for row in connection.execute(
            """SELECT id, status FROM channel_deliveries
               WHERE job_id = ANY(%s::bigint[]) AND status IN ('pending', 'sending', 'uncertain')
               ORDER BY id""",
            (related_ids,),
        ).fetchall():
            blockers.append({"kind": "pending_delivery", "delivery_id": row["id"], "status": row["status"]})
        for row in connection.execute(
            """SELECT id, status FROM outbound_email_logs
               WHERE job_id = ANY(%s::bigint[]) AND status = 'pending' ORDER BY id""",
            (related_ids,),
        ).fetchall():
            blockers.append({"kind": "pending_email", "email_log_id": row["id"], "status": row["status"]})
        for row in connection.execute(
            """SELECT id, status FROM model_service_calls
               WHERE job_id = ANY(%s::bigint[]) AND status IN ('pending', 'unknown') ORDER BY id""",
            (related_ids,),
        ).fetchall():
            blockers.append({"kind": "incomplete_model_call", "call_id": str(row["id"]), "status": row["status"]})
        for row in connection.execute(
            """SELECT id, status FROM sandbox_development_sessions
               WHERE job_id = ANY(%s::bigint[]) AND status IN ('pending', 'active', 'finalizing')
               ORDER BY id""",
            (related_ids,),
        ).fetchall():
            blockers.append({"kind": "active_development_session", "session_id": row["id"], "status": row["status"]})
        if connection.execute(
            """SELECT 1 FROM supervisor_instructions
               WHERE job_id = ANY(%s::bigint[]) AND consumed_at IS NULL LIMIT 1""",
            (related_ids,),
        ).fetchone():
            blockers.append({"kind": "pending_supervisor_instruction"})
        if connection.execute(
            """SELECT 1 FROM request_messages
               WHERE thread_id = %s
                 AND (status IN ('pending', 'processing')
                      OR (status = 'ready' AND metadata->>'queued' IS DISTINCT FROM 'true'))
               LIMIT 1""",
            (task["thread_id"],),
        ).fetchone():
            blockers.append({"kind": "pending_conversation_message"})
        snapshot = {
            "task": {
                "id": task_id,
                "thread_id": task["thread_id"],
                "workstream_id": task["workstream_id"],
                "status": task["status"],
                "updated_at": task["updated_at"],
                "deleted_at": task["deleted_at"],
            },
            "related_jobs": related_jobs,
            "blockers": blockers,
        }
        digest = hashlib.sha256(
            json.dumps(json_safe(snapshot), sort_keys=True, separators=(",", ":")).encode(),
        ).hexdigest()
        return related_ids, blockers, digest

    def preview(self, task_id):
        if type(task_id) is not int or task_id <= 0:
            raise TaskDeletionError("invalid task identifier")
        with self.db.connect() as connection:
            task = self._locked_task(connection, task_id)
            if task["deleted_at"] is not None or task["status"] == "deleted":
                return {
                    "task_id": task_id, "status": "deleted", "deleted": True,
                    "task_summary": DELETED_TASK_MARKER, "marker": DELETED_TASK_MARKER,
                    "deleted_at": task["deleted_at"],
                    "deletable": False, "blockers": [], "preview_digest": None,
                    "thread_id": task["thread_id"], "workstream_id": task["workstream_id"],
                }
            related_ids, blockers, digest = self._inventory(connection, task)
            logs = connection.execute(
                "SELECT count(*) AS count FROM task_logs WHERE job_id = %s", (task_id,),
            ).fetchone()["count"]
            return json_safe({
                "task_id": task_id,
                "status": task["status"],
                "thread_id": task["thread_id"],
                "workstream_id": task["workstream_id"],
                "deletable": not blockers,
                "blockers": blockers,
                "related_task_count": max(0, len(related_ids) - 1),
                "task_log_count": logs,
                "conversation_preserved": True,
                "project_data_preserved": True,
                "preview_digest": digest,
            })

    def delete(self, task_id, preview_digest):
        if type(task_id) is not int or task_id <= 0:
            raise TaskDeletionError("invalid task identifier")
        if not isinstance(preview_digest, str) or len(preview_digest) != 64:
            raise TaskDeletionError("a current deletion preview is required")
        with self.db.connect() as connection:
            task = self._locked_task(connection, task_id, for_update=True)
            if task["deleted_at"] is not None or task["status"] == "deleted":
                return {
                    "task_id": task_id, "id": task_id, "status": "deleted", "deleted": True,
                    "task_summary": DELETED_TASK_MARKER, "marker": DELETED_TASK_MARKER,
                    "deleted_at": task["deleted_at"], "thread_id": task["thread_id"],
                    "workstream_id": task["workstream_id"],
                }
            _, blockers, current_digest = self._inventory(connection, task)
            if current_digest != preview_digest:
                raise TaskDeletionError("task or dependent work changed; preview again before confirming")
            if blockers:
                raise TaskDeletionError("deletion blocked by active or dependent work")
            connection.execute(
                "DELETE FROM task_logs WHERE job_id = %s", (task_id,),
            )
            connection.execute(
                "DELETE FROM agent_checkpoints WHERE job_id = %s", (task_id,),
            )
            connection.execute(
                "DELETE FROM supervisor_instructions WHERE job_id = %s", (task_id,),
            )
            connection.execute(
                "DELETE FROM model_service_calls WHERE job_id = %s", (task_id,),
            )
            connection.execute(
                """UPDATE workstream_object_events SET changes = '{}'::jsonb, reason = NULL
                   WHERE object_id = %s""",
                (task["object_id"],),
            )
            row = connection.execute(
                """UPDATE jobs SET status = 'deleted', deleted_at = now(),
                     task_summary = %s, metadata = %s, last_error = NULL,
                     has_new_context = false, locked_at = NULL, locked_by = NULL,
                     embedding = NULL, embedding_model = NULL, embedding_dimensions = NULL,
                     embedding_updated_at = NULL, completed_at = COALESCE(completed_at, now()),
                     updated_at = now()
                   WHERE id = %s AND deleted_at IS NULL
                   RETURNING id, status, deleted_at""",
                (DELETED_TASK_MARKER, Jsonb({"deleted_task": True}), task_id),
            ).fetchone()
            if row is None:
                raise TaskDeletionError("task changed while deletion was in progress")
            return json_safe({
                "task_id": row["id"], "id": row["id"], "status": row["status"], "deleted": True,
                "task_summary": DELETED_TASK_MARKER, "marker": DELETED_TASK_MARKER,
                "deleted_at": row["deleted_at"], "thread_id": task["thread_id"],
                "workstream_id": task["workstream_id"],
                "conversation_preserved": True, "project_data_preserved": True,
            })
