from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .models import AtomicObservation, EvidenceArtifact, ScannerCoverageManifest

SCANNER_VERSION = "1.0.0"
EXTRACTOR_VERSION = "market-projection-v1"
RULE_VERSION = "market-evidence-safety-v1"

SOURCE_SCANNER_IDS: Mapping[str, str] = {
    "sec_form_d_atom_v1": "sec_legal_capital_stream_v1",
    "sec_registration_atom_v1": "sec_legal_capital_stream_v1",
    "sec_current_report_atom_v1": "sec_legal_capital_stream_v1",
    "sec_crowdfunding_atom_v1": "sec_legal_capital_stream_v1",
    "sec_reg_a_atom_v1": "sec_legal_capital_stream_v1",
    "sec_litigation_rss_v1": "government_action_award_stream_v1",
    "federal_register_documents_v1": "government_action_award_stream_v1",
    "gdelt_doc_metadata_v2": "open_news_radar_v1",
    "hacker_news_show_v0": "open_news_radar_v1",
    "hacker_news_new_v0": "open_news_radar_v1",
}

_SEC_ITEM_EVENT_TYPES: Mapping[str, str] = {
    "1.01": "material_agreement_disclosure",
    "2.01": "acquisition_or_disposition_disclosure",
    "2.02": "results_of_operations_disclosure",
    "3.02": "unregistered_securities_sale_disclosure",
    "5.02": "leadership_change_disclosure",
}
_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "ref_src",
    "vero_conv",
    "vero_id",
}
_AMOUNT_PROHIBITED_CLAIMS: Mapping[str, tuple[str, ...]] = {
    "amount_sold_as_of_filing": (
        "cash received",
        "money raised beyond the literal filed amount-sold field",
        "runway",
        "valuation",
    ),
    "total_offering_amount": (
        "cash received",
        "money raised",
        "runway",
        "valuation",
    ),
    "award_ceiling": ("cash received", "recognized revenue", "obligation"),
    "obligation": ("cash received", "recognized revenue", "total contract value"),
    "payment": ("recurring revenue", "available budget", "willingness to buy"),
    "unknown": ("cash received", "revenue", "available budget"),
}


@dataclass(frozen=True, slots=True)
class MarketEvidenceProjection:
    """Typed, append-only evidence payload produced from one resolved market batch."""

    artifacts: tuple[EvidenceArtifact, ...]
    coverage_manifests: tuple[ScannerCoverageManifest, ...]
    observations: tuple[AtomicObservation, ...]

    def persistence_kwargs(
        self,
    ) -> dict[
        str,
        tuple[EvidenceArtifact, ...]
        | tuple[ScannerCoverageManifest, ...]
        | tuple[AtomicObservation, ...],
    ]:
        """Return names accepted by the append-only scanner completion transaction."""

        return {
            "artifacts": self.artifacts,
            "coverage_manifests": self.coverage_manifests,
            "observations": self.observations,
        }


def project_market_batch(
    batch: Mapping[str, Any],
    *,
    company_key: str,
    observed_at: datetime | str | None = None,
) -> MarketEvidenceProjection:
    """Project ``MarketParseResult.to_store_batch()`` into typed ledger evidence.

    ``company_key`` is a caller-resolved canonical company domain. The caller must pass only
    documents assigned to that company. ``observed_at`` is only needed for an empty batch,
    because non-empty parser documents already carry their observation timestamp.
    """

    company = _company_key(company_key)
    source_id = _text(batch.get("source_id"), 200)
    try:
        scanner_id = SOURCE_SCANNER_IDS[source_id]
    except KeyError as exc:
        raise ValueError(f"unsupported market source_id: {source_id or '<empty>'}") from exc

    source_policy_version = _text(batch.get("source_policy_version"), 100)
    raw_documents = batch.get("documents", ())
    if not _is_sequence(raw_documents):
        raise ValueError("market batch documents must be a JSON array")
    documents = tuple(_as_mapping(item, "document") for item in raw_documents)
    batch_observed_at = _projection_time(documents, observed_at)

    rejected_items = _nonnegative_int(batch.get("rejected_items"))
    warnings = [_text(item, 500) for item in _sequence(batch.get("warnings")) if _text(item)]
    artifacts: dict[str, EvidenceArtifact] = {}
    observations: dict[str, AtomicObservation] = {}

    for document in documents:
        artifact = _project_artifact(
            document,
            company=company,
            scanner_id=scanner_id,
            source_id=source_id,
            source_policy_version=source_policy_version,
            fallback_observed_at=batch_observed_at,
        )
        artifacts.setdefault(artifact.artifact_key, artifact)
        for observation in _project_document_observations(
            document,
            artifact=artifact,
            company=company,
            scanner_id=scanner_id,
            source_id=source_id,
            source_policy_version=source_policy_version,
        ):
            observations.setdefault(observation.observation_key, observation)

    manifest = _project_manifest(
        batch,
        documents=documents,
        artifacts=tuple(artifacts.values()),
        company=company,
        scanner_id=scanner_id,
        source_id=source_id,
        source_policy_version=source_policy_version,
        observed_at=batch_observed_at,
        rejected_items=rejected_items,
        warnings=warnings,
    )
    normalized_observations = tuple(
        observation.model_copy(update={"coverage_keys": [manifest.manifest_key]})
        for observation in _enforce_one_score_per_causal_root(tuple(observations.values()))
    )
    return MarketEvidenceProjection(
        artifacts=tuple(artifacts.values()),
        coverage_manifests=(manifest,),
        observations=normalized_observations,
    )


def _project_artifact(
    document: Mapping[str, Any],
    *,
    company: str,
    scanner_id: str,
    source_id: str,
    source_policy_version: str,
    fallback_observed_at: datetime,
) -> EvidenceArtifact:
    canonical_url = _canonical_url(document.get("canonical_url"))
    if not canonical_url:
        raise ValueError("market document requires a canonical public HTTP(S) URL")
    document_id = _text(document.get("document_id"), 500)
    if not document_id:
        raise ValueError("market document requires document_id")
    document_observed_at = _datetime(document.get("observed_at")) or fallback_observed_at
    published_at, published_basis = _document_source_time(document, source_id)
    content_hash = _content_hash(document)
    artifact_key = _stable_key(
        "artifact", scanner_id, company, canonical_url, content_hash
    )
    title = _text(document.get("title"), 1_000)
    summary = _text(document.get("summary"), 1_500)
    excerpt = _text(" — ".join(value for value in (title, summary) if value), 2_000)
    publisher = _text(document.get("publisher_name"), 500) or _host(canonical_url)
    metadata = _mapping(document.get("metadata"))
    return EvidenceArtifact(
        artifact_key=artifact_key,
        company_key=company,
        scanner_id=scanner_id,
        scanner_version=SCANNER_VERSION,
        extractor_version=EXTRACTOR_VERSION,
        artifact_type=_text(document.get("document_type"), 200) or "public_metadata",
        canonical_url=canonical_url,
        original_url=canonical_url,
        publisher=publisher,
        fetched_at=document_observed_at,
        mime_type=_source_mime_type(source_id),
        content_hash=content_hash,
        published_at=published_at,
        published_time_basis=published_basis,
        robots_state="not_applicable",
        excerpt=excerpt,
        locator=canonical_url,
        retention_policy="structured_metadata_and_hash_only",
        license_policy="source_policy_registry_applies",
        metadata={
            "assertion_mode": _text(document.get("assertion_mode"), 200),
            "canonical_key": _text(document.get("canonical_key"), 500),
            "causal_root_key": _text(document.get("causal_root_key"), 500),
            "document_id": document_id,
            "external_id": _text(document.get("external_id"), 1_000),
            "external_key": _text(document.get("external_key"), 500),
            "publisher_domain": _text(document.get("publisher_domain"), 500),
            "revision_key": _text(document.get("revision_key"), 500),
            "source_external_id": _text(document.get("source_external_id"), 500),
            "source_id": source_id,
            "source_policy_version": source_policy_version,
            "source_updated_at": _iso(_datetime(document.get("source_updated_at"))),
            "structured_metadata": _json_safe(metadata),
        },
    )


def _project_document_observations(
    document: Mapping[str, Any],
    *,
    artifact: EvidenceArtifact,
    company: str,
    scanner_id: str,
    source_id: str,
    source_policy_version: str,
) -> tuple[AtomicObservation, ...]:
    raw_mentions = document.get("mentions", ())
    raw_events = document.get("events", ())
    if not _is_sequence(raw_mentions) or not _is_sequence(raw_events):
        raise ValueError("market document mentions and events must be JSON arrays")
    mentions = tuple(_as_mapping(item, "mention") for item in raw_mentions)
    mention_by_key = {
        _text(item.get("mention_key"), 500): item
        for item in mentions
        if _text(item.get("mention_key"), 500)
    }
    results: list[AtomicObservation] = []
    for raw_event in raw_events:
        event = _as_mapping(raw_event, "event")
        event_key = _text(event.get("event_key") or event.get("external_id"), 500)
        if not event_key:
            event_key = _stable_key("market-event", artifact.artifact_key, event)
        subject = mention_by_key.get(_text(event.get("mention_key"), 500), {})
        causal_root = _causal_root(document, event, source_id, event_key)
        origin = _origin_fingerprint(document, event, source_id, artifact.canonical_url)
        source_actor = _source_actor(document, source_id, company)
        event_type = _text(event.get("event_type"), 200) or "unknown_event"
        assertion_mode = _text(event.get("assertion_mode"), 200) or "unknown"
        safe_claim = _text(event.get("safe_claim") or event.get("summary"), 1_000)
        prohibited_claims = _claim_list(event.get("prohibited_claims"))
        effective_at = _datetime(event.get("effective_at"))
        effective_basis = _event_time_basis(event, document, source_id)
        exact_8k = _safe_exact_8k(
            document, event, subject, source_id, effective_at, effective_basis
        )
        direct_show_hn = _safe_direct_show_hn(
            document, event, subject, source_id, company, effective_at, effective_basis
        )
        score_eligible = exact_8k or direct_show_hn
        confidence: Literal["low", "medium", "high"] = _confidence(event.get("confidence"))
        if score_eligible:
            confidence = "high"
        discovery_only = _discovery_only(event, subject, direct_show_hn)
        predicate = _event_predicate(scanner_id, source_id, event_type, exact_8k, direct_show_hn)
        identity_basis = (
            "resolved company plus exact high-confidence SEC filer identifier"
            if exact_8k
            else "resolved company equals parser-vetted direct Show HN domain"
            if direct_show_hn
            else "caller-resolved company association; no outcome inference"
        )
        qualifiers = {
            "assertion_mode": assertion_mode,
            "identity_basis": identity_basis,
            "independence_signal_key": causal_root,
            "prohibited_claims": prohibited_claims,
            "safe_claim": safe_claim,
            "score_policy": (
                "weak_timing_signal_max_one_per_causal_root"
                if score_eligible
                else "context_only"
            ),
            "source_id": source_id,
            "source_policy_version": source_policy_version,
        }
        results.append(
            AtomicObservation(
                observation_key=_stable_key(
                    "observation",
                    scanner_id,
                    company,
                    artifact.content_hash,
                    event_key,
                    predicate,
                ),
                company_key=company,
                scanner_id=scanner_id,
                scanner_version=SCANNER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                rule_id=predicate,
                rule_version=RULE_VERSION,
                entity_key=company,
                predicate=predicate,
                value_type="json",
                value={
                    "assertion_mode": assertion_mode,
                    "event_type": event_type,
                    "form": _event_metadata_value(document, event, "form"),
                    "item_number": _event_metadata_value(document, event, "item_number"),
                },
                qualifiers=qualifiers,
                summary=safe_claim,
                source_url=artifact.canonical_url,
                epistemic_status="observed",
                validity_status="historical",
                confidence=confidence,
                confidence_method=identity_basis,
                first_observed_at=artifact.fetched_at,
                last_observed_at=artifact.fetched_at,
                effective_at=effective_at,
                effective_time_basis=effective_basis,
                artifact_keys=[artifact.artifact_key],
                score_eligible=score_eligible,
                discovery_only=discovery_only,
                source_actor_key=source_actor,
                origin_fingerprint=origin,
                causal_root_key=causal_root,
                metadata={
                    "document_id": _text(document.get("document_id"), 500),
                    "event_key": event_key,
                    "independence_cluster": _text(event.get("independence_cluster"), 500),
                    "one_causal_root_one_independence_signal": True,
                    "parser_score_eligible_ignored": bool(event.get("score_eligible")),
                    "projection_score_gate": (
                        "exact_8k_item"
                        if exact_8k
                        else "safe_show_hn_direct_domain"
                        if direct_show_hn
                        else "closed"
                    ),
                    "source_event_metadata": _json_safe(_mapping(event.get("metadata"))),
                },
            )
        )
        results.extend(
            _amount_observations(
                event,
                event_key=event_key,
                assertion_mode=assertion_mode,
                safe_claim=safe_claim,
                prohibited_claims=prohibited_claims,
                artifact=artifact,
                company=company,
                scanner_id=scanner_id,
                source_id=source_id,
                source_policy_version=source_policy_version,
                source_actor=source_actor,
                origin=origin,
                causal_root=causal_root,
                effective_at=effective_at,
                effective_basis=effective_basis,
                confidence=confidence,
            )
        )
    return tuple(results)


def _amount_observations(
    event: Mapping[str, Any],
    *,
    event_key: str,
    assertion_mode: str,
    safe_claim: str,
    prohibited_claims: list[str],
    artifact: EvidenceArtifact,
    company: str,
    scanner_id: str,
    source_id: str,
    source_policy_version: str,
    source_actor: str,
    origin: str,
    causal_root: str,
    effective_at: datetime | None,
    effective_basis: Literal[
        "source_explicit",
        "source_header",
        "filing_date",
        "feed_date",
        "inferred",
        "scan_time",
        "unknown",
    ],
    confidence: Literal["low", "medium", "high"],
) -> tuple[AtomicObservation, ...]:
    amounts = _event_amounts(event)
    results: list[AtomicObservation] = []
    for index, amount in enumerate(amounts):
        semantics = _text(amount.get("semantics"), 100) or "unknown"
        currency = _text(amount.get("currency"), 20).upper()
        raw_value = _text(amount.get("value"), 100)
        value, value_type = _number(raw_value)
        amount_prohibited = list(
            dict.fromkeys(
                [
                    *prohibited_claims,
                    *_AMOUNT_PROHIBITED_CLAIMS.get(
                        semantics, _AMOUNT_PROHIBITED_CLAIMS["unknown"]
                    ),
                ]
            )
        )
        predicate = f"market.amount.{_slug(semantics)}"
        amount_summary = (
            f"The source reports literal {semantics} of {raw_value} {currency}; "
            "no cash, revenue, runway, valuation, budget, or buying intent is inferred."
        )
        results.append(
            AtomicObservation(
                observation_key=_stable_key(
                    "observation-amount",
                    scanner_id,
                    company,
                    artifact.content_hash,
                    event_key,
                    index,
                    semantics,
                    raw_value,
                    currency,
                ),
                company_key=company,
                scanner_id=scanner_id,
                scanner_version=SCANNER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                rule_id=predicate,
                rule_version=RULE_VERSION,
                entity_key=company,
                predicate=predicate,
                value_type=value_type,
                value=value,
                unit=currency,
                qualifiers={
                    "amount_semantics": semantics,
                    "assertion_mode": assertion_mode,
                    "independence_signal_key": causal_root,
                    "prohibited_claims": amount_prohibited,
                    "raw_literal": raw_value,
                    "safe_claim": safe_claim,
                    "score_policy": "literal_amount_context_only",
                    "source_id": source_id,
                    "source_policy_version": source_policy_version,
                },
                summary=_text(amount_summary, 1_000),
                source_url=artifact.canonical_url,
                epistemic_status="observed",
                validity_status="historical",
                confidence=confidence,
                confidence_method="literal structured source field; outcome semantics prohibited",
                first_observed_at=artifact.fetched_at,
                last_observed_at=artifact.fetched_at,
                effective_at=effective_at,
                effective_time_basis=effective_basis,
                artifact_keys=[artifact.artifact_key],
                score_eligible=False,
                discovery_only=False,
                source_actor_key=source_actor,
                origin_fingerprint=origin,
                causal_root_key=causal_root,
                metadata={
                    "event_key": event_key,
                    "one_causal_root_one_independence_signal": True,
                    "semantic_guardrail": "amount field never implies business outcome",
                },
            )
        )
    return tuple(results)


def _project_manifest(
    batch: Mapping[str, Any],
    *,
    documents: Sequence[Mapping[str, Any]],
    artifacts: Sequence[EvidenceArtifact],
    company: str,
    scanner_id: str,
    source_id: str,
    source_policy_version: str,
    observed_at: datetime,
    rejected_items: int,
    warnings: Sequence[str],
) -> ScannerCoverageManifest:
    successful = len(artifacts)
    attempted = successful + rejected_items
    times = [item.published_at for item in artifacts if item.published_at is not None]
    limitations = [
        "Global or bounded source stream; absence is never company-level evidence.",
        "Metadata-only projection; no page-body or unstated business outcome is inferred.",
        _scanner_limitation(scanner_id),
        *warnings,
    ]
    content_roots = [item.content_hash for item in artifacts]
    manifest_key = _stable_key(
        "coverage",
        scanner_id,
        company,
        source_id,
        _iso(observed_at),
        content_roots,
        _json_safe(_mapping(batch.get("cursor"))),
    )
    return ScannerCoverageManifest(
        manifest_key=manifest_key,
        scanner_id=scanner_id,
        scanner_version=SCANNER_VERSION,
        surface=f"market_source:{source_id}",
        scope=company,
        window_start=min(times) if times else None,
        window_end=max(times) if times else None,
        attempted_units=attempted,
        successful_units=successful,
        failed_units=rejected_items,
        # "complete" is scoped to the returned bounded poll response, never to the company or
        # the source universe. That is why absence_interpretable remains false even when every
        # returned unit projected successfully.
        completeness="partial" if rejected_items else "complete",
        truncated=bool(rejected_items),
        absence_interpretable=False,
        limitations=list(dict.fromkeys(_text(item, 500) for item in limitations if item))[:20],
        source_urls=list(dict.fromkeys(item.canonical_url for item in artifacts))[:30],
        observed_at=observed_at,
        metadata={
            "cursor": _json_safe(_mapping(batch.get("cursor"))),
            "document_count": len(documents),
            "etag": _text(batch.get("etag"), 500),
            "last_modified": _text(batch.get("last_modified"), 500),
            "rejected_items": rejected_items,
            "source_id": source_id,
            "source_policy_version": source_policy_version,
            "unit_semantics": "parsed documents; rejected_items are conservatively failed",
            "warnings": list(warnings),
        },
    )


def _safe_exact_8k(
    document: Mapping[str, Any],
    event: Mapping[str, Any],
    subject: Mapping[str, Any],
    source_id: str,
    effective_at: datetime | None,
    effective_basis: str,
) -> bool:
    if source_id != "sec_current_report_atom_v1" or effective_at is None:
        return False
    if effective_basis not in {"filing_date", "source_header"}:
        return False
    document_metadata = _mapping(document.get("metadata"))
    event_metadata = _mapping(event.get("metadata"))
    form = _text(event_metadata.get("form") or document_metadata.get("form"), 20).upper()
    if form != "8-K" or bool(document_metadata.get("is_amendment")):
        return False
    item_number = _text(event_metadata.get("item_number"), 20)
    event_type = _text(event.get("event_type"), 200)
    if _SEC_ITEM_EVENT_TYPES.get(item_number) != event_type:
        return False
    if _text(event.get("assertion_mode"), 100) != "filed_item_disclosure":
        return False
    if _confidence(event.get("confidence")) != "high" or not _high_sec_subject(subject):
        return False
    host = _host(_canonical_url(document.get("canonical_url")))
    return host == "sec.gov" or host.endswith(".sec.gov")


def _safe_direct_show_hn(
    document: Mapping[str, Any],
    event: Mapping[str, Any],
    subject: Mapping[str, Any],
    source_id: str,
    company: str,
    effective_at: datetime | None,
    effective_basis: str,
) -> bool:
    if (
        source_id != "hacker_news_show_v0"
        or effective_at is None
        or effective_basis != "source_explicit"
        or _text(event.get("event_type"), 100) != "show_hn_submission"
        or _text(event.get("assertion_mode"), 100) != "community_submission"
    ):
        return False
    document_metadata = _mapping(document.get("metadata"))
    event_metadata = _mapping(event.get("metadata"))
    candidate = _company_key_or_empty(
        subject.get("candidate_domain") or subject.get("domain")
    )
    if (
        not bool(document_metadata.get("is_show_hn"))
        or not bool(event_metadata.get("promotable_direct_domain"))
        or not bool(subject.get("promotable"))
        or _text(subject.get("role"), 100) != "subject"
        or _text(subject.get("resolution_state"), 100) != "direct_domain_candidate"
        or not candidate
        or candidate != company
    ):
        return False
    outbound = _canonical_url(document_metadata.get("outbound_url"))
    outbound_host = _host(outbound)
    hn_host = _host(_canonical_url(document.get("canonical_url")))
    return (
        bool(outbound)
        and (outbound_host == candidate or outbound_host.endswith(f".{candidate}"))
        and hn_host == "news.ycombinator.com"
    )


def _high_sec_subject(subject: Mapping[str, Any]) -> bool:
    if _confidence(subject.get("confidence")) != "high":
        return False
    role = _text(subject.get("role") or subject.get("entity_role"), 100)
    identifiers = {
        **_mapping(subject.get("external_ids")),
        **_mapping(subject.get("identifiers")),
    }
    resolution = _text(subject.get("resolution_state"), 100)
    return role in {"filer", "issuer", "registrant"} and (
        bool(_text(identifiers.get("cik"), 30)) or resolution in {"resolved", "matched"}
    )


def _discovery_only(
    event: Mapping[str, Any],
    subject: Mapping[str, Any],
    direct_show_hn: bool,
) -> bool:
    if direct_show_hn:
        return False
    metadata = _mapping(event.get("metadata"))
    if bool(metadata.get("requires_subject_resolution")):
        return True
    return not bool(subject)


def _event_predicate(
    scanner_id: str,
    source_id: str,
    event_type: str,
    exact_8k: bool,
    direct_show_hn: bool,
) -> str:
    if exact_8k:
        return "legal.sec.8k_item_disclosure"
    if direct_show_hn:
        return "news.launch.show_hn_direct_domain"
    if scanner_id == "sec_legal_capital_stream_v1":
        return f"legal.sec.{_slug(event_type)}"
    if scanner_id == "government_action_award_stream_v1":
        return f"legal.government.{_slug(event_type)}"
    if source_id == "gdelt_doc_metadata_v2":
        return "news.attention.gdelt_metadata"
    return f"news.attention.{_slug(event_type)}"


def _event_amounts(event: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    metadata_amounts = _mapping(event.get("metadata")).get("amounts")
    results: list[Mapping[str, Any]] = []
    if _is_sequence(metadata_amounts):
        results.extend(
            _as_mapping(item, "amount") for item in metadata_amounts if isinstance(item, Mapping)
        )
    if not results and event.get("amount") is not None:
        results.append(
            {
                "value": event.get("amount"),
                "currency": event.get("amount_currency") or event.get("currency"),
                "semantics": event.get("amount_semantics") or "unknown",
            }
        )
    unique: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for amount in results:
        identity = (
            _text(amount.get("semantics"), 100),
            _text(amount.get("value"), 100),
            _text(amount.get("currency"), 20).upper(),
        )
        if identity[1]:
            unique.setdefault(identity, amount)
    return tuple(unique.values())


def _event_metadata_value(
    document: Mapping[str, Any], event: Mapping[str, Any], name: str
) -> Any:
    event_value = _mapping(event.get("metadata")).get(name)
    if event_value is not None and event_value != "":
        return event_value
    return _mapping(document.get("metadata")).get(name)


def _document_source_time(
    document: Mapping[str, Any], source_id: str
) -> tuple[
    datetime | None,
    Literal["source_explicit", "source_header", "filing_date", "feed_date", "unknown"],
]:
    published_at = _datetime(document.get("published_at"))
    source_updated_at = _datetime(document.get("source_updated_at"))
    if source_id.startswith("sec_") and source_id.endswith("_atom_v1"):
        if published_at is not None:
            return published_at, "filing_date"
        if source_updated_at is not None:
            return source_updated_at, "source_header"
    if source_id == "gdelt_doc_metadata_v2":
        return published_at, "feed_date" if published_at else "unknown"
    if source_id == "sec_litigation_rss_v1":
        source_time = published_at or source_updated_at
        return source_time, "feed_date" if source_time else "unknown"
    if published_at is not None:
        return published_at, "source_explicit"
    if source_updated_at is not None:
        return source_updated_at, "source_header"
    return None, "unknown"


def _event_time_basis(
    event: Mapping[str, Any], document: Mapping[str, Any], source_id: str
) -> Literal[
    "source_explicit",
    "source_header",
    "filing_date",
    "feed_date",
    "inferred",
    "scan_time",
    "unknown",
]:
    basis = _text(event.get("effective_time_basis"), 100)
    if not _datetime(event.get("effective_at")):
        return "unknown"
    if basis == "filing_date":
        if _datetime(document.get("published_at")):
            return "filing_date"
        if _datetime(document.get("source_updated_at")):
            return "source_header"
        return "unknown"
    if source_id == "gdelt_doc_metadata_v2" and basis == "source_timestamp":
        return "feed_date"
    if basis in {"effective_date", "publication_date", "source_timestamp"}:
        return "source_explicit"
    return "unknown"


def _source_actor(document: Mapping[str, Any], source_id: str, company: str) -> str:
    if source_id.startswith("sec_") and source_id not in {"sec_litigation_rss_v1"}:
        return f"issuer:{company}"
    publisher_domain = _company_key_or_empty(document.get("publisher_domain"))
    if source_id in {"sec_litigation_rss_v1", "federal_register_documents_v1"}:
        return f"government:{publisher_domain or source_id}"
    if source_id.startswith("hacker_news_"):
        by = _slug(_mapping(document.get("metadata")).get("by")) or "unknown"
        return f"community:hacker-news:{by}"
    return f"publisher:{publisher_domain or _host(_canonical_url(document.get('canonical_url')))}"


def _origin_fingerprint(
    document: Mapping[str, Any],
    event: Mapping[str, Any],
    source_id: str,
    canonical_url: str,
) -> str:
    supplied = _text(
        event.get("origin_fingerprint")
        or _mapping(event.get("metadata")).get("origin_fingerprint")
        or document.get("origin_fingerprint")
        or _mapping(document.get("metadata")).get("origin_fingerprint"),
        500,
    )
    if supplied:
        return supplied
    native_id = (
        document.get("source_external_id")
        or document.get("external_id")
        or document.get("external_key")
        or canonical_url
    )
    return _stable_key("origin", source_id, native_id)


def _causal_root(
    document: Mapping[str, Any],
    event: Mapping[str, Any],
    source_id: str,
    event_key: str,
) -> str:
    supplied = _text(
        event.get("causal_root_key")
        or _mapping(event.get("metadata")).get("causal_root_key")
        or document.get("causal_root_key"),
        500,
    )
    return supplied or _stable_key("causal", source_id, event_key)


def _enforce_one_score_per_causal_root(
    observations: tuple[AtomicObservation, ...],
) -> tuple[AtomicObservation, ...]:
    seen: set[str] = set()
    results: list[AtomicObservation] = []
    for observation in observations:
        if observation.score_eligible and observation.causal_root_key in seen:
            metadata = dict(observation.metadata)
            metadata["projection_score_gate"] = "deduplicated_causal_root"
            results.append(
                observation.model_copy(update={"score_eligible": False, "metadata": metadata})
            )
            continue
        if observation.score_eligible:
            seen.add(observation.causal_root_key)
        results.append(observation)
    return tuple(results)


def _projection_time(
    documents: Sequence[Mapping[str, Any]], observed_at: datetime | str | None
) -> datetime:
    supplied = _datetime(observed_at)
    if supplied is not None:
        return supplied
    times = [_datetime(item.get("observed_at")) for item in documents]
    available = [item for item in times if item is not None]
    if available:
        return max(available)
    raise ValueError("observed_at is required when the market batch has no documents")


def _content_hash(document: Mapping[str, Any]) -> str:
    supplied = _text(document.get("content_hash"), 128).lower()
    if re.fullmatch(r"[0-9a-f]{64}", supplied):
        return supplied
    content = {
        key: _json_safe(document.get(key))
        for key in (
            "assertion_mode",
            "canonical_url",
            "document_type",
            "external_id",
            "metadata",
            "published_at",
            "source_updated_at",
            "summary",
            "title",
        )
    }
    return hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()


def _canonical_url(value: Any) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    try:
        parsed = urlparse(candidate)
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").casefold()
        port = parsed.port
    except (UnicodeError, ValueError):
        return ""
    if parsed.username or parsed.password:
        return ""
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host if not port or default_port else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in _TRACKING_QUERY_KEYS
    ]
    return urlunparse((scheme, netloc, path, "", urlencode(sorted(query)), ""))


def _company_key(value: Any) -> str:
    result = _company_key_or_empty(value)
    if not result:
        raise ValueError("company_key must be a resolved canonical company domain")
    return result


def _company_key_or_empty(value: Any) -> str:
    text = str(value or "").strip().casefold().rstrip(".")
    if not text:
        return ""
    if "://" in text:
        text = _host(_canonical_url(text))
    if text.startswith("www."):
        text = text[4:]
    if (
        not text
        or "/" in text
        or "@" in text
        or " " in text
        or "." not in text
        or not re.fullmatch(r"[a-z0-9.-]+", text)
    ):
        return ""
    return text


def _number(value: str) -> tuple[int | float | str, Literal["integer", "number", "string"]]:
    try:
        amount = Decimal(value.replace(",", ""))
    except (InvalidOperation, ValueError):
        return value, "string"
    if not amount.is_finite():
        return value, "string"
    if amount == amount.to_integral_value():
        return int(amount), "integer"
    return float(amount), "number"


def _source_mime_type(source_id: str) -> str:
    if source_id.startswith("sec_") and source_id != "sec_litigation_rss_v1":
        return "application/atom+xml"
    if source_id == "sec_litigation_rss_v1":
        return "application/rss+xml"
    return "application/json"


def _scanner_limitation(scanner_id: str) -> str:
    if scanner_id == "sec_legal_capital_stream_v1":
        return "A filing and its literal fields do not establish capital receipt or budget."
    if scanner_id == "government_action_award_stream_v1":
        return "A government publication or mention is not a legal conclusion or company impact."
    return "Indexing, syndication, votes, and mentions do not establish traction or budget."


def _stable_key(kind: str, *parts: Any) -> str:
    digest = hashlib.sha256(_canonical_json(parts).encode("utf-8")).hexdigest()
    return f"{kind}:v1:{digest}"


def _canonical_json(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if _is_sequence(value):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _json_safe(item) for key, item in value.items()}


def _as_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"market {label} must be a JSON object")
    return _mapping(value)


def _sequence(value: Any) -> tuple[Any, ...]:
    return tuple(value) if _is_sequence(value) else ()


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _claim_list(value: Any) -> list[str]:
    return list(dict.fromkeys(_text(item, 200) for item in _sequence(value) if _text(item)))


def _confidence(value: Any) -> Literal["low", "medium", "high"]:
    text = _text(value, 20).casefold()
    if text == "high":
        return "high"
    if text == "medium":
        return "medium"
    return "low"


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""


def _text(value: Any, limit: int = 10_000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")[:100]


def _nonnegative_int(value: Any) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, result)
