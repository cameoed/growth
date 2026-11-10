"""Bounded creative annotations and auditable similarity; no network or image model calls.

URL identity, exact bytes, near-duplicate pixels and semantic concepts are separate facts.
The caller supplies actual visual observations; URL text never becomes a visual label.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION = "creative-semantics-v1"
MAX_ASSETS = 200
_CONCEPT_FIELDS = {"hook": 3.0, "problem": 2.0, "offer": 2.0, "scene": 2.0, "product": 3.0}


class CreativeAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_key: str = Field(min_length=1, max_length=300)
    source_url: str = Field(min_length=1, max_length=2_000)
    content_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    hook: str = Field(default="", max_length=500)
    problem: str = Field(default="", max_length=500)
    offer: str = Field(default="", max_length=500)
    scene: str = Field(default="", max_length=500)
    product: str = Field(default="", max_length=500)
    channel: str = Field(default="", max_length=100)
    format: str = Field(default="", max_length=100)
    language: str = Field(default="", max_length=100)
    evidence_excerpt: str = Field(default="", max_length=1_000)
    provenance: Literal["human", "image_model"]
    model: str = Field(default="", max_length=150)
    version: str = Field(default=VERSION, max_length=150)
    observed_at: datetime
    validation_status: Literal["unreviewed", "confirmed", "rejected"] = "unreviewed"
    validated_by: str = Field(default="", max_length=150)
    validated_at: datetime | None = None
    perceptual_hash: str = Field(default="", pattern=r"^(?:[a-f0-9]{16})?$")
    perceptual_hash_algorithm: Literal["", "dhash-9x8-v1"] = ""

    @model_validator(mode="after")
    def provenance_is_explicit(self) -> CreativeAnnotation:
        if self.provenance == "image_model" and not self.model:
            raise ValueError("Image-model annotations require a model identifier")
        if self.validation_status != "unreviewed" and not (self.validated_by and self.validated_at):
            raise ValueError("Reviewed annotations require reviewer and review time")
        if bool(self.perceptual_hash) != bool(self.perceptual_hash_algorithm):
            raise ValueError("Perceptual fingerprints require their algorithm")
        return self


class CreativeCluster(BaseModel):
    cluster_key: str
    asset_keys: list[str]
    basis: Literal["semantic", "exact_content", "near_duplicate_pixels", "unresolved"]
    representative: dict[str, str] = Field(default_factory=dict)
    human_confirmed_assets: int = 0
    minimum_similarity: float | None = None


class CreativeClustering(BaseModel):
    version: str = VERSION
    asset_count: int = 0
    exact_content_count: int = 0
    semantically_annotated_assets: int = 0
    inferred_concept_count: int | None = None
    clusters: list[CreativeCluster] = Field(default_factory=list)
    score_eligible: bool = False
    limitations: list[str] = Field(default_factory=list)


class CreativeBenchmark(BaseModel):
    version: str = VERSION
    model_versions: list[str] = Field(default_factory=list)
    similarity_threshold: float = 0.82
    labelled_assets: int
    evaluated_pairs: int
    positive_pairs: int
    negative_pairs: int
    true_positives: int
    false_positives: int
    false_negatives: int
    pair_precision: float | None
    pair_recall: float | None
    pair_f1: float | None
    annotation_coverage: float
    score_eligible: bool
    limitations: list[str]


def content_fingerprint(data: bytes) -> str:
    """Exact bytes only: this digest does not establish visual or conceptual similarity."""
    if not data or len(data) > 12_000_000:
        raise ValueError("Asset bytes must contain between 1 and 12,000,000 bytes")
    return hashlib.sha256(data).hexdigest()


def luminance_fingerprint(rows: Sequence[Sequence[int]]) -> str:
    """dHash of a caller-decoded 9×8 grayscale sample; never a hash of the asset URL."""
    if len(rows) != 8 or any(len(row) != 9 for row in rows):
        raise ValueError("A 9 by 8 decoded luminance sample is required")
    if any(type(value) is not int or not 0 <= value <= 255 for row in rows for value in row):
        raise ValueError("Luminance values must be integers from 0 to 255")
    result = 0
    for row in rows:
        for left, right in zip(row, row[1:], strict=False):
            result = (result << 1) | int(left > right)
    return f"{result:016x}"


def annotation_cache_key(content_sha256: str, *, model: str, version: str = VERSION) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", content_sha256) or not model or not version:
        raise ValueError("Cache identity requires content SHA-256, model and annotation version")
    return hashlib.sha256(
        json.dumps([content_sha256, model, version], separators=(",", ":")).encode()
    ).hexdigest()


def normalize_creative_annotation(
    payload: Mapping[str, Any],
    *,
    asset_key: str,
    source_url: str,
    content_sha256: str,
    model: str,
    observed_at: datetime,
    version: str = VERSION,
) -> CreativeAnnotation:
    """Allow-list a model response and assign trusted provenance outside model output.

    A provider response cannot self-validate, change the asset identity or inject a hash.
    Empty/unknown labels remain empty rather than inventing a customer problem.
    """
    fields = [*_CONCEPT_FIELDS, "channel", "format", "language", "evidence_excerpt"]
    labels = {
        key: str(payload[key]).strip()
        for key in fields
        if isinstance(payload.get(key), str)
        and str(payload[key]).strip().casefold() not in {"unknown", "n/a", "not visible", "none"}
    }
    return CreativeAnnotation(
        **labels,
        asset_key=asset_key,
        source_url=source_url,
        content_sha256=content_sha256,
        provenance="image_model",
        model=model,
        version=version,
        observed_at=observed_at,
    )


def _tokens(value: str) -> set[str]:
    if value.strip().casefold() in {"unknown", "not visible", "none", "n/a", "unspecified"}:
        return set()
    return set(re.findall(r"\w+", value.casefold())) - {
        "a",
        "an",
        "the",
        "and",
        "of",
        "in",
        "with",
        "for",
        "to",
        "on",
        "is",
    }


def _semantic_ready(item: CreativeAnnotation) -> bool:
    return sum(bool(_tokens(getattr(item, field))) for field in _CONCEPT_FIELDS) >= 2


def semantic_similarity(left: CreativeAnnotation, right: CreativeAnnotation) -> float | None:
    """Conservative, deterministic weighted label overlap; not an embedding or vision model."""
    if not _semantic_ready(left) or not _semantic_ready(right):
        return None
    numerator = denominator = 0.0
    shared = 0
    for field, weight in _CONCEPT_FIELDS.items():
        a, b = _tokens(getattr(left, field)), _tokens(getattr(right, field))
        if not a and not b:
            continue
        denominator += weight
        if a and b:
            shared += 1
            numerator += weight * len(a & b) / len(a | b)
    if shared < 2:
        return None
    return numerator / denominator if denominator else None


def _pair_basis(
    left: CreativeAnnotation, right: CreativeAnnotation, threshold: float, max_hamming: int
) -> tuple[str, float] | None:
    if left.content_sha256 and left.content_sha256 == right.content_sha256:
        return "exact_content", 1.0
    semantic = semantic_similarity(left, right)
    # Different declared products/offers prevent a pixel hash from collapsing similar templates.
    conflicting_content = any(
        _tokens(getattr(left, field))
        and _tokens(getattr(right, field))
        and not (_tokens(getattr(left, field)) & _tokens(getattr(right, field)))
        for field in ("product", "offer")
    )
    if semantic is not None and semantic >= threshold and not conflicting_content:
        return "semantic", semantic
    if (
        left.perceptual_hash
        and right.perceptual_hash
        and left.perceptual_hash_algorithm == right.perceptual_hash_algorithm
        and not conflicting_content
    ):
        distance = (int(left.perceptual_hash, 16) ^ int(right.perceptual_hash, 16)).bit_count()
        if distance <= max_hamming:
            return "near_duplicate_pixels", 1 - distance / 64
    return None


def _deduplicate_annotations(
    annotations: Sequence[CreativeAnnotation | Mapping[str, Any]],
) -> list[CreativeAnnotation]:
    selected: dict[str, CreativeAnnotation] = {}
    for raw in annotations[: MAX_ASSETS * 5]:
        item = (
            raw if isinstance(raw, CreativeAnnotation) else CreativeAnnotation.model_validate(raw)
        )
        previous = selected.get(item.asset_key)
        raw_stamp = item.validated_at or item.observed_at
        stamp = raw_stamp.replace(tzinfo=UTC) if not raw_stamp.tzinfo else raw_stamp
        raw_previous_stamp = (previous.validated_at or previous.observed_at) if previous else None
        previous_stamp = (
            raw_previous_stamp.replace(tzinfo=UTC)
            if raw_previous_stamp and not raw_previous_stamp.tzinfo
            else raw_previous_stamp
            if raw_previous_stamp
            else datetime.min.replace(tzinfo=UTC)
        )
        if previous is None or (item.validation_status != "unreviewed", stamp) > (
            previous.validation_status != "unreviewed",
            previous_stamp,
        ):
            selected[item.asset_key] = item
    return [
        selected[key] for key in sorted(selected) if selected[key].validation_status != "rejected"
    ][:MAX_ASSETS]


def cluster_creatives(
    annotations: Sequence[CreativeAnnotation | Mapping[str, Any]],
    *,
    similarity_threshold: float = 0.82,
    max_pixel_hamming: int = 5,
    benchmark: CreativeBenchmark | None = None,
) -> CreativeClustering:
    """Complete-link clustering prevents chains of weak matches collapsing many concepts."""
    if not 0.5 <= similarity_threshold <= 1 or not 0 <= max_pixel_hamming <= 10:
        raise ValueError("Invalid bounded clustering threshold")
    items = _deduplicate_annotations(annotations)
    groups: list[list[CreativeAnnotation]] = []
    for item in items:
        for group in groups:
            if all(
                _pair_basis(item, other, similarity_threshold, max_pixel_hamming) for other in group
            ):
                group.append(item)
                break
        else:
            groups.append([item])
    clusters = []
    for group in groups:
        keys = sorted(item.asset_key for item in group)
        pair_results = [
            _pair_basis(left, right, similarity_threshold, max_pixel_hamming)
            for index, left in enumerate(group)
            for right in group[index + 1 :]
        ]
        bases = {pair[0] for pair in pair_results if pair}
        ready = all(_semantic_ready(item) for item in group)
        basis = (
            "semantic"
            if ready and (not bases or "semantic" in bases)
            else "exact_content"
            if bases == {"exact_content"}
            else "near_duplicate_pixels"
            if bases
            else "unresolved"
        )
        clusters.append(
            CreativeCluster(
                cluster_key="concept:" + hashlib.sha256("|".join(keys).encode()).hexdigest()[:24],
                asset_keys=keys,
                basis=basis,
                representative={field: getattr(group[0], field) for field in _CONCEPT_FIELDS},
                human_confirmed_assets=sum(item.validation_status == "confirmed" for item in group),
                minimum_similarity=min((pair[1] for pair in pair_results if pair), default=None),
            )
        )
    semantic_count = sum(_semantic_ready(item) for item in items)
    all_labelled = bool(items) and semantic_count == len(items)
    model_versions = {f"{item.provenance}:{item.model}:{item.version}" for item in items}
    benchmark_valid = bool(
        benchmark
        and benchmark.score_eligible
        and benchmark.version == VERSION
        and benchmark.similarity_threshold == similarity_threshold
        and model_versions.issubset(set(benchmark.model_versions))
    )
    limitations = [
        "Concept groups describe the supplied sample; they do not establish ad performance.",
        "Exact content and pixel similarity do not establish shared creative concepts.",
    ]
    if len(annotations) > MAX_ASSETS:
        limitations.append(f"Analysis is bounded to {MAX_ASSETS} distinct asset identities.")
    if not all_labelled:
        limitations.append("Some assets lack at least two substantive concept labels.")
    if not benchmark_valid:
        limitations.append(
            "Model concept counts require a human benchmark before influencing scores."
        )
    return CreativeClustering(
        asset_count=len(items),
        exact_content_count=len({item.content_sha256 or item.asset_key for item in items}),
        semantically_annotated_assets=semantic_count,
        inferred_concept_count=len(clusters) if all_labelled else None,
        clusters=clusters,
        score_eligible=all_labelled and benchmark_valid,
        limitations=limitations,
    )


def evaluate_creative_benchmark(
    annotations: Sequence[CreativeAnnotation | Mapping[str, Any]],
    human_concept_labels: Mapping[str, str],
    *,
    minimum_pairs: int = 20,
    similarity_threshold: float = 0.82,
) -> CreativeBenchmark:
    """Pairwise precision/recall against operator labels, with both positive and negative pairs."""
    result = cluster_creatives(annotations, similarity_threshold=similarity_threshold)
    predictions = {key: item.cluster_key for item in result.clusters for key in item.asset_keys}
    labelled = sorted(
        key for key, label in human_concept_labels.items() if label and key in predictions
    )
    tp = fp = fn = positive = negative = 0
    for index, left in enumerate(labelled):
        for right in labelled[index + 1 :]:
            actual = human_concept_labels[left] == human_concept_labels[right]
            predicted = predictions[left] == predictions[right]
            positive += actual
            negative += not actual
            tp += actual and predicted
            fp += not actual and predicted
            fn += actual and not predicted
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    coverage = len(labelled) / len(human_concept_labels) if human_concept_labels else 0.0
    eligible = (
        positive + negative >= max(20, minimum_pairs)
        and positive >= 5
        and negative >= 5
        and precision is not None
        and precision >= 0.9
        and recall is not None
        and recall >= 0.75
        and coverage >= 0.8
    )
    return CreativeBenchmark(
        model_versions=sorted(
            {
                f"{item.provenance}:{item.model}:{item.version}"
                for item in _deduplicate_annotations(annotations)
            }
        ),
        similarity_threshold=similarity_threshold,
        labelled_assets=len(labelled),
        evaluated_pairs=positive + negative,
        positive_pairs=positive,
        negative_pairs=negative,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        pair_precision=precision,
        pair_recall=recall,
        pair_f1=f1,
        annotation_coverage=coverage,
        score_eligible=eligible,
        limitations=[
            "Operator labels are ground truth; agreement does not measure ad effectiveness.",
            "Use a held-out representative sample and rerun after model or prompt changes.",
        ]
        + (
            []
            if eligible
            else ["Insufficient sample, coverage or agreement for score eligibility."]
        ),
    )
