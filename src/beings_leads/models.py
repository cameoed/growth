from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .creative_capacity import CreativeCapacityHypothesis, HypothesisClaim
from .creative_capacity import _utc_now as utc_now
from .entity_intelligence import CompanyRoleAssessment

FRONTIER_SCANNER_IDS = (
    "creative_asset_genome_v1",
    "lifecycle_email_exhaust_v1",
    "app_release_pulse_v1",
    "localization_burden_v1",
    "creator_program_contract_v1",
    "dol_lca_workforce_v1",
    "sec_form_d_capital_v1",
    "ai_artifact_provenance_v1",
    "owned_media_pulse_v1",
    "corporate_blog_engine_v1",
    "creative_hiring_demand_v1",
    "product_release_engine_v1",
    "event_webinar_engine_v1",
    "trust_center_readiness_v1",
    "brand_asset_governance_v1",
    "growth_measurement_schema_v1",
    "seo_visibility_engine_v1",
    "public_design_system_v1",
    "sf_ai_capital_constellation_v1",
)


class DiscoveryAssertionEvidence(BaseModel):
    """The bounded public context for one source-to-company discovery assertion."""

    publisher_domain: str
    relation_type: Literal[
        "customer",
        "testimonial",
        "case_study",
        "partner",
        "award",
        "member",
        "sponsor",
        "exhibitor",
        "portfolio",
        "unknown",
    ] = "unknown"
    source_url: str
    context_excerpt: str = Field(default="", max_length=1_000)
    context_hash: str = Field(default="", max_length=128)
    published_at: datetime | None = None
    confidence: Literal["low", "medium", "high"] = "low"
    current_relationship_unverified: bool = True
    evidence_kind: Literal[
        "anchor",
        "linked_logo",
        "logo_label",
        "logo_filename",
        "internal_record",
    ] = "anchor"
    asset_url: str = Field(default="", max_length=2_000)

    @field_validator("publisher_domain")
    @classmethod
    def normalize_publisher_domain(cls, value: str) -> str:
        return value.strip().lower().rstrip(".")[:253]


class CompanyAppearanceEvidence(BaseModel):
    """One company displayed on another company's already-fetched public page."""

    subject_name: str = Field(default="", max_length=160)
    subject_domain: str = Field(default="", max_length=253)
    subject_url: str = Field(default="", max_length=2_000)
    asset_url: str = Field(default="", max_length=2_000)
    extraction_method: Literal[
        "anchor",
        "linked_logo",
        "logo_label",
        "logo_filename",
        "internal_record",
    ] = "anchor"
    identity_confidence: Literal["low", "medium", "high"] = "low"
    assertion: DiscoveryAssertionEvidence

    @field_validator("subject_name")
    @classmethod
    def normalize_subject_name(cls, value: str) -> str:
        return " ".join(value.split()).strip(" \t\r\n-|·:;")[:160]

    @field_validator("subject_domain")
    @classmethod
    def normalize_subject_domain(cls, value: str) -> str:
        return value.strip().lower().rstrip(".").removeprefix("www.")[:253]

    @model_validator(mode="after")
    def require_identity(self) -> CompanyAppearanceEvidence:
        if not self.subject_name and not self.subject_domain:
            raise ValueError("company appearance requires a subject name or domain")
        return self


class DiscoveredHost(BaseModel):
    source_apex: str
    hostname: str
    first_seen: datetime | None = None
    discovery_rank: float = 0.0
    label_hint: str = ""
    discovery_evidence_urls: list[str] = Field(default_factory=list)
    provider_record_ids: list[str] = Field(default_factory=list)
    discovery_assertions: list[DiscoveryAssertionEvidence] = Field(
        default_factory=list,
        max_length=30,
    )

    @field_validator("source_apex", "hostname")
    @classmethod
    def lowercase_host(cls, value: str) -> str:
        return value.strip().rstrip(".").lower()


class LanguageAssessment(BaseModel):
    """Bounded evidence about the language a public website presents to visitors."""

    detected_language: str = "unknown"
    english_presentation: bool | None = None
    confidence: Literal["low", "medium", "high"] = "low"
    reason: str = "Insufficient public text to determine presentation language"
    declared_language: str = ""
    analyzed_characters: int = Field(default=0, ge=0)
    latin_character_ratio: float = Field(default=0.0, ge=0.0, le=1.0)


class PageEvidence(BaseModel):
    buyer_evidence: list[dict] = Field(default_factory=list, max_length=30)
    url: str
    status_code: int | None = None
    title: str = ""
    description: str = ""
    text_excerpt: str = ""
    content_hash: str = ""
    canonical_url: str = ""
    language: LanguageAssessment = Field(default_factory=LanguageAssessment)


class VendorEvidence(BaseModel):
    vendor: str
    category: str
    source_type: Literal[
        "subprocessor",
        "privacy_disclosure",
        "spf",
        "mx",
        "ns",
        "caa",
        "txt_verification",
    ]
    confidence: Literal["low", "medium", "high"]
    evidence_url: str
    record: str = ""
    purpose: str = ""


class VendorIntelligence(BaseModel):
    evidence: list[VendorEvidence] = Field(default_factory=list)
    vendors: list[str] = Field(default_factory=list)
    category_counts: dict[str, int] = Field(default_factory=dict)
    source_counts: dict[str, int] = Field(default_factory=dict)
    confidence_counts: dict[str, int] = Field(default_factory=dict)
    high_confidence_vendors: list[str] = Field(default_factory=list)
    corroborated_vendors: list[str] = Field(default_factory=list)
    dns_vendor_count: int = 0
    disclosed_vendor_count: int = 0
    stack_score: int = Field(default=0, ge=0, le=100)
    creative_commerce_score: int = Field(default=0, ge=0, le=100)
    dmarc_policy: str = ""
    bimi_published: bool = False
    bimi_verified_mark: bool = False
    bimi_logo_url: str | None = None
    bimi_evidence_url: str | None = None
    disclosure_url: str | None = None
    dns_records: dict[str, list[str]] = Field(default_factory=dict)
    career_hosts: list[str] = Field(default_factory=list)
    career_host_evidence_urls: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


class SalesOSINT(BaseModel):
    """Public sales signals optimized for recurring AI creative qualification."""

    growth_technologies: list[str] = Field(default_factory=list)
    creative_technologies: list[str] = Field(default_factory=list)
    image_assets: int = 0
    video_assets: int = 0
    embedded_video_players: int = 0
    video_platforms: list[str] = Field(default_factory=list)
    asset_hosts: list[str] = Field(default_factory=list)
    feed_urls: list[str] = Field(default_factory=list)
    feed_url: str | None = None
    feed_items: int = 0
    feed_recent_30d: int = 0
    feed_recent_90d: int = 0
    feed_commercial_items: int = 0
    feed_latest: str | None = None
    catalog_provider: str | None = None
    catalog_sample_size: int = 0
    catalog_total_hint: int = 0
    catalog_available_products: int = 0
    catalog_variants: int = 0
    catalog_product_images: int = 0
    catalog_median_price: float = 0.0
    catalog_max_price: float = 0.0
    catalog_currency: str = ""
    compensated_jobs: int = 0
    news_query: str = ""
    news_articles: int = 0
    news_commercial_triggers: int = 0
    news_latest: str | None = None
    news_titles: list[str] = Field(default_factory=list)
    news_urls: list[str] = Field(default_factory=list)
    creative_demand_score: int = Field(default=0, ge=0, le=100)
    paid_growth_score: int = Field(default=0, ge=0, le=100)
    commerce_scale_score: int = Field(default=0, ge=0, le=100)
    operating_capacity_score: int = Field(default=0, ge=0, le=100)
    timing_score: int = Field(default=0, ge=0, le=100)
    overall_score: int = Field(default=0, ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class PublicJobEvidence(BaseModel):
    posted_time_basis: Literal["unknown", "source_explicit", "source_updated"] = "unknown"
    stable_id: str = ""
    provider_id: str = ""
    current: bool = True
    state_reason: str = ""
    valid_through: datetime | None = None
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None
    compensation_ranges: list[dict[str, Any]] = Field(default_factory=list)
    deliverable_expectations: list[dict[str, Any]] = Field(default_factory=list)
    contract_signals: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    """A public ATS posting normalized without inferring employment or urgency."""

    provider: Literal["greenhouse", "lever", "ashby"]
    title: str
    source_url: str
    location: str = ""
    department: str = ""
    description_excerpt: str = ""
    posted_at: datetime | None = None
    compensation_published: bool = False
    role_families: list[str] = Field(default_factory=list)
    production_pressure_signals: list[str] = Field(default_factory=list)
    external_capacity_signals: list[str] = Field(default_factory=list)
    ai_workflow_signals: list[str] = Field(default_factory=list)
    rights_risk_signals: list[str] = Field(default_factory=list)


class ScannerCoverageManifest(BaseModel):
    """Coverage is evidence about what a scanner attempted, never a positive signal."""

    execution_id: int | None = None
    manifest_key: str
    scanner_id: str
    scanner_version: str
    surface: str
    scope: str = ""
    window_start: datetime | None = None
    window_end: datetime | None = None
    attempted_units: int = Field(default=0, ge=0)
    successful_units: int = Field(default=0, ge=0)
    failed_units: int = Field(default=0, ge=0)
    total_units: int | None = Field(default=None, ge=0)
    completeness: Literal["none", "partial", "complete", "not_applicable"] = "none"
    truncated: bool = False
    absence_interpretable: bool = False
    limitations: list[str] = Field(default_factory=list, max_length=20)
    source_urls: list[str] = Field(default_factory=list, max_length=30)
    observed_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def enforce_safe_absence(self) -> ScannerCoverageManifest:
        if self.successful_units > self.attempted_units:
            raise ValueError("successful_units cannot exceed attempted_units")
        if self.failed_units > self.attempted_units:
            raise ValueError("failed_units cannot exceed attempted_units")
        if self.total_units is not None and self.attempted_units > self.total_units:
            raise ValueError("attempted_units cannot exceed total_units")
        if self.absence_interpretable and (
            self.completeness not in {"complete", "not_applicable"}
            or self.truncated
            or self.failed_units
        ):
            raise ValueError(
                "absence is interpretable only for complete, untruncated, failure-free coverage"
            )
        return self


class EvidenceArtifact(BaseModel):
    """A content-addressed public locator and bounded extraction record."""

    execution_id: int | None = None
    artifact_key: str
    company_key: str
    scanner_id: str
    scanner_version: str
    extractor_version: str = ""
    artifact_type: str = "public_document"
    canonical_url: str = ""
    original_url: str = ""
    publisher: str = ""
    fetched_at: datetime = Field(default_factory=utc_now)
    status_code: int | None = Field(default=None, ge=100, le=599)
    mime_type: str = ""
    content_hash: str = Field(default="", max_length=128)
    published_at: datetime | None = None
    published_time_basis: Literal[
        "source_explicit",
        "source_header",
        "filing_date",
        "feed_date",
        "inferred",
        "unknown",
    ] = "unknown"
    etag: str = ""
    last_modified: str = ""
    byte_count: int = Field(default=0, ge=0)
    robots_state: Literal["allowed", "blocked", "not_checked", "not_applicable"] = "not_checked"
    truncated: bool = False
    excerpt: str = Field(default="", max_length=2_000)
    locator: str = Field(default="", max_length=1_000)
    retention_policy: str = "structured_facts_and_hash"
    license_policy: str = "public_source_terms_apply"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AtomicObservation(BaseModel):
    """One append-only, typed fact or conservative derivation with full lineage."""

    execution_id: int | None = None
    observation_key: str
    company_key: str
    scanner_id: str
    scanner_version: str
    extractor_version: str = ""
    rule_id: str = ""
    rule_version: str = ""
    entity_key: str
    predicate: str
    value_type: Literal[
        "string",
        "integer",
        "number",
        "boolean",
        "date",
        "datetime",
        "json",
        "null",
    ] = "json"
    value: Any = None
    unit: str = ""
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    summary: str = Field(default="", max_length=1_000)
    source_url: str = ""
    epistemic_status: Literal["observed", "derived", "unknown"] = "observed"
    validity_status: Literal[
        "current",
        "historical",
        "expired",
        "disputed",
        "unknown",
    ] = "unknown"
    confidence: Literal["low", "medium", "high"] = "low"
    confidence_method: str = ""
    first_observed_at: datetime = Field(default_factory=utc_now)
    last_observed_at: datetime | None = None
    effective_at: datetime | None = None
    effective_time_basis: Literal[
        "source_explicit",
        "source_header",
        "filing_date",
        "feed_date",
        "inferred",
        "scan_time",
        "unknown",
    ] = "unknown"
    expires_at: datetime | None = None
    artifact_keys: list[str] = Field(default_factory=list, max_length=20)
    coverage_keys: list[str] = Field(default_factory=list, max_length=20)
    score_eligible: bool = False
    discovery_only: bool = False
    source_actor_key: str = ""
    origin_fingerprint: str = ""
    causal_root_key: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def enforce_scoring_safety(self) -> AtomicObservation:
        if self.last_observed_at is None:
            self.last_observed_at = self.first_observed_at
        if self.last_observed_at < self.first_observed_at:
            raise ValueError("last_observed_at cannot precede first_observed_at")
        if self.score_eligible and (
            self.discovery_only
            or self.epistemic_status == "unknown"
            or self.validity_status in {"expired", "disputed"}
        ):
            raise ValueError("unknown, disputed, expired, or discovery-only evidence cannot score")
        return self


ScannerExecutionStatus = Literal[
    "running",
    "success",
    "success_empty",
    "not_applicable",
    "partial",
    "transient_error",
    "rate_limited",
    "robots_blocked",
    "identity_ambiguous",
    "failed",
]


class ScannerExecutionSummary(BaseModel):
    """Typed accounting and terminal state for one company/scanner execution."""

    execution_id: int | None = None
    candidate_id: int | None = None
    company_key: str
    scanner_id: str
    scanner_version: str
    extractor_version: str = ""
    scope_key: str = ""
    config_hash: str = ""
    status: ScannerExecutionStatus
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    next_due_at: datetime | None = None
    request_count: int = Field(default=0, ge=0)
    byte_count: int = Field(default=0, ge=0)
    token_count: int = Field(default=0, ge=0)
    cost_microusd: int = Field(default=0, ge=0)
    artifact_keys: list[str] = Field(default_factory=list, max_length=100)
    coverage_keys: list[str] = Field(default_factory=list, max_length=100)
    observation_count: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list, max_length=20)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScannerScheduleDecision(BaseModel):
    """Logged expected-information-gain inputs, selection, and propensity."""

    company_key: str
    scanner_id: str
    scanner_version: str
    decided_at: datetime = Field(default_factory=utc_now)
    applicable: bool
    applicability_reason: str = ""
    current_action: str = ""
    success_probability: float = Field(default=0.0, ge=0.0, le=1.0)
    expected_action_entropy_reduction: float = Field(default=0.0, ge=0.0, le=1.0)
    action_boundary_sensitivity: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_value: float = Field(default=0.0, ge=0.0, le=1.0)
    cost_penalty: float = Field(default=0.0, ge=0.0, le=1.0)
    rate_penalty: float = Field(default=0.0, ge=0.0, le=1.0)
    identity_penalty: float = Field(default=0.0, ge=0.0, le=1.0)
    risk_penalty: float = Field(default=0.0, ge=0.0, le=1.0)
    expected_information_gain: float = Field(default=0.0, ge=-4.0, le=4.0)
    selected: bool = False
    selection_reason: str = ""
    exploration: bool = False
    propensity: float = Field(default=0.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FrontierIntelligence(BaseModel):
    """Backward-compatible snapshot projection of append-only frontier evidence."""

    version: str = "frontier-intelligence-v1"
    executions: list[ScannerExecutionSummary] = Field(default_factory=list)
    coverage_manifests: list[ScannerCoverageManifest] = Field(default_factory=list)
    evidence_artifacts: list[EvidenceArtifact] = Field(default_factory=list)
    observations: list[AtomicObservation] = Field(default_factory=list)
    schedule_decisions: list[ScannerScheduleDecision] = Field(default_factory=list)
    features: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime | None = None


class WebIntelligence(BaseModel):
    """Bounded, public, mostly first-party evidence collected without AI."""

    jsonld_types: list[str] = Field(default_factory=list)
    product_objects: int = 0
    offer_objects: int = 0
    organization_objects: int = 0
    location_objects: int = 0
    max_review_count: int = 0
    ats_boards: dict[str, list[str]] = Field(default_factory=dict)
    open_jobs: int = 0
    creative_jobs: int = 0
    photo_video_jobs: int = 0
    growth_jobs: int = 0
    leadership_jobs: int = 0
    job_titles: list[str] = Field(default_factory=list)
    job_postings: list[PublicJobEvidence] = Field(default_factory=list)
    career_urls: list[str] = Field(default_factory=list)
    career_url_sources: dict[str, str] = Field(default_factory=dict)
    portfolio_domains: list[str] = Field(default_factory=list)
    company_appearances: list[CompanyAppearanceEvidence] = Field(
        default_factory=list,
        max_length=100,
    )
    sitemap_total_urls: int = 0
    sitemap_counts: dict[str, int] = Field(default_factory=dict)
    sitemap_recent_30d: int = 0
    sitemap_recent_90d: int = 0
    sitemap_recent_counts: dict[str, int] = Field(default_factory=dict)
    sitemaps_checked: int = 0
    latest_sitemap_change: str | None = None
    common_crawl_pages: int = 0
    common_crawl_latest: str | None = None
    common_crawl_collection: str | None = None
    vendors: VendorIntelligence = Field(default_factory=VendorIntelligence)
    sales: SalesOSINT = Field(default_factory=SalesOSINT)
    frontier: FrontierIntelligence = Field(default_factory=FrontierIntelligence)
    source_urls: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


_PROOF_ONLY_APPEARANCE_RELATIONS = frozenset({"case_study", "customer", "testimonial"})
_PROOF_ONLY_APPEARANCE_METHODS = frozenset({"linked_logo", "logo_label", "logo_filename"})


def legacy_agency_portfolio_domains(web: WebIntelligence) -> list[str]:
    """Return compatibility-era portfolio domains not explained by structured proof.

    Company appearances are source-to-company discovery evidence. Customer, testimonial,
    case-study, and logo observations must not become a workload or operating-capacity claim
    about the publisher merely because an older snapshot also projected them into
    ``portfolio_domains``. A portfolio-only text anchor remains available to genuine legacy
    agency scoring, as do snapshots created before structured appearance evidence existed.
    """

    legacy_supported_domains = {
        appearance.subject_domain.casefold().removeprefix("www.").rstrip(".")
        for appearance in web.company_appearances
        if appearance.subject_domain
        and appearance.assertion.relation_type == "portfolio"
        and appearance.extraction_method == "anchor"
    }
    proof_only_domains = {
        appearance.subject_domain.casefold().removeprefix("www.").rstrip(".")
        for appearance in web.company_appearances
        if appearance.subject_domain
        and (
            appearance.assertion.relation_type in _PROOF_ONLY_APPEARANCE_RELATIONS
            or appearance.extraction_method in _PROOF_ONLY_APPEARANCE_METHODS
        )
    } - legacy_supported_domains
    retained: list[str] = []
    for value in web.portfolio_domains:
        domain = value.strip().casefold().removeprefix("www.").rstrip(".")
        if domain and domain not in proof_only_domains and domain not in retained:
            retained.append(domain)
    return retained


class PublicEmailEvidence(BaseModel):
    """An explicitly published public email with enough context to use safely."""

    address: str
    source_url: str
    method: Literal[
        "mailto",
        "visible_text",
        "json_ld",
        "microdata",
        "encoded_markup",
        "vcard",
        "common_crawl",
    ]
    purpose: Literal[
        "new_business",
        "sales",
        "partnerships",
        "marketing",
        "production",
        "general",
        "support",
        "press",
        "careers",
        "legal",
        "privacy",
        "security",
        "unknown",
    ] = "unknown"
    confidence: Literal["low", "medium", "high"] = "low"
    outreach_eligible: bool = False
    domain_matches_site: bool = False
    mx_observed: bool | None = None
    historical: bool = False
    observed_at: datetime | None = None
    person_name: str = ""
    person_role: str = ""


class PublicPhoneEvidence(BaseModel):
    """A phone number explicitly published on a first-party public contact surface."""

    number: str
    dial_number: str
    source_url: str
    method: Literal["tel", "json_ld", "microdata", "vcard"]
    purpose: Literal[
        "new_business",
        "sales",
        "partnerships",
        "marketing",
        "production",
        "general",
        "support",
        "press",
        "careers",
        "legal",
        "privacy",
        "security",
        "unknown",
    ] = "unknown"
    confidence: Literal["low", "medium", "high"] = "low"
    outreach_eligible: bool = False
    historical: bool = False
    observed_at: datetime | None = None


class PublicPersonEvidence(BaseModel):
    """A named person and role explicitly published by the company."""

    name: str
    role: str
    source_url: str
    method: Literal["json_ld", "microdata", "semantic_team_card", "vcard"]
    confidence: Literal["low", "medium", "high"] = "medium"
    buyer_role: Literal[
        "economic_buyer",
        "champion",
        "delivery_risk",
        "unknown",
    ] = "unknown"
    profile_url: str | None = None
    observed_at: datetime | None = None


class ContactIntelligence(BaseModel):
    emails: list[PublicEmailEvidence] = Field(default_factory=list)
    preferred_email: str | None = None
    phones: list[PublicPhoneEvidence] = Field(default_factory=list)
    preferred_phone: str | None = None
    people: list[PublicPersonEvidence] = Field(default_factory=list)
    contact_pages_checked: list[str] = Field(default_factory=list)
    contact_forms: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class AdIntelligence(BaseModel):
    """Current verified paid-ad evidence projected separately from site snapshots.

    The relational ad ledger is authoritative. This compact model is passed into scoring and
    transport payloads so current ad observations never rewrite historical website evidence.
    """

    identity_state: Literal["unresolved", "probable", "verified", "ambiguous", "rejected"] = (
        "unresolved"
    )
    identity_confidence: int = Field(default=0, ge=0, le=100)
    advertiser_ids: list[str] = Field(default_factory=list)
    advertiser_names: list[str] = Field(default_factory=list)
    meta_page_id: str = ""
    meta_page_name: str = ""
    meta_page_url: str = ""
    linked_instagram_handle: str = ""
    platforms: list[str] = Field(default_factory=list)
    total_ads: int = Field(default=0, ge=0)
    active_ads: int = Field(default=0, ge=0)
    unique_creatives: int = Field(default=0, ge=0)
    active_unique_creatives: int = Field(default=0, ge=0)
    video_creatives: int = Field(default=0, ge=0)
    image_creatives: int = Field(default=0, ge=0)
    new_creatives_7d: int = Field(default=0, ge=0)
    new_creatives_30d: int = Field(default=0, ge=0)
    new_creatives_90d: int = Field(default=0, ge=0)
    observation_windows_90d: int = Field(default=0, ge=0)
    # Curated/positive-only libraries stay in the totals above for operator context. These
    # optional fields identify the subset that is safe to use in scoring. None preserves
    # backward compatibility for callers that construct the compact model directly.
    scoreable_platforms: list[str] = Field(default_factory=list)
    scoreable_total_ads: int | None = Field(default=None, ge=0)
    scoreable_active_ads: int | None = Field(default=None, ge=0)
    scoreable_unique_creatives: int | None = Field(default=None, ge=0)
    scoreable_active_unique_creatives: int | None = Field(default=None, ge=0)
    scoreable_new_creatives_7d: int | None = Field(default=None, ge=0)
    scoreable_new_creatives_30d: int | None = Field(default=None, ge=0)
    scoreable_new_creatives_90d: int | None = Field(default=None, ge=0)
    scoreable_observation_windows_90d: int | None = Field(default=None, ge=0)
    scoreable_first_started_at: datetime | None = None
    scoreable_latest_started_at: datetime | None = None
    first_started_at: datetime | None = None
    latest_started_at: datetime | None = None
    last_observed_at: datetime | None = None
    recurring_established: bool = False
    source_urls: list[str] = Field(default_factory=list)
    coverage_notes: list[str] = Field(default_factory=list)


class SiteObservation(BaseModel):
    core_hydration_reused: bool = False
    core_hydration_retry_components: list[str] = Field(default_factory=list, max_length=20)
    core_hydration_ats_receipts: dict[str, dict[str, Any]] = Field(
        default_factory=dict, max_length=20)
    core_hydration_errors: list[str] = Field(default_factory=list, max_length=20)
    buyer_evidence: list[dict] = Field(default_factory=list)
    paid_creative_asset_keys: list[str] = Field(default_factory=list)
    hostname: str
    observed_at: datetime = Field(default_factory=utc_now)
    fetch_outcome: Literal[
        "unknown",
        "live",
        "retryable_error",
        "platform_blocked",
        "not_public_storefront",
        "retired_hostname",
        "circuit_open",
    ] = "unknown"
    fetch_disposition: str = Field(default="", max_length=500)
    fetch_attempts: int = Field(default=0, ge=0)
    recovered_after_retry: bool = False
    fetch_provider: Literal["direct", "microlink"] = "direct"
    fallback_attempted: bool = False
    fallback_proxy_used: bool = False
    fallback_error: str = Field(default="", max_length=500)
    direct_fetch_error: str = Field(default="", max_length=1_000)
    canonical_recovery_required: bool = False
    tenant_label: str = Field(default="", max_length=160)
    live: bool = False
    final_url: str | None = None
    canonical_host: str | None = None
    status_code: int | None = None
    title: str = ""
    description: str = ""
    text_excerpt: str = ""
    content_hash: str = ""
    language: LanguageAssessment = Field(default_factory=LanguageAssessment)
    pages: list[PageEvidence] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    contacts: ContactIntelligence = Field(default_factory=ContactIntelligence)
    contact_urls: list[str] = Field(default_factory=list)
    legal_urls: list[str] = Field(default_factory=list)
    social_urls: list[str] = Field(default_factory=list)
    evidence_urls: list[str] = Field(default_factory=list)
    signals: dict[str, list[str]] = Field(default_factory=dict)
    technologies: list[str] = Field(default_factory=list)
    public_ids: dict[str, list[str]] = Field(default_factory=dict)
    outbound_domains: list[str] = Field(default_factory=list)
    web: WebIntelligence = Field(default_factory=WebIntelligence)
    error: str | None = None


ScoreComponentName = Literal[
    "recurring_demand",
    "paid_distribution",
    "ability_to_pay",
    "beings_fit",
    "operational_maturity",
    "reachability",
]


class PlacementFit(BaseModel):
    score: int = Field(default=0, ge=0, le=100)
    confidence: Literal["low", "medium", "high", "very_high"] = "low"
    ready: bool = False
    specialist_roles: list[Literal["ai_photo", "ai_video"]] = Field(default_factory=list)
    agency_specialties: list[str] = Field(default_factory=list)
    evidence_families: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class ScoreCard(BaseModel):
    company_role: CompanyRoleAssessment = Field(default_factory=CompanyRoleAssessment)
    total: int = Field(ge=0, le=100)
    components: dict[ScoreComponentName, int]
    archetype: Literal[
        "agency",
        "ecommerce_brand",
        "creator_education",
        "media_membership",
        "saas_or_other",
        "unknown",
    ] = "unknown"
    confidence: Literal["low", "medium", "high"] = "low"
    disqualified: bool = False
    disqualifiers: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    budget_ready: bool = False
    budget_confidence: Literal["low", "medium", "high"] = "low"
    evidence_families: list[str] = Field(default_factory=list)
    cross_site_reasons: list[str] = Field(default_factory=list)
    demand_score: int = Field(default=0, ge=0, le=100)
    capacity_score: int = Field(default=0, ge=0, le=100)
    timing_score: int = Field(default=0, ge=0, le=100)
    growth_score: int = Field(default=0, ge=0, le=100)
    data_confidence: int = Field(default=0, ge=0, le=100)
    contact_priority: int = Field(default=0, ge=0, le=100)
    opportunity_reasons: list[str] = Field(default_factory=list)
    growth_reasons: list[str] = Field(default_factory=list)
    latest_trigger: str = ""
    placement: PlacementFit = Field(default_factory=PlacementFit)
    creative_capacity: CreativeCapacityHypothesis = Field(
        default_factory=CreativeCapacityHypothesis
    )


class DecisionMaker(BaseModel):
    name: str
    role: str
    source_url: str


class AIAnalysis(BaseModel):
    company_name: str = ""
    company_url: str | None = None
    archetype: Literal[
        "agency",
        "ecommerce_brand",
        "creator_education",
        "media_membership",
        "saas_or_other",
        "unknown",
    ] = "unknown"
    likely_recurring_5k_fit: Literal["unknown", "unlikely", "possible", "likely"] = "unknown"
    likely_recurring_6k_fit: Literal["unlikely", "possible", "likely"] = "unlikely"
    budget_fit_score: int = Field(ge=0, le=100)
    placement_fit: Literal["unlikely", "possible", "strong"] = "unlikely"
    placement_fit_score: int = Field(default=0, ge=0, le=100)
    recommended_specialist_roles: list[Literal["ai_photo", "ai_video"]] = Field(
        default_factory=list,
        max_length=2,
    )
    placement_rationale: list[str] = Field(default_factory=list, max_length=5)
    confidence: Literal["low", "medium", "high"]
    rationale: list[str] = Field(default_factory=list, max_length=5)
    evidence: list[str] = Field(default_factory=list, max_length=8)
    risks: list[str] = Field(default_factory=list, max_length=5)
    recommended_offer: str = ""
    decision_maker_roles: list[str] = Field(default_factory=list, max_length=5)
    public_decision_makers: list[DecisionMaker] = Field(default_factory=list, max_length=5)
    evidence_urls: list[str] = Field(default_factory=list, max_length=10)
    observations: list[HypothesisClaim] = Field(default_factory=list, max_length=8)
    inferences: list[HypothesisClaim] = Field(default_factory=list, max_length=8)
    economic_buyer_role: str = ""
    plausible_first_month_asks: list[str] = Field(default_factory=list, max_length=3)
    rights_ai_risks: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def enforce_epistemic_labels(self) -> AIAnalysis:
        if any(item.epistemic_status != "observed" for item in self.observations):
            raise ValueError("AI observations must be labeled observed")
        if any(item.epistemic_status != "inferred" for item in self.inferences):
            raise ValueError("AI inferences must be labeled inferred")
        return self


class SourceSuggestion(BaseModel):
    source_key: str = ""
    source_kind: Literal[
        "tenant_apex",
        "agency_directory",
        "brand_directory",
        "brand_group_portfolio",
        "platform_customer_list",
        "repeatable_search",
    ]
    apex: str | None = None
    url: str
    name: str
    category: Literal[
        "Agency",
        "Ecommerce",
        "Consumer / Digital",
        "B2B / SaaS",
        "Brand group",
        "Other",
    ]
    relationship_type: Literal["buyer", "referral_partner"] = "buyer"
    source_family: Literal[
        "competitor_customer_index",
        "performance_creative_system",
        "creative_operations_platform",
        "creator_ugc_platform",
        "ad_campaign_index",
        "app_gaming_ua",
        "commerce_growth_platform",
        "creative_hiring_index",
        "agency_delivery_capacity",
        "other",
    ] = "other"
    buyer_archetypes: list[
        Literal[
            "paid_growth_brand",
            "consumer_app_game",
            "enterprise_creative_team",
            "multi_brand_operator",
            "saas_product_marketing",
            "agency_delivery_team",
        ]
    ] = Field(default_factory=list, max_length=6)
    discovery_mechanism: str
    expected_6k_fit: Literal["medium", "high", "very_high"]
    confidence: Literal["low", "medium", "high"]
    rationale: list[str] = Field(default_factory=list, max_length=5)
    evidence_urls: list[str] = Field(default_factory=list, max_length=8)


class SourceScoutBatch(BaseModel):
    suggestions: list[SourceSuggestion] = Field(default_factory=list, max_length=12)


class RankedProspect(BaseModel):
    candidate_id: int
    hostname: str
    source_apex: str
    label_hint: str = ""
    first_seen: datetime | None = None
    discovery_evidence_urls: list[str] = Field(default_factory=list)
    provider_record_ids: list[str] = Field(default_factory=list)
    observation: SiteObservation
    score: ScoreCard
    ai: AIAnalysis | None = None
    final_score: int = Field(ge=0, le=100)
    changes: list[dict[str, Any]] = Field(default_factory=list)
