"""Exact, rebuildable loading indexes; source/scoring tables remain authoritative.

Install/update/seed in one savepoint. Derived rows follow every mutation and rollback.
Search uses indexed LIKE, never token relevance or a new matching contract.
"""

from __future__ import annotations

import re

VERSION = "exact-read-indexes-v2"
COLUMNS = (
    "company_key,policy_version,dirty,eligible,valid_until,as_of,lens_ready,score,"
    "demand_score,timing_score,access_score"
)


def expected_version():
    from .ranking_modes import VERSION as lens_version

    return f"{VERSION}:{lens_version}"


def ready(connection):
    try:
        row = connection.execute(
            "SELECT version FROM engine_read_index_state WHERE id=1"
        ).fetchone()
        return bool(row and row[0] == expected_version())
    except Exception as exc:
        import sqlite3

        if not isinstance(exc, sqlite3.OperationalError):
            raise
        return False


def _projection_select(key=None):
    from .ranking_modes import match_sql

    return (
        "SELECT r.company_key,r.policy_version,r.dirty,r.eligible,r.valid_until,r.as_of,"
        "l.company_key IS NOT NULL,r.score,l.demand_score,l.timing_score,l.access_score "
        "FROM engine_company_ranking r LEFT JOIN engine_company_lenses l ON "
        + match_sql()
        + (f" WHERE r.company_key={key}" if key else "")
    )


def initialize(connection):
    if ready(connection):
        return
    connection.execute("SAVEPOINT exact_read_indexes")
    try:
        connection.execute("""CREATE TABLE IF NOT EXISTS engine_read_index_state(
            id INTEGER PRIMARY KEY CHECK(id=1), version TEXT NOT NULL, generation INTEGER NOT NULL,
            search_enabled INTEGER NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS engine_company_listing(
            company_key TEXT PRIMARY KEY REFERENCES engine_company_ranking(company_key)
                ON DELETE CASCADE,
            policy_version TEXT NOT NULL,dirty INTEGER NOT NULL,eligible INTEGER NOT NULL,
            valid_until TEXT NOT NULL,as_of TEXT NOT NULL,lens_ready INTEGER NOT NULL,
            score INTEGER,demand_score INTEGER,timing_score INTEGER,access_score INTEGER)""")
        for column in ("score", "demand_score", "timing_score", "access_score"):
            lens = "lens_ready," if column != "score" else ""
            connection.execute(f"DROP INDEX IF EXISTS idx_engine_listing_{column}")
            connection.execute(
                f"CREATE INDEX idx_engine_listing_{column} ON "
                f"engine_company_listing(policy_version,dirty,{lens}"
                f"{column} DESC,company_key,valid_until,as_of)"
            )
        connection.execute("""CREATE INDEX IF NOT EXISTS idx_engine_listing_gaps ON
            engine_company_listing(policy_version,dirty,lens_ready,valid_until,as_of)""")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_engine_ranking_future ON "
            "engine_company_ranking(dirty,as_of)"
        )
        connection.execute("""CREATE INDEX IF NOT EXISTS idx_engine_people_receipt ON
            scanner_executions(company_key,scanner_id,scanner_version,status,
                COALESCE(finished_at,started_at) DESC,id DESC)""")
        connection.execute("DELETE FROM engine_company_listing")
        connection.execute(f"INSERT INTO engine_company_listing({COLUMNS}) " + _projection_select())
        search = any(
            "ENABLE_FTS5" in row[0] for row in connection.execute("PRAGMA compile_options")
        )
        if search:
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS engine_company_search USING "
                "fts5(company_key,company_name,tokenize='trigram',detail=none)"
            )
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS engine_candidate_search USING "
                "fts5(hostname,label_hint,company_key,tokenize='trigram',detail=none)"
            )
            connection.execute("DELETE FROM engine_company_search")
            connection.execute(
                "INSERT INTO engine_company_search(rowid,company_key,company_name) "
                "SELECT rowid,company_key,company_name FROM engine_company_ranking"
            )
            connection.execute("DELETE FROM engine_candidate_search")
            connection.execute(
                "INSERT INTO engine_candidate_search"
                "(rowid,hostname,label_hint,company_key) "
                "SELECT c.id,c.hostname,c.label_hint,m.company_key FROM candidates c "
                "LEFT JOIN engine_ranking_members m ON m.candidate_id=c.id"
            )
        # Old versions' triggers must not survive an additive upgrade.
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name GLOB 'engine_read_*'"
        ).fetchall():
            connection.execute(f'DROP TRIGGER "{row[0]}"')
        for table in ("engine_company_ranking", "engine_company_lenses"):
            for action in ("INSERT", "UPDATE", "DELETE"):
                refs = (
                    ("NEW",)
                    if action == "INSERT"
                    else (("OLD",) if action == "DELETE" else ("OLD", "NEW"))
                )
                body = []
                for ref in refs:
                    key = f"{ref}.company_key"
                    body += [
                        f"DELETE FROM engine_company_listing WHERE company_key={key};",
                        f"INSERT INTO engine_company_listing({COLUMNS}) {_projection_select(key)};",
                    ]
                if search and table == "engine_company_ranking":
                    if action != "INSERT":
                        body.append("DELETE FROM engine_company_search WHERE rowid=OLD.rowid;")
                    if action != "DELETE":
                        body.append(
                            "INSERT INTO engine_company_search(rowid,company_key,company_name) "
                            "VALUES(NEW.rowid,NEW.company_key,NEW.company_name);"
                        )
                _trigger(connection, table, action, "projection", " ".join(body))
        if search:
            for table, field in (("candidates", "id"), ("engine_ranking_members", "candidate_id")):
                for action in ("INSERT", "UPDATE", "DELETE"):
                    refs = (
                        ("NEW",)
                        if action == "INSERT"
                        else (("OLD",) if action == "DELETE" else ("OLD", "NEW"))
                    )
                    body = []
                    for ref in refs:
                        key = f"{ref}.{field}"
                        body.append(f"DELETE FROM engine_candidate_search WHERE rowid={key};")
                        body.append(
                            "INSERT INTO engine_candidate_search"
                            "(rowid,hostname,label_hint,company_key) "
                            "SELECT c.id,c.hostname,c.label_hint,m.company_key FROM candidates c "
                            "LEFT JOIN engine_ranking_members m ON m.candidate_id=c.id "
                            f"WHERE c.id={key};"
                        )
                    _trigger(connection, table, action, "search", " ".join(body))
        for table in (
            "engine_company_ranking",
            "engine_company_lenses",
            "engine_ranking_members",
            "engine_ranking_pending",
            "engine_ranking_backfill",
            "candidates",
            "sales_accounts",
        ):
            for action in ("INSERT", "UPDATE", "DELETE"):
                _trigger(
                    connection,
                    table,
                    action,
                    "generation",
                    "UPDATE engine_read_index_state SET generation=generation+1 WHERE id=1;",
                )
        connection.execute(
            "INSERT INTO engine_read_index_state VALUES(1,?,1,?) "
            "ON CONFLICT(id) DO UPDATE SET version=excluded.version,"
            "generation=generation+1,search_enabled=excluded.search_enabled",
            (expected_version(), int(search)),
        )
        connection.execute("RELEASE exact_read_indexes")
    except BaseException:
        connection.execute("ROLLBACK TO exact_read_indexes")
        connection.execute("RELEASE exact_read_indexes")
        raise


def _trigger(connection, table, action, kind, body):
    connection.execute(
        f"CREATE TRIGGER engine_read_{kind}_{table}_{action.lower()} "
        f"AFTER {action} ON {table} BEGIN {body} END"
    )


def indexed_search(connection, term):
    # Short/wildcard-only queries keep the original exhaustive LIKE semantics.
    if "\x00" in term or not any(len(part) >= 3 for part in re.split(r"[%_]", term)):
        return False
    row = connection.execute(
        "SELECT search_enabled FROM engine_read_index_state WHERE id=1"
    ).fetchone()
    return bool(row and row[0])


def ranking_search_sql():
    return (
        "SELECT company_key FROM engine_company_search WHERE company_key LIKE ? "
        "UNION SELECT company_key FROM engine_company_search WHERE company_name LIKE ? "
        "UNION SELECT m.company_key FROM engine_candidate_search f "
        "CROSS JOIN engine_ranking_members m ON m.candidate_id=f.rowid WHERE f.hostname LIKE ?"
    )


def pending_search_sql():
    return (
        "SELECT rowid FROM engine_candidate_search WHERE hostname LIKE ? "
        "UNION SELECT rowid FROM engine_candidate_search WHERE label_hint LIKE ? "
        "UNION SELECT rowid FROM engine_candidate_search WHERE company_key LIKE ?"
    )
