"""Shared admission for current work, without leases, evidence files or database writes.

Operating system byte locks release automatically on process death. A single descriptor per
process/path and a local reservation set also coordinate threads.
Never unlink the operational lock file: replacing its inode would split admission domains.
"""
from __future__ import annotations

import asyncio
import errno
import math
import os
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from .file_lock import lock_slot, unlock_slot


def _local_ranking_cpu_limit():
    if os.name == "nt":
        return 4
    try:
        memory = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return 4
    return max(4, min(8, (os.cpu_count() or 2) - 2, memory // (2 * 1024**3)))


LIMITS = {
    "network": (0, 30),
    "cpu": (64, 4),
    # Background desktop ranking can also claim adjacent slots from this same range.
    "local_ranking_cpu": (64, _local_ranking_cpu_limit()),
    "parse_jobs": (96, 2),
    "parse_bytes": (128, 16),
    "microlink_network": (192, 128),
}
PARSE_BYTE_UNIT = 4 * 1024 * 1024
_controllers = {}
_guard = threading.Lock()
_metrics = {}


def database_for_resources(database=None):
    if database is None:
        from .provider_rate_limit import DATABASE

        database = DATABASE.get() or os.environ.get("BEINGS_LEADS_DB", "data/leads.db")
    return Path(database).resolve()


class _Controller:
    def __init__(self, database):
        self.path = database.with_name(f".{database.name}.scan-resources.lock")
        self.lock = threading.Lock()
        self.fd = None
        self.held = set()

    def _close_idle(self):
        if not self.held and self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def take(self, lane, weight):
        start, count = LIMITS[lane]
        with self.lock:
            if self.fd is None:
                self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            acquired = []
            try:
                for slot in range(start, start + count):
                    if slot in self.held:
                        continue
                    try:
                        lock_slot(self.fd, slot)
                    except OSError as exc:
                        if exc.errno in (errno.EACCES, errno.EAGAIN):
                            continue
                        raise
                    acquired.append(slot)
                    self.held.add(slot)
                    if len(acquired) == weight:
                        return acquired
            except BaseException:
                self.release(acquired, locked=True)
                raise
            self.release(acquired, locked=True)
            return None

    def release(self, slots, *, locked=False):
        if not locked:
            with self.lock:
                return self.release(slots, locked=True)
        for slot in slots:
            unlock_slot(self.fd, slot)
            self.held.remove(slot)
        self._close_idle()


def _controller(database):
    key = os.getpid(), database_for_resources(database)
    with _guard:
        if key not in _controllers:
            _controllers[key] = _Controller(key[1])
        return _controllers[key]


def _weight(lane, weight):
    if lane not in LIMITS or not 1 <= weight <= LIMITS[lane][1]:
        raise ValueError("Invalid shared resource admission")
    return weight


def parse_weight(size):
    # Includes conservative tree expansion, in addition to the encoded input packet.
    # Oversized pages take the entire lane; the complete input is never truncated.
    return min(LIMITS["parse_bytes"][1], max(1, math.ceil(size * 24 / PARSE_BYTE_UNIT)))


def _record(lane, started):
    with _guard:
        row = _metrics.setdefault(lane, {
            "admitted": 0, "wait_seconds": 0.0, "hold_seconds": 0.0,
        })
        row["admitted"] += 1
        row["wait_seconds"] += time.monotonic() - started


def _record_hold(lane, started):
    with _guard:
        _metrics[lane]["hold_seconds"] += time.monotonic() - started


def metrics():
    with _guard:
        return {key: dict(value) for key, value in _metrics.items()}


@contextmanager
def resource(lane, *, weight=1, database=None):
    weight = _weight(lane, weight)
    controller = _controller(database)
    started = time.monotonic()
    slots = None
    try:
        while slots is None:
            slots = controller.take(lane, weight)
            if slots is None:
                time.sleep(.02)
        _record(lane, started)
        admitted = time.monotonic()
        yield
    finally:
        if slots is not None:
            _record_hold(lane, admitted)
            controller.release(slots)


@asynccontextmanager
async def async_resource(lane, *, weight=1, database=None):
    weight = _weight(lane, weight)
    controller = _controller(database)
    started = time.monotonic()
    slots = None
    try:
        while slots is None:
            slots = controller.take(lane, weight)
            if slots is None:
                await asyncio.sleep(.02)
        _record(lane, started)
        admitted = time.monotonic()
        yield
    finally:
        if slots is not None:
            _record_hold(lane, admitted)
            controller.release(slots)
