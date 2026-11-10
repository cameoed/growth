"""Structured Smart Scan diagnostics, independent of SQLite progress and collection."""

import asyncio
import json
import logging
import os
import sys
from collections import Counter
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from threading import Lock
from time import monotonic, process_time

MARKER = "smart_scan_diagnostic "
CHILD_ENV = "BEINGS_SMART_SCAN_DIAGNOSTICS"
SCAN_ID = ContextVar("diagnostic_scan_id", default=None)
SOURCE = ContextVar("diagnostic_source", default=None)


def emit(scan_id, event, **fields):
    if scan_id:
        logging.getLogger("beings_leads.web").info(
            "%s%s", MARKER, json.dumps({
                "scan_id": scan_id, "event": event, "at": datetime.now(UTC).isoformat(), **fields,
            }),
        )


def child_record(event, **fields):
    if os.getenv(CHILD_ENV) == "1":
        # The original operation can finish if the diagnostic pipe is closed.
        with suppress(OSError, ValueError):
            print(MARKER + json.dumps({"event": event, **fields}), file=sys.stderr, flush=True)


def error_fields(exc):
    fields = {"error_type": type(exc).__name__}
    if name := getattr(exc, "sqlite_errorname", None):
        fields["sqlite_error"] = name
    return fields


@contextmanager
def child_step(step):
    if os.getenv(CHILD_ENV) != "1":
        yield
        return
    wall, cpu = monotonic(), process_time()
    child_record("company_step", step=step, status="started")
    fields = {}
    try:
        yield
    except BaseException as exc:
        fields = error_fields(exc)
        raise
    finally:
        child_record(
            "company_step", step=step, status="error" if fields else "returned",
            wall_seconds=round(monotonic() - wall, 6), cpu_seconds=round(process_time() - cpu, 6),
            **fields,
        )


class SourceMetrics:
    def __init__(self):
        self.counts = Counter()
        self.seconds = Counter()
        self.longest = Counter()
        self.result = {}
        self.lock = Lock()

    def record(self, key, seconds=None):
        with self.lock:
            self.counts[key] += 1
            if seconds is not None:
                self.seconds[key] += seconds
                self.longest[key] = max(self.longest[key], seconds)


@contextmanager
def source_trace(operation, *, child=False, **fields):
    scan_id = SCAN_ID.get()
    if not scan_id and not (child and os.getenv(CHILD_ENV) == "1"):
        yield None
        return
    metrics = SourceMetrics()
    token = SOURCE.set(metrics)
    started = monotonic()
    outcome = "returned"
    error = {}
    try:
        if not child:
            emit(scan_id, "source_started", operation=operation, **fields)
        yield metrics
    except BaseException as exc:
        outcome = "interrupted" if isinstance(exc, asyncio.CancelledError) else "error"
        error = error_fields(exc)
        raise
    finally:
        SOURCE.reset(token)
        with metrics.lock:
            report = dict(
                operation=operation, outcome=outcome, wall_seconds=round(monotonic() - started, 6),
                counts=dict(metrics.counts),
                seconds={k: round(v, 6) for k, v in metrics.seconds.items()},
                longest_seconds={k: round(v, 6) for k, v in metrics.longest.items()},
                result=metrics.result, **fields, **error,
            )
        if child:
            child_record("source_finished", **report)
        else:
            emit(scan_id, "source_finished", **report)


@contextmanager
def source_time(key):
    metrics = SOURCE.get()
    if metrics is None:
        yield
        return
    started = monotonic()
    try:
        yield
    except BaseException as exc:
        metrics.record(f"{key}.{type(exc).__name__}")
        raise
    finally:
        metrics.record(key, monotonic() - started)


def record_http(response):
    metrics = SOURCE.get()
    if metrics is not None:
        if response.extensions.get("beings_request_deferred"):
            metrics.record("http.local_rate_deferral")
        else:
            metrics.record(f"http.{response.status_code}")
