from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _deduplicate_strings(cls, value: list[str]) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in value if item.strip()))


EpistemicStatus = Literal["observed", "inferred", "unknown"]
TruthState = Literal["met", "not_met", "unknown"]
ConfidenceLevel = Literal["low", "medium", "high"]
QualificationState = Literal["pursue", "needs_research", "disqualified"]

PrincipalSignalName = Literal[
    "distribution_investment",
    "recurring_creative_demand",
    "production_capacity_gap",
]
QualificationGateName = Literal[
    "recurring_work_3_months",
    "three_immediate_asks",
    "economic_buyer",
    "ai_permissible",
    "one_specialist_lane",
    "annual_capacity_60k",
]
CreativeCapacityDimensionName = Literal[
    "distribution_investment",
    "recurring_creative_demand",
    "production_capacity_gap",
    "ai_brand_readiness",
    "timing_access",
]
DerivedSignalName = Literal[
    "distribution_production_mismatch",
    "concept_entropy_gap",
    "video_opportunity_gap",
    "content_exhaust",
    "launch_collision",
    "format_debt",
    "ai_taste_gap",
    "production_avoidance_opportunity",
    "agency_buyer_candidate",
    "agency_external_capacity_model",
]
SignalFamily = Literal[
    "distribution_investment",
    "recurring_workload",
    "production_pressure",
    "commercial_timing",
    "ability_proxy",
    "buyer_access",
    "proof_match",
    "risk",
    "coverage",
]
SignalPolarity = Literal["positive", "negative", "neutral"]
CoverageState = Literal["unknown", "partial", "complete", "failed"]
RevenueAction = Literal[
    "contact_now",
    "research_next",
    "watch",
    "nurture",
    "deprioritize",
]
# Scanner ids are versioned backend capabilities. Keeping this as a closed Literal made every
# additive scanner a breaking model change, so validate the stable identifier shape instead while
# preserving every legacy value.
NextBestScan = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]


class HypothesisClaim(BaseModel):
    """A machine-produced statement that can never represent human verification."""

    claim: str = Field(min_length=1)
    epistemic_status: EpistemicStatus
    truth_state: TruthState = "unknown"
    signal: PrincipalSignalName | None = None
    dimension: CreativeCapacityDimensionName | None = None
    source_url: str | None = None
    observed_at: datetime | None = None
    confidence: ConfidenceLevel = "low"
    human_verified: Literal[False] = False


class HypothesisSignal(BaseModel):
    truth_state: TruthState = "unknown"
    epistemic_status: EpistemicStatus = "unknown"
    confidence: ConfidenceLevel = "low"
    summary: str = ""
    evidence_urls: list[str] = Field(default_factory=list)
    human_verified: Literal[False] = False


class HypothesisPrincipalSignals(BaseModel):
    distribution_investment: HypothesisSignal = Field(default_factory=HypothesisSignal)
    recurring_creative_demand: HypothesisSignal = Field(default_factory=HypothesisSignal)
    production_capacity_gap: HypothesisSignal = Field(default_factory=HypothesisSignal)


class BuyingSignalEvent(BaseModel):
    """Atomic, time-aware public evidence used only for machine prioritization."""

    event_key: str = ""
    subject_key: str = ""
    event_type: str = ""
    family: SignalFamily = "coverage"
    polarity: SignalPolarity = "neutral"
    strength: int = Field(default=0, ge=0, le=100)
    current_strength: float = Field(default=0.0, ge=0.0, le=100.0)
    confidence: ConfidenceLevel = "low"
    coverage: CoverageState = "unknown"
    epistemic_status: EpistemicStatus = "unknown"
    source_kind: str = ""
    source_url: str | None = None
    observed_at: datetime | None = None
    effective_at: datetime | None = None
    expires_at: datetime | None = None
    refresh_at: datetime | None = None
    half_life_days: float = Field(default=30.0, gt=0.0, le=3650.0)
    independence_key: str = ""
    summary: str = ""
    details: dict[str, str] = Field(default_factory=dict)
    human_verified: Literal[False] = False

    @model_validator(mode="after")
    def require_provenance_for_observed_evidence(self) -> BuyingSignalEvent:
        if self.epistemic_status == "observed" and self.strength:
            if not self.event_key.strip():
                raise ValueError("observed signal events require a stable event_key")
            if not (self.source_url or "").strip() or self.observed_at is None:
                raise ValueError("observed signal events require source_url and observed_at")
            if not self.summary.strip():
                raise ValueError("observed signal events require a summary")
        if self.expires_at and self.effective_at and self.expires_at < self.effective_at:
            raise ValueError("signal event expiry cannot precede its effective time")
        return self


class SignalCluster(BaseModel):
    """A capped family-level synthesis that prevents correlated double-counting."""

    cluster_key: str = ""
    family: SignalFamily = "coverage"
    event_keys: list[str] = Field(default_factory=list)
    positive_strength: int = Field(default=0, ge=0, le=100)
    negative_strength: int = Field(default=0, ge=0, le=100)
    net_strength: int = Field(default=0, ge=-100, le=100)
    family_cap: int = Field(default=100, ge=0, le=100)
    event_count: int = Field(default=0, ge=0)
    independent_evidence_count: int = Field(default=0, ge=0)
    corroboration_count: int = Field(default=0, ge=0)
    confidence: ConfidenceLevel = "low"
    coverage: CoverageState = "unknown"
    summary: str = ""
    positive_summary: str = ""
    negative_summary: str = ""
    evidence_urls: list[str] = Field(default_factory=list)
    observed_dates: list[datetime] = Field(default_factory=list)
    human_verified: Literal[False] = False

    deduplicate_cluster_lists = field_validator("event_keys", "evidence_urls")(
        classmethod(_deduplicate_strings)
    )


class OpportunityVector(BaseModel):
    """Separate evidence dimensions; values are ranking indices, never purchase probabilities."""

    version: Literal["revenue-opportunity-v1"] = "revenue-opportunity-v1"
    policy_version: str = "legacy-unversioned"
    family_effects: dict[str, dict[str, int]] = Field(default_factory=dict)
    evidence_group_effects: dict[str, dict[str, int]] = Field(default_factory=dict)
    source_effects: dict[str, dict[str, int]] = Field(default_factory=dict)
    evidence_accounting: list[dict] = Field(default_factory=list)
    evidence_accounting_summary: dict = Field(default_factory=dict)
    freshness_audit: dict = Field(default_factory=dict)
    fit: int = Field(default=0, ge=0, le=100)
    need: int = Field(default=0, ge=0, le=100)
    timing: int = Field(default=0, ge=0, le=100)
    ability: int = Field(default=0, ge=0, le=100)
    access: int = Field(default=0, ge=0, le=100)
    proof: int = Field(default=0, ge=0, le=100)
    evidence_quality: int = Field(default=0, ge=0, le=100)
    risk: int = Field(default=0, ge=0, le=100)
    uncertainty: int = Field(default=100, ge=0, le=100)
    durable_fit: int = Field(default=0, ge=0, le=100)
    contact_now: int = Field(default=0, ge=0, le=100)
    founder_priority: int = Field(default=0, ge=0, le=100)
    retained_value_proxy: int = Field(default=0, ge=0, le=100)
    confidence: ConfidenceLevel = "low"
    known_dimensions: list[str] = Field(default_factory=list)
    unknown_dimensions: list[str] = Field(
        default_factory=lambda: [
            "fit",
            "need",
            "timing",
            "ability",
            "access",
            "proof",
        ]
    )
    reasons: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    dimension_event_keys: dict[str, list[str]] = Field(default_factory=dict)
    manual_qualification_required: Literal[True] = True
    human_verified: Literal[False] = False

    deduplicate_opportunity_lists = field_validator(
        "known_dimensions", "unknown_dimensions", "reasons", "risks"
    )(classmethod(_deduplicate_strings))


class PublicPersonReference(BaseModel):
    """A public role reference; it never implies a private address or outreach permission."""

    name: str = ""
    role: str = ""
    company: str = ""
    source_url: str | None = None
    profile_url: str | None = None
    observed_at: datetime | None = None
    confidence: ConfidenceLevel = "low"
    current_status: Literal["unknown", "current_public", "historical"] = "unknown"
    buyer_role_hint: Literal["economic_buyer", "champion", "delivery_risk", "unknown"] = "unknown"
    outreach_eligible: Literal[False] = False
    human_verified: Literal[False] = False

    @model_validator(mode="after")
    def require_public_person_provenance(self) -> PublicPersonReference:
        if self.name.strip() and (
            not self.role.strip() or not (self.source_url or "").strip() or self.observed_at is None
        ):
            raise ValueError("named public people require role, source_url, and observed_at")
        return self


class BuyerRoleHypothesis(BaseModel):
    """A role to verify in discovery, optionally supported by cited public people."""

    role_type: Literal["champion", "economic_buyer", "approver", "implementer", "hidden_buyer"] = (
        "champion"
    )
    role: str = ""
    rationale: str = ""
    epistemic_status: EpistemicStatus = "unknown"
    confidence: ConfidenceLevel = "low"
    evidence_urls: list[str] = Field(default_factory=list)
    public_people: list[PublicPersonReference] = Field(default_factory=list)
    human_verified: Literal[False] = False

    deduplicate_buyer_evidence = field_validator("evidence_urls")(
        classmethod(_deduplicate_strings)
    )


class DecisionProofPoint(BaseModel):
    statement: str = ""
    source_url: str | None = None
    observed_at: datetime | None = None
    confidence: ConfidenceLevel = "low"
    event_key: str = ""

    @model_validator(mode="after")
    def require_proof_provenance(self) -> DecisionProofPoint:
        if self.statement.strip() and (
            not (self.source_url or "").strip() or self.observed_at is None
        ):
            raise ValueError("decision proof points require source_url and observed_at")
        return self


class SalesDecisionPacket(BaseModel):
    """Evidence-safe founder preparation; it is never an autonomous outreach instruction."""

    version: Literal["sales-decision-packet-v1"] = "sales-decision-packet-v1"
    ready: bool = False
    action: RevenueAction = "research_next"
    contact_window: str = ""
    why_now: str = ""
    why_now_event_key: str = ""
    why_now_source_url: str | None = None
    why_now_observed_at: datetime | None = None
    conditional_gap: str = ""
    proof_points: list[DecisionProofPoint] = Field(default_factory=list, max_length=3)
    buyer_roles: list[BuyerRoleHypothesis] = Field(default_factory=list, max_length=5)
    public_people: list[PublicPersonReference] = Field(default_factory=list, max_length=5)
    call_opener: str = ""
    email_subject: str = ""
    email_angle: str = ""
    first_month_asks: list[str] = Field(default_factory=list, max_length=3)
    discovery_questions: list[str] = Field(default_factory=list, max_length=3)
    likely_objection: str = ""
    safe_response: str = ""
    do_not_claim: list[str] = Field(default_factory=list)
    next_research_action: NextBestScan = "none"
    missing_gaps: list[str] = Field(default_factory=list)
    evidence_urls: list[str] = Field(default_factory=list)
    evidence_dates: dict[str, datetime] = Field(default_factory=dict)
    human_verified: Literal[False] = False

    deduplicate_decision_lists = field_validator(
        "first_month_asks", "discovery_questions", "do_not_claim", "missing_gaps", "evidence_urls"
    )(classmethod(_deduplicate_strings))

    @model_validator(mode="after")
    def enforce_safe_packet_shape(self) -> SalesDecisionPacket:
        if len(self.email_angle.split()) > 100:
            raise ValueError("decision packet email_angle must not exceed 100 words")
        if self.ready:
            if len(self.discovery_questions) != 3:
                raise ValueError("ready decision packets require exactly three discovery questions")
            if (
                not self.why_now.strip()
                or not self.call_opener.strip()
                or not self.email_angle.strip()
            ):
                raise ValueError("ready decision packets require why-now, call, and email language")
            if not self.evidence_urls:
                raise ValueError("ready decision packets require evidence URLs")
            if (
                not self.why_now_event_key.strip()
                or not (self.why_now_source_url or "").strip()
                or self.why_now_observed_at is None
            ):
                raise ValueError("ready decision packets require cited why-now evidence")
        return self


class CreativeCapacityHypothesis(BaseModel):
    company_role: dict = Field(default_factory=dict)

    """Conservative scanner/AI research, never a manual pursuit decision."""

    version: Literal[
        "creative-capacity-v1-provisional",
        "creative-capacity-v2-provisional",
        "creative-capacity-v3-provisional",
        "creative-capacity-v4-provisional",
    ] = "creative-capacity-v4-provisional"
    total: int = Field(default=0, ge=0, le=100)
    distribution_investment: int = Field(default=0, ge=0, le=25)
    recurring_creative_demand: int = Field(default=0, ge=0, le=20)
    production_capacity_gap: int = Field(default=0, ge=0, le=25)
    ai_brand_readiness: int = Field(default=0, ge=0, le=15)
    timing_access: int = Field(default=0, ge=0, le=15)
    principal_signals: HypothesisPrincipalSignals = Field(
        default_factory=HypothesisPrincipalSignals
    )
    derived_signals: list[DerivedSignalName] = Field(default_factory=list)
    observations: list[HypothesisClaim] = Field(default_factory=list)
    inferences: list[HypothesisClaim] = Field(default_factory=list)
    signal_events: list[BuyingSignalEvent] = Field(default_factory=list)
    signal_clusters: list[SignalCluster] = Field(default_factory=list)
    opportunity: OpportunityVector = Field(default_factory=OpportunityVector)
    decision_packet: SalesDecisionPacket = Field(default_factory=SalesDecisionPacket)
    next_best_scan: NextBestScan = "none"
    review_ready: bool = False
    blockers: list[str] = Field(default_factory=list)
    human_verified: Literal[False] = False

    @model_validator(mode="after")
    def derive_total_and_enforce_epistemics(self) -> CreativeCapacityHypothesis:
        self.total = (
            self.distribution_investment
            + self.recurring_creative_demand
            + self.production_capacity_gap
            + self.ai_brand_readiness
            + self.timing_access
        )
        self.review_ready = bool(
            self.total >= 70
            and all(signal.truth_state == "met" for _, signal in self.principal_signals)
        )
        if any(item.epistemic_status != "observed" for item in self.observations):
            raise ValueError("hypothesis observations must be labeled observed")
        if any(item.epistemic_status != "inferred" for item in self.inferences):
            raise ValueError("hypothesis inferences must be labeled inferred")
        return self


class EvidenceClaim(BaseModel):
    """Claim-level provenance retained independently from scanner snapshots."""

    id: int | None = Field(default=None, ge=1)
    account_id: int | None = Field(default=None, ge=1)
    signals: list[PrincipalSignalName] = Field(default_factory=list)
    gates: list[QualificationGateName] = Field(default_factory=list)
    dimensions: list[CreativeCapacityDimensionName] = Field(default_factory=list)
    claim: str = Field(min_length=1)
    epistemic_status: EpistemicStatus
    truth_state: TruthState = "unknown"
    source_kind: Literal[
        "company_site",
        "ad_library",
        "social_feed",
        "career_page",
        "press_release",
        "public_directory",
        "discovery_call",
        "internal_history",
        "warm_introduction",
        "operator_observation",
        "other",
    ]
    source_url: str | None = None
    observed_at: datetime
    confidence: ConfidenceLevel = "low"
    human_verified: bool = False
    verified_by: str = ""
    verified_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    @field_validator("signals", "gates", "dimensions")
    @classmethod
    def deduplicate_coverage(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def validate_verification(self) -> EvidenceClaim:
        public_sources = {
            "company_site",
            "ad_library",
            "social_feed",
            "career_page",
            "press_release",
            "public_directory",
        }
        if self.source_kind in public_sources and not (self.source_url or "").strip():
            raise ValueError("public evidence claims require source_url")
        if self.human_verified and (not self.verified_by.strip() or self.verified_at is None):
            raise ValueError("human-verified claims require verified_by and verified_at")
        return self


class PrincipalSignalSet(BaseModel):
    distribution_investment: TruthState = "unknown"
    recurring_creative_demand: TruthState = "unknown"
    production_capacity_gap: TruthState = "unknown"

    def as_dict(self) -> dict[PrincipalSignalName, TruthState]:
        return self.model_dump()


class QualificationGateSet(BaseModel):
    recurring_work_3_months: TruthState = "unknown"
    three_immediate_asks: TruthState = "unknown"
    economic_buyer: TruthState = "unknown"
    ai_permissible: TruthState = "unknown"
    one_specialist_lane: TruthState = "unknown"
    annual_capacity_60k: TruthState = "unknown"

    def as_dict(self) -> dict[QualificationGateName, TruthState]:
        return self.model_dump()


class CreativeCapacityDimensions(BaseModel):
    """The exact creative-capacity-v1 25/20/25/15/15 axes."""

    distribution_investment: int = Field(default=0, ge=0, le=25)
    recurring_creative_demand: int = Field(default=0, ge=0, le=20)
    production_capacity_gap: int = Field(default=0, ge=0, le=25)
    ai_brand_readiness: int = Field(default=0, ge=0, le=15)
    timing_access: int = Field(default=0, ge=0, le=15)

    @property
    def total(self) -> int:
        return sum(self.model_dump().values())


class QualificationPenalty(BaseModel):
    name: str = Field(min_length=1)
    points: int = Field(ge=1, le=15)
    reason: str = Field(min_length=1)


class EconomicBuyer(BaseModel):
    name: str = ""
    role: str = Field(min_length=1)

    @field_validator("name", "role")
    @classmethod
    def normalize_identity(cls, value: str, info) -> str:
        normalized = value.strip()
        if info.field_name == "role" and not normalized:
            raise ValueError("economic buyer role cannot be blank")
        return normalized


class StrategicQualification(BaseModel):
    version: Literal["creative-capacity-v1"] = "creative-capacity-v1"
    dimensions: CreativeCapacityDimensions = Field(default_factory=CreativeCapacityDimensions)
    penalties: list[QualificationPenalty] = Field(default_factory=list, max_length=5)
    principal_signals: PrincipalSignalSet = Field(default_factory=PrincipalSignalSet)
    gates: QualificationGateSet = Field(default_factory=QualificationGateSet)
    confidence: ConfidenceLevel = "low"
    buyer: EconomicBuyer | None = None
    why_now: str = ""
    first_month_asks: list[str] = Field(default_factory=list, max_length=3)
    total_score: int = Field(default=0, ge=0, le=100)
    state: QualificationState = "needs_research"
    blockers: list[str] = Field(default_factory=list)
    evidence_coverage: dict[PrincipalSignalName, bool] = Field(default_factory=dict)

    @field_validator("first_month_asks")
    @classmethod
    def normalize_asks(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]


def _credible_asks(asks: Sequence[str]) -> list[str]:
    normalized = [ask.strip() for ask in asks if ask.strip()]
    return list(dict.fromkeys(normalized))


def evaluate_strategic_qualification(
    qualification: StrategicQualification,
    claims: Sequence[EvidenceClaim],
) -> StrategicQualification:
    """Derive score and pursuit state solely from typed human-controlled inputs."""

    # Callers can mutate Pydantic objects without assignment validation. Rebuilding from
    # serialized data makes the decision boundary validate every nested value again.
    qualification = StrategicQualification.model_validate(qualification.model_dump())
    claims = [EvidenceClaim.model_validate(claim.model_dump()) for claim in claims]

    total_score = max(
        0,
        min(
            100,
            qualification.dimensions.total
            - min(30, sum(item.points for item in qualification.penalties)),
        ),
    )
    signal_states = qualification.principal_signals.as_dict()
    gate_states = qualification.gates.as_dict()
    evidence_coverage: dict[PrincipalSignalName, bool] = {
        signal: any(
            signal in claim.signals
            and claim.human_verified
            and claim.epistemic_status == "observed"
            and claim.truth_state == "met"
            for claim in claims
        )
        for signal in signal_states
    }
    blockers: list[str] = []
    if total_score < 70:
        blockers.append("Strategic score is below 70")
    for signal, state in signal_states.items():
        if state != "met":
            blockers.append(f"Principal signal {signal} is {state}")
    for gate, state in gate_states.items():
        if state != "met":
            blockers.append(f"Hard gate {gate} is {state}")
    asks = _credible_asks(qualification.first_month_asks)
    if len(asks) != 3:
        blockers.append("Exactly three distinct first-month asks are required")
    if qualification.buyer is None:
        blockers.append("An economic buyer is required")
    for signal, covered in evidence_coverage.items():
        if not covered:
            blockers.append(f"Principal signal {signal} lacks a verified observed claim")

    has_failure = "not_met" in signal_states.values() or "not_met" in gate_states.values()
    if has_failure:
        state: QualificationState = "disqualified"
    elif not blockers:
        state = "pursue"
    else:
        state = "needs_research"
    return qualification.model_copy(
        update={
            "first_month_asks": asks,
            "total_score": total_score,
            "state": state,
            "blockers": blockers,
            "evidence_coverage": evidence_coverage,
        },
        deep=True,
    )


class CreativeCapacityMap(BaseModel):
    version: Literal["creative-capacity-map-v1"] = "creative-capacity-map-v1"
    current_promotion: list[str] = Field(default_factory=list)
    distinct_concept_count: int | None = Field(default=None, ge=0)
    concept_notes: list[str] = Field(default_factory=list)
    observed_demand: list[str] = Field(default_factory=list)
    production_gap_hypothesis: str = ""
    first_month_asks: list[str] = Field(default_factory=list, max_length=3)
    first_month_queue: list[str] = Field(default_factory=list)
    economic_buyer: EconomicBuyer | None = None
    rights_ai_objection: str = ""
    ready: bool = False

    @field_validator("first_month_asks", "first_month_queue")
    @classmethod
    def normalize_work_items(cls, value: list[str]) -> list[str]:
        return _credible_asks(value)


def evaluate_capacity_map(
    capacity_map: CreativeCapacityMap,
    qualification: StrategicQualification,
) -> CreativeCapacityMap:
    capacity_map = CreativeCapacityMap.model_validate(capacity_map.model_dump())
    qualification = StrategicQualification.model_validate(qualification.model_dump())
    asks = _credible_asks(capacity_map.first_month_asks)
    signal_states = qualification.principal_signals.as_dict()
    gate_states = qualification.gates.as_dict()
    derived_total = max(
        0,
        min(
            100,
            qualification.dimensions.total
            - min(30, sum(item.points for item in qualification.penalties)),
        ),
    )
    qualification_is_pursuable = bool(
        qualification.state == "pursue"
        and qualification.total_score == derived_total
        and derived_total >= 70
        and not qualification.blockers
        and qualification.buyer is not None
        and len(_credible_asks(qualification.first_month_asks)) == 3
        and all(state == "met" for state in signal_states.values())
        and all(state == "met" for state in gate_states.values())
        and all(qualification.evidence_coverage.get(signal, False) for signal in signal_states)
    )
    ready = bool(
        qualification_is_pursuable
        and capacity_map.economic_buyer is not None
        and len(asks) == 3
        and capacity_map.first_month_queue
        and capacity_map.production_gap_hypothesis.strip()
    )
    return capacity_map.model_copy(update={"first_month_asks": asks, "ready": ready}, deep=True)


class DiscoveryReview(BaseModel):
    held_at: datetime | None = None
    outcome: Literal["unknown", "qualified", "not_fit", "no_budget", "not_now"] = "unknown"
    outcome_reason: str = Field(default="", max_length=4000)
    research_minutes: float = Field(default=0, ge=0, le=100000)
    confirmed_recurring_need: bool | None = None
    creative_backlog: list[str] = Field(default_factory=list)
    must_ship_30_days: list[str] = Field(default_factory=list)
    must_ship_60_days: list[str] = Field(default_factory=list)
    must_ship_90_days: list[str] = Field(default_factory=list)
    new_concepts_last_month: int | None = Field(default=None, ge=0)
    idea_to_usable_asset_days: float | None = Field(default=None, ge=0)
    paid_media_structure: Literal["unknown", "consolidated_broad", "segmented", "mixed"] = "unknown"
    optimization_event: str = ""
    weekly_optimization_events: int | None = Field(default=None, ge=0)
    creative_constraint: Literal["unknown", "not_primary", "contributing", "primary"] = "unknown"
    paid_media_notes: str = ""
    employee_monthly_cost: float | None = Field(default=None, ge=0)
    agency_monthly_cost: float | None = Field(default=None, ge=0)
    freelancer_monthly_cost: float | None = Field(default=None, ge=0)
    vendor_coordination_hours: float | None = Field(default=None, ge=0)
    fidelity_rights_constraints: list[str] = Field(default_factory=list)
    approver: EconomicBuyer | None = None
    approver_turnaround_days: float | None = Field(default=None, ge=0)
    first_three_asks: list[str] = Field(default_factory=list, max_length=3)
    notes: str = ""
    completed: bool = False

    @model_validator(mode="after")
    def require_discovery_result_context(self) -> DiscoveryReview:
        if self.completed and self.held_at is not None:
            held = self.held_at if self.held_at.tzinfo else self.held_at.replace(tzinfo=UTC)
            if held > datetime.now(UTC):
                raise ValueError("Completed discovery cannot be held in the future")
        if self.completed and self.held_at is None:
            raise ValueError("A completed discovery review requires held_at")
        if (
            self.completed
            and self.outcome in {"not_fit", "no_budget", "not_now"}
            and not self.outcome_reason.strip()
        ):
            raise ValueError("A negative discovery outcome requires outcome_reason")
        return self


class SalesOutcome(BaseModel):
    outreach_outcome: Literal[
        "not_contacted", "contacted", "replied", "meeting_booked", "no_response"
    ] = "not_contacted"
    proposal_outcome: Literal["not_sent", "pending", "won", "lost"] = "not_sent"
    proposal_sent_at: datetime | None = None
    proposal_monthly_value: float | None = Field(default=None, ge=0)
    won_at: datetime | None = None
    lost_at: datetime | None = None
    lost_reason: str = ""
    month_one_started_at: date | None = None
    notes: str = ""

    @model_validator(mode="after")
    def require_loss_reason(self) -> SalesOutcome:
        if self.proposal_outcome == "lost" and not self.lost_reason.strip():
            raise ValueError("lost proposals require lost_reason")
        return self


class DeliveryOutcome(BaseModel):
    time_to_specialist_match_hours: float | None = Field(default=None, ge=0)
    time_to_first_useful_draft_hours: float | None = Field(default=None, ge=0)
    time_to_first_activated_asset_hours: float | None = Field(default=None, ge=0)
    cycle_time_reduction_percent: float | None = Field(default=None, ge=0, le=100)
    assets_delivered: int = Field(default=0, ge=0)
    assets_used: int = Field(default=0, ge=0)
    revision_rounds: int = Field(default=0, ge=0)
    revision_hours: float = Field(default=0, ge=0)
    internal_hours_displaced: float = Field(default=0, ge=0)
    vendor_coordination_hours_displaced: float = Field(default=0, ge=0)
    month_two_queue_depth: int = Field(default=0, ge=0)
    direct_delivery_cost: float | None = Field(default=None, ge=0)
    recognized_revenue: float | None = Field(default=None, ge=0)
    delivery_burden: Literal["unknown", "low", "medium", "high", "uneconomic"] = "unknown"
    day_21_renewal_intent: Literal["unknown", "yes", "maybe", "no"] = "unknown"
    renewal_decision: Literal["pending", "renewed", "not_renewed"] = "pending"
    renewal_decided_at: date | None = None
    notes: str = ""

    @model_validator(mode="after")
    def derive_delivery_metrics(self) -> DeliveryOutcome:
        if self.assets_used > self.assets_delivered:
            raise ValueError("assets_used cannot exceed assets_delivered")
        return self

    @computed_field(return_type=float | None)
    @property
    def contribution_margin(self) -> float | None:
        if self.direct_delivery_cost is None or self.recognized_revenue is None:
            return None
        return self.recognized_revenue - self.direct_delivery_cost

    @computed_field(return_type=float | None)
    @property
    def delivered_to_used_rate(self) -> float | None:
        if not self.assets_delivered:
            return None
        return self.assets_used / self.assets_delivered


class SalesAccount(BaseModel):
    id: int | None = Field(default=None, ge=1)
    candidate_id: int | None = Field(default=None, ge=1)
    company: str = Field(min_length=1)
    website: str = Field(min_length=1)
    cohort: str = "prototype-v1"
    relationship_type: Literal["buyer", "referral_partner"] = "buyer"
    segment: Literal[
        "consumer_brand",
        "consumer_app",
        "digital_product",
        "gaming",
        "subscription_business",
        "saas_ai_adjacency",
        "agency_buyer",
        "agency_referral_partner",
        "other",
    ] = "other"
    lead_source: Literal[
        "past_client",
        "founder_investor_network",
        "specialist_referral",
        "targeted_outbound",
        "other",
    ] = "targeted_outbound"
    referrer: str = ""
    intro_status: Literal["none", "requested", "opted_in", "introduced", "declined"] = "none"
    lifecycle: Literal[
        "research",
        "qualified",
        "outreach",
        "discovery",
        "proposal",
        "won",
        "lost",
        "paused",
    ] = "research"
    qualification: StrategicQualification = Field(default_factory=StrategicQualification)
    capacity_map: CreativeCapacityMap = Field(default_factory=CreativeCapacityMap)
    discovery: DiscoveryReview = Field(default_factory=DiscoveryReview)
    outcome: SalesOutcome = Field(default_factory=SalesOutcome)
    delivery: DeliveryOutcome = Field(default_factory=DeliveryOutcome)
    claims: list[EvidenceClaim] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    @model_validator(mode="after")
    def align_agency_segment_and_relationship(self) -> SalesAccount:
        if self.segment == "agency_buyer":
            self.relationship_type = "buyer"
        if self.segment == "agency_referral_partner":
            self.relationship_type = "referral_partner"
        return self
