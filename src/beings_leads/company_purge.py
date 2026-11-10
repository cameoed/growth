"""Preview and delete explicitly selected company-owned research."""

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .domain_policy import delete_owned_rows, expand_owned_rows
from .sales_engine.evidence_accounting import LEGACY, PACKETS
from .sales_engine.ranking import policy_version
from .storage_json import usage_snapshot
from .storage_maintenance import database_file_sizes, reclaim_free_pages
from .store import LeadStore

# Curated endings kept by the domain filter.
COMMON_ENDINGS = (
    "ai", "app", "at", "au", "be", "biz", "br", "ca", "ch", "cn", "co", "com", "de", "dk",
    "edu", "es", "eu", "fi", "fr", "gov", "hk", "ie", "in", "info", "io", "it", "jp", "kr",
    "me", "mil", "mx", "net", "nl", "no", "nz", "org", "pl", "pt", "se", "sg", "tech", "tw",
    "uk", "us", "vc", "za",
)
_TOKEN_KEY = secrets.token_bytes(32)
_TOKEN_SECONDS = 300
_PAYLOAD_BATCH_SIZE = 4096
# Shared run history, admission controls and financial/usage records have independent owners.
_SHARED_RECORDS = {"buyer_collection_receipts", "treg_call_recovery"}
_HOST_OWNERS = {"free_signal_cache": "host", "ai_review_receipts": "hostname"}
_RESEARCH_PAYLOAD_FIELDS = {
    "candidates": ("discovery_evidence_json", "provider_record_ids_json",
                   "discovery_assertions_json"),
    "snapshots": ("observation_json", "score_json", "ai_json"),
    "evidence_artifacts": ("excerpt", "metadata_json"),
    "atomic_observations": ("value_json", "qualifiers_json", "summary", "metadata_json"),
    "intelligence_feature_snapshots": ("features_json", "missingness_json", "coverage_json",
                                       "scanner_states_json", "evidence_ages_json",
                                       "observation_keys_json"),
    LEGACY: ("record_json",),
    PACKETS: ("records",),
    "engine_evidence_accounting": ("record_json",),
}


PurgeCriterion = Literal[
    "bottom_percent", "score_at_most", "other_endings", "unresearched",
    "repeated_missing_homepage", "repeated_homepage_server_errors",
]


class PurgeFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion: PurgeCriterion
    value: int = Field(default=10, ge=0, le=100, strict=True)


class PurgeCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion: PurgeCriterion | None = None
    value: int = Field(default=10, ge=0, le=100, strict=True)
    filters: list[PurgeFilter] | None = Field(
        default=None, min_length=1, max_length=len(get_args(PurgeCriterion)),
    )

    @model_validator(mode="after")
    def validate_filters(self):
        if "filters" not in self.model_fields_set and self.criterion is not None:
            return self
        if self.filters is None or {"criterion", "value"} & self.model_fields_set:
            raise ValueError("Provide one criterion or a list of filters.")
        if len({item.criterion for item in self.filters}) != len(self.filters):
            raise ValueError("Choose each purge filter only once.")
        return self

    def selected_filters(self) -> list[PurgeFilter]:
        if self.filters is not None:
            return self.filters
        return [PurgeFilter(criterion=self.criterion, value=self.value)]

    def job_criteria(self) -> dict:
        if self.filters is None:
            return {"criterion": self.criterion, "value": self.value}
        return {"filters": [item.model_dump() for item in self.filters]}


class PurgeConfirmation(PurgeCriteria):
    token: str = Field(max_length=100)
    confirmation: Literal["PURGE"]


class PurgeCancelled(Exception):
    pass


class PurgeCancellation:
    def __init__(self):
        self.requested = Event()
        self._lock = Lock()
        self._committing = False

    def request(self):
        with self._lock:
            if self._committing:
                return False
            self.requested.set()
            return True

    def begin_commit(self):
        with self._lock:
            if self.requested.is_set():
                raise PurgeCancelled
            self._committing = True


class ProtectedEndings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endings: str = Field(max_length=8192)


def _protected_endings(connection):
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_purge_preferences'"
    ).fetchone():
        row = connection.execute(
            "SELECT endings_json FROM company_purge_preferences WHERE id=1"
        ).fetchone()
        if row:
            return tuple(json.loads(row[0]))
    return COMMON_ENDINGS


def save_protected_endings(database: Path, request: ProtectedEndings):
    endings = set()
    for label in re.split(r"[,\s]+", request.endings.strip()):
        if not label:
            continue
        try:
            ending = label.removeprefix(".").lower().encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError("Enter domain endings like .com or .vc.") from None
        if not re.fullmatch(r"[a-z]{2,63}|xn--[a-z0-9][a-z0-9-]{0,57}[a-z0-9]", ending):
            raise ValueError("Enter domain endings like .com or .vc. Use only the last label.")
        endings.add(ending)
    if not endings:
        raise ValueError("Keep at least one protected domain ending.")
    kept = sorted(endings)
    with LeadStore(database, initialize=False) as store, store.connection as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""CREATE TABLE IF NOT EXISTS company_purge_preferences (
            id INTEGER PRIMARY KEY CHECK(id=1), endings_json TEXT NOT NULL
        )""")
        connection.execute(
            "INSERT INTO company_purge_preferences VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET endings_json=excluded.endings_json",
            (json.dumps(kept),),
        )
    return {"kept_endings": kept}


def _selection(connection, criteria, now, protected_endings):
    criterion = criteria.criterion
    ranked = criterion in {"bottom_percent", "score_at_most"}
    score = "r.score" if ranked else "NULL"
    join = """LEFT JOIN engine_company_ranking r ON r.company_key=i.company_key
      AND r.policy_version=? AND r.dirty=0 AND r.as_of<=? AND r.valid_until>?""" if ranked else ""
    sql = f"""WITH protected(company_key) AS (
        SELECT website_host FROM sales_accounts
        UNION SELECT company_key FROM outbound_people
        UNION SELECT m.company_key FROM engine_mission_members m WHERE
            m.candidate_id IN (
                SELECT candidate_id FROM sales_accounts
                UNION SELECT candidate_id FROM outbound_people
                UNION SELECT candidate_id FROM prospect_tags
                UNION SELECT candidate_id FROM prospect_workflow
                UNION SELECT candidate_id FROM prospect_activities
            )
    )
    SELECT i.company_key, i.company_name, i.input_version, {score} AS score
    FROM engine_mission_inventory i
    {join}
    WHERE i.candidate_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM protected p WHERE p.company_key=i.company_key)
    """
    stamp = now.isoformat()
    params = [policy_version(), stamp, stamp] if ranked else []
    if ranked:
        sql += " AND r.score IS NOT NULL"
    if criterion == "score_at_most":
        sql += " AND r.score<=?"
        params.append(criteria.value)
    if criterion == "unresearched":
        sql += """ AND NOT EXISTS (
            SELECT 1 FROM engine_mission_members m
            WHERE m.company_key=i.company_key AND m.snapshot_id IS NOT NULL
        )"""
    failure_tests = {
        "repeated_missing_homepage": "status IN (404, 410)",
        "repeated_homepage_server_errors": "status BETWEEN 500 AND 599",
    }
    if criterion in failure_tests:
        failure_test = failure_tests[criterion]
        # Status is in the existing observation projection. Decode only older
        # envelopes that may lack it.
        sql += f""" AND (
            SELECT SUM(matches) FROM (
                SELECT scan_day, MIN(CASE WHEN {failure_test} THEN 1 ELSE 0 END) AS matches
                FROM (
                    SELECT date(s.observed_at) AS scan_day,
                           CASE
                               WHEN json_type(s.observation_json, '$.status_code') IS NOT NULL
                                 OR json_type(s.observation_json, '$."$beings_storage"') IS NULL
                               THEN json_extract(s.observation_json, '$.status_code')
                               ELSE json_extract(storage_json(s.observation_json), '$.status_code')
                           END AS status
                    FROM engine_mission_members m
                    JOIN snapshots s ON s.candidate_id=m.candidate_id
                    WHERE m.company_key=i.company_key
                      AND julianday(s.observed_at) BETWEEN julianday(?) AND julianday(?)
                )
                GROUP BY scan_day ORDER BY scan_day DESC LIMIT 3
            )
        ) = 3"""
        params.extend([(now - timedelta(days=90)).isoformat(), stamp])
    order = "r.score, i.company_key" if ranked else "i.company_key"
    rows = connection.execute(sql + " ORDER BY " + order, params).fetchall()
    eligible = len(rows)
    if criterion == "other_endings":
        rows = [
            row for row in rows if row["company_key"].lower().rstrip(".").rsplit(".", 1)[-1]
            not in protected_endings
        ]
    if criterion == "bottom_percent":
        if not criteria.value:
            raise ValueError("Choose a percentage from 1 to 100.")
        rows = rows[:(eligible * criteria.value + 99) // 100]
    return rows, eligible


def _stored_payload_bytes(connection, keys, progress=None):
    if not keys:
        return 0
    tables = {
        row[1]: not row[4] for row in connection.execute("PRAGMA main.table_list")
        if row[2] == "table"
    }
    # Measure encoded values, including compressed BLOBs, without decoding them.
    # Newer SQLite reads direct octet lengths from record metadata.
    length = "octet_length({})" if sqlite3.sqlite_version_info >= (3, 43) else (
        "length(CAST({} AS BLOB))"
    )
    selected = "SELECT value FROM json_each(?)"
    owners = ("SELECT candidate_id FROM engine_mission_members "
              f"WHERE company_key IN ({selected})")
    predicates = {"candidates": f"id IN ({owners})", "snapshots": f"candidate_id IN ({owners})"}
    statements = []
    for table, fields in _RESEARCH_PAYLOAD_FIELDS.items():
        if table not in tables:
            continue
        size = " + ".join(f"COALESCE({length.format(column)}, 0)" for column in fields)
        where = predicates.get(table, f"company_key IN ({selected})")
        if tables[table] and table != "candidates":
            # Drive the payload lookup from sorted IDs. An IN predicate on the
            # outer table can make SQLite scan all stored research instead.
            statements.append(
                "WITH owned AS MATERIALIZED ("
                f"SELECT rowid AS id FROM {table} WHERE {where} ORDER BY rowid) "
                f"SELECT COALESCE(SUM({size}), 0) FROM owned "
                f"CROSS JOIN {table} AS payload ON payload.rowid=owned.id"
            )
        else:
            statements.append(f"SELECT COALESCE(SUM({size}), 0) FROM {table} WHERE {where}")
    batches = range(0, len(keys), _PAYLOAD_BATCH_SIZE)
    total = len(statements) * len(batches)
    processed = payload_bytes = 0
    if progress is not None and total:
        progress("storage", 0, total)
    for start in batches:
        batch = json.dumps(keys[start:start + _PAYLOAD_BATCH_SIZE])
        for statement in statements:
            payload_bytes += connection.execute(statement, (batch,)).fetchone()[0]
            processed += 1
            if progress is not None:
                progress("storage", processed, total)
    return payload_bytes


def _preview(connection, criteria, now, database, *, progress=None):
    filters = sorted(criteria.selected_filters(), key=lambda item: item.criterion)
    endings = (_protected_endings(connection)
               if any(item.criterion == "other_endings" for item in filters) else ())
    kept_endings = frozenset(endings)
    digest = hashlib.sha256()
    digest.update(json.dumps([str(database), database.stat().st_ino, endings]).encode())
    selected = {}
    filter_counts = []
    for item in filters:
        matches, eligible = _selection(connection, item, now, kept_endings)
        count = {**item.model_dump(), "matched": len(matches), "eligible": eligible}
        filter_counts.append(count)
        digest.update(json.dumps(count).encode())
        for row in matches:
            digest.update(json.dumps(tuple(row)).encode())
            key = row["company_key"]
            if key not in selected or row["score"] is not None:
                selected[key] = row
    rows = list(selected.values())
    if len(filters) > 1:
        rows.sort(key=lambda row: (row["score"] is None, row["score"] or 0, row["company_key"]))
    keys = [row["company_key"] for row in rows]
    candidates = snapshots = 0
    if progress is not None and keys:
        progress("records", 0, len(keys))
    for start in range(0, len(keys), 500):
        batch = keys[start:start + 500]
        placeholders = ",".join("?" for _ in batch)
        members = connection.execute(
            "SELECT m.candidate_id,m.snapshot_id,m.revision,"
            "(SELECT COUNT(*) FROM snapshots s WHERE s.candidate_id=m.candidate_id) "
            f"FROM engine_mission_members m WHERE m.company_key IN ({placeholders}) "
            "ORDER BY m.candidate_id", batch,
        )
        for member in members:
            candidates += 1
            snapshots += member[3]
            digest.update(json.dumps(tuple(member)).encode())
        if progress is not None:
            progress("records", start + len(batch), len(keys))
    return keys, digest.hexdigest(), {
        "companies": len(rows), "eligible": eligible if len(filters) == 1 else len(rows),
        "candidates": candidates,
        "snapshots": snapshots,
        "examples": [{"name": row["company_name"], "domain": row["company_key"],
                      "score": row["score"]} for row in rows[:8]],
        **({"filter_counts": filter_counts} if criteria.filters is not None else {}),
    }


def _token(selection, deadline):
    signature = hmac.digest(_TOKEN_KEY, f"{deadline}:{selection}".encode(), "sha256").hex()
    return f"{deadline}.{signature}"


def purge_settings(database: Path):
    storage = {"page_bytes": 0, "free_page_bytes": 0, "live_page_bytes": 0}
    kept = COMMON_ENDINGS
    reclaim_status = "unavailable"
    reclaim_available = False
    if database.exists():
        with LeadStore(database, initialize=False, read_only=True) as store:
            storage = _purge_storage_snapshot(store.connection, database)
            kept = _protected_endings(store.connection)
            mode = store.connection.execute("PRAGMA auto_vacuum").fetchone()[0]
            reclaim_status = "ready" if mode else "needs_migration"
            reclaim_available = bool(mode and _pending_disk_reclaim(storage))
    return {"storage": storage, "kept_endings": kept, "default_endings": COMMON_ENDINGS,
            "reclaim_status": reclaim_status, "reclaim_available": reclaim_available}


def _purge_storage_snapshot(connection, database):
    storage = usage_snapshot(connection)
    file_bytes, disk_bytes = database_file_sizes(database)
    return {**storage, "file_bytes": file_bytes, "disk_bytes": disk_bytes}


def _pending_disk_reclaim(storage):
    # The logical page count can shrink before a pinned reader lets WAL truncate the file.
    return storage["free_page_bytes"] > 0 or storage["file_bytes"] > storage["page_bytes"]


def reclaim_purge_storage(database: Path, *, progress=None):
    if not database.is_file():
        raise ValueError("No company database is available.")
    with LeadStore(database, initialize=False) as store:
        result = reclaim_free_pages(store.connection, database, progress=progress)
        storage = _purge_storage_snapshot(store.connection, database)
        return {**result, "storage": storage, "reclaim_available": (
            result["reclaim_status"] != "needs_migration"
            and _pending_disk_reclaim(storage)
        )}


def preview_purge(
    database: Path, criteria: PurgeCriteria, *, progress=None, matches=None, cancelled=None,
):
    if not database.exists():
        return {"companies": 0, "eligible": 0, "candidates": 0, "snapshots": 0,
                "payload_bytes": 0, "examples": [], "token": None,
                **({"filter_counts": [
                    {**item.model_dump(), "matched": 0, "eligible": 0} for item in criteria.filters
                ]} if criteria.filters is not None else {})}
    with LeadStore(database, initialize=False, read_only=True) as store:
        connection = store.connection
        if cancelled is not None:
            connection.set_progress_handler(cancelled, 1000)
        connection.execute("BEGIN")
        keys, selection, summary = _preview(
            connection, criteria, datetime.now(UTC), database, progress=progress,
        )
        if matches is not None:
            matches(summary)
        payload_bytes = _stored_payload_bytes(connection, keys, progress)
    token = _token(selection, int(time.time()) + _TOKEN_SECONDS) if keys else None
    return {**summary, "payload_bytes": payload_bytes, "token": token}


def _delete_companies(connection, keys, progress):
    connection.execute("PRAGMA defer_foreign_keys=ON")
    connection.execute("CREATE TEMP TABLE purge_companies (key TEXT PRIMARY KEY)")
    connection.executemany("INSERT INTO purge_companies VALUES (?)", ((key,) for key in keys))
    connection.execute(
        "CREATE TEMP TABLE denied_rows (table_name TEXT, row_id INTEGER,"
        "PRIMARY KEY(table_name,row_id))"
    )
    connection.execute(
        """INSERT INTO denied_rows SELECT 'candidates',candidate_id
        FROM engine_mission_members WHERE company_key IN (SELECT key FROM purge_companies)"""
    )
    tables = [
        row[1] for row in connection.execute("PRAGMA table_list")
        if row[0] == "main" and row[2] == "table" and not row[4]
        and not row[1].startswith("sqlite_") and row[1] not in _SHARED_RECORDS
    ]
    # Remove dependent rows before the candidate deletion triggers settle projections.
    tables.sort(key=lambda name: name == "candidates")
    columns = {
        table: {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        for table in tables
    }
    for table in tables:
        owner = _HOST_OWNERS.get(table, "company_key")
        if owner in columns[table]:
            connection.execute(
                f'INSERT OR IGNORE INTO denied_rows SELECT ?,rowid FROM "{table}" '
                f'WHERE "{owner}" IN (SELECT key FROM purge_companies)', (table,),
            )
    expand_owned_rows(connection, tables, columns)
    delete_owned_rows(connection, tables, preserve_invalidation=True, progress=progress)
    if connection.execute(
        "SELECT 1 FROM engine_mission_inventory "
        "WHERE company_key IN (SELECT key FROM purge_companies)"
    ).fetchone():
        raise ValueError("A selected company still has active records. Preview again.")


def confirm_purge(database: Path, request: PurgeConfirmation, *, progress=None, cancellation=None):
    if cancellation is None:
        cancellation = PurgeCancellation()
    if cancellation.requested.is_set():
        raise PurgeCancelled
    message = "The preview expired or the selection changed. Preview the purge again."
    try:
        deadline = int(request.token.split(".", 1)[0])
    except ValueError:
        raise ValueError(message) from None
    if deadline <= time.time() or not database.exists():
        raise ValueError(message)
    with LeadStore(database, initialize=False) as store:
        connection = store.connection
        connection.execute("PRAGMA secure_delete=ON")
        with connection:
            connection.set_progress_handler(cancellation.requested.is_set, 1000)
            try:
                connection.execute("BEGIN IMMEDIATE")
                keys, selection, summary = _preview(
                    connection, request, datetime.now(UTC), database,
                )
                if not keys or deadline <= time.time() or not hmac.compare_digest(
                    request.token, _token(selection, deadline)
                ):
                    raise ValueError(message)
                before = usage_snapshot(connection)["free_page_bytes"]
                _delete_companies(connection, keys, progress)
                storage = usage_snapshot(connection)
                cancellation.begin_commit()
            except sqlite3.OperationalError as exc:
                if (
                    exc.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
                    and cancellation.requested.is_set()
                ):
                    raise PurgeCancelled from exc
                raise
            finally:
                # Leave the handler off for the context manager's rollback or commit.
                connection.set_progress_handler(None, 0)
        return {"companies": summary["companies"], "storage": storage,
                "reusable_bytes": max(0, storage["free_page_bytes"] - before)}
