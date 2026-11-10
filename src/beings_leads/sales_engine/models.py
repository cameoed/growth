"""Bounded commercial facts, operator inputs and reproducible research decisions.

Prices are integer micro-USD (one dollar is 1_000_000). A missing cost, capacity,
permission or outcome stays unknown; none of these contracts predicts conversion.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..creative_capacity import _utc_now as utc_now

Money = Annotated[int, Field(strict=True, ge=0, le=10**15)]
Tag = Annotated[str, Field(min_length=1, max_length=80)]
Tags = Annotated[list[Tag], Field(max_length=40)]
ShortNote = Annotated[str, Field(min_length=1, max_length=1000)]
Identifier = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[\w.:-]+$")]


def normalize_company_key(value: str) -> str:
    """Canonical DNS company identity; paths and userinfo cannot become companies."""
    raw = value.strip()
    parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    if parts.scheme not in {"http", "https"} or parts.username or parts.password:
        raise ValueError("company_key must be a website hostname without credentials")
    try:
        host = (parts.hostname or "").rstrip(".").encode("idna").decode().lower()
        port = parts.port
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid company hostname") from exc
    if parts.path not in {"", "/"} or parts.query or parts.fragment or port is not None:
        raise ValueError("company_key must identify a hostname, not a page or port")
    if host.startswith("www."):
        host = host[4:]
    if len(host) > 253 or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z0-9.-]+", host):
        raise ValueError("company_key must be a valid website hostname")
    if any(
        not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
        for label in host.split(".")
    ):
        raise ValueError("invalid company hostname")
    return host


def validate_source_url(value: str) -> str:
    raw = value.strip()
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("source URL must be an absolute HTTP(S) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("source URL cannot contain credentials")
    try:
        _ = parts.port
    except ValueError as exc:
        raise ValueError("invalid source URL port") from exc
    if any(character.isspace() or ord(character) < 32 for character in raw):
        raise ValueError("source URL cannot contain whitespace or control characters")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


class EngineModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    @field_validator("*", mode="after")
    @classmethod
    def aware_datetimes(cls, value: object) -> object:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("timestamps must include a timezone")
            return value.astimezone(UTC)
        return value


class CitedEvidence(EngineModel):
    source_url: str | None = Field(default=None, min_length=1, max_length=2048)
    source_kind: Literal["public_source", "buyer_conversation", "operator_observation"] = (
        "public_source"
    )
    recorded_by: str = Field(default="", max_length=160)
    excerpt: str = Field(min_length=1, max_length=2000)
    observed_at: datetime
    subject_company_key: str = Field(min_length=1, max_length=253)
    evidence_kind: Literal["observed", "inferred", "human_confirmed"] = "observed"

    @field_validator("source_url")
    @classmethod
    def evidence_url(cls, value: str | None) -> str | None:
        return validate_source_url(value) if value is not None else None

    _company = field_validator("subject_company_key")(normalize_company_key)

    @model_validator(mode="after")
    def source_provenance(self) -> Self:
        if self.source_kind == "public_source" and self.source_url is None:
            raise ValueError("public-source evidence requires a source URL")
        if self.source_kind != "public_source" and not self.recorded_by:
            raise ValueError("conversation and operator evidence require a recorder")
        return self


class ProductionOpportunity(EngineModel):
    id: Identifier | None = None
    company_key: str = Field(min_length=1, max_length=253)
    candidate_id: int | None = Field(default=None, ge=1)
    account_id: int | None = Field(default=None, ge=1)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    evidence: list[CitedEvidence] = Field(min_length=1, max_length=30)
    required_skills: Tags = Field(default_factory=list)
    style_tags: Tags = Field(default_factory=list)
    channel_tags: Tags = Field(default_factory=list)
    languages: Tags = Field(default_factory=list)
    estimated_hours: float | None = Field(default=None, gt=0, le=744)
    monthly_price_microusd: Money = 5_000_000_000
    status: Literal["hypothesis", "confirmed", "closed"] = "hypothesis"
    observed_at: datetime = Field(default_factory=utc_now)
    missing_inputs: list[ShortNote] = Field(default_factory=list, max_length=20)
    counter_hypotheses: list[ShortNote] = Field(default_factory=list, max_length=20)
    approval_owner: str = Field(default="", max_length=160)

    _company = field_validator("company_key")(normalize_company_key)

    @model_validator(mode="after")
    def evidence_subject(self) -> Self:
        if not any(item.subject_company_key == self.company_key for item in self.evidence):
            raise ValueError("opportunity needs evidence attributed to its own company")
        if self.status == "confirmed" and not any(
            item.evidence_kind == "human_confirmed" and item.subject_company_key == self.company_key
            for item in self.evidence
        ):
            raise ValueError("confirmed opportunities require human-confirmed evidence")
        return self


class RelationshipEvidence(EngineModel):
    id: Identifier | None = None
    subject_company_key: str = Field(min_length=1, max_length=253)
    object_company_key: str = Field(min_length=1, max_length=253)
    relationship_type: Literal[
        "vendor_customer",
        "agency_client",
        "former_collaborator",
        "portfolio_client",
        "referral",
        "parent_subsidiary",
        "partner",
    ]
    source_url: str = Field(min_length=1, max_length=2048)
    excerpt: str = Field(min_length=1, max_length=2000)
    observed_at: datetime
    relationship_started_at: date | None = None
    relationship_ended_at: date | None = None
    permission_for_intro: Literal["unknown", "requested", "granted", "declined"] = "unknown"
    permission_recorded_at: datetime | None = None
    referrer_name: str = Field(default="", max_length=160)
    notes: str = Field(default="", max_length=2000)

    _subject = field_validator("subject_company_key")(normalize_company_key)
    _object = field_validator("object_company_key")(normalize_company_key)
    _url = field_validator("source_url")(validate_source_url)

    @model_validator(mode="after")
    def relationship_consistency(self) -> Self:
        if self.subject_company_key == self.object_company_key:
            raise ValueError("relationships must connect different companies")
        if self.permission_for_intro != "unknown" and self.permission_recorded_at is None:
            raise ValueError("intro permission needs its recorded date")
        if (
            self.relationship_started_at
            and self.relationship_ended_at
            and self.relationship_started_at > self.relationship_ended_at
        ):
            raise ValueError("relationship cannot end before it starts")
        return self


class ResearchReview(EngineModel):
    idempotency_key: Identifier
    company_key: str = Field(min_length=1, max_length=253)
    candidate_id: int | None = Field(default=None, ge=1)
    account_id: int | None = Field(default=None, ge=1)
    reviewer: str = Field(min_length=1, max_length=160)
    research_action: str = Field(min_length=1, max_length=120)
    actual_minutes: float = Field(ge=0, le=1440)
    label: Literal[
        "unknown",
        "no_fit",
        "needs_research",
        "relevant",
        "qualified_conversation",
        "confirmed_recurring_need",
    ] = "unknown"
    useful: bool | None = None
    decision_changed: bool | None = None
    observed_at: datetime = Field(default_factory=utc_now)
    source_urls: list[str] = Field(default_factory=list, max_length=30)
    notes: str = Field(default="", max_length=4000)

    _company = field_validator("company_key")(normalize_company_key)

    @field_validator("source_urls")
    @classmethod
    def source_identities(cls, value: list[str]) -> list[str]:
        return [validate_source_url(item) for item in value]


class DecisionFeature(EngineModel):
    key: Identifier
    value: float | str | bool | None
    observed_at: datetime
    source_url: str | None = Field(default=None, max_length=2048)

    @field_validator("value")
    @classmethod
    def bounded_feature(cls, value: object) -> object:
        if isinstance(value, str) and len(value) > 1000:
            raise ValueError("feature text exceeds 1000 characters")
        return value

    @field_validator("source_url")
    @classmethod
    def feature_source(cls, value: str | None) -> str | None:
        return validate_source_url(value) if value else None


class DecisionSnapshot(EngineModel):
    idempotency_key: Identifier
    company_key: str = Field(min_length=1, max_length=253)
    candidate_id: int | None = Field(default=None, ge=1)
    account_id: int | None = Field(default=None, ge=1)
    policy_version: Identifier = "sales-engine-v1"
    as_of: datetime = Field(default_factory=utc_now)
    action: Literal["contact", "research", "watch", "nurture", "deprioritize"]
    rationale: str = Field(min_length=1, max_length=4000)
    features: list[DecisionFeature] = Field(default_factory=list, max_length=100)
    review_ids: list[int] = Field(default_factory=list, max_length=100)
    experiment_key: Identifier | None = None
    next_research_action: str = Field(default="", max_length=120)

    _company = field_validator("company_key")(normalize_company_key)

    @model_validator(mode="after")
    def temporal_boundary(self) -> Self:
        if any(feature.observed_at > self.as_of for feature in self.features):
            raise ValueError("decision features must be observed at or before as_of")
        if len({feature.key for feature in self.features}) != len(self.features):
            raise ValueError("decision feature keys must be unique")
        return self


class BudgetPolicy(EngineModel):
    monthly_limit_microusd: Money = 100_000_000
    per_account_limit_microusd: Money = 10_000_000


class BudgetReservationRequest(EngineModel):
    idempotency_key: Identifier
    company_key: str = Field(min_length=1, max_length=253)
    provider: str = Field(min_length=1, max_length=120)
    purpose: str = Field(min_length=1, max_length=200)
    maximum_microusd: Annotated[int, Field(strict=True, gt=0, le=10**15)]

    _company = field_validator("company_key")(normalize_company_key)


class LearningMetric(EngineModel):
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    minimum_sample: int = Field(ge=1)
    observed_rate: float | None = None
    wilson_low: float | None = None
    wilson_high: float | None = None
    status: Literal["insufficient_data", "descriptive_only"]
