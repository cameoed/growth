"""Exact transactional mission listing/search state. Source observations remain authoritative."""

import sqlite3
from datetime import datetime

from ..sales_engine.persistence import SalesEngineRepository
from .spec import VERSION as POLICY

VERSION = "mission-read-index-v1:" + POLICY
COLUMNS = (
    "mission_id,company_key,revision,input_version,policy_version,score,as_of,valid_until,dirty"
)


def ready(connection):
    try:
        row = connection.execute(
            "SELECT version FROM engine_mission_read_state WHERE id=1"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return False
    return bool(row and row[0] == VERSION)


def _listing_select(predicate="1"):
    return (
        "SELECT s.mission_id,s.company_key,s.revision,s.input_version,s.policy_version,"
        "s.score,s.as_of,s.valid_until,s.input_version<>i.input_version "
        "FROM engine_mission_scores s JOIN engine_mission_inventory i "
        "ON i.company_key=s.company_key WHERE " + predicate
    )


def _search_insert(key):
    return (
        "INSERT INTO engine_mission_search(rowid,company_key,company_name,aliases) "
        "SELECT i.rowid,i.company_key,i.company_name,COALESCE((SELECT group_concat(c.hostname,' ') "
        "FROM engine_mission_members m JOIN candidates c ON c.id=m.candidate_id "
        "WHERE m.company_key=i.company_key),'') "
        "FROM engine_mission_inventory i WHERE i.company_key=" + key + ";"
    )


def initialize(connection):
    if ready(connection):
        _initialize_discovery_order(connection)
        return
    with SalesEngineRepository(connection)._transaction():
        connection.execute("""CREATE TABLE IF NOT EXISTS engine_mission_read_state(
            id INTEGER PRIMARY KEY CHECK(id=1),version TEXT NOT NULL,generation INTEGER NOT NULL,
            search_enabled INTEGER NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS engine_mission_listing(
            mission_id TEXT NOT NULL REFERENCES engine_missions(id) ON DELETE CASCADE,
            company_key TEXT NOT NULL
                REFERENCES engine_mission_inventory(company_key) ON DELETE CASCADE,
            revision INTEGER NOT NULL,input_version TEXT NOT NULL,policy_version TEXT NOT NULL,
            score INTEGER,as_of TEXT NOT NULL,valid_until TEXT NOT NULL,dirty INTEGER NOT NULL,
            PRIMARY KEY(mission_id,company_key))""")
        for name, fields in (
            ("order", "mission_id,score DESC,company_key"),
            ("coverage", "mission_id,revision,policy_version,dirty,valid_until,as_of"),
            ("expiry", "mission_id,valid_until"),
            ("future", "mission_id,as_of"),
            ("company", "company_key,mission_id"),
        ):
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_mission_listing_{name} "
                f"ON engine_mission_listing({fields})"
            )
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name GLOB 'mission_read_*'"
        ).fetchall():
            connection.execute(f'DROP TRIGGER "{row[0]}"')
        connection.execute("DELETE FROM engine_mission_listing")
        connection.execute(f"INSERT INTO engine_mission_listing({COLUMNS}) " + _listing_select())
        search = any(
            "ENABLE_FTS5" in row[0] for row in connection.execute("PRAGMA compile_options")
        )
        if search:
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS engine_mission_search USING "
                "fts5(company_key,company_name,aliases,tokenize='trigram',detail=none)"
            )
            connection.execute("DELETE FROM engine_mission_search")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_mission_members_search "
                "ON engine_mission_members(company_key,candidate_id)"
            )
            connection.execute(
                "INSERT INTO engine_mission_search(rowid,company_key,company_name,aliases) "
                "SELECT i.rowid,i.company_key,i.company_name,COALESCE((SELECT "
                "group_concat(c.hostname,' ') FROM engine_mission_members m "
                "JOIN candidates c ON c.id=m.candidate_id "
                "WHERE m.company_key=i.company_key),'') FROM engine_mission_inventory i"
            )
        for action in ("INSERT", "UPDATE", "DELETE"):
            refs = (
                ("NEW",)
                if action == "INSERT"
                else ("OLD",)
                if action == "DELETE"
                else ("OLD", "NEW")
            )
            body = ""
            for ref in refs:
                condition = f"mission_id={ref}.mission_id AND company_key={ref}.company_key"
                body += f"DELETE FROM engine_mission_listing WHERE {condition};"
                body += (
                    f"INSERT INTO engine_mission_listing({COLUMNS}) "
                    + _listing_select(
                        f"s.mission_id={ref}.mission_id AND s.company_key={ref}.company_key"
                    )
                    + ";"
                )
            _trigger(connection, "engine_mission_scores", action, "listing", body)
            body = ""
            if action == "INSERT":
                body += (
                    f"INSERT INTO engine_mission_listing({COLUMNS}) "
                    + _listing_select("s.company_key=NEW.company_key")
                    + ";"
                )
            if action == "UPDATE":
                body += (
                    "UPDATE engine_mission_listing SET dirty=(input_version<>NEW.input_version) "
                    "WHERE company_key=NEW.company_key;"
                )
            if search:
                for ref in refs:
                    body += f"DELETE FROM engine_mission_search WHERE rowid={ref}.rowid;"
                    body += _search_insert(f"{ref}.company_key")
            if body:
                _trigger(connection, "engine_mission_inventory", action, "inventory", body)
            if search:
                body = ""
                for ref in refs:
                    key = f"{ref}.company_key"
                    body += f"DELETE FROM engine_mission_search WHERE company_key={key};"
                    body += _search_insert(key)
                _trigger(connection, "engine_mission_members", action, "aliases", body)
        for table in (
            "engine_missions",
            "engine_mission_scores",
            "engine_mission_inventory",
            "engine_mission_members",
            "sales_accounts",
        ):
            for action in ("INSERT", "UPDATE", "DELETE"):
                _trigger(
                    connection,
                    table,
                    action,
                    "generation",
                    "UPDATE engine_mission_read_state SET generation=generation+1 WHERE id=1;",
                )
        connection.execute(
            "INSERT INTO engine_mission_read_state VALUES(1,?,1,?) "
            "ON CONFLICT(id) DO UPDATE SET version=excluded.version,"
            "generation=generation+1,search_enabled=excluded.search_enabled",
            (VERSION, int(search)),
        )
    _initialize_discovery_order(connection)


def discovery_order_ready(connection):
    return connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='engine_mission_discovery_order'"
    ).fetchone() is not None


def _initialize_discovery_order(connection):
    if discovery_order_ready(connection):
        return
    with SalesEngineRepository(connection)._transaction():
        if discovery_order_ready(connection):
            return
        connection.execute("""CREATE TABLE engine_mission_discovery_order(
            company_key TEXT PRIMARY KEY,first_discovered_at TEXT NOT NULL)""")
        connection.execute("""CREATE INDEX idx_mission_discovery_order
            ON engine_mission_discovery_order(first_discovered_at DESC,company_key)""")
        select = (
            "INSERT INTO engine_mission_discovery_order "
            "SELECT m.company_key,MIN(c.discovered_at) FROM engine_mission_members m "
            "JOIN candidates c ON c.id=m.candidate_id "
        )
        connection.execute(select + "GROUP BY m.company_key")

        def refresh(key):
            return (
                "DELETE FROM engine_mission_discovery_order WHERE company_key=" + key + "; "
                + select + "WHERE m.company_key=" + key + " GROUP BY m.company_key;"
            )

        old = refresh("OLD.company_key")
        new = refresh("NEW.company_key")
        for action, body in (
            ("INSERT", new),
            ("DELETE", old),
            ("UPDATE OF company_key,candidate_id", old + new),
        ):
            condition = (
                " WHEN NEW.company_key IS NOT OLD.company_key "
                "OR NEW.candidate_id IS NOT OLD.candidate_id" if action.startswith("UPDATE") else ""
            )
            operation = action.split()[0].lower()
            connection.execute(
                f"CREATE TRIGGER mission_discovery_member_{operation} AFTER {action} "
                f"ON engine_mission_members{condition} BEGIN {body} END"
            )
        key = "(SELECT company_key FROM engine_mission_members WHERE candidate_id=NEW.id)"
        connection.execute(
            "CREATE TRIGGER mission_discovery_candidate_update AFTER UPDATE OF discovered_at "
            "ON candidates WHEN NEW.discovered_at IS NOT OLD.discovered_at "
            f"BEGIN {refresh(key)} END"
        )


def _trigger(connection, table, action, kind, body):
    connection.execute(
        f"CREATE TRIGGER mission_read_{kind}_{table}_{action.lower()} "
        f"AFTER {action} ON {table} BEGIN {body} END"
    )


def generation(connection):
    return connection.execute(
        "SELECT generation FROM engine_mission_read_state WHERE id=1"
    ).fetchone()[0]


def search_filter(connection, term, *, optimized=True):
    if not term:
        return "1", []
    pattern = f"%{term}%"
    matcher = (
        "(i.company_name LIKE ? OR i.company_key LIKE ? OR EXISTS "
        "(SELECT 1 FROM engine_mission_members m JOIN candidates c ON c.id=m.candidate_id "
        "WHERE m.company_key=i.company_key AND c.hostname LIKE ?))"
    )
    args = [pattern] * 3
    import re

    if (
        optimized
        and ready(connection)
        and "\x00" not in term
        and any(len(part) >= 3 for part in re.split(r"[%_]", term))
        and connection.execute(
            "SELECT search_enabled FROM engine_mission_read_state WHERE id=1"
        ).fetchone()[0]
    ):
        matcher = (
            "i.company_key IN (SELECT company_key FROM engine_mission_search "
            "WHERE company_key LIKE ? "
            "UNION SELECT company_key FROM engine_mission_search WHERE company_name LIKE ? "
            "UNION SELECT company_key FROM engine_mission_search WHERE aliases LIKE ?) AND "
            + matcher
        )
        args = [pattern] * 3 + args
    return matcher, args


def deadline(connection, mission_id, now, default, *, table="engine_mission_listing"):
    if table not in {"engine_mission_listing", "engine_mission_scores"}:
        raise ValueError("Invalid mission deadline source")
    stamp = now.isoformat()
    row = connection.execute(
        "SELECT "
        f"(SELECT MIN(valid_until) FROM {table} WHERE mission_id=? AND valid_until>?),"
        f"(SELECT MIN(as_of) FROM {table} WHERE mission_id=? AND as_of>?)",
        (mission_id, stamp, mission_id, stamp),
    ).fetchone()
    try:
        return min([default, *(datetime.fromisoformat(value) for value in row if value)])
    except (ValueError, TypeError):
        return now
