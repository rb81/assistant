import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg.types.json import Jsonb

from .artifact_processor import public_artifact_manifest, public_attachment_metadata
from .config import AppConfig, agent_email, message_id_domain, worker_id
from .database import Database, json_safe
from .polling import poll_interval_seconds, run_poll_loop


LOGGER = logging.getLogger("assistant.projects")
TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}


def truncate_text(value: Any, limit: int = 4000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return "%s..." % text[:limit]


def compact_json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=True)


class ProjectScheduler:
    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.locked_by = worker_id("project-scheduler")

    def run_forever(self, stop_requested) -> None:
        interval = poll_interval_seconds(self.config, "agent.projects.scheduler_poll_interval_seconds")
        run_poll_loop(
            stop_requested,
            self.run_once,
            interval,
            should_sleep=lambda count: not count,
            on_result=lambda count: LOGGER.info("project scheduler processed %s item(s)", count),
            logger=LOGGER,
            error_message="project scheduler loop failed",
        )

    def run_once(self) -> int:
        if not self.config.get_bool("agent.projects.enabled", True):
            return 0
        synced = self.sync_task_jobs()
        queued = self.queue_ready_tasks()
        return synced + queued

    def sync_task_jobs(self) -> int:
        processed = 0
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT pt.*,
                           p.original_job_id,
                           p.title AS project_title,
                           p.status AS project_status,
                           j.status AS job_status,
                           j.task_summary AS job_summary,
                           j.last_error AS job_last_error
                    FROM project_tasks pt
                    JOIN projects p ON p.id = pt.project_id
                    JOIN jobs j ON j.id = pt.job_id
                    WHERE pt.status IN ('queued', 'running')
                      AND p.status IN ('queued', 'running')
                      AND j.status IN ('running', 'completed', 'failed', 'cancelled')
                    ORDER BY pt.project_id ASC, pt.sequence ASC
                    FOR UPDATE OF pt, p SKIP LOCKED
                    LIMIT 100
                    """
                )
                rows = list(cur.fetchall())
                for row in rows:
                    if row["job_status"] == "running" and row["status"] != "running":
                        cur.execute(
                            "UPDATE project_tasks SET status = 'running', updated_at = now() WHERE id = %s",
                            (row["id"],),
                        )
                        processed += 1
                        continue
                    if row["job_status"] not in TERMINAL_JOB_STATUSES:
                        continue
                    self.finish_project_task(cur, row)
                    processed += 1
        return processed

    def finish_project_task(self, cur, task: dict[str, Any]) -> None:
        if task["job_status"] == "completed":
            cur.execute(
                """
                UPDATE project_tasks
                SET status = 'completed',
                    result_summary = %s,
                    completed_at = now(),
                    last_error = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (task.get("job_summary") or "Task completed.", task["id"]),
            )
            cur.execute(
                "INSERT INTO manual_events(event_type, payload) VALUES (%s, %s)",
                (
                    "project_task_completed",
                    Jsonb(json_safe({"project_id": task["project_id"], "project_task_id": task["id"], "job_id": task["job_id"]})),
                ),
            )
            return

        reason = task.get("job_last_error") or "Project task %s was %s." % (task["sequence"], task["job_status"])
        cur.execute(
            """
            UPDATE project_tasks
            SET status = %s,
                completed_at = now(),
                last_error = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (task["job_status"], reason, task["id"]),
        )
        self.fail_project(
            cur,
            task["project_id"],
            "Project task %s (%s) %s: %s" % (task["sequence"], task["title"], task["job_status"], reason),
        )

    def queue_ready_tasks(self) -> int:
        processed = 0
        limit = self.config.get_int("agent.projects.max_projects_per_tick", 25)
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM projects
                    WHERE status IN ('queued', 'running')
                      AND run_at <= now()
                    ORDER BY priority DESC, created_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                    """,
                    (limit,),
                )
                projects = list(cur.fetchall())
                for project in projects:
                    processed += self.process_project(cur, project)
        return processed

    def process_project(self, cur, project: dict[str, Any]) -> int:
        cur.execute(
            """
            SELECT *
            FROM project_tasks
            WHERE project_id = %s
            ORDER BY sequence ASC
            FOR UPDATE
            """,
            (project["id"],),
        )
        tasks = list(cur.fetchall())
        if not tasks:
            self.fail_project(cur, project["id"], "Project has no tasks.")
            return 1

        failed = [task for task in tasks if task["status"] in ("failed", "cancelled")]
        if failed:
            task = failed[0]
            self.fail_project(
                cur,
                project["id"],
                "Project task %s (%s) %s: %s"
                % (task["sequence"], task["title"], task["status"], task.get("last_error") or "no reason recorded"),
            )
            return 1

        if all(task["status"] == "completed" for task in tasks):
            self.complete_project(cur, project["id"])
            return 1

        if any(task["status"] in ("queued", "running") for task in tasks):
            return 0

        next_task = next((task for task in tasks if task["status"] == "pending"), None)
        if not next_task:
            return 0

        prior = [task for task in tasks if task["sequence"] < next_task["sequence"]]
        if any(task["status"] != "completed" for task in prior):
            return 0

        job = self.create_job_for_task(cur, project, next_task, tasks)
        cur.execute(
            """
            UPDATE project_tasks
            SET status = 'queued',
                job_id = %s,
                queued_at = now(),
                updated_at = now()
            WHERE id = %s
            """,
            (job["id"], next_task["id"]),
        )
        cur.execute(
            """
            UPDATE projects
            SET status = 'running',
                locked_at = NULL,
                locked_by = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (project["id"],),
        )
        cur.execute(
            "INSERT INTO manual_events(event_type, payload) VALUES (%s, %s)",
            (
                "project_task_queued",
                Jsonb(json_safe({"project_id": project["id"], "project_task_id": next_task["id"], "job_id": job["id"]})),
            ),
        )
        LOGGER.info("queued project %s task %s as job %s", project["id"], next_task["id"], job["id"])
        return 1

    def create_job_for_task(
        self,
        cur,
        project: dict[str, Any],
        task: dict[str, Any],
        tasks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        message_id = "<project-%s-task-%s-%s@%s>" % (
            project["id"],
            task["id"],
            int(now.timestamp() * 1000000),
            message_id_domain(self.config),
        )
        subject = "Project #%s task %s: %s" % (project["id"], task["sequence"], task["title"])
        completed = [item for item in tasks if item["status"] == "completed"]
        project_metadata = project.get("metadata") if isinstance(project.get("metadata"), dict) else {}
        workspace_path = str(project_metadata.get("workspace_path") or "").strip()
        cur.execute(
            """
            SELECT id, message_id, from_address, subject, body_text, body_html, attachments, received_at
            FROM emails
            WHERE thread_id = %s
            ORDER BY received_at DESC, id DESC
            LIMIT 5
            """,
            (project["original_thread_id"],),
        )
        original_emails = list(reversed(cur.fetchall()))
        cur.execute(
            """
            SELECT *
            FROM processed_artifacts
            WHERE thread_id = %s
            ORDER BY email_id ASC, id ASC
            LIMIT 100
            """,
            (project["original_thread_id"],),
        )
        artifacts_by_email: dict[int, list[dict[str, Any]]] = {}
        for artifact in cur.fetchall():
            artifacts_by_email.setdefault(int(artifact["email_id"]), []).append(public_artifact_manifest(artifact))
        body_lines = [
            "Project child task.",
            "",
            "Project ID: %s" % project["id"],
            "Project title: %s" % project["title"],
            "Original job ID: %s" % project["original_job_id"],
            "Task sequence: %s" % task["sequence"],
            "Task title: %s" % task["title"],
        ]
        if workspace_path:
            body_lines.extend(
                [
                    "Shared project workspace: %s" % workspace_path,
                    "Write durable task outputs under this workspace when files are useful for later project tasks.",
                ]
            )
        body_lines.extend(
            [
                "",
                "Complete this task only. Do not reply to project@local. Use task_complete with a response containing the task result when done, or task_failed if blocked.",
                "You may use normal tools and Deep Research Request if useful, but you must not create another project.",
                "",
                "Task instructions:",
                task["task"],
            ]
        )
        if completed:
            body_lines.extend(["", "Prior completed project task results:"])
            for item in completed:
                body_lines.extend(
                    [
                        "%s. %s" % (item["sequence"], item["title"]),
                        "Summary:",
                        item.get("result_summary") or "completed",
                        "",
                    ]
                )
        if original_emails:
            body_lines.extend(["", "Original user thread context:"])
            for email in original_emails:
                body_lines.append(
                    "\nEmail ID: %s\nMessage-ID: %s\nFrom: %s\nSubject: %s\nReceived: %s\nBody:\n%s"
                    % (
                        email["id"],
                        email["message_id"],
                        email["from_address"],
                        email.get("subject") or "",
                        email["received_at"],
                        truncate_text(email.get("body_text") or email.get("body_html") or "", 4000),
                    )
                )
                if email.get("attachments"):
                    body_lines.append("Attachments: %s" % compact_json([public_attachment_metadata(item) for item in email["attachments"]]))
                if artifacts_by_email.get(int(email["id"])):
                    body_lines.append("Processed artifacts: %s" % compact_json(artifacts_by_email[int(email["id"])]))
        body = "\n".join(body_lines)
        cur.execute(
            """
            INSERT INTO emails(
              message_id,
              thread_id,
              from_address,
              to_addresses,
              subject,
              body_text,
              received_at,
              is_actionable
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, true)
            RETURNING *
            """,
            (message_id, message_id, "project@local", [agent_email(self.config)], subject, body, now),
        )
        email_row = cur.fetchone()
        cur.execute(
            """
            INSERT INTO jobs(thread_id, task_summary, priority, run_at, metadata)
            VALUES (%s, %s, %s, now(), %s)
            RETURNING *
            """,
            (
                message_id,
                subject,
                int(task.get("priority") or project.get("priority") or 0),
                Jsonb(
                    json_safe(
                        {
                            "spawned_by": "project",
                            "project_id": project["id"],
                            "project_task_id": task["id"],
                            "parent_job_id": project["original_job_id"],
                            "project_workspace": workspace_path,
                        }
                    )
                ),
            ),
        )
        job_row = cur.fetchone()
        cur.execute(
            "INSERT INTO manual_events(event_type, payload) VALUES (%s, %s)",
            (
                "project_task_job_created",
                Jsonb(
                    json_safe(
                        {
                            "project_id": project["id"],
                            "project_task_id": task["id"],
                            "job_id": job_row["id"],
                            "email_id": email_row["id"],
                        }
                    )
                ),
            ),
        )
        return job_row

    def complete_project(self, cur, project_id: int) -> None:
        project = self.project_for_update(cur, project_id)
        if project is None or project["status"] not in ("queued", "running"):
            return
        tasks = self.project_tasks(cur, project_id)
        summary = self.project_summary(project, tasks, "completed")
        cur.execute(
            """
            UPDATE projects
            SET status = 'completed',
                result_summary = %s,
                completed_at = now(),
                last_error = NULL,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (summary, project_id),
        )
        self.notify_original_job(cur, project, "completed", summary)
        cur.execute(
            "INSERT INTO manual_events(event_type, payload) VALUES (%s, %s)",
            ("project_completed", Jsonb(json_safe({"project_id": project_id, "original_job_id": project["original_job_id"]}))),
        )

    def fail_project(self, cur, project_id: int, reason: str) -> None:
        project = self.project_for_update(cur, project_id)
        if project is None or project["status"] not in ("queued", "running"):
            return
        tasks = self.project_tasks(cur, project_id)
        summary = "%s\n\n%s" % (reason, self.project_summary(project, tasks, "failed"))
        cur.execute(
            """
            UPDATE projects
            SET status = 'failed',
                result_summary = %s,
                completed_at = now(),
                last_error = %s,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (summary, reason, project_id),
        )
        self.notify_original_job(cur, project, "failed", summary)
        cur.execute(
            "INSERT INTO manual_events(event_type, payload) VALUES (%s, %s)",
            (
                "project_failed",
                Jsonb(json_safe({"project_id": project_id, "original_job_id": project["original_job_id"], "reason": reason})),
            ),
        )

    def project_for_update(self, cur, project_id: int) -> Optional[dict[str, Any]]:
        cur.execute("SELECT * FROM projects WHERE id = %s FOR UPDATE", (project_id,))
        return cur.fetchone()

    def project_tasks(self, cur, project_id: int) -> list[dict[str, Any]]:
        cur.execute("SELECT * FROM project_tasks WHERE project_id = %s ORDER BY sequence ASC", (project_id,))
        return list(cur.fetchall())

    def project_summary(self, project: dict[str, Any], tasks: list[dict[str, Any]], status: str) -> str:
        lines = ["Project #%s %s: %s" % (project["id"], status, project["title"]), ""]
        project_metadata = project.get("metadata") if isinstance(project.get("metadata"), dict) else {}
        workspace_path = str(project_metadata.get("workspace_path") or "").strip()
        if workspace_path:
            lines.extend(["Shared workspace: %s" % workspace_path, ""])
        for task in tasks:
            detail = task.get("result_summary") or task.get("last_error") or ""
            lines.append("%s. [%s] %s" % (task["sequence"], task["status"], task["title"]))
            if detail:
                lines.append("   %s" % detail)
        return "\n".join(lines).strip()

    def notify_original_job(self, cur, project: dict[str, Any], status: str, summary: str) -> None:
        instruction = "\n".join(
            [
                "Project #%s has %s." % (project["id"], status),
                "",
                summary,
                "",
                "Continue the original user task using these project results. Reply to the user when the original task is complete.",
            ]
        )
        cur.execute(
            "INSERT INTO supervisor_instructions(job_id, instruction, created_by) VALUES (%s, %s, %s)",
            (project["original_job_id"], instruction, "project-scheduler"),
        )
        cur.execute(
            """
            UPDATE jobs
            SET status = CASE WHEN status IN ('needs_review', 'waiting') THEN 'queued' ELSE status END,
                run_at = CASE WHEN status IN ('needs_review', 'waiting') THEN now() ELSE run_at END,
                has_new_context = true,
                last_error = CASE WHEN status IN ('queued', 'needs_review', 'waiting') THEN NULL ELSE last_error END,
                locked_at = CASE WHEN status IN ('needs_review', 'waiting') THEN NULL ELSE locked_at END,
                locked_by = CASE WHEN status IN ('needs_review', 'waiting') THEN NULL ELSE locked_by END,
                updated_at = now()
            WHERE id = %s
              AND status NOT IN ('completed', 'failed', 'cancelled')
            RETURNING id
            """,
            (project["original_job_id"],),
        )
        updated = cur.fetchone()
        if updated:
            self.log_job_event(
                cur,
                project["original_job_id"],
                "status_change",
                output_data={"status": "queued", "reason": "project %s" % status, "project_id": project["id"]},
            )

    def log_job_event(
        self,
        cur,
        job_id: int,
        event_type: str,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
        tool_name: Optional[str] = None,
    ) -> None:
        cur.execute("SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM task_logs WHERE job_id = %s", (job_id,))
        sequence = cur.fetchone()["next_sequence"]
        cur.execute(
            """
            INSERT INTO task_logs(job_id, sequence, event_type, tool_name, input_data, output_data)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                job_id,
                sequence,
                event_type,
                tool_name,
                Jsonb(json_safe(input_data)) if input_data is not None else None,
                Jsonb(json_safe(output_data)) if output_data is not None else None,
            ),
        )
