from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from .domain_policy import is_disallowed
from .frontier_intelligence import (
    SCANNER_REGISTRY,
    FrontierDocument,
    FrontierScannerContext,
    run_scanner,
)
from .models import (
    AtomicObservation,
    EvidenceArtifact,
    FrontierIntelligence,
    ScannerCoverageManifest,
    ScannerExecutionSummary,
    ScannerScheduleDecision,
)
from .store import (
    SCANNER_FAILURE_STATUSES,
    SCANNER_REUSABLE_STATUSES,
    LeadStore,
)

BoundedFetch = Callable[[str, int], Awaitable[FrontierDocument]]
RuntimeState = Literal[
    "completed",
    "reused",
    "active",
    "not_due",
    "deduplicated",
    "deferred",
    "failed",
    "invalid",
]

# These are transport-private accounting fields, not origin response headers. A
# caller-supplied bounded fetcher can use them when one logical target fetch also
# required bounded auxiliary work (for example, a robots.txt policy check). The
# adapter consumes and strips them before the document reaches a scanner.
FRONTIER_AUXILIARY_REQUEST_COUNT_HEADER = "x-beings-frontier-auxiliary-request-count"
FRONTIER_AUXILIARY_BYTE_COUNT_HEADER = "x-beings-frontier-auxiliary-byte-count"

_FEATURE_ROOTS = {
    "creative_asset_genome_v1": "creative_asset_genome",
    "lifecycle_email_exhaust_v1": "lifecycle_email_exhaust",
    "app_release_pulse_v1": "app_release_pulse",
    "localization_burden_v1": "localization_burden",
    "creator_program_contract_v1": "creator_program_contract",
    "dol_lca_workforce_v1": "dol_lca_workforce",
    "sec_form_d_capital_v1": "sec_form_d_capital",
    "ai_artifact_provenance_v1": "ai_artifact_provenance",
    "owned_media_pulse_v1": "owned_media_pulse",
    "corporate_blog_engine_v1": "corporate_blog_engine",
    "creative_hiring_demand_v1": "creative_hiring_demand",
    "product_release_engine_v1": "product_release_engine",
    "event_webinar_engine_v1": "event_webinar_engine",
    "trust_center_readiness_v1": "trust_center_readiness",
    "brand_asset_governance_v1": "brand_asset_governance",
    "growth_measurement_schema_v1": "growth_measurement_schema",
    "seo_visibility_engine_v1": "seo_visibility_engine",
    "public_design_system_v1": "public_design_system",
    "sf_ai_capital_constellation_v1": "sf_ai_capital_constellation",
}
if set(_FEATURE_ROOTS) != set(SCANNER_REGISTRY) or len(set(_FEATURE_ROOTS.values())) != len(
    _FEATURE_ROOTS
):
    raise RuntimeError("every registered company scanner needs one unique runtime feature root")
_FORBIDDEN_HOST_SUFFIXES = (
    ".internal",
    ".lan",
    ".local",
    ".localhost",
    ".onion",
)


class FrontierFetchPolicyError(ValueError):
    """A requested URL or returned document violated the runtime fetch boundary."""

    def __init__(self, message: str, *, terminal_status: str = "identity_ambiguous") -> None:
        super().__init__(message)
        self.terminal_status = terminal_status


class FrontierFetchBudgetExceeded(RuntimeError):
    """The runtime-wide request or response-byte budget has been exhausted."""


@dataclass(frozen=True, slots=True)
class FrontierRuntimeLimits:
    """Defense-in-depth caps above each scanner's own registry budget."""

    max_scanners: int = len(SCANNER_REGISTRY)
    max_context_documents: int = 25
    max_document_bytes: int = 2_000_000
    max_context_records: int = 10_000
    max_request_urls: int = 30
    max_fetch_requests: int = 30
    max_fetch_response_bytes: int = 2_000_000
    max_fetch_total_bytes: int = 10_000_000
    max_coverage_per_scanner: int = 20
    max_artifacts_per_scanner: int = 100
    max_observations_per_scanner: int = 500
    max_schedule_decisions_per_scanner: int = 20
    max_lineage_edges_per_scanner: int = 1_000
    max_error_characters: int = 1_000

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"Frontier runtime limit {name} must be positive")


@dataclass(frozen=True, slots=True)
class ScannerRuntimeOutcome:
    scanner_id: str
    state: RuntimeState
    execution_id: int | None = None
    terminal_status: str = ""
    error: str = ""
    coverage_count: int = 0
    artifact_count: int = 0
    observation_count: int = 0


@dataclass(frozen=True, slots=True)
class FrontierRuntimeReport:
    candidate_id: int
    company_key: str
    outcomes: tuple[ScannerRuntimeOutcome, ...]
    frontier: FrontierIntelligence
    fetch_requests: int = 0
    fetch_bytes: int = 0
    target_fetch_requests: int = 0
    target_fetch_bytes: int = 0
    auxiliary_fetch_requests: int = 0
    auxiliary_fetch_bytes: int = 0

    @property
    def completed_scanners(self) -> tuple[str, ...]:
        return tuple(item.scanner_id for item in self.outcomes if item.state == "completed")

    @property
    def reused_scanners(self) -> tuple[str, ...]:
        return tuple(item.scanner_id for item in self.outcomes if item.state == "reused")


@dataclass(slots=True)
class FrontierFetchAdapter:
    """Stateful URL-policy and global-budget wrapper around an SSRF-safe fetcher.

    The supplied fetcher must itself pin and validate DNS/redirect peer IPs, honor
    robots/source terms, enforce timeouts, and never return more than ``maximum_bytes``.
    This wrapper rejects dangerous URL shapes before that fetcher runs, validates the
    final document URL, and enforces independent request and byte ceilings.
    """

    bounded_fetch: BoundedFetch
    max_requests: int
    max_response_bytes: int
    max_total_bytes: int
    requests: int = 0
    bytes_received: int = 0
    target_requests: int = 0
    target_bytes_received: int = 0
    auxiliary_requests: int = 0
    auxiliary_bytes_received: int = 0

    async def __call__(self, url: str, maximum_bytes: int) -> FrontierDocument:
        safe_url = _public_http_url(url)
        if self.requests >= self.max_requests:
            raise FrontierFetchBudgetExceeded("frontier request budget exhausted")
        remaining = self.max_total_bytes - self.bytes_received
        allowed = min(
            max(0, int(maximum_bytes)),
            self.max_response_bytes,
            remaining,
        )
        if allowed <= 0:
            raise FrontierFetchBudgetExceeded("frontier response-byte budget exhausted")
        self.requests += 1
        self.target_requests += 1
        try:
            document = await self.bounded_fetch(safe_url, allowed)
        except Exception as exc:
            # The trusted transport reports bytes/auxiliary work even on an HTTP failure.
            # Retrying must never reclaim already-consumed request or response allowance.
            failed_bytes = max(0, int(getattr(exc, "frontier_response_bytes", 0)))
            auxiliary_requests = max(0, int(getattr(exc, "frontier_auxiliary_requests", 0)))
            auxiliary_bytes = max(0, int(getattr(exc, "frontier_auxiliary_bytes", 0)))
            self.bytes_received += failed_bytes + auxiliary_bytes
            self.target_bytes_received += failed_bytes
            self.requests += auxiliary_requests
            self.auxiliary_requests += auxiliary_requests
            self.auxiliary_bytes_received += auxiliary_bytes
            raise
        if not isinstance(document, FrontierDocument):
            raise TypeError("bounded frontier fetch must return FrontierDocument")
        body = document.body
        self.bytes_received += len(body)
        self.target_bytes_received += len(body)
        auxiliary_requests, auxiliary_bytes, public_headers = _consume_auxiliary_fetch_accounting(
            document.headers
        )
        self.requests += auxiliary_requests
        self.bytes_received += auxiliary_bytes
        self.auxiliary_requests += auxiliary_requests
        self.auxiliary_bytes_received += auxiliary_bytes
        final_url = _public_http_url(document.url or safe_url)
        if len(body) > allowed:
            raise FrontierFetchBudgetExceeded(
                "bounded frontier fetch exceeded its response-byte contract"
            )
        if self.requests > self.max_requests:
            raise FrontierFetchBudgetExceeded(
                "frontier request budget exhausted by auxiliary fetches"
            )
        if self.bytes_received > self.max_total_bytes:
            raise FrontierFetchBudgetExceeded(
                "frontier response-byte budget exhausted by auxiliary fetches"
            )
        return replace(document, url=final_url, headers=public_headers)


@dataclass(frozen=True, slots=True)
class _NormalizedScannerResult:
    frontier: FrontierIntelligence
    summary: ScannerExecutionSummary
    lineage: tuple[dict[str, Any], ...]
    capped: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _consume_auxiliary_fetch_accounting(
    headers: Mapping[str, str],
) -> tuple[int, int, dict[str, str]]:
    """Read trusted transport accounting and remove it from origin evidence."""
    reserved = {
        FRONTIER_AUXILIARY_REQUEST_COUNT_HEADER,
        FRONTIER_AUXILIARY_BYTE_COUNT_HEADER,
    }
    accounting: dict[str, str] = {}
    public_headers: dict[str, str] = {}
    for raw_key, raw_value in headers.items():
        key = str(raw_key)
        normalized = key.strip().casefold()
        value = str(raw_value)
        if normalized not in reserved:
            public_headers[key] = value
            continue
        if normalized in accounting:
            raise FrontierFetchPolicyError(f"duplicate frontier accounting field: {normalized}")
        accounting[normalized] = value

    def count(header: str) -> int:
        raw = accounting.get(header, "0").strip()
        if not re.fullmatch(r"[0-9]{1,18}", raw):
            raise FrontierFetchPolicyError(
                f"invalid nonnegative frontier accounting field: {header}"
            )
        return int(raw)

    return (
        count(FRONTIER_AUXILIARY_REQUEST_COUNT_HEADER),
        count(FRONTIER_AUXILIARY_BYTE_COUNT_HEADER),
        public_headers,
    )


def make_frontier_fetch_adapter(
    ssrf_safe_bounded_fetch: BoundedFetch,
    *,
    max_requests: int = 30,
    max_response_bytes: int = 2_000_000,
    max_total_bytes: int = 10_000_000,
) -> FrontierFetchAdapter:
    """Wrap a DNS/redirect-safe bounded fetcher with URL and aggregate hard caps."""
    if max_requests <= 0 or max_response_bytes <= 0 or max_total_bytes <= 0:
        raise ValueError("Frontier fetch budgets must be positive")
    return FrontierFetchAdapter(
        bounded_fetch=ssrf_safe_bounded_fetch,
        max_requests=int(max_requests),
        max_response_bytes=int(max_response_bytes),
        max_total_bytes=int(max_total_bytes),
    )


def group_frontier_by_scanner(
    frontier: FrontierIntelligence,
    *,
    scanner_ids: Sequence[str] | None = None,
) -> dict[str, FrontierIntelligence]:
    """Split a merged projection so one scanner can own one execution transaction."""
    discovered = {
        item.scanner_id
        for collection in (
            frontier.executions,
            frontier.coverage_manifests,
            frontier.evidence_artifacts,
            frontier.observations,
            frontier.schedule_decisions,
        )
        for item in collection
    }
    requested = (
        list(dict.fromkeys(item.strip() for item in scanner_ids if item.strip()))
        if scanner_ids is not None
        else sorted(discovered)
    )
    groups: dict[str, FrontierIntelligence] = {}
    for scanner_id in requested:
        if scanner_id not in discovered:
            continue
        feature_root = _FEATURE_ROOTS.get(scanner_id, "")
        features: dict[str, Any] = {}
        if feature_root and feature_root in frontier.features:
            features[feature_root] = frontier.features[feature_root]
        groups[scanner_id] = FrontierIntelligence(
            version=frontier.version,
            executions=[item for item in frontier.executions if item.scanner_id == scanner_id],
            coverage_manifests=[
                item for item in frontier.coverage_manifests if item.scanner_id == scanner_id
            ],
            evidence_artifacts=[
                item for item in frontier.evidence_artifacts if item.scanner_id == scanner_id
            ],
            observations=[item for item in frontier.observations if item.scanner_id == scanner_id],
            schedule_decisions=[
                item for item in frontier.schedule_decisions if item.scanner_id == scanner_id
            ],
            features=features,
            updated_at=frontier.updated_at,
        )
    return groups


def persist_frontier_projection(
    store: LeadStore,
    candidate_id: int,
    frontier: FrontierIntelligence,
    *,
    run_id: int | None = None,
    force_refresh: bool = False,
    limits: FrontierRuntimeLimits | None = None,
) -> FrontierRuntimeReport:
    """Persist an already-extracted projection without HTML parsing or network work.

    Scanner ids are discovered with a shallow identity pass. Each scanner/version is
    claimed before its group is constructed, normalized, or persisted, so a reusable
    group incurs no producer work beyond that identity pass.
    """
    runtime_limits = limits or FrontierRuntimeLimits()
    scanner_ids = _projection_scanner_ids(frontier)
    selected, invalid = _selected_scanners(scanner_ids, runtime_limits)
    outcomes: list[ScannerRuntimeOutcome] = [
        ScannerRuntimeOutcome(
            scanner_id=scanner_id,
            state="invalid",
            error="unknown or over-budget frontier scanner",
        )
        for scanner_id in invalid
    ]
    company_key = ""
    for scanner_id in selected:
        versions = _projection_versions(frontier, scanner_id)
        if len(versions) != 1:
            outcomes.append(
                ScannerRuntimeOutcome(
                    scanner_id=scanner_id,
                    state="invalid",
                    error="projection must contain exactly one scanner version",
                )
            )
            continue
        scanner_version = versions[0]
        definition = SCANNER_REGISTRY[scanner_id]
        matching_execution = next(
            (
                item
                for item in frontier.executions
                if item.scanner_id == scanner_id and item.scanner_version == scanner_version
            ),
            None,
        )
        try:
            claim = store.claim_scanner_work(
                [int(candidate_id)],
                scanner_id=scanner_id,
                scanner_version=scanner_version,
                run_id=run_id,
                extractor_version=(
                    matching_execution.extractor_version
                    if matching_execution is not None
                    else definition.extractor_version
                ),
                scope_key=(matching_execution.scope_key if matching_execution is not None else ""),
                config={
                    "projection_version": frontier.version,
                    "config_hash": (
                        matching_execution.config_hash if matching_execution is not None else ""
                    ),
                },
                maximum=1,
                force_refresh=force_refresh,
            )
        except Exception as exc:
            outcomes.append(
                ScannerRuntimeOutcome(
                    scanner_id=scanner_id,
                    state="failed",
                    error=_bounded_error(exc, runtime_limits),
                )
            )
            continue
        if candidate_id in claim["reused_candidate_ids"]:
            execution_id = claim["reused_executions"].get(candidate_id)
            if execution_id is not None:
                company_key = _execution_company(store, execution_id) or company_key
            outcomes.append(
                ScannerRuntimeOutcome(
                    scanner_id=scanner_id,
                    state="reused",
                    execution_id=execution_id,
                )
            )
            continue
        claim_state = _unclaimed_state(claim, int(candidate_id))
        if claim_state is not None:
            outcomes.append(ScannerRuntimeOutcome(scanner_id=scanner_id, state=claim_state))
            continue
        execution_id = claim["claimed"].get(int(candidate_id))
        if execution_id is None:
            outcomes.append(ScannerRuntimeOutcome(scanner_id=scanner_id, state="deferred"))
            continue
        execution_identity = _execution_summary(store, execution_id)
        canonical_company = execution_identity.company_key if execution_identity else ""
        if execution_identity is None or not canonical_company:
            _settle_runtime_error(
                store,
                execution_id,
                "scanner execution has no canonical company identity",
                status="identity_ambiguous",
            )
            outcomes.append(
                ScannerRuntimeOutcome(
                    scanner_id=scanner_id,
                    state="failed",
                    execution_id=execution_id,
                    terminal_status="identity_ambiguous",
                    error="scanner execution has no canonical company identity",
                )
            )
            continue
        company_key = canonical_company
        try:
            # Group construction intentionally occurs only after the durable claim.
            group = group_frontier_by_scanner(
                frontier,
                scanner_ids=[scanner_id],
            ).get(scanner_id)
            if group is None:
                raise ValueError("projection has no scanner-owned payload")
            normalized = _normalize_scanner_result(
                group,
                scanner_id=scanner_id,
                scanner_version=scanner_version,
                execution_id=execution_id,
                execution_identity=execution_identity,
                candidate_id=int(candidate_id),
                company_key=canonical_company,
                request_count=0,
                byte_count=0,
                limits=runtime_limits,
                force_partial=False,
                warnings=(),
            )
            _persist_schedule_decisions(
                store,
                normalized.frontier.schedule_decisions,
                candidate_id=int(candidate_id),
                execution_id=execution_id,
            )
            persisted = _persist_normalized_result(store, execution_id, normalized)
            outcomes.append(
                ScannerRuntimeOutcome(
                    scanner_id=scanner_id,
                    state="completed",
                    execution_id=execution_id,
                    terminal_status=normalized.summary.status,
                    coverage_count=int(persisted["coverage_count"]),
                    artifact_count=int(persisted["artifact_count"]),
                    observation_count=int(persisted["observation_count"]),
                    error="; ".join(dict.fromkeys(
                        [*normalized.summary.errors, *normalized.warnings]
                    ))[:runtime_limits.max_error_characters],
                )
            )
        except Exception as exc:
            error = _bounded_error(exc, runtime_limits)
            _persist_error_decision(
                store,
                scanner_id=scanner_id,
                company_key=canonical_company,
                candidate_id=int(candidate_id),
                execution_id=execution_id,
                error=error,
            )
            _settle_runtime_error(store, execution_id, error)
            outcomes.append(
                ScannerRuntimeOutcome(
                    scanner_id=scanner_id,
                    state="failed",
                    execution_id=execution_id,
                    terminal_status="transient_error",
                    error=error,
                )
            )
    if not company_key:
        company_key = _projection_company_key(frontier)
    hydrated = store.frontier_intelligence(company_key)
    return FrontierRuntimeReport(
        candidate_id=int(candidate_id),
        company_key=company_key,
        outcomes=tuple(outcomes),
        frontier=hydrated,
    )


async def run_frontier_runtime(
    store: LeadStore,
    candidate_id: int,
    context: FrontierScannerContext,
    *,
    scanner_ids: Sequence[str] | None = None,
    run_id: int | None = None,
    ssrf_safe_bounded_fetch: BoundedFetch | None = None,
    force_refresh: bool = False,
    limits: FrontierRuntimeLimits | None = None,
    hydrate_report: bool = True,
) -> FrontierRuntimeReport:
    """Claim, run, persist, and hydrate independent frontier scanner executions.

    Every scanner lease is committed before HTML parsing or optional fetch work. One
    scanner exception is converted to its own retryable failure and never aborts the
    remaining scanners. A 90-day reusable claim performs no parser or fetch work.
    """
    runtime_limits = limits or FrontierRuntimeLimits()
    selected, invalid = _selected_scanners(scanner_ids, runtime_limits)
    adapter = (
        make_frontier_fetch_adapter(
            ssrf_safe_bounded_fetch,
            max_requests=runtime_limits.max_fetch_requests,
            max_response_bytes=runtime_limits.max_fetch_response_bytes,
            max_total_bytes=runtime_limits.max_fetch_total_bytes,
        )
        if ssrf_safe_bounded_fetch is not None
        else None
    )
    outcomes: list[ScannerRuntimeOutcome] = [
        ScannerRuntimeOutcome(
            scanner_id=scanner_id,
            state="invalid",
            error="unknown frontier scanner",
        )
        for scanner_id in invalid
    ]
    company_key = _context_company_key(context)

    for scanner_id in selected:
        definition = SCANNER_REGISTRY[scanner_id]
        try:
            claim = store.claim_scanner_work(
                [int(candidate_id)],
                scanner_id=scanner_id,
                scanner_version=definition.version,
                run_id=run_id,
                extractor_version=definition.extractor_version,
                scope_key=context.primary_url,
                config={
                    "cost_class": definition.cost_class,
                    "max_requests": definition.max_requests,
                    "max_response_bytes": definition.max_response_bytes,
                    "ttl_seconds": definition.ttl_seconds,
                },
                maximum=1,
                force_refresh=force_refresh,
            )
        except Exception as exc:
            outcomes.append(
                ScannerRuntimeOutcome(
                    scanner_id=scanner_id,
                    state="failed",
                    error=_bounded_error(exc, runtime_limits),
                )
            )
            continue

        if candidate_id in claim["reused_candidate_ids"]:
            execution_id = claim["reused_executions"].get(candidate_id)
            if execution_id is not None:
                company_key = _execution_company(store, execution_id) or company_key
            outcomes.append(
                ScannerRuntimeOutcome(
                    scanner_id=scanner_id,
                    state="reused",
                    execution_id=execution_id,
                )
            )
            continue
        claim_state = _unclaimed_state(claim, int(candidate_id))
        if claim_state is not None:
            outcomes.append(ScannerRuntimeOutcome(scanner_id=scanner_id, state=claim_state))
            continue

        execution_id = claim["claimed"].get(int(candidate_id))
        if execution_id is None:
            outcomes.append(
                ScannerRuntimeOutcome(
                    scanner_id=scanner_id,
                    state="deferred",
                    error="scanner claim returned no execution",
                )
            )
            continue
        execution_identity = _execution_summary(store, execution_id)
        canonical_company = execution_identity.company_key if execution_identity else ""
        if execution_identity is None or not canonical_company:
            _settle_runtime_error(
                store,
                execution_id,
                "scanner execution has no canonical company identity",
                status="identity_ambiguous",
            )
            outcomes.append(
                ScannerRuntimeOutcome(
                    scanner_id=scanner_id,
                    state="failed",
                    execution_id=execution_id,
                    terminal_status="identity_ambiguous",
                    error="scanner execution has no canonical company identity",
                )
            )
            continue
        company_key = canonical_company

        requests_before = adapter.requests if adapter is not None else 0
        bytes_before = adapter.bytes_received if adapter is not None else 0
        try:
            bounded_context, context_capped, context_warnings = _bounded_context(
                context,
                company_key=canonical_company,
                limits=runtime_limits,
            )
            # Only the constellation parser consumes saved frontier observations. Other
            # scanners use their documents/records/URLs, so rebuilding the entire company
            # ledger here duplicates the complete post-publication report read below.
            if "frontier_observations" in definition.applicability_inputs:
                bounded_context = replace(
                    bounded_context,
                    metadata={
                        **bounded_context.metadata,
                        "frontier": store.frontier_intelligence(canonical_company),
                    },
                )
            raw = await run_scanner(scanner_id, bounded_context, adapter)
            request_count = adapter.requests - requests_before if adapter is not None else 0
            byte_count = adapter.bytes_received - bytes_before if adapter is not None else 0
            grouped = group_frontier_by_scanner(raw, scanner_ids=[scanner_id])
            group = grouped.get(scanner_id)
            if group is None:
                raise ValueError("scanner returned no scanner-owned execution or evidence")
            normalized = _normalize_scanner_result(
                group,
                scanner_id=scanner_id,
                scanner_version=definition.version,
                execution_id=execution_id,
                execution_identity=execution_identity,
                candidate_id=int(candidate_id),
                company_key=canonical_company,
                request_count=request_count,
                byte_count=byte_count,
                limits=runtime_limits,
                force_partial=context_capped,
                warnings=context_warnings,
            )
            _persist_schedule_decisions(
                store,
                normalized.frontier.schedule_decisions,
                candidate_id=int(candidate_id),
                execution_id=execution_id,
            )
            persisted = _persist_normalized_result(
                store,
                execution_id,
                normalized,
            )
            outcomes.append(
                ScannerRuntimeOutcome(
                    scanner_id=scanner_id,
                    state="completed",
                    execution_id=execution_id,
                    terminal_status=normalized.summary.status,
                    coverage_count=int(persisted["coverage_count"]),
                    artifact_count=int(persisted["artifact_count"]),
                    observation_count=int(persisted["observation_count"]),
                    error="; ".join(dict.fromkeys(
                        [*normalized.summary.errors, *normalized.warnings]
                    ))[:runtime_limits.max_error_characters],
                )
            )
        except asyncio.CancelledError:
            # Deadlines/pause must release the committed scanner claim before
            # sibling workers drain or the store closes. Prior evidence survives.
            _settle_runtime_error(
                store, execution_id, "Scanner interrupted; unfinished work remains retryable",
                request_count=(adapter.requests - requests_before if adapter is not None else 0),
                byte_count=(adapter.bytes_received - bytes_before if adapter is not None else 0),
            )
            raise
        except Exception as exc:
            error = _bounded_error(exc, runtime_limits)
            _persist_error_decision(
                store,
                scanner_id=scanner_id,
                company_key=canonical_company,
                candidate_id=int(candidate_id),
                execution_id=execution_id,
                error=error,
            )
            _settle_runtime_error(
                store,
                execution_id,
                error,
                request_count=(adapter.requests - requests_before if adapter is not None else 0),
                byte_count=(adapter.bytes_received - bytes_before if adapter is not None else 0),
            )
            outcomes.append(
                ScannerRuntimeOutcome(
                    scanner_id=scanner_id,
                    state="failed",
                    execution_id=execution_id,
                    terminal_status="transient_error",
                    error=error,
                )
            )

    # A batch owner can publish each check without repeatedly materializing a report
    # it will discard. Dependency reads above remain fresh; that owner must hydrate
    # the complete final company projection before returning its own result.
    frontier = (
        store.frontier_intelligence(company_key) if hydrate_report else FrontierIntelligence()
    )
    return FrontierRuntimeReport(
        candidate_id=int(candidate_id),
        company_key=company_key,
        outcomes=tuple(outcomes),
        frontier=frontier,
        fetch_requests=adapter.requests if adapter is not None else 0,
        fetch_bytes=adapter.bytes_received if adapter is not None else 0,
        target_fetch_requests=adapter.target_requests if adapter is not None else 0,
        target_fetch_bytes=(adapter.target_bytes_received if adapter is not None else 0),
        auxiliary_fetch_requests=(adapter.auxiliary_requests if adapter is not None else 0),
        auxiliary_fetch_bytes=(adapter.auxiliary_bytes_received if adapter is not None else 0),
    )


def _public_http_url(raw: str) -> str:
    if is_disallowed(raw):
        raise FrontierFetchPolicyError("frontier fetch domain is disallowed")
    candidate = raw.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise FrontierFetchPolicyError("frontier fetch requires an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise FrontierFetchPolicyError("frontier fetch URLs cannot contain credentials")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").casefold().rstrip(".")
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise FrontierFetchPolicyError("frontier fetch URL has an invalid host") from exc
    if host == "localhost" or host.endswith(_FORBIDDEN_HOST_SUFFIXES) or "." not in host:
        raise FrontierFetchPolicyError("frontier fetch URL has a non-public host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise FrontierFetchPolicyError("frontier fetch URL has a non-public address")
    if port is not None and port not in {80, 443}:
        raise FrontierFetchPolicyError("frontier fetch URL uses a disallowed port")
    return parsed._replace(netloc=f"{host}:{port}" if port else host, fragment="").geturl()


def _selected_scanners(
    scanner_ids: Sequence[str] | None,
    limits: FrontierRuntimeLimits,
) -> tuple[list[str], list[str]]:
    raw = list(SCANNER_REGISTRY) if scanner_ids is None else list(scanner_ids)
    normalized = list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))
    selected = [item for item in normalized if item in SCANNER_REGISTRY][: limits.max_scanners]
    invalid = [item for item in normalized if item not in SCANNER_REGISTRY]
    invalid.extend(item for item in normalized[limits.max_scanners :] if item in SCANNER_REGISTRY)
    return selected, list(dict.fromkeys(invalid))


def _projection_scanner_ids(frontier: FrontierIntelligence) -> list[str]:
    return list(
        dict.fromkeys(
            item.scanner_id
            for collection in (
                frontier.executions,
                frontier.coverage_manifests,
                frontier.evidence_artifacts,
                frontier.observations,
                frontier.schedule_decisions,
            )
            for item in collection
            if item.scanner_id
        )
    )


def _projection_versions(
    frontier: FrontierIntelligence,
    scanner_id: str,
) -> list[str]:
    return list(
        dict.fromkeys(
            item.scanner_version
            for collection in (
                frontier.executions,
                frontier.coverage_manifests,
                frontier.evidence_artifacts,
                frontier.observations,
                frontier.schedule_decisions,
            )
            for item in collection
            if item.scanner_id == scanner_id and item.scanner_version
        )
    )


def _projection_company_key(frontier: FrontierIntelligence) -> str:
    for collection in (
        frontier.executions,
        frontier.evidence_artifacts,
        frontier.observations,
        frontier.schedule_decisions,
    ):
        for item in collection:
            value = item.company_key.strip().casefold().rstrip(".")
            if value:
                return value.removeprefix("www.")
    return ""


def _context_company_key(context: FrontierScannerContext) -> str:
    value = context.company_key.strip().casefold().rstrip(".")
    if "://" in value:
        value = urlsplit(value).hostname or ""
    return value.removeprefix("www.")


def _execution_company(store: LeadStore, execution_id: int) -> str:
    summary = _execution_summary(store, execution_id)
    return summary.company_key if summary is not None else ""


def _execution_summary(
    store: LeadStore,
    execution_id: int,
) -> ScannerExecutionSummary | None:
    execution = store.scanner_execution(execution_id, include_evidence=False)
    return execution["summary"] if execution is not None else None


def _unclaimed_state(
    claim: Mapping[str, Any],
    candidate_id: int,
) -> Literal["active", "not_due", "deduplicated", "deferred"] | None:
    for key, state in (
        ("active_candidate_ids", "active"),
        ("not_due_candidate_ids", "not_due"),
        ("deduplicated_candidate_ids", "deduplicated"),
        ("deferred_candidate_ids", "deferred"),
    ):
        if candidate_id in claim[key]:
            return state  # type: ignore[return-value]
    return None


def _bounded_context(
    context: FrontierScannerContext,
    *,
    company_key: str,
    limits: FrontierRuntimeLimits,
) -> tuple[FrontierScannerContext, bool, tuple[str, ...]]:
    warnings: list[str] = []
    documents: list[FrontierDocument] = []
    if len(context.documents) > limits.max_context_documents:
        warnings.append("context document cap reached")
    for document in context.documents[: limits.max_context_documents]:
        body = document.body
        if len(body) > limits.max_document_bytes:
            body = body[: limits.max_document_bytes]
            warnings.append("context document byte cap reached")
        documents.append(replace(document, content=body))
    if len(context.records) > limits.max_context_records:
        warnings.append("context record cap reached")
    if len(context.request_urls) > limits.max_request_urls:
        warnings.append("context request-URL cap reached")
    return (
        replace(
            context,
            company_key=company_key,
            documents=tuple(documents),
            records=tuple(context.records[: limits.max_context_records]),
            request_urls=tuple(context.request_urls[: limits.max_request_urls]),
        ),
        bool(warnings),
        tuple(dict.fromkeys(warnings)),
    )


def _normalize_scanner_result(
    frontier: FrontierIntelligence,
    *,
    scanner_id: str,
    scanner_version: str,
    execution_id: int,
    execution_identity: ScannerExecutionSummary,
    candidate_id: int,
    company_key: str,
    request_count: int,
    byte_count: int,
    limits: FrontierRuntimeLimits,
    force_partial: bool,
    warnings: Sequence[str],
) -> _NormalizedScannerResult:
    capped_warnings = list(warnings)
    coverage = frontier.coverage_manifests[: limits.max_coverage_per_scanner]
    artifacts = frontier.evidence_artifacts[: limits.max_artifacts_per_scanner]
    observations = frontier.observations[: limits.max_observations_per_scanner]
    schedules = frontier.schedule_decisions[: limits.max_schedule_decisions_per_scanner]
    capped = force_partial
    for collection, maximum, label in (
        (frontier.coverage_manifests, limits.max_coverage_per_scanner, "coverage"),
        (frontier.evidence_artifacts, limits.max_artifacts_per_scanner, "artifact"),
        (frontier.observations, limits.max_observations_per_scanner, "observation"),
        (
            frontier.schedule_decisions,
            limits.max_schedule_decisions_per_scanner,
            "schedule-decision",
        ),
    ):
        if len(collection) > maximum:
            capped = True
            capped_warnings.append(f"{label} cap reached")

    normalized_artifacts = [
        item.model_copy(
            update={
                "company_key": company_key,
                "scanner_id": scanner_id,
                "scanner_version": scanner_version,
            }
        )
        for item in artifacts
    ]
    # Parsers may emit the exact same artifact for several facts. Keep every
    # distinct extraction/provenance record; collapse only identical records.
    unique_artifacts: dict[str, EvidenceArtifact] = {}
    for artifact in normalized_artifacts:
        identity = json.dumps(artifact.model_dump(mode="json"), sort_keys=True)
        unique_artifacts.setdefault(identity, artifact)
    normalized_artifacts = list(unique_artifacts.values())
    artifact_keys = {item.artifact_key for item in normalized_artifacts}
    normalized_coverage = [
        item.model_copy(
            update={
                "scanner_id": scanner_id,
                "scanner_version": scanner_version,
            }
        )
        for item in coverage
    ]
    coverage_keys = {item.manifest_key for item in normalized_coverage}
    normalized_observations: list[AtomicObservation] = []
    evidence_gap = False
    lineage_gap_details: list[str] = []
    for item in observations:
        retained_artifacts = [key for key in item.artifact_keys if key in artifact_keys]
        retained_coverage = [key for key in item.coverage_keys if key in coverage_keys]
        if not retained_artifacts or not retained_coverage:
            evidence_gap = True
            if len(lineage_gap_details) < 5:
                lineage_gap_details.append(
                    f"Lineage gap for {item.predicate[:120]}: "
                    f"artifacts {len(retained_artifacts)}/{len(item.artifact_keys)}; "
                    f"coverage {len(retained_coverage)}/{len(item.coverage_keys)}"
                )
        normalized_observations.append(
            item.model_copy(
                update={
                    "company_key": company_key,
                    "scanner_id": scanner_id,
                    "scanner_version": scanner_version,
                    "artifact_keys": retained_artifacts,
                    "coverage_keys": retained_coverage,
                    "score_eligible": bool(
                        item.score_eligible and retained_artifacts and retained_coverage
                    ),
                }
            )
        )
    # A scanner can encounter the same fact on several fetched surfaces. Retain
    # all provenance without attempting duplicate execution/key inserts.
    grouped: dict[str, dict[str, AtomicObservation]] = {}
    for observation in normalized_observations:
        semantic = observation.model_dump(mode="json", exclude={
            "artifact_keys", "coverage_keys", "first_observed_at", "last_observed_at",
        })
        fingerprint = hashlib.sha256(
            json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        variants = grouped.setdefault(observation.observation_key, {})
        existing = variants.get(fingerprint)
        if existing is None:
            variants[fingerprint] = observation
        else:
            artifacts_union = list(dict.fromkeys(
                [*existing.artifact_keys, *observation.artifact_keys]
            ))
            coverage_union = list(dict.fromkeys(
                [*existing.coverage_keys, *observation.coverage_keys]
            ))
            if len(artifacts_union) > 20 or len(coverage_union) > 20:
                # Keep a separate provenance variant rather than violating model bounds.
                provenance = hashlib.sha256(json.dumps({
                    "semantic": semantic, "artifacts": sorted(observation.artifact_keys),
                    "coverage": sorted(observation.coverage_keys),
                }, sort_keys=True).encode()).hexdigest()
                prior = variants.get(provenance, observation)
                variants[provenance] = prior.model_copy(update={
                    "first_observed_at": min(
                        prior.first_observed_at, observation.first_observed_at
                    ),
                    "last_observed_at": max(prior.last_observed_at, observation.last_observed_at),
                })
                continue
            variants[fingerprint] = existing.model_copy(update={
                "artifact_keys": artifacts_union,
                "coverage_keys": coverage_union,
                "first_observed_at": min(existing.first_observed_at, observation.first_observed_at),
                "last_observed_at": max(existing.last_observed_at, observation.last_observed_at),
            })
    normalized_observations = [
        observation.model_copy(update={"observation_key": f"{key}:{fingerprint}"})
        if len(variants) > 1 else observation
        for key, variants in grouped.items()
        for fingerprint, observation in variants.items()
    ]
    if evidence_gap:
        capped = True
        capped_warnings.append("observation evidence lineage is incomplete")
        capped_warnings.extend(lineage_gap_details)
    normalized_schedules = [
        item.model_copy(
            update={
                "company_key": company_key,
                "scanner_id": scanner_id,
                "scanner_version": scanner_version,
            }
        )
        for item in schedules
    ]
    lineage = _extracted_from_lineage(
        normalized_observations,
        normalized_artifacts,
        limit=limits.max_lineage_edges_per_scanner,
    )
    if sum(len(item.artifact_keys) for item in normalized_observations) > len(lineage):
        capped = True
        capped_warnings.append("lineage cap reached")
    if capped:
        normalized_coverage = [
            item.model_copy(
                update={
                    "completeness": "partial" if item.attempted_units else "none",
                    "truncated": True,
                    "absence_interpretable": False,
                    "limitations": list(dict.fromkeys([*item.limitations, *capped_warnings]))[:20],
                }
            )
            for item in normalized_coverage
        ]
    summary = _aggregate_summary(
        frontier.executions,
        scanner_id=scanner_id,
        scanner_version=scanner_version,
        execution_id=execution_id,
        execution_identity=execution_identity,
        candidate_id=candidate_id,
        company_key=company_key,
        coverage=normalized_coverage,
        observations=normalized_observations,
        artifacts=normalized_artifacts,
        request_count=request_count,
        byte_count=byte_count,
        capped=capped,
        warnings=capped_warnings,
    )
    if not normalized_schedules:
        definition = SCANNER_REGISTRY[scanner_id]
        normalized_schedules.append(
            ScannerScheduleDecision(
                company_key=company_key,
                scanner_id=scanner_id,
                scanner_version=scanner_version,
                applicable=summary.status != "not_applicable",
                applicability_reason="Scanner produced no explicit schedule decision.",
                success_probability=definition.success_probability,
                evidence_value=definition.evidence_value,
                cost_penalty=min(1.0, definition.max_requests / 20),
                expected_information_gain=0.0,
                selected=True,
                selection_reason="frontier runtime execution",
                propensity=1.0,
            )
        )
    normalized = FrontierIntelligence(
        version=frontier.version,
        executions=[summary],
        coverage_manifests=normalized_coverage,
        evidence_artifacts=normalized_artifacts,
        observations=normalized_observations,
        schedule_decisions=normalized_schedules,
        features=frontier.features,
        updated_at=frontier.updated_at,
    )
    return _NormalizedScannerResult(
        frontier=normalized,
        summary=summary,
        lineage=tuple(lineage),
        capped=capped,
        warnings=tuple(dict.fromkeys(capped_warnings)),
    )


def _aggregate_summary(
    executions: Sequence[ScannerExecutionSummary],
    *,
    scanner_id: str,
    scanner_version: str,
    execution_id: int,
    execution_identity: ScannerExecutionSummary,
    candidate_id: int,
    company_key: str,
    coverage: Sequence[ScannerCoverageManifest],
    observations: Sequence[AtomicObservation],
    artifacts: Sequence[EvidenceArtifact],
    request_count: int,
    byte_count: int,
    capped: bool,
    warnings: Sequence[str],
) -> ScannerExecutionSummary:
    now = datetime.now(UTC)
    if executions:
        base = min(executions, key=lambda item: item.started_at)
        statuses = {item.status for item in executions}
        started_at = execution_identity.started_at
        finished_values = [item.finished_at for item in executions if item.finished_at]
        finished_at = max(finished_values) if finished_values else now
        due_values = [item.next_due_at for item in executions if item.next_due_at]
        next_due_at = min(due_values) if due_values else None
        errors = list(
            dict.fromkeys([error for item in executions for error in item.errors] + list(warnings))
        )[:20]
        total_requests = max(request_count, sum(item.request_count for item in executions))
        total_bytes = max(byte_count, sum(item.byte_count for item in executions))
        token_count = sum(item.token_count for item in executions)
        cost_microusd = sum(item.cost_microusd for item in executions)
        extractor_version = execution_identity.extractor_version
        scope_key = execution_identity.scope_key
        config_hash = execution_identity.config_hash
        metadata = {
            **base.metadata,
            "merged_scope_keys": list(
                dict.fromkeys(item.scope_key for item in executions if item.scope_key)
            )[:20],
        }
    else:
        statuses = set()
        started_at = execution_identity.started_at
        finished_at = now
        next_due_at = None
        errors = list(dict.fromkeys(warnings))[:20]
        total_requests = request_count
        total_bytes = byte_count
        token_count = 0
        cost_microusd = 0
        extractor_version = execution_identity.extractor_version
        scope_key = execution_identity.scope_key
        config_hash = execution_identity.config_hash
        metadata = {}

    status = _terminal_status(
        statuses,
        coverage=coverage,
        observations=observations,
        capped=capped,
    )
    return ScannerExecutionSummary(
        execution_id=execution_id,
        candidate_id=candidate_id,
        company_key=company_key,
        scanner_id=scanner_id,
        scanner_version=scanner_version,
        extractor_version=extractor_version,
        scope_key=scope_key,
        config_hash=config_hash,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        next_due_at=next_due_at,
        request_count=total_requests,
        byte_count=total_bytes,
        token_count=token_count,
        cost_microusd=cost_microusd,
        artifact_keys=[item.artifact_key for item in artifacts][:100],
        coverage_keys=[item.manifest_key for item in coverage][:100],
        observation_count=len(observations),
        errors=errors,
        metadata=metadata,
    )


def _terminal_status(
    statuses: set[str],
    *,
    coverage: Sequence[ScannerCoverageManifest],
    observations: Sequence[AtomicObservation],
    capped: bool,
) -> str:
    if capped or "partial" in statuses:
        return "partial"
    failures = statuses & SCANNER_FAILURE_STATUSES
    reusable = statuses & SCANNER_REUSABLE_STATUSES
    if failures:
        if reusable or observations:
            return "partial"
        for status in (
            "identity_ambiguous",
            "robots_blocked",
            "rate_limited",
            "transient_error",
            "failed",
        ):
            if status in failures:
                return status
    if not coverage:
        return "failed"
    safe_coverage = all(
        item.completeness in {"complete", "not_applicable"}
        and not item.truncated
        and not item.failed_units
        for item in coverage
    )
    if not safe_coverage:
        return "partial"
    if observations:
        return "success"
    if all(item.completeness == "not_applicable" for item in coverage):
        return "not_applicable"
    if all(item.absence_interpretable for item in coverage):
        return "success_empty"
    return "partial"


def _extracted_from_lineage(
    observations: Sequence[AtomicObservation],
    artifacts: Sequence[EvidenceArtifact],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    artifact_keys = {item.artifact_key for item in artifacts}
    artifacts_by_url: dict[str, str] = {}
    for artifact in artifacts:
        for url in (artifact.canonical_url, artifact.original_url):
            if url:
                artifacts_by_url.setdefault(url, artifact.artifact_key)
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for observation in observations:
        keys = [key for key in observation.artifact_keys if key in artifact_keys]
        if not keys and observation.source_url in artifacts_by_url:
            keys = [artifacts_by_url[observation.source_url]]
        for artifact_key in keys:
            identity = observation.observation_key, artifact_key
            if identity in seen:
                continue
            seen.add(identity)
            edges.append(
                {
                    "subject_observation_key": observation.observation_key,
                    "relationship_type": "extracted_from",
                    "artifact_key": artifact_key,
                    "details": {"source_url": observation.source_url},
                }
            )
            if len(edges) >= limit:
                return edges
    return edges


def _persist_schedule_decisions(
    store: LeadStore,
    decisions: Sequence[ScannerScheduleDecision],
    *,
    candidate_id: int,
    execution_id: int,
) -> None:
    for decision in decisions:
        store.record_scanner_schedule_decision(
            decision,
            candidate_id=candidate_id,
            execution_id=execution_id,
        )


def _persist_normalized_result(
    store: LeadStore,
    execution_id: int,
    result: _NormalizedScannerResult,
) -> dict[str, Any]:
    frontier = result.frontier
    keyword = {
        "coverage_manifests": frontier.coverage_manifests,
        "artifacts": frontier.evidence_artifacts,
        "observations": frontier.observations,
        "lineage": list(result.lineage),
        "next_due_at": result.summary.next_due_at,
    }
    if result.summary.status in SCANNER_REUSABLE_STATUSES:
        return store.complete_scanner_execution(
            execution_id,
            result.summary,
            **keyword,
        )
    if result.summary.status not in SCANNER_FAILURE_STATUSES:
        raise ValueError(f"unsupported terminal scanner status: {result.summary.status}")
    # Every failed method receives a bounded retry window. In particular, robots denials and
    # explicit throttles must not be hammered by successive scheduler passes.
    retry_due = result.summary.next_due_at
    return store.fail_scanner_execution(
        execution_id,
        status=result.summary.status,
        error="; ".join(result.summary.errors),
        summary=result.summary,
        **{**keyword, "next_due_at": retry_due},
    )


def _persist_error_decision(
    store: LeadStore,
    *,
    scanner_id: str,
    company_key: str,
    candidate_id: int,
    execution_id: int,
    error: str,
) -> None:
    definition = SCANNER_REGISTRY[scanner_id]
    try:
        store.record_scanner_schedule_decision(
            ScannerScheduleDecision(
                company_key=company_key,
                scanner_id=scanner_id,
                scanner_version=definition.version,
                applicable=True,
                applicability_reason="Scanner was claimed before runtime failure.",
                success_probability=definition.success_probability,
                evidence_value=definition.evidence_value,
                cost_penalty=min(1.0, definition.max_requests / 20),
                expected_information_gain=0.0,
                selected=True,
                selection_reason="runtime claim",
                propensity=1.0,
                metadata={"runtime_error": error},
            ),
            candidate_id=candidate_id,
            execution_id=execution_id,
        )
    except Exception:
        return


def _settle_runtime_error(
    store: LeadStore,
    execution_id: int,
    error: str,
    *,
    status: str = "transient_error",
    request_count: int = 0,
    byte_count: int = 0,
) -> None:
    try:
        execution = store.scanner_execution(execution_id, include_evidence=False)
        if execution is None or execution["summary"].status != "running":
            return
        if status not in SCANNER_FAILURE_STATUSES:
            status = "transient_error"
        running = execution["summary"]
        summary = running.model_copy(
            update={
                "status": status,
                "finished_at": datetime.now(UTC),
                "request_count": max(running.request_count, int(request_count)),
                "byte_count": max(running.byte_count, int(byte_count)),
                "errors": list(dict.fromkeys([*running.errors, error]))[:20],
            }
        )
        store.fail_scanner_execution(
            execution_id,
            status=status,  # type: ignore[arg-type]
            error=error,
            summary=summary,
        )
    except Exception:
        return


def _bounded_error(exc: Exception, limits: FrontierRuntimeLimits) -> str:
    message = re.sub(r"\s+", " ", str(exc)).strip()
    message = re.sub(r"(?i)https?://[^\s/@]+:[^\s/@]+@", "https://", message)
    return f"{type(exc).__name__}: {message}"[: limits.max_error_characters]


__all__ = [
    "BoundedFetch",
    "FRONTIER_AUXILIARY_BYTE_COUNT_HEADER",
    "FRONTIER_AUXILIARY_REQUEST_COUNT_HEADER",
    "FrontierFetchAdapter",
    "FrontierFetchBudgetExceeded",
    "FrontierFetchPolicyError",
    "FrontierRuntimeLimits",
    "FrontierRuntimeReport",
    "ScannerRuntimeOutcome",
    "group_frontier_by_scanner",
    "make_frontier_fetch_adapter",
    "persist_frontier_projection",
    "run_frontier_runtime",
]
