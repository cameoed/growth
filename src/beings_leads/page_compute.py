"""Bounded pure page extraction; adaptive planning stays ordered in the owner."""
from __future__ import annotations

import asyncio
import atexit
import os
import pickle
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from threading import Lock

from .scan_resources import async_resource, database_for_resources, parse_weight, resource

_pool_instance = None
_pool_guard = Lock()
MAX_PACKET_BYTES = 8 * 1024 * 1024


def _forbid_io(*args, **kwargs):
    raise RuntimeError("Pure page workers cannot access databases or the network")


def _initialize():
    import _sqlite3
    import socket
    import sqlite3

    _sqlite3.connect = sqlite3.connect = sqlite3.dbapi2.connect = _forbid_io
    socket.create_connection = socket.socket.connect = socket.socket.connect_ex = _forbid_io


def _extract(packet, database):
    from .inspect import _extract_page

    with resource("cpu", database=database):
        url, status, body, research, observed_at = pickle.loads(packet)
        result = _extract_page(url, status, body, research=research, observed_at=observed_at)
    return result, research


def _pool():
    global _pool_instance
    with _pool_guard:
        if _pool_instance is None:
            _pool_instance = ProcessPoolExecutor(
                max_workers=2, mp_context=get_context("spawn"), initializer=_initialize,
            )
        return _pool_instance


async def extract_page(url, status, body, *, research):
    from datetime import UTC, datetime

    from .inspect import _extract_page

    observed_at = datetime.now(UTC)
    try:
        enabled = int(os.environ.get("BEINGS_PAGE_WORKERS", "2")) > 0
    except ValueError:
        enabled = False
    if not enabled:
        return _extract_page(url, status, body, research=research, observed_at=observed_at)
    database = database_for_resources()
    async with async_resource("parse_jobs", database=database):
        # Acquire before encoding so queued companies cannot accumulate copied packets.
        try:
            packet = pickle.dumps((url, status, body, research, observed_at),
                                  protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            async with async_resource("cpu", database=database):
                return _extract_page(url, status, body, research=research, observed_at=observed_at)
        async with async_resource(
            "parse_bytes", weight=parse_weight(len(packet)), database=database,
        ):
            return await _dispatch(packet, url, status, body, research, observed_at, database)


async def _dispatch(packet, url, status, body, research, observed_at, database):
    from .inspect import _extract_page

    if len(packet) > MAX_PACKET_BYTES:
        # Preserve all evidence when serial execution is cheaper/safer than copying it.
        async with async_resource("cpu", database=database):
            return _extract_page(url, status, body, research=research, observed_at=observed_at)
    future = None
    try:
        future = asyncio.get_running_loop().run_in_executor(_pool(), _extract, packet, database)
        result, updated = await asyncio.shield(future)
    except asyncio.CancelledError:
        # A process cannot be interrupted safely midway through extraction. Keep
        # ownership/byte permits until it finishes; never apply its result after stop.
        while future is not None and not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if future is not None and not future.cancelled():
            future.exception()
        raise
    except Exception:
        # Failed dispatch retains the complete authoritative path and planner state.
        # Pure workers cannot have performed any external side effect to repeat.
        async with async_resource("cpu", database=database):
            return _extract_page(url, status, body, research=research, observed_at=observed_at)
    research.__dict__.clear()
    research.__dict__.update(updated.__dict__)
    return result


def shutdown():
    global _pool_instance
    with _pool_guard:
        pool, _pool_instance = _pool_instance, None
    if pool is not None:
        pool.shutdown(wait=True, cancel_futures=True)


atexit.register(shutdown)
