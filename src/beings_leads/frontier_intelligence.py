from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
import tldextract
from bs4 import BeautifulSoup, Tag

from .hiring_intelligence import (
    NormalizedJobPosting,
    deduplicate_job_postings,
    extract_job_postings,
    normalize_job_posting,
)
from .hiring_intelligence import _utc as _now
from .html_document import parse_html, reuse_html
from .models import (
    FRONTIER_SCANNER_IDS,
    AtomicObservation,
    EvidenceArtifact,
    FrontierIntelligence,
    ScannerCoverageManifest,
    ScannerExecutionSummary,
    ScannerScheduleDecision,
)
from .retrieval_policy import scanner_retry_delay
from .site_research import has_first_party_job_schema

ScannerCostClass = Literal["local", "keyless_public", "shared_bulk"]
FetchAdapter = Callable[[str, int], Awaitable["FrontierDocument"]]

_EXTRACTOR_VERSION = "1.0.0"
_RULE_VERSION = "1.0.0"
_MAX_DOCUMENT_BYTES = 2_000_000
_MAX_ASSETS = 500
_TLD_EXTRACT = tldextract.TLDExtract(
    suffix_list_urls=(),
    include_psl_private_domains=True,
)


@dataclass(frozen=True, slots=True)
class ScannerDefinition:
    scanner_id: str
    version: str
    extractor_version: str
    cost_class: ScannerCostClass
    ttl_seconds: int
    max_requests: int
    max_response_bytes: int
    applicability_inputs: tuple[str, ...]
    eig_dimensions: tuple[str, ...]
    success_probability: float
    evidence_value: float
    safe_claim: str
    prohibited_claims: tuple[str, ...]


def _definition(
    scanner_id: str,
    *,
    cost: ScannerCostClass,
    ttl_days: int,
    requests: int,
    response_bytes: int,
    applies: tuple[str, ...],
    eig: tuple[str, ...],
    probability: float,
    value: float,
    safe: str,
    prohibited: tuple[str, ...],
) -> ScannerDefinition:
    return ScannerDefinition(
        scanner_id=scanner_id,
        version="1.0.0",
        extractor_version=_EXTRACTOR_VERSION,
        cost_class=cost,
        ttl_seconds=ttl_days * 86_400,
        max_requests=requests,
        max_response_bytes=response_bytes,
        applicability_inputs=applies,
        eig_dimensions=eig,
        success_probability=probability,
        evidence_value=value,
        safe_claim=safe,
        prohibited_claims=prohibited,
    )


SCANNER_DEFINITIONS: tuple[ScannerDefinition, ...] = (
    _definition(
        "creative_asset_genome_v1",
        cost="local",
        ttl_days=7,
        requests=5,
        response_bytes=5_000_000,
        applies=("saved_first_party_html",),
        eig=("proof_match", "creative_footprint", "research_coverage"),
        probability=0.95,
        value=0.90,
        safe="The bounded sample exposes media URL identities, formats, aspects, and DAM surfaces.",
        prohibited=("creative spend", "performance", "fatigue", "staffing gap"),
    ),
    _definition(
        "lifecycle_email_exhaust_v1",
        cost="keyless_public",
        ttl_days=1,
        requests=9,
        response_bytes=2_000_000,
        applies=("first_party_published_archive_link",),
        eig=("recurring_workload", "commercial_timing"),
        probability=0.55,
        value=0.88,
        safe="A first-party-linked public archive exposes these campaign pages.",
        prohibited=("send volume", "list size", "automation", "performance", "spend"),
    ),
    _definition(
        "app_release_pulse_v1",
        cost="keyless_public",
        ttl_days=1,
        requests=3,
        response_bytes=1_000_000,
        applies=("canonical_domain", "first_party_app_link_or_association"),
        eig=("product_surface", "commercial_timing", "localization"),
        probability=0.72,
        value=0.86,
        safe="The domain associates with this app; Apple reports the observed release facts.",
        prohibited=("installs", "users", "revenue", "team size"),
    ),
    _definition(
        "localization_burden_v1",
        cost="local",
        ttl_days=7,
        requests=5,
        response_bytes=2_000_000,
        applies=("saved_first_party_html_or_sitemap",),
        eig=("production_complexity", "market_variants"),
        probability=0.92,
        value=0.84,
        safe="The site declares these language or region variants.",
        prohibited=("regional revenue", "market launch", "translation headcount"),
    ),
    _definition(
        "creator_program_contract_v1",
        cost="keyless_public",
        ttl_days=7,
        requests=2,
        response_bytes=1_000_000,
        applies=("first_party_creator_or_affiliate_program_link",),
        eig=("content_obligations", "rights_risk", "buyer_access"),
        probability=0.62,
        value=0.92,
        safe="The public program page discloses these content or rights requirements.",
        prohibited=("creator count", "payments made", "performance", "spend"),
    ),
    _definition(
        "dol_lca_workforce_v1",
        cost="shared_bulk",
        ttl_days=30,
        requests=0,
        response_bytes=0,
        applies=("exact_legal_name", "corroborating_location", "local_dol_index"),
        eig=("operating_capacity", "specialty_workforce"),
        probability=0.48,
        value=0.78,
        safe="DOL rows report certified positions, occupations, worksites, and wages.",
        prohibited=("employee count", "actual hires", "headcount", "current budget"),
    ),
    _definition(
        "sec_form_d_capital_v1",
        cost="shared_bulk",
        ttl_days=90,
        requests=0,
        response_bytes=0,
        applies=("exact_legal_name", "corroborating_identity", "local_sec_index"),
        eig=("capital_access", "commercial_timing"),
        probability=0.35,
        value=0.88,
        safe="The issuer filed a Form D notice reporting the literal amount-sold facts.",
        prohibited=("money raised", "valuation", "cash", "runway", "profitability"),
    ),
    _definition(
        "ai_artifact_provenance_v1",
        cost="keyless_public",
        ttl_days=7,
        requests=9,
        response_bytes=2_000_000,
        applies=("first_party_html", "exact_domain_or_first_party_artifact_link"),
        eig=("ai_native_fit", "technical_product_surface"),
        probability=0.70,
        value=0.90,
        safe="The official-domain-linked organization publishes these public AI artifacts.",
        prohibited=("company size", "traction", "revenue", "budget"),
    ),
    _definition(
        "owned_media_pulse_v1",
        cost="keyless_public",
        ttl_days=1,
        requests=1,
        response_bytes=1_000_000,
        applies=("officially_linked_feed_or_video_sitemap",),
        eig=("recurring_workload", "commercial_timing"),
        probability=0.75,
        value=0.85,
        safe="The officially linked feed published these items in the bounded window.",
        prohibited=("audience", "views", "total output", "production spend", "conversion"),
    ),
    _definition(
        "corporate_blog_engine_v1",
        cost="keyless_public",
        ttl_days=1,
        requests=5,
        response_bytes=3_000_000,
        applies=("first_party_linked_blog_hub_or_feed",),
        eig=("distribution_investment", "recurring_workload", "content_strategy"),
        probability=0.82,
        value=0.91,
        safe=(
            "A bounded first-party-linked sample exposes the observed editorial cadence and "
            "content-operation surfaces."
        ),
        prohibited=(
            "content spend",
            "budget",
            "traffic",
            "audience",
            "performance",
            "team size",
            "purchase intent",
        ),
    ),
    _definition(
        "creative_hiring_demand_v1",
        cost="keyless_public",
        ttl_days=1,
        requests=8,
        response_bytes=4_000_000,
        applies=(
            "first_party_linked_careers_or_ats",
            "same_company_sitemap_or_dns_career_surface",
            "structured_public_ats_records",
        ),
        eig=(
            "operating_capacity",
            "creative_workload",
            "production_pressure",
            "commercial_timing",
        ),
        probability=0.82,
        value=0.94,
        safe=(
            "Public company-bound or first-party-linked careers surfaces expose these "
            "distinct current postings and deterministic role families."
        ),
        prohibited=(
            "hires completed",
            "employee count",
            "headcount growth",
            "urgency",
            "budget",
            "outsourcing intent",
            "purchase intent",
        ),
    ),
    _definition(
        "product_release_engine_v1",
        cost="keyless_public",
        ttl_days=1,
        requests=4,
        response_bytes=2_000_000,
        applies=("first_party_linked_changelog_release_notes_or_feed",),
        eig=("recurring_workload", "product_surface", "commercial_timing"),
        probability=0.76,
        value=0.91,
        safe="First-party-linked release surfaces expose this bounded product-update cadence.",
        prohibited=(
            "adoption",
            "launch success",
            "users",
            "revenue",
            "staffing",
            "budget",
            "roadmap truth",
        ),
    ),
    _definition(
        "event_webinar_engine_v1",
        cost="keyless_public",
        ttl_days=1,
        requests=5,
        response_bytes=3_000_000,
        applies=("first_party_linked_event_webinar_calendar_or_feed",),
        eig=("recurring_workload", "owned_programming", "commercial_timing"),
        probability=0.72,
        value=0.90,
        safe="First-party-linked event surfaces expose these explicitly dated public events.",
        prohibited=("attendance", "demand", "pipeline", "success", "spend"),
    ),
    _definition(
        "trust_center_readiness_v1",
        cost="keyless_public",
        ttl_days=7,
        requests=3,
        response_bytes=2_000_000,
        applies=("first_party_linked_trust_security_compliance_or_assurance_center",),
        eig=("enterprise_readiness", "buyer_access"),
        probability=0.62,
        value=0.86,
        safe="A public first-party trust surface states these procurement and control families.",
        prohibited=(
            "certification validity",
            "revenue",
            "customers",
            "company size",
            "security quality",
            "budget",
        ),
    ),
    _definition(
        "brand_asset_governance_v1",
        cost="keyless_public",
        ttl_days=7,
        requests=3,
        response_bytes=2_000_000,
        applies=("first_party_linked_brand_guidelines_press_kit_or_asset_portal",),
        eig=("creative_infrastructure", "outreach_context"),
        probability=0.64,
        value=0.74,
        safe=(
            "A public first-party brand surface exposes these governed asset and usage categories."
        ),
        prohibited=("creative cadence", "spend", "team size", "production gap"),
    ),
    _definition(
        "growth_measurement_schema_v1",
        cost="local",
        ttl_days=7,
        requests=0,
        response_bytes=0,
        applies=("saved_first_party_html",),
        eig=("distribution_investment", "growth_measurement"),
        probability=0.90,
        value=0.84,
        safe="Saved first-party HTML exposes these normalized public measurement call shapes.",
        prohibited=(
            "event fired",
            "experiment ran",
            "active ads",
            "conversions",
            "performance",
            "spend",
        ),
    ),
    _definition(
        "seo_visibility_engine_v1",
        cost="local",
        ttl_days=30,
        requests=0,
        response_bytes=0,
        applies=("saved_first_party_html",),
        eig=("operating_capacity", "distribution_maturity"),
        probability=0.94,
        value=0.76,
        safe=(
            "A bounded first-party page sample exposes indexability, metadata, canonical, "
            "structured-data, content-depth, and internal-discovery readiness."
        ),
        prohibited=(
            "search ranking",
            "organic traffic",
            "search demand",
            "revenue",
            "budget",
            "purchase intent",
        ),
    ),
    _definition(
        "public_design_system_v1",
        cost="keyless_public",
        ttl_days=7,
        requests=2,
        response_bytes=2_000_000,
        applies=("first_party_linked_storybook_design_system_or_component_library",),
        eig=("creative_infrastructure", "outreach_context"),
        probability=0.58,
        value=0.72,
        safe="A public first-party-linked design-system surface exposes this bounded metadata.",
        prohibited=("team size", "design maturity", "product scale", "spend", "purchase intent"),
    ),
    _definition(
        "sf_ai_capital_constellation_v1",
        cost="local",
        ttl_days=7,
        requests=1,
        response_bytes=1_000_000,
        applies=("geography_evidence", "frontier_observations", "optional_datasf_index"),
        eig=("research_priority", "independent_signal_intersection"),
        probability=0.45,
        value=0.95,
        safe="Independent cited public facts co-occur for this SF AI research candidate.",
        prohibited=("headquarters", "private finances", "contact now", "auto qualification"),
    ),
)
SCANNER_REGISTRY = {item.scanner_id: item for item in SCANNER_DEFINITIONS}
if tuple(item.scanner_id for item in SCANNER_DEFINITIONS) != FRONTIER_SCANNER_IDS or len(
    SCANNER_REGISTRY
) != len(SCANNER_DEFINITIONS):
    raise RuntimeError(
        "frontier scanner definitions must exactly match the canonical unique scanner ids"
    )


@dataclass(frozen=True, slots=True)
class FrontierDocument:
    url: str
    content: bytes | str
    kind: str = "public_document"
    status_code: int | None = 200
    mime_type: str = ""
    publisher: str = ""
    first_party_source_url: str = ""
    officially_linked: bool = False
    discovery_basis: str = ""
    discovery_evidence_url: str = ""
    robots_state: Literal["allowed", "blocked", "not_checked", "not_applicable"] = "not_checked"
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def body(self) -> bytes:
        return self.content if isinstance(self.content, bytes) else self.content.encode("utf-8")


@dataclass(frozen=True, slots=True)
class FrontierScannerContext:
    company_key: str
    primary_url: str = ""
    company_name: str = ""
    legal_name: str = ""
    locations: tuple[str, ...] = ()
    documents: tuple[FrontierDocument, ...] = ()
    records: tuple[Mapping[str, Any], ...] = ()
    request_urls: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _Fact:
    predicate: str
    value: Any
    value_type: Literal[
        "string", "integer", "number", "boolean", "date", "datetime", "json", "null"
    ]
    entity_key: str
    summary: str
    source_url: str
    qualifiers: Mapping[str, Any] = field(default_factory=dict)
    unit: str = ""
    epistemic_status: Literal["observed", "derived", "unknown"] = "observed"
    validity_status: Literal["current", "historical", "expired", "disputed", "unknown"] = "current"
    confidence: Literal["low", "medium", "high"] = "high"
    confidence_method: str = "deterministic_parser"
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
    score_eligible: bool = False
    discovery_only: bool = False
    source_actor_key: str = ""
    origin_fingerprint: str = ""
    causal_root_key: str = ""
    artifact_keys: tuple[str, ...] | None = None


def _hash(*parts: Any) -> str:
    encoded = "\x1f".join(
        json.dumps(part, sort_keys=True, default=str)
        if isinstance(part, (dict, list, tuple))
        else str(part)
        for part in parts
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _json(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _text(value: Any, limit: int = 2_000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _host(value: str) -> str:
    raw = value.strip().casefold().rstrip(".")
    if "://" in raw or raw.startswith("//"):
        try:
            raw = urlparse(raw if "://" in raw else f"https:{raw}").hostname or ""
        except ValueError:
            return ""
    else:
        raw = raw.split("/", 1)[0].split(":", 1)[0]
    return raw.removeprefix("www.").rstrip(".")


def _domain(value: str) -> str:
    host = _host(value)
    parsed = _TLD_EXTRACT(host)
    return f"{parsed.domain}.{parsed.suffix}" if parsed.domain and parsed.suffix else host


def _same_company(left: str, right: str) -> bool:
    return bool(_host(left) and _host(right) and _domain(left) == _domain(right))


def _safe_url(raw: str, base: str = "") -> str:
    candidate = urljoin(base, raw.strip()) if base else raw.strip()
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    try:
        parsed = urlparse(candidate)
        parsed_host = parsed.hostname
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed_host
        or parsed.username
        or parsed.password
    ):
        return ""
    host = parsed_host.casefold().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return ""
    try:
        parsed_port = parsed.port
    except ValueError:
        return ""
    port = f":{parsed_port}" if parsed_port else ""
    return urlunparse(
        (parsed.scheme.casefold(), f"{host}{port}", parsed.path or "/", "", parsed.query, "")
    )


def _date(value: Any) -> datetime | None:
    text = _text(value, 100)
    if not text:
        return None
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            result = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            result = None
            for format_string in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
                try:
                    result = datetime.strptime(text, format_string).replace(tzinfo=UTC)
                    break
                except ValueError:
                    continue
            if result is not None:
                return result
            match = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", text)
            if not match:
                return None
            try:
                result = datetime(*(int(group) for group in match.groups()), tzinfo=UTC)
            except ValueError:
                return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _name(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold())).strip()


def _location(value: Any) -> str:
    text = _name(value)
    for old, new in (
        ("street", "st"),
        ("avenue", "ave"),
        ("road", "rd"),
        ("suite", "ste"),
        ("california", "ca"),
    ):
        text = re.sub(rf"\b{old}\b", new, text)
    return text


def _location_match(expected: Sequence[str], parts: Sequence[Any]) -> bool:
    candidate = _location(" ".join(str(part or "") for part in parts))
    if not candidate:
        return False
    for value in expected:
        target = _location(value)
        target_numbers = set(re.findall(r"\b\d+\b", target))
        candidate_numbers = set(re.findall(r"\b\d+\b", candidate))
        if target_numbers and candidate_numbers and not target_numbers & candidate_numbers:
            continue
        if target and (target in candidate or candidate in target):
            return True
        if target and len(set(target.split()) & set(candidate.split())) >= 3:
            return True
    return False


def _row(row: Mapping[str, Any], *names: str) -> Any:
    normalized = {re.sub(r"[^A-Z0-9]", "", str(key).upper()): value for key, value in row.items()}
    for name in names:
        key = re.sub(r"[^A-Z0-9]", "", name.upper())
        if key in normalized:
            return normalized[key]
    return None


def _positive_int(value: Any) -> int | None:
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if 0 < result <= 100_000_000 else None


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(re.sub(r"[^0-9.-]", "", str(value or "")))
    except InvalidOperation:
        return None
    return result if 0 <= result <= Decimal("1000000000000000") else None


def _artifact(
    scanner_id: str,
    company_key: str,
    document: FrontierDocument,
    now: datetime,
    *,
    excerpt: str = "",
    published_at: datetime | None = None,
    published_basis: str = "unknown",
    truncated: bool = False,
) -> EvidenceArtifact:
    definition = SCANNER_REGISTRY[scanner_id]
    body = document.body
    canonical = _safe_url(document.url)
    content_hash = hashlib.sha256(body).hexdigest()
    headers = {str(key).casefold(): str(value) for key, value in document.headers.items()}
    artifact = EvidenceArtifact(
        artifact_key=f"artifact:{scanner_id}:{_hash(canonical, content_hash)[:32]}",
        company_key=_host(company_key) or _name(company_key),
        scanner_id=scanner_id,
        scanner_version=definition.version,
        extractor_version=definition.extractor_version,
        artifact_type=document.kind,
        canonical_url=canonical,
        original_url=document.url,
        publisher=document.publisher or _host(canonical),
        fetched_at=now,
        status_code=document.status_code,
        mime_type=document.mime_type,
        content_hash=content_hash,
        published_at=published_at,
        published_time_basis=published_basis,  # type: ignore[arg-type]
        etag=headers.get("etag", "")[:500],
        last_modified=headers.get("last-modified", "")[:500],
        byte_count=len(body),
        robots_state=(
            "not_applicable"
            if definition.cost_class in {"shared_bulk", "local"}
            else document.robots_state
        ),
        truncated=truncated,
        excerpt=_text(excerpt),
        locator=canonical,
        metadata={
            "first_party_source_url": _safe_url(document.first_party_source_url),
            "officially_linked": bool(document.officially_linked),
            **(
                {"discovery_basis": _text(document.discovery_basis, 100)}
                if document.discovery_basis
                else {}
            ),
            **(
                {"discovery_evidence_url": _safe_url(document.discovery_evidence_url)}
                if document.discovery_evidence_url
                else {}
            ),
            "identity_basis": (
                "same_company_domain"
                if _same_company(canonical, company_key)
                else "exact_first_party_link"
                if document.first_party_source_url
                and _same_company(document.first_party_source_url, company_key)
                else "unresolved"
            ),
        },
    )

    # Same document bytes can support different excerpts, dates and provenance.
    # Address the structured extraction too, so none is discarded on insertion.
    identity = artifact.model_dump(
        mode="json", exclude={"artifact_key", "execution_id", "fetched_at"}
    )
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return artifact.model_copy(
        update={"artifact_key": f"artifact:{scanner_id}:{fingerprint}"}
    )


def _result(
    scanner_id: str,
    company_key: str,
    *,
    facts: Sequence[_Fact] = (),
    artifacts: Sequence[EvidenceArtifact] = (),
    observed_at: datetime | None = None,
    status: str | None = None,
    surface: str,
    scope: str = "",
    attempted: int = 0,
    successful: int = 0,
    failed: int = 0,
    total: int | None = None,
    completeness: Literal["none", "partial", "complete", "not_applicable"] = "complete",
    truncated: bool = False,
    absence_interpretable: bool = False,
    limitations: Sequence[str] = (),
    errors: Sequence[str] = (),
    features: Mapping[str, Any] | None = None,
    applicable_reason: str = "",
) -> FrontierIntelligence:
    now = _now(observed_at)
    definition = SCANNER_REGISTRY[scanner_id]
    company = _host(company_key) or _name(company_key)
    manifest_key = f"coverage:{scanner_id}:{_hash(company, surface, scope)[:32]}"
    coverage = ScannerCoverageManifest(
        manifest_key=manifest_key,
        scanner_id=scanner_id,
        scanner_version=definition.version,
        surface=surface,
        scope=scope,
        attempted_units=attempted,
        successful_units=successful,
        failed_units=failed,
        total_units=total,
        completeness=completeness,
        truncated=truncated,
        absence_interpretable=absence_interpretable,
        limitations=[_text(item, 500) for item in limitations][:20],
        source_urls=list(
            dict.fromkeys(item.canonical_url for item in artifacts if item.canonical_url)
        )[:30],
        observed_at=now,
    )
    artifact_keys = [item.artifact_key for item in artifacts]
    observations: list[AtomicObservation] = []
    for fact in facts[:500]:
        value = _json(fact.value)
        origin = fact.origin_fingerprint or _hash(
            fact.source_url, fact.predicate, fact.entity_key, value
        )
        observations.append(
            AtomicObservation(
                observation_key=(
                    f"observation:{scanner_id}:"
                    f"{_hash(company, fact.predicate, fact.entity_key, value)[:32]}"
                ),
                company_key=company,
                scanner_id=scanner_id,
                scanner_version=definition.version,
                extractor_version=definition.extractor_version,
                rule_id=fact.predicate,
                rule_version=_RULE_VERSION,
                entity_key=fact.entity_key,
                predicate=fact.predicate,
                value_type=fact.value_type,
                value=value,
                unit=fact.unit,
                qualifiers=_json(dict(fact.qualifiers)),
                summary=_text(fact.summary, 1_000),
                source_url=_safe_url(fact.source_url),
                epistemic_status=fact.epistemic_status,
                validity_status=fact.validity_status,
                confidence=fact.confidence,
                confidence_method=fact.confidence_method,
                first_observed_at=now,
                last_observed_at=now,
                effective_at=fact.effective_at,
                effective_time_basis=fact.effective_time_basis,
                artifact_keys=(
                    list(fact.artifact_keys[:20])
                    if fact.artifact_keys is not None
                    else artifact_keys[:20]
                ),
                coverage_keys=[manifest_key],
                score_eligible=fact.score_eligible,
                discovery_only=fact.discovery_only,
                source_actor_key=fact.source_actor_key or f"publisher:{_host(fact.source_url)}",
                origin_fingerprint=origin,
                causal_root_key=fact.causal_root_key or origin,
            )
        )
    terminal = status or (
        "partial"
        if completeness == "partial" or truncated or failed
        else "success"
        if observations
        else "success_empty"
    )
    execution = ScannerExecutionSummary(
        company_key=company,
        scanner_id=scanner_id,
        scanner_version=definition.version,
        extractor_version=definition.extractor_version,
        scope_key=scope or surface,
        config_hash=_hash(
            definition.version, definition.max_requests, definition.max_response_bytes
        ),
        status=terminal,  # type: ignore[arg-type]
        started_at=now,
        finished_at=now,
        next_due_at=now + timedelta(seconds=definition.ttl_seconds),
        byte_count=sum(item.byte_count for item in artifacts),
        artifact_keys=artifact_keys[:100],
        coverage_keys=[manifest_key],
        observation_count=len(observations),
        errors=[_text(item, 500) for item in errors][:20] or (
            [f"{terminal}: {scanner_id} / {surface} / {company}: "
             f"{applicable_reason or ('no attributable evidence for scope ' + (scope or surface))}"]
            if terminal in {"failed", "transient_error", "identity_ambiguous",
                            "robots_blocked", "rate_limited"} else []
        ),
        metadata={
            "safe_claim": definition.safe_claim,
            "prohibited_claims": list(definition.prohibited_claims),
        },
    )
    penalty = min(1.0, definition.max_requests / 20)
    schedule = ScannerScheduleDecision(
        company_key=company,
        scanner_id=scanner_id,
        scanner_version=definition.version,
        decided_at=now,
        applicable=terminal != "not_applicable",
        applicability_reason=applicable_reason,
        success_probability=definition.success_probability,
        expected_action_entropy_reduction=0.5 if terminal != "not_applicable" else 0.0,
        action_boundary_sensitivity=0.5 if terminal != "not_applicable" else 0.0,
        evidence_value=definition.evidence_value,
        cost_penalty=penalty,
        expected_information_gain=(
            definition.success_probability * 0.25 * definition.evidence_value - penalty
        ),
        selected=True,
        selection_reason="explicit scanner execution",
        propensity=1.0,
        metadata={"eig_dimensions": list(definition.eig_dimensions)},
    )
    return FrontierIntelligence(
        executions=[execution],
        coverage_manifests=[coverage],
        evidence_artifacts=list(artifacts),
        observations=observations,
        schedule_decisions=[schedule],
        features=_json(dict(features or {})),
        updated_at=now,
    )


def _artifact_keys_for_sources(
    artifacts: Sequence[EvidenceArtifact],
    sources: Iterable[str],
) -> tuple[str, ...]:
    """Return only the artifacts whose exact fetched surfaces support a fact."""

    by_url = {
        _canonical_surface_url(item.canonical_url, item.canonical_url): item.artifact_key
        for item in artifacts
        if item.canonical_url
    }
    return tuple(
        dict.fromkeys(
            artifact_key
            for source in sources
            if source and (artifact_key := by_url.get(_canonical_surface_url(source, source)))
        )
    )


def _empty(
    scanner_id: str,
    company_key: str,
    reason: str,
    *,
    observed_at: datetime | None = None,
    status: Literal[
        "success_empty", "not_applicable", "identity_ambiguous", "failed", "transient_error"
    ] = "not_applicable",
    errors: Sequence[str] = (),
) -> FrontierIntelligence:
    safe_empty = status in {"success_empty", "not_applicable"}
    completeness: Literal["none", "partial", "complete", "not_applicable"] = "none"
    if status == "success_empty":
        completeness = "complete"
    elif status == "not_applicable":
        completeness = "not_applicable"
    return _result(
        scanner_id,
        company_key,
        observed_at=observed_at,
        status=status,
        surface="applicability",
        completeness=completeness,
        absence_interpretable=safe_empty,
        limitations=[reason],
        errors=errors,
        applicable_reason=reason,
    )


_ASSET_QUERY_KEYS = {
    "auto",
    "crop",
    "dpr",
    "fit",
    "fm",
    "format",
    "h",
    "height",
    "q",
    "quality",
    "resize",
    "v",
    "version",
    "w",
    "width",
}
_IMAGE_FORMATS = {"avif", "gif", "heic", "jpeg", "jpg", "png", "svg", "webp"}
_VIDEO_FORMATS = {"m3u8", "mov", "mp4", "webm"}


def _asset_urls(soup: BeautifulSoup, base_url: str) -> tuple[list[dict[str, Any]], bool]:
    results: list[dict[str, Any]] = []

    def add(raw: str, node: Tag | None = None, *, context: str = "") -> None:
        url = _safe_url(raw, base_url)
        if url and len(results) <= _MAX_ASSETS:
            results.append(
                {
                    "url": url,
                    "width": _positive_int(node.get("width")) if node else None,
                    "height": _positive_int(node.get("height")) if node else None,
                    "tag": node.name if node else "meta",
                    "context": context
                    or _text(
                        f"{node.get('alt') or ''} {node.get('class') or ''}" if node else "",
                        300,
                    ),
                }
            )

    for node in soup.find_all(["img", "source", "video"]):
        for attribute in ("src", "data-src", "data-original", "poster"):
            if value := str(node.get(attribute) or "").strip():
                add(value, node)
        for attribute in ("srcset", "data-srcset"):
            for candidate in str(node.get(attribute) or "").split(","):
                parts = candidate.strip().split(maxsplit=1)
                if parts:
                    add(parts[0], node)
    for node in soup.find_all("meta"):
        key = str(node.get("property") or node.get("name") or "").casefold()
        if key in {"og:image", "og:image:url", "og:video", "twitter:image"}:
            add(str(node.get("content") or ""), context=key)
    deduped = {item["url"]: item for item in results[:_MAX_ASSETS]}
    return list(deduped.values()), len(results) > _MAX_ASSETS


def _asset_format(url: str) -> str:
    parsed = urlparse(url)
    query = {key.casefold(): value.casefold() for key, value in parse_qsl(parsed.query)}
    explicit = query.get("fm") or query.get("format")
    if explicit in _IMAGE_FORMATS | _VIDEO_FORMATS:
        return explicit
    suffix = parsed.path.rsplit(".", 1)[-1].casefold() if "." in parsed.path else ""
    return suffix if suffix in _IMAGE_FORMATS | _VIDEO_FORMATS else "unknown"


def _master_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    parts = [part for part in parsed.path.split("/") if part]
    if host == "res.cloudinary.com" and "upload" in parts:
        upload = parts.index("upload")
        tail = parts[upload + 1 :]
        asset = next(
            (
                index
                for index, part in enumerate(tail)
                if re.fullmatch(r"v\d+", part) or "." in part
            ),
            len(tail),
        )
        if asset < len(tail) and re.fullmatch(r"v\d+", tail[asset]):
            asset += 1
        parts = parts[: upload + 1] + tail[asset:]
    if host == "ik.imagekit.io":
        parts = [part for part in parts if not part.startswith(("tr:", "tr-"))]
    query = sorted(
        (key.casefold(), value)
        for key, value in parse_qsl(parsed.query)
        if key.casefold() not in _ASSET_QUERY_KEYS
        and not key.casefold().startswith(("utm_", "cache", "cb", "__"))
    )
    path = "/" + "/".join(parts)
    stem, dot, suffix = path.rpartition(".")
    if dot:
        stem = re.sub(
            r"(?:@\d+x|[-_](?:\d{2,5}x\d{2,5}|w[-_]?\d{2,5}|\d{2,5}w))$",
            "",
            stem,
            flags=re.I,
        )
        path = f"{stem}.{suffix}"
    return urlunparse(
        (
            parsed.scheme.casefold(),
            host,
            path,
            "",
            urlencode(query),
            "",
        )
    )


def _dam(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    parts = [part for part in parsed.path.split("/") if part]
    if host == "res.cloudinary.com" and parts:
        return "cloudinary", parts[0].casefold()
    if host.endswith(".imgix.net"):
        return "imgix", host.removesuffix(".imgix.net")
    if host == "images.ctfassets.net" and parts:
        return "contentful", parts[0].casefold()
    if host == "cdn.sanity.io" and len(parts) > 2 and parts[0] == "images":
        return "sanity", parts[1].casefold()
    if host.endswith(".bynder.com"):
        return "bynder", host.removesuffix(".bynder.com")
    if host == "ik.imagekit.io" and parts:
        return "imagekit", parts[0].casefold()
    return "", ""


def _dimensions(record: Mapping[str, Any]) -> tuple[int | None, int | None]:
    query = {key.casefold(): value for key, value in parse_qsl(urlparse(str(record["url"])).query)}
    width = _positive_int(record.get("width")) or _positive_int(
        query.get("w") or query.get("width")
    )
    height = _positive_int(record.get("height")) or _positive_int(
        query.get("h") or query.get("height")
    )
    return width, height


def _aspect(width: int | None, height: int | None) -> str:
    if not width or not height:
        return "unknown"
    ratio = width / height
    if 0.92 <= ratio <= 1.08:
        return "square"
    if ratio >= 1.9:
        return "wide"
    if ratio > 1.08:
        return "landscape"
    return "tall" if ratio <= 0.55 else "portrait"


def _meaningful_asset(record: Mapping[str, Any]) -> bool:
    parsed = urlparse(str(record.get("url") or ""))
    context = f"{parsed.path} {record.get('context') or ''}".casefold()
    if re.search(
        r"(?:^|[/_.-])(logo|icon|favicon|avatar|badge|pixel|spacer|emoji|sprite)(?:[/_.-]|$)",
        context,
    ):
        return False
    width, height = _dimensions(record)
    if width and height and width <= 96 and height <= 96:
        return False
    if record.get("tag") == "video" or "og:" in context:
        return True
    if width and height and max(width, height) >= 300:
        return True
    return bool(
        re.search(
            r"(?:hero|banner|campaign|product|collection|gallery|creative|content|media|upload)",
            context,
        )
    )


def parse_creative_asset_genome(
    html: str | bytes,
    url: str,
    *,
    company_key: str = "",
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    scanner = "creative_asset_genome_v1"
    now = _now(observed_at)
    source = _safe_url(url)
    company = _host(company_key) or _host(source)
    if not source or not _same_company(source, company):
        return _empty(
            scanner,
            company,
            "HTML is not on the canonical company domain.",
            observed_at=now,
            status="identity_ambiguous",
        )
    raw = html if isinstance(html, bytes) else html.encode()
    bounded = raw[:_MAX_DOCUMENT_BYTES]
    document_truncated = len(raw) > len(bounded)
    soup = parse_html(bounded.decode(errors="replace"), "html.parser")
    assets, asset_truncated = _asset_urls(soup, source)
    concepts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    dams: dict[tuple[str, str], set[str]] = defaultdict(set)
    formats: set[str] = set()
    aspects: set[str] = set()
    meaningful_assets = [asset for asset in assets if _meaningful_asset(asset)]
    for asset in meaningful_assets:
        master = _master_url(str(asset["url"]))
        width, height = _dimensions(asset)
        fmt = _asset_format(str(asset["url"]))
        aspect = _aspect(width, height)
        concepts[master].append({"format": fmt, "aspect": aspect})
        formats.add(fmt)
        aspects.add(aspect)
        provider, tenant = _dam(str(asset["url"]))
        if provider:
            dams[(provider, tenant)].add(master)
    formats.discard("unknown")
    aspects.discard("unknown")
    summary = (
        f"The bounded sample exposes {len(concepts)} media URL groups across "
        f"{len(formats)} formats and {len(aspects)} aspect families."
    )
    facts: list[_Fact] = []
    if concepts:
        facts.extend(
            [
                _Fact(
                    "creative.asset_concept_family_count",
                    len(concepts),
                    "integer",
                    f"page:{source}",
                    summary,
                    source,
                    {
                        "measurement_type": "media_url_groups_not_semantic_concepts",
                        "sampled_asset_urls": len(assets),
                        "meaningful_asset_urls": len(meaningful_assets),
                    },
                    "concept_families",
                    score_eligible=len(concepts) >= 3,
                ),
                _Fact(
                    "creative.asset_format_count",
                    len(formats),
                    "integer",
                    f"page:{source}",
                    f"Observed formats: {', '.join(sorted(formats)) or 'none'}.",
                    source,
                    {"formats": sorted(formats)},
                    "formats",
                    discovery_only=True,
                ),
                _Fact(
                    "creative.aspect_family_count",
                    len(aspects),
                    "integer",
                    f"page:{source}",
                    f"Observed aspect families: {', '.join(sorted(aspects)) or 'none'}.",
                    source,
                    {"aspect_families": sorted(aspects)},
                    "aspect_families",
                    discovery_only=True,
                ),
            ]
        )
    for master, renditions in sorted(concepts.items())[:100]:
        facts.append(
            _Fact(
                "creative.asset_concept",
                _hash(master)[:24],
                "string",
                f"asset:{_hash(master)[:24]}",
                f"Observed one public asset family with {len(renditions)} rendition(s).",
                source,
                {
                    "source_master_url": master,
                    "rendition_count": len(renditions),
                    "formats": sorted({item["format"] for item in renditions}),
                    "aspect_families": sorted({item["aspect"] for item in renditions}),
                },
                discovery_only=True,
                causal_root_key=f"asset:{_hash(master)}",
            )
        )
    for (provider, tenant), masters in sorted(dams.items()):
        facts.append(
            _Fact(
                "creative.dam_tenant",
                {"provider": provider, "tenant": tenant},
                "json",
                f"dam:{provider}:{tenant}",
                f"Public URLs expose a {provider} tenant across {len(masters)} asset families.",
                source,
                {"concept_family_count": len(masters)},
                confidence="medium",
                confidence_method="provider_specific_url_pattern",
                discovery_only=True,
            )
        )
    document = FrontierDocument(
        source, bounded, "first_party_html", mime_type="text/html", publisher=company
    )
    artifact = _artifact(
        scanner, company, document, now, excerpt=summary, truncated=document_truncated
    )
    truncated = document_truncated or asset_truncated
    return _result(
        scanner,
        company,
        facts=facts,
        artifacts=[artifact],
        observed_at=now,
        surface="saved_first_party_html_assets",
        scope=source,
        attempted=1,
        successful=1,
        total=1,
        completeness="partial" if truncated else "complete",
        truncated=truncated,
        absence_interpretable=not truncated,
        limitations=("Only assets referenced by the bounded saved document were inspected.",),
        features={
            "creative_asset_genome": {
                "concept_family_count": len(concepts),
                "formats": sorted(formats),
                "aspect_families": sorted(aspects),
                "dam_tenants": [
                    {"provider": provider, "tenant": tenant, "concepts": len(masters)}
                    for (provider, tenant), masters in sorted(dams.items())
                ],
            }
        },
        applicable_reason="Saved first-party HTML was available.",
    )


def parse_localization_burden(
    html: str | bytes,
    url: str,
    *,
    company_key: str = "",
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    scanner = "localization_burden_v1"
    now = _now(observed_at)
    source = _safe_url(url)
    company = _host(company_key) or _host(source)
    if not source or not _same_company(source, company):
        return _empty(
            scanner,
            company,
            "Localization page identity is ambiguous.",
            observed_at=now,
            status="identity_ambiguous",
        )
    raw = html if isinstance(html, bytes) else html.encode()
    bounded = raw[:_MAX_DOCUMENT_BYTES]
    truncated = len(raw) > len(bounded)
    soup = parse_html(bounded.decode(errors="replace"), "html.parser")
    variants: dict[tuple[str, str], str] = {}
    for node in soup.find_all("link", href=True):
        rel = {str(item).casefold() for item in node.get("rel", [])}
        locale = _text(node.get("hreflang"), 30).casefold().replace("_", "-")
        alternate = _safe_url(str(node.get("href") or ""), source)
        if (
            "alternate" in rel
            and alternate
            and (locale == "x-default" or re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,3})?", locale))
        ):
            variants[(locale, alternate)] = "html_hreflang"
    facts: list[_Fact] = []
    if variants:
        facts.append(
            _Fact(
                "localization.declared_variant_count",
                len(variants),
                "integer",
                f"page:{source}",
                f"The page declares {len(variants)} language or region alternate URLs.",
                source,
                {"locales": sorted({locale for locale, _ in variants if locale != "x-default"})},
                "declared_alternates",
                score_eligible=True,
            )
        )
    for locale, alternate in sorted(variants)[:100]:
        facts.append(
            _Fact(
                "localization.locale_variant",
                {"locale": locale, "url": alternate},
                "json",
                f"locale:{locale}:{_hash(alternate)[:16]}",
                f"The page declares a {locale} alternate.",
                source,
                {"declaration_method": "html_hreflang"},
                discovery_only=True,
                causal_root_key=f"localized-page:{_master_url(alternate)}",
            )
        )
    document = FrontierDocument(
        source, bounded, "first_party_html", mime_type="text/html", publisher=company
    )
    artifact = _artifact(
        scanner,
        company,
        document,
        now,
        excerpt=f"{len(variants)} hreflang alternates",
        truncated=truncated,
    )
    return _result(
        scanner,
        company,
        facts=facts,
        artifacts=[artifact],
        observed_at=now,
        surface="first_party_localization_declarations",
        scope=source,
        attempted=1,
        successful=1,
        total=1,
        completeness="partial" if truncated else "complete",
        truncated=truncated,
        absence_interpretable=not truncated,
        limitations=("Declared alternates do not establish regional revenue or staffing.",),
        features={
            "localization_burden": {
                "declared_variant_count": len(variants),
                "locales": sorted({locale for locale, _ in variants if locale != "x-default"}),
            }
        },
        applicable_reason="Saved first-party HTML was available.",
    )


_ARCHIVE_RE = re.compile(
    r"campaign-archive|newsletter(?:s|-archive)?|email-archive|past-issues", re.I
)
_CAMPAIGN_HOSTS = ("mailchi.mp", "campaign-archive.com", "list-manage.com")
_MONTH_DATE_RE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+"
    r"\d{1,2},?\s+20\d{2}\b",
    re.I,
)


def discover_lifecycle_archive_urls(html: str | bytes, url: str) -> list[str]:
    return discover_frontier_request_surfaces(html, url)["lifecycle_email_exhaust_v1"][:5]


def _page_date(soup: BeautifulSoup) -> datetime | None:
    for node in soup.find_all(["time", "meta"]):
        value = ""
        if node.name == "time":
            value = str(node.get("datetime") or node.get_text(" ", strip=True))
        else:
            key = str(node.get("property") or node.get("name") or "").casefold()
            if key in {"article:published_time", "date", "datepublished"}:
                value = str(node.get("content") or "")
        if parsed := _date(value):
            return parsed
    match = _MONTH_DATE_RE.search(soup.get_text(" ", strip=True))
    return _date(match.group()) if match else None


def parse_lifecycle_archive(
    html: str | bytes,
    url: str,
    *,
    company_key: str = "",
    first_party_source_url: str = "",
    campaign_pages: Mapping[str, str | bytes] | None = None,
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    scanner = "lifecycle_email_exhaust_v1"
    now = _now(observed_at)
    source = _safe_url(url)
    company = _host(company_key) or _host(first_party_source_url) or _host(source)
    cited = bool(first_party_source_url and _same_company(first_party_source_url, company))
    if not source or not (_same_company(source, company) or cited):
        return _empty(
            scanner,
            company,
            "External archive lacks a cited first-party link.",
            observed_at=now,
            status="identity_ambiguous",
        )
    raw = html if isinstance(html, bytes) else html.encode()
    bounded = raw[:_MAX_DOCUMENT_BYTES]
    truncated = len(raw) > len(bounded)
    soup = parse_html(bounded.decode(errors="replace"), "html.parser")
    campaigns: dict[str, tuple[str, datetime | None]] = {}
    for anchor in soup.find_all("a", href=True):
        if anchor.find_parent(["nav", "footer", "header"]):
            continue
        candidate = _safe_url(str(anchor.get("href") or ""), source)
        host = _host(candidate)
        if not candidate or candidate == source:
            continue
        provider_campaign = any(
            host == item or host.endswith(f".{item}") for item in _CAMPAIGN_HOSTS
        )
        path_campaign = bool(
            re.search(
                r"/(?:campaigns?|newsletters?|email|past-issues?|issues?)/",
                urlparse(candidate).path.casefold(),
            )
        )
        if not (provider_campaign or path_campaign):
            continue
        parent = anchor.find_parent(["article", "li", "tr", "div"])
        context = _text(parent.get_text(" ", strip=True) if isinstance(parent, Tag) else "", 500)
        match = _MONTH_DATE_RE.search(context)
        campaigns[candidate] = (
            _text(anchor.get_text(" ", strip=True) or context, 300),
            _date(match.group()) if match else None,
        )
    supplied = campaign_pages or {}
    details: dict[str, tuple[str, datetime | None]] = {}
    fetched_campaign_urls: set[str] = set()
    documents = [
        FrontierDocument(
            source,
            bounded,
            "public_lifecycle_archive",
            mime_type="text/html",
            publisher=_host(source),
            first_party_source_url=first_party_source_url,
            officially_linked=True,
        )
    ]
    for campaign_url, (title, published) in list(campaigns.items())[:8]:
        content = supplied.get(campaign_url)
        if content is not None:
            campaign_raw = content if isinstance(content, bytes) else content.encode()
            campaign_soup = parse_html(
                campaign_raw[:_MAX_DOCUMENT_BYTES].decode(errors="replace"), "html.parser"
            )
            published = _page_date(campaign_soup) or published
            title = _text(
                campaign_soup.title.get_text(" ", strip=True) if campaign_soup.title else title, 300
            )
            documents.append(
                FrontierDocument(
                    campaign_url,
                    campaign_raw[:_MAX_DOCUMENT_BYTES],
                    "public_campaign_page",
                    mime_type="text/html",
                    officially_linked=True,
                )
            )
            fetched_campaign_urls.add(campaign_url)
        details[campaign_url] = (title, published)
    facts: list[_Fact] = []
    dated = sum(published is not None for _, published in details.values())
    if campaigns:
        facts.append(
            _Fact(
                "lifecycle.public_campaign_count",
                len(campaigns),
                "integer",
                f"archive:{source}",
                f"The first-party-linked archive exposes {len(campaigns)} campaign pages.",
                source,
                {"dated_detail_sample": dated, "detail_cap": 8},
                "public_campaign_pages",
                score_eligible=dated > 0,
            )
        )
    for campaign_url, (title, published) in details.items():
        facts.append(
            _Fact(
                "lifecycle.campaign",
                {"url": campaign_url, "title": title},
                "json",
                f"campaign:{_hash(campaign_url)[:24]}",
                f"Public campaign page: {title or campaign_url}.",
                campaign_url if campaign_url in fetched_campaign_urls else source,
                {"first_party_linked_archive": source},
                effective_at=published,
                effective_time_basis="source_explicit" if published else "unknown",
                score_eligible=published is not None,
                discovery_only=published is None,
                causal_root_key=f"campaign:{_hash(campaign_url)}",
            )
        )
    artifacts = []
    for document in documents:
        published = details.get(document.url, ("", None))[1]
        artifacts.append(
            _artifact(
                scanner,
                company,
                document,
                now,
                published_at=published,
                published_basis="source_explicit" if published else "unknown",
                truncated=truncated and document.url == source,
            )
        )
    detail_target = min(8, len(campaigns))
    fetched_details = len(documents) - 1
    partial = truncated or fetched_details < detail_target
    return _result(
        scanner,
        company,
        facts=facts,
        artifacts=artifacts,
        observed_at=now,
        surface="first_party_linked_public_campaign_archive",
        scope=source,
        attempted=1 + fetched_details,
        successful=1 + fetched_details,
        total=1 + detail_target,
        completeness="partial" if partial else "complete",
        truncated=truncated or len(campaigns) > 8,
        absence_interpretable=not campaigns and not truncated,
        limitations=(
            "Public archives do not establish send volume, audience, automation, or results.",
        ),
        features={
            "lifecycle_email_exhaust": {
                "public_campaign_count": len(campaigns),
                "dated_detail_count": dated,
            }
        },
        applicable_reason="A first-party-published lifecycle archive was supplied.",
    )


def _association_apps(payload: Any, platform: str) -> list[dict[str, Any]]:
    apps: list[dict[str, Any]] = []
    if platform == "android":
        for statement in payload[:100] if isinstance(payload, list) else []:
            target = statement.get("target", {}) if isinstance(statement, dict) else {}
            relations = statement.get("relation", []) if isinstance(statement, dict) else []
            package = _text(target.get("package_name"), 300) if isinstance(target, dict) else ""
            if (
                isinstance(target, dict)
                and target.get("namespace") == "android_app"
                and re.fullmatch(r"[A-Za-z0-9_.]+", package)
            ):
                apps.append(
                    {
                        "platform": "android",
                        "app_id": package,
                        "relations": sorted({_text(item, 200) for item in relations if item}),
                    }
                )
        return apps
    if not isinstance(payload, dict):
        return apps
    applinks = payload.get("applinks", {})
    details = applinks.get("details", []) if isinstance(applinks, dict) else []
    for detail in details[:100] if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        identifiers: list[Any] = [detail["appID"]] if detail.get("appID") else []
        if isinstance(detail.get("appIDs"), list):
            identifiers.extend(detail["appIDs"])
        for raw in identifiers:
            app_id = _text(raw, 300)
            if re.fullmatch(r"[A-Z0-9]{5,20}\.[A-Za-z0-9_.-]+", app_id):
                apps.append(
                    {
                        "platform": "apple",
                        "app_id": app_id,
                        "bundle_id": app_id.split(".", 1)[1],
                    }
                )
    return apps


def parse_app_release_pulse(
    association: str | bytes | Mapping[str, Any] | Sequence[Any],
    association_url: str,
    *,
    company_key: str = "",
    apple_lookup: Mapping[str, Any] | None = None,
    first_party_app_ids: Sequence[str | int] = (),
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    scanner = "app_release_pulse_v1"
    now = _now(observed_at)
    source = _safe_url(association_url)
    company = _host(company_key) or _host(source)
    if not source or not _same_company(source, company):
        return _empty(
            scanner,
            company,
            "Association file is not on the company domain.",
            observed_at=now,
            status="identity_ambiguous",
        )
    body = (
        association
        if isinstance(association, bytes)
        else association.encode()
        if isinstance(association, str)
        else json.dumps(association, sort_keys=True).encode()
    )
    bounded = body[:_MAX_DOCUMENT_BYTES]
    truncated = len(body) > len(bounded)
    try:
        payload = json.loads(bounded)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _empty(
            scanner,
            company,
            "Association file is invalid JSON.",
            observed_at=now,
            status="failed",
            errors=[f"{source}: Association file is invalid JSON: {_text(exc, 300)}"],
        )
    platform = "android" if urlparse(source).path.endswith("assetlinks.json") else "apple"
    if not isinstance(payload, list if platform == "android" else dict):
        return _empty(
            scanner, company, "Association document has an invalid JSON structure.",
            observed_at=now, status="failed",
            errors=[f"{source}: Association file is invalid JSON structure for {platform}."],
        )
    apps = _association_apps(payload, platform)
    facts = [
        _Fact(
            "app.domain_association",
            app,
            "json",
            f"app:{app['platform']}:{app['app_id']}",
            f"The company domain publicly associates with {app['platform']} app {app['app_id']}.",
            source,
            score_eligible=True,
            source_actor_key=f"domain:{company}",
        )
        for app in apps
    ]
    lookup_artifact: EvidenceArtifact | None = None
    results = apple_lookup.get("results", []) if isinstance(apple_lookup, Mapping) else []
    count = (
        apple_lookup.get("resultCount", len(results)) if isinstance(apple_lookup, Mapping) else 0
    )
    if isinstance(results, list) and _positive_int(count) == 1 and isinstance(results[0], dict):
        result = results[0]
        track_id = str(result.get("trackId") or "")
        bundle_id = _text(result.get("bundleId"), 300)
        seller_url = _safe_url(str(result.get("sellerUrl") or ""))
        association_bundles = {str(item.get("bundle_id")) for item in apps if item.get("bundle_id")}
        numeric_ids = {str(item) for item in first_party_app_ids if str(item).isdigit()}
        basis = ""
        if track_id in numeric_ids:
            basis = "first_party_numeric_app_id"
        elif bundle_id and bundle_id in association_bundles:
            basis = "associated_bundle_id"
        elif seller_url and _same_company(seller_url, company):
            basis = "seller_url_domain"
        if basis and track_id:
            released = _date(result.get("currentVersionReleaseDate") or result.get("releaseDate"))
            lookup_url = f"https://itunes.apple.com/lookup?id={track_id}"
            facts.append(
                _Fact(
                    "app.store_release",
                    {
                        "track_id": track_id,
                        "bundle_id": bundle_id,
                        "version": _text(result.get("version"), 100),
                        "category": _text(result.get("primaryGenreName"), 200),
                        "seller": _text(result.get("sellerName"), 300),
                    },
                    "json",
                    f"app:apple-store:{track_id}",
                    (
                        f"Apple reports version {_text(result.get('version'), 100)} "
                        "for the associated app."
                    ),
                    lookup_url,
                    {
                        "identity_basis": basis,
                        "supported_languages": result.get("languageCodesISO2A", [])
                        if isinstance(result.get("languageCodesISO2A"), list)
                        else [],
                        "rating_count_reported": _positive_int(result.get("userRatingCount")),
                    },
                    effective_at=released,
                    effective_time_basis="source_explicit" if released else "unknown",
                    score_eligible=released is not None,
                    discovery_only=released is None,
                    causal_root_key=f"app-release:{track_id}:{result.get('version', '')}",
                )
            )
            lookup_document = FrontierDocument(
                lookup_url,
                json.dumps(apple_lookup, sort_keys=True, default=str),
                "apple_lookup_metadata",
                mime_type="application/json",
                publisher="Apple",
            )
            lookup_artifact = _artifact(
                scanner,
                company,
                lookup_document,
                now,
                published_at=released,
                published_basis="source_explicit" if released else "unknown",
            )
    document = FrontierDocument(
        source,
        bounded,
        f"{platform}_domain_association",
        mime_type="application/json",
        publisher=company,
    )
    artifacts = [_artifact(scanner, company, document, now, truncated=truncated)]
    if lookup_artifact:
        artifacts.append(lookup_artifact)
    return _result(
        scanner,
        company,
        facts=facts,
        artifacts=artifacts,
        observed_at=now,
        surface="domain_app_association_and_exact_apple_lookup",
        scope=source,
        attempted=1 + int(apple_lookup is not None),
        successful=1 + int(lookup_artifact is not None),
        total=1 + int(apple_lookup is not None),
        completeness="partial" if truncated else "complete",
        truncated=truncated,
        absence_interpretable=not truncated,
        limitations=("Android output is association evidence only; Google Play is not scraped.",),
        features={
            "app_release_pulse": {
                "associated_apps": apps,
                "apple_lookup_matched": bool(lookup_artifact),
            }
        },
        applicable_reason="A canonical-domain association file was supplied.",
    )


_PROGRAM_MARKERS = (
    "affiliate program",
    "ambassador program",
    "brand ambassador",
    "creator program",
    "influencer program",
    "referral program",
    "ugc creator",
)


def discover_creator_program_urls(html: str | bytes, url: str) -> list[str]:
    return discover_frontier_request_surfaces(html, url)["creator_program_contract_v1"][:5]


def parse_creator_program_contract(
    html: str | bytes,
    url: str,
    *,
    company_key: str = "",
    first_party_source_url: str = "",
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    scanner = "creator_program_contract_v1"
    now = _now(observed_at)
    source = _safe_url(url)
    company = _host(company_key) or _host(first_party_source_url) or _host(source)
    cited = bool(first_party_source_url and _same_company(first_party_source_url, company))
    if not source or not (_same_company(source, company) or cited):
        return _empty(
            scanner,
            company,
            "External program page lacks a cited first-party link.",
            observed_at=now,
            status="identity_ambiguous",
        )
    raw = html if isinstance(html, bytes) else html.encode()
    bounded = raw[:_MAX_DOCUMENT_BYTES]
    truncated = len(raw) > len(bounded)
    soup = parse_html(bounded.decode(errors="replace"), mutable=True)
    for node in soup(["script", "style", "noscript", "template"]):
        node.decompose()
    text = _text(soup.get_text(" ", strip=True), 100_000)
    lowered = text.casefold()
    program_types = [
        item for item in _PROGRAM_MARKERS if item in f"{urlparse(source).path.casefold()} {lowered}"
    ]
    if not program_types:
        return _empty(
            scanner,
            company,
            "Page does not explicitly describe a creator program.",
            observed_at=now,
        )
    channels = sorted(
        {
            item
            for item in ("instagram", "tiktok", "youtube", "facebook", "pinterest")
            if re.search(rf"\b{item}\b", lowered)
        }
    )
    format_markers = (
        "blog post",
        "photo",
        "reel",
        "social post",
        "story",
        "ugc",
        "unboxing",
        "video",
    )
    obligation_sentences = [
        sentence
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text)
        if re.search(
            r"\b(?:must|required|requirement|deliver|create|post|publish|submit|provide|"
            r"you will|you'll|expected to)\b",
            sentence,
            re.I,
        )
        and any(marker in sentence.casefold() for marker in format_markers)
    ]
    formats = sorted(
        {
            marker
            for sentence in obligation_sentences[:30]
            for marker in format_markers
            if marker in sentence.casefold()
        }
    )
    cadence = list(
        dict.fromkeys(
            _text(match.group(), 200)
            for sentence in obligation_sentences[:30]
            for match in re.finditer(
                r"\b(?:at least\s*)?\d{1,2}\s+(?:posts?|photos?|videos?|reels?|stories?)"
                r"(?:\s+per\s+(?:week|month|campaign))?\b",
                sentence,
                re.I,
            )
        )
    )[:10]
    rights_patterns = {
        "perpetual_usage": r"\b(?:perpetual|in perpetuity)\b.{0,80}\b(?:use|license|rights)\b",
        "paid_media_usage": r"\b(?:paid media|paid social|whitelisting|spark ads?)\b",
        "content_license": r"\b(?:usage rights?|right to use|reuse|repurpose|license)\b",
        "exclusivity": r"\b(?:exclusive|exclusivity|non-compete)\b",
    }
    rights = {
        key: _text(match.group(), 250)
        for key, pattern in rights_patterns.items()
        if (match := re.search(pattern, text, re.I))
    }
    incentives = list(
        dict.fromkeys(
            _text(match.group(), 100)
            for match in re.finditer(
                r"(?:\b\d{1,2}(?:\.\d+)?%\s+(?:commission|of sales)|"
                r"\$\d[\d,]*\s+per\s+(?:post|video|sale))",
                text,
                re.I,
            )
        )
    )[:10]
    program = {
        "program_type": program_types[0],
        "channels": channels,
        "content_formats": formats,
        "cadence_requirements": cadence,
        "rights_categories": sorted(rights),
        "incentives_disclosed": incentives,
        "obligation_excerpts": [_text(item, 300) for item in obligation_sentences[:5]],
    }
    facts = [
        _Fact(
            "creator.program",
            program,
            "json",
            f"creator-program:{_hash(source)[:24]}",
            f"The company publicly describes a {program_types[0]}.",
            source,
            {"first_party_source_url": first_party_source_url},
            score_eligible=bool(formats or cadence),
            discovery_only=not bool(formats or cadence),
            causal_root_key=f"creator-program:{_hash(source)}",
        )
    ]
    for requirement in cadence:
        facts.append(
            _Fact(
                "creator.content_requirement",
                requirement,
                "string",
                f"creator-requirement:{_hash(source, requirement)[:24]}",
                f"Public program requirement: {requirement}.",
                source,
                discovery_only=True,
                causal_root_key=f"creator-program:{_hash(source)}",
            )
        )
    for category, excerpt in rights.items():
        facts.append(
            _Fact(
                "creator.usage_right",
                category,
                "string",
                f"creator-right:{_hash(source, category)[:24]}",
                f"The public program contains a {category.replace('_', ' ')} clause.",
                source,
                {"matched_excerpt": excerpt},
                discovery_only=True,
                causal_root_key=f"creator-program:{_hash(source)}",
            )
        )
    document = FrontierDocument(
        source,
        bounded,
        "public_creator_program_terms",
        mime_type="text/html",
        publisher=_host(source),
        first_party_source_url=first_party_source_url,
        officially_linked=True,
    )
    artifact = _artifact(
        scanner, company, document, now, excerpt=f"Public {program_types[0]}", truncated=truncated
    )
    return _result(
        scanner,
        company,
        facts=facts,
        artifacts=[artifact],
        observed_at=now,
        surface="first_party_linked_creator_program_terms",
        scope=source,
        attempted=1,
        successful=1,
        total=1,
        completeness="partial" if truncated else "complete",
        truncated=truncated,
        absence_interpretable=not truncated,
        limitations=(
            "Forms are never submitted; disclosed incentives do not establish payment or spend.",
        ),
        features={"creator_program_contract": program},
        applicable_reason="An explicit first-party-linked program page was supplied.",
    )


def build_dol_lca_index(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, tuple[Mapping[str, Any], ...]]:
    index: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        employer = _name(_row(row, "EMPLOYER_NAME", "EMPLOYER_BUSINESS_NAME", "EMPLOYER"))
        if employer:
            index[employer].append(row)
    return {key: tuple(value) for key, value in index.items()}


def parse_dol_lca_index(
    index_or_rows: Mapping[str, Sequence[Mapping[str, Any]]] | Iterable[Mapping[str, Any]],
    *,
    company_key: str,
    legal_name: str,
    locations: Sequence[str],
    source_url: str = "https://www.dol.gov/agencies/eta/foreign-labor/performance",
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    scanner = "dol_lca_workforce_v1"
    now = _now(observed_at)
    index = (
        index_or_rows if isinstance(index_or_rows, Mapping) else build_dol_lca_index(index_or_rows)
    )
    legal = _name(legal_name)
    if not legal:
        return _empty(
            scanner, company_key, "Exact legal name is required for the DOL join.", observed_at=now
        )
    rows = list(index.get(legal, ()))
    if rows and not locations:
        return _empty(
            scanner,
            company_key,
            "Legal-name-only DOL match is ambiguous.",
            observed_at=now,
            status="identity_ambiguous",
        )
    identity_matched = [
        row
        for row in rows
        if _location_match(
            locations,
            (
                _row(row, "EMPLOYER_ADDRESS1", "EMPLOYER_ADDRESS"),
                _row(row, "EMPLOYER_CITY"),
                _row(row, "EMPLOYER_STATE"),
                _row(row, "EMPLOYER_POSTAL_CODE"),
                _row(row, "WORKSITE_CITY"),
                _row(row, "WORKSITE_STATE"),
            ),
        )
    ]
    if rows and not identity_matched:
        return _empty(
            scanner,
            company_key,
            "DOL name matched but location did not corroborate.",
            observed_at=now,
            status="identity_ambiguous",
        )
    rows_by_case: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in identity_matched:
        case_number = _text(_row(row, "CASE_NUMBER", "CASE_NO", "CASE_ID"), 200)
        identity = case_number or _hash(row)
        rows_by_case[identity].append(row)
    matched: list[Mapping[str, Any]] = []
    for case_rows in rows_by_case.values():
        dated = [
            (
                _date(_row(row, "RECEIVED_DATE", "CASE_SUBMITTED", "DECISION_DATE")),
                row,
            )
            for row in case_rows
        ]
        latest_date = max((date for date, _ in dated if date is not None), default=None)
        latest_rows = (
            [row for date, row in dated if date == latest_date]
            if latest_date is not None
            else case_rows
        )
        # Conflicting or terminal statuses at the latest known point fail closed.
        if any(
            _text(_row(row, "CASE_STATUS", "STATUS"), 100).casefold() != "certified"
            for row in latest_rows
        ):
            continue
        matched.append(latest_rows[-1])
    matched = matched[:200]
    facts: list[_Fact] = []
    total_positions = 0
    explicit_dates: list[datetime] = []
    for row in matched:
        positions = (
            _positive_int(_row(row, "TOTAL_WORKER_POSITIONS", "TOTAL_WORKERS", "WORKERS")) or 0
        )
        total_positions += positions
        case_number = _text(_row(row, "CASE_NUMBER", "CASE_NO", "CASE_ID"), 200)
        occupation = _text(_row(row, "SOC_TITLE", "JOB_TITLE"), 300)
        status = _text(_row(row, "CASE_STATUS", "STATUS"), 100)
        worksite = _text(
            f"{_row(row, 'WORKSITE_CITY') or ''} {_row(row, 'WORKSITE_STATE') or ''}", 300
        )
        filed = _date(_row(row, "RECEIVED_DATE", "CASE_SUBMITTED", "DECISION_DATE"))
        if filed:
            explicit_dates.append(filed)
        wage_from = _decimal(_row(row, "WAGE_RATE_OF_PAY_FROM", "WAGE_RATE_FROM"))
        wage_to = _decimal(_row(row, "WAGE_RATE_OF_PAY_TO", "WAGE_RATE_TO"))
        facts.append(
            _Fact(
                "workforce.lca_record",
                {
                    "case_number": case_number,
                    "case_status": status,
                    "occupation": occupation,
                    "worksite": worksite,
                    "certified_positions_reported": positions,
                    "wage_from": str(wage_from) if wage_from is not None else None,
                    "wage_to": str(wage_to) if wage_to is not None else None,
                    "wage_unit": _text(_row(row, "WAGE_UNIT_OF_PAY", "WAGE_UNIT"), 100),
                },
                "json",
                f"dol-lca:{case_number or _hash(row)[:24]}",
                (
                    f"DOL row {case_number or 'without a public id'} reports {positions} "
                    f"position(s) for {occupation or 'an occupation'}."
                ),
                source_url,
                {"as_reported_by_employer": True},
                effective_at=filed,
                effective_time_basis="filing_date" if filed else "unknown",
                discovery_only=True,
                source_actor_key="agency:us-dol-oflc",
                causal_root_key=f"dol-lca:{case_number or _hash(row)}",
            )
        )
    latest = max(explicit_dates, default=None)
    if matched:
        facts.insert(
            0,
            _Fact(
                "workforce.lca_certified_position_count",
                total_positions,
                "integer",
                f"employer:{legal}",
                (
                    f"Matched DOL rows report {total_positions} certified positions; "
                    "this is not headcount."
                ),
                source_url,
                {
                    "matched_record_count": len(matched),
                    "identity_basis": "exact_name_plus_location",
                },
                "certified_positions_reported",
                effective_at=latest,
                effective_time_basis="filing_date" if latest else "unknown",
                score_eligible=total_positions > 0 and latest is not None,
                discovery_only=latest is None,
                source_actor_key="agency:us-dol-oflc",
                causal_root_key=f"dol-employer:{legal}",
            ),
        )
    slice_body = json.dumps(matched, sort_keys=True, default=str)
    document = FrontierDocument(
        source_url,
        slice_body,
        "dol_lca_local_index_slice",
        mime_type="application/json",
        publisher="U.S. Department of Labor",
    )
    artifact = _artifact(
        scanner, company_key, document, now, excerpt=f"{len(matched)} exact-name/location DOL rows"
    )
    return _result(
        scanner,
        company_key,
        facts=facts,
        artifacts=[artifact],
        observed_at=now,
        surface="official_dol_lca_quarterly_index",
        scope=legal,
        attempted=1,
        successful=1,
        total=1,
        completeness="complete",
        absence_interpretable=True,
        limitations=(
            "Employer-reported disclosure rows do not prove actual hires or current employment.",
            "Certified positions are not company headcount or a budget estimate.",
        ),
        features={
            "dol_lca_workforce": {
                "matched_record_count": len(matched),
                "certified_positions_reported": total_positions,
            }
        },
        applicable_reason="Exact legal name and corroborating public location were available.",
    )


def build_sec_form_d_index(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, tuple[Mapping[str, Any], ...]]:
    index: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        issuer = _name(
            _row(row, "ISSUER_NAME", "ENTITY_NAME", "ISSUERENTITYNAME", "NAME_OF_ISSUER")
        )
        if issuer:
            index[issuer].append(row)
    return {key: tuple(value) for key, value in index.items()}


def _excluded_form_d(name: str, row: Mapping[str, Any]) -> bool:
    pooled = str(
        _row(row, "IS_POOLED_INVESTMENT_FUND", "ISPOOLEDINVESTMENTFUND") or ""
    ).casefold() in {"1", "true", "yes", "y", "x"}
    obvious_vehicle = re.search(
        r"\b(?:fund|lp|l p|spv|special purpose|investment vehicle|feeder|master fund)\b",
        _name(name),
    )
    return pooled or bool(obvious_vehicle)


def parse_sec_form_d_index(
    index_or_rows: Mapping[str, Sequence[Mapping[str, Any]]] | Iterable[Mapping[str, Any]],
    *,
    company_key: str,
    legal_name: str,
    locations: Sequence[str] = (),
    phone: str = "",
    source_url: str = "https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets",
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    scanner = "sec_form_d_capital_v1"
    now = _now(observed_at)
    index = (
        index_or_rows
        if isinstance(index_or_rows, Mapping)
        else build_sec_form_d_index(index_or_rows)
    )
    legal = _name(legal_name)
    if not legal:
        return _empty(scanner, company_key, "Exact legal issuer name is required.", observed_at=now)
    rows = [row for row in index.get(legal, ()) if not _excluded_form_d(legal_name, row)]
    normalized_phone = re.sub(r"\D", "", phone)
    matched: list[Mapping[str, Any]] = []
    for row in rows:
        address_match = bool(
            locations
            and _location_match(
                locations,
                (
                    _row(row, "STREET1", "ISSUER_STREET1", "ADDRESS1"),
                    _row(row, "CITY", "ISSUER_CITY"),
                    _row(row, "STATEORCOUNTRY", "STATE", "ISSUER_STATE"),
                    _row(row, "ZIPCODE", "ZIP", "POSTAL_CODE"),
                ),
            )
        )
        row_phone = re.sub(r"\D", "", str(_row(row, "PHONE", "ISSUER_PHONE") or ""))
        phone_match = bool(
            len(normalized_phone) >= 10
            and len(row_phone) >= 10
            and normalized_phone[-10:] == row_phone[-10:]
        )
        row_domain = _host(str(_row(row, "ISSUER_WEBSITE", "WEBSITE", "DOMAIN") or ""))
        if address_match or phone_match or (row_domain and _same_company(row_domain, company_key)):
            matched.append(row)
    if rows and not matched:
        return _empty(
            scanner,
            company_key,
            "SEC name matched without address, phone, or domain corroboration.",
            observed_at=now,
            status="identity_ambiguous",
        )
    matched = matched[:100]
    facts: list[_Fact] = []
    sold_values: list[float] = []
    for row in matched:
        accession = _text(_row(row, "ACCESSION_NUMBER", "ACCESSIONNUMBER", "ACCESSION_NO"), 100)
        amount_sold = _decimal(_row(row, "TOTAL_AMOUNT_SOLD", "TOTALAMOUNTSOLD", "AMOUNT_SOLD"))
        offering_amount = _decimal(
            _row(row, "TOTAL_OFFERING_AMOUNT", "TOTALOFFERINGAMOUNT", "OFFERING_AMOUNT")
        )
        filed = _date(_row(row, "FILING_DATE", "FILINGDATE"))
        first_sale = _date(_row(row, "DATE_OF_FIRST_SALE", "DATEOFFIRSTSALE"))
        amendment = str(_row(row, "IS_AMENDMENT", "ISAMENDMENT") or "").casefold() in {
            "1",
            "true",
            "yes",
            "y",
            "x",
        }
        entity = f"sec-form-d:{accession or _hash(row)[:24]}"
        offering_identity = (
            _text(
                _row(row, "OFFERING_DATA_ID", "FILE_NUMBER"),
                200,
            )
            or f"{legal}:{first_sale.date().isoformat() if first_sale else 'undated'}"
        )
        causal_root = f"sec-offering:{_hash(offering_identity)}"
        value = {
            "accession_number": accession,
            "filing_date": filed.isoformat() if filed else None,
            "first_sale_date": first_sale.isoformat() if first_sale else None,
            "amount_sold_reported": str(amount_sold) if amount_sold is not None else None,
            "total_offering_amount_reported": str(offering_amount)
            if offering_amount is not None
            else None,
            "amendment": amendment,
        }
        facts.append(
            _Fact(
                "capital.form_d_filing",
                value,
                "json",
                entity,
                f"Issuer {legal_name} filed a Form D notice reporting the literal filed fields.",
                source_url,
                {"as_filed": True, "identity_basis": "exact_name_plus_corroboration"},
                effective_at=filed,
                effective_time_basis="filing_date" if filed else "unknown",
                score_eligible=amount_sold is not None and filed is not None,
                discovery_only=filed is None,
                source_actor_key="agency:us-sec",
                causal_root_key=causal_root,
            )
        )
        if amount_sold is not None:
            sold_values.append(float(amount_sold))
            facts.append(
                _Fact(
                    "capital.form_d_amount_sold",
                    float(amount_sold),
                    "number",
                    entity,
                    f"The Form D notice literally reports amount sold of {amount_sold}.",
                    source_url,
                    {
                        "total_offering_amount_reported_separately": str(offering_amount)
                        if offering_amount is not None
                        else None,
                        "not_a_cash_or_runway_claim": True,
                    },
                    "USD_as_reported",
                    effective_at=filed,
                    effective_time_basis="filing_date" if filed else "unknown",
                    score_eligible=filed is not None,
                    discovery_only=filed is None,
                    source_actor_key="agency:us-sec",
                    causal_root_key=causal_root,
                )
            )
    document = FrontierDocument(
        source_url,
        json.dumps(matched, sort_keys=True, default=str),
        "sec_form_d_local_index_slice",
        mime_type="application/json",
        publisher="U.S. Securities and Exchange Commission",
    )
    artifact = _artifact(
        scanner,
        company_key,
        document,
        now,
        excerpt=f"{len(matched)} corroborated non-fund Form D rows",
    )
    return _result(
        scanner,
        company_key,
        facts=facts,
        artifacts=[artifact],
        observed_at=now,
        surface="official_sec_form_d_quarterly_index",
        scope=legal,
        attempted=1,
        successful=1,
        total=1,
        completeness="complete",
        absence_interpretable=True,
        limitations=(
            "Total offering amount is never treated as amount sold.",
            "Amount sold does not establish cash, valuation, or runway.",
            "Pooled funds and obvious SPVs are excluded.",
        ),
        features={
            "sec_form_d_capital": {
                "matched_filing_count": len(matched),
                "amounts_sold_reported": sold_values,
            }
        },
        applicable_reason="Exact legal issuer identity was corroborated.",
    )


_AI_CORE = (
    re.compile(
        r"\b(?:generative|agentic)\s+ai\s+(?:platform|product|infrastructure|studio|company)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:generative|agentic)\s+ai\s+(?:image|video|agent|model|creative)\s+"
        r"(?:platform|product|infrastructure|studio|company)\b",
        re.I,
    ),
    re.compile(r"\bai[- ]native\b", re.I),
    re.compile(
        r"\b(?:build|create|generate|deploy|train)\s+(?:generative\s+)?ai\s+"
        r"(?:agents?|models?|images?|videos?)\b",
        re.I,
    ),
)
_AI_FEATURE = (
    re.compile(r"\bai-powered\b", re.I),
    re.compile(r"\b(?:using|with|powered by)\s+(?:generative\s+)?ai\b", re.I),
    re.compile(r"\b(?:ai assistant|ai feature|machine learning)\b", re.I),
)
_AI_MARKETING = (
    re.compile(r"\bembrac(?:e|ing)\s+ai\b", re.I),
    re.compile(r"\bai-driven future\b", re.I),
)
_PROVIDERS = (
    ("github", re.compile(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/#?]+)", re.I)),
    ("huggingface", re.compile(r"^https?://(?:www\.)?huggingface\.co/([^/]+)/([^/#?]+)", re.I)),
    ("npm", re.compile(r"^https?://(?:www\.)?npmjs\.com/package/([^#?]+)", re.I)),
    ("pypi", re.compile(r"^https?://(?:www\.)?pypi\.org/project/([^/#?]+)", re.I)),
)


def _provider_identity(url: str) -> tuple[str, str]:
    canonical = _safe_url(url)
    for provider, pattern in _PROVIDERS:
        if match := pattern.match(canonical):
            return provider, "/".join(match.groups()).removesuffix(".git")
    return "", ""


def _ai_role(soup: BeautifulSoup) -> tuple[str, list[str]]:
    focused = " | ".join(
        _text(node.get_text(" ", strip=True), 500)
        for node in soup.find_all(["h1", "h2", "title"], limit=20)
    )
    for pattern in _AI_CORE:
        if match := pattern.search(focused):
            return "core_product", [match.group()]
    full = _text(soup.get_text(" ", strip=True), 20_000)
    core_matches = [match.group() for pattern in _AI_CORE for match in pattern.finditer(full)]
    if len({item.casefold() for item in core_matches}) >= 2:
        return "core_product", list(dict.fromkeys(core_matches))[:5]
    feature_matches = [
        match.group() for pattern in _AI_FEATURE for match in pattern.finditer(focused)
    ]
    if feature_matches:
        return "meaningful_feature", list(dict.fromkeys(feature_matches))[:5]
    marketing = [match.group() for pattern in _AI_MARKETING for match in pattern.finditer(full)]
    if marketing:
        return "marketing_only", list(dict.fromkeys(marketing))[:5]
    return "unknown", []


def _first_party_provider_link(
    provider: str,
    identifier: str,
    company: str,
) -> bool:
    company_token = re.sub(r"[^a-z0-9]", "", _host(company).split(".", 1)[0])
    first_segment = identifier.split("/", 1)[0].removeprefix("@")
    owner_token = re.sub(r"[^a-z0-9]", "", first_segment.casefold())
    if not company_token or not owner_token:
        return False
    if provider in {"github", "huggingface"}:
        return owner_token == company_token
    return owner_token == company_token or owner_token.startswith(company_token)


def parse_ai_artifact_provenance(
    html: str | bytes,
    url: str,
    *,
    company_key: str = "",
    artifacts: Sequence[Mapping[str, Any]] = (),
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    scanner = "ai_artifact_provenance_v1"
    now = _now(observed_at)
    source = _safe_url(url)
    company = _host(company_key) or _host(source)
    if not source or not _same_company(source, company):
        return _empty(
            scanner,
            company,
            "AI copy must originate on the company domain.",
            observed_at=now,
            status="identity_ambiguous",
        )
    raw = html if isinstance(html, bytes) else html.encode()
    bounded = raw[:_MAX_DOCUMENT_BYTES]
    truncated = len(raw) > len(bounded)
    soup = parse_html(bounded.decode(errors="replace"), "html.parser")
    links: dict[str, tuple[str, str]] = {}
    for anchor in soup.find_all("a", href=True):
        candidate = _safe_url(str(anchor.get("href") or ""), source).rstrip("/")
        provider, identifier = _provider_identity(candidate)
        if provider and _first_party_provider_link(provider, identifier, company):
            links[candidate] = (provider, identifier)
    role, spans = _ai_role(soup)
    facts: list[_Fact] = []
    if role != "unknown":
        facts.append(
            _Fact(
                "ai.product_role",
                role,
                "string",
                f"company:{company}",
                f"Deterministic first-party copy classification: {role.replace('_', ' ')}.",
                source,
                {"cited_spans": spans, "classifier_used": False},
                epistemic_status="derived",
                confidence="high" if role == "core_product" else "medium",
                confidence_method="deterministic_first_party_heading_rules",
                score_eligible=role in {"core_product", "meaningful_feature"},
                discovery_only=role == "marketing_only",
                source_actor_key=f"domain:{company}",
                causal_root_key=f"ai-product-copy:{_hash(company, role, spans)}",
            )
        )
    accepted: dict[str, dict[str, Any]] = {
        linked_url: {
            "provider": provider,
            "artifact_id": identifier,
            "url": linked_url,
            "identity_basis": "exact_first_party_link",
            "metadata_supplied": False,
        }
        for linked_url, (provider, identifier) in links.items()
    }
    for record in artifacts[:100]:
        record_url = _safe_url(str(record.get("url") or record.get("html_url") or "")).rstrip("/")
        provider, identifier = _provider_identity(record_url)
        if not provider:
            provider = _text(record.get("provider"), 50).casefold()
            identifier = _text(record.get("artifact_id") or record.get("name"), 300)
        owner_domain = _host(str(record.get("owner_domain") or record.get("homepage") or ""))
        exact_link = record_url in links
        exact_domain = bool(owner_domain and _same_company(owner_domain, company))
        if provider not in {item[0] for item in _PROVIDERS} or not identifier:
            continue
        if not (exact_link or exact_domain):
            continue
        accepted[record_url or f"https://{company}/"] = {
            "provider": provider,
            "artifact_id": identifier,
            "url": record_url,
            "identity_basis": "exact_first_party_link" if exact_link else "exact_owner_domain",
            "metadata_supplied": True,
            "kind": _text(record.get("kind"), 100),
            "updated_at": _date(record.get("updated_at") or record.get("lastModified")),
            "metrics": _json(record.get("metrics", {}))
            if isinstance(record.get("metrics"), Mapping)
            else {},
        }
    documents = [
        FrontierDocument(
            source, bounded, "first_party_ai_product_html", mime_type="text/html", publisher=company
        )
    ]
    provider_artifact_urls: set[str] = set()
    for record in accepted.values():
        provider = str(record["provider"])
        identifier = str(record["artifact_id"])
        record_url = str(record.get("url") or "")
        metadata_supplied = bool(record.get("metadata_supplied"))
        observation_source = record_url if metadata_supplied and record_url else source
        if metadata_supplied and record_url:
            documents.append(
                FrontierDocument(
                    record_url,
                    json.dumps(record, sort_keys=True, default=str),
                    f"{provider}_public_metadata",
                    mime_type="application/json",
                    publisher=provider,
                    first_party_source_url=(
                        source if record["identity_basis"] == "exact_first_party_link" else ""
                    ),
                    officially_linked=(record["identity_basis"] == "exact_first_party_link"),
                )
            )
            provider_artifact_urls.add(record_url)
        updated = record.get("updated_at")
        facts.append(
            _Fact(
                "ai.artifact",
                {"provider": provider, "artifact_id": identifier, "url": record_url},
                "json",
                f"ai-artifact:{provider}:{identifier}",
                (
                    f"The official company domain links to public {provider} artifact {identifier}."
                    if record["identity_basis"] == "exact_first_party_link"
                    else (
                        f"Provider metadata reports company-domain ownership for {provider} "
                        f"artifact {identifier}."
                    )
                ),
                observation_source,
                {
                    "identity_basis": record["identity_basis"],
                    "reported_metrics": record.get("metrics", {}),
                    "metrics_do_not_establish_traction": True,
                },
                effective_at=updated if isinstance(updated, datetime) else None,
                effective_time_basis="source_explicit"
                if isinstance(updated, datetime)
                else "unknown",
                epistemic_status=(
                    "observed"
                    if record["identity_basis"] == "exact_first_party_link"
                    else "derived"
                ),
                confidence=(
                    "high" if record["identity_basis"] == "exact_first_party_link" else "medium"
                ),
                confidence_method=(
                    "exact_first_party_link"
                    if record["identity_basis"] == "exact_first_party_link"
                    else "provider_metadata_exact_owner_domain"
                ),
                score_eligible=record["identity_basis"] == "exact_first_party_link",
                discovery_only=record["identity_basis"] != "exact_first_party_link",
                source_actor_key=f"domain:{company}",
                causal_root_key=f"ai-artifact:{provider}:{identifier}",
            )
        )
    evidence = [
        _artifact(scanner, company, document, now, truncated=truncated and document.url == source)
        for document in documents
    ]
    return _result(
        scanner,
        company,
        facts=facts,
        artifacts=evidence,
        observed_at=now,
        surface="first_party_ai_copy_and_exact_domain_artifacts",
        scope=source,
        attempted=1 + len(artifacts),
        successful=1 + sum(bool(item.get("metadata_supplied")) for item in accepted.values()),
        total=1 + len(artifacts),
        completeness="partial" if truncated else "complete",
        truncated=truncated,
        absence_interpretable=not truncated,
        limitations=(
            "Display-name similarity never establishes artifact ownership.",
            "Stars, downloads, and commits do not establish company size or traction.",
        ),
        features={
            "ai_artifact_provenance": {
                "product_role": role,
                "artifact_count": len(accepted),
                "providers": sorted({str(item["provider"]) for item in accepted.values()}),
                "provider_artifact_urls": sorted(provider_artifact_urls),
            }
        },
        applicable_reason="First-party product copy or exact-domain artifact links were available.",
    )


_OWNED_MEDIA_CONTEXT_RE = re.compile(
    r"\b(?:podcast|video|youtube|vimeo|media feed|audio|m[ -]?rss)\b|video-sitemap",
    re.I,
)


def discover_owned_media_urls(html: str | bytes, url: str) -> list[str]:
    return discover_frontier_request_surfaces(html, url)["owned_media_pulse_v1"][:10]


def _tag(value: str) -> str:
    return value.rsplit("}", 1)[-1].casefold()


def _element_text(node: ET.Element, names: set[str]) -> str:
    for child in node.iter():
        if _tag(child.tag) in names and child.text:
            return _text(child.text, 1_000)
    return ""


def _xml_parse_bytes(body: bytes) -> bytes:
    """Ignore transport BOM/leading whitespace, retaining original artifact bytes."""
    value = body.lstrip(b" \t\r\n")
    if value.startswith(b"\xef\xbb\xbf"):
        value = value[3:].lstrip(b" \t\r\n")
    return value


def parse_owned_media_feed(
    body: str | bytes,
    url: str,
    *,
    company_key: str = "",
    first_party_source_url: str = "",
    officially_linked: bool = False,
    observed_at: datetime | None = None,
    mime_type: str = "",
) -> FrontierIntelligence:
    scanner = "owned_media_pulse_v1"
    now = _now(observed_at)
    source = _safe_url(url)
    company = _host(company_key) or _host(first_party_source_url) or _host(source)
    same_domain = bool(source and _same_company(source, company))
    cited = bool(
        officially_linked
        and first_party_source_url
        and _same_company(first_party_source_url, company)
    )
    if not source or not (same_domain or cited):
        return _empty(
            scanner,
            company,
            "External feed lacks exact first-party linkage.",
            observed_at=now,
            status="identity_ambiguous",
        )
    raw = body if isinstance(body, bytes) else body.encode()
    bounded = raw[:_MAX_DOCUMENT_BYTES]
    document_truncated = len(raw) > len(bounded)
    html_surface = "html" in mime_type.casefold() or bool(
        re.match(rb"\s*(?:<!doctype\s+html\b|<(?:html|head|body|div|section)\b)",
                 _xml_parse_bytes(bounded), re.I)
    )
    if html_surface:
        root = ET.Element("media")
        soup = parse_html(bounded.decode(errors="replace"), "html.parser")
        seen_items: set[str] = set()
        for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)},
                                    limit=100):
            try:
                payload = json.loads(script.string or script.get_text(" ", strip=True))
            except (ValueError, TypeError, RecursionError):
                continue
            stack = [payload]
            visited = 0
            while stack and visited < 500:
                current = stack.pop()
                visited += 1
                if isinstance(current, list):
                    stack.extend(current[:100])
                    continue
                if not isinstance(current, Mapping):
                    continue
                stack.extend(value for value in list(current.values())[:100]
                             if isinstance(value, (dict, list)))
                types = current.get("@type", [])
                types = types if isinstance(types, list) else [types]
                if not any(str(t).casefold() in {"podcastepisode", "videoobject", "audioobject"}
                           for t in types):
                    continue
                title = _text(current.get("name") or current.get("headline"), 1_000)
                item_url = _safe_url(_text(current.get("url") or current.get("@id"), 1_000),
                                     source)
                published = _text(current.get("datePublished") or current.get("uploadDate"), 100)
                identity = _hash(title, item_url, published)
                if not (title or item_url) or identity in seen_items:
                    continue
                seen_items.add(identity)
                item = ET.SubElement(root, "item")
                for key, value in (("title", title), ("link", item_url),
                                   ("guid", identity), ("published", published)):
                    ET.SubElement(item, key).text = value
    elif re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", bounded, re.I):
        return _empty(
            scanner,
            company,
            "Owned-media XML declarations are not accepted.",
            observed_at=now,
            status="failed",
        )
    else:
        try:
            root = ET.fromstring(_xml_parse_bytes(bounded))
        except ET.ParseError as exc:
            return _empty(
                scanner, company, "Owned-media feed is invalid XML.",
                observed_at=now, status="failed", errors=[str(exc)],
            )
    entries = [node for node in root.iter() if _tag(node.tag) in {"entry", "item", "url"}]
    items: list[dict[str, Any]] = []
    for entry in entries[:20]:
        published = _date(
            _element_text(entry, {"published", "updated", "pubdate", "date", "publication_date"})
        )
        title = _element_text(entry, {"title"})
        item_id = _element_text(entry, {"id", "guid", "video_id"})
        item_url = _safe_url(_element_text(entry, {"loc", "link"}), source)
        if not item_url:
            for child in entry.iter():
                if _tag(child.tag) == "link" and child.attrib.get("href"):
                    item_url = _safe_url(str(child.attrib["href"]), source)
                    break
        items.append(
            {
                "item_id": item_id or item_url or _hash(title, published),
                "title": title,
                "url": item_url,
                "published_at": published,
                "media_type": "video" if "youtube.com" in source else "podcast_or_feed",
            }
        )
    dated = [item for item in items if isinstance(item["published_at"], datetime)]
    recent_30 = sum(
        timedelta(0) <= now - item["published_at"] <= timedelta(days=30) for item in dated
    )
    recent_90 = sum(
        timedelta(0) <= now - item["published_at"] <= timedelta(days=90) for item in dated
    )
    facts: list[_Fact] = []
    if items:
        facts.append(
            _Fact(
                "media.publishing_pulse",
                {
                    "sampled_items": len(items),
                    "dated_items": len(dated),
                    "recent_30d": recent_30,
                    "recent_90d": recent_90,
                },
                "json",
                f"owned-feed:{_hash(source)[:24]}",
                (
                    f"The officially linked media source exposes {recent_30} dated item(s) in "
                    f"30 days and {recent_90} in 90 days."
                ),
                source,
                {"item_cap": 20, "official_link_basis": first_party_source_url},
                score_eligible=recent_90 > 0,
                discovery_only=not dated,
                source_actor_key=f"domain:{company}",
                causal_root_key=f"owned-feed:{_hash(source)}",
            )
        )
    for item in items:
        published = item["published_at"]
        facts.append(
            _Fact(
                "media.publication",
                {key: value for key, value in item.items() if key != "published_at"},
                "json",
                f"media-item:{_hash(source, item['item_id'])[:24]}",
                f"Officially linked {'media page' if html_surface else 'feed'} item: "
                f"{item['title'] or item['item_id']}.",
                source,
                effective_at=published,
                effective_time_basis=("source_explicit" if html_surface else "feed_date")
                if published else "unknown",
                score_eligible=published is not None,
                discovery_only=published is None,
                source_actor_key=f"domain:{company}",
                causal_root_key=f"media-item:{_hash(source, item['item_id'])}",
            )
        )
    document = FrontierDocument(
        source,
        bounded,
        "official_owned_media_surface" if html_surface else "official_owned_media_feed",
        mime_type="text/html" if html_surface else "application/xml",
        publisher=_host(source),
        first_party_source_url=first_party_source_url,
        officially_linked=same_domain or cited,
    )
    truncated = document_truncated or len(entries) > 20
    artifact = _artifact(
        scanner,
        company,
        document,
        now,
        excerpt=f"{len(items)} bounded {'media page' if html_surface else 'feed'} items",
        truncated=truncated,
    )
    return _result(
        scanner,
        company,
        facts=facts,
        artifacts=[artifact],
        observed_at=now,
        surface="officially_linked_owned_media_surface" if html_surface
        else "officially_linked_owned_media_feed",
        scope=source,
        attempted=1,
        successful=1,
        total=1,
        completeness="partial" if truncated or html_surface else "complete",
        truncated=truncated,
        absence_interpretable=not truncated and not html_surface,
        limitations=(
            "At most 20 items are parsed; publication does not establish audience or spend.",
        ),
        features={
            "owned_media_pulse": {
                "sampled_items": len(items),
                "dated_items": len(dated),
                "recent_30d": recent_30,
                "recent_90d": recent_90,
            }
        },
        applicable_reason="An officially linked public media surface was supplied.",
        # HTML without typed media cannot establish absence or non-applicability.
        # Retain the artifact with partial coverage so research remains due.

    )


_BLOG_SURFACE_RE = re.compile(
    r"(?:^|/)(?:blog|insights?|resources?|articles?|learn|stories?|ideas|"
    r"thought-leadership|news(?:room)?)(?:/|$)",
    re.I,
)
_BLOG_FEED_RE = re.compile(r"(?:^|/)(?:feed|rss|atom)(?:[./_-]|$)", re.I)
_COMMENT_FEED_LABEL_RE = re.compile(
    r"\bcomments?[\s_-]+(?:rss|atom|feeds?)\b|"
    r"\b(?:rss|atom|feeds?)(?:\s+for)?[\s_-]+comments?\b",
    re.I,
)


def _is_comment_feed_url(url: str) -> bool:
    parsed = urlparse(url)
    return bool(
        re.search(r"(?:^|/)comments?/feed(?:[./_-]|$)", parsed.path, re.I)
        or any(
            key.casefold() == "feed"
            and (value.casefold() == "comments" or value.casefold().startswith("comments-"))
            for key, value in parse_qsl(parsed.query)
        )
    )


_BLOG_EXCLUDED_SURFACE_RE = re.compile(
    r"(?:^|/)(?:docs?|documentation|help|support|status|careers?|jobs?|community|"
    r"changelog|release-notes?|tags?|categories?|authors?|page)(?:/|$)",
    re.I,
)
_BLOG_PRESS_RE = re.compile(
    r"\b(?:press release|news release|investor relations?|quarterly results?|earnings|"
    r"financial results?|sec filing|form 10-[kq]|8-k|appoints?|board of directors?)\b",
    re.I,
)
_BLOG_UPDATE_RE = re.compile(
    r"\b(?:changelog|release notes?|what'?s new|version \d|maintenance|incident|"
    r"service status|product update)\b",
    re.I,
)
_BLOG_DOCS_RE = re.compile(
    r"\b(?:api reference|documentation|support article|help center|troubleshooting|"
    r"developer docs?|knowledge base)\b",
    re.I,
)
_BLOG_EDITORIAL_RE = re.compile(
    r"\b(?:how to|guide|best practices?|tips?|strategy|strategies|trends?|insights?|"
    r"research|report|benchmark|playbook|template|comparison|customer stor(?:y|ies)|"
    r"case stud(?:y|ies)|thought leadership|webinar|ebook|whitepaper|use cases?)\b",
    re.I,
)
_BLOG_CTA_RE = re.compile(
    r"\b(?:get started|book (?:a )?demo|request (?:a )?demo|contact sales|try (?:it )?free|"
    r"start (?:a )?free|download|register|sign up|subscribe|talk to (?:an )?expert)\b",
    re.I,
)
_BLOG_TOPIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "customer_proof",
        re.compile(
            r"\b(?:customer stor(?:y|ies)|case stud(?:y|ies)|success stor(?:y|ies))\b", re.I
        ),
    ),
    (
        "research_and_thought_leadership",
        re.compile(
            r"\b(?:research|report|benchmark|survey|trends?|insights?|thought leadership)\b", re.I
        ),
    ),
    (
        "education_and_seo",
        re.compile(
            r"\b(?:how to|guide|best practices?|tips?|template|playbook|comparison|versus|vs\.)\b",
            re.I,
        ),
    ),
    (
        "product_and_use_cases",
        re.compile(r"\b(?:product|feature|use cases?|workflow|integration|launch)\b", re.I),
    ),
    (
        "events_and_multiformat",
        re.compile(r"\b(?:webinar|event|podcast|video|ebook|whitepaper)\b", re.I),
    ),
    (
        "developer_and_ai",
        re.compile(
            r"\b(?:developer|api|artificial intelligence|generative ai|machine learning|"
            r"agentic|ai)\b",
            re.I,
        ),
    ),
    (
        "trust_and_regulation",
        re.compile(r"\b(?:security|privacy|compliance|trust|regulation|governance)\b", re.I),
    ),
    ("culture_and_hiring", re.compile(r"\b(?:culture|careers?|hiring|team|workplace)\b", re.I)),
)


_FRONTIER_NETWORK_SURFACE_OWNERS = (
    "lifecycle_email_exhaust_v1",
    "creative_hiring_demand_v1",
    "creator_program_contract_v1",
    "product_release_engine_v1",
    "event_webinar_engine_v1",
    "owned_media_pulse_v1",
    "trust_center_readiness_v1",
    "brand_asset_governance_v1",
    "public_design_system_v1",
    "corporate_blog_engine_v1",
)
if len(_FRONTIER_NETWORK_SURFACE_OWNERS) != len(set(_FRONTIER_NETWORK_SURFACE_OWNERS)) or not set(
    _FRONTIER_NETWORK_SURFACE_OWNERS
).issubset(SCANNER_REGISTRY):
    raise RuntimeError("frontier network-surface owners must be canonical and unique")

_PRODUCT_RELEASE_SURFACE_RE = re.compile(
    r"(?:^|/)(?:changelog|change-log|release-notes?|releases?|what-?s-new|whatsnew|"
    r"product-updates?|product-news)(?:[./_-]|/|$)|\b(?:changelog|release notes?|"
    r"what'?s new|product updates?|release feed)\b",
    re.I,
)
_PRODUCT_RELEASE_EXCLUDED_RE = re.compile(
    r"\b(?:press[- ]releases?|news[- ]releases?|investor relations?|"
    r"security advisories?|incident|maintenance|status page)\b",
    re.I,
)


def _release_surface_priority(url: str) -> int:
    """Prefer explicit public feeds/changelogs among already proven release links."""

    parsed = urlparse(url)
    path = parsed.path.casefold()
    feed_query = any(
        key.casefold() in {"format", "output", "feed"}
        and value.casefold() in {"rss", "atom", "json", "jsonfeed"}
        for key, value in parse_qsl(parsed.query)
    )
    if (
        path.endswith((".xml", ".rss", ".atom", ".json"))
        or re.search(r"(?:^|/)(?:rss|atom|feed)(?:/|$)", path)
        or feed_query
    ):
        return 0
    if re.search(r"(?:changelog|change-log|release-notes?)", path):
        return 1
    return 2


_EVENT_SURFACE_RE = re.compile(
    r"(?:^|/)(?:events?|webinars?|conferences?|calendar|on-demand)(?:[./_-]|/|$)|"
    r"\b(?:events?|webinars?|conferences?|event calendar|on[ -]?demand)\b",
    re.I,
)


def _event_surface_priority(url: str) -> int:
    """Order already admitted event targets without guessing or authorizing URLs."""

    path = urlparse(url).path.casefold()
    if path.endswith(".ics"):
        return 0
    if path.endswith((".json", ".xml", ".rss", ".atom")):
        return 1
    if re.search(r"(?:^|[/_-])20\d{2}[/_-](?:0?[1-9]|1[0-2])(?:[/_.-]|$)", path):
        return 2
    return 3


_TRUST_SURFACE_RE = re.compile(
    r"(?:^|/)(?:trust(?:-center)?|security(?:-center)?|compliance(?:-center)?|"
    r"assurance(?:-center)?|privacy-security)(?:/|$|\.(?:html?|php)$)|"
    r"\b(?:trust center|security center|compliance center|assurance center|"
    r"privacy and security|security\.txt)\b",
    re.I,
)
_BRAND_SURFACE_RE = re.compile(
    r"(?:^|/)(?:brand(?:-guidelines?|-assets?|-portal)?|media-kit|press-kit|asset-center|"
    r"logo-library)(?:/|$|\.(?:html?|php)$)|\b(?:brand guidelines?|brand assets?|brand portal|"
    r"media kit|press kit|asset center|logo library)\b",
    re.I,
)
_DESIGN_SYSTEM_SURFACE_RE = re.compile(
    r"(?:^|/)(?:storybook|design-system|style-guide|pattern-library|component-library)"
    r"(?:[./_-]|/|$)|\b(?:storybook|design system|style guide|pattern library|"
    r"component library)\b",
    re.I,
)
_CAREER_SURFACE_RE = re.compile(
    r"(?:^|/)(?:careers?|jobs?|hiring|join-us|join-our-team|open-roles?|openings?|"
    r"vacancies|employment|opportunities|work-with-us)(?:[./_-]|/|$)|"
    r"\b(?:careers?|job openings?|open roles?|join (?:us|our team)|we(?:'|’)re hiring|"
    r"work with us|employment opportunities)\b",
    re.I,
)
_CAREER_EXCLUDED_RE = re.compile(
    r"(?:^|/)(?:apply|application|candidate-login|talent-community|job-alerts?|"
    r"saved-jobs?)(?:/|$)|\b(?:apply now|talent community|candidate login|job alerts?)\b",
    re.I,
)
_ATS_CAREER_HOSTS = (
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
)
_NON_SUBJECT_LINK_CONTEXT_RE = re.compile(
    r"\b(?:testimonial|case[-_ ]?stud(?:y|ies)|customer[-_ ]?(?:logo|story)|"
    r"client[-_ ]?(?:logo|story)|portfolio[-_ ]?item)\b",
    re.I,
)
_SURFACE_REJECT_RE = re.compile(
    r"(?:^|/)(?:login|log-in|sign-in|signin|sign-up|signup|account|auth|oauth|checkout|"
    r"register|registration)(?:/|$)|\b(?:log in|sign in|create an account|gated)\b",
    re.I,
)
_SURFACE_BINARY_RE = re.compile(
    r"\.(?:7z|ai|avi|avif|css|docx?|eot|eps|gif|heic|ico|jpe?g|mov|mp4|pdf|png|pptx?|psd|rar|"
    r"svg|tar|tiff?|ttf|webm|webp|woff2?|zip)$",
    re.I,
)
_SURFACE_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def is_frontier_asset_url(url: str) -> bool:
    """Apply the document boundary again to persisted URLs and redirect targets."""
    path = urlparse(url).path.casefold()
    return bool(_SURFACE_BINARY_RE.search(path) or re.search(r"\.(?:m?js|map|wasm)$", path))


def is_frontier_utility_url(url: str) -> bool:
    """Embedding APIs and appointment booking are not public research surfaces."""
    parsed = urlparse(url)
    host = _host(url)
    path = parsed.path.casefold()
    return bool(
        re.search(r"(?:^|/)(?:oembed|appointments?)(?:/|$)", path)
        or any("/oembed/" in value.casefold() for key, value in parse_qsl(parsed.query)
               if key.casefold() == "rest_route")
        or host in {"calendly.com", "cal.com", "youcanbook.me"}
        or host.endswith((".calendly.com", ".youcanbook.me"))
    )


def _editorial_article_url(url: str) -> bool:
    parts = [part for part in urlparse(url).path.casefold().split("/") if part]
    return bool(
        any(key.casefold() == "pid" and value.casefold() == "newsarchive"
            for key, value in parse_qsl(urlparse(url).query))
        or (
            len(parts) > 1
            and (
                parts[0] in {
                    "blog", "news", "articles", "insights", "stories", "features", "opinion",
                }
                or (
                    parts[-1].isdigit() and len(parts[-1]) >= 5
                    and any(part.count("-") >= 2 for part in parts[:-1])
                    and parts[0] not in {
                        "job", "jobs", "career", "careers", "positions", "roles",
                        "vacancies", "employment",
                    }
                )
            )
        )
    )


def is_frontier_request_target(url: str, scanner_id: str, company_key: str) -> bool:
    if is_frontier_utility_url(url):
        return False
    if scanner_id == "event_webinar_engine_v1" and (
        _host(url) in {"vimeo.com", "www.vimeo.com"}
        and urlparse(url).path.casefold().startswith("/ondemand/")
    ):
        return False
    if _editorial_article_url(url):
        if scanner_id in {
            "creator_program_contract_v1", "brand_asset_governance_v1",
            "trust_center_readiness_v1", "creative_hiring_demand_v1",
        }:
            return False
        if scanner_id in {"corporate_blog_engine_v1", "owned_media_pulse_v1"} and not (
            _same_company(url, company_key)
        ):
            return False
    return True


def _canonical_surface_url(raw: str, base: str) -> str:
    candidate = _safe_url(raw, base)
    if not candidate:
        return ""
    parsed = urlparse(candidate)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in _SURFACE_TRACKING_QUERY_KEYS
    ]
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path or "/",
            "",
            urlencode(query, doseq=True),
            "",
        )
    )


_BROAD_SOCIAL_HOSTS = (
    "youtube.com", "youtu.be", "facebook.com", "fb.com", "fb.watch",
    "instagram.com", "tiktok.com", "twitter.com", "x.com", "t.co",
    "linkedin.com", "lnkd.in", "pinterest.com", "pin.it", "threads.net",
    "threads.com", "snapchat.com", "reddit.com",
)


def is_broad_social_research_url(url: str) -> bool:
    """Exclude organic platform fetches, independently of paid-ad API collection."""

    host = _host(url)
    return any(
        host == platform or host.endswith(f".{platform}")
        for platform in _BROAD_SOCIAL_HOSTS
    )


def classify_frontier_surface_owner(url: str) -> str:
    """Return an unambiguous path-owned scanner, leaving neutral redirects unclassified."""

    candidate = _canonical_surface_url(url, url)
    if (not candidate or is_broad_social_research_url(candidate)
            or is_frontier_utility_url(candidate)):
        return ""
    parsed = urlparse(candidate)
    path = parsed.path.casefold()
    combined = f"{path} {parsed.query}".casefold()
    host = _host(candidate)
    if _SURFACE_REJECT_RE.search(path) or _SURFACE_BINARY_RE.search(path):
        return ""
    if _editorial_article_url(candidate):
        return "corporate_blog_engine_v1"
    if _ARCHIVE_RE.search(combined) or any(
        host == provider or host.endswith(f".{provider}") for provider in _CAMPAIGN_HOSTS
    ):
        return "lifecycle_email_exhaust_v1"
    if (
        (_CAREER_SURFACE_RE.search(combined) or host in _ATS_CAREER_HOSTS)
        and not _CAREER_EXCLUDED_RE.search(combined)
        # Hosted ATS roots must include a tenant/resource path. A provider homepage is not a
        # company-bound board and can never authorize work by itself.
        and (host not in _ATS_CAREER_HOSTS or bool(path.strip("/")))
    ):
        return "creative_hiring_demand_v1"
    if any(marker in combined for marker in _PROGRAM_MARKERS) or re.search(
        r"(?:^|/)(?:affiliates?|ambassadors?|creators?|influencers?|referrals?|ugc)"
        r"(?:/|$)",
        path,
        re.I,
    ):
        return "creator_program_contract_v1"
    if _PRODUCT_RELEASE_SURFACE_RE.search(combined) and not _PRODUCT_RELEASE_EXCLUDED_RE.search(
        combined
    ):
        return "product_release_engine_v1"
    if _EVENT_SURFACE_RE.search(combined) or path.endswith(".ics"):
        return "event_webinar_engine_v1"
    if re.search(r"podcast|video-sitemap", path, re.I):
        return "owned_media_pulse_v1"
    if _TRUST_SURFACE_RE.search(combined):
        return "trust_center_readiness_v1"
    if _BRAND_SURFACE_RE.search(combined):
        return "brand_asset_governance_v1"
    if _DESIGN_SYSTEM_SURFACE_RE.search(combined):
        return "public_design_system_v1"
    if _BLOG_SURFACE_RE.search(path) and not _BLOG_EXCLUDED_SURFACE_RE.search(path):
        return "corporate_blog_engine_v1"
    return ""


def discover_frontier_request_surfaces(html: str | bytes, url: str) -> dict[str, list[str]]:
    """Assign every explicitly linked optional network surface to exactly one scanner.

    The page URL is the provenance boundary: no guessed endpoint is produced. Broad social
    platforms, direct binaries, forms, login, account, and registration targets are rejected.
    Ownership follows ``_FRONTIER_NETWORK_SURFACE_OWNERS``.
    """

    owned: dict[str, list[str]] = {
        scanner_id: [] for scanner_id in _FRONTIER_NETWORK_SURFACE_OWNERS
    }
    source = _safe_url(url)
    if not source:
        return owned
    raw = html if isinstance(html, bytes) else html.encode()
    soup = parse_html(raw[:_MAX_DOCUMENT_BYTES].decode(errors="replace"), "html.parser")
    source_is_editorial = bool(_BLOG_SURFACE_RE.search(urlparse(source).path))
    source_release_context = f"{urlparse(source).path} {_host(source).split('.')[0]}"
    source_is_release = bool(
        _PRODUCT_RELEASE_SURFACE_RE.search(source_release_context)
        and not _PRODUCT_RELEASE_EXCLUDED_RE.search(source_release_context)
    )
    claims: dict[str, tuple[int, int]] = {}
    occurrence = 0

    def claim(node: Tag) -> None:
        nonlocal occurrence
        occurrence += 1
        raw_href = str(node.get("href") or "")
        candidate = _canonical_surface_url(raw_href, source)
        if (
            not candidate or candidate == source or node.find_parent("form")
            or is_broad_social_research_url(candidate)
            or is_frontier_utility_url(candidate)
        ):
            return
        parsed = urlparse(candidate)
        path = parsed.path.casefold()
        rel_value = node.get("rel", [])
        rel = {
            str(item).casefold()
            for item in (rel_value if isinstance(rel_value, list) else [rel_value])
        }
        # Asset declarations may have editorial words in their paths; they are
        # not optional research documents, including extensionless asset URLs.
        if node.name == "link" and rel & {"stylesheet", "icon", "apple-touch-icon", "mask-icon"}:
            return
        mime = _text(node.get("type"), 100).casefold()
        label = _text(
            f"{node.get_text(' ', strip=True)} {node.get('aria-label') or ''} "
            f"{node.get('title') or ''}",
            500,
        )
        combined = f"{path} {parsed.query} {label}".casefold()
        if (
            _is_comment_feed_url(candidate)
            or _COMMENT_FEED_LABEL_RE.search(label)
            or (
                any(item in mime for item in ("rss", "atom", "xml", "json"))
                and re.match(r"\s*comments?\s+(?:for|on)\b", label, re.I)
            )
            or rel & {"replies", "comments", "comment-feed"}
            or _SURFACE_REJECT_RE.search(path)
            or _SURFACE_REJECT_RE.search(label)
            or _CAREER_EXCLUDED_RE.search(combined)
            or _SURFACE_BINARY_RE.search(path)
            or node.has_attr("download")
        ):
            return

        owner = ""
        host = _host(candidate)
        if _editorial_article_url(candidate):
            # A linked article is editorial context, not the company's program,
            # trust center or asset portal merely because its title uses those words.
            if _same_company(candidate, source):
                owner = "corporate_blog_engine_v1"
            else:
                return
        elif _ARCHIVE_RE.search(combined) or any(
            host == provider or host.endswith(f".{provider}") for provider in _CAMPAIGN_HOSTS
        ):
            owner = "lifecycle_email_exhaust_v1"
        elif (
            (_CAREER_SURFACE_RE.search(combined) or host in _ATS_CAREER_HOSTS)
            and not _CAREER_EXCLUDED_RE.search(combined)
            and (host not in _ATS_CAREER_HOSTS or path.strip("/"))
        ):
            if host in _ATS_CAREER_HOSTS:
                ancestor_context = " ".join(
                    _text(
                        f"{ancestor.get('id') or ''} "
                        f"{' '.join(str(item) for item in ancestor.get('class', []))}",
                        300,
                    )
                    for ancestor in node.parents
                    if isinstance(ancestor, Tag)
                )
                if _NON_SUBJECT_LINK_CONTEXT_RE.search(ancestor_context):
                    return
            owner = "creative_hiring_demand_v1"
        elif any(marker in combined for marker in _PROGRAM_MARKERS) or re.search(
            r"(?:^|/)(?:affiliates?|ambassadors?|creators?|influencers?|referrals?|ugc)"
            r"(?:/|$)",
            path,
            re.I,
        ):
            owner = "creator_program_contract_v1"
        elif (
            _PRODUCT_RELEASE_SURFACE_RE.search(combined)
            or (
                source_is_release
                and (
                    (
                        "alternate" in rel
                        and any(item in mime for item in ("rss", "atom", "json", "xml"))
                    )
                    or (
                        _release_surface_priority(candidate) == 0
                        and re.search(r"\b(?:rss|atom|release feed|changelog feed)\b", label)
                    )
                )
                and not _BLOG_SURFACE_RE.search(path)
            )
        ) and not _PRODUCT_RELEASE_EXCLUDED_RE.search(combined):
            owner = "product_release_engine_v1"
        elif _EVENT_SURFACE_RE.search(combined) or path.endswith(".ics") or "text/calendar" in mime:
            owner = "event_webinar_engine_v1"
        else:
            media_feed = bool(
                (
                    "alternate" in rel
                    and any(item in mime for item in ("rss", "atom", "xml"))
                    and _OWNED_MEDIA_CONTEXT_RE.search(f"{combined} {urlparse(source).path}")
                )
                or re.search(r"podcast|video-sitemap", path, re.I)
                or (re.search(r"rss|feed", path, re.I) and _OWNED_MEDIA_CONTEXT_RE.search(combined))
            )
            if media_feed and not _BLOG_SURFACE_RE.search(path):
                owner = "owned_media_pulse_v1"
            elif _TRUST_SURFACE_RE.search(combined):
                owner = "trust_center_readiness_v1"
            elif _BRAND_SURFACE_RE.search(combined):
                owner = "brand_asset_governance_v1"
            elif _DESIGN_SYSTEM_SURFACE_RE.search(combined):
                owner = "public_design_system_v1"
            else:
                editorial_context = bool(
                    _BLOG_SURFACE_RE.search(path)
                    or re.search(
                        r"\b(?:blog|insights?|resources?|articles?|stories?|newsroom)\b",
                        combined,
                    )
                )
                feed_link = bool(
                    "alternate" in rel
                    and any(item in mime for item in ("rss", "atom", "xml", "json"))
                    and (
                        source_is_editorial
                        or editorial_context
                        or (
                            _BLOG_FEED_RE.search(path)
                            and not _OWNED_MEDIA_CONTEXT_RE.search(
                                f"{combined} {urlparse(source).path}"
                            )
                        )
                    )
                )
                if (
                    not _BLOG_EXCLUDED_SURFACE_RE.search(path)
                    and (editorial_context or feed_link)
                    and not (_OWNED_MEDIA_CONTEXT_RE.search(combined) and not editorial_context)
                    and (
                        _same_company(candidate, source)
                        or feed_link
                        or re.fullmatch(
                            r"(?:our |corporate |company )?(?:blog|newsroom|insights)(?: on .+)?",
                            label.strip(), re.I,
                        )
                    )
                ):
                    owner = "corporate_blog_engine_v1"
        if not owner or not is_frontier_request_target(candidate, owner, source):
            return
        rank = _FRONTIER_NETWORK_SURFACE_OWNERS.index(owner)
        previous = claims.get(candidate)
        if previous is None or rank < previous[0]:
            claims[candidate] = (rank, occurrence)

    # Alternate/feed declarations are intentionally considered before navigation links.
    for node in soup.find_all("link", href=True, limit=100):
        claim(node)
    for node in soup.find_all("a", href=True, limit=1_000):
        claim(node)
    for candidate, (rank, order) in sorted(
        claims.items(), key=lambda item: (item[1][0], item[1][1], item[0])
    ):
        del order
        owned[_FRONTIER_NETWORK_SURFACE_OWNERS[rank]].append(candidate)
    # Prefer machine-readable calendars/feeds before generic event hubs, using
    # only targets that passed the same first-party ownership classification.
    owned["product_release_engine_v1"].sort(key=_release_surface_priority)
    owned["event_webinar_engine_v1"].sort(key=_event_surface_priority)
    flattened = [candidate for urls in owned.values() for candidate in urls]
    if len(flattened) != len(set(flattened)):  # pragma: no cover - construction invariant
        raise RuntimeError("a frontier request URL acquired more than one scanner owner")
    return owned


def discover_product_release_urls(html: str | bytes, url: str) -> list[str]:
    return discover_frontier_request_surfaces(html, url)["product_release_engine_v1"][:4]


def discover_creative_hiring_urls(html: str | bytes, url: str) -> list[str]:
    """Return bounded careers/ATS URLs owned by the central request classifier."""

    return discover_frontier_request_surfaces(html, url)["creative_hiring_demand_v1"][:8]


def discover_event_webinar_urls(html: str | bytes, url: str) -> list[str]:
    return discover_frontier_request_surfaces(html, url)["event_webinar_engine_v1"][:5]


def discover_trust_center_urls(html: str | bytes, url: str) -> list[str]:
    return discover_frontier_request_surfaces(html, url)["trust_center_readiness_v1"][:3]


def discover_brand_asset_urls(html: str | bytes, url: str) -> list[str]:
    return discover_frontier_request_surfaces(html, url)["brand_asset_governance_v1"][:3]


def discover_public_design_system_urls(html: str | bytes, url: str) -> list[str]:
    return discover_frontier_request_surfaces(html, url)["public_design_system_v1"][:2]


def discover_corporate_blog_urls(html: str | bytes, url: str) -> list[str]:
    """Return only blog/feed surfaces owned by the central request classifier."""

    return discover_frontier_request_surfaces(html, url)["corporate_blog_engine_v1"][:5]


def _blog_date(value: Any, now: datetime) -> datetime | None:
    parsed = _date(value)
    if parsed is None or parsed < datetime(1990, 1, 1, tzinfo=UTC):
        return None
    return parsed if parsed <= now + timedelta(days=1) else None


def _blog_node_date(node: Tag | BeautifulSoup, now: datetime) -> datetime | None:
    for child in node.find_all(["time", "meta"], limit=30):
        if child.name == "time":
            value = child.get("datetime") or child.get_text(" ", strip=True)
        else:
            key = str(child.get("property") or child.get("name") or "").casefold()
            if key not in {
                "article:published_time",
                "date",
                "datepublished",
                "publish-date",
            }:
                continue
            value = child.get("content") or ""
        if parsed := _blog_date(value, now):
            return parsed
    match = _MONTH_DATE_RE.search(node.get_text(" ", strip=True))
    return _blog_date(match.group(), now) if match else None


def _blog_mapping_url(value: Any, source: str) -> str:
    if isinstance(value, Mapping):
        value = value.get("@id") or value.get("url") or ""
    return _safe_url(str(value or ""), source)


def _blog_names(value: Any) -> list[str]:
    raw_values = value if isinstance(value, list) else [value]
    names: list[str] = []
    for item in raw_values:
        if isinstance(item, Mapping):
            item = item.get("name") or item.get("@id") or ""
        name = _text(item, 200)
        if name:
            names.append(name)
    return list(dict.fromkeys(names))[:20]


def _blog_categories(value: Any) -> list[str]:
    raw_values = value if isinstance(value, list) else [value]
    categories: list[str] = []
    for item in raw_values:
        for part in re.split(r"[,|]", str(item or "")):
            category = _text(part, 100)
            if category:
                categories.append(category)
    return list(dict.fromkeys(categories))[:30]


def _blog_content_class(record: Mapping[str, Any]) -> str:
    text = " ".join(
        (
            str(record.get("title") or ""),
            str(record.get("url") or ""),
            " ".join(str(item) for item in record.get("categories", ())),
            str(record.get("schema_type") or ""),
            str(record.get("excerpt") or ""),
        )
    )
    path = urlparse(str(record.get("url") or "")).path
    editorial_markers = bool(_BLOG_EDITORIAL_RE.search(text))
    if _BLOG_DOCS_RE.search(text) or re.search(r"/(?:docs?|help|support|kb)/", path, re.I):
        return "docs_or_support"
    if _BLOG_UPDATE_RE.search(text) or re.search(
        r"/(?:changelog|release-notes?|status)/", path, re.I
    ):
        return "product_update"
    newsroom_path = bool(re.search(r"/(?:press|newsroom|investors?|company-news)/", path, re.I))
    if (
        _BLOG_PRESS_RE.search(text)
        or str(record.get("schema_type") or "").casefold() == "newsarticle"
        or (newsroom_path and not editorial_markers)
    ):
        return "press_or_investor"
    if (
        editorial_markers
        or _BLOG_SURFACE_RE.search(path)
        or str(record.get("schema_type") or "").casefold() in {"article", "blogposting"}
    ):
        return "editorial"
    return "unknown"


def _blog_record(
    *,
    source: str,
    title: Any,
    url: Any,
    published_at: datetime | None,
    authors: Sequence[str] = (),
    categories: Sequence[str] = (),
    schema_type: str = "",
    excerpt: str = "",
    date_basis: str = "unknown",
    structured: bool = False,
    visual: bool = False,
    video: bool = False,
) -> dict[str, Any] | None:
    item_url = _canonical_surface_url(str(url or ""), source)
    item_title = _text(title, 300)
    if not item_url and not item_title:
        return None
    path = urlparse(item_url).path.casefold()
    if _BLOG_EXCLUDED_SURFACE_RE.search(path):
        return None
    record: dict[str, Any] = {
        "item_id": item_url or _hash(item_title, published_at),
        "title": item_title,
        "url": item_url,
        "published_at": published_at,
        "date_basis": date_basis if published_at else "unknown",
        "source_surface_url": source,
        "authors": list(dict.fromkeys(_text(item, 200) for item in authors if _text(item, 200)))[
            :20
        ],
        "categories": list(
            dict.fromkeys(_text(item, 100) for item in categories if _text(item, 100))
        )[:30],
        "schema_type": _text(schema_type, 100),
        "excerpt": _text(excerpt, 500),
        "structured": bool(structured),
        "visual": bool(visual),
        "video": bool(video),
    }
    context = f"{item_title} {record['excerpt']} {' '.join(record['categories'])}"
    record["cta"] = bool(_BLOG_CTA_RE.search(context))
    record["topics"] = [name for name, pattern in _BLOG_TOPIC_PATTERNS if pattern.search(context)]
    record["content_class"] = _blog_content_class(record)
    return record


def _blog_feed_records(
    body: bytes,
    source: str,
    now: datetime,
) -> tuple[list[dict[str, Any]], str]:
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", body, re.I):
        return [], "Blog XML declarations are not accepted."
    try:
        root = ET.fromstring(_xml_parse_bytes(body))
    except ET.ParseError as exc:
        return [], f"Blog feed is invalid XML: {_text(exc, 300)}"
    records: list[dict[str, Any]] = []
    for entry in [node for node in root.iter() if _tag(node.tag) in {"entry", "item", "url"}][:40]:
        title = _element_text(entry, {"title"})
        published = _blog_date(
            _element_text(entry, {"published", "pubdate", "date", "publication_date"}),
            now,
        )
        item_url = _safe_url(_element_text(entry, {"loc", "link"}), source)
        if not item_url:
            for child in entry.iter():
                if _tag(child.tag) == "link" and child.attrib.get("href"):
                    item_url = _safe_url(str(child.attrib["href"]), source)
                    break
        authors = [
            _text(child.text, 200)
            for child in entry.iter()
            if _tag(child.tag) in {"author", "creator"} and _text(child.text, 200)
        ]
        if not authors:
            author = _element_text(entry, {"author", "creator", "name"})
            authors = [author] if author else []
        categories = [
            _text(child.attrib.get("term") or child.text, 100)
            for child in entry.iter()
            if _tag(child.tag) == "category" and _text(child.attrib.get("term") or child.text, 100)
        ]
        excerpt = _element_text(entry, {"description", "summary", "content", "encoded"})
        serialized = ET.tostring(entry, encoding="unicode")[:20_000]
        record = _blog_record(
            source=source,
            title=title,
            url=item_url,
            published_at=published,
            authors=authors,
            categories=categories,
            schema_type="BlogPosting",
            excerpt=BeautifulSoup(excerpt, "html.parser").get_text(" ", strip=True),
            date_basis="feed_date",
            structured=True,
            visual=bool(re.search(r"(?:<|&lt;)(?:img|picture)\b|media:content", serialized, re.I)),
            video=bool(re.search(r"<(?:video|iframe)\b|youtube|vimeo", serialized, re.I)),
        )
        if record:
            records.append(record)
    return records, ""


def _blog_json_ld_records(
    soup: BeautifulSoup,
    source: str,
    now: datetime,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}, limit=100):
        try:
            payload = json.loads(script.string or script.get_text(" ", strip=True))
        except (json.JSONDecodeError, RecursionError, TypeError):
            continue
        stack = [payload]
        visited = 0
        while stack and visited < 500:
            current = stack.pop()
            visited += 1
            if isinstance(current, list):
                stack.extend(current[:100])
                continue
            if not isinstance(current, Mapping):
                continue
            stack.extend(
                value for value in list(current.values())[:100] if isinstance(value, (dict, list))
            )
            raw_types = current.get("@type", ())
            types = raw_types if isinstance(raw_types, list) else [raw_types]
            schema_type = next(
                (
                    str(item)
                    for item in types
                    if str(item).casefold() in {"article", "blogposting", "newsarticle"}
                ),
                "",
            )
            if not schema_type:
                continue
            record = _blog_record(
                source=source,
                title=current.get("headline") or current.get("name"),
                url=(
                    current.get("url") or _blog_mapping_url(current.get("mainEntityOfPage"), source)
                ),
                published_at=_blog_date(current.get("datePublished"), now),
                authors=_blog_names(current.get("author")),
                categories=_blog_categories(
                    current.get("articleSection") or current.get("keywords")
                ),
                schema_type=schema_type,
                excerpt=str(current.get("description") or current.get("articleBody") or ""),
                date_basis="source_explicit",
                structured=True,
                visual=bool(current.get("image") or current.get("thumbnailUrl")),
                video=bool(current.get("video")),
            )
            if record:
                records.append(record)
    return records


def _blog_html_records(body: bytes, source: str, now: datetime) -> list[dict[str, Any]]:
    soup = parse_html(body.decode(errors="replace"), "html.parser")
    records = _blog_json_ld_records(soup, source, now)
    for article in soup.find_all("article", limit=50):
        heading = article.find(["h1", "h2", "h3", "h4"])
        anchor = heading.find("a", href=True) if isinstance(heading, Tag) else None
        anchor = anchor or article.find("a", href=True)
        item_url = _safe_url(str(anchor.get("href") or ""), source) if anchor else source
        author_nodes = article.find_all(
            attrs={"class": re.compile(r"(?:author|byline)", re.I)}, limit=10
        )
        category_nodes = article.find_all(
            "a", href=re.compile(r"/(?:category|tag|topic)s?/", re.I), limit=20
        )
        record = _blog_record(
            source=source,
            title=(
                heading.get_text(" ", strip=True)
                if isinstance(heading, Tag)
                else anchor.get_text(" ", strip=True)
                if anchor
                else ""
            ),
            url=item_url,
            published_at=_blog_node_date(article, now),
            authors=[node.get_text(" ", strip=True) for node in author_nodes],
            categories=[node.get_text(" ", strip=True) for node in category_nodes],
            schema_type="BlogPosting"
            if _BLOG_SURFACE_RE.search(urlparse(item_url).path)
            else "Article",
            excerpt=article.get_text(" ", strip=True),
            date_basis="source_explicit",
            structured=True,
            visual=bool(article.find(["img", "picture"])),
            video=bool(article.find(["video", "iframe"])),
        )
        if record:
            records.append(record)
    if not records:
        container = soup.find("main") or soup
        for anchor in container.find_all("a", href=True, limit=500):
            if anchor.find_parent(["nav", "footer", "header"]):
                continue
            item_url = _safe_url(str(anchor.get("href") or ""), source)
            if not _BLOG_SURFACE_RE.search(urlparse(item_url).path):
                continue
            title = _text(anchor.get_text(" ", strip=True), 300)
            if len(title) < 8:
                continue
            parent = anchor.find_parent(["article", "li", "section", "div"])
            context_node = parent if isinstance(parent, Tag) else anchor
            record = _blog_record(
                source=source,
                title=title,
                url=item_url,
                published_at=_blog_node_date(context_node, now),
                excerpt=context_node.get_text(" ", strip=True),
                date_basis="source_explicit",
                structured=False,
                visual=bool(context_node.find(["img", "picture"])),
                video=bool(context_node.find(["video", "iframe"])),
            )
            if record:
                records.append(record)
    return records


def _dedupe_blog_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for raw in records:
        record = dict(raw)
        key = str(record.get("url") or "") or _hash(record.get("title"), record.get("published_at"))
        current = deduped.get(key)
        if current is None:
            deduped[key] = record
            continue
        for list_key in ("authors", "categories", "topics"):
            current[list_key] = list(
                dict.fromkeys([*current.get(list_key, ()), *record.get(list_key, ())])
            )[:30]
        for bool_key in ("structured", "visual", "video", "cta"):
            current[bool_key] = bool(current.get(bool_key) or record.get(bool_key))
        if current.get("published_at") is None and record.get("published_at") is not None:
            current["published_at"] = record["published_at"]
            current["date_basis"] = record.get("date_basis", "unknown")
            current["source_surface_url"] = record.get("source_surface_url", "")
        if len(str(record.get("excerpt") or "")) > len(str(current.get("excerpt") or "")):
            current["excerpt"] = record["excerpt"]
        current["content_class"] = _blog_content_class(current)
    return list(deduped.values())[:40]


def parse_corporate_blog_engine(
    body: str | bytes,
    url: str,
    *,
    company_key: str = "",
    first_party_source_url: str = "",
    officially_linked: bool = False,
    additional_surfaces: Sequence[FrontierDocument] = (),
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    """Measure one bounded, identity-proven public corporate editorial system."""

    scanner = "corporate_blog_engine_v1"
    now = _now(observed_at)
    primary = FrontierDocument(
        url=url,
        content=body,
        kind="corporate_blog_surface",
        mime_type="",
        publisher=_host(url),
        first_party_source_url=first_party_source_url,
        officially_linked=officially_linked,
    )
    company = _host(company_key) or _host(first_party_source_url) or _host(url)
    supplied = [primary, *list(additional_surfaces)[:4]]
    accepted: list[FrontierDocument] = []
    for document in supplied:
        source = _safe_url(document.url)
        if source and not _is_comment_feed_url(source) and (
            _same_company(source, company)
            or (
                document.officially_linked
                and document.first_party_source_url
                and _same_company(document.first_party_source_url, company)
            )
        ):
            accepted.append(document)
    if not accepted:
        return _empty(
            scanner,
            company,
            "Blog surface lacks exact first-party linkage.",
            observed_at=now,
            status="identity_ambiguous",
        )

    all_records: list[dict[str, Any]] = []
    artifacts: list[EvidenceArtifact] = []
    errors: list[str] = []
    truncated = len(supplied) > 5
    successful = 0
    primary_source = _safe_url(accepted[0].url)
    for document in accepted:
        source = _safe_url(document.url)
        raw = document.body
        bounded = raw[:_MAX_DOCUMENT_BYTES]
        document_truncated = len(raw) > len(bounded)
        truncated = truncated or document_truncated
        prefix = _xml_parse_bytes(bounded)[:100].lower()
        is_xml = (
            "xml" in document.mime_type.casefold()
            or prefix.startswith((b"<?xml", b"<rss", b"<feed", b"<urlset"))
            or bool(re.match(rb"<!\s*(?:doctype\s+(?!html\b)|entity\b)", prefix))
        )
        if is_xml:
            records, error = _blog_feed_records(bounded, source, now)
            if error:
                errors.append(f"{source}: {error}")
                continue
        else:
            records = _blog_html_records(bounded, source, now)
        successful += 1
        all_records.extend(records)
        artifacts.append(
            _artifact(
                scanner,
                company,
                replace(
                    document,
                    url=source,
                    content=bounded,
                    kind="corporate_blog_feed" if is_xml else "corporate_blog_surface",
                ),
                now,
                excerpt=f"{len(records)} bounded editorial item candidate(s)",
                truncated=document_truncated,
            )
        )
    if not successful:
        return _result(
            scanner,
            company,
            artifacts=artifacts,
            observed_at=now,
            status="failed",
            surface="first_party_linked_corporate_blog",
            scope=primary_source,
            attempted=len(accepted),
            successful=0,
            failed=len(accepted),
            total=len(accepted),
            completeness="none",
            truncated=truncated,
            absence_interpretable=False,
            limitations=("No valid bounded blog surface could be parsed.",),
            errors=errors,
            applicable_reason="First-party-linked blog surfaces were supplied.",
        )

    records = _dedupe_blog_records(all_records)
    editorial = [item for item in records if item["content_class"] == "editorial"]
    dated_editorial = [item for item in editorial if isinstance(item.get("published_at"), datetime)]
    recent_30 = [
        item
        for item in dated_editorial
        if timedelta(0) <= now - item["published_at"] <= timedelta(days=30)
    ]
    recent_90 = [
        item
        for item in dated_editorial
        if timedelta(0) <= now - item["published_at"] <= timedelta(days=90)
    ]
    recent_180 = [
        item
        for item in dated_editorial
        if timedelta(0) <= now - item["published_at"] <= timedelta(days=180)
    ]
    active_months = {item["published_at"].strftime("%Y-%m") for item in recent_180}
    sorted_dates = sorted({item["published_at"] for item in dated_editorial})
    gaps = [
        max(0, (right - left).days)
        for left, right in zip(sorted_dates, sorted_dates[1:], strict=False)
    ]
    median_gap = sorted(gaps)[len(gaps) // 2] if gaps else None
    authors = sorted(
        {str(author) for item in editorial for author in item.get("authors", ()) if str(author)}
    )
    categories = sorted(
        {
            str(category)
            for item in editorial
            for category in item.get("categories", ())
            if str(category)
        }
    )
    topic_counts = {
        topic: sum(topic in item.get("topics", ()) for item in editorial)
        for topic, _ in _BLOG_TOPIC_PATTERNS
    }
    topic_counts = {key: value for key, value in topic_counts.items() if value}
    top_topics = [
        key for key, _ in sorted(topic_counts.items(), key=lambda item: (-item[1], item[0]))[:3]
    ]
    editorial_count = len(editorial)
    structured_count = sum(bool(item.get("structured")) for item in editorial)
    visual_count = sum(bool(item.get("visual")) for item in editorial)
    video_count = sum(bool(item.get("video")) for item in editorial)
    cta_count = sum(bool(item.get("cta")) for item in editorial)
    sample_count = len(records)
    qualifying = (
        editorial_count >= 4
        and len(dated_editorial) >= 3
        and len(recent_90) >= 1
        and len(active_months) >= 2
    )
    maturity_score = 0
    maturity_score += 10 if editorial_count >= 4 else 0
    maturity_score += 10 if len(dated_editorial) >= 3 else 0
    maturity_score += 15 if recent_90 else 0
    maturity_score += 10 if len(recent_90) >= 4 else 0
    maturity_score += 15 if len(active_months) >= 2 else 0
    maturity_score += 10 if len(active_months) >= 4 else 0
    maturity_score += 10 if editorial_count and structured_count / editorial_count >= 0.5 else 0
    maturity_score += 5 if len(categories) >= 3 else 0
    maturity_score += 5 if len(authors) >= 2 else 0
    maturity_score += 5 if editorial_count and visual_count / editorial_count >= 0.5 else 0
    maturity_score += 5 if editorial_count and cta_count / editorial_count >= 0.25 else 0
    if qualifying and maturity_score >= 70 and len(recent_90) >= 4 and len(active_months) >= 3:
        maturity = "robust"
    elif qualifying and maturity_score >= 50:
        maturity = "established"
    elif recent_90:
        maturity = "active"
    elif editorial:
        maturity = "present_or_stale"
    else:
        maturity = "non_editorial_surface"
    latest = max(
        (item["published_at"] for item in dated_editorial),
        default=None,
    )
    latest_basis = next(
        (
            str(item.get("date_basis") or "unknown")
            for item in dated_editorial
            if item.get("published_at") == latest
        ),
        "unknown",
    )
    aggregate = {
        "maturity": maturity,
        "maturity_score": maturity_score,
        "sampled_items": sample_count,
        "editorial_items": editorial_count,
        "dated_editorial_items": len(dated_editorial),
        "recent_30d": len(recent_30),
        "recent_90d": len(recent_90),
        "recent_180d": len(recent_180),
        "active_months_180d": len(active_months),
        "median_gap_days": median_gap,
        "distinct_authors": len(authors),
        "distinct_categories": len(categories),
        "structured_items": structured_count,
        "visual_items": visual_count,
        "video_items": video_count,
        "cta_items": cta_count,
        "top_topics": top_topics,
        "content_classes": {
            name: sum(item["content_class"] == name for item in records)
            for name in (
                "editorial",
                "press_or_investor",
                "product_update",
                "docs_or_support",
                "unknown",
            )
        },
    }
    confidence = "high" if _same_company(primary_source, company) else "medium"
    facts: list[_Fact] = [
        _Fact(
            "content.blog_engine",
            aggregate,
            "json",
            f"owned-publishing:{company}",
            (
                f"The bounded first-party-linked sample classifies the corporate blog engine as "
                f"{maturity}, with {len(recent_90)} dated editorial item(s) in 90 days across "
                f"{len(active_months)} active month(s) in 180 days."
            ),
            primary_source,
            {
                "score_policy": "established_editorial_engine_v1",
                "item_cap": 40,
                "surface_cap": 5,
                "first_party_link_basis": first_party_source_url,
            },
            confidence=confidence,  # type: ignore[arg-type]
            effective_at=latest,
            effective_time_basis=latest_basis,  # type: ignore[arg-type]
            score_eligible=maturity in {"established", "robust"},
            discovery_only=maturity not in {"established", "robust"},
            source_actor_key=f"domain:{company}",
            causal_root_key=f"owned-publishing:{company}",
        ),
        _Fact(
            "content.blog_topic_profile",
            {"topic_counts": topic_counts, "top_topics": top_topics},
            "json",
            f"owned-publishing:{company}:topics",
            "Observed title, category, and bounded excerpt terms form this outreach topic profile.",
            primary_source,
            confidence=confidence,  # type: ignore[arg-type]
            discovery_only=True,
            source_actor_key=f"domain:{company}",
            causal_root_key=f"owned-publishing:{company}",
        ),
        _Fact(
            "content.blog_visual_surface",
            {
                "editorial_items": editorial_count,
                "visual_items": visual_count,
                "video_items": video_count,
                "cta_items": cta_count,
            },
            "json",
            f"owned-publishing:{company}:visual",
            "The bounded editorial sample exposes these visual, video, and CTA markers.",
            primary_source,
            confidence=confidence,  # type: ignore[arg-type]
            discovery_only=True,
            source_actor_key=f"domain:{company}",
            causal_root_key=f"owned-publishing:{company}",
        ),
    ]
    for item in records:
        published = item.get("published_at")
        facts.append(
            _Fact(
                "content.blog_publication",
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"published_at", "excerpt"}
                },
                "json",
                f"blog-item:{_hash(item.get('item_id'))[:24]}",
                f"Bounded corporate blog item: {item.get('title') or item.get('item_id')}.",
                str(item.get("source_surface_url") or primary_source),
                {"content_class": item.get("content_class", "unknown")},
                confidence=confidence,  # type: ignore[arg-type]
                effective_at=published if isinstance(published, datetime) else None,
                effective_time_basis=(
                    str(item.get("date_basis") or "unknown")
                    if isinstance(published, datetime)
                    else "unknown"
                ),  # type: ignore[arg-type]
                discovery_only=True,
                source_actor_key=f"domain:{company}",
                causal_root_key=f"blog-item:{_hash(item.get('item_id'))}",
            )
        )
    completeness: Literal["partial", "complete"] = "partial" if truncated or errors else "complete"
    return _result(
        scanner,
        company,
        facts=facts,
        artifacts=artifacts,
        observed_at=now,
        surface="first_party_linked_corporate_blog",
        scope=primary_source,
        attempted=len(accepted),
        successful=successful,
        failed=len(errors),
        total=len(accepted),
        completeness=completeness,
        truncated=truncated,
        absence_interpretable=not truncated and not errors,
        limitations=(
            "The sample is bounded to five linked surfaces and forty item records.",
            "Observed editorial operations do not establish spend, traffic, performance, "
            "team size, budget, or purchase intent.",
            "Press, investor, changelog, docs, support, stale, undated, and future-dated "
            "items cannot qualify the score-bearing aggregate.",
        ),
        errors=errors,
        features={
            "corporate_blog_engine": {
                **aggregate,
                "authors": authors[:20],
                "categories": categories[:30],
                "topic_counts": topic_counts,
                "source_urls": [item.canonical_url for item in artifacts],
            }
        },
        applicable_reason="First-party-linked corporate blog surfaces were supplied.",
    )


_HIRING_PARTIAL_SURFACE_RE = re.compile(
    r"\b(?:load more|show more|view more jobs?|next page|page \d+ of \d+|"
    r"additional openings?|more openings?)\b",
    re.I,
)
_HIRING_PROVIDER_HOSTS = frozenset(
    {
        *_ATS_CAREER_HOSTS,
        "boards-api.greenhouse.io",
        "api.lever.co",
        "api.ashbyhq.com",
    }
)


def _normalized_structured_hiring_record(
    record: Mapping[str, Any],
    now: datetime,
) -> NormalizedJobPosting | None:
    if str(record.get("record_type") or "") != "public_job_evidence":
        return None
    provider = str(record.get("provider") or "").casefold()
    source_url = _safe_url(str(record.get("source_url") or ""))
    if provider not in {"greenhouse", "lever", "ashby"} or not source_url:
        return None
    normalized = dict(record)
    normalized["url"] = source_url
    normalized["description"] = _text(record.get("description_excerpt"), 1_000)
    normalized["datePosted"] = (
        None if record.get("posted_time_basis") == "source_updated" else record.get("posted_at")
    )
    return normalize_job_posting(
        normalized,
        provider=provider,
        fallback_url=source_url,
        now=now,
    )


def _hiring_summary_record(records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    return next(
        (item for item in records if str(item.get("record_type") or "") == "legacy_hiring_summary"),
        {},
    )


def parse_creative_hiring_demand(
    body: str | bytes,
    url: str,
    *,
    company_key: str = "",
    first_party_source_url: str = "",
    officially_linked: bool = False,
    additional_surfaces: Sequence[FrontierDocument] = (),
    structured_records: Sequence[Mapping[str, Any]] = (),
    primary_document: FrontierDocument | None = None,
    observed_at: datetime | None = None,
    is_detail_page: bool = False,
) -> FrontierIntelligence:
    """Normalize bounded public postings without equating listings to hires or budget."""

    scanner = "creative_hiring_demand_v1"
    now = _now(observed_at)
    company = _host(company_key) or _host(first_party_source_url) or _host(url)
    primary = primary_document or FrontierDocument(
        url=url,
        content=body,
        kind="company_career_surface",
        publisher=_host(url),
        first_party_source_url=first_party_source_url,
        officially_linked=officially_linked,
        discovery_basis="first_party_link" if officially_linked else "same_company_surface",
        discovery_evidence_url=first_party_source_url,
    )
    supplied = (
        [primary, *list(additional_surfaces)[:7]] if primary.body else list(additional_surfaces[:8])
    )
    accepted: list[FrontierDocument] = []
    artifacts: list[EvidenceArtifact] = []
    postings: list[NormalizedJobPosting] = []
    errors: list[str] = []
    truncated = len(additional_surfaces) > 7
    partial_surface = is_detail_page
    successful = 0

    for document in supplied:
        source = _safe_url(document.url)
        if not source or not _document_is_company_bound(document, company):
            continue
        raw = document.body
        bounded = raw[:_MAX_DOCUMENT_BYTES]
        document_truncated = len(raw) > len(bounded)
        truncated = truncated or document_truncated
        try:
            extracted = extract_job_postings(bounded, source, now=now, max_postings=250)
        except (RecursionError, TypeError, ValueError) as exc:
            errors.append(f"{source}: {type(exc).__name__}: {_text(exc, 300)}")
            continue
        soup = parse_html(bounded.decode(errors="replace"), "html.parser")
        top_level = _text(
            " ".join(
                node.get_text(" ", strip=True) for node in soup.find_all(["title", "h1"], limit=10)
            ),
            2_000,
        )
        host = _host(source)
        semantic_surface = bool(
            extracted
            or host in _ATS_CAREER_HOSTS
            or _CAREER_SURFACE_RE.search(f"{urlparse(source).path} {top_level}")
        )
        if not semantic_surface:
            # A DNS-resolving label is authorization to inspect, never evidence that the page is
            # a careers board and never a basis for a zero-opening tombstone.
            continue
        accepted.append(document)
        successful += 1
        postings.extend(extracted)
        page_text = _text(soup.get_text(" ", strip=True), 20_000)
        page_partial = bool(
            document_truncated
            or soup.find("link", rel=lambda value: value and "next" in str(value).casefold())
            or _HIRING_PARTIAL_SURFACE_RE.search(page_text)
        )
        partial_surface = partial_surface or page_partial
        artifacts.append(
            _artifact(
                scanner,
                company,
                replace(
                    document,
                    url=source,
                    content=bounded,
                    kind=(
                        "ats_career_board"
                        if host in _ATS_CAREER_HOSTS
                        else "company_career_surface"
                    ),
                ),
                now,
                excerpt=f"{len(extracted)} distinct bounded posting candidate(s)",
                truncated=document_truncated,
            )
        )

    structured = [
        posting
        for record in structured_records[:500]
        if (posting := _normalized_structured_hiring_record(record, now)) is not None
    ]
    postings = deduplicate_job_postings([*postings, *structured])
    raw_summary_record = _hiring_summary_record(structured_records)
    summary_record = (
        raw_summary_record
        if structured
        or raw_summary_record.get("ats_boards")
        or raw_summary_record.get("source_urls")
        else {}
    )
    structured_complete = bool(summary_record.get("coverage_complete"))
    structured_total = max(0, int(summary_record.get("open_jobs") or 0))
    if summary_record:
        source_candidates = [
            source
            for raw_source in summary_record.get("source_urls", ())
            if (source := _safe_url(str(raw_source)))
            and (_host(source) in _HIRING_PROVIDER_HOSTS or _same_company(source, company))
        ]
        structured_source = (
            source_candidates[0] if source_candidates else _safe_url(first_party_source_url or url)
        )
        if structured_source:
            structured_payload = [
                {
                    "title": item.title,
                    "source_url": item.source_url,
                    "location": item.location,
                    "department": item.department,
                    "posted_at": item.posted_at.isoformat() if item.posted_at else None,
                    "role_families": list(item.role_families),
                    "creative_subfamilies": list(item.creative_subfamilies),
                    "description_hash": item.description_hash,
                }
                for item in structured[:250]
            ]
            artifacts.append(
                _artifact(
                    scanner,
                    company,
                    FrontierDocument(
                        url=structured_source,
                        content=json.dumps(
                            {
                                "summary": {
                                    "open_jobs": structured_total,
                                    "coverage_complete": structured_complete,
                                },
                                "postings": structured_payload,
                            },
                            sort_keys=True,
                        ),
                        kind="normalized_public_ats_records",
                        mime_type="application/json",
                        publisher=_host(structured_source),
                        first_party_source_url=first_party_source_url or url,
                        officially_linked=False,
                        discovery_basis="structured_public_ats_adapter",
                        discovery_evidence_url=structured_source,
                        robots_state="not_applicable",
                    ),
                    now,
                    excerpt=f"{len(structured)} normalized public ATS record(s)",
                )
            )

    current = [item for item in postings if item.current]
    creative = [item for item in current if item.classification.creative]
    photo_video = [item for item in creative if item.classification.photo_video]
    dated_creative = [item for item in creative if item.posted_at is not None]
    recent_creative = [
        item
        for item in dated_creative
        if timedelta(0) <= now - item.posted_at <= timedelta(days=60)  # type: ignore[operator]
    ]
    complete = bool(
        artifacts
        and not errors
        and not truncated
        and not partial_surface
        and (accepted or structured_complete)
    )
    if not artifacts:
        return _empty(
            scanner,
            company,
            "No identity-bound, semantically valid public careers surface or ATS record "
            "was available.",
            observed_at=now,
            status="identity_ambiguous" if supplied or structured_records else "not_applicable",
        )

    current_openings = max(len(current), structured_total if structured_complete else 0)
    creative_count = len(creative)
    photo_video_count = len(photo_video)
    creative_share = round(creative_count / current_openings, 4) if current_openings else 0.0
    subfamily_counts = {
        family: sum(family in item.creative_subfamilies for item in creative)
        for family in sorted({family for item in creative for family in item.creative_subfamilies})
    }
    production_context = sum(bool(item.production_pressure_signals) for item in creative)
    external_context = sum(bool(item.external_capacity_signals) for item in creative)
    creative_coverage_complete = bool(complete and structured_total <= len(current))
    counts_lower_bound = not creative_coverage_complete
    primary_source = artifacts[0].canonical_url
    latest = max((item.posted_at for item in dated_creative), default=None)
    workforce_gate = current_openings >= 10 and (complete or structured_complete)
    creative_gate = creative_count >= 1
    velocity_gate = bool(recent_creative)
    stable_qualifiers = {
        "claim_basis": "literal_public_openings",
        "current_state": True,
    }
    root = f"surface:hiring:{company}"
    facts: list[_Fact] = []
    for posting in current[:250]:
        facts.append(
            _Fact(
                "hiring.job_posting",
                {
                    **posting.as_dict(),
                    "stable_id": posting.stable_id,
                    "provider": posting.provider,
                    "title": posting.title,
                    "source_urls": list(posting.source_urls),
                    "location": posting.location,
                    "department": posting.department,
                    "posted_at": posting.posted_at.isoformat() if posting.posted_at else None,
                    "role_families": list(posting.role_families),
                    "creative_subfamilies": list(posting.creative_subfamilies),
                    "photo_video": posting.classification.photo_video,
                    "production_pressure_signals": list(posting.production_pressure_signals),
                    "external_capacity_signals": list(posting.external_capacity_signals),
                    "ai_workflow_signals": list(posting.ai_workflow_signals),
                    "rights_risk_signals": list(posting.rights_risk_signals),
                    "description_hash": posting.description_hash,
                },
                "json",
                posting.stable_id,
                f"Current public posting: {posting.title}.",
                posting.source_url or primary_source,
                {"claim_basis": "literal_public_posting"},
                effective_at=posting.posted_at,
                effective_time_basis="source_explicit" if posting.posted_at else "unknown",
                discovery_only=True,
                source_actor_key=f"domain:{company}",
                causal_root_key=root,
            )
        )
    facts.extend(
        [
            _Fact(
                "hiring.workforce_demand_current",
                {
                    "current_openings": current_openings,
                    "distinct_postings": current_openings,
                    "sampled_postings": len(current),
                    "coverage_complete": complete or structured_complete,
                    "surface_count": min(8, max(1, len(accepted) + bool(summary_record))),
                },
                "json",
                f"hiring:{company}:workforce-current",
                f"Public careers evidence lists {current_openings} current opening(s).",
                primary_source,
                {
                    **stable_qualifiers,
                    "coverage_complete": complete or structured_complete,
                    "counts_are_lower_bounds": False,
                    "projection_score_gate": workforce_gate,
                },
                score_eligible=workforce_gate,
                discovery_only=False,
                source_actor_key=f"domain:{company}",
                causal_root_key=root,
            ),
            _Fact(
                "hiring.creative_demand_current",
                {
                    "current_creative_openings": creative_count,
                    "current_photo_video_openings": photo_video_count,
                    "creative_share": creative_share,
                    "role_families": list(subfamily_counts),
                    "role_family_counts": subfamily_counts,
                    "production_language_postings": production_context,
                    "external_capacity_language_postings": external_context,
                    "strength_tier": (
                        "five_plus"
                        if creative_count >= 5
                        else "two_plus"
                        if creative_count >= 2
                        else "one"
                        if creative_count
                        else "none"
                    ),
                },
                "json",
                f"hiring:{company}:creative-current",
                f"Public careers evidence lists {creative_count} current creative opening(s), "
                f"including {photo_video_count} photo/video opening(s).",
                primary_source,
                {
                    **stable_qualifiers,
                    "coverage_complete": creative_coverage_complete,
                    "counts_are_lower_bounds": counts_lower_bound,
                    "projection_score_gate": creative_gate,
                },
                score_eligible=creative_gate,
                discovery_only=False,
                source_actor_key=f"domain:{company}",
                causal_root_key=root,
            ),
            _Fact(
                "hiring.creative_velocity_current",
                {
                    "recent_creative_openings_60d": len(recent_creative),
                    "dated_creative_openings": len(dated_creative),
                    "window_days": 60,
                    "latest_posted_at": latest.isoformat() if latest else None,
                },
                "json",
                f"hiring:{company}:creative-velocity-current",
                f"Public careers evidence includes {len(recent_creative)} source-dated creative "
                "opening(s) from the last 60 days.",
                primary_source,
                {
                    **stable_qualifiers,
                    "coverage_complete": creative_coverage_complete,
                    "counts_are_lower_bounds": counts_lower_bound,
                    "projection_score_gate": velocity_gate,
                },
                effective_at=latest,
                effective_time_basis="source_explicit" if latest else "unknown",
                score_eligible=velocity_gate,
                discovery_only=False,
                source_actor_key=f"domain:{company}",
                causal_root_key=root,
            ),
        ]
    )
    coverage: Literal["partial", "complete"] = "complete" if complete else "partial"
    return _result(
        scanner,
        company,
        facts=facts,
        artifacts=artifacts,
        observed_at=now,
        surface="public_company_careers_and_ats",
        scope=primary_source,
        attempted=len(supplied) + bool(summary_record),
        successful=successful + bool(summary_record),
        failed=len(errors),
        total=len(supplied) + bool(summary_record),
        completeness=coverage,
        truncated=truncated or partial_surface,
        absence_interpretable=complete,
        limitations=(
            "Counts describe distinct public postings, not hires, seats, employee count, "
            "growth, or urgency.",
            "Creative classification is deterministic and conservative; partial surfaces "
            "are lower bounds.",
            "Public hiring does not establish budget, outsourcing intent, or purchase intent.",
        ),
        errors=errors,
        features={
            "creative_hiring_demand": {
                "current_openings": current_openings,
                "current_creative_openings": creative_count,
                "current_photo_video_openings": photo_video_count,
                "recent_creative_openings_60d": len(recent_creative),
                "coverage_complete": complete or structured_complete,
                "counts_are_lower_bounds": counts_lower_bound,
                "creative_subfamily_counts": subfamily_counts,
                "source_urls": [item.canonical_url for item in artifacts],
                "postings": [item.as_dict() for item in current[:50]],
            }
        },
        applicable_reason=(
            "An identity-bound public careers surface or normalized ATS record was supplied."
        ),
    )


_RELEASE_SUBSTANTIVE_RE = re.compile(
    r"\b(?:announc(?:e|ed|ing)|introduc(?:e|ed|ing)|launch(?:ed|ing)?|new (?:feature|"
    r"integration|workflow|dashboard|editor|api|model|product)|feature|integration|"
    r"generally available|general availability|redesign|major update|now supports?|"
    r"expanded|upgrade)\b",
    re.I,
)
_RELEASE_OPERATIONAL_RE = re.compile(
    r"\b(?:bug ?fix(?:es)?|patch(?:es)?|security advisor(?:y|ies)|cve-\d+|incident|"
    r"maintenance|status update|deprecat(?:e|ed|ion)|minor fixes?|performance fixes?|"
    r"hotfix)\b",
    re.I,
)


def _surface_documents(
    scanner_id: str,
    body: str | bytes,
    url: str,
    *,
    company_key: str,
    first_party_source_url: str,
    officially_linked: bool,
    additional_surfaces: Sequence[FrontierDocument],
    document_kind: str,
    surface_cap: int,
    primary_document: FrontierDocument | None = None,
) -> tuple[str, list[FrontierDocument], bool]:
    company = _host(company_key) or _host(first_party_source_url) or _host(url)
    primary = (
        replace(
            primary_document,
            url=url,
            content=body,
            kind=document_kind,
            publisher=primary_document.publisher or _host(url),
            first_party_source_url=(
                primary_document.first_party_source_url or first_party_source_url
            ),
            officially_linked=(
                primary_document.officially_linked
                or officially_linked
                or bool(first_party_source_url)
            ),
        )
        if primary_document is not None
        else FrontierDocument(
            url=url,
            content=body,
            kind=document_kind,
            publisher=_host(url),
            first_party_source_url=first_party_source_url,
            officially_linked=officially_linked or bool(first_party_source_url),
        )
    )
    supplied = [primary, *list(additional_surfaces)]
    accepted: list[FrontierDocument] = []
    truncated = len(supplied) > surface_cap
    for document in supplied[:surface_cap]:
        source = _safe_url(document.url)
        cited = bool(
            document.first_party_source_url
            and _same_company(document.first_party_source_url, company)
        )
        if not source or not (_same_company(source, company) or cited):
            continue
        raw = document.body
        bounded = raw[:_MAX_DOCUMENT_BYTES]
        truncated = truncated or len(raw) > len(bounded)
        accepted.append(
            replace(
                document,
                url=source,
                content=bounded,
                kind=(document_kind if document.kind in {"", "public_document"} else document.kind),
                first_party_source_url=(document.first_party_source_url or first_party_source_url),
                officially_linked=(document.officially_linked or cited),
            )
        )
    return company, accepted, truncated


def _structured_feed_records(
    document: FrontierDocument,
    now: datetime,
    *,
    item_cap: int,
) -> tuple[list[dict[str, Any]], str]:
    """Extract bounded generic feed records without executing or expanding content."""

    body = document.body
    source = _safe_url(document.url)
    prefix = body.lstrip()[:100].lower()
    records: list[dict[str, Any]] = []
    if prefix.startswith((b"{", b"[")) or "json" in document.mime_type.casefold():
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, RecursionError, TypeError) as exc:
            return [], f"invalid JSON feed: {type(exc).__name__}"
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, Mapping):
            # Keep an explicit empty array valid; do not turn an API error or
            # an unfamiliar wrapper into successful empty release coverage.
            items = next(
                (payload[key] for key in ("items", "releases", "entries") if key in payload),
                None,
            )
        else:
            items = None
        if not isinstance(items, list):
            return [], "JSON release feed requires an items/releases/entries array"
        invalid_entries = any(not isinstance(item, Mapping) for item in items[:item_cap])
        for item in items[:item_cap]:
            if not isinstance(item, Mapping):
                continue
            published = _date(
                item.get("date_published") or item.get("published_at") or item.get("date")
                or item.get("released_at") or item.get("release_date")
                or item.get("publishedAt") or item.get("datePublished")
            )
            modified = _date(item.get("date_modified") or item.get("modified_at"))
            if published and not (
                datetime(1990, 1, 1, tzinfo=UTC) <= published <= now + timedelta(days=1)
            ):
                published = None
            if modified and not (
                datetime(1990, 1, 1, tzinfo=UTC) <= modified <= now + timedelta(days=1)
            ):
                modified = None
            item_url = _safe_url(str(item.get("url") or item.get("external_url") or ""), source)
            records.append(
                {
                    "item_id": _text(item.get("id"), 300)
                    or item_url
                    or _hash(item.get("title") or item.get("name"), published),
                    "title": _text(item.get("title") or item.get("name"), 500),
                    "url": item_url,
                    "published_at": published,
                    "modified_at": modified,
                    "date_basis": "feed_date" if published else "unknown",
                    "excerpt": _text(
                        item.get("summary") or item.get("content_text")
                        or item.get("description") or "",
                        1_000,
                    ),
                    "source_surface_url": source,
                }
            )
        return records, "JSON release feed contains non-object entries" if invalid_entries else ""
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", body, re.I):
        return [], "XML declarations with DOCTYPE or ENTITY are not accepted"
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, RecursionError) as exc:
        return [], f"invalid XML feed: {type(exc).__name__}"
    entries = [node for node in root.iter() if _tag(node.tag) in {"item", "entry", "url"}]
    for entry in entries[:item_cap]:
        published = _date(
            _element_text(entry, {"published", "pubdate", "date", "publication_date"})
        )
        modified = _date(_element_text(entry, {"updated", "modified", "lastmod"}))
        if published and not (
            datetime(1990, 1, 1, tzinfo=UTC) <= published <= now + timedelta(days=1)
        ):
            published = None
        if modified and not (
            datetime(1990, 1, 1, tzinfo=UTC) <= modified <= now + timedelta(days=1)
        ):
            modified = None
        item_url = _safe_url(_element_text(entry, {"loc", "link"}), source)
        if not item_url:
            for child in entry.iter():
                if _tag(child.tag) == "link" and child.attrib.get("href"):
                    item_url = _safe_url(str(child.attrib["href"]), source)
                    break
        records.append(
            {
                "item_id": _element_text(entry, {"id", "guid"})
                or item_url
                or _hash(_element_text(entry, {"title"}), published),
                "title": _element_text(entry, {"title"}),
                "url": item_url,
                "published_at": published,
                "modified_at": modified,
                "date_basis": "feed_date" if published else "unknown",
                "excerpt": _element_text(entry, {"summary", "description", "content"}),
                "source_surface_url": source,
            }
        )
    return records, ""


def _release_html_records(
    document: FrontierDocument,
    now: datetime,
    *,
    item_cap: int,
) -> list[dict[str, Any]]:
    source = _safe_url(document.url)
    soup = parse_html(document.body.decode(errors="replace"), "html.parser")
    records: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}, limit=50):
        try:
            payload = json.loads(script.string or script.get_text(" ", strip=True))
        except (json.JSONDecodeError, RecursionError, TypeError):
            continue
        stack = [payload]
        visited = 0
        while stack and visited < 300 and len(records) < item_cap:
            current = stack.pop()
            visited += 1
            if isinstance(current, list):
                stack.extend(current[:100])
                continue
            if not isinstance(current, Mapping):
                continue
            stack.extend(
                value for value in list(current.values())[:100] if isinstance(value, (dict, list))
            )
            raw_types = current.get("@type", ())
            types = raw_types if isinstance(raw_types, list) else [raw_types]
            if not any(
                str(value).casefold().rsplit("/", 1)[-1]
                in {"article", "blogposting", "newsarticle", "techarticle"}
                for value in types
            ):
                continue
            published = _date(current.get("datePublished"))
            modified = _date(current.get("dateModified"))
            if published and not (
                datetime(1990, 1, 1, tzinfo=UTC) <= published <= now + timedelta(days=1)
            ):
                published = None
            if modified and not (
                datetime(1990, 1, 1, tzinfo=UTC) <= modified <= now + timedelta(days=1)
            ):
                modified = None
            item_url = _safe_url(str(current.get("url") or ""), source)
            title = _text(current.get("headline") or current.get("name"), 500)
            if title or item_url:
                records.append(
                    {
                        "item_id": item_url or _hash(title, published),
                        "title": title,
                        "url": item_url,
                        "published_at": published,
                        "modified_at": modified,
                        "date_basis": "source_explicit" if published else "unknown",
                        "excerpt": _text(current.get("description"), 1_000),
                        "source_surface_url": source,
                    }
                )
    nodes = list(soup.find_all("article", limit=item_cap))
    if not nodes:
        nodes = [
            node
            for node in soup.find_all(["li", "section", "div"], limit=300)
            if node.find("time") and node.find(["h1", "h2", "h3", "h4"])
        ][:item_cap]
    for node in nodes[:item_cap]:
        heading = node.find(["h1", "h2", "h3", "h4"])
        if not isinstance(heading, Tag):
            continue
        anchor = heading.find("a", href=True) or node.find("a", href=True)
        item_url = _safe_url(str(anchor.get("href") or ""), source) if anchor else source
        published = _blog_node_date(node, now)
        records.append(
            {
                "item_id": item_url or _hash(heading.get_text(" ", strip=True), published),
                "title": _text(heading.get_text(" ", strip=True), 500),
                "url": item_url,
                "published_at": published,
                "modified_at": None,
                "date_basis": "source_explicit" if published else "unknown",
                "excerpt": _text(node.get_text(" ", strip=True), 1_000),
                "source_surface_url": source,
            }
        )
    if not records:
        heading = soup.find("h1")
        published = _blog_node_date(soup, now)
        if isinstance(heading, Tag):
            records.append(
                {
                    "item_id": source,
                    "title": _text(heading.get_text(" ", strip=True), 500),
                    "url": source,
                    "published_at": published,
                    "modified_at": None,
                    "date_basis": "source_explicit" if published else "unknown",
                    "excerpt": _text((soup.find("main") or soup).get_text(" ", strip=True), 1_000),
                    "source_surface_url": source,
                }
            )
    return records[:item_cap]


def _connected_record_groups(
    records: Sequence[Mapping[str, Any]],
    same_record: Callable[[Mapping[str, Any], Mapping[str, Any]], bool],
) -> list[list[Mapping[str, Any]]]:
    parents = list(range(len(records)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for left in range(len(records)):
        for right in range(left + 1, len(records)):
            if same_record(records[left], records[right]):
                left_root = root(left)
                right_root = root(right)
                if left_root != right_root:
                    parents[right_root] = left_root
    grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for index, record in enumerate(records):
        grouped[root(index)].append(record)
    return list(grouped.values())


def _dedupe_release_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in records:
        title = _text(raw.get("title"), 500)
        published = raw.get("published_at")
        published = published if isinstance(published, datetime) else None
        modified = raw.get("modified_at")
        modified = modified if isinstance(modified, datetime) else None
        raw_url = str(raw.get("url") or "")
        canonical_url = _canonical_surface_url(raw_url, raw_url) if raw_url else ""
        raw_item_id = _text(raw.get("item_id"), 300)
        item_id_url = _safe_url(raw_item_id)
        item_id = _canonical_surface_url(item_id_url, item_id_url) if item_id_url else raw_item_id
        context = f"{title} {_text(raw.get('excerpt'), 1_000)}"
        operational = bool(_RELEASE_OPERATIONAL_RE.search(context))
        substantive = bool(_RELEASE_SUBSTANTIVE_RE.search(context))
        release_type = (
            "operational_or_patch" if operational else "substantive" if substantive else "unknown"
        )
        source_surface = _safe_url(str(raw.get("source_surface_url") or ""))
        normalized.append(
            {
                "item_id": item_id or canonical_url or _hash(_name(title), published),
                "item_id_is_url": bool(item_id_url),
                "title": title,
                "normalized_title": _name(title),
                "url": canonical_url,
                "published_at": published,
                "modified_at": modified,
                "date_basis": _text(raw.get("date_basis"), 30) or "unknown",
                "release_type": release_type,
                "source_surface_urls": [source_surface] if source_surface else [],
                "date_source_surface_urls": [source_surface]
                if source_surface and published
                else [],
                "score_source_surface_urls": [source_surface]
                if source_surface and published and release_type == "substantive"
                else [],
            }
        )

    def same_release(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        same_title = bool(
            left.get("normalized_title")
            and left.get("normalized_title") == right.get("normalized_title")
        )
        left_date = left.get("published_at")
        right_date = right.get("published_at")
        same_time = bool(
            isinstance(left_date, datetime)
            and isinstance(right_date, datetime)
            and abs(left_date - right_date) <= timedelta(hours=36)
        )
        same_feed_id = bool(
            left.get("item_id")
            and left.get("item_id") == right.get("item_id")
            and not left.get("item_id_is_url")
            and not right.get("item_id_is_url")
        )
        same_item_url = bool(left.get("url") and left.get("url") == right.get("url") and same_title)
        return same_feed_id or same_item_url or (same_title and same_time)

    deduped: list[dict[str, Any]] = []
    for candidates in _connected_record_groups(normalized, same_release):
        dates = [
            item["published_at"]
            for item in candidates
            if isinstance(item.get("published_at"), datetime)
        ]
        conflicting_dates = bool(dates and max(dates) - min(dates) > timedelta(hours=36))
        merged_date = None if conflicting_dates else min(dates, default=None)
        modified_dates = [
            item["modified_at"]
            for item in candidates
            if isinstance(item.get("modified_at"), datetime)
        ]
        deduped.append(
            {
                "item_id": next(
                    (str(item.get("item_id")) for item in candidates if item.get("item_id")),
                    _hash(candidates),
                ),
                "title": max(
                    (str(item.get("title") or "") for item in candidates),
                    key=len,
                    default="",
                ),
                "url": next(
                    (str(item.get("url")) for item in candidates if item.get("url")),
                    "",
                ),
                "published_at": merged_date,
                "modified_at": max(modified_dates, default=None),
                "date_basis": (
                    "source_explicit"
                    if merged_date
                    and any(
                        item.get("published_at") == merged_date
                        and item.get("date_basis") == "source_explicit"
                        for item in candidates
                    )
                    else "feed_date"
                    if merged_date
                    else "unknown"
                ),
                "release_type": next(
                    (
                        release_type
                        for release_type in (
                            "operational_or_patch",
                            "substantive",
                            "unknown",
                        )
                        if any(item.get("release_type") == release_type for item in candidates)
                    ),
                    "unknown",
                ),
                "source_surface_urls": list(
                    dict.fromkeys(
                        source_url
                        for item in candidates
                        for source_url in item.get("source_surface_urls", [])
                        if source_url
                    )
                ),
                "date_source_surface_urls": list(
                    dict.fromkeys(
                        source_url
                        for item in candidates
                        if merged_date is not None and item.get("published_at") == merged_date
                        for source_url in item.get("date_source_surface_urls", [])
                        if source_url
                    )
                ),
                "score_source_surface_urls": list(
                    dict.fromkeys(
                        source_url
                        for item in candidates
                        if merged_date is not None
                        and item.get("published_at") == merged_date
                        and item.get("release_type") == "substantive"
                        for source_url in item.get("score_source_surface_urls", [])
                        if source_url
                    )
                ),
            }
        )
    return deduped[:40]


def parse_product_release_engine(
    body: str | bytes,
    url: str,
    *,
    company_key: str = "",
    first_party_source_url: str = "",
    officially_linked: bool = False,
    additional_surfaces: Sequence[FrontierDocument] = (),
    primary_document: FrontierDocument | None = None,
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    """Parse bounded first-party-linked changelog and release-feed surfaces."""

    scanner = "product_release_engine_v1"
    now = _now(observed_at)
    company, documents, truncated = _surface_documents(
        scanner,
        body,
        url,
        company_key=company_key,
        first_party_source_url=first_party_source_url,
        officially_linked=officially_linked,
        additional_surfaces=additional_surfaces,
        document_kind="product_release_surface",
        surface_cap=4,
        primary_document=primary_document,
    )
    if not documents:
        return _empty(
            scanner,
            company,
            "Release surface lacks exact first-party linkage.",
            observed_at=now,
            status="identity_ambiguous",
        )
    records: list[dict[str, Any]] = []
    artifacts: list[EvidenceArtifact] = []
    errors: list[str] = []
    successful = 0
    for document in documents:
        prefix = document.body.lstrip()[:100].lower()
        path = urlparse(document.url).path.casefold()
        feed_like = (
            "xml" in document.mime_type.casefold()
            or "json" in document.mime_type.casefold()
            or path.endswith((".xml", ".rss", ".atom", ".json"))
            or prefix.startswith((b"<?xml", b"<rss", b"<feed", b"{", b"["))
        )
        if feed_like:
            parsed, error = _structured_feed_records(document, now, item_cap=40)
            if error:
                errors.append(f"{document.url}: {error}")
                # Preserve supported entries from a mixed-format response, but
                # keep its coverage partial rather than dropping usable evidence.
                if not parsed:
                    continue
        else:
            parsed = _release_html_records(document, now, item_cap=40)
        successful += 1
        records.extend(parsed)
        artifacts.append(
            _artifact(
                scanner,
                company,
                replace(
                    document,
                    kind="product_release_feed" if feed_like else "product_release_surface",
                ),
                now,
                excerpt=f"{len(parsed)} bounded release candidate(s)",
                truncated=truncated,
            )
        )
    records = _dedupe_release_records(records)
    substantive = [
        item
        for item in records
        if item["release_type"] == "substantive"
        and isinstance(item.get("published_at"), datetime)
        and item.get("score_source_surface_urls")
        and timedelta(0) <= now - item["published_at"]
    ]
    recent_180 = [item for item in substantive if now - item["published_at"] <= timedelta(days=180)]
    recent_90 = [item for item in substantive if now - item["published_at"] <= timedelta(days=90)]
    active_months = {item["published_at"].strftime("%Y-%m") for item in recent_180}
    qualifying = len(recent_180) >= 4 and len(recent_90) >= 2 and len(active_months) >= 2
    latest_item = max(substantive, key=lambda item: item["published_at"], default=None)
    latest = latest_item["published_at"] if latest_item else None
    maturity = "established" if qualifying else "active" if recent_90 else "present"
    source = (
        str((latest_item or {}).get("score_source_surface_urls", [""])[0])
        if (latest_item or {}).get("score_source_surface_urls")
        else artifacts[0].canonical_url
        if artifacts
        else _safe_url(documents[0].url)
    )
    contributing_records = recent_180 or substantive
    # Nonqualifying summaries still describe successful parsed documents.
    # They must not lose provenance merely because every row is empty,
    # undated, operational or otherwise ineligible for a substantive count.
    aggregate_artifact_keys = _artifact_keys_for_sources(
        artifacts,
        (
            source_url
            for item in contributing_records
            for source_url in item.get("score_source_surface_urls", [])
        )
        if contributing_records
        else (artifact.canonical_url for artifact in artifacts),
    )
    aggregate = {
        "maturity": maturity,
        "substantive_items_180d": len(recent_180),
        "recent_90d": len(recent_90),
        "active_months_180d": len(active_months),
        "latest_release_at": latest.isoformat() if latest else None,
    }
    facts: list[_Fact] = [] if not successful else [
        _Fact(
            "product.release_engine",
            aggregate,
            "json",
            f"product-release:{company}",
            (
                f"The bounded release sample is {maturity}, with {len(recent_180)} "
                f"substantive dated item(s) in 180 days."
            ),
            source,
            {
                "score_policy": "established_product_release_engine_v1",
                "projection_score_gate": qualifying,
                "requires_explicit_dates": True,
                "item_cap": 40,
            },
            confidence="high" if _same_company(source, company) else "medium",
            effective_at=latest,
            effective_time_basis=(
                str(latest_item.get("date_basis"))  # type: ignore[arg-type]
                if latest_item
                else "unknown"
            ),
            score_eligible=qualifying,
            discovery_only=not qualifying,
            source_actor_key=f"domain:{company}",
            causal_root_key=f"product-release:{company}",
            artifact_keys=aggregate_artifact_keys,
        )
    ]
    current = next(
        (
            item
            for item in sorted(substantive, key=lambda item: item["published_at"], reverse=True)
            if now - item["published_at"] <= timedelta(days=30)
        ),
        None,
    )
    if current is not None:
        current_sources = tuple(str(item) for item in current.get("score_source_surface_urls", []))
        current_source = current_sources[0] if current_sources else source
        facts.append(
            _Fact(
                "product.release_current",
                {
                    "title": current["title"],
                    "url": current["url"],
                    "release_type": current["release_type"],
                },
                "json",
                f"product-release-current:{_hash(current['item_id'])[:24]}",
                (
                    "A substantive product release was explicitly dated within 30 days: "
                    f"{current['title']}."
                ),
                current_source,
                {
                    "score_policy": "recent_substantive_release_v1",
                    "projection_score_gate": True,
                    "date_window_days": 30,
                    "date_basis": current["date_basis"],
                },
                effective_at=current["published_at"],
                effective_time_basis=str(current["date_basis"]),  # type: ignore[arg-type]
                score_eligible=True,
                source_actor_key=f"domain:{company}",
                causal_root_key=f"product-release:{company}",
                artifact_keys=_artifact_keys_for_sources(artifacts, current_sources),
            )
        )
    current_item_id = str(current.get("item_id") or "") if current is not None else ""
    for item in records:
        if str(item.get("item_id") or "") == current_item_id:
            continue
        published_at = item.get("published_at")
        if not isinstance(published_at, datetime):
            continue
        item_sources = tuple(str(value) for value in item.get("source_surface_urls", []))
        item_source = item_sources[0] if item_sources else source
        facts.append(
            _Fact(
                "product.release_current",
                {
                    "title": item["title"],
                    "url": item["url"],
                    "release_type": item["release_type"],
                },
                "json",
                f"product-release-current:{_hash(item['item_id'])[:24]}",
                "The latest bounded record for this item does not satisfy the current "
                "substantive-release gate.",
                item_source,
                {
                    "score_policy": "recent_substantive_release_v1",
                    "projection_score_gate": False,
                    "date_window_days": 30,
                    "date_basis": item["date_basis"],
                },
                effective_at=published_at,
                effective_time_basis=str(item["date_basis"]),
                score_eligible=False,
                discovery_only=True,
                source_actor_key=f"domain:{company}",
                causal_root_key=f"product-release:{company}",
                artifact_keys=_artifact_keys_for_sources(artifacts, item_sources),
            )
        )
    for item in records:
        item_sources = tuple(str(value) for value in item.get("source_surface_urls", []))
        item_source = item_sources[0] if item_sources else source
        facts.append(
            _Fact(
                "product.release_item",
                {
                    "title": item["title"],
                    "url": item["url"],
                    "release_type": item["release_type"],
                    "modified_at": (
                        item["modified_at"].isoformat()
                        if isinstance(item.get("modified_at"), datetime)
                        else None
                    ),
                },
                "json",
                f"product-release-item:{_hash(item['item_id'])[:24]}",
                f"Bounded public release item: {item['title'] or item['item_id']}.",
                item_source,
                effective_at=item.get("published_at"),
                effective_time_basis=str(item.get("date_basis") or "unknown"),  # type: ignore[arg-type]
                discovery_only=True,
                source_actor_key=f"domain:{company}",
                causal_root_key=f"product-release:{company}",
                artifact_keys=_artifact_keys_for_sources(artifacts, item_sources),
            )
        )
    completeness: Literal["partial", "complete"] = "partial" if truncated or errors else "complete"
    return _result(
        scanner,
        company,
        facts=facts,
        artifacts=artifacts,
        observed_at=now,
        status="failed" if not successful else None,
        surface="first_party_linked_product_releases",
        scope=source,
        attempted=len(documents),
        successful=successful,
        failed=len(errors),
        total=len(documents),
        completeness="none" if not successful else completeness,
        truncated=truncated,
        absence_interpretable=bool(successful and not truncated and not errors),
        limitations=(
            "At most four surfaces and forty items are parsed.",
            "Counts describe parsed surfaces only; empty samples do not establish "
            "site-wide absence.",
            "Patch, security, incident, maintenance, undated, future, and stale rows cannot "
            "satisfy cadence.",
            "Observed releases do not establish adoption, success, users, revenue, staffing, "
            "budget, or roadmap truth.",
        ),
        errors=errors,
        features={
            "product_release_engine": {
                **aggregate,
                "sampled_items": len(records),
                "release_type_counts": {
                    release_type: sum(item["release_type"] == release_type for item in records)
                    for release_type in ("substantive", "operational_or_patch", "unknown")
                },
                "source_urls": [item.canonical_url for item in artifacts],
            }
        },
        applicable_reason="First-party-linked release surfaces were supplied.",
    )


_EVENT_CANCELLED_RE = re.compile(r"\b(?:cancelled|canceled|eventcancelled)\b", re.I)
_EVENT_UNCONFIRMED_RE = re.compile(
    r"\b(?:postponed|rescheduled|tentative|eventpostponed|eventrescheduled)\b",
    re.I,
)
_EVENT_ON_DEMAND_RE = re.compile(r"\b(?:on[ -]?demand|watch (?:the )?recording|recorded)\b", re.I)
_EVENT_NODE_SEMANTICS_RE = re.compile(
    r"\b(?:event|webinar|conference|summit|workshop|roundtable|town hall|demo day|"
    r"register|registration|speaker|venue|attend|livestream|on[ -]?demand)\b",
    re.I,
)


def _ics_date(value: str) -> datetime | None:
    raw = _text(value, 100).split(":", 1)[-1]
    for format_string in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(raw, format_string).replace(tzinfo=UTC)
        except ValueError:
            continue
    return _date(raw)


def _event_record(
    *,
    source: str,
    title: Any,
    url: Any,
    start_at: datetime | None,
    status: Any = "",
    uid: Any = "",
    context: Any = "",
    date_basis: str = "unknown",
    now: datetime,
) -> dict[str, Any] | None:
    item_url = _safe_url(str(url or ""), source)
    explicit_uid = _text(uid, 300)
    uid_url = _safe_url(explicit_uid)
    uid_provided = bool(explicit_uid)
    name = _text(title, 500)
    status_text = _text(status, 200)
    combined = f"{name} {status_text} {_text(context, 500)}"
    if start_at and not (datetime(1990, 1, 1, tzinfo=UTC) <= start_at <= now + timedelta(days=730)):
        start_at = None
    if _EVENT_CANCELLED_RE.search(combined):
        state = "cancelled"
    elif _EVENT_UNCONFIRMED_RE.search(combined):
        state = "postponed_or_tentative"
    elif start_at and start_at >= now:
        state = "upcoming"
    elif _EVENT_ON_DEMAND_RE.search(combined) or (
        start_at and now - start_at <= timedelta(days=180)
    ):
        state = "recent_or_on_demand"
    elif start_at:
        state = "historical"
    else:
        state = "unknown"
    identifier = (
        _canonical_surface_url(uid_url, uid_url)
        if uid_url
        else explicit_uid or item_url or _hash(name, start_at)
    )
    if not (name or item_url or uid):
        return None
    source_surface = _safe_url(source)
    scoreable_state = state in {"upcoming", "recent_or_on_demand", "historical"}
    return {
        "event_id": identifier,
        "event_id_is_url": bool(uid_url) or (not explicit_uid and bool(item_url)),
        "uid_provided": uid_provided,
        "canonical_identity": f"uid:{identifier}"
        if uid_provided
        else _canonical_surface_url(item_url, item_url)
        if item_url
        else identifier
        or (
            f"{_name(name)}:{start_at.isoformat() if start_at else ''}"
            if name or start_at
            else identifier
        ),
        "title": name,
        "url": item_url,
        "start_at": start_at,
        "date_basis": date_basis if start_at else "unknown",
        "state": state,
        "source_surface_urls": [source_surface] if source_surface else [],
        "date_source_surface_urls": [source_surface] if source_surface and start_at else [],
        "score_source_surface_urls": [source_surface]
        if source_surface and start_at and scoreable_state
        else [],
    }


def _event_ics_records(
    body: bytes,
    source: str,
    now: datetime,
) -> tuple[list[dict[str, Any]], str, bool]:
    if b"\x00" in body:
        return [], "calendar contains NUL bytes", False
    text = body.decode("utf-8", errors="replace")
    lines: list[str] = []
    for raw_line in text.splitlines()[:10_000]:
        if raw_line.startswith((" ", "\t")) and lines:
            lines[-1] += raw_line[1:]
        else:
            lines.append(raw_line)
    calendar_cancelled = any(
        re.match(r"(?i)^METHOD(?:;[^:]*)?:CANCEL\s*$", line.strip()) for line in lines
    )
    records: list[dict[str, Any]] = []
    current: dict[str, str] | None = None
    recurrence_seen = False
    for line in lines:
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            current = {}
            continue
        if upper == "END:VEVENT":
            if current is not None:
                record = _event_record(
                    source=source,
                    title=current.get("SUMMARY"),
                    url=current.get("URL"),
                    start_at=_ics_date(current.get("DTSTART", "")),
                    status="CANCELLED" if calendar_cancelled else current.get("STATUS"),
                    uid=current.get("UID"),
                    context=current.get("DESCRIPTION"),
                    date_basis="source_explicit",
                    now=now,
                )
                if record:
                    records.append(record)
            current = None
            if len(records) >= 50:
                break
            continue
        if current is None or ":" not in line:
            continue
        raw_key, value = line.split(":", 1)
        key = raw_key.split(";", 1)[0].upper()
        if key == "RRULE":
            recurrence_seen = True
            continue
        if key in {"SUMMARY", "URL", "DTSTART", "STATUS", "UID", "DESCRIPTION"}:
            current[key] = _text(value, 1_000)
    if "BEGIN:VCALENDAR" not in text.upper():
        return [], "invalid iCalendar envelope", recurrence_seen
    return records[:50], "", recurrence_seen


def _event_json_records(
    body: bytes,
    source: str,
    now: datetime,
) -> tuple[list[dict[str, Any]], str]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, RecursionError, TypeError) as exc:
        return [], f"invalid JSON event feed: {type(exc).__name__}"
    if isinstance(payload, Mapping):
        raw_items = payload.get("items") or payload.get("events") or payload.get("data") or []
        if isinstance(raw_items, Mapping):
            raw_items = list(raw_items.values())
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []
    if not isinstance(raw_items, list):
        return [], "JSON event items are not a list"
    records: list[dict[str, Any]] = []
    for item in raw_items[:50]:
        if not isinstance(item, Mapping):
            continue
        record = _event_record(
            source=source,
            title=item.get("title") or item.get("name"),
            url=item.get("url") or item.get("external_url"),
            start_at=_date(
                item.get("startDate") or item.get("start_date") or item.get("event_date")
            ),
            status=item.get("eventStatus") or item.get("status"),
            uid=item.get("id") or item.get("uid"),
            context=item.get("summary") or item.get("description"),
            date_basis="source_explicit",
            now=now,
        )
        if record:
            records.append(record)
    return records, ""


def _event_xml_records(
    body: bytes,
    source: str,
    now: datetime,
) -> tuple[list[dict[str, Any]], str]:
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", body, re.I):
        return [], "XML declarations with DOCTYPE or ENTITY are not accepted"
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, RecursionError) as exc:
        return [], f"invalid XML event feed: {type(exc).__name__}"
    entries = [node for node in root.iter() if _tag(node.tag) in {"event", "item", "entry"}]
    records: list[dict[str, Any]] = []
    for entry in entries[:50]:
        item_url = _safe_url(_element_text(entry, {"url", "loc", "link"}), source)
        if not item_url:
            for child in entry.iter():
                if _tag(child.tag) == "link" and child.attrib.get("href"):
                    item_url = _safe_url(str(child.attrib["href"]), source)
                    break
        record = _event_record(
            source=source,
            title=_element_text(entry, {"title", "name", "summary"}),
            url=item_url,
            start_at=_date(
                _element_text(
                    entry,
                    {
                        "startdate",
                        "start_date",
                        "dtstart",
                        "event_date",
                    },
                )
            ),
            status=_element_text(entry, {"eventstatus", "status"}),
            uid=_element_text(entry, {"uid", "id", "guid"}),
            context=_element_text(entry, {"description", "content"}),
            date_basis="feed_date",
            now=now,
        )
        if record:
            records.append(record)
    return records, ""


def _event_html_records(body: bytes, source: str, now: datetime) -> list[dict[str, Any]]:
    soup = parse_html(body.decode(errors="replace"), "html.parser")
    records: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}, limit=50):
        try:
            payload = json.loads(script.string or script.get_text(" ", strip=True))
        except (json.JSONDecodeError, RecursionError, TypeError):
            continue
        stack = [payload]
        visited = 0
        while stack and visited < 300 and len(records) < 50:
            current = stack.pop()
            visited += 1
            if isinstance(current, list):
                stack.extend(current[:100])
                continue
            if not isinstance(current, Mapping):
                continue
            stack.extend(
                value for value in list(current.values())[:100] if isinstance(value, (dict, list))
            )
            raw_types = current.get("@type", ())
            types = raw_types if isinstance(raw_types, list) else [raw_types]
            if not any(str(value).casefold().rsplit("/", 1)[-1] == "event" for value in types):
                continue
            record = _event_record(
                source=source,
                title=current.get("name"),
                url=current.get("url"),
                start_at=_date(current.get("startDate")),
                status=current.get("eventStatus"),
                uid=current.get("@id") or current.get("identifier"),
                context=(
                    f"{current.get('description') or ''} {current.get('eventAttendanceMode') or ''}"
                ),
                date_basis="source_explicit",
                now=now,
            )
            if record:
                records.append(record)
    nodes = list(soup.find_all("article", limit=50))
    if not nodes:
        nodes = [
            node
            for node in soup.find_all(["li", "section", "div"], limit=300)
            if node.find("time")
            and _EVENT_SURFACE_RE.search(node.get_text(" ", strip=True)[:1_000])
        ][:50]
    for node in nodes:
        node_context = _text(node.get_text(" ", strip=True), 2_000)
        link_context = " ".join(
            str(anchor.get("href") or "") for anchor in node.find_all("a", href=True, limit=20)
        )
        if not _EVENT_NODE_SEMANTICS_RE.search(f"{node_context} {link_context}"):
            continue
        heading = node.find(["h1", "h2", "h3", "h4"])
        anchor = heading.find("a", href=True) if isinstance(heading, Tag) else None
        anchor = anchor or node.find("a", href=True)
        record = _event_record(
            source=source,
            title=(
                heading.get_text(" ", strip=True)
                if isinstance(heading, Tag)
                else anchor.get_text(" ", strip=True)
                if anchor
                else ""
            ),
            url=str(anchor.get("href") or "") if anchor else source,
            start_at=_event_node_date(node, now),
            status=node.get_text(" ", strip=True),
            context=node.get_text(" ", strip=True),
            date_basis="source_explicit",
            now=now,
        )
        if record:
            records.append(record)
    return records[:50]


def _event_node_date(node: Tag | BeautifulSoup, now: datetime) -> datetime | None:
    for child in node.find_all(["time", "meta"], limit=30):
        if child.name == "time":
            value = child.get("datetime") or child.get_text(" ", strip=True)
        else:
            key = str(child.get("property") or child.get("name") or "").casefold()
            if key not in {
                "date",
                "startdate",
                "event:start_time",
                "event:startdate",
            }:
                continue
            value = child.get("content")
        parsed = _date(value)
        if parsed and datetime(1990, 1, 1, tzinfo=UTC) <= parsed <= now + timedelta(days=730):
            return parsed
    return None


def _dedupe_event_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in records:
        title = _text(raw.get("title"), 500)
        start_at = raw.get("start_at")
        start_at = start_at if isinstance(start_at, datetime) else None
        raw_url = str(raw.get("url") or "")
        canonical_url = _canonical_surface_url(raw_url, raw_url) if raw_url else ""
        raw_id = _text(raw.get("event_id"), 300)
        id_url = _safe_url(raw_id)
        event_id = _canonical_surface_url(id_url, id_url) if id_url else raw_id
        uid_provided = bool(raw.get("uid_provided")) and bool(event_id)
        sources = [
            source_url
            for source_url in (
                _safe_url(str(value)) for value in raw.get("source_surface_urls", [])
            )
            if source_url
        ]
        normalized.append(
            {
                "event_id": event_id or canonical_url or _hash(_name(title), start_at),
                "event_id_is_url": bool(id_url),
                "uid_provided": uid_provided,
                "canonical_identity": f"uid:{event_id}"
                if uid_provided
                else canonical_url or event_id
                if id_url
                else (
                    f"{_name(title)}:{start_at.isoformat() if start_at else ''}"
                    if title or start_at
                    else event_id
                ),
                "title": title,
                "normalized_title": _name(title),
                "url": canonical_url,
                "start_at": start_at,
                "date_basis": _text(raw.get("date_basis"), 30) or "unknown",
                "state": _text(raw.get("state"), 50) or "unknown",
                "source_surface_urls": sources,
                "date_source_surface_urls": [
                    source_url
                    for source_url in (
                        _safe_url(str(value)) for value in raw.get("date_source_surface_urls", [])
                    )
                    if source_url
                ],
                "score_source_surface_urls": [
                    source_url
                    for source_url in (
                        _safe_url(str(value)) for value in raw.get("score_source_surface_urls", [])
                    )
                    if source_url
                ],
            }
        )

    def same_event(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        same_title = bool(
            left.get("normalized_title")
            and left.get("normalized_title") == right.get("normalized_title")
        )
        left_date = left.get("start_at")
        right_date = right.get("start_at")
        same_time = bool(
            isinstance(left_date, datetime)
            and isinstance(right_date, datetime)
            and abs(left_date - right_date) <= timedelta(hours=36)
        )
        same_feed_id = bool(
            left.get("event_id")
            and left.get("event_id") == right.get("event_id")
            and left.get("uid_provided")
            and right.get("uid_provided")
        )
        same_item_url = bool(left.get("url") and left.get("url") == right.get("url") and same_title)
        return same_feed_id or same_item_url or (same_title and same_time)

    deduped: list[dict[str, Any]] = []
    for candidates in _connected_record_groups(normalized, same_event):
        dates = [
            item["start_at"] for item in candidates if isinstance(item.get("start_at"), datetime)
        ]
        conflicting_dates = bool(dates and max(dates) - min(dates) > timedelta(hours=36))
        merged_date = None if conflicting_dates else min(dates, default=None)
        merged_url = next(
            (str(item.get("url")) for item in candidates if item.get("url")),
            "",
        )
        merged_title = max(
            (str(item.get("title") or "") for item in candidates),
            key=len,
            default="",
        )
        merged_id = next(
            (str(item.get("event_id")) for item in candidates if item.get("event_id")),
            _hash(candidates),
        )
        explicit_uids = sorted(
            {
                str(item.get("event_id"))
                for item in candidates
                if item.get("uid_provided") and item.get("event_id")
            }
        )
        stable_uid = explicit_uids[0] if len(explicit_uids) == 1 else ""
        merged_state = (
            "cancelled"
            if any(item.get("state") == "cancelled" for item in candidates)
            else "postponed_or_tentative"
            if any(item.get("state") == "postponed_or_tentative" for item in candidates)
            else next(
                (
                    state
                    for state in (
                        "upcoming",
                        "recent_or_on_demand",
                        "historical",
                        "unknown",
                    )
                    if any(item.get("state") == state for item in candidates)
                ),
                "unknown",
            )
            if merged_date
            else "unknown"
        )
        deduped.append(
            {
                "event_id": merged_id,
                "uid_provided": bool(stable_uid),
                "canonical_identity": f"uid:{stable_uid}"
                if stable_uid
                else merged_url
                or (
                    f"{_name(merged_title)}:{merged_date.isoformat() if merged_date else ''}"
                    if merged_title or merged_date
                    else merged_id
                ),
                "title": merged_title,
                "url": merged_url,
                "start_at": merged_date,
                "date_basis": (
                    "source_explicit"
                    if merged_date
                    and any(
                        item.get("start_at") == merged_date
                        and item.get("date_basis") == "source_explicit"
                        for item in candidates
                    )
                    else "feed_date"
                    if merged_date
                    else "unknown"
                ),
                "state": merged_state,
                "source_surface_urls": list(
                    dict.fromkeys(
                        source_url
                        for item in candidates
                        for source_url in item.get("source_surface_urls", [])
                        if source_url
                    )
                ),
                "date_source_surface_urls": list(
                    dict.fromkeys(
                        source_url
                        for item in candidates
                        if merged_date is not None and item.get("start_at") == merged_date
                        for source_url in item.get("date_source_surface_urls", [])
                        if source_url
                    )
                ),
                "score_source_surface_urls": list(
                    dict.fromkeys(
                        source_url
                        for item in candidates
                        if merged_date is not None
                        and item.get("start_at") == merged_date
                        and item.get("state") == merged_state
                        and merged_state in {"upcoming", "recent_or_on_demand", "historical"}
                        for source_url in item.get("score_source_surface_urls", [])
                        if source_url
                    )
                ),
            }
        )
    return deduped[:50]


def parse_event_webinar_engine(
    body: str | bytes,
    url: str,
    *,
    company_key: str = "",
    first_party_source_url: str = "",
    officially_linked: bool = False,
    additional_surfaces: Sequence[FrontierDocument] = (),
    primary_document: FrontierDocument | None = None,
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    """Parse bounded Event JSON-LD, feeds, and calendars without recurrence expansion."""

    scanner = "event_webinar_engine_v1"
    now = _now(observed_at)
    company, documents, truncated = _surface_documents(
        scanner,
        body,
        url,
        company_key=company_key,
        first_party_source_url=first_party_source_url,
        officially_linked=officially_linked,
        additional_surfaces=additional_surfaces,
        document_kind="event_surface",
        surface_cap=5,
        primary_document=primary_document,
    )
    if not documents:
        return _empty(
            scanner,
            company,
            "Event surface lacks exact first-party linkage.",
            observed_at=now,
            status="identity_ambiguous",
        )
    records: list[dict[str, Any]] = []
    artifacts: list[EvidenceArtifact] = []
    errors: list[str] = []
    recurrence_seen = False
    successful = 0
    for document in documents:
        source = _safe_url(document.url)
        prefix = document.body.lstrip()[:100].lower()
        path = urlparse(source).path.casefold()
        if (
            path.endswith(".ics")
            or "text/calendar" in document.mime_type.casefold()
            or b"begin:vcalendar" in prefix
        ):
            parsed, error, recurrence = _event_ics_records(document.body, source, now)
            kind = "event_calendar"
            recurrence_seen = recurrence_seen or recurrence
        elif (
            path.endswith(".json")
            or prefix.startswith((b"{", b"["))
            or "json" in document.mime_type.casefold()
        ):
            parsed, error = _event_json_records(document.body, source, now)
            kind = "event_feed"
        elif (
            path.endswith((".xml", ".rss", ".atom"))
            or prefix.startswith((b"<?xml", b"<rss", b"<feed"))
            or "xml" in document.mime_type.casefold()
        ):
            parsed, error = _event_xml_records(document.body, source, now)
            kind = "event_feed"
        else:
            parsed = _event_html_records(document.body, source, now)
            error = ""
            kind = "event_surface"
        if error:
            errors.append(f"{source}: {error}")
            continue
        successful += 1
        records.extend(parsed)
        artifacts.append(
            _artifact(
                scanner,
                company,
                replace(document, kind=kind),
                now,
                excerpt=f"{len(parsed)} bounded event candidate(s)",
                truncated=truncated,
            )
        )
    records = _dedupe_event_records(records)
    eligible = [
        item
        for item in records
        if item["state"] in {"upcoming", "recent_or_on_demand", "historical"}
        and isinstance(item.get("start_at"), datetime)
        and item.get("score_source_surface_urls")
        and now - timedelta(days=365) <= item["start_at"] <= now + timedelta(days=180)
    ]
    dated_events = len(eligible)
    distinct_months = len({item["start_at"].strftime("%Y-%m") for item in eligible})
    upcoming = [
        item
        for item in eligible
        if timedelta(days=3) <= item["start_at"] - now <= timedelta(days=45)
    ]
    recent = [
        item
        for item in eligible
        if timedelta(0) <= now - item["start_at"] <= timedelta(days=180)
        or item["state"] == "recent_or_on_demand"
    ]
    qualifying = dated_events >= 3 and distinct_months >= 2
    latest_or_next_item = max(eligible, key=lambda item: item["start_at"], default=None)
    latest_or_next = latest_or_next_item["start_at"] if latest_or_next_item else None
    source = (
        str((latest_or_next_item or {}).get("score_source_surface_urls", [""])[0])
        if (latest_or_next_item or {}).get("score_source_surface_urls")
        else artifacts[0].canonical_url
        if artifacts
        else _safe_url(documents[0].url)
    )
    # Positive counts cite only contributing dated records. A zero eligible
    # count instead describes the exact successfully parsed surfaces, including
    # empty listings and pages containing only cancelled/undated records.
    aggregate_artifact_keys = _artifact_keys_for_sources(
        artifacts,
        (
            source_url
            for item in eligible
            for source_url in item.get("score_source_surface_urls", [])
        )
        if eligible
        else (artifact.canonical_url for artifact in artifacts),
    )
    aggregate = {
        "dated_events": dated_events,
        "distinct_months": distinct_months,
        "upcoming_events": len(upcoming),
        "recent_events": len(recent),
    }
    facts: list[_Fact] = [
        _Fact(
            "events.program_engine",
            aggregate,
            "json",
            f"event-program:{company}",
            (
                f"The bounded public event sample contains {dated_events} distinct dated "
                f"event(s) across {distinct_months} month(s)."
            ),
            source,
            {
                "score_policy": "recurring_event_program_v1",
                "projection_score_gate": qualifying,
                "minimum_distinct_events": 3,
                "minimum_distinct_months": 2,
                "item_cap": 50,
            },
            confidence="high" if _same_company(source, company) else "medium",
            effective_at=latest_or_next,
            effective_time_basis=(
                str(latest_or_next_item.get("date_basis"))  # type: ignore[arg-type]
                if latest_or_next_item
                else "unknown"
            ),
            score_eligible=qualifying,
            discovery_only=not qualifying,
            source_actor_key=f"domain:{company}",
            causal_root_key=f"owned-programming:{company}",
            artifact_keys=aggregate_artifact_keys,
        )
    ] if successful else []
    for item in upcoming:
        item_sources = tuple(str(value) for value in item.get("score_source_surface_urls", []))
        item_source = item_sources[0] if item_sources else source
        event_identity = str(item["canonical_identity"] or item["event_id"])
        event_root = f"event:{_hash(event_identity)}"
        facts.append(
            _Fact(
                "events.upcoming",
                {
                    "title": item["title"],
                    "url": item["url"] or item_source,
                    "state": item["state"],
                    "start_at": item["start_at"].isoformat(),
                },
                "json",
                f"event-upcoming:{_hash(event_identity)[:24]}",
                (
                    "A non-cancelled public event is explicitly dated 3–45 days away: "
                    f"{item['title']}."
                ),
                item_source,
                {
                    "score_policy": "upcoming_confirmed_event_v1",
                    "projection_score_gate": True,
                    "window_days": [3, 45],
                },
                effective_at=item["start_at"],
                effective_time_basis=str(item["date_basis"]),  # type: ignore[arg-type]
                score_eligible=True,
                source_actor_key=f"domain:{company}",
                causal_root_key=event_root,
                artifact_keys=_artifact_keys_for_sources(artifacts, item_sources),
            )
        )
    positive_upcoming_identities = {
        str(item["canonical_identity"] or item["event_id"]) for item in upcoming
    }
    for item in records:
        event_identity = str(item["canonical_identity"] or item["event_id"])
        if event_identity in positive_upcoming_identities:
            continue
        start_at = item.get("start_at")
        state = str(item.get("state") or "unknown")
        if not isinstance(start_at, datetime) and state not in {
            "cancelled",
            "postponed_or_tentative",
        }:
            continue
        item_sources = tuple(str(value) for value in item.get("source_surface_urls", []))
        item_source = item_sources[0] if item_sources else source
        facts.append(
            _Fact(
                "events.upcoming",
                {
                    "title": item["title"],
                    "url": item["url"] or item_source,
                    "state": state,
                    "start_at": start_at.isoformat() if isinstance(start_at, datetime) else "",
                },
                "json",
                f"event-upcoming:{_hash(event_identity)[:24]}",
                (
                    "The latest bounded event record no longer satisfies the confirmed "
                    "three-to-45-day upcoming window."
                ),
                item_source,
                {
                    "score_policy": "upcoming_confirmed_event_v1",
                    "projection_score_gate": False,
                    "window_days": [3, 45],
                },
                effective_at=start_at if isinstance(start_at, datetime) else None,
                effective_time_basis=str(item.get("date_basis") or "unknown"),
                score_eligible=False,
                discovery_only=True,
                source_actor_key=f"domain:{company}",
                causal_root_key=f"event:{_hash(event_identity)}",
                artifact_keys=_artifact_keys_for_sources(artifacts, item_sources),
            )
        )
    for item in records:
        item_sources = tuple(str(value) for value in item.get("source_surface_urls", []))
        item_source = item_sources[0] if item_sources else source
        event_identity = str(item["canonical_identity"] or item["event_id"])
        facts.append(
            _Fact(
                "events.public_event",
                {
                    "title": item["title"],
                    "url": item["url"] or item_source,
                    "state": item["state"],
                },
                "json",
                f"event-item:{_hash(event_identity)[:24]}",
                (
                    f"Bounded public event listing: {item['title'] or item['event_id']} "
                    f"({item['state']})."
                ),
                item_source,
                effective_at=item.get("start_at"),
                effective_time_basis=str(item.get("date_basis") or "unknown"),  # type: ignore[arg-type]
                discovery_only=True,
                source_actor_key=f"domain:{company}",
                causal_root_key=f"event:{_hash(event_identity)}",
                artifact_keys=_artifact_keys_for_sources(artifacts, item_sources),
            )
        )
    completeness: Literal["partial", "complete"] = "partial" if truncated or errors else "complete"
    limitations = [
        "At most five surfaces and fifty events are parsed; calendar recurrence is never expanded.",
        "Counts describe successfully parsed surfaces only; zero does not establish "
        "site-wide absence.",
        "Registration does not establish attendance, demand, pipeline, success, or spend.",
        "Cancelled, undated, stale, and far-future events cannot satisfy score gates.",
    ]
    if recurrence_seen:
        limitations.append("An RRULE was observed but deliberately not expanded.")
    return _result(
        scanner,
        company,
        facts=facts,
        artifacts=artifacts,
        observed_at=now,
        status=(
            "failed" if not successful
            else "success_empty" if not records and not errors and not truncated
            else None
        ),
        surface="first_party_linked_event_program",
        scope=source,
        attempted=len(documents),
        successful=successful,
        failed=len(errors),
        total=len(documents),
        completeness="none" if not successful else completeness,
        truncated=truncated,
        absence_interpretable=bool(successful and not truncated and not errors),
        limitations=limitations,
        errors=errors,
        features={
            "event_webinar_engine": {
                **aggregate,
                "sampled_events": len(records),
                "state_counts": {
                    state: sum(item["state"] == state for item in records)
                    for state in (
                        "upcoming",
                        "recent_or_on_demand",
                        "cancelled",
                        "postponed_or_tentative",
                        "historical",
                        "unknown",
                    )
                },
                "recurrence_observed_not_expanded": recurrence_seen,
                "source_urls": [item.canonical_url for item in artifacts],
            }
        },
        applicable_reason="First-party-linked event surfaces were supplied.",
    )


_TRUST_CONTROL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("soc_assurance", re.compile(r"\bSOC\s*(?:1|2|3)(?:\s+Type\s+(?:I|II|1|2))?\b", re.I)),
    ("iso_27001", re.compile(r"\bISO(?:/IEC)?\s*27001\b", re.I)),
    ("identity_access", re.compile(r"\b(?:SSO|SCIM|SAML|single sign-on)\b", re.I)),
    ("encryption", re.compile(r"\bencrypt(?:ion|ed)(?:\s+at\s+rest|\s+in\s+transit)?\b", re.I)),
    (
        "availability_resilience",
        re.compile(
            r"\b(?:availability|uptime|service level agreement|SLA|business continuity|"
            r"disaster recovery)\b",
            re.I,
        ),
    ),
    (
        "security_contact_policy",
        re.compile(
            r"\b(?:security contact|vulnerability disclosure|responsible disclosure|"
            r"security\.txt)\b",
            re.I,
        ),
    ),
    (
        "assurance_documents",
        re.compile(
            r"\b(?:audit report|assurance report|security questionnaire|CAIQ|"
            r"certificate available|request (?:a )?report)\b",
            re.I,
        ),
    ),
)
_EXPLICIT_TRUST_CENTER_RE = re.compile(
    r"\b(?:trust center|security center|compliance center|assurance center|"
    r"security and compliance|security (?:&|and) trust)\b",
    re.I,
)
_TRUST_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|without|unsupported|unavailable|cannot|can't|do not|don't|"
    r"does not|doesn't|isn't|aren't|wasn't|weren't|won't|lacks?|not yet|"
    r"not currently)\b",
    re.I,
)


def _affirmative_trust_marker(text: str, pattern: re.Pattern[str]) -> bool:
    for clause in re.split(r"(?<=[.!?;])\s+|[\r\n]+", text):
        for match in pattern.finditer(clause):
            local = clause[max(0, match.start() - 100) : min(len(clause), match.end() + 60)]
            if not _TRUST_NEGATION_RE.search(local):
                return True
    return False


def parse_trust_center_readiness(
    body: str | bytes,
    url: str,
    *,
    company_key: str = "",
    first_party_source_url: str = "",
    officially_linked: bool = False,
    additional_surfaces: Sequence[FrontierDocument] = (),
    primary_document: FrontierDocument | None = None,
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    scanner = "trust_center_readiness_v1"
    now = _now(observed_at)
    company, documents, truncated = _surface_documents(
        scanner,
        body,
        url,
        company_key=company_key,
        first_party_source_url=first_party_source_url,
        officially_linked=officially_linked,
        additional_surfaces=additional_surfaces,
        document_kind="trust_center",
        surface_cap=3,
        primary_document=primary_document,
    )
    if not documents:
        return _empty(
            scanner,
            company,
            "Trust surface lacks exact first-party linkage.",
            observed_at=now,
            status="identity_ambiguous",
        )
    observed_families: set[str] = set()
    document_evidence: list[dict[str, Any]] = []
    artifacts: list[EvidenceArtifact] = []
    for document in documents:
        source = _safe_url(document.url)
        path = urlparse(source).path.casefold()
        security_txt = path.endswith("security.txt")
        document_families: set[str] = set()
        document_explicit_center = False
        if security_txt:
            visible = document.body.decode(errors="replace")[:100_000]
            if re.search(r"(?im)^(?:contact|policy):", visible):
                document_families.add("security_contact_policy")
            kind = "security_txt"
        else:
            soup = parse_html(document.body.decode(errors="replace"), "html.parser")
            visible = _text((soup.find("main") or soup).get_text(" ", strip=True), 100_000)
            heading = _text(
                " ".join(
                    node.get_text(" ", strip=True)
                    for node in soup.find_all(["title", "h1", "h2"], limit=20)
                ),
                3_000,
            )
            document_explicit_center = bool(
                _EXPLICIT_TRUST_CENTER_RE.search(heading)
                or re.search(
                    r"(?:^|/)(?:trust|trust-center|security-center|compliance-center|"
                    r"assurance|assurance-center|privacy-security)(?:/|$)",
                    path,
                    re.I,
                )
            )
            kind = "trust_center"
        for family, pattern in _TRUST_CONTROL_PATTERNS:
            if _affirmative_trust_marker(visible, pattern):
                document_families.add(family)
        observed_families.update(document_families)
        artifact = _artifact(
            scanner,
            company,
            replace(document, kind=kind),
            now,
            excerpt=f"{len(document_families)} normalized public control family/families",
            truncated=truncated,
        )
        artifacts.append(artifact)
        document_evidence.append(
            {
                "source": source,
                "explicit_center": document_explicit_center,
                "families": sorted(document_families),
                "artifact_key": artifact.artifact_key,
            }
        )
    qualifying_record = max(
        (
            item
            for item in document_evidence
            if item["explicit_center"] and len(item["families"]) >= 3
        ),
        key=lambda item: len(item["families"]),
        default=None,
    )
    representative = qualifying_record or max(
        (item for item in document_evidence if item["explicit_center"]),
        key=lambda item: len(item["families"]),
        default=max(document_evidence, key=lambda item: len(item["families"])),
    )
    qualifying = qualifying_record is not None
    source = str(representative["source"])
    families = tuple(str(item) for item in representative["families"])
    explicit_center = bool(representative["explicit_center"])
    value = {
        "explicit_trust_center": explicit_center,
        "control_families": list(families),
        "control_family_count": len(families),
        "observed_control_families_all_surfaces": sorted(observed_families),
    }
    fact = _Fact(
        "enterprise.trust_readiness",
        value,
        "json",
        f"enterprise-trust:{company}",
        (
            f"The public trust surface states {len(families)} normalized enterprise control "
            "family/families."
        ),
        source,
        {
            "score_policy": "enterprise_trust_surface_v1",
            "projection_score_gate": qualifying,
            "minimum_control_families": 3,
            "explicit_trust_center": explicit_center,
        },
        confidence="high" if _same_company(source, company) else "medium",
        score_eligible=qualifying,
        discovery_only=not qualifying,
        source_actor_key=f"domain:{company}",
        causal_root_key=f"enterprise-trust:{company}",
        artifact_keys=(str(representative["artifact_key"]),),
    )
    return _result(
        scanner,
        company,
        facts=[fact],
        artifacts=artifacts,
        observed_at=now,
        surface="first_party_linked_public_trust_center",
        scope=source,
        attempted=len(documents),
        successful=len(documents),
        total=len(documents),
        completeness="partial" if truncated else "complete",
        truncated=truncated,
        absence_interpretable=not truncated,
        limitations=(
            "Public self-assertions do not validate certifications, security quality, "
            "customers, company size, revenue, or budget.",
            "Certificates, gated audit reports, forms, controls, ports, and private systems "
            "are not fetched or tested.",
            "DPA and subprocessor vendor extraction remains owned by vendor intelligence.",
        ),
        features={"trust_center_readiness": value},
        applicable_reason="First-party-linked public trust surfaces were supplied.",
    )


_BRAND_ASSET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("logos", re.compile(r"\b(?:logo|wordmark|logomark)\b", re.I)),
    ("photography", re.compile(r"\b(?:photo|photography|image library)\b", re.I)),
    ("motion_video", re.compile(r"\b(?:motion|animation|video assets?)\b", re.I)),
    ("social_campaign", re.compile(r"\b(?:social media|campaign assets?|ad templates?)\b", re.I)),
    ("templates", re.compile(r"\b(?:templates?|presentation|slide deck)\b", re.I)),
    (
        "typography_color",
        re.compile(r"\b(?:typography|typeface|fonts?|color palette|colours?)\b", re.I),
    ),
)
_BRAND_RULE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("clear_space", re.compile(r"\bclear space\b", re.I)),
    ("minimum_size", re.compile(r"\bminimum (?:display )?size\b", re.I)),
    (
        "modification_limits",
        re.compile(r"\b(?:do not|don't) (?:alter|modify|distort|rotate|recolor)\b", re.I),
    ),
    (
        "trademark_usage",
        re.compile(r"\b(?:trademark|brand usage|usage guidelines?|terms of use)\b", re.I),
    ),
    ("approval_or_attribution", re.compile(r"\b(?:approval|attribution|credit line)\b", re.I)),
)


def parse_brand_asset_governance(
    body: str | bytes,
    url: str,
    *,
    company_key: str = "",
    first_party_source_url: str = "",
    officially_linked: bool = False,
    additional_surfaces: Sequence[FrontierDocument] = (),
    primary_document: FrontierDocument | None = None,
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    scanner = "brand_asset_governance_v1"
    now = _now(observed_at)
    company, documents, truncated = _surface_documents(
        scanner,
        body,
        url,
        company_key=company_key,
        first_party_source_url=first_party_source_url,
        officially_linked=officially_linked,
        additional_surfaces=additional_surfaces,
        document_kind="brand_asset_portal",
        surface_cap=3,
        primary_document=primary_document,
    )
    if not documents:
        return _empty(
            scanner,
            company,
            "Brand surface lacks exact first-party linkage.",
            observed_at=now,
            status="identity_ambiguous",
        )
    categories: set[str] = set()
    rule_families: set[str] = set()
    governed_portal = False
    artifacts: list[EvidenceArtifact] = []
    for document in documents:
        soup = parse_html(document.body.decode(errors="replace"), "html.parser")
        visible = _text((soup.find("main") or soup).get_text(" ", strip=True), 100_000)
        link_context = " ".join(
            f"{anchor.get_text(' ', strip=True)} {urlparse(str(anchor.get('href') or '')).path}"
            for anchor in soup.find_all("a", href=True, limit=500)
        )
        combined = f"{visible} {_text(link_context, 50_000)}"
        governed_portal = governed_portal or bool(
            _BRAND_SURFACE_RE.search(
                f"{urlparse(document.url).path} "
                + " ".join(
                    node.get_text(" ", strip=True)
                    for node in soup.find_all(["title", "h1", "h2"], limit=20)
                )
            )
        )
        categories.update(
            name for name, pattern in _BRAND_ASSET_PATTERNS if pattern.search(combined)
        )
        rule_families.update(
            name for name, pattern in _BRAND_RULE_PATTERNS if pattern.search(visible)
        )
        artifacts.append(
            _artifact(
                scanner,
                company,
                replace(document, kind="brand_asset_portal"),
                now,
                excerpt=f"{len(categories)} public asset category/categories",
                truncated=truncated,
            )
        )
    source = _safe_url(documents[0].url)
    value = {
        "governed_asset_portal": governed_portal,
        "asset_categories": sorted(categories),
        "usage_rule_families": sorted(rule_families),
    }
    fact = _Fact(
        "brand.asset_governance",
        value,
        "json",
        f"brand-assets:{company}",
        f"The public brand surface exposes {len(categories)} normalized asset category/categories.",
        source,
        {"projection_score_gate": False, "context_only": True, "binary_assets_not_fetched": True},
        confidence="high" if _same_company(source, company) else "medium",
        score_eligible=False,
        discovery_only=True,
        source_actor_key=f"domain:{company}",
        causal_root_key=f"creative-infrastructure:{company}",
    )
    return _result(
        scanner,
        company,
        facts=[fact],
        artifacts=artifacts,
        observed_at=now,
        surface="first_party_linked_brand_asset_governance",
        scope=source,
        attempted=len(documents),
        successful=len(documents),
        total=len(documents),
        completeness="partial" if truncated else "complete",
        truncated=truncated,
        absence_interpretable=not truncated,
        limitations=(
            "Binary logos, fonts, photos, videos, templates, archives, and brand books are "
            "never downloaded or redistributed.",
            "A public brand kit does not establish cadence, spend, team size, or a production gap.",
        ),
        features={"brand_asset_governance": value},
        applicable_reason="First-party-linked public brand surfaces were supplied.",
    )


_MEASUREMENT_CALL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "gtag",
        re.compile(
            r"\bgtag\s*\(\s*(['\"])event\1\s*,\s*(['\"])([A-Za-z][A-Za-z0-9 _.-]{0,79})\2", re.I
        ),
    ),
    (
        "data_layer",
        re.compile(
            r"\bdataLayer\.push\s*\(\s*\{[^{}]{0,500}?['\"]?event['\"]?\s*:\s*"
            r"(['\"])([A-Za-z][A-Za-z0-9 _.-]{0,79})\1",
            re.I,
        ),
    ),
    (
        "meta",
        re.compile(
            r"\bfbq\s*\(\s*(['\"])(?:track|trackCustom)\1\s*,\s*"
            r"(['\"])([A-Za-z][A-Za-z0-9 _.-]{0,79})\2",
            re.I,
        ),
    ),
    (
        "segment",
        re.compile(r"\banalytics\.track\s*\(\s*(['\"])([A-Za-z][A-Za-z0-9 _.-]{0,79})\1", re.I),
    ),
    (
        "rudderstack",
        re.compile(
            r"\b(?:window\.)?rudderanalytics\.track\s*\(\s*"
            r"(['\"])([A-Za-z][A-Za-z0-9 _.-]{0,79})\1",
            re.I,
        ),
    ),
)
_MEASUREMENT_EVENT_FAMILIES: dict[str, str] = {
    "page_view": "awareness",
    "view_content": "consideration",
    "view_item": "consideration",
    "product_viewed": "consideration",
    "search": "consideration",
    "add_to_cart": "checkout",
    "product_added": "checkout",
    "view_cart": "checkout",
    "begin_checkout": "checkout",
    "checkout_started": "checkout",
    "purchase": "conversion",
    "order_completed": "conversion",
    "generate_lead": "conversion",
    "lead": "conversion",
    "sign_up": "conversion",
    "subscribe": "conversion",
    "contact_sales": "conversion",
    "request_demo": "conversion",
    "demo_requested": "conversion",
    "video_start": "media",
    "video_progress": "media",
    "video_complete": "media",
    "media_view": "media",
    "image_view": "media",
    "experiment_view": "experiment",
    "experiment_impression": "experiment",
    "variant_view": "experiment",
}
_EXECUTABLE_INLINE_SCRIPT_TYPES = {
    "",
    "module",
    "application/ecmascript",
    "application/javascript",
    "text/ecmascript",
    "text/javascript",
    "text/jscript",
}


def _normalized_measurement_event(value: str) -> str:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value.strip())
    return re.sub(r"[^a-z0-9]+", "_", spaced.casefold()).strip("_")


def _javascript_code_positions(code: str) -> list[bool]:
    """Mark executable lexical positions without evaluating or fully parsing JavaScript."""

    positions = [False] * len(code)
    state = "code"
    escaped = False
    index = 0
    while index < len(code):
        char = code[index]
        following = code[index + 1] if index + 1 < len(code) else ""
        if state == "code":
            if char == "/" and following == "/":
                state = "line_comment"
                index += 2
                continue
            if char == "/" and following == "*":
                state = "block_comment"
                index += 2
                continue
            positions[index] = True
            if char == "'":
                state = "single_quote"
            elif char == '"':
                state = "double_quote"
            elif char == "`":
                state = "template"
            index += 1
            continue
        if state == "line_comment":
            if char in "\r\n":
                state = "code"
                positions[index] = True
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and following == "/":
                index += 2
                state = "code"
            else:
                index += 1
            continue
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif (
            (state == "single_quote" and char == "'")
            or (state == "double_quote" and char == '"')
            or (state == "template" and char == "`")
        ):
            state = "code"
        index += 1
    return positions


def parse_growth_measurement_schema(
    html: str | bytes,
    url: str,
    *,
    company_key: str = "",
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    scanner = "growth_measurement_schema_v1"
    now = _now(observed_at)
    source = _safe_url(url)
    company = _host(company_key) or _host(source)
    if not source or not _same_company(source, company):
        return _empty(
            scanner,
            company,
            "Saved measurement HTML identity is ambiguous.",
            observed_at=now,
            status="identity_ambiguous",
        )
    raw = html if isinstance(html, bytes) else html.encode()
    bounded = raw[:_MAX_DOCUMENT_BYTES]
    truncated = len(raw) > len(bounded)
    soup = parse_html(bounded.decode(errors="replace"), "html.parser")
    calls: list[tuple[str, str]] = []
    for script in soup.find_all("script", limit=500):
        if script.get("src"):
            continue
        script_type = str(script.get("type") or "").split(";", 1)[0].strip().casefold()
        if script_type not in _EXECUTABLE_INLINE_SCRIPT_TYPES:
            continue
        code = (script.string or script.get_text(" ", strip=False))[:100_000]
        code_positions = _javascript_code_positions(code)
        for provider, pattern in _MEASUREMENT_CALL_PATTERNS:
            for match in pattern.finditer(code):
                if not code_positions[match.start()]:
                    continue
                candidate = _normalized_measurement_event(match.group(match.lastindex or 1))
                if candidate in _MEASUREMENT_EVENT_FAMILIES:
                    calls.append((provider, candidate))
                    if len(calls) >= 100:
                        break
            if len(calls) >= 100:
                break
        if len(calls) >= 100:
            break
    event_names = sorted({event for _, event in calls})
    families = sorted({_MEASUREMENT_EVENT_FAMILIES[event] for event in event_names})
    has_media_or_experiment = bool({"media", "experiment"} & set(families))
    has_conversion = "conversion" in families
    qualifying = (
        len(calls) >= 4 and len(families) >= 2 and has_media_or_experiment and has_conversion
    )
    document = FrontierDocument(
        source,
        bounded,
        "first_party_measurement_html",
        mime_type="text/html",
        publisher=company,
    )
    artifact = _artifact(
        scanner,
        company,
        document,
        now,
        excerpt=f"{len(calls)} allowlisted static measurement call(s)",
        truncated=truncated,
    )
    value = {
        "explicit_event_calls": len(calls),
        "funnel_families": families,
        "has_media_or_experiment": has_media_or_experiment,
        "has_conversion": has_conversion,
    }
    facts: list[_Fact] = [
        _Fact(
            "measurement.public_funnel_schema",
            value,
            "json",
            f"measurement-schema:{company}",
            f"Saved first-party HTML contains {len(calls)} allowlisted static event call(s).",
            source,
            {
                "score_policy": "public_growth_schema_v1",
                "projection_score_gate": qualifying,
                "minimum_explicit_calls": 4,
                "minimum_funnel_families": 2,
            },
            confidence="medium",
            confidence_method="bounded_static_call_shape_allowlist",
            score_eligible=qualifying,
            discovery_only=not qualifying,
            source_actor_key=f"domain:{company}",
            causal_root_key=f"distribution:first_party:{company}",
        )
    ]
    return _result(
        scanner,
        company,
        facts=facts,
        artifacts=[artifact],
        observed_at=now,
        surface="saved_first_party_measurement_schema",
        scope=source,
        attempted=1,
        successful=1,
        total=1,
        completeness="partial" if truncated else "complete",
        truncated=truncated,
        absence_interpretable=not truncated,
        limitations=(
            "JavaScript is never executed and linked scripts are never fetched.",
            "Only allowlisted normalized event names and families are retained; IDs, "
            "parameters, arbitrary values, and script bodies are not stored.",
            "Public code does not establish fired events, active experiments or ads, "
            "conversions, performance, or spend.",
        ),
        features={
            "growth_measurement_schema": {
                **value,
                "event_names": event_names,
                "provider_counts": {
                    provider: sum(item_provider == provider for item_provider, _ in calls)
                    for provider in sorted({provider for provider, _ in calls})
                },
            }
        },
        applicable_reason="Saved first-party HTML was available.",
    )


_SEO_COMMERCIAL_SURFACE_RE = re.compile(
    r"\b(?:pricing|products?|collections?|shop|store|services?|solutions?|case[- ]stud(?:y|ies)|"
    r"customers?)\b",
    re.I,
)


def parse_seo_visibility_engine(
    html: str | bytes,
    url: str,
    *,
    company_key: str = "",
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    """Project bounded page-level SEO readiness without claiming measured visibility.

    This is deliberately a saved-HTML audit, not a rank tracker. It records literal page
    structure and admits only one weak company-level ability path downstream. Missing tags or a
    client-rendered shell never become negative buyer evidence.
    """

    scanner = "seo_visibility_engine_v1"
    now = _now(observed_at)
    source = _safe_url(url)
    company = _host(company_key) or _host(source)
    if not source or not _same_company(source, company):
        return _empty(
            scanner,
            company,
            "Saved SEO HTML identity is ambiguous.",
            observed_at=now,
            status="identity_ambiguous",
        )

    raw = html if isinstance(html, bytes) else html.encode()
    bounded = raw[:_MAX_DOCUMENT_BYTES]
    truncated = len(raw) > len(bounded)
    soup = parse_html(bounded.decode(errors="replace"), "html.parser")

    title = _text(soup.title.get_text(" ", strip=True) if soup.title else "", 300)
    description_node = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    description = _text(description_node.get("content") if description_node else "", 500)
    canonical_node = soup.find(
        "link",
        rel=lambda value: bool(
            value
            and "canonical"
            in {str(item).casefold() for item in (value if isinstance(value, list) else [value])}
        ),
    )
    canonical_url = (
        _safe_url(str(canonical_node.get("href") or ""), source) if canonical_node else ""
    )
    canonical_same_company = bool(canonical_url and _same_company(canonical_url, company))

    robots_directives: set[str] = set()
    for node in soup.find_all("meta", limit=100):
        name = str(node.get("name") or "").strip().casefold()
        if name not in {"robots", "googlebot"}:
            continue
        robots_directives.update(
            token
            for raw_token in str(node.get("content") or "").casefold().split(",")
            if (token := raw_token.strip())
        )
    noindex = "noindex" in robots_directives or "none" in robots_directives

    h1_count = len(soup.find_all("h1", limit=10))
    h2_count = len(soup.find_all("h2", limit=100))
    structured_types: set[str] = set()
    structured_blocks = 0
    for script in soup.find_all(
        "script",
        attrs={"type": re.compile(r"ld\+json", re.I)},
        limit=100,
    ):
        try:
            payload = json.loads(script.string or script.get_text(" ", strip=True))
        except (json.JSONDecodeError, RecursionError, TypeError):
            continue
        structured_blocks += 1
        stack = [payload]
        visited = 0
        while stack and visited < 500:
            current = stack.pop()
            visited += 1
            if isinstance(current, list):
                stack.extend(current[:100])
                continue
            if not isinstance(current, Mapping):
                continue
            raw_types = current.get("@type", ())
            type_values = [raw_types] if isinstance(raw_types, str) else raw_types
            if isinstance(type_values, list):
                structured_types.update(
                    _text(value, 80).rsplit("/", 1)[-1] for value in type_values if _text(value, 80)
                )
            stack.extend(
                child for child in list(current.values())[:100] if isinstance(child, (dict, list))
            )

    internal_urls: set[str] = set()
    for anchor in soup.find_all("a", href=True, limit=1_000):
        candidate = _safe_url(str(anchor.get("href") or ""), source)
        if candidate and _same_company(candidate, company):
            internal_urls.add(candidate)

    visible_soup = deepcopy(soup)
    for node in visible_soup(["script", "style", "noscript", "svg", "template"]):
        node.decompose()
    visible = _text(visible_soup.get_text(" ", strip=True), 200_000)
    word_count = len(re.findall(r"\b[\w'-]+\b", visible))
    page_context = _text(
        " ".join(
            [
                urlparse(source).path.replace("-", " "),
                title,
                *(node.get_text(" ", strip=True) for node in soup.find_all(["h1", "h2"], limit=20)),
            ]
        ),
        5_000,
    )
    commercial_surface = bool(_SEO_COMMERCIAL_SURFACE_RE.search(page_context))

    title_valid = 10 <= len(title) <= 70
    description_valid = 50 <= len(description) <= 180
    exact_h1 = h1_count == 1
    readiness_score = min(
        100,
        (15 if not noindex else 0)
        + (12 if title_valid else 0)
        + (10 if description_valid else 0)
        + (12 if canonical_same_company else 0)
        + (10 if exact_h1 else 0)
        + (8 if structured_blocks else 0)
        + (15 if word_count >= 300 else 8 if word_count >= 150 else 0)
        + (12 if len(internal_urls) >= 8 else 6 if len(internal_urls) >= 3 else 0)
        + (6 if commercial_surface else 0),
    )
    qualifying = bool(
        not truncated
        and not noindex
        and title_valid
        and canonical_same_company
        and exact_h1
        and word_count >= 250
        and len(internal_urls) >= 5
        and readiness_score >= 75
    )
    value = {
        "source_url": source,
        "readiness_score": readiness_score,
        "meta_allows_indexing": not noindex,
        "robots_directives": sorted(robots_directives),
        "title_present": bool(title),
        "title_length": len(title),
        "description_present": bool(description),
        "description_length": len(description),
        "canonical_present": bool(canonical_url),
        "canonical_same_company": canonical_same_company,
        "h1_count": h1_count,
        "h2_count": h2_count,
        "structured_data_blocks": structured_blocks,
        "structured_data_types": sorted(structured_types)[:30],
        "visible_word_count": word_count,
        "same_company_link_count": len(internal_urls),
        "commercial_surface": commercial_surface,
        "qualifies_as_ability_proxy": qualifying,
        "sample_truncated": truncated,
    }
    summary = (
        f"The bounded page sample has {readiness_score}/100 search-readiness structure, "
        f"{word_count} visible words, and {len(internal_urls)} same-company links."
    )
    document = FrontierDocument(
        source,
        bounded,
        "first_party_seo_html",
        mime_type="text/html",
        publisher=company,
    )
    artifact = _artifact(
        scanner,
        company,
        document,
        now,
        excerpt=summary,
        truncated=truncated,
    )
    fact = _Fact(
        "seo.page_readiness",
        value,
        "json",
        f"seo-page:{source}",
        summary + " This is page structure, not measured search ranking, traffic, or buyer intent.",
        source,
        {
            "score_policy": "seo_operating_footprint_v1",
            "projection_score_gate": qualifying,
            "minimum_readiness_score": 75,
            "minimum_visible_words": 250,
            "minimum_same_company_links": 5,
            "rank_measured": False,
            "traffic_measured": False,
        },
        confidence="medium",
        confidence_method="bounded_static_seo_structure",
        score_eligible=qualifying,
        discovery_only=not qualifying,
        source_actor_key=f"domain:{company}",
        causal_root_key=f"seo-operating-footprint:{company}",
    )
    return _result(
        scanner,
        company,
        facts=[fact],
        artifacts=[artifact],
        observed_at=now,
        surface="saved_first_party_seo_html",
        scope=source,
        attempted=1,
        successful=1,
        total=1,
        completeness="partial" if truncated else "complete",
        truncated=truncated,
        absence_interpretable=not truncated,
        limitations=(
            "Only the bounded saved HTML page is assessed; robots.txt, rendered JavaScript, "
            "search results, traffic, backlinks, and off-site authority are not measured.",
            "Page readiness does not establish ranking, demand, revenue, budget, or "
            "purchase intent.",
            "Missing structure or a client-rendered shell is unknown buyer evidence, never "
            "a penalty.",
        ),
        features={"seo_visibility_engine": {"pages": [value]}},
        applicable_reason="Saved first-party HTML was available.",
    )


_DESIGN_MEDIA_FAMILIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hero_banner", re.compile(r"\b(?:hero|banner)\b", re.I)),
    ("image_gallery", re.compile(r"\b(?:image|gallery|carousel)\b", re.I)),
    ("video_media", re.compile(r"\b(?:video|media player)\b", re.I)),
    ("social_campaign", re.compile(r"\b(?:social|campaign|promotion|promo)\b", re.I)),
    ("testimonial_proof", re.compile(r"\b(?:testimonial|customer logo|case study)\b", re.I)),
    ("pricing_cta", re.compile(r"\b(?:pricing|call to action|cta)\b", re.I)),
)


def parse_public_design_system(
    body: str | bytes,
    url: str,
    *,
    company_key: str = "",
    first_party_source_url: str = "",
    officially_linked: bool = False,
    additional_surfaces: Sequence[FrontierDocument] = (),
    primary_document: FrontierDocument | None = None,
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    scanner = "public_design_system_v1"
    now = _now(observed_at)
    company, documents, truncated = _surface_documents(
        scanner,
        body,
        url,
        company_key=company_key,
        first_party_source_url=first_party_source_url,
        officially_linked=officially_linked,
        additional_surfaces=additional_surfaces,
        document_kind="public_design_system",
        surface_cap=2,
        primary_document=primary_document,
    )
    if not documents:
        return _empty(
            scanner,
            company,
            "Design-system surface lacks exact first-party linkage.",
            observed_at=now,
            status="identity_ambiguous",
        )
    component_keys: set[str] = set()
    media_families: set[str] = set()
    frameworks: set[str] = set()
    docs_structure: set[str] = set()
    artifacts: list[EvidenceArtifact] = []
    errors: list[str] = []
    successful = 0
    for document in documents:
        prefix = document.body.lstrip()[:100].lower()
        texts: list[str] = []
        if prefix.startswith((b"{", b"[")) or "json" in document.mime_type.casefold():
            try:
                payload = json.loads(document.body)
            except (json.JSONDecodeError, RecursionError, TypeError) as exc:
                errors.append(f"{document.url}: invalid design-system JSON: {type(exc).__name__}")
                continue
            entries = payload.get("entries", {}) if isinstance(payload, Mapping) else {}
            if isinstance(entries, Mapping):
                for key, raw_entry in list(entries.items())[:500]:
                    if not isinstance(raw_entry, Mapping):
                        continue
                    title = _text(raw_entry.get("title") or raw_entry.get("name"), 300)
                    entry_type = _text(raw_entry.get("type"), 50).casefold()
                    if title:
                        component_keys.add(_name(title))
                        texts.append(title)
                    elif key:
                        component_keys.add(_name(key))
                    if entry_type in {"docs", "story"}:
                        docs_structure.add(entry_type)
            texts.append(
                _text(
                    json.dumps({"v": payload.get("v")}) if isinstance(payload, Mapping) else "", 100
                )
            )
        else:
            soup = parse_html(document.body.decode(errors="replace"), "html.parser")
            for node in soup.find_all(["a", "h1", "h2", "h3", "h4"], limit=500):
                text_value = _text(node.get_text(" ", strip=True), 300)
                href = str(node.get("href") or "") if isinstance(node, Tag) else ""
                if text_value:
                    texts.append(text_value)
                if text_value and re.search(
                    r"(?:story|component|pattern|docs?|canvas)", f"{href} {text_value}", re.I
                ):
                    component_keys.add(_name(text_value))
                if re.search(
                    r"\b(?:docs?|stories?|canvas|controls|patterns?)\b",
                    f"{href} {text_value}",
                    re.I,
                ):
                    docs_structure.add(_name(text_value)[:50])
        combined = _text(" ".join(texts), 100_000)
        media_families.update(
            name for name, pattern in _DESIGN_MEDIA_FAMILIES if pattern.search(combined)
        )
        for framework in ("React", "Vue", "Angular", "Svelte", "Web Components", "Storybook"):
            if re.search(rf"\b{re.escape(framework)}\b", combined, re.I):
                frameworks.add(framework.casefold().replace(" ", "_"))
        successful += 1
        artifacts.append(
            _artifact(
                scanner,
                company,
                replace(document, kind="public_design_system"),
                now,
                excerpt=f"{len(component_keys)} bounded public component key(s)",
                truncated=truncated,
            )
        )
    source = _safe_url(documents[0].url)
    value = {
        "public_component_count": min(len(component_keys), 500),
        "media_marketing_component_families": sorted(media_families),
        "framework_markers": sorted(frameworks),
        "documentation_structure_count": min(len(docs_structure), 100),
    }
    facts = []
    if successful:
        facts.append(
            _Fact(
                "design.public_system",
                value,
                "json",
                f"public-design-system:{company}",
                (
                    "The linked public design-system sample exposes "
                    f"{value['public_component_count']} bounded component key(s)."
                ),
                source,
                {"projection_score_gate": False, "context_only": True, "endpoint_guessing": False},
                confidence="high" if _same_company(source, company) else "medium",
                score_eligible=False,
                discovery_only=True,
                source_actor_key=f"domain:{company}",
                causal_root_key=f"creative-infrastructure:{company}",
            )
        )
    return _result(
        scanner,
        company,
        facts=facts,
        artifacts=artifacts,
        observed_at=now,
        status="failed" if not successful else None,
        surface="first_party_linked_public_design_system",
        scope=source,
        attempted=len(documents),
        successful=successful,
        failed=len(errors),
        total=len(documents),
        completeness="none" if not successful else "partial" if truncated or errors else "complete",
        truncated=truncated,
        absence_interpretable=bool(successful and not truncated and not errors),
        limitations=(
            "Only explicitly linked static HTML or JSON is parsed; no browser, script "
            "execution, endpoint guessing, or bundle enumeration occurs.",
            "Public component metadata does not establish team size, design maturity, "
            "product scale, spend, or purchase intent.",
        ),
        errors=errors,
        features={"public_design_system": value},
        applicable_reason="First-party-linked public design-system surfaces were supplied.",
    )


_SF_RE = re.compile(
    r"\b(?:san francisco|sf,?\s+ca|bay area|oakland|berkeley|san mateo|redwood city|"
    r"palo alto|menlo park|mountain view|south san francisco)\b",
    re.I,
)


def parse_sf_ai_capital_constellation(
    frontier: FrontierIntelligence,
    *,
    company_key: str,
    legal_name: str = "",
    first_party_locations: Sequence[str] = (),
    datasf_rows: Sequence[Mapping[str, Any]] = (),
    source_url: str = "https://data.sfgov.org/resource/g8m3-pdis.json",
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    scanner = "sf_ai_capital_constellation_v1"
    now = _now(observed_at)
    company = _host(company_key)
    legal = _name(legal_name)
    datasf_matches: list[Mapping[str, Any]] = []
    for row in datasf_rows[:10_000]:
        names = {
            _name(_row(row, "dba_name", "DBA Name", "business_name")),
            _name(_row(row, "full_business_name", "Ownership Name", "legal_name")),
        }
        if (
            legal
            and legal in names
            and _location_match(
                first_party_locations,
                (
                    _row(row, "business_location", "Business Location"),
                    _row(row, "street_address", "Street Address"),
                    _row(row, "city", "City"),
                    _row(row, "state", "State"),
                ),
            )
        ):
            datasf_matches.append(row)
    geography = [item for item in first_party_locations if _SF_RE.search(item)]
    if datasf_matches:
        geography.append("DataSF exact-name-and-address match")
    ai = [
        item
        for item in frontier.observations
        if item.company_key == company
        and item.scanner_id == "ai_artifact_provenance_v1"
        and item.predicate == "ai.product_role"
        and item.value in {"core_product", "meaningful_feature"}
        and item.score_eligible
        and item.validity_status not in {"expired", "disputed"}
    ]
    capacity_scanners = {
        "capital.form_d_amount_sold": "sec_form_d_capital_v1",
        "capital.form_d_filing": "sec_form_d_capital_v1",
        "workforce.lca_certified_position_count": "dol_lca_workforce_v1",
    }
    capacity = [
        item
        for item in frontier.observations
        if item.company_key == company
        and item.predicate in capacity_scanners
        and item.scanner_id == capacity_scanners[item.predicate]
        and item.score_eligible
        and item.validity_status not in {"expired", "disputed"}
    ]
    creative_predicates = {
        "creative.asset_concept_family_count": "creative_asset_genome_v1",
        "lifecycle.public_campaign_count": "lifecycle_email_exhaust_v1",
        "app.store_release": "app_release_pulse_v1",
        "localization.declared_variant_count": "localization_burden_v1",
        "creator.program": "creator_program_contract_v1",
        "media.publishing_pulse": "owned_media_pulse_v1",
    }
    creative = [
        item
        for item in frontier.observations
        if item.company_key == company
        and item.predicate in creative_predicates
        and item.scanner_id == creative_predicates[item.predicate]
        and item.score_eligible
        and item.validity_status not in {"expired", "disputed"}
    ]
    facts: list[_Fact] = []
    if geography and ai and capacity:
        components = [*ai, *capacity, *creative]
        actors = sorted({item.source_actor_key for item in components if item.source_actor_key})
        roots = sorted({item.causal_root_key for item in components if item.causal_root_key})
        facts.append(
            _Fact(
                "constellation.sf_ai_capital_candidate",
                {
                    "research_candidate": True,
                    "sf_evidence": geography,
                    "ai_evidence_count": len(ai),
                    "capital_or_capacity_evidence_count": len(capacity),
                    "independent_creative_trigger": bool(creative),
                    "creative_trigger_absence_interpretable": False,
                    "recommended_action": "research_next",
                },
                "json",
                f"company:{company}",
                "Independent public SF, AI-product, and capital/capacity facts co-occur."
                + (
                    " An independent creative trigger also exists."
                    if creative
                    else " No independent creative trigger is yet observed in this projection."
                ),
                source_url,
                {
                    "component_observation_keys": [item.observation_key for item in components][
                        :50
                    ],
                    "independent_source_actors": actors,
                    "data_sf_proves_headquarters": False,
                    "ai_plus_capital_is_not_contact_now": True,
                    "lineage_mode": "pointer_only_non_scoreable",
                },
                epistemic_status="derived",
                confidence="high" if len(actors) >= 3 and creative else "medium",
                confidence_method="independent_predicate_constellation_rule",
                discovery_only=True,
                source_actor_key="derived:frontier-constellation",
                causal_root_key=f"constellation:{_hash(roots)}",
            )
        )
    document = FrontierDocument(
        source_url,
        json.dumps(datasf_matches, sort_keys=True, default=str),
        "datasf_registered_business_index_slice",
        mime_type="application/json",
        publisher="DataSF",
    )
    artifact = _artifact(
        scanner, company, document, now, excerpt=f"{len(datasf_matches)} exact DataSF matches"
    )
    return _result(
        scanner,
        company,
        facts=facts,
        artifacts=[artifact],
        observed_at=now,
        surface="sf_ai_capital_independent_fact_constellation",
        scope=company,
        attempted=1,
        successful=1,
        total=1,
        status=None if facts else "partial",
        completeness="complete" if facts else "partial",
        absence_interpretable=False,
        limitations=(
            "DataSF corroborates a location but does not establish headquarters.",
            "AI plus capital/capacity is research context, never automatic contact-now "
            "qualification.",
        ),
        features={
            "sf_ai_capital_constellation": {
                "candidate": bool(facts),
                "ai_evidence_count": len(ai),
                "capacity_evidence_count": len(capacity),
                "creative_trigger_count": len(creative),
                "recommended_action": "research_next" if facts else "insufficient_evidence",
            }
        },
        applicable_reason="Current frontier projection and public geography inputs were available.",
    )


def _merge_values(left: Any, right: Any) -> Any:
    if isinstance(left, dict) and isinstance(right, dict):
        merged = dict(left)
        for key, value in right.items():
            merged[key] = _merge_values(merged[key], value) if key in merged else value
        return merged
    if isinstance(left, list) and isinstance(right, list):
        result: list[Any] = []
        seen: set[str] = set()
        for item in [*left, *right]:
            identity = json.dumps(item, sort_keys=True, default=str)
            if identity not in seen:
                seen.add(identity)
                result.append(item)
        return sorted(result, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return right


def merge_frontier(
    target: FrontierIntelligence,
    incoming: FrontierIntelligence,
) -> FrontierIntelligence:
    """Return a deterministic snapshot projection without mutating either input."""

    artifact_map = {item.artifact_key: item for item in target.evidence_artifacts}
    for item in incoming.evidence_artifacts:
        old = artifact_map.get(item.artifact_key)
        if old is None or item.fetched_at >= old.fetched_at:
            artifact_map[item.artifact_key] = item
    coverage_map = {item.manifest_key: item for item in target.coverage_manifests}
    for item in incoming.coverage_manifests:
        old = coverage_map.get(item.manifest_key)
        if old is None or item.observed_at >= old.observed_at:
            coverage_map[item.manifest_key] = item
    observation_map = {item.observation_key: item for item in target.observations}
    for item in incoming.observations:
        old = observation_map.get(item.observation_key)
        if old is None:
            observation_map[item.observation_key] = item
            continue
        newer = (
            item
            if (item.last_observed_at or item.first_observed_at)
            >= (old.last_observed_at or old.first_observed_at)
            else old
        )
        observation_map[item.observation_key] = newer.model_copy(
            update={
                "first_observed_at": min(old.first_observed_at, item.first_observed_at),
                "last_observed_at": max(
                    old.last_observed_at or old.first_observed_at,
                    item.last_observed_at or item.first_observed_at,
                ),
                "artifact_keys": list(dict.fromkeys([*old.artifact_keys, *item.artifact_keys]))[
                    :20
                ],
                "coverage_keys": list(dict.fromkeys([*old.coverage_keys, *item.coverage_keys]))[
                    :20
                ],
            }
        )
    executions = {
        (item.scanner_id, item.scanner_version, item.scope_key): item for item in target.executions
    }
    for item in incoming.executions:
        key = item.scanner_id, item.scanner_version, item.scope_key
        old = executions.get(key)
        if old is None or (item.finished_at or item.started_at) >= (
            old.finished_at or old.started_at
        ):
            executions[key] = item
    schedules = {
        (item.scanner_id, item.scanner_version): item for item in target.schedule_decisions
    }
    for item in incoming.schedule_decisions:
        key = item.scanner_id, item.scanner_version
        old = schedules.get(key)
        if old is None or item.decided_at >= old.decided_at:
            schedules[key] = item
    updated = max(
        (item for item in (target.updated_at, incoming.updated_at) if item is not None),
        default=None,
    )
    return FrontierIntelligence(
        version=incoming.version or target.version,
        executions=sorted(executions.values(), key=lambda item: (item.scanner_id, item.scope_key)),
        coverage_manifests=sorted(coverage_map.values(), key=lambda item: item.manifest_key),
        evidence_artifacts=sorted(artifact_map.values(), key=lambda item: item.artifact_key),
        observations=sorted(observation_map.values(), key=lambda item: item.observation_key),
        schedule_decisions=sorted(schedules.values(), key=lambda item: item.scanner_id),
        features=_json(_merge_values(target.features, incoming.features)),
        updated_at=updated,
    )


def _app_discovery(html: str | bytes, url: str, company: str) -> tuple[list[str], list[str]]:
    raw = html if isinstance(html, bytes) else html.encode()
    soup = parse_html(raw[:_MAX_DOCUMENT_BYTES].decode(errors="replace"), "html.parser")
    numeric_ids: list[str] = []
    app_link = False
    for anchor in soup.find_all("a", href=True):
        candidate = _safe_url(str(anchor.get("href") or ""), url)
        if "apps.apple.com/" in candidate or "itunes.apple.com/" in candidate:
            app_link = True
            if match := re.search(r"/id(\d+)", candidate):
                numeric_ids.append(match.group(1))
        elif "play.google.com/store/apps/details" in candidate:
            app_link = True
    numeric_ids = list(dict.fromkeys(numeric_ids))
    requests = []
    if app_link:
        requests = [
            f"https://{company}/.well-known/assetlinks.json",
            f"https://{company}/.well-known/apple-app-site-association",
        ]
        if numeric_ids:
            requests.append(f"https://itunes.apple.com/lookup?id={numeric_ids[0]}")
        else:
            requests.append(f"https://{company}/apple-app-site-association")
    return requests, numeric_ids


def _frontier_identity_inputs(
    html: str | bytes,
    url: str,
) -> dict[str, list[str]]:
    raw = html if isinstance(html, bytes) else html.encode()
    soup = parse_html(raw[:_MAX_DOCUMENT_BYTES].decode(errors="replace"), "html.parser")
    legal_names: set[str] = set()
    organization_names: set[str] = set()
    postal_addresses: set[str] = set()
    telephones: set[str] = set()

    def type_names(node: Mapping[str, Any]) -> set[str]:
        raw_types = node.get("@type", [])
        values = [raw_types] if isinstance(raw_types, str) else raw_types
        return (
            {_text(value, 100).casefold().rsplit("/", 1)[-1] for value in values if value}
            if isinstance(values, list)
            else set()
        )

    def address_string(value: Any) -> str:
        if isinstance(value, str):
            return _text(value, 500)
        if not isinstance(value, Mapping):
            return ""
        parts = [
            value.get("streetAddress"),
            value.get("addressLocality"),
            value.get("addressRegion"),
            value.get("postalCode"),
            value.get("addressCountry"),
        ]
        return _text(", ".join(str(part) for part in parts if part), 500)

    for script in soup.find_all(
        "script",
        attrs={"type": re.compile(r"ld\+json", re.I)},
        limit=100,
    ):
        try:
            payload = json.loads(script.string or script.get_text(" ", strip=True))
        except (json.JSONDecodeError, RecursionError, TypeError):
            continue
        stack = [payload]
        visited = 0
        while stack and visited < 500:
            current = stack.pop()
            visited += 1
            if isinstance(current, list):
                stack.extend(current[:100])
                continue
            if not isinstance(current, Mapping):
                continue
            stack.extend(
                value for value in list(current.values())[:100] if isinstance(value, (dict, list))
            )
            types = type_names(current)
            if "organization" in types or "corporation" in types:
                organization_name = _text(current.get("name"), 300)
                if organization_name:
                    organization_names.add(organization_name)
                legal_name = _text(current.get("legalName"), 300)
                if legal_name:
                    legal_names.add(legal_name)
                address = address_string(current.get("address"))
                if address:
                    postal_addresses.add(address)
                telephone = _text(current.get("telephone"), 100)
                if telephone:
                    telephones.add(telephone)
            elif "postaladdress" in types:
                address = address_string(current)
                if address:
                    postal_addresses.add(address)
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if href.casefold().startswith("tel:"):
            telephone = _text(href[4:].split("?", 1)[0], 100)
            if telephone:
                telephones.add(telephone)
    return {
        "legal_names": sorted(legal_names)[:10],
        "organization_names": sorted(organization_names)[:10],
        "postal_addresses": sorted(postal_addresses)[:20],
        "telephones": sorted(telephones)[:20],
        "source_urls": [_safe_url(url)],
    }


@reuse_html
def extract_frontier_from_html(
    html: str | bytes,
    url: str,
    *,
    company_key: str = "",
    observed_at: datetime | None = None,
) -> FrontierIntelligence:
    """Run zero-request HTML scanners and surface URLs for later optional adapters."""

    now = _now(observed_at)
    company = _host(company_key) or _host(url)
    if not company or not _same_company(url, company):
        return FrontierIntelligence(
            features={"frontier_discovery": {"error": "first_party_identity_ambiguous"}},
            updated_at=now,
        )
    result = FrontierIntelligence(updated_at=now)
    for parsed in (
        parse_creative_asset_genome(html, url, company_key=company, observed_at=now),
        parse_localization_burden(html, url, company_key=company, observed_at=now),
        parse_ai_artifact_provenance(html, url, company_key=company, observed_at=now),
        parse_growth_measurement_schema(html, url, company_key=company, observed_at=now),
        parse_seo_visibility_engine(html, url, company_key=company, observed_at=now),
    ):
        result = merge_frontier(result, parsed)
    path = urlparse(url).path.casefold()
    raw = html if isinstance(html, bytes) else html.encode()
    soup = parse_html(raw[:_MAX_DOCUMENT_BYTES].decode(errors="replace"), "html.parser")
    top_level_context = _text(
        " ".join(
            node.get_text(" ", strip=True) for node in soup.find_all(["title", "h1"], limit=10)
        ),
        3_000,
    )
    # H2 cards and link labels are outbound discovery hints, not proof that the current document
    # is itself that scanner's surface.
    page_context = f"{path} {top_level_context}"
    structured_job_detail = (
        not _CAREER_SURFACE_RE.search(page_context) and has_first_party_job_schema(soup, url)
    )
    if _ARCHIVE_RE.search(page_context):
        page_owner = "lifecycle_email_exhaust_v1"
    elif (structured_job_detail or _CAREER_SURFACE_RE.search(page_context)) and not (
        _CAREER_EXCLUDED_RE.search(page_context)
    ):
        page_owner = "creative_hiring_demand_v1"
    elif re.search(r"creator|affiliate|ambassador|influencer|referral|ugc", page_context, re.I):
        page_owner = "creator_program_contract_v1"
    elif _PRODUCT_RELEASE_SURFACE_RE.search(
        page_context
    ) and not _PRODUCT_RELEASE_EXCLUDED_RE.search(page_context):
        page_owner = "product_release_engine_v1"
    elif _EVENT_SURFACE_RE.search(page_context) and not _SURFACE_REJECT_RE.search(page_context):
        page_owner = "event_webinar_engine_v1"
    elif _TRUST_SURFACE_RE.search(page_context):
        page_owner = "trust_center_readiness_v1"
    elif _BRAND_SURFACE_RE.search(page_context):
        page_owner = "brand_asset_governance_v1"
    elif _DESIGN_SYSTEM_SURFACE_RE.search(page_context):
        page_owner = "public_design_system_v1"
    elif _BLOG_SURFACE_RE.search(path) and not _BLOG_EXCLUDED_SURFACE_RE.search(path):
        page_owner = "corporate_blog_engine_v1"
    else:
        page_owner = ""
    if page_owner == "creator_program_contract_v1":
        result = merge_frontier(
            result,
            parse_creator_program_contract(html, url, company_key=company, observed_at=now),
        )
    elif page_owner == "lifecycle_email_exhaust_v1":
        result = merge_frontier(
            result,
            parse_lifecycle_archive(html, url, company_key=company, observed_at=now),
        )
    elif page_owner == "creative_hiring_demand_v1":
        result = merge_frontier(
            result,
            parse_creative_hiring_demand(
                html,
                url,
                company_key=company,
                officially_linked=True,
                observed_at=now,
                is_detail_page=structured_job_detail,
            ),
        )
    elif page_owner == "product_release_engine_v1":
        result = merge_frontier(
            result,
            parse_product_release_engine(
                html,
                url,
                company_key=company,
                officially_linked=True,
                observed_at=now,
            ),
        )
    elif page_owner == "event_webinar_engine_v1":
        result = merge_frontier(
            result,
            parse_event_webinar_engine(
                html,
                url,
                company_key=company,
                officially_linked=True,
                observed_at=now,
            ),
        )
    elif page_owner == "trust_center_readiness_v1":
        result = merge_frontier(
            result,
            parse_trust_center_readiness(
                html,
                url,
                company_key=company,
                officially_linked=True,
                observed_at=now,
            ),
        )
    elif page_owner == "brand_asset_governance_v1":
        result = merge_frontier(
            result,
            parse_brand_asset_governance(
                html,
                url,
                company_key=company,
                officially_linked=True,
                observed_at=now,
            ),
        )
    elif page_owner == "public_design_system_v1":
        result = merge_frontier(
            result,
            parse_public_design_system(
                html,
                url,
                company_key=company,
                officially_linked=True,
                observed_at=now,
            ),
        )
    elif page_owner == "corporate_blog_engine_v1":
        # Core hydration may already have fetched the blog hub. Persist its bounded local
        # projection now so the optional runtime never needs to refetch a completed page.
        result = merge_frontier(
            result,
            parse_corporate_blog_engine(
                html,
                url,
                company_key=company,
                officially_linked=True,
                observed_at=now,
            ),
        )
    app_urls, app_ids = _app_discovery(html, url, company)
    owned_surfaces = discover_frontier_request_surfaces(html, url)
    request_urls = {
        "lifecycle_email_exhaust_v1": owned_surfaces["lifecycle_email_exhaust_v1"][:5],
        "app_release_pulse_v1": app_urls,
        "creator_program_contract_v1": owned_surfaces["creator_program_contract_v1"][:5],
        "owned_media_pulse_v1": owned_surfaces["owned_media_pulse_v1"][:10],
        "corporate_blog_engine_v1": owned_surfaces["corporate_blog_engine_v1"][:5],
        "creative_hiring_demand_v1": owned_surfaces["creative_hiring_demand_v1"][:8],
        "product_release_engine_v1": owned_surfaces["product_release_engine_v1"][:4],
        "event_webinar_engine_v1": owned_surfaces["event_webinar_engine_v1"][:5],
        "trust_center_readiness_v1": owned_surfaces["trust_center_readiness_v1"][:3],
        "brand_asset_governance_v1": owned_surfaces["brand_asset_governance_v1"][:3],
        "public_design_system_v1": owned_surfaces["public_design_system_v1"][:2],
    }
    source_url = _safe_url(url)
    request_provenance = {
        request_url: source_url for urls in request_urls.values() for request_url in urls
    }
    request_url_sources = {
        scanner_id: {request_url: source_url for request_url in urls}
        for scanner_id, urls in request_urls.items()
    }
    identity_inputs = _frontier_identity_inputs(html, url)
    discovery = {
        "request_urls": request_urls,
        "request_provenance": request_provenance,
        "request_url_sources": request_url_sources,
        "first_party_app_ids": app_ids,
        "identity_inputs": identity_inputs,
        # Compatibility projections for existing orchestration. These remain separate: an
        # Organization `name` is never upgraded into an exact legal-name identity input.
        "first_party_legal_names": identity_inputs["legal_names"],
        "first_party_company_names": identity_inputs["organization_names"],
        "first_party_locations": identity_inputs["postal_addresses"],
        "first_party_phones": identity_inputs["telephones"],
        "source_url": source_url,
    }
    result.features = _json(_merge_values(result.features, {"frontier_discovery": discovery}))
    return result


def _html_documents(context: FrontierScannerContext) -> list[FrontierDocument]:
    documents: list[FrontierDocument] = []
    for document in context.documents:
        prefix = document.body[:500].lstrip().lower()
        html_like = (
            "html" in document.mime_type.casefold()
            or "html" in document.kind.casefold()
            or prefix.startswith((b"<!doctype html", b"<html"))
        )
        if html_like and _same_company(document.url, context.company_key):
            documents.append(document)
    return documents


def _document_is_company_bound(document: FrontierDocument, company_key: str) -> bool:
    return _same_company(document.url, company_key) or bool(
        document.officially_linked
        and document.first_party_source_url
        and _same_company(document.first_party_source_url, company_key)
    )


def _discovered_request_urls(
    scanner_id: str,
    context: FrontierScannerContext,
) -> list[str]:
    return list(_request_provenance(scanner_id, context))


def _request_provenance(
    scanner_id: str,
    context: FrontierScannerContext,
) -> dict[str, str]:
    provenance: dict[str, str] = {}
    locally_owned_by_url: dict[str, str] = {}
    for document in _html_documents(context):
        if scanner_id in _FRONTIER_NETWORK_SURFACE_OWNERS:
            discovered = discover_frontier_request_surfaces(document.body, document.url)
            for owner, owned_urls in discovered.items():
                for owned_url in owned_urls:
                    locally_owned_by_url[owned_url] = owner
            urls = discovered.get(scanner_id, [])
        elif scanner_id == "app_release_pulse_v1":
            app_urls, _ = _app_discovery(document.body, document.url, context.company_key)
            urls = app_urls
        else:
            urls = []
        for raw in urls:
            request_url = _safe_url(raw)
            source_url = _safe_url(document.url)
            if request_url and source_url:
                provenance[request_url] = source_url

    metadata_roots = [context.metadata]
    discovery_metadata = context.metadata.get("frontier_discovery")
    if isinstance(discovery_metadata, Mapping):
        metadata_roots.append(discovery_metadata)

    # Orchestration constructs one context per scanner, so its bounded request tuple is itself a
    # scanner-specific declaration. A URL still needs a same-company source in the legacy map.
    declared_urls = {
        request_url for raw in context.request_urls if (request_url := _safe_url(str(raw)))
    }
    declared_urls.update(provenance)
    scanner_specific_sources: list[Mapping[Any, Any]] = []
    legacy_flat_sources: list[Mapping[Any, Any]] = []
    for root in metadata_roots:
        declared = root.get("request_urls")
        if isinstance(declared, Mapping):
            scanner_declared = declared.get(scanner_id)
            if isinstance(scanner_declared, (list, tuple, set)):
                declared_urls.update(
                    request_url for raw in scanner_declared if (request_url := _safe_url(str(raw)))
                )
        sources = root.get("request_url_sources", root.get("request_sources"))
        if isinstance(sources, Mapping):
            scanner_sources = sources.get(scanner_id)
            if isinstance(scanner_sources, Mapping):
                scanner_specific_sources.append(scanner_sources)
            elif all(not isinstance(value, Mapping) for value in sources.values()):
                legacy_flat_sources.append(sources)
        flat = root.get("request_provenance")
        if isinstance(flat, Mapping):
            legacy_flat_sources.append(flat)

    # Scanner-keyed maps are the authoritative compatibility contract. A flat legacy map may
    # contribute only URLs explicitly declared for this scanner or rediscovered by its sole owner.
    for candidate in scanner_specific_sources:
        if not isinstance(candidate, Mapping):
            continue
        for raw_url, raw_source in candidate.items():
            request_url = _safe_url(str(raw_url))
            source_url = _safe_url(str(raw_source))
            local_owner = locally_owned_by_url.get(request_url)
            if (
                request_url
                and (not local_owner or local_owner == scanner_id)
                and source_url
                and _same_company(source_url, context.company_key)
            ):
                provenance[request_url] = source_url
    for candidate in legacy_flat_sources:
        for raw_url, raw_source in candidate.items():
            request_url = _safe_url(str(raw_url))
            source_url = _safe_url(str(raw_source))
            local_owner = locally_owned_by_url.get(request_url)
            if (
                request_url in declared_urls
                and (not local_owner or local_owner == scanner_id)
                and source_url
                and _same_company(source_url, context.company_key)
            ):
                provenance[request_url] = source_url
    return dict(sorted(provenance.items()))


def _request_urls(scanner_id: str, context: FrontierScannerContext) -> list[str]:
    urls = [*_discovered_request_urls(scanner_id, context), *context.request_urls]
    admitted = list(dict.fromkeys(
        url for raw in urls if (url := _safe_url(raw)) and not is_frontier_asset_url(url)
        and is_frontier_request_target(url, scanner_id, context.company_key)
    ))
    if scanner_id == "corporate_blog_engine_v1":
        admitted = [url for url in admitted if not _is_comment_feed_url(url)]
    if scanner_id == "event_webinar_engine_v1":
        admitted.sort(key=_event_surface_priority)
    elif scanner_id == "product_release_engine_v1":
        admitted.sort(key=_release_surface_priority)
    return admitted


def _account(
    result: FrontierIntelligence,
    *,
    requests: int,
    fetched_bytes: int,
    truncated: bool,
    errors: Sequence[str],
    error_statuses: Sequence[str] = (),
) -> FrontierIntelligence:
    failure_status = next(
        (
            candidate
            for candidate in (
                "robots_blocked",
                "rate_limited",
                "identity_ambiguous",
                "transient_error",
                "failed",
            )
            if candidate in error_statuses
        ),
        "transient_error",
    )
    executions: list[ScannerExecutionSummary] = []
    for execution in result.executions:
        status = execution.status
        if (
            errors
            and not result.observations
            and status
            in {
                "success_empty",
                "not_applicable",
                "identity_ambiguous",
            }
        ):
            status = failure_status
        elif (errors and status == "success") or (
            truncated and status in {"success", "success_empty", "not_applicable"}
        ):
            status = "partial"
        executions.append(
            execution.model_copy(
                update={
                    "request_count": requests,
                    "byte_count": max(execution.byte_count, fetched_bytes),
                    "status": status,
                    "errors": list(dict.fromkeys([*execution.errors, *errors]))[:20],
                }
            )
        )
    manifests: list[ScannerCoverageManifest] = []
    for coverage in result.coverage_manifests:
        if errors or truncated:
            manifests.append(
                coverage.model_copy(
                    update={
                        "completeness": "partial" if coverage.attempted_units else "none",
                        "truncated": coverage.truncated or truncated,
                        "absence_interpretable": False,
                        "failed_units": min(
                            coverage.attempted_units,
                            max(coverage.failed_units, len(errors)),
                        ),
                    }
                )
            )
        else:
            manifests.append(coverage)
    return result.model_copy(update={"executions": executions, "coverage_manifests": manifests})


async def run_scanner(
    scanner_id: str,
    context: FrontierScannerContext,
    fetch: FetchAdapter | None = None,
) -> FrontierIntelligence:
    """Run one bounded scanner after the caller has claimed its scanner lease.

    HTML scanners use ``context.documents``. Optional web scanners discover first-party-linked URLs
    from those documents plus ``context.request_urls``. DOL/SEC use local ``context.records``. AI
    accepts provider metadata in ``records``. The constellation accepts a ``FrontierIntelligence``
    at ``metadata['frontier']`` and optional DataSF rows in ``records``. ``metadata['phone']`` helps
    SEC identity; ``metadata['first_party_app_ids']`` helps Apple identity.

    ``fetch`` is invoked only inside this function, never at import/extraction time. Its owner
    remains responsible for DNS, redirect, robots, and peer-IP policy; this layer also enforces URL
    shape, per-scanner request count, and aggregate response bytes.
    """

    if scanner_id not in SCANNER_REGISTRY:
        raise KeyError(f"Unknown frontier scanner: {scanner_id}")
    definition = SCANNER_REGISTRY[scanner_id]
    now = _now(context.observed_at)
    documents = list(context.documents)
    request_count = 0
    byte_count = 0
    fetch_truncated = False
    fetch_errors: list[str] = []
    fetch_error_statuses: list[str] = []
    recovered_fetches: list[dict[str, str]] = []
    reused_documents: list[str] = []
    unproven_fetches = 0
    company_deferred = False
    if fetch is not None and definition.max_requests:
        proven_requests = _request_provenance(scanner_id, context)
        specifically_discovered = set(proven_requests)
        request_urls = _request_urls(scanner_id, context)[: definition.max_requests]
        spare_requests = definition.max_requests - len(request_urls)
        for request_url in request_urls:
            if any(
                document.body
                and _canonical_surface_url(document.url, document.url)
                == _canonical_surface_url(request_url, request_url)
                and _document_is_company_bound(document, context.company_key)
                for document in documents
            ):
                # Parse the already supplied, attributable bytes through the same
                # normal path. Reuse is not a new request or collection timestamp.
                reused_documents.append(request_url)
                continue
            remaining = definition.max_response_bytes - byte_count
            if remaining <= 0:
                fetch_truncated = True
                break
            document = None
            prior_failure = ""
            fetch_succeeded = False
            for attempt in range(2):
                request_count += 1
                try:
                    document = await fetch(request_url, remaining)
                except Exception as exc:
                    company_deferred = bool(getattr(exc, "company_research_deferred", False))
                    byte_count += max(0, int(getattr(exc, "frontier_response_bytes", 0)))
                    remaining = definition.max_response_bytes - byte_count
                    detail = f"fetch {request_url}: {type(exc).__name__}: {_text(exc, 500)}"
                    delay = scanner_retry_delay(exc)
                    if attempt == 0 and spare_requests > 0 and remaining > 0 and delay is not None:
                        spare_requests -= 1
                        prior_failure = detail
                        await asyncio.sleep(delay)
                        continue
                    fetch_errors.append(
                        f"{prior_failure} | retry failed: {detail}" if prior_failure else detail
                    )
                    status = str(getattr(exc, "terminal_status", "transient_error"))
                    if isinstance(exc, httpx.HTTPStatusError):
                        status = (
                            "rate_limited" if exc.response.status_code == 429 else
                            "transient_error" if exc.response.status_code >= 500
                            or exc.response.status_code in {408, 425} else "failed"
                        )
                    fetch_error_statuses.append(status if status in {
                        "failed", "identity_ambiguous", "rate_limited",
                        "robots_blocked", "transient_error",
                    } else "transient_error")
                else:
                    fetch_succeeded = True
                break
            if not fetch_succeeded:
                if company_deferred:
                    break
                continue
            if not isinstance(document, FrontierDocument):
                fetch_errors.append(f"{request_url}: unsupported fetch result")
                fetch_error_statuses.append("failed")
                continue
            if prior_failure:
                recovered_fetches.append({"url": request_url, "prior_failure": prior_failure})
            body = document.body
            if len(body) > remaining:
                body = body[:remaining]
                fetch_truncated = True
            byte_count += len(body)
            link_proven = request_url in specifically_discovered
            if not link_proven:
                # Caller-supplied request URLs are candidates, not first-party evidence. We still
                # account for the attempted request, but its response cannot hydrate observations.
                unproven_fetches += 1
                continue
            first_party = document.first_party_source_url
            if not first_party and link_proven:
                first_party = proven_requests[request_url]
            document_kind = document.kind
            if scanner_id == "app_release_pulse_v1":
                request_path = urlparse(request_url).path
                if request_path.endswith("/lookup"):
                    document_kind = "apple_lookup_metadata"
                elif request_path.endswith(("assetlinks.json", "apple-app-site-association")):
                    document_kind = "domain_app_association"
            elif scanner_id == "product_release_engine_v1":
                prefix = body.lstrip()[:100].lower()
                document_kind = (
                    "product_release_feed"
                    if prefix.startswith((b"<?xml", b"<rss", b"<feed", b"{", b"["))
                    or any(
                        marker in document.mime_type.casefold()
                        for marker in ("xml", "json", "rss", "atom")
                    )
                    else "product_release_surface"
                )
            elif scanner_id == "event_webinar_engine_v1":
                request_path = urlparse(request_url).path.casefold()
                prefix = body.lstrip()[:100].lower()
                document_kind = (
                    "event_calendar"
                    if request_path.endswith(".ics")
                    or "calendar" in document.mime_type.casefold()
                    or b"begin:vcalendar" in prefix
                    else "event_feed"
                    if prefix.startswith((b"<?xml", b"<rss", b"<feed", b"{", b"["))
                    or any(
                        marker in document.mime_type.casefold()
                        for marker in ("xml", "json", "rss", "atom")
                    )
                    else "event_surface"
                )
            elif scanner_id == "trust_center_readiness_v1":
                document_kind = (
                    "security_txt"
                    if urlparse(request_url).path.casefold().endswith("security.txt")
                    else "trust_center"
                )
            elif scanner_id == "brand_asset_governance_v1":
                document_kind = "brand_asset_portal"
            elif scanner_id == "public_design_system_v1":
                document_kind = "public_design_system"
            elif scanner_id == "creative_hiring_demand_v1":
                document_kind = (
                    "ats_career_board"
                    if _host(request_url) in _ATS_CAREER_HOSTS
                    else "company_career_surface"
                )
            documents.append(
                replace(
                    document,
                    content=body,
                    kind=document_kind,
                    first_party_source_url=first_party,
                    # A fetch-adapter assertion alone cannot launder a caller-provided URL.
                    # External URLs need the exact discovery provenance computed above.
                    officially_linked=(
                        document.officially_linked
                        if scanner_id == "creative_hiring_demand_v1"
                        else link_proven or _same_company(request_url, context.company_key)
                    ),
                )
            )
    local = replace(context, documents=tuple(documents), observed_at=now)
    html_docs = _html_documents(local)

    if scanner_id in {
        "creative_asset_genome_v1",
        "localization_burden_v1",
        "growth_measurement_schema_v1",
        "seo_visibility_engine_v1",
    }:
        if not html_docs:
            result = _empty(
                scanner_id,
                context.company_key,
                "No saved first-party HTML was available.",
                observed_at=now,
            )
        else:
            result = FrontierIntelligence(updated_at=now)
            parsed_results: list[FrontierIntelligence] = []
            local_parsers = {
                "creative_asset_genome_v1": parse_creative_asset_genome,
                "localization_burden_v1": parse_localization_burden,
                "growth_measurement_schema_v1": parse_growth_measurement_schema,
                "seo_visibility_engine_v1": parse_seo_visibility_engine,
            }
            parser = local_parsers[scanner_id]
            for document in html_docs[:5]:
                parsed = parser(
                    document.content,
                    document.url,
                    company_key=context.company_key,
                    observed_at=now,
                )
                parsed_results.append(parsed)
                result = merge_frontier(
                    result,
                    parsed,
                )
            if scanner_id == "growth_measurement_schema_v1":
                measurement_candidates = [
                    (item, parsed)
                    for parsed in parsed_results
                    for item in parsed.observations
                    if item.predicate == "measurement.public_funnel_schema"
                ]
                if measurement_candidates:
                    winner, winner_projection = max(
                        measurement_candidates,
                        key=lambda candidate: (
                            candidate[0].score_eligible,
                            int(candidate[0].value.get("explicit_event_calls", 0))
                            if isinstance(candidate[0].value, Mapping)
                            else 0,
                            len(candidate[0].value.get("funnel_families", ()))
                            if isinstance(candidate[0].value, Mapping)
                            and isinstance(candidate[0].value.get("funnel_families"), list)
                            else 0,
                            candidate[0].source_url,
                        ),
                    )
                    features = dict(result.features)
                    features["growth_measurement_schema"] = winner_projection.features.get(
                        "growth_measurement_schema", {}
                    )
                    result = result.model_copy(
                        update={
                            "observations": [
                                item
                                for item in result.observations
                                if item.predicate != "measurement.public_funnel_schema"
                            ]
                            + [winner],
                            "features": features,
                        },
                        deep=True,
                    )
    elif scanner_id == "lifecycle_email_exhaust_v1":
        urls = set(_request_urls(scanner_id, local))
        archives = [
            item
            for item in documents
            if (
                "archive" in item.kind.casefold()
                or item.url in urls
                or _ARCHIVE_RE.search(urlparse(item.url).path)
            )
            and _document_is_company_bound(item, context.company_key)
        ]
        if not archives:
            result = _empty(
                scanner_id,
                context.company_key,
                "No first-party-published lifecycle archive was discovered.",
                observed_at=now,
                status="identity_ambiguous" if unproven_fetches else "not_applicable",
            )
        else:
            archive = archives[0]
            pages = {
                item.url: item.content
                for item in documents
                if item is not archive and "campaign" in item.kind.casefold()
            }
            result = parse_lifecycle_archive(
                archive.content,
                archive.url,
                company_key=context.company_key,
                first_party_source_url=archive.first_party_source_url,
                campaign_pages=pages,
                observed_at=now,
            )
    elif scanner_id == "app_release_pulse_v1":
        associations = [
            item
            for item in documents
            if "association" in item.kind.casefold()
            or urlparse(item.url).path.endswith(("assetlinks.json", "apple-app-site-association"))
        ]
        if not associations:
            result = _empty(
                scanner_id,
                context.company_key,
                "No canonical-domain app association was available.",
                observed_at=now,
                status="identity_ambiguous" if unproven_fetches else "not_applicable",
            )
        else:
            lookup_doc = next(
                (item for item in documents if "apple_lookup" in item.kind.casefold()), None
            )
            lookup = None
            if lookup_doc:
                with suppress(json.JSONDecodeError):
                    lookup = json.loads(lookup_doc.body)
            metadata_app_ids = context.metadata.get("first_party_app_ids", ())
            metadata_app_ids = (
                metadata_app_ids if isinstance(metadata_app_ids, (tuple, list, set)) else ()
            )
            discovered_app_ids: list[str] = []
            for html_document in html_docs[:5]:
                _, document_app_ids = _app_discovery(
                    html_document.content,
                    html_document.url,
                    context.company_key,
                )
                discovered_app_ids.extend(document_app_ids)
            app_ids = tuple(
                dict.fromkeys(
                    str(item)
                    for item in [*metadata_app_ids, *discovered_app_ids]
                    if str(item).isdigit()
                )
            )
            result = FrontierIntelligence(updated_at=now)
            for association in associations[:2]:
                result = merge_frontier(
                    result,
                    parse_app_release_pulse(
                        association.content,
                        association.url,
                        company_key=context.company_key,
                        apple_lookup=lookup,
                        first_party_app_ids=tuple(app_ids),
                        observed_at=now,
                    ),
                )
    elif scanner_id == "creator_program_contract_v1":
        urls = set(_request_urls(scanner_id, local))
        programs = [
            item
            for item in documents
            if (
                "creator" in item.kind.casefold()
                or "affiliate" in item.kind.casefold()
                or item.url in urls
            )
            and _document_is_company_bound(item, context.company_key)
        ]
        if not programs:
            result = _empty(
                scanner_id,
                context.company_key,
                "No creator program page was discovered.",
                observed_at=now,
                status="identity_ambiguous" if unproven_fetches else "not_applicable",
            )
        else:
            program = programs[0]
            result = parse_creator_program_contract(
                program.content,
                program.url,
                company_key=context.company_key,
                first_party_source_url=program.first_party_source_url,
                observed_at=now,
            )
    elif scanner_id == "dol_lca_workforce_v1":
        result = parse_dol_lca_index(
            context.records,
            company_key=context.company_key,
            legal_name=context.legal_name,
            locations=context.locations,
            observed_at=now,
        )
    elif scanner_id == "sec_form_d_capital_v1":
        result = parse_sec_form_d_index(
            context.records,
            company_key=context.company_key,
            legal_name=context.legal_name,
            locations=context.locations,
            phone=str(context.metadata.get("phone") or ""),
            observed_at=now,
        )
    elif scanner_id == "ai_artifact_provenance_v1":
        if not html_docs:
            result = _empty(
                scanner_id,
                context.company_key,
                "No first-party product HTML was available.",
                observed_at=now,
            )
        else:
            result = parse_ai_artifact_provenance(
                html_docs[0].content,
                html_docs[0].url,
                company_key=context.company_key,
                artifacts=context.records,
                observed_at=now,
            )
    elif scanner_id == "owned_media_pulse_v1":
        urls = set(_request_urls(scanner_id, local))
        feeds = [
            item
            for item in documents
            if (
                "feed" in item.kind.casefold()
                or "xml" in item.mime_type.casefold()
                or item.url in urls
            )
            and _document_is_company_bound(item, context.company_key)
        ]
        if not feeds:
            result = _empty(
                scanner_id,
                context.company_key,
                "No officially linked media feed was discovered.",
                observed_at=now,
                status="identity_ambiguous" if unproven_fetches else "not_applicable",
            )
        else:
            # Preserve the single-source publication pulse: prefer an actual feed
            # over an HTML landing page rather than counting both as independent items.
            feed = next((
                item for item in feeds if "html" not in item.mime_type.casefold()
                and item.body.lstrip().lower().startswith((b"<?xml", b"<rss", b"<feed"))
            ), feeds[0])
            result = parse_owned_media_feed(
                feed.content, feed.url, company_key=context.company_key,
                first_party_source_url=feed.first_party_source_url,
                officially_linked=feed.officially_linked, observed_at=now,
                mime_type=feed.mime_type,
            )
    elif scanner_id == "corporate_blog_engine_v1":
        urls = set(_request_urls(scanner_id, local))
        surfaces = [
            item
            for item in documents
            if item.url in urls and _document_is_company_bound(item, context.company_key)
        ]
        if not surfaces:
            result = _empty(
                scanner_id,
                context.company_key,
                "No first-party-linked corporate blog surface was discovered.",
                observed_at=now,
                status="identity_ambiguous" if unproven_fetches else "not_applicable",
            )
        else:
            primary = surfaces[0]
            result = parse_corporate_blog_engine(
                primary.content,
                primary.url,
                company_key=context.company_key,
                first_party_source_url=primary.first_party_source_url,
                officially_linked=primary.officially_linked,
                additional_surfaces=surfaces[1:5],
                observed_at=now,
            )
    elif scanner_id == "creative_hiring_demand_v1":
        urls = set(_request_urls(scanner_id, local))
        surfaces = [
            item
            for item in documents
            if (
                item.url in urls
                or "career" in item.kind.casefold()
                or "ats" in item.kind.casefold()
            )
            and _document_is_company_bound(item, context.company_key)
        ][: definition.max_requests]
        if not surfaces and not context.records:
            result = _empty(
                scanner_id,
                context.company_key,
                "No company-bound careers surface or normalized public ATS record was available.",
                observed_at=now,
                status="identity_ambiguous" if unproven_fetches else "not_applicable",
            )
        else:
            primary = surfaces[0] if surfaces else None
            result = parse_creative_hiring_demand(
                primary.content if primary is not None else b"",
                primary.url if primary is not None else context.primary_url,
                company_key=context.company_key,
                first_party_source_url=(
                    primary.first_party_source_url if primary is not None else context.primary_url
                ),
                officially_linked=bool(primary and primary.officially_linked),
                additional_surfaces=surfaces[1:],
                structured_records=context.records,
                primary_document=primary,
                observed_at=now,
            )
    elif scanner_id in {
        "product_release_engine_v1",
        "event_webinar_engine_v1",
        "trust_center_readiness_v1",
        "brand_asset_governance_v1",
        "public_design_system_v1",
    }:
        urls = set(_request_urls(scanner_id, local))
        kind_markers = {
            "product_release_engine_v1": ("product_release",),
            "event_webinar_engine_v1": ("event_surface", "event_feed", "event_calendar"),
            "trust_center_readiness_v1": ("trust_center", "security_txt"),
            "brand_asset_governance_v1": ("brand_asset",),
            "public_design_system_v1": ("design_system", "storybook"),
        }[scanner_id]
        surfaces = [
            item
            for item in documents
            if (item.url in urls or any(marker in item.kind.casefold() for marker in kind_markers))
            and _document_is_company_bound(item, context.company_key)
        ][: definition.max_requests]
        if not surfaces:
            result = _empty(
                scanner_id,
                context.company_key,
                "No exact first-party-linked scanner surface was discovered.",
                observed_at=now,
                status="identity_ambiguous" if unproven_fetches else "not_applicable",
            )
        else:
            surface_parsers = {
                "product_release_engine_v1": parse_product_release_engine,
                "event_webinar_engine_v1": parse_event_webinar_engine,
                "trust_center_readiness_v1": parse_trust_center_readiness,
                "brand_asset_governance_v1": parse_brand_asset_governance,
                "public_design_system_v1": parse_public_design_system,
            }
            primary = surfaces[0]
            result = surface_parsers[scanner_id](
                primary.content,
                primary.url,
                company_key=context.company_key,
                first_party_source_url=primary.first_party_source_url,
                officially_linked=primary.officially_linked,
                additional_surfaces=surfaces[1:],
                primary_document=primary,
                observed_at=now,
            )
    elif scanner_id == "sf_ai_capital_constellation_v1":
        frontier = context.metadata.get("frontier")
        if not isinstance(frontier, FrontierIntelligence):
            result = _empty(
                scanner_id,
                context.company_key,
                "Current frontier projection was not supplied.",
                observed_at=now,
            )
        else:
            result = parse_sf_ai_capital_constellation(
                frontier,
                company_key=context.company_key,
                legal_name=context.legal_name,
                first_party_locations=context.locations,
                datasf_rows=context.records,
                observed_at=now,
            )
    else:  # pragma: no cover - guarded by registry/orchestration parity
        raise RuntimeError(f"No parser is registered for frontier scanner {scanner_id}")
    result = _account(
        result,
        requests=request_count,
        fetched_bytes=byte_count,
        truncated=fetch_truncated,
        errors=fetch_errors,
        error_statuses=fetch_error_statuses,
    )
    if recovered_fetches:
        result = result.model_copy(update={"executions": [
            execution.model_copy(update={"metadata": {
                **execution.metadata, "recovered_fetches": recovered_fetches,
            }}) for execution in result.executions
        ]})
    if reused_documents:
        result = result.model_copy(update={"executions": [
            execution.model_copy(update={"metadata": {
                **execution.metadata, "reused_documents": reused_documents,
            }}) for execution in result.executions
        ]})
    return result


__all__ = [
    "FetchAdapter",
    "FrontierDocument",
    "FrontierScannerContext",
    "SCANNER_DEFINITIONS",
    "SCANNER_REGISTRY",
    "ScannerDefinition",
    "build_dol_lca_index",
    "build_sec_form_d_index",
    "classify_frontier_surface_owner",
    "discover_brand_asset_urls",
    "discover_creator_program_urls",
    "discover_corporate_blog_urls",
    "discover_creative_hiring_urls",
    "discover_event_webinar_urls",
    "discover_frontier_request_surfaces",
    "discover_lifecycle_archive_urls",
    "discover_owned_media_urls",
    "discover_product_release_urls",
    "discover_public_design_system_urls",
    "discover_trust_center_urls",
    "extract_frontier_from_html",
    "merge_frontier",
    "parse_ai_artifact_provenance",
    "parse_app_release_pulse",
    "parse_brand_asset_governance",
    "parse_creator_program_contract",
    "parse_creative_asset_genome",
    "parse_corporate_blog_engine",
    "parse_creative_hiring_demand",
    "parse_dol_lca_index",
    "parse_event_webinar_engine",
    "parse_growth_measurement_schema",
    "parse_lifecycle_archive",
    "parse_localization_burden",
    "parse_owned_media_feed",
    "parse_product_release_engine",
    "parse_public_design_system",
    "parse_sec_form_d_index",
    "parse_seo_visibility_engine",
    "parse_sf_ai_capital_constellation",
    "parse_trust_center_readiness",
    "run_scanner",
]
