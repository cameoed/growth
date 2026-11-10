"""Exact committed list caching with temporal invalidation and bounded single-flight reads."""

from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime
from threading import Lock, RLock

from . import read_indexes

MAX_ENTRIES = 64
MAX_BYTES = 16 * 1024 * 1024
_CACHE = OrderedDict()
_FLIGHTS = {}
_LOCK = RLock()
_BYTES = 0


@contextmanager
def flight(key, cancelled=None):
    import sqlite3

    with _LOCK:
        lock, users = _FLIGHTS.get(key, (Lock(), 0))
        _FLIGHTS[key] = (lock, users + 1)
    acquired = False
    try:
        while not acquired:
            if cancelled is not None and cancelled.is_set():
                raise sqlite3.OperationalError("interrupted")
            acquired = lock.acquire(timeout=0.05)
        yield
    finally:
        if acquired:
            lock.release()
        with _LOCK:
            users = _FLIGHTS[key][1] - 1
            if users:
                _FLIGHTS[key] = (lock, users)
            else:
                del _FLIGHTS[key]


def get(key, revision, now):
    import json

    with _LOCK:
        entry = _CACHE.get(key)
        if entry is None or entry[0] != revision or not entry[1] <= now < entry[2]:
            return None
        _CACHE.move_to_end(key)
        return json.loads(entry[3])


def put(key, revision, now, expires, payload):
    import json

    global _BYTES
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    if len(encoded) > MAX_BYTES:
        return
    with _LOCK:
        previous = _CACHE.pop(key, None)
        if previous:
            _BYTES -= len(previous[3])
        _CACHE[key] = (revision, now, expires, encoded)
        _BYTES += len(encoded)
        while len(_CACHE) > MAX_ENTRIES or _BYTES > MAX_BYTES:
            _, removed = _CACHE.popitem(last=False)
            _BYTES -= len(removed[3])


def state(connection):
    try:
        row = connection.execute(
            "SELECT version,generation FROM engine_read_index_state WHERE id=1"
        ).fetchone()
    except Exception as exc:
        import sqlite3

        if not isinstance(exc, sqlite3.OperationalError):
            raise
        return None
    return row[1] if row and row[0] == read_indexes.expected_version() else None


def deadline(connection, now, default):
    # Coverage includes all expired clean rows, not only displayed/eligible companies.
    stamp = now.isoformat()
    row = connection.execute(
        "SELECT (SELECT MIN(valid_until) FROM engine_company_ranking "
        "WHERE dirty=0 AND valid_until>?),"
        "(SELECT MIN(as_of) FROM engine_company_ranking WHERE dirty=0 AND as_of>?)",
        (stamp, stamp),
    ).fetchone()
    try:
        return min([default, *(datetime.fromisoformat(value) for value in row if value)])
    except (TypeError, ValueError):
        # Legacy malformed/naive timestamps retain authoritative SQL behavior; do not cache.
        return now
