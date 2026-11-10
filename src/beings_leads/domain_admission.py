"""Exact, local admission accounting for shared-domain company floods."""
from __future__ import annotations

import ipaddress
from collections import defaultdict

import tldextract

from .domain_policy import add_disallowed_domains, is_disallowed, normalize_domain

SUBDOMAIN_LIMIT = 50
_PUBLIC = tldextract.TLDExtract(cache_dir=None, suffix_list_urls=(),
                              include_psl_private_domains=False)


def company_host(value: str) -> str:
    return normalize_domain(value).removeprefix("www.")


def registered_root(value: str) -> str:
    host = company_host(value)
    try:
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        pass
    parsed = _PUBLIC(host)
    return f"{parsed.domain}.{parsed.suffix}" if parsed.domain and parsed.suffix else ""


def detect_subdomain_floods(values) -> dict[str, int]:
    groups = defaultdict(set)
    for value in values:
        host = company_host(value)
        root = registered_root(host)
        if root and host != root:
            groups[root].add(host)
    return {root: len(hosts) for root, hosts in groups.items() if len(hosts) > SUBDOMAIN_LIMIT}


def block_discovery_floods(values) -> dict[str, int]:
    detected = detect_subdomain_floods(values)
    if detected:
        add_disallowed_domains(detected)
    return detected


def initialize_admission(connection) -> None:
    """Backfill thin stored identities once; never hydrate evidence or call providers."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS domain_admission_hosts ("
        "root TEXT NOT NULL, hostname TEXT NOT NULL, PRIMARY KEY(root,hostname))"
    )
    marker = "domain-admission-hosts-v1"
    if connection.execute("SELECT 1 FROM schema_migrations WHERE name=?", (marker,)).fetchone():
        return
    from .domain_report import collect_hosts
    register_hosts(connection, collect_hosts(connection))
    connection.execute(
        "INSERT INTO schema_migrations(name,applied_at,rows_changed) "
        "VALUES (?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),0)", (marker,)
    )
    connection.commit()


def register_hosts(connection, values) -> dict[str, int]:
    """Serialize distinct identity publication across workers, then persist any bans.

    Only unblocked subdomain identities are retained. A banned root retains its rule,
    not a sample of the research removed from the inventory.
    """
    rows = set()
    for value in values:
        host = company_host(value)
        root = registered_root(host)
        if root and host != root and not is_disallowed(host):
            rows.add((root, host))
    if not rows:
        return {}
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    connection.executemany("INSERT OR IGNORE INTO domain_admission_hosts VALUES (?,?)", rows)
    roots = sorted({root for root, _ in rows})
    detected = {}
    for root in roots:
        count = connection.execute(
            "SELECT COUNT(*) FROM domain_admission_hosts WHERE root=?", (root,)
        ).fetchone()[0]
        if count > SUBDOMAIN_LIMIT:
            detected[root] = count
    if detected:
        add_disallowed_domains(detected)
        connection.executemany("DELETE FROM domain_admission_hosts WHERE root=?",
                               [(root,) for root in detected])
    return detected
