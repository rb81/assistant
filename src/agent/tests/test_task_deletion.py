import threading
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from assistant_agent.requests import IncomingRequest, RequestService, TransactionDatabase
from assistant_agent.task_deletion import (
    DELETED_TASK_MARKER,
    DeletedTaskError,
    TaskDeletionError,
    TaskDeletionService,
)
from test_workstreams import job, remove, store


def completed_job(store):
    task = job(store, "Delete me")
    store.db.update_job_status(task["id"], "completed")
    store.db.execute(
        "UPDATE request_messages SET metadata = metadata || '{\"queued\":true}'::jsonb WHERE thread_id = %s",
        (task["thread_id"],),
    )
    return store.db.fetch_one("SELECT * FROM jobs WHERE id = %s", (task["id"],))


def test_delete_preserves_conversation_and_shared_project_data(store):
    task = completed_job(store)
    other = job(store, "Other project")
    store.db.update_job_status(other["id"], "completed")
    store.db.execute(
        "INSERT INTO workstream_attachments(workstream_id, object_id) VALUES (%s, %s)",
        (other["workstream_id"], task["object_id"]),
    )
    store.db.log_event(task["id"], "tool_result", output_data={"private": "execution detail"})
    store.db.execute(
        "INSERT INTO agent_checkpoints(job_id, message_history, reason) VALUES (%s, %s, 'test')",
        (task["id"], Jsonb([{"role": "assistant", "content": "checkpoint"}])),
    )
    store.db.execute(
        "INSERT INTO supervisor_instructions(job_id, instruction, consumed_at) VALUES (%s, 'old instruction', now())",
        (task["id"],),
    )
    store.db.execute(
        """INSERT INTO model_service_calls(id, job_id, preset, status)
           VALUES (%s, %s, '{}'::jsonb, 'completed')""",
        (str(uuid.uuid4()), task["id"]),
    )
    store.db.execute(
        """INSERT INTO channel_deliveries(job_id, channel, destination, request_key, body_text,
                   status, workstream_id)
           VALUES (%s, 'management', 'management', %s, 'conversation reply', 'sent', %s)""",
        (task["id"], "delivered-%s" % task["id"], task["workstream_id"]),
    )
    before_messages = store.db.fetch_all(
        "SELECT id, body_text FROM request_messages WHERE thread_id = %s ORDER BY id", (task["thread_id"],),
    )
    assert before_messages
    before_objects = store.db.fetch_all(
        "SELECT workstream_id FROM workstream_attachments WHERE object_id = %s ORDER BY workstream_id",
        (task["object_id"],),
    )

    service = TaskDeletionService(store.db)
    preview = service.preview(task["id"])
    assert preview["deletable"]
    result = service.delete(task["id"], preview["preview_digest"])

    assert result["status"] == "deleted"
    assert result["marker"] == DELETED_TASK_MARKER
    tombstone = store.db.fetch_one("SELECT * FROM jobs WHERE id = %s", (task["id"],))
    assert tombstone["status"] == "deleted"
    assert tombstone["task_summary"] == DELETED_TASK_MARKER
    assert tombstone["deleted_at"] is not None
    assert tombstone["metadata"] == {"deleted_task": True}
    marker = service.get_tombstone(task["id"])
    assert marker["id"] == task["id"]
    assert marker["thread_id"] == task["thread_id"]
    assert marker["workstream_id"] == task["workstream_id"]
    assert marker["deleted_at"] is not None
    with pytest.raises(DeletedTaskError) as error:
        service.assert_operable(task["id"])
    assert error.value.marker == marker
    assert store.db.fetch_all("SELECT * FROM task_logs WHERE job_id = %s", (task["id"],)) == []
    assert store.db.fetch_all("SELECT * FROM agent_checkpoints WHERE job_id = %s", (task["id"],)) == []
    assert store.db.fetch_all("SELECT * FROM supervisor_instructions WHERE job_id = %s", (task["id"],)) == []
    assert store.db.fetch_all("SELECT * FROM model_service_calls WHERE job_id = %s", (task["id"],)) == []
    assert store.db.fetch_all(
        "SELECT id, body_text FROM request_messages WHERE thread_id = %s ORDER BY id", (task["thread_id"],),
    ) == before_messages
    assert store.db.fetch_one(
        "SELECT body_text FROM channel_deliveries WHERE job_id = %s AND status = 'sent'", (task["id"],),
    )["body_text"] == "conversation reply"
    assert store.db.fetch_all(
        "SELECT workstream_id FROM workstream_attachments WHERE object_id = %s ORDER BY workstream_id",
        (task["object_id"],),
    ) == before_objects
    task_object_events = store.db.fetch_all(
        "SELECT changes, reason FROM workstream_object_events WHERE object_id = %s", (task["object_id"],),
    )
    assert task_object_events
    assert all(event == {"changes": {}, "reason": None} for event in task_object_events)
    assert store.db.fetch_one("SELECT id FROM workstreams WHERE id = %s", (task["workstream_id"],))
    assert service.preview(task["id"])["marker"] == DELETED_TASK_MARKER
    assert service.delete(task["id"], "0" * 64)["status"] == "deleted"


@pytest.mark.parametrize("status", ["queued", "running", "waiting", "needs_review"])
def test_active_task_status_blocks_deletion(store, status):
    task = job(store, "Active task")
    if status != "queued":
        store.db.update_job_status(task["id"], status)
    preview = TaskDeletionService(store.db).preview(task["id"])
    assert not preview["deletable"]
    assert any(blocker["kind"] == "task_status" for blocker in preview["blockers"])
    with pytest.raises(TaskDeletionError, match="blocked"):
        TaskDeletionService(store.db).delete(task["id"], preview["preview_digest"])


def test_active_child_task_blocks_deletion_and_stales_prior_confirmation(store):
    task = completed_job(store)
    service = TaskDeletionService(store.db)
    preview = service.preview(task["id"])
    child = store.db.fetch_one(
        """INSERT INTO jobs(thread_id, task_summary, metadata)
           VALUES (%s, 'Dependent task', %s) RETURNING id""",
        (task["thread_id"], Jsonb({"parent_job_id": str(task["id"])})),
    )
    with pytest.raises(TaskDeletionError, match="changed"):
        service.delete(task["id"], preview["preview_digest"])
    refreshed = service.preview(task["id"])
    assert not refreshed["deletable"]
    assert any(blocker["kind"] == "dependent_task" and blocker["task_id"] == child["id"]
               for blocker in refreshed["blockers"])


def test_active_work_in_a_shared_project_blocks_task_deletion(store):
    task = completed_job(store)
    other = job(store, "Shared project work")
    store.db.update_job_status(other["id"], "running")
    store.db.execute(
        "INSERT INTO workstream_attachments(workstream_id, object_id) VALUES (%s, %s)",
        (other["workstream_id"], task["object_id"]),
    )
    preview = TaskDeletionService(store.db).preview(task["id"])
    assert any(blocker["kind"] == "shared_project_active_task" and blocker["task_id"] == other["id"]
               for blocker in preview["blockers"])


def test_research_is_retained_and_active_runs_block_task_deletion(store):
    task = completed_job(store)
    run = store.db.fetch_one(
        """INSERT INTO deep_research_runs(original_job_id, original_thread_id, title, research_question)
           VALUES (%s, %s, 'Research report', 'A research question') RETURNING *""",
        (task["id"], task["thread_id"]),
    )
    store.db.execute(
        """INSERT INTO deep_research_events(run_id, sequence, event_type, output_data)
           VALUES (%s, 1, 'search_result', %s)""",
        (run["id"], Jsonb({"result": "project research"})),
    )
    service = TaskDeletionService(store.db)
    preview = service.preview(task["id"])
    assert not preview["deletable"]
    assert any(blocker["kind"] == "active_research" for blocker in preview["blockers"])

    store.db.execute("UPDATE deep_research_runs SET status = 'completed' WHERE id = %s", (run["id"],))
    preview = service.preview(task["id"])
    assert preview["deletable"]
    result = service.delete(task["id"], preview["preview_digest"])
    assert result["status"] == "deleted"
    assert store.db.fetch_one("SELECT result_data FROM deep_research_runs WHERE id = %s", (run["id"],))
    assert store.db.fetch_one("SELECT output_data FROM deep_research_events WHERE run_id = %s", (run["id"],))


def test_pending_reminder_blocks_deletion_until_cancelled(store):
    task = completed_job(store)
    reminder = store.db.fetch_one(
        """INSERT INTO reminders(title, task, run_at, created_by_job_id)
           VALUES ('Follow up', 'Follow up task', now() + interval '1 day', %s) RETURNING id""",
        (task["id"],),
    )
    service = TaskDeletionService(store.db)
    preview = service.preview(task["id"])
    assert any(blocker["kind"] == "pending_reminder" for blocker in preview["blockers"])
    store.db.execute("UPDATE reminders SET status = 'cancelled' WHERE id = %s", (reminder["id"],))
    preview = service.preview(task["id"])
    assert preview["deletable"]
    service.delete(task["id"], preview["preview_digest"])
    with pytest.raises(psycopg.Error, match="deleted task"):
        store.db.execute(
            """INSERT INTO reminders(title, task, run_at, created_by_job_id)
               VALUES ('Too late', 'Do not schedule', now() + interval '1 day', %s)""",
            (task["id"],),
        )


def test_deleted_task_cannot_gain_a_new_child_task(store):
    task = completed_job(store)
    service = TaskDeletionService(store.db)
    service.delete(task["id"], service.preview(task["id"])["preview_digest"])
    with pytest.raises(psycopg.Error, match="deleted task"):
        store.db.fetch_one(
            """INSERT INTO jobs(thread_id, task_summary, metadata)
               VALUES (%s, 'Late child', %s) RETURNING id""",
            (task["thread_id"], Jsonb({"parent_job_id": str(task["id"])})),
        )


def test_deleted_task_cannot_gain_new_execution_logs(store):
    task = completed_job(store)
    service = TaskDeletionService(store.db)
    service.delete(task["id"], service.preview(task["id"])["preview_digest"])
    with pytest.raises(psycopg.Error, match="deleted task"):
        store.db.log_event(task["id"], "status_change", output_data={"private": "late event"})
    assert store.db.fetch_one("SELECT count(*) AS total FROM task_logs WHERE job_id = %s",
                              (task["id"],))["total"] == 0


def test_concurrent_child_creation_waits_for_delete_then_is_rejected(store):
    task = completed_job(store)
    service = TaskDeletionService(store.db)
    digest = service.preview(task["id"])["preview_digest"]
    started = threading.Event()
    finished = threading.Event()
    errors = []

    def create_child():
        started.set()
        try:
            store.db.fetch_one(
                """INSERT INTO jobs(thread_id, task_summary, metadata)
                   VALUES (%s, 'Racing child', %s) RETURNING id""",
                (task["thread_id"], Jsonb({"parent_job_id": str(task["id"])})),
            )
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    with store.db.connect() as connection:
        result = TaskDeletionService(TransactionDatabase(connection)).delete(task["id"], digest)
        thread = threading.Thread(target=create_child)
        thread.start()
        assert started.wait(5)
        assert not finished.wait(0.1)
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result["status"] == "deleted"
    assert finished.is_set()
    assert len(errors) == 1
    assert isinstance(errors[0], psycopg.Error)
    assert "deleted task" in str(errors[0])


def wait_for_advisory_wait(store, pid):
    for _ in range(100):
        if store.db.fetch_one(
            "SELECT 1 FROM pg_locks WHERE pid = %s AND locktype = 'advisory' AND NOT granted", (pid,),
        ):
            return
        threading.Event().wait(0.05)
    raise AssertionError("deletion did not wait for the request lock")


def test_concurrent_request_and_delete_use_one_lock_order(store):
    task = completed_job(store)
    service = TaskDeletionService(store.db)
    digest = service.preview(task["id"])["preview_digest"]
    pid, errors = [], []

    def delete():
        try:
            with store.db.connect() as connection:
                pid.append(connection.info.backend_pid)
                TaskDeletionService(TransactionDatabase(connection)).delete(task["id"], digest)
        except Exception as error:
            errors.append(error)

    with store.db.connect() as connection:
        connection.execute("SET LOCAL statement_timeout = '10s'")
        request = TransactionDatabase(connection)
        RequestService(request).receive_request(IncomingRequest(
            "management", "race-%s" % task["id"], task["thread_id"], "management", task["thread_id"],
            "Follow-up while deletion is confirmed",
        ))
        racer = threading.Thread(target=delete)
        racer.start()
        while not pid:
            threading.Event().wait(0.01)
        wait_for_advisory_wait(store, pid[0])
        # Management replies lock the task row after the request lock; deletion must not hold it yet.
        request.fetch_one("SELECT id FROM jobs WHERE id = %s FOR UPDATE", (task["id"],))
    racer.join(timeout=10)
    assert not racer.is_alive()
    assert len(errors) == 1
    assert type(errors[0]) is TaskDeletionError
    assert "preview again" in str(errors[0])
    assert store.db.fetch_one("SELECT status FROM jobs WHERE id = %s", (task["id"],))["status"] == "completed"
    assert store.db.fetch_one(
        "SELECT 1 FROM jobs WHERE thread_id = %s AND status = 'queued'", (task["thread_id"],),
    )


def test_operability_check_waits_for_delete_then_reports_tombstone(store):
    task = completed_job(store)
    service = TaskDeletionService(store.db)
    digest = service.preview(task["id"])["preview_digest"]
    errors = []

    def reply():
        try:
            with store.db.connect() as connection:
                TaskDeletionService(TransactionDatabase(connection)).assert_operable(task["id"])
        except Exception as error:
            errors.append(error)

    with store.db.connect() as connection:
        TaskDeletionService(TransactionDatabase(connection)).delete(task["id"], digest)
        racer = threading.Thread(target=reply)
        racer.start()
        racer.join(timeout=0.2)
        assert racer.is_alive()
    racer.join(timeout=10)
    assert len(errors) == 1 and isinstance(errors[0], DeletedTaskError)
    assert errors[0].marker["marker"] == DELETED_TASK_MARKER


def test_tombstone_is_immutable_but_survives_rehoming_by_workstream_deletion(store):
    task = completed_job(store)
    survivor = job(store, "Surviving project")
    store.db.update_job_status(survivor["id"], "completed")
    store.db.execute(
        "INSERT INTO workstream_attachments(workstream_id, object_id) VALUES (%s, %s)",
        (survivor["workstream_id"], task["object_id"]),
    )
    service = TaskDeletionService(store.db)
    service.delete(task["id"], service.preview(task["id"])["preview_digest"])
    for statement in (
        "UPDATE jobs SET task_summary = 'restored' WHERE id = %s",
        "UPDATE jobs SET status = 'queued', deleted_at = NULL WHERE id = %s",
        "UPDATE jobs SET workstream_id = workstream_id + 0 * id + 1 WHERE id = %s",
    ):
        with pytest.raises(psycopg.Error, match="deleted tasks are immutable"):
            store.db.execute(statement, (task["id"],))
    assert remove(store, task["workstream_id"])["status"] == "completed"
    tombstone = service.get_tombstone(task["id"])
    assert tombstone["workstream_id"] == survivor["workstream_id"]
    assert tombstone["marker"] == DELETED_TASK_MARKER


def test_pending_workstream_deletion_blocks_task_deletion_and_replies(store):
    task = completed_job(store)
    other = job(store, "Deleting project")
    store.db.execute(
        "INSERT INTO workstream_attachments(workstream_id, object_id) VALUES (%s, %s)",
        (other["workstream_id"], task["object_id"]),
    )
    store.db.execute("UPDATE workstreams SET status = 'deleting' WHERE id = %s", (other["workstream_id"],))
    service = TaskDeletionService(store.db)
    with pytest.raises(TaskDeletionError, match="being deleted"):
        service.preview(task["id"])
    with pytest.raises(TaskDeletionError, match="being deleted"):
        service.assert_operable(task["id"])


def test_guidance_cannot_be_added_to_a_deleted_task(store):
    task = completed_job(store)
    service = TaskDeletionService(store.db)
    service.delete(task["id"], service.preview(task["id"])["preview_digest"])
    with pytest.raises(psycopg.Error, match="deleted task"):
        store.db.add_job_instruction(task["id"], "late /admin instruct", "admin")
    assert store.db.fetch_all("SELECT id FROM supervisor_instructions WHERE job_id = %s", (task["id"],)) == []
    assert store.db.fetch_all("SELECT id FROM task_logs WHERE job_id = %s", (task["id"],)) == []
    assert store.db.fetch_one("SELECT status FROM jobs WHERE id = %s", (task["id"],))["status"] == "deleted"


def test_guidance_racing_an_uncommitted_delete_waits_then_is_rejected(store):
    task = completed_job(store)
    service = TaskDeletionService(store.db)
    digest = service.preview(task["id"])["preview_digest"]
    errors = []

    def instruct():
        try:
            store.db.execute(
                "INSERT INTO supervisor_instructions(job_id, instruction, created_by) VALUES (%s, 'race', 'admin')",
                (task["id"],),
            )
        except Exception as error:
            errors.append(error)

    with store.db.connect() as connection:
        TaskDeletionService(TransactionDatabase(connection)).delete(task["id"], digest)
        racer = threading.Thread(target=instruct)
        racer.start()
        racer.join(timeout=0.2)
        assert racer.is_alive()
    racer.join(timeout=10)
    assert len(errors) == 1 and "deleted task" in str(errors[0])
    assert store.db.fetch_all("SELECT id FROM supervisor_instructions WHERE job_id = %s", (task["id"],)) == []


def test_uncommitted_guidance_makes_racing_delete_wait_then_block(store):
    task = completed_job(store)
    service = TaskDeletionService(store.db)
    digest = service.preview(task["id"])["preview_digest"]
    errors = []

    def delete():
        try:
            service.delete(task["id"], digest)
        except Exception as error:
            errors.append(error)

    with store.db.connect() as connection:
        connection.execute(
            "INSERT INTO supervisor_instructions(job_id, instruction, created_by) VALUES (%s, 'first', 'admin')",
            (task["id"],),
        )
        racer = threading.Thread(target=delete)
        racer.start()
        racer.join(timeout=0.2)
        assert racer.is_alive()
    racer.join(timeout=10)
    assert len(errors) == 1 and type(errors[0]) is TaskDeletionError
    assert {"kind": "pending_supervisor_instruction"} in service.preview(task["id"])["blockers"]
    assert store.db.fetch_one("SELECT status FROM jobs WHERE id = %s", (task["id"],))["status"] == "completed"


def wait_until_blocked_or_sleeping(store, pid):
    for _ in range(200):
        if store.db.fetch_one(
            """SELECT 1 FROM pg_stat_activity WHERE pid = %s
               AND (wait_event_type = 'Lock' OR query LIKE '%%pg_sleep%%')""", (pid,),
        ):
            return
        threading.Event().wait(0.02)
    raise AssertionError("research worker did not reach its status update")


@pytest.mark.parametrize("reply_locks_task_row", [False, True])
def test_research_status_change_and_request_resume_do_not_deadlock(store, reply_locks_task_row):
    task = job(store, "Waiting for research")
    store.db.execute("UPDATE jobs SET status = 'waiting' WHERE id = %s", (task["id"],))
    run = store.db.fetch_one(
        """INSERT INTO deep_research_runs(original_job_id, original_thread_id, title, research_question, status)
           VALUES (%s, %s, 'Research', 'Question', 'waiting_for_input') RETURNING id""",
        (task["id"], task["thread_id"]),
    )
    pid, errors = [], []

    def research_worker():
        try:
            with store.db.connect() as connection:
                pid.append(connection.info.backend_pid)
                connection.execute("SET LOCAL statement_timeout = '10s'")
                connection.execute(
                    "UPDATE deep_research_runs SET status = 'failed' WHERE id = %s", (run["id"],),
                )
                connection.execute("SELECT pg_sleep(0.3)")
        except Exception as error:
            errors.append(error)

    with store.db.connect() as connection:
        connection.execute("SET LOCAL statement_timeout = '10s'")
        connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("request:" + task["thread_id"],))
        if reply_locks_task_row:
            # Management replies lock the task row before receiving the request.
            connection.execute("SELECT id FROM jobs WHERE id = %s FOR UPDATE", (task["id"],))
        worker = threading.Thread(target=research_worker)
        worker.start()
        while not pid:
            threading.Event().wait(0.01)
        wait_until_blocked_or_sleeping(store, pid[0])
        # Request handling resumes waiting research while holding the request lock.
        RequestService(TransactionDatabase(connection)).receive_request(IncomingRequest(
            "management", "resume-%s" % run["id"], task["thread_id"], "management", task["thread_id"],
            "Here is the answer",
        ))
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert errors == []
    assert store.db.fetch_one("SELECT status FROM deep_research_runs WHERE id = %s", (run["id"],))["status"] == "failed"


def legacy_child(store, parent, status="completed"):
    child = store.db.fetch_one(
        """INSERT INTO jobs(thread_id, task_summary, metadata) VALUES (%s, 'Legacy child', %s) RETURNING id""",
        ("legacy-child-%s" % uuid.uuid4().hex, Jsonb({"parent_job_id": str(parent["id"])})),
    )
    store.db.execute("UPDATE jobs SET status = %s WHERE id = %s", (status, child["id"]))
    return child


def insert_grandchild(connection, child):
    return connection.execute(
        """INSERT INTO jobs(thread_id, task_summary, metadata) VALUES (%s, 'Racing grandchild', %s) RETURNING id""",
        ("legacy-grandchild-%s" % uuid.uuid4().hex, Jsonb({"parent_job_id": str(child["id"])})),
    ).fetchone()


def test_grandchild_creation_waits_for_ancestor_delete_then_is_rejected(store):
    task = completed_job(store)
    child = legacy_child(store, task)
    service = TaskDeletionService(store.db)
    digest = service.preview(task["id"])["preview_digest"]
    errors = []

    def create():
        try:
            with store.db.connect() as connection:
                insert_grandchild(connection, child)
        except Exception as error:
            errors.append(error)

    with store.db.connect() as connection:
        TaskDeletionService(TransactionDatabase(connection)).delete(task["id"], digest)
        racer = threading.Thread(target=create)
        racer.start()
        racer.join(timeout=0.3)
        assert racer.is_alive()
    racer.join(timeout=10)
    assert len(errors) == 1 and "deleted task" in str(errors[0])
    assert store.db.fetch_all(
        "SELECT id FROM jobs WHERE metadata->>'parent_job_id' = %s", (str(child["id"]),),
    ) == []


def test_uncommitted_grandchild_makes_ancestor_delete_wait_then_block(store):
    task = completed_job(store)
    child = legacy_child(store, task)
    service = TaskDeletionService(store.db)
    digest = service.preview(task["id"])["preview_digest"]
    errors = []

    def delete():
        try:
            service.delete(task["id"], digest)
        except Exception as error:
            errors.append(error)

    with store.db.connect() as connection:
        grandchild = insert_grandchild(connection, child)
        racer = threading.Thread(target=delete)
        racer.start()
        racer.join(timeout=0.3)
        assert racer.is_alive()
    racer.join(timeout=10)
    assert len(errors) == 1 and type(errors[0]) is TaskDeletionError
    assert {"kind": "dependent_task", "task_id": grandchild["id"], "status": "queued"} in (
        service.preview(task["id"])["blockers"]
    )
    assert store.db.fetch_one("SELECT status FROM jobs WHERE id = %s", (task["id"],))["status"] == "completed"


def test_descendant_of_deleted_ancestor_cannot_be_reactivated(store):
    task = completed_job(store)
    child = legacy_child(store, task)
    grandchild = legacy_child(store, child)
    service = TaskDeletionService(store.db)
    service.delete(task["id"], service.preview(task["id"])["preview_digest"])
    with pytest.raises(psycopg.Error, match="deleted task"):
        store.db.execute("UPDATE jobs SET status = 'queued' WHERE id = %s", (grandchild["id"],))
    store.db.execute("UPDATE jobs SET status = 'failed' WHERE id = %s", (grandchild["id"],))
