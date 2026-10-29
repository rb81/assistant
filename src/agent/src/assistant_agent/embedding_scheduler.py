import logging
import time
from contextlib import contextmanager
from typing import Iterator

from .config import AppConfig
from .database import Database
from .embedding_client import keyword_only
from .embedding_maintenance import EmbeddingMaintenance


LOGGER = logging.getLogger("assistant.embedding_scheduler")
ACTIVITY_LOCK = "assistant_embedding_activity"
STARTUP_LOCK = "assistant_embedding_startup"


class EmbeddingScheduler:
    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.maintenance = EmbeddingMaintenance(db, config)

    def startup_waiting(self) -> bool:
        with self.db.connect() as connection:
            connection.autocommit = True
            available = connection.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS locked", (STARTUP_LOCK,),
            ).fetchone()["locked"]
            if available:
                connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (STARTUP_LOCK,))
            return not available

    @contextmanager
    def idle(self) -> Iterator[bool]:
        with self.db.connect() as connection:
            connection.autocommit = True
            available = connection.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS locked", (STARTUP_LOCK,),
            ).fetchone()["locked"]
            if not available:
                yield False
                return
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS locked", (ACTIVITY_LOCK,),
            ).fetchone()["locked"]
            connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (STARTUP_LOCK,))
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (ACTIVITY_LOCK,))

    @contextmanager
    def execution(self, wait_seconds: float = 60) -> Iterator[bool]:
        deadline = time.monotonic() + wait_seconds
        exclusive = False
        shared = False
        fresh = not self.maintenance.client.enabled
        with self.db.connect() as connection:
            connection.autocommit = True
            connection.execute("SELECT pg_advisory_lock_shared(hashtext(%s))", (STARTUP_LOCK,))
            waiting = True
            try:
                while not fresh and time.monotonic() < deadline:
                    if not exclusive:
                        exclusive = connection.execute(
                            "SELECT pg_try_advisory_lock(hashtext(%s)) AS locked", (ACTIVITY_LOCK,),
                        ).fetchone()["locked"]
                        if not exclusive:
                            # Current vectors can be used by concurrent task workers.
                            shared = connection.execute(
                                "SELECT pg_try_advisory_lock_shared(hashtext(%s)) AS locked", (ACTIVITY_LOCK,),
                            ).fetchone()["locked"]
                            if shared:
                                fresh = self.maintenance.status()["complete"]
                                if fresh:
                                    break
                                connection.execute("SELECT pg_advisory_unlock_shared(hashtext(%s))", (ACTIVITY_LOCK,))
                                shared = False
                            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
                            continue
                    try:
                        fresh = self.maintenance.status()["complete"]
                        if not fresh:
                            result = self.maintenance.refresh_pending(deadline=deadline)
                            fresh = self.maintenance.status()["complete"]
                            if not fresh and not result["refreshed"]:
                                time.sleep(min(1, max(0, deadline - time.monotonic())))
                    except Exception:
                        LOGGER.exception("pre-task embedding refresh failed; retrying until startup deadline")
                        time.sleep(min(1, max(0, deadline - time.monotonic())))
                if not shared:
                    # Downgrade without a gap in which an indexer could start.
                    connection.execute("SELECT pg_advisory_lock_shared(hashtext(%s))", (ACTIVITY_LOCK,))
                    shared = True
                if exclusive:
                    connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (ACTIVITY_LOCK,))
                    exclusive = False
                connection.execute("SELECT pg_advisory_unlock_shared(hashtext(%s))", (STARTUP_LOCK,))
                waiting = False
                if not fresh:
                    LOGGER.warning("embedding startup wait expired; proceeding with keyword-only retrieval for this run")
                token = keyword_only.set(not fresh)
                try:
                    yield fresh
                finally:
                    keyword_only.reset(token)
            finally:
                if waiting:
                    connection.execute("SELECT pg_advisory_unlock_shared(hashtext(%s))", (STARTUP_LOCK,))
                if exclusive:
                    connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (ACTIVITY_LOCK,))
                if shared:
                    connection.execute("SELECT pg_advisory_unlock_shared(hashtext(%s))", (ACTIVITY_LOCK,))
                connection.execute("SELECT pg_notify('assistant_embeddings', '')")
