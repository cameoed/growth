from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import lru_cache
from math import ceil
from pathlib import Path

import httpx

from .agency_discovery import canonicalize_public_website, discover_agencies
from .ai import analysis_cache_key, analyze_candidate
from .async_work import gather_owned
from .company_research import LOCAL_CAPACITY_DEFERRED
from .config import PLATFORM_PROFILES, USER_AGENT, source_hydration_profile
from .cross_site import enrich_cross_site
from .discovery import (
    DiscoveryError,
    discover_apex,
    is_obvious_infrastructure_candidate,
    load_apex_inputs,
)
from .free_intelligence import (
    enrich_common_crawl,
    enrich_site_owned_intelligence,
    latest_common_crawl_index,
)
from .frontier_orchestration import (
    FrontierTarget,
    orchestrate_frontier_intelligence,
)
from .frontier_sources import load_local_frontier_sources
from .inspect import inspect_host
from .language import is_english_eligible
from .models import (
    FRONTIER_SCANNER_IDS,
    AdIntelligence,
    AIAnalysis,
    CompanyAppearanceEvidence,
    DiscoveredHost,
    DiscoveryAssertionEvidence,
    ScoreCard,
    SiteObservation,
    legacy_agency_portfolio_domains,
)
from .platform_network_policy import (
    PlatformApexCircuitBreaker,
    is_platform_tenant,
    is_recoverable_tenant_label,
    is_terminal_fetch_outcome,
)
from .provider_rate_limit import DATABASE, client_pool_slots
from .report import write_reports
from .sales_intelligence import enrich_news_intelligence
from .scan_activity import encode_scan_action
from .scan_diagnostics import child_record, child_step, source_trace
from .scan_resources import metrics as resource_metrics
from .scanner_manifest import validate_manifest
from .scoring import blend_ai_score, score_candidate
from .site_research import DEFAULT_SITE_RESEARCH_PAGES
from .source_scout import scout_sources
from .source_scout import source_scout_output_coverage as _source_scout_output_coverage
from .storage_json import usage_snapshot
from .store import (
    COMPANY_SCAN_FRESH_HOURS,
    LeadStore,
    SnapshotPlanning,
    observation_completes_company_work,
)

SnapshotInputs = SnapshotPlanning | dict[str, object]


@dataclass
class ScanConfig:
    apexes: list[str]
    max_hosts_per_source: int
    max_network_inspections: int
    adaptive_refresh: bool
    force_refresh: bool
    manual_company_refresh: bool
    reuse_core_only: bool
    people_hydration_enabled: bool
    microlink_enabled: bool
    concurrency: int
    timeout: float
    pages_per_site: int
    max_bytes_per_page: int
    english_only: bool
    free_intelligence: bool
    max_sitemaps_per_site: int
    max_common_crawl_lookups: int
    max_news_lookups: int
    max_frontier_scans: int
    frontier_scanners: list[str] | None
    respect_frontier_schedule: bool
    frontier_data_dir: str
    agency_discovery: bool
    ai: bool
    ai_web: bool
    min_ai_score: int
    max_ai_calls: int
    max_ai_web_calls: int
    model: str
    only_hosts: list[str] | None = None
    job_id: str | None = None
    frontier_scanner_manifest: dict[str, list[str]] | None = None


@dataclass
class InspectedCandidate:
    candidate: DiscoveredHost
    observation: SiteObservation
    score: ScoreCard
    ai: AIAnalysis | None = None

    @property
    def final_score(self) -> int:
        verified_recovery = bool(
            self.ai
            and self.ai.company_url
            and self.ai.confidence in {"medium", "high"}
            and self.ai.placement_fit in {"possible", "strong"}
        )
        return blend_ai_score(
            self.score,
            (self.ai.placement_fit_score or self.ai.budget_fit_score if self.ai else None),
            verified_recovery=verified_recovery,
        )


PUBLISHER_APPEARANCE_SOURCE = "publisher-appearance"
LEGACY_PORTFOLIO_SOURCE = "agency-portfolio"
APPEARANCE_EXPANSION_CAP = 500
APPEARANCE_BACKLOG_SOURCES = (
    PUBLISHER_APPEARANCE_SOURCE,
    LEGACY_PORTFOLIO_SOURCE,
)

_APPEARANCE_RELATION_RANK = {
    "case_study": 16,
    "testimonial": 15,
    "portfolio": 14,
    "customer": 12,
    "award": 8,
    "partner": 7,
    "member": 6,
    "sponsor": 6,
    "exhibitor": 5,
    "unknown": 0,
}
_APPEARANCE_CONFIDENCE_RANK = {"low": 0, "medium": 4, "high": 8}


def _assertion_fingerprint(assertion: DiscoveryAssertionEvidence) -> str:
    """Return a stable full-evidence identity, including asset/extraction provenance."""

    return json.dumps(assertion.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _merge_candidate_provenance(
    current: DiscoveredHost,
    incoming: DiscoveredHost,
) -> DiscoveredHost:
    """Merge an additive discovery edge without replacing the company's original source."""

    assertions: dict[str, DiscoveryAssertionEvidence] = {}
    for assertion in [*current.discovery_assertions, *incoming.discovery_assertions]:
        assertions[_assertion_fingerprint(assertion)] = assertion
    return current.model_copy(
        update={
            "first_seen": current.first_seen or incoming.first_seen,
            "label_hint": current.label_hint or incoming.label_hint,
            "discovery_rank": max(current.discovery_rank, incoming.discovery_rank),
            "discovery_evidence_urls": list(
                dict.fromkeys(
                    [
                        *current.discovery_evidence_urls,
                        *incoming.discovery_evidence_urls,
                    ]
                )
            )[:30],
            "provider_record_ids": list(
                dict.fromkeys(
                    [
                        *current.provider_record_ids,
                        *incoming.provider_record_ids,
                    ]
                )
            )[:30],
            "discovery_assertions": list(assertions.values())[:30],
        }
    )


def _appearance_candidate(
    publisher: InspectedCandidate,
    appearance: CompanyAppearanceEvidence,
) -> DiscoveredHost | None:
    """Convert one domain-backed appearance into an auditable future target."""

    hostname = canonicalize_public_website(
        appearance.subject_domain or appearance.subject_url
    )
    publisher_host = (
        publisher.observation.canonical_host or publisher.candidate.hostname
    ).removeprefix("www.")
    if not hostname or hostname == publisher_host:
        return None

    assertion = appearance.assertion
    relation = str(assertion.relation_type)
    confidence = str(appearance.identity_confidence)
    discovery_rank = min(
        50.0,
        float(
            24
            + _APPEARANCE_RELATION_RANK.get(relation, 0)
            + _APPEARANCE_CONFIDENCE_RANK.get(confidence, 0)
        ),
    )
    record_identity = assertion.context_hash[:20] or hostname
    return DiscoveredHost(
        source_apex=PUBLISHER_APPEARANCE_SOURCE,
        hostname=hostname,
        discovery_rank=discovery_rank,
        label_hint=appearance.subject_name or hostname,
        discovery_evidence_urls=[assertion.source_url],
        provider_record_ids=[
            f"publisher-appearance:{assertion.publisher_domain}:{record_identity}"
        ],
        discovery_assertions=[assertion],
    )


def _collect_company_appearance_candidates(
    inspected: list[InspectedCandidate],
    *,
    limit: int = APPEARANCE_EXPANSION_CAP,
) -> tuple[list[DiscoveredHost], list[DiscoveredHost], dict[str, int]]:
    """Vacuum domain-backed appearances from every inspected company without fetching them."""

    bounded_limit = max(0, int(limit))
    appearance_by_host: dict[str, DiscoveredHost] = {}
    stats = {
        "observed": 0,
        "domain_backed": 0,
        "invalid_or_self": 0,
        "deferred_by_cap": 0,
        "assertions_queued": 0,
    }
    ordered_publishers = sorted(
        inspected,
        key=lambda item: max(
            item.score.creative_capacity.total,
            item.score.placement.score,
            item.score.total,
        ),
        reverse=True,
    )
    for item in ordered_publishers:
        appearances = sorted(
            item.observation.web.company_appearances,
            key=lambda appearance: (
                _APPEARANCE_RELATION_RANK.get(str(appearance.assertion.relation_type), 0),
                _APPEARANCE_CONFIDENCE_RANK.get(str(appearance.identity_confidence), 0),
            ),
            reverse=True,
        )
        stats["observed"] += len(appearances)
        for appearance in appearances:
            candidate = _appearance_candidate(item, appearance)
            if candidate is None:
                stats["invalid_or_self"] += 1
                continue
            stats["domain_backed"] += 1
            current = appearance_by_host.get(candidate.hostname)
            if current is not None:
                appearance_by_host[candidate.hostname] = _merge_candidate_provenance(
                    current,
                    candidate,
                )
                continue
            if len(appearance_by_host) >= bounded_limit:
                stats["deferred_by_cap"] += 1
                continue
            appearance_by_host[candidate.hostname] = candidate

    # Backward compatibility for snapshots produced before structured company appearances.
    # Preserve the old high-confidence agency gate and never let this weaker domain-only path
    # displace a structured assertion for the same subject.
    legacy_by_host: dict[str, DiscoveredHost] = {}
    for item in ordered_publishers:
        if item.score.disqualified or item.score.placement.score < 50:
            continue
        own_hosts = {
            item.candidate.hostname.removeprefix("www."),
            (item.observation.canonical_host or "").removeprefix("www."),
        }
        for raw_host in legacy_agency_portfolio_domains(item.observation.web):
            hostname = canonicalize_public_website(raw_host)
            if (
                not hostname
                or hostname in own_hosts
                or hostname in appearance_by_host
                or hostname in legacy_by_host
            ):
                continue
            if len(appearance_by_host) + len(legacy_by_host) >= bounded_limit:
                stats["deferred_by_cap"] += 1
                continue
            legacy_by_host[hostname] = DiscoveredHost(
                source_apex=LEGACY_PORTFOLIO_SOURCE,
                hostname=hostname,
                discovery_rank=min(
                    45.0,
                    20.0 + item.score.placement.score * 0.25,
                ),
                label_hint=(
                    "First-party work reference · "
                    f"{item.observation.canonical_host or item.candidate.hostname}"
                ),
            )

    appearance_candidates = list(appearance_by_host.values())
    legacy_candidates = list(legacy_by_host.values())
    stats["assertions_queued"] = sum(
        len(candidate.discovery_assertions) for candidate in appearance_candidates
    )
    return appearance_candidates, legacy_candidates, stats


def _candidate_has_new_assertions(
    existing: DiscoveredHost | None,
    incoming: DiscoveredHost,
) -> bool:
    if existing is None:
        return bool(incoming.discovery_assertions)
    known = {_assertion_fingerprint(assertion) for assertion in existing.discovery_assertions}
    return any(
        _assertion_fingerprint(assertion) not in known
        for assertion in incoming.discovery_assertions
    )


def _eligible_for_ai_review(item: InspectedCandidate, min_score: int) -> bool:
    if (
        item.observation.canonical_recovery_required
        and is_recoverable_tenant_label(item.observation.tenant_label)
    ):
        return True
    if not item.observation.live:
        return False
    if not item.score.disqualified:
        return (
            max(
                item.score.placement.score,
                item.score.total,
                item.score.contact_priority,
                item.score.creative_capacity.total,
            )
            >= min_score
        )
    profile = PLATFORM_PROFILES.get(item.candidate.source_apex)
    return bool(
        profile
        and profile.kind == "agency"
        and item.score.disqualifiers == ["Public page contains too little business evidence"]
    )


def _snapshot_age_hours(snapshot: SnapshotInputs) -> float:
    try:
        observed_at = datetime.fromisoformat(
            snapshot.observed_at if isinstance(snapshot, SnapshotPlanning)
            else str(snapshot["observed_at"])
        )
    except (KeyError, TypeError, ValueError):
        return float("inf")
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - observed_at).total_seconds() / 3_600, 0.0)


def _completion_age_hours(snapshot: Mapping[str, object]) -> float:
    """Return age since successful company work finished, not when its first page loaded."""

    try:
        completed_at = datetime.fromisoformat(str(snapshot["completed_at"]))
    except (KeyError, TypeError, ValueError):
        return float("inf")
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - completed_at).total_seconds() / 3_600, 0.0)


def _refresh_interval_hours(snapshot: SnapshotInputs) -> int:
    """Refresh valuable live sites often and low-yield/dead sites sparingly."""
    if isinstance(snapshot, SnapshotPlanning):
        live, has_error = snapshot.live, snapshot.has_error
        final_score, budget_ready = snapshot.final_score, snapshot.budget_ready
    else:
        observation = snapshot["observation"]
        score = snapshot["score"]
        if not isinstance(observation, SiteObservation) or not isinstance(score, ScoreCard):
            return 24
        live, has_error = observation.live, bool(observation.error)
        final_score, budget_ready = int(snapshot.get("final_score", 0)), score.budget_ready
    if has_error:
        return 24 * 7
    if not live:
        return 24 * 30
    target_score, principal_signals_met, review_ready = _snapshot_target_strength(snapshot)
    if review_ready or target_score >= 50 or principal_signals_met >= 2:
        return 24 * 3
    if final_score >= 70 or budget_ready:
        return 24 * 3
    if target_score >= 30 or principal_signals_met >= 1:
        return 24 * 7
    if final_score >= 45:
        return 24 * 7
    return 24 * 14


def _snapshot_target_strength(
    snapshot: SnapshotPlanning | Mapping[str, object],
) -> tuple[int, int, bool]:
    """Read current target strength from the additive projection or an in-memory score."""
    if isinstance(snapshot, SnapshotPlanning):
        return snapshot.target_strength
    projection = snapshot.get("target_intelligence")
    if isinstance(projection, Mapping):
        try:
            return (
                int(projection.get("target_score", 0)),
                int(projection.get("principal_signals_met", 0)),
                bool(projection.get("review_ready", False)),
            )
        except (TypeError, ValueError):
            pass
    score = snapshot.get("score")
    if not isinstance(score, ScoreCard):
        return (0, 0, False)
    hypothesis = score.creative_capacity
    signal_count = sum(signal.truth_state == "met" for _, signal in hypothesis.principal_signals)
    return hypothesis.total, signal_count, hypothesis.review_ready


def _snapshot_is_reusable(snapshot: dict[str, object]) -> bool:
    observation = snapshot.get("observation")
    if isinstance(observation, SiteObservation) and not (
        observation_completes_company_work(observation)
    ):
        return False
    if (
        isinstance(observation, SiteObservation)
        and observation.live
        and not observation.contacts.contact_pages_checked
    ):
        # Snapshots created before the provenance-safe contact pass should receive one
        # bounded refresh even when their ordinary liveness cadence says they are fresh.
        return False
    return _snapshot_age_hours(snapshot) < _refresh_interval_hours(snapshot)


def _network_priority(
    candidate: DiscoveredHost,
    snapshot: SnapshotInputs | None,
) -> tuple[float, float, float]:
    if snapshot is None:
        return (3.0, candidate.discovery_rank, 0.0)
    if isinstance(snapshot, SnapshotPlanning):
        live, final_score = snapshot.live, snapshot.final_score
    else:
        observation = snapshot["observation"]
        live = isinstance(observation, SiteObservation) and observation.live
        final_score = float(snapshot.get("final_score", 0))
    target_score, principal_signals_met, review_ready = _snapshot_target_strength(snapshot)
    if (
        live
        and (review_ready or target_score >= 50 or principal_signals_met >= 2 or final_score >= 70)
    ):
        tier = 4.0
    elif (
        live
        and (target_score >= 30 or principal_signals_met >= 1)
    ):
        tier = 3.5
    elif live:
        tier = 2.0
    else:
        tier = 1.0
    overdue = _snapshot_age_hours(snapshot) / max(_refresh_interval_hours(snapshot), 1)
    return (tier, overdue, candidate.discovery_rank)


def _select_network_candidates(
    candidates: list[DiscoveredHost],
    latest_by_host: Mapping[str, SnapshotInputs],
    maximum: int,
    source_profiles: Mapping[str, Mapping[str, object]] | None = None,
    source_performance: Mapping[str, Mapping[str, object]] | None = None,
) -> list[DiscoveredHost]:
    """Choose a source-balanced batch across brands, agencies, and adjacencies."""

    unique_candidates = list(
        {
            candidate.hostname: candidate
            for candidate in candidates
            if not is_obvious_infrastructure_candidate(candidate)
        }.values()
    )
    if maximum <= 0:
        return unique_candidates

    buckets: dict[str, list[DiscoveredHost]] = {
        "direct": [],
        "agency": [],
        "adjacency": [],
    }
    for candidate in unique_candidates:
        edge_profile = (source_profiles or {}).get(candidate.hostname, {})
        profile = source_hydration_profile(candidate.source_apex)
        relationship_lane = str(edge_profile.get("relationship_lane", profile.relationship_lane))
        target_scope = str(edge_profile.get("target_scope", profile.target_scope))
        platform = PLATFORM_PROFILES.get(candidate.source_apex)
        if relationship_lane == "referral_partner" or (
            not edge_profile and platform and platform.kind == "agency"
        ):
            buckets["agency"].append(candidate)
        elif target_scope == "direct":
            buckets["direct"].append(candidate)
        else:
            buckets["adjacency"].append(candidate)

    ordered = {
        bucket: _source_balanced_order(
            items,
            latest_by_host,
            source_profiles=source_profiles,
            source_performance=source_performance,
        )
        for bucket, items in buckets.items()
    }
    direct_target = min(maximum, ceil(maximum * 0.70))
    agency_target = min(maximum - direct_target, ceil(maximum * 0.20))
    adjacency_target = max(0, maximum - direct_target - agency_target)
    selected = [
        *ordered["direct"][:direct_target],
        *ordered["agency"][:agency_target],
        *ordered["adjacency"][:adjacency_target],
    ]
    selected_hosts = {candidate.hostname for candidate in selected}

    # Reassign unused capacity without wasting a manual scan. Strong prior snapshots rise
    # within each source bucket; site evidence later decides buyer versus referral placement.
    for bucket in ("direct", "agency", "adjacency"):
        for candidate in ordered[bucket]:
            if len(selected) >= maximum:
                break
            if candidate.hostname in selected_hosts:
                continue
            selected.append(candidate)
            selected_hosts.add(candidate.hostname)
    return selected


def _source_balanced_order(
    candidates: list[DiscoveredHost],
    latest_by_host: Mapping[str, SnapshotInputs],
    *,
    source_profiles: Mapping[str, Mapping[str, object]] | None = None,
    source_performance: Mapping[str, Mapping[str, object]] | None = None,
) -> list[DiscoveredHost]:
    """Return weighted round-robin source order while retaining value within each source."""

    by_source: dict[str, list[DiscoveredHost]] = {}
    for candidate in candidates:
        edge_profile = (source_profiles or {}).get(candidate.hostname, {})
        allocation_key = str(
            edge_profile.get("allocation_key") or f"source:{candidate.source_apex}"
        )
        by_source.setdefault(allocation_key, []).append(candidate)
    for source_candidates in by_source.values():
        source_candidates.sort(
            key=lambda item: _network_priority(item, latest_by_host.get(item.hostname)),
            reverse=True,
        )
    ordered: list[DiscoveredHost] = []
    positions = {source: 0 for source in by_source}

    def effective_weight(candidate: DiscoveredHost) -> int:
        base_weight = int(
            (source_profiles or {})
            .get(candidate.hostname, {})
            .get("weight", source_hydration_profile(candidate.source_apex).weight)
        )
        performance = (source_performance or {}).get(candidate.source_apex, {})
        inspected = int(performance.get("inspected_count") or 0)
        if inspected < 20:
            return max(1, min(base_weight, 10))
        opportunity = float(performance.get("opportunity_score") or 0)
        multiplier = 0.5 + opportunity / 100
        return max(1, min(round(base_weight * multiplier), 10))

    weights = {
        source: max(effective_weight(candidate) for candidate in source_candidates)
        for source, source_candidates in by_source.items()
    }
    sources = sorted(
        by_source,
        key=lambda source: (-weights[source], source),
    )
    source_order = {source: index for index, source in enumerate(sources)}
    deficits = {source: 0 for source in sources}
    while len(ordered) < len(candidates):
        available = [source for source in sources if positions[source] < len(by_source[source])]
        if not available:
            break
        total_weight = sum(weights[source] for source in available)
        for source in available:
            deficits[source] += weights[source]
        source = max(
            available,
            key=lambda value: (
                deficits[value],
                weights[value],
                -source_order[value],
            ),
        )
        position = positions[source]
        ordered.append(by_source[source][position])
        positions[source] += 1
        deficits[source] -= total_weight
    return ordered


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="beings-leads",
        description="Discover and monitor evidence-backed Beings.org sales prospects.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", aliases=["monitor"], help="Run a new snapshot")
    scan.add_argument("inputs", nargs="*", help="Apex domains or text files of apex domains")
    scan.add_argument("--db", default="data/leads.db", help="SQLite database path")
    scan.add_argument("--report", default="reports/latest.md", help="Markdown report path")
    scan.add_argument("--json-report", help="Optional JSON report path")
    scan.add_argument("--max-hosts-per-source", type=int, default=100)
    scan.add_argument(
        "--max-network-inspections",
        type=int,
        default=0,
        help="Maximum unseen or refresh-due hosts to fetch; zero is unlimited",
    )
    scan.add_argument(
        "--adaptive-refresh",
        action="store_true",
        help="Reuse successful company work for 90 days and prioritize due gaps",
    )
    scan.add_argument("--reuse-core-only", action="store_true", help=argparse.SUPPRESS)
    scan.add_argument("--no-people-hydration", action="store_false",
                      dest="people_hydration_enabled", default=True, help=argparse.SUPPRESS)
    scan.add_argument("--no-microlink", action="store_false",
                      dest="microlink_enabled", default=True, help=argparse.SUPPRESS)
    scan.add_argument("--force-refresh", action="store_true", help=argparse.SUPPRESS)
    scan.add_argument("--manual-company-refresh", action="store_true", help=argparse.SUPPRESS)
    scan.add_argument("--manual-tag-refresh", action="store_true", help=argparse.SUPPRESS)
    scan.add_argument("--concurrency", type=int, default=20)
    scan.add_argument("--timeout", type=float, default=12.0)
    scan.add_argument("--pages-per-site", type=int, default=DEFAULT_SITE_RESEARCH_PAGES)
    scan.add_argument("--max-bytes-per-page", type=int, default=750_000)
    scan.add_argument(
        "--include-non-english",
        action="store_false",
        dest="english_only",
        help="Explicitly include non-English-presenting sites in inspection and scoring",
    )
    scan.set_defaults(english_only=True)
    scan.add_argument("--only-hosts-stdin", action="store_true", help=argparse.SUPPRESS)
    scan.add_argument(
        "--only-host",
        action="append",
        dest="only_hosts",
        default=None,
        help="Retry only these discovered hostnames",
    )
    scan.add_argument(
        "--no-free-intelligence",
        action="store_false",
        dest="free_intelligence",
        help="Skip sitemap, structured-data, public ATS, and archive enrichment",
    )
    scan.set_defaults(free_intelligence=True)
    scan.add_argument("--max-sitemaps-per-site", type=int, default=4)
    scan.add_argument(
        "--max-common-crawl-lookups",
        type=int,
        default=30,
        help="Capped archive lookups for the strongest deterministic shortlist",
    )
    scan.add_argument(
        "--max-news-lookups",
        type=int,
        default=5,
        help="Capped free recent-news lookups for the strongest deterministic shortlist",
    )
    scan.add_argument(
        "--max-frontier-scans",
        type=int,
        default=20,
        help=(
            "Maximum free frontier scanner follow-ups; EIG selects work unless explicit "
            "--frontier-scanner values are supplied. Saved-page extraction does not consume "
            "this budget."
        ),
    )
    scan.add_argument(
        "--frontier-scanner",
        action="append",
        dest="frontier_scanners",
        default=None,
        help=(
            "Explicitly run this frontier scanner for each applicable target; repeat for "
            "multiple scanners. Completed work inside 90 days is reused."
        ),
    )
    scan.add_argument("--frontier-scanner-manifest-json", default=None, help=argparse.SUPPRESS)
    scan.add_argument(
        "--respect-frontier-schedule",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    scan.add_argument(
        "--frontier-data-dir",
        default=os.getenv("BEINGS_FRONTIER_DATA_DIR", "data/frontier"),
        help=("Local directory containing optional official DOL LCA, SEC Form D, and DataSF files"),
    )
    scan.add_argument(
        "--agency-discovery",
        action="store_true",
        help="Add cached rotating public agency, studio, and production-company discovery",
    )
    scan.add_argument("--ai", action="store_true", help="Enable OpenAI qualification")
    scan.add_argument(
        "--ai-web",
        action="store_true",
        help="Allow OpenAI web search during qualification (requires --ai)",
    )
    scan.add_argument("--min-ai-score", type=int, default=45)
    scan.add_argument("--max-ai-calls", type=int, default=40)
    scan.add_argument(
        "--max-ai-web-calls",
        type=int,
        default=8,
        help="Maximum fresh AI reviews allowed to use web search",
    )
    scan.add_argument(
        "--model",
        default=os.getenv("BEINGS_LEADS_MODEL", "gpt-5-mini"),
        help="OpenAI model (default: BEINGS_LEADS_MODEL or gpt-5-mini)",
    )
    scan.add_argument("--report-limit", type=int, default=100)
    scan.add_argument("--minimum-report-score", type=int, default=45)
    scan.add_argument("--job-id", help=argparse.SUPPRESS)
    scan.add_argument("--database-ready", action="store_true", help=argparse.SUPPRESS)

    report = subparsers.add_parser("report", help="Regenerate a report from stored snapshots")
    report.add_argument("--db", default="data/leads.db")
    report.add_argument("--run-id", type=int)
    report.add_argument("--report", default="reports/latest.md")
    report.add_argument("--json-report")
    report.add_argument("--report-limit", type=int, default=100)
    report.add_argument("--minimum-report-score", type=int, default=45)

    stats = subparsers.add_parser("stats", help="Show database and latest-run statistics")
    stats.add_argument("--db", default="data/leads.db")

    scout = subparsers.add_parser("scout-sources", help="Find new prospecting sources with AI")
    scout.add_argument("--db", default="data/leads.db")
    scout.add_argument("--max-suggestions", type=int, default=8)
    scout.add_argument("--max-tool-calls", type=int, default=6)
    scout.add_argument(
        "--model",
        default=os.getenv(
            "BEINGS_LEADS_SOURCE_MODEL",
            os.getenv("BEINGS_LEADS_MODEL", "gpt-5-mini"),
        ),
    )
    return parser


def _validate_scan_args(args: argparse.Namespace) -> None:
    if getattr(args, "manual_tag_refresh", False):
        if not args.only_hosts or len(args.only_hosts) > 10_000 or args.inputs:
            raise ValueError("Tag refresh requires 1–10,000 explicit saved hosts")
        if args.ai or args.ai_web or args.reuse_core_only or args.agency_discovery:
            raise ValueError("Tag refresh permits only explicit free website research")
    has_explicit_host = any(str(host).strip() for host in (args.only_hosts or []))
    if not args.inputs and not args.agency_discovery and not has_explicit_host:
        raise ValueError("Provide a source apex, --only-host, or enable --agency-discovery")
    if args.reuse_core_only and (not has_explicit_host or args.manual_company_refresh):
        raise ValueError("--reuse-core-only requires explicit hosts and no manual core refresh")
    if args.ai_web and not args.ai:
        raise ValueError("--ai-web requires --ai")
    if args.manual_company_refresh and len(args.only_hosts or []) != 1:
        raise ValueError("--manual-company-refresh requires exactly one --only-host")
    if args.ai and not os.getenv("OPENAI_API_KEY"):
        raise ValueError("--ai requires OPENAI_API_KEY")
    for name in ("concurrency", "pages_per_site", "max_bytes_per_page", "report_limit"):
        if getattr(args, name) <= 0:
            raise ValueError(f"--{name.replace('_', '-')} must be positive")
    if args.max_hosts_per_source < 0:
        raise ValueError("--max-hosts-per-source cannot be negative")
    if args.max_network_inspections < 0:
        raise ValueError("--max-network-inspections cannot be negative")
    if not 1 <= args.max_sitemaps_per_site <= 12:
        raise ValueError("--max-sitemaps-per-site must be between 1 and 12")
    if args.max_common_crawl_lookups < 0:
        raise ValueError("--max-common-crawl-lookups cannot be negative")
    if args.max_news_lookups < 0:
        raise ValueError("--max-news-lookups cannot be negative")
    if args.max_news_lookups > 50:
        raise ValueError("--max-news-lookups cannot exceed 50")
    if not 0 <= args.max_frontier_scans <= 500:
        raise ValueError("--max-frontier-scans must be between 0 and 500")
    frontier_scanners = [
        str(item).strip() for item in (args.frontier_scanners or []) if str(item).strip()
    ]
    unknown_frontier_scanners = sorted(set(frontier_scanners) - set(FRONTIER_SCANNER_IDS))
    if unknown_frontier_scanners:
        raise ValueError(
            "Unknown --frontier-scanner value(s): "
            f"{', '.join(unknown_frontier_scanners)}. Valid values: "
            f"{', '.join(FRONTIER_SCANNER_IDS)}"
        )
    if getattr(args, "frontier_scanner_manifest_json", None):
        validate_manifest(json.loads(args.frontier_scanner_manifest_json), args.only_hosts,
                          reuse_core_only=args.reuse_core_only,
                          respect_schedule=args.respect_frontier_schedule,
                          maximum=args.max_frontier_scans)
    if not str(args.frontier_data_dir).strip():
        raise ValueError("--frontier-data-dir cannot be blank")
    if not 0 <= args.min_ai_score <= 100:
        raise ValueError("--min-ai-score must be between 0 and 100")
    if args.max_ai_calls < 0:
        raise ValueError("--max-ai-calls cannot be negative")
    if args.ai and not 0 <= args.max_ai_web_calls <= args.max_ai_calls:
        raise ValueError("--max-ai-web-calls must be between 0 and --max-ai-calls")


async def _discover_all(
    client: httpx.AsyncClient,
    apexes: list[str],
    *,
    max_hosts: int,
    progress: Callable[[int, int], None] | None = None,
    checkpoint: Callable[[str, list[DiscoveredHost], str | None], None] | None = None,
) -> tuple[list[DiscoveredHost], dict[str, str]]:
    complete = 0

    async def one(apex: str) -> tuple[str, list[DiscoveredHost] | None, str | None]:
        nonlocal complete
        candidates: list[DiscoveredHost] | None = None
        error: str | None = None
        try:
            candidates = await discover_apex(client, apex, max_hosts=max_hosts)
        except (httpx.HTTPError, DiscoveryError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if checkpoint:
                checkpoint(apex, candidates or [], error)
            complete += 1
            if progress:
                progress(complete, len(apexes))
        return apex, candidates, error

    results = await asyncio.gather(*(one(apex) for apex in apexes))
    candidates: list[DiscoveredHost] = []
    errors: dict[str, str] = {}
    for apex, found, error in results:
        if error:
            errors[apex] = error
            print(f"discovery warning [{apex}]: {error}", file=sys.stderr)
        else:
            print(f"discovered [{apex}]: {len(found or [])} candidates", file=sys.stderr)
            candidates.extend(found or [])
    deduplicated = {candidate.hostname: candidate for candidate in candidates}
    return list(deduplicated.values()), errors


async def _inspect_all(
    client: httpx.AsyncClient,
    candidates: list[DiscoveredHost],
    *,
    concurrency: int,
    pages_per_site: int,
    max_bytes_per_page: int,
    english_only: bool = True,
    microlink_enabled: bool = True,
    progress: Callable[[int, int], None] | None = None,
    checkpoint: Callable[[InspectedCandidate], None] | None = None,
    circuit_breaker: PlatformApexCircuitBreaker | None = None,
    on_error: Callable[[DiscoveredHost, str], None] | None = None,
    ready: Callable[[InspectedCandidate], Awaitable[None]] | None = None,
) -> list[InspectedCandidate]:
    semaphore = asyncio.Semaphore(concurrency)
    platform_locks = {
        candidate.source_apex.strip().lower().rstrip("."): asyncio.Lock()
        for candidate in candidates
        if circuit_breaker is not None and is_platform_tenant(candidate)
    }
    complete = 0

    async def inspect_with_policy(candidate: DiscoveredHost) -> SiteObservation:
        observation = (
            circuit_breaker.skip_observation(candidate)
            if circuit_breaker is not None
            else None
        )
        if observation is None:
            try:
                inspection_options: dict[str, object] = {
                    "pages_per_site": pages_per_site,
                    "max_bytes_per_page": max_bytes_per_page,
                    "english_only": english_only,
                }
                microlink_key = (
                    os.getenv("MICROLINK_API_KEY", "").strip() if microlink_enabled else ""
                )
                if microlink_key:
                    inspection_options["microlink_api_key"] = microlink_key
                observation = await inspect_host(client, candidate, **inspection_options)
            except Exception as exc:
                if isinstance(exc, sqlite3.Error):
                    raise
                # One target can require a client certificate or otherwise fail TLS;
                # preserve that target failure without misclassifying policy/store errors.
                error = f"{type(exc).__name__}: {exc}"[:1_000]
                observation = SiteObservation(
                    hostname=candidate.hostname,
                    fetch_outcome="retryable_error",
                    error=error,
                )
                print(f"inspection warning [{candidate.hostname}]: {error}", file=sys.stderr)
            if (circuit_breaker is not None
                and observation.fetch_disposition != LOCAL_CAPACITY_DEFERRED):
                circuit_breaker.record(candidate, observation)
        return observation

    async def one(candidate: DiscoveredHost) -> InspectedCandidate | None:
        nonlocal complete
        platform_lock = platform_locks.get(
            candidate.source_apex.strip().lower().rstrip(".")
        )
        if platform_lock is None:
            async with semaphore:
                observation = await inspect_with_policy(candidate)
        else:
            async with platform_lock, semaphore:
                observation = await inspect_with_policy(candidate)
        stage = "website scoring"
        try:
            score = score_candidate(candidate, observation, english_only=english_only)
            item = InspectedCandidate(candidate, observation, score)
            stage = "website checkpoint validation"
            if checkpoint:
                checkpoint(item)
        except Exception as exc:
            if isinstance(exc, (sqlite3.Error, OSError)) or (
                stage == "website checkpoint validation" and not isinstance(exc, ValueError)
            ):
                raise
            detail = f"{stage}: {type(exc).__name__}: {exc}"[:2_000]
            if on_error:
                on_error(candidate, detail)
            print(f"company processing warning [{candidate.hostname}]: {detail}", file=sys.stderr)
            item = None
        complete += 1
        if progress:
            progress(complete, len(candidates))
        if complete % 25 == 0 or complete == len(candidates):
            print(f"inspected: {complete}/{len(candidates)}", file=sys.stderr)
        if item is not None and ready is not None:
            await ready(item)
        return item

    results = await gather_owned(*(one(candidate) for candidate in candidates))
    return [item for item in results if item is not None]


def _save_snapshot_batch(
    store: LeadStore,
    run_id: int,
    items: list[InspectedCandidate],
    *,
    phase: str,
    on_error: Callable[[DiscoveredHost, str], None] | None = None,
) -> int:
    """Checkpoint a batch with the existing per-run upsert semantics."""

    store.update_run_progress(
        run_id,
        phase=phase,
        inspected=0,
        total_candidates=len(items),
    )
    candidate_ids = store.candidate_ids([item.candidate.hostname for item in items])
    changed = 0
    for index, item in enumerate(items, start=1):
        candidate_id = candidate_ids.get(item.candidate.hostname)
        if candidate_id is None:
            raise RuntimeError(
                f"Cannot save snapshot for unpersisted candidate {item.candidate.hostname}"
            )
        try:
            changes = store.save_snapshot(
                run_id, candidate_id, item.observation, item.score, item.ai, item.final_score,
            )
        except ValueError as exc:
            if on_error is None:
                raise
            on_error(item.candidate, f"{phase} snapshot validation: {type(exc).__name__}: {exc}")
            continue
        changed += any(change["kind"] != "new_candidate" for change in changes)
        if index % 10 == 0 or index == len(items):
            store.update_run_progress(
                run_id,
                phase=phase,
                inspected=index,
                total_candidates=len(items),
            )
    return changed


def _persist_frontier_feature_snapshots(
    store: LeadStore,
    items: list[InspectedCandidate],
) -> tuple[int, list[str]]:
    """Append leakage-safe training rows after current frontier evidence is rescored."""

    candidate_ids = store.candidate_ids([item.candidate.hostname for item in items])
    company_keys = store.candidate_company_keys(list(candidate_ids.values()))
    cutoff = datetime.now(UTC)
    persisted = 0
    errors: list[str] = []
    for item in items:
        candidate_id = candidate_ids.get(item.candidate.hostname)
        company_key = company_keys.get(candidate_id or -1)
        frontier = item.observation.web.frontier
        if not company_key or not (
            frontier.executions or frontier.observations or frontier.schedule_decisions
        ):
            continue
        action = item.score.creative_capacity.decision_packet.action
        try:
            exports = store.export_intelligence_features_as_of(
                cutoff,
                company_keys=[company_key],
                persist=True,
                action_shown=action,
            )
            persisted += len(exports)
        except Exception as exc:
            errors.append(
                f"{item.candidate.hostname}: feature export: {type(exc).__name__}: {exc}"[:1_000]
            )
    return persisted, errors


def _load_frontier_record_catalog(
    directory: str,
) -> tuple[object | None, dict[str, object]]:
    """Load optional official bulk indexes without making a network request."""

    path = Path(directory).expanduser()
    if not path.exists():
        return None, {
            "directory": str(path),
            "status": "absent",
            "scanners": {},
            "files": [],
            "errors": [],
        }
    try:
        catalog = load_local_frontier_sources(path)
    except (OSError, ValueError) as exc:
        return None, {
            "directory": str(path),
            "status": "failed",
            "scanners": {},
            "files": [],
            "errors": [f"{type(exc).__name__}: {exc}"[:1_000]],
        }
    scanner_stats = {scanner_id: asdict(stats) for scanner_id, stats in catalog.stats.items()}
    errors = [asdict(item) for item in catalog.errors]
    return catalog, {
        "directory": str(path.resolve()),
        "status": (
            "partial"
            if errors and any(item["status"] == "loaded" for item in scanner_stats.values())
            else "failed"
            if errors
            else "loaded"
            if any(item["status"] == "loaded" for item in scanner_stats.values())
            else "absent"
        ),
        "scanners": scanner_stats,
        "files": [
            {
                "scanner_id": item.scanner_id,
                "filename": item.filename,
                "sha256": item.sha256,
                "byte_count": item.byte_count,
                "rows_seen": item.rows_seen,
            }
            for item in catalog.provenance
        ],
        "errors": errors,
    }


def _frontier_source_error_messages(summary: dict[str, object]) -> list[str]:
    """Turn isolated local-source failures into bounded run-level diagnostics."""

    messages: list[str] = []
    for item in summary.get("errors", []):
        if not isinstance(item, dict):
            messages.append(f"local frontier source: {str(item)[:900]}")
            continue
        scanner_id = str(item.get("scanner_id") or "unknown-scanner")[:120]
        filename = str(item.get("filename") or "unknown-file")[:128]
        code = str(item.get("code") or "load_failed")[:120]
        message = str(item.get("message") or "The local source could not be loaded.")[:700]
        messages.append(
            f"local frontier source [{scanner_id}/{filename}]: {code}: {message}"[:1_000]
        )
    return list(dict.fromkeys(messages))[:20]


def _stored_ad_intelligence(store: LeadStore, candidate: DiscoveredHost) -> AdIntelligence:
    candidate_id = store.candidate_ids([candidate.hostname]).get(candidate.hostname)
    if candidate_id is None:
        return AdIntelligence()
    return AdIntelligence.model_validate(store.current_candidate_ad_intelligence(candidate_id))


async def _ai_enrich(
    inspected: list[InspectedCandidate],
    *,
    store: LeadStore,
    min_score: int,
    max_calls: int,
    max_web_calls: int,
    model: str,
    web_search: bool,
    fresh_call_hosts: set[str] | None = None,
    progress: Callable[[int, int], None] | None = None,
    activity: Callable[[str, str, str], None] | None = None,
) -> dict[str, object]:
    errors: list[str] = []
    error_hosts: list[str] = []
    ranked = sorted(
        (item for item in inspected if _eligible_for_ai_review(item, min_score)),
        key=lambda item: max(
            item.score.placement.score,
            item.score.contact_priority,
            item.score.total,
            item.score.creative_capacity.total,
            round(item.candidate.discovery_rank * 2),
        ),
        reverse=True,
    )
    fresh_calls = 0
    fresh_web_calls = 0
    cache_hits = 0
    reviewed = 0
    print(f"AI ranked pool: {len(ranked)} candidates", file=sys.stderr)
    for index, item in enumerate(ranked):
        web_cache_key = analysis_cache_key(
            item.candidate,
            item.observation,
            item.score,
            model=model,
            web_search=True,
        )
        standard_cache_key = analysis_cache_key(
            item.candidate,
            item.observation,
            item.score,
            model=model,
            web_search=False,
        )
        cached = store.get_ai_cache(web_cache_key) if web_search else None
        if cached is None and (not web_search or fresh_web_calls >= max_web_calls):
            cached = store.get_ai_cache(standard_cache_key)
        if cached is not None:
            item.ai = cached
            cache_hits += 1
            reviewed += 1
            if activity:
                activity(
                    f"AI reused {item.candidate.hostname}",
                    "content-addressed analysis cache hit",
                    "item",
                )
            if progress:
                progress(index + 1, len(ranked))
            continue
        from .paid_scan_plan import has_recent_ai_review

        if has_recent_ai_review(
            store, item.candidate, model=model, keys=(web_cache_key, standard_cache_key)
        ):
            if activity:
                activity(f"AI review held {item.candidate.hostname}",
                         "Successful matching review within 90 days; no new provider call",
                         "item")
            if progress:
                progress(index + 1, len(ranked))
            continue
        if fresh_call_hosts is not None and item.candidate.hostname not in fresh_call_hosts:
            if progress:
                progress(index + 1, len(ranked))
            continue
        if fresh_calls >= max_calls:
            if progress:
                progress(index + 1, len(ranked))
            continue
        use_web = web_search and fresh_web_calls < max_web_calls
        cache_key = web_cache_key if use_web else standard_cache_key
        try:
            item.ai = await analyze_candidate(
                item.candidate,
                item.observation,
                item.score,
                model=model,
                web_search=use_web,
            )
        except Exception as exc:  # preserve the run if one provider call fails
            message = f"{item.candidate.hostname}: {type(exc).__name__}: {exc}"
            errors.append(message)
            error_hosts.append(item.candidate.hostname)
            print(f"AI error [{fresh_calls + 1}/{max_calls}]: {message}", file=sys.stderr)
            if activity:
                activity(f"AI failed {item.candidate.hostname}", str(exc), "error")
        else:
            store.set_ai_cache(
                cache_key,
                item.ai,
                model=model,
                web_search=use_web,
            )
            from .paid_scan_plan import record_ai_review

            record_ai_review(store, cache_key, item.candidate.hostname, model)
            reviewed += 1
            print(f"AI reviewed: {fresh_calls + 1}/{max_calls}", file=sys.stderr)
            if activity:
                activity(
                    f"AI reviewed {item.candidate.hostname}",
                    "web-grounded review" if use_web else "model review committed",
                    "item",
                )
        fresh_calls += 1
        fresh_web_calls += int(use_web)
        if progress:
            progress(index + 1, len(ranked))
    return {
        "errors": errors,
        "error_hosts": error_hosts,
        "reviewed": reviewed,
        "fresh_calls": fresh_calls,
        "fresh_web_calls": fresh_web_calls,
        "cache_hits": cache_hits,
    }


async def _free_enrich_all(
    client: httpx.AsyncClient,
    inspected: list[InspectedCandidate],
    *,
    store: LeadStore,
    concurrency: int,
    max_sitemaps_per_site: int,
    english_only: bool = True,
    progress: Callable[[int, int], None] | None = None,
    checkpoint: Callable[[InspectedCandidate, bool], None] | None = None,
    activity: Callable[[str, str, str], None] | None = None,
    on_error: Callable[[DiscoveredHost, str], None] | None = None,
    shared_semaphore: asyncio.Semaphore | None = None,
) -> dict[str, object]:
    live = [
        item
        for item in inspected
        if item.observation.live
        and is_english_eligible(item.observation, english_only=english_only)
    ]
    semaphore = shared_semaphore or asyncio.Semaphore(min(concurrency, 12))
    complete = 0
    cache_hits = 0
    network_calls = 0
    errors: list[str] = []
    error_hosts: list[str] = []

    async def one(item: InspectedCandidate) -> None:
        nonlocal complete, cache_hits, network_calls
        succeeded = False
        try:
            async with semaphore:
                from .company_research import CompanyResearchBudget, public_requests

                budget = CompanyResearchBudget(store.connection, store._target_canonical_domain(
                    item.candidate, item.observation))
                with public_requests(budget):
                    summary = await enrich_site_owned_intelligence(
                        client,
                        item.observation,
                        cache_get=store.get_free_signal_cache,
                        cache_set=store.set_free_signal_cache,
                        max_sitemaps=max_sitemaps_per_site,
                        retry_components=(item.observation.core_hydration_retry_components or None
                                          if item.observation.core_hydration_reused else None),
                    )
                has_mx = bool(item.observation.web.vendors.dns_records.get("MX"))
                for contact in item.observation.contacts.emails:
                    if contact.domain_matches_site:
                        contact.mx_observed = has_mx
            cache_hits += summary["cache_hits"]
            network_calls += summary["network_calls"]
            component_errors = [str(error) for error in summary.get("retryable_errors", [])][:20]
            item.observation.core_hydration_errors = component_errors
            succeeded = not component_errors
            if component_errors:
                errors.extend(
                    f"{item.candidate.hostname}: {error}"[:1_000]
                    for error in component_errors
                )
                error_hosts.append(item.candidate.hostname)
        except Exception as exc:
            if isinstance(exc, sqlite3.Error):
                raise
            item.observation.core_hydration_errors = [f"{type(exc).__name__}: {exc}"[:1_000]]
            item.observation.core_hydration_retry_components = ["*"]
            error = f"{item.candidate.hostname}: {type(exc).__name__}: {exc}"[:1_000]
            errors.append(error)
            error_hosts.append(item.candidate.hostname)
            print(
                f"free intelligence warning [{item.candidate.hostname}]: {error}",
                file=sys.stderr,
            )
        if checkpoint:
            try:
                checkpoint(item, succeeded)
            except ValueError as exc:
                if on_error is None:
                    raise
                succeeded = False
                detail = f"site-owned scoring/checkpoint validation: {type(exc).__name__}: {exc}"
                on_error(item.candidate, detail)
                errors.append(f"{item.candidate.hostname}: {detail}"[:2_000])
                error_hosts.append(item.candidate.hostname)
        if activity:
            activity(
                f"Enriched {item.candidate.hostname}",
                (
                    "site-owned intelligence committed"
                    if succeeded
                    else "site-owned enrichment failed"
                ),
                "item" if succeeded else "error",
            )
        complete += 1
        if progress:
            progress(complete, len(live))
        if complete % 25 == 0 or complete == len(live):
            print(f"free intelligence: {complete}/{len(live)}", file=sys.stderr)

    await gather_owned(*(one(item) for item in live))
    return {
        "eligible": len(live),
        "enriched": complete,
        "cache_hits": cache_hits,
        "network_calls": network_calls,
        "vendors_detected": sum(len(item.observation.web.vendors.vendors) for item in live),
        "disclosures_found": sum(
            bool(item.observation.web.vendors.disclosure_url) for item in live
        ),
        "feeds_found": sum(bool(item.observation.web.sales.feed_url) for item in live),
        "catalogs_found": sum(bool(item.observation.web.sales.catalog_provider) for item in live),
        "errors": errors[:20],
        "error_hosts": error_hosts,
    }


async def _news_enrich(
    client: httpx.AsyncClient,
    inspected: list[InspectedCandidate],
    *,
    store: LeadStore,
    maximum: int,
    progress: Callable[[int, int], None] | None = None,
    activity: Callable[[str, str, str], None] | None = None,
) -> dict[str, object]:
    if maximum <= 0:
        return {
            "eligible": 0,
            "checked": 0,
            "fresh_calls": 0,
            "articles": 0,
            "triggers": 0,
            "errors": [],
            "error_hosts": [],
        }
    eligible = sorted(
        (
            item
            for item in inspected
            if item.observation.live
            and not item.score.disqualified
            and item.score.demand_score >= 20
            and item.score.capacity_score >= 20
        ),
        key=lambda item: (
            item.score.contact_priority,
            item.observation.web.sales.overall_score,
            item.score.demand_score,
        ),
        reverse=True,
    )[:maximum]
    fresh_calls = 0
    article_count = 0
    trigger_count = 0
    errors: list[str] = []
    error_hosts: list[str] = []
    checked = 0
    for index, item in enumerate(eligible, start=1):
        result = await enrich_news_intelligence(
            client,
            item.observation,
            cache_get=store.get_free_signal_cache,
            cache_set=store.set_free_signal_cache,
        )
        checked += int(result.get("checked", False))
        fresh_calls += int(result.get("fresh", False))
        article_count += int(result.get("articles", 0))
        trigger_count += int(result.get("triggers", 0))
        if result.get("error"):
            errors.append(f"{item.candidate.hostname}: {result['error']}")
            error_hosts.append(item.candidate.hostname)
        if activity:
            activity(
                f"News checked {item.candidate.hostname}",
                (
                    str(result.get("error"))
                    if result.get("error")
                    else (
                        f"{int(result.get('articles') or 0)} articles · "
                        f"{int(result.get('triggers') or 0)} triggers"
                    )
                ),
                "error" if result.get("error") else "item",
            )
        if progress:
            progress(index, len(eligible))
        if "HTTP 429" in str(result.get("error", "")):
            error_hosts.extend(remaining.candidate.hostname for remaining in eligible[index:])
            break
        if result.get("fresh") and index < len(eligible):
            await asyncio.sleep(5.1)
    return {
        "eligible": len(eligible),
        "checked": checked,
        "fresh_calls": fresh_calls,
        "articles": article_count,
        "triggers": trigger_count,
        "errors": errors[:10],
        "error_hosts": error_hosts,
    }


def _optional_enrichment_candidates(
    inspected: list[InspectedCandidate],
    *,
    store: LeadStore,
    cache_kind: str,
    freshly_inspected_hosts: set[str],
) -> list[InspectedCandidate]:
    """Retry optional evidence without reopening completed owned-site work.

    New core inspections always receive their configured optional pass. Reused companies are
    admitted only when the optional cache is due/absent or the cached projection has never been
    attached to the reusable observation. Failed cache rows naturally become due on their shorter
    TTL, so this is an independent retry cadence rather than a second website scan.
    """

    selected: list[InspectedCandidate] = []
    for item in inspected:
        host = (
            (item.observation.canonical_host or item.observation.hostname)
            .removeprefix("www.")
            .lower()
        )
        cached = store.get_free_signal_cache(host, cache_kind)
        if cache_kind == "gdelt_news":
            projection_missing = not bool(item.observation.web.sales.news_query)
        elif cache_kind == "common_crawl":
            projection_missing = not bool(item.observation.web.common_crawl_collection)
        else:
            projection_missing = True
        if (
            item.candidate.hostname in freshly_inspected_hosts
            or cached is None
            or projection_missing
        ):
            selected.append(item)
    return selected


async def _common_crawl_enrich(
    client: httpx.AsyncClient,
    inspected: list[InspectedCandidate],
    *,
    store: LeadStore,
    maximum: int,
    progress: Callable[[int, int], None] | None = None,
    activity: Callable[[str, str, str], None] | None = None,
) -> dict[str, object]:
    if maximum <= 0:
        return {
            "eligible": 0,
            "checked": 0,
            "fresh_calls": 0,
            "errors": [],
            "error_hosts": [],
        }
    eligible = sorted(
        (
            item
            for item in inspected
            if item.observation.live
            and not item.score.disqualified
            and item.score.demand_score >= 20
            and item.score.capacity_score >= 20
        ),
        key=lambda item: (
            item.score.contact_priority,
            item.score.demand_score,
            item.score.capacity_score,
        ),
        reverse=True,
    )[:maximum]
    if not eligible:
        return {
            "eligible": 0,
            "checked": 0,
            "fresh_calls": 0,
            "errors": [],
            "error_hosts": [],
        }
    try:
        collection = await latest_common_crawl_index(client)
    except Exception as exc:
        return {
            "eligible": len(eligible),
            "checked": 0,
            "fresh_calls": 0,
            "errors": [f"{type(exc).__name__}: {exc}"],
            "error_hosts": [item.candidate.hostname for item in eligible],
        }
    fresh_calls = 0
    errors: list[str] = []
    error_hosts: list[str] = []
    for index, item in enumerate(eligible, start=1):
        error = ""
        try:
            fresh_calls += int(
                await enrich_common_crawl(
                    client,
                    item.observation,
                    collection=collection,
                    cache_get=store.get_free_signal_cache,
                    cache_set=store.set_free_signal_cache,
                )
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            errors.append(f"{item.candidate.hostname}: {error}")
            error_hosts.append(item.candidate.hostname)
        if activity:
            activity(
                f"Archive checked {item.candidate.hostname}",
                error or "Common Crawl footprint committed",
                "error" if error else "item",
            )
        if progress:
            progress(index, len(eligible))
    return {
        "eligible": len(eligible),
        "checked": len(eligible),
        "fresh_calls": fresh_calls,
        "collection": collection,
        "errors": errors[:10],
        "error_hosts": error_hosts,
    }


async def _run_scan(args: argparse.Namespace) -> int:
    _validate_scan_args(args)
    if getattr(args, "manual_tag_refresh", False):
        args.manual_company_refresh = True
    apexes = load_apex_inputs(args.inputs)
    frontier_scanners = list(
        dict.fromkeys(
            str(item).strip() for item in (args.frontier_scanners or []) if str(item).strip()
        )
    )
    explicit_hosts = list(
        dict.fromkeys(
            str(host).strip().lower().rstrip(".")
            for host in (args.only_hosts or [])
            if str(host).strip()
        )
    )
    manifest = validate_manifest(
        json.loads(args.frontier_scanner_manifest_json)
        if args.frontier_scanner_manifest_json else {}, explicit_hosts,
        reuse_core_only=args.reuse_core_only, respect_schedule=args.respect_frontier_schedule,
        maximum=args.max_frontier_scans,
    )
    if manifest:
        frontier_scanners = list(dict.fromkeys(
            scanner for ids in manifest.values() for scanner in ids
        ))
    config = ScanConfig(
        apexes=apexes,
        max_hosts_per_source=args.max_hosts_per_source,
        max_network_inspections=args.max_network_inspections,
        adaptive_refresh=args.adaptive_refresh,
        force_refresh=args.force_refresh,
        manual_company_refresh=args.manual_company_refresh,
        reuse_core_only=args.reuse_core_only,
        people_hydration_enabled=args.people_hydration_enabled,
        microlink_enabled=args.microlink_enabled,
        concurrency=args.concurrency,
        timeout=args.timeout,
        pages_per_site=args.pages_per_site,
        max_bytes_per_page=args.max_bytes_per_page,
        english_only=args.english_only,
        free_intelligence=args.free_intelligence,
        max_sitemaps_per_site=args.max_sitemaps_per_site,
        max_common_crawl_lookups=args.max_common_crawl_lookups,
        max_news_lookups=args.max_news_lookups,
        max_frontier_scans=args.max_frontier_scans,
        frontier_scanners=frontier_scanners or None,
        respect_frontier_schedule=bool(args.respect_frontier_schedule),
        frontier_data_dir=args.frontier_data_dir,
        agency_discovery=args.agency_discovery,
        ai=args.ai,
        ai_web=args.ai_web,
        min_ai_score=args.min_ai_score,
        max_ai_calls=args.max_ai_calls,
        max_ai_web_calls=args.max_ai_web_calls,
        model=args.model,
        only_hosts=explicit_hosts or None,
        job_id=args.job_id,
        frontier_scanner_manifest=manifest or None,
    )
    with child_step("open_database"):
        store = LeadStore(args.db, initialize=not args.database_ready)
    with child_step("initial_storage_stats"):
        storage_before = usage_snapshot(store.connection)
    with child_step("register_company_run"):
        run_id = store.start_run(asdict(config))
    child_record("company_registered", run_id=run_id, offered_hosts=len(explicit_hosts))
    store.track_run_phases(run_id)
    company_processing_errors: dict[str, str] = {}
    prior_observation = lru_cache(maxsize=16)(store.snapshot_observation)

    def emit_action(message: str, detail: str = "", level: str = "item") -> None:
        if not args.job_id:
            return
        print(
            encode_scan_action(message, detail=detail, level=level),
            file=sys.stderr,
            flush=True,
        )

    def report_platform_persistence_error(operation: str, exc: Exception) -> None:
        print(
            f"platform circuit persistence warning [{operation}]: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

    def record_company_error(candidate: DiscoveredHost, detail: str) -> None:
        company_processing_errors[candidate.hostname] = detail[:2_000]
        store.record_source_error(run_id, candidate.hostname, detail)
        emit_action(f"Company processing failed · {candidate.hostname}", detail, "error")

    def rescore(
        item: InspectedCandidate, *, stage: str = "company rescoring",
        previous_observation: SiteObservation | None = None,
    ) -> bool:
        try:
            if previous_observation is None:
                snapshot = latest_by_host.get(item.candidate.hostname)
                if isinstance(snapshot, SnapshotPlanning):
                    previous_observation = prior_observation(snapshot.snapshot_id)
                elif snapshot is not None:
                    previous_observation = snapshot["observation"]
            item.score = score_candidate(
                item.candidate, item.observation,
                previous_observation=previous_observation,
                ad_intelligence=_stored_ad_intelligence(store, item.candidate),
                english_only=args.english_only,
            )
        except Exception as exc:
            if isinstance(exc, (sqlite3.Error, OSError)):
                raise
            record_company_error(item.candidate, f"{stage}: {type(exc).__name__}: {exc}")
            return False
        return True

    circuit_breaker = PlatformApexCircuitBreaker(
        active_cooldowns=store.active_platform_apex_cooldowns(),
        active_failure_windows=store.active_platform_apex_failure_windows(),
        on_window_reset=store.clear_platform_apex_failure_window,
        on_failure_record=store.record_platform_apex_failure,
        active_cooldown_lookup=store.platform_apex_cooldown,
        on_persistence_error=report_platform_persistence_error,
    )
    claimed_candidate_ids: set[int] = set()
    checkpointed_complete_candidate_ids: set[int] = set()
    locally_deferred_ids: set[int] = set()
    claimed_hostnames_by_id: dict[int, str] = {}
    run_finalized = False
    if explicit_hosts:
        print(
            f"run {run_id}: hydrating {len(explicit_hosts)} persisted hosts",
            file=sys.stderr,
        )
    else:
        print(f"run {run_id}: scanning {len(apexes)} sources", file=sys.stderr)
    rate_token = DATABASE.set(Path(args.db))
    try:
        timeout = httpx.Timeout(args.timeout, connect=min(args.timeout, 8.0))
        pool_size = max(args.concurrency + len(apexes), 20)
        limits = httpx.Limits(
            max_connections=pool_size,
            max_keepalive_connections=max(args.concurrency, 10),
        )
        async with httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            headers={"User-Agent": USER_AGENT},
            trust_env=False,
        ) as client, client_pool_slots(client, pool_size):
            agency_summary: dict[str, object] = {
                "enabled": bool(args.agency_discovery),
                "new_candidates": 0,
                "backlog_candidates": 0,
                "network_calls": 0,
                "cache_hits": 0,
                "osm_metro": "",
                "wikidata_page": 0,
                "errors": [],
            }
            agency_candidates: list[DiscoveredHost] = []
            if args.agency_discovery and not explicit_hosts:
                store.update_run_progress(
                    run_id,
                    phase="agency_discovery",
                    external_checked=0,
                    total_candidates=2,
                )
                agency_result = await discover_agencies(
                    client,
                    rotation=max(run_id - 1, 0),
                    cache_get=store.get_free_signal_cache,
                    cache_set=store.set_free_signal_cache,
                )
                store.upsert_derived_candidates(agency_result.candidates)
                backlog_limit = (
                    max(args.max_network_inspections * 4, 1_000)
                    if args.max_network_inspections
                    else 2_000
                )
                agency_candidates = store.discovery_backlog(
                    [
                        "openstreetmap.org",
                        "wikidata.org",
                        "agency-portfolio",
                        "ai-recovery",
                    ],
                    limit=backlog_limit,
                )
                agency_summary = {
                    "enabled": True,
                    "new_candidates": len(agency_result.candidates),
                    "backlog_candidates": len(agency_candidates),
                    "network_calls": agency_result.network_calls,
                    "cache_hits": agency_result.cache_hits,
                    "osm_metro": agency_result.osm_metro,
                    "wikidata_page": agency_result.wikidata_page,
                    "errors": agency_result.errors,
                }
                store.update_run_progress(
                    run_id,
                    phase="agency_discovery",
                    external_checked=2,
                    total_candidates=2,
                )
                for error in agency_result.errors:
                    source = error.split(":", 1)[0]
                    store.record_source_error(run_id, source, error)
            if explicit_hosts:
                with child_step("load_requested_hosts"):
                    candidates = store.candidates_by_hosts(explicit_hosts)
                loaded_hosts = {candidate.hostname for candidate in candidates}
                missing_hosts = [
                    hostname for hostname in explicit_hosts if hostname not in loaded_hosts
                ]
                if missing_hosts:
                    raise ValueError(
                        "Explicit hosts are not persisted: " + ", ".join(missing_hosts)
                    )
                source_errors: dict[str, str] = {}
                with child_step("publish_discovering_progress"):
                    store.update_run_progress(
                        run_id,
                        phase="discovering",
                        discovered=len(candidates),
                        total_candidates=len(candidates),
                    )
            else:
                checkpointed_source_errors: set[str] = set()

                def checkpoint_discovery(
                    apex: str,
                    found: list[DiscoveredHost],
                    error: str | None,
                ) -> None:
                    if found:
                        store.upsert_candidates(found)
                    if error:
                        store.record_source_error(run_id, apex, error)
                        checkpointed_source_errors.add(apex)
                    emit_action(
                        f"Discovered {apex}",
                        error or f"{len(found):,} candidates committed",
                        "error" if error else "item",
                    )

                candidates, source_errors = await _discover_all(
                    client,
                    apexes,
                    max_hosts=args.max_hosts_per_source,
                    progress=lambda complete, total: store.update_run_progress(
                        run_id,
                        phase="discovering",
                        discovered=complete,
                        total_candidates=total,
                    ),
                    checkpoint=checkpoint_discovery,
                )
                store.upsert_candidates(candidates)
                for apex, error in source_errors.items():
                    if apex not in checkpointed_source_errors:
                        store.record_source_error(run_id, apex, error)
            discovered_candidate_count = len(candidates)
            persisted_backlog: list[DiscoveredHost] = []
            appearance_backlog_loaded = 0
            legacy_portfolio_backlog_loaded = 0
            if args.adaptive_refresh and not explicit_hosts:
                backlog_limit = (
                    max(args.max_network_inspections * 4, 1_000)
                    if args.max_network_inspections
                    else 20_000
                )
                hydration_sources = list(
                    dict.fromkeys([*apexes, *APPEARANCE_BACKLOG_SOURCES])
                )
                persisted_backlog = store.target_hydration_backlog(
                    hydration_sources,
                    limit=backlog_limit,
                )
                appearance_backlog_loaded = sum(
                    candidate.source_apex == PUBLISHER_APPEARANCE_SOURCE
                    for candidate in persisted_backlog
                )
                legacy_portfolio_backlog_loaded = sum(
                    candidate.source_apex == LEGACY_PORTFOLIO_SOURCE
                    for candidate in persisted_backlog
                )
            merged_candidates = list(
                {
                    candidate.hostname: candidate
                    for candidate in [
                        *candidates,
                        *agency_candidates,
                        *persisted_backlog,
                    ]
                }.values()
            )
            candidates = [
                candidate
                for candidate in merged_candidates
                if not is_obvious_infrastructure_candidate(candidate)
            ]
            infrastructure_skipped = len(merged_candidates) - len(candidates)
            hostnames = [candidate.hostname for candidate in candidates]
            with child_step("read_latest_snapshots"):
                if args.reuse_core_only and args.ai and not args.free_intelligence:
                    ai_review_snapshots = store.latest_snapshots(hostnames)
                    latest_by_host = ai_review_snapshots
                else:
                    latest_by_host = store.latest_snapshot_planning(hostnames)
            with child_step("resolve_candidate_ids"):
                candidate_ids_by_host = store.candidate_ids(
                    [candidate.hostname for candidate in candidates]
                )
            candidate_by_id = {
                candidate_ids_by_host[candidate.hostname]: candidate
                for candidate in candidates
                if candidate.hostname in candidate_ids_by_host
            }
            with child_step("read_hydration_profiles"):
                source_profiles = store.candidate_hydration_profiles(
                    [candidate.hostname for candidate in candidates]
                )
                source_performance = {
                    str(source["apex"]): source for source in store.list_sources()
                }
            with child_step("read_completed_company_snapshots"):
                completed_by_id = (
                    {} if args.manual_company_refresh else store.completed_company_snapshots(
                        list(candidate_by_id), max_age_hours=COMPANY_SCAN_FRESH_HOURS,
                        recent_observation_hours=(
                            24 * 30 if args.adaptive_refresh and not args.force_refresh
                            and not args.reuse_core_only else None
                        ),
                    )
                )
            with child_step("plan_website_candidates"):
                completed_by_host = {
                    candidate.hostname: completed_by_id[candidate_id]
                    for candidate_id, candidate in candidate_by_id.items()
                    if candidate_id in completed_by_id
                }
                hard_recent_snapshots = {
                    hostname: snapshot
                    for hostname, snapshot in completed_by_host.items()
                    if _completion_age_hours(snapshot) < COMPANY_SCAN_FRESH_HOURS
                }
                if args.manual_company_refresh:
                    reusable_snapshots = {}
                elif args.reuse_core_only and args.ai and not args.free_intelligence:
                    # A selected AI review consumes saved evidence, including older or
                    # incomplete core observations, without silently collecting websites.
                    # Their original timestamps still govern evidence/AI eligibility.
                    reusable_snapshots = ai_review_snapshots
                elif args.reuse_core_only:
                    reusable_snapshots = hard_recent_snapshots
                elif args.adaptive_refresh and not args.force_refresh:
                    reusable_snapshots = {
                        hostname: snapshot
                        for hostname, snapshot in completed_by_host.items()
                        if hostname in hard_recent_snapshots or _snapshot_is_reusable(snapshot)
                    }
                else:
                    # The private retry flag may bypass a longer adaptive interval, never
                    # the non-negotiable 90-day floor for fully completed company work.
                    reusable_snapshots = hard_recent_snapshots
                reused = {
                    candidate.hostname: InspectedCandidate(
                        candidate,
                        snapshot["observation"],
                        snapshot["score"],
                        snapshot["ai"],
                    )
                    for candidate in candidates
                    if (snapshot := reusable_snapshots.get(candidate.hostname)) is not None
                }
                reused_snapshot_by_host = dict(reusable_snapshots)
                due_candidates = [
                    candidate for candidate in candidates if candidate.hostname not in reused
                ]
                ordered_due_candidates = _select_network_candidates(
                    due_candidates,
                    latest_by_host,
                    len(due_candidates),
                    source_profiles,
                    source_performance,
                )
                ordered_due_ids = [
                    candidate_ids_by_host[candidate.hostname]
                    for candidate in ordered_due_candidates
                    if candidate.hostname in candidate_ids_by_host
                ]
            with child_step("claim_company_work"):
                claim_result = store.claim_company_scan_work(
                    [] if args.reuse_core_only else ordered_due_ids,
                    run_id=run_id,
                    maximum=args.max_network_inspections,
                    force_refresh=args.force_refresh,
                    manual_company_refresh=args.manual_company_refresh,
                )
            claimed_candidate_ids = set(claim_result["claimed_candidate_ids"])
            claimed_hostnames_by_id = {
                candidate_id: candidate_by_id[candidate_id].hostname
                for candidate_id in claimed_candidate_ids
                if candidate_id in candidate_by_id
            }
            for candidate_id, snapshot in claim_result["recent_snapshots"].items():
                candidate = candidate_by_id.get(int(candidate_id))
                if candidate is None:
                    continue
                reused_snapshot_by_host[candidate.hostname] = snapshot
                reused[candidate.hostname] = InspectedCandidate(
                    candidate,
                    snapshot["observation"],
                    snapshot["score"],
                    snapshot["ai"],
                )
                emit_action(
                    f"Reused {candidate.hostname}",
                    "current core hydration retained",
                )
            with child_step("read_enrichment_retry_snapshots"):
                retry_snapshots = (
                    store.enrichment_retry_snapshots(sorted(claimed_candidate_ids))
                    if args.free_intelligence and not args.manual_company_refresh else {}
                )
            enrichment_retries = [
                InspectedCandidate(candidate_by_id[cid], payload["observation"],
                                   payload["score"], payload["ai"])
                for cid, payload in retry_snapshots.items()
            ]
            network_candidates = [
                candidate
                for candidate in ordered_due_candidates
                if candidate_ids_by_host.get(candidate.hostname) in claimed_candidate_ids
                and candidate_ids_by_host.get(candidate.hostname) not in retry_snapshots
            ]
            active_claim_candidate_ids = set(claim_result["active_candidate_ids"])
            retry_deferred_candidate_ids = set(
                claim_result["retry_deferred_candidate_ids"]
            )
            deduplicated_candidate_ids = set(claim_result["deduplicated_candidate_ids"])
            deferred_candidate_ids = set(claim_result["deferred_candidate_ids"])
            if args.reuse_core_only:
                deferred_candidate_ids.update(ordered_due_ids)
            deferred_candidates = len(deferred_candidate_ids)
            work_total = len(reused) + len(network_candidates) + len(enrichment_retries)
            child_record(
                "company_plan", run_id=run_id, reused=len(reused),
                claimed=len(claimed_candidate_ids),
                website_checks=len(network_candidates), enrichment_retries=len(enrichment_retries),
                deferred=deferred_candidates, retry_deferred=len(retry_deferred_candidate_ids),
                deduplicated=len(deduplicated_candidate_ids),
                already_active=len(active_claim_candidate_ids),
            )
            with child_step("publish_inspecting_progress"):
                store.update_run_progress(
                    run_id,
                    phase="inspecting",
                    discovered=len(candidates),
                    inspected=len(reused),
                    total_candidates=work_total,
                )

            def checkpoint_inspection(item: InspectedCandidate) -> None:
                candidate_id = candidate_ids_by_host.get(item.candidate.hostname)
                if candidate_id is None:
                    raise RuntimeError(
                        f"Cannot checkpoint unpersisted candidate {item.candidate.hostname}"
                    )
                if item.observation.fetch_disposition == LOCAL_CAPACITY_DEFERRED:
                    store.defer_company_scan_work(run_id, candidate_id, item.candidate.hostname)
                    locally_deferred_ids.add(candidate_id)
                    return
                requires_free_enrichment = (
                    args.free_intelligence
                    and item.observation.live
                    and is_english_eligible(
                        item.observation,
                        english_only=args.english_only,
                    )
                )
                complete_company_work = bool(
                    observation_completes_company_work(item.observation)
                    and not requires_free_enrichment
                )
                store.save_snapshot(
                    run_id,
                    candidate_id,
                    item.observation,
                    item.score,
                    item.ai,
                    item.final_score,
                    complete_company_work=complete_company_work,
                    failed_company_work=(
                        not item.observation.live
                        and not observation_completes_company_work(item.observation)
                    ),
                )
                if complete_company_work:
                    checkpointed_complete_candidate_ids.add(candidate_id)
                emit_action(
                    f"Inspected {item.candidate.hostname}",
                    (
                        item.observation.error
                        or item.observation.fetch_disposition
                        or f"score {item.final_score} · owned-site snapshot committed"
                    ),
                    "error" if item.observation.error else "item",
                )

            def checkpoint_free_enrichment(
                item: InspectedCandidate,
                succeeded: bool,
            ) -> None:
                candidate_id = candidate_ids_by_host.get(item.candidate.hostname)
                if candidate_id is None:
                    raise RuntimeError(
                        f"Cannot checkpoint unpersisted candidate {item.candidate.hostname}"
                    )
                if not rescore(item, stage="site-owned scoring"):
                    return
                complete_company_work = bool(
                    succeeded and observation_completes_company_work(item.observation)
                )
                store.save_snapshot(
                    run_id,
                    candidate_id,
                    item.observation,
                    item.score,
                    item.ai,
                    item.final_score,
                    complete_company_work=complete_company_work,
                    failed_company_work=not succeeded,
                )
                if complete_company_work:
                    checkpointed_complete_candidate_ids.add(candidate_id)

            streamed_free = {}
            streamed_enriched = 0
            core_scores = {}
            enrichment_slots = asyncio.Semaphore(min(args.concurrency, 12))

            async def enrich_ready(item):
                nonlocal streamed_enriched
                core_scores[item.candidate.hostname] = item.score.model_dump_json()
                if (not args.free_intelligence or not item.observation.live
                    or not is_english_eligible(item.observation, english_only=args.english_only)):
                    return
                streamed_free[item.candidate.hostname] = await _free_enrich_all(
                    client, [item], store=store, concurrency=args.concurrency,
                    max_sitemaps_per_site=args.max_sitemaps_per_site,
                    english_only=args.english_only, checkpoint=checkpoint_free_enrichment,
                    activity=emit_action, on_error=record_company_error,
                    shared_semaphore=enrichment_slots,
                )
                streamed_enriched += int(streamed_free[item.candidate.hostname]["enriched"])
                store.update_run_progress(run_id, phase="inspecting", enriched=streamed_enriched)

            with child_step("website_inspection"), source_trace(
                "website_inspection", child=True, run_id=run_id,
            ) as metrics:
                network_inspected = await _inspect_all(
                    client,
                    network_candidates,
                    concurrency=args.concurrency,
                    pages_per_site=args.pages_per_site,
                    max_bytes_per_page=args.max_bytes_per_page,
                    english_only=args.english_only,
                    microlink_enabled=args.microlink_enabled,
                    progress=lambda complete, total: store.update_run_progress(
                        run_id,
                        phase="inspecting",
                        discovered=len(candidates),
                        inspected=len(reused) + complete,
                        total_candidates=work_total,
                    ),
                    checkpoint=checkpoint_inspection,
                    circuit_breaker=circuit_breaker,
                    on_error=record_company_error,
                    ready=enrich_ready,
                )
                if metrics is not None:
                    metrics.result.update(
                        companies_checked=len(network_inspected),
                        fetch_outcomes=dict(Counter(
                            item.observation.fetch_outcome for item in network_inspected
                        )),
                        companies_with_homepage_timeout=sum(
                            "timeout" in error.lower() or "timed out" in error.lower()
                            for item in network_inspected
                            if (error := " ".join((
                                item.observation.error or "", item.observation.direct_fetch_error,
                            )))
                        ),
                    )
            network_inspected = [item for item in network_inspected
                                 if item.observation.fetch_disposition != LOCAL_CAPACITY_DEFERRED]
            inspected_by_host = {item.candidate.hostname: item for item in network_inspected}
            inspected_by_host.update({item.candidate.hostname: item for item in enrichment_retries})
            inspected_by_host.update(reused)
            inspected = [
                inspected_by_host[candidate.hostname]
                for candidate in candidates
                if candidate.hostname in inspected_by_host
            ]
            score_before_by_host = {
                item.candidate.hostname: core_scores.get(
                    item.candidate.hostname, item.score.model_dump_json(),
                ) for item in inspected
            }
            inspection_errors = [
                f"{item.candidate.hostname}: {item.observation.error}"
                for item in network_inspected
                if item.observation.error
            ]
            inspection_skips = [
                (
                    f"{item.candidate.hostname}: "
                    f"{item.observation.fetch_disposition or item.observation.fetch_outcome}"
                )
                for item in network_inspected
                if is_terminal_fetch_outcome(item.observation)
            ]
            free_summary: dict[str, object] = {
                "eligible": 0,
                "enriched": 0,
                "cache_hits": 0,
                "network_calls": 0,
                "vendors_detected": 0,
                "disclosures_found": 0,
                "feeds_found": 0,
                "catalogs_found": 0,
                "errors": [],
                "error_hosts": [],
            }
            if args.free_intelligence:
                remaining_enrichment = [
                    item for item in [*network_inspected, *enrichment_retries]
                    if item.candidate.hostname not in streamed_free
                ]
                store.update_run_progress(
                    run_id,
                    phase="free_intelligence",
                    inspected=len(reused) + streamed_enriched,
                    enriched=streamed_enriched,
                    total_candidates=streamed_enriched + sum(
                        item.observation.live and is_english_eligible(
                            item.observation, english_only=args.english_only,
                        ) for item in remaining_enrichment
                    ),
                )
                free_summary = await _free_enrich_all(
                    client,
                    remaining_enrichment,
                    store=store,
                    concurrency=args.concurrency,
                    max_sitemaps_per_site=args.max_sitemaps_per_site,
                    english_only=args.english_only,
                    progress=lambda complete, total: store.update_run_progress(
                        run_id,
                        phase="free_intelligence",
                        inspected=len(reused) + streamed_enriched + complete,
                        enriched=streamed_enriched + complete,
                        total_candidates=streamed_enriched + total,
                    ),
                    checkpoint=checkpoint_free_enrichment,
                    activity=emit_action,
                    on_error=record_company_error,
                )

            if streamed_free:
                for key in free_summary:
                    if key in {"errors", "error_hosts"}:
                        free_summary[key] = [
                            value for row in [*streamed_free.values(), free_summary]
                            for value in row[key]
                        ]
                    else:
                        free_summary[key] += sum(int(row[key]) for row in streamed_free.values())
                free_summary["errors"] = free_summary["errors"][:20]
                store.update_run_progress(
                    run_id, phase="free_intelligence",
                    inspected=len(reused) + int(free_summary["enriched"]),
                    enriched=int(free_summary["enriched"]),
                    total_candidates=int(free_summary["eligible"]),
                )

            cross_site_summary = enrich_cross_site(
                (item.candidate, item.observation)
                for item in inspected
                if is_english_eligible(item.observation, english_only=args.english_only)
            )
            for item in inspected:
                rescore(item)

            # Public inspection and site-owned enrichment are the expensive bulk work.
            # Persist them before bounded archive/news/AI phases so an interrupted run can
            # resume from these snapshots instead of fetching the same hosts again.
            _save_snapshot_batch(
                store,
                run_id,
                [item for item in network_inspected
                 if item.candidate.hostname not in company_processing_errors],
                phase="checkpointing",
                on_error=record_company_error,
            )
            network_hosts = {item.candidate.hostname for item in network_inspected}
            optional_observation_before = {
                item.candidate.hostname: item.observation.model_dump_json() for item in inspected
            }

            frontier_record_source = None
            frontier_source_summary: dict[str, object] = {
                "directory": args.frontier_data_dir,
                "status": "disabled",
                "scanners": {},
                "files": [],
                "errors": [],
            }
            if args.free_intelligence and args.max_frontier_scans:
                frontier_record_source, frontier_source_summary = _load_frontier_record_catalog(
                    args.frontier_data_dir
                )

            frontier_targets = [
                FrontierTarget(
                    candidate_id=candidate_ids_by_host[item.candidate.hostname],
                    candidate=item.candidate,
                    observation=item.observation,
                    company_name=(
                        item.ai.company_name
                        if item.ai and item.ai.company_name
                        else item.candidate.label_hint
                    ),
                    current_action=(item.score.creative_capacity.decision_packet.action),
                    boundary_score=item.score.creative_capacity.opportunity.founder_priority,
                    persist_embedded=item.candidate.hostname in network_hosts,
                )
                for item in inspected
                if item.observation.live and item.candidate.hostname in candidate_ids_by_host
            ]
            frontier_summary: dict[str, object] = {
                "targets": len(frontier_targets),
                "embedded_groups": 0,
                "embedded_completed": 0,
                "embedded_reused": 0,
                "schedule_decisions": 0,
                "optional_selected": 0,
                "optional_completed": 0,
                "optional_partial": 0,
                "optional_reused": 0,
                "optional_deferred": 0,
                "optional_failed": 0,
                "fetch_requests": 0,
                "fetch_bytes": 0,
                "selected_scanners": [],
                "feature_snapshots": 0,
                "local_sources": frontier_source_summary,
                "errors": [],
                "error_hosts": [],
            }
            if frontier_targets:
                store.update_run_progress(
                    run_id,
                    phase="frontier_intelligence",
                    external_checked=0,
                    total_candidates=0,
                )
                last_frontier_progress = time.monotonic()

                def frontier_progress() -> None:
                    nonlocal last_frontier_progress
                    now = time.monotonic()
                    if now - last_frontier_progress >= 5:
                        store.update_run_progress(run_id, phase="frontier_intelligence")
                        last_frontier_progress = now

                def frontier_work_progress(completed: int, total: int) -> None:
                    store.update_run_progress(
                        run_id, phase="frontier_intelligence",
                        external_checked=completed, total_candidates=total,
                    )

                frontier_result = await orchestrate_frontier_intelligence(
                    store,
                    frontier_targets,
                    run_id=run_id,
                    client=client,
                    maximum_optional_scans=(
                        args.max_frontier_scans if args.free_intelligence else 0
                    ),
                    record_source=frontier_record_source,
                    manual_scanner_ids=frontier_scanners,
                    company_scanner_ids=config.frontier_scanner_manifest,
                    manual_force_refresh=not args.respect_frontier_schedule,
                    activity=emit_action,
                    progress=frontier_progress,
                    work_progress=frontier_work_progress,
                )
                frontier_summary = frontier_result.as_dict()
                frontier_summary["feature_snapshots"] = 0
                frontier_summary["local_sources"] = frontier_source_summary
                for item in inspected:
                    rescore(item)

            local_source_errors = _frontier_source_error_messages(frontier_source_summary)
            if local_source_errors:
                frontier_errors = list(frontier_summary.get("errors", []))
                frontier_errors.extend(local_source_errors)
                frontier_summary["errors"] = list(dict.fromkeys(frontier_errors))[:20]

            news_summary: dict[str, object] = {
                "eligible": 0,
                "checked": 0,
                "fresh_calls": 0,
                "articles": 0,
                "triggers": 0,
                "errors": [],
                "error_hosts": [],
            }
            if args.free_intelligence and args.max_news_lookups:
                store.update_run_progress(
                    run_id,
                    phase="news_intelligence",
                    external_checked=0,
                    total_candidates=args.max_news_lookups,
                )
                news_summary = await _news_enrich(
                    client,
                    _optional_enrichment_candidates(
                        inspected,
                        store=store,
                        cache_kind="gdelt_news",
                        freshly_inspected_hosts=network_hosts,
                    ),
                    store=store,
                    maximum=args.max_news_lookups,
                    progress=lambda complete, total: store.update_run_progress(
                        run_id,
                        phase="news_intelligence",
                        external_checked=complete,
                        total_candidates=total,
                    ),
                    activity=emit_action,
                )
                for item in inspected:
                    rescore(item)

            archive_summary: dict[str, object] = {
                "eligible": 0,
                "checked": 0,
                "fresh_calls": 0,
                "errors": [],
                "error_hosts": [],
            }
            if args.free_intelligence and args.max_common_crawl_lookups:
                store.update_run_progress(
                    run_id,
                    phase="archive_footprint",
                    external_checked=0,
                    total_candidates=args.max_common_crawl_lookups,
                )
                archive_summary = await _common_crawl_enrich(
                    client,
                    _optional_enrichment_candidates(
                        inspected,
                        store=store,
                        cache_kind="common_crawl",
                        freshly_inspected_hosts=network_hosts,
                    ),
                    store=store,
                    maximum=args.max_common_crawl_lookups,
                    progress=lambda complete, total: store.update_run_progress(
                        run_id,
                        phase="archive_footprint",
                        external_checked=complete,
                        total_candidates=total,
                    ),
                    activity=emit_action,
                )
                for item in inspected:
                    rescore(item)

        # Every already-fetched company page can expose additional companies. Queue resolved
        # identities for the next ordinary adaptive run, but never recursively fetch them here.
        # If an appeared company already has a snapshot, merge the new source edge and rescore
        # that stored evidence locally.
        (
            appearance_candidates,
            legacy_portfolio_candidates,
            appearance_summary,
        ) = _collect_company_appearance_candidates(inspected)
        appearance_hosts = [candidate.hostname for candidate in appearance_candidates]
        appearance_existing = {
            candidate.hostname: candidate
            for candidate in store.candidates_by_hosts(appearance_hosts)
        }
        appearance_changed_hosts = {
            candidate.hostname
            for candidate in appearance_candidates
            if _candidate_has_new_assertions(
                appearance_existing.get(candidate.hostname),
                candidate,
            )
        }
        store.upsert_derived_candidates(appearance_candidates)
        store.upsert_derived_candidates(legacy_portfolio_candidates)

        stored_appearance_candidates = {
            candidate.hostname: candidate
            for candidate in store.candidates_by_hosts(appearance_hosts)
        }
        current_inspected_by_host = {
            item.candidate.hostname: item for item in inspected
        }
        appearance_updated_current_hosts: set[str] = set()
        for hostname in sorted(appearance_changed_hosts & current_inspected_by_host.keys()):
            item = current_inspected_by_host[hostname]
            stored_candidate = stored_appearance_candidates.get(hostname)
            if stored_candidate is None:
                continue
            item.candidate = _merge_candidate_provenance(item.candidate, stored_candidate)
            rescore(item)
            appearance_updated_current_hosts.add(hostname)

        local_appearance_hosts = sorted(
            appearance_changed_hosts - current_inspected_by_host.keys()
        )
        local_appearance_snapshots = store.latest_snapshots(local_appearance_hosts)
        locally_rescored_appearances: list[InspectedCandidate] = []
        for hostname in local_appearance_hosts:
            snapshot = local_appearance_snapshots.get(hostname)
            stored_candidate = stored_appearance_candidates.get(hostname)
            if snapshot is None or stored_candidate is None:
                continue
            observation = snapshot["observation"]
            if not isinstance(observation, SiteObservation):
                continue
            item = InspectedCandidate(
                stored_candidate, observation, snapshot["score"], snapshot["ai"],
            )
            if rescore(item, stage="company-appearance scoring", previous_observation=observation):
                locally_rescored_appearances.append(item)

        local_appearance_ids = store.candidate_ids(
            [item.candidate.hostname for item in locally_rescored_appearances]
        )
        candidate_ids_by_host.update(local_appearance_ids)
        local_appearance_completions = store.completed_company_snapshots(
            list(local_appearance_ids.values())
        )
        for hostname, candidate_id in local_appearance_ids.items():
            completion = local_appearance_completions.get(candidate_id)
            if completion is not None:
                reused_snapshot_by_host[hostname] = completion

        appearance_summary.update(
            {
                "queued": len(appearance_candidates),
                "new_candidate_targets": sum(
                    hostname not in appearance_existing for hostname in appearance_hosts
                ),
                "new_assertion_targets": len(appearance_changed_hosts),
                "current_run_targets_rescored": len(appearance_updated_current_hosts),
                "stored_targets_rescored": len(locally_rescored_appearances),
                "legacy_portfolio_queued": len(legacy_portfolio_candidates),
                "backlog_loaded": appearance_backlog_loaded,
                "legacy_backlog_loaded": legacy_portfolio_backlog_loaded,
                "network_scans_started": 0,
                "cap": APPEARANCE_EXPANSION_CAP,
            }
        )

        ai_summary: dict[str, object] = {
            "errors": [],
            "reviewed": 0,
            "fresh_calls": 0,
            "fresh_web_calls": 0,
            "cache_hits": 0,
        }
        ai_before = {
            item.candidate.hostname: item.ai.model_dump_json() if item.ai else None
            for item in inspected
        }
        if args.ai:
            store.update_run_progress(
                run_id,
                phase="ai_review",
                inspected=len(reused),
                external_checked=0,
            )
            ai_summary = await _ai_enrich(
                inspected,
                store=store,
                min_score=args.min_ai_score,
                max_calls=args.max_ai_calls,
                max_web_calls=args.max_ai_web_calls,
                model=args.model,
                web_search=args.ai_web,
                fresh_call_hosts={item.candidate.hostname for item in inspected},
                progress=lambda complete, total: store.update_run_progress(
                    run_id,
                    phase="ai_review",
                    external_checked=complete,
                    total_candidates=total,
                ),
                activity=emit_action,
            )

        ai_recovery_candidates: list[DiscoveredHost] = []
        seen_recovery_hosts: set[str] = set()
        for item in inspected:
            if not item.ai or not item.ai.company_url:
                continue
            hostname = canonicalize_public_website(item.ai.company_url)
            current_identity = (
                item.observation.canonical_host or item.candidate.hostname
            ).removeprefix("www.")
            if not hostname or hostname == current_identity or hostname in seen_recovery_hosts:
                continue
            seen_recovery_hosts.add(hostname)
            ai_recovery_candidates.append(
                DiscoveredHost(
                    source_apex="ai-recovery",
                    hostname=hostname,
                    discovery_rank=min(
                        50.0,
                        24.0
                        + float(item.ai.placement_fit_score or item.ai.budget_fit_score) * 0.25,
                    ),
                    label_hint=f"Public AI identity recovery · {item.candidate.hostname}",
                )
            )
        store.upsert_derived_candidates(ai_recovery_candidates)

        save_items = [
            item
            for item in inspected
            if item.candidate.hostname in network_hosts
            or item.candidate.hostname in appearance_updated_current_hosts
            or item.observation.model_dump_json()
            != optional_observation_before[item.candidate.hostname]
            or (item.ai.model_dump_json() if item.ai else None)
            != ai_before[item.candidate.hostname]
            or item.score.model_dump_json()
            != score_before_by_host[item.candidate.hostname]
        ]
        save_items.extend(locally_rescored_appearances)
        save_items = [
            item for item in save_items
            if item.candidate.hostname not in company_processing_errors
        ]
        changed = _save_snapshot_batch(
            store,
            run_id,
            save_items,
            phase="saving",
            on_error=record_company_error,
        )
        feature_snapshot_count, feature_snapshot_errors = _persist_frontier_feature_snapshots(
            store, inspected
        )
        frontier_summary["feature_snapshots"] = feature_snapshot_count
        if feature_snapshot_errors:
            frontier_errors = list(frontier_summary.get("errors", []))
            frontier_errors.extend(feature_snapshot_errors)
            frontier_summary["errors"] = list(dict.fromkeys(frontier_errors))[:20]
            frontier_error_hosts = list(frontier_summary.get("error_hosts", []))
            frontier_error_hosts.extend(error.split(":", 1)[0] for error in feature_snapshot_errors)
            frontier_summary["error_hosts"] = list(dict.fromkeys(frontier_error_hosts))[:100]
        for item in save_items:
            hostname = item.candidate.hostname
            if hostname in network_hosts or hostname not in reused_snapshot_by_host:
                continue
            candidate_id = candidate_ids_by_host.get(hostname)
            if candidate_id is not None:
                store.update_completed_company_snapshot(candidate_id, run_id)

        # Core owned-site work governs the 90-day company-completion ledger. News,
        # Common Crawl, and AI are independent optional enrichments: their failures
        # remain visible and retryable at their own cache/call cadence, but must never
        # force pages, sitemaps, jobs, contacts, catalogs, and vendors to run again.
        free_failure_hosts = set(free_summary.get("error_hosts", []))
        core_completed_hosts = {
            item.candidate.hostname
            for item in network_inspected
            if observation_completes_company_work(item.observation)
            and item.candidate.hostname not in free_failure_hosts
            and item.candidate.hostname not in company_processing_errors
        }
        completed_company_candidate_ids = {
            candidate_id
            for candidate_id in claimed_candidate_ids
            if claimed_hostnames_by_id.get(candidate_id) in core_completed_hosts
        }
        failed_company_candidate_ids = (
            claimed_candidate_ids - completed_company_candidate_ids - locally_deferred_ids
        )
        retryable_hosts = sorted(
            claimed_hostnames_by_id[candidate_id]
            for candidate_id in failed_company_candidate_ids
            if candidate_id in claimed_hostnames_by_id
        )
        deferred_enrichment_hosts = sorted(
            set(
                list(news_summary.get("error_hosts", []))
                + list(archive_summary.get("error_hosts", []))
                + list(ai_summary.get("error_hosts", []))
                + list(frontier_summary.get("error_hosts", []))
            )
        )
        storage_after = usage_snapshot(store.connection)
        summary = {
            "database_storage": {
                "before": storage_before, "after": storage_after,
                "live_page_bytes_delta": (
                    storage_after["live_page_bytes"] - storage_before["live_page_bytes"]
                ),
                "scope": "database allocation during run, including concurrent writers",
            },
            "sources_requested": len(apexes),
            "discovery_mode": "persisted_hosts" if explicit_hosts else "source_enumeration",
            "explicit_hosts_requested": len(explicit_hosts),
            "explicit_hosts_loaded": discovered_candidate_count if explicit_hosts else 0,
            "explicit_hosts_missing": [],
            "source_errors": len(source_errors) + len(company_processing_errors),
            "company_processing_errors": {
                "count": len(company_processing_errors),
                "samples": [f"{host}: {error}" for host, error
                            in list(company_processing_errors.items())[:20]],
            },
            "inspection_errors": {
                "count": len(inspection_errors),
                "samples": inspection_errors[:20],
            },
            "inspection_skips": {
                "count": len(inspection_skips),
                "samples": inspection_skips[:20],
            },
            "retrieval_recovery": {
                "fetch_attempts": sum(
                    item.observation.fetch_attempts for item in network_inspected
                ),
                "additional_attempts": sum(
                    max(item.observation.fetch_attempts - 1, 0)
                    for item in network_inspected
                ),
                "retried_candidates": sum(
                    item.observation.fetch_attempts > 1 for item in network_inspected
                ),
                "recovered_candidates": sum(
                    item.observation.recovered_after_retry for item in network_inspected
                ),
                "microlink_attempted": sum(
                    item.observation.fallback_attempted for item in network_inspected
                ),
                "microlink_recovered": sum(
                    item.observation.fetch_provider == "microlink"
                    for item in network_inspected
                ),
            },
            "platform_apex_circuits": circuit_breaker.summary(),
            "process_resource_usage": resource_metrics(),
            "candidates_discovered": discovered_candidate_count,
            "candidates_inventory_persisted": discovered_candidate_count,
            "candidates_hydration_backlog_merged": len(persisted_backlog),
            "candidates_with_source_intent": len(source_profiles),
            "candidates_infrastructure_skipped": infrastructure_skipped,
            "candidates_evaluated": len(inspected),
            "candidates_reused_without_network": len(reused),
            "candidates_reused_within_90d": sum(
                _completion_age_hours(snapshot) < COMPANY_SCAN_FRESH_HOURS
                for snapshot in reused_snapshot_by_host.values()
            ),
            "candidates_reused_within_30d": sum(
                _completion_age_hours(snapshot) < 30 * 24
                for snapshot in reused_snapshot_by_host.values()
            ),
            # Retained as a compatibility alias for existing report consumers. Its
            # meaning remains literal rather than representing the full reuse window.
            "candidates_reused_within_24h": sum(
                _completion_age_hours(snapshot) < 24
                for snapshot in reused_snapshot_by_host.values()
            ),
            "candidates_network_scanned": len(network_inspected),
            "candidates_enrichment_retried": len(enrichment_retries),
            "companies_claimed_for_network": len(claimed_candidate_ids),
            "companies_deferred_by_local_capacity": len(locally_deferred_ids),
            "companies_skipped_active_scan": len(active_claim_candidate_ids),
            "companies_deferred_after_failure": len(retry_deferred_candidate_ids),
            "companies_deduplicated_by_identity": len(deduplicated_candidate_ids),
            "companies_force_refreshed": (len(claimed_candidate_ids) if args.force_refresh else 0),
            "companies_manually_refreshed": (
                len(claimed_candidate_ids) if args.manual_company_refresh else 0
            ),
            "candidates_non_english_filtered": sum(
                item.observation.language.english_presentation is False
                for item in network_inspected
            ),
            "candidates_deferred_due_to_network_budget": deferred_candidates,
            "english_only": args.english_only,
            "sites_live": sum(item.observation.live for item in inspected),
            "scored_70_plus": sum(item.final_score >= 70 for item in inspected),
            "scored_80_plus": sum(item.final_score >= 80 for item in inspected),
            "budget_ready": sum(item.score.budget_ready for item in inspected),
            "placement_ready": sum(item.score.placement.ready for item in inspected),
            "placement_very_high": sum(
                item.score.placement.confidence == "very_high" for item in inspected
            ),
            "materially_changed": changed,
            "cross_site": cross_site_summary,
            "agency_discovery": agency_summary,
            "portfolio_expansion": {
                "queued": len(legacy_portfolio_candidates),
                "cap": APPEARANCE_EXPANSION_CAP,
                "compatibility_fallback": True,
            },
            "company_appearance_vacuum": appearance_summary,
            "ai_identity_recovery": {
                "queued": len(ai_recovery_candidates),
                "cap": args.max_ai_calls,
            },
            "free_intelligence": free_summary,
            "common_crawl": archive_summary,
            "news_intelligence": news_summary,
            "frontier_intelligence": frontier_summary,
            "ai_reviewed": ai_summary["reviewed"],
            "ai_calls": ai_summary["fresh_calls"],
            "ai_web_calls": ai_summary["fresh_web_calls"],
            "ai_cache_hits": ai_summary["cache_hits"],
            "ai_errors": ai_summary["errors"],
            "retryable_hosts": retryable_hosts,
            "deferred_enrichment_hosts": deferred_enrichment_hosts,
        }
        failure_count = (
            len(source_errors)
            + len(set(company_processing_errors) - set(retryable_hosts))
            + len(agency_summary.get("errors", []))
            + len(retryable_hosts)
            + len(news_summary.get("errors", []))
            + len(archive_summary.get("errors", []))
            + len(frontier_summary.get("errors", []))
            + len(ai_summary["errors"])
        )
        summary["failure_count"] = failure_count
        unsuccessful_hosts = set(retryable_hosts) | company_processing_errors.keys()
        summary["success_count"] = sum(
            item.candidate.hostname not in unsuccessful_hosts for item in inspected
        )
        status = "partial" if failure_count or locally_deferred_ids else "complete"
        with child_step("settle_company_work"):
            store.finish_run_with_company_scan_work(
                run_id,
                status,
                summary,
                completed_candidate_ids=sorted(completed_company_candidate_ids),
                failed_candidate_ids=sorted(failed_company_candidate_ids),
                failure_reason="; ".join(retryable_hosts[:5]),
            )
        run_finalized = True
        child_record(
            "company_settled", run_id=run_id, status=status,
            core_completed=len(completed_company_candidate_ids),
            core_failed=len(failed_company_candidate_ids),
            local_capacity_deferred=len(locally_deferred_ids),
            website_checks=len(network_inspected), reused=len(reused),
        )
        # Internal dashboard workers publish their complete result in SQLite; their
        # parent reads that durable receipt and never consumes these report files.
        # Do not hydrate/rank another report cohort or duplicate evidence on disk.
        managed_worker = bool(args.job_id and args.database_ready)
        if not managed_worker:
            metadata = store.run_metadata(run_id)
            prospects = store.get_ranked(run_id, args.report_limit)
            write_reports(
                metadata,
                prospects,
                markdown_path=args.report,
                json_path=args.json_report,
                minimum_score=args.minimum_report_score,
            )
        print(json.dumps(summary, indent=2), file=sys.stderr)
        if managed_worker:
            print(json.dumps({"run_id": run_id, "status": status}))
        else:
            print(str(Path(args.report).resolve()))
        return 0
    except asyncio.CancelledError:
        if not run_finalized:
            incomplete_candidate_ids = (
                claimed_candidate_ids - checkpointed_complete_candidate_ids - locally_deferred_ids
            )
            retryable_hosts = sorted(
                claimed_hostnames_by_id[candidate_id]
                for candidate_id in incomplete_candidate_ids
                if candidate_id in claimed_hostnames_by_id
            )
            store.finish_run_with_company_scan_work(
                run_id,
                "partial",
                {
                    "interrupted": True,
                    "completed_before_interrupt": len(checkpointed_complete_candidate_ids),
                    "retryable_hosts": retryable_hosts,
                },
                completed_candidate_ids=sorted(checkpointed_complete_candidate_ids),
                failed_candidate_ids=sorted(incomplete_candidate_ids),
                failure_reason="scan_interrupted",
            )
            run_finalized = True
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if not run_finalized:
            incomplete_candidate_ids = (
                claimed_candidate_ids - checkpointed_complete_candidate_ids - locally_deferred_ids
            )
            store.finish_run_with_company_scan_work(
                run_id,
                "failed",
                {
                    "error": error,
                    "completed_before_failure": len(checkpointed_complete_candidate_ids),
                    "retryable_hosts": sorted(
                        claimed_hostnames_by_id[candidate_id]
                        for candidate_id in incomplete_candidate_ids
                        if candidate_id in claimed_hostnames_by_id
                    ),
                },
                completed_candidate_ids=sorted(checkpointed_complete_candidate_ids),
                failed_candidate_ids=sorted(incomplete_candidate_ids),
                failure_reason=error,
            )
        raise
    finally:
        store.close()
        DATABASE.reset(rate_token)


def _run_report(args: argparse.Namespace) -> int:
    with LeadStore(args.db) as store:
        metadata = store.run_metadata(args.run_id)
        if not metadata:
            raise ValueError("No completed or partial run found")
        prospects = store.get_ranked(metadata["id"], args.report_limit)
        write_reports(
            metadata,
            prospects,
            markdown_path=args.report,
            json_path=args.json_report,
            minimum_score=args.minimum_report_score,
        )
    print(str(Path(args.report).resolve()))
    return 0


def _source_scout_diagnostics(value: object) -> dict[str, object]:
    diagnostics = getattr(value, "diagnostics", {})
    return dict(diagnostics) if isinstance(diagnostics, dict) else {}


async def _run_source_scout(args: argparse.Namespace) -> int:
    if not os.getenv("OPENAI_API_KEY"):
        raise ValueError("scout-sources requires OPENAI_API_KEY")
    if not 1 <= args.max_suggestions <= 12:
        raise ValueError("--max-suggestions must be between 1 and 12")
    if not 1 <= args.max_tool_calls <= 10:
        raise ValueError("--max-tool-calls must be between 1 and 10")
    with LeadStore(args.db) as store:
        run_id = store.start_scout_run(args.model, args.max_tool_calls)
    rate_token = DATABASE.set(Path(args.db))
    try:
        with LeadStore(args.db) as store:
            existing_sources = [source["apex"] for source in store.list_sources()]
            suggestion_rows = store.list_source_suggestions()
            existing_suggestions = list(
                dict.fromkeys(
                    str(value)
                    for suggestion in suggestion_rows
                    for value in (
                        suggestion.get("source_key"),
                        suggestion.get("source_identity_key"),
                    )
                    if value
                )
            )
            prior_source_outcomes = store.source_scout_source_context(limit=200)
            recent_query_fingerprints = store.recent_source_scout_query_fingerprints()
        suggestions = await scout_sources(
            existing_sources,
            existing_suggestions,
            model=args.model,
            max_suggestions=args.max_suggestions,
            max_tool_calls=args.max_tool_calls,
            cycle=run_id,
            prior_source_outcomes=prior_source_outcomes,
            recent_query_fingerprints=recent_query_fingerprints,
        )
        coverage = _source_scout_output_coverage(suggestions)
        diagnostics = _source_scout_diagnostics(suggestions)
        run_status = (
            "partial"
            if diagnostics.get("failed_source_families")
            or diagnostics.get("tool_budget_overrun_source_families")
            else "complete"
        )
        with LeadStore(args.db) as store:
            store.upsert_source_suggestions(run_id, suggestions)
            store.finish_scout_run(
                run_id,
                run_status,
                {
                    "suggestions": len(suggestions),
                    "max_tool_calls": args.max_tool_calls,
                    **diagnostics,
                    **coverage,
                },
            )
        print(json.dumps([item.model_dump(mode="json") for item in suggestions], indent=2))
        return 0
    except Exception as exc:
        diagnostics = _source_scout_diagnostics(exc)
        with LeadStore(args.db) as store:
            store.finish_scout_run(
                run_id,
                "failed",
                {
                    "error": f"{type(exc).__name__}: {exc}",
                    **diagnostics,
                },
            )
        raise

    finally:
        DATABASE.reset(rate_token)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {"scan", "monitor"}:
            if getattr(args, "only_hosts_stdin", False):
                raw = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError("Host input exceeds 4 MiB")
                hosts = json.loads(raw)
                if (not isinstance(hosts, list) or not 1 <= len(hosts) <= 10_000
                        or not all(isinstance(host, str) and host for host in hosts)):
                    raise ValueError("Host input must contain 1–10,000 hostnames")
                args.only_hosts = [*(args.only_hosts or []), *hosts]
            return asyncio.run(_run_scan(args))
        if args.command == "report":
            return _run_report(args)
        if args.command == "scout-sources":
            return asyncio.run(_run_source_scout(args))
        if args.command == "stats":
            with LeadStore(args.db) as store:
                print(json.dumps(store.stats(), indent=2))
            return 0
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
