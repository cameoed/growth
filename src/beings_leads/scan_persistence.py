"""One ordered, bounded owner for progress persistence; no collection/provider dispatch."""

import logging
import pickle
import sqlite3
from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Condition
from time import monotonic, sleep

from .scan_diagnostics import emit, error_fields

logger = logging.getLogger(__name__)
MAX_PENDING = 128
MAX_BYTES = 16 * 1024 * 1024
SLOW_WRITE_SECONDS = 1


class OrderedWriter:
    def __init__(self):
        self._condition = Condition()
        self._pending = deque()
        self._owners = Counter()
        self._bytes = 0
        self._inflight = False
        self._executor = None
        self._running = False
        self._failures = {}

    def submit(self, owner, key, operation, payload):
        # Serialization makes queued state immutable and gives an exact byte budget.
        diagnostic = None
        if operation is write_packet:
            diagnostic = (payload[3].get("id"), payload[2], monotonic())
        encoded = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
        future = Future()
        with self._condition:
            if key is not None and self._pending and self._pending[-1][1] == (owner, key):
                previous = self._pending.pop()
                self._bytes -= len(previous[3])
                self._owners[owner] -= 1
                previous[4].set_result(None)
            while len(self._pending) >= MAX_PENDING or (
                self._bytes + len(encoded) > MAX_BYTES and (self._pending or self._inflight)
            ):
                self._condition.wait()
            # A single oversized complete snapshot uses backpressure rather than
            # truncation; it cannot coexist with another queued/in-flight packet.
            self._pending.append((owner, (owner, key), operation, encoded, future, diagnostic))
            self._owners[owner] += 1
            self._bytes += len(encoded)
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scan-writer")
            if not self._running:
                self._running = True
                self._executor.submit(self._run)
        return future

    def _run(self):
        while True:
            with self._condition:
                if not self._pending:
                    self._running = False
                    self._condition.notify_all()
                    return
                owner, _, operation, encoded, future, diagnostic = self._pending.popleft()
                self._inflight = True
                pending = len(self._pending)
            started = monotonic()
            error = {}
            try:
                result = operation(pickle.loads(encoded))
            except BaseException as exc:
                error = error_fields(exc)
                logger.exception("ordered scan persistence failed")
                if not getattr(operation, "_barrier", False):
                    with self._condition:
                        self._failures[owner] = exc
                future.set_exception(exc)
            else:
                future.set_result(result)
            finally:
                elapsed = monotonic() - started
                with self._condition:
                    self._bytes -= len(encoded)
                    self._owners[owner] -= 1
                    if not self._owners[owner]:
                        del self._owners[owner]
                    self._inflight = False
                    self._condition.notify_all()
                if diagnostic is not None:
                    scan_id, kind, queued_at = diagnostic
                    queued = started - queued_at
                    if error or max(queued, elapsed) >= SLOW_WRITE_SECONDS:
                        emit(scan_id, "ordered_write", kind=kind, pending_packets=pending,
                             queue_seconds=round(queued, 6), operation_seconds=round(elapsed, 6),
                             **error)

    def barrier(self, owner):
        with self._condition:
            if not self._owners.get(owner):
                future = Future()
                error = self._failures.pop(owner, None)
                if error:
                    future.set_exception(error)
                else:
                    future.set_result(None)
                return future

        def finish(payload):
            with self._condition:
                error = self._failures.pop(owner, None)
            if error:
                raise error

        finish._barrier = True
        return self.submit(owner, None, finish, None)

    def close(self):
        with self._condition:
            executor = self._executor
        if executor:
            executor.shutdown(wait=True)
        with self._condition:
            self._executor = None
            self._failures.clear()


WRITER = OrderedWriter()


def write_packet(packet):
    deadline = monotonic() + 60
    for attempt in range(6):
        try:
            return _write_packet(packet)
        except sqlite3.OperationalError as exc:
            if ((getattr(exc, "sqlite_errorcode", 0) & 0xff) != sqlite3.SQLITE_BUSY
                or attempt == 5 or monotonic() >= deadline):
                raise
            sleep(0.05 * (attempt + 1))


def _write_packet(packet):
    from .domain_policy import policy_scope
    from .store import LeadStore

    path, policy, kind, state, value = packet
    with policy_scope(policy), LeadStore(path) as store, store.connection:
        store.connection.execute("PRAGMA busy_timeout=10000")
        store.connection.execute("BEGIN IMMEDIATE")
        if kind in {"state", "error_event"}:
            state = dict(state)
            if state.get("started_at"):
                boundary, added = store.smart_scan_new_companies(
                    started_at=str(state["started_at"]),
                    start_candidate_id=state.get("new_company_start_id"),
                    finished_at=state.get("finished_at"),
                    excluded_candidate_ids=state.get("new_company_excluded_ids"),
                )
                state["new_company_start_id"] = boundary
                state["new_companies_added"] = added
            store.save_smart_scan_run(state, commit=False)
            if kind == "error_event":
                error, event, retain = value
                store.record_smart_scan_error(state["id"], **error, commit=False)
                store.append_smart_scan_event(state["id"], event, retain=retain, commit=False)
            return {
                key: state[key]
                for key in ("id", "new_company_start_id", "new_companies_added")
                if key in state
            }
        if not store.connection.execute(
            "SELECT 1 FROM smart_scan_runs WHERE id=?", (state["id"],)
        ).fetchone():
            store.save_smart_scan_run(state)
        if kind == "event":
            event, retain = value
            store.append_smart_scan_event(state["id"], event, retain=retain)
