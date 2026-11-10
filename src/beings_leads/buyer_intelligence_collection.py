"""Quote-first, bounded collection of five buyer signals using existing research budgets.

Quotes freeze company identity, request parameters and maximum prices. Receipts/reservations
precede the request. Uncertain calls retain their ceiling and are never silently retried.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from .buyer_intelligence import (
    fingerprint,
    parse_ads,
    parse_instagram,
    parse_posts,
    parse_tiktok,
    parse_workforce,
    rows,
    save_evidence,
)
from .provider_rate_limit import provider_stream
from .sales_engine.models import BudgetReservationRequest
from .sales_engine.persistence import SalesEngineRepository
from .sales_engine.ranking import policy_version
from .store import LeadStore
from .treg_people import resolve_treg_credentials

ENDPOINTS = {
    "google_ads": ("scrapecreators.x.v1-google-company-ads", 1880, 7),
    "linkedin_ads": ("scrapecreators.x.v1-linkedin-ads-search", 1880, 7),
    "linkedin_ad_detail": ("scrapecreators.x.v1-linkedin-ad", 1880, 7),
    "buyer_posts": ("harvestapi.linkedin.post.search", 4000, 7),
    "workforce": ("companyenrich.companies.workforce", 49000, 30),
    "creator_instagram": ("tikhub.x.instagram-v2-fetch-user-tagged-posts", 2000, 14),
}
MAX_RESPONSE_BYTES = 2_000_000
MAX_RUN_MICROUSD = 5_000_000
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS buyer_collection_quotes(
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        maximum_microusd INTEGER NOT NULL, status TEXT NOT NULL,
        plan_json TEXT NOT NULL, error_kind TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS buyer_collection_receipts(
        id TEXT PRIMARY KEY, quote_id TEXT NOT NULL, company_key TEXT NOT NULL,
        capability TEXT NOT NULL, request_key TEXT NOT NULL, observed_at TEXT NOT NULL,
        status TEXT NOT NULL, maximum_microusd INTEGER NOT NULL, actual_microusd INTEGER,
        records INTEGER NOT NULL DEFAULT 0, response_hash TEXT NOT NULL DEFAULT '',
        coverage_json TEXT NOT NULL DEFAULT '{}', error_kind TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS idx_buyer_receipts_due "
    "ON buyer_collection_receipts(company_key,capability,observed_at DESC)",
)


def initialize_schema(connection):
    for statement in SCHEMA:
        connection.execute(statement)


def _profiles(observation):
    social = observation.get("social_urls") or []
    linkedin = next(
        (
            u
            for u in social
            if urlsplit(u).hostname in {"linkedin.com", "www.linkedin.com"}
            and urlsplit(u).path.startswith("/company/")
        ),
        "",
    )
    instagram = next(
        (
            urlsplit(u).path.strip("/").split("/")[0]
            for u in social
            if urlsplit(u).hostname in {"instagram.com", "www.instagram.com"}
            and len(urlsplit(u).path.strip("/").split("/")) == 1
        ),
        "",
    )
    # Brand-owned accounts do not prove external production. Use only creator profiles
    # attributed to a partnership on a fetched first-party page.
    tiktok = set()
    for page in observation.get("pages") or []:
        for evidence in page.get("buyer_evidence") or []:
            profile = (evidence.get("data") or {}).get("discovery_creator_profile", "")
            parts = urlsplit(profile)
            if (
                evidence.get("identity_verified") is True
                and evidence.get("identity_basis") == "first_party_external_creator_attribution"
                and parts.hostname in {"tiktok.com", "www.tiktok.com"}
                and re.fullmatch(r"/@[\w.]+/?", parts.path)
            ):
                tiktok.add(parts.path.strip("/@"))
    tiktok = sorted(tiktok)[:3]
    return linkedin, instagram, tiktok


def _due(connection, company, capability, now):
    days = ENDPOINTS.get(capability, (None, 0, 14))[2]
    return not connection.execute(
        "SELECT 1 FROM buyer_collection_receipts WHERE company_key=? AND capability=? "
        "AND (status IN ('reserved','uncertain') OR observed_at>?) LIMIT 1",
        (company, capability, (now - timedelta(days=days)).isoformat()),
    ).fetchone()


def create_quote(store, *, company_limit=50, maximum_microusd=1_000_000, now=None):
    if type(company_limit) is not int or not 1 <= company_limit <= 250:
        raise ValueError("company_limit must be between 1 and 250")
    if type(maximum_microusd) is not int or not 1 <= maximum_microusd <= MAX_RUN_MICROUSD:
        raise ValueError("Run ceiling must be between $0.000001 and $5")
    now = now or datetime.now(UTC)
    repo = SalesEngineRepository(store.connection)
    repo.initialize_schema()
    initialize_schema(store.connection)
    configured = resolve_treg_credentials() is not None
    tiktok_configured = bool(os.getenv("TIKTOK_COMMERCIAL_ACCESS_TOKEN"))
    if not configured and not tiktok_configured:
        raise ValueError("Configure Treg or TikTok in Settings")
    candidates = store.connection.execute(
        "SELECT company_key,candidate_id,snapshot_id,company_name,score,target_json FROM "
        "engine_company_ranking WHERE policy_version=? AND dirty=0 AND eligible=1 AND excluded=0 "
        "AND as_of<=? AND valid_until>? AND score IS NOT NULL "
        "ORDER BY score DESC,company_key LIMIT ?",
        (policy_version(), now.isoformat(), now.isoformat(), company_limit),
    ).fetchall()
    plan = []
    company_remaining = {}
    spent = 0
    ceiling = min(maximum_microusd, repo.budget_summary()["remaining_microusd"])
    for row in candidates:
        snapshot = store.connection.execute(
            "SELECT observation_json FROM snapshots WHERE id=?", (row["snapshot_id"],)
        ).fetchone()
        if not snapshot:
            continue
        observation = json.loads(snapshot[0])
        linkedin, instagram, tiktok = _profiles(observation)
        target = json.loads(row["target_json"])
        opportunity = target.get("opportunity") or {}
        # Cheap demand signals lead; staffing and creator enrichment refine qualified demand.
        promising = row["score"] >= 40 and opportunity.get("need", 0) >= 30
        company = row["company_key"]
        people = store.treg_people_intelligence(company, as_of=now)
        people_rows = people.get("people", []) if people.get("status") == "fresh" else []
        company_remaining[company] = repo.budget_summary(company_key=company)["account"][
            "remaining_microusd"
        ]
        requests = []
        if configured:
            requests.extend(
                [
                    (
                        "google_ads",
                        {"domain": company, "format": "video", "get_ad_details": "false"},
                        1,
                    ),
                    (
                        "google_ads",
                        {"domain": company, "format": "image", "get_ad_details": "false"},
                        1,
                    ),
                ]
            )
            if linkedin:
                requests.append(
                    (
                        "linkedin_ads",
                        {
                            "company": row["company_name"],
                            "startDate": (now - timedelta(days=90)).date().isoformat(),
                        },
                        4,
                    )
                )
                requests.append(
                    (
                        "buyer_posts",
                        {
                            "authorsCompany": linkedin,
                            "search": (
                                '"looking for" OR "seeking" OR "RFP" OR "appointed" OR "promoted"'
                            ),
                            "sortBy": "date",
                            "postedLimit": "month",
                            "page": 1,
                        },
                        1,
                    )
                )
            if promising:
                requests.append(("workforce", {"domain": company}, 1))
                if instagram:
                    requests.append(("creator_instagram", {"username": instagram}, 1))
        if promising and tiktok_configured and tiktok:
            requests.append(("creator_tiktok", {"creator_usernames": tiktok}, 1))
        for capability, params, calls in requests:
            if not _due(store.connection, company, capability, now):
                continue
            cost = ENDPOINTS.get(capability, ("", 0, 0))[1] * calls
            plan.append(
                {
                    "company_key": company,
                    "candidate_id": row["candidate_id"],
                    "snapshot_id": row["snapshot_id"],
                    "company_name": row["company_name"],
                    "capability": capability,
                    "params": params,
                    "maximum_microusd": cost,
                    "linkedin": linkedin,
                    "instagram": instagram,
                    "people": people_rows[:12],
                    "calls": calls,
                }
            )
    # Cover inexpensive demand/purchase evidence across the cohort before richer follow-ups.
    plan.sort(
        key=lambda work: work["capability"] in {"workforce", "creator_instagram", "creator_tiktok"}
    )
    selected = []
    for work in plan:
        cost, company = work["maximum_microusd"], work["company_key"]
        if cost + spent <= ceiling and cost <= company_remaining[company]:
            selected.append(work)
            spent += cost
            company_remaining[company] -= cost
    plan = selected
    quote_id = uuid4().hex
    store.connection.execute(
        "INSERT INTO buyer_collection_quotes VALUES(?,?,?,?,?,?,?)",
        (
            quote_id,
            now.isoformat(),
            (now + timedelta(minutes=20)).isoformat(),
            spent,
            "quoted",
            json.dumps(plan),
            "",
        ),
    )
    store.connection.commit()
    return quote_status(store.connection, quote_id)


def quote_status(connection, quote_id):
    row = connection.execute(
        "SELECT * FROM buyer_collection_quotes WHERE id=?", (quote_id,)
    ).fetchone()
    if row is None:
        raise LookupError("Buyer intelligence quote not found")
    plan = json.loads(row["plan_json"])
    receipts = connection.execute(
        "SELECT status,COUNT(*) n,SUM(records) records,SUM(actual_microusd) actual "
        "FROM buyer_collection_receipts WHERE quote_id=? GROUP BY status",
        (quote_id,),
    ).fetchall()
    return {
        "id": row["id"],
        "expires_at": row["expires_at"],
        "status": row["status"],
        "maximum_microusd": row["maximum_microusd"],
        "companies": len({r["company_key"] for r in plan}),
        "requests_maximum": sum(r["calls"] for r in plan),
        "capabilities": sorted({r["capability"] for r in plan}),
        "receipts": [dict(r) for r in receipts],
        "error_kind": row["error_kind"],
    }


class ProviderCostOverrun(RuntimeError):
    pass


class BuyerProviderClient:
    def __init__(self, *, client=None, credentials=None):
        self.client = client
        self.credentials = credentials or resolve_treg_credentials()

    async def _request(self, method, address, **kwargs):
        owns = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=45, follow_redirects=False, trust_env=False
        )
        try:
            async with provider_stream(client,method, address, **kwargs) as response:
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise ValueError("Provider response exceeded the byte limit")
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("Provider response is not an object")
                return payload, response.headers
        finally:
            if owns:
                await client.aclose()

    async def verify_prices(self, capabilities):
        for capability in sorted(set(capabilities) - {"creator_tiktok"}):
            endpoint, ceiling, _ = ENDPOINTS[capability]
            payload, _headers = await self._request(
                "GET", "https://treg.to/catalog/endpoints/" + endpoint
            )
            cost = (payload.get("endpoint") or {}).get("cost") or {}
            try:
                current = Decimal(str(cost["usd"])) * 1_000_000
            except (KeyError, InvalidOperation):
                raise ValueError("Provider price unavailable; create a new quote later") from None
            if (
                not current.is_finite()
                or current < 0
                or current > ceiling
                or cost.get("type") not in {"per_call", "per_success", "per_successful_call"}
            ):
                raise ValueError("Provider price changed; collection was not started")

    async def collect(self, capability, params, *, idempotency_key):
        if capability == "creator_tiktok":
            token = os.getenv("TIKTOK_COMMERCIAL_ACCESS_TOKEN", "")
            if not token:
                raise ValueError("TikTok commercial-content access token is missing")
            now = datetime.now(UTC)
            payload, _headers = await self._request(
                "POST",
                "https://open.tiktokapis.com/v2/research/adlib/commercial_content/query/",
                headers={"Authorization": "Bearer " + token},
                params={
                    "fields": "id,create_timestamp,create_date,label,brand_names,creator,videos"
                },
                json={
                    "filters": {
                        "content_published_date_range": {
                            "min": (now - timedelta(days=90)).strftime("%Y%m%d"),
                            "max": now.strftime("%Y%m%d"),
                        },
                        "creator_usernames": params["creator_usernames"],
                    },
                    "max_count": 20,
                },
            )
            if (payload.get("error") or {}).get("code") != "ok":
                raise ValueError("TikTok commercial-content query failed")
            return payload, 0
        if not self.credentials:
            raise ValueError("Treg is not configured")
        endpoint = ENDPOINTS[capability][0]
        headers = {
            "X-Treg-Token": self.credentials.token,
            "Idempotency-Key": idempotency_key,
            "X-Treg-Client": "beings-growth",
            "X-Treg-Meta": "workspace=beings-growth,workflow=buyer-intelligence",
            "X-Treg-Route-Max-Cost": str(ENDPOINTS[capability][1] / 1_000_000),
        }
        if self.credentials.org:
            headers["X-Treg-Org"] = self.credentials.org
        payload, response_headers = await self._request(
            "GET", self.credentials.base_url + "/call/" + endpoint, headers=headers, params=params
        )
        if (
            payload.get("success") is False
            or payload.get("status") in {"error", "failed"}
            or (isinstance(payload.get("status"), int) and payload["status"] >= 400)
            or (isinstance(payload.get("code"), int) and payload["code"] >= 400)
        ):
            raise ValueError("Provider reported an unsuccessful result")
        # Missing charge reporting remains unknown, not free.
        cost = response_headers.get("X-Treg-Cost-Micro")
        return payload, int(cost) if cost is not None else None


def parse_result(capability, payload, work, now):
    company = work["company_key"]
    if capability in {"google_ads", "linkedin_ads", "linkedin_ad_detail"}:
        if capability == "linkedin_ad_detail" and "ads" not in payload:
            payload = {"ads": [payload]}
        return parse_ads(
            payload,
            company,
            platform="google" if capability == "google_ads" else "linkedin",
            observed_at=now,
            company_profile=work["linkedin"],
        )
    if capability == "buyer_posts":
        return parse_posts(
            payload,
            company,
            observed_at=now,
            company_profile=work["linkedin"],
            people=work["people"],
        )
    if capability == "workforce":
        parsed = parse_workforce(payload, company, observed_at=now)
        production_roles = [
            {"name": p.get("name"), "title": p.get("title"), "profile_url": p.get("linkedin_url")}
            for p in work["people"]
            if p.get("accepted")
            and p.get("company_domain") == company
            and re.search(
                r"designer|video|motion|creative director|producer", p.get("title", ""), re.I
            )
        ]
        return [
            item.model_copy(
                update={
                    "data": {
                        **item.data,
                        "observed_production_roles": production_roles
                        if item.effective_at == now
                        else [],
                    }
                }
            )
            for item in parsed
        ]
    if capability == "creator_instagram":
        return parse_instagram(payload, company, observed_at=now, handle=work["instagram"])
    return parse_tiktok(payload, company, observed_at=now, brand_names=[work["company_name"]])


def _claim_quote(store, quote_id, maximum, now):
    store.connection.execute("BEGIN IMMEDIATE")
    try:
        row = store.connection.execute(
            "SELECT * FROM buyer_collection_quotes WHERE id=?", (quote_id,)
        ).fetchone()
        if not row:
            raise LookupError("Buyer intelligence quote not found")
        if row["maximum_microusd"] != maximum:
            raise ValueError("Confirm the exact quoted maximum")
        if row["status"] != "quoted":
            raise ValueError("This quote was already started; no duplicate requests sent")
        if row["expires_at"] <= now.isoformat():
            raise ValueError("Quote expired; create a new quote")
        store.connection.execute(
            "UPDATE buyer_collection_quotes SET status='running' WHERE id=?", (quote_id,)
        )
        store.connection.commit()
        return json.loads(row["plan_json"])
    except BaseException:
        store.connection.rollback()
        raise


async def run_quote(
    database_path, quote_id, confirmed_maximum_microusd, *, provider=None, prepared_plan=None
):
    provider = provider or BuyerProviderClient()
    with LeadStore(database_path) as store:
        repo = SalesEngineRepository(store.connection)
        repo.initialize_schema()
        initialize_schema(store.connection)
        store.connection.commit()
        plan = (
            prepared_plan
            if prepared_plan is not None
            else _claim_quote(store, quote_id, confirmed_maximum_microusd, datetime.now(UTC))
        )
    try:
        capabilities = {w["capability"] for w in plan}
        if "linkedin_ads" in capabilities:
            capabilities.add("linkedin_ad_detail")
        await provider.verify_prices(capabilities)
        failed = False
        cancelled = False
        for work in plan:
            capability = work["capability"]
            with LeadStore(database_path) as store:
                control = store.connection.execute(
                    "SELECT status FROM buyer_collection_quotes WHERE id=?", (quote_id,)
                ).fetchone()[0]
                if control == "cancel_requested":
                    cancelled = True
                    break
                # Identity may have changed since the quote. Never hydrate the replacement host.
                identity = store.connection.execute(
                    "SELECT company_key FROM engine_ranking_members WHERE candidate_id=?",
                    (work["candidate_id"],),
                ).fetchone()
                if not identity or identity[0] != work["company_key"]:
                    continue
            payload = await _execute(
                database_path, quote_id, work, capability, work["params"], provider
            )
            if payload is None:
                failed = True
                continue
            if capability == "linkedin_ads":
                # Bounded details supply dates/landing-domain attribution missing on search pages.
                for row in rows(payload, ("ads",))[:3]:
                    source = row.get("url") or ""
                    if source.startswith("https://www.linkedin.com/ad-library/detail/"):
                        detail = await _execute(
                            database_path,
                            quote_id,
                            work,
                            "linkedin_ad_detail",
                            {"url": source},
                            provider,
                        )
                        failed = failed or detail is None
        with LeadStore(database_path) as store:
            cancelled = (
                cancelled
                or store.connection.execute(
                    "SELECT status FROM buyer_collection_quotes WHERE id=?", (quote_id,)
                ).fetchone()[0]
                == "cancel_requested"
            )
            store.connection.execute(
                "UPDATE buyer_collection_quotes SET status=? WHERE id=?",
                ("cancelled" if cancelled else "partial" if failed else "complete", quote_id),
            )
            store.connection.commit()
            return quote_status(store.connection, quote_id)
    except BaseException as exc:
        with LeadStore(database_path) as store:
            store.connection.execute(
                "UPDATE buyer_collection_quotes SET status='failed',error_kind=? WHERE id=?",
                (type(exc).__name__, quote_id),
            )
            store.connection.commit()
        raise


async def _execute(path, quote_id, work, capability, params, provider):
    now = datetime.now(UTC)
    company = work["company_key"]
    request_key = fingerprint(company, capability, params)
    key = "buyer:" + fingerprint(quote_id, request_key)
    maximum = ENDPOINTS.get(capability, ("", 0, 0))[1]
    with LeadStore(path) as store:
        repo = SalesEngineRepository(store.connection)
        repo.initialize_schema()
        # Cross-quote lock prevents concurrent quotes from charging the same cached request.
        store.connection.execute("BEGIN IMMEDIATE")
        try:
            recent = store.connection.execute(
                "SELECT 1 FROM buyer_collection_receipts WHERE request_key=? "
                "AND (status IN ('reserved','uncertain') OR observed_at>?)",
                (
                    request_key,
                    (now - timedelta(days=ENDPOINTS.get(capability, (None, 0, 14))[2])).isoformat(),
                ),
            ).fetchone()
            if recent:
                store.connection.rollback()
                return {}
            control = store.connection.execute(
                "SELECT status FROM buyer_collection_quotes WHERE id=?", (quote_id,)
            ).fetchone()[0]
            if control != "running":
                store.connection.rollback()
                return {}
            quote_maximum = store.connection.execute(
                "SELECT maximum_microusd FROM buyer_collection_quotes WHERE id=?", (quote_id,)
            ).fetchone()[0]
            committed = store.connection.execute(
                "SELECT COALESCE(SUM(maximum_microusd),0) FROM buyer_collection_receipts "
                "WHERE quote_id=?",
                (quote_id,),
            ).fetchone()[0]
            if committed + maximum > quote_maximum:
                raise ValueError("Quoted run ceiling exhausted")
            if maximum:
                repo.reserve_budget(
                    BudgetReservationRequest(
                        idempotency_key=key,
                        company_key=company,
                        provider="treg",
                        purpose="Buyer evidence: " + capability,
                        maximum_microusd=maximum,
                    )
                )
            store.connection.execute(
                "INSERT INTO buyer_collection_receipts(id,quote_id,company_key,capability,"
                "request_key,observed_at,status,maximum_microusd) VALUES(?,?,?,?,?,?,?,?)",
                (
                    key,
                    quote_id,
                    company,
                    capability,
                    request_key,
                    now.isoformat(),
                    "reserved",
                    maximum,
                ),
            )
            store.connection.commit()
        except BaseException:
            store.connection.rollback()
            raise
    try:
        payload, cost = await provider.collect(capability, params, idempotency_key=key)
        if cost is not None and (type(cost) is not int or cost < 0 or cost > maximum):
            with LeadStore(path) as store:
                if type(cost) is int and cost >= 0:
                    # Record the actual provider debit honestly while preserving the approved
                    # maximum. This is an overrun receipt, never permission to continue spending.
                    store.connection.execute(
                        "UPDATE buyer_collection_receipts SET actual_microusd=? WHERE id=?",
                        (cost, key),
                    )
                    if maximum:
                        SalesEngineRepository(store.connection).settle_budget(key, maximum)
                    # The research budget sums the excess recorded in this receipt separately;
                    # the original authorized reservation remains unchanged.
                store.connection.commit()
            raise ProviderCostOverrun("Provider charge exceeded the reserved ceiling")
        evidence = parse_result(capability, payload, work, now)
        with LeadStore(path) as store:
            repo = SalesEngineRepository(store.connection)
            if cost is not None and maximum:
                repo.settle_budget(key, cost)
            count = save_evidence(store.connection, evidence)
            store.connection.execute(
                "UPDATE buyer_collection_receipts SET status=?,actual_microusd=?,records=?,"
                "response_hash=?,coverage_json=? WHERE id=?",
                (
                    "success" if evidence else "empty",
                    cost,
                    count,
                    fingerprint(payload),
                    json.dumps(
                        {
                            "coverage": "partial",
                            "sample_limit": 200,
                            "absence_interpretable": False,
                            "identity_verified_records": sum(r.identity_verified for r in evidence),
                            "cost_reconciled": cost is not None,
                        }
                    ),
                    key,
                ),
            )
            store.connection.commit()
        return payload
    except Exception as exc:
        with LeadStore(path) as store:
            store.connection.execute(
                "UPDATE buyer_collection_receipts SET status='uncertain',error_kind=? WHERE id=?",
                (type(exc).__name__, key),
            )
            store.connection.commit()
        if isinstance(exc, ProviderCostOverrun):
            raise
        return None
