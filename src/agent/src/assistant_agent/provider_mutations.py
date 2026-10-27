import hashlib
import json
from contextlib import contextmanager, nullcontext
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .database import Database, json_safe


def mutation_key(
    provider: str, resource_type: str, resource_key: str, operation: str,
    object_version: int, desired_state: dict[str, Any],
) -> str:
    payload = json_safe({
        "provider": provider,
        "resource_type": resource_type,
        "resource_key": resource_key,
        "operation": operation,
        "object_version": object_version,
        "desired_state": desired_state,
    })
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def prepare(
    db: Database,
    provider: str,
    resource_type: str,
    resource_key: str,
    operation: str,
    key: str,
    before_state: dict[str, Any],
    desired_state: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(db, Database) or not getattr(db, "database_url", None):
        return {"id": None, "status": "pending", "before_state": before_state, "is_new": True}
    with psycopg.connect(db.database_url, row_factory=dict_row) as connection:
        inserted = connection.execute(
            """INSERT INTO provider_mutations(
                 provider_name, resource_type, resource_key, operation, idempotency_key,
                 before_state, desired_state
               ) VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (idempotency_key) DO NOTHING RETURNING id""",
            (provider, resource_type, resource_key, operation, key,
             Jsonb(json_safe(before_state)), Jsonb(json_safe(desired_state))),
        ).fetchone() is not None
        row = connection.execute(
            "SELECT * FROM provider_mutations WHERE idempotency_key = %s FOR UPDATE",
            (key,),
        ).fetchone()
        if (row["provider_name"] != provider or row["resource_type"] != resource_type
                or row["resource_key"] != resource_key or row["operation"] != operation
                or row["desired_state"] != json_safe(desired_state)):
            raise ValueError("provider mutation key is already associated with a different operation")
        if row["status"] == "failed":
            row = connection.execute(
                """UPDATE provider_mutations SET status = 'pending', attempts = attempts + 1,
                     last_error = NULL, updated_at = now()
                   WHERE id = %s RETURNING *""",
                (row["id"],),
            ).fetchone()
        elif row["status"] != "completed":
            row = connection.execute(
                """UPDATE provider_mutations SET attempts = attempts + 1, updated_at = now()
                   WHERE id = %s RETURNING *""",
                (row["id"],),
            ).fetchone()
        row["is_new"] = inserted
        return row


@contextmanager
def operation_lock(db: Database, key: str):
    if not isinstance(db, Database) or not getattr(db, "database_url", None):
        with nullcontext():
            yield
        return
    with psycopg.connect(db.database_url, autocommit=True) as connection:
        lock_key = "provider-mutation:" + key
        connection.execute("SELECT pg_advisory_lock(hashtext(%s))", (lock_key,))
        try:
            yield
        finally:
            connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_key,))


def complete(db: Database, mutation_id: int | None, result_state: dict[str, Any]) -> None:
    if mutation_id is None:
        return
    db.execute(
        """UPDATE provider_mutations SET status = 'completed', result_state = %s,
             last_error = NULL, completed_at = now(), updated_at = now()
           WHERE id = %s""",
        (Jsonb(json_safe(result_state)), mutation_id),
    )


def fail(db: Database, mutation_id: int | None, error: str, *, uncertain: bool) -> None:
    if mutation_id is None:
        return
    if not isinstance(db, Database) or not getattr(db, "database_url", None):
        return
    with psycopg.connect(db.database_url) as connection:
        connection.execute(
            """UPDATE provider_mutations SET status = %s, last_error = %s, updated_at = now()
               WHERE id = %s AND status <> 'completed'""",
            ("uncertain" if uncertain else "failed", str(error)[:2000], mutation_id),
        )
