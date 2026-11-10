"""Current parsed directory receipts; exact source/page scopes, never whole-list assumptions."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from functools import partial

from .async_work import run_thread_owned
from .domain_policy import disallowed_domains, is_disallowed
from .models import DiscoveredHost
from .scan_diagnostics import source_time
from .store import LeadStore

REVISION = "directory-page-receipts-v3-exact-hosts"


class SourceCrawlCache:
    def __init__(self, database, suggestion_id, suggestion, limits, *, write_work=None):
        self.database = database
        self.write_work = write_work or run_thread_owned
        self.source_url = suggestion.url
        self.policy = hashlib.sha256("\n".join(disallowed_domains()).encode()).hexdigest()
        self.scope = json.dumps([REVISION, suggestion.source_kind, suggestion.url,
                                 limits.max_depth, limits.max_bytes_per_page,
                                 limits.max_pdf_bytes, limits.max_pdf_pages], sort_keys=True)
        # Official-domain resolution has not changed. Preserve identical paid receipts
        # under their established key while upgrading only direct extraction semantics.
        self.ai_scope = json.dumps([
            "directory-page-receipts-v2", suggestion.source_kind, suggestion.url,
            limits.max_depth, limits.max_bytes_per_page, limits.max_pdf_bytes, limits.max_pdf_pages,
        ], sort_keys=True)
        self.pages_reused = 0
        self.fresh_until = None
        self.api_calls = 0
        self.api_complete = False
        self.api_pending = False
        self.known_hosts = set()
        with LeadStore(database, initialize=False) as store:
            with store.connection:
                store.connection.execute("""CREATE TABLE IF NOT EXISTS source_crawl_pages (
                    cache_key TEXT PRIMARY KEY, source_url TEXT NOT NULL, url TEXT NOT NULL,
                    policy TEXT NOT NULL, observed_at TEXT NOT NULL, payload_json TEXT NOT NULL
                )""")
                store.connection.execute("PRAGMA secure_delete=ON")
                store.connection.execute(
                    "DELETE FROM source_crawl_pages WHERE policy<>? "
                    "OR (COALESCE(json_extract(payload_json,'$._receipt_revision'),'')<>? "
                    "AND NOT (length(url)=64 AND url NOT GLOB '*[^0-9a-f]*')) "
                    "OR julianday(observed_at)<julianday(?)",
                    (self.policy, REVISION, (datetime.now(UTC) - timedelta(hours=24)).isoformat()),
                )
            rows = store.connection.execute("""
                SELECT DISTINCT c.hostname FROM discovery_source_candidates lineage
                JOIN candidates c ON c.id=lineage.candidate_id
                JOIN source_suggestions s ON s.id=lineage.suggestion_id
                WHERE COALESCE(s.duplicate_of_suggestion_id,s.id)=?
            """, (suggestion_id,))
            self.known_hosts = {r[0] for r in rows}

    def key(self, url, kind):
        scope = self.ai_scope if kind == "ai_resolution" else self.scope
        return hashlib.sha256(json.dumps([scope, self.policy, kind, url]).encode()).hexdigest()

    def get(self, url, kind):
        with LeadStore(self.database, initialize=False, read_only=True) as store:
            row = store.connection.execute(
                "SELECT observed_at,payload_json FROM source_crawl_pages WHERE cache_key=?",
                (self.key(url, kind),),
            ).fetchone()
        if not row:
            return None
        try:
            stamp = datetime.fromisoformat(row[0])
            if datetime.now(UTC) - stamp >= timedelta(hours=24):
                return None
            value = json.loads(row[1])
            value.pop("_receipt_revision", None)
        except (TypeError, ValueError):
            return None
        expires = stamp + timedelta(hours=24)
        self.fresh_until = min(self.fresh_until, expires) if self.fresh_until else expires
        self.pages_reused += 1
        return value

    async def get_async(self, url, kind):
        with source_time("directory_cache_read"):
            return await run_thread_owned(self.get, url, kind)

    async def put_async(self, url, kind, payload):
        await self.write_work(partial(self.put, url, kind, payload))

    def put(self, url, kind, payload):
        stamp = datetime.now(UTC)
        expires = stamp + timedelta(hours=24)
        self.fresh_until = min(self.fresh_until, expires) if self.fresh_until else expires
        with LeadStore(self.database, initialize=False) as store, store.connection:
            store.connection.execute("""INSERT OR REPLACE INTO source_crawl_pages
                VALUES(?,?,?,?,?,?)""", (self.key(url, kind), self.source_url, url,
                self.policy, stamp.isoformat(), json.dumps({
                    **payload, "_receipt_revision": REVISION,
                })))

    def candidates(self, values):
        result = []
        for value in values:
            try:
                item = DiscoveredHost.model_validate(value)
            except ValueError:
                continue
            if item.hostname not in self.known_hosts and not is_disallowed(item.hostname):
                result.append(item)
        return result
