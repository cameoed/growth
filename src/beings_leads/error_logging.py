"""Bounded local diagnostic files, independent of evidence storage."""

from __future__ import annotations

import io
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_runtime_handler: TimedRotatingFileHandler | None = None


class RuntimeLogHandler(TimedRotatingFileHandler):
    """Failed diagnostic I/O must not report through redirected stderr."""

    def __init__(self, *args, **kwargs):
        self._retry_after = 0.0
        self._last_notice = float("-inf")
        super().__init__(*args, **kwargs)

    def emit(self, record):
        if time.monotonic() >= self._retry_after:
            super().emit(record)

    def flush(self):
        if time.monotonic() < self._retry_after:
            return
        try:
            super().flush()
        except (OSError, ValueError):
            self.handleError(None)

    def handleError(self, record):
        # The base implementation writes to sys.stderr, which routes back here.
        error = sys.exc_info()[1]
        now = time.monotonic()
        self._retry_after = now + 5
        if now - self._last_notice < 60:
            return
        self._last_notice = now
        fallback = sys.__stderr__
        if fallback is None or isinstance(fallback, DiagnosticStream):
            return
        try:
            fallback.write(
                "Beings Growth diagnostic log unavailable "
                f"({type(error).__name__}, errno={getattr(error, 'errno', None)}); "
                "retrying local logging after 5 seconds.\n"
            )
            fallback.flush()
        except Exception:
            pass  # The original native startup stream can also be full or closed.


def prune_runtime_logs(directory: Path, *, now: datetime | None = None) -> int:
    cutoff = (now or datetime.now(UTC)) - timedelta(days=7)
    removed = 0
    for path in directory.glob("beings-growth-runtime.log.*"):
        try:
            day = datetime.strptime(path.name.rsplit(".", 1)[-1], "%Y-%m-%d").date()
        except ValueError:
            continue
        # Drop the cutoff day too: never keep a file containing older-than-seven-day lines.
        if day <= cutoff.date():
            path.unlink(missing_ok=True)
            removed += 1
    legacy = directory / "beings-growth.log"
    if legacy.exists() and legacy.stat().st_mtime < cutoff.timestamp():
        legacy.unlink(missing_ok=True)
        removed += 1
    active = directory / "beings-growth-runtime.log"
    handler = _runtime_handler
    if handler is not None:
        handler.acquire()
    try:
        if active.exists() and active.stat().st_mtime < cutoff.timestamp():
            # Append-mode writers remain valid; never unlink their current file.
            with active.open("w", encoding="utf-8"):
                pass
    finally:
        if handler is not None:
            handler.release()
    return removed


class DiagnosticStream(io.TextIOBase):
    def __init__(self, handler: TimedRotatingFileHandler):
        self.handler = handler

    def write(self, text: str) -> int:
        if text.strip():
            self.handler.handle(
                logging.makeLogRecord({"msg": text.rstrip(), "levelno": logging.INFO})
            )
        return len(text)

    def flush(self) -> None:
        self.handler.flush()


def configure_runtime_logging(directory: Path) -> TimedRotatingFileHandler:
    global _runtime_handler
    directory.mkdir(parents=True, exist_ok=True)
    prune_runtime_logs(directory)
    legacy = directory / "beings-growth.log"
    if legacy.exists():
        # The native wrapper holds this append-mode file for early startup only.
        # Successful worker startup retires its undated historical contents.
        with legacy.open("w", encoding="utf-8"):
            pass
    handler = RuntimeLogHandler(
        directory / "beings-growth-runtime.log",
        when="midnight",
        utc=True,
        backupCount=6,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _runtime_handler = handler
    stream = DiagnosticStream(handler)
    old_streams = (sys.stdout, sys.stderr)
    for item in [logging.getLogger(), *logging.Logger.manager.loggerDict.values()]:
        if isinstance(item, logging.Logger):
            for existing in item.handlers:
                if isinstance(existing, logging.StreamHandler) and existing.stream in old_streams:
                    existing.setStream(stream)
    sys.stdout = stream
    sys.stderr = stream
    return handler
