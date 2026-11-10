from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

import httpx
import tldextract

from .models import AtomicObservation, EvidenceArtifact, ScannerCoverageManifest
from .provider_rate_limit import (
    ProviderRateLimitDeferred,
    dataforseo_rate_limit_code,
    provider_request,
)
from .rehydration import REHYDRATION_DAYS
from .value_utils import canonical_json_hash as _hash

SCANNER_ID = "dataforseo_search_visibility_v1"
SCANNER_VERSION = "1.0.0"
EXTRACTOR_VERSION = "dataforseo-labs-google-v1"
RULE_VERSION = "search-visibility-capacity-v1"
LOCATION_CODE = 2840
LANGUAGE_CODE = "en"
REFRESH_DAYS = REHYDRATION_DAYS
EVIDENCE_DAYS = 30
TRAJECTORY_FEATURE_KEY = (
    "derived|dataforseo_search_visibility_v1|seo.visibility_trajectory"
)
TRAJECTORY_SCORE_POLICY = "dataforseo_search_visibility_trajectory_v1"
BULK_LIMIT = 1_000
BULK_ENDPOINT = (
    "https://api.dataforseo.com/v3/dataforseo_labs/google/"
    "bulk_traffic_estimation/live"
)
DOMAIN_OVERVIEW_ENDPOINT = (
    "https://api.dataforseo.com/v3/dataforseo_labs/google/"
    "domain_rank_overview/live"
)
_POSITION_FIELDS = (
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
_SEO_SUFFIXES = tldextract.TLDExtract(
    suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True,
)


class DataForSEOError(RuntimeError):
    """A sanitized DataForSEO transport or response-contract failure."""


@dataclass(frozen=True, slots=True)
class DataForSEOResult:
    endpoint: str
    task_id: str
    cost_usd: Decimal
    response_hash: str
    response_bytes: int
    items: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class DataForSEOProjection:
    artifacts: tuple[EvidenceArtifact, ...]
    coverage_manifests: tuple[ScannerCoverageManifest, ...]
    observations: tuple[AtomicObservation, ...]


def _domain(value: object) -> str:
    raw = str(value or "").strip().lower().rstrip(".")
    if raw.startswith("www."):
        raw = raw[4:]
    if not raw or "://" in raw or "/" in raw or len(raw) > 253:
        raise DataForSEOError("DataForSEO returned an invalid target identity")
    try:
        normalized = raw.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise DataForSEOError("DataForSEO returned an invalid target identity") from exc
    labels = normalized.split(".")
    if (
        len(labels) < 2
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or re.fullmatch(r"[a-z0-9-]+", label) is None
            for label in labels
        )
    ):
        raise DataForSEOError("DataForSEO returned an invalid target identity")
    return normalized


@lru_cache(maxsize=8192)
def seo_registered_domain(value: str) -> str:
    """Resolve SEO collection scope locally without merging independent hosted tenants."""
    try:
        host = _domain(value)
    except DataForSEOError:
        return ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return ""
    parts = _SEO_SUFFIXES(host)
    if parts.suffix and not parts.domain:
        return ""
    return parts.top_domain_under_public_suffix or host


def group_seo_cohort(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep ranked representatives and merge a frozen legacy cohort's root freshness."""
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        root = seo_registered_domain(str(item.get("canonical_domain") or ""))
        if not root:
            continue
        if root not in grouped:
            grouped[root] = {**item, "seo_domain": root, "rank": len(grouped) + 1}
            continue
        current = grouped[root]
        current["seo_due"] = bool(current.get("seo_due", True)) and bool(item.get("seo_due", True))
        if str(item.get("last_gathered_at") or "") > str(current.get("last_gathered_at") or ""):
            current["last_gathered_at"] = item.get("last_gathered_at")
            current["next_due_at"] = item.get("next_due_at")
    return list(grouped.values())


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DataForSEOError(f"DataForSEO response is missing {label}")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise DataForSEOError(f"DataForSEO response is missing {label}")
    return value


def _nonnegative_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataForSEOError(f"DataForSEO returned an invalid {label}")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise DataForSEOError(f"DataForSEO returned an invalid {label}")
    return normalized


def _nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DataForSEOError(f"DataForSEO returned an invalid {label}")
    return value


def _optional_metric(value: object, label: str) -> dict[str, Any]:
    if value is None:
        return {"etv": 0.0, "count": 0}
    metric = _mapping(value, label)
    return {
        "etv": _nonnegative_number(metric.get("etv", 0), f"{label} etv"),
        "count": _nonnegative_integer(metric.get("count", 0), f"{label} count"),
    }


def _status_code(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _safe_status_message(value: object) -> str:
    return " ".join(str(value or "DataForSEO request failed").split())[:240]


def _task_envelope(
    payload: object,
    *,
    raw: bytes,
    endpoint: str,
) -> tuple[Mapping[str, Any], Decimal]:
    envelope = _mapping(payload, "response envelope")
    if _status_code(envelope.get("status_code")) != 20000:
        raise DataForSEOError(_safe_status_message(envelope.get("status_message")))
    tasks = _sequence(envelope.get("tasks"), "tasks")
    if len(tasks) != 1:
        raise DataForSEOError("DataForSEO response must contain exactly one task")
    task = _mapping(tasks[0], "task")
    if _status_code(task.get("status_code")) != 20000:
        raise DataForSEOError(_safe_status_message(task.get("status_message")))
    expected_path = endpoint.removeprefix("https://api.dataforseo.com/").strip("/").split("/")
    if list(_sequence(task.get("path"), "task path")) != expected_path:
        raise DataForSEOError("DataForSEO response path did not match the requested endpoint")
    try:
        cost = Decimal(str(task.get("cost", envelope.get("cost", 0))))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DataForSEOError("DataForSEO returned an invalid task cost") from exc
    if not cost.is_finite() or cost < 0:
        raise DataForSEOError("DataForSEO returned an invalid task cost")
    if not raw:
        raise DataForSEOError("DataForSEO returned an empty response")
    return task, cost


def parse_bulk_response(
    payload: object,
    *,
    requested_targets: Sequence[str],
    raw: bytes | None = None,
) -> DataForSEOResult:
    targets = tuple(dict.fromkeys(_domain(target) for target in requested_targets))
    if not targets or len(targets) > BULK_LIMIT:
        raise ValueError(f"Bulk traffic estimation requires 1-{BULK_LIMIT} unique targets")
    body = raw or json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    task, cost = _task_envelope(payload, raw=body, endpoint=BULK_ENDPOINT)
    task_data = _mapping(task.get("data"), "task request data")
    if (
        tuple(_domain(target) for target in _sequence(task_data.get("targets"), "task targets"))
        != targets
        or _nonnegative_integer(task_data.get("location_code"), "location code")
        != LOCATION_CODE
        or str(task_data.get("language_code") or "").lower() != LANGUAGE_CODE
        or "include_clickstream_data" in task_data
    ):
        raise DataForSEOError("DataForSEO response request identity did not match")
    results = _sequence(task.get("result"), "task result")
    if len(results) != 1:
        raise DataForSEOError("DataForSEO bulk response must contain one result")
    result = _mapping(results[0], "bulk result")
    raw_items_value = result.get("items")
    raw_items = [] if raw_items_value is None else _sequence(raw_items_value, "bulk items")
    result_location = result.get("location_code")
    result_language = result.get("language_code")
    if raw_items and (
        _nonnegative_integer(result_location, "result location code") != LOCATION_CODE
        or str(result_language or "").lower() != LANGUAGE_CODE
    ):
        raise DataForSEOError("DataForSEO bulk result market did not match")
    if not raw_items and not (
        result_location is None
        and result_language is None
        or result_location == LOCATION_CODE
        and str(result_language or "").lower() == LANGUAGE_CODE
    ):
        raise DataForSEOError("DataForSEO bulk result market did not match")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_item in raw_items:
        item = _mapping(raw_item, "bulk item")
        target = _domain(item.get("target"))
        if target not in targets or target in normalized:
            raise DataForSEOError("DataForSEO bulk response target did not match")
        metrics_value = item.get("metrics")
        metrics = {} if metrics_value is None else _mapping(metrics_value, "bulk metrics")
        normalized[target] = {
            "target": target,
            "location_code": LOCATION_CODE,
            "language_code": LANGUAGE_CODE,
            "organic": _optional_metric(metrics.get("organic"), "organic"),
            "paid": _optional_metric(metrics.get("paid"), "paid"),
            "featured_snippet": _optional_metric(
                metrics.get("featured_snippet"), "featured snippet"
            ),
            "local_pack": _optional_metric(metrics.get("local_pack"), "local pack"),
            "provider_estimate": True,
            "data_available": bool(metrics),
        }
    for target in targets:
        normalized.setdefault(
            target,
            {
                "target": target,
                "location_code": LOCATION_CODE,
                "language_code": LANGUAGE_CODE,
                "organic": {"etv": 0.0, "count": 0},
                "paid": {"etv": 0.0, "count": 0},
                "featured_snippet": {"etv": 0.0, "count": 0},
                "local_pack": {"etv": 0.0, "count": 0},
                "provider_estimate": True,
                "data_available": False,
            },
        )
    return DataForSEOResult(
        endpoint=BULK_ENDPOINT,
        task_id=str(task.get("id") or "")[:100],
        cost_usd=cost,
        response_hash=hashlib.sha256(body).hexdigest(),
        response_bytes=len(body),
        items=tuple(normalized[target] for target in targets),
    )


def parse_domain_overview_response(
    payload: object,
    *,
    requested_target: str,
    raw: bytes | None = None,
) -> DataForSEOResult:
    target = _domain(requested_target)
    body = raw or json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    task, cost = _task_envelope(payload, raw=body, endpoint=DOMAIN_OVERVIEW_ENDPOINT)
    task_data = _mapping(task.get("data"), "task request data")
    if (
        _domain(task_data.get("target")) != target
        or _nonnegative_integer(task_data.get("location_code"), "location code")
        != LOCATION_CODE
        or str(task_data.get("language_code") or "").lower() != LANGUAGE_CODE
        or "include_clickstream_data" in task_data
    ):
        raise DataForSEOError("DataForSEO response request identity did not match")
    results = _sequence(task.get("result"), "task result")
    if len(results) != 1:
        raise DataForSEOError("DataForSEO domain overview must contain one result")
    result = _mapping(results[0], "domain overview result")
    if _domain(result.get("target")) != target:
        raise DataForSEOError("DataForSEO domain overview identity did not match")
    raw_items_value = result.get("items")
    raw_items = (
        []
        if raw_items_value is None
        else _sequence(raw_items_value, "domain overview items")
    )
    if len(raw_items) > 1:
        raise DataForSEOError("DataForSEO domain overview returned too many items")
    result_location = result.get("location_code")
    result_language = result.get("language_code")
    if raw_items and (
        _nonnegative_integer(result_location, "result location code") != LOCATION_CODE
        or str(result_language or "").lower() != LANGUAGE_CODE
    ):
        raise DataForSEOError("DataForSEO domain overview identity did not match")
    if not raw_items and not (
        result_location is None
        and result_language is None
        or result_location == LOCATION_CODE
        and str(result_language or "").lower() == LANGUAGE_CODE
    ):
        raise DataForSEOError("DataForSEO domain overview identity did not match")
    raw_item = _mapping(raw_items[0], "domain overview item") if raw_items else {}
    metrics_value = raw_item.get("metrics")
    metrics = (
        {}
        if metrics_value is None
        else _mapping(metrics_value, "domain overview metrics")
    )
    organic_value = metrics.get("organic")
    organic_raw = (
        {} if organic_value is None else _mapping(organic_value, "organic ranking metrics")
    )
    paid_raw = metrics.get("paid")
    organic = _optional_metric(organic_raw, "organic")
    organic["estimated_paid_traffic_cost"] = _nonnegative_number(
        organic_raw.get("estimated_paid_traffic_cost", 0),
        "organic estimated paid traffic cost",
    )
    for field in _POSITION_FIELDS:
        organic[field] = _nonnegative_integer(organic_raw.get(field, 0), field)
    ranked_total = sum(int(organic[field]) for field in _POSITION_FIELDS)
    if ranked_total > int(organic["count"]):
        raise DataForSEOError("DataForSEO organic rank buckets exceed the reported count")
    paid = _optional_metric(paid_raw, "paid")
    if isinstance(paid_raw, Mapping):
        paid["estimated_paid_traffic_cost"] = _nonnegative_number(
            paid_raw.get("estimated_paid_traffic_cost", 0),
            "paid estimated traffic cost",
        )
    item = {
        "target": target,
        "location_code": LOCATION_CODE,
        "language_code": LANGUAGE_CODE,
        "organic": organic,
        "paid": paid,
        "provider_estimate": True,
        "data_available": bool(metrics),
    }
    return DataForSEOResult(
        endpoint=DOMAIN_OVERVIEW_ENDPOINT,
        task_id=str(task.get("id") or "")[:100],
        cost_usd=cost,
        response_hash=hashlib.sha256(body).hexdigest(),
        response_bytes=len(body),
        items=(item,),
    )


class DataForSEOClient:
    def __init__(
        self,
        login: str,
        password: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not login.strip() or not password:
            raise ValueError("DataForSEO login and API password are required")
        self.login = login.strip()
        self.password = password
        self.client = client

    async def _post(self, endpoint: str, task: Mapping[str, Any]) -> tuple[object, bytes]:
        if endpoint not in {BULK_ENDPOINT, DOMAIN_OVERVIEW_ENDPOINT}:
            raise ValueError("Unsupported DataForSEO endpoint")
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
        )
        try:
            response = await provider_request(client, "POST",
                endpoint,
                auth=httpx.BasicAuth(self.login, self.password),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                json=[dict(task)],
            )
            if response.status_code != 200:
                raise DataForSEOError(f"DataForSEO request failed with HTTP {response.status_code}")
            raw = bytes(response.content)
            try:
                payload = response.json()
            except ValueError as exc:
                raise DataForSEOError("DataForSEO returned invalid JSON") from exc
            if rate_code := dataforseo_rate_limit_code(payload):
                raise DataForSEOError(
                    f"DataForSEO rate_limited: provider code {rate_code}; retry deferred"
                )
            return payload, raw
        except ProviderRateLimitDeferred as exc:
            raise DataForSEOError(str(exc)) from exc
        except httpx.TimeoutException as exc:
            raise DataForSEOError("DataForSEO request timed out") from exc
        except httpx.RequestError as exc:
            raise DataForSEOError("DataForSEO request could not be completed") from exc
        finally:
            if owns_client:
                await client.aclose()

    async def bulk_traffic_estimation(self, targets: Sequence[str]) -> DataForSEOResult:
        normalized = tuple(dict.fromkeys(_domain(target) for target in targets))
        if not normalized or len(normalized) > BULK_LIMIT:
            raise ValueError(f"Bulk traffic estimation requires 1-{BULK_LIMIT} unique targets")
        payload, raw = await self._post(
            BULK_ENDPOINT,
            {
                "targets": list(normalized),
                "location_code": LOCATION_CODE,
                "language_code": LANGUAGE_CODE,
                "item_types": ["organic", "paid", "featured_snippet", "local_pack"],
            },
        )
        return parse_bulk_response(payload, requested_targets=normalized, raw=raw)

    async def domain_rank_overview(self, target: str) -> DataForSEOResult:
        normalized = _domain(target)
        payload, raw = await self._post(
            DOMAIN_OVERVIEW_ENDPOINT,
            {
                "target": normalized,
                "location_code": LOCATION_CODE,
                "language_code": LANGUAGE_CODE,
                "limit": 1,
            },
        )
        return parse_domain_overview_response(
            payload,
            requested_target=normalized,
            raw=raw,
        )


def _qualifies(value: Mapping[str, Any], stage: str) -> bool:
    organic = _mapping(value.get("organic"), "projected organic metrics")
    organic_count = _nonnegative_integer(organic.get("count"), "organic count")
    organic_etv = _nonnegative_number(organic.get("etv"), "organic etv")
    paid = _mapping(value.get("paid"), "projected paid metrics")
    paid_count = _nonnegative_integer(paid.get("count"), "paid count")
    if stage == "domain_overview":
        top_ten = sum(
            _nonnegative_integer(organic.get(field), field)
            for field in _POSITION_FIELDS[:3]
        )
        return organic_count >= 25 and (top_ten >= 1 or organic_etv >= 100)
    return organic_count >= 25 or organic_etv >= 100 or paid_count >= 1


def project_dataforseo_item(
    item: Mapping[str, Any],
    *,
    stage: str,
    observed_at: datetime | None = None,
    task_id: str = "",
    response_hash: str = "",
    company_domain: str | None = None,
) -> DataForSEOProjection:
    if stage not in {"bulk", "domain_overview"}:
        raise ValueError("DataForSEO projection stage must be bulk or domain_overview")
    now = observed_at or datetime.now(UTC)
    measured = _domain(item.get("target"))
    company = _domain(company_domain) if company_domain is not None else measured
    if company != measured and (
        not seo_registered_domain(measured)
        or seo_registered_domain(company) != seo_registered_domain(measured)
        or measured != seo_registered_domain(measured)
    ):
        raise DataForSEOError("DataForSEO recipient is outside the measured root domain")
    value = json.loads(json.dumps(dict(item), sort_keys=True))
    if value.get("provider_estimate") is not True:
        raise DataForSEOError("DataForSEO projection requires provider-estimate provenance")
    data_available = value.get("data_available")
    if not isinstance(data_available, bool):
        raise DataForSEOError("DataForSEO projection requires explicit data availability")
    if value.get("location_code") != LOCATION_CODE or value.get("language_code") != LANGUAGE_CODE:
        raise DataForSEOError("DataForSEO projection market did not match")
    qualifies = company == measured and data_available and _qualifies(value, stage)
    source_url = BULK_ENDPOINT if stage == "bulk" else DOMAIN_OVERVIEW_ENDPOINT
    content_hash = _hash(value)
    suffix = content_hash[:24]
    artifact_key = f"artifact:{SCANNER_ID}:{stage}:{company}:{suffix}"
    coverage_key = f"coverage:{SCANNER_ID}:{stage}:{company}:{suffix}"
    observation_key = f"observation:{SCANNER_ID}:{stage}:{company}:{suffix}"
    predicate = (
        "seo.search_visibility_estimate"
        if stage == "bulk"
        else "seo.domain_rank_overview"
    )
    organic = _mapping(value.get("organic"), "organic metrics")
    excerpt = (
        f"DataForSEO {stage.replace('_', ' ')} for {measured}: organic ETV "
        f"{round(float(organic.get('etv') or 0), 2)}, "
        f"{int(organic.get('count') or 0)} ranked organic result(s)."
    )
    manifest = ScannerCoverageManifest(
        manifest_key=coverage_key,
        scanner_id=SCANNER_ID,
        scanner_version=SCANNER_VERSION,
        surface=f"dataforseo_labs_google_{stage}",
        scope=company,
        attempted_units=1,
        successful_units=1,
        total_units=1,
        completeness="complete",
        truncated=False,
        absence_interpretable=False,
        limitations=[
            "DataForSEO modeled estimates are not first-party analytics",
            "United States English market only",
            "Search visibility does not establish buyer intent, budget, or creative demand",
            *([f"Metrics describe {measured}, not the recipient subdomain"]
              if company != measured else []),
        ],
        source_urls=[source_url],
        observed_at=now,
        metadata={"provider": "DataForSEO", "task_id": task_id[:100],
                  "measured_domain": measured},
    )
    artifact = EvidenceArtifact(
        artifact_key=artifact_key,
        company_key=company,
        scanner_id=SCANNER_ID,
        scanner_version=SCANNER_VERSION,
        extractor_version=EXTRACTOR_VERSION,
        artifact_type="licensed_api_structured_metrics",
        canonical_url=source_url,
        original_url=source_url,
        publisher="DataForSEO",
        fetched_at=now,
        status_code=200,
        mime_type="application/json",
        content_hash=content_hash,
        byte_count=len(json.dumps(value, sort_keys=True).encode()),
        robots_state="not_applicable",
        excerpt=excerpt,
        locator=f"task:{task_id or 'unknown'} target:{measured}",
        retention_policy="normalized_metrics_hash_and_bounded_excerpt",
        license_policy="dataforseo_api_terms_apply",
        metadata={
            "provider": "DataForSEO",
            "task_id": task_id[:100],
            "response_hash": response_hash[:128],
            "full_response_retained": False,
            "measured_domain": measured,
        },
    )
    observation = AtomicObservation(
        observation_key=observation_key,
        company_key=company,
        scanner_id=SCANNER_ID,
        scanner_version=SCANNER_VERSION,
        extractor_version=EXTRACTOR_VERSION,
        rule_id=predicate,
        rule_version=RULE_VERSION,
        entity_key=f"dataforseo-google-us-en:{measured}",
        predicate=predicate,
        value_type="json",
        value=value,
        qualifiers={
            "score_policy": "dataforseo_search_visibility_capacity_v1",
            "provider": "DataForSEO",
            "provider_estimate": True,
            "rank_measured": stage == "domain_overview",
            "traffic_is_estimated": True,
            "first_party_analytics": False,
            "location_code": LOCATION_CODE,
            "language_code": LANGUAGE_CODE,
            "coverage_complete": True,
            "clickstream_included": False,
            "qualifies_as_ability_proxy": qualifies,
            "measured_domain": measured,
            "recipient_domain": company,
            "scope_matches_company": company == measured,
        },
        summary=excerpt,
        source_url=source_url,
        epistemic_status="observed",
        validity_status="current",
        confidence="medium",
        confidence_method="licensed_provider_structured_response",
        first_observed_at=now,
        last_observed_at=now,
        expires_at=now + timedelta(days=EVIDENCE_DAYS),
        artifact_keys=[artifact_key],
        coverage_keys=[coverage_key],
        score_eligible=qualifies,
        discovery_only=False,
        source_actor_key="provider:dataforseo",
        origin_fingerprint=f"dataforseo:google:{LOCATION_CODE}:{LANGUAGE_CODE}:{stage}",
        causal_root_key=f"seo-search-visibility:{measured}",
        metadata={
            "provider_task_id": task_id[:100],
            "response_hash": response_hash[:128],
            "projection_score_gate": "exact_dataforseo_search_visibility",
            "prohibited_claims": [
                "first-party traffic",
                "revenue",
                "available budget",
                "creative demand",
                "purchase intent",
            ],
        },
    )
    return DataForSEOProjection(
        artifacts=(artifact,),
        coverage_manifests=(manifest,),
        observations=(observation,),
    )


__all__ = [
    "BULK_ENDPOINT",
    "BULK_LIMIT",
    "DOMAIN_OVERVIEW_ENDPOINT",
    "DataForSEOClient",
    "DataForSEOError",
    "DataForSEOProjection",
    "DataForSEOResult",
    "LANGUAGE_CODE",
    "LOCATION_CODE",
    "REFRESH_DAYS",
    "SCANNER_ID",
    "TRAJECTORY_FEATURE_KEY",
    "TRAJECTORY_SCORE_POLICY",
    "parse_bulk_response",
    "parse_domain_overview_response",
    "project_dataforseo_item",
]
