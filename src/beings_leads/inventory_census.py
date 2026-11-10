"""One exact company census keyed by transactional inputs and temporal boundaries."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from threading import Lock

from .domain_policy import disallowed_domains, policy_rules

VERSION = 1
MIGRATION = "scan-inventory-revision-v1"
# Only values used by the company census, not scoring or unrelated bookkeeping.
INPUTS = {
    "candidates": ("id", "source_apex", "hostname"),
    "snapshots": ("id", "candidate_id", "observed_at", "observation_json"),
    "snapshot_target_intelligence": ("snapshot_id", "canonical_domain"),
    "prospect_workflow": ("candidate_id", "scan_enabled"),
    "company_scan_work": ("company_key", "effort_key", "status", "completed_at",
                          "lease_expires_at", "next_retry_at"),
}
TRIGGERS = tuple(f"scan_inventory_{table}_{operation}"
                 for table in INPUTS for operation in ("insert", "delete", "update"))


def initialize(connection):
    """Install only a derived revision counter; no census backfill or provider work."""
    connection.execute("SAVEPOINT scan_inventory_schema")
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS scan_inventory_state ("
                           "id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, "
                           "generation INTEGER NOT NULL)")
        connection.execute("INSERT OR IGNORE INTO scan_inventory_state VALUES (1,?,0)", (VERSION,))
        for table, columns in INPUTS.items():
            for operation in ("insert", "delete", "update"):
                update = f" OF {','.join(columns)}" if operation == "update" else ""
                changed = " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in columns)
                condition = f" WHEN {changed}" if operation == "update" else ""
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS scan_inventory_{table}_{operation} "
                    f"AFTER {operation.upper()}{update} ON {table}{condition} BEGIN "
                    "UPDATE scan_inventory_state SET generation=generation+1 WHERE id=1; END"
                )
        connection.execute("INSERT OR IGNORE INTO schema_migrations(name,applied_at,rows_changed) "
                           "VALUES (?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),0)", (MIGRATION,))
        connection.execute("RELEASE scan_inventory_schema")
    except BaseException:
        connection.execute("ROLLBACK TO scan_inventory_schema")
        connection.execute("RELEASE scan_inventory_schema")
        raise


def _revision(connection):
    try:
        row = connection.execute(
            "SELECT version,generation FROM scan_inventory_state WHERE id=1",
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return None
    triggers = connection.execute(
        f"SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' "
        f"AND name IN ({','.join('?' for _ in TRIGGERS)})", TRIGGERS,
    ).fetchone()[0]
    if row is None or row[0] != VERSION or triggers != len(TRIGGERS):
        return None
    return row[0], row[1], connection.execute("PRAGMA schema_version").fetchone()[0]


class InventoryCensus:
    def __init__(self):
        self.lock = Lock()
        self.entry = None

    def read(self, store, policy, *, progress=None, cancel=None):
        # Never share uncommitted or already-pinned caller state with another request.
        if store.connection.in_transaction or store.opened_read_only_identity is None:
            return store.smart_scan_inventory(max_company_failure_rate=policy, progress=progress)
        while not self.lock.acquire(timeout=0.05):
            if cancel is not None and cancel.is_set():
                raise sqlite3.OperationalError("interrupted")
        try:
            if cancel is not None and cancel.is_set():
                raise sqlite3.OperationalError("interrupted")
            identity = store._database_identity()
            if store.opened_read_only_identity != identity:
                self.entry = None
                raise sqlite3.OperationalError("database replaced during inventory read; retry")
            store.connection.execute("BEGIN")
            store.connection.execute("SELECT rootpage FROM sqlite_schema LIMIT 1").fetchone()
            revision = _revision(store.connection)
            domains = disallowed_domains()
            as_of = datetime.now(UTC)
            key = identity, revision, policy, domains
            entry = self.entry
            if (revision is not None and entry is not None and entry[0] == key
                    and entry[1] <= as_of < entry[2]):
                if progress:
                    progress("Reusing unchanged company inventory")
                return dict(entry[3])
            with policy_rules(domains):
                inventory = store.smart_scan_inventory(
                    max_company_failure_rate=policy, progress=progress,
                )
            deadline = store.smart_scan_inventory_valid_until(
                as_of, include_failure_history=policy is not None,
            )
            finished = datetime.now(UTC)
            if store._database_identity() != identity:
                self.entry = None
                raise sqlite3.OperationalError("database replaced during inventory read; retry")
            # Revision and census belong to the same committed snapshot. A later
            # relevant commit changes the next reader's revision in its own snapshot.
            if (revision is not None and domains == disallowed_domains()
                    and as_of <= finished < deadline
                    and not (cancel is not None and cancel.is_set())):
                self.entry = (key, as_of, deadline, dict(inventory))
            else:
                self.entry = None
            return inventory
        finally:
            self.lock.release()

    def close(self):
        with self.lock:
            self.entry = None


CENSUS = InventoryCensus()
