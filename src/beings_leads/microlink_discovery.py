#!/usr/bin/env python3
"""Explicit, budgeted Microlink Search for net-new buyer discovery."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

import httpx

from beings_leads.domain_admission import company_host, registered_root
from beings_leads.domain_policy import is_disallowed
from beings_leads.inspect import MICROLINK_API_URL, UnsafeTargetError, _bounded_get
from beings_leads.microlink_mission import GENERIC, LOCATIONS, scale_hint
from beings_leads.models import DiscoveredHost
from beings_leads.provider_rate_limit import rate_limit_scope
from beings_leads.source_discovery import canonicalize_public_website
from beings_leads.store import LeadStore

SOURCE = "microlink.google"
PUBLISHERS_AND_LISTINGS = {
    "adage.com", "adweek.com", "apnews.com", "axios.com", "benzinga.com", "bloomberg.com",
    "builtin.com", "businesswire.com", "campaignlive.com", "digiday.com", "fashionnetwork.com",
    "finance.yahoo.com", "forbes.com", "glassdoor.com", "globenewswire.com", "greenhouse.io",
    "indeed.com", "jobgether.com", "jooble.org", "lever.co", "linkedin.com", "marketingdive.com",
    "icims.com", "welcometothejungle.com", "remotesource.com", "ncaamarket.ncaa.org", "gohire.io",
    "mediabistro.com", "news.google.com", "prnewswire.com", "reuters.com", "smartrecruiters.com",
    "techcrunch.com", "the-dots.com", "theverge.com", "wellfound.com", "workable.com",
    "workday.com", "myworkdayjobs.com", "yahoo.com", "ziprecruiter.com", "ashbyhq.com",
    "bamboohr.com", "rippling.com", "teamtailor.com", "theorg.com", "wwd.com",
}
HIRING_PATH = re.compile(r"/(?:careers?|jobs?|job-details?|open-roles|vacancies)(?:/|$)", re.I)
LAUNCH_PATH = re.compile(
    r"/(?:newsroom|press(?:-releases?)?|blog|news|stories|announcements?)(?:/|$)", re.I,
)
HIRING_SIGNAL = re.compile(
    r"creative (?:strateg|produc|operat|test)|performance creative|paid social|"
    r"growth creative|content produc|brand market|video produc", re.I,
)
LAUNCH_SIGNAL = re.compile(
    r"launch|roll(?:s|ed)? out|expand|unveil|debut|introduc|new campaign", re.I,
)
SECTORS = ("beauty", "apparel", "food beverage", "fitness", "home goods", "pet care",
           "consumer software", "retail", "subscription commerce", "health wellness")
ROLES = ("creative strategist", "performance creative", "creative producer", "paid social",
         "content production", "creative operations", "video producer", "brand marketing")
MARKETS = tuple(LOCATIONS)
ATS = {"ashbyhq.com", "greenhouse.io", "lever.co", "myworkdayjobs.com", "workable.com"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS microlink_discovery_budgets (
    budget_id TEXT PRIMARY KEY,
    request_cap INTEGER NOT NULL CHECK(request_cap BETWEEN 1 AND 45000)
);
CREATE TABLE IF NOT EXISTS microlink_discovery_requests (
    query_key TEXT PRIMARY KEY,
    budget_id TEXT NOT NULL REFERENCES microlink_discovery_budgets(budget_id),
    lane TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('reserved','complete','failed')),
    recorded_at TEXT NOT NULL,
    result_count INTEGER NOT NULL DEFAULT 0,
    cache_hit INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_microlink_discovery_budget_requests
ON microlink_discovery_requests(budget_id);
CREATE TABLE IF NOT EXISTS microlink_discovery_contact_batches (
    budget_id TEXT NOT NULL REFERENCES microlink_discovery_budgets(budget_id),
    batch TEXT NOT NULL,
    request_cap INTEGER NOT NULL CHECK(request_cap > 0),
    run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    PRIMARY KEY(budget_id, batch)
);
CREATE TABLE IF NOT EXISTS microlink_discovery_leads (
    query_key TEXT NOT NULL REFERENCES microlink_discovery_requests(query_key),
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    PRIMARY KEY(query_key, candidate_id)
);
"""


def recipes(anchor: date, market: str):
    """Stable within the week. Vary query order and windows across successive weeks."""
    week = anchor - timedelta(days=anchor.weekday())
    offset = week.toordinal() // 7
    after = (week - timedelta(days=30)).isoformat()
    for index in range(len(SECTORS) * len(ROLES)):
        sector = SECTORS[(index + offset) % len(SECTORS)]
        role = ROLES[(index // len(SECTORS) + offset) % len(ROLES)]
        path = (
            "(site:jobs.lever.co OR site:job-boards.greenhouse.io OR site:jobs.ashbyhq.com)"
            if index % 3 == 1 else "(inurl:careers OR inurl:jobs)"
        )
        yield "hiring", "search", market, week, (
            f'"{role}" {sector} {path} after:{after} '
            "-site:linkedin.com -site:indeed.com -site:glassdoor.com"
        )
        signal = (
            '"launches its"', '"new brand campaign"', '"expands into"',
            '"introduces its"', '"announces launch"', '"unveils its"',
            '"new collection"', '"rolls out"',
        )[(index // len(SECTORS) + offset) % 8]
        surface = "news" if index % 5 == 1 else "search"
        location = "(inurl:newsroom OR inurl:press OR inurl:blog)" if surface == "search" else ""
        yield "launch", surface, market, week, (
            f"{signal} {sector} {location} after:{after} "
            "-site:prnewswire.com -site:businesswire.com -site:globenewswire.com"
        )


def query_key(recipe):
    # The local admission term does not change the provider request or its existing receipt.
    return hashlib.sha256(json.dumps(recipe[:5], default=str).encode()).hexdigest()


def candidate_hit(item, lane, *, term=""):
    """Keep plausible first-party discovery URLs. A hit carries no buyer assertion."""
    if not isinstance(item, dict):
        return None
    url = item.get("url")
    if not isinstance(url, str) or len(url) > 1500:
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or parts.username or parts.password
                or parts.port not in {80, 443, None}):
            return None
        host = canonicalize_public_website(url)
        root = registered_root(host) if host else ""
    except (ValueError, TypeError):
        return None
    if (not root or is_disallowed(host) or
            any(host == bad or host.endswith("." + bad) for bad in PUBLISHERS_AND_LISTINGS)):
        return None
    path = parts.path
    title = str(item.get("title") or "")[:300]
    text = title + " " + str(item.get("description") or item.get("snippet") or "")[:500]
    if lane == "mission":
        words = set(re.findall(r"[^\W_]+", term.casefold())) - GENERIC
        content_words = set(re.findall(r"[^\W_]+", text.casefold()))
        if not words or len(words & content_words) < max(1, (len(words) + 1) // 2):
            return None
        if re.search(r"\b(?:top \d+|best \d+|list of companies|company directory)\b", title, re.I):
            return None
        brand = re.sub(r"[^a-z0-9]", "", root.split(".")[0])
        headline = re.sub(r"[^a-z0-9]", "", title.casefold())
        if len(brand) < 3 or brand not in headline:
            return None
        portal = host.removesuffix("." + root) if host != root else ""
        if portal in {"careers", "jobs", "corporate", "newsroom"}:
            host = root
    elif lane == "hiring":
        portal = host.removesuffix("." + root) if host != root else ""
        if not (HIRING_PATH.search(path) or portal in {"careers", "jobs", "job"}):
            return None
        if not HIRING_SIGNAL.search(text):
            return None
        if portal in {"careers", "jobs", "job"}:
            host = root
        if path.rstrip("/") in {"", "/careers", "/jobs"}:
            return None
    else:
        brand = re.sub(r"[^a-z0-9]", "", root.split(".")[0])
        headline = re.sub(r"[^a-z0-9]", "", title.split(" | ", 1)[0].casefold())
        if not (LAUNCH_PATH.search(path) and LAUNCH_SIGNAL.search(text)
                and len(brand) >= 4 and 0 <= headline.find(brand) <= 30):
            return None
    return host, parts._replace(fragment="").geturl()


def ats_url(item):
    if not isinstance(item, dict):
        return None
    url = item.get("url")
    if not isinstance(url, str) or len(url) > 1500:
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.username or parts.password
                or parts.port not in {443, None} or registered_root(parts.hostname or "") not in ATS
                or is_disallowed(url)):
            return None
    except ValueError:
        return None
    return url if HIRING_SIGNAL.search(str(item.get("title", ""))[:300]) else None


def employer_website(body, observed_at):
    """Resolve only a single explicitly named company website on a recent posting."""
    from beings_leads.hiring_intelligence import _is_jobposting, _jsonld_nodes
    from beings_leads.html_document import parse_html

    hosts = set()
    soup = parse_html(body.decode("utf-8", errors="replace"))
    for script in soup.find_all("script", type=re.compile(r"ld\+json", re.I))[:40]:
        if script.find_parent("template"):
            continue
        try:
            nodes = _jsonld_nodes(json.loads(script.string or ""))
        except (ValueError, RecursionError):
            continue
        for node in nodes:
            if not _is_jobposting(node) or not HIRING_SIGNAL.search(str(node.get("title") or "")):
                continue
            try:
                posted = date.fromisoformat(str(node.get("datePosted") or "")[:10])
            except ValueError:
                continue
            if not 0 <= (observed_at - posted).days <= 45:
                continue
            employer = node.get("hiringOrganization")
            if not isinstance(employer, dict):
                continue
            for name in ("url", "sameAs"):
                values = employer.get(name)
                for url in values if isinstance(values, list) else [values]:
                    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                        continue
                    try:
                        host = canonicalize_public_website(url)
                    except ValueError:
                        continue
                    if (host and registered_root(host) not in PUBLISHERS_AND_LISTINGS
                            and not is_disallowed(host)):
                        hosts.add(host)
    return next(iter(hosts)) if len(hosts) == 1 else None


async def ats_hit(client, url, anchor, robots):
    from beings_leads.frontier_orchestration import _FrontierHttpFetcher

    host = urlsplit(url).hostname
    fetcher = _FrontierHttpFetcher(
        client, "microlink_discovery", host, url, {url}, {}, {}, set(), set(), robots_cache=robots,
    )
    try:
        async with asyncio.timeout(15):
            await fetcher._check_robots(url)
            response, body = await _bounded_get(
                client, url, max_bytes=450_000, max_redirects=0, timeout=10,
            )
        if response.is_success:
            match = employer_website(body, anchor)
            if match:
                return match, url
    except (httpx.HTTPError, OSError, UnsafeTargetError, TimeoutError, ValueError):
        pass
    return None


def known_hosts(connection):
    hosts = {company_host(row[0]) for row in connection.execute("SELECT hostname FROM candidates")}
    hosts.update(company_host(row[0]) for row in connection.execute(
        "SELECT DISTINCT company_key FROM engine_ranking_members"
    ))
    hosts.update(company_host(row[0]) for row in connection.execute(
        "SELECT website_host FROM sales_accounts WHERE website_host<>''"
    ))
    return hosts


def remaining_queries(connection, anchor, market, lane):
    keys = {query_key(r) for r in recipes(anchor, market) if lane == "both" or r[0] == lane}
    if connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE name='microlink_discovery_requests'"
    ).fetchone():
        placeholders = ",".join("?" for _ in keys)
        keys.difference_update(row[0] for row in connection.execute(
            "SELECT query_key FROM microlink_discovery_requests "
            f"WHERE query_key IN ({placeholders})",
            sorted(keys),
        ))
    return len(keys)


def initialize(connection, budget, cap):
    connection.executescript(SCHEMA)
    with connection:
        connection.execute("INSERT OR IGNORE INTO microlink_discovery_budgets VALUES (?,?)",
                           (budget, cap))
    stored = connection.execute(
        "SELECT request_cap FROM microlink_discovery_budgets WHERE budget_id=?", (budget,)
    ).fetchone()
    if stored[0] != cap:
        raise ValueError("Budget ceiling is immutable. Reuse the original ceiling.")


def reserved_requests(connection, budget):
    search = connection.execute(
        "SELECT COUNT(*) FROM microlink_discovery_requests WHERE budget_id=?", (budget,)
    ).fetchone()[0]
    contacts = connection.execute(
        "SELECT COALESCE(SUM(request_cap),0) FROM microlink_discovery_contact_batches "
        "WHERE budget_id=?", (budget,)
    ).fetchone()[0]
    return search + contacts


def reserve(connection, budget, cap, key, lane):
    connection.execute("BEGIN IMMEDIATE")
    try:
        if connection.execute(
            "SELECT 1 FROM microlink_discovery_requests WHERE query_key=?", (key,)
        ).fetchone():
            state = "reused"
        elif reserved_requests(connection, budget) >= cap:
            state = "capped"
        else:
            connection.execute(
                "INSERT INTO microlink_discovery_requests VALUES (?,?,?,'reserved',?,0,0)",
                (key, budget, lane, datetime.now(UTC).isoformat()),
            )
            state = "reserved"
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
        return state


async def provider_search(client, api_key, recipe):
    lane, surface, market, _, query = recipe[:5]
    period = "" if lane == "mission" else "&period=month"
    target = f"https://{SOURCE}/{quote_plus(query)}/10/{market}?type={surface}{period}"
    endpoint = httpx.URL(MICROLINK_API_URL, params={
        "url": target, "meta": "false", "ttl": "1d", "staleTtl": "0",
    })
    response, body = await _bounded_get(
        client, str(endpoint), max_bytes=300_000, max_redirects=0,
        accept="application/json", allowed_content_types=("application/json", "text/json"),
        request_headers={"x-api-key": api_key}, timeout=35,
    )
    response.raise_for_status()
    if response.extensions.get("beings_body_truncated"):
        raise ValueError("Search response exceeded byte limit")
    payload = json.loads(body)
    data = payload.get("data") if isinstance(payload, dict) else None
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or payload.get("status") != "success":
        raise ValueError("Unexpected Microlink Search response")
    return results, "HIT" in response.headers.get("x-cache-status", "").upper()


def persist(store, budget, hits, maximum, *, inventory=None, prefer_large=False):
    db = store.connection
    if inventory is None:
        inventory = known_hosts(db)
    edges = {}
    for hit in hits:
        key, lane, results = hit[:3]
        term = hit[3] if len(hit) > 3 else ""
        for position, item in enumerate(results):
            match = item if isinstance(item, tuple) else candidate_hit(item, lane, term=term)
            if match is None:
                continue
            host, url = match
            if company_host(host) in inventory:
                continue
            text = " ".join(str(item.get(field) or "")[:500] for field in (
                "title", "description", "snippet",
            )) if isinstance(item, dict) else ""
            rank = scale_hint(text) if prefer_large else 0
            # Position only breaks ties among companies with the same search clues.
            priority = rank + max(0, 10 - position) / 100 if prefer_large else 0
            edge = edges.setdefault(host, {"urls": set(), "keys": set(), "rank": 0.0})
            edge["urls"].add(url)
            edge["keys"].add(key)
            edge["rank"] = max(edge["rank"], priority)
    selected = sorted(edges.items(), key=lambda item: -item[1]["rank"])
    fresh = []
    for host, edge in selected:
        if len(fresh) >= maximum:
            break
        if is_disallowed(host):
            continue
        if db.execute(
            "SELECT 1 FROM candidates WHERE hostname IN (?,?) "
            "UNION ALL SELECT 1 FROM engine_ranking_members WHERE company_key=? "
            "UNION ALL SELECT 1 FROM sales_accounts WHERE website_host=? LIMIT 1",
            (host, "www." + host, host, host),
        ).fetchone():
            inventory.add(company_host(host))
            continue
        fresh.append((host, edge))
    if not fresh:
        return 0
    store.upsert_candidates([
        DiscoveredHost(
            hostname=host, source_apex=SOURCE, discovery_rank=edge["rank"],
            discovery_evidence_urls=sorted(edge["urls"])[:12],
            provider_record_ids=[
                f"microlink-search:{budget}:{key}" for key in sorted(edge["keys"])
            ],
        )
        for host, edge in fresh
    ])
    links = []
    for host, edge in fresh:
        row = db.execute("SELECT id FROM candidates WHERE hostname=?", (host,)).fetchone()
        if row and not is_disallowed(host):
            inventory.add(company_host(host))
            links.extend((key, row[0]) for key in edge["keys"])
    with db:
        db.executemany("INSERT OR IGNORE INTO microlink_discovery_leads VALUES (?,?)", links)
    return len({cid for _, cid in links})


def saved_candidate_count(db, budget):
    return db.execute(
        "SELECT COUNT(DISTINCT l.candidate_id) FROM microlink_discovery_leads l "
        "JOIN microlink_discovery_requests q USING(query_key) WHERE q.budget_id=?", (budget,)
    ).fetchone()[0]


def report(db, budget):
    requests = [dict(row) for row in db.execute(
        "SELECT lane,state,COUNT(*) requests,SUM(result_count) hits,SUM(cache_hit) cached "
        "FROM microlink_discovery_requests WHERE budget_id=? GROUP BY lane,state", (budget,)
    )]
    leads = [dict(row) for row in db.execute(
        "SELECT c.id,c.hostname,GROUP_CONCAT(DISTINCT q.lane) lanes,"
        "COUNT(DISTINCT q.query_key) queries "
        "FROM microlink_discovery_leads l JOIN microlink_discovery_requests q USING(query_key) "
        "JOIN candidates c ON c.id=l.candidate_id WHERE q.budget_id=? "
        "GROUP BY c.id ORDER BY c.discovery_rank DESC,queries DESC,c.hostname", (budget,)
    )]
    contacts = [dict(row) for row in db.execute(
        "SELECT b.batch,b.request_cap,b.run_id,r.status,r.summary_json "
        "FROM microlink_discovery_contact_batches b LEFT JOIN runs r ON r.id=b.run_id "
        "WHERE b.budget_id=? ORDER BY b.batch", (budget,)
    )]
    for row in contacts:
        row["summary"] = json.loads(row.pop("summary_json") or "{}")
    return {"budget": budget, "requests": requests, "leads": leads, "contact_batches": contacts}



async def run_search(store, args, key, *, stopped=lambda: False):
    db = store.connection
    initialize(db, args.budget, args.cap)
    plan = getattr(args, "plan", None)
    if plan is None:
        plan = [recipe for recipe in recipes(args.date, args.market)
                if args.lane == "both" or args.lane == recipe[0]]
    inventory = known_hosts(db)
    prefer_large = getattr(args, "prefer_large", False)
    batch_size = 12 if args.lane == "mission" else 6
    pending = []
    limits = httpx.Limits(max_connections=batch_size, max_keepalive_connections=batch_size)
    counters = Counter()
    seen_ats = set()
    robots = {}
    async with httpx.AsyncClient(limits=limits, trust_env=False) as client:
        with rate_limit_scope(args.database):
            async def fetch(fingerprint, recipe):
                try:
                    results, cache_hit = await provider_search(client, key, recipe)
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise
                    # The durable reservation remains uncertain. Let peers drain and stop.
                    counters["database_busy"] += 1
                    return None
                except (httpx.HTTPError, OSError, ValueError) as exc:
                    code = (exc.response.status_code
                            if isinstance(exc, httpx.HTTPStatusError) else None)
                    counters[f"failed:{code or type(exc).__name__}"] += 1
                    if code is not None or isinstance(exc, ValueError):
                        with db:
                            db.execute("UPDATE microlink_discovery_requests SET state='failed' "
                                       "WHERE query_key=?", (fingerprint,))
                    return None
                with db:
                    db.execute(
                        "UPDATE microlink_discovery_requests SET state='complete',result_count=?,"
                        "cache_hit=? WHERE query_key=?", (len(results), int(cache_hit), fingerprint)
                    )
                if recipe[0] == "hiring":
                    urls = [
                        url for item in results if (url := ats_url(item)) and url not in seen_ats
                    ]
                    urls = list(dict.fromkeys(urls))[:3]
                    seen_ats.update(urls)
                    resolved = await asyncio.gather(
                        *(ats_hit(client, url, args.date, robots) for url in urls)
                    )
                    results.extend(match for match in resolved if match)
                    counters["ats_resolved"] += sum(bool(match) for match in resolved)
                return fingerprint, recipe[0], results, *recipe[5:]

            def publish(hits, maximum):
                return persist(
                    store, args.budget, [hit for hit in hits if hit], maximum,
                    inventory=inventory, prefer_large=prefer_large,
                )

            maximum = args.max_candidates - saved_candidate_count(db, args.budget)
            dispatched = 0
            for recipe in plan:
                if (stopped() or maximum <= 0 or dispatched >= args.wave
                        or counters.get("failed:429")
                        or counters.get("database_busy")):
                    break
                fingerprint = query_key(recipe)
                try:
                    state = reserve(db, args.budget, args.cap, fingerprint, recipe[0])
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise
                    counters["database_busy"] += 1
                    # No additional provider call should begin during a busy stop.
                    pending.clear()
                    break
                if state == "capped":
                    break
                if state == "reused":
                    continue
                pending.append((fingerprint, recipe))
                dispatched += 1
                if len(pending) == batch_size or dispatched == args.wave:
                    hits = await asyncio.gather(*(fetch(*item) for item in pending))
                    maximum -= publish(hits, maximum)
                    pending.clear()
            if pending and not stopped():
                hits = await asyncio.gather(*(fetch(*item) for item in pending))
                publish(hits, maximum)
    return dict(counters)


def contact_cohort(store, budget, limit, min_score, *, research=False):
    from beings_leads.sales_engine.ranking import policy_version

    db = store.connection
    now = datetime.now(UTC).isoformat()
    rows = db.execute(
        """SELECT DISTINCT c.id,c.hostname,r.company_key,r.score,r.eligible,
        r.target_json,r.assessment_json
        FROM microlink_discovery_leads l JOIN microlink_discovery_requests q USING(query_key)
        JOIN candidates c ON c.id=l.candidate_id
        JOIN engine_ranking_members m ON m.candidate_id=c.id
        JOIN engine_company_ranking r ON r.company_key=m.company_key
        WHERE q.budget_id=? AND r.excluded=0 AND r.dirty=0
            AND r.as_of<=? AND r.valid_until>? AND r.score>=? AND r.policy_version=?
        ORDER BY r.score DESC,c.id""", (budget, now, now, min_score, policy_version())
    )
    seen = set()
    cohort = []
    for row in rows:
        if not row["eligible"]:
            if not research:
                continue
            target = json.loads(row["target_json"])
            assessment = json.loads(row["assessment_json"])
            if (target.get("disqualified") or target.get("target_scope") == "out_of_scope"
                    or target.get("relationship_lane") == "referral_partner"
                    or assessment.get("readiness") == "deprioritize"):
                continue
        identity = row["company_key"]
        if identity in seen or is_disallowed(identity) or is_disallowed(row["hostname"]):
            continue
        if db.execute(
            """SELECT 1 FROM candidates c LEFT JOIN engine_ranking_members m
            ON m.candidate_id=c.id WHERE (c.hostname=? OR m.company_key=?)
            AND NOT EXISTS (
                SELECT 1 FROM microlink_discovery_leads l
                JOIN microlink_discovery_requests q USING(query_key)
                WHERE l.candidate_id=c.id AND q.budget_id=?
            ) LIMIT 1""", (identity, identity, budget)
        ).fetchone():
            continue
        seen.add(identity)
        cohort.append({"candidate_id": row["id"], "company_key": identity,
                       "hostname": row["hostname"], "score": row["score"],
                       "eligible": bool(row["eligible"])})
        if len(cohort) >= limit:
            break
    return cohort


def reserve_contacts(db, budget, cap, batch, amount):
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute(
            "SELECT 1 FROM microlink_discovery_contact_batches WHERE budget_id=? AND batch=?",
            (budget, batch),
        ).fetchone():
            raise ValueError("This contact batch is already reserved. Inspect its receipt.")
        if reserved_requests(db, budget) + amount > cap:
            raise ValueError("Contact batch would exceed the total budget.")
        db.execute(
            "INSERT INTO microlink_discovery_contact_batches VALUES (?,?,?,NULL)",
            (budget, batch, amount)
        )
    except BaseException:
        db.rollback()
        raise
    else:
        db.commit()


async def verify_owned_pages(store, args, run_id, options, key, cohort, client, checkpoint):
    """Check one cited page and search for named buyer roles on the verified company site."""
    from beings_leads.async_work import gather_owned
    from beings_leads.microlink_hydration import Hydrator, MicrolinkLimitReached, _own_url
    from beings_leads.site_research import SiteResearchPlanner

    lock = asyncio.Lock()
    stopped = asyncio.Event()
    slots = asyncio.Semaphore(4)
    db = store.connection
    results = Counter()

    async def verify(company, signal, base):
        owner = Hydrator(
            args.database, run_id, options, key, client, checkpoint, lambda _: None,
            db_lock=lock, stopped=stopped,
        )
        planner = SiteResearchPlanner(base)

        async def page(url, feature):
            try:
                await owner.page(company, url, feature, planner)
                results[f"{feature}_checked"] += 1
            except (httpx.HTTPError, OSError, ValueError, UnsafeTargetError):
                results[f"{feature}_failed"] += 1

        async with slots:
            if stopped.is_set():
                return
            try:
                if signal:
                    await page(signal, "company_pages")
                query = (
                    f"site:{company['company_key']} "
                    '("chief marketing officer" OR "head of marketing" OR "VP of marketing" '
                    'OR "creative director" OR "chief brand officer")'
                )
                target = f"https://{SOURCE}/{quote_plus(query)}/10/{args.market}?type=search"
                receipt = await owner.request(company, {
                    "url": target, "meta": "false", "ttl": "1d", "staleTtl": "0",
                }, "contact_identity_search")
                if receipt is None:
                    return
                raw = receipt[2].get("results")
                if not isinstance(raw, list):
                    await owner.db(lambda owned: owner.fail_pending(
                        owned, receipt[0], "invalid:search_results",
                    ))
                    results["role_search_failed"] += 1
                    return
                urls = []
                for item in raw:
                    if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                        continue
                    url = _own_url(item["url"], base)
                    if url and url != signal and url not in urls:
                        urls.append(url)
                # Store only owned URLs, without treating snippets as contact proof.
                await owner.publish(
                    company, receipt, "contact_identity_search",
                    results=[{"url": url} for url in urls], url=target,
                )
                results["role_search_complete"] += 1
                for url in urls[:2]:
                    await page(url, "contacts")
            except MicrolinkLimitReached:
                stopped.set()
                results["limited"] += 1
            except (httpx.HTTPError, OSError, ValueError, UnsafeTargetError):
                results["role_search_failed"] += 1

    work = []
    # Up to four requests per company here, with room for normal contact traversal.
    for company in cohort[:args.contact_cap // 8]:
        base = f"https://{company['company_key']}/"
        row = db.execute(
            "SELECT evidence_urls_json FROM candidate_source_edges "
            "WHERE candidate_id=? AND source_apex=?", (company["candidate_id"], SOURCE),
        ).fetchone()
        urls = json.loads(row[0] or "[]") if row else []
        signal = next((url for raw in urls if (url := _own_url(raw, base))), "")
        work.append((company, signal, base))
    with rate_limit_scope(args.database):
        await gather_owned(*(verify(*item) for item in work))
    return dict(results)


async def run_contacts(store, args, api_key):
    from beings_leads.microlink_hydration import MicrolinkHydrationOptions, run_hydration

    db = store.connection
    initialize(db, args.budget, args.cap)
    cohort = contact_cohort(
        store, args.budget, args.contact_companies, args.min_score, research=True,
    )
    if not cohort:
        return {"companies": 0, "requests": 0, "status": "no_current_scored_companies"}
    options = MicrolinkHydrationOptions(
        contacts=True, company_limit=len(cohort), request_limit=args.contact_cap,
    )
    reserve_contacts(db, args.budget, args.cap, args.contact_batch, args.contact_cap)
    run_id = store.start_run({
        "only_hosts": [row["hostname"] for row in cohort],
        "microlink_hydration": options.model_dump(),
        "microlink_discovery_budget": args.budget,
    })
    with db:
        db.execute(
            "UPDATE microlink_discovery_contact_batches SET run_id=? "
            "WHERE budget_id=? AND batch=?", (run_id, args.budget, args.contact_batch)
        )

    async def checkpoint():
        await asyncio.sleep(0)

    try:
        limits = httpx.Limits(max_connections=12, max_keepalive_connections=12)
        async with httpx.AsyncClient(timeout=45, limits=limits, trust_env=False) as client:
            discovery_pages = await verify_owned_pages(
                store, args, run_id, options, api_key, cohort, client, checkpoint,
            )
            outcome = await run_hydration(
                args.database, run_id, options, api_key, cohort, client=client,
                checkpoint=checkpoint, progress=lambda _: None,
            )
            outcome["discovery_pages"] = discovery_pages
    except BaseException:
        store.finish_run(run_id, "interrupted", {"contacts": len(cohort)})
        raise
    store.finish_run(run_id, "partial" if outcome["failed"] else "complete", outcome)
    return {"run_id": run_id, "companies": len(cohort), **outcome}

def qualify(store, args):
    """Inspect the bounded, currently unhydrated pilot cohort using the regular scanner."""
    db = store.connection
    rows = report(db, args.budget)["leads"]
    candidates = []
    for row in rows:
        host = row["hostname"]
        if is_disallowed(host) or (
            host != registered_root(host)
            and host.split(".", 1)[0] in {"careers", "jobs", "job"}
        ):
            continue
        if not db.execute(
            "SELECT 1 FROM snapshots WHERE candidate_id=? LIMIT 1", (row["id"],)
        ).fetchone():
            candidates.append(row)
    # Alternate lanes rather than taking an alphabetical slice from a single lane.
    hiring = [r for r in candidates if "hiring" in r["lanes"]]
    launch = [r for r in candidates if "hiring" not in r["lanes"]]
    ordered = []
    while hiring or launch:
        if hiring:
            ordered.append(hiring.pop(0))
        if launch:
            ordered.append(launch.pop(0))
    if args.qualify_hosts:
        by_host = {row["hostname"]: row for row in ordered}
        ordered = [by_host[host] for host in dict.fromkeys(args.qualify_hosts) if host in by_host]
    hosts = [row["hostname"] for row in ordered[:args.qualify_companies]]
    if not hosts:
        return {"status": "no_unhydrated_companies", "companies": 0}
    payload = {
        "sources": [], "only_hosts": hosts, "max_hosts_per_source": len(hosts),
        "max_network_inspections": len(hosts), "adaptive_refresh": True,
        "pages_per_site": 4, "concurrency": args.qualify_concurrency, "english_only": True,
        "free_intelligence": not args.core_only,
        "max_frontier_scans": 0, "max_common_crawl_lookups": 0,
        "max_news_lookups": 0, "microlink_enabled": False, "people_hydration_enabled": False,
        "ai": False, "ai_web": False, "max_ai_calls": 0, "max_ai_web_calls": 0,
    }
    with httpx.Client(timeout=60, trust_env=False) as client:
        response = client.post("http://127.0.0.1:8787/api/scans", json=payload)
        response.raise_for_status()
    return {**response.json(), "companies": len(hosts)}


def publish_scores(store, budget):
    from beings_leads.sales_engine.ranking import refresh_candidate_rankings

    rows = store.connection.execute(
        """SELECT DISTINCT l.candidate_id
        FROM microlink_discovery_leads l JOIN microlink_discovery_requests q USING(query_key)
        JOIN engine_ranking_members m ON m.candidate_id=l.candidate_id
        LEFT JOIN engine_company_ranking r ON r.company_key=m.company_key
        WHERE q.budget_id=? AND (r.snapshot_id IS NULL OR r.snapshot_id<>m.snapshot_id
          OR r.dirty<>0 OR r.valid_until<=?)""",
        (budget, datetime.now(UTC).isoformat()),
    )
    return {"published": refresh_candidate_rankings(store, [row[0] for row in rows])}


def outcomes(store, budget):
    from types import SimpleNamespace

    from beings_leads.sales_engine.summaries import company_summary

    engine = SimpleNamespace(store=store, connection=store.connection)
    rows = report(store.connection, budget)["leads"]
    cohort = contact_cohort(store, budget, len(rows), 0, research=True) if rows else []
    names = []
    scored = []
    for company in cohort:
        payload, _ = company_summary(engine, company["candidate_id"])
        score = payload["sales_priority"]["score"]
        if score is None:
            continue
        scored.append({"hostname": payload["company_key"], "score": score,
                       "eligible": company["eligible"]})
        for person in payload["contact_recommendations"]["people"]:
            names.append({
                "hostname": payload["company_key"], "score": score,
                "name": person["name"], "title": person["title"],
                "source_url": person["source_url"], "email": person.get("email"),
                "profile_url": person.get("profile_url"),
                "reachable": bool(person.get("email") or person.get("profile_url")),
            })
    return {"new_domains": len(rows), "scored": scored, "named_contacts": names}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=(
        "estimate", "search", "qualify", "publish-scores", "contacts", "outcomes", "report",
    ))
    parser.add_argument("--database", type=Path)
    parser.add_argument("--budget", required=True)
    parser.add_argument("--cap", type=int, default=120)
    parser.add_argument("--wave", type=int, default=24)
    parser.add_argument("--max-candidates", type=int, default=100)
    parser.add_argument("--market", choices=MARKETS, default="us")
    parser.add_argument("--lane", choices=("both", "hiring", "launch"), default="both")
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(UTC).date())
    parser.add_argument("--confirm-cap", type=int)
    parser.add_argument("--contact-batch", default="first")
    parser.add_argument("--contact-cap", type=int, default=100)
    parser.add_argument("--contact-companies", type=int, default=12)
    parser.add_argument("--min-score", type=int, default=25)
    parser.add_argument("--qualify-companies", type=int, default=16)
    parser.add_argument("--qualify-hosts", nargs="+")
    parser.add_argument("--qualify-concurrency", type=int, default=4)
    parser.add_argument("--core-only", action="store_true")
    args = parser.parse_args()
    if not (1 <= args.wave <= args.cap <= 45_000 and 1 <= args.max_candidates <= 500):
        parser.error("Require 1 <= wave <= cap <= 45000 and 1 <= max-candidates <= 500")
    if not (1 <= args.contact_cap <= 45_000 and 1 <= args.contact_companies <= 100
            and 0 <= args.min_score <= 100 and 1 <= args.qualify_companies <= 100
            and 1 <= args.qualify_concurrency <= 20):
        parser.error("Invalid contact limits")
    if args.mode == "estimate":
        print(json.dumps({"budget": args.budget, "cap": args.cap, "wave": args.wave,
                          "market": args.market, "lane": args.lane,
                          "max_candidates": args.max_candidates,
                          "contact_cap": args.contact_cap,
                          "contact_companies": args.contact_companies}))
        return
    if not args.database or not args.database.is_file():
        parser.error("Specify an existing operational database")
    if args.mode in {"search", "contacts"}:
        if args.confirm_cap != args.cap:
            parser.error("Search requires --confirm-cap matching the estimate")
        api_key = os.getenv("MICROLINK_API_KEY", "").strip()
        if not api_key:
            from beings_leads.web import _load_env_file

            _load_env_file()
            api_key = os.getenv("MICROLINK_API_KEY", "").strip()
        if not api_key:
            parser.error("MICROLINK_API_KEY is not configured")
    readonly = args.mode in {"report", "outcomes", "qualify"}
    with LeadStore(args.database, initialize=False, read_only=readonly) as store:
        if args.mode == "qualify":
            print(json.dumps(qualify(store, args)))
            return
        if args.mode == "publish-scores":
            print(json.dumps(publish_scores(store, args.budget)))
            return
        if args.mode == "outcomes":
            print(json.dumps(outcomes(store, args.budget)))
            return
        if args.mode == "contacts":
            result = asyncio.run(run_contacts(store, args, api_key))
            print(json.dumps(result))
            return
        errors = asyncio.run(run_search(store, args, api_key)) if args.mode == "search" else {}
        print(json.dumps({**report(store.connection, args.budget), "errors": errors}))


if __name__ == "__main__":
    main()
