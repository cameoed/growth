from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from .creative_capacity import (
    BuyerRoleHypothesis,
    BuyingSignalEvent,
    ConfidenceLevel,
    DecisionProofPoint,
    DerivedSignalName,
    NextBestScan,
    OpportunityVector,
    PublicPersonReference,
    RevenueAction,
    SalesDecisionPacket,
    SignalCluster,
    SignalFamily,
)
from .evidence_policy import (
    DURABLE_FAMILIES,
    DURABLE_HORIZON_DAYS,
    event_weight,
    historical_operating_context,
)
from .evidence_policy import (
    VERSION as FRESHNESS_VERSION,
)
from .models import (
    FRONTIER_SCANNER_IDS,
    AdIntelligence,
    DiscoveredHost,
    SiteObservation,
    legacy_agency_portfolio_domains,
)
from .value_utils import mapping_or_empty as _frontier_mapping

_CONFIDENCE_WEIGHT: dict[ConfidenceLevel, float] = {
    "low": 0.45,
    "medium": 0.72,
    "high": 1.0,
}
_COVERAGE_WEIGHT = {
    "unknown": 0.70,
    "partial": 0.85,
    "complete": 1.0,
    "failed": 0.0,
}
_FAMILY_CAP: dict[SignalFamily, int] = {
    "distribution_investment": 90,
    "recurring_workload": 90,
    "production_pressure": 90,
    "commercial_timing": 85,
    "ability_proxy": 80,
    "buyer_access": 85,
    "proof_match": 80,
    "risk": 100,
    "coverage": 100,
}
_DIMENSIONS = ("fit", "need", "timing", "ability", "access", "proof")
_EXTERNAL_CAPACITY_TERMS = (
    "overflow",
    "white-label",
    "white label",
    "flexible capacity",
    "scale up or down",
    "production partner",
    "freelance network",
    "contract creative",
)
_INTERNAL_CAPACITY_TERMS = (
    "in-house studio",
    "in house studio",
    "internal creative team",
    "our production team",
    "full-service production team",
)
_RIGHTS_RISK_TERMS = (
    "no generative ai",
    "ai prohibited",
    "do not use artificial intelligence",
    "model release required",
    "rights-managed imagery",
    "rights managed imagery",
)
_COMPETITIVE_TERMS = (
    "unlimited ai creative",
    "unlimited ai content",
    "ai content subscription",
    "ai creative subscription",
)

# Market-stream projections share the append-only evidence ledger with the company scanners
# above, but they are a separate scanner class. Keep this tuple separate so adding a
# source-level feed can never silently change the exact Discovery company-scanner contract.
MARKET_SCANNER_IDS = (
    "sec_legal_capital_stream_v1",
    "government_action_award_stream_v1",
    "open_news_radar_v1",
)
VENDOR_SCANNER_IDS = ("dataforseo_search_visibility_v1",)

VERSION = "revenue-evidence-v6-full-priority-scale"
# Maximum raw priority at the existing signal caps and maximum aggregate evidence.
_MAX_RAW_FOUNDER_PRIORITY = 85

_REVENUE_EVIDENCE_SCANNER_IDS = frozenset(
    (*FRONTIER_SCANNER_IDS, *MARKET_SCANNER_IDS, *VENDOR_SCANNER_IDS)
)


@dataclass(frozen=True, slots=True)
class RevenueSynthesisEvidence:
    """Scores already computed by deterministic callers, all optional and non-probabilistic."""

    archetype: str = "unknown"
    demand_score: int = 0
    capacity_score: int = 0
    timing_score: int = 0
    growth_score: int = 0
    data_confidence: int = 0
    placement_score: int = 0
    placement_ready: bool = False
    reachability_score: int = 0


@dataclass(frozen=True, slots=True)
class RevenueIntelligenceResult:
    signal_events: tuple[BuyingSignalEvent, ...]
    signal_clusters: tuple[SignalCluster, ...]
    opportunity: OpportunityVector
    decision_packet: SalesDecisionPacket
    next_best_scan: NextBestScan
    derived_signals: tuple[DerivedSignalName, ...]

    def hypothesis_update(self) -> dict[str, object]:
        """Return the additive fields accepted by ``CreativeCapacityHypothesis.model_copy``."""

        return {
            "version": "creative-capacity-v4-provisional",
            "signal_events": list(self.signal_events),
            "signal_clusters": list(self.signal_clusters),
            "opportunity": self.opportunity,
            "decision_packet": self.decision_packet,
            "next_best_scan": self.next_best_scan,
            "derived_signals": list(self.derived_signals),
        }


@dataclass(frozen=True, slots=True)
class ScannerInformationGainDecision:
    """One auditable scanner-selection decision, including exploration propensity."""

    scanner_id: str
    applicable: bool
    fresh: bool
    resolves_dimensions: tuple[str, ...]
    missing_dimensions_resolved: tuple[str, ...]
    success_probability: float
    expected_action_entropy_reduction: float
    action_boundary_sensitivity: float
    evidence_value: float
    normalized_cost: float
    rate_penalty: float
    identity_penalty: float
    risk_penalty: float
    expected_information_gain: float
    selected: bool
    exploration_selected: bool
    exploration_share: float
    exploration_draw: float
    selection_draw: float
    selection_propensity: float

    def log_record(self) -> dict[str, object]:
        """Return a persistence-ready record without coupling to the store models."""

        return {
            "scanner_id": self.scanner_id,
            "applicable": self.applicable,
            "fresh": self.fresh,
            "resolves_dimensions": list(self.resolves_dimensions),
            "missing_dimensions_resolved": list(self.missing_dimensions_resolved),
            "success_probability": self.success_probability,
            "expected_action_entropy_reduction": (self.expected_action_entropy_reduction),
            "action_boundary_sensitivity": self.action_boundary_sensitivity,
            "evidence_value": self.evidence_value,
            "normalized_cost": self.normalized_cost,
            "cost_penalty": self.normalized_cost,
            "rate_penalty": self.rate_penalty,
            "identity_penalty": self.identity_penalty,
            "risk_penalty": self.risk_penalty,
            "expected_information_gain": self.expected_information_gain,
            "selected": self.selected,
            "exploration_selected": self.exploration_selected,
            "exploration": self.exploration_selected,
            "exploration_share": self.exploration_share,
            "exploration_draw": self.exploration_draw,
            "selection_draw": self.selection_draw,
            "selection_propensity": self.selection_propensity,
            "propensity": self.selection_propensity,
        }


def _clamp(value: float | int) -> int:
    return max(0, min(100, round(value)))


def _unit_interval(value: object, *, field: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number between zero and one")
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise ValueError(f"{field} must be between zero and one")
    return parsed


def schedule_scanners_by_expected_information_gain(
    methods: Sequence[Mapping[str, object]],
    *,
    missing_dimensions: Sequence[str],
    exploration_share: float = 0.10,
    exploration_draw: float = 1.0,
    selection_draw: float = 0.0,
) -> tuple[ScannerInformationGainDecision, ...]:
    """Choose one action-changing scanner and expose its exact selection propensity.

    All numeric method inputs are normalized to ``0..1``. A method mapping accepts
    ``scanner_id``, ``applicable``, ``fresh``, ``resolves_dimensions``,
    ``success_probability``, ``expected_action_entropy_reduction``,
    ``action_boundary_sensitivity``, ``evidence_value``, ``normalized_cost``, and the
    three penalty fields. ``can_change_action`` admits a risk/verification method even when
    the current missing-dimension list is empty.

    Selection is a logged mixture policy: deterministic exploitation chooses the highest
    positive EIG, while exploration chooses uniformly from positive eligible methods. The
    caller supplies both draws so historical replays and tests remain deterministic.
    """

    share = _unit_interval(
        exploration_share,
        field="exploration_share",
        default=0.10,
    )
    explore_draw = _unit_interval(
        exploration_draw,
        field="exploration_draw",
        default=1.0,
    )
    choose_draw = _unit_interval(
        selection_draw,
        field="selection_draw",
        default=0.0,
    )
    missing = {str(value).strip() for value in missing_dimensions if str(value).strip()}
    provisional: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for method in methods:
        scanner_id = str(method.get("scanner_id") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", scanner_id):
            raise ValueError("scanner_id must be a stable lowercase identifier")
        if scanner_id in seen_ids:
            raise ValueError(f"duplicate scanner_id: {scanner_id}")
        seen_ids.add(scanner_id)
        raw_dimensions = method.get("resolves_dimensions", method.get("eig_dimensions", ()))
        if isinstance(raw_dimensions, str):
            dimensions = (raw_dimensions.strip(),) if raw_dimensions.strip() else ()
        elif isinstance(raw_dimensions, Sequence):
            dimensions = tuple(
                dict.fromkeys(str(value).strip() for value in raw_dimensions if str(value).strip())
            )
        else:
            raise ValueError("resolves_dimensions must be a sequence of strings")
        resolved = tuple(value for value in dimensions if value in missing)
        fresh = bool(method.get("fresh", False))
        applicable = bool(method.get("applicable", True))
        can_change_action = bool(method.get("can_change_action", False))
        applicable = applicable and not fresh and bool(resolved or can_change_action)
        success = _unit_interval(
            method.get("success_probability"),
            field="success_probability",
            default=0.5,
        )
        entropy = _unit_interval(
            method.get("expected_action_entropy_reduction"),
            field="expected_action_entropy_reduction",
            default=0.0,
        )
        boundary = _unit_interval(
            method.get("action_boundary_sensitivity"),
            field="action_boundary_sensitivity",
            default=0.0,
        )
        evidence_value = _unit_interval(
            method.get("evidence_value"),
            field="evidence_value",
            default=0.0,
        )
        cost = _unit_interval(
            method.get("normalized_cost", method.get("cost_penalty")),
            field="normalized_cost",
            default=0.0,
        )
        rate_penalty = _unit_interval(method.get("rate_penalty"), field="rate_penalty", default=0.0)
        identity_penalty = _unit_interval(
            method.get("identity_penalty"), field="identity_penalty", default=0.0
        )
        risk_penalty = _unit_interval(method.get("risk_penalty"), field="risk_penalty", default=0.0)
        gross_value = success * entropy * boundary * evidence_value
        net_value = gross_value - cost - rate_penalty - identity_penalty - risk_penalty
        eig = round(net_value, 8) if applicable else 0.0
        provisional.append(
            {
                "scanner_id": scanner_id,
                "applicable": applicable,
                "fresh": fresh,
                "resolves_dimensions": dimensions,
                "missing_dimensions_resolved": resolved,
                "success_probability": success,
                "expected_action_entropy_reduction": entropy,
                "action_boundary_sensitivity": boundary,
                "evidence_value": evidence_value,
                "normalized_cost": cost,
                "rate_penalty": rate_penalty,
                "identity_penalty": identity_penalty,
                "risk_penalty": risk_penalty,
                "expected_information_gain": eig,
            }
        )

    eligible = sorted(
        (
            item
            for item in provisional
            if item["applicable"] and float(item["expected_information_gain"]) > 0
        ),
        key=lambda item: (
            -float(item["expected_information_gain"]),
            str(item["scanner_id"]),
        ),
    )
    selected_id = ""
    exploration_selected = False
    if eligible:
        if share > 0 and explore_draw < share:
            exploration_selected = True
            exploration_pool = sorted(eligible, key=lambda item: str(item["scanner_id"]))
            index = min(int(choose_draw * len(exploration_pool)), len(exploration_pool) - 1)
            selected_id = str(exploration_pool[index]["scanner_id"])
        else:
            selected_id = str(eligible[0]["scanner_id"])
    uniform_probability = share / len(eligible) if eligible else 0.0
    exploitation_id = str(eligible[0]["scanner_id"]) if eligible else ""
    decisions: list[ScannerInformationGainDecision] = []
    for item in provisional:
        scanner_id = str(item["scanner_id"])
        propensity = 0.0
        if item in eligible:
            propensity = uniform_probability
            if scanner_id == exploitation_id:
                propensity += 1.0 - share
        decisions.append(
            ScannerInformationGainDecision(
                scanner_id=scanner_id,
                applicable=bool(item["applicable"]),
                fresh=bool(item["fresh"]),
                resolves_dimensions=tuple(item["resolves_dimensions"]),
                missing_dimensions_resolved=tuple(item["missing_dimensions_resolved"]),
                success_probability=float(item["success_probability"]),
                expected_action_entropy_reduction=float(item["expected_action_entropy_reduction"]),
                action_boundary_sensitivity=float(item["action_boundary_sensitivity"]),
                evidence_value=float(item["evidence_value"]),
                normalized_cost=float(item["normalized_cost"]),
                rate_penalty=float(item["rate_penalty"]),
                identity_penalty=float(item["identity_penalty"]),
                risk_penalty=float(item["risk_penalty"]),
                expected_information_gain=float(item["expected_information_gain"]),
                selected=scanner_id == selected_id,
                exploration_selected=(scanner_id == selected_id and exploration_selected),
                exploration_share=share,
                exploration_draw=explore_draw,
                selection_draw=choose_draw,
                selection_propensity=round(propensity, 8),
            )
        )
    return tuple(
        sorted(
            decisions,
            key=lambda item: (-item.expected_information_gain, item.scanner_id),
        )
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(normalized[:10])
        except ValueError:
            return None
    return _aware(parsed)


def _canonical_url(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return value.strip()
    if not parsed.scheme or not parsed.netloc:
        return value.strip()
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def stable_signal_event_key(
    subject_key: str,
    event_type: str,
    family: SignalFamily,
    source_url: str,
    *,
    identity: str = "",
    scanner_id: str = "",
    scanner_version: str = "",
    extractor_version: str = "",
    rule_id: str = "",
    rule_version: str = "",
) -> str:
    """Create a stable projection key without volatile scan timestamps or prose."""

    components = [
        subject_key.strip().lower(),
        event_type.strip().lower(),
        family,
        _canonical_url(source_url),
        identity.strip().lower(),
    ]
    lineage = (
        scanner_id.strip().lower(),
        scanner_version.strip().lower(),
        extractor_version.strip().lower(),
        rule_id.strip().lower(),
        rule_version.strip().lower(),
    )
    # Empty lineage preserves every legacy event key. Frontier projection keys include every
    # implementation/rule version so a changed extractor cannot silently replace older semantics.
    if any(lineage):
        components.extend(("lineage-v1", *lineage))
    payload = "|".join(components)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"revenue-v1:{digest}"


def decay_event(
    event: BuyingSignalEvent,
    *,
    now: datetime | None = None,
) -> BuyingSignalEvent:
    """Apply confidence, coverage, expiry, and exponential half-life decay."""

    reference = _aware(now) or datetime.now(UTC)
    effective_at = _aware(event.effective_at) or _aware(event.observed_at)
    expires_at = _aware(event.expires_at)
    observed_at = _aware(event.observed_at)
    if (
        effective_at is None
        or (effective_at > reference and event.event_type != "frontier_event_upcoming")
        or (observed_at is not None and observed_at > reference)
        or (expires_at is not None and reference >= expires_at)
    ):
        current = 0.0
    else:
        current = (
            event.strength
            * _CONFIDENCE_WEIGHT[event.confidence]
            * _COVERAGE_WEIGHT[event.coverage]
            * event_weight(event, effective_at, reference)
        )
    return event.model_copy(update={"current_strength": round(min(100.0, current), 2)})


def _deduplicated_strength(events: Sequence[BuyingSignalEvent], cap: int) -> tuple[int, int]:
    strongest_by_independence: dict[str, float] = {}
    for event in events:
        if event.current_strength <= 0:
            continue
        independence_key = event.independence_key.strip() or event.event_key
        strongest_by_independence[independence_key] = max(
            strongest_by_independence.get(independence_key, 0.0),
            event.current_strength,
        )
    strengths = sorted(strongest_by_independence.values(), reverse=True)
    if not strengths:
        return 0, 0
    # The strongest fact carries the family. Truly independent facts add bounded corroboration.
    score = strengths[0] + sum(strengths[1:3]) * 0.15
    return min(cap, round(score)), len(strengths)


def cluster_signal_events(
    events: Sequence[BuyingSignalEvent],
    *,
    now: datetime | None = None,
) -> list[SignalCluster]:
    """Reduce atomic evidence to one capped score per causal family."""

    decayed = [decay_event(event, now=now) for event in events]
    grouped: dict[SignalFamily, list[BuyingSignalEvent]] = defaultdict(list)
    for event in decayed:
        grouped[event.family].append(event)

    clusters: list[SignalCluster] = []
    for family in sorted(grouped):
        family_events = grouped[family]
        cap = _FAMILY_CAP[family]
        positive, positive_independent = _deduplicated_strength(
            [event for event in family_events if event.polarity == "positive"], cap
        )
        negative, negative_independent = _deduplicated_strength(
            [event for event in family_events if event.polarity == "negative"], cap
        )
        independent = len(
            {
                event.independence_key.strip() or event.event_key
                for event in family_events
                if event.current_strength > 0
            }
        )
        net = max(-100, min(100, positive - negative))
        ranked = sorted(family_events, key=lambda item: item.current_strength, reverse=True)
        dominant = next((item for item in ranked if item.current_strength > 0), None)
        dominant_positive = next(
            (item for item in ranked if item.current_strength > 0 and item.polarity == "positive"),
            None,
        )
        dominant_negative = next(
            (item for item in ranked if item.current_strength > 0 and item.polarity == "negative"),
            None,
        )
        if independent >= 2 and abs(net) >= 50:
            confidence: ConfidenceLevel = "high"
        elif abs(net) >= 25 or independent >= 2:
            confidence = "medium"
        else:
            confidence = "low"
        coverage_values = {event.coverage for event in family_events}
        if coverage_values == {"complete"}:
            coverage = "complete"
        elif coverage_values == {"failed"}:
            coverage = "failed"
        elif coverage_values & {"partial", "complete"}:
            # Coverage is only complete when every contributing surface says so. One complete
            # scanner must never conceal another partial, failed, or unattempted surface.
            coverage = "partial"
        else:
            coverage = "unknown"
        summary = dominant.summary if dominant else "No current evidence survives decay."
        if dominant and independent > 1:
            summary += f" Corroborated by {independent - 1} independent evidence path(s)."
        urls = [event.source_url or "" for event in ranked if event.source_url]
        dates = [
            value
            for event in ranked
            if (value := _aware(event.effective_at) or _aware(event.observed_at)) is not None
        ]
        clusters.append(
            SignalCluster(
                cluster_key=f"revenue-cluster-v1:{family}",
                family=family,
                event_keys=[event.event_key for event in ranked],
                positive_strength=positive,
                negative_strength=negative,
                net_strength=net,
                family_cap=cap,
                event_count=len(family_events),
                independent_evidence_count=max(
                    independent, positive_independent, negative_independent
                ),
                corroboration_count=max(0, independent - 1),
                confidence=confidence,
                coverage=coverage,
                summary=summary,
                positive_summary=dominant_positive.summary if dominant_positive else "",
                negative_summary=dominant_negative.summary if dominant_negative else "",
                evidence_urls=urls,
                observed_dates=dates,
            )
        )
    return clusters


def _all_urls(observation: SiteObservation) -> list[str]:
    values = [
        *observation.web.sales.source_urls,
        *observation.web.source_urls,
        *observation.evidence_urls,
        *(page.url for page in observation.pages),
        observation.final_url or "",
        *(contact.source_url for contact in observation.contacts.emails),
        *(contact.source_url for contact in observation.contacts.phones),
    ]
    return list(
        dict.fromkeys(value for value in values if value.startswith(("http://", "https://")))
    )


def _first_url(observation: SiteObservation, *tokens: str) -> str:
    # Website claims must never inherit the first DNS/vendor/feed evidence URL.
    urls = list(
        dict.fromkeys(
            [
                *(p.url for p in observation.pages),
                observation.final_url or "",
                *_all_urls(observation),
            ]
        )
    )
    urls = [url for url in urls if url]
    if tokens:
        matched = next(
            (url for url in urls if any(token.lower() in url.lower() for token in tokens)),
            None,
        )
        if matched:
            return matched
    if urls:
        return urls[0]
    return f"https://{observation.canonical_host or observation.hostname}/"


def _supported_signal_source(observation: SiteObservation, category: str) -> str | None:
    pages = [(p.url, " ".join((p.title, p.description, p.text_excerpt))) for p in observation.pages]
    if observation.final_url:
        pages.append(
            (
                observation.final_url,
                " ".join((observation.title, observation.description, observation.text_excerpt)),
            )
        )
    for value in observation.signals.get(category, []):
        explicit = _contextual_signal_source(value)
        if explicit:
            source_host = (urlsplit(explicit).hostname or "").removeprefix("www.")
            company = (observation.canonical_host or observation.hostname).removeprefix("www.")
            if source_host == company or source_host.endswith("." + company):
                return explicit
            continue
        phrase = str(value).strip().casefold()
        for url, excerpt in pages:
            for sentence in re.split(r"[.!?\n]", excerpt):
                if not phrase or phrase not in sentence.casefold():
                    continue
                if re.search(
                    r'["“”]|\b(?:client|customer)(?:s|\'s)?\s+'
                    r"(?:needs?|wanted|requested|produces|launches|runs|said|says)\b",
                    sentence,
                    re.I,
                ):
                    continue
                return url
    return None


def _retained_delivery_sources(observation: SiteObservation) -> list[tuple[str, str, list[str]]]:
    """Literal retained-client work narratives; service menus and client logos are insufficient."""
    pages = [(p.url, p.text_excerpt) for p in observation.pages]
    pages.append((observation.final_url or "", observation.text_excerpt))
    results = []
    for source, text in pages:
        if not source or not text:
            continue
        for match in re.finditer(
            r"\b(?:multi[- ]year|long[- ]term|retained)\s+"
            r"(?:brand\s+)?(?:content\s+)?(?:production\s+)?"
            r"partnerships?\b",
            text,
            re.I,
        ):
            excerpt = text[max(0, match.start() - 300) : match.end() + 650]
            # Delivery must have happened over time. Marketing an available retainer is fit only.
            if not re.search(
                r"\b(?:spanning|followed|produced|delivered|worked|since)\b", excerpt, re.I
            ):
                continue
            years = {int(y) for y in re.findall(r"\b(20\d{2})\b", excerpt)}
            if not years or min(years) >= observation.observed_at.year:
                continue
            categories = [
                name
                for name, pattern in {
                    "video": r"\b(?:videos?|brand films?|films?)\b",
                    "campaign": r"\b(?:campaigns?|launches?)\b",
                    "photography": r"\b(?:photography|photos?|shoots?)\b",
                    "social": r"\bsocial(?:[- ]first)?\b",
                }.items()
                if re.search(pattern, excerpt, re.I)
            ]
            if len(categories) < 2:
                continue
            results.append((source, excerpt.strip(), categories))
    return results[:6]


def _production_postings(observation: SiteObservation) -> dict[str, object]:
    now = _aware(observation.observed_at)
    result = {}
    for posting in observation.web.job_postings:
        if not posting.current or (posting.valid_through and _aware(posting.valid_through) < now):
            continue
        if not (
            {"creative", "photo_video_production"} & set(posting.role_families)
            or posting.production_pressure_signals
            or posting.external_capacity_signals
        ):
            continue
        key = posting.stable_id or f"{posting.provider}:{posting.source_url}"
        if not posting.source_url:
            continue
        result[key] = posting
    return result


def _complete_hiring_scopes(observation: SiteObservation) -> set[tuple[str, str]]:
    return {
        (m.scanner_id, m.scope or m.surface)
        for m in observation.web.frontier.coverage_manifests
        if m.scanner_id == "creative_hiring_demand_v1"
        and m.completeness == "complete"
        and not m.truncated
        and not m.failed_units
        and m.successful_units > 0
        and _aware(m.observed_at) == _aware(observation.observed_at)
    }


def _contextual_signal_source(value: str) -> str | None:
    """Return the page URL embedded by the contextual first-party extractor."""

    match = re.search(r"(?:^|\|\s*)source=(https?://\S+)\s*$", value.strip(), re.I)
    return match.group(1).rstrip(".,;)") if match else None


def _contextual_signal_subtype(value: str) -> str:
    head = value.split(":", 1)[0].strip().lower().replace("-", "_")
    return re.sub(r"[^a-z0-9_]+", "_", head).strip("_")


def _group_contextual_signals_by_source(
    observation: SiteObservation,
    category: str,
) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for value in observation.signals.get(category, []):
        source_url = _contextual_signal_source(value)
        # A contextual excerpt without its actual source is coverage, not business evidence.
        if not source_url:
            continue
        if value not in grouped[source_url]:
            grouped[source_url].append(value)
    return grouped


def _event(
    *,
    subject_key: str,
    event_type: str,
    family: SignalFamily,
    polarity: str,
    strength: int,
    confidence: ConfidenceLevel,
    coverage: str,
    source_kind: str,
    source_url: str,
    observed_at: datetime,
    summary: str,
    half_life_days: float,
    effective_at: datetime | None = None,
    identity: str = "",
    independence_key: str = "",
    epistemic_status: str = "observed",
    details: dict[str, str] | None = None,
    scanner_id: str = "",
    scanner_version: str = "",
    extractor_version: str = "",
    rule_id: str = "",
    rule_version: str = "",
    fallback_effective_to_observed: bool = True,
    expires_at: datetime | None = None,
) -> BuyingSignalEvent:
    effective = _aware(effective_at)
    if effective is None and fallback_effective_to_observed:
        effective = _aware(observed_at)
    expiry = _aware(expires_at)
    event_details = dict(details or {})
    # Website business/operating evidence is durable. Explicit deadlines and external
    # provider validity remain authoritative; freshness metadata belongs to the projection.
    if family in DURABLE_FAMILIES and source_kind in {
        "company_site", "catalog", "technology", "feed", "sitemap", "deterministic_synthesis",
        "ad_library",
    }:
        event_details["freshness_policy"] = FRESHNESS_VERSION
        if expiry is None:
            expiry = _aware(observed_at) + timedelta(days=DURABLE_HORIZON_DAYS)
    if expiry is None and effective is not None:
        expiry = effective + timedelta(days=half_life_days * 5)
    return BuyingSignalEvent(
        event_key=stable_signal_event_key(
            subject_key,
            event_type,
            family,
            source_url,
            identity=identity,
            scanner_id=scanner_id,
            scanner_version=scanner_version,
            extractor_version=extractor_version,
            rule_id=rule_id,
            rule_version=rule_version,
        ),
        subject_key=subject_key,
        event_type=event_type,
        family=family,
        polarity=polarity,
        strength=_clamp(strength),
        confidence=confidence,
        coverage=coverage,
        epistemic_status=epistemic_status,
        source_kind=source_kind,
        source_url=source_url,
        observed_at=_aware(observed_at),
        effective_at=effective,
        expires_at=expiry,
        half_life_days=half_life_days,
        independence_key=independence_key or f"{family}:{source_kind}",
        summary=summary,
        details=event_details,
    )


@dataclass(frozen=True, slots=True)
class _FrontierEventSpec:
    event_type: str
    family: SignalFamily
    strength: int
    summary: str
    half_life_days: float


_UNSAFE_FRONTIER_TIME_BASES = {
    "",
    "unknown",
    "scan_time",
    "scan_timestamp",
    "observed_at",
    "first_observed",
    "first_observed_at",
    "inferred",
}
_INVALID_FRONTIER_VALIDITY = {
    "ambiguous",
    "disputed",
    "expired",
    "invalid",
    "rejected",
    "superseded",
    "unknown",
}
_FRONTIER_STABLE_SURFACE_PREDICATES = {
    "creative.asset_concept_family_count",
    "lifecycle.public_campaign_count",
    "localization.declared_variant_count",
    "workforce.lca_certified_position_count",
    "ai.product_role",
    "media.publishing_pulse",
    "content.blog_engine",
    "product.release_engine",
    "product.release_current",
    "events.program_engine",
    "events.upcoming",
    "enterprise.trust_readiness",
    "measurement.public_funnel_schema",
    "seo.page_readiness",
    "seo.search_visibility_estimate",
    "seo.domain_rank_overview",
    "hiring.workforce_demand_current",
    "hiring.creative_demand_current",
    "hiring.creative_velocity_current",
}


def _frontier_number(value: object, *fallbacks: object) -> float | None:
    for candidate in (value, *fallbacks):
        if isinstance(candidate, bool):
            continue
        if isinstance(candidate, (int, float)):
            return max(0.0, float(candidate))
        if isinstance(candidate, str):
            try:
                return max(0.0, float(candidate.replace(",", "").strip()))
            except ValueError:
                continue
        if isinstance(candidate, Mapping):
            for key in (
                "count",
                "value",
                "items_30d",
                "recent_90d",
                "recent_30d",
                "campaign_count",
                "position_count",
                "variant_count",
                "concept_family_count",
            ):
                if key in candidate:
                    nested = _frontier_number(candidate[key])
                    if nested is not None:
                        return nested
    return None


def _frontier_text(value: object, qualifiers: Mapping[str, object]) -> str:
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, Mapping):
        for key in ("role", "classification", "product_role", "value"):
            text = str(value.get(key) or "").strip().lower()
            if text:
                return text
    for key in ("role", "classification", "product_role"):
        text = str(qualifiers.get(key) or "").strip().lower()
        if text:
            return text
    return ""


def _frontier_exact_count(value: Mapping[str, object], key: str) -> int | None:
    candidate = value.get(key)
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 0:
        return None
    return candidate


def _frontier_exact_nonnegative_number(
    value: Mapping[str, object],
    key: str,
) -> float | None:
    candidate = value.get(key)
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        return None
    normalized = float(candidate)
    return normalized if math.isfinite(normalized) and normalized >= 0 else None


def _frontier_exact_text(value: Mapping[str, object], key: str) -> str:
    candidate = value.get(key)
    return candidate.strip() if isinstance(candidate, str) else ""


def _frontier_exact_ratio(value: Mapping[str, object], key: str) -> float | None:
    candidate = value.get(key)
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        return None
    ratio = float(candidate)
    return ratio if 0.0 <= ratio <= 1.0 else None


def _frontier_normalized_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(
        dict.fromkeys(
            normalized
            for item in value
            if isinstance(item, str) and (normalized := item.strip().casefold())
        )
    )


_FRONTIER_TRUST_CONTROL_FAMILIES = frozenset(
    {
        "soc_assurance",
        "iso_27001",
        "identity_access",
        "encryption",
        "availability_resilience",
        "security_contact_policy",
        "assurance_documents",
    }
)
_FRONTIER_MEASUREMENT_FAMILIES = frozenset(
    {"awareness", "consideration", "checkout", "conversion", "media", "experiment"}
)


def _frontier_score_policy(
    qualifiers: Mapping[str, object],
    expected: str,
) -> bool:
    if str(qualifiers.get("score_policy") or "") != expected:
        return False
    # Revenue-bearing frontier rows must carry the parser's explicit literal gate. Missing legacy
    # fields, truthy strings, and parser-side false gates all fail closed.
    return qualifiers.get("projection_score_gate") is True


def _frontier_literal_hiring_gate(
    qualifiers: Mapping[str, object],
    *,
    require_complete: bool = False,
) -> bool:
    """Fail closed unless the parser attests literal current counts with typed coverage.

    A positive creative-role count remains a useful exact lower bound when pagination or another
    bounded surface is partial.  Such a row must say so explicitly and can never tombstone a prior
    complete state.  Workforce-scale claims and interpretable zeroes require complete coverage.
    """

    literal_current = bool(
        qualifiers.get("claim_basis") == "literal_public_openings"
        and qualifiers.get("projection_score_gate") is True
        and qualifiers.get("current_state") is True
    )
    if not literal_current:
        return False
    if qualifiers.get("coverage_complete") is True:
        return True
    return bool(
        not require_complete
        and qualifiers.get("coverage_complete") is False
        and qualifiers.get("counts_are_lower_bounds") is True
    )


def _frontier_explicit_age_days(
    atomic: object,
    *,
    observed_at: datetime | None,
) -> float | None:
    effective_basis = str(getattr(atomic, "effective_time_basis", "") or "")
    if effective_basis not in {"source_explicit", "feed_date"}:
        return None
    effective_at = _aware(getattr(atomic, "effective_at", None))
    if observed_at is None or effective_at is None:
        return None
    return (observed_at - effective_at).total_seconds() / 86_400


def _frontier_event_spec(
    atomic: object,
    *,
    observed_at: datetime | None = None,
) -> _FrontierEventSpec | None:
    scanner_id = str(getattr(atomic, "scanner_id", "") or "").strip()
    predicate = str(getattr(atomic, "predicate", "") or "").strip()
    value = getattr(atomic, "value", None)
    qualifiers = _frontier_mapping(getattr(atomic, "qualifiers", {}))
    metadata = _frontier_mapping(getattr(atomic, "metadata", {}))
    count = _frontier_number(value, qualifiers, metadata)

    # Market feeds are intentionally fail-closed at this second boundary.  The projection
    # layer must have admitted the exact predicate and left its explicit gate metadata intact;
    # a parser flag, generic filing, amount, government notice, or attention count is never
    # sufficient.  Both admitted facts are weak activity/timing markers, not ability-to-pay or
    # purchase-intent evidence.
    if scanner_id == "sec_legal_capital_stream_v1" and predicate == "legal.sec.8k_item_disclosure":
        structured_value = _frontier_mapping(value)
        if (
            str(qualifiers.get("score_policy") or "")
            != "weak_timing_signal_max_one_per_causal_root"
            or str(metadata.get("projection_score_gate") or "") != "exact_8k_item"
            or str(structured_value.get("form") or "").upper() != "8-K"
            or not str(structured_value.get("item_number") or "").strip()
        ):
            return None
        return _FrontierEventSpec(
            event_type="market_sec_8k_item_disclosure",
            family="commercial_timing",
            strength=26,
            summary=(
                "An identity-resolved SEC filing reports a specifically enumerated 8-K "
                "item on a source-reported filing date. This is a weak activity marker only; "
                "it does not establish transaction completion, capital raised, available "
                "budget, or purchase intent."
            ),
            half_life_days=30,
        )
    if scanner_id == "open_news_radar_v1" and predicate == "news.launch.show_hn_direct_domain":
        if (
            str(qualifiers.get("score_policy") or "")
            != "weak_timing_signal_max_one_per_causal_root"
            or str(metadata.get("projection_score_gate") or "") != "safe_show_hn_direct_domain"
        ):
            return None
        return _FrontierEventSpec(
            event_type="market_show_hn_direct_domain_launch",
            family="commercial_timing",
            strength=22,
            summary=(
                "A source-timed Show HN submission links directly to the resolved company "
                "domain. This is a weak launch/activity marker only; it does not establish "
                "ownership, traction, revenue, funding, team size, or purchase intent."
            ),
            half_life_days=21,
        )

    if scanner_id == "creative_hiring_demand_v1" and predicate == (
        "hiring.workforce_demand_current"
    ):
        workforce = _frontier_mapping(value)
        current_openings = _frontier_exact_count(workforce, "current_openings")
        distinct_postings = _frontier_exact_count(workforce, "distinct_postings")
        surface_count = _frontier_exact_count(workforce, "surface_count")
        if (
            not _frontier_literal_hiring_gate(qualifiers, require_complete=True)
            or workforce.get("coverage_complete") is not True
            or current_openings is None
            or not 10 <= current_openings <= 5_000
            or distinct_postings != current_openings
            or surface_count is None
            or not 1 <= surface_count <= 8
        ):
            return None
        strength = 58 if current_openings >= 100 else 50 if current_openings >= 30 else 40
        return _FrontierEventSpec(
            event_type="frontier_workforce_hiring_surface",
            family="ability_proxy",
            strength=strength,
            summary=(
                f"A bounded public careers snapshot lists {current_openings} distinct current "
                f"opening(s) across {surface_count} inspected surface(s). This is a literal "
                "public-opening count, not employee count, completed hiring, headcount growth, "
                "urgency, revenue, available budget, outsourcing intent, or purchase intent."
            ),
            half_life_days=14,
        )
    if scanner_id == "creative_hiring_demand_v1" and predicate == (
        "hiring.creative_demand_current"
    ):
        creative = _frontier_mapping(value)
        creative_openings = _frontier_exact_count(creative, "current_creative_openings")
        photo_video_openings = _frontier_exact_count(creative, "current_photo_video_openings")
        creative_share = _frontier_exact_ratio(creative, "creative_share")
        role_families = _frontier_normalized_values(creative.get("role_families"))
        production_language = _frontier_exact_count(creative, "production_language_postings")
        external_capacity_language = _frontier_exact_count(
            creative, "external_capacity_language_postings"
        )
        if (
            not _frontier_literal_hiring_gate(qualifiers)
            or creative_openings is None
            or not 1 <= creative_openings <= 5_000
            or photo_video_openings is None
            or photo_video_openings > creative_openings
            or creative_share is None
            or creative_share <= 0
            or not role_families
            or len(role_families) > 12
            or production_language is None
            or production_language > creative_openings
            or external_capacity_language is None
            or external_capacity_language > creative_openings
        ):
            return None
        strength = 72 if creative_openings >= 5 else 58 if creative_openings >= 2 else 42
        return _FrontierEventSpec(
            event_type="frontier_creative_hiring_surface",
            family="production_pressure",
            strength=strength,
            summary=(
                f"A bounded public careers snapshot lists {creative_openings} distinct current "
                f"creative opening(s), including {photo_video_openings} photo/video-production "
                f"opening(s); {production_language} posting(s) contain literal production "
                f"language and {external_capacity_language} contain literal external-capacity "
                "language. These public listings do not establish completed hiring, workload, "
                "urgency, a staffing gap, available budget, outsourcing intent, or purchase intent."
            ),
            half_life_days=14,
        )
    if scanner_id == "creative_hiring_demand_v1" and predicate == (
        "hiring.creative_velocity_current"
    ):
        velocity = _frontier_mapping(value)
        recent_openings = _frontier_exact_count(velocity, "recent_creative_openings_60d")
        dated_openings = _frontier_exact_count(velocity, "dated_creative_openings")
        window_days = _frontier_exact_count(velocity, "window_days")
        latest_posted_at = _parse_datetime(_frontier_exact_text(velocity, "latest_posted_at"))
        effective_at = _aware(getattr(atomic, "effective_at", None))
        age_days = _frontier_explicit_age_days(atomic, observed_at=observed_at)
        if (
            not _frontier_literal_hiring_gate(qualifiers)
            or recent_openings is None
            or not 1 <= recent_openings <= 5_000
            or dated_openings is None
            or dated_openings < recent_openings
            or dated_openings > 5_000
            or window_days != 60
            or latest_posted_at is None
            or effective_at is None
            or abs((latest_posted_at - effective_at).total_seconds()) > 1
            or age_days is None
            or not 0 <= age_days <= 60
        ):
            return None
        strength = 62 if recent_openings >= 5 else 54 if recent_openings >= 2 else 44
        return _FrontierEventSpec(
            event_type="frontier_creative_hiring_current",
            family="commercial_timing",
            strength=strength,
            summary=(
                f"A bounded public careers snapshot lists {recent_openings} current creative "
                "opening(s) with explicit source dates in the last 60 days. This is a dated "
                "public-listing signal, not evidence of completed hiring, growth, urgency, "
                "workload, available budget, outsourcing intent, or purchase intent."
            ),
            half_life_days=30,
        )

    if scanner_id == "product_release_engine_v1" and predicate == "product.release_engine":
        release_engine = _frontier_mapping(value)
        maturity = _frontier_exact_text(release_engine, "maturity").casefold()
        substantive_180 = _frontier_exact_count(release_engine, "substantive_items_180d")
        recent_90 = _frontier_exact_count(release_engine, "recent_90d")
        active_months = _frontier_exact_count(release_engine, "active_months_180d")
        latest_release = _parse_datetime(_frontier_exact_text(release_engine, "latest_release_at"))
        effective_at = _aware(getattr(atomic, "effective_at", None))
        latest_age_days = _frontier_explicit_age_days(atomic, observed_at=observed_at)
        if (
            not _frontier_score_policy(qualifiers, "established_product_release_engine_v1")
            or maturity != "established"
            or substantive_180 is None
            or substantive_180 < 4
            or substantive_180 > 40
            or recent_90 is None
            or recent_90 < 2
            or recent_90 > substantive_180
            or active_months is None
            or active_months < 2
            or active_months > min(6, substantive_180)
            or latest_release is None
            or effective_at is None
            or abs((latest_release - effective_at).total_seconds()) > 1
            or latest_age_days is None
            or not 0 <= latest_age_days <= 90
        ):
            return None
        return _FrontierEventSpec(
            event_type="frontier_product_release_engine",
            family="recurring_workload",
            strength=44,
            summary=(
                f"A bounded first-party-linked release surface exposes {round(substantive_180)} "
                f"substantive dated product release(s) in 180 days, including "
                f"{round(recent_90)} in 90 days across {round(active_months)} active month(s). "
                "This is public release cadence, not evidence of adoption, launch success, users, "
                "revenue, staffing, available budget, roadmap truth, or purchase intent."
            ),
            half_life_days=90,
        )
    if scanner_id == "product_release_engine_v1" and predicate == "product.release_current":
        release = _frontier_mapping(value)
        title = _frontier_exact_text(release, "title")
        url = _frontier_exact_text(release, "url")
        release_type = _frontier_exact_text(release, "release_type")
        age_days = _frontier_explicit_age_days(atomic, observed_at=observed_at)
        if (
            not _frontier_score_policy(qualifiers, "recent_substantive_release_v1")
            or not title
            or not url
            or release_type.casefold() != "substantive"
            or age_days is None
            or not 0 <= age_days <= 30
        ):
            return None
        return _FrontierEventSpec(
            event_type="frontier_product_release_current",
            family="commercial_timing",
            strength=52,
            summary=(
                "A first-party-linked release surface reports a substantive product release on an "
                "explicit source date within 30 days. This is a current product-activity marker, "
                "not evidence of adoption, launch success, users, revenue, staffing, available "
                "budget, roadmap truth, or purchase intent."
            ),
            half_life_days=30,
        )
    if scanner_id == "event_webinar_engine_v1" and predicate == "events.program_engine":
        program = _frontier_mapping(value)
        dated_events = _frontier_exact_count(program, "dated_events")
        distinct_months = _frontier_exact_count(program, "distinct_months")
        upcoming_events = _frontier_exact_count(program, "upcoming_events")
        recent_events = _frontier_exact_count(program, "recent_events")
        if (
            not _frontier_score_policy(qualifiers, "recurring_event_program_v1")
            or dated_events is None
            or dated_events < 3
            or dated_events > 50
            or distinct_months is None
            or distinct_months < 2
            or distinct_months > min(19, dated_events)
            or upcoming_events is None
            or recent_events is None
            or upcoming_events > dated_events
            or recent_events > dated_events
            or upcoming_events + recent_events > dated_events
        ):
            return None
        return _FrontierEventSpec(
            event_type="frontier_event_program_engine",
            family="recurring_workload",
            strength=54 if dated_events >= 6 else 50,
            summary=(
                f"A bounded first-party-linked event surface exposes {round(dated_events)} "
                f"distinct explicitly dated event(s) across {round(distinct_months)} month(s). "
                "This is recurring public programming, not evidence of attendance, demand, "
                "pipeline, success, production spend, or purchase intent."
            ),
            half_life_days=120,
        )
    if scanner_id == "event_webinar_engine_v1" and predicate == "events.upcoming":
        upcoming = _frontier_mapping(value)
        title = _frontier_exact_text(upcoming, "title")
        url = _frontier_exact_text(upcoming, "url")
        state = _frontier_exact_text(upcoming, "state").casefold()
        start_at = _parse_datetime(_frontier_exact_text(upcoming, "start_at"))
        effective_at = _aware(getattr(atomic, "effective_at", None))
        age_days = _frontier_explicit_age_days(atomic, observed_at=observed_at)
        days_until = -age_days if age_days is not None else None
        if (
            not _frontier_score_policy(qualifiers, "upcoming_confirmed_event_v1")
            or not title
            or not url
            or state != "upcoming"
            or start_at is None
            or effective_at is None
            or abs((start_at - effective_at).total_seconds()) > 1
            or days_until is None
            or not 3 <= days_until <= 45
        ):
            return None
        return _FrontierEventSpec(
            event_type="frontier_event_upcoming",
            family="commercial_timing",
            strength=48,
            summary=(
                "A first-party-linked public event surface lists a non-cancelled event on an "
                "explicit source date three to 45 days away. The listing does not establish "
                "attendance, demand, pipeline, success, production spend, or purchase intent."
            ),
            half_life_days=21,
        )
    if scanner_id == "trust_center_readiness_v1" and predicate == ("enterprise.trust_readiness"):
        readiness = _frontier_mapping(value)
        control_families = _frontier_normalized_values(readiness.get("control_families"))
        family_count = _frontier_exact_count(readiness, "control_family_count")
        if (
            not _frontier_score_policy(qualifiers, "enterprise_trust_surface_v1")
            or readiness.get("explicit_trust_center") is not True
            or len(control_families) < 3
            or not set(control_families) <= _FRONTIER_TRUST_CONTROL_FAMILIES
            or family_count is None
            or family_count < 3
            or family_count > len(_FRONTIER_TRUST_CONTROL_FAMILIES)
            or family_count != len(control_families)
        ):
            return None
        return _FrontierEventSpec(
            event_type="frontier_enterprise_trust_readiness",
            family="ability_proxy",
            strength=46 if len(control_families) >= 5 else 38,
            summary=(
                f"An explicitly linked public trust or assurance center exposes "
                f"{round(family_count)} enterprise procurement/control family/families. This "
                "proves only a public enterprise-readiness surface, not certification validity, "
                "customers, revenue, company size, security quality, available budget, or "
                "purchase intent."
            ),
            half_life_days=180,
        )
    if scanner_id == "growth_measurement_schema_v1" and predicate == (
        "measurement.public_funnel_schema"
    ):
        schema = _frontier_mapping(value)
        explicit_calls = _frontier_exact_count(schema, "explicit_event_calls")
        funnel_families = _frontier_normalized_values(schema.get("funnel_families"))
        if (
            not _frontier_score_policy(qualifiers, "public_growth_schema_v1")
            or explicit_calls is None
            or explicit_calls < 4
            or explicit_calls > 100
            or len(funnel_families) < 2
            or not set(funnel_families) <= _FRONTIER_MEASUREMENT_FAMILIES
            or "conversion" not in funnel_families
            or not {"media", "experiment"} & set(funnel_families)
            or schema.get("has_media_or_experiment") is not True
            or schema.get("has_conversion") is not True
        ):
            return None
        return _FrontierEventSpec(
            event_type="frontier_public_funnel_schema",
            family="distribution_investment",
            strength=40 if explicit_calls >= 8 or len(funnel_families) >= 3 else 34,
            summary=(
                f"Saved first-party HTML exposes {round(explicit_calls)} explicit public "
                f"measurement call(s) across {len(funnel_families)} funnel family/families, "
                "including media/experiment and conversion shapes. This is visible "
                "instrumentation code, not evidence that an event fired, an experiment ran, ads "
                "are active, conversions occurred, performance improved, or money was spent."
            ),
            half_life_days=120,
        )
    if scanner_id == "seo_visibility_engine_v1" and predicate == "seo.page_readiness":
        readiness = _frontier_mapping(value)
        score = _frontier_exact_count(readiness, "readiness_score")
        word_count = _frontier_exact_count(readiness, "visible_word_count")
        internal_links = _frontier_exact_count(readiness, "same_company_link_count")
        title_length = _frontier_exact_count(readiness, "title_length")
        h1_count = _frontier_exact_count(readiness, "h1_count")
        if (
            not _frontier_score_policy(qualifiers, "seo_operating_footprint_v1")
            or score is None
            or not 75 <= score <= 100
            or word_count is None
            or word_count < 250
            or word_count > 100_000
            or internal_links is None
            or not 5 <= internal_links <= 1_000
            or title_length is None
            or not 10 <= title_length <= 70
            or h1_count != 1
            or readiness.get("meta_allows_indexing") is not True
            or readiness.get("title_present") is not True
            or readiness.get("canonical_present") is not True
            or readiness.get("canonical_same_company") is not True
            or readiness.get("qualifies_as_ability_proxy") is not True
            or readiness.get("sample_truncated") is not False
            or qualifiers.get("rank_measured") is not False
            or qualifiers.get("traffic_measured") is not False
        ):
            return None
        strength = 32 if score >= 90 else 28 if score >= 82 else 24
        return _FrontierEventSpec(
            event_type="frontier_seo_operating_footprint",
            family="ability_proxy",
            strength=strength,
            summary=(
                f"A bounded first-party page has {score}/100 static search-readiness structure, "
                f"{word_count} visible words, and {internal_links} same-company links. This is "
                "a weak operating-maturity proxy, not a measured search ranking, traffic, "
                "demand, revenue, available budget, or purchase intent."
            ),
            half_life_days=180,
        )
    if scanner_id == "dataforseo_search_visibility_v1" and predicate in {
        "seo.search_visibility_estimate",
        "seo.domain_rank_overview",
    }:
        visibility = _frontier_mapping(value)
        organic = _frontier_mapping(visibility.get("organic"))
        paid = _frontier_mapping(visibility.get("paid"))
        target = _frontier_exact_text(visibility, "target").lower().removeprefix("www.")
        organic_count = _frontier_exact_count(organic, "count")
        organic_etv = _frontier_exact_nonnegative_number(organic, "etv")
        paid_count = _frontier_exact_count(paid, "count")
        paid_etv = _frontier_exact_nonnegative_number(paid, "etv")
        detail = predicate == "seo.domain_rank_overview"
        if (
            str(qualifiers.get("score_policy") or "") != "dataforseo_search_visibility_capacity_v1"
            or str(metadata.get("projection_score_gate") or "")
            != "exact_dataforseo_search_visibility"
            or visibility.get("provider_estimate") is not True
            or visibility.get("data_available", True) is not True
            or qualifiers.get("provider_estimate") is not True
            or qualifiers.get("traffic_is_estimated") is not True
            or qualifiers.get("first_party_analytics") is not False
            or qualifiers.get("coverage_complete") is not True
            or qualifiers.get("clickstream_included") is not False
            or qualifiers.get("qualifies_as_ability_proxy") is not True
            or qualifiers.get("location_code") != 2840
            or qualifiers.get("language_code") != "en"
            or qualifiers.get("rank_measured") is not detail
            or visibility.get("location_code") != 2840
            or visibility.get("language_code") != "en"
            or not target
            or target != str(getattr(atomic, "company_key", "")).lower().removeprefix("www.")
            or organic_count is None
            or organic_etv is None
            or paid_count is None
            or paid_etv is None
        ):
            return None
        if detail:
            positions = [
                _frontier_exact_count(organic, field)
                for field in (
                    "pos_1",
                    "pos_2_3",
                    "pos_4_10",
                    "pos_11_20",
                    "pos_21_30",
                    "pos_31_40",
                    "pos_41_50",
                    "pos_51_60",
                    "pos_61_70",
                    "pos_71_80",
                    "pos_81_90",
                    "pos_91_100",
                )
            ]
            if any(item is None for item in positions):
                return None
            ranked_total = sum(int(item or 0) for item in positions)
            top_ten = sum(int(item or 0) for item in positions[:3])
            traffic_cost = _frontier_exact_nonnegative_number(
                organic,
                "estimated_paid_traffic_cost",
            )
            if (
                ranked_total > organic_count
                or traffic_cost is None
                or organic_count < 25
                or (top_ten < 1 and organic_etv < 100)
            ):
                return None
            strength = (
                52
                if top_ten >= 100 or organic_count >= 10_000
                else 46
                if top_ten >= 20 or organic_count >= 1_000
                else 38
                if top_ten >= 5 or organic_count >= 100
                else 30
            )
            summary = (
                f"DataForSEO's US-English Google dataset estimates {organic_count} organic "
                f"ranking result(s), including {top_ten} in positions 1-10, and modeled "
                f"monthly organic traffic volume of {round(organic_etv, 1)}. This is a "
                "medium-capped search operating-capacity proxy, not first-party analytics, "
                "creative demand, revenue, available budget, or purchase intent."
            )
        else:
            if organic_count < 25 and organic_etv < 100 and paid_count < 1:
                return None
            strength = (
                46
                if organic_count >= 10_000 or organic_etv >= 100_000
                else 40
                if organic_count >= 1_000 or organic_etv >= 10_000
                else 34
                if organic_count >= 100 or organic_etv >= 1_000
                else 28
            )
            summary = (
                f"DataForSEO's US-English Google dataset estimates {organic_count} organic "
                f"ranking result(s) and modeled monthly organic traffic volume of "
                f"{round(organic_etv, 1)}. This is a medium-capped search operating-capacity "
                "proxy, not first-party analytics, creative demand, revenue, available "
                "budget, or purchase intent."
            )
        return _FrontierEventSpec(
            event_type=(
                "vendor_dataforseo_domain_rank_overview"
                if detail
                else "vendor_dataforseo_search_visibility_estimate"
            ),
            family="ability_proxy",
            strength=strength,
            summary=summary,
            half_life_days=30,
        )

    if scanner_id == "creative_asset_genome_v1" and predicate == (
        "creative.asset_concept_family_count"
    ):
        if count is None or count < 1:
            return None
        strength = 42 if count >= 10 else 34 if count >= 4 else 24
        return _FrontierEventSpec(
            event_type="frontier_creative_asset_surface",
            family="proof_match",
            strength=min(34, strength),
            summary=(
                f"The bounded public first-party sample exposes {round(count)} media URL "
                "groups. These are a static creative footprint, not semantic concepts, "
                "recurring output, spend, performance, or a staffing gap."
            ),
            half_life_days=180,
        )
    if scanner_id == "lifecycle_email_exhaust_v1" and predicate == (
        "lifecycle.public_campaign_count"
    ):
        if count is None or count < 1:
            return None
        strength = 48 if count >= 6 else 38 if count >= 3 else 26
        return _FrontierEventSpec(
            event_type="frontier_public_campaign_cadence",
            family="proof_match",
            strength=min(34, strength),
            summary=(
                f"A first-party-linked public campaign archive exposes {round(count)} "
                "campaign page(s); it does not establish send volume, audience, automation, "
                "performance, or spend."
            ),
            half_life_days=120,
        )
    if scanner_id == "app_release_pulse_v1" and predicate == "app.store_release":
        return _FrontierEventSpec(
            event_type="frontier_app_store_release",
            family="commercial_timing",
            strength=50,
            summary=(
                "An exact first-party domain association and public app-store record report "
                "a dated release or update; this does not establish installs, users, revenue, "
                "traction, or team size."
            ),
            half_life_days=30,
        )
    if scanner_id == "localization_burden_v1" and predicate == (
        "localization.declared_variant_count"
    ):
        if count is None or count < 2:
            return None
        strength = 46 if count >= 8 else 36 if count >= 4 else 28
        return _FrontierEventSpec(
            event_type="frontier_localization_surface",
            family="proof_match",
            strength=min(34, strength),
            summary=(
                f"The bounded first-party sample declares {round(count)} language or region "
                "variant(s); this suggests localization surface area, not regional revenue, "
                "a market launch, or translation headcount."
            ),
            half_life_days=365,
        )
    if scanner_id == "creator_program_contract_v1" and predicate == "creator.program":
        program = _frontier_mapping(value)
        formats = program.get("content_formats", ())
        cadence = program.get("cadence_requirements", ())
        if not formats and not cadence:
            return None
        return _FrontierEventSpec(
            event_type="frontier_creator_content_requirement",
            family="recurring_workload" if cadence else "proof_match",
            strength=46 if cadence else 30,
            summary=(
                "A first-party public creator or affiliate program states content formats "
                "or cadence requirements; this does not establish creator count, payment, "
                "participation, campaign performance, or spend."
            ),
            half_life_days=180,
        )
    if scanner_id == "dol_lca_workforce_v1" and predicate == (
        "workforce.lca_certified_position_count"
    ):
        if count is None or count < 1:
            return None
        strength = 58 if count >= 20 else 46 if count >= 5 else 34
        return _FrontierEventSpec(
            event_type="frontier_specialty_workforce_disclosure",
            family="ability_proxy",
            strength=strength,
            summary=(
                f"Identity-resolved official DOL disclosure records report {round(count)} "
                "certified specialty-worker position(s); this is not employee count, actual "
                "hiring, salary truth, current budget, or willingness to buy."
            ),
            half_life_days=365,
        )
    if scanner_id == "sec_form_d_capital_v1" and predicate == "capital.form_d_filing":
        return _FrontierEventSpec(
            event_type="frontier_form_d_filing",
            family="commercial_timing",
            strength=52,
            summary=(
                "An identity-resolved SEC Form D notice reports a dated issuer filing; it "
                "does not establish proceeds beyond literal amount-sold fields, valuation, "
                "cash, runway, profitability, budget, or willingness to buy."
            ),
            half_life_days=90,
        )
    if scanner_id == "ai_artifact_provenance_v1" and predicate == "ai.product_role":
        product_role = _frontier_text(value, qualifiers)
        if product_role not in {"core_product", "meaningful_feature"}:
            return None
        cited_spans = qualifiers.get("cited_spans", ())
        if not isinstance(cited_spans, Sequence) or isinstance(cited_spans, str):
            return None
        explicit_ai_copy = any(
            re.search(
                r"\b(?:ai|artificial intelligence|machine learning|generative|agentic)\b",
                str(span),
                re.I,
            )
            for span in cited_spans
        )
        if not explicit_ai_copy:
            return None
        strength = 52 if product_role == "core_product" else 38
        return _FrontierEventSpec(
            event_type="frontier_inferred_ai_product_role",
            family="proof_match",
            strength=strength,
            summary=(
                "Exact-domain or first-party evidence describes AI as a core product or "
                "meaningful feature; this is a fit signal, not evidence of traction, company "
                "size, revenue, budget, or purchase intent."
            ),
            half_life_days=180,
        )
    if scanner_id == "owned_media_pulse_v1" and predicate == "media.publishing_pulse":
        if count is None or count < 1:
            return None
        strength = 48 if count >= 8 else 38 if count >= 3 else 26
        return _FrontierEventSpec(
            event_type="frontier_owned_media_cadence",
            family="recurring_workload",
            strength=strength,
            summary=(
                f"An officially linked bounded public feed exposes {round(count)} recent "
                "media item(s); this does not establish total output, audience, views, "
                "production spend, or conversion."
            ),
            half_life_days=90,
        )
    if scanner_id == "corporate_blog_engine_v1" and predicate == "content.blog_engine":
        engine = _frontier_mapping(value)
        maturity = str(engine.get("maturity") or "").strip().casefold()
        recent_90 = _frontier_number(engine.get("recent_90d"))
        active_months = _frontier_number(engine.get("active_months_180d"))
        dated_items = _frontier_number(engine.get("dated_editorial_items"))
        editorial_items = _frontier_number(engine.get("editorial_items"))
        if (
            str(qualifiers.get("score_policy") or "") != "established_editorial_engine_v1"
            or maturity not in {"established", "robust"}
            or recent_90 is None
            or recent_90 < 1
            or active_months is None
            or active_months < 2
            or dated_items is None
            or dated_items < 3
            or editorial_items is None
            or editorial_items < 4
        ):
            return None
        strength = 54 if maturity == "robust" else 42
        raw_topics = engine.get("top_topics", ())
        topics = (
            [str(item).replace("_", " ") for item in raw_topics[:3]]
            if isinstance(raw_topics, Sequence) and not isinstance(raw_topics, str)
            else []
        )
        topic_clause = f" Themes include {', '.join(topics)}." if topics else ""
        return _FrontierEventSpec(
            event_type="frontier_corporate_blog_engine",
            family="recurring_workload",
            strength=strength,
            summary=(
                f"A bounded first-party-linked sample exposes an {maturity} corporate "
                f"editorial engine with {round(recent_90)} dated editorial item(s) in "
                f"90 days across {round(active_months)} active month(s) in 180 days."
                f"{topic_clause} "
                "This demonstrates recurring public content operations, not spend, traffic, "
                "audience, performance, team size, available budget, or purchase intent."
            ),
            half_life_days=90,
        )
    # Brand/design surfaces and the SF/AI/capital constellation are deliberately context-only.
    # Record-level and relationship predicates also remain in the normalized ledger without
    # becoming points, even if a malformed producer marks them score eligible.
    return None


def _frontier_coverage(frontier: object, atomic: object) -> str:
    from .frontier_lineage import ReceiptIndex

    scanner_id = str(getattr(atomic, "scanner_id", "") or "")
    manifests = ReceiptIndex(
        [item for item in frontier.coverage_manifests if item.scanner_id == scanner_id],
        "manifest_key",
    )
    keys = tuple(str(value) for value in getattr(atomic, "coverage_keys", ()) if value)
    if not keys:
        return "unknown"
    states: list[str] = []
    for key in keys:
        manifest = manifests.get(atomic, key)
        if manifest is None:
            states.append("unknown")
            continue
        status = str(
            getattr(manifest, "status", "") or getattr(manifest, "outcome", "") or ""
        ).lower()
        completeness = str(getattr(manifest, "completeness", "") or "").lower()
        failed_units = int(getattr(manifest, "failed_units", 0) or 0)
        successful_units = int(getattr(manifest, "successful_units", 0) or 0)
        truncated = bool(getattr(manifest, "truncated", False))
        if status in {"failed", "failed_transient", "failed_permanent"} or (
            failed_units and not successful_units
        ):
            states.append("failed")
        elif truncated or failed_units:
            states.append("partial")
        elif completeness in {"complete", "exhaustive", "bounded_complete"}:
            states.append("complete")
        elif completeness in {"partial", "sampled", "truncated"}:
            states.append("partial")
        else:
            states.append("unknown")
    if states and set(states) == {"complete"}:
        return "complete"
    if states and set(states) == {"failed"}:
        return "failed"
    if any(state in {"complete", "partial"} for state in states):
        return "partial"
    return "unknown"


def _collect_frontier_events(
    observation: SiteObservation,
    *,
    subject_key: str,
    accounting: dict | None = None,
) -> list[BuyingSignalEvent]:
    frontier = getattr(observation.web, "frontier", None)
    if frontier is None:
        return []
    accounting = accounting if accounting is not None else {}
    for atomic in frontier.observations:
        accounting[atomic.observation_key] = "superseded_by_current_surface"
    from .frontier_lineage import ReceiptIndex

    artifact_by_key = ReceiptIndex(frontier.evidence_artifacts, "artifact_key")
    coverage_by_key = ReceiptIndex(frontier.coverage_manifests, "manifest_key")

    def execution_time(execution: object) -> datetime:
        return _aware(
            getattr(execution, "finished_at", None) or getattr(execution, "started_at", None)
        ) or datetime.min.replace(tzinfo=UTC)

    empty_terminal_by_scanner: dict[tuple[str, str], datetime] = {}
    for execution in getattr(frontier, "executions", ()):
        status = str(getattr(execution, "status", "") or "").casefold()
        if status not in {"success_empty", "not_applicable"}:
            continue
        scanner_id = str(getattr(execution, "scanner_id", "") or "").strip()
        company_key = str(getattr(execution, "company_key", "") or "").strip().lower()
        coverage_keys = tuple(
            str(value) for value in getattr(execution, "coverage_keys", ()) if value
        )
        manifests = [manifest for key in coverage_keys
                     if (manifest := coverage_by_key.get(execution, key)) is not None]
        if not manifests or len(manifests) != len(coverage_keys):
            continue
        if any(
            not bool(getattr(manifest, "absence_interpretable", False))
            or bool(getattr(manifest, "truncated", False))
            or int(getattr(manifest, "failed_units", 0) or 0)
            or str(getattr(manifest, "completeness", "") or "").casefold()
            not in {"complete", "not_applicable"}
            for manifest in manifests
        ):
            continue
        key = scanner_id, company_key
        finished = execution_time(execution)
        empty_terminal_by_scanner[key] = max(
            empty_terminal_by_scanner.get(key, datetime.min.replace(tzinfo=UTC)),
            finished,
        )
    stable_groups: dict[tuple[str, str, str, str], list[object]] = {}
    historical: list[object] = []
    for atomic in getattr(frontier, "observations", ()):
        scanner_id = str(getattr(atomic, "scanner_id", "") or "").strip()
        effective = _aware(getattr(atomic, "effective_at", None))
        expiry = _aware(getattr(atomic, "expires_at", None))
        if effective is not None and expiry is not None and expiry < effective:
            accounting[atomic.observation_key] = "invalid_time_range"
            continue
        predicate = str(getattr(atomic, "predicate", "") or "").strip()
        if predicate not in _FRONTIER_STABLE_SURFACE_PREDICATES:
            historical.append(atomic)
            continue
        stable_key = (
            scanner_id,
            str(getattr(atomic, "company_key", "") or "").strip().lower(),
            str(getattr(atomic, "entity_key", "") or "").strip(),
            predicate,
        )
        stable_groups.setdefault(stable_key, []).append(atomic)

    def observed_time(atomic: object) -> datetime:
        return _aware(
            getattr(atomic, "last_observed_at", None) or getattr(atomic, "first_observed_at", None)
        ) or datetime.min.replace(tzinfo=UTC)

    def exact_lineage(atomic: object) -> bool:
        scanner_id = str(getattr(atomic, "scanner_id", "") or "").strip()
        company_key = str(getattr(atomic, "company_key", "") or "").lower().removeprefix("www.")
        source_url = _canonical_url(str(getattr(atomic, "source_url", "") or ""))
        artifact_keys = tuple(str(value) for value in getattr(atomic, "artifact_keys", ()) if value)
        return bool(
            source_url
            and any(
                str(getattr(artifact, "scanner_id", "")) == scanner_id
                and str(getattr(artifact, "company_key", "")).lower().removeprefix("www.")
                == company_key
                and str(getattr(artifact, "content_hash", "") or "")
                and _canonical_url(str(getattr(artifact, "canonical_url", "") or "")) == source_url
                for key in artifact_keys
                if (artifact := artifact_by_key.get(atomic, key)) is not None
            )
        )

    latest_stable: list[object] = []
    for group in stable_groups.values():
        latest_time = max(observed_time(item) for item in group)
        group_scanner = str(getattr(group[0], "scanner_id", "") or "").strip()
        group_company = str(getattr(group[0], "company_key", "") or "").strip().lower()
        if (
            empty_terminal_by_scanner.get(
                (group_scanner, group_company),
                datetime.min.replace(tzinfo=UTC),
            )
            > latest_time
        ):
            continue
        if group_scanner == "creative_hiring_demand_v1":
            # A partial crawl can prove a positive lower bound, but it cannot prove that roles
            # disappeared. Keep the strongest literal count at or after the newest complete
            # snapshot. A newer complete zero therefore retires older positives, while a later
            # partial positive can safely raise that lower bound again.
            addressed = [item for item in group if exact_lineage(item)] or group

            def hiring_count(item: object) -> int:
                item_value = _frontier_mapping(getattr(item, "value", None))
                item_predicate = str(getattr(item, "predicate", "") or "")
                field = {
                    "hiring.workforce_demand_current": "current_openings",
                    "hiring.creative_demand_current": "current_creative_openings",
                    "hiring.creative_velocity_current": "recent_creative_openings_60d",
                }.get(item_predicate, "")
                return _frontier_exact_count(item_value, field) or 0

            addressed = [
                item
                for item in addressed
                if (
                    (item_qualifiers := _frontier_mapping(getattr(item, "qualifiers", {}))).get(
                        "claim_basis"
                    )
                    == "literal_public_openings"
                    and item_qualifiers.get("current_state") is True
                    and (
                        str(getattr(item, "predicate", "") or "")
                        != "hiring.workforce_demand_current"
                        or item_qualifiers.get("coverage_complete") is True
                    )
                    and (
                        item_qualifiers.get("coverage_complete") is True
                        or (
                            item_qualifiers.get("coverage_complete") is False
                            and item_qualifiers.get("counts_are_lower_bounds") is True
                            and hiring_count(item) > 0
                        )
                    )
                )
            ]
            if not addressed:
                continue
            complete_rows = [
                item
                for item in addressed
                if _frontier_mapping(getattr(item, "qualifiers", {})).get("coverage_complete")
                is True
                and _frontier_coverage(frontier, item) == "complete"
            ]
            complete_cutoff = max(
                (observed_time(item) for item in complete_rows),
                default=datetime.min.replace(tzinfo=UTC),
            )
            pool = [item for item in addressed if observed_time(item) >= complete_cutoff]

            latest_stable.append(
                max(
                    pool,
                    key=lambda item: (
                        hiring_count(item),
                        observed_time(item),
                        bool(getattr(item, "score_eligible", False)),
                        str(getattr(item, "observation_key", "") or ""),
                    ),
                )
            )
            continue
        cohort = [item for item in group if observed_time(item) == latest_time]
        lineage_valid = [item for item in cohort if exact_lineage(item)]
        pool = lineage_valid or cohort
        latest_stable.append(
            max(
                pool,
                key=lambda item: (
                    bool(getattr(item, "score_eligible", False))
                    and not bool(getattr(item, "discovery_only", False))
                    and str(getattr(item, "validity_status", "") or "").lower()
                    not in _INVALID_FRONTIER_VALIDITY,
                    str(getattr(item, "observation_key", "") or ""),
                ),
            )
        )
    candidates: list[BuyingSignalEvent] = []
    for atomic in [*historical, *latest_stable]:
        key = atomic.observation_key
        if not bool(getattr(atomic, "score_eligible", False)):
            accounting[key] = "parser_not_score_eligible"
            continue
        if bool(getattr(atomic, "discovery_only", False)):
            accounting[key] = "discovery_identity_only"
            continue
        scanner_id = str(getattr(atomic, "scanner_id", "") or "").strip()
        if scanner_id not in _REVENUE_EVIDENCE_SCANNER_IDS:
            accounting[key] = "unmapped_scanner"
            continue
        atomic_company = str(getattr(atomic, "company_key", "") or "").strip().lower()
        if atomic_company.removeprefix("www.") != subject_key.lower().removeprefix("www."):
            accounting[key] = "wrong_company"
            continue
        validity = str(getattr(atomic, "validity_status", "") or "").lower()
        if validity in _INVALID_FRONTIER_VALIDITY:
            accounting[key] = "invalidity:" + validity
            continue
        epistemic_status = str(getattr(atomic, "epistemic_status", "") or "unknown").lower()
        if epistemic_status == "derived":
            epistemic_status = "inferred"
        if epistemic_status not in {"observed", "inferred"}:
            accounting[key] = "unknown_truth_state"
            continue
        predicate = str(getattr(atomic, "predicate", "") or "").strip()
        if scanner_id == "ai_artifact_provenance_v1" and predicate == "ai.product_role":
            # A deterministic label over first-party copy is still a classification. It may
            # support a low-confidence fit hypothesis, never an observed fact about the company.
            epistemic_status = "inferred"
        source_url = str(getattr(atomic, "source_url", "") or "").strip()
        observed_at = _aware(
            getattr(atomic, "last_observed_at", None) or getattr(atomic, "first_observed_at", None)
        )
        source_actor = str(getattr(atomic, "source_actor_key", "") or "").strip()
        origin = str(getattr(atomic, "origin_fingerprint", "") or "").strip()
        causal_root = str(getattr(atomic, "causal_root_key", "") or "").strip()
        observation_key = str(getattr(atomic, "observation_key", "") or "").strip()
        scanner_version = str(getattr(atomic, "scanner_version", "") or "").strip()
        extractor_version = str(getattr(atomic, "extractor_version", "") or "").strip()
        rule_id = str(getattr(atomic, "rule_id", "") or "").strip()
        rule_version = str(getattr(atomic, "rule_version", "") or "").strip()
        entity_key = str(getattr(atomic, "entity_key", "") or "").strip()
        artifact_keys = tuple(str(value) for value in getattr(atomic, "artifact_keys", ()) if value)
        artifacts = [artifact for key in artifact_keys
                     if (artifact := artifact_by_key.get(atomic, key)) is not None]
        matching_artifacts = [
            item
            for item in artifacts
            if str(getattr(item, "scanner_id", "")) == scanner_id
            and str(getattr(item, "scanner_version", "") or "") == scanner_version
            and (
                not extractor_version
                or str(getattr(item, "extractor_version", "") or "") == extractor_version
            )
            and str(getattr(item, "company_key", "")).lower().removeprefix("www.")
            == subject_key.lower().removeprefix("www.")
            and str(getattr(item, "content_hash", "") or "")
            and _canonical_url(str(getattr(item, "canonical_url", "") or ""))
            == _canonical_url(source_url)
        ]
        if not matching_artifacts:
            accounting[key] = "missing_exact_artifact_lineage"
            # The cited source itself must be content-addressed. An unrelated artifact from the
            # same scanner/company cannot launder an unsupported URL into revenue scoring.
            continue
        required = (
            source_url,
            source_actor,
            origin,
            causal_root,
            observation_key,
            scanner_version,
            extractor_version,
            rule_id,
            rule_version,
            entity_key,
        )
        if observed_at is None or not all(required):
            accounting[key] = "incomplete_provenance"
            continue
        spec = _frontier_event_spec(atomic, observed_at=observed_at)
        if spec is None:
            accounting[key] = "predicate_rule_not_satisfied"
            continue
        effective_basis = str(getattr(atomic, "effective_time_basis", "") or "").lower()
        effective_at = _aware(getattr(atomic, "effective_at", None))
        if effective_basis in _UNSAFE_FRONTIER_TIME_BASES:
            effective_at = None
        if (
            scanner_id == "dol_lca_workforce_v1"
            and predicate == "workforce.lca_certified_position_count"
            and effective_at is None
        ):
            # LCA datasets span historical quarters. First seeing an undated aggregate today
            # cannot turn old disclosure rows into a current ability signal.
            accounting[key] = "undated_workforce_disclosure"
            continue
        if spec.family == "commercial_timing" and effective_at is None:
            # Undated material can be useful context but can never become a why-now event.
            accounting[key] = "undated_timing"
            continue
        confidence = str(getattr(atomic, "confidence", "") or "low").lower()
        if confidence not in _CONFIDENCE_WEIGHT:
            confidence = "low"
        if epistemic_status == "inferred":
            confidence = "low"
        if scanner_id == "growth_measurement_schema_v1" and confidence == "high":
            # Static call shapes are instrumentation context, not runtime observations. Even a
            # deterministic parse cannot make this stronger than medium-confidence distribution.
            confidence = "medium"
        if (
            scanner_id
            in {
                "seo_visibility_engine_v1",
                "dataforseo_search_visibility_v1",
            }
            and confidence == "high"
        ):
            # Static page structure is only a weak operating proxy. It never becomes a
            # high-confidence rank, traffic, budget, or buyer-intent observation.
            confidence = "medium"
        coverage = _frontier_coverage(frontier, atomic)
        if coverage in {"unknown", "failed"}:
            # Missing/failed manifests remain useful in the atomic ledger, but cannot silently
            # become score-bearing evidence. A partial bounded sample may support a conditional
            # positive fact; an unaccounted surface cannot.
            accounting[key] = "coverage:" + coverage
            continue
        independence_root = causal_root
        if scanner_id == "creative_asset_genome_v1":
            # Page copies and image renditions are one company footprint, never corroboration.
            independence_root = f"surface:creative-footprint:{subject_key}"
        elif scanner_id in {"app_release_pulse_v1", "product_release_engine_v1"} and predicate in {
            "app.store_release",
            "product.release_engine",
            "product.release_current",
        }:
            # Store records, changelog cadence, and a current release can all describe one product
            # release operation. A second parser must replace/corroborate that surface, not create
            # an independent reason.
            independence_root = f"surface:product-release:{subject_key}"
        elif scanner_id in {
            "owned_media_pulse_v1",
            "corporate_blog_engine_v1",
            "event_webinar_engine_v1",
        } and predicate in {
            "media.publishing_pulse",
            "content.blog_engine",
            "events.program_engine",
        }:
            # Editorial feeds, owned-media feeds, and recurring event programming can be different
            # renderings of one owned programming operation. Keep only the strongest reason.
            independence_root = f"surface:owned-programming:{subject_key}"
        elif scanner_id == "growth_measurement_schema_v1" and predicate == (
            "measurement.public_funnel_schema"
        ):
            independence_root = f"distribution:first_party:{subject_key}"
        elif scanner_id == "seo_visibility_engine_v1" and predicate == ("seo.page_readiness"):
            independence_root = f"seo-operating-footprint:{subject_key}"
        elif scanner_id == "dataforseo_search_visibility_v1" and predicate in {
            "seo.search_visibility_estimate",
            "seo.domain_rank_overview",
        }:
            independence_root = f"seo-search-visibility:{subject_key}"
        elif scanner_id == "trust_center_readiness_v1" and predicate == (
            "enterprise.trust_readiness"
        ):
            independence_root = f"surface:enterprise-readiness:{subject_key}"
        elif scanner_id == "creative_hiring_demand_v1":
            independence_root = {
                "hiring.workforce_demand_current": "ability:hiring_scale",
                "hiring.creative_demand_current": "production:hiring",
                "hiring.creative_velocity_current": "timing:creative_hiring",
            }.get(predicate, causal_root)
        elif scanner_id == "event_webinar_engine_v1" and predicate == "events.upcoming":
            # Upcoming timing must retain the producer's stable event UID/URL root; a company-wide
            # root would collapse unrelated future events, while an arbitrary root could inflate
            # corroboration across repeated renderings.
            if not causal_root.startswith("event:"):
                accounting[key] = "invalid_event_identity"
                continue
            independence_root = causal_root
        elif predicate in _FRONTIER_STABLE_SURFACE_PREDICATES:
            # Aggregate counts change value across scans but remain one measurement surface.
            # Historical counts therefore replace/corroborate that surface rather than becoming
            # independent reasons to believe the same thing.
            independence_root = f"surface:{scanner_id}:{entity_key}:{predicate}"
        expires_at = _aware(getattr(atomic, "expires_at", None))
        hard_expiry: datetime | None = None
        if scanner_id == "product_release_engine_v1" and effective_at is not None:
            if predicate == "product.release_current":
                hard_expiry = effective_at + timedelta(days=30)
            elif predicate == "product.release_engine":
                hard_expiry = effective_at + timedelta(days=90)
        elif (
            scanner_id == "event_webinar_engine_v1"
            and predicate == "events.upcoming"
            and effective_at is not None
        ):
            hard_expiry = effective_at + timedelta(days=1)
        elif scanner_id == "event_webinar_engine_v1" and predicate == "events.program_engine":
            hard_expiry = observed_at + timedelta(days=120)
        elif (
            scanner_id == "trust_center_readiness_v1" and predicate == "enterprise.trust_readiness"
        ):
            hard_expiry = observed_at + timedelta(days=90)
        elif scanner_id == "creative_hiring_demand_v1":
            if predicate == "hiring.creative_velocity_current" and effective_at is not None:
                hard_expiry = effective_at + timedelta(days=60)
            elif predicate in {
                "hiring.workforce_demand_current",
                "hiring.creative_demand_current",
            }:
                hard_expiry = observed_at + timedelta(days=14)
        if hard_expiry is not None:
            expires_at = min(expires_at, hard_expiry) if expires_at else hard_expiry
        if effective_at is not None and expires_at is not None and expires_at < effective_at:
            # Preserve the original atomic row. An impossible derived interval is coverage,
            # never a reason to shift dates, award points, or abort company/batch scoring.
            accounting[key] = "invalid_time_range"
            continue
        event = _event(
            subject_key=subject_key,
            event_type=spec.event_type,
            family=spec.family,
            polarity="positive",
            strength=spec.strength,
            confidence=confidence,
            coverage=coverage,
            source_kind=f"frontier:{scanner_id}",
            source_url=source_url,
            observed_at=observed_at,
            effective_at=effective_at,
            expires_at=expires_at,
            identity=(
                f"{observation_key}|actor:{source_actor}|origin:{origin}|causal:{causal_root}"
            ),
            summary=spec.summary,
            half_life_days=spec.half_life_days,
            independence_key=(
                independence_root
                if scanner_id == "creative_hiring_demand_v1"
                else f"causal:{independence_root}"
            ),
            epistemic_status=epistemic_status,
            details={
                "scanner_id": scanner_id,
                "scanner_version": scanner_version,
                "extractor_version": extractor_version,
                "rule_id": rule_id,
                "rule_version": rule_version,
                "source_actor_key": source_actor,
                "origin_fingerprint": origin,
                "causal_root_key": causal_root,
                "observation_key": observation_key,
                "predicate": predicate,
                "artifact_keys": ", ".join(artifact_keys),
                "score_eligible": "true",
                "discovery_only": "false",
                "effective_time_basis": effective_basis,
            },
            scanner_id=scanner_id,
            scanner_version=scanner_version,
            extractor_version=extractor_version,
            rule_id=rule_id,
            rule_version=rule_version,
            fallback_effective_to_observed=False,
        )
        candidates.append(event)
        accounting[key] = "eligible"

    # Keep every accepted observation's lineage. Clustering already reduces each root
    # to its strongest contribution; dropping rows here makes accounting incomplete.
    return sorted(candidates, key=lambda item: (item.family, item.event_key))


_DISCOVERY_ASSERTION_RELATION_LABELS = {
    "customer": "customer",
    "testimonial": "testimonial",
    "case_study": "case study",
    "partner": "partner",
    "award": "award",
    "member": "member",
    "sponsor": "sponsor",
    "exhibitor": "exhibitor",
    "portfolio": "portfolio",
}
_DISCOVERY_ASSERTION_SCANNER_ID = "source_discovery_assertion_v1"
_DISCOVERY_ASSERTION_SCANNER_VERSION = "1.0.0"
_DISCOVERY_ASSERTION_EXTRACTOR_VERSIONS = {
    "anchor": "anchor_context_v1",
    "linked_logo": "linked_logo_context_v1",
    "logo_label": "logo_label_context_v1",
    "logo_filename": "logo_filename_context_v1",
    "internal_record": "internal_record_context_v1",
}
_DISCOVERY_ASSERTION_RULE_ID = "source_edge.appeared_on_publisher_page"
_DISCOVERY_ASSERTION_RULE_VERSION = "1.0.0"


def _collect_discovery_assertion_events(
    candidate: DiscoveredHost,
    *,
    subject_key: str,
    observed_at: datetime,
) -> list[BuyingSignalEvent]:
    """Project source-edge context without turning an appearance into a relationship claim."""

    events: list[BuyingSignalEvent] = []
    for assertion in candidate.discovery_assertions:
        publisher = assertion.publisher_domain.strip().lower().removeprefix("www.")
        relation = str(assertion.relation_type).strip().lower()
        relation_label = _DISCOVERY_ASSERTION_RELATION_LABELS.get(relation)
        source_url = assertion.source_url.strip()
        context_hash = assertion.context_hash.strip().lower()
        try:
            source_host = (urlsplit(source_url).hostname or "").lower().removeprefix("www.")
        except ValueError:
            source_host = ""
        if (
            not publisher
            or not relation_label
            or not source_url.startswith(("http://", "https://"))
            or not source_host
            or not (source_host == publisher or source_host.endswith(f".{publisher}"))
            or not re.fullmatch(r"[0-9a-f]{64}", context_hash)
        ):
            continue
        confidence = str(assertion.confidence).lower()
        if confidence not in _CONFIDENCE_WEIGHT:
            confidence = "low"
        evidence_kind = str(assertion.evidence_kind).strip().lower()
        extractor_version = _DISCOVERY_ASSERTION_EXTRACTOR_VERSIONS.get(
            evidence_kind,
            _DISCOVERY_ASSERTION_EXTRACTOR_VERSIONS["anchor"],
        )
        published_at = _aware(assertion.published_at)
        strength = {
            "case_study": 38,
            "testimonial": 36,
            "portfolio": 34,
            "award": 32,
            "partner": 30,
            "customer": 30,
            "member": 26,
            "sponsor": 26,
            "exhibitor": 24,
        }[relation]
        summary = (
            f"{subject_key} appeared on {publisher}'s public {relation_label} page; this "
            "does not establish a current customer, partner, or other active relationship."
        )
        events.append(
            _event(
                subject_key=subject_key,
                event_type="publisher_page_appearance",
                family="proof_match",
                polarity="positive",
                strength=strength,
                confidence=confidence,  # type: ignore[arg-type]
                coverage="partial",
                source_kind="source_discovery_assertion",
                source_url=source_url,
                observed_at=observed_at,
                effective_at=published_at,
                identity=(f"publisher:{publisher}|relation:{relation}|context:{context_hash}"),
                summary=summary,
                half_life_days=180,
                independence_key=f"publisher-context:{publisher}:{context_hash}",
                details={
                    "publisher_domain": publisher,
                    "relation_type": relation,
                    "context_hash": context_hash,
                    "current_relationship_unverified": "true",
                    "evidence_kind": evidence_kind,
                    **({"asset_url": assertion.asset_url} if assertion.asset_url else {}),
                    "scanner_id": _DISCOVERY_ASSERTION_SCANNER_ID,
                    "scanner_version": _DISCOVERY_ASSERTION_SCANNER_VERSION,
                    "extractor_version": extractor_version,
                    "rule_id": _DISCOVERY_ASSERTION_RULE_ID,
                    "rule_version": _DISCOVERY_ASSERTION_RULE_VERSION,
                },
                scanner_id=_DISCOVERY_ASSERTION_SCANNER_ID,
                scanner_version=_DISCOVERY_ASSERTION_SCANNER_VERSION,
                extractor_version=extractor_version,
                rule_id=_DISCOVERY_ASSERTION_RULE_ID,
                rule_version=_DISCOVERY_ASSERTION_RULE_VERSION,
                fallback_effective_to_observed=False,
            )
        )
    return events


def _active_ad_values(ads: AdIntelligence) -> tuple[int, int, int, int, list[str]]:
    projected_scoreable_subset = ads.scoreable_unique_creatives is not None
    if projected_scoreable_subset:
        # Once a provider declares a scoreable subset, every omitted scoreable
        # counter is unknown-within-that-subset and must fail closed to zero. Falling
        # back field-by-field can leak curated/display-only totals into buying signals.
        active = int(ads.scoreable_active_unique_creatives or 0)
        new_7d = int(ads.scoreable_new_creatives_7d or 0)
        new_30d = int(ads.scoreable_new_creatives_30d or 0)
        windows = int(ads.scoreable_observation_windows_90d or 0)
    else:
        active = ads.active_unique_creatives
        new_7d = ads.new_creatives_7d
        new_30d = ads.new_creatives_30d
        windows = ads.observation_windows_90d
    # An explicitly empty scoreable subset means the observed surface is display-only
    # (for example, a curated positive-only gallery). Do not silently fall back to the
    # display platforms and turn them into a recommended sales channel.
    platforms = ads.scoreable_platforms if projected_scoreable_subset else ads.platforms
    return active, new_7d, new_30d, windows, platforms


def _collect_events(
    candidate: DiscoveredHost,
    observation: SiteObservation,
    *,
    previous_observation: SiteObservation | None,
    ads: AdIntelligence,
    evidence: RevenueSynthesisEvidence,
    accounting: dict | None = None,
) -> list[BuyingSignalEvent]:
    events: list[BuyingSignalEvent] = []
    observed_at = _aware(observation.observed_at) or datetime.now(UTC)
    subject = (observation.canonical_host or observation.hostname or candidate.hostname).lower()
    site_url = _first_url(observation)
    sales = observation.web.sales
    web = observation.web
    recent = web.sitemap_recent_counts

    def signal_count(name: str) -> int:
        return len(observation.signals.get(name, []))

    def add(**kwargs: object) -> None:
        source_url = str(kwargs.pop("source_url", site_url))
        events.append(
            _event(
                subject_key=subject,
                observed_at=observed_at,
                source_url=source_url,
                **kwargs,
            )
        )

    if not observation.live:
        add(
            event_type="site_unreachable",
            family="risk",
            polarity="negative",
            strength=90,
            confidence="high",
            coverage="complete",
            source_kind="company_site",
            summary="The public company site was not reachable as an inspectable HTML page.",
            half_life_days=7,
            independence_key="risk:site_reachability",
        )
    if observation.status_code in {401, 403}:
        add(
            event_type="site_publicly_gated",
            family="risk",
            polarity="negative",
            strength=60,
            confidence="high",
            coverage="complete",
            source_kind="company_site",
            summary="The inspected company site blocked public evidence collection.",
            half_life_days=14,
            independence_key="risk:site_reachability",
        )
    if observation.language.english_presentation is False:
        add(
            event_type="non_english_presentation",
            family="risk",
            polarity="negative",
            strength=75,
            confidence=observation.language.confidence,
            coverage="complete",
            source_kind="company_site",
            summary="The inspected public site does not primarily present in English.",
            half_life_days=90,
            independence_key="risk:language",
        )
    if observation.signals.get("parked"):
        add(
            event_type="parked_domain",
            family="risk",
            polarity="negative",
            strength=100,
            confidence="high",
            coverage="complete",
            source_kind="company_site",
            summary="The inspected domain presents as parked or offered for sale.",
            half_life_days=30,
            independence_key="risk:identity",
        )

    active_ads, new_7d, new_30d, ad_windows, ad_platforms = _active_ad_values(ads)
    verified_ads = ads.identity_state == "verified" and ads.identity_confidence >= 80
    ad_url = next(
        (
            url
            for url in [*ads.source_urls, ads.meta_page_url]
            if url.startswith(("http://", "https://"))
        ),
        "",
    )
    if verified_ads and active_ads and ad_url:
        strength = (
            85 if active_ads >= 10 else 75 if active_ads >= 5 else 62 if active_ads >= 2 else 50
        )
        events.append(
            _event(
                subject_key=subject,
                event_type="verified_active_paid_creative",
                family="distribution_investment",
                polarity="positive",
                strength=strength,
                confidence="high" if ads.identity_confidence >= 90 else "medium",
                coverage="complete",
                source_kind="ad_library",
                source_url=ad_url,
                observed_at=observed_at,
                effective_at=_aware(ads.last_observed_at) or observed_at,
                summary=(
                    f"A verified advertiser identity has {active_ads} active deduplicated "
                    "creative group(s)."
                ),
                half_life_days=30,
                independence_key="distribution:verified_ads",
                details={"active_creative_groups": str(active_ads)},
            )
        )
        subset = ads.scoreable_unique_creatives is not None
        latest_ad_start = _aware(
            ads.scoreable_latest_started_at if subset else ads.latest_started_at
        )
        latest_ad_start = latest_ad_start or _aware(ads.last_observed_at) or observed_at
        if new_7d:
            events.append(
                _event(
                    subject_key=subject,
                    event_type="paid_creative_launch_7d",
                    family="commercial_timing",
                    polarity="positive",
                    strength=min(90, 65 + new_7d * 3),
                    confidence="high",
                    coverage="complete",
                    source_kind="ad_library",
                    source_url=ad_url,
                    observed_at=observed_at,
                    effective_at=latest_ad_start,
                    identity=latest_ad_start.date().isoformat(),
                    summary=(
                        f"The verified advertiser launched {new_7d} creative group(s) in "
                        "the last seven days."
                    ),
                    half_life_days=14,
                    independence_key="timing:paid_creative_launch",
                )
            )
        elif new_30d:
            events.append(
                _event(
                    subject_key=subject,
                    event_type="paid_creative_launch_30d",
                    family="commercial_timing",
                    polarity="positive",
                    strength=min(75, 48 + new_30d * 2),
                    confidence="high",
                    coverage="complete",
                    source_kind="ad_library",
                    source_url=ad_url,
                    observed_at=observed_at,
                    effective_at=latest_ad_start,
                    identity=latest_ad_start.date().isoformat(),
                    summary=(
                        f"The verified advertiser launched {new_30d} creative group(s) in "
                        "the last 30 days."
                    ),
                    half_life_days=21,
                    independence_key="timing:paid_creative_launch",
                )
            )
        first_start = _aware(ads.scoreable_first_started_at if subset else ads.first_started_at)
        last_start = _aware(ads.scoreable_latest_started_at if subset else ads.latest_started_at)
        dated_production = bool(
            first_start
            and last_start
            and last_start - first_start >= timedelta(days=21)
            and new_30d > 0
        )
        if dated_production and (ads.recurring_established or ad_windows >= 2):
            events.append(
                _event(
                    subject_key=subject,
                    event_type="longitudinal_paid_creative",
                    family="recurring_workload",
                    polarity="positive",
                    strength=78 if ads.recurring_established else 62,
                    confidence="high" if ads.recurring_established else "medium",
                    coverage="complete",
                    source_kind="ad_library",
                    source_url=ad_url,
                    observed_at=observed_at,
                    summary=(
                        f"Verified paid creative appears across {ad_windows} distinct "
                        "observation window(s)."
                    ),
                    half_life_days=90,
                    independence_key="recurring:ad_history",
                )
            )
        if ads.image_creatives >= 3 and ads.video_creatives == 0:
            events.append(
                _event(
                    subject_key=subject,
                    event_type="paid_format_video_gap",
                    family="production_pressure",
                    polarity="positive",
                    strength=38,
                    confidence="low",
                    coverage="complete",
                    source_kind="ad_library",
                    source_url=ad_url,
                    observed_at=observed_at,
                    summary=(
                        "The verified paid creative observed is image-heavy with no video "
                        "creative in the covered set; performance is unknown."
                    ),
                    half_life_days=30,
                    independence_key="production:paid_format_mix",
                )
            )
        if active_ads >= 5 and new_30d == 0:
            events.append(
                _event(
                    subject_key=subject,
                    event_type="covered_low_paid_refresh",
                    family="production_pressure",
                    polarity="positive",
                    strength=35,
                    confidence="low",
                    coverage="complete",
                    source_kind="ad_library",
                    source_url=ad_url,
                    observed_at=observed_at,
                    summary=(
                        "The verified ad set remains active without a newly observed creative "
                        "group in 30 days; this suggests refresh pressure, not fatigue."
                    ),
                    half_life_days=21,
                    independence_key="production:paid_refresh",
                    epistemic_status="inferred",
                )
            )

    paid_hits = signal_count("paid_distribution")
    public_marketing_ids = sum(
        bool(observation.public_ids.get(key))
        for key in ("google_tag", "meta_pixel", "tiktok_pixel", "linkedin_partner")
    )
    paid_source = _supported_signal_source(observation, "paid_distribution")
    if (paid_hits and paid_source) or public_marketing_ids or sales.paid_growth_score >= 30:
        add(
            event_type="first_party_paid_distribution",
            family="distribution_investment",
            polarity="positive",
            strength=min(
                65,
                25 + paid_hits * 8 + public_marketing_ids * 6 + sales.paid_growth_score // 5,
            ),
            confidence="medium" if paid_hits and public_marketing_ids else "low",
            coverage="partial",
            source_kind="company_site",
            source_url=paid_source or observation.final_url or site_url,
            summary=(
                "First-party pages expose paid-distribution language, marketing identifiers, "
                "or a paid-growth technology footprint."
            ),
            half_life_days=75,
            independence_key=f"causal:distribution:first_party:{subject}",
        )

    recurring_hits = signal_count("recurring")
    creative_hits = signal_count("creative_demand")
    recent_commercial = (
        sales.feed_recent_30d
        + recent.get("products", 0)
        + recent.get("campaigns", 0)
        + recent.get("case_studies", 0)
    )
    recurring_source = _supported_signal_source(observation, "recurring")
    creative_source = _supported_signal_source(observation, "creative_demand")
    if recurring_hits and creative_hits and recurring_source and creative_source:
        add(
            event_type="explicit_recurring_creative_language",
            family="proof_match" if evidence.archetype == "agency" else "recurring_workload",
            polarity="positive",
            strength=min(72, 38 + recurring_hits * 8 + creative_hits * 5),
            confidence="medium",
            coverage="partial",
            source_kind="company_site",
            source_url=recurring_source,
            summary="First-party language connects recurring work with creative delivery.",
            half_life_days=180,
            independence_key="recurring:first_party_language",
        )
    if evidence.archetype == "agency":
        for source, excerpt, formats in _retained_delivery_sources(observation):
            add(
                event_type="attributed_retained_client_delivery",
                family="recurring_workload",
                polarity="positive",
                strength=68,
                confidence="medium",
                coverage="partial",
                source_kind="company_site",
                source_url=source,
                summary="A first-party dated account describes delivered "
                "creative work in a multi-year client partnership; "
                "external capacity need is unknown.",
                half_life_days=120,
                independence_key="recurring:first_party_retained_delivery",
                epistemic_status="inferred",
                effective_at=None,
                fallback_effective_to_observed=False,
                details={
                    "excerpt": excerpt,
                    "creative_deliverables": ", ".join(formats),
                    "source_effective_date": "unknown",
                    "external_procurement": "unknown",
                },
            )
    if sales.feed_recent_30d and sales.feed_url and sales.feed_latest:
        recent_commercial = min(sales.feed_recent_30d, sales.feed_commercial_items)
        effective = (
            _parse_datetime(sales.feed_latest)
            or _parse_datetime(web.latest_sitemap_change)
            or observed_at
        )
        add(
            event_type="recent_commercial_publishing",
            family="recurring_workload",
            polarity="positive",
            strength=min(78, 32 + recent_commercial * 5),
            confidence="medium" if recent_commercial >= 3 else "low",
            coverage="complete" if web.sitemaps_checked else "partial",
            source_kind="feed" if sales.feed_recent_30d else "sitemap",
            source_url=sales.feed_url,
            effective_at=effective,
            identity=effective.date().isoformat(),
            summary=(
                f"Public feeds or sitemaps show {recent_commercial} recent commercial, "
                "product, campaign, or case-study item(s)."
            ),
            half_life_days=45,
            independence_key="recurring:publishing",
        )

    catalog_scale = max(sales.catalog_available_products, sales.catalog_total_hint)
    if catalog_scale >= 10:
        add(
            event_type="public_catalog_scale",
            family="ability_proxy",
            polarity="positive",
            strength=70 if catalog_scale >= 250 else 58 if catalog_scale >= 75 else 42,
            confidence="medium",
            coverage="partial",
            source_kind="catalog",
            source_url=sales.feed_url or _first_url(observation, "product", "shop", "catalog"),
            summary=(
                f"The public catalog exposes at least {catalog_scale} product or catalog "
                "records, an operating-scale proxy rather than revenue evidence."
            ),
            half_life_days=180,
            independence_key="ability:catalog",
        )
    if web.vendors.stack_score >= 45:
        add(
            event_type="operating_vendor_stack",
            family="ability_proxy",
            polarity="positive",
            strength=min(65, 25 + web.vendors.stack_score // 2),
            confidence="low",
            coverage="partial",
            source_kind="technology",
            source_url=web.vendors.disclosure_url or site_url,
            summary=(
                "Public DNS, disclosures, or page evidence show an operating technology "
                "footprint; it does not establish budget."
            ),
            half_life_days=180,
            independence_key="ability:technology",
            epistemic_status="inferred",
        )

    production_job_postings = list(_production_postings(observation).values())
    open_production_roles = (
        len(production_job_postings)
        if web.job_postings
        else max(web.creative_jobs, web.photo_video_jobs)
    )
    if open_production_roles:
        posting_support = sum(
            bool(
                posting.production_pressure_signals
                or posting.external_capacity_signals
                or posting.ai_workflow_signals
                or posting.rights_risk_signals
            )
            for posting in production_job_postings
        )
        base_strength = (
            72 if open_production_roles >= 5 else 58 if open_production_roles >= 2 else 42
        )
        source_url = (
            production_job_postings[0].source_url
            if production_job_postings
            else _first_url(
                observation,
                "career",
                "job",
                "greenhouse",
                "lever",
                "ashby",
            )
        )
        posted_dates = [
            _aware(posting.posted_at)
            for posting in production_job_postings
            if _aware(posting.posted_at) is not None
        ]
        add(
            event_type="open_creative_production_roles",
            family="production_pressure",
            polarity="positive",
            strength=min(82, base_strength + min(12, posting_support * 4)),
            confidence="medium",
            coverage="complete" if web.ats_boards else "partial",
            source_kind="career_page",
            source_url=source_url,
            effective_at=max(posted_dates) if posted_dates else observed_at,
            summary=(
                f"Public careers data includes {open_production_roles} creative, photo, or "
                f"video role(s); {posting_support} structured posting(s) add workflow or "
                "capacity context, but hiring pressure does not prove willingness to outsource."
            ),
            half_life_days=45,
            independence_key="production:hiring",
            details={
                "structured_postings": str(len(production_job_postings)),
                "contextual_postings": str(posting_support),
            },
        )
        if web.ats_boards or production_job_postings:
            add(
                event_type="current_creative_production_hiring",
                family="commercial_timing",
                polarity="positive",
                strength=min(68, 44 + open_production_roles * 4),
                confidence="medium",
                coverage="complete" if web.ats_boards else "partial",
                source_kind="career_page",
                source_url=source_url,
                effective_at=max(posted_dates) if posted_dates else observed_at,
                summary=(
                    f"Current public careers data lists {open_production_roles} creative, "
                    "photo, or video production role(s)."
                ),
                half_life_days=30,
                independence_key="timing:creative_hiring",
            )

    visible_video = sales.video_assets + sales.embedded_video_players
    static_heavy = sales.image_assets >= 12 and visible_video * 8 < sales.image_assets
    if static_heavy:
        add(
            event_type="first_party_static_heavy_mix",
            family="production_pressure",
            polarity="positive",
            strength=36,
            confidence="low",
            coverage="partial",
            source_kind="company_site",
            source_url=_first_url(observation, "campaign", "work", "product"),
            summary=(
                f"Inspected pages contain {sales.image_assets} images and {visible_video} "
                "visible video assets or players; this is a format opportunity, not a "
                "capacity fact."
            ),
            half_life_days=60,
            independence_key="production:first_party_format_mix",
        )

    corpus = " ".join(
        (
            observation.title,
            observation.description,
            observation.text_excerpt,
            *(page.text_excerpt for page in observation.pages),
        )
    ).lower()
    contextual_pressure = _group_contextual_signals_by_source(observation, "production_pressure")
    contextual_offer_present = any(
        _contextual_signal_subtype(value) == "external_capacity_offer"
        for values in contextual_pressure.values()
        for value in values
    )
    for source_url, values in contextual_pressure.items():
        shortfall_values = [
            value
            for value in values
            if _contextual_signal_subtype(value)
            in {"capacity_shortfall", "backlog", "throughput_pressure", "outsourcing"}
        ]
        if not shortfall_values:
            continue
        subtypes = sorted({_contextual_signal_subtype(value) for value in shortfall_values})
        add(
            event_type="explicit_first_party_production_pressure",
            family="production_pressure",
            polarity="positive",
            strength=min(82, 58 + len(subtypes) * 7),
            confidence="high" if len(subtypes) >= 2 else "medium",
            coverage="partial",
            source_kind="company_site",
            source_url=source_url,
            identity=source_url,
            summary=(
                "First-party language explicitly describes a production shortfall, backlog, "
                "throughput constraint, or use of outside production; scope and duration remain "
                "unverified."
            ),
            half_life_days=90,
            independence_key="production:first_party_explicit_pressure",
            details={"subtypes": ", ".join(subtypes)},
        )

    contextual_capacity = _group_contextual_signals_by_source(observation, "production_capacity")
    for source_url, values in contextual_capacity.items():
        subtypes = sorted({_contextual_signal_subtype(value) for value in values})
        add(
            event_type="explicit_first_party_internal_capacity",
            family="production_pressure",
            polarity="negative",
            strength=min(70, 45 + len(subtypes) * 7),
            confidence="medium",
            coverage="partial",
            source_kind="company_site",
            source_url=source_url,
            identity=source_url,
            summary=(
                "First-party language describes an in-house team, staffed production function, "
                "or owned studio; overflow need is not established."
            ),
            half_life_days=180,
            independence_key="production:first_party_internal_capacity",
            details={"subtypes": ", ".join(subtypes)},
        )

    contextual_fit: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for category in (
        "ai_workflow",
        "business_model",
        "creative_use_cases",
        "localization",
        "creative_formats",
    ):
        for source_url, values in _group_contextual_signals_by_source(
            observation, category
        ).items():
            contextual_fit[source_url][category].extend(values)
    for source_url, categories in contextual_fit.items():
        labels = sorted(categories)
        subtype_count = len(
            {
                _contextual_signal_subtype(value)
                for values in categories.values()
                for value in values
            }
        )
        add(
            event_type="first_party_creative_operating_context",
            family="proof_match",
            polarity="positive",
            strength=min(72, 28 + len(labels) * 7 + subtype_count * 2),
            confidence="medium" if len(labels) >= 2 else "low",
            coverage="partial",
            source_kind="company_site",
            source_url=source_url,
            identity=source_url,
            summary=(
                "First-party pages describe relevant creative formats, use cases, localization, "
                "business model, or AI workflow; this supports lane fit, not purchase intent."
            ),
            half_life_days=180,
            independence_key="proof:first_party_operating_context",
            details={"categories": ", ".join(labels)},
        )

    contextual_rights = _group_contextual_signals_by_source(observation, "rights_risk")
    for source_url, values in contextual_rights.items():
        subtypes = sorted({_contextual_signal_subtype(value) for value in values})
        explicit_restriction = "ai_restriction" in subtypes
        add(
            event_type=(
                "explicit_first_party_ai_restriction"
                if explicit_restriction
                else "first_party_rights_governance"
            ),
            family="risk",
            polarity="negative",
            strength=74 if explicit_restriction else min(42, 25 + len(subtypes) * 4),
            confidence="medium" if explicit_restriction else "low",
            coverage="partial",
            source_kind="company_site",
            source_url=source_url,
            identity=source_url,
            summary=(
                "First-party language explicitly restricts AI use and requires verification "
                "before proposing production."
                if explicit_restriction
                else "First-party language identifies rights, likeness, authenticity, or "
                "fidelity governance that must be verified in discovery."
            ),
            half_life_days=180,
            independence_key="risk:first_party_ai_rights",
            details={"subtypes": ", ".join(subtypes)},
        )

    if contextual_offer_present:
        offer_source = next(
            source_url
            for source_url, values in contextual_pressure.items()
            if any(
                _contextual_signal_subtype(value) == "external_capacity_offer" for value in values
            )
        )
        add(
            event_type="first_party_external_capacity_offer",
            family="proof_match",
            polarity="positive",
            strength=54,
            confidence="medium",
            coverage="partial",
            source_kind="company_site",
            source_url=offer_source,
            summary=(
                "First-party language offers white-label, overflow, or external production "
                "capacity; this supports an agency operating-model hypothesis, not buyer need."
            ),
            half_life_days=180,
            independence_key="proof:first_party_operating_context",
        )

    external_terms = [term for term in _EXTERNAL_CAPACITY_TERMS if term in corpus]
    if external_terms and not contextual_pressure:
        add(
            event_type="explicit_external_capacity_model",
            family="proof_match",
            polarity="positive",
            strength=min(72, 48 + len(external_terms) * 7),
            confidence="medium",
            coverage="partial",
            source_kind="company_site",
            source_url=_first_url(observation, "service", "production", "work"),
            summary=(
                "First-party language explicitly references flexible, overflow, external, "
                "or white-label production capacity offered as a service; buying need is unknown."
            ),
            half_life_days=180,
            independence_key="proof:first_party_operating_context",
            details={"terms": ", ".join(external_terms[:5])},
        )
    internal_terms = [term for term in _INTERNAL_CAPACITY_TERMS if term in corpus]
    if internal_terms and not contextual_capacity:
        add(
            event_type="explicit_internal_capacity",
            family="production_pressure",
            polarity="negative",
            strength=min(55, 34 + len(internal_terms) * 5),
            confidence="low",
            coverage="partial",
            source_kind="company_site",
            source_url=_first_url(observation, "team", "studio", "production"),
            summary=(
                "First-party language references material internal creative or production "
                "capacity; overflow need remains unknown."
            ),
            half_life_days=180,
            independence_key="production:internal_capacity",
            details={"terms": ", ".join(internal_terms[:5])},
        )

    if sales.news_commercial_triggers:
        news_url = next(iter(sales.news_urls), site_url)
        effective = _parse_datetime(sales.news_latest) or observed_at
        events.append(
            _event(
                subject_key=subject,
                event_type="recent_commercial_news",
                family="commercial_timing",
                polarity="positive",
                strength=min(82, 48 + sales.news_commercial_triggers * 7),
                confidence="medium",
                coverage="partial",
                source_kind="news",
                source_url=news_url,
                observed_at=observed_at,
                effective_at=effective,
                identity=effective.date().isoformat(),
                summary=(
                    f"Public news contains {sales.news_commercial_triggers} recent launch, "
                    "expansion, funding, partnership, or commercial trigger(s)."
                ),
                half_life_days=30,
                independence_key="timing:news",
            )
        )
    feed_effective = _parse_datetime(sales.feed_latest)
    if sales.feed_recent_30d and sales.feed_commercial_items and feed_effective:
        add(
            event_type="recent_first_party_commercial_feed",
            family="commercial_timing",
            polarity="positive",
            strength=min(72, 44 + sales.feed_commercial_items * 5),
            confidence="medium",
            coverage="partial",
            source_kind="feed",
            source_url=sales.feed_url or _first_url(observation, "feed", "news", "campaign"),
            effective_at=feed_effective,
            identity=feed_effective.date().isoformat(),
            summary=(
                f"A dated first-party feed contains {sales.feed_commercial_items} recent "
                "commercial, product, or campaign item(s)."
            ),
            half_life_days=30,
            independence_key="timing:first_party_feed",
        )
    recent_launches = recent.get("products", 0) + recent.get("campaigns", 0)
    if recent_launches:
        effective = _parse_datetime(web.latest_sitemap_change) or observed_at
        add(
            event_type="recent_product_campaign_surface",
            family="commercial_timing",
            polarity="positive",
            strength=min(38, 18 + recent_launches * 2),
            confidence="low",
            coverage="complete" if web.sitemaps_checked else "partial",
            source_kind="sitemap",
            source_url=_first_url(observation, "product", "campaign", "collection"),
            effective_at=effective,
            identity=effective.date().isoformat(),
            summary=(
                f"Sitemap last-modified metadata marks {recent_launches} product or campaign "
                "URL(s) within 30 days; automated platform rewrites make this a research cue, "
                "not a contact trigger."
            ),
            half_life_days=30,
            independence_key="timing:first_party_launch",
            epistemic_status="inferred",
        )

    portfolio_count = (
        len(legacy_agency_portfolio_domains(web)) if evidence.archetype == "agency" else 0
    )
    if portfolio_count:
        add(
            event_type="agency_client_portfolio",
            family="proof_match",
            polarity="positive",
            strength=min(75, 35 + portfolio_count * 5),
            confidence="medium" if portfolio_count >= 3 else "low",
            coverage="partial",
            source_kind="company_site",
            source_url=_first_url(observation, "work", "client", "case", "portfolio"),
            summary=(
                f"Agency work pages link to {portfolio_count} probable client domain(s); "
                "current retained workload remains unverified."
            ),
            half_life_days=180,
            independence_key="proof:agency_portfolio",
        )
    if creative_hits and observation.live and creative_source:
        add(
            event_type="explicit_visual_creative_work",
            family="proof_match",
            polarity="positive",
            strength=min(62, 32 + creative_hits * 7),
            confidence="medium" if creative_hits >= 2 else "low",
            coverage="partial",
            source_kind="company_site",
            source_url=creative_source,
            summary="First-party pages explicitly reference visual creative work.",
            half_life_days=180,
            independence_key="proof:first_party_creative",
        )

    eligible_emails = [
        contact
        for contact in observation.contacts.emails
        if contact.outreach_eligible and not contact.historical
    ]
    eligible_phones = [
        contact
        for contact in observation.contacts.phones
        if contact.outreach_eligible and not contact.historical
    ]
    if eligible_emails or eligible_phones:
        contact = eligible_emails[0] if eligible_emails else eligible_phones[0]
        events.append(
            _event(
                subject_key=subject,
                event_type="public_business_contact",
                family="buyer_access",
                polarity="positive",
                strength=68 if getattr(contact, "purpose", "unknown") != "general" else 55,
                confidence=contact.confidence,
                coverage="complete",
                source_kind="public_contact",
                source_url=contact.source_url,
                observed_at=observed_at,
                summary=(
                    "A current first-party business contact is explicitly published and "
                    "marked outreach-eligible by the contact safety rules."
                ),
                half_life_days=90,
                independence_key="access:first_party_contact",
            )
        )
    elif observation.contact_urls or observation.contacts.contact_forms:
        add(
            event_type="public_contact_route",
            family="buyer_access",
            polarity="positive",
            strength=36,
            confidence="medium",
            coverage="complete" if observation.contacts.contact_pages_checked else "partial",
            source_kind="public_contact",
            source_url=(observation.contact_urls or observation.contacts.contact_forms)[0],
            summary="A current first-party contact route is publicly available.",
            half_life_days=90,
            independence_key="access:first_party_contact_route",
        )

    rights_terms = [term for term in _RIGHTS_RISK_TERMS if term in corpus]
    if rights_terms and not contextual_rights:
        add(
            event_type="explicit_ai_rights_constraint",
            family="risk",
            polarity="negative",
            strength=min(78, 50 + len(rights_terms) * 8),
            confidence="medium",
            coverage="partial",
            source_kind="company_site",
            source_url=_first_url(observation, "legal", "policy", "terms", "brand"),
            summary=(
                "Public language contains an AI, rights, model-release, or imagery constraint "
                "that must be verified before proposing production."
            ),
            half_life_days=180,
            independence_key="risk:ai_rights",
            details={"terms": ", ".join(rights_terms[:5])},
        )
    competitive_terms = [term for term in _COMPETITIVE_TERMS if term in corpus]
    if competitive_terms:
        add(
            event_type="direct_service_conflict",
            family="risk",
            polarity="negative",
            strength=78,
            confidence="medium",
            coverage="partial",
            source_kind="company_site",
            source_url=_first_url(observation, "service", "pricing", "ai"),
            summary="First-party language suggests a directly competing AI-creative subscription.",
            half_life_days=180,
            independence_key="risk:competitive_conflict",
        )

    if evidence.placement_score:
        add(
            event_type="deterministic_specialist_lane_match",
            family="proof_match",
            polarity="positive",
            strength=evidence.placement_score,
            confidence="low",
            coverage="partial",
            source_kind="deterministic_synthesis",
            source_url=_first_url(observation, "work", "campaign", "product"),
            summary=(
                "Deterministic public evidence suggests a possible photo/video specialist-lane "
                "match; a human must validate taste and workflow fit."
            ),
            half_life_days=120,
            independence_key="proof:deterministic_placement",
            epistemic_status="inferred",
        )

    if previous_observation is not None:
        # Counts from sampled catalogs are lower bounds, not comparable censuses. A newly
        # collected catalog can never establish growth. The dated release/window adapters
        # handle supported changes using stable identities and explicit coverage instead.
        current_jobs = _production_postings(observation)
        previous_jobs = _production_postings(previous_observation)
        current_scope = _complete_hiring_scopes(observation)
        previous_scope = _complete_hiring_scopes(previous_observation)
        ordered = observed_at > (_aware(previous_observation.observed_at) or observed_at)
        comparable = ordered and bool(current_scope) and current_scope == previous_scope
        new_jobs = set(current_jobs) - set(previous_jobs) if comparable else set()
        # Require new stable postings AND net growth using the same definition on both sides.
        job_delta = max(0, len(current_jobs) - len(previous_jobs)) if new_jobs else 0
        if job_delta:
            posting = current_jobs[sorted(new_jobs)[0]]
            add(
                event_type="creative_hiring_increase_since_prior_observation",
                family="commercial_timing",
                polarity="positive",
                strength=min(64, 38 + job_delta * 5),
                confidence="medium",
                coverage="complete",
                source_kind="career_page",
                source_url=posting.source_url,
                identity="|".join(sorted(new_jobs)),
                summary=(
                    f"Comparable complete careers censuses contain {job_delta} more "
                    "current creative-production postings, with new stable posting identities; "
                    "this does not establish completed hiring or business growth."
                ),
                half_life_days=30,
                independence_key="timing:hiring_change",
                details={
                    "new_posting_ids": ", ".join(sorted(new_jobs)),
                    "prior_observed_at": previous_observation.observed_at.isoformat(),
                },
            )
        if (
            ordered
            and observation.content_hash
            and previous_observation.content_hash
            and observation.content_hash != previous_observation.content_hash
        ):
            add(
                event_type="generic_site_change_since_prior_observation",
                family="commercial_timing",
                polarity="positive",
                strength=18,
                confidence="low",
                coverage="complete",
                source_kind="company_site",
                summary=(
                    "The inspected public page changed since the prior observation; the change "
                    "is not by itself a commercial trigger."
                ),
                half_life_days=14,
                identity=observation.content_hash,
                independence_key="timing:generic_site_change",
            )

    events.extend(
        _collect_discovery_assertion_events(
            candidate,
            subject_key=subject,
            observed_at=observed_at,
        )
    )
    frontier_events = _collect_frontier_events(
        observation, subject_key=subject, accounting=accounting
    )
    if any(event.event_type == "frontier_public_funnel_schema" for event in frontier_events):
        # The richer public schema replaces the generic identifier/paid-language reason. Keeping
        # both would show two rows for one first-party distribution surface even though the cluster
        # correctly shares one independence key.
        events = [event for event in events if event.event_type != "first_party_paid_distribution"]
    canonical_creative_hiring = any(
        event.event_type == "frontier_creative_hiring_surface" for event in frontier_events
    )
    canonical_hiring_velocity = any(
        event.event_type == "frontier_creative_hiring_current" for event in frontier_events
    )
    if canonical_creative_hiring or canonical_hiring_velocity:
        # These legacy rows are alternate projections of the same public openings. The canonical
        # aggregate has stricter literal-count, coverage, lineage, and source-date gates, so it
        # replaces rather than stacks with the generic careers-page interpretation.
        events = [
            event
            for event in events
            if event.event_type
            not in {
                "open_creative_production_roles",
                "current_creative_production_hiring",
                "creative_hiring_increase_since_prior_observation",
            }
        ]
    events.extend(frontier_events)
    return events


def _merge_events(events: Sequence[BuyingSignalEvent]) -> list[BuyingSignalEvent]:
    latest: dict[str, BuyingSignalEvent] = {}
    for event in events:
        current = latest.get(event.event_key)
        current_time = _aware(current.observed_at) if current else None
        event_time = _aware(event.observed_at)
        if current is None or (event_time or datetime.min.replace(tzinfo=UTC)) >= (
            current_time or datetime.min.replace(tzinfo=UTC)
        ):
            latest[event.event_key] = event
    return sorted(latest.values(), key=lambda item: (item.family, item.event_key))


def _cluster_map(clusters: Sequence[SignalCluster]) -> dict[SignalFamily, SignalCluster]:
    return {cluster.family: cluster for cluster in clusters}


def _positive(clusters: dict[SignalFamily, SignalCluster], family: SignalFamily) -> int:
    cluster = clusters.get(family)
    return max(0, cluster.net_strength) if cluster else 0


def _negative(clusters: dict[SignalFamily, SignalCluster], family: SignalFamily) -> int:
    cluster = clusters.get(family)
    return cluster.negative_strength if cluster else 0


def _opportunity_vector(
    observation: SiteObservation,
    clusters: Sequence[SignalCluster],
    evidence: RevenueSynthesisEvidence,
    *,
    trace: bool = True,
) -> OpportunityVector:
    by_family = _cluster_map(clusters)
    distribution = _positive(by_family, "distribution_investment")
    recurring = _positive(by_family, "recurring_workload")
    production = _positive(by_family, "production_pressure")
    timing_events = _positive(by_family, "commercial_timing")
    ability_events = _positive(by_family, "ability_proxy")
    access_events = _positive(by_family, "buyer_access")
    proof_events = _positive(by_family, "proof_match")
    risk = max(
        _negative(by_family, "risk"),
        _negative(by_family, "production_pressure") // 2,
    )

    archetype_prior = {
        "agency": 65,
        "ecommerce_brand": 65,
        "media_membership": 48,
        "creator_education": 42,
        "saas_or_other": 42,
        "unknown": 20,
    }.get(evidence.archetype, 32)
    fit = _clamp(
        archetype_prior * 0.25
        + evidence.demand_score * 0.30
        + recurring * 0.25
        + proof_events * 0.20
    )
    need = _clamp(production * 0.45 + recurring * 0.30 + distribution * 0.25)
    supporting_need_families = sum(value >= 25 for value in (distribution, recurring, production))
    if supporting_need_families < 2:
        need = min(need, 44)
    # Legacy timing/growth aggregates have no independent receipt/effective-date contract.
    # They may corroborate a surviving dated event, never keep an expired trigger alive.
    timing = _clamp(
        timing_events * 0.85
        + min(evidence.timing_score, timing_events) * 0.10
        + min(evidence.growth_score, timing_events) * 0.05
    )
    ability = _clamp(ability_events * 0.55 + evidence.capacity_score * 0.45)
    access = _clamp(access_events * 0.75 + evidence.reachability_score * 0.25)
    proof = _clamp(proof_events * 0.55 + evidence.placement_score * 0.45)

    independent_paths = sum(min(3, cluster.independent_evidence_count) for cluster in clusters)
    # Additional rows from one causal path improve traceability, not confidence. Quality grows
    # only with currently surviving independent paths, so expired or correlated scanner output
    # cannot make the company look better merely by increasing event volume.
    event_quality = min(100, independent_paths * 9)
    evidence_quality = _clamp(evidence.data_confidence * 0.70 + event_quality * 0.30)
    known: list[str] = []
    if evidence.archetype != "unknown" and (evidence.demand_score or recurring or proof_events):
        known.append("fit")
    if supporting_need_families >= 2:
        known.append("need")
    if timing_events:
        known.append("timing")
    if ability_events or evidence.capacity_score:
        known.append("ability")
    if access_events:
        known.append("access")
    if proof_events or evidence.placement_score:
        known.append("proof")
    unknown = [dimension for dimension in _DIMENSIONS if dimension not in known]
    uncertainty = _clamp(100 - evidence_quality * 0.75 + len(unknown) * 6)

    durable_fit = _clamp(
        fit * 0.28
        + need * 0.27
        + ability * 0.23
        + proof * 0.12
        + evidence_quality * 0.10
        - risk * 0.25
    )
    contact_now = _clamp(
        durable_fit * 0.50 + timing * 0.25 + access * 0.15 + evidence_quality * 0.10 - risk * 0.30
    )
    retained_value = _clamp(
        recurring * 0.45 + fit * 0.25 + need * 0.20 + ability * 0.10 - risk * 0.20
    )
    founder_priority = _clamp(
        contact_now * 0.65 + retained_value * 0.25 + proof * 0.10 - uncertainty * 0.15
    )
    founder_priority = _clamp(founder_priority * 100 / _MAX_RAW_FOUNDER_PRIORITY)
    if "need" in unknown:
        durable_fit = min(durable_fit, 45)
        contact_now = min(contact_now, 40)
        founder_priority = min(founder_priority, 40)
    if "timing" in unknown:
        contact_now = min(contact_now, 55)
        founder_priority = min(founder_priority, 55)
    if "access" in unknown:
        contact_now = min(contact_now, 55)
        founder_priority = min(founder_priority, 55)
    if not observation.live or risk >= 75:
        contact_now = min(contact_now, 15)
        founder_priority = min(founder_priority, 15)

    if evidence_quality >= 65 and len(known) >= 5 and "need" in known:
        confidence: ConfidenceLevel = "high"
    elif evidence_quality >= 40 and len(known) >= 3:
        confidence = "medium"
    else:
        confidence = "low"
    dimension_keys = {
        family: cluster.event_keys for family, cluster in by_family.items() if cluster.event_keys
    }
    reasons = [
        cluster.positive_summary or cluster.summary
        for cluster in sorted(clusters, key=lambda item: item.positive_strength, reverse=True)
        if cluster.net_strength > 0
    ][:6]
    risks = [
        cluster.negative_summary or cluster.summary
        for cluster in sorted(clusters, key=lambda item: item.negative_strength, reverse=True)
        if cluster.negative_strength > 0
    ][:5]
    vector = OpportunityVector(
        policy_version=VERSION,
        fit=fit,
        need=need,
        timing=timing,
        ability=ability,
        access=access,
        proof=proof,
        evidence_quality=evidence_quality,
        risk=risk,
        uncertainty=uncertainty,
        durable_fit=durable_fit,
        contact_now=contact_now,
        founder_priority=founder_priority,
        retained_value_proxy=retained_value,
        confidence=confidence,
        known_dimensions=known,
        unknown_dimensions=unknown,
        reasons=reasons,
        risks=risks,
        dimension_event_keys=dimension_keys,
    )
    if trace:
        dimensions = (
            "fit",
            "need",
            "timing",
            "ability",
            "access",
            "proof",
            "evidence_quality",
            "risk",
            "uncertainty",
            "durable_fit",
            "contact_now",
            "retained_value_proxy",
            "founder_priority",
        )
        # Family effects measure the event channel while holding aggregate context fixed.
        # Full source effects separately rebuild aggregates and dependent evidence in LeadStore.
        # Both are conditional sensitivities, not additive points or causal sales lift.
        vector.family_effects = {
            family: {name: getattr(vector, name) - getattr(removed, name) for name in dimensions}
            for family in by_family
            for removed in [
                _opportunity_vector(
                    observation, [c for c in clusters if c.family != family], evidence, trace=False
                )
            ]
        }
    return vector


def _selected_frontier_scan(observation: SiteObservation | None) -> str:
    if observation is None:
        return ""
    frontier = getattr(observation.web, "frontier", None)
    if frontier is None:
        return ""
    reference = _aware(getattr(frontier, "updated_at", None)) or _aware(observation.observed_at)
    fresh_scanners: set[str] = set()
    if reference is not None:
        for item in getattr(frontier, "executions", ()):
            if str(getattr(item, "status", "")) not in {
                "success",
                "success_empty",
                "not_applicable",
            }:
                continue
            next_due = _aware(getattr(item, "next_due_at", None))
            completed = _aware(getattr(item, "finished_at", None)) or _aware(
                getattr(item, "started_at", None)
            )
            within_hard_reuse_window = bool(
                completed is not None and completed > reference - timedelta(hours=24)
            )
            if (next_due is not None and next_due > reference) or within_hard_reuse_window:
                fresh_scanners.add(str(getattr(item, "scanner_id", "")))
    selected = [
        item
        for item in getattr(frontier, "schedule_decisions", ())
        if bool(getattr(item, "selected", False))
        and str(getattr(item, "scanner_id", "")) in FRONTIER_SCANNER_IDS
        and str(getattr(item, "scanner_id", "")) not in fresh_scanners
    ]
    if not selected:
        return ""
    selected.sort(
        key=lambda item: (
            -float(getattr(item, "expected_information_gain", 0.0) or 0.0),
            str(getattr(item, "scanner_id", "")),
        )
    )
    return str(getattr(selected[0], "scanner_id", ""))


def _next_best_scan(
    opportunity: OpportunityVector,
    archetype: str,
    observation: SiteObservation | None = None,
) -> NextBestScan:
    frontier_scan = _selected_frontier_scan(observation)
    if frontier_scan:
        return frontier_scan
    unknown = set(opportunity.unknown_dimensions)
    if "need" in unknown:
        if opportunity.fit >= 35:
            return "creative_hiring_and_capacity"
        return "verified_paid_ad_activity"
    if "timing" in unknown:
        return "recent_commercial_events"
    if archetype == "agency" and "proof" in unknown:
        return "agency_client_portfolio"
    if "access" in unknown:
        return "public_buyer_and_contact"
    if "proof" in unknown:
        return "creative_format_and_concept_mix"
    if opportunity.durable_fit >= 55 and opportunity.risk < 50:
        return "ai_rights_and_workflow"
    return "none"


def _action(opportunity: OpportunityVector, observation: SiteObservation) -> RevenueAction:
    if not observation.live or opportunity.risk >= 75:
        return "deprioritize"
    if (
        opportunity.founder_priority >= 65
        and opportunity.need >= 50
        and opportunity.timing >= 42
        and opportunity.access >= 35
        and opportunity.evidence_quality >= 42
    ):
        return "contact_now"
    if opportunity.durable_fit >= 48 and opportunity.timing < 40:
        return "watch"
    if opportunity.durable_fit >= 42 and opportunity.uncertainty >= 38:
        return "research_next"
    if opportunity.durable_fit >= 32:
        return "nurture"
    if opportunity.uncertainty >= 70 and opportunity.fit >= 25:
        return "research_next"
    return "deprioritize"


def _public_people(observation: SiteObservation) -> list[PublicPersonReference]:
    people: list[PublicPersonReference] = []
    seen: set[tuple[str, str]] = set()
    for person in observation.contacts.people:
        name = person.name.strip()
        role = person.role.strip()
        key = (name.lower(), role.lower())
        if not name or not role or not person.source_url or key in seen:
            continue
        seen.add(key)
        people.append(
            PublicPersonReference(
                name=name,
                role=role,
                company=observation.title,
                source_url=person.source_url,
                profile_url=person.profile_url,
                observed_at=_aware(person.observed_at) or _aware(observation.observed_at),
                confidence=person.confidence,
                current_status="current_public",
                buyer_role_hint=person.buyer_role,
            )
        )
    for contact in observation.contacts.emails:
        name = contact.person_name.strip()
        role = contact.person_role.strip()
        if not name or not role or not contact.source_url or contact.historical:
            continue
        key = (name.lower(), role.lower())
        if key in seen:
            continue
        seen.add(key)
        people.append(
            PublicPersonReference(
                name=name,
                role=role,
                company=observation.title,
                source_url=contact.source_url,
                observed_at=_aware(contact.observed_at) or _aware(observation.observed_at),
                confidence=contact.confidence,
                current_status="current_public",
            )
        )
    return people[:5]


def _buyer_roles(
    archetype: str,
    people: Sequence[PublicPersonReference],
    evidence_urls: Sequence[str],
    observation: SiteObservation,
) -> list[BuyerRoleHypothesis]:
    if archetype == "agency":
        definitions = (
            ("economic_buyer", "Agency owner or managing director", "Owns margin and capacity."),
            ("champion", "Head of creative or production", "Owns the delivery queue."),
            ("approver", "Operations or finance lead", "Validates workflow and recurring cost."),
        )
    else:
        definitions = (
            ("economic_buyer", "Founder, CMO, or VP Marketing", "Owns growth and creative budget."),
            ("champion", "Head of growth, creative, or content", "Owns campaign throughput."),
            (
                "approver",
                "Brand, operations, legal, or finance lead",
                "Validates risk and workflow.",
            ),
        )
    roles: list[BuyerRoleHypothesis] = []
    contextual_role_urls: dict[str, list[str]] = defaultdict(list)
    role_subtype_map = {
        "marketing_leader": "economic_buyer",
        "owner_executive": "economic_buyer",
        "brand_creative_leader": "champion",
        "production_leader": "champion",
        "commerce_social_leader": "champion",
    }
    for value in observation.signals.get("buyer_roles", []):
        role_type = role_subtype_map.get(_contextual_signal_subtype(value))
        source_url = _contextual_signal_source(value)
        if role_type and source_url:
            contextual_role_urls[role_type].append(source_url)
    for role_type, role, rationale in definitions:
        matching = [
            person
            for person in people
            if person.buyer_role_hint == role_type
            or any(token in person.role.lower() for token in role.lower().split() if len(token) > 4)
        ]
        role_evidence = list(
            dict.fromkeys([*contextual_role_urls.get(role_type, []), *evidence_urls[:2]])
        )
        if contextual_role_urls.get(role_type):
            rationale += (
                " A first-party page also contains this role category, but identity and "
                "authority remain unverified."
            )
        roles.append(
            BuyerRoleHypothesis(
                role_type=role_type,
                role=role,
                rationale=rationale + " This role hypothesis must be verified.",
                epistemic_status="inferred",
                confidence=("medium" if matching or contextual_role_urls.get(role_type) else "low"),
                evidence_urls=role_evidence,
                public_people=matching[:2],
            )
        )
    return roles


def _first_month_asks(
    observation: SiteObservation,
    ads: AdIntelligence,
    archetype: str,
    now: datetime | None = None,
) -> list[str]:
    from .sales_engine.workload import build_workload_intelligence

    workload = build_workload_intelligence(observation, now=now or observation.observed_at)
    return [
        f"{task.title}: {task.description} Source: {task.evidence[0].source_url}"
        for task in workload.tasks[:3]
        if task.evidence
    ]


def _decision_packet(
    candidate: DiscoveredHost,
    observation: SiteObservation,
    ads: AdIntelligence,
    events: Sequence[BuyingSignalEvent],
    opportunity: OpportunityVector,
    next_scan: NextBestScan,
    archetype: str,
    now: datetime | None = None,
) -> SalesDecisionPacket:
    action = _action(opportunity, observation)
    # The caller has already decayed this projection against its explicit reference time.
    # Re-decaying against wall-clock time would make historical replays non-deterministic.
    current_events = list(events)
    timing_events = sorted(
        (
            event
            for event in current_events
            if event.family == "commercial_timing"
            and event.polarity == "positive"
            and event.epistemic_status == "observed"
            and event.current_strength >= 20
            and event.confidence != "low"
            and event.event_type != "generic_site_change_since_prior_observation"
        ),
        key=lambda item: item.current_strength,
        reverse=True,
    )
    why_event = timing_events[0] if timing_events else None

    def current_context_event(*event_types: str) -> BuyingSignalEvent | None:
        candidates = [
            event
            for event in current_events
            if event.event_type in event_types
            and event.polarity == "positive"
            and event.epistemic_status == "observed"
            and event.current_strength > 0
            and event.source_url
        ]
        return max(candidates, key=lambda item: item.current_strength, default=None)

    release_context_event = current_context_event(
        "frontier_product_release_current",
        "frontier_product_release_engine",
        "frontier_app_store_release",
    )
    event_context_event = current_context_event(
        "frontier_event_upcoming",
        "frontier_event_program_engine",
    )
    trust_context_event = current_context_event("frontier_enterprise_trust_readiness")
    measurement_context_event = current_context_event("frontier_public_funnel_schema")
    hiring_context_event = current_context_event(
        "frontier_creative_hiring_surface",
        "frontier_workforce_hiring_surface",
    )
    blog_event = next(
        (
            event
            for event in current_events
            if event.event_type == "frontier_corporate_blog_engine"
            and event.polarity == "positive"
            and event.current_strength > 0
        ),
        None,
    )
    outreach_context_event = (
        why_event
        or release_context_event
        or event_context_event
        or measurement_context_event
        or hiring_context_event
        or blog_event
        or trust_context_event
    )
    observed_positive = sorted(
        (
            event
            for event in current_events
            if event.polarity == "positive"
            and event.epistemic_status == "observed"
            and event.current_strength > 0
            and event.source_url
            and event.observed_at
        ),
        key=lambda item: item.current_strength,
        reverse=True,
    )
    proof_events: list[BuyingSignalEvent] = []
    seen_families: set[SignalFamily] = set()
    for event in observed_positive:
        if event.family in seen_families:
            continue
        seen_families.add(event.family)
        proof_events.append(event)
        if len(proof_events) == 3:
            break
    proof_points = [
        DecisionProofPoint(
            statement=event.summary,
            source_url=event.source_url,
            observed_at=event.observed_at,
            confidence=event.confidence,
            event_key=event.event_key,
        )
        for event in proof_events
    ]
    company = (observation.title or candidate.label_hint or candidate.hostname).strip()
    why_now = (
        why_event.summary
        if why_event
        else "No time-sensitive public trigger is established by the current evidence."
    )
    production_support = {
        event.family
        for event in current_events
        if event.polarity == "positive"
        and event.current_strength >= 20
        and event.family in {"distribution_investment", "recurring_workload", "production_pressure"}
    }
    if len(production_support) >= 2 and "production_pressure" in production_support:
        conditional_gap = (
            "Independent public distribution, workload, or capacity evidence suggests production "
            "may be struggling to keep pace; that remains a hypothesis to test in discovery."
        )
    else:
        conditional_gap = (
            "Current public evidence does not establish a production-capacity gap; verify the "
            "recurring queue and internal capacity before proposing a lane."
        )
    if opportunity.timing >= 65:
        contact_window = "Within seven days while the observed commercial trigger is still fresh."
    elif opportunity.timing >= 40:
        contact_window = (
            "Within 30 days, subject to confirming the observed trigger remains active."
        )
    else:
        contact_window = "No immediate contact window is established; refresh the missing signal."

    if why_event and why_event.event_type in {
        "frontier_product_release_current",
        "frontier_app_store_release",
    }:
        email_subject = "release creative queue"
        release_signal = (
            "your public release surface lists a recent substantive product release"
            if why_event.event_type == "frontier_product_release_current"
            else "a first-party-linked app-store record reports a recent dated update"
        )
        call_opener = (
            f"The reason I called is that I noticed {release_signal}. That does not tell me "
            "whether "
            "the release has created a production constraint. If an associated photo and video "
            "queue is confirmed, would a dedicated specialist lane be useful?"
        )
        email_angle = (
            f"I noticed {release_signal}. That does not establish adoption, performance, or a "
            "capacity gap, but it creates a concrete question: is there a confirmed queue of "
            "release-related photo and video work? Beings provides one specialist-led monthly lane "
            "for recurring production. Worth comparing the next few approved deliverables?"
        )
    elif why_event and why_event.event_type == "frontier_event_upcoming":
        email_subject = "event content queue"
        call_opener = (
            "The reason I called is that I noticed your public event surface lists a non-cancelled "
            "event in the next three to 45 days. That does not establish attendance, demand, or a "
            "production gap. If the event asset and follow-up queue is confirmed, would a "
            "dedicated "
            "photo and video lane be useful?"
        )
        email_angle = (
            "I noticed your public event surface lists a non-cancelled event in the next three to "
            "45 days. The listing does not establish attendance, pipeline, success, or a capacity "
            "gap. If there is a confirmed queue for event promotion and approved follow-up assets, "
            "Beings provides one specialist-led monthly photo and video lane. Worth comparing the "
            "next deliverables?"
        )
    elif why_event and why_event.event_type == "frontier_creative_hiring_current":
        email_subject = "creative production queue"
        call_opener = (
            "The reason I called is that your public careers surface lists recently dated "
            "creative openings. The listings do not tell me whether anyone was hired, whether "
            "the work is urgent, or whether any work belongs outside the team. Is there a "
            "confirmed photo or video queue the new roles are expected to support?"
        )
        email_angle = (
            "I noticed your public careers surface lists recently dated creative openings. "
            "That does not establish completed hiring, urgency, budget, or a production gap. "
            "If there is a confirmed photo and video queue behind those roles, Beings provides "
            "one specialist-led monthly lane. Worth comparing the next approved deliverables?"
        )
    elif why_event:
        email_subject = "campaign production"
        call_opener = (
            f"The reason I called is that I noticed {why_event.summary.rstrip('.').lower()}. "
            "That may create more photo and video work than the team wants to carry internally, "
            "although I do not know if that is true. Would it be useful to compare the next "
            "production queue with a dedicated specialist lane?"
        )
        email_angle = (
            f"I noticed {why_event.summary.rstrip('.').lower()}. That can create a lot of photo "
            "and video versions, although I do not know whether production is a constraint for "
            "your team. Beings provides one specialist-led lane for recurring photo and video "
            "work. Worth comparing notes on the next campaign queue?"
        )
    elif release_context_event:
        email_subject = "release creative queue"
        call_opener = (
            "I noticed your public release surface shows a sustained cadence of substantive dated "
            "product updates. That does not tell me whether those updates create photo or video "
            "work, or whether production is constrained. Is there a confirmed recurring release "
            "asset queue worth comparing with a dedicated specialist lane?"
        )
        email_angle = (
            "I noticed your public release surface shows a sustained cadence of substantive dated "
            "product updates. That does not establish adoption, success, or a capacity gap. If "
            "each "
            "approved release creates recurring photo and video asks, Beings provides one "
            "specialist-led monthly production lane. Is there a current release asset queue worth "
            "comparing?"
        )
    elif event_context_event:
        email_subject = "event content queue"
        call_opener = (
            "I noticed your public event surface shows a recurring program of explicitly dated "
            "events across multiple months. That does not establish attendance, demand, or a "
            "production gap. Does the program create a confirmed recurring queue for promotion, "
            "session assets, or approved follow-up content?"
        )
        email_angle = (
            "I noticed your public event surface shows a recurring program of explicitly dated "
            "events across multiple months. That does not establish attendance, pipeline, success, "
            "or a capacity gap. If it creates a confirmed recurring photo and video queue, Beings "
            "provides one specialist-led monthly production lane. Worth comparing the next few "
            "approved event deliverables?"
        )
    elif measurement_context_event:
        email_subject = "creative testing queue"
        call_opener = (
            "I noticed saved first-party page code exposes a public funnel-event schema spanning "
            "media or experiment and conversion families. That does not show that any call fired, "
            "experiment ran, ad is active, or conversion occurred. If it corresponds to a "
            "confirmed "
            "creative testing queue, would a dedicated photo and video lane be useful?"
        )
        email_angle = (
            "I noticed saved first-party page code exposes a public funnel-event schema spanning "
            "media or experiment and conversion families. That does not establish active ads, "
            "experiments, conversions, performance, or a capacity gap. If there is a confirmed "
            "creative testing queue behind it, Beings provides one specialist-led monthly photo "
            "and "
            "video lane. Worth comparing the next approved test assets?"
        )
    elif hiring_context_event:
        email_subject = "creative production queue"
        call_opener = (
            "I noticed your public careers surface lists current creative openings. That does "
            "not tell me whether hiring is complete, whether production is constrained, or "
            "whether work belongs outside the team. Is there a confirmed photo or video queue "
            "those roles are expected to support?"
        )
        email_angle = (
            "I noticed your public careers surface lists current creative openings. The public "
            "listings do not establish urgency, budget, or a capacity gap. If there is a "
            "confirmed recurring photo and video queue behind them, Beings provides one "
            "specialist-led monthly production lane. Worth comparing the next deliverables?"
        )
    elif blog_event:
        email_subject = "editorial-to-video production"
        blog_signal = blog_event.summary.split("This demonstrates", 1)[0].strip().rstrip(".")
        call_opener = (
            f"I noticed {blog_signal.lower()}. I cannot tell from that whether production is "
            "a constraint, but is the team turning each editorial brief into the photo, motion, "
            "and video variants it could use across channels?"
        )
        email_angle = (
            f"I noticed {blog_signal.lower()}. That does not tell me whether you have a capacity "
            "gap, but it does create a concrete repurposing question: are those editorial briefs "
            "also becoming channel-ready photo and video variants? Beings provides one "
            "specialist-led monthly lane for that recurring derivative work. Worth comparing "
            "the next few briefs?"
        )
    elif trust_context_event:
        email_subject = "enterprise content workflow"
        call_opener = (
            "I noticed your explicitly linked public trust or assurance center exposes several "
            "enterprise procurement and control families. That does not verify certification, "
            "security quality, budget, or a production gap. If enterprise-facing education or "
            "sales "
            "assets recur, is there a confirmed photo and video queue worth discussing?"
        )
        email_angle = (
            "I noticed your public trust or assurance center exposes several enterprise "
            "procurement and control families. That does not verify certification, security "
            "quality, customers, budget, or a capacity gap. If enterprise-facing education or "
            "sales enablement creates a "
            "confirmed recurring visual-content queue, Beings provides one specialist-led monthly "
            "photo and video lane. Worth comparing the next approved deliverables?"
        )
    else:
        email_subject = "photo and video queue"
        call_opener = (
            f"I have been researching {company}. The public evidence does not establish an "
            "immediate production bottleneck, so I am calling only to understand whether a "
            "recurring photo and video queue exists before suggesting anything."
        )
        email_angle = (
            f"I have been researching {company}, but the public evidence does not establish an "
            "immediate production bottleneck. Beings runs a specialist-led photo and video lane "
            "for teams with recurring campaign queues. Is there a current queue worth learning "
            "about, or is the timing off?"
        )
    public_people = _public_people(observation)
    cited_context = [event for event in (why_event, outreach_context_event) if event is not None]
    cited_events = list(
        {event.event_key: event for event in [*cited_context, *proof_events]}.values()
    )
    evidence_urls = list(
        dict.fromkeys(event.source_url for event in cited_events if event.source_url)
    )
    evidence_dates = {
        event.source_url: _aware(event.effective_at) or _aware(event.observed_at)
        for event in cited_events
        if event.source_url and (_aware(event.effective_at) or _aware(event.observed_at))
    }
    if why_event and why_event.source_url:
        why_date = _aware(why_event.effective_at) or _aware(why_event.observed_at)
        if why_date is not None:
            evidence_dates[why_event.source_url] = why_date
    risks_corpus = " ".join(opportunity.risks).lower()
    if "rights" in risks_corpus or "ai" in risks_corpus:
        likely_objection = "AI-generated work may not satisfy our brand, rights, or fidelity rules."
        safe_response = (
            "Treat AI permissibility, rights, likeness, and fidelity as discovery gates. Do not "
            "propose production until the approver confirms the acceptable lane."
        )
    elif "internal" in risks_corpus:
        likely_objection = "We already have an internal creative or production team."
        safe_response = (
            "Position this only as a bounded specialist or overflow lane, and withdraw the idea if "
            "there is no recurring backlog the internal team wants to externalize."
        )
    else:
        likely_objection = "We may not have enough recurring work for a dedicated monthly lane."
        safe_response = (
            "Do not argue with that. Validate three immediate asks and a three-month queue before "
            "discussing a subscription."
        )
    do_not_claim = [
        "Do not claim verified revenue, ad spend, budget, purchase intent, or ROI.",
        "Do not state that production is constrained; present it as a hypothesis to test.",
        "Do not claim creative fatigue or performance from public creative counts.",
        "Do not treat a public person or contact route as a confirmed decision maker.",
        "Do not assume AI, likeness, rights, fidelity, or brand-policy approval.",
    ]
    current_types = {event.event_type for event in current_events if event.current_strength > 0}
    if current_types & {
        "frontier_product_release_engine",
        "frontier_product_release_current",
        "frontier_app_store_release",
    }:
        do_not_claim.append(
            "Do not infer adoption, launch success, users, revenue, staffing, budget, or roadmap "
            "truth from a public release surface."
        )
    if current_types & {"frontier_event_program_engine", "frontier_event_upcoming"}:
        do_not_claim.append(
            "Do not infer attendance, demand, pipeline, event success, or spend from an event "
            "listing or registration surface."
        )
    if "frontier_enterprise_trust_readiness" in current_types:
        do_not_claim.append(
            "Do not infer certification validity, customers, company size, security quality, or "
            "budget from a public trust surface."
        )
    if "frontier_public_funnel_schema" in current_types:
        do_not_claim.append(
            "Do not claim that a visible measurement call fired, an experiment ran, ads are "
            "active, "
            "or conversions occurred."
        )
    if "frontier_seo_operating_footprint" in current_types:
        do_not_claim.append(
            "Do not infer a search ranking, organic traffic, demand, revenue, available budget, "
            "or purchase intent from static page-level SEO readiness."
        )
    if current_types & {
        "vendor_dataforseo_search_visibility_estimate",
        "vendor_dataforseo_domain_rank_overview",
    }:
        do_not_claim.append(
            "Do not present DataForSEO modeled traffic or ranking coverage as first-party "
            "analytics, creative demand, revenue, available budget, or purchase intent."
        )
    if current_types & {
        "frontier_workforce_hiring_surface",
        "frontier_creative_hiring_surface",
        "frontier_creative_hiring_current",
    }:
        do_not_claim.append(
            "Do not infer completed hiring, employee count, headcount growth, workload, urgency, "
            "available budget, outsourcing intent, or purchase intent from public openings."
        )
    questions = [
        "Which photo and video deliverables must ship in the next 30, 60, and 90 days?",
        "Where does the path from approved concept to usable channel-ready asset slow down?",
        "Who owns budget, brand approval, rights, and the final decision for external production?",
    ]
    asks = _first_month_asks(observation, ads, archetype, now=now)
    ready = bool(
        why_event and len(proof_points) >= 2 and evidence_urls and action != "deprioritize"
    )
    if len(email_angle.split()) > 100:
        # Source excerpts can be arbitrarily long. Narrative validation must not
        # discard a company's score or its complete, separately retained evidence.
        email_angle = (
            "I noticed a dated public activity signal from your team. That does not establish "
            "a production constraint, approved budget, or purchase intent. Is there a confirmed "
            "recurring photo and video queue worth comparing with a dedicated monthly "
            "production lane?"
        )
    return SalesDecisionPacket(
        ready=ready,
        action=action,
        contact_window=contact_window,
        why_now=why_now,
        why_now_event_key=why_event.event_key if why_event else "",
        why_now_source_url=why_event.source_url if why_event else None,
        why_now_observed_at=why_event.observed_at if why_event else None,
        conditional_gap=conditional_gap,
        proof_points=proof_points,
        buyer_roles=_buyer_roles(archetype, public_people, evidence_urls, observation),
        public_people=public_people,
        call_opener=call_opener,
        email_subject=email_subject,
        email_angle=email_angle,
        first_month_asks=asks,
        discovery_questions=questions,
        likely_objection=likely_objection,
        safe_response=safe_response,
        do_not_claim=do_not_claim,
        next_research_action=next_scan,
        missing_gaps=opportunity.unknown_dimensions,
        evidence_urls=evidence_urls,
        evidence_dates=evidence_dates,
    )


def _derived_signals(
    events: Sequence[BuyingSignalEvent],
    clusters: Sequence[SignalCluster],
    archetype: str,
    *,
    placement_ready: bool,
) -> tuple[DerivedSignalName, ...]:
    by_family = _cluster_map(clusters)
    distribution = _positive(by_family, "distribution_investment")
    recurring = _positive(by_family, "recurring_workload")
    production = _positive(by_family, "production_pressure")
    types = {event.event_type for event in events if event.current_strength > 0}
    signals: list[DerivedSignalName] = []
    if distribution >= 25 and recurring >= 25 and production >= 25:
        signals.append("distribution_production_mismatch")
    if "paid_format_video_gap" in types or "first_party_static_heavy_mix" in types:
        signals.extend(("video_opportunity_gap", "format_debt"))
    if "covered_low_paid_refresh" in types:
        signals.append("content_exhaust")
    if {
        "paid_creative_launch_7d",
        "paid_creative_launch_30d",
        "recent_first_party_commercial_feed",
    } & types and production >= 25:
        signals.append("launch_collision")
    if {
        "explicit_external_capacity_model",
        "first_party_external_capacity_offer",
    } & types:
        signals.extend(("agency_external_capacity_model", "production_avoidance_opportunity"))
    scoped_procurement = any(
        event.current_strength > 0
        and event.event_type == "hydrated_workload_feature"
        and event.family == "production_pressure"
        and event.source_kind == "saved_sales_evidence"
        and event.coverage == "complete"
        and event.confidence == "high"
        and event.source_url
        and event.details.get("kind") == "recurring_work_with_external_request"
        and event.details.get("work_scope", "").strip()
        and event.details.get("independent_cycles", "").isdigit()
        and int(event.details["independent_cycles"]) >= 2
        for event in events
    )
    if archetype == "agency" and (
        (placement_ready and "agency_client_portfolio" in types and recurring >= 25)
        or scoped_procurement
    ):
        signals.append("agency_buyer_candidate")
    return tuple(dict.fromkeys(signals))


def synthesize_revenue_intelligence(
    candidate: DiscoveredHost,
    observation: SiteObservation,
    *,
    previous_observation: SiteObservation | None = None,
    ad_intelligence: AdIntelligence | None = None,
    evidence: RevenueSynthesisEvidence | None = None,
    historical_events: Sequence[BuyingSignalEvent] = (),
    now: datetime | None = None,
    trace: bool = True,
) -> RevenueIntelligenceResult:
    """Build deterministic conversion intelligence from existing public evidence.

    ``RevenueSynthesisEvidence`` is the integration boundary for dimensions that scoring has
    already computed. Values remain bounded ranking indices, not financial facts or conversion
    probabilities. Historical events are optional and deduplicated by stable key. No field returned
    here can establish the manual qualification gates in ``StrategicQualification``.
    """

    context = evidence or RevenueSynthesisEvidence()
    ads = ad_intelligence or AdIntelligence()
    frontier_accounting = {}
    generated = _collect_events(
        candidate,
        observation,
        previous_observation=previous_observation,
        ads=ads,
        evidence=context,
        accounting=frontier_accounting,
    )
    # Ad receipts have their own observation clock. A later website scan must not
    # freshen an old advertising observation or its recurrence/format hypotheses.
    ad_stamp = _aware(ads.last_observed_at)
    if ad_stamp:
        generated = [e.model_copy(update={
            "observed_at": ad_stamp,
            "effective_at": ad_stamp if e.effective_at == e.observed_at else e.effective_at,
            "expires_at": (ad_stamp + (e.expires_at - e.observed_at)
                           if e.expires_at and e.effective_at == e.observed_at else e.expires_at),
        }) if e.source_kind == "ad_library" else e for e in generated]
    generated = [historical_operating_context(e, now or datetime.now(UTC)) for e in generated]
    # A workbench copy of an existing observation is the same causal evidence.
    # Preserve distinct sources, but never award a second independent path for the same URL/family.
    roots = {(event.family, event.source_url): event.independence_key for event in generated}
    additional = [
        event.model_copy(
            update={
                "independence_key": roots.get(
                    (event.family, event.source_url), event.independence_key
                )
            }
        )
        if event.event_key.startswith("hydrated:")
        else event
        for event in historical_events
    ]
    from .buyer_intelligence import buyer_events

    buyer_evidence = [*observation.buyer_evidence,
                      *(r for page in observation.pages for r in page.buyer_evidence)]
    generated.extend(buyer_events(
        observation.canonical_host or observation.hostname, buyer_evidence,
        [*generated, *additional], now=now or datetime.now(UTC),
        paid_asset_keys=observation.paid_creative_asset_keys,
    ))
    merged = _merge_events([*additional, *generated])
    decayed = [decay_event(event, now=now) for event in merged]
    clusters = cluster_signal_events(decayed, now=now)
    opportunity = _opportunity_vector(observation, clusters, context, trace=trace)
    opportunity.evidence_accounting = [
        {"input_key": "atomic:" + key, "reason": reason}
        for key, reason in sorted(frontier_accounting.items())
    ]
    group_keys = sorted(
        {(e.family, e.independence_key or e.event_key) for e in decayed if e.current_strength > 0}
    )
    for family, root in group_keys if trace else ():
        remaining = [
            e for e in decayed if (e.family, e.independence_key or e.event_key) != (family, root)
        ]
        removed = _opportunity_vector(
            observation, cluster_signal_events(remaining, now=now), context, trace=False
        )
        opportunity.evidence_group_effects[f"{family}|{root}"] = {
            name: getattr(opportunity, name) - getattr(removed, name)
            for name in (
                "fit",
                "need",
                "timing",
                "ability",
                "access",
                "proof",
                "evidence_quality",
                "risk",
                "uncertainty",
                "durable_fit",
                "contact_now",
                "retained_value_proxy",
                "founder_priority",
            )
        }
    next_scan = _next_best_scan(opportunity, context.archetype, observation)
    packet = _decision_packet(
        candidate,
        observation,
        ads,
        decayed,
        opportunity,
        next_scan,
        context.archetype,
        now=now,
    )
    return RevenueIntelligenceResult(
        signal_events=tuple(decayed),
        signal_clusters=tuple(clusters),
        opportunity=opportunity,
        decision_packet=packet,
        next_best_scan=next_scan,
        derived_signals=_derived_signals(
            decayed,
            clusters,
            context.archetype,
            placement_ready=context.placement_ready,
        ),
    )
