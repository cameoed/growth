"""Bounded operator URL import into the shared company inventory."""
from __future__ import annotations

import re
from pathlib import Path

from .agency_discovery import canonicalize_public_website
from .domain_policy import is_disallowed
from .models import DiscoveredHost
from .store import LeadStore

MAX_IMPORT_ENTRIES = 10_000
MAX_IMPORT_BYTES = 4 * 1024 * 1024


def parse_company_urls(text: str) -> dict:
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ValueError("Paste at most 4 MiB of URLs")
    entries = [value for value in re.split(r"[\s,;]+", text.strip()) if value]
    if not entries:
        raise ValueError("Paste at least one company URL")
    if len(entries) > MAX_IMPORT_ENTRIES:
        raise ValueError("Paste at most 10,000 URLs per import; split larger lists")
    hosts: dict[str, list[str]] = {}
    errors = []
    duplicates = 0
    for index, value in enumerate(entries, 1):
        try:
            host = canonicalize_public_website(value)
        except ValueError:
            host = None
        reason = "invalid" if not host else "blocked" if is_disallowed(host) else ""
        if reason:
            # Blocked identities must not be retained as sampled research/audit data.
            errors.append({"entry": index, "reason": reason})
            continue
        if host in hosts:
            duplicates += 1
        urls = hosts.setdefault(host, [])
        if value not in urls:
            urls.append(value)
    return {"total": len(entries), "duplicates": duplicates, "hosts": hosts, "errors": errors}


def save_company_urls(database: Path, text: str, tag: str = "") -> dict:
    from .company_tags import normalize_tag
    from .store import _now

    tag = normalize_tag(tag)
    parsed = parse_company_urls(text)
    with LeadStore(database) as store:
        hosts = parsed.pop("hosts")
        existing = store.candidate_ids(list(hosts))
        store.upsert_candidates([
            DiscoveredHost(source_apex="operator-import", hostname=host,
                           discovery_evidence_urls=urls)
            for host, urls in hosts.items()
        ])
        # Admission can block a shared-domain flood during persistence. Never scan it.
        accepted = list(store.candidate_ids(list(hosts)))
        admission_blocked = len(hosts) - len(accepted)
        if tag:
            ids = store.candidate_ids(accepted)
            with store.connection:
                store.connection.executemany(
                    "INSERT OR IGNORE INTO prospect_tags(candidate_id,tag,created_at) "
                    "VALUES (?,?,?)",
                    [(cid, tag, _now()) for cid in ids.values()],
                )
    errors = parsed["errors"]
    return {
        **parsed, "hosts": accepted, "accepted": len(accepted),
        "created": sum(host not in existing for host in accepted),
        "existing": sum(host in existing for host in accepted),
        "invalid": sum(error["reason"] == "invalid" for error in errors),
        "blocked": sum(error["reason"] == "blocked" for error in errors) + admission_blocked,
    }
