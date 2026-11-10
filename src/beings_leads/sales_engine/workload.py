"""Cited production briefs and conservative sequences over comparable public observations.

This module is pure. It never fetches evidence, writes observations, equates a vacancy with
a hire, or labels a company's ads as underperforming. Every output is a discovery hypothesis.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..hiring_intelligence import extract_deliverable_expectations
from ..models import SiteObservation
from ..value_utils import naive_as_utc as _utc
from .visual import CreativeAnnotation, CreativeClustering, cluster_creatives

VERSION = "workload-sequences-v2-score-first"
MAX_HISTORY = 24
MAX_EVENTS = 500


def _date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if not isinstance(value, str):
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _company(value: str) -> str:
    return (
        value.casefold()
        .removeprefix("domain:")
        .removeprefix("publisher:")
        .removeprefix("www.")
        .rstrip(".")
    )


def _key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


class WorkloadCitation(BaseModel):
    company_key: str
    source_url: str
    excerpt: str
    observed_at: datetime
    effective_at: datetime | None = None
    time_basis: str = "unknown"
    evidence_kind: Literal["observed", "human_confirmed"] = "observed"


class WorkloadEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_key: str = Field(min_length=1, max_length=300)
    company_key: str = Field(min_length=1, max_length=253)
    kind: Literal[
        "product_launch",
        "campaign_launch",
        "market_expansion",
        "agency_client_win",
        "contractor_request",
        "job_open",
        "localization_request",
    ]
    title: str = Field(min_length=1, max_length=500)
    source_url: str = Field(min_length=1, max_length=2_000)
    excerpt: str = Field(min_length=1, max_length=2_000)
    observed_at: datetime
    effective_at: datetime | None = None
    time_basis: Literal["source_explicit", "feed_date", "observed", "unknown"] = "unknown"
    subject_verified: bool = False
    distribution_verified: bool = False
    current: bool = True
    channels: list[str] = Field(default_factory=list, max_length=20)
    formats: list[str] = Field(default_factory=list, max_length=20)
    languages: list[str] = Field(default_factory=list, max_length=20)
    product: str = Field(default="", max_length=300)
    related_entity: str = Field(default="", max_length=300)
    work_scope: str = Field(default="", max_length=300)
    expires_at: datetime | None = None
    job_id: str = Field(default="", max_length=300)
    deliverables: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None


class CreativeWindow(BaseModel):
    """One bounded surface census; identities are assets, never automatically concepts."""

    model_config = ConfigDict(extra="forbid")

    company_key: str
    scope: str = Field(min_length=1, max_length=2_000)
    observed_at: datetime
    window_start: datetime | None = None
    window_end: datetime | None = None
    coverage: Literal["none", "partial", "complete"] = "partial"
    identity_verified: bool = False
    truncated: bool = False
    failed_units: int = Field(default=0, ge=0)
    asset_keys: list[str] = Field(default_factory=list, max_length=500)
    concept_keys: list[str] = Field(default_factory=list, max_length=500)
    concepts_validated: bool = False
    languages: list[str] = Field(default_factory=list, max_length=100)
    source_urls: list[str] = Field(default_factory=list, max_length=30)
    channel: str = Field(default="", max_length=100)
    limitations: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def valid_window(self) -> CreativeWindow:
        if bool(self.window_start) != bool(self.window_end):
            raise ValueError("Collection windows need both start and end")
        if (
            self.window_start
            and self.window_end
            and _utc(self.window_start) > _utc(self.window_end)
        ):
            raise ValueError("Collection window ends before its start")
        return self


class WorkloadSequence(BaseModel):
    sequence_key: str
    kind: Literal[
        "launches_outpace_observed_refresh",
        "localization_coverage_gap",
        "client_win_then_contractors",
        "persistent_vacancy_with_campaigns",
    ]
    title: str
    hypothesis: str
    evidence: list[WorkloadCitation]
    first_observed_at: datetime
    last_observed_at: datetime
    observation_windows: int
    confidence: Literal["low", "medium"] = "medium"
    counter_hypotheses: list[str]
    confirmation_questions: list[str]
    limitations: list[str] = Field(default_factory=list)


class WorkloadTask(BaseModel):
    task_key: str
    company_key: str
    title: str
    description: str
    trigger_kind: str
    evidence: list[WorkloadCitation]
    required_skills: list[str]
    channel_tags: list[str]
    formats: list[str]
    languages: list[str]
    estimated_hours_min: float
    estimated_hours_max: float
    effort_basis: str = "planning assumption; confirm workload and scope with the buyer"
    effort_assumptions: list[str]
    missing_inputs: list[str]
    counter_hypotheses: list[str]
    status: Literal["hypothesis"] = "hypothesis"


class WorkloadFeature(BaseModel):
    """Internal evidence join. Counts describe observations, never hours, spend or probability."""

    feature_key: str
    kind: str
    family: Literal["recurring_workload", "production_pressure", "proof_match"]
    strength: int = Field(ge=0, le=100)
    scope: str
    independent_cycles: int = 0
    evidence: list[WorkloadCitation]
    effective_at: datetime
    expires_at: datetime
    alternatives: list[str] = Field(default_factory=list)


class WorkloadIntelligence(BaseModel):
    version: str = VERSION
    company_key: str
    as_of: datetime
    current_observed_at: datetime
    source_event_count: int
    comparable_windows: int
    sequences: list[WorkloadSequence] = Field(default_factory=list)
    features: list[WorkloadFeature] = Field(default_factory=list)
    tasks: list[WorkloadTask] = Field(default_factory=list)
    creative: CreativeClustering | None = None
    limitations: list[str] = Field(default_factory=list)


def _citation(event: WorkloadEvent) -> WorkloadCitation:
    return WorkloadCitation(
        company_key=event.company_key,
        source_url=event.source_url,
        excerpt=event.excerpt,
        observed_at=event.observed_at,
        effective_at=event.effective_at,
        time_basis=event.time_basis,
    )


def _event_time(event: WorkloadEvent) -> datetime:
    return _utc(event.effective_at or event.observed_at)


def _recency_time(event: WorkloadEvent) -> datetime:
    return _utc(event.observed_at) if event.job_id else _event_time(event)


def _source_time(event: WorkloadEvent) -> bool:
    return bool(event.effective_at and event.time_basis in {"source_explicit", "feed_date"})


def _job_events(observation: SiteObservation) -> list[WorkloadEvent]:
    """A saved snapshot is the observation clock, including cached ATS records."""
    result = []
    for job in observation.web.job_postings[:100]:
        raw = job.model_dump()
        if not raw.get("current", True) or not {"creative", "photo_video_production"}.intersection(
            job.role_families
        ):
            continue
        if job.valid_through and _utc(job.valid_through) < _utc(observation.observed_at):
            continue
        first = _date(raw.get("first_observed_at"))
        last = _date(raw.get("last_observed_at"))
        # Old snapshots have no receipt clock; the original snapshot time is the upper bound.
        observed = min(_utc(observation.observed_at), last) if last else observation.observed_at
        job_id = raw.get("stable_id") or _key(job.provider, job.source_url, job.title)
        deliverables = list(raw.get("deliverable_expectations") or [])
        if not deliverables:
            deliverables = [
                asdict(item)
                for item in extract_deliverable_expectations(
                    job.description_excerpt, source_url=job.source_url
                )
            ]
        posted = job.posted_at if raw.get("posted_time_basis") == "source_explicit" else None
        contract = bool(raw.get("contract_signals"))
        result.append(
            WorkloadEvent(
                event_key=f"job:{job_id}",
                company_key=_company(observation.canonical_host or observation.hostname),
                kind="contractor_request" if contract else "job_open",
                title=job.title,
                source_url=job.source_url,
                excerpt=(job.description_excerpt or f"Public role: {job.title}")[:2_000],
                observed_at=observed,
                effective_at=posted,
                time_basis="source_explicit" if posted else "observed",
                subject_verified=True,
                current=True,
                job_id=job_id,
                channels=list(raw.get("channels") or []),
                formats=list(raw.get("formats") or []),
                deliverables=deliverables,
                first_observed_at=min(first, _utc(observed)) if first else observed,
                last_observed_at=last if last and last <= _utc(observed) else None,
            )
        )
    return result


def _frontier_job_events(observation: SiteObservation) -> list[WorkloadEvent]:
    """Read hydrated literal posting records without requiring another website scan.

    Old records containing only a role/title remain context. No title becomes a duty, quota,
    permanent hire, or outsourcing request. Posting aggregates share the same hiring root.
    """
    company = _company(observation.canonical_host or observation.hostname)
    frontier = observation.web.frontier
    artifacts = {a.artifact_key: a for a in frontier.evidence_artifacts}
    manifests = {m.manifest_key: m for m in frontier.coverage_manifests}
    result = []
    for item in frontier.observations[:2_000]:
        if (
            item.scanner_id != "creative_hiring_demand_v1"
            or item.predicate != "hiring.job_posting"
            or not isinstance(item.value, dict)
            or item.epistemic_status != "observed"
            or item.validity_status == "disputed"
            or _company(item.company_key) != company
            or _company(item.source_actor_key) != company
            or item.qualifiers.get("claim_basis") != "literal_public_posting"
        ):
            continue
        value = item.value
        if not {"creative", "photo_video_production"}.intersection(
            value.get("role_families") or []
        ):
            continue
        if value.get("exclusion_reasons"):
            continue
        addressed = any(
            a.scanner_id == item.scanner_id
            and _company(a.company_key) == company
            and a.content_hash
            for key in item.artifact_keys
            if (a := artifacts.get(key)) is not None
        )
        covered = any(
            m.scanner_id == item.scanner_id
            and m.completeness in {"partial", "complete"}
            and m.successful_units > 0
            for key in item.coverage_keys
            if (m := manifests.get(key)) is not None
        )
        if not addressed or not covered:
            continue
        stable_id, title = str(value.get("stable_id") or ""), str(value.get("title") or "")
        source = str(value.get("source_url") or item.source_url)
        if not stable_id or not title or not source.startswith(("http://", "https://")):
            continue
        observed = _utc(item.last_observed_at or item.first_observed_at)
        receipt = _date(value.get("last_observed_at"))
        if receipt:
            observed = min(observed, receipt)
        excerpt = str(value.get("description_excerpt") or "")[:2_000]
        deliverables = [
            dict(d)
            for d in value.get("deliverable_expectations", [])
            if isinstance(d, dict) and str(d.get("deliverable") or "").strip()
        ][:20]
        if not deliverables and excerpt:
            deliverables = [
                asdict(d) for d in extract_deliverable_expectations(excerpt, source_url=source)
            ]
        expiry = min(
            [
                observed + timedelta(days=14),
                *[d for d in (_date(value.get("valid_through")), item.expires_at) if d],
            ]
        )
        current = (
            value.get("current", True) is True
            and item.validity_status != "expired"
            and str(value.get("state_reason") or "").casefold()
            not in {"closed", "withdrawn", "filled", "expired"}
        )
        posted = item.effective_at if item.effective_time_basis == "source_explicit" else None
        result.append(
            WorkloadEvent(
                event_key=f"job:{stable_id}",
                job_id=stable_id,
                company_key=company,
                kind="contractor_request" if value.get("contract_signals") else "job_open",
                title=title[:500],
                source_url=source,
                excerpt=excerpt or f"Public role: {title}",
                observed_at=observed,
                effective_at=posted,
                time_basis="source_explicit" if posted else "observed",
                current=current,
                subject_verified=True,
                expires_at=expiry,
                channels=[str(v) for v in value.get("channels", [])][:20],
                formats=[str(v) for v in value.get("formats", [])][:20],
                work_scope=str(value.get("work_scope") or "")[:300],
                related_entity=str(value.get("related_entity") or "")[:300],
                deliverables=deliverables,
                first_observed_at=_date(value.get("first_observed_at")) or item.first_observed_at,
                last_observed_at=observed,
            )
        )
    return result


def events_from_observation(observation: SiteObservation) -> list[WorkloadEvent]:
    """Adapt existing company-bound structured evidence; aggregate counts cannot become events."""
    company = _company(observation.canonical_host or observation.hostname)
    events = [*_job_events(observation), *_frontier_job_events(observation)]
    mapping = {
        "product.release_current": "product_launch",
        "product.release_item": "product_launch",
        "campaign.launch": "campaign_launch",
        "market.expansion": "market_expansion",
        "agency.client_win": "agency_client_win",
        "production.contractor_request": "contractor_request",
        "localization.request": "localization_request",
    }
    for item in observation.web.frontier.observations[:2_000]:
        if item.predicate not in mapping or not isinstance(item.value, dict):
            continue
        value = item.value
        if _company(item.company_key) != company or item.epistemic_status != "observed":
            continue
        if item.validity_status == "disputed":
            continue
        if (
            mapping[item.predicate] == "product_launch"
            and value.get("release_type") != "substantive"
        ):
            continue
        # Feed/source dates only; sitemap lastmod and scan dates cannot manufacture launch timing.
        if (
            item.effective_time_basis not in {"source_explicit", "feed_date"}
            or not item.effective_at
        ):
            continue
        title = str(value.get("title") or "").strip()
        source = str(value.get("url") or item.source_url)
        if not title or not source:
            continue
        source_actor = _company(item.source_actor_key)
        # An explicitly different actor (for example a quoted customer) is not the buyer's
        # request merely because the company published it on its own website.
        if source_actor and source_actor != company:
            continue
        attributed = source_actor == company or _company(urlsplit(source).hostname or "") == company
        if not attributed:
            continue
        excerpt = str(value.get("excerpt") or item.summary or title)[:2_000]
        deliverables = []
        if mapping[item.predicate] in {"contractor_request", "localization_request"}:
            structured = value.get("deliverables") or value.get("deliverable_expectations")
            if isinstance(structured, list):
                deliverables = [dict(raw) for raw in structured if isinstance(raw, dict)][:20]
            if not deliverables:
                deliverables = [
                    asdict(raw)
                    for raw in extract_deliverable_expectations(excerpt, source_url=source)
                ]
        events.append(
            WorkloadEvent(
                event_key=str(
                    value.get("event_key") or _key(mapping[item.predicate], source, title)
                ),
                company_key=company,
                kind=mapping[item.predicate],
                title=title,
                source_url=source,
                excerpt=excerpt,
                # Frontier refreshes can be newer than the saved website snapshot. Preserve the
                # actual receipt clock; build_workload_intelligence applies its global as-of cutoff.
                observed_at=_utc(item.last_observed_at or item.first_observed_at),
                effective_at=item.effective_at,
                time_basis=item.effective_time_basis,
                subject_verified=True,
                distribution_verified=value.get("distribution_verified") is True,
                current=(
                    item.validity_status != "expired"
                    and value.get("current", True) is True
                    and str(value.get("status", "")).casefold()
                    not in {"withdrawn", "cancelled", "closed", "fulfilled", "resolved"}
                ),
                expires_at=item.expires_at,
                work_scope=str(value.get("work_scope") or "")[:300],
                channels=[str(v) for v in value.get("channels", [])][:20],
                formats=[str(v) for v in value.get("formats", [])][:20],
                languages=[str(v) for v in value.get("languages", [])][:20],
                product=str(value.get("product") or title)[:300],
                related_entity=str(value.get("related_entity") or "")[:300],
                deliverables=deliverables,
            )
        )
    return events


def creative_windows_from_observation(observation: SiteObservation) -> list[CreativeWindow]:
    company = _company(observation.canonical_host or observation.hostname)
    frontier = observation.web.frontier
    results = []
    for manifest in frontier.coverage_manifests:
        if manifest.scanner_id != "creative_asset_genome_v1":
            continue
        stamp = _utc(manifest.observed_at)
        assets = [
            item
            for item in frontier.observations
            if item.predicate in {"creative.asset_concept", "creative.asset_identity"}
            and manifest.manifest_key in item.coverage_keys
            and _company(item.company_key) == company
            and _utc(item.first_observed_at) <= stamp
        ]
        # Legacy observations may lack the manifest association; do not mix different scopes.
        if not assets:
            assets = [
                item
                for item in frontier.observations
                if item.predicate in {"creative.asset_concept", "creative.asset_identity"}
                and item.source_url == manifest.scope
                and _company(item.company_key) == company
                and _utc(item.first_observed_at) <= stamp
            ]
        results.append(
            CreativeWindow(
                company_key=company,
                scope=manifest.scope or manifest.surface,
                observed_at=stamp,
                coverage=manifest.completeness
                if manifest.completeness != "not_applicable"
                else "none",
                identity_verified=True,
                truncated=manifest.truncated,
                failed_units=manifest.failed_units,
                asset_keys=sorted(
                    {
                        str(item.qualifiers.get("source_master_url") or item.entity_key)
                        for item in assets
                    }
                )[:500],
                source_urls=manifest.source_urls or ([manifest.scope] if manifest.scope else []),
                channel="website",
                limitations=[
                    "Asset URLs are not concepts; changed bytes under a URL may be missed."
                ],
            )
        )
    return results


def _complete(window: CreativeWindow) -> bool:
    return bool(
        window.coverage == "complete"
        and window.identity_verified
        and not window.truncated
        and not window.failed_units
        and window.source_urls
    )


def _comparable_groups(windows: Sequence[CreativeWindow]) -> list[list[CreativeWindow]]:
    grouped: dict[str, list[CreativeWindow]] = defaultdict(list)
    for window in windows:
        if _complete(window):
            grouped[window.scope].append(window)
    groups = []
    for values in grouped.values():
        retained: list[CreativeWindow] = []
        for window in sorted(values, key=lambda item: _utc(item.observed_at)):
            if retained:
                previous = retained[-1]
                if _utc(window.observed_at) - _utc(previous.observed_at) < timedelta(days=7):
                    continue
                if bool(window.window_start) != bool(previous.window_start):
                    continue
                if window.window_start and previous.window_start:
                    duration = _utc(window.window_end) - _utc(window.window_start)
                    old_duration = _utc(previous.window_end) - _utc(previous.window_start)
                    if duration.total_seconds() <= 0 or old_duration.total_seconds() <= 0:
                        continue
                    if not 0.8 <= duration / old_duration <= 1.25:
                        continue
                    if _utc(window.window_start) < _utc(previous.window_end):
                        continue
            retained.append(window)
        if len(retained) >= 2:
            groups.append(retained)
    return groups


def _window_citation(window: CreativeWindow) -> WorkloadCitation:
    return WorkloadCitation(
        company_key=window.company_key,
        source_url=window.source_urls[0],
        excerpt=(
            f"Complete bounded census of {window.scope}: "
            f"{len(set(window.asset_keys))} asset identities."
        ),
        observed_at=window.observed_at,
        time_basis="observed",
    )


def _build_sequences(
    company: str,
    events: Sequence[WorkloadEvent],
    sightings: Sequence[WorkloadEvent],
    groups: Sequence[Sequence[CreativeWindow]],
    now: datetime,
) -> list[WorkloadSequence]:
    sequences = []
    launches = [
        item
        for item in events
        if item.kind in {"product_launch", "campaign_launch"} and _source_time(item)
    ]
    for group in groups:
        first, last = group[0], group[-1]
        in_window = [
            item
            for item in launches
            if _utc(first.observed_at) < _event_time(item) <= _utc(last.observed_at)
        ]
        unique_dates = {_event_time(item).date() for item in in_window}
        observed_new = set(last.asset_keys) - set(first.asset_keys)
        if (
            len(in_window) >= 2
            and len(unique_dates) >= 2
            and last.asset_keys
            and len(observed_new) <= 1
        ):
            semantic = first.concepts_validated and last.concepts_validated
            if semantic and len(set(last.concept_keys) - set(first.concept_keys)) > 1:
                continue
            metric = "validated concept" if semantic else "asset identity"
            sequences.append(
                WorkloadSequence(
                    sequence_key=_key(company, "launch_refresh", first.scope),
                    kind="launches_outpace_observed_refresh",
                    title=f"Repeated launches with little observed {metric} refresh",
                    hypothesis=(
                        f"{len(in_window)} dated launches occurred while this sampled surface "
                        f"added at most one {metric}. Check whether launch creative is queued."
                    ),
                    evidence=[
                        *[_citation(item) for item in in_window[:6]],
                        _window_citation(first),
                        _window_citation(last),
                    ],
                    first_observed_at=first.observed_at,
                    last_observed_at=last.observed_at,
                    observation_windows=len(group),
                    counter_hypotheses=[
                        "Creative may ship on other channels or under existing asset URLs.",
                        "Existing creative may meet the buyer's needs and perform well.",
                        "These releases may require no new marketing production.",
                    ],
                    confirmation_questions=[
                        "Which of these launches has an approved but unshipped creative brief?",
                        "Which channel and production owner would use the first deliverable?",
                    ],
                    limitations=[note for note in last.limitations][:5],
                )
            )
        expansions = [
            item
            for item in events
            if item.kind == "market_expansion"
            and item.languages
            and _source_time(item)
            and _utc(first.observed_at) < _event_time(item) <= _utc(last.observed_at)
        ]
        for expansion in expansions[:3]:
            missing = set(expansion.languages) - set(last.languages)
            requests = [
                item
                for item in events
                if item.kind == "localization_request"
                and set(item.languages) & missing
                and _event_time(item) >= _event_time(expansion)
            ]
            # Absence needs a validated language census and a concrete localization brief.
            if not missing or not last.concepts_validated or not requests:
                continue
            sequences.append(
                WorkloadSequence(
                    sequence_key=_key(company, "localization", expansion.event_key, last.scope),
                    kind="localization_coverage_gap",
                    title=f"Localization brief after {expansion.title}",
                    hypothesis=(
                        "A cited expansion and localization request name "
                        f"{', '.join(sorted(missing))}; "
                        "those languages are absent from the inspected creative census."
                    ),
                    evidence=[_citation(expansion), _citation(requests[0]), _window_citation(last)],
                    first_observed_at=first.observed_at,
                    last_observed_at=last.observed_at,
                    observation_windows=len(group),
                    counter_hypotheses=[
                        "Localized creative may already exist on uninspected channels.",
                        "Localization may be handled internally or by another supplier.",
                    ],
                    confirmation_questions=[
                        "Which market, approved copy and adaptations remain unshipped?"
                    ],
                )
            )
    wins = [item for item in events if item.kind == "agency_client_win" and _source_time(item)]
    for win in wins[:5]:
        requests = [
            item
            for item in events
            if item.kind == "contractor_request"
            and timedelta(0) <= _event_time(item) - _event_time(win) <= timedelta(days=45)
            and item.deliverables
        ]
        if not requests:
            continue
        request = requests[-1]
        sequences.append(
            WorkloadSequence(
                sequence_key=_key(company, "win_contractors", win.event_key, request.event_key),
                kind="client_win_then_contractors",
                title=f"Production request after {win.title}",
                hypothesis="A client win is followed by a specific external production request.",
                evidence=[_citation(win), _citation(request)],
                first_observed_at=win.observed_at,
                last_observed_at=request.observed_at,
                observation_windows=2,
                counter_hypotheses=[
                    "The contractor request may support a different client or an evergreen roster.",
                    "The agency may already have assigned this production work.",
                ],
                confirmation_questions=[
                    "Does this request support the new client, and what paid work remains open?"
                ],
            )
        )
    by_job: dict[str, list[WorkloadEvent]] = defaultdict(list)
    for event in sightings:
        if event.job_id and event.last_observed_at is not None:
            by_job[event.job_id].append(event)
    for event in events:
        if not event.job_id or not event.deliverables or not event.current:
            continue
        observed = sorted({_utc(item.observed_at) for item in by_job[event.job_id]})
        if len(observed) < 3 or observed[-1] - observed[0] < timedelta(days=21):
            continue
        # Require three genuine sightings at least seven days apart; repeated cached records fail.
        separated = [observed[0]]
        for stamp in observed[1:]:
            if stamp - separated[-1] >= timedelta(days=7):
                separated.append(stamp)
        campaigns = [item for item in launches if observed[0] <= _event_time(item) <= observed[-1]]
        if len(separated) < 3 or len(campaigns) < 2 or now - observed[-1] > timedelta(days=14):
            continue
        first_job = min(by_job[event.job_id], key=lambda item: _utc(item.observed_at))
        sequences.append(
            WorkloadSequence(
                sequence_key=_key(company, "persistent_job", event.job_id),
                kind="persistent_vacancy_with_campaigns",
                title=f"Continuing campaigns while {event.title} remains listed",
                hypothesis=(
                    f"The same production role was observed {len(separated)} times over "
                    f"{(observed[-1] - observed[0]).days} days while dated launches continued."
                ),
                evidence=[
                    _citation(first_job),
                    _citation(event),
                    *[_citation(item) for item in campaigns[:3]],
                ],
                first_observed_at=observed[0],
                last_observed_at=observed[-1],
                observation_windows=len(separated),
                counter_hypotheses=[
                    "The listing may be evergreen, a replacement, or filled but not removed.",
                    "Permanent hiring does not establish willingness or budget to outsource.",
                ],
                confirmation_questions=[
                    "Is the role open, and which tasks could an interim specialist own?"
                ],
            )
        )
    return list({item.sequence_key: item for item in sequences}.values())[:12]


def _explicit_scope_match(left: WorkloadEvent, right: WorkloadEvent) -> bool:
    for field in ("work_scope", "related_entity", "product"):
        a, b = (
            str(getattr(left, field)).strip().casefold(),
            str(getattr(right, field)).strip().casefold(),
        )
        if a and b:
            return a == b
    return False


def _scope_match(left: WorkloadEvent, right: WorkloadEvent) -> bool:
    if any(
        getattr(left, field) and getattr(right, field)
        for field in ("work_scope", "related_entity", "product")
    ):
        return _explicit_scope_match(left, right)
    return bool(set(left.channels) & set(right.channels)) and bool(
        set(left.formats) & set(right.formats)
    )


def _window_matches(event: WorkloadEvent, window: CreativeWindow) -> bool:
    return bool(
        (event.work_scope and event.work_scope == window.scope)
        or (window.channel and window.channel in event.channels)
    )


def _structured_features(events, sightings, groups, now):
    result = []

    def emit(kind, family, strength, scope, selected, *, windows=(), cycles=0, alternatives=()):
        citations = [_citation(e) for e in selected]
        citations.extend(_window_citation(w) for w in windows)
        effective = max(_event_time(e) for e in selected)
        expiry = min(
            [
                now + timedelta(days=90),
                *[
                    (_utc(e.expires_at) if e.expires_at else _recency_time(e) + timedelta(days=90))
                    for e in selected
                ],
            ]
        )
        if expiry <= now:
            return
        result.append(
            WorkloadFeature(
                feature_key=_key(kind, scope, *sorted(e.event_key for e in selected)),
                kind=kind,
                family=family,
                strength=strength,
                scope=scope,
                independent_cycles=cycles,
                evidence=citations,
                effective_at=effective,
                expires_at=expiry,
                alternatives=list(alternatives),
            )
        )

    launches = [
        e for e in events if e.kind in {"campaign_launch", "product_launch"} and _source_time(e)
    ]
    # Copies of the same dated source are one cycle even when imported under new row keys.
    launches = list({(e.kind, e.source_url, _event_time(e).date()): e for e in launches}.values())
    used = set()
    for seed in launches:
        peers = [e for e in launches if _scope_match(seed, e) or e is seed]
        dates = sorted({_event_time(e).date() for e in peers})
        if len(dates) < 2 or (dates[-1] - dates[0]).days < 7:
            continue
        supported = [
            e
            for e in peers
            if e.deliverables
            and e.channels
            and (
                e.distribution_verified
                or any(_window_matches(e, w) and w.channel for group in groups for w in group)
            )
        ]
        supported_dates = sorted({_event_time(e).date() for e in supported})
        if len(supported_dates) < 2 or (supported_dates[-1] - supported_dates[0]).days < 7:
            continue
        scope = seed.work_scope or seed.related_entity or seed.product or ",".join(seed.channels)
        if scope in used:
            continue
        used.add(scope)
        emit(
            "repeated_campaign_deliverables",
            "recurring_workload",
            78,
            scope,
            supported,
            cycles=len(supported_dates),
            alternatives=("Published work may already be fully staffed.",),
        )
        for request in events:
            if request.kind != "contractor_request" or not request.deliverables:
                continue
            # Procurement must name the same work/client/product. Shared channel or
            # format vocabulary alone cannot establish that an agency is buying capacity.
            scoped = [e for e in supported if _explicit_scope_match(request, e)]
            scoped_dates = sorted({_event_time(e).date() for e in scoped})
            if len(scoped_dates) >= 2 and (scoped_dates[-1] - scoped_dates[0]).days >= 7:
                emit(
                    "recurring_work_with_external_request",
                    "production_pressure",
                    82,
                    request.work_scope or request.related_entity or request.product,
                    [*scoped, request],
                    cycles=len(scoped_dates),
                    alternatives=("Approved service budget and unassigned work remain unknown.",),
                )
    for group in groups:
        first, last = group[0], group[-1]
        selected = [
            e
            for e in launches
            if _window_matches(e, last)
            and _utc(first.observed_at) < _event_time(e) <= _utc(last.observed_at)
        ]
        dates = {_event_time(e).date() for e in selected}
        if len(dates) >= 2 and set(last.asset_keys) - set(first.asset_keys):
            emit(
                "launches_with_creative_turnover",
                "recurring_workload",
                72,
                last.scope,
                selected,
                windows=[first, last],
                cycles=len(dates),
                alternatives=("Asset turnover is not semantic novelty, workload hours or spend.",),
            )
    for expansion in events:
        if expansion.kind != "market_expansion" or not _source_time(expansion):
            continue
        for request in events:
            if (
                request.kind == "localization_request"
                and request.deliverables
                and set(expansion.languages) & set(request.languages)
                and _scope_match(expansion, request)
                and timedelta(0)
                <= _event_time(request) - _event_time(expansion)
                <= timedelta(days=60)
            ):
                emit(
                    "expansion_with_localization_brief",
                    "production_pressure",
                    72,
                    expansion.work_scope or expansion.product,
                    [expansion, request],
                    alternatives=("The one-off adaptation does not establish recurring demand.",),
                )
    for win in events:
        if win.kind != "agency_client_win" or not _source_time(win):
            continue
        for request in events:
            if (
                request.kind == "contractor_request"
                and request.deliverables
                and _scope_match(win, request)
                and timedelta(0) <= _event_time(request) - _event_time(win) <= timedelta(days=45)
            ):
                emit(
                    "agency_client_production_procurement",
                    "production_pressure",
                    74,
                    win.work_scope or win.related_entity,
                    [win, request],
                    alternatives=("A client win alone does not establish a retained engagement.",),
                )
    by_job = defaultdict(list)
    for event in sightings:
        if event.job_id and event.last_observed_at and event.current:
            by_job[event.job_id].append(event)
    for event in events:
        if not event.job_id or not event.deliverables:
            continue
        observations = sorted(
            {
                _utc(e.last_observed_at)
                for e in by_job[event.job_id]
                if _utc(e.last_observed_at) <= _utc(e.observed_at) <= now
            }
        )
        spaced = []
        for stamp in observations:
            if not spaced or stamp - spaced[-1] >= timedelta(days=7):
                spaced.append(stamp)
        campaigns = [e for e in launches if _scope_match(event, e)]
        if (
            len(spaced) >= 3
            and spaced[-1] - spaced[0] >= timedelta(days=21)
            and now - spaced[-1] <= timedelta(days=14)
            and len({_event_time(e).date() for e in campaigns}) >= 2
        ):
            emit(
                "persistent_scoped_vacancy",
                "production_pressure",
                64,
                event.work_scope or event.job_id,
                [by_job[event.job_id][0], event, *campaigns],
                cycles=len(spaced),
                alternatives=(
                    "Evergreen or replacement role.",
                    "Filled listing may remain visible.",
                ),
            )
    return sorted(
        {f.feature_key: f for f in result}.values(),
        key=lambda f: (f.strength, f.effective_at),
        reverse=True,
    )[:24]


def _task(event: WorkloadEvent, sequence: WorkloadSequence | None = None) -> WorkloadTask | None:
    hiring = event.kind in {"job_open", "contractor_request", "localization_request"}
    if hiring and not event.deliverables:
        return None
    deliverable = event.deliverables[0] if event.deliverables else {}
    channels = list(dict.fromkeys([*event.channels, *deliverable.get("channels", [])]))
    formats = list(dict.fromkeys([*event.formats, *deliverable.get("formats", [])]))
    context = " ".join(
        [event.title, *formats, str(deliverable.get("deliverable") or "")]
    ).casefold()
    video = any(term in context for term in ("video", "motion", "animation"))
    localized = event.kind == "localization_request" or "localization" in context
    skills = [
        "video production"
        if video
        else "image production"
        if "image" in context
        else "creative concept development"
    ]
    if localized:
        skills.append("localization")
    low, high = (8.0, 16.0) if video else (2.0, 6.0) if localized else (4.0, 12.0)
    evidence = [_citation(event)]
    if deliverable.get("excerpt"):
        evidence[0] = evidence[0].model_copy(
            update={"excerpt": str(deliverable["excerpt"])[:2_000]}
        )
    if sequence:
        evidence.extend(
            item
            for item in sequence.evidence
            if item.source_url not in {existing.source_url for existing in evidence}
        )
    if hiring:
        subject = str(deliverable.get("deliverable") or "production")
        title = f"Scope {subject} from the {event.title} brief"
        description = (
            f"Validate the cited responsibility in {event.title}, then deliver one approved "
            f"{subject} pilot using the buyer's actual brief and existing inputs."
        )
    elif event.kind in {"product_launch", "campaign_launch"}:
        title = f"Develop one launch concept for {event.product or event.title}"
        description = (
            f"Review the launch described in {event.title} and develop one approved creative "
            "concept for its intended audience and channel, after confirming a real production gap."
        )
    else:
        return None
    missing = [
        "Approved brief and first deliverable",
        "Budget owner and approval process",
        "Rights, brand constraints and AI permission",
        "Current production process and expense this would replace",
        "Actual recurring queue for the next 90 days",
    ]
    if not channels:
        missing.append("Target distribution channel")
    if not formats:
        missing.append("Required asset format, dimensions and duration")
    if localized and not event.languages:
        missing.append("Target languages and native-language approver")
    counters = (
        sequence.counter_hypotheses
        if sequence
        else [
            "This work may already be completed or covered by an existing team or supplier.",
            "The public signal does not establish an approved service budget or recurring backlog.",
        ]
    )
    if event.kind == "job_open":
        counters = [
            *counters,
            "A permanent hiring budget may not be transferable to outsourced production.",
        ]
    return WorkloadTask(
        task_key=_key(
            event.company_key, event.event_key, str(deliverable.get("deliverable") or "concept")
        ),
        company_key=event.company_key,
        title=title[:200],
        description=description,
        trigger_kind=event.kind,
        evidence=evidence[:10],
        required_skills=skills,
        channel_tags=channels[:20],
        formats=formats[:20],
        languages=event.languages,
        estimated_hours_min=low,
        estimated_hours_max=high,
        effort_assumptions=[
            "One sequential pilot deliverable with supplied source materials and approved copy.",
            "One review round; confirm actual review needs and expected throughput with the buyer.",
            "Published hiring quotas describe the role; they are not a Beings capacity commitment.",
        ],
        missing_inputs=missing,
        counter_hypotheses=counters,
    )


def build_workload_intelligence(
    current: SiteObservation,
    history: Sequence[SiteObservation] = (),
    *,
    events: Sequence[WorkloadEvent | Mapping[str, Any]] = (),
    ads: Sequence[CreativeWindow | Mapping[str, Any]] = (),
    annotations: Sequence[CreativeAnnotation | Mapping[str, Any]] = (),
    now: datetime | None = None,
) -> WorkloadIntelligence:
    """Synthesize at most three grounded tasks; missing evidence means fewer tasks, never filler."""
    reference = _utc(now or current.observed_at)
    company = _company(current.canonical_host or current.hostname)
    snapshots = [
        item
        for item in [*history, current]
        if _company(item.canonical_host or item.hostname) == company
        and _utc(item.observed_at) <= reference
    ]
    snapshots.sort(key=lambda item: _utc(item.observed_at))
    limitations = [
        "All production tasks and bottlenecks are hypotheses for buyer confirmation.",
        "Public creative inventory does not reveal spend, ROI, fatigue or purchase intent.",
    ]
    sightings = [event for snapshot in snapshots for event in events_from_observation(snapshot)]
    supplied_keys: set[str] = set()
    for raw in events:
        try:
            event = raw if isinstance(raw, WorkloadEvent) else WorkloadEvent.model_validate(raw)
            sightings.append(event)
            supplied_keys.add(event.event_key)
        except (ValidationError, ValueError, TypeError):
            limitations.append("An invalid supplied workload event was excluded.")
    sightings = [
        item
        for item in sightings
        if _company(item.company_key) == company
        and item.subject_verified
        and _utc(item.observed_at) <= reference
        and _event_time(item) <= reference
        and reference - _recency_time(item) <= timedelta(days=120)
    ]
    latest: dict[str, WorkloadEvent] = {}
    current_job_keys = {
        item.event_key for item in events_from_observation(current) if item.job_id and item.current
    }
    for event in sorted(sightings, key=lambda item: _utc(item.observed_at)):
        # Retain older job sightings for sequence comparison, not as still-open jobs today.
        if (
            event.job_id
            and event.event_key not in current_job_keys
            and event.event_key not in supplied_keys
        ):
            continue
        latest[event.event_key] = event
    reconciled = {}
    for event in sorted(latest.values(), key=lambda e: (_utc(e.observed_at), not e.current)):
        identity = (
            (event.kind, event.source_url, event.job_id)
            if event.kind in {"job_open", "contractor_request", "localization_request"}
            else (event.kind, event.source_url, event.effective_at)
        )
        reconciled[identity] = event
    active = [
        item
        for item in reconciled.values()
        if item.current
        and reference - _recency_time(item) <= timedelta(days=90)
        and (not item.expires_at or _utc(item.expires_at) > reference)
    ]
    windows = [
        window for snapshot in snapshots for window in creative_windows_from_observation(snapshot)
    ]
    for raw in ads:
        try:
            windows.append(
                raw if isinstance(raw, CreativeWindow) else CreativeWindow.model_validate(raw)
            )
        except (ValidationError, ValueError, TypeError):
            limitations.append("An invalid supplied creative collection window was excluded.")
    windows = [
        item
        for item in windows
        if _company(item.company_key) == company
        and _utc(item.observed_at) <= reference
        and reference - _utc(item.observed_at) <= timedelta(days=120)
    ]
    groups = _comparable_groups(windows)
    sequences = _build_sequences(company, active, sightings, groups, reference)
    features = _structured_features(active, sightings, groups, reference)
    if not groups:
        limitations.append(
            "At least two complete, comparable creative censuses seven days apart are needed."
        )
    if reference - _utc(current.observed_at) > timedelta(days=30):
        limitations.append(
            "The current website observation is older than 30 days; reconfirm active work."
        )
    candidates = sorted(
        active,
        key=lambda item: (
            item.kind == "contractor_request",
            any(
                item.source_url == cite.source_url
                for sequence in sequences
                for cite in sequence.evidence
            ),
            _event_time(item),
        ),
        reverse=True,
    )
    tasks: list[WorkloadTask] = []
    seen_sources: set[str] = set()
    for event in candidates:
        if event.source_url in seen_sources:
            continue
        sequence = next(
            (
                item
                for item in sequences
                if any(cite.source_url == event.source_url for cite in item.evidence)
            ),
            None,
        )
        task = _task(event, sequence)
        if task:
            tasks.append(task)
            seen_sources.add(event.source_url)
        if len(tasks) >= 3:
            break
    creative = None
    if annotations:
        try:
            accepted = [
                item
                if isinstance(item, CreativeAnnotation)
                else CreativeAnnotation.model_validate(item)
                for item in annotations
            ]
            accepted = [item for item in accepted if _utc(item.observed_at) <= reference]
            creative = cluster_creatives(accepted)
        except (ValidationError, ValueError, TypeError):
            limitations.append(
                "Invalid creative annotations require correction before concept analysis."
            )
    if not tasks:
        limitations.append(
            "No current company-attributed launch or executable job brief supports a task."
        )
    return WorkloadIntelligence(
        company_key=company,
        as_of=reference,
        current_observed_at=current.observed_at,
        source_event_count=len(active),
        comparable_windows=sum(len(group) for group in groups),
        sequences=sequences,
        features=features,
        tasks=tasks,
        creative=creative,
        limitations=list(dict.fromkeys(limitations)),
    )
