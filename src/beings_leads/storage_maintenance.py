"""Offline, resumable representation changes; no collection or production copies."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .domain_policy import is_disallowed
from .sales_engine.evidence_accounting import (
    LEGACY,
    initialize_storage,
    migrate_company,
)
from .storage_json import COLUMNS, PREFIX, configure, decode_text, encode_text

MAX_BATCH_BYTES = 8 * 1024 * 1024
_RECLAIM_BATCH_BYTES = 16 * 1024 * 1024
_RECLAIM_BUSY_MS = 250


def preservation_fingerprints(connection, *, progress=lambda value: None):
    """Hash untouched fields; transformed JSON/accounting is verified record by record."""
    result = {}
    for (table,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND name NOT IN ('engine_evidence_accounting','engine_evidence_accounting_legacy',"
        "'engine_evidence_packets') ORDER BY name"
    ).fetchall():
        info = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        columns = [row[1] for row in info if row[1] not in COLUMNS.get(table, {})]
        keys = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
        order = ",".join('"' + key + '"' for key in keys) if keys else "rowid"
        projection = ",".join('"' + column + '"' for column in columns)
        digest, count = hashlib.sha256(), 0
        for row in connection.execute(f'SELECT {projection} FROM "{table}" ORDER BY {order}'):
            # One canonical serialization per row avoids millions of per-cell
            # encoders. SQLite scalar types stay distinct; BLOBs have a typed tag.
            digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":"),
                                     default=_blob_json).encode("utf-8") + b"\n")
            count += 1
        result[table] = {"rows": count, "columns": columns, "sha256": digest.hexdigest()}
        progress({"stage": "preservation", "table": table, "rows": count})
    return result


def _blob_json(value):
    if isinstance(value, bytes):
        return {"$sqlite_blob_hex": value.hex()}
    raise TypeError(f"Unsupported SQLite scalar: {type(value).__name__}")


def checkpoint(connection):
    busy, _, _ = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if busy:
        raise RuntimeError("Storage maintenance requires all app readers/writers to be stopped")


def enable_incremental_vacuum_for_empty_store(connection):
    """Choose the file format before the first transaction or WAL header."""
    if connection.execute("PRAGMA page_count").fetchone()[0] == 0:
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")


def database_file_sizes(path):
    """Return apparent file size and allocated disk bytes for the active main file."""
    sizes = path.stat()
    blocks = getattr(sizes, "st_blocks", None)
    return sizes.st_size, sizes.st_size if blocks is None else blocks * 512


def reclaim_free_pages(connection, database, *, progress=None):
    """Release freelist pages in retryable transactions after an independent commit."""
    if connection.in_transaction:
        raise ValueError("Commit caller work before reclaiming disk space")
    path = Path(database)
    status, error = "complete", None
    busy = "Close other database readers or writers and try reclaiming disk space again."
    before = after = database_file_sizes(path)
    timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    connection.execute(f"PRAGMA busy_timeout={_RECLAIM_BUSY_MS}")
    try:
        # Checkpoint before starting, then after each batch to bound the WAL
        # and make the main file's truncation visible on disk.
        if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
            status, error = "deferred", busy
        else:
            total = connection.execute("PRAGMA freelist_count").fetchone()[0]
            mode = connection.execute("PRAGMA auto_vacuum").fetchone()[0]
            if total and mode == 0:
                status = "needs_migration"
            elif total and mode == 2:
                page_size = connection.execute("PRAGMA page_size").fetchone()[0]
                batch_pages = max(1, _RECLAIM_BATCH_BYTES // page_size)
                processed = 0
                if progress is not None:
                    progress(processed, total)
                while processed < total:
                    with connection:
                        connection.execute("BEGIN IMMEDIATE")
                        free = connection.execute("PRAGMA freelist_count").fetchone()[0]
                        batch = min(free, batch_pages, total - processed)
                        if not batch:
                            break
                        # Exhaust the pragma cursor before committing the batch.
                        connection.execute(f"PRAGMA incremental_vacuum({batch})").fetchall()
                        remaining = connection.execute("PRAGMA freelist_count").fetchone()[0]
                    if remaining >= free:
                        status, error = "deferred", "SQLite did not release additional pages."
                        break
                    if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                        status, error = "deferred", busy
                        break
                    processed += free - remaining
                    if progress is not None:
                        progress(min(processed, total), total)
            after = database_file_sizes(path)
    except sqlite3.OperationalError as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            status, error = "deferred", busy
        elif code == sqlite3.SQLITE_FULL:
            status, error = "deferred", "Free some working space and retry disk reclamation."
        elif code == sqlite3.SQLITE_IOERR:
            status, error = "deferred", "A disk I/O error interrupted reclamation."
        else:
            raise
        after = database_file_sizes(path)
    finally:
        connection.execute(f"PRAGMA busy_timeout={timeout}")
    result = {
        "reclaim_status": status,
        "reclaimed_bytes": max(0, before[1] - after[1]),
        "reclaimed_file_bytes": max(0, before[0] - after[0]),
    }
    if error:
        result["reclaim_error"] = error
    return result


@contextmanager
def without_triggers(connection, table):
    """Caller owns a transaction; rollback restores every original trigger as well."""
    triggers = connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
    ).fetchall()
    for name, _ in triggers:
        connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
    yield
    for _, sql in triggers:
        connection.execute(sql)


def _validate_owner(owner):
    if owner and is_disallowed(owner):
        raise ValueError("Disallowed company owner requires the existing purge maintenance first")


def compress_table(connection, table, columns, *, batch_size=64, reproject=False,
                   progress=lambda value: None):
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    names = list(columns)
    quoted = ",".join('"' + name + '"' for name in names)
    available = {r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')}
    owners = [name for name in ("company_key", "canonical_domain") if name in available]
    owner_projection = "".join(',"' + name + '"' for name in owners)
    cursor, checked, changed, saved = 0, 0, 0, 0
    digest = hashlib.sha256()
    while True:
        read = connection.execute(
            f'SELECT rowid,{quoted}{owner_projection} FROM "{table}" '
            'WHERE rowid>? ORDER BY rowid', (cursor,)
        )
        batch, batch_bytes = [], 0
        for row in read:
            batch.append(tuple(row))
            batch_bytes += sum(len(v.encode("utf-8")) for v in row[1:] if isinstance(v, str))
            if len(batch) >= batch_size or batch_bytes >= MAX_BATCH_BYTES:
                break
        read.close()
        if not batch:
            break
        updates = []
        for row in batch:
            values = row[1:1 + len(names)]
            for owner in row[1 + len(names):]:
                _validate_owner(owner)
            replacements = []
            logical = []
            for column, value in zip(names, values, strict=True):
                original = decode_text(value)
                logical.append(original)
                if original is None:
                    replacements.append(value)
                    continue
                decoded = json.loads(original)
                if table == "snapshots" and column == "observation_json":
                    # Canonical identity owns the evidence; an older discovery
                    # alias/platform host does not replace an allowed owner.
                    _validate_owner(decoded.get("canonical_host") or decoded.get("hostname"))
                # External asset hosts are locators, not company ownership.
                # Representation changes preserve them byte for byte. Explicit
                # purge/audit code still decodes all content before policy checks.
                packed = value if value.startswith(PREFIX) and not reproject else encode_text(
                    original, columns[column]
                )
                if decode_text(packed) != original:
                    raise ValueError("Lossless JSON verification failed")
                replacements.append(packed)
            digest.update((json.dumps([row[0], *logical], ensure_ascii=False) + "\n").encode())
            checked += 1
            if tuple(replacements) != values:
                updates.append((*replacements, row[0]))
                saved += sum(len(old.encode()) - len(new.encode())
                             for old, new in zip(values, replacements, strict=True)
                             if isinstance(old, str) and isinstance(new, str))
        if updates:
            connection.execute("BEGIN IMMEDIATE")
            try:
                with without_triggers(connection, table):
                    assignments = ",".join(f'"{name}"=?' for name in names)
                    connection.executemany(
                        f'UPDATE "{table}" SET {assignments} WHERE rowid=?', updates
                    )
                    for values in updates:
                        stored = tuple(connection.execute(
                            f'SELECT {quoted} FROM "{table}" WHERE rowid=?', (values[-1],)
                        ).fetchone())
                        if stored != values[:-1]:
                            raise ValueError("Stored JSON verification failed")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            changed += len(updates)
        cursor = batch[-1][0]
        if checked % 1024 < len(batch):
            checkpoint(connection)
            progress({"table": table, "checked": checked, "changed": changed,
                      "payload_bytes_saved": saved})
    checkpoint(connection)
    return {"checked": checked, "changed": changed, "payload_bytes_saved": saved,
            "logical_sha256": digest.hexdigest()}


def optimize(connection, *, batch_size=64, reproject=False, progress=lambda value: None):
    """All changes are bounded and independently verified; rerunning safely resumes."""
    if connection.in_transaction:
        raise ValueError("Commit caller work before offline maintenance")
    configure(connection, rows=False)
    if connection.row_factory is not None:
        raise ValueError("Offline maintenance requires raw SQLite rows")
    connection.execute("PRAGMA secure_delete=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA cache_size=-8192")
    connection.execute("PRAGMA mmap_size=0")
    connection.execute("PRAGMA journal_size_limit=16777216")
    connection.execute("BEGIN IMMEDIATE")
    try:
        initialize_storage(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = {}
    for table, columns in COLUMNS.items():
        if table not in tables:
            continue
        result[table] = compress_table(connection, table, columns, batch_size=batch_size,
                                       reproject=reproject,
                                       progress=progress)
        progress({"table": table, **result[table]})
    companies = records = 0
    digest = hashlib.sha256()
    while True:
        row = connection.execute(f"SELECT company_key FROM {LEGACY} "
                                 "ORDER BY company_key LIMIT 1").fetchone()
        if row is None:
            break
        company = row[0]
        _validate_owner(company)
        connection.execute("BEGIN IMMEDIATE")
        try:
            count, fingerprint = migrate_company(
                connection, company, validate_record=json.loads
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        digest.update(json.dumps([company, count, fingerprint]).encode())
        companies += 1
        records += count
        if companies % 25 == 0:
            checkpoint(connection)
            progress({"table": "engine_evidence_accounting", "companies": companies,
                      "records": records})
    checkpoint(connection)
    result["accounting_migrated"] = {"companies": companies, "records": records,
                                      "logical_sha256": digest.hexdigest()}
    return result


def verify_integrity(connection):
    # Offline FK checks revisit wide parent-table pages. A tiny cache causes
    # repeated physical reads on the full inventory; restore the caller's bound.
    previous_cache = connection.execute("PRAGMA cache_size").fetchone()[0]
    connection.execute("PRAGMA cache_size=-262144")
    try:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Database quick_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Database foreign_key_check failed")
    finally:
        connection.execute(f"PRAGMA cache_size={previous_cache}")


def compaction_budget(connection, path):
    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    pages = connection.execute("PRAGMA page_count").fetchone()[0]
    free = connection.execute("PRAGMA freelist_count").fetchone()[0]
    # VACUUM INTO needs one replacement. Reserve 25% plus 256 MiB for page layout
    # and operational journals; never start a rewrite hoping the volume will fit.
    live = (pages - free) * page_size
    required = live * 5 // 4 + 256 * 1024 * 1024
    available = shutil.disk_usage(Path(path).parent).free
    return {"active_bytes": pages * page_size, "live_page_bytes": live,
            "freelist_bytes": free * page_size, "working_bytes_required": required,
            "working_bytes_available": available, "can_compact": available >= required}
