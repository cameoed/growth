"""Bounded local CPU evaluation. Workers cannot publish or contact providers."""

import atexit
import multiprocessing
import os
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from contextlib import nullcontext
from functools import cache
from threading import Event

from ..scan_resources import LIMITS, resource
from .spec import VERSION

INVENTORY_PIN_SQL = (
    "SELECT candidate_id,input_version FROM engine_mission_inventory WHERE company_key=?"
)


def _mission_is_current(store, mission):
    from .store import get_mission

    try:
        current = get_mission(store.connection, mission["id"], archived=True)
    except LookupError:
        return False
    return (
        not current["archived"]
        and current["revision"] == mission["revision"]
        and current["spec"] == mission["spec"]
    )


def _inventory_pin(store, row):
    inventory = store.connection.execute(
        INVENTORY_PIN_SQL,
        (row["company_key"],),
    ).fetchone()
    return tuple(inventory) if inventory is not None else None


def evaluate_read_only(path, identity, mission, row, now, policy=VERSION, *, reader=None):
    from ..store import LeadStore
    from .scoring import _evaluate

    with (
        nullcontext(reader)
        if reader is not None
        else LeadStore(path, initialize=False, read_only=True) as store
    ):
        if store._database_identity() != identity or policy != VERSION:
            return {"stale": True}
        # First read pins the WAL snapshot; all evidence and review reads share it.
        store.connection.execute("BEGIN")
        if (
            _inventory_pin(store, row) != (row["candidate_id"], row["input_version"])
            or not _mission_is_current(store, mission)
        ):
            return {"stale": True}
        return _evaluate(store, row, mission, now)


_worker_reader = None
_worker_key = None


def _close_worker_reader():
    global _worker_reader, _worker_key
    if _worker_reader is not None:
        _worker_reader.close()
    _worker_reader = _worker_key = None


atexit.register(_close_worker_reader)


def _reader_for(path, identity):
    from ..store import LeadStore

    global _worker_reader, _worker_key
    key = (os.getpid(), str(path), identity)
    if key != _worker_key:
        _close_worker_reader()
        _worker_reader = LeadStore(path, initialize=False, read_only=True)
        _worker_key = key
    return _worker_reader


def evaluate_worker(path, identity, mission, row, now, policy=VERSION, cpu_lane="cpu"):
    """Reuse one bounded read-only connection; each company gets a fresh WAL snapshot."""
    reader = _reader_for(path, identity)
    try:
        with resource(cpu_lane, database=path):
            return evaluate_read_only(
                path, identity, mission, row, now, policy, reader=reader,
            )
    finally:
        # Never pin a reader snapshot between jobs or block checkpointing while idle.
        reader.connection.rollback()


def evaluate_company_worker(path, identity, jobs, now, policy=VERSION, cpu_lane="cpu"):
    from .scoring import _evaluate, load_evidence

    reader = _reader_for(path, identity)
    company_row = jobs[0][1]

    @cache
    def evidence():
        return load_evidence(reader, company_row["company_key"], now)

    try:
        with resource(cpu_lane, database=path):
            if reader._database_identity() != identity or policy != VERSION:
                return [{"stale": True} for _ in jobs]
            reader.connection.execute("BEGIN")
            pin = _inventory_pin(reader, company_row)
            return [
                _evaluate(reader, row, mission, now, evidence=evidence)
                if pin == (row["candidate_id"], row["input_version"])
                and _mission_is_current(reader, mission)
                else {"stale": True}
                for mission, row in jobs
            ]
    finally:
        reader.connection.rollback()


class MissionComputePool:
    """Persistent processes; at most one complete company packet per worker in flight."""

    def __init__(self, workers=None, *, min_parallel_seconds=0.004,
                 reserve_cpu=True, should_defer=None, local_background=False):
        available = max(1, (os.cpu_count() or 2) - int(reserve_cpu))
        self._cpu_lane = "local_ranking_cpu" if local_background else "cpu"
        limit = LIMITS[self._cpu_lane][1]
        self.workers = max(1, min(workers if workers is not None else available, available, limit))
        self._executor = None
        self.min_parallel_seconds = min_parallel_seconds
        self._should_defer = should_defer
        self._stopping = Event()

    @property
    def stopping(self):
        return self._stopping.is_set()

    @property
    def accepting(self):
        return not self.stopping and (self._should_defer is None or not self._should_defer())

    def request_stop(self):
        self._stopping.set()

    def should_parallelize(self, durations):
        # These are completed, fully scored companies, not sampled evidence.
        # Keep cheap work inline; reevaluate the decision as each company completes.
        if self.min_parallel_seconds == 0:
            return True
        threshold = self.min_parallel_seconds / (4 if self._executor is not None else 1)
        return len(durations) >= 4 and sum(durations[-4:]) / 4 >= threshold

    def evaluate(self, path, identity, mission, rows, now):
        return self._evaluate_packets(
            evaluate_worker, rows,
            lambda row: (path, identity, mission, row, now, VERSION, self._cpu_lane),
        )

    def evaluate_companies(self, path, identity, companies, now):
        return self._evaluate_packets(
            evaluate_company_worker, companies,
            lambda jobs: (path, identity, jobs, now, VERSION, self._cpu_lane),
        )

    def _evaluate_packets(self, worker, packets, arguments):
        if not self.accepting:
            return
        if self._executor is None:
            self._executor = ProcessPoolExecutor(
                max_workers=self.workers, mp_context=multiprocessing.get_context("spawn")
            )
        iterator = iter(packets)
        pending = {}

        def submit():
            if not self.accepting:
                return
            packet = next(iterator, None)
            if packet is not None:
                future = self._executor.submit(worker, *arguments(packet))
                pending[future] = packet

        try:
            for _ in range(self.workers):
                submit()
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                while done:
                    future = done.pop()
                    row = pending.pop(future)
                    yield row, future.result()
                    del future
                    # Publication has consumed the packet before admitting another.
                    submit()
        finally:
            for future in pending:
                future.cancel()

    def close(self):
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
