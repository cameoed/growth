"""Bounded, provider-free source search before company discovery."""
from __future__ import annotations

import asyncio
import hashlib
import random

import httpx

from .async_work import run_thread_owned
from .domain_admission import registered_root
from .domain_policy import is_disallowed
from .models import SourceSuggestion
from .provider_rate_limit import provider_stream
from .source_discovery import (
    SourceDiscoveryLimits,
    canonicalize_public_source_url,
    crawl_public_source,
    source_execution_blockers,
)
from .treg_people import resolve_treg_credentials

ENDPOINT = "tinyfish.web.search"
MAX_QUERIES = 6
MAX_SOURCES = 6
MAX_RESPONSE_BYTES = 2_000_000
THEMES = (
    ("consumer brands", "member directory"),
    ("ecommerce retail", "exhibitor directory"),
    ("mobile gaming apps", "sponsors partners"),
    ("performance marketing", "customer stories"),
    ("creative operations", "customers case studies"),
    ("food beverage beauty", "brand directory"),
    ("consumer products", "award winners"),
    ("retail technology", "partner directory"),
    ("venture consumer", "portfolio companies"),
    ("design advertising", "agency directory"),
    ("subscription brands", "association members"),
    ("digital products", "conference exhibitors"),
    ("manufacturing suppliers", "member companies"),
    ("travel hospitality", "exhibitor list"),
    ("fitness wellness", "brand directory"),
    ("fashion accessories", "stockists brands"),
)
REGIONS = ("United States", "United Kingdom", "Canada", "Australia", "Europe", "global")


def source_queries(parent_id: str, search_terms: list[str] | None = None) -> list[str]:
    """A replayable varied sample, without provider/AI keyword generation."""
    rng = random.Random(hashlib.sha256(parent_id.encode()).hexdigest())
    queries = [
        f'{subject} "{roster}" {rng.choice(REGIONS)} '
        f'{rng.choice(("public", "official", "companies"))}'
        for subject, roster in rng.sample(THEMES, MAX_QUERIES)
    ]

    # Keep the original random draw sequence and leave at least one exploration query.
    for index, term in enumerate((search_terms or [])[:5]):
        _, roster = rng.choice(THEMES)
        queries[index] = f'{term} "{roster}" {rng.choice(REGIONS)} official'
    return queries


def source_from_result(row: object) -> SourceSuggestion | None:
    if not isinstance(row, dict):
        return None
    url = canonicalize_public_source_url(str(row.get("url") or ""))
    if not url or is_disallowed(url):
        return None
    text = f"{row.get('title', '')} {row.get('snippet', '')} {url}".lower()
    if any(word in text for word in ("login", "sign in", "buy database", "email lists")):
        return None
    if not any(word in text for word in (
        "directory", "exhibitor", "members", "sponsors", "customers", "customer stories",
        "case studies", "case-studies", "portfolio companies", "our brands", "award winners",
        "supplier register", "vendor register", "company register", "accredited", "certified",
        "exporters", "company roster", "participating companies", "licensees",
    )):
        return None
    kind = "platform_customer_list" if any(word in text for word in (
        "customers", "customer stories", "case studies", "case-studies"
    )) else "brand_directory"
    if "portfolio companies" in text or "our brands" in text:
        kind = "brand_group_portfolio"
    elif "agency directory" in text:
        kind = "agency_directory"
    suggestion = SourceSuggestion(
        source_kind=kind, url=url, name=str(row.get("title") or url)[:80],
        category="Other",
        discovery_mechanism="Public linked company roster; verify official domains",
        expected_6k_fit="medium", confidence="low",
        rationale=["Search-discovered public roster; membership does not establish buyer fit."],
        evidence_urls=[url],
    )
    return None if source_execution_blockers(suggestion) else suggestion


async def search_sources(client, credentials, query: str, *, request_key: str) -> list:
    headers = {
        "X-Treg-Token": credentials.token,
        "X-Treg-Client": "beings-growth",
        "X-Treg-Meta": "workspace=beings-growth,workflow=source-scout",
        "X-Treg-Route-Max-Cost": "0",
        "Idempotency-Key": request_key,
        "Accept": "application/json",
    }
    if credentials.org:
        headers["X-Treg-Org"] = credentials.org
    try:
        async with provider_stream(client,
            "GET", f"{credentials.base_url}/call/{ENDPOINT}", headers=headers,
            params={"query": query, "domain_type": "web", "language": "en"},
        ) as response:
            if response.status_code >= 400:
                raise RuntimeError(f"Free source search returned HTTP {response.status_code}")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_RESPONSE_BYTES:
                    raise RuntimeError("Free source search exceeded response size limit")
            import json
            payload = json.loads(chunks)
            # Named endpoint only: never issue a paid fallback request.
            if response.headers.get("X-Treg-Cost-Micro") is None:
                raise RuntimeError("Free source search charge could not be confirmed")
            if int(response.headers["X-Treg-Cost-Micro"]):
                raise RuntimeError("Free source search reported an unexpected provider charge")
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list):
                raise RuntimeError("Free source search returned an invalid result contract")
            return results
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError("Free source search transport or response failure") from exc


async def validate_source(client, suggestion) -> bool:
    """Validate an enumerable public roster without AI or paid page hydration."""
    if source_execution_blockers(suggestion):
        return False
    crawled = await crawl_public_source(
        client, suggestion,
        limits=SourceDiscoveryLimits(max_pages=2, max_results=100, max_microlink_pages=0),
    )
    roots = {registered_root(c.hostname) for c in crawled.candidates
             if not is_disallowed(c.hostname)}
    roots.discard("")
    roots.discard(registered_root(suggestion.url))
    return not crawled.errors and len(roots) >= 2


async def scout_free_sources(
    database, parent_id: str, *, checkpoint, event, search_terms=None,
) -> dict:
    from .store import LeadStore
    credentials = resolve_treg_credentials()
    if credentials is None:
        return {"status": "unavailable", "accepted": 0, "reason": "Treg is not configured"}
    created_run_id = None

    def begin():
        nonlocal created_run_id
        with LeadStore(database) as store:
            # Resume once per parent, even after process death before publication.
            rows = store.connection.execute(
                "SELECT status,summary_json FROM source_scout_runs WHERE model=? ORDER BY id DESC",
                (ENDPOINT,),
            )
            import json
            for row in rows:
                previous = json.loads(row["summary_json"] or "{}")
                if (previous.get("parent_id") == parent_id
                        and row["status"] in {"complete", "partial"}):
                    return previous, None, None
            created_run_id = store.start_scout_run(ENDPOINT, 0)
            store.connection.execute(
                "UPDATE source_scout_runs SET summary_json=? WHERE id=?",
                (json.dumps({"parent_id": parent_id}), created_run_id),
            )
            store.connection.commit()
            known_urls = {
                row["url"] for row in store.list_source_suggestions(include_aliases=True)
            }
        return None, created_run_id, known_urls

    try:
        previous, run_id, known = await run_thread_owned(begin)
    except asyncio.CancelledError:
        if created_run_id is not None:
            def cancel_start():
                with LeadStore(database) as store:
                    store.finish_scout_run(created_run_id, "cancelled", {
                        "parent_id": parent_id, "status": "cancelled",
                    })
            await run_thread_owned(cancel_start)
        raise
    if previous is not None:
        return previous
    summary = {"parent_id": parent_id, "provider": ENDPOINT, "queries": [], "accepted": 0,
               "validated": 0, "errors": [], "status": "complete", "cost_usd": 0}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            for index, query in enumerate(source_queries(parent_id, search_terms)):
                await checkpoint()
                event("Searching for new company sources", detail=query)
                summary["queries"].append(query)
                try:
                    results = await search_sources(
                        client, credentials, query,
                        request_key=f"source-scout:{parent_id}:{index}",
                    )
                except RuntimeError as exc:
                    summary["errors"].append(str(exc))
                    break  # A rate/cap/auth outage must not fan out retries.
                for row in results[:10]:
                    await checkpoint()
                    suggestion = source_from_result(row)
                    if not suggestion or suggestion.url in known:
                        continue
                    known.add(suggestion.url)
                    if summary["validated"] >= MAX_SOURCES:
                        continue
                    summary["validated"] += 1
                    try:
                        valid = await validate_source(client, suggestion)
                    except Exception:
                        summary["errors"].append("Public source validation failed")
                        continue
                    if not valid:
                        continue
                    def save(item=suggestion):
                        with LeadStore(database) as store:
                            return store.upsert_source_suggestions(
                                run_id, [item], auto_accept_new=True,
                            )
                    accepted = await run_thread_owned(save)
                    summary["accepted"] += accepted
                    if accepted:
                        event("New company source accepted", detail=suggestion.name)
        if summary["errors"]:
            summary["status"] = "partial"
    except asyncio.CancelledError:
        summary["status"] = "cancelled"
        raise
    except Exception:
        summary["status"] = "failed"
        summary["errors"].append("Free source scout failed before completion")
        raise
    finally:
        def finish():
            with LeadStore(database) as store:
                store.finish_scout_run(run_id, summary["status"], summary)
        await run_thread_owned(finish)
    return summary
