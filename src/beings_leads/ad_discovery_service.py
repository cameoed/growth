from __future__ import annotations

import asyncio
import math
import os
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .ad_intelligence import (
    META_ACTOR_ID,
    TIKTOK_LIBRARY_ACTOR_ID,
    TIKTOK_TOP_ACTOR_ID,
    AdDiscoveryRequest,
    AdRateCard,
    AdScanQuote,
    ApifyHTTPAdapter,
    ApifyRun,
    IdentityAssessment,
    NormalizedAd,
    NormalizedAdvertiser,
    assess_advertiser_identity,
    build_meta_actor_input,
    build_meta_page_url,
    build_meta_search_url,
    default_rate_cards,
    normalize_meta_ad,
    normalize_tiktok_ad,
    normalize_tiktok_top_ad,
    registrable_domain,
    social_handle_from_url,
)
from .config import USER_AGENT
from .domain_policy import is_disallowed
from .free_intelligence import enrich_site_owned_intelligence
from .inspect import inspect_host
from .language import is_english_eligible
from .models import AdIntelligence, DiscoveredHost, SiteObservation
from .naming import concise_company_name
from .rehydration import REHYDRATION_INTERVAL
from .scoring import blend_ai_score, score_candidate
from .store import COMPANY_SCAN_FRESH_HOURS, LeadStore, observation_completes_company_work

MIN_PROVIDER_CALL_CAP_USD = Decimal("0.01")
PROVIDER_CAP_PRECISION = Decimal("0.0001")
META_PROVIDER_READ_FRESHNESS = REHYDRATION_INTERVAL
BLOCKED_DISCOVERY_DOMAINS = frozenset(
    {
        "amazon.com",
        "apps.apple.com",
        "bit.ly",
        "facebook.com",
        "instagram.com",
        "linktr.ee",
        "lnk.bio",
        "play.google.com",
        "shop.app",
        "tiktok.com",
        "walmart.com",
        "wa.me",
        "youtube.com",
    }
)


class PaidRunCancelled(RuntimeError):
    pass


class PaidBudgetExhausted(RuntimeError):
    pass


def _microusd(value: Decimal | int | float | str | None) -> int:
    if value is None:
        return 0
    return max(0, int((Decimal(str(value)) * Decimal(1_000_000)).to_integral_value()))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _raw_input_url(item: Mapping[str, Any]) -> str:
    for key in ("inputUrl", "input_url", "sourceUrl", "source_url", "url"):
        value = item.get(key)
        if isinstance(value, str) and "facebook.com/ads/library" in value:
            return value
    return ""


def _ad_evidence_url(ad: NormalizedAd) -> str:
    if ad.platform == "meta":
        return f"https://www.facebook.com/ads/library/?id={ad.external_id}"
    if ad.surface == "tiktok_library":
        return f"https://library.tiktok.com/ads/detail/?ad_id={ad.external_id}"
    return "https://ads.tiktok.com/business/creativecenter/inspiration/topads/"


def _first_party_handles(urls: Sequence[str]) -> dict[str, list[str]]:
    handles: dict[str, list[str]] = defaultdict(list)
    for url in urls:
        for source, target in (
            ("meta", "facebook"),
            ("instagram", "instagram"),
            ("tiktok", "tiktok"),
        ):
            value = social_handle_from_url(source, url)  # type: ignore[arg-type]
            if value and value not in handles[target]:
                handles[target].append(value)
    return dict(handles)


def _candidate_names(
    candidate: Mapping[str, Any], observation: SiteObservation | None
) -> list[str]:
    values = [
        str(candidate.get("company_name") or ""),
        str(candidate.get("label_hint") or ""),
        observation.title if observation else "",
    ]
    domain = registrable_domain(str(candidate.get("canonical_domain") or ""))
    if domain:
        values.append(domain.split(".", 1)[0].replace("-", " "))
    expanded: list[str] = []
    for value in values:
        cleaned = " ".join(value.split()).strip()
        if not cleaned:
            continue
        expanded.append(cleaned)
        for separator in (" | ", " — ", " – ", " - ", ": "):
            if separator in cleaned:
                expanded.append(cleaned.split(separator, 1)[0].strip())
    return list(dict.fromkeys(value for value in expanded if value))


def _best_identity_assessment(
    *,
    canonical_domain: str,
    advertiser: NormalizedAdvertiser,
    observed_landing_domains: Sequence[str],
    candidate: Mapping[str, Any],
    observation: SiteObservation | None,
) -> IdentityAssessment:
    social_urls = list(candidate.get("social_urls") or [])
    if observation:
        social_urls.extend(observation.social_urls)
    handles = _first_party_handles(social_urls)
    assessments = [
        assess_advertiser_identity(
            canonical_domain=canonical_domain,
            advertiser=advertiser,
            observed_landing_domains=observed_landing_domains,
            candidate_name=name,
            first_party_handles=handles,
        )
        for name in (_candidate_names(candidate, observation) or [""])
    ]
    state_rank = {"verified": 4, "probable": 3, "ambiguous": 2, "rejected": 1}
    return max(
        assessments,
        key=lambda value: (state_rank.get(value.state, 0), value.confidence),
    )


@dataclass
class QueryMission:
    query_id: int
    platform: str
    surface: str
    phase: str
    query: str
    country: str
    input_url: str
    result_cap: int
    estimated_cost_microusd: int
    query_key: str
    seed_candidate_id: int | None = None


@dataclass
class StoredAd:
    ad: NormalizedAd
    advertiser_id: int
    creative_id: int
    query_id: int


@dataclass
class BudgetLedger:
    approved: Decimal
    allocated: Decimal = Decimal("0")
    allocations: list[dict[str, str]] = field(default_factory=list)

    @property
    def remaining(self) -> Decimal:
        return max(Decimal("0"), self.approved - self.allocated)

    def reserve(self, label: str, planned: Decimal) -> Decimal:
        desired = max(MIN_PROVIDER_CALL_CAP_USD, planned)
        desired = desired.quantize(PROVIDER_CAP_PRECISION, rounding=ROUND_DOWN)
        if desired < MIN_PROVIDER_CALL_CAP_USD:
            desired = MIN_PROVIDER_CALL_CAP_USD
        allocation = min(self.remaining, desired)
        allocation = allocation.quantize(PROVIDER_CAP_PRECISION, rounding=ROUND_DOWN)
        if allocation < MIN_PROVIDER_CALL_CAP_USD:
            raise PaidBudgetExhausted(
                f"No safe provider-call allocation remains for {label}; increase the run cap"
            )
        self.allocated += allocation
        self.allocations.append({"label": label, "maximum_charge_usd": format(allocation, "f")})
        return allocation


class AdDiscoveryExecutor:
    """Execute one explicitly approved paid-ad run with no retry or scheduling behavior."""

    def __init__(
        self,
        db_path: str | Path,
        token: str,
        *,
        cancel_event: asyncio.Event | None = None,
        adapter_factory: Callable[[str], ApifyHTTPAdapter] = ApifyHTTPAdapter,
        active_run_callback: Callable[[ApifyHTTPAdapter | None, str | None], None] | None = None,
        seed_candidate_ids: list[int] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.token = token
        self.cancel_event = cancel_event or asyncio.Event()
        self.adapter_factory = adapter_factory
        self.active_run_callback = active_run_callback
        self.seed_candidate_ids = seed_candidate_ids
        self.provider_run_ids: list[str] = []
        self.provider_dataset_ids: list[str] = []
        self.provider_reported_cost = Decimal("0")
        self.warehouse_queries_reused = 0
        self.errors: list[str] = []
        self._query_missions: dict[int, QueryMission] = {}

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise PaidRunCancelled("Paid-ad run cancelled by operator")

    @staticmethod
    def _planned_charge(card: AdRateCard, max_items: int) -> Decimal:
        known = card.start_cost(1) + card.result_price_usd * max_items
        if not card.platform_usage_included:
            known *= Decimal("1.5")
        return max(known, MIN_PROVIDER_CALL_CAP_USD)

    def _update_run(self, store: LeadStore, run_id: int, **updates: Any) -> None:
        store.update_ad_discovery_run(
            run_id,
            provider_run_ids=self.provider_run_ids,
            provider_dataset_ids=self.provider_dataset_ids,
            actual_cost_microusd=_microusd(self.provider_reported_cost),
            **updates,
        )

    def _refresh_run_progress(
        self,
        store: LeadStore,
        *,
        candidates_promoted: int | None = None,
    ) -> None:
        query_rows = store.list_ad_discovery_queries(self._run_id)
        item_counts = store.connection.execute(
            """
            SELECT COUNT(DISTINCT advertiser_id) AS advertisers,
                   COUNT(DISTINCT creative_id) AS creatives
            FROM ad_run_items WHERE run_id=?
            """,
            (self._run_id,),
        ).fetchone()
        verified_links = store.connection.execute(
            """
            SELECT COUNT(DISTINCT l.id)
            FROM advertiser_domain_links l
            JOIN ad_run_items i ON i.advertiser_id=l.advertiser_id
            WHERE i.run_id=? AND l.state='verified' AND l.confidence>=80
            """,
            (self._run_id,),
        ).fetchone()
        updates: dict[str, Any] = {
            "queries_total": len(query_rows),
            "queries_completed": sum(
                row["status"] in {"complete", "partial", "warehouse_fresh"}
                for row in query_rows
            ),
            "advertisers_found": int(item_counts["advertisers"] or 0),
            "creatives_found": int(item_counts["creatives"] or 0),
            "links_verified": int(verified_links[0] or 0),
        }
        if candidates_promoted is not None:
            updates["candidates_promoted"] = candidates_promoted
        self._update_run(store, self._run_id, **updates)

    def _create_query(
        self,
        store: LeadStore,
        run_id: int,
        *,
        platform: str,
        surface: str,
        phase: str,
        query: str,
        country: str,
        input_url: str,
        result_cap: int,
        estimated_cost: Decimal,
        query_key: str,
        seed_candidate_id: int | None = None,
    ) -> QueryMission:
        query_id = store.upsert_ad_discovery_query(
            run_id,
            platform=platform,
            surface=surface,
            phase=phase,
            query=query,
            country=country,
            input_url=input_url,
            result_cap=result_cap,
            estimated_cost_microusd=_microusd(estimated_cost),
            status="queued",
            query_key=query_key,
        )
        mission = QueryMission(
            query_id=query_id,
            platform=platform,
            surface=surface,
            phase=phase,
            query=query,
            country=country,
            input_url=input_url,
            result_cap=result_cap,
            estimated_cost_microusd=_microusd(estimated_cost),
            query_key=query_key,
            seed_candidate_id=seed_candidate_id,
        )
        self._query_missions[query_id] = mission
        return mission

    def _save_query_state(
        self,
        store: LeadStore,
        mission: QueryMission,
        *,
        status: str,
        result_count: int = 0,
        unique_advertisers: int = 0,
        actual_cost_microusd: int = 0,
        provider_run_id: str = "",
        provider_dataset_id: str = "",
        error: str = "",
    ) -> None:
        store.upsert_ad_discovery_query(
            self._run_id,
            platform=mission.platform,
            surface=mission.surface,
            phase=mission.phase,
            query=mission.query,
            country=mission.country,
            input_url=mission.input_url,
            result_cap=mission.result_cap,
            result_count=result_count,
            unique_advertisers=unique_advertisers,
            estimated_cost_microusd=mission.estimated_cost_microusd,
            actual_cost_microusd=actual_cost_microusd,
            status=status,
            provider_run_id=provider_run_id,
            provider_dataset_id=provider_dataset_id,
            error=error,
            query_key=mission.query_key,
        )

    def _reuse_fresh_query(
        self,
        store: LeadStore,
        *,
        platform: str,
        surface: str,
        phase: str,
        query: str,
        country: str,
        input_url: str,
        result_cap: int,
        estimated_cost: Decimal,
        query_key: str,
        seed_candidate_id: int | None = None,
    ) -> bool:
        """Record a warehouse hit and return without crossing the provider boundary."""

        previous = store.recent_completed_ad_query(
            platform=platform,
            surface=surface,
            phase=phase,
            country=country,
            input_url=input_url,
            minimum_result_cap=result_cap,
            fresh_for=META_PROVIDER_READ_FRESHNESS,
            exclude_run_id=self._run_id,
        )
        if previous is None:
            return False
        mission = self._create_query(
            store,
            self._run_id,
            platform=platform,
            surface=surface,
            phase=phase,
            query=query,
            country=country,
            input_url=input_url,
            result_cap=result_cap,
            estimated_cost=estimated_cost,
            query_key=query_key,
            seed_candidate_id=seed_candidate_id,
        )
        self._save_query_state(
            store,
            mission,
            status="warehouse_fresh",
            result_count=int(previous["result_count"]),
            unique_advertisers=int(previous["unique_advertisers"]),
        )
        self.warehouse_queries_reused += 1
        self._refresh_run_progress(store)
        return True

    async def _call_actor(
        self,
        adapter: ApifyHTTPAdapter,
        store: LeadStore,
        ledger: BudgetLedger,
        *,
        label: str,
        actor_id: str,
        actor_input: Mapping[str, Any],
        max_items: int,
        card: AdRateCard,
        missions: Sequence[QueryMission],
    ) -> tuple[list[Mapping[str, Any]], ApifyRun]:
        self._check_cancelled()
        try:
            call_cap = ledger.reserve(label, self._planned_charge(card, max_items))
        except PaidBudgetExhausted as exc:
            for mission in missions:
                self._save_query_state(
                    store,
                    mission,
                    status="skipped_budget",
                    error=str(exc),
                )
            self._refresh_run_progress(store)
            raise
        for mission in missions:
            self._save_query_state(store, mission, status="running")
        started: ApifyRun | None = None
        finished: ApifyRun | None = None
        try:
            started = await adapter.start_actor(
                actor_id,
                actor_input,
                max_items=max_items,
                max_total_charge_usd=call_cap,
                timeout_seconds=3_600,
            )
            self.provider_run_ids.append(started.id)
            if started.default_dataset_id:
                self.provider_dataset_ids.append(started.default_dataset_id)
            if self.active_run_callback:
                self.active_run_callback(adapter, started.id)
            self._update_run(store, self._run_id)
            finished = await adapter.wait_for_run(started.id, timeout_seconds=3_900)
            if finished.usage_total_usd is not None:
                self.provider_reported_cost += finished.usage_total_usd
            dataset_id = finished.default_dataset_id or started.default_dataset_id
            if dataset_id and dataset_id not in self.provider_dataset_ids:
                self.provider_dataset_ids.append(dataset_id)
            items = (
                await adapter.fetch_dataset_items(dataset_id, maximum_items=max_items)
                if dataset_id
                else []
            )
            self._check_cancelled()
        except (PaidRunCancelled, asyncio.CancelledError) as exc:
            if started is not None and (finished is None or not finished.terminal):
                try:
                    await adapter.abort_run(started.id)
                except Exception as abort_exc:
                    self.errors.append(
                        f"{label} abort: {type(abort_exc).__name__}: {abort_exc}"
                    )
            status = "cancelled" if isinstance(exc, PaidRunCancelled) else "interrupted"
            for mission in missions:
                self._save_query_state(
                    store,
                    mission,
                    status=status,
                    actual_cost_microusd=(
                        _microusd(finished.usage_total_usd) // max(len(missions), 1)
                        if finished and finished.usage_total_usd is not None
                        else 0
                    ),
                    provider_run_id=started.id if started else "",
                    provider_dataset_id=(
                        (finished.default_dataset_id if finished else None)
                        or (started.default_dataset_id if started else None)
                        or ""
                    ),
                    error=str(exc),
                )
            self._refresh_run_progress(store)
            raise
        except Exception as exc:
            if started is not None and (finished is None or not finished.terminal):
                try:
                    await adapter.abort_run(started.id)
                except Exception as abort_exc:
                    self.errors.append(
                        f"{label} abort: {type(abort_exc).__name__}: {abort_exc}"
                    )
            error = f"{type(exc).__name__}: {exc}"[:4_000]
            self.errors.append(f"{label}: {error}")
            for mission in missions:
                self._save_query_state(
                    store,
                    mission,
                    status="failed",
                    actual_cost_microusd=(
                        _microusd(finished.usage_total_usd) // max(len(missions), 1)
                        if finished and finished.usage_total_usd is not None
                        else 0
                    ),
                    provider_run_id=started.id if started else "",
                    provider_dataset_id=(
                        (finished.default_dataset_id if finished else None)
                        or (started.default_dataset_id if started else None)
                        or ""
                    ),
                    error=error,
                )
            self._refresh_run_progress(store)
            raise
        finally:
            if self.active_run_callback:
                self.active_run_callback(None, None)
        # Assigned inside the successful branch above; exceptions always re-raise.
        assert started is not None and finished is not None
        per_query_cost = (
            _microusd(finished.usage_total_usd) // max(len(missions), 1)
            if finished.usage_total_usd is not None
            else 0
        )
        status = "complete" if finished.succeeded else "partial" if items else "failed"
        error = "" if finished.succeeded else finished.status_message or finished.status
        # Preserve query attribution while visiting each dataset item only once.
        items_by_input: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for item in items:
            items_by_input[_raw_input_url(item)].append(item)
        # Preserve every micro-dollar when one provider receipt spans queries.
        remainder = (
            _microusd(finished.usage_total_usd) % max(len(missions), 1)
            if finished.usage_total_usd is not None else 0
        )
        for index, mission in enumerate(missions):
            matched = items_by_input.get(mission.input_url, [])
            scoped = matched if matched else (items if len(missions) == 1 else [])
            advertisers = {
                str(
                    item.get("pageID")
                    or item.get("pageId")
                    or item.get("advertiserId")
                    or ""
                )
                for item in scoped
            }
            advertisers.discard("")
            self._save_query_state(
                store,
                mission,
                status=status,
                result_count=len(scoped),
                unique_advertisers=len(advertisers),
                actual_cost_microusd=per_query_cost + int(index < remainder),
                provider_run_id=finished.id,
                provider_dataset_id=dataset_id or "",
                error=error,
            )
        if not finished.succeeded:
            self.errors.append(f"{label}: {error}")
        self._refresh_run_progress(store)
        return items, finished

    @staticmethod
    def _mission_for_item(
        item: Mapping[str, Any], missions: Sequence[QueryMission]
    ) -> QueryMission:
        input_url = _raw_input_url(item)
        return next(
            (mission for mission in missions if mission.input_url == input_url),
            missions[0],
        )

    def _persist_ads(
        self,
        store: LeadStore,
        run_id: int,
        items: Sequence[Mapping[str, Any]],
        missions: Sequence[QueryMission],
        normalizer: Callable[[Mapping[str, Any]], NormalizedAd | None],
        *,
        coverage_type: str,
    ) -> list[StoredAd]:
        stored: list[StoredAd] = []
        missions_by_input: dict[str, QueryMission] = {}
        for mission in missions:
            # Match the existing first-mission policy for duplicate input URLs.
            missions_by_input.setdefault(mission.input_url, mission)
        for item in items:
            ad = normalizer(item)
            if ad is None or not ad.advertiser.external_id:
                continue
            if any(is_disallowed(value) for value in [*ad.landing_domains, *ad.landing_urls]):
                continue
            mission = missions_by_input.get(_raw_input_url(item)) or missions[0]
            advertiser_id = store.upsert_advertiser_entity(
                platform=ad.advertiser.platform,
                external_id=ad.advertiser.external_id,
                display_name=ad.advertiser.name,
                handle=ad.advertiser.handle,
                profile_url=ad.advertiser.profile_url or "",
                linked_instagram_handle=ad.advertiser.linked_instagram_handle,
                coverage_type=coverage_type,
                provenance={
                    "source_payload_hash": ad.source_payload_hash,
                    "categories": ad.advertiser.categories,
                },
            )
            source_url = _ad_evidence_url(ad)
            creative_id = store.upsert_ad_creative(
                advertiser_id,
                platform=ad.platform,
                external_id=ad.external_id,
                creative_group_id=ad.collation_id or ad.creative_fingerprint,
                creative_fingerprint=ad.creative_fingerprint,
                is_active=ad.is_active is True,
                started_at=_iso(ad.started_at),
                ended_at=_iso(ad.ended_at),
                publisher_platforms=ad.publisher_platforms,
                body=ad.body,
                headline=ad.headline,
                cta=ad.cta,
                landing_url=ad.landing_urls[0] if ad.landing_urls else "",
                landing_host=(
                    urlparse(ad.landing_urls[0]).hostname or ""
                    if ad.landing_urls
                    else ""
                ),
                landing_domain=ad.landing_domains[0] if ad.landing_domains else "",
                media_type=ad.media_type,
                source_url=source_url,
                coverage_type=coverage_type,
                provenance={"source_payload_hash": ad.source_payload_hash,
                            "media_urls": ad.media_urls},
            )
            store.record_ad_run_item(
                run_id,
                mission.query_id,
                advertiser_id=advertiser_id,
                creative_id=creative_id,
                provider_item_id=ad.external_id,
                active=ad.is_active,
                payload={
                    "surface": ad.surface,
                    "source_payload_hash": ad.source_payload_hash,
                    "landing_domains": ad.landing_domains,
                },
            )
            stored.append(
                StoredAd(
                    ad=ad,
                    advertiser_id=advertiser_id,
                    creative_id=creative_id,
                    query_id=mission.query_id,
                )
            )
        return stored

    async def _inspect_domain(
        self,
        client: httpx.AsyncClient,
        domain: str,
        advertiser: NormalizedAdvertiser,
    ) -> SiteObservation:
        candidate = DiscoveredHost(
            source_apex="meta-ad-library",
            hostname=domain,
            label_hint=advertiser.name,
            discovery_evidence_urls=[advertiser.profile_url] if advertiser.profile_url else [],
            provider_record_ids=[f"{advertiser.platform}:{advertiser.external_id}"],
        )
        return await self._inspect_candidate(client, candidate, pages_per_site=3)

    @staticmethod
    async def _inspect_candidate(
        client: httpx.AsyncClient,
        candidate: DiscoveredHost,
        *,
        pages_per_site: int,
    ) -> SiteObservation:
        options: dict[str, Any] = {"pages_per_site": pages_per_site}
        if microlink_key := os.getenv("MICROLINK_API_KEY", "").strip():
            options["microlink_api_key"] = microlink_key
        return await inspect_host(client, candidate, **options)

    async def _resolve_meta_identities(
        self,
        store: LeadStore,
        client: httpx.AsyncClient,
        request: AdDiscoveryRequest,
        stored_ads: Sequence[StoredAd],
    ) -> tuple[dict[int, set[int]], dict[int, Mapping[str, Any]], int]:
        by_advertiser: dict[int, list[NormalizedAd]] = defaultdict(list)
        for item in stored_ads:
            by_advertiser[item.advertiser_id].append(item.ad)
        advertiser_candidates: dict[int, set[int]] = defaultdict(set)
        candidates: dict[int, Mapping[str, Any]] = {}
        inspected: dict[str, SiteObservation] = {}
        promoted = 0
        checked_new_advertisers = 0
        new_advertiser_limit = (
            request.max_new_advertisers
            if request.mode in {"discover", "both"}
            else 0
        )
        for advertiser_id, ads in by_advertiser.items():
            self._check_cancelled()
            advertiser = ads[0].advertiser
            domains = list(
                dict.fromkeys(domain for ad in ads for domain in ad.landing_domains if domain)
            )
            for domain in domains:
                normalized_domain = registrable_domain(domain)
                if not normalized_domain or is_disallowed(domain):
                    continue
                blocked = normalized_domain in BLOCKED_DISCOVERY_DOMAINS
                existing = store.find_candidate_by_canonical_domain(normalized_domain)
                observation: SiteObservation | None = None
                site_confirmed = existing is not None
                if existing is None and not blocked:
                    if checked_new_advertisers >= new_advertiser_limit:
                        continue
                    checked_new_advertisers += 1
                    if request.verify_landing_domains:
                        try:
                            observation = inspected.get(normalized_domain)
                            if observation is None:
                                observation = await self._inspect_domain(
                                    client, normalized_domain, advertiser
                                )
                                inspected[normalized_domain] = observation
                            site_confirmed = bool(
                                observation.live and is_english_eligible(observation)
                            )
                        except Exception as exc:
                            self.errors.append(
                                f"identity inspection {normalized_domain}: "
                                f"{type(exc).__name__}: {exc}"
                            )
                    else:
                        site_confirmed = False
                candidate: Mapping[str, Any] = existing or {
                    "canonical_domain": normalized_domain,
                    "company_name": concise_company_name(
                        label_hint=advertiser.name,
                        title=observation.title if observation else "",
                        canonical_domain=normalized_domain,
                        hostname=normalized_domain,
                    ),
                    "label_hint": advertiser.name,
                    "social_urls": observation.social_urls if observation else [],
                }
                assessment = _best_identity_assessment(
                    canonical_domain=normalized_domain,
                    advertiser=advertiser,
                    observed_landing_domains=(
                        [normalized_domain]
                        if site_confirmed or existing is not None
                        else []
                    ),
                    candidate=candidate,
                    observation=observation,
                )
                if blocked:
                    assessment = IdentityAssessment(
                        state="ambiguous",
                        confidence=20,
                        domain_match=False,
                        handle_match=assessment.handle_match,
                        name_match=assessment.name_match,
                        reasons=["Marketplace, social, or short-link domains cannot be promoted."],
                    )
                elif not site_confirmed and existing is None and assessment.state == "verified":
                    assessment = assessment.model_copy(
                        update={
                            "state": "probable",
                            "confidence": min(assessment.confidence, 70),
                            "reasons": [
                                *assessment.reasons,
                                "The first-party website did not pass public landing verification.",
                            ],
                        }
                    )
                landing_url = next(
                    (
                        url
                        for ad in ads
                        for url in ad.landing_urls
                        if registrable_domain(url) == normalized_domain
                    ),
                    "",
                )
                candidate_id = int(existing["candidate_id"]) if existing else None
                evidence = [
                    {"kind": "identity", "detail": reason}
                    for reason in assessment.reasons
                ]
                evidence.extend(
                    {"kind": "ad", "url": _ad_evidence_url(ad)} for ad in ads[:10]
                )
                link_id = store.upsert_advertiser_domain_link(
                    advertiser_id,
                    domain=normalized_domain,
                    candidate_id=candidate_id,
                    landing_host=urlparse(landing_url).hostname or "",
                    landing_url=landing_url,
                    state=assessment.state,
                    confidence=assessment.confidence,
                    landing_domain_match=assessment.domain_match,
                    social_handle_match=assessment.handle_match,
                    name_match=assessment.name_match,
                    evidence=evidence,
                )
                del link_id
                if assessment.state != "verified" or assessment.confidence < 80:
                    continue
                provider_id = f"meta:page:{advertiser.external_id}"
                # Converge Meta discovery onto the existing page/site-discovery record when
                # its latest canonical domain matches. Creating a second candidate keyed by
                # the landing domain would split one company into two scoring identities.
                discovery_hostname = (
                    str(existing["hostname"]) if existing else normalized_domain
                )
                discovery = DiscoveredHost(
                    source_apex="meta-ad-library",
                    hostname=discovery_hostname,
                    label_hint=advertiser.name,
                    discovery_rank=min(1.0, assessment.confidence / 100),
                    discovery_evidence_urls=list(
                        dict.fromkeys(
                            [
                                *(_ad_evidence_url(ad) for ad in ads[:10]),
                                *( [advertiser.profile_url] if advertiser.profile_url else [] ),
                            ]
                        )
                    ),
                    provider_record_ids=[provider_id],
                )
                store.upsert_derived_candidates([discovery])
                candidate_id = int(existing["candidate_id"]) if existing else None
                if candidate_id is None:
                    identifiers = store.candidate_ids(
                        [normalized_domain, f"www.{normalized_domain}"]
                    )
                    candidate_id = identifiers.get(normalized_domain) or identifiers.get(
                        f"www.{normalized_domain}"
                    )
                if candidate_id is None:
                    resolved = store.find_candidate_by_canonical_domain(normalized_domain)
                    candidate_id = int(resolved["candidate_id"]) if resolved else None
                if candidate_id is None:
                    continue
                if existing is None:
                    promoted += 1
                store.upsert_advertiser_domain_link(
                    advertiser_id,
                    domain=normalized_domain,
                    candidate_id=candidate_id,
                    landing_host=urlparse(landing_url).hostname or "",
                    landing_url=landing_url,
                    state="verified",
                    confidence=assessment.confidence,
                    landing_domain_match=assessment.domain_match,
                    social_handle_match=assessment.handle_match,
                    name_match=assessment.name_match,
                    evidence=evidence,
                )
                advertiser_candidates[advertiser_id].add(candidate_id)
                resolved_candidate = store.find_candidate_by_canonical_domain(normalized_domain)
                if resolved_candidate:
                    candidates[candidate_id] = resolved_candidate
        return advertiser_candidates, candidates, promoted

    async def _run_meta_broad(
        self,
        adapter: ApifyHTTPAdapter,
        store: LeadStore,
        client: httpx.AsyncClient,
        ledger: BudgetLedger,
        request: AdDiscoveryRequest,
        cards: Mapping[str, AdRateCard],
        seeds: Sequence[Mapping[str, Any]],
    ) -> tuple[list[StoredAd], dict[int, set[int]], dict[int, Mapping[str, Any]], int]:
        missions: list[QueryMission] = []
        discovery_queries = (
            request.meta_queries if request.mode in {"discover", "both"} else []
        )
        for index, query in enumerate(discovery_queries):
            url = build_meta_search_url(
                query,
                country=request.country,
                active_only=request.active_only,
            )
            query_key = f"meta-discovery:{index}:{query.casefold()}"
            estimated_cost = cards["meta"].result_price_usd * request.meta_ads_per_query
            if self._reuse_fresh_query(
                store,
                platform="meta",
                surface="meta_library",
                phase="discovery",
                query=query,
                country=request.country,
                input_url=url,
                result_cap=request.meta_ads_per_query,
                estimated_cost=estimated_cost,
                query_key=query_key,
            ):
                continue
            missions.append(
                self._create_query(
                    store,
                    self._run_id,
                    platform="meta",
                    surface="meta_library",
                    phase="discovery",
                    query=query,
                    country=request.country,
                    input_url=url,
                    result_cap=request.meta_ads_per_query,
                    estimated_cost=estimated_cost,
                    query_key=query_key,
                )
            )
        if request.mode in {"hydrate", "both"}:
            for seed in seeds:
                query = str(seed.get("company_name") or seed.get("canonical_domain") or "")
                url = build_meta_search_url(
                    query,
                    country=request.country,
                    active_only=request.active_only,
                )
                missions.append(
                    self._create_query(
                        store,
                        self._run_id,
                        platform="meta",
                        surface="meta_library",
                        phase="existing_target_lookup",
                        query=query,
                        country=request.country,
                        input_url=url,
                        result_cap=request.meta_ads_per_query,
                        estimated_cost=cards["meta"].result_price_usd
                        * request.meta_ads_per_query,
                        query_key=f"meta-existing:{seed['candidate_id']}",
                        seed_candidate_id=int(seed["candidate_id"]),
                    )
                )
        if not missions:
            return [], {}, {}, 0
        self._update_run(
            store,
            self._run_id,
            phase="meta_discovery",
            queries_total=len(self._query_missions),
        )
        # The actor builder already deduplicates URLs; reserve for actual inputs,
        # retaining separate mission receipts for every requested query/target.
        max_items = len({mission.input_url for mission in missions}) * (
            request.meta_ads_per_query + 30
        )
        actor_input = build_meta_actor_input(
            [mission.input_url for mission in missions],
            limit_per_source=request.meta_ads_per_query,
            run_tag=f"beings-ad-run-{self._run_id}",
            active_only=request.active_only,
            country=request.country,
        )
        items, _ = await self._call_actor(
            adapter,
            store,
            ledger,
            label="Meta discovery and existing-target lookup",
            actor_id=META_ACTOR_ID,
            actor_input=actor_input,
            max_items=max_items,
            card=cards["meta"],
            missions=missions,
        )
        stored = self._persist_ads(
            store,
            self._run_id,
            items,
            missions,
            normalize_meta_ad,
            coverage_type="meta_public_ad_library",
        )
        links, candidates, promoted = await self._resolve_meta_identities(
            store, client, request, stored
        )
        self._refresh_run_progress(store, candidates_promoted=promoted)
        return stored, links, candidates, promoted

    async def _run_meta_exact(
        self,
        adapter: ApifyHTTPAdapter,
        store: LeadStore,
        ledger: BudgetLedger,
        request: AdDiscoveryRequest,
        cards: Mapping[str, AdRateCard],
        advertiser_candidates: Mapping[int, set[int]],
        broad_ads: Sequence[StoredAd],
    ) -> list[StoredAd]:
        advertiser_by_id: dict[int, NormalizedAdvertiser] = {}
        for stored in broad_ads:
            if stored.advertiser_id in advertiser_candidates:
                advertiser_by_id[stored.advertiser_id] = stored.ad.advertiser
        advertisers = list(advertiser_by_id.items())[: request.deep_advertiser_upper_bound]
        if not advertisers:
            return []
        missions: list[QueryMission] = []
        for _advertiser_id, advertiser in advertisers:
            try:
                url = build_meta_page_url(
                    advertiser.external_id,
                    country=request.country,
                    active_only=request.active_only,
                )
            except ValueError:
                continue
            query_key = f"meta-page:{advertiser.external_id}"
            estimated_cost = (
                cards["meta"].result_price_usd * request.meta_ads_per_advertiser
            )
            if self._reuse_fresh_query(
                store,
                platform="meta",
                surface="meta_library",
                phase="advertiser_hydration",
                query=advertiser.name,
                country=request.country,
                input_url=url,
                result_cap=request.meta_ads_per_advertiser,
                estimated_cost=estimated_cost,
                query_key=query_key,
            ):
                continue
            missions.append(
                self._create_query(
                    store,
                    self._run_id,
                    platform="meta",
                    surface="meta_library",
                    phase="advertiser_hydration",
                    query=advertiser.name,
                    country=request.country,
                    input_url=url,
                    result_cap=request.meta_ads_per_advertiser,
                    estimated_cost=estimated_cost,
                    query_key=query_key,
                )
            )
        if not missions:
            return []
        self._update_run(
            store,
            self._run_id,
            phase="meta_advertiser_hydration",
            queries_total=len(self._query_missions),
        )
        max_items = len({mission.input_url for mission in missions}) * (
            request.meta_ads_per_advertiser + 30
        )
        actor_input = build_meta_actor_input(
            [mission.input_url for mission in missions],
            limit_per_source=request.meta_ads_per_advertiser,
            run_tag=f"beings-ad-run-{self._run_id}-pages",
            active_only=request.active_only,
            country=request.country,
        )
        items, _ = await self._call_actor(
            adapter,
            store,
            ledger,
            label="Meta exact advertiser hydration",
            actor_id=META_ACTOR_ID,
            actor_input=actor_input,
            max_items=max_items,
            card=cards["meta"],
            missions=missions,
        )
        stored = self._persist_ads(
            store,
            self._run_id,
            items,
            missions,
            normalize_meta_ad,
            coverage_type="meta_public_ad_library",
        )
        self._refresh_run_progress(store)
        return stored

    async def _run_tiktok_for_candidate(
        self,
        adapter: ApifyHTTPAdapter,
        store: LeadStore,
        ledger: BudgetLedger,
        request: AdDiscoveryRequest,
        cards: Mapping[str, AdRateCard],
        candidate: Mapping[str, Any],
    ) -> list[StoredAd]:
        stored: list[StoredAd] = []
        candidate_id = int(candidate["candidate_id"])
        company_name = str(candidate.get("company_name") or candidate.get("canonical_domain"))
        domain = str(candidate.get("canonical_domain") or candidate.get("hostname"))
        if request.tiktok_library_enabled:
            pages = max(1, math.ceil(request.tiktok_ads_per_advertiser / 12))
            mission = self._create_query(
                store,
                self._run_id,
                platform="tiktok",
                surface="tiktok_library",
                phase="cross_platform_hydration",
                query=company_name,
                country="ALL",
                input_url="",
                result_cap=request.tiktok_ads_per_advertiser,
                estimated_cost=cards["tiktok_library"].result_price_usd
                * request.tiktok_ads_per_advertiser,
                query_key=f"tiktok-library:{candidate_id}",
                seed_candidate_id=candidate_id,
            )
            try:
                items, _ = await self._call_actor(
                    adapter,
                    store,
                    ledger,
                    label=f"TikTok Commercial Content Library: {domain}",
                    actor_id=TIKTOK_LIBRARY_ACTOR_ID,
                    actor_input={
                        "query": domain,
                        "advertiserName": company_name,
                        "country": "all",
                        "maxPages": pages,
                        "quickSearch": False,
                        "sortBy": "last_shown_date,desc",
                    },
                    max_items=request.tiktok_ads_per_advertiser,
                    card=cards["tiktok_library"],
                    missions=[mission],
                )
                stored.extend(
                    self._persist_ads(
                        store,
                        self._run_id,
                        items,
                        [mission],
                        normalize_tiktok_ad,
                        coverage_type="europe_commercial_content_library",
                    )
                )
            except PaidBudgetExhausted as exc:
                self.errors.append(str(exc))
        if request.tiktok_top_enabled:
            mission = self._create_query(
                store,
                self._run_id,
                platform="tiktok",
                surface="tiktok_top",
                phase="cross_platform_hydration",
                query=company_name,
                country=request.country,
                input_url="",
                result_cap=request.tiktok_top_ads_per_advertiser,
                estimated_cost=cards["tiktok_top"].result_price_usd
                * request.tiktok_top_ads_per_advertiser,
                query_key=f"tiktok-top:{candidate_id}",
                seed_candidate_id=candidate_id,
            )
            try:
                items, _ = await self._call_actor(
                    adapter,
                    store,
                    ledger,
                    label=f"TikTok Top Ads: {domain}",
                    actor_id=TIKTOK_TOP_ACTOR_ID,
                    actor_input={
                        "keyword": company_name,
                        "country": [request.country] if request.country != "ALL" else [],
                        "orderBy": "for_you",
                        "period": "30",
                        "maxItems": request.tiktok_top_ads_per_advertiser,
                    },
                    max_items=request.tiktok_top_ads_per_advertiser,
                    card=cards["tiktok_top"],
                    missions=[mission],
                )
                stored.extend(
                    self._persist_ads(
                        store,
                        self._run_id,
                        items,
                        [mission],
                        normalize_tiktok_top_ad,
                        coverage_type="curated_top_ads_positive_only",
                    )
                )
            except PaidBudgetExhausted as exc:
                self.errors.append(str(exc))
        return stored

    def _resolve_tiktok_identities(
        self,
        store: LeadStore,
        candidate: Mapping[str, Any],
        stored_ads: Sequence[StoredAd],
    ) -> dict[int, set[int]]:
        candidate_id = int(candidate["candidate_id"])
        domain = registrable_domain(
            str(candidate.get("canonical_domain") or candidate.get("hostname") or "")
        )
        by_advertiser: dict[int, list[NormalizedAd]] = defaultdict(list)
        for stored in stored_ads:
            by_advertiser[stored.advertiser_id].append(stored.ad)
        linked: dict[int, set[int]] = defaultdict(set)
        for advertiser_id, ads in by_advertiser.items():
            advertiser = ads[0].advertiser
            observed_domains = list(
                dict.fromkeys(value for ad in ads for value in ad.landing_domains)
            )
            assessment = _best_identity_assessment(
                canonical_domain=domain,
                advertiser=advertiser,
                observed_landing_domains=observed_domains,
                candidate=candidate,
                observation=None,
            )
            landing_url = next(
                (
                    url
                    for ad in ads
                    for url in ad.landing_urls
                    if registrable_domain(url) == domain
                ),
                "",
            )
            store.upsert_advertiser_domain_link(
                advertiser_id,
                domain=domain,
                candidate_id=candidate_id if assessment.state == "verified" else None,
                landing_host=urlparse(landing_url).hostname or "",
                landing_url=landing_url,
                state=assessment.state,
                confidence=assessment.confidence,
                landing_domain_match=assessment.domain_match,
                social_handle_match=assessment.handle_match,
                name_match=assessment.name_match,
                evidence=[
                    *({"kind": "identity", "detail": reason} for reason in assessment.reasons),
                    *({"kind": "ad", "url": _ad_evidence_url(ad)} for ad in ads[:10]),
                ],
            )
            if assessment.state == "verified" and assessment.confidence >= 80:
                linked[advertiser_id].add(candidate_id)
        return linked

    def _record_observations(
        self,
        store: LeadStore,
        run_id: int,
        ads: Sequence[StoredAd],
        advertiser_candidates: Mapping[int, set[int]],
    ) -> set[int]:
        grouped: dict[tuple[int, str, str], list[NormalizedAd]] = defaultdict(list)
        for item in ads:
            for candidate_id in advertiser_candidates.get(item.advertiser_id, set()):
                grouped[(candidate_id, item.ad.platform, item.ad.surface)].append(item.ad)
        now = datetime.now(UTC)
        affected: set[int] = set()
        coverage_notes = {
            "meta_library": "Meta Library covers Facebook and Instagram publisher placements.",
            "tiktok_library": (
                "TikTok Commercial Content Library is an EU/EEA/UK transparency surface; "
                "absence is not negative evidence."
            ),
            "tiktok_top": (
                "TikTok Top Ads is curated and positive-only; it does not establish exhaustive "
                "advertiser activity."
            ),
        }
        coverage_types = {
            "meta_library": "meta_public_ad_library",
            "tiktok_library": "europe_commercial_content_library",
            "tiktok_top": "curated_top_ads_positive_only",
        }
        for (candidate_id, platform, surface), values in grouped.items():
            unique_by_id = {ad.external_id: ad for ad in values}
            values = list(unique_by_id.values())
            group_ids = list(
                dict.fromkeys(ad.collation_id or ad.creative_fingerprint for ad in values)
            )
            active_values = [ad for ad in values if ad.is_active is True]
            active_groups = list(
                dict.fromkeys(
                    ad.collation_id or ad.creative_fingerprint for ad in active_values
                )
            )
            started = [ad.started_at for ad in values if ad.started_at]
            new_7d = sum(value >= now - timedelta(days=7) for value in started)
            new_30d = sum(value >= now - timedelta(days=30) for value in started)
            new_90d = sum(value >= now - timedelta(days=90) for value in started)
            publishers = list(
                dict.fromkeys(
                    publisher.casefold()
                    for ad in values
                    for publisher in ad.publisher_platforms
                )
            )
            sources = list(dict.fromkeys(_ad_evidence_url(ad) for ad in values))
            store.record_candidate_ad_observation(
                candidate_id,
                run_id,
                platform=platform,
                surface=surface,
                coverage_type=coverage_types[surface],
                identity_state="verified",
                identity_confidence=80,
                total_ads=len(values),
                active_ads=len(active_values),
                unique_creatives=len(group_ids),
                active_unique_creatives=len(active_groups),
                video_creatives=sum(ad.media_type == "video" for ad in values),
                image_creatives=sum(ad.media_type == "image" for ad in values),
                new_creatives_7d=new_7d,
                new_creatives_30d=new_30d,
                new_creatives_90d=new_90d,
                first_started_at=_iso(min(started)) if started else None,
                latest_started_at=_iso(max(started)) if started else None,
                platforms=publishers,
                creative_group_ids=group_ids,
                active_creative_group_ids=active_groups,
                source_urls=sources,
                coverage_notes=[coverage_notes[surface]],
            )
            affected.add(candidate_id)
        return affected

    async def _hydrate_websites(
        self,
        store: LeadStore,
        client: httpx.AsyncClient,
        paid_run_id: int,
        request: AdDiscoveryRequest,
        candidates: Mapping[int, Mapping[str, Any]],
    ) -> dict[str, int]:
        empty_summary = {
            "attempted": 0,
            "saved": 0,
            "live": 0,
            "non_english_skipped": 0,
            "reused_recent_complete": 0,
            "skipped_active": 0,
            "deduplicated_identity": 0,
            "failed": 0,
        }
        if not request.hydrate_websites or request.website_hydration_limit <= 0:
            return empty_summary
        candidate_ids = list(candidates)
        if not candidate_ids:
            return empty_summary
        recent_snapshots = store.completed_company_snapshots(
            candidate_ids,
            max_age_hours=COMPANY_SCAN_FRESH_HOURS,
        )
        hydration_run = store.start_run(
            {
                "kind": "ad_discovery_website_hydration",
                "paid_ad_run_id": paid_run_id,
                "manual": True,
                "hosts": [
                    candidate.get("canonical_domain") or candidate.get("hostname")
                    for candidate in candidates.values()
                ],
            }
        )
        due_candidate_ids = [
            candidate_id for candidate_id in candidate_ids if candidate_id not in recent_snapshots
        ]
        saved = 0
        live = 0
        non_english_skipped = 0
        attempted = 0
        completed_candidate_ids: list[int] = []
        failed_candidate_ids: list[int] = []
        claimed_candidate_ids: list[int] = []
        website_errors: list[str] = []
        skipped_active = 0
        deduplicated_identity = 0

        def candidate_for(
            candidate_id: int,
            observation: SiteObservation | None = None,
        ) -> DiscoveredHost | None:
            candidate_data = candidates[candidate_id]
            host = registrable_domain(
                str(
                    candidate_data.get("canonical_domain")
                    or candidate_data.get("hostname")
                    or (observation.canonical_host if observation else "")
                    or (observation.hostname if observation else "")
                )
            )
            if not host:
                return None
            return DiscoveredHost(
                source_apex=str(candidate_data.get("source_apex") or "meta-ad-library"),
                hostname=host,
                label_hint=str(candidate_data.get("company_name") or ""),
                discovery_evidence_urls=list(
                    candidate_data.get("discovery_evidence_urls") or []
                ),
                provider_record_ids=list(candidate_data.get("provider_record_ids") or []),
            )

        def final_score(score, ai) -> int:
            verified_recovery = bool(
                ai
                and ai.company_url
                and ai.confidence in {"medium", "high"}
                and ai.placement_fit in {"possible", "strong"}
            )
            ai_score = ai.placement_fit_score or ai.budget_fit_score if ai else None
            return blend_ai_score(score, ai_score, verified_recovery=verified_recovery)

        def summary_payload(*, error: str = "") -> dict[str, int | str]:
            payload: dict[str, int | str] = {
                "attempted": attempted,
                "saved": saved,
                "live": live,
                "non_english_skipped": non_english_skipped,
                "reused_recent_complete": len(recent_snapshots),
                "skipped_active": skipped_active,
                "deduplicated_identity": deduplicated_identity,
                "failed": len(failed_candidate_ids),
            }
            if error:
                payload["error"] = error
            return payload

        store.update_run_progress(
            hydration_run,
            phase="ad_discovery_hydration",
            total_candidates=len(candidate_ids),
        )
        try:
            claim = store.claim_company_scan_work(
                due_candidate_ids,
                run_id=hydration_run,
                maximum=request.website_hydration_limit,
            )
            claimed_candidate_ids = list(claim["claimed_candidate_ids"])
            recent_snapshots.update(claim["recent_snapshots"])
            skipped_active = len(claim["active_candidate_ids"])
            deduplicated_identity = len(claim["deduplicated_candidate_ids"])
            recent_candidate_ids = [
                candidate_id
                for candidate_id in candidate_ids
                if candidate_id in recent_snapshots
            ]
            total_work = len(recent_candidate_ids) + len(claimed_candidate_ids)
            store.update_run_progress(
                hydration_run,
                phase="ad_discovery_hydration",
                total_candidates=total_work,
            )

            for candidate_id in recent_candidate_ids:
                snapshot = recent_snapshots[candidate_id]
                observation = snapshot["observation"].model_copy(deep=True)
                candidate = candidate_for(candidate_id, observation)
                if candidate is None:
                    continue
                ad_intelligence = AdIntelligence.model_validate(
                    store.current_candidate_ad_intelligence(candidate_id)
                )
                score = score_candidate(
                    candidate,
                    observation,
                    previous_observation=observation,
                    ad_intelligence=ad_intelligence,
                )
                ai = snapshot.get("ai")
                store.save_snapshot(
                    hydration_run,
                    candidate_id,
                    observation,
                    score,
                    ai,
                    final_score(score, ai),
                )
                if not store.update_completed_company_snapshot(candidate_id, hydration_run):
                    raise RuntimeError(
                        f"Could not retain completed website work for candidate {candidate_id}"
                    )
                saved += 1
                store.update_run_progress(
                    hydration_run,
                    phase="ad_discovery_hydration",
                    inspected=attempted,
                    enriched=saved,
                    total_candidates=total_work,
                )

            for candidate_id in claimed_candidate_ids:
                self._check_cancelled()
                attempted += 1
                candidate = candidate_for(candidate_id)
                if candidate is None:
                    failed_candidate_ids.append(candidate_id)
                    website_errors.append(
                        f"website hydration candidate {candidate_id}: invalid host"
                    )
                    continue
                observation = await self._inspect_candidate(
                    client, candidate, pages_per_site=4
                )
                candidate_failed = not observation_completes_company_work(observation)
                if observation.error:
                    message = f"website inspection {candidate.hostname}: {observation.error}"
                    website_errors.append(message)
                    self.errors.append(message)
                if observation.live:
                    live += 1
                    if is_english_eligible(observation):
                        try:
                            await enrich_site_owned_intelligence(
                                client,
                                observation,
                                cache_get=store.get_free_signal_cache,
                                cache_set=store.set_free_signal_cache,
                                max_sitemaps=4,
                            )
                        except Exception as exc:
                            candidate_failed = True
                            message = (
                                f"website enrichment {candidate.hostname}: "
                                f"{type(exc).__name__}: {exc}"
                            )
                            website_errors.append(message)
                            self.errors.append(message)
                    else:
                        non_english_skipped += 1
                ad_intelligence = AdIntelligence.model_validate(
                    store.current_candidate_ad_intelligence(candidate_id)
                )
                score = score_candidate(
                    candidate,
                    observation,
                    previous_observation=store.previous_observation(candidate.hostname),
                    ad_intelligence=ad_intelligence,
                )
                store.save_snapshot(
                    hydration_run,
                    candidate_id,
                    observation,
                    score,
                    None,
                    score.total,
                )
                saved += 1
                if candidate_failed:
                    failed_candidate_ids.append(candidate_id)
                else:
                    completed_candidate_ids.append(candidate_id)
                store.update_run_progress(
                    hydration_run,
                    phase="ad_discovery_hydration",
                    inspected=attempted,
                    enriched=saved,
                    total_candidates=total_work,
                )
            status = "partial" if failed_candidate_ids else "complete"
            summary = summary_payload()
            store.finish_run_with_company_scan_work(
                hydration_run,
                status,
                {"paid_ad_run_id": paid_run_id, **summary},
                completed_candidate_ids=completed_candidate_ids,
                failed_candidate_ids=failed_candidate_ids,
                failure_reason="; ".join(website_errors[:5]),
            )
        except Exception as exc:
            completed = set(completed_candidate_ids)
            failed_candidate_ids = list(
                dict.fromkeys(
                    [
                        *failed_candidate_ids,
                        *(
                            candidate_id
                            for candidate_id in claimed_candidate_ids
                            if candidate_id not in completed
                        ),
                    ]
                )
            )
            error = f"{type(exc).__name__}: {exc}"
            store.finish_run_with_company_scan_work(
                hydration_run,
                "failed",
                {"paid_ad_run_id": paid_run_id, **summary_payload(error=error)},
                completed_candidate_ids=completed_candidate_ids,
                failed_candidate_ids=failed_candidate_ids,
                failure_reason=error,
            )
            raise
        return {key: int(value) for key, value in summary_payload().items()}

    async def execute(
        self,
        run_id: int,
        request: AdDiscoveryRequest,
        quote: AdScanQuote,
    ) -> dict[str, Any]:
        self._run_id = run_id
        cards = default_rate_cards(quote.apify_tier)
        ledger = BudgetLedger(request.max_total_charge_usd)
        with LeadStore(self.db_path) as store:
            store.update_ad_discovery_run(
                run_id,
                status="running",
                phase="preparing",
            )
            seeds = (
                store.select_ad_hydration_seeds(
                    limit=request.existing_target_limit,
                    only_due=True,
                    **({"candidate_ids": self.seed_candidate_ids, "lane": "all", "scope": "all"}
                       if self.seed_candidate_ids is not None else {}),
                )
                if request.mode in {"hydrate", "both"}
                else []
            )
            async with self.adapter_factory(self.token) as adapter, httpx.AsyncClient(
                timeout=httpx.Timeout(25.0, connect=8.0),
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            ) as client:
                broad_ads: list[StoredAd] = []
                advertiser_candidates: dict[int, set[int]] = defaultdict(set)
                candidates: dict[int, Mapping[str, Any]] = {
                    int(seed["candidate_id"]): seed for seed in seeds
                }
                promoted = 0
                if request.meta_enabled:
                    (
                        broad_ads,
                        meta_links,
                        meta_candidates,
                        promoted,
                    ) = await self._run_meta_broad(
                        adapter,
                        store,
                        client,
                        ledger,
                        request,
                        cards,
                        seeds,
                    )
                    for advertiser_id, values in meta_links.items():
                        advertiser_candidates[advertiser_id].update(values)
                    candidates.update(meta_candidates)
                    try:
                        exact_ads = await self._run_meta_exact(
                            adapter,
                            store,
                            ledger,
                            request,
                            cards,
                            advertiser_candidates,
                            broad_ads,
                        )
                    except PaidBudgetExhausted as exc:
                        self.errors.append(str(exc))
                        exact_ads = []
                else:
                    exact_ads = []
                all_ads = [*broad_ads, *exact_ads]
                if request.tiktok_library_enabled or request.tiktok_top_enabled:
                    self._update_run(store, run_id, phase="tiktok_hydration")
                    for candidate in list(candidates.values())[
                        : request.deep_advertiser_upper_bound
                    ]:
                        self._check_cancelled()
                        tiktok_ads = await self._run_tiktok_for_candidate(
                            adapter,
                            store,
                            ledger,
                            request,
                            cards,
                            candidate,
                        )
                        tiktok_links = self._resolve_tiktok_identities(
                            store, candidate, tiktok_ads
                        )
                        for advertiser_id, values in tiktok_links.items():
                            advertiser_candidates[advertiser_id].update(values)
                        all_ads.extend(tiktok_ads)
                        self._refresh_run_progress(
                            store,
                            candidates_promoted=promoted,
                        )
                affected = self._record_observations(
                    store,
                    run_id,
                    all_ads,
                    advertiser_candidates,
                )
                for candidate_id in affected:
                    resolved = next(
                        (
                            value
                            for value in candidates.values()
                            if int(value.get("candidate_id") or 0) == candidate_id
                        ),
                        None,
                    )
                    if resolved:
                        candidates[candidate_id] = resolved
                affected_candidates = {
                    candidate_id: candidates[candidate_id]
                    for candidate_id in sorted(affected)
                    if candidate_id in candidates
                }
                self._update_run(store, run_id, phase="website_hydration")
                website_summary = await self._hydrate_websites(
                    store,
                    client,
                    run_id,
                    request,
                    affected_candidates,
                )
            links = store.list_advertiser_domain_links(limit=1_000)["items"]
            run_advertiser_ids = {item.advertiser_id for item in all_ads}
            verified_links = [
                link
                for link in links
                if link["state"] == "verified"
                and int(link["advertiser_id"]) in run_advertiser_ids
            ]
            query_rows = store.list_ad_discovery_queries(run_id)
            summary = {
                "provider": "apify",
                "manual": True,
                "approved_cap_usd": format(request.max_total_charge_usd, "f"),
                "allocated_provider_caps_usd": format(ledger.allocated, "f"),
                "provider_reported_cost_usd": format(self.provider_reported_cost, "f"),
                "budget_allocations": ledger.allocations,
                "queries": len(query_rows),
                "queries_completed": sum(
                    row["status"] in {"complete", "partial", "warehouse_fresh"}
                    for row in query_rows
                ),
                "provider_queries_started": sum(
                    bool(row["provider_run_id"]) for row in query_rows
                ),
                "warehouse_queries_reused": self.warehouse_queries_reused,
                "advertisers": len({item.advertiser_id for item in all_ads}),
                "creatives": len({item.creative_id for item in all_ads}),
                "verified_links": len(verified_links),
                "candidates_promoted": promoted,
                "candidates_with_ad_observations": len(affected),
                "website_hydration": website_summary,
                "errors": self.errors[:25],
                "coverage_notes": [
                    "Meta combines Facebook and Instagram publisher placement evidence.",
                    "TikTok Commercial Content Library coverage is Europe-focused.",
                    "TikTok Top Ads is curated positive-only evidence.",
                    "Missing ad-library results never count as negative evidence.",
                    "Fresh identical Meta reads are served from the local ad ledger for 90 days.",
                ],
            }
            self._update_run(
                store,
                run_id,
                phase="finalizing",
                queries_total=len(query_rows),
                queries_completed=summary["queries_completed"],
                advertisers_found=summary["advertisers"],
                links_verified=summary["verified_links"],
                candidates_promoted=promoted,
                creatives_found=summary["creatives"],
                summary=summary,
            )
            return summary
