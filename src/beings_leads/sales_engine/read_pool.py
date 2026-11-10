"""Bounded, exclusively leased read services; never shared concurrently or used for writes.

Four connections × 64 MiB maximum page-cache allowance. Request cancellation only interrupts
its leased reader. Source collection and the publication writer retain their existing owners.
"""

from contextlib import contextmanager, suppress
from pathlib import Path
from threading import Condition

from ..store import LeadStore
from .service import SalesEngineService

MAX_CONNECTIONS = 4
CACHE_KIB = 65536


class ReadPool:
    def __init__(self):
        self._condition = Condition()
        self._idle = []
        self._open = 0

    @contextmanager
    def lease(self, path, cancelled=None):
        import sqlite3

        identity = Path(path).resolve()
        stat = identity.stat() if identity.exists() else None
        key = (str(identity), stat.st_dev, stat.st_ino) if stat else (str(identity), None, None)
        engine = None
        reserved = False
        try:
            with self._condition:
                while engine is None and not reserved:
                    if cancelled is not None and cancelled.is_set():
                        raise sqlite3.OperationalError("interrupted")
                    if self._idle:
                        saved_key, saved_engine = self._idle.pop()
                        if key == saved_key:
                            engine = saved_engine
                        else:
                            saved_engine.store.close()
                            self._open -= 1
                    elif self._open < MAX_CONNECTIONS:
                        self._open += 1
                        reserved = True
                    else:
                        self._condition.wait(timeout=0.05)
            if reserved:
                store = LeadStore(identity, check_same_thread=False)
                stat = identity.stat()
                key = (str(identity), stat.st_dev, stat.st_ino)
                try:
                    store.connection.execute(f"PRAGMA cache_size=-{CACHE_KIB}")
                    engine = SalesEngineService(store)
                except BaseException:
                    store.close()
                    raise
            # Reconcile config changes and external migrations at every checkout.
            from ..domain_policy import reconcile_disallowed_domains

            reconcile_disallowed_domains(engine.connection)
            schema = engine.connection.execute("PRAGMA schema_version").fetchone()[0]
            if schema != getattr(engine, "_read_schema_version", None):
                engine = SalesEngineService(engine.store)
                engine._read_schema_version = engine.connection.execute(
                    "PRAGMA schema_version"
                ).fetchone()[0]
            engine._read_cancel = cancelled
            engine.connection.execute("PRAGMA query_only=ON")
            if cancelled is not None:
                engine.connection.set_progress_handler(lambda: int(cancelled.is_set()), 2000)
            yield engine
        finally:
            if engine is not None:
                engine.connection.set_progress_handler(None, 0)
                engine._read_cancel = None
                engine.connection.rollback()
                engine.connection.execute("PRAGMA query_only=OFF")
                with self._condition:
                    self._idle.append((key, engine))
                    self._condition.notify()
            elif reserved:
                with self._condition:
                    self._open -= 1
                    self._condition.notify()

    def close(self):
        with self._condition:
            for _, engine in self._idle:
                engine.store.close()
            self._open -= len(self._idle)
            self._idle.clear()


async def run_read(request, pool, path, operation):
    import asyncio
    import sqlite3
    from threading import Event

    from fastapi.responses import Response

    cancelled = Event()

    def compute():
        with pool.lease(path, cancelled) as engine:
            return operation(engine)

    task = asyncio.create_task(asyncio.to_thread(compute))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.05)
            if not task.done() and await request.is_disconnected():
                cancelled.set()
        try:
            return await task
        except sqlite3.OperationalError:
            if cancelled.is_set():
                return Response(status_code=499)
            raise
    finally:
        cancelled.set()
        # Thread cancellation is cooperative; wait for rollback and lease return.
        if not task.done():
            with suppress(sqlite3.OperationalError):
                await asyncio.shield(task)
