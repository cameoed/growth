"""Read saved intelligence into the shared causal score. No collection or database writes.

Adapters contribute bounded evidence, never points per row or inferred buyer confirmation.
Unresolved identities, unsupported concepts and generic graph edges remain research context.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from ..creative_capacity import BuyingSignalEvent
from ..evidence_policy import DURABLE_FAMILIES, DURABLE_HORIZON_DAYS
from ..evidence_policy import VERSION as FRESHNESS_VERSION
from ..value_utils import timezone_datetime as _date
from .contacts import recommend_contacts
from .models import normalize_company_key, validate_source_url
from .visual import CreativeAnnotation, cluster_creatives, evaluate_creative_benchmark
from .workload import (
    CreativeWindow,
    WorkloadEvent,
    _comparable_groups,
    build_workload_intelligence,
    creative_windows_from_observation,
    events_from_observation,
)

VERSION = "hydrated-evidence-v7-microlink-pages"


def _fresh(value, now, days=90):
    stamp = _date(value)
    return stamp if stamp and now - timedelta(days=days) <= stamp <= now else None


def _source(value):
    try:
        return validate_source_url(value)
    except (TypeError, ValueError):
        return None


def _same(value, company):
    try:
        return normalize_company_key(value) == company
    except (TypeError, ValueError):
        return False


def _event(
    company,
    kind,
    family,
    strength,
    source,
    stamp,
    summary,
    root,
    *,
    confidence="medium",
    coverage="partial",
    half_life=30,
    details=None,
    effective_at=None,
    expires_at=None,
):
    event_details = dict(details or {})
    if family in DURABLE_FAMILIES:
        event_details["freshness_policy"] = FRESHNESS_VERSION
        expires_at = expires_at or stamp + timedelta(days=DURABLE_HORIZON_DAYS)
    return BuyingSignalEvent(
        event_key="hydrated:"
        + hashlib.sha256(f"{company}|{kind}|{source}|{stamp.isoformat()}".encode()).hexdigest()[
            :24
        ],
        subject_key=f"domain:{company}",
        event_type=kind,
        family=family,
        polarity="positive",
        strength=strength,
        confidence=confidence,
        coverage=coverage,
        epistemic_status="inferred",
        source_kind="saved_sales_evidence",
        source_url=source,
        observed_at=stamp,
        effective_at=effective_at or stamp,
        expires_at=expires_at or stamp + timedelta(days=90),
        half_life_days=half_life,
        independence_key=root,
        summary=summary,
        details=event_details,
    )


def supplemental_events(
    company,
    observation,
    history,
    *,
    people=None,
    events=(),
    windows=(),
    annotations=(),
    labels=None,
    relationships=(),
    now=None,
):
    """Translate only attributable, current saved facts; correlated inputs share a causal root."""
    now = now or datetime.now(UTC)
    company = normalize_company_key(company)
    result = []
    contacts = recommend_contacts(
        {
            "company_key": company,
            "as_of": now.isoformat(),
            "observed_at": observation.observed_at.isoformat(),
            "public_contacts": observation.contacts.model_dump(mode="json"),
            "licensed_professional_contacts": people or {},
        },
        limit=10,
    )
    for person in contacts["people"]:
        stamp = _fresh(person["observed_at"], now)
        if stamp and (person["profile_url"] or person["email"]):
            result.append(
                _event(
                    company,
                    "hydrated_buyer_contact",
                    "buyer_access",
                    78,
                    person["source_url"],
                    stamp,
                    "A current relevant business contact has a saved professional route.",
                    "access:first_party_contact",
                    coverage="complete",
                    half_life=90,
                    details={"provenance": person["source_kind"],
                             "person_profile": person["profile_url"] or ""},
                    expires_at=min(
                        stamp + timedelta(days=90),
                        _date(person.get("expires_at")) or stamp + timedelta(days=90),
                    ),
                )
            )
    # Grants are directional: the relationship's object is the buyer to introduce.
    # A portfolio logo or generic partnership does not establish a warm introduction.
    latest = {}
    for row in relationships:
        pair = (
            row.get("subject_company_key"),
            row.get("object_company_key"),
            row.get("referrer_name"),
        )
        stamp = _date(row.get("permission_recorded_at"))
        if stamp and stamp <= now and (pair not in latest or stamp > latest[pair][0]):
            latest[pair] = (stamp, row)
    for stamp, row in latest.values():
        source = _source(row.get("source_url"))
        if (
            _same(row.get("object_company_key"), company)
            and row.get("permission_for_intro") == "granted"
            and row.get("referrer_name")
            and not row.get("relationship_ended_at")
            and _fresh(stamp, now)
            and _fresh(row.get("observed_at"), now)
            and source
        ):
            result.append(
                _event(
                    company,
                    "hydrated_permissioned_intro",
                    "buyer_access",
                    85,
                    source,
                    stamp,
                    "A named referrer has granted a current introduction to this company.",
                    "access:permissioned_intro",
                    coverage="complete",
                    half_life=30,
                    details={"relationship_id": str(row.get("id") or "")},
                    expires_at=min(
                        stamp + timedelta(days=90),
                        _date(row.get("observed_at")) + timedelta(days=90),
                    ),
                )
            )
    accepted = []
    for raw in [
        *events_from_observation(observation),
        # Historical job sightings explain persistence but cannot re-open a vanished role.
        *(e for obs in history for e in events_from_observation(obs) if not e.job_id),
        *events,
    ]:
        try:
            event = WorkloadEvent.model_validate(raw)
        except (ValidationError, TypeError):
            continue
        stamp = _fresh(event.observed_at, now)
        if not (
            event.subject_verified
            and stamp
            and _same(event.company_key, company)
            and _source(event.source_url)
        ):
            continue
        # Resolve current/withdrawn state before time eligibility. A fresh withdrawal of an
        # older request must still retire its prior positive, even when the request date is old.
        accepted.append(event)
    latest_events = {}
    for event in sorted(accepted, key=lambda e: (e.observed_at, not e.current)):
        identity = (
            (event.kind, event.source_url, event.job_id)
            if event.kind in {"job_open", "contractor_request", "localization_request"}
            else (event.kind, event.source_url, event.effective_at)
        )
        latest_events[identity] = event
    accepted = []
    for event in latest_events.values():
        current = event.current and (not event.expires_at or event.expires_at > now)
        if event.effective_at and not event.job_id and not _fresh(event.effective_at, now):
            current = False
        accepted.append(event.model_copy(update={"current": current}))
    valid_windows = []
    windows = [
        *windows,
        *(w for obs in [observation, *history] for w in creative_windows_from_observation(obs)),
    ]
    for raw in windows:
        try:
            window = CreativeWindow.model_validate(raw)
        except (ValidationError, TypeError):
            continue
        if (
            _same(window.company_key, company)
            and _fresh(window.observed_at, now, days=365)
            and window.identity_verified
            and window.coverage == "complete"
            and not window.truncated
            and not window.failed_units
            and window.source_urls
            and all(_source(s) for s in window.source_urls)
        ):
            valid_windows.append(window)
    workload = build_workload_intelligence(
        observation,
        history,
        events=accepted,
        ads=valid_windows,
        now=now,
    )
    # Score all accepted requests, independent of the packet's three-task display limit.
    for event in accepted:
        if not event.current or event.kind not in {
            "job_open",
            "contractor_request",
            "localization_request",
        }:
            continue
        deliverable = next(
            (
                d
                for d in event.deliverables
                if isinstance(d.get("deliverable"), str) and d["deliverable"].strip()
            ),
            None,
        )
        if deliverable is None:
            continue
        result.append(
            _event(
                company,
                "hydrated_production_request",
                "production_pressure",
                58,
                event.source_url,
                event.observed_at,
                "A sourced brief describes concrete creative deliverables.",
                "production:hiring" if event.kind == "job_open" else "production:external_request",
                details={
                    "excerpt": event.excerpt[:1000],
                    "work_scope": event.work_scope,
                    "event_key": event.event_key,
                },
                effective_at=(
                    event.observed_at if event.job_id else event.effective_at or event.observed_at
                ),
                expires_at=event.expires_at,
            )
        )
    for event in accepted:
        if event.current and event.kind in {
            "product_launch",
            "campaign_launch",
            "market_expansion",
            "agency_client_win",
        }:
            stamp = _fresh(event.effective_at, now, days=45)
            if stamp and event.time_basis in {"source_explicit", "feed_date"}:
                result.append(
                    _event(
                        company,
                        "hydrated_dated_launch",
                        "commercial_timing",
                        50,
                        event.source_url,
                        stamp,
                        event.title,
                        "timing:first_party_launch",
                        half_life=21,
                    )
                )
    for group in _comparable_groups(valid_windows):
        first, last = group[0], group[-1]
        seen_assets = set(first.asset_keys)
        additions = []
        for window in group[1:]:
            additions.append(set(window.asset_keys) - seen_assets)
            seen_assets.update(window.asset_keys)
        distinct_additions = set().union(*additions) if additions else set()
        recurring = len(group) >= 3 and sum(bool(a) for a in additions) >= 2
        if first.asset_keys and last.asset_keys and distinct_additions:
            result.append(
                _event(
                    company,
                    "hydrated_creative_recurrence" if recurring else "hydrated_creative_turnover",
                    "recurring_workload" if recurring else "proof_match",
                    66 if recurring else 34,
                    last.source_urls[0],
                    last.observed_at,
                    "Comparable complete creative censuses show observed asset identity changes.",
                    "recurring:ad_history"
                    if recurring
                    else f"causal:surface:creative-footprint:{company}",
                    coverage="complete",
                    half_life=45,
                    details={
                        "comparable_windows": str(len(group)),
                        "changed_intervals": str(sum(bool(a) for a in additions)),
                        "semantic_novelty": "unknown",
                        "scope": last.scope,
                        "comparison_dates": json.dumps([w.observed_at.isoformat() for w in group]),
                    },
                    expires_at=min(w.observed_at + timedelta(days=365) for w in group),
                )
            )
    for feature in workload.features:
        citations = feature.evidence
        if not citations or not all(
            _same(c.company_key, company) and _source(c.source_url) and _fresh(c.observed_at, now)
            for c in citations
        ):
            continue
        latest_citation = max(citations, key=lambda c: c.observed_at)
        root = (
            "recurring:scoped_production"
            if feature.family == "recurring_workload"
            else "production:hiring"
            if feature.kind == "persistent_scoped_vacancy"
            else "production:external_request"
        )
        result.append(
            _event(
                company,
                "hydrated_workload_feature",
                feature.family,
                feature.strength,
                latest_citation.source_url,
                latest_citation.observed_at,
                feature.kind,
                root,
                coverage="complete",
                confidence="high",
                half_life=45,
                effective_at=feature.effective_at,
                expires_at=min(feature.expires_at,
                               *(c.observed_at + timedelta(days=90) for c in citations)),
                details={
                    "feature_key": feature.feature_key,
                    "kind": feature.kind,
                    "work_scope": feature.scope,
                    "independent_cycles": str(feature.independent_cycles),
                    "evidence": json.dumps([c.model_dump(mode="json") for c in citations]),
                    "alternatives": json.dumps(feature.alternatives),
                },
            )
        )
    current_annotations = []
    for raw in annotations:
        try:
            annotation = CreativeAnnotation.model_validate(raw)
        except (ValidationError, TypeError):
            continue
        if (
            _fresh(annotation.observed_at, now)
            and _source(annotation.source_url)
            and (not annotation.validated_at or _fresh(annotation.validated_at, now))
        ):
            current_annotations.append(annotation)
    benchmark = evaluate_creative_benchmark(current_annotations, labels) if labels else None
    creative = cluster_creatives(current_annotations, benchmark=benchmark)
    if creative.score_eligible and (creative.inferred_concept_count or 0) >= 2:
        stamp = max(a.observed_at for a in current_annotations)
        result.append(
            _event(
                company,
                "hydrated_validated_creative",
                "proof_match",
                42,
                current_annotations[0].source_url,
                stamp,
                "Validated creative evidence shows multiple concepts in the inspected sample.",
                "proof:first_party_creative",
                half_life=60,
                details={"annotation_assets": json.dumps(
                    [a.asset_key for a in current_annotations]
                )},
                expires_at=min(
                    _date(stamp) + timedelta(days=90)
                    for annotation in current_annotations
                    for stamp in [annotation.observed_at, annotation.validated_at]
                    if stamp is not None
                ),
            )
        )
    return result


@dataclass(frozen=True, slots=True)
class SupplementalInputs:
    """Complete saved inputs owned by one scoring call, never a cross-call cache."""

    company: str
    as_of: datetime
    work: tuple
    annotations: tuple
    labels: tuple
    relationships: tuple
    people: dict
    microlink_pages: tuple = ()
    microlink_boundaries: tuple = ()


def load_supplemental_inputs(store, observation, *, now=None, include_people=True):
    """Read complete ledgers once; legacy databases without optional tables still work."""
    now = now or datetime.now(UTC)
    company = normalize_company_key(observation.canonical_host or observation.hostname)
    connection = store.connection
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    queries = {
        "engine_workload_inputs": (
            "SELECT payload_json FROM engine_workload_inputs WHERE company_key=? "
            "ORDER BY id DESC",
            (company,),
        ),
        "engine_creative_annotations": (
            "SELECT annotation_json FROM engine_creative_annotations a WHERE company_key=? "
            "AND json_extract(CASE WHEN json_valid(annotation_json) THEN annotation_json "
            "ELSE '{}' END,'$.observed_at')<=? "
            "AND id=(SELECT MAX(b.id) FROM engine_creative_annotations b "
            "WHERE b.company_key=a.company_key AND b.asset_key=a.asset_key "
            "AND json_extract(CASE WHEN json_valid(b.annotation_json) THEN b.annotation_json "
            "ELSE '{}' END,'$.observed_at')<=?) ORDER BY id DESC",
            (company, now.isoformat(), now.isoformat()),
        ),
        "engine_concept_labels": (
            "SELECT labels_json FROM engine_concept_labels WHERE company_key=?",
            (company,),
        ),
        "engine_relationships": (
            "SELECT payload_json FROM engine_relationships WHERE object_company_key=? "
            "ORDER BY updated_at DESC",
            (company,),
        ),
    }
    prefetch = getattr(connection, "prefetch", None)
    if prefetch is not None:
        prefetch(query for table, query in queries.items() if table in tables)

    def payloads(table):
        if table not in tables:
            return []
        result = []
        for row in connection.execute(*queries[table]):
            try:
                result.append(json.loads(row[0]))
            except (ValueError, TypeError):
                continue
        return result

    from ..microlink_hydration import future_boundaries, saved_pages

    return SupplementalInputs(
        company=company,
        as_of=now,
        work=tuple(payloads("engine_workload_inputs")),
        annotations=tuple(payloads("engine_creative_annotations")),
        labels=tuple(payloads("engine_concept_labels")),
        relationships=tuple(payloads("engine_relationships")),
        people=store.treg_people_intelligence(company, as_of=now) if include_people else {},
        microlink_pages=(tuple(saved_pages(connection, company, now=now, with_keys=True))
                         if {"atomic_observations", "scanner_executions"} <= tables else ()),
        microlink_boundaries=(future_boundaries(connection, company, now=now)
                              if {"atomic_observations", "scanner_executions"} <= tables else ()),
    )


def load_supplemental_events(
    store, candidate_id, observation, history=(), *, now=None, excluded_source=None,
    inputs: SupplementalInputs | None = None,
    page_scores: dict | None = None,
):
    """Rebuild events for each source removal using that replay's full site/history."""
    now = now or datetime.now(UTC)
    company = normalize_company_key(observation.canonical_host or observation.hostname)
    if inputs is None:
        inputs = load_supplemental_inputs(
            store, observation, now=now, include_people=excluded_source != "contacts",
        )
    if inputs.company != company or inputs.as_of != now:
        raise ValueError("Supplemental inputs require the same company and reference time")
    result = supplemental_events(
        company,
        observation,
        history,
        people=(None if excluded_source == "contacts" else
                inputs.people),
        events=[r for r in inputs.work if isinstance(r, dict) and "kind" in r]
        if excluded_source != "saved_work" else [],
        windows=[r for r in inputs.work if isinstance(r, dict) and "scope" in r]
        if excluded_source not in {"saved_work", "advertising"} else [],
        annotations=inputs.annotations if excluded_source != "creative_annotations" else [],
        labels=(inputs.labels[0]
                if inputs.labels and excluded_source != "creative_annotations" else None),
        relationships=inputs.relationships if excluded_source != "relationships" else [],
        now=now,
    )
    from ..models import DiscoveredHost
    from ..scoring import score_candidate
    from .source_sensitivity import without_source

    for key, original_page in inputs.microlink_pages:
        # Existing stable event keys and causal roots deduplicate repeated page facts,
        # while complementary technology/media extraction from that URL survives.
        page = (without_source(original_page, excluded_source, for_scoring=True) if excluded_source
                else original_page)
        # The owner supplies a cache for this one company/reference-time calculation.
        # Only full page equality can reuse baseline events; changed removals retain
        # their complete scorer. score_candidate owns its private observation copy.
        cache_key = (company, now, key)
        unchanged = page == original_page
        cached = page_scores.get(cache_key) if page_scores is not None and unchanged else None
        signals = cached[1] if cached is not None and cached[0] == original_page else None
        if signals is None:
            signals = score_candidate(
                DiscoveredHost(hostname=company, source_apex=""), page,
                reference_time=now, trace=False,
            ).creative_capacity.signal_events
            if page_scores is not None and unchanged:
                page_scores[cache_key] = (original_page, signals)
        expiry = page.observed_at + timedelta(days=30)
        for event in signals:
            if event.event_type in {"site_unreachable", "site_publicly_gated",
                                    "non_english_presentation", "parked_domain"}:
                # One supplemental page does not redefine the primary site's availability,
                # language or ownership. The original homepage still owns those judgments.
                continue
            result.append(event.model_copy(update={
                "expires_at": min(event.expires_at, expiry) if event.expires_at else expiry,
                "details": {**event.details, "observation_key": key,
                            "provider": "microlink"},
            }))
    return result
