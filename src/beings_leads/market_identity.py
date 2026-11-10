"""Prepare exact market name matching before taking the SQLite writer."""

import logging
import sqlite3
import time
from contextlib import contextmanager
from itertools import islice
from threading import Lock

logger = logging.getLogger("uvicorn.error").getChild("market_identity")
_TABLE = "market_identity_revision"
_CHANGES = "market_identity_changes"
_INPUTS = {
    "candidates": "id,hostname,label_hint",
    "snapshots": "id,candidate_id,observed_at,observation_json,ai_json",
    "snapshot_target_intelligence": "snapshot_id,canonical_domain",
    "sales_accounts": "candidate_id,company",
}
_cache_lock = Lock()
_cache = None


def initialize(connection):
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_CHANGES,),
    ).fetchone():
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {_TABLE}("
            "id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)"
        )
        connection.execute(f"INSERT OR IGNORE INTO {_TABLE} VALUES(1,0)")
        connection.execute(f"""CREATE TABLE IF NOT EXISTS {_CHANGES}(
            candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL
        )""")
        for table, columns in _INPUTS.items():
            for operation in ("INSERT", "UPDATE", "DELETE"):
                event = f"UPDATE OF {columns}" if operation == "UPDATE" else operation
                trigger = f"market_identity_{table}_{operation.lower()}"
                references = {"INSERT": ("NEW",), "UPDATE": ("OLD", "NEW"),
                              "DELETE": ("OLD",)}[operation]
                if table == "snapshot_target_intelligence":
                    snapshots = ",".join(f"{ref}.snapshot_id" for ref in references)
                    ids = f"SELECT candidate_id FROM snapshots WHERE id IN ({snapshots})"
                else:
                    column = "id" if table == "candidates" else "candidate_id"
                    ids = ",".join(f"{ref}.{column}" for ref in references)
                connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
                connection.execute(
                    f"CREATE TRIGGER {trigger} "
                    f"AFTER {event} ON {table} BEGIN "
                    f"UPDATE {_TABLE} SET revision=revision+1 WHERE id=1; "
                    f"INSERT INTO {_CHANGES}(candidate_id, revision) "
                    f"SELECT id, (SELECT revision FROM {_TABLE} WHERE id=1) "
                    f"FROM candidates WHERE id IN ({ids}) "
                    "ON CONFLICT(candidate_id) DO UPDATE SET revision=excluded.revision; END"
                )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _revision(connection):
    return connection.execute(f"SELECT revision FROM {_TABLE} WHERE id=1").fetchone()[0]


def group_names(entries):
    names = {}
    for name, company_key, candidate_id, rank in entries:
        companies = names.setdefault(name, {})
        current = companies.get(company_key)
        if current is None or rank > current[1]:
            companies[company_key] = (candidate_id, rank)
    return names


def _prepared_names(store, revision):
    """Reuse entries only if their per-candidate revision matches this read snapshot."""
    global _cache
    identity = store._database_identity()
    with _cache_lock:
        previous = _cache if _cache is not None and _cache[0] == identity else None
        if previous is not None and previous[1] == revision:
            return previous[4]
        versions = dict(store.connection.execute(f"""
            SELECT c.id,COALESCE(ch.revision,0)
            FROM candidates c LEFT JOIN {_CHANGES} ch ON ch.candidate_id=c.id
        """))
        old_versions, old_entries = previous[2:4] if previous is not None else ({}, {})
        entries = {cid: old_entries[cid] for cid, version in versions.items()
                   if old_versions.get(cid) == version and cid in old_entries}
        changed = (cid for cid, version in versions.items() if old_versions.get(cid) != version)
        if previous is None:
            rows = store._market_candidate_name_entries()
            for entry in rows:
                entries.setdefault(entry[2], []).append(entry)
        else:
            while batch := list(islice(changed, 400)):
                for entry in store._market_candidate_name_entries(batch):
                    entries.setdefault(entry[2], []).append(entry)
        names = group_names(entry for company in entries.values() for entry in company)
        _cache = (identity, revision, versions, entries, names)
        return names


@contextmanager
def write_with_identity(store, *, resolve):
    """Keep the exact name index consistent with the documents and cursor commit."""
    connection = store.connection
    if connection.in_transaction:
        raise sqlite3.OperationalError("Market publication requires an independent transaction")
    started = time.monotonic()
    stage = "identity_read" if resolve else "writer_wait"
    try:
        for attempt in range(1, 4):
            names = {}
            if resolve:
                stage = "identity_read"
                connection.execute("BEGIN")
                try:
                    revision = _revision(connection)
                    names = _prepared_names(store, revision)
                finally:
                    connection.rollback()
            stage = "writer_wait"
            writer_started = time.monotonic()
            connection.execute("BEGIN IMMEDIATE")
            if not resolve or _revision(connection) == revision:
                break
            connection.rollback()
            logger.info("market_identity_retry attempt=%d reason=identity_changed", attempt)
        else:
            raise RuntimeError("Company identities changed during market name matching")
        stage = "publication"
        acquired = time.monotonic()
        yield names
        connection.commit()
        logger.info(
            "market_publication names=%d prepare_ms=%.0f writer_wait_ms=%.0f writer_hold_ms=%.0f",
            len(names), (writer_started - started) * 1000, (acquired - writer_started) * 1000,
            (time.monotonic() - acquired) * 1000,
        )
    except BaseException as exc:
        connection.rollback()
        logger.warning(
            "market_publication_failed stage=%s elapsed_ms=%.0f error=%s sqlite_code=%s",
            stage, (time.monotonic() - started) * 1000, type(exc).__name__,
            getattr(exc, "sqlite_errorcode", None),
        )
        raise
