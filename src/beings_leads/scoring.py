from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from .config import PLATFORM_PROFILES
from .creative_capacity import (
    BuyingSignalEvent,
    CreativeCapacityHypothesis,
    HypothesisClaim,
    HypothesisPrincipalSignals,
    HypothesisSignal,
)
from .dataforseo_intelligence import TRAJECTORY_FEATURE_KEY, TRAJECTORY_SCORE_POLICY
from .entity_intelligence import (
    CompanyRoleAssessment,
    assess_company_role,
    company_self_corpus,
    project_company_self_observation,
)
from .evidence_policy import VERSION as FRESHNESS_VERSION
from .evidence_policy import durable_weight, utc_reference
from .language import assess_observation_language
from .models import (
    AdIntelligence,
    DiscoveredHost,
    ScoreCard,
    SiteObservation,
    legacy_agency_portfolio_domains,
)
from .placement import EXTERNAL_CAPACITY_TERMS, assess_placement_fit
from .revenue_intelligence import (
    RevenueSynthesisEvidence,
    synthesize_revenue_intelligence,
)
from .sales_intelligence import refresh_sales_intelligence

COMPONENT_MAX = {
    "recurring_demand": 25,
    "paid_distribution": 20,
    "ability_to_pay": 20,
    "beings_fit": 15,
    "operational_maturity": 10,
    "reachability": 10,
}

_HIRING_SCANNER_ID = "creative_hiring_demand_v1"
_HIRING_WORKFORCE_PREDICATE = "hiring.workforce_demand_current"
_HIRING_CREATIVE_PREDICATE = "hiring.creative_demand_current"


def _frontier_observed_at(atomic: object) -> datetime:
    value = getattr(atomic, "last_observed_at", None) or getattr(atomic, "first_observed_at", None)
    if not isinstance(value, datetime):
        return datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _exact_nonnegative_integer(value: object, key: str) -> int | None:
    if not isinstance(value, dict):
        return None
    candidate = value.get(key)
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 0:
        return None
    return candidate


def _seo_trajectory_points(observation: SiteObservation) -> int:
    """Read only the strict store-derived, positive-only SEO trajectory contract."""

    frontier = getattr(observation.web, "frontier", None)
    features = getattr(frontier, "features", {}) if frontier is not None else {}
    feature = features.get(TRAJECTORY_FEATURE_KEY) if isinstance(features, dict) else None
    if not isinstance(feature, dict):
        return 0
    points = feature.get("points")
    interval = feature.get("interval_days")
    current_age = feature.get("current_age_days")
    count_delta = feature.get("organic_count_delta")
    etv_delta = feature.get("organic_etv_delta")
    if (
        feature.get("score_policy") != TRAJECTORY_SCORE_POLICY
        or feature.get("scanner_id") != "dataforseo_search_visibility_v1"
        or feature.get("predicate")
        not in {"seo.search_visibility_estimate", "seo.domain_rank_overview"}
        or feature.get("sample_count") != 2
        or feature.get("positive_only") is not True
        or feature.get("modeled_provider_estimates") is not True
        or isinstance(points, bool)
        or not isinstance(points, int)
        or not 1 <= points <= 4
        or isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not 21 <= float(interval) <= 120
        or isinstance(current_age, bool)
        or not isinstance(current_age, (int, float))
        or not 0 <= float(current_age) < 30
        or isinstance(count_delta, bool)
        or not isinstance(count_delta, int)
        or isinstance(etv_delta, bool)
        or not isinstance(etv_delta, (int, float))
        or count_delta <= 0
        and float(etv_delta) <= 0
        or feature.get("count_emergent") is not True
        and feature.get("etv_emergent") is not True
    ):
        return 0
    return points


def _current_hiring_aggregate_values(
    observation: SiteObservation, *, reference_time: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Return only the newest lineage-backed, complete hiring aggregates.

    These aggregates can be score-ineligible because a literal count may sit below the
    revenue-event threshold.  They can still safely populate the legacy count projection, but
    only after the current-state, coverage, artifact, and company bindings pass.  A newer explicit
    zero therefore retires an older frontier count; the caller's max projection still never
    lowers independently hydrated legacy ATS values.
    """

    frontier = getattr(observation.web, "frontier", None)
    if frontier is None:
        return {}
    company_key = (
        (observation.canonical_host or observation.hostname).strip().lower().removeprefix("www.")
    )
    from .frontier_lineage import ReceiptIndex

    now = utc_reference(reference_time or observation.observed_at)
    artifacts = ReceiptIndex(frontier.evidence_artifacts, "artifact_key")
    manifests = ReceiptIndex(frontier.coverage_manifests, "manifest_key")
    candidates: dict[str, list[object]] = {}
    for atomic in getattr(frontier, "observations", ()):
        predicate = str(getattr(atomic, "predicate", "") or "")
        if predicate not in {_HIRING_WORKFORCE_PREDICATE, _HIRING_CREATIVE_PREDICATE}:
            continue
        if str(getattr(atomic, "scanner_id", "") or "") != _HIRING_SCANNER_ID:
            continue
        atomic_company = (
            str(getattr(atomic, "company_key", "") or "").strip().lower().removeprefix("www.")
        )
        qualifiers = getattr(atomic, "qualifiers", {})
        observed_at = _frontier_observed_at(atomic)
        expiry = min(observed_at + timedelta(days=14), atomic.expires_at or
                     observed_at + timedelta(days=14))
        declared_complete = (
            isinstance(qualifiers, dict) and qualifiers.get("coverage_complete") is True
        )
        declared_lower_bound = (
            isinstance(qualifiers, dict)
            and qualifiers.get("coverage_complete") is False
            and qualifiers.get("counts_are_lower_bounds") is True
        )
        if (
            atomic_company != company_key
            or observed_at > now
            or expiry <= now
            or bool(getattr(atomic, "discovery_only", False))
            or str(getattr(atomic, "validity_status", "") or "").casefold() != "current"
            or str(getattr(atomic, "epistemic_status", "") or "").casefold() != "observed"
            or not isinstance(qualifiers, dict)
            or qualifiers.get("claim_basis") != "literal_public_openings"
            or qualifiers.get("current_state") is not True
            or (predicate == _HIRING_WORKFORCE_PREDICATE and not declared_complete)
            or (
                predicate == _HIRING_CREATIVE_PREDICATE
                and not (declared_complete or declared_lower_bound)
            )
        ):
            continue
        coverage_keys = tuple(str(key) for key in getattr(atomic, "coverage_keys", ()) if str(key))
        covered = bool(coverage_keys)
        for key in coverage_keys:
            manifest = manifests.get(atomic, key)
            if (
                manifest is None
                or str(getattr(manifest, "scanner_id", "") or "") != _HIRING_SCANNER_ID
            ):
                covered = False
                break
            completeness = str(getattr(manifest, "completeness", "") or "").casefold()
            if declared_complete and (
                completeness != "complete"
                or bool(getattr(manifest, "truncated", False))
                or int(getattr(manifest, "failed_units", 0) or 0) != 0
            ):
                covered = False
                break
            if declared_lower_bound and (
                completeness not in {"partial", "complete"}
                or int(getattr(manifest, "successful_units", 0) or 0) < 1
            ):
                covered = False
                break
        source_url = str(getattr(atomic, "source_url", "") or "").strip()
        artifact_keys = tuple(str(key) for key in getattr(atomic, "artifact_keys", ()) if str(key))
        addressed = bool(source_url and artifact_keys) and any(
            (artifact := artifacts.get(atomic, key)) is not None
            and str(getattr(artifact, "scanner_id", "") or "") == _HIRING_SCANNER_ID
            and (
                str(getattr(artifact, "company_key", "") or "").strip().lower().removeprefix("www.")
                == company_key
            )
            and str(getattr(artifact, "canonical_url", "") or "").strip() == source_url
            and bool(str(getattr(artifact, "content_hash", "") or ""))
            for key in artifact_keys
        )
        if not covered or not addressed:
            continue
        candidates.setdefault(predicate, []).append(atomic)
    latest: dict[str, object] = {}
    count_fields = {
        _HIRING_WORKFORCE_PREDICATE: "current_openings",
        _HIRING_CREATIVE_PREDICATE: "current_creative_openings",
    }
    for predicate, rows in candidates.items():
        complete_rows = [
            row for row in rows if getattr(row, "qualifiers", {}).get("coverage_complete") is True
        ]
        cutoff = max(
            (_frontier_observed_at(row) for row in complete_rows),
            default=datetime.min.replace(tzinfo=UTC),
        )
        current_rows = [row for row in rows if _frontier_observed_at(row) >= cutoff]
        count_field = count_fields[predicate]
        latest[predicate] = max(
            current_rows,
            key=lambda row: (
                _exact_nonnegative_integer(getattr(row, "value", None), count_field) or 0,
                _frontier_observed_at(row),
                str(getattr(row, "observation_key", "") or ""),
            ),
        )
    return {
        predicate: value
        for predicate, atomic in latest.items()
        if isinstance((value := getattr(atomic, "value", None)), dict)
    }


def _project_current_frontier_hiring(
    observation: SiteObservation, *, reference_time: datetime | None = None,
) -> None:
    """Raise legacy hiring counters from canonical current aggregates without addition."""

    aggregates = _current_hiring_aggregate_values(observation, reference_time=reference_time)
    workforce = aggregates.get(_HIRING_WORKFORCE_PREDICATE, {})
    creative = aggregates.get(_HIRING_CREATIVE_PREDICATE, {})
    current_openings = _exact_nonnegative_integer(workforce, "current_openings")
    distinct_postings = _exact_nonnegative_integer(workforce, "distinct_postings")
    surface_count = _exact_nonnegative_integer(workforce, "surface_count")
    if (
        current_openings is not None
        and distinct_postings is not None
        and current_openings == distinct_postings
        and surface_count is not None
        and (surface_count > 0 or current_openings == 0)
    ):
        observation.web.open_jobs = max(observation.web.open_jobs, current_openings)

    creative_openings = _exact_nonnegative_integer(creative, "current_creative_openings")
    photo_video_openings = _exact_nonnegative_integer(creative, "current_photo_video_openings")
    if (
        creative_openings is not None
        and photo_video_openings is not None
        and photo_video_openings <= creative_openings
    ):
        observation.web.creative_jobs = max(observation.web.creative_jobs, creative_openings)
        observation.web.photo_video_jobs = max(
            observation.web.photo_video_jobs, photo_video_openings
        )


def _cap(name: str, value: int) -> int:
    return max(0, min(value, COMPONENT_MAX[name]))


def _scale_points(value: int, thresholds: tuple[tuple[int, int], ...]) -> int:
    return next((points for minimum, points in thresholds if value >= minimum), 0)


def _outreach_emails(observation: SiteObservation) -> list[str]:
    return list(
        dict.fromkeys(
            contact.address
            for contact in observation.contacts.emails
            if contact.outreach_eligible
            and not contact.historical
            and contact.confidence in {"medium", "high"}
        )
    )


def _first_source_url(
    *groups: Sequence[str | None],
    containing: tuple[str, ...] = (),
) -> str | None:
    urls = list(
        dict.fromkeys(
            value.strip() for group in groups for value in group if value and value.strip()
        )
    )
    if containing:
        direct = next(
            (url for url in urls if any(token in url.lower() for token in containing)),
            None,
        )
        if direct:
            return direct
    return urls[0] if urls else None


def classify_archetype(
    candidate: DiscoveredHost,
    observation: SiteObservation,
    *,
    company_role: CompanyRoleAssessment | None = None,
) -> str:
    """Project attributed company roles into the backwards-compatible archetype."""
    assessment = company_role or assess_company_role(observation, candidate=candidate)
    role_map = {
        "production_agency": "agency",
        "consumer_brand": "ecommerce_brand",
        "consumer_app_game": "saas_or_other",
        "software_vendor": "saas_or_other",
        "publisher": "media_membership",
        "directory_or_awards": "saas_or_other",
    }
    if assessment.role in role_map:
        return role_map[assessment.role]
    profile = PLATFORM_PROFILES.get(candidate.source_apex)
    if profile and profile.kind == "creator_education":
        return "creator_education"
    if profile and profile.kind == "b2b_marketing":
        return "saas_or_other"
    # Generic agency keyword counts are intentionally no longer an identity claim.
    if len(observation.signals.get("creator_education", [])) >= 2:
        return "creator_education"
    return "unknown"


def assess_creative_capacity_hypothesis(
    observation: SiteObservation,
    *,
    archetype: str = "unknown",
    ad_intelligence: AdIntelligence | None = None,
    agency_buyer_ready: bool = False,
) -> CreativeCapacityHypothesis:
    """Build an unverified research hypothesis without deciding pursuit eligibility."""

    signals = observation.signals
    web = observation.web
    sales = web.sales
    recent = web.sitemap_recent_counts
    paid_hits = len(signals.get("paid_distribution", []))
    recurring_hits = len(signals.get("recurring", []))
    creative_hits = len(signals.get("creative_demand", []))
    public_marketing_ids = sum(
        bool(observation.public_ids.get(key))
        for key in ("google_tag", "meta_pixel", "tiktok_pixel", "linkedin_partner")
    )
    public_marketing_ids += sum(
        technology in observation.technologies
        for technology in ("Meta Pixel", "TikTok Pixel", "Google Ads/Analytics")
    )
    public_marketing_ids = min(public_marketing_ids, 4)
    fallback_urls = [
        *observation.evidence_urls,
        *(page.url for page in observation.pages),
        observation.final_url or f"https://{observation.hostname}/",
    ]
    distribution_url = _first_source_url(
        sales.source_urls,
        web.source_urls,
        fallback_urls,
        containing=("ad", "campaign", "marketing", "growth"),
    )
    demand_url = _first_source_url(
        sales.source_urls,
        web.source_urls,
        fallback_urls,
        containing=("feed", "product", "campaign", "news", "press"),
    )
    careers_url = _first_source_url(
        web.source_urls,
        sales.source_urls,
        fallback_urls,
        containing=("career", "job", "greenhouse", "lever", "ashby"),
    )
    observed_at = observation.observed_at
    observations: list[HypothesisClaim] = []
    inferences: list[HypothesisClaim] = []
    ads = ad_intelligence or AdIntelligence()
    verified_ads = ads.identity_state == "verified" and ads.identity_confidence >= 80
    projected_scoreable_subset = ads.scoreable_unique_creatives is not None
    if projected_scoreable_subset:
        scoreable_active_creatives = int(ads.scoreable_active_unique_creatives or 0)
        scoreable_unique_creatives = int(ads.scoreable_unique_creatives or 0)
        scoreable_new_7d = int(ads.scoreable_new_creatives_7d or 0)
        scoreable_new_30d = int(ads.scoreable_new_creatives_30d or 0)
        scoreable_windows = int(ads.scoreable_observation_windows_90d or 0)
    else:
        scoreable_active_creatives = ads.active_unique_creatives
        scoreable_unique_creatives = ads.unique_creatives
        scoreable_new_7d = ads.new_creatives_7d
        scoreable_new_30d = ads.new_creatives_30d
        scoreable_windows = ads.observation_windows_90d
    scoreable_platforms = ads.scoreable_platforms if projected_scoreable_subset else ads.platforms
    direct_ad_evidence = verified_ads and scoreable_active_creatives > 0
    ad_source_url = ads.source_urls[0] if ads.source_urls else None

    if direct_ad_evidence:
        observed_ad_points = _scale_points(
            scoreable_active_creatives,
            ((10, 12), (5, 10), (2, 8), (1, 6)),
        )
        platform_points = _scale_points(len(scoreable_platforms), ((3, 4), (2, 3), (1, 1)))
        proxy_points = (
            min(paid_hits * 2, 4)
            + min(public_marketing_ids, 2)
            + _scale_points(sales.paid_growth_score, ((70, 3), (45, 2), (25, 1)))
        )
        distribution_investment = min(
            25,
            observed_ad_points + platform_points + min(proxy_points, 9),
        )
        distribution_met = direct_ad_evidence
    else:
        distribution_investment = min(
            25,
            min(paid_hits * 4, 8)
            + min(public_marketing_ids * 2, 6)
            + _scale_points(sales.paid_growth_score, ((70, 9), (50, 7), (30, 4)))
            + _scale_points(
                sales.video_assets + sales.embedded_video_players,
                ((10, 3), (3, 2), (1, 1)),
            ),
        )
        distribution_met = bool(
            (paid_hits and (public_marketing_ids or sales.paid_growth_score >= 30))
            or (sales.paid_growth_score >= 60 and sales.image_assets + sales.video_assets > 0)
        )
    if direct_ad_evidence:
        observations.append(
            HypothesisClaim(
                claim=(
                    f"A verified advertiser identity has {scoreable_active_creatives} active "
                    f"deduplicated creative groups across {len(scoreable_platforms)} observed "
                    "paid platform surfaces."
                ),
                epistemic_status="observed",
                truth_state="met",
                signal="distribution_investment",
                dimension="distribution_investment",
                source_url=ad_source_url,
                observed_at=ads.last_observed_at or observed_at,
                confidence="high" if scoreable_active_creatives >= 2 else "medium",
            )
        )
    if paid_hits or public_marketing_ids or sales.paid_growth_score:
        observations.append(
            HypothesisClaim(
                claim=(
                    f"Public scan found {paid_hits} paid-distribution language signals, "
                    f"{public_marketing_ids} marketing identifiers, and a "
                    f"{sales.paid_growth_score}/100 paid-growth stack score."
                ),
                epistemic_status="observed",
                truth_state="met" if distribution_met else "unknown",
                signal="distribution_investment",
                dimension="distribution_investment",
                source_url=distribution_url,
                observed_at=observed_at,
                confidence="medium" if distribution_met else "low",
            )
        )

    recent_demand = (
        sales.feed_recent_30d
        + recent.get("products", 0)
        + recent.get("campaigns", 0)
        + recent.get("case_studies", 0)
    )
    contextual_use_case_hits = len(signals.get("creative_use_cases", []))
    recurring_business_hits = sum(
        value.startswith(("subscription_membership:", "agency_retainer:"))
        for value in signals.get("business_model", [])
    )
    site_recurring_met = bool(
        (recurring_hits and creative_hits)
        or recent_demand >= 3
        or (sales.feed_recent_30d >= 2 and sales.feed_commercial_items >= 1)
        or (contextual_use_case_hits >= 2 and (recurring_hits or recent_demand))
        or (recurring_business_hits and contextual_use_case_hits)
    )
    if direct_ad_evidence:
        first_party_points = min(
            8,
            min(recurring_hits * 2, 4)
            + min(creative_hits * 2, 4)
            + min(contextual_use_case_hits, 2)
            + _scale_points(recent_demand, ((10, 4), (3, 2), (1, 1))),
        )
        current_variety_points = _scale_points(
            scoreable_active_creatives,
            ((10, 3), (5, 2), (2, 1), (1, 1)),
        )
        recent_refresh_points = _scale_points(
            scoreable_new_30d,
            ((10, 3), (5, 2), (2, 1)),
        )
        creative_workload_points = min(5, current_variety_points + recent_refresh_points)
        longitudinal_points = (
            8 if ads.recurring_established else _scale_points(scoreable_windows, ((2, 4), (1, 0)))
        )
        recurring_creative_demand = min(
            20,
            first_party_points + creative_workload_points + longitudinal_points,
        )
    else:
        recurring_creative_demand = min(
            20,
            min(recurring_hits * 4, 8)
            + min(creative_hits * 3, 6)
            + min(contextual_use_case_hits * 2, 4)
            + min(recurring_business_hits * 2, 2)
            + _scale_points(recent_demand, ((10, 4), (3, 3), (1, 1)))
            + _scale_points(sales.creative_demand_score, ((70, 4), (50, 3), (30, 2))),
        )
    recurring_met = site_recurring_met or bool(direct_ad_evidence and ads.recurring_established)
    if direct_ad_evidence and scoreable_unique_creatives:
        observations.append(
            HypothesisClaim(
                claim=(
                    f"Paid-ad snapshots contain {scoreable_unique_creatives} deduplicated "
                    f"creative groups across {scoreable_windows} distinct 30-day windows."
                ),
                epistemic_status="observed",
                truth_state="met" if ads.recurring_established else "unknown",
                signal="recurring_creative_demand",
                dimension="recurring_creative_demand",
                source_url=ad_source_url,
                observed_at=ads.last_observed_at or observed_at,
                confidence="medium" if ads.recurring_established else "low",
            )
        )
    if recurring_hits or creative_hits or recent_demand or sales.creative_demand_score:
        observations.append(
            HypothesisClaim(
                claim=(
                    f"Public scan found {recurring_hits} recurring-work signals, "
                    f"{creative_hits} creative-demand signals, and {recent_demand} recent "
                    "commercial or content items."
                ),
                epistemic_status="observed",
                truth_state="met" if recurring_met else "unknown",
                signal="recurring_creative_demand",
                dimension="recurring_creative_demand",
                source_url=demand_url,
                observed_at=observed_at,
                confidence="medium" if recurring_met else "low",
            )
        )

    # photo_video_jobs is a more specific subset of creative_jobs in the ATS pass.
    open_production_roles = max(web.creative_jobs, web.photo_video_jobs)
    contextual_pressure = signals.get("production_pressure", [])
    direct_pressure = [
        value
        for value in contextual_pressure
        if value.startswith(
            ("capacity_shortfall:", "backlog:", "throughput_pressure:", "outsourcing:")
        )
    ]
    external_capacity_offers = [
        value for value in contextual_pressure if value.startswith("external_capacity_offer:")
    ]
    explicit_capacity = signals.get("production_capacity", [])
    job_pressure_hits = sum(
        bool(posting.production_pressure_signals)
        for posting in web.job_postings
        if set(posting.role_families) & {"creative", "photo_video_production"}
    )
    job_external_capacity_hits = sum(
        bool(posting.external_capacity_signals)
        for posting in web.job_postings
        if set(posting.role_families) & {"creative", "photo_video_production"}
    )
    visible_video = sales.video_assets + sales.embedded_video_players
    static_heavy = sales.image_assets >= 12 and visible_video * 8 < sales.image_assets
    agency_capacity_corpus = company_self_corpus(observation, for_delivery=True)
    agency_external_capacity_hits = (
        [term for term in EXTERNAL_CAPACITY_TERMS if term in agency_capacity_corpus]
        if archetype == "agency"
        else []
    )
    catalog_load = max(
        sales.catalog_total_hint,
        sales.catalog_available_products,
        web.sitemap_counts.get("products", 0),
    )
    use_case_hits = contextual_use_case_hits
    workload_pressure = bool(
        recurring_met
        and (
            recent_demand >= 3
            or catalog_load >= 50
            or use_case_hits >= 2
            or (direct_ad_evidence and scoreable_active_creatives >= 5)
        )
    )
    distribution_production_mismatch = bool(
        distribution_met
        and recurring_met
        and (open_production_roles or static_heavy or direct_pressure or workload_pressure)
    )
    gap_families: set[str] = set()
    if open_production_roles or job_pressure_hits:
        gap_families.add("creative_hiring")
    if direct_pressure:
        gap_families.add("first_party_pressure")
    if job_external_capacity_hits or any(
        value.startswith("outsourcing:") for value in direct_pressure
    ):
        gap_families.add("externalized_workflow")
    if distribution_production_mismatch:
        gap_families.add("distribution_workload_mismatch")
    if workload_pressure and (static_heavy or use_case_hits >= 2):
        gap_families.add("workload_format_pressure")
    raw_gap_points = (
        _scale_points(open_production_roles, ((5, 11), (2, 8), (1, 5)))
        + min(job_pressure_hits * 3, 6)
        + min(len(direct_pressure) * 5, 10)
        + min(job_external_capacity_hits * 4, 6)
        + (4 if static_heavy else 0)
        + (5 if distribution_production_mismatch else 0)
        + (
            3
            if archetype == "agency"
            and agency_buyer_ready
            and (agency_external_capacity_hits or external_capacity_offers)
            else 0
        )
    )
    capacity_counterweight = min(10, len(explicit_capacity) * 4)
    production_capacity_gap = min(25, max(0, raw_gap_points - capacity_counterweight))
    # This remains a provisional machine hypothesis. Require at least two causally
    # independent public families; one job, one keyword, or one static-heavy page can
    # never establish the principal signal by itself.
    primary_gap_families = gap_families & {
        "creative_hiring",
        "first_party_pressure",
        "externalized_workflow",
    }
    gap_met = bool(
        production_capacity_gap >= 10
        and len(gap_families) >= 2
        and primary_gap_families
        and (not explicit_capacity or len(gap_families) >= 3 or bool(direct_pressure))
    )
    if open_production_roles:
        observations.append(
            HypothesisClaim(
                claim=(
                    f"The public careers footprint includes {open_production_roles} creative, "
                    "photo, or video openings."
                ),
                epistemic_status="observed",
                truth_state="met",
                dimension="production_capacity_gap",
                source_url=careers_url,
                observed_at=observed_at,
                confidence="medium",
            )
        )
    if static_heavy:
        observations.append(
            HypothesisClaim(
                claim=(
                    f"The inspected public pages contain {sales.image_assets} image assets "
                    f"and {visible_video} visible video assets or players."
                ),
                epistemic_status="observed",
                truth_state="met",
                dimension="production_capacity_gap",
                source_url=demand_url,
                observed_at=observed_at,
                confidence="low",
            )
        )
    if agency_external_capacity_hits:
        observations.append(
            HypothesisClaim(
                claim=(
                    "First-party agency language references external or flexible production "
                    "capacity: " + ", ".join(agency_external_capacity_hits[:4]) + "."
                ),
                epistemic_status="observed",
                truth_state="unknown",
                dimension="production_capacity_gap",
                source_url=demand_url,
                observed_at=observed_at,
                confidence="low",
            )
        )
    if direct_pressure:
        pressure_url = next(
            (value.rsplit("source=", 1)[1] for value in direct_pressure if "source=" in value),
            demand_url,
        )
        observations.append(
            HypothesisClaim(
                claim=(
                    f"First-party contextual extraction found {len(direct_pressure)} explicit "
                    "production-pressure or outsourcing statement(s)."
                ),
                epistemic_status="observed",
                truth_state="met",
                dimension="production_capacity_gap",
                source_url=pressure_url,
                observed_at=observed_at,
                confidence="medium",
            )
        )
    if explicit_capacity:
        capacity_url = next(
            (value.rsplit("source=", 1)[1] for value in explicit_capacity if "source=" in value),
            demand_url,
        )
        observations.append(
            HypothesisClaim(
                claim=(
                    f"First-party pages also describe {len(explicit_capacity)} internal "
                    "creative or production capacity signal(s), which is counterevidence."
                ),
                epistemic_status="observed",
                truth_state="not_met",
                dimension="production_capacity_gap",
                source_url=capacity_url,
                observed_at=observed_at,
                confidence="medium",
            )
        )
    if production_capacity_gap:
        inferences.append(
            HypothesisClaim(
                claim=(
                    "Public demand, hiring, asset-mix, or external-capacity language suggests "
                    "production may be lagging distribution; it does not prove willingness "
                    "to outsource."
                ),
                epistemic_status="inferred",
                truth_state="met" if gap_met else "unknown",
                signal="production_capacity_gap",
                dimension="production_capacity_gap",
                source_url=careers_url if open_production_roles else demand_url,
                observed_at=observed_at,
                confidence="medium" if gap_met else "low",
            )
        )

    ai_workflow_hits = len(signals.get("ai_workflow", []))
    rights_evidence = signals.get("rights_risk", [])
    ai_restrictions = [value for value in rights_evidence if value.startswith("ai_restriction:")]
    governed_rights = [
        value
        for value in rights_evidence
        if value.startswith(("usage_rights:", "likeness_consent:", "authenticity_fidelity:"))
    ]
    ai_job_hits = sum(bool(posting.ai_workflow_signals) for posting in web.job_postings)
    ai_brand_readiness = min(
        15,
        max(
            0,
            _scale_points(ai_workflow_hits + ai_job_hits, ((4, 9), (2, 7), (1, 5)))
            + _scale_points(use_case_hits, ((4, 3), (2, 2), (1, 1)))
            + _scale_points(len(governed_rights), ((2, 2), (1, 1)))
            + _scale_points(catalog_load, ((250, 2), (50, 1)))
            - min(len(ai_restrictions) * 6, 10),
        ),
    )
    if ai_brand_readiness:
        inferences.append(
            HypothesisClaim(
                claim=(
                    "Explicit public workflow, use-case, catalog, or rights-governance evidence "
                    "may support an AI-native production lane; permissibility remains a human gate."
                ),
                epistemic_status="inferred",
                truth_state="unknown",
                dimension="ai_brand_readiness",
                source_url=demand_url,
                observed_at=observed_at,
                confidence="medium" if ai_workflow_hits or ai_job_hits else "low",
            )
        )

    commercial_triggers = sales.news_commercial_triggers + sales.feed_commercial_items
    commercial_recent_urls = sum(
        recent.get(key, 0) for key in ("products", "campaigns", "case_studies")
    )
    timing_access = min(
        15,
        _scale_points(commercial_recent_urls, ((20, 5), (7, 4), (3, 3), (1, 2)))
        + _scale_points(commercial_triggers, ((5, 5), (2, 3), (1, 2)))
        + _scale_points(web.creative_jobs + web.growth_jobs, ((5, 4), (2, 3), (1, 2)))
        + (1 if observation.contacts.preferred_email or observation.contact_urls else 0)
        + (
            3
            if direct_ad_evidence and scoreable_new_7d
            else 2
            if direct_ad_evidence and scoreable_new_30d
            else 0
        ),
    )
    if direct_ad_evidence and (scoreable_new_7d or scoreable_new_30d):
        observations.append(
            HypothesisClaim(
                claim=(
                    f"The verified advertiser launched {scoreable_new_7d} creative groups "
                    f"in 7 days and {scoreable_new_30d} in 30 days."
                ),
                epistemic_status="observed",
                truth_state="met",
                dimension="timing_access",
                source_url=ad_source_url,
                observed_at=ads.last_observed_at or observed_at,
                confidence="medium",
            )
        )
    if commercial_triggers or commercial_recent_urls or web.creative_jobs or web.growth_jobs:
        observations.append(
            HypothesisClaim(
                claim=(
                    f"Public sources show {commercial_triggers} commercial triggers, "
                    f"{commercial_recent_urls} recently changed commercial URLs, and "
                    f"{web.creative_jobs + web.growth_jobs} creative or growth openings."
                ),
                epistemic_status="observed",
                truth_state="met",
                dimension="timing_access",
                source_url=careers_url if web.creative_jobs or web.growth_jobs else demand_url,
                observed_at=observed_at,
                confidence="medium" if timing_access >= 7 else "low",
            )
        )

    derived_signals = []
    if distribution_production_mismatch:
        derived_signals.append("distribution_production_mismatch")
    if distribution_met and static_heavy:
        derived_signals.append("video_opportunity_gap")
        derived_signals.append("format_debt")
    if recurring_met and sales.feed_commercial_items >= 2 and visible_video <= 1:
        derived_signals.append("content_exhaust")
    if commercial_triggers and open_production_roles:
        derived_signals.append("launch_collision")
    if agency_external_capacity_hits:
        derived_signals.append("agency_external_capacity_model")
    if direct_pressure and any(value.startswith("outsourcing:") for value in direct_pressure):
        derived_signals.append("production_avoidance_opportunity")
    if archetype == "agency" and agency_buyer_ready:
        derived_signals.append("agency_buyer_candidate")

    principal_signals = HypothesisPrincipalSignals(
        distribution_investment=HypothesisSignal(
            truth_state="met" if distribution_met else "unknown",
            epistemic_status=(
                "observed" if direct_ad_evidence or paid_hits or public_marketing_ids else "unknown"
            ),
            confidence=(
                "high"
                if direct_ad_evidence and scoreable_active_creatives >= 2
                else "medium"
                if distribution_met
                else "low"
            ),
            summary="Public evidence suggests paid visual distribution."
            if distribution_met
            else "Paid visual distribution is not established by current public evidence.",
            evidence_urls=list(
                dict.fromkeys(
                    url
                    for url in (ad_source_url, distribution_url)
                    if url and distribution_investment
                )
            ),
        ),
        recurring_creative_demand=HypothesisSignal(
            truth_state="met" if recurring_met else "unknown",
            epistemic_status=(
                "observed"
                if (direct_ad_evidence and scoreable_unique_creatives)
                or recurring_hits
                or creative_hits
                or recent_demand
                else "unknown"
            ),
            confidence="medium" if recurring_met else "low",
            summary="Public cadence and workload evidence suggest recurring creative demand."
            if recurring_met
            else "Three months of recurring creative demand remains unknown.",
            evidence_urls=list(
                dict.fromkeys(
                    url for url in (ad_source_url, demand_url) if url and recurring_creative_demand
                )
            ),
        ),
        production_capacity_gap=HypothesisSignal(
            truth_state="met" if gap_met else "unknown",
            epistemic_status="inferred" if production_capacity_gap else "unknown",
            confidence="medium" if gap_met else "low",
            summary=(
                "Production may be failing to keep pace with visible distribution and demand."
                if gap_met
                else "A production-capacity gap is not established by current public evidence."
            ),
            evidence_urls=(
                [careers_url if open_production_roles else demand_url]
                if (careers_url if open_production_roles else demand_url)
                and production_capacity_gap
                else []
            ),
        ),
    )
    blockers = [
        f"Principal signal {name} remains unknown"
        for name, value in principal_signals.model_dump().items()
        if value["truth_state"] != "met"
    ]
    blockers.extend(
        [
            "Six hard gates require human verification before pursuit",
            "Economic buyer and plausible $60,000 annual capacity are unverified",
        ]
    )
    if archetype == "agency" and not agency_buyer_ready:
        blockers.append(
            "Agency remains referral-only until recurring delivery and placement-capacity "
            "evidence clears the direct-buyer threshold"
        )
    total = (
        distribution_investment
        + recurring_creative_demand
        + production_capacity_gap
        + ai_brand_readiness
        + timing_access
    )
    review_ready = bool(
        total >= 70
        and all(value["truth_state"] == "met" for value in principal_signals.model_dump().values())
    )
    return CreativeCapacityHypothesis(
        version="creative-capacity-v3-provisional",
        distribution_investment=distribution_investment,
        recurring_creative_demand=recurring_creative_demand,
        production_capacity_gap=production_capacity_gap,
        ai_brand_readiness=ai_brand_readiness,
        timing_access=timing_access,
        principal_signals=principal_signals,
        derived_signals=derived_signals,
        observations=observations,
        inferences=inferences,
        review_ready=review_ready,
        blockers=blockers,
    )


def _growth_profile(
    observation: SiteObservation, portfolio_count: int
) -> tuple[int, list[str], dict[str, int]]:
    """Score independent public signals that commonly accompany company growth."""
    web = observation.web
    sales = web.sales
    recent = web.sitemap_recent_counts
    technology_count = len(set(sales.growth_technologies + sales.creative_technologies))
    public_marketing_ids = sum(
        bool(observation.public_ids.get(key))
        for key in ("google_tag", "meta_pixel", "tiktok_pixel", "linkedin_partner")
    )
    recent_commercial = (
        sales.feed_commercial_items
        + sales.news_commercial_triggers
        + recent.get("products", 0)
        + recent.get("campaigns", 0)
        + recent.get("case_studies", 0)
    )
    dimensions = {
        "technology_investment": min(
            _scale_points(web.vendors.stack_score, ((70, 18), (45, 12), (25, 6)))
            + _scale_points(len(web.vendors.category_counts), ((8, 8), (5, 5), (3, 2)))
            + _scale_points(len(web.vendors.corroborated_vendors), ((3, 5), (1, 2)))
            + _scale_points(technology_count, ((8, 7), (5, 4), (2, 2))),
            30,
        ),
        "go_to_market_investment": min(
            _scale_points(sales.paid_growth_score, ((70, 16), (50, 11), (30, 6)))
            + _scale_points(public_marketing_ids, ((3, 7), (2, 4), (1, 2)))
            + _scale_points(len(sales.growth_technologies), ((6, 5), (3, 3), (1, 1))),
            25,
        ),
        "operating_scale": min(
            _scale_points(web.open_jobs, ((50, 12), (20, 8), (5, 4), (1, 2)))
            + _scale_points(web.creative_jobs + web.growth_jobs, ((10, 8), (4, 5), (1, 2)))
            + _scale_points(
                portfolio_count + web.location_objects,
                ((10, 5), (3, 3), (1, 1)),
            ),
            20,
        ),
        "recent_velocity": min(
            _scale_points(web.sitemap_recent_30d, ((30, 12), (10, 8), (3, 4), (1, 2)))
            + _scale_points(sales.feed_recent_30d, ((8, 8), (3, 5), (1, 2)))
            + _scale_points(recent_commercial, ((8, 8), (3, 5), (1, 2)))
            + _scale_points(web.sitemap_recent_90d, ((100, 4), (25, 2), (5, 1))),
            25,
        ),
    }
    active = sum(value >= 5 for value in dimensions.values())
    growth_score = min(100, sum(dimensions.values()))
    reasons: list[str] = []
    if dimensions["technology_investment"] >= 8:
        reasons.append(
            f"Technology investment: {web.vendors.stack_score}/100 vendor stack across "
            f"{len(web.vendors.category_counts)} categories"
        )
    if dimensions["go_to_market_investment"] >= 8:
        reasons.append(
            f"Go-to-market investment: paid-growth stack score {sales.paid_growth_score}/100"
        )
    if dimensions["operating_scale"] >= 5:
        roles = web.creative_jobs + web.growth_jobs
        reasons.append(
            f"Operating scale: {roles} creative/growth roles and {web.open_jobs} public openings"
        )
    if dimensions["recent_velocity"] >= 5:
        reasons.append(
            f"Recent velocity: {recent_commercial} product, campaign, case-study, "
            "feed, or news signals"
        )
    if active >= 3 and growth_score >= 55:
        reasons.append("Multiple independent growth dimensions corroborate expansion or investment")
    return growth_score, reasons, dimensions


def score_candidate(
    candidate: DiscoveredHost,
    observation: SiteObservation,
    *,
    previous_observation: SiteObservation | None = None,
    ad_intelligence: AdIntelligence | None = None,
    english_only: bool = True,
    supplemental_events: Sequence[BuyingSignalEvent] = (),
    reference_time: datetime | None = None,
    trace: bool = True,
) -> ScoreCard:
    reference_time = utc_reference(reference_time)
    # All projections are disposable. Never mutate raw caller observations or prior scans.
    observation = observation.model_copy(deep=True)
    previous_observation = (
        previous_observation.model_copy(deep=True) if previous_observation else None
    )
    _project_current_frontier_hiring(observation, reference_time=reference_time)
    if previous_observation is not None:
        _project_current_frontier_hiring(previous_observation, reference_time=reference_time)
    refresh_sales_intelligence(observation)
    profile = PLATFORM_PROFILES.get(candidate.source_apex)
    components = {name: 0 for name in COMPONENT_MAX}
    reasons: list[str] = []
    evidence: list[str] = []
    disqualifiers: list[str] = []

    if not observation.live:
        disqualifiers.append("Site is not currently reachable as a public HTML page")
    if observation.status_code in {401, 403}:
        disqualifiers.append("Site is gated or blocks public inspection")
    language = assess_observation_language(observation)
    if english_only and language.english_presentation is False:
        disqualifiers.append("Website does not present primarily in English")
    parked_hosts = ("afternic.com", "bodis.com", "dan.com", "hugedomains.com", "sedo.com")
    final_host = (
        observation.canonical_host
        or (urlparse(observation.final_url).hostname if observation.final_url else None)
        or observation.hostname
    ).lower()
    if observation.signals.get("parked") or any(
        final_host == host or final_host.endswith(f".{host}") for host in parked_hosts
    ):
        disqualifiers.append("Domain is parked or offered for sale")
    text_size = len(observation.text_excerpt.strip())
    if observation.live and text_size < 80:
        disqualifiers.append("Public page contains too little business evidence")

    if profile:
        components["ability_to_pay"] += profile.ability_prior
        components["recurring_demand"] += profile.recurring_prior
        components["beings_fit"] += profile.fit_prior
        reasons.append(profile.note)

    company_role = assess_company_role(observation, candidate=candidate)
    observation = project_company_self_observation(observation, company_role=company_role)
    if previous_observation is not None:
        previous_observation = project_company_self_observation(previous_observation)
    signals = observation.signals
    archetype = classify_archetype(candidate, observation, company_role=company_role)
    agency_hits = len(signals.get("agency", [])) if archetype == "agency" else 0
    ecommerce_hits = len(signals.get("ecommerce", [])) if archetype == "ecommerce_brand" else 0

    recurring_hits = len(signals.get("recurring", []))
    creative_hits = len(signals.get("creative_demand", []))
    paid_hits = len(signals.get("paid_distribution", []))
    scale_hits = len(signals.get("scale", []))
    pricing_hits = len(signals.get("pricing", []))
    cross_site_hits = len(signals.get("cross_site", []))

    components["recurring_demand"] += min(recurring_hits * 3, 9)
    components["recurring_demand"] += min(creative_hits * 2, 8)
    if agency_hits >= 3:
        components["recurring_demand"] += 4
        evidence.append(f"Agency/service signals: {', '.join(signals['agency'][:4])}")

    components["paid_distribution"] += min(paid_hits * 4, 16)
    ads = ad_intelligence or AdIntelligence()
    verified_ads = ads.identity_state == "verified" and ads.identity_confidence >= 80
    projected_scoreable_subset = ads.scoreable_unique_creatives is not None
    if projected_scoreable_subset:
        scoreable_active_creatives = int(ads.scoreable_active_unique_creatives or 0)
        scoreable_new_7d = int(ads.scoreable_new_creatives_7d or 0)
        scoreable_new_30d = int(ads.scoreable_new_creatives_30d or 0)
        scoreable_windows = int(ads.scoreable_observation_windows_90d or 0)
    else:
        scoreable_active_creatives = ads.active_unique_creatives
        scoreable_new_7d = ads.new_creatives_7d
        scoreable_new_30d = ads.new_creatives_30d
        scoreable_windows = ads.observation_windows_90d
    scoreable_platforms = ads.scoreable_platforms if projected_scoreable_subset else ads.platforms
    scoreable_ads = verified_ads and scoreable_active_creatives > 0
    if scoreable_ads:
        components["paid_distribution"] += _scale_points(
            scoreable_active_creatives,
            ((10, 16), (5, 13), (2, 10), (1, 7)),
        )
        evidence.append(
            f"Verified paid ads: {scoreable_active_creatives} active creative groups across "
            f"{len(scoreable_platforms)} platform surfaces"
        )
    if "Meta Pixel" in observation.technologies:
        components["paid_distribution"] += 2
    if "TikTok Pixel" in observation.technologies:
        components["paid_distribution"] += 2
    if "Google Ads/Analytics" in observation.technologies:
        components["paid_distribution"] += 1
    if paid_hits:
        evidence.append(
            f"Paid-distribution language: {', '.join(signals['paid_distribution'][:4])}"
        )

    components["ability_to_pay"] += min(scale_hits * 2, 8)
    components["ability_to_pay"] += min(pricing_hits, 3)
    if len(observation.pages) >= 3:
        components["ability_to_pay"] += 1
    if scale_hits:
        evidence.append(f"Scale signals: {', '.join(signals['scale'][:4])}")
    if pricing_hits:
        evidence.append(f"Public pricing found: {', '.join(signals['pricing'][:3])}")

    components["beings_fit"] += min(creative_hits * 2, 10)
    if archetype in {"ecommerce_brand", "media_membership"}:
        components["beings_fit"] += 2
    if creative_hits:
        evidence.append(f"Creative-demand signals: {', '.join(signals['creative_demand'][:5])}")

    components["operational_maturity"] += min(scale_hits * 2, 6)
    if signals.get("hiring"):
        components["operational_maturity"] += 2
        evidence.append(f"Hiring signal: {', '.join(signals['hiring'][:2])}")
    if observation.canonical_host and observation.canonical_host != candidate.hostname:
        components["operational_maturity"] += 2
        evidence.append(f"Tenant resolves to branded domain {observation.canonical_host}")
    if len(observation.social_urls) >= 3:
        components["operational_maturity"] += 1
    if cross_site_hits:
        components["operational_maturity"] += min(cross_site_hits, 2)
        components["ability_to_pay"] += min(cross_site_hits // 2, 1)
        evidence.append(f"Cross-site corroboration: {signals['cross_site'][0]}")

    outreach_emails = _outreach_emails(observation)
    if outreach_emails:
        components["reachability"] += 5
        evidence.append(f"Public business email: {outreach_emails[0]}")
    if observation.contact_urls:
        components["reachability"] += 3
    if observation.social_urls:
        components["reachability"] += 2

    if recurring_hits or creative_hits:
        reasons.append("Site shows evidence of repeatable creative work")
    if paid_hits:
        reasons.append("Site explicitly discusses paid acquisition or performance")
    if scale_hits:
        reasons.append("Site contains organizational or market-scale signals")
    if not evidence and observation.live:
        reasons.append("Live business evidence is weak; retain for monitoring only")

    evidence_families: list[str] = []
    if recurring_hits >= 2 or creative_hits >= 3 or agency_hits >= 4:
        evidence_families.append("recurring_creative_demand")
    if (
        scoreable_ads
        or paid_hits >= 2
        or sum(
            technology in observation.technologies
            for technology in ("Meta Pixel", "TikTok Pixel", "Google Ads/Analytics")
        )
        >= 2
    ):
        evidence_families.append("paid_distribution")
    strong_platform = bool(profile and profile.ability_prior >= 10)
    if strong_platform or scale_hits >= 2 or (archetype == "agency" and agency_hits >= 4):
        evidence_families.append("ability_to_pay")
    if (
        len(observation.pages) >= 3
        or bool(signals.get("hiring"))
        or bool(observation.canonical_host and observation.canonical_host != candidate.hostname)
        or len(observation.social_urls) >= 3
    ):
        evidence_families.append("operational_maturity")
    if outreach_emails or observation.contact_urls:
        evidence_families.append("reachability")
    if cross_site_hits:
        evidence_families.append("cross_site_corroboration")
    web = observation.web
    seo_trajectory_points = _seo_trajectory_points(observation)
    recent = web.sitemap_recent_counts
    portfolio_count = len(legacy_agency_portfolio_domains(web)) if archetype == "agency" else 0

    demand_score = 0
    demand_score += min(recurring_hits * 6, 18)
    demand_score += min(creative_hits * 5, 20)
    demand_score += min(paid_hits * 4, 12)
    demand_score += min(ecommerce_hits * 2, 6)
    if archetype == "agency":
        demand_score += min(agency_hits * 4, 16)
    demand_score += _scale_points(portfolio_count, ((10, 26), (5, 20), (3, 14), (1, 6)))
    demand_score += min(recent.get("case_studies", 0) * 5, 15)
    demand_score += min(
        (recent.get("products", 0) + recent.get("campaigns", 0)) * 2,
        14,
    )
    demand_score += min(web.creative_jobs * 5, 15)
    demand_score += min(web.growth_jobs * 2, 8)
    demand_score += min(web.vendors.creative_commerce_score // 10, 10)
    demand_score += min(web.sales.creative_demand_score // 12, 8)
    demand_score += min(web.sales.paid_growth_score // 20, 5)
    if scoreable_ads:
        demand_score += min(scoreable_active_creatives * 2, 14)
        demand_score += 10 if ads.recurring_established else min(scoreable_windows * 2, 4)
    demand_score = min(100, demand_score)

    capacity_score = min((profile.ability_prior if profile else 0) * 2, 28)
    capacity_score += min(scale_hits * 7, 21)
    capacity_score += _scale_points(
        web.sitemap_counts.get("products", 0) + web.product_objects,
        ((1_000, 20), (200, 15), (50, 10), (10, 5), (1, 2)),
    )
    capacity_score += _scale_points(
        web.max_review_count,
        ((1_000, 18), (250, 14), (50, 9), (10, 5)),
    )
    capacity_score += _scale_points(
        web.location_objects + web.sitemap_counts.get("locations", 0),
        ((50, 14), (10, 10), (3, 6), (1, 2)),
    )
    capacity_score += _scale_points(web.open_jobs, ((100, 18), (30, 14), (10, 10), (3, 6), (1, 3)))
    if any(
        technology in observation.technologies
        for technology in ("Salesforce Commerce Cloud", "VTEX")
    ):
        capacity_score += 14
    elif "BigCommerce" in observation.technologies:
        capacity_score += 9
    elif "Shopify" in observation.technologies:
        capacity_score += 5
    if len(observation.pages) >= 3:
        capacity_score += 5
    if observation.canonical_host and observation.canonical_host != candidate.hostname:
        capacity_score += 6
    if len(observation.social_urls) >= 1:
        capacity_score += 2
    if signals.get("hiring"):
        capacity_score += 5
    if archetype == "agency" and agency_hits >= 4:
        capacity_score += 12
    capacity_score += min(portfolio_count * 2, 10)
    capacity_score += min(web.common_crawl_pages // 40, 5)
    capacity_score += min(web.vendors.stack_score // 5, 18)
    capacity_score += min(web.sales.commerce_scale_score // 12, 8)
    capacity_score += min(web.sales.operating_capacity_score // 20, 5)
    capacity_score = min(100, capacity_score)

    timing_score = _scale_points(web.sitemap_recent_30d, ((50, 35), (15, 28), (5, 20), (1, 10)))
    timing_score += _scale_points(web.sitemap_recent_90d, ((100, 18), (25, 14), (5, 9), (1, 4)))
    timing_score += min(web.creative_jobs * 8, 24)
    timing_score += min(web.growth_jobs * 4, 16)
    if signals.get("hiring"):
        timing_score += 8
    if any(
        value in signals.get("creative_demand", [])
        for value in ("product launch", "new collection", "campaign")
    ):
        timing_score += 10
    timing_score += min(web.sales.timing_score // 8, 12)
    if scoreable_ads:
        timing_score += min(12, scoreable_new_7d * 4)
        if not scoreable_new_7d:
            timing_score += min(8, scoreable_new_30d * 2)
    changed_reasons: list[str] = []
    if previous_observation:
        previous_web = previous_observation.web
        product_delta = web.sitemap_counts.get("products", 0) - previous_web.sitemap_counts.get(
            "products", 0
        )
        case_delta = web.sitemap_counts.get("case_studies", 0) - previous_web.sitemap_counts.get(
            "case_studies", 0
        )
        job_delta = (
            web.creative_jobs
            + web.growth_jobs
            - (previous_web.creative_jobs + previous_web.growth_jobs)
        )
        if product_delta > 0:
            timing_score += min(15, 5 + product_delta)
            changed_reasons.append(f"{product_delta} net new product URLs")
        if case_delta > 0:
            timing_score += min(18, 6 + case_delta * 2)
            changed_reasons.append(f"{case_delta} net new case-study URLs")
        if job_delta > 0:
            timing_score += min(15, 5 + job_delta * 3)
            changed_reasons.append(f"{job_delta} net new creative/growth roles")
        credible_vendors = {
            item.vendor for item in web.vendors.evidence if item.confidence != "low"
        }
        previous_credible_vendors = {
            item.vendor for item in previous_web.vendors.evidence if item.confidence != "low"
        }
        added_vendors = sorted(credible_vendors - previous_credible_vendors)
        if added_vendors:
            timing_score += min(10, 3 + len(added_vendors) * 2)
            changed_reasons.append(f"New vendor evidence: {', '.join(added_vendors[:4])}")
        news_delta = (
            web.sales.news_commercial_triggers - previous_web.sales.news_commercial_triggers
        )
        feed_trigger_delta = (
            web.sales.feed_commercial_items - previous_web.sales.feed_commercial_items
        )
        if news_delta > 0:
            timing_score += min(12, 4 + news_delta * 3)
            changed_reasons.append(f"{news_delta} new commercial news triggers")
        if feed_trigger_delta > 0:
            timing_score += min(10, 3 + feed_trigger_delta * 2)
            changed_reasons.append(f"{feed_trigger_delta} new first-party launch signals")
        if (
            previous_observation.content_hash
            and observation.content_hash != previous_observation.content_hash
        ):
            timing_score += 5
    timing_score = min(100, timing_score)

    growth_score, growth_reasons, growth_dimensions = _growth_profile(observation, portfolio_count)
    if seo_trajectory_points:
        growth_dimensions["recent_velocity"] = min(
            25,
            growth_dimensions["recent_velocity"] + seo_trajectory_points,
        )
        growth_score = min(100, sum(growth_dimensions.values()))
    growth_boosts = {
        "recurring_demand": _scale_points(
            growth_dimensions["recent_velocity"], ((18, 3), (10, 2), (5, 1))
        ),
        "paid_distribution": _scale_points(
            growth_dimensions["go_to_market_investment"], ((18, 3), (10, 2), (5, 1))
        ),
        "ability_to_pay": _scale_points(
            growth_dimensions["operating_scale"], ((14, 3), (8, 2), (4, 1))
        ),
        "operational_maturity": _scale_points(
            growth_dimensions["technology_investment"], ((20, 3), (12, 2), (6, 1))
        ),
    }
    if sum(value >= 5 for value in growth_dimensions.values()) < 2:
        growth_boosts = {name: min(value, 1) for name, value in growth_boosts.items()}
    if growth_score >= 50 and sum(value >= 5 for value in growth_dimensions.values()) >= 2:
        evidence_families.append("growth_investment")
    for name, boost in growth_boosts.items():
        components[name] += boost
    components = {name: _cap(name, value) for name, value in components.items()}
    raw_total = sum(components.values())
    total = min(raw_total, 34) if disqualifiers else raw_total

    nonzero_categories = sum(bool(signals.get(key)) for key in signals)
    if total >= 70 and nonzero_categories >= 4:
        confidence = "high"
    elif total >= 45 and nonzero_categories >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    data_confidence = 0
    data_confidence += 20 if observation.live else 0
    data_confidence += min(len(observation.pages) * 5, 15)
    data_confidence += 20 if web.sitemaps_checked else 0
    data_confidence += 10 if web.jsonld_types else 0
    data_confidence += 15 if web.ats_boards else 0
    data_confidence += 15 if outreach_emails or observation.contact_urls else 0
    data_confidence += 5 if observation.public_ids else 0
    data_confidence += 10 if len(observation.text_excerpt.strip()) >= 250 else 0
    data_confidence += 5 if web.common_crawl_collection else 0
    data_confidence += 5 if len(web.vendors.evidence) >= 2 else 0
    data_confidence += 5 if web.sales.source_urls else 0
    data_confidence += 10 if scoreable_ads else 0
    data_confidence = min(100, data_confidence)

    placement = assess_placement_fit(
        observation,
        archetype=archetype,
        demand_score=demand_score,
        capacity_score=capacity_score,
        data_confidence=data_confidence,
        company_role=company_role,
    )

    reachability_score = round(components["reachability"] / COMPONENT_MAX["reachability"] * 100)
    contact_priority = round(
        demand_score * 0.45
        + capacity_score * 0.35
        + timing_score * 0.15
        + reachability_score * 0.05
        + growth_score * 0.05
    )
    contact_priority = round(contact_priority * (0.75 + data_confidence * 0.0025))
    if placement.score:
        contact_priority = round(
            placement.score * 0.50
            + demand_score * 0.20
            + capacity_score * 0.15
            + timing_score * 0.10
            + reachability_score * 0.05
        )
    if demand_score < 25:
        contact_priority = min(contact_priority, 35)
    elif demand_score < 45:
        contact_priority = min(contact_priority, 60)
    if capacity_score < 30:
        contact_priority = min(contact_priority, 45)
    elif capacity_score < 45:
        contact_priority = min(contact_priority, 65)
    if disqualifiers:
        contact_priority = min(contact_priority, 25)

    opportunity_reasons: list[str] = list(growth_reasons)
    if scoreable_ads:
        opportunity_reasons.append(
            f"{scoreable_active_creatives} verified active paid creative groups on "
            + ", ".join(scoreable_platforms[:4])
        )
    if portfolio_count:
        opportunity_reasons.append(
            f"{portfolio_count} probable client domains across agency work pages"
        )
    if recent.get("products", 0) or recent.get("campaigns", 0):
        opportunity_reasons.append(
            f"{recent.get('products', 0) + recent.get('campaigns', 0)} recent product/campaign URLs"
        )
    if recent.get("case_studies", 0):
        opportunity_reasons.append(f"{recent['case_studies']} recently updated case-study URLs")
    if web.creative_jobs or web.growth_jobs:
        opportunity_reasons.append(
            f"{web.creative_jobs + web.growth_jobs} open creative/growth roles"
        )
    if web.max_review_count:
        opportunity_reasons.append(
            f"Structured review footprint reaches {web.max_review_count:,} reviews"
        )
    if web.sitemap_counts.get("products", 0):
        opportunity_reasons.append(
            f"{web.sitemap_counts['products']:,} product URLs in public sitemaps"
        )
    if web.vendors.stack_score:
        opportunity_reasons.append(
            f"Vendor stack {web.vendors.stack_score}/100 across "
            f"{len(web.vendors.category_counts)} operating categories"
        )
    if web.vendors.creative_commerce_score:
        opportunity_reasons.append(
            f"Creative-commerce stack relevance {web.vendors.creative_commerce_score}/100"
        )
    if web.vendors.corroborated_vendors:
        opportunity_reasons.append(
            "DNS and disclosure corroborate: " + ", ".join(web.vendors.corroborated_vendors[:4])
        )
    opportunity_reasons.extend(web.sales.reasons[:6])
    opportunity_reasons.extend(changed_reasons)
    if demand_score >= 50:
        evidence_families.append("verified_demand")
    if capacity_score >= 45:
        evidence_families.append("capacity_to_buy")
    if timing_score >= 35:
        evidence_families.append("active_trigger")
    if web.sitemaps_checked:
        evidence_families.append("first_party_web_footprint")
    if web.ats_boards:
        evidence_families.append("public_hiring_data")
    if web.vendors.stack_score >= 45:
        evidence_families.append("vendor_stack_capacity")
    if web.vendors.creative_commerce_score >= 30:
        evidence_families.append("creative_commerce_stack")
    if web.sales.creative_demand_score >= 50:
        evidence_families.append("creative_operations")
    if web.sales.paid_growth_score >= 50:
        evidence_families.append("paid_growth_stack")
    if scoreable_ads:
        evidence_families.append("verified_paid_ads")
    if web.sales.commerce_scale_score >= 45:
        evidence_families.append("public_commerce_scale")
    if web.sales.news_commercial_triggers or web.sales.feed_commercial_items >= 2:
        evidence_families.append("commercial_trigger")
    evidence_families = list(dict.fromkeys(evidence_families))

    if scoreable_ads and scoreable_new_7d:
        latest_trigger = "Verified paid creative launched in the last 7 days"
    elif scoreable_ads and scoreable_new_30d:
        latest_trigger = "Verified paid creative launched in the last 30 days"
    elif web.creative_jobs or web.growth_jobs:
        latest_trigger = "Hiring for creative or growth"
    elif web.sales.news_commercial_triggers:
        latest_trigger = "Recent launch, expansion, partnership, or funding coverage"
    elif web.sales.feed_commercial_items >= 2:
        latest_trigger = "Active first-party launch or campaign publishing"
    elif changed_reasons and changed_reasons[-1].startswith("New vendor evidence"):
        latest_trigger = changed_reasons[-1]
    elif recent.get("case_studies", 0):
        latest_trigger = "New or updated client work"
    elif recent.get("products", 0) or recent.get("campaigns", 0):
        latest_trigger = "Active product or campaign launches"
    elif changed_reasons:
        latest_trigger = changed_reasons[0]
    elif web.sitemap_recent_30d:
        latest_trigger = "Active site publishing"
    elif growth_score >= 50:
        latest_trigger = "Multiple growth investment signals"
    else:
        latest_trigger = ""

    core_families = {
        "recurring_creative_demand",
        "paid_distribution",
        "ability_to_pay",
        "operational_maturity",
    }
    required = core_families | ({"reachability"} if archetype == "agency" else set())
    budget_ready = (
        not disqualifiers
        and total >= 70
        and required.issubset(evidence_families)
        and demand_score >= 55
        and capacity_score >= 45
        and data_confidence >= 40
        and archetype in {"agency", "ecommerce_brand", "saas_or_other"}
    )
    if budget_ready and data_confidence >= 45 and len(evidence_families) >= 5:
        budget_confidence = "high"
    elif core_families.issubset(evidence_families) and total >= 60:
        budget_confidence = "medium"
    else:
        budget_confidence = "low"

    creative_capacity = assess_creative_capacity_hypothesis(
        observation,
        archetype=archetype,
        ad_intelligence=ads,
        agency_buyer_ready=placement.ready and not bool(disqualifiers),
    )
    aggregate_weight = durable_weight(observation.observed_at, reference_time)
    revenue_intelligence = synthesize_revenue_intelligence(
        candidate,
        observation,
        now=reference_time,
        trace=trace,
        historical_events=supplemental_events,
        previous_observation=previous_observation,
        ad_intelligence=ads,
        evidence=RevenueSynthesisEvidence(
            archetype=archetype,
            demand_score=round(demand_score * aggregate_weight),
            capacity_score=round(capacity_score * aggregate_weight),
            timing_score=round(timing_score * aggregate_weight),
            growth_score=round(growth_score * aggregate_weight),
            data_confidence=round(data_confidence * aggregate_weight),
            placement_score=round(placement.score * aggregate_weight),
            placement_ready=placement.ready and not bool(disqualifiers),
            reachability_score=round(reachability_score * aggregate_weight),
        ),
    )
    revenue_intelligence.opportunity.freshness_audit = {
        "policy": FRESHNESS_VERSION,
        "observed_at": observation.observed_at.isoformat(),
        "as_of": reference_time.isoformat(),
        "aggregate_weight": round(durable_weight(observation.observed_at, reference_time), 6),
        "raw_context": {
            "demand": demand_score, "capacity": capacity_score, "timing": timing_score,
            "growth": growth_score, "confidence": data_confidence,
            "placement": placement.score, "reachability": reachability_score,
        },
    }
    revenue_update = revenue_intelligence.hypothesis_update()
    revenue_update["company_role"] = company_role.model_dump(mode="json")
    revenue_update["derived_signals"] = list(
        dict.fromkeys(
            [
                *creative_capacity.derived_signals,
                *revenue_intelligence.derived_signals,
            ]
        )
    )
    creative_capacity = CreativeCapacityHypothesis.model_validate(
        {**creative_capacity.model_dump(), **revenue_update}
    )
    # The legacy blended value remains represented in its component dimensions. Founder
    # priority now drives contact order because it separately accounts for fit, need,
    # timing, access, proof, risk, evidence quality, and uncertainty.
    contact_priority = revenue_intelligence.opportunity.founder_priority
    opportunity_reasons = list(
        dict.fromkeys(
            [
                *revenue_intelligence.opportunity.reasons,
                *opportunity_reasons,
            ]
        )
    )[:10]
    decision_why_now = revenue_intelligence.decision_packet.why_now
    if decision_why_now and not decision_why_now.startswith("No time-sensitive public trigger"):
        latest_trigger = decision_why_now

    return ScoreCard(
        total=total,
        components=components,
        archetype=archetype,
        company_role=company_role,
        confidence=confidence,
        disqualified=bool(disqualifiers),
        disqualifiers=disqualifiers,
        reasons=list(dict.fromkeys(reasons))[:8],
        evidence=list(dict.fromkeys(evidence))[:12],
        budget_ready=budget_ready,
        budget_confidence=budget_confidence,
        evidence_families=evidence_families,
        cross_site_reasons=signals.get("cross_site", [])[:8],
        demand_score=demand_score,
        capacity_score=capacity_score,
        timing_score=timing_score,
        growth_score=growth_score,
        data_confidence=data_confidence,
        contact_priority=contact_priority,
        opportunity_reasons=list(dict.fromkeys(opportunity_reasons))[:10],
        growth_reasons=growth_reasons[:6],
        latest_trigger=latest_trigger,
        placement=placement,
        creative_capacity=creative_capacity,
    )


def blend_ai_score(
    score: ScoreCard, ai_score: int | None, *, verified_recovery: bool = False
) -> int:
    if ai_score is None:
        return score.total
    if score.disqualified and not verified_recovery:
        return score.total
    if score.disqualified:
        return max(0, min(100, round(score.total * 0.3 + ai_score * 0.7)))
    return max(0, min(100, round(score.total * 0.7 + ai_score * 0.3)))
