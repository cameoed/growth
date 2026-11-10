"""Bounded pure replay compute; workers never open SQLite, collect, or publish.

The owner prepares the unchanged scorer inputs from its existing transaction. Only
independent source-removal replays run elsewhere; the score and accounting writer stays single.
Small jobs retain the serial path because process dispatch costs more than their compute.
"""

from __future__ import annotations

import atexit
import os
import pickle
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from threading import Lock

_POOL = None
_POOL_LOCK = Lock()
_BATCH_LOCK = Lock()
MAX_WORKERS = 2
MAX_JOB_BYTES = 8 * 1024 * 1024


def enabled():
    try:
        count = int(os.environ.get("BEINGS_SYNTHESIS_WORKERS", str(MAX_WORKERS)))
    except ValueError:
        return False
    return count > 0 and (os.cpu_count() or 1) > 1


def _forbid_io(*args, **kwargs):
    raise RuntimeError("Pure synthesis workers cannot access databases or the network")


def _initialize_worker():
    import _sqlite3
    import socket
    import sqlite3

    _sqlite3.connect = sqlite3.connect = sqlite3.dbapi2.connect = _forbid_io
    socket.create_connection = socket.socket.connect = socket.socket.connect_ex = _forbid_io


def _dimensions(kwargs, database=None):
    from ..scan_resources import resource
    from ..scoring import score_candidate
    from .source_sensitivity import DIMENSIONS

    if isinstance(kwargs, bytes):
        kwargs = pickle.loads(kwargs)
    with resource("cpu", database=database):
        opportunity = score_candidate(**kwargs).creative_capacity.opportunity
    return {name: getattr(opportunity, name) for name in DIMENSIONS}


def _pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            requested = int(os.environ.get("BEINGS_SYNTHESIS_WORKERS", str(MAX_WORKERS)))
            _POOL = ProcessPoolExecutor(
                max_workers=max(1, min(requested, MAX_WORKERS)),
                mp_context=get_context("spawn"),
                initializer=_initialize_worker,
            )
        return _POOL


def measure(baseline, prepared_jobs, replay, *, baseline_inputs=None):
    from ..scan_resources import database_for_resources
    from .source_sensitivity import DIMENSIONS, SOURCES, identical_score_inputs

    removed = {}
    # Bound both process count and prepared/in-flight data across simultaneous scorer calls.
    with _BATCH_LOCK:
        pending = deque()
        jobs = iter(prepared_jobs)
        try:
            pool = None
            while True:
                while len(pending) < MAX_WORKERS:
                    item = next(jobs, None)
                    if item is None:
                        break
                    source, kwargs = item
                    if baseline_inputs is not None and identical_score_inputs(
                        kwargs, baseline_inputs
                    ):
                        removed[source] = {name: getattr(baseline, name) for name in DIMENSIONS}
                        continue
                    if pool is None:
                        pool = _pool()
                    encoded = pickle.dumps(kwargs, protocol=pickle.HIGHEST_PROTOCOL)
                    if len(encoded) > MAX_JOB_BYTES:
                        raise RuntimeError("Oversized replay retains the complete serial path")
                    pending.append((source, pool.submit(
                        _dimensions, encoded, database_for_resources()
                    )))
                if not pending:
                    break
                source, future = pending.popleft()
                removed[source] = future.result()
            if set(removed) != set(SOURCES):
                raise ValueError("Incomplete source replay batch requires the complete serial path")
        except Exception:
            # Dispatch/pickle/process failures must never weaken or omit a source replay.
            # The original serial scorer also propagates any actual scoring error here.
            for _, future in pending:
                future.cancel()
            return {
                source: {
                    field: getattr(baseline, field) - getattr(result, field) for field in DIMENSIONS
                }
                for source in SOURCES
                for result in [replay(source).creative_capacity.opportunity]
            }
    return {
        source: {field: getattr(baseline, field) - removed[source][field] for field in DIMENSIONS}
        for source in SOURCES
    }


def shutdown():
    global _POOL
    with _POOL_LOCK:
        pool, _POOL = _POOL, None
    if pool is not None:
        pool.shutdown(wait=True, cancel_futures=True)


atexit.register(shutdown)
