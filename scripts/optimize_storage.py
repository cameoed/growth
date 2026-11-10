#!/usr/bin/env python3
"""Offline lossless storage optimization with verified atomic compaction and no backups."""

import argparse
import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from beings_leads.storage_json import configure
from beings_leads.storage_maintenance import (
    checkpoint,
    compaction_budget,
    optimize,
    preservation_fingerprints,
    verify_integrity,
)


def progress(value):
    print(json.dumps(value), flush=True)


def fingerprints(connection):
    """Hash every physical row without exporting any company evidence."""
    result = {}
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND (name NOT LIKE 'sqlite_%' OR name='sqlite_sequence') "
        "ORDER BY name"
    ).fetchall()
    for (table,) in tables:
        info = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        keys = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
        order = ",".join('"' + key + '"' for key in keys) if keys else "rowid"
        digest, count = hashlib.sha256(), 0
        for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):
            for value in row:
                data = value if isinstance(value, bytes) else json.dumps(
                    value, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                digest.update(type(value).__name__.encode() + b":"
                              + str(len(data)).encode() + b":" + data)
            digest.update(b"\n")
            count += 1
        result[table] = {"rows": count, "sha256": digest.hexdigest()}
        progress({"stage": "fingerprint", "table": table, "rows": count})
    return result


def compact(connection, database, *, enable_incremental_vacuum=False):
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    budget = compaction_budget(connection, database)
    progress({"stage": "compaction_budget", **budget})
    if not budget["can_compact"]:
        return {"compacted": False, **budget}
    replacement = database.with_name("." + database.name + ".storage-working")
    if replacement.exists():
        raise RuntimeError("A prior migration working file exists; inspect it before continuing")
    before = fingerprints(connection)
    schema_before = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type,name"
    ).fetchall()
    checkpoint(connection)
    created = False
    published = False
    try:
        fd = os.open(replacement, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        os.close(fd)
        if (enable_incremental_vacuum
                and connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 0):
            # SQLite applies the requested pointer-map format to the replacement.
            # A legacy source has no pointer map and keeps its original mode.
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        connection.execute("VACUUM INTO ?", (str(replacement),))
        with closing(sqlite3.connect(replacement)) as target:
            configure(target, rows=False)
            target.execute("PRAGMA foreign_keys=ON")
            if enable_incremental_vacuum:
                target.execute("PRAGMA auto_vacuum=INCREMENTAL")
                if target.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
                    raise ValueError("Compaction could not enable incremental vacuum")
            progress({"stage": "replacement_integrity", "state": "started"})
            verify_integrity(target)
            progress({"stage": "replacement_integrity", "state": "verified"})
            if fingerprints(target) != before:
                raise ValueError("Compaction changed physical row contents")
            if target.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                "ORDER BY type,name"
            ).fetchall() != schema_before:
                raise ValueError("Compaction changed schema or immutable guards")
            if journal_mode == "wal":
                if target.execute("PRAGMA journal_mode=WAL").fetchall() != [("wal",)]:
                    raise ValueError("Compaction could not preserve WAL mode")
                target.commit()
        with replacement.open("rb") as verified:
            os.fchmod(verified.fileno(), stat.S_IMODE(database.stat().st_mode))
            os.fsync(verified.fileno())
        checkpoint(connection)
        connection.close()
        # Closing SQLite owns sidecar cleanup. Never manually delete active WAL/SHM.
        os.replace(replacement, database)
        published = True
        directory = os.open(database.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {"compacted": True, "after_bytes": database.stat().st_size,
                "fingerprints": before}
    finally:
        if created and not published and replacement.exists():
            # The original active file is still the only operational dataset.
            replacement.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--compact-only", action="store_true")
    parser.add_argument("--no-compact", action="store_true")
    parser.add_argument("--enable-incremental-vacuum", action="store_true",
                        help="Enable incremental page reclamation in the compacted replacement")
    parser.add_argument("--release-zero-blocks", action="store_true",
                        help="Release already-zero disk blocks on macOS before compaction")
    parser.add_argument("--reproject", action="store_true",
                        help="Rebuild existing envelope projections after a SQL-path migration")
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    if args.no_compact and args.enable_incremental_vacuum:
        parser.error("--enable-incremental-vacuum requires compaction")
    database = args.database.resolve(strict=True)
    lock = os.open(database.parent, os.O_RDONLY)
    connection = None
    previous_term = signal.getsignal(signal.SIGTERM)

    def cancel(*_):
        raise KeyboardInterrupt("Storage maintenance cancelled; committed batches remain valid")

    signal.signal(signal.SIGTERM, cancel)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "Stop the dashboard and scanners before storage maintenance"
            ) from exc
        before = database.stat().st_size
        connection = sqlite3.connect(database, timeout=5)
        configure(connection, rows=False)
        result = {"before_bytes": before, "backups_created": 0}
        if not args.compact_only:
            preserved = preservation_fingerprints(connection, progress=progress)
            result["optimization"] = optimize(
                connection, reproject=args.reproject, progress=progress
            )
            if preservation_fingerprints(connection, progress=progress) != preserved:
                raise ValueError("Storage rewrite changed protected fields or original ledgers")
            result["preserved"] = preserved
        progress({"stage": "source_integrity", "state": "started"})
        verify_integrity(connection)
        progress({"stage": "source_integrity", "state": "verified"})
        result["integrity"] = "ok"
        if args.release_zero_blocks:
            from beings_leads.storage_sparse import release_zero_blocks

            connection.execute("PRAGMA locking_mode=EXCLUSIVE")
            connection.execute("BEGIN EXCLUSIVE")
            connection.commit()
            checkpoint(connection)
            result["zero_blocks"] = release_zero_blocks(database, progress=progress)
            # Whole-file byte identity preserves the just-verified SQLite structure
            # and foreign keys. VACUUM's changed structure is verified separately.
        result["compaction"] = (
            {"compacted": False, **compaction_budget(connection, database)}
            if args.no_compact else compact(
                connection, database, enable_incremental_vacuum=args.enable_incremental_vacuum,
            )
        )
        result["after_bytes"] = database.stat().st_size
        if args.summary:
            args.summary.parent.mkdir(parents=True, exist_ok=True)
            args.summary.write_text(json.dumps(result, indent=2) + "\n")
        progress(result)
    finally:
        if connection is not None:
            connection.close()
        os.close(lock)
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    main()
