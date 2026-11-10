"""Offline, read-only discovery of repeated company domain namespaces."""

from __future__ import annotations

import argparse
import ipaddress
import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import tldextract

from .config import PLATFORM_PROFILES
from .domain_policy import POLICY_PATH, is_disallowed, normalize_domain

PUBLIC = tldextract.TLDExtract(
    cache_dir=None, suffix_list_urls=(), include_psl_private_domains=False
)
PRIVATE = tldextract.TLDExtract(
    cache_dir=None, suffix_list_urls=(), include_psl_private_domains=True
)
SERVICE_LABELS = {
    "api",
    "cdn",
    "mail",
    "email",
    "smtp",
    "imap",
    "pop",
    "webmail",
    "ftp",
    "admin",
    "autodiscover",
    "autoconfig",
    "cpanel",
    "cpcontacts",
    "cpcalendars",
    "dev",
    "test",
    "staging",
    "www",
    "ns1",
    "ns2",
    "status",
    "support",
    "help",
    "docs",
    "assets",
    "static",
}


def collect_hosts(connection: sqlite3.Connection) -> dict[str, set[str]]:
    """Thin identity reads only: no receipt JSON, migrations or provider requests."""
    hosts = defaultdict(set)
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    queries = [("candidates", "SELECT hostname FROM candidates", "candidate")]
    if "engine_ranking_members" in tables:
        queries.append(
            (
                "engine_ranking_members",
                "SELECT DISTINCT company_key FROM engine_ranking_members",
                "canonical",
            )
        )
    if "sales_accounts" in tables:
        queries.append(
            (
                "sales_accounts",
                "SELECT COALESCE(NULLIF(website_host,''),website) FROM sales_accounts",
                "saved",
            )
        )
    for table, query, origin in queries:
        if table not in tables:
            continue
        for row in connection.execute(query):
            host = normalize_domain(row[0]).removeprefix("www.")
            try:
                ipaddress.ip_address(host)
                continue
            except ValueError:
                pass
            if host:
                hosts[host].add(origin)
    return dict(hosts)


def repeated_suffixes(hosts: dict[str, set[str]], *, limit: int = 10) -> list[dict]:
    clusters = defaultdict(set)
    for host in hosts:
        extracted = PUBLIC(host)
        if not extracted.domain or not extracted.suffix:
            continue
        root = f"{extracted.domain}.{extracted.suffix}"
        if root == host:
            continue
        parts = host.split(".")
        root_length = len(root.split("."))
        # Include the parent namespace at every level, never com/co.uk/etc.
        for start in range(1, len(parts) - root_length + 1):
            clusters[".".join(parts[start:])].add(host)
    ranked = []
    for suffix, members in clusters.items():
        branches = {host[: -(len(suffix) + 1)].split(".")[-1] for host in members}
        if len(branches) < 2:
            continue
        service = sum(branch in SERVICE_LABELS for branch in branches)
        private = PRIVATE(f"sample.{suffix}")
        kind = "Repeated namespace; inspect samples"
        if private.is_private and private.suffix == suffix:
            kind = "PSL-listed shared hosting"
        elif suffix in PLATFORM_PROFILES:
            kind = "Known discovery platform"
        elif service / len(branches) >= 0.75:
            kind = "Mostly service/internal subdomains"
        ranked.append(
            {
                "domain": suffix,
                "unique_descendant_hosts": len(members),
                "independent_labels": len(branches),
                "candidate_hosts": sum("candidate" in hosts[host] for host in members),
                "canonical_hosts": sum("canonical" in hosts[host] for host in members),
                "saved_hosts": sum("saved" in hosts[host] for host in members),
                "apex_present": suffix in hosts,
                "already_disallowed": is_disallowed(suffix),
                "pattern": kind,
                "samples": [f"https://{host}/" for host in sorted(members)[:3]],
                "_members": members,
            }
        )
    ranked.sort(
        key=lambda row: (-row["unique_descendant_hosts"], row["domain"].count("."), row["domain"])
    )
    selected = []
    seen_members = set()
    for row in ranked:
        identity = frozenset(row.pop("_members"))
        if identity in seen_members:
            continue
        seen_members.add(identity)
        selected.append(row)
        if len(selected) == limit:
            break
    return selected


def report(database: Path, *, limit: int = 10) -> dict:
    if limit < 1:
        raise ValueError("Limit must be positive")
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        hosts = collect_hosts(connection)
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "database": str(database.resolve()),
            "unique_inventory_hosts": len(hosts),
            "count_basis": "Distinct descendant hosts; www aliases and receipts do not add counts.",
            "top": repeated_suffixes(hosts, limit=limit),
            "notes": [
                "Includes candidates, canonical ranking keys and saved-company websites; "
                "source labels are excluded.",
                "Uses the bundled PSL offline; bare public suffixes are excluded, "
                "private hosting namespaces retained.",
                "Includes unscanned/inactive hosts; these are not buyer or live-site counts.",
                "Nested rows can overlap; identical sets appear once. Frequency is not buyer fit.",
                "Previously purged company.site hosts are absent from this report.",
            ],
        }
    finally:
        connection.close()


def markdown(data: dict) -> str:
    lines = [
        "# Repeated domain namespaces",
        "",
        f"As of {data['generated_at']} · {data['unique_inventory_hosts']:,} unique stored hosts",
        "",
        "| Domain | Hosts | Independent labels | Pattern | Example |",
        "|---|---:|---:|---|---|",
    ]
    for row in data["top"]:
        lines.append(
            f"| {row['domain']} | {row['unique_descendant_hosts']:,} | "
            f"{row['independent_labels']:,} | {row['pattern']} | {row['samples'][0]} |"
        )
    lines.extend(
        ["", *[f"- {note}" for note in data["notes"]], "", "## Samples and inventory coverage", ""]
    )
    for row in data["top"]:
        lines.extend(
            [
                f"### {row['domain']}",
                "",
                f"Candidate hosts: {row['candidate_hosts']:,}; "
                f"canonical hosts: {row['canonical_hosts']:,}; "
                f"saved hosts: {row['saved_hosts']:,}. "
                f"Already disallowed: {row['already_disallowed']}.",
                "",
                *[f"- {url}" for url in row["samples"]],
                "",
            ]
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/leads.db"))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    data = report(args.database, limit=args.limit)
    rendered = json.dumps(data, indent=2) if args.format == "json" else markdown(data)
    if args.output:
        database_path = args.database.resolve()
        protected = {database_path, POLICY_PATH.resolve()}
        protected.update(
            Path(str(database_path) + suffix) for suffix in ("-wal", "-shm", "-journal")
        )
        if args.output.resolve() in protected:
            parser.error("Output cannot overwrite the database, its journals or the disallow list")
        args.output.write_text(rendered + "\n")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
