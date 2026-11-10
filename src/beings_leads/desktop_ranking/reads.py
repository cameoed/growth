"""Exact SQLite reads across one short-lived WAL snapshot."""

import base64
import logging
import sqlite3
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass

from ..storage_json import decode_text
from .wire import FRAME_BYTES, ProtocolError

MAX_QUERIES = 512
MAX_QUERY_LENGTH = 512 * 1024
CACHE_BYTES = 8 * 1024 * 1024
logger = logging.getLogger(__name__)
_ALLOWED_PRAGMAS = {"schema_version", "query_only", "table_info", "table_xinfo"}
_READ_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE}


def authorize_read(action, argument, value, database, source):
    if action in _READ_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and value not in {
        "load_extension",
        "readfile",
        "writefile",
    }:
        return sqlite3.SQLITE_OK
    if (
        action == sqlite3.SQLITE_PRAGMA
        and argument in _ALLOWED_PRAGMAS
        and (value is None or argument in {"table_info", "table_xinfo"})
    ):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def query_key(sql, parameters=()):
    # Use identical SQL text and SQLite positional bindings for lookup.
    return sql, tuple(parameters)


def row_size(row):
    return 32 + sum(
        16 + len(value) * (2 if isinstance(value, str) else 1)
        if isinstance(value, (str, bytes))
        else 24
        for value in row
    )


@dataclass
class Rows:
    columns: list[str]
    values: list
    size: int
    error: str | None = None


def write_rows(wire, columns, rows):
    batch, size = [], 0
    for row in rows:
        size += row_size(row)
        batch.append(
            [
                [base64.b64encode(value).decode("ascii")] if isinstance(value, bytes) else value
                for value in row
            ]
        )
        if size >= FRAME_BYTES or len(batch) >= 256:
            wire.send(["more", columns, batch] if columns is not None else batch)
            columns = None
            batch, size = [], 0
    if columns is not None:
        wire.send(["complete", columns, batch])
        return
    if batch:
        wire.send(batch)
    wire.send(None)


def receive_rows(wire):
    head = wire.receive()
    if isinstance(head, dict):
        return Rows([], [], 0, head["sql_error"])
    status, columns, batch = head
    if status not in {"complete", "more"} or not isinstance(columns, list):
        raise ProtocolError("Invalid SQL result")
    rows, size = [], 0
    while batch is not None:
        decoded = [
            [
                base64.b64decode(value[0], validate=True) if isinstance(value, list) else value
                for value in row
            ]
            for row in batch
        ]
        rows.extend(decoded)
        size += sum(map(row_size, decoded))
        batch = wire.receive() if status == "more" else None
    return Rows(columns, rows, size)


def write_queries(connection, wire, queries):
    if not isinstance(queries, list) or not 1 <= len(queries) <= MAX_QUERIES:
        raise ProtocolError("Invalid ranking query batch")
    for sql, parameters in queries:
        if len(sql) > MAX_QUERY_LENGTH:
            raise ProtocolError("Ranking query is too large")
        started = time.monotonic()
        cursor = connection.cursor()
        cursor.row_factory = None
        try:
            cursor.execute(sql, parameters)
        except sqlite3.Error as exc:
            wire.send({"sql_error": str(exc)})
            logger.warning("Ranking read failed: %s", type(exc).__name__)
            continue
        if cursor.description is None:
            raise ProtocolError("Ranking query did not return rows")
        write_rows(wire, [column[0] for column in cursor.description], cursor)
        elapsed = time.monotonic() - started
        if elapsed >= 0.6:
            logger.info("Slow ranking read %.2fs: %s", elapsed, " ".join(sql.split())[:140])


class SqlRow(tuple):
    def __new__(cls, values, columns, indices):
        row = super().__new__(
            cls,
            (
                decode_text(value) if column.lower().endswith("_json") else value
                for column, value in zip(columns, values, strict=True)
            ),
        )
        row.columns = columns
        row.indices = indices
        return row

    def keys(self):
        return self.columns

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                key = self.indices[key.lower() if key.isascii() else key]
            except KeyError:
                raise IndexError("No item with that key") from None
        return super().__getitem__(key)


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.row_factory = SqlRow
        self.description = None
        self.rows = iter(())

    def execute(self, sql, parameters=()):
        return self._load(self.connection.query(sql, parameters))

    def _load(self, result):
        if result.error is not None:
            raise sqlite3.OperationalError(result.error)
        columns = result.columns
        self.description = [(column, None, None, None, None, None, None) for column in columns]
        if self.row_factory is None:
            self.rows = map(tuple, result.values)
        else:
            indices = {
                c.lower() if c.isascii() else c: i for i, c in reversed(list(enumerate(columns)))
            }
            self.rows = (SqlRow(values, columns, indices) for values in result.values)
        return self

    def __iter__(self):
        return self.rows

    def fetchone(self):
        return next(self.rows, None)

    def fetchall(self):
        return list(self.rows)


class SnapshotConnection:
    """DB-API read subset used by the authoritative scorer; no local database is opened."""

    in_transaction = True

    def __init__(self, exchange):
        self.exchange = exchange
        self.cache = OrderedDict()
        self.cached_bytes = 0

    def cursor(self):
        return Cursor(self)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def query(self, sql, parameters):
        key = query_key(sql, parameters)
        if key not in self.cache:
            (result,) = self.exchange([key])
            self._remember(key, result)
            return result
        self.cache.move_to_end(key)
        return self.cache[key]

    def _prefetch_results(self, queries):
        pending = [
            key
            for key in dict.fromkeys(query_key(*args) for args in queries)
            if key not in self.cache
        ]
        for start in range(0, len(pending), MAX_QUERIES):
            chunk = pending[start : start + MAX_QUERIES]
            yield from zip(chunk, self.exchange(chunk), strict=True)

    def prefetch(self, queries):
        for key, result in self._prefetch_results(queries):
            self._remember(key, result)

    def execute_prefetched(self, query, companions):
        # Use the primary result immediately even when it exceeds the cache budget.
        primary = query_key(*query)
        result = self.cache.get(primary)
        for key, rows in self._prefetch_results((query, *companions)):
            if key == primary:
                result = rows
            else:
                self._remember(key, rows)
        return self.cursor()._load(result)

    @contextmanager
    def read_many(self, queries):
        """Stream one-use cursors through a bulk read without populating the cache."""
        results = iter(self.exchange(queries)) if queries else iter(())
        try:
            yield (self.cursor()._load(result) for result in results)
        finally:
            # A malformed source row can end evaluation early. Drain this request so
            # the next scorer read starts at its own reply.
            for _ in results:
                pass

    def _remember(self, key, result):
        # Oversized evidence stays exact and can be read on demand without retaining it twice.
        cost = result.size + len(key[0]) + 128
        if cost <= CACHE_BYTES:
            self.cache[key] = result
            self.cached_bytes += cost
            while self.cached_bytes > CACHE_BYTES:
                evicted, rows = self.cache.popitem(last=False)
                self.cached_bytes -= rows.size + len(evicted[0]) + 128
