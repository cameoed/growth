"""Cheap domain admission policy and explicit transactional inventory removal."""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from .file_lock import directory_lock

logger = logging.getLogger(__name__)

DOMAIN_POLICY_REVISION = "owned-residual-v2"

POLICY_PATH = Path(__file__).resolve().parents[2] / "config/disallowed_domains.txt"
_POLICY_SCOPE = ContextVar("domain_policy_path", default=None)
_POLICY_RULES = ContextVar("domain_policy_rules", default=None)


def policy_path():
    return _POLICY_SCOPE.get() or POLICY_PATH


@contextmanager
def policy_scope(path):
    """Carry a configured path to a writer without freezing the file's live rules."""
    token = _POLICY_SCOPE.set(Path(path))
    try:
        yield
    finally:
        _POLICY_SCOPE.reset(token)


@contextmanager
def policy_rules(domains):
    """Use the authoritative database owner's policy for one remote read snapshot."""
    token = _POLICY_RULES.set(tuple(domains))
    try:
        yield
    finally:
        _POLICY_RULES.reset(token)


def normalize_domain(value: str) -> str:
    raw = str(value or "").strip()
    try:
        host = urlsplit(raw if "://" in raw else f"https://{raw}").hostname or ""
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError):
        return ""
    if (
        len(host) > 253
        or not host
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
            for part in host.split(".")
        )
    ):
        return ""
    return host


@lru_cache(maxsize=4)
def _read_policy(path: str, mtime: int, size: int) -> tuple[str, ...]:
    lines = Path(path).read_text().splitlines() if mtime else []
    domains = {"company.site"}
    for line in lines:
        raw = line.split("#", 1)[0].strip()
        if raw:
            domain = normalize_domain(raw)
            if not domain or "." not in domain or any(not part for part in domain.split(".")):
                raise ValueError(f"Invalid disallowed domain: {raw}")
            domains.add(domain)
    return tuple(sorted(domains))


def disallowed_domains() -> tuple[str, ...]:
    if (rules := _POLICY_RULES.get()) is not None:
        return rules
    path = policy_path()
    try:
        stat = path.stat()
        return _read_policy(str(path), stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        return ("company.site",)


def add_disallowed_domains(values) -> tuple[str, ...]:
    """Merge policy additions atomically under a process-wide directory lock."""
    additions = {normalize_domain(value) for value in values}
    additions = {value for value in additions if value and "." in value}
    if not additions:
        return disallowed_domains()
    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with directory_lock(path.parent):
        domains = tuple(sorted({*disallowed_domains(), *additions}))
        if domains != disallowed_domains():
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                "# Domains and all subdomains; additions purge company data.\n"
                + "\n".join(domains) + "\n"
            )
            temporary.replace(path)
        return domains


def is_disallowed(value: str) -> bool:
    host = normalize_domain(value)
    if not host:
        return False
    rules = _domain_set(disallowed_domains())
    while host:
        if host in rules:
            return True
        host = host.partition(".")[2]
    return False


@lru_cache(maxsize=4)
def _domain_set(domains: tuple[str, ...]) -> frozenset[str]:
    return frozenset(domains)


def require_allowed(value: str) -> None:
    if is_disallowed(value):
        raise ValueError(f"Domain is disallowed: {normalize_domain(value)}")


def expand_owned_rows(connection, tables, columns):
    # Expand ownership through both declared FKs and legacy logical identity links.
    children = {}
    for table in tables:
        edges = {
            (row[2], row[3], row[4])
            for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
        }
        for column, parent in (("candidate_id", "candidates"), ("account_id", "sales_accounts")):
            if column in columns[table]:
                edges.add((parent, column, "id"))
        for parent, source, dest in edges:
            if parent in columns:
                children.setdefault(parent, []).append((table, source, dest))

    changed = {row[0] for row in connection.execute("SELECT DISTINCT table_name FROM denied_rows")}
    primary_keys = {}
    while changed:
        next_changed = set()
        for parent in changed:
            for table, source, dest in children.get(parent, ()):
                if not dest:
                    if parent not in primary_keys:
                        primary_keys[parent] = next(
                            row[1]
                            for row in connection.execute(f'PRAGMA table_info("{parent}")')
                            if row[5]
                        )
                    dest = primary_keys[parent]
                inserted = connection.execute(
                    f'''INSERT OR IGNORE INTO denied_rows
                    SELECT ?, child.rowid FROM "{table}" child
                    WHERE child."{source}" IN (
                        SELECT parent."{dest}" FROM "{parent}" parent JOIN denied_rows d
                        ON d.table_name=? AND d.row_id=parent.rowid
                    )''',
                    (table, parent),
                )
                if inserted.rowcount:
                    next_changed.add(table)
        changed = next_changed


def delete_owned_rows(connection, tables, *, preserve_invalidation=False, progress=None):
    counts = dict(
        connection.execute("SELECT table_name, COUNT(*) FROM denied_rows GROUP BY table_name")
    )
    logger.info("Domain purge ownership closure: %s", counts)
    # SQLite checks child FKs for each deleted parent. Temporary supporting
    # indexes prevent an inventory-wide child scan for every blocked company.
    purge_indexes = []
    for table in tables:
        for fk in connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
            parent, source = fk[2], fk[3]
            if parent not in counts:
                continue
            indexed = any(
                connection.execute(f'PRAGMA index_info("{index[1]}")').fetchone()[2] == source
                for index in connection.execute(f'PRAGMA index_list("{table}")').fetchall()
            )
            primary = any(
                row[1] == source and row[5] and row[2] == "INTEGER"
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            )
            if not indexed and not primary:
                name = f"domain_purge_{table}_{source}"
                connection.execute(f'CREATE INDEX "{name}" ON "{table}"("{source}")')
                purge_indexes.append(name)
    # Normal collection keeps immutable receipts append-only. An explicitly
    # requested domain purge is the controlled exception, within this writer
    # transaction. Restore every original trigger before committing.
    triggers = [
        tuple(row)
        for row in connection.execute(
            "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'"
        )
        if row[1] in counts and (not preserve_invalidation or "RAISE(" in row[2].upper())
    ]
    for name, _, _ in triggers:
        connection.execute(f'DROP TRIGGER "{name}"')
    processed, total = 0, sum(counts.values())
    if progress is not None:
        progress(processed, total)
    for table in tables:
        if table in counts:
            delete = f'DELETE FROM "{table}" WHERE rowid IN '
            selection = "SELECT row_id FROM denied_rows WHERE table_name=?"
            if progress is None or counts[table] <= 2048:
                connection.execute(f"{delete}({selection})", (table,))
                processed += counts[table]
                if progress is not None:
                    progress(processed, total)
            else:
                last = None
                while True:
                    bounded = selection + (" AND row_id>?" if last is not None else "")
                    args = (table, last) if last is not None else (table,)
                    batch = connection.execute(
                        bounded + " ORDER BY row_id LIMIT 2048", args,
                    ).fetchall()
                    if not batch:
                        break
                    last = batch[-1][0]
                    connection.execute(f"{delete}({bounded} AND row_id<=?)", (*args, last))
                    # A previously cascaded row still counts as a processed target.
                    processed += len(batch)
                    progress(processed, total)
            logger.info("Domain purge removed %s rows from %s", counts[table], table)
    for _, _, sql in triggers:
        connection.execute(sql)
    for name in purge_indexes:
        connection.execute(f'DROP INDEX "{name}"')
    if counts and "engine_read_index_state" in tables:
        connection.execute(
            "UPDATE engine_read_index_state SET generation=generation+1 WHERE id=1"
        )
    return counts


def reconcile_disallowed_domains(connection) -> dict[str, int]:
    """Delete owned records and dependent rows once for each policy revision.

    Shared run/source rows survive. External references in another company's raw
    evidence do not imply ownership and are deliberately not treated as identities.
    """
    domains = disallowed_domains()
    revision = DOMAIN_POLICY_REVISION + "\n" + "\n".join(domains)
    fingerprint = hashlib.sha256(revision.encode()).hexdigest()
    marker = f"domain-disallow:{fingerprint}"
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    def matches(value):
        host = normalize_domain(value)
        return bool(host) and any(
            host == domain or host.endswith(f".{domain}") for domain in domains
        )

    connection.create_function("domain_disallowed", 1, matches)
    residual_marker = f"domain-residual-cleanup:{fingerprint}"
    if connection.execute("SELECT 1 FROM schema_migrations WHERE name=?", (marker,)).fetchone():
        if not connection.execute(
            "SELECT 1 FROM schema_migrations WHERE name=?", (residual_marker,)
        ).fetchone():
            from .domain_cleanup import clean_residual_references
            clean_residual_references(connection)
        return {}
    counts = {}
    connection.execute("PRAGMA secure_delete=ON")
    with connection:
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        connection.execute("PRAGMA defer_foreign_keys=ON")
        connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS denied_rows "
            "(table_name TEXT, row_id INTEGER, PRIMARY KEY(table_name,row_id))"
        )
        connection.execute("DELETE FROM denied_rows")
        columns = {}
        domain_columns = {
            "hostname",
            "root",
            "url",
            "source_url",
            "candidate_domain",
            "canonical_url",
            "normalized_url",
            "evidence_url",
            "apex",
            "host",
            "website_host",
            "website",
            "company_key",
            "canonical_domain",
            "domain",
            "landing_domain",
            "landing_host",
            "subject_company_key",
            "object_company_key",
            "subject_key",
            "object_key",
        }
        for table in tables:
            columns[table] = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            owned = columns[table] & domain_columns
            # An error belongs to its source; a surviving company may merely have
            # been discovered through that source, so do not apply this globally.
            if table == "source_errors" and "source_apex" in columns[table]:
                owned.add("source_apex")
            if owned:
                predicates = []
                parameters = [table]
                for column in sorted(owned):
                    if column in {"website", "url", "source_url", "canonical_url", "normalized_url",
                                  "evidence_url", "subject_key", "object_key"}:
                        predicates.append(f'domain_disallowed("{column}")')
                    else:
                        normalized = f'lower(rtrim("{column}", "."))'
                        for domain in domains:
                            predicates.append(f"({normalized}=? OR {normalized} LIKE ?)")
                            parameters.extend([domain, f"%.{domain}"])
                predicate = " OR ".join(predicates)
                connection.execute(
                    f'INSERT OR IGNORE INTO denied_rows SELECT ?, rowid FROM "{table}" '
                    f"WHERE {predicate}",
                    parameters,
                )
                logger.info("Domain purge checked %s", table)
        expand_owned_rows(connection, tables, columns)
        counts = delete_owned_rows(connection, tables)
        connection.execute("DELETE FROM schema_migrations WHERE name LIKE 'domain-disallow:%'")
        connection.execute(
            "INSERT INTO schema_migrations(name,applied_at,rows_changed) "
            "VALUES (?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?)",
            (marker, sum(counts.values())),
        )
        connection.execute("DROP TABLE denied_rows")
    from .domain_cleanup import clean_residual_references
    clean_residual_references(connection)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Disallow a domain and purge its company data")
    parser.add_argument("--add", required=True)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    domain = normalize_domain(args.add)
    if not domain or "." not in domain or any(not part for part in domain.split(".")):
        parser.error("Supply a valid domain or HTTP(S) URL")
    if not args.database.is_file():
        parser.error("Database must already exist")
    domains = sorted({*disallowed_domains(), domain})
    POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = POLICY_PATH.with_suffix(".tmp")
    temporary.write_text(
        "# Domains and all subdomains; additions purge company data.\n" + "\n".join(domains) + "\n"
    )
    temporary.replace(POLICY_PATH)
    from .store import LeadStore

    with LeadStore(args.database) as store:
        from .domain_cleanup import clean_generated_research

        # Filesystem removal belongs to this explicit maintenance command, not to
        # an arbitrary store/read constructor using a separately configured database.
        reports_removed = clean_generated_research(POLICY_PATH.parent.parent)
        print(
            {
                "disallowed": domains,
                "reports_removed": reports_removed,
                "remaining_candidates": store.connection.execute(
                    "SELECT COUNT(*) FROM candidates WHERE domain_disallowed(hostname)"
                ).fetchone()[0],
            }
        )


if __name__ == "__main__":
    main()
