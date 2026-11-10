from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import urllib.robotparser
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import parse_qs, parse_qsl, urlsplit, urlunsplit

import httpx

from .company_research import (
    PUBLIC_FETCH_SECONDS,
    PUBLIC_NETWORK_SCANNERS,
    CompanyResearchBudget,
    CompanyResearchDeferred,
    substantive_failure,
)
from .config import USER_AGENT
from .frontier_intelligence import (
    SCANNER_DEFINITIONS,
    SCANNER_REGISTRY,
    FrontierDocument,
    FrontierScannerContext,
    classify_frontier_surface_owner,
    is_broad_social_research_url,
    is_frontier_asset_url,
    is_frontier_request_target,
    is_frontier_utility_url,
    merge_frontier,
)
from .frontier_runtime import (
    FRONTIER_AUXILIARY_BYTE_COUNT_HEADER,
    FRONTIER_AUXILIARY_REQUEST_COUNT_HEADER,
    FrontierFetchPolicyError,
    FrontierRuntimeReport,
    persist_frontier_projection,
    run_frontier_runtime,
)
from .inspect import _bounded_get
from .models import (
    FRONTIER_SCANNER_IDS,
    DiscoveredHost,
    FrontierIntelligence,
    ScannerScheduleDecision,
    SiteObservation,
)
from .provider_rate_limit import REQUEST_ACTIVITY
from .rehydration import REHYDRATION_INTERVAL
from .retrieval_policy import scanner_host_hold_seconds
from .revenue_intelligence import schedule_scanners_by_expected_information_gain
from .store import (
    SCANNER_FAILURE_STATUSES,
    SCANNER_REUSABLE_STATUSES,
    SEO_READINESS_SCANNER_ID,
    LeadStore,
)
from .value_utils import mapping_or_empty as _mapping

FRONTIER_COMPANY_WORKERS = 6
FRONTIER_SCANNER_WALL_SECONDS = 90.0
FRONTIER_HOST_FAILURE_COOLDOWN_SECONDS = 300.0

_NETWORK_SCANNERS = set(PUBLIC_NETWORK_SCANNERS)
_LOCAL_HTML_SCANNERS = {
    "creative_asset_genome_v1",
    "localization_burden_v1",
    "ai_artifact_provenance_v1",
    "growth_measurement_schema_v1",
    "seo_visibility_engine_v1",
}
_SHARED_SCANNERS = {"dol_lca_workforce_v1", "sec_form_d_capital_v1"}
_CONSTELLATION_SCANNERS = {"sf_ai_capital_constellation_v1"}
_ORCHESTRATION_STRATEGIES = (
    _NETWORK_SCANNERS,
    _LOCAL_HTML_SCANNERS,
    _SHARED_SCANNERS,
    _CONSTELLATION_SCANNERS,
)
if set(SCANNER_REGISTRY) != set().union(*_ORCHESTRATION_STRATEGIES) or sum(
    len(strategy) for strategy in _ORCHESTRATION_STRATEGIES
) != len(SCANNER_REGISTRY):
    raise RuntimeError("every registered company scanner needs one orchestration strategy")
_SF_RE = re.compile(
    r"\b(?:san francisco|sf,?\s+ca|bay area|oakland|berkeley|san mateo|"
    r"redwood city|palo alto|menlo park|mountain view|south san francisco)\b",
    re.I,
)


class FrontierRecordSource(Protocol):
    """Exact-name, local-only records supplied by a shared dataset adapter."""

    def records_for(
        self,
        scanner_id: str,
        legal_name: str,
    ) -> Sequence[Mapping[str, Any]]: ...


@dataclass(slots=True)
class FrontierTarget:
    candidate_id: int
    candidate: DiscoveredHost
    observation: SiteObservation
    company_name: str = ""
    current_action: str = "research_next"
    boundary_score: int = 0
    persist_embedded: bool = False


@dataclass(frozen=True, slots=True)
class FrontierBatchSummary:
    targets: int = 0
    embedded_groups: int = 0
    embedded_completed: int = 0
    embedded_reused: int = 0
    schedule_decisions: int = 0
    optional_selected: int = 0
    optional_completed: int = 0
    optional_partial: int = 0
    optional_reused: int = 0
    optional_deferred: int = 0
    optional_failed: int = 0
    fetch_requests: int = 0
    fetch_bytes: int = 0
    selected_scanners: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    error_hosts: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "targets": self.targets,
            "embedded_groups": self.embedded_groups,
            "embedded_completed": self.embedded_completed,
            "embedded_reused": self.embedded_reused,
            "schedule_decisions": self.schedule_decisions,
            "optional_selected": self.optional_selected,
            "optional_completed": self.optional_completed,
            "optional_partial": self.optional_partial,
            "optional_reused": self.optional_reused,
            "optional_deferred": self.optional_deferred,
            "optional_failed": self.optional_failed,
            "fetch_requests": self.fetch_requests,
            "fetch_bytes": self.fetch_bytes,
            "selected_scanners": list(self.selected_scanners),
            "errors": list(self.errors),
            "error_hosts": list(self.error_hosts),
        }


@dataclass(slots=True)
class _PreparedTarget:
    target: FrontierTarget
    company_key: str
    embedded: FrontierIntelligence
    current: FrontierIntelligence
    discovery: Mapping[str, Any]
    legal_name: str
    locations: tuple[str, ...]
    phone: str
    company_name: str
    request_urls: dict[str, tuple[str, ...]]
    request_sources: dict[str, str]
    request_bases: dict[str, str]
    request_evidence_urls: dict[str, str]
    app_ids: tuple[str, ...]
    records: dict[str, tuple[Mapping[str, Any], ...]]
    recent_reusable_scanners: frozenset[str]
    scanner_guards: dict[str, str]
    seo_admitted: bool = False
    network_held: bool = False
    decisions: tuple[Any, ...] = ()
    selected_scanner: str = ""
    selected_eig: float = 0.0


@dataclass(slots=True)
class _FrontierHttpFetcher:
    client: httpx.AsyncClient
    scanner_id: str
    company_key: str
    primary_url: str
    allowed_urls: set[str]
    request_sources: Mapping[str, str]
    url_owners: Mapping[str, str]
    app_ids: set[str]
    blocked_urls: set[str]
    request_bases: Mapping[str, str] = field(default_factory=dict)
    request_evidence_urls: Mapping[str, str] = field(default_factory=dict)
    robots_cache: dict[str, urllib.robotparser.RobotFileParser | bool] = field(default_factory=dict)
    progress: Callable[[], None] | None = None
    robots_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    host_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    host_failures: dict[str, int] = field(default_factory=dict)
    host_holds: dict[str, float] = field(default_factory=dict)
    healthy_hosts: set[str] = field(default_factory=set)
    request_phases: dict[asyncio.Task, tuple[str, str]] = field(default_factory=dict)
    interrupted_origins: set[str] = field(default_factory=set)
    company_budget: CompanyResearchBudget | None = None
    admission_tasks: set[asyncio.Task] = field(default_factory=set)

    def observe_request(self, phase: str, url: str) -> None:
        task = asyncio.current_task()
        if phase == "admission":
            self.admission_tasks.add(task)
        else:
            self.admission_tasks.discard(task)
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc.casefold()}"
        previous = self.request_phases.pop(task, None)
        if phase in {"dns", "network"}:
            self.request_phases[task] = (phase, origin)
        elif phase == "cancelled" and previous and previous[1] == origin:
            self.interrupted_origins.add(origin)

    def defer_interrupted_origins(self) -> None:
        # Called only for an expired scanner deadline, never for an operator pause.
        # Permit/CPU/database/settlement waits have no active DNS/network phase.
        for origin in self.interrupted_origins:
            self.healthy_hosts.discard(origin)
            self.host_failures[origin] = max(2, self.host_failures.get(origin, 0))
            self.host_holds[origin] = max(
                self.host_holds.get(origin, 0),
                time.monotonic() + FRONTIER_HOST_FAILURE_COOLDOWN_SECONDS,
            )

    async def __call__(self, url: str, maximum_bytes: int) -> FrontierDocument:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc.casefold()}"
        if origin in self.healthy_hosts and not self.host_holds.get(origin):
            return await self._guarded_fetch(url, maximum_bytes, origin)
        async with self.host_locks.setdefault(origin, asyncio.Lock()):
            if origin not in self.healthy_hosts or self.host_holds.get(origin):
                return await self._guarded_fetch(url, maximum_bytes, origin)
        return await self._guarded_fetch(url, maximum_bytes, origin)

    async def _guarded_fetch(
        self, url: str, maximum_bytes: int, origin: str
    ) -> FrontierDocument:
        self.admission_tasks.discard(asyncio.current_task())
        if self.company_budget is not None:
            self.company_budget.check()
        until = self.host_holds.get(origin, 0)
        if until > time.monotonic():
            raise FrontierFetchPolicyError(
                "Research host temporarily deferred after repeated transport failures",
                terminal_status="transient_error",
            )
        if until:
            self.host_holds.pop(origin, None)
            self.host_failures.pop(origin, None)
        try:
            if self.company_budget is None:
                document = await self._fetch(url, maximum_bytes)
            else:
                async with asyncio.timeout(PUBLIC_FETCH_SECONDS):
                    document = await self._fetch(url, maximum_bytes)
        except Exception as exc:
            if isinstance(exc, TimeoutError) and asyncio.current_task() in self.admission_tasks:
                self.admission_tasks.discard(asyncio.current_task())
                raise CompanyResearchDeferred(
                    "Research deferred while waiting for request admission; company not penalized"
                ) from exc
            diagnostic = str(exc).casefold()
            rate_hold = scanner_host_hold_seconds(
                exc, minimum=FRONTIER_HOST_FAILURE_COOLDOWN_SECONDS,
            )
            if self.company_budget is not None and substantive_failure(exc):
                # Missing optional platform-association files do not establish a
                # failing company. Policy/duplicate/asset skips never consume this.
                optional_file = urlsplit(url).path in {
                    "/.well-known/assetlinks.json", "/.well-known/apple-app-site-association",
                    "/apple-app-site-association",
                }
                if not (optional_file and isinstance(exc, httpx.HTTPStatusError)
                        and exc.response.status_code == 404):
                    self.company_budget.failure(minimum_hold=rate_hold)
            immediate_hold = (bool(rate_hold) or "dns resolution failed" in diagnostic
                              or "certificate_verify_failed" in diagnostic
                              or "certificate verify failed" in diagnostic
                              or getattr(exc, "terminal_status", "") == "robots_blocked"
                              or (isinstance(exc, httpx.HTTPStatusError)
                                  and exc.response.status_code in {401, 403}))
            if immediate_hold or isinstance(
                exc, (httpx.NetworkError, httpx.TimeoutException,
                      httpx.RemoteProtocolError, TimeoutError)
            ):
                failures = self.host_failures.get(origin, 0) + 1
                self.healthy_hosts.discard(origin)
                self.host_failures[origin] = failures
                if immediate_hold or failures >= 2:
                    self.host_holds[origin] = (
                        time.monotonic() + max(FRONTIER_HOST_FAILURE_COOLDOWN_SECONDS, rate_hold)
                    )
            raise
        self.host_failures.pop(origin, None)
        self.host_holds.pop(origin, None)
        self.healthy_hosts.add(origin)
        if self.company_budget is not None:
            self.company_budget.success()
        return document

    async def _fetch(self, url: str, maximum_bytes: int) -> FrontierDocument:
        canonical = _canonical_url(url)
        if is_frontier_asset_url(canonical):
            raise FrontierFetchPolicyError("Optional research skipped a non-document asset URL")
        if is_frontier_utility_url(canonical):
            raise FrontierFetchPolicyError("Optional research skipped an embedding or booking URL")
        if not canonical or not self._authorized(canonical):
            raise FrontierFetchPolicyError(
                "frontier URL was not discovered on a first-party surface"
            )
        if canonical in self.blocked_urls or self._is_company_root(canonical):
            raise FrontierFetchPolicyError(
                "core website URL is already hydrated and cannot be fetched by an optional scanner"
            )

        auxiliary_requests, auxiliary_bytes = await self._check_robots(canonical)
        redirect_auxiliary_requests = 0
        redirect_auxiliary_bytes = 0
        redirect_hops = 0

        async def validate_redirect(_current: str, redirected: str) -> None:
            nonlocal redirect_auxiliary_requests, redirect_auxiliary_bytes, redirect_hops
            redirect_hops += 1
            final_candidate = _canonical_url(redirected)
            if is_frontier_asset_url(final_candidate):
                raise FrontierFetchPolicyError("Optional research redirect targeted an asset URL")
            if is_frontier_utility_url(final_candidate):
                raise FrontierFetchPolicyError("Optional research redirect targeted a utility URL")
            if not final_candidate or not self._redirect_authorized(canonical, final_candidate):
                raise FrontierFetchPolicyError(
                    "frontier redirect crossed the explicitly authorized host boundary"
                )
            if final_candidate in self.blocked_urls or self._is_company_root(final_candidate):
                raise FrontierFetchPolicyError(
                    "frontier redirect targeted already-hydrated core website content"
                )
            redirect_owner = self.url_owners.get(
                final_candidate
            ) or classify_frontier_surface_owner(final_candidate)
            if redirect_owner and redirect_owner != self.scanner_id:
                raise FrontierFetchPolicyError(
                    "frontier redirect targeted a surface owned by another scanner"
                )
            requests, byte_count = await self._check_robots(final_candidate)
            redirect_auxiliary_requests += requests
            redirect_auxiliary_bytes += byte_count

        lookup_target = urlsplit(canonical)
        apple_lookup = (
            self.scanner_id == "app_release_pulse_v1"
            and (lookup_target.hostname or "").lower() in {
                "itunes.apple.com", "itunes.apple.com.cn"
            }
            and lookup_target.path == "/lookup"
        )
        accepted_types = (
            "html", "json", "xml", "rss", "atom", "text/calendar", "text/plain",
        ) + (("text/javascript",) if apple_lookup else ())
        response, body = await _bounded_get(
            self.client,
            canonical,
            max_bytes=max(1, int(maximum_bytes)),
            accept=(
                "text/html,application/xhtml+xml,application/json,application/xml,"
                "text/xml,application/rss+xml,application/atom+xml,text/calendar,"
                "text/plain;q=0.8"
            ),
            allowed_content_types=accepted_types,
            redirect_validator=validate_redirect,
        )
        if self.progress:
            self.progress()
        content_type = response.headers.get("content-type", "").casefold()
        allowed_content_types = accepted_types
        if content_type and not any(
            allowed in content_type for allowed in allowed_content_types
        ):
            raise FrontierFetchPolicyError(
                "frontier source returned unsupported Content-Type "
                f"{content_type.split(';', 1)[0]}",
                terminal_status="failed",
            )
        if response.status_code < 200 or response.status_code >= 300:
            error = httpx.HTTPStatusError(
                f"frontier source {canonical} returned HTTP {response.status_code}",
                request=response.request,
                response=response,
            )
            error.frontier_response_bytes = len(body)
            error.frontier_auxiliary_requests = (
                auxiliary_requests + redirect_auxiliary_requests + redirect_hops
            )
            error.frontier_auxiliary_bytes = auxiliary_bytes + redirect_auxiliary_bytes
            raise error
        final_url = _canonical_url(str(response.url))
        if not final_url or not self._redirect_authorized(canonical, final_url):
            raise FrontierFetchPolicyError(
                "frontier redirect crossed the explicitly authorized host boundary"
            )
        if "text/javascript" in content_type:
            lookup_response = urlsplit(final_url)
            if not (
                apple_lookup
                and (lookup_response.hostname or "").lower() in {
                    "itunes.apple.com", "itunes.apple.com.cn"
                }
                and lookup_response.path == "/lookup"
            ):
                raise FrontierFetchPolicyError(
                    "JavaScript MIME is allowed only for Apple lookup JSON"
                )
            try:
                lookup_payload = json.loads(body)
            except (ValueError, UnicodeDecodeError) as exc:
                raise FrontierFetchPolicyError("Apple lookup returned invalid JSON") from exc
            if not isinstance(lookup_payload, dict) or not isinstance(
                lookup_payload.get("results"), list
            ):
                raise FrontierFetchPolicyError("Apple lookup returned an invalid results payload")
        first_party_source = self.request_sources.get(canonical, "")
        discovery_basis = self.request_bases.get(canonical, "first_party_link")
        headers = {str(key): str(value) for key, value in response.headers.items()}
        headers[FRONTIER_AUXILIARY_REQUEST_COUNT_HEADER] = str(
            auxiliary_requests + redirect_auxiliary_requests + redirect_hops
        )
        headers[FRONTIER_AUXILIARY_BYTE_COUNT_HEADER] = str(
            auxiliary_bytes + redirect_auxiliary_bytes
        )
        return FrontierDocument(
            url=final_url or canonical,
            content=body,
            kind=_document_kind(
                final_url or canonical,
                response.headers.get("content-type", ""),
                scanner_id=self.scanner_id,
            ),
            status_code=response.status_code,
            mime_type=response.headers.get("content-type", "").split(";", 1)[0],
            publisher=(urlsplit(final_url or canonical).hostname or "").lower(),
            first_party_source_url=first_party_source,
            officially_linked=discovery_basis == "first_party_link",
            discovery_basis=discovery_basis,
            discovery_evidence_url=self.request_evidence_urls.get(canonical, first_party_source),
            robots_state="allowed",
            headers=headers,
        )

    def _authorized(self, url: str) -> bool:
        if is_broad_social_research_url(url):
            return False
        if url in self.allowed_urls and url in self.request_sources:
            return True
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host in {"itunes.apple.com", "itunes.apple.com.cn"} and parsed.path == "/lookup":
            requested_ids = {
                item for raw in parse_qs(parsed.query).get("id", ()) for item in str(raw).split(",")
            }
            return bool(requested_ids) and requested_ids <= self.app_ids
        return False

    def _is_company_root(self, url: str) -> bool:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        return host == self.company_key.removeprefix("www.") and parsed.path in {"", "/"}

    @staticmethod
    def _normalized_host(url: str) -> str:
        return (urlsplit(url).hostname or "").lower().rstrip(".").removeprefix("www.")

    def _company_bound_host(self, host: str) -> bool:
        company_host = self.company_key.lower().rstrip(".").removeprefix("www.")
        return bool(
            company_host and (host == company_host or host.endswith(f".{company_host}"))
        )

    def _redirect_authorized(self, initial_url: str, redirected_url: str) -> bool:
        initial_host = self._normalized_host(initial_url)
        redirected_host = self._normalized_host(redirected_url)
        if (
            not initial_host or not redirected_host
            or is_broad_social_research_url(redirected_url)
        ):
            return False
        if self._company_bound_host(initial_host):
            return self._company_bound_host(redirected_host)
        # Shared hosted-provider tenants stay on the exact host, resource path, and semantic
        # query. A tenant is often selected by query string rather than path, so only common
        # attribution parameters may change without fresh first-party provenance.
        initial_path = urlsplit(initial_url).path.rstrip("/") or "/"
        redirected_path = urlsplit(redirected_url).path.rstrip("/") or "/"
        tracking_keys = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid"}

        def semantic_query(url: str) -> tuple[tuple[str, str], ...]:
            return tuple(
                sorted(
                    (key, value)
                    for key, value in parse_qsl(
                        urlsplit(url).query,
                        keep_blank_values=True,
                    )
                    if not key.lower().startswith("utm_")
                    and key.lower() not in tracking_keys
                )
            )

        return (
            initial_host == redirected_host
            and initial_path == redirected_path
            and semantic_query(initial_url) == semantic_query(redirected_url)
        )

    async def _check_robots(self, url: str) -> tuple[int, int]:
        parsed = urlsplit(url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        async with self.robots_locks.setdefault(origin, asyncio.Lock()):
            return await self._check_robots_unlocked(url)

    async def _check_robots_unlocked(self, url: str) -> tuple[int, int]:
        parsed = urlsplit(url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        cached = self.robots_cache.get(origin)
        requests = 0
        byte_count = 0
        if cached is None:
            robots_url = f"{origin}/robots.txt"
            response, body = await _bounded_get(
                self.client,
                robots_url,
                max_bytes=256_000,
                accept="text/plain,*/*;q=0.1",
                allowed_content_types=("text/plain", "text/", "html"),
                redirect_validator=self._validate_robots_redirect,
            )
            requests = 1
            byte_count = len(body)
            if self.progress:
                self.progress()
            if response.status_code in {401, 403}:
                self.robots_cache[origin] = False
                cached = False
            elif response.status_code == 404:
                self.robots_cache[origin] = True
                cached = True
            elif 200 <= response.status_code < 300:
                parser = urllib.robotparser.RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(body.decode("utf-8", errors="replace").splitlines())
                self.robots_cache[origin] = parser
                cached = parser
            else:
                raise FrontierFetchPolicyError(
                    f"robots policy could not be verified (HTTP {response.status_code})",
                    terminal_status=(
                        "rate_limited" if response.status_code == 429 else "transient_error"
                    ),
                )
        allowed = cached if isinstance(cached, bool) else cached.can_fetch(USER_AGENT, url)
        if not allowed:
            raise FrontierFetchPolicyError(
                "robots.txt disallows this frontier URL",
                terminal_status="robots_blocked",
            )
        return requests, byte_count

    async def _validate_robots_redirect(self, current: str, redirected: str) -> None:
        if self._normalized_host(current) != self._normalized_host(redirected):
            raise FrontierFetchPolicyError("robots.txt redirect crossed its origin host")


def _canonical_url(value: str) -> str:
    try:
        parsed = urlsplit(str(value).strip())
        _ = parsed.port
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.lower().rstrip(".")
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def _document_kind(url: str, content_type: str, *, scanner_id: str = "") -> str:
    path = urlsplit(url).path.casefold()
    normalized_type = content_type.casefold()
    is_feed = "html" not in normalized_type and ("xml" in normalized_type
        or "feed+json" in normalized_type or any(
        token in path for token in ("feed", "rss", "atom", "podcast")
    ))
    if scanner_id == "product_release_engine_v1":
        return "product_release_feed" if is_feed else "product_release_surface"
    if scanner_id == "event_webinar_engine_v1":
        if "text/calendar" in normalized_type or path.endswith(".ics"):
            return "event_calendar"
        return "event_feed" if is_feed else "event_surface"
    if scanner_id == "trust_center_readiness_v1":
        return "security_txt" if path.endswith("/.well-known/security.txt") else "trust_center"
    if scanner_id == "brand_asset_governance_v1":
        return "brand_asset_portal"
    if scanner_id == "public_design_system_v1":
        return "public_design_system"
    if scanner_id == "creative_hiring_demand_v1":
        host = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
        return (
            "ats_career_board"
            if host
            in {
                "boards.greenhouse.io",
                "job-boards.greenhouse.io",
                "jobs.lever.co",
                "jobs.ashbyhq.com",
            }
            else "company_career_surface"
        )
    if "itunes.apple.com" in url and path == "/lookup":
        return "apple_lookup"
    if path.endswith(("assetlinks.json", "apple-app-site-association")):
        return "app_association"
    if any(token in path for token in ("creator", "affiliate", "ambassador", "influencer")):
        return "creator_program"
    if any(token in path for token in ("archive", "campaign", "newsletter")):
        return "lifecycle_archive"
    if any(token in path for token in ("blog", "insight", "resource", "article")):
        return "corporate_blog_surface"
    if is_feed:
        return "official_owned_media_feed"
    return "public_document"


def _strings(value: object, *, maximum: int = 100) -> tuple[str, ...]:
    if isinstance(value, str):
        raw: Sequence[object] = (value,)
    elif isinstance(value, Sequence):
        raw = value
    else:
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in raw[:maximum] if str(item).strip()))


def _identity_inputs(
    discovery: Mapping[str, Any],
    fallback_company_name: str,
) -> tuple[str, tuple[str, ...], str, str]:
    legal_names = _strings(
        discovery.get("first_party_legal_names", discovery.get("legal_names", ())),
        maximum=10,
    )
    legal_name = legal_names[0] if len({item.casefold() for item in legal_names}) == 1 else ""
    company_names = _strings(
        discovery.get("first_party_company_names", discovery.get("company_names", ())),
        maximum=10,
    )
    locations = _strings(
        discovery.get("first_party_locations", discovery.get("locations", ())),
        maximum=30,
    )
    phones = _strings(
        discovery.get("first_party_phones", discovery.get("phones", ())),
        maximum=10,
    )
    return (
        legal_name,
        locations,
        phones[0] if len(phones) == 1 else "",
        company_names[0] if company_names else fallback_company_name,
    )


def _request_inputs(
    discovery: Mapping[str, Any],
) -> tuple[dict[str, tuple[str, ...]], dict[str, str], tuple[str, ...]]:
    raw_map = _mapping(discovery.get("request_urls"))
    raw_sources = _mapping(
        discovery.get("request_url_sources", discovery.get("request_sources", {}))
    )
    request_urls: dict[str, tuple[str, ...]] = {}
    sources: dict[str, str] = {}
    owners_by_url: dict[str, set[str]] = {}
    for raw_scanner_id, value in raw_map.items():
        scanner_id = str(raw_scanner_id)
        if scanner_id not in SCANNER_REGISTRY:
            continue
        scanner_sources = _mapping(raw_sources.get(scanner_id))
        admitted: list[str] = []
        for raw in _strings(value, maximum=30):
            url = _canonical_url(raw)
            source = _canonical_url(str(scanner_sources.get(url) or ""))
            # Persisted optional work requires exact scanner-keyed provenance. A flat map,
            # another scanner's map, or the discovery page's default URL cannot establish
            # ownership and must not authorize a fetch.
            if (url and source and not is_broad_social_research_url(url)
                    and not is_frontier_asset_url(url)
                    and is_frontier_request_target(url, scanner_id, source)):
                admitted.append(url)
                sources[url] = source
                owners_by_url.setdefault(url, set()).add(scanner_id)
        request_urls[scanner_id] = tuple(dict.fromkeys(admitted))
    collisions = {url for url, owners in owners_by_url.items() if len(owners) > 1}
    if collisions:
        # A persisted URL with multiple scanner owners bypasses the live single-owner
        # classifier. Without the source HTML needed to reclassify it, fail closed for every
        # claimant rather than fetch or score the same surface twice.
        request_urls = {
            scanner_id: tuple(url for url in urls if url not in collisions)
            for scanner_id, urls in request_urls.items()
        }
        for url in collisions:
            sources.pop(url, None)
    return request_urls, sources, _strings(discovery.get("first_party_app_ids"), maximum=20)


def _company_bound_url(url: str, company_key: str) -> bool:
    host = (urlsplit(url).hostname or "").casefold().rstrip(".").removeprefix("www.")
    key = company_key.casefold().rstrip(".").removeprefix("www.")
    return bool(host and key and (host == key or host.endswith(f".{key}")))


def _augment_hiring_request_inputs(
    observation: SiteObservation,
    company_key: str,
    request_urls: dict[str, tuple[str, ...]],
    request_sources: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Add only typed sitemap/DNS careers candidates; never guess a path or provider tenant."""

    scanner_id = "creative_hiring_demand_v1"
    request_bases = {
        url: "first_party_link" for urls in request_urls.values() for url in urls
    }
    evidence_urls = {
        url: request_sources[url]
        for urls in request_urls.values()
        for url in urls
        if url in request_sources
    }
    admitted = list(request_urls.get(scanner_id, ()))
    fallback_source = _canonical_url(
        observation.final_url or f"https://{company_key}/"
    )

    for raw_url in observation.web.career_urls:
        url = _canonical_url(raw_url)
        raw_source = observation.web.career_url_sources.get(raw_url, "")
        source = _canonical_url(raw_source)
        if not (
            url
            and source
            and _company_bound_url(url, company_key)
            and _company_bound_url(source, company_key)
        ):
            continue
        if url not in admitted:
            admitted.append(url)
        request_sources[url] = source
        request_bases.setdefault(url, "same_company_sitemap")
        evidence_urls[url] = source

    for raw_host in observation.web.vendors.career_hosts:
        host = str(raw_host).strip().casefold().rstrip(".")
        url = _canonical_url(f"https://{host}/")
        evidence = _canonical_url(
            observation.web.vendors.career_host_evidence_urls.get(host, "")
        )
        if not (
            url
            and fallback_source
            and evidence
            and _company_bound_url(url, company_key)
        ):
            continue
        if url not in admitted:
            admitted.append(url)
        # The source field remains a company-bound authorization anchor. The distinct DoH URL is
        # retained as evidence and the basis prevents it from being called an official link.
        request_sources[url] = fallback_source
        request_bases.setdefault(url, "dns_exact_nonwildcard_host")
        evidence_urls[url] = evidence

    request_urls[scanner_id] = tuple(admitted[:30])
    return request_bases, evidence_urls


def _hiring_records(observation: SiteObservation) -> tuple[Mapping[str, Any], ...]:
    ats_source_hosts = {
        "boards-api.greenhouse.io",
        "api.lever.co",
        "api.ashbyhq.com",
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "jobs.lever.co",
        "jobs.ashbyhq.com",
    }
    ats_sources = [
        source
        for source in observation.web.source_urls
        if (urlsplit(source).hostname or "").casefold() in ats_source_hosts
    ]
    ats_error = any(
        re.search(
            r"\b(?:greenhouse|lever|ashby)\b.*\b(?:error|invalid|returned|failed)\b",
            error,
            re.I,
        )
        for error in observation.web.errors
    )
    records: list[Mapping[str, Any]] = [
        {
            "record_type": "legacy_hiring_summary",
            "open_jobs": int(observation.web.open_jobs),
            "creative_jobs": int(observation.web.creative_jobs),
            "photo_video_jobs": int(observation.web.photo_video_jobs),
            "growth_jobs": int(observation.web.growth_jobs),
            "leadership_jobs": int(observation.web.leadership_jobs),
            "ats_boards": observation.web.ats_boards,
            "source_urls": list(
                dict.fromkeys(
                    [
                        *ats_sources,
                        *(
                            item.source_url
                            for item in observation.web.job_postings
                            if item.source_url
                        ),
                    ]
                )
            )[:30],
            "coverage_complete": bool(observation.web.ats_boards) and not ats_error,
        }
    ]
    records.extend(
        {
            "record_type": "public_job_evidence",
            **item.model_dump(mode="json"),
        }
        for item in observation.web.job_postings[:30]
    )
    return tuple(records)


def _source_records(
    source: FrontierRecordSource | None,
    scanner_id: str,
    legal_name: str,
) -> tuple[Mapping[str, Any], ...]:
    if source is None or not legal_name:
        return ()
    try:
        records = source.records_for(scanner_id, legal_name)
    except (KeyError, OSError, TypeError, ValueError):
        return ()
    return tuple(item for item in records[:10_000] if isinstance(item, Mapping))


def _scanner_guard_states(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Return method-level success, active, and retry-plan guards across versions."""

    now = datetime.now(UTC)
    guards: dict[str, str] = {}
    priority = {"not_due": 1, "active": 2, "reused": 3}
    for row in rows:
        scanner_id = str(row.get("scanner_id") or "")
        if not scanner_id:
            continue
        status = str(row.get("status") or "")
        completed = _parse_time(row.get("completed_at"))
        next_due = _parse_time(row.get("next_due_at"))
        lease_expires = _parse_time(row.get("lease_expires_at"))
        guard = ""
        if status in SCANNER_REUSABLE_STATUSES and (
            (completed is not None and now - REHYDRATION_INTERVAL < completed <= now)
            or (next_due is not None and next_due > now)
        ):
            guard = "reused"
        elif status == "running" and lease_expires is not None and lease_expires > now:
            guard = "active"
        elif status in SCANNER_FAILURE_STATUSES and next_due is not None and next_due > now:
            guard = "not_due"
        if guard and priority[guard] > priority.get(guards.get(scanner_id, ""), 0):
            guards[scanner_id] = guard
    return guards


def _recent_reusable_scanners(rows: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Return only hard-floor scanner reuses, excluding stale/not-due and failed work."""
    now = datetime.now(UTC)
    reusable: set[str] = set()
    for row in rows:
        if str(row.get("status") or "") not in SCANNER_REUSABLE_STATUSES:
            continue
        completed = _parse_time(row.get("completed_at"))
        scanner_id = str(row.get("scanner_id") or "")
        if (
            scanner_id
            and completed is not None
            and now - REHYDRATION_INTERVAL < completed <= now
        ):
            reusable.add(scanner_id)
    return frozenset(reusable)


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _frontier_contains_scanner(frontier: FrontierIntelligence, scanner_id: str) -> bool:
    return any(
        str(getattr(item, "scanner_id", "")) == scanner_id
        for collection in (
            frontier.executions,
            frontier.coverage_manifests,
            frontier.evidence_artifacts,
            frontier.observations,
            frontier.schedule_decisions,
        )
        for item in collection
    )


def _without_seo_readiness(frontier: FrontierIntelligence) -> FrontierIntelligence:
    """Remove local SEO data before persistence for an out-of-cohort company."""

    return frontier.model_copy(
        update={
            "executions": [
                item for item in frontier.executions if item.scanner_id != SEO_READINESS_SCANNER_ID
            ],
            "coverage_manifests": [
                item
                for item in frontier.coverage_manifests
                if item.scanner_id != SEO_READINESS_SCANNER_ID
            ],
            "evidence_artifacts": [
                item
                for item in frontier.evidence_artifacts
                if item.scanner_id != SEO_READINESS_SCANNER_ID
            ],
            "observations": [
                item
                for item in frontier.observations
                if item.scanner_id != SEO_READINESS_SCANNER_ID
            ],
            "schedule_decisions": [
                item
                for item in frontier.schedule_decisions
                if item.scanner_id != SEO_READINESS_SCANNER_ID
            ],
            "features": {
                key: value
                for key, value in frontier.features.items()
                if key != "seo_visibility_engine"
            },
        }
    )


def _applicability(
    scanner_id: str,
    prepared: _PreparedTarget,
) -> tuple[bool, str]:
    if scanner_id == SEO_READINESS_SCANNER_ID and not prepared.seo_admitted:
        return False, "Local SEO readiness is limited to the current canonical top 2,500."
    if scanner_id in _LOCAL_HTML_SCANNERS:
        embedded = prepared.target.persist_embedded and _frontier_contains_scanner(
            prepared.embedded,
            scanner_id,
        )
        reusable = scanner_id in prepared.recent_reusable_scanners
        applicable = embedded or reusable
        if embedded:
            reason = "A current core hydration produced this local scanner projection."
        elif reusable:
            reason = "A completed local scanner execution is inside its hard reuse window."
        else:
            reason = (
                "No current embedded projection or hard-window reusable execution is available; "
                "optional orchestration will not refetch the company website."
            )
        return applicable, reason
    if scanner_id in _NETWORK_SCANNERS:
        urls = prepared.request_urls.get(scanner_id, ())
        if scanner_id == "app_release_pulse_v1":
            applicable = bool(urls or prepared.app_ids)
        elif scanner_id == "creative_hiring_demand_v1":
            applicable = bool(
                urls
                or any(
                    str(item.get("record_type") or "") == "public_job_evidence"
                    for item in prepared.records.get(scanner_id, ())
                )
                or prepared.target.observation.web.ats_boards
            )
        else:
            applicable = bool(urls)
        return (
            applicable,
            (
                "A bounded company careers surface or normalized public ATS record is available."
                if scanner_id == "creative_hiring_demand_v1"
                else "A first-party page exposed a bounded public scanner URL."
            )
            if applicable
            else (
                "No company-bound careers surface or normalized public ATS record was observed."
                if scanner_id == "creative_hiring_demand_v1"
                else "No first-party-published scanner URL was observed."
            ),
        )
    if scanner_id == "dol_lca_workforce_v1":
        applicable = bool(
            prepared.legal_name and prepared.locations and prepared.records[scanner_id]
        )
        return (
            applicable,
            "Exact first-party legal name, location, and local DOL rows are available."
            if applicable
            else "Exact legal-name/location evidence or local DOL rows are unavailable.",
        )
    if scanner_id == "sec_form_d_capital_v1":
        applicable = bool(
            prepared.legal_name
            and prepared.records[scanner_id]
            and (prepared.locations or prepared.phone or prepared.company_key)
        )
        return (
            applicable,
            "Exact first-party legal name and corroboration inputs are available."
            if applicable
            else "Exact legal-name records or corroboration inputs are unavailable.",
        )
    if scanner_id in _CONSTELLATION_SCANNERS:
        sf = any(_SF_RE.search(item) for item in prepared.locations)
        applicable = sf and bool(prepared.current.observations)
        return (
            applicable,
            "First-party Bay Area geography and existing frontier evidence are available."
            if applicable
            else "Bay Area geography or prerequisite frontier evidence is unavailable.",
        )
    return False, "No bounded applicability rule exists."


def _draws(run_id: int | None, company_key: str) -> tuple[float, float]:
    digest = hashlib.sha256(f"frontier-eig-v1|{run_id or 0}|{company_key}".encode()).digest()
    denominator = float(2**64)
    return (
        int.from_bytes(digest[:8], "big") / denominator,
        int.from_bytes(digest[8:16], "big") / denominator,
    )


def _prepare_decisions(
    prepared: _PreparedTarget,
    store: LeadStore,
    run_id: int | None,
) -> tuple[Any, ...]:
    from .sales_engine.maintenance import (
        adjusted_research_prior,
        empirical_scanner_yield,
        research_utility,
    )
    from .sales_engine.research_allocation import (
        boundary_sensitivity,
        ranking_context,
        resolved_dimensions,
    )

    empirical = empirical_scanner_yield(store)
    fresh = set(prepared.scanner_guards)
    now = datetime.now(UTC)
    context = ranking_context(
        store.connection, prepared.target.candidate_id,
        fallback_score=prepared.target.boundary_score, now=now,
    )
    methods: list[dict[str, object]] = []
    all_dimensions: list[str] = []
    resolved = resolved_dimensions(
        prepared.current.observations, SCANNER_REGISTRY, prepared.company_key, now=now,
    )
    for definition in SCANNER_DEFINITIONS:
        applicable, _ = _applicability(definition.scanner_id, prepared)
        all_dimensions.extend(definition.eig_dimensions)
        normalized_cost = {
            "local": 0.01,
            "shared_bulk": 0.025,
            "keyless_public": 0.055,
        }[definition.cost_class]
        utility, normalized_cost = research_utility(
            empirical.get(definition.scanner_id), prior=definition.evidence_value,
            cost=normalized_cost,
        )
        methods.append(
            {
                "scanner_id": definition.scanner_id,
                "applicable": applicable,
                "fresh": definition.scanner_id in fresh,
                "resolves_dimensions": definition.eig_dimensions,
                "success_probability": adjusted_research_prior(
                    definition.success_probability, empirical.get(definition.scanner_id)
                ),
                "expected_action_entropy_reduction": min(
                    1.0, 0.50 + 0.08 * len(definition.eig_dimensions)
                ),
                "action_boundary_sensitivity": boundary_sensitivity(
                    context, definition.eig_dimensions, cached=definition.cost_class == "local",
                ),
                "evidence_value": utility,
                "normalized_cost": normalized_cost,
                "rate_penalty": 0.02 if definition.cost_class == "keyless_public" else 0.0,
                "identity_penalty": (0.04 if definition.scanner_id in _SHARED_SCANNERS else 0.0),
                "risk_penalty": (
                    0.03
                    if definition.scanner_id
                    in {"sec_form_d_capital_v1", "ai_artifact_provenance_v1"}
                    else 0.0
                ),
                "can_change_action": definition.scanner_id
                in {
                    "creator_program_contract_v1",
                    "ai_artifact_provenance_v1",
                    "product_release_engine_v1",
                    "event_webinar_engine_v1",
                    "trust_center_readiness_v1",
                    "growth_measurement_schema_v1",
                    "sf_ai_capital_constellation_v1",
                },
            }
        )
    exploration_draw, selection_draw = _draws(run_id, prepared.company_key)
    return schedule_scanners_by_expected_information_gain(
        methods,
        missing_dimensions=tuple(
            item for item in dict.fromkeys(all_dimensions) if item not in resolved
        ),
        exploration_share=0.10,
        exploration_draw=exploration_draw,
        selection_draw=selection_draw,
    )


def _schedule_model(
    prepared: _PreparedTarget,
    raw: Any,
    *,
    actually_selected: bool,
    budget_rank: int | None,
    maximum: int,
    decided_at: datetime,
) -> ScannerScheduleDecision:
    definition = SCANNER_REGISTRY[raw.scanner_id]
    applicable, reason = _applicability(raw.scanner_id, prepared)
    selected = bool(raw.selected and actually_selected)
    selection_reason = ""
    if selected:
        selection_reason = "highest logged expected information gain within batch budget"
    elif raw.selected:
        selection_reason = "eligible but deferred by global frontier scanner budget"
    elif not applicable:
        selection_reason = "not applicable from current evidence"
    elif raw.fresh:
        selection_reason = "scanner result is not due"
    else:
        selection_reason = "lower expected information gain"
    return ScannerScheduleDecision(
        company_key=prepared.company_key,
        scanner_id=raw.scanner_id,
        scanner_version=definition.version,
        decided_at=decided_at,
        applicable=applicable,
        applicability_reason=reason,
        current_action=prepared.target.current_action,
        success_probability=raw.success_probability,
        expected_action_entropy_reduction=raw.expected_action_entropy_reduction,
        action_boundary_sensitivity=raw.action_boundary_sensitivity,
        evidence_value=raw.evidence_value,
        cost_penalty=raw.normalized_cost,
        rate_penalty=raw.rate_penalty,
        identity_penalty=raw.identity_penalty,
        risk_penalty=raw.risk_penalty,
        expected_information_gain=raw.expected_information_gain,
        selected=selected,
        selection_reason=selection_reason,
        exploration=bool(selected and raw.exploration_selected),
        propensity=raw.selection_propensity if selected else 0.0,
        metadata={
            "policy": "frontier_eig_empirical_yield_v2",
            "resolves_dimensions": list(raw.resolves_dimensions),
            "missing_dimensions_resolved": list(raw.missing_dimensions_resolved),
            "fresh": raw.fresh,
            "cost_class": definition.cost_class,
            "safe_claim": definition.safe_claim,
            "prohibited_claims": list(definition.prohibited_claims),
            "exploration_share": raw.exploration_share,
            "exploration_draw": raw.exploration_draw,
            "selection_draw": raw.selection_draw,
            "conditional_selection_propensity": raw.selection_propensity,
            "batch_budget": maximum,
            "batch_rank": budget_rank,
        },
    )


def _normalize_manual_scanner_ids(
    scanner_ids: Sequence[str] | None,
) -> tuple[str, ...] | None:
    normalized = tuple(
        dict.fromkeys(
            str(scanner_id).strip() for scanner_id in scanner_ids or () if str(scanner_id).strip()
        )
    )
    if not normalized:
        return None
    unknown = sorted(set(normalized) - set(FRONTIER_SCANNER_IDS))
    if unknown:
        raise ValueError(
            "Unknown frontier scanner id(s): "
            f"{', '.join(unknown)}. Valid values: {', '.join(FRONTIER_SCANNER_IDS)}"
        )
    return normalized


def _manual_schedule_model(
    prepared: _PreparedTarget,
    scanner_id: str,
    *,
    requested: bool,
    actually_selected: bool,
    guard_state: str,
    budget_rank: int | None,
    maximum: int,
    decided_at: datetime,
) -> ScannerScheduleDecision:
    definition = SCANNER_REGISTRY[scanner_id]
    applicable, reason = _applicability(scanner_id, prepared)
    selected = bool(requested and applicable and actually_selected)
    if selected:
        selection_reason = "explicitly requested by operator and admitted within the batch budget"
    elif requested and not applicable:
        selection_reason = "explicitly requested by operator but not currently applicable"
    elif requested and guard_state == "reused":
        selection_reason = "existing successful result is still current"
    elif requested and guard_state == "active":
        selection_reason = "scanner method already has active work"
    elif requested and guard_state == "not_due":
        selection_reason = "existing retry or TTL plan is not due"
    elif requested:
        selection_reason = "explicitly requested by operator but deferred by batch budget"
    else:
        selection_reason = "not requested by operator"
    normalized_cost = {
        "local": 0.01,
        "shared_bulk": 0.025,
        "keyless_public": 0.055,
    }[definition.cost_class]
    return ScannerScheduleDecision(
        company_key=prepared.company_key,
        scanner_id=scanner_id,
        scanner_version=definition.version,
        decided_at=decided_at,
        applicable=applicable,
        applicability_reason=reason,
        current_action=prepared.target.current_action,
        success_probability=definition.success_probability,
        expected_action_entropy_reduction=0.0,
        action_boundary_sensitivity=0.0,
        evidence_value=definition.evidence_value,
        cost_penalty=normalized_cost,
        rate_penalty=0.02 if definition.cost_class == "keyless_public" else 0.0,
        identity_penalty=0.04 if scanner_id in _SHARED_SCANNERS else 0.0,
        risk_penalty=(
            0.03 if scanner_id in {"sec_form_d_capital_v1", "ai_artifact_provenance_v1"} else 0.0
        ),
        expected_information_gain=0.0,
        selected=selected,
        selection_reason=selection_reason,
        exploration=False,
        propensity=1.0 if selected else 0.0,
        metadata={
            "policy": "operator_manual_v1",
            "operator_requested": requested,
            "resolves_dimensions": list(definition.eig_dimensions),
            "fresh": bool(guard_state),
            "guard_state": guard_state,
            "cost_class": definition.cost_class,
            "safe_claim": definition.safe_claim,
            "prohibited_claims": list(definition.prohibited_claims),
            "conditional_selection_propensity": 1.0 if selected else 0.0,
            "batch_budget": maximum,
            "batch_rank": budget_rank,
        },
    )


def _context(prepared: _PreparedTarget, scanner_id: str) -> FrontierScannerContext:
    request_urls = prepared.request_urls.get(scanner_id, ())
    if scanner_id == "creative_hiring_demand_v1" and any(
        str(item.get("record_type") or "") == "public_job_evidence"
        for item in prepared.records.get(scanner_id, ())
    ):
        # The existing public provider adapter already fetched and normalized these exact ATS
        # tenants. Preserve same-company careers candidates, but do not pay for a second rendering
        # of Greenhouse/Lever/Ashby in the canonical scanner execution.
        request_urls = tuple(
            url
            for url in request_urls
            if (urlsplit(url).hostname or "").casefold()
            not in {
                "boards.greenhouse.io",
                "job-boards.greenhouse.io",
                "jobs.lever.co",
                "jobs.ashbyhq.com",
            }
        )
    return FrontierScannerContext(
        company_key=prepared.company_key,
        primary_url=prepared.target.observation.final_url or f"https://{prepared.company_key}/",
        company_name=prepared.company_name,
        legal_name=prepared.legal_name,
        locations=prepared.locations,
        records=prepared.records.get(scanner_id, ()),
        request_urls=request_urls,
        metadata={
            "phone": prepared.phone,
            "first_party_app_ids": prepared.app_ids,
            "request_provenance": {
                url: prepared.request_sources[url]
                for url in request_urls
                if url in prepared.request_sources
            },
            "request_bases": {
                url: prepared.request_bases.get(url, "first_party_link")
                for url in request_urls
            },
            "request_evidence_urls": {
                url: prepared.request_evidence_urls.get(
                    url,
                    prepared.request_sources.get(url, ""),
                )
                for url in request_urls
            },
            "frontier": prepared.current,
        },
        observed_at=prepared.target.observation.observed_at,
    )


def _blocked_core_urls(observation: SiteObservation) -> set[str]:
    values = [observation.final_url or "", *(item.url for item in observation.pages)]
    return {url for raw in values if (url := _canonical_url(raw))}


async def orchestrate_frontier_intelligence(
    store: LeadStore,
    targets: Sequence[FrontierTarget],
    *,
    run_id: int | None,
    client: httpx.AsyncClient,
    maximum_optional_scans: int = 20,
    record_source: FrontierRecordSource | None = None,
    manual_scanner_ids: Sequence[str] | None = None,
    manual_force_refresh: bool = True,
    company_scanner_ids: Mapping[str, Sequence[str]] | None = None,
    activity: Callable[[str, str, str], None] | None = None,
    progress: Callable[[], None] | None = None,
    work_progress: Callable[[int, int], None] | None = None,
) -> FrontierBatchSummary:
    """Persist in-memory scanner output, schedule optional work, and hydrate.

    This function never invokes core company inspection. Existing first-party pages are placed on a
    hard blocklist; optional network work is admitted only from URL+source pairs discovered in the
    already-fetched HTML projection, and each scanner claim is committed before its fetch callback.
    With no manual scanner ids, the existing one-per-target EIG policy is preserved. Explicit ids
    use a separately logged operator policy and still reuse completed work inside 90 days. Local
    manual work is admitted only for a current embedded projection or that hard reusable execution;
    optional orchestration never reopens the company website to recreate missing local input.
    """

    maximum = max(0, min(int(maximum_optional_scans), 500))
    manual_ids = _normalize_manual_scanner_ids(manual_scanner_ids)
    if company_scanner_ids is not None and manual_ids is None:
        raise ValueError("A company scanner manifest requires explicit scanner IDs")
    prepared_targets: list[_PreparedTarget] = []
    errors: list[str] = []
    error_hosts: list[str] = []
    embedded_groups = 0
    embedded_completed = 0
    embedded_reused = 0

    company_keys = store.candidate_company_keys([item.candidate_id for item in targets])
    key_selector = getattr(store, "dataforseo_ranked_company_keys", None)
    seo_top_company_keys = key_selector(limit=2_500) if key_selector else {
        str(item["canonical_domain"]).strip().lower().removeprefix("www.")
        for item in store.dataforseo_ranking_candidates(limit=2_500)
    }
    for target in targets:
        company_key = company_keys.get(target.candidate_id, target.candidate.hostname)
        embedded = target.observation.web.frontier
        seo_admitted = company_key in seo_top_company_keys
        if not seo_admitted:
            embedded = _without_seo_readiness(embedded)
            target.observation.web.frontier = embedded
        if target.persist_embedded:
            embedded_groups += len({item.scanner_id for item in embedded.executions})
            try:
                embedded_report = persist_frontier_projection(
                    store,
                    target.candidate_id,
                    embedded,
                    run_id=run_id,
                    force_refresh=False,
                )
                embedded_completed += len(embedded_report.completed_scanners)
                embedded_reused += len(embedded_report.reused_scanners)
                company_key = embedded_report.company_key or company_key
                current = merge_frontier(embedded, embedded_report.frontier)
            except Exception as exc:
                message = (
                    f"{target.candidate.hostname}: embedded frontier: {type(exc).__name__}: {exc}"
                )
                errors.append(message[:1_000])
                error_hosts.append(target.candidate.hostname)
                current = merge_frontier(embedded, store.frontier_intelligence(company_key))
        else:
            # Reused snapshots may contain yesterday's hydrated projection. Never turn that
            # projection into a new execution; only genuinely in-memory extraction is persisted.
            current = merge_frontier(embedded, store.frontier_intelligence(company_key))
        if not seo_admitted:
            current = _without_seo_readiness(current)
        target.observation.web.frontier = current
        discovery = _mapping(embedded.features.get("frontier_discovery"))
        legal_name, locations, phone, company_name = _identity_inputs(
            discovery, target.company_name
        )
        request_urls, request_sources, app_ids = _request_inputs(discovery)
        request_bases, request_evidence_urls = _augment_hiring_request_inputs(
            target.observation,
            company_key,
            request_urls,
            request_sources,
        )
        # Core hydration already extracted these exact pages into the retained
        # embedded projection. Raw HTML is not persisted, so never fabricate a
        # replacement document or retry a URL that the fetch boundary must reject.
        core_urls = _blocked_core_urls(target.observation)
        request_urls = {
            scanner_id: tuple(url for url in urls if _canonical_url(url) not in core_urls)
            for scanner_id, urls in request_urls.items()
        }
        records = {
            scanner_id: _source_records(record_source, scanner_id, legal_name)
            for scanner_id in (
                "dol_lca_workforce_v1",
                "sec_form_d_capital_v1",
                "sf_ai_capital_constellation_v1",
            )
        }
        records["creative_hiring_demand_v1"] = _hiring_records(target.observation)
        scanner_work = store.scanner_work_states([target.candidate_id]).get(
            target.candidate_id,
            (),
        )
        prepared = _PreparedTarget(
            target=target,
            company_key=company_key,
            embedded=embedded,
            current=current,
            discovery=discovery,
            legal_name=legal_name,
            locations=locations,
            phone=phone,
            company_name=company_name,
            request_urls=request_urls,
            request_sources=request_sources,
            request_bases=request_bases,
            request_evidence_urls=request_evidence_urls,
            app_ids=app_ids,
            records=records,
            recent_reusable_scanners=_recent_reusable_scanners(scanner_work),
            scanner_guards=_scanner_guard_states(scanner_work),
            seo_admitted=seo_admitted,
        )
        prepared.network_held = bool(CompanyResearchBudget(
            store.connection, company_key).held_until())
        if prepared.network_held:
            for scanner_id in _NETWORK_SCANNERS:
                prepared.scanner_guards.setdefault(scanner_id, "not_due")
        if manual_ids is None:
            prepared.decisions = _prepare_decisions(prepared, store, run_id)
            selected = next((item for item in prepared.decisions if item.selected), None)
            if selected is not None:
                prepared.selected_scanner = selected.scanner_id
                prepared.selected_eig = selected.expected_information_gain
        prepared_targets.append(prepared)
        if progress:
            progress()

    execution_queue: list[tuple[_PreparedTarget, str]]
    protected_work: list[tuple[_PreparedTarget, str]] = []
    if manual_ids is None:
        eligible = sorted(
            (item for item in prepared_targets if item.selected_scanner),
            key=lambda item: (
                -item.selected_eig,
                -item.target.boundary_score,
                item.company_key,
                item.selected_scanner,
            ),
        )
        eligible_work = [(item, item.selected_scanner) for item in eligible]
    else:
        requested_work = [
            (prepared, scanner_id)
            for prepared in prepared_targets
            for scanner_id in manual_ids
            if (company_scanner_ids is None or scanner_id in company_scanner_ids.get(
                prepared.target.candidate.hostname, ()))
            if _applicability(scanner_id, prepared)[0]
        ]
        protected_work = [
            (prepared, scanner_id)
            for prepared, scanner_id in requested_work
            if ((prepared.network_held and scanner_id in _NETWORK_SCANNERS)
                or (not manual_force_refresh and scanner_id in prepared.scanner_guards))
        ]
        eligible_work = [
            (prepared, scanner_id)
            for prepared, scanner_id in requested_work
            if not (prepared.network_held and scanner_id in _NETWORK_SCANNERS)
            and (manual_force_refresh or scanner_id not in prepared.scanner_guards)
        ]
    execution_queue = eligible_work[:maximum]
    allocated = {
        (prepared.target.candidate_id, scanner_id) for prepared, scanner_id in execution_queue
    }
    rank_by_identity = {
        (prepared.target.candidate_id, scanner_id): index
        for index, (prepared, scanner_id) in enumerate(eligible_work, start=1)
    }

    schedule_count = 0
    for prepared in prepared_targets:
        requested = set(manual_ids or ())
        if company_scanner_ids is not None:
            requested &= set(company_scanner_ids.get(prepared.target.candidate.hostname, ()))
        decided_at = datetime.now(UTC)
        if manual_ids is None:
            decisions = (
                _schedule_model(
                    prepared,
                    raw,
                    actually_selected=(prepared.target.candidate_id, raw.scanner_id) in allocated,
                    budget_rank=rank_by_identity.get(
                        (prepared.target.candidate_id, raw.scanner_id)
                    ),
                    maximum=maximum,
                    decided_at=decided_at,
                )
                for raw in prepared.decisions
            )
        else:
            decisions = (
                _manual_schedule_model(
                    prepared,
                    definition.scanner_id,
                    requested=definition.scanner_id in requested,
                    actually_selected=(
                        prepared.target.candidate_id,
                        definition.scanner_id,
                    )
                    in allocated,
                    guard_state=prepared.scanner_guards.get(definition.scanner_id, ""),
                    budget_rank=rank_by_identity.get(
                        (prepared.target.candidate_id, definition.scanner_id)
                    ),
                    maximum=maximum,
                    decided_at=decided_at,
                )
                for definition in SCANNER_DEFINITIONS
            )
        for decision in decisions:
            store.record_scanner_schedule_decision(
                decision,
                candidate_id=prepared.target.candidate_id,
            )
            schedule_count += 1
            if progress:
                progress()
            if (
                activity
                and manual_ids is not None
                and decision.scanner_id in requested
                and not decision.selected
            ):
                activity(
                    f"{decision.scanner_id} · {prepared.target.candidate.hostname}",
                    decision.selection_reason or decision.applicability_reason or "skipped",
                    "skip",
                )

    preflight_reused = sum(
        prepared.scanner_guards.get(scanner_id) == "reused"
        for prepared, scanner_id in protected_work
    )
    preflight_deferred = len(protected_work) - preflight_reused
    optional_completed = 0
    optional_partial = 0
    optional_reused = 0
    optional_deferred = 0
    optional_failed = 0
    fetch_requests = 0
    fetch_bytes = 0
    selected_scanners = [scanner_id for _, scanner_id in execution_queue]
    robots_cache: dict[str, urllib.robotparser.RobotFileParser | bool] = {}
    robots_locks: dict[str, asyncio.Lock] = {}
    host_locks: dict[str, asyncio.Lock] = {}
    host_failures: dict[str, int] = {}
    host_holds: dict[str, float] = {}
    healthy_hosts: set[str] = set()
    completed_checks = 0
    company_budgets = {
        prepared.company_key: CompanyResearchBudget(store.connection, prepared.company_key)
        for prepared in prepared_targets
    }
    if work_progress:
        work_progress(0, len(execution_queue))

    async def execute(prepared: _PreparedTarget, scanner_id: str) -> None:
        nonlocal optional_completed, optional_partial, optional_reused
        nonlocal optional_deferred, optional_failed, fetch_requests, fetch_bytes
        nonlocal completed_checks
        context = _context(prepared, scanner_id)
        fetcher = None
        if scanner_id in _NETWORK_SCANNERS:
            budget = company_budgets[prepared.company_key]
            if until := budget.held_until():
                optional_deferred += 1
                completed_checks += 1
                if activity:
                    activity(f"{scanner_id} · {prepared.target.candidate.hostname}",
                             f"Company research deferred until {until.isoformat()}", "item")
                if work_progress:
                    work_progress(completed_checks, len(execution_queue))
                if progress:
                    progress()
                return
            allowed = {_canonical_url(url) for url in context.request_urls if _canonical_url(url)}
            fetcher = _FrontierHttpFetcher(
                client=client,
                scanner_id=scanner_id,
                company_key=prepared.company_key,
                primary_url=context.primary_url,
                allowed_urls=allowed,
                request_sources=prepared.request_sources,
                request_bases=prepared.request_bases,
                request_evidence_urls=prepared.request_evidence_urls,
                url_owners={
                    canonical_url: owner
                    for owner, urls in prepared.request_urls.items()
                    for url in urls
                    if (canonical_url := _canonical_url(url))
                },
                app_ids=set(prepared.app_ids),
                blocked_urls=_blocked_core_urls(prepared.target.observation),
                robots_cache=robots_cache,
                robots_locks=robots_locks,
                host_locks=host_locks,
                host_failures=host_failures,
                host_holds=host_holds,
                healthy_hosts=healthy_hosts,
                progress=progress,
                company_budget=budget,
            )
        deadline = asyncio.timeout(FRONTIER_SCANNER_WALL_SECONDS)
        activity_token = REQUEST_ACTIVITY.set(fetcher.observe_request) if fetcher else None
        try:
            async with deadline:
                report: FrontierRuntimeReport = await run_frontier_runtime(
                    store,
                    prepared.target.candidate_id,
                    context,
                    scanner_ids=[scanner_id],
                    run_id=run_id,
                    ssrf_safe_bounded_fetch=fetcher,
                    force_refresh=manual_ids is not None and manual_force_refresh,
                    hydrate_report=False,
                )
            fetch_requests += report.fetch_requests
            fetch_bytes += report.fetch_bytes
            for outcome in report.outcomes:
                if outcome.state == "completed":
                    if outcome.terminal_status == "partial":
                        optional_partial += 1
                        optional_completed += 1
                    elif outcome.terminal_status in SCANNER_FAILURE_STATUSES:
                        optional_failed += 1
                        message = (
                            f"{prepared.target.candidate.hostname}: {scanner_id}: "
                            f"{outcome.error or outcome.terminal_status}"
                        )
                        errors.append(message[:1_000])
                        error_hosts.append(prepared.target.candidate.hostname)
                    else:
                        optional_completed += 1
                elif outcome.state == "reused":
                    optional_reused += 1
                elif outcome.state == "failed":
                    optional_failed += 1
                    message = f"{prepared.target.candidate.hostname}: {scanner_id}: {outcome.error}"
                    errors.append(message[:1_000])
                    error_hosts.append(prepared.target.candidate.hostname)
                else:
                    optional_deferred += 1
                if activity:
                    action_state = outcome.state
                    if outcome.state == "completed" and outcome.terminal_status:
                        action_state = outcome.terminal_status
                    activity(
                        f"{scanner_id} · {prepared.target.candidate.hostname}",
                        outcome.error or action_state.replace("_", " "),
                        (
                            "error"
                            if outcome.state == "failed"
                            or outcome.terminal_status in SCANNER_FAILURE_STATUSES
                            else "item"
                        ),
                    )
        except Exception as exc:
            optional_failed += 1
            if deadline.expired() and fetcher is not None:
                fetcher.defer_interrupted_origins()
            detail = (
                f"Scanner exceeded its {FRONTIER_SCANNER_WALL_SECONDS:g}-second deadline; retryable"
                if isinstance(exc, TimeoutError) else f"{type(exc).__name__}: {exc}"
            )
            message = f"{prepared.target.candidate.hostname}: {scanner_id}: {detail}"
            errors.append(message[:1_000])
            error_hosts.append(prepared.target.candidate.hostname)
            if activity:
                activity(
                    f"{scanner_id} failed · {prepared.target.candidate.hostname}",
                    detail,
                    "error",
                )
        finally:
            if activity_token is not None:
                REQUEST_ACTIVITY.reset(activity_token)
        # Final hydration below publishes all committed checks together. Required
        # dependent parsers read their current inputs inside the scanner runtime.
        completed_checks += 1
        if work_progress:
            work_progress(completed_checks, len(execution_queue))
        if progress:
            progress()

    # Serialize canonical-company work so each check sees the previous publication.
    # Independent companies keep moving while another company's request is slow.
    groups: dict[str, list[tuple[_PreparedTarget, str]]] = {}
    for prepared, scanner_id in execution_queue:
        groups.setdefault(prepared.company_key, []).append((prepared, scanner_id))
    pending = deque(deque(items) for items in groups.values())

    async def worker() -> None:
        while pending:
            group = pending.popleft()
            prepared, scanner_id = group.popleft()
            await execute(prepared, scanner_id)
            if group:
                pending.append(group)

    workers = [asyncio.create_task(worker()) for _ in range(
        min(FRONTIER_COMPANY_WORKERS, len(pending))
    )]
    try:
        await asyncio.gather(*workers)
    finally:
        for task in workers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    # Decisions were appended after the embedded projection. Rehydrate every target so the current
    # snapshot and future feature export carry the complete schedule/evidence projection.
    for prepared in prepared_targets:
        hydrated = store.frontier_intelligence(prepared.company_key)
        current = merge_frontier(
            prepared.embedded,
            hydrated,
        )
        if not prepared.seo_admitted:
            current = _without_seo_readiness(current)
        prepared.target.observation.web.frontier = current
        if progress:
            progress()

    return FrontierBatchSummary(
        targets=len(targets),
        embedded_groups=embedded_groups,
        embedded_completed=embedded_completed,
        embedded_reused=embedded_reused,
        schedule_decisions=schedule_count,
        optional_selected=len(execution_queue),
        optional_completed=optional_completed,
        optional_partial=optional_partial,
        optional_reused=optional_reused + preflight_reused,
        optional_deferred=(
            optional_deferred
            + preflight_deferred
            + max(0, len(eligible_work) - len(execution_queue))
        ),
        optional_failed=optional_failed,
        fetch_requests=fetch_requests,
        fetch_bytes=fetch_bytes,
        selected_scanners=tuple(selected_scanners),
        errors=tuple(dict.fromkeys(errors)),
        error_hosts=tuple(dict.fromkeys(error_hosts)),
    )


__all__ = [
    "FrontierBatchSummary",
    "FrontierRecordSource",
    "FrontierTarget",
    "orchestrate_frontier_intelligence",
]
