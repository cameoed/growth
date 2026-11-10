from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import tldextract

SOURCE_POLICY_REGISTRY_VERSION = "2026-08-28.1"

SourceCategory = Literal[
    "legal_capital",
    "government_action",
    "government_notice",
    "open_news_metadata",
]
SourcePolicyStatus = Literal[
    "enabled",
    "disabled_prohibited",
    "disabled_license_required",
    "disabled_pending_review",
]
SourceParser = Literal[
    "sec_atom",
    "rss_atom",
    "federal_register_json",
    "gdelt_json",
    "hacker_news_json",
    "disabled",
]
PolicyCapability = Literal[
    "fetch",
    "store_metadata",
    "derive",
    "store_page_body",
    "commercial_use",
]
EntityRole = Literal[
    "issuer",
    "registrant",
    "filer",
    "subject",
    "publisher",
    "regulator",
    "submitter",
    "mentioned",
]
ResolutionState = Literal[
    "unresolved",
    "direct_domain_candidate",
    "publisher_only",
    "regulator_only",
]

_TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())
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
_BLOCKED_HN_REGISTRABLE_DOMAINS = {
    "amazonaws.com",
    "apple.com",
    "bitbucket.org",
    "cloudfront.net",
    "codesandbox.io",
    "discord.gg",
    "dropbox.com",
    "facebook.com",
    "figma.com",
    "firebaseapp.com",
    "fly.dev",
    "github.com",
    "gitlab.com",
    "google.com",
    "googleusercontent.com",
    "herokuapp.com",
    "huggingface.co",
    "instagram.com",
    "linktr.ee",
    "linkedin.com",
    "medium.com",
    "netlify.app",
    "news.ycombinator.com",
    "notion.site",
    "notion.so",
    "npmjs.com",
    "pages.dev",
    "producthunt.com",
    "pypi.org",
    "r2.dev",
    "reddit.com",
    "render.com",
    "replit.app",
    "stackblitz.com",
    "substack.com",
    "t.co",
    "twitter.com",
    "vercel.app",
    "vimeo.com",
    "web.app",
    "workers.dev",
    "x.com",
    "ycombinator.com",
    "youtu.be",
    "youtube.com",
}
_RESERVED_HOST_SUFFIXES = (
    ".example",
    ".internal",
    ".invalid",
    ".local",
    ".localhost",
    ".onion",
    ".test",
)
_SEC_ACCESSION_RE = re.compile(r"\b\d{10}-\d{2}-\d{6}\b")
_SEC_CIK_RE = re.compile(r"\bCIK\s*(?:Number|No\.?|#|:)?\s*0*(\d{1,10})\b", re.I)
_SEC_FILE_NUMBER_RE = re.compile(
    r"\b(?:File|Offering)\s*(?:Number|No\.?)\s*[:#]?\s*([A-Za-z0-9-]{3,40})",
    re.I,
)
_SEC_ITEM_RE = re.compile(r"\bItem(?:s)?\s+((?:\d\.\d{2})(?:\s*[,;and]+\s*\d\.\d{2})*)", re.I)
_MONEY_RE = r"(?:US\s*)?\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)"


@dataclass(frozen=True, slots=True)
class MarketSourcePolicy:
    source_id: str
    version: str
    label: str
    category: SourceCategory
    status: SourcePolicyStatus
    parser: SourceParser
    endpoint_urls: tuple[str, ...]
    official_domains: tuple[str, ...]
    terms_url: str
    reviewed_on: date
    allow_metadata_storage: bool
    allow_derived_analysis: bool
    allow_page_body_storage: bool
    allow_commercial_use: bool
    max_requests: int
    max_response_bytes: int
    max_items: int
    minimum_poll_seconds: int
    allowed_forms: tuple[str, ...] = ()
    denial_reason: str = ""

    @property
    def enabled(self) -> bool:
        return self.status == "enabled"

    def __post_init__(self) -> None:
        if not self.source_id or not self.version or not self.terms_url:
            raise ValueError("market source policies require an id, version, and terms URL")
        for name in ("max_requests", "max_response_bytes", "max_items", "minimum_poll_seconds"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.enabled and (not self.allow_metadata_storage or not self.allow_commercial_use):
            raise ValueError("enabled sources must allow metadata storage and commercial use")
        if not self.enabled and not self.denial_reason:
            raise ValueError("disabled sources require a denial reason")
        if self.parser == "sec_atom" and not self.allowed_forms:
            raise ValueError("SEC Atom sources require an exact allowed-form set")


def _source_policy(
    source_id: str,
    *,
    label: str,
    category: SourceCategory,
    parser: SourceParser,
    urls: tuple[str, ...],
    domains: tuple[str, ...],
    terms_url: str,
    requests: int = 4,
    response_bytes: int = 1_000_000,
    items: int = 100,
    poll_seconds: int = 900,
    forms: tuple[str, ...] = (),
) -> MarketSourcePolicy:
    return MarketSourcePolicy(
        source_id=source_id,
        version="1.0.0",
        label=label,
        category=category,
        status="enabled",
        parser=parser,
        endpoint_urls=urls,
        official_domains=domains,
        terms_url=terms_url,
        reviewed_on=date(2026, 8, 28),
        allow_metadata_storage=True,
        allow_derived_analysis=True,
        allow_page_body_storage=False,
        allow_commercial_use=True,
        max_requests=requests,
        max_response_bytes=response_bytes,
        max_items=items,
        minimum_poll_seconds=poll_seconds,
        allowed_forms=forms,
    )


def _disabled_policy(
    source_id: str,
    *,
    label: str,
    status: SourcePolicyStatus,
    terms_url: str,
    reason: str,
) -> MarketSourcePolicy:
    return MarketSourcePolicy(
        source_id=source_id,
        version="1.0.0",
        label=label,
        category="open_news_metadata",
        status=status,
        parser="disabled",
        endpoint_urls=(),
        official_domains=(),
        terms_url=terms_url,
        reviewed_on=date(2026, 8, 28),
        allow_metadata_storage=False,
        allow_derived_analysis=False,
        allow_page_body_storage=False,
        allow_commercial_use=False,
        max_requests=1,
        max_response_bytes=1,
        max_items=1,
        minimum_poll_seconds=86_400,
        denial_reason=reason,
    )


MARKET_SOURCE_POLICIES: tuple[MarketSourcePolicy, ...] = (
    _source_policy(
        "sec_form_d_atom_v1",
        label="SEC Form D notices",
        category="legal_capital",
        parser="sec_atom",
        urls=(
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&company=&count=100"
            "&dateb=&output=atom&owner=include&start=0&type=D",
        ),
        domains=("sec.gov", "data.sec.gov"),
        terms_url="https://www.sec.gov/about/developer-resources",
        forms=("D", "D/A"),
    ),
    _source_policy(
        "sec_registration_atom_v1",
        label="SEC registration statements",
        category="legal_capital",
        parser="sec_atom",
        urls=(
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&company=&count=100"
            "&dateb=&output=atom&owner=include&start=0&type=S-1",
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&company=&count=100"
            "&dateb=&output=atom&owner=include&start=0&type=F-1",
        ),
        domains=("sec.gov", "data.sec.gov"),
        terms_url="https://www.sec.gov/about/developer-resources",
        forms=("S-1", "S-1/A", "F-1", "F-1/A"),
    ),
    _source_policy(
        "sec_current_report_atom_v1",
        label="SEC current reports",
        category="legal_capital",
        parser="sec_atom",
        urls=(
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&company=&count=100"
            "&dateb=&output=atom&owner=include&start=0&type=8-K",
        ),
        domains=("sec.gov", "data.sec.gov"),
        terms_url="https://www.sec.gov/about/developer-resources",
        forms=("8-K", "8-K/A"),
    ),
    _source_policy(
        "sec_crowdfunding_atom_v1",
        label="SEC Regulation Crowdfunding filings",
        category="legal_capital",
        parser="sec_atom",
        urls=(
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&company=&count=100"
            "&dateb=&output=atom&owner=include&start=0&type=C",
        ),
        domains=("sec.gov", "data.sec.gov"),
        terms_url="https://www.sec.gov/about/developer-resources",
        forms=("C", "C/A", "C-U", "C-U/A"),
    ),
    _source_policy(
        "sec_reg_a_atom_v1",
        label="SEC Regulation A filings",
        category="legal_capital",
        parser="sec_atom",
        urls=(
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&company=&count=100"
            "&dateb=&output=atom&owner=include&start=0&type=1-A",
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&company=&count=100"
            "&dateb=&output=atom&owner=include&start=0&type=1-K",
        ),
        domains=("sec.gov", "data.sec.gov"),
        terms_url="https://www.sec.gov/about/developer-resources",
        forms=("1-A", "1-A/A", "1-K", "1-K/A"),
    ),
    _source_policy(
        "sec_litigation_rss_v1",
        label="SEC litigation releases",
        category="government_action",
        parser="rss_atom",
        urls=("https://www.sec.gov/enforcement-litigation/litigation-releases/rss",),
        domains=("sec.gov",),
        terms_url="https://www.sec.gov/about/rss-feeds",
        poll_seconds=3_600,
    ),
    _source_policy(
        "federal_register_documents_v1",
        label="Federal Register documents",
        category="government_notice",
        parser="federal_register_json",
        urls=(
            "https://www.federalregister.gov/api/v1/documents.json?per_page=100&order=newest",
        ),
        domains=("federalregister.gov", "govinfo.gov"),
        terms_url="https://www.federalregister.gov/developers/documentation/api/v1",
        poll_seconds=3_600,
    ),
    _source_policy(
        "gdelt_doc_metadata_v2",
        label="GDELT DOC metadata",
        category="open_news_metadata",
        parser="gdelt_json",
        urls=("https://api.gdeltproject.org/api/v2/doc/doc?mode=artlist&format=json",),
        domains=("gdeltproject.org",),
        terms_url="https://www.gdeltproject.org/",
        poll_seconds=900,
    ),
    _source_policy(
        "hacker_news_show_v0",
        label="Hacker News Show stories",
        category="open_news_metadata",
        parser="hacker_news_json",
        urls=("https://hacker-news.firebaseio.com/v0/showstories.json",),
        domains=("hacker-news.firebaseio.com", "news.ycombinator.com"),
        terms_url="https://github.com/HackerNews/API",
        requests=101,
        response_bytes=500_000,
        poll_seconds=900,
    ),
    _source_policy(
        "hacker_news_new_v0",
        label="Hacker News new stories",
        category="open_news_metadata",
        parser="hacker_news_json",
        urls=("https://hacker-news.firebaseio.com/v0/newstories.json",),
        domains=("hacker-news.firebaseio.com", "news.ycombinator.com"),
        terms_url="https://github.com/HackerNews/API",
        requests=101,
        response_bytes=500_000,
        poll_seconds=900,
    ),
    _disabled_policy(
        "pr_newswire_rss",
        label="PR Newswire",
        status="disabled_prohibited",
        terms_url="https://www.prnewswire.com/terms-of-use.html",
        reason=(
            "Commercial scraping, data-mining, and AI reuse are not authorized by the "
            "published terms."
        ),
    ),
    _disabled_policy(
        "business_wire",
        label="Business Wire",
        status="disabled_license_required",
        terms_url="https://www.businesswire.com/portal/site/home/terms-of-use/",
        reason="A commercial content/feed license or written permission is required.",
    ),
    _disabled_policy(
        "globenewswire_rss",
        label="GlobeNewswire",
        status="disabled_license_required",
        terms_url="https://www.globenewswire.com/TermsOfUse",
        reason="Licensing review or written permission is required before commercial ingestion.",
    ),
    _disabled_policy(
        "techcrunch_rss",
        label="TechCrunch",
        status="disabled_pending_review",
        terms_url="https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html",
        reason="Direct publisher-feed commercial reuse has not been approved.",
    ),
    _disabled_policy(
        "venturebeat_rss",
        label="VentureBeat",
        status="disabled_pending_review",
        terms_url="https://venturebeat.com/terms-of-service/",
        reason="Direct publisher-feed commercial reuse has not been approved.",
    ),
)
MARKET_SOURCE_POLICY_REGISTRY: Mapping[str, MarketSourcePolicy] = MappingProxyType(
    {policy.source_id: policy for policy in MARKET_SOURCE_POLICIES}
)
if len(MARKET_SOURCE_POLICY_REGISTRY) != len(MARKET_SOURCE_POLICIES):
    raise RuntimeError("market source policy ids must be unique")

LEGAL_MARKET_SOURCE_IDS: tuple[str, ...] = tuple(
    policy.source_id
    for policy in MARKET_SOURCE_POLICIES
    if policy.enabled
    and policy.category in {"legal_capital", "government_action", "government_notice"}
)
NEWS_MARKET_SOURCE_IDS: tuple[str, ...] = tuple(
    policy.source_id
    for policy in MARKET_SOURCE_POLICIES
    if policy.enabled and policy.category == "open_news_metadata"
)


class MarketSourcePolicyDenied(PermissionError):
    def __init__(self, source_id: str, capability: PolicyCapability, reason: str) -> None:
        super().__init__(f"Source policy denies {capability} for {source_id}: {reason}")
        self.source_id = source_id
        self.capability = capability
        self.reason = reason


class MarketPayloadError(ValueError):
    pass


def require_market_source_policy(
    source_id: str,
    *,
    capability: PolicyCapability = "fetch",
) -> MarketSourcePolicy:
    policy = MARKET_SOURCE_POLICY_REGISTRY.get(source_id)
    if policy is None:
        raise MarketSourcePolicyDenied(
            source_id, capability, "source is not in the policy registry"
        )
    if capability == "fetch" and source_id in LEGAL_MARKET_SOURCE_IDS:
        raise MarketSourcePolicyDenied(source_id, capability, "Legal filings collection retired")
    if capability == "fetch" and source_id in NEWS_MARKET_SOURCE_IDS:
        raise MarketSourcePolicyDenied(source_id, capability, "News reader collection retired")
    allowed = {
        "fetch": policy.enabled and policy.allow_commercial_use,
        "store_metadata": policy.enabled and policy.allow_metadata_storage,
        "derive": policy.enabled and policy.allow_derived_analysis,
        "store_page_body": policy.enabled and policy.allow_page_body_storage,
        "commercial_use": policy.enabled and policy.allow_commercial_use,
    }[capability]
    if not allowed:
        reason = policy.denial_reason or f"policy status is {policy.status}"
        raise MarketSourcePolicyDenied(source_id, capability, reason)
    return policy


@dataclass(frozen=True, slots=True)
class MarketFeedLimits:
    max_response_bytes: int = 1_000_000
    max_items: int = 100
    max_text_chars: int = 8_000
    max_xml_nodes: int = 5_000
    max_xml_depth: int = 24

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class MarketDocumentKeys:
    canonical_key: str
    external_key: str
    revision_key: str
    causal_key: str


@dataclass(frozen=True, slots=True)
class MarketAmount:
    value: str
    currency: str
    semantics: Literal[
        "amount_sold_as_of_filing",
        "total_offering_amount",
        "award_ceiling",
        "obligation",
        "payment",
        "unknown",
    ]

    def to_dict(self) -> dict[str, str]:
        return {
            "value": self.value,
            "currency": self.currency,
            "semantics": self.semantics,
        }


@dataclass(frozen=True, slots=True)
class MarketDocument:
    source_id: str
    source_policy_version: str
    document_key: str
    canonical_key: str
    external_key: str
    revision_key: str
    causal_key: str
    external_id: str
    canonical_url: str
    title: str
    summary: str
    document_kind: str
    assertion_mode: str
    publisher_name: str
    publisher_domain: str
    published_at: datetime | None
    source_updated_at: datetime | None
    observed_at: datetime
    content_sha256: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MarketEntityMention:
    mention_key: str
    document_key: str
    raw_name: str
    normalized_name: str
    role: EntityRole
    identifiers: Mapping[str, str] = field(default_factory=dict)
    candidate_domain: str = ""
    promotable: bool = False
    resolution_state: ResolutionState = "unresolved"
    confidence: Literal["low", "medium", "high"] = "medium"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MarketEvent:
    event_key: str
    document_key: str
    causal_key: str
    event_type: str
    assertion_mode: str
    effective_at: datetime | None
    effective_time_basis: Literal[
        "effective_date", "filing_date", "publication_date", "source_timestamp", "unknown"
    ]
    subject_mention_key: str
    safe_summary: str
    prohibited_claims: tuple[str, ...]
    confidence: Literal["low", "medium", "high"]
    score_eligible: bool
    independence_cluster: str
    amounts: tuple[MarketAmount, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MarketParseResult:
    source_id: str
    source_policy_version: str
    documents: tuple[MarketDocument, ...] = ()
    mentions: tuple[MarketEntityMention, ...] = ()
    events: tuple[MarketEvent, ...] = ()
    rejected_items: int = 0
    warnings: tuple[str, ...] = ()
    cursor: Mapping[str, Any] = field(default_factory=dict)
    etag: str = ""
    last_modified: str = ""

    @property
    def promotable_domains(self) -> tuple[str, ...]:
        return tuple(
            sorted({item.candidate_domain for item in self.mentions if item.promotable})
        )

    def to_store_batch(self) -> dict[str, Any]:
        mentions_by_document: dict[str, list[MarketEntityMention]] = {}
        events_by_document: dict[str, list[MarketEvent]] = {}
        for mention in self.mentions:
            mentions_by_document.setdefault(mention.document_key, []).append(mention)
        for event in self.events:
            events_by_document.setdefault(event.document_key, []).append(event)

        documents: list[dict[str, Any]] = []
        for document in self.documents:
            document_mentions = [
                {
                    "mention_key": mention.mention_key,
                    "entity_name": mention.raw_name,
                    "entity_role": mention.role,
                    "external_ids": dict(mention.identifiers),
                    "domain": mention.candidate_domain or None,
                    "raw_name": mention.raw_name,
                    "normalized_name": mention.normalized_name,
                    "role": mention.role,
                    "identifiers": dict(mention.identifiers),
                    "candidate_domain": mention.candidate_domain or None,
                    "promotable": mention.promotable,
                    "resolution_state": mention.resolution_state,
                    "confidence": mention.confidence,
                    "metadata": _json_safe(mention.metadata),
                }
                for mention in mentions_by_document.get(document.document_key, [])
            ]
            document_events: list[dict[str, Any]] = []
            for event in events_by_document.get(document.document_key, []):
                primary_amount = next(
                    (
                        amount
                        for amount in event.amounts
                        if amount.semantics == "amount_sold_as_of_filing"
                    ),
                    event.amounts[0] if event.amounts else None,
                )
                event_metadata = dict(_json_safe(event.metadata))
                if event.amounts:
                    event_metadata["amounts"] = [amount.to_dict() for amount in event.amounts]
                document_events.append(
                    {
                        "event_key": event.event_key,
                        "external_id": event.event_key,
                        "event_type": event.event_type,
                        "assertion_mode": event.assertion_mode,
                        "effective_at": _iso(event.effective_at),
                        "effective_time_basis": event.effective_time_basis,
                        "amount": primary_amount.value if primary_amount else None,
                        "currency": primary_amount.currency if primary_amount else None,
                        "amount_currency": primary_amount.currency if primary_amount else None,
                        "amount_semantics": primary_amount.semantics if primary_amount else None,
                        "causal_root_key": event.causal_key,
                        "independence_cluster": event.independence_cluster,
                        "confidence": event.confidence,
                        "safe_claim": event.safe_summary,
                        "prohibited_claims": list(event.prohibited_claims),
                        "summary": event.safe_summary,
                        "mention_key": event.subject_mention_key or None,
                        "score_eligible": event.score_eligible,
                        "metadata": event_metadata,
                    }
                )
            documents.append(
                {
                    "document_id": document.document_key,
                    "external_id": document.external_id,
                    "source_external_id": document.external_key,
                    "canonical_url": document.canonical_url,
                    "canonical_key": document.canonical_key,
                    "external_key": document.external_key,
                    "revision_key": document.revision_key,
                    "causal_root_key": document.causal_key,
                    "title": document.title,
                    "summary": document.summary,
                    "published_at": _iso(document.published_at),
                    "source_updated_at": _iso(document.source_updated_at),
                    "observed_at": _iso(document.observed_at),
                    "document_type": document.document_kind,
                    "assertion_mode": document.assertion_mode,
                    "publisher_name": document.publisher_name,
                    "publisher_domain": document.publisher_domain,
                    "content_hash": document.content_sha256,
                    "source_policy_version": document.source_policy_version,
                    "metadata": _json_safe(document.metadata),
                    "mentions": document_mentions,
                    "events": document_events,
                }
            )
        return {
            "source_id": self.source_id,
            "source_policy_version": self.source_policy_version,
            "cursor": _json_safe(self.cursor),
            "etag": self.etag or None,
            "last_modified": self.last_modified or None,
            "rejected_items": self.rejected_items,
            "warnings": list(self.warnings),
            "documents": documents,
        }


@dataclass(frozen=True, slots=True)
class MarketFetchResponse:
    url: str
    content: bytes | str | Mapping[str, Any] | Sequence[Any]
    status_code: int = 200
    mime_type: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)


MarketFetchAdapter = Callable[
    [str, int],
    Awaitable[MarketFetchResponse | bytes | str | Mapping[str, Any] | Sequence[Any]],
]


def _stable_key(kind: str, *parts: Any) -> str:
    encoded = json.dumps(
        _json_safe(parts), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return f"{kind}:v1:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


def canonicalize_market_url(url: str) -> str:
    candidate = str(url or "").strip()
    if not candidate:
        return ""
    try:
        parsed = urlparse(candidate)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        return ""
    if parsed.username or parsed.password:
        return ""
    netloc = host
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    if port and not default_port:
        netloc = f"{host}:{port}"
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_QUERY_KEYS
    ]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunparse(
        (parsed.scheme.lower(), netloc, path, "", urlencode(sorted(query_items)), "")
    )


def build_market_document_keys(
    *,
    source_id: str,
    external_id: str,
    canonical_url: str,
    content_sha256: str,
    causal_parts: Sequence[Any],
) -> MarketDocumentKeys:
    canonical_url = canonicalize_market_url(canonical_url)
    external_key = _stable_key("external", source_id, external_id)
    canonical_key = _stable_key("canonical", canonical_url) if canonical_url else external_key
    revision_key = _stable_key("revision", external_key, content_sha256)
    return MarketDocumentKeys(
        canonical_key=canonical_key,
        external_key=external_key,
        revision_key=revision_key,
        causal_key=_stable_key("causal", *causal_parts),
    )


def _content_hash(*parts: Any) -> str:
    encoded = json.dumps(
        _json_safe(parts), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bounded_payload(payload: bytes | str, maximum_bytes: int) -> str:
    raw = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
    if len(raw) > maximum_bytes:
        raise MarketPayloadError(f"payload exceeds {maximum_bytes} bytes")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MarketPayloadError("payload is not valid UTF-8") from exc


def _bounded_text(value: Any, maximum_chars: int) -> str:
    text = html.unescape(str(value or "")).replace("\x00", " ")
    text = re.sub(r"<[^>]{0,1000}>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:maximum_chars]


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _normalized_causal_title(value: str) -> str:
    value = re.sub(r"^show\s+hn\s*:\s*", "", value, flags=re.I)
    return _normalized_name(value)


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            result = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S", "%Y%m%d"):
                try:
                    result = datetime.strptime(text, fmt).replace(tzinfo=UTC)
                    break
                except ValueError:
                    continue
            else:
                return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _parse_unix_datetime(value: Any) -> datetime | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _policy_limits(
    policy: MarketSourcePolicy, limits: MarketFeedLimits | None
) -> MarketFeedLimits:
    requested = limits or MarketFeedLimits(
        max_response_bytes=policy.max_response_bytes,
        max_items=policy.max_items,
    )
    hard = MarketFeedLimits()
    return MarketFeedLimits(
        max_response_bytes=min(requested.max_response_bytes, policy.max_response_bytes),
        max_items=min(requested.max_items, policy.max_items),
        max_text_chars=min(requested.max_text_chars, hard.max_text_chars),
        max_xml_nodes=min(requested.max_xml_nodes, hard.max_xml_nodes),
        max_xml_depth=min(requested.max_xml_depth, hard.max_xml_depth),
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1].casefold()


def _safe_xml_root(payload: bytes | str, limits: MarketFeedLimits) -> ET.Element:
    text = _bounded_payload(payload, limits.max_response_bytes)
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, flags=re.I):
        raise MarketPayloadError("DTD and entity declarations are not accepted")
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, RecursionError) as exc:
        raise MarketPayloadError("invalid RSS/Atom XML") from exc
    node_count = 0
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        node_count += 1
        if node_count > limits.max_xml_nodes:
            raise MarketPayloadError("XML node limit exceeded")
        if depth > limits.max_xml_depth:
            raise MarketPayloadError("XML depth limit exceeded")
        stack.extend((child, depth + 1) for child in list(node))
    return root


def _element_text(node: ET.Element | None, limit: int) -> str:
    if node is None:
        return ""
    return _bounded_text(" ".join(node.itertext()), limit)


def _first_child_text(entry: ET.Element, names: Iterable[str], limit: int) -> str:
    expected = {name.casefold() for name in names}
    for child in list(entry):
        if _local_name(child.tag) in expected:
            value = _element_text(child, limit)
            if value:
                return value
    return ""


def _entry_link(entry: ET.Element) -> str:
    choices: list[tuple[int, str]] = []
    for child in list(entry):
        if _local_name(child.tag) != "link":
            continue
        href = str(child.attrib.get("href") or _element_text(child, 4_000)).strip()
        if not href:
            continue
        rel = str(child.attrib.get("rel", "alternate")).casefold()
        choices.append((0 if rel == "alternate" else 1, href))
    return canonicalize_market_url(min(choices)[1]) if choices else ""


def _entry_categories(entry: ET.Element, limit: int) -> tuple[str, ...]:
    values: list[str] = []
    for child in list(entry):
        if _local_name(child.tag) != "category":
            continue
        value = str(child.attrib.get("term") or _element_text(child, limit)).strip()
        if value and value not in values:
            values.append(value)
    return tuple(values)


def _feed_entries(root: ET.Element, maximum_items: int) -> tuple[list[ET.Element], int]:
    entries = [node for node in root.iter() if _local_name(node.tag) in {"entry", "item"}]
    return entries[:maximum_items], max(0, len(entries) - maximum_items)


def _mention(
    document: MarketDocument,
    *,
    raw_name: str,
    role: EntityRole,
    identifiers: Mapping[str, str] | None = None,
    candidate_domain: str = "",
    promotable: bool = False,
    resolution_state: ResolutionState = "unresolved",
    confidence: Literal["low", "medium", "high"] = "medium",
    metadata: Mapping[str, Any] | None = None,
) -> MarketEntityMention:
    identifiers = dict(identifiers or {})
    return MarketEntityMention(
        mention_key=_stable_key(
            "mention",
            document.document_key,
            role,
            _normalized_name(raw_name),
            identifiers,
            candidate_domain,
        ),
        document_key=document.document_key,
        raw_name=raw_name,
        normalized_name=_normalized_name(raw_name),
        role=role,
        identifiers=identifiers,
        candidate_domain=candidate_domain,
        promotable=promotable,
        resolution_state=resolution_state,
        confidence=confidence,
        metadata=dict(metadata or {}),
    )


def _event(
    document: MarketDocument,
    *,
    event_type: str,
    assertion_mode: str,
    effective_at: datetime | None,
    subject_mention_key: str,
    safe_summary: str,
    causal_suffix: str = "",
    effective_time_basis: Literal[
        "effective_date", "filing_date", "publication_date", "source_timestamp", "unknown"
    ] = "source_timestamp",
    confidence: Literal["low", "medium", "high"] = "high",
    score_eligible: bool = False,
    amounts: tuple[MarketAmount, ...] = (),
    prohibited_claims: tuple[str, ...] = (
        "automatic qualification",
        "budget",
        "company size",
    ),
    metadata: Mapping[str, Any] | None = None,
) -> MarketEvent:
    causal = (
        _stable_key("causal", document.causal_key, causal_suffix)
        if causal_suffix
        else document.causal_key
    )
    return MarketEvent(
        event_key=_stable_key(
            "event", document.revision_key, event_type, assertion_mode, subject_mention_key
        ),
        document_key=document.document_key,
        causal_key=causal,
        event_type=event_type,
        assertion_mode=assertion_mode,
        effective_at=effective_at,
        effective_time_basis=effective_time_basis if effective_at else "unknown",
        subject_mention_key=subject_mention_key,
        safe_summary=safe_summary,
        prohibited_claims=prohibited_claims,
        confidence=confidence,
        score_eligible=score_eligible,
        independence_cluster=f"source:{document.source_id}",
        amounts=amounts,
        metadata=dict(metadata or {}),
    )


def _build_document(
    *,
    policy: MarketSourcePolicy,
    external_id: str,
    canonical_url: str,
    title: str,
    summary: str,
    document_kind: str,
    assertion_mode: str,
    publisher_name: str,
    publisher_domain: str,
    published_at: datetime | None,
    source_updated_at: datetime | None,
    observed_at: datetime,
    metadata: Mapping[str, Any],
    causal_parts: Sequence[Any],
) -> MarketDocument:
    content_sha256 = _content_hash(
        title,
        summary,
        canonicalize_market_url(canonical_url),
        _iso(published_at),
        _iso(source_updated_at),
        metadata,
    )
    keys = build_market_document_keys(
        source_id=policy.source_id,
        external_id=external_id,
        canonical_url=canonical_url,
        content_sha256=content_sha256,
        causal_parts=causal_parts,
    )
    return MarketDocument(
        source_id=policy.source_id,
        source_policy_version=policy.version,
        document_key=keys.revision_key,
        canonical_key=keys.canonical_key,
        external_key=keys.external_key,
        revision_key=keys.revision_key,
        causal_key=keys.causal_key,
        external_id=external_id,
        canonical_url=canonicalize_market_url(canonical_url),
        title=title,
        summary=summary,
        document_kind=document_kind,
        assertion_mode=assertion_mode,
        publisher_name=publisher_name,
        publisher_domain=publisher_domain,
        published_at=published_at,
        source_updated_at=source_updated_at,
        observed_at=observed_at,
        content_sha256=content_sha256,
        metadata=dict(metadata),
    )


def _finalize_result(
    policy: MarketSourcePolicy,
    documents: Iterable[MarketDocument],
    mentions: Iterable[MarketEntityMention],
    events: Iterable[MarketEvent],
    *,
    rejected_items: int = 0,
    warnings: Iterable[str] = (),
) -> MarketParseResult:
    document_map = {item.document_key: item for item in documents}
    mention_map = {item.mention_key: item for item in mentions}
    event_map = {item.event_key: item for item in events}
    return MarketParseResult(
        source_id=policy.source_id,
        source_policy_version=policy.version,
        documents=tuple(document_map.values()),
        mentions=tuple(mention_map.values()),
        events=tuple(event_map.values()),
        rejected_items=rejected_items,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _merge_results(
    results: Sequence[MarketParseResult], policy: MarketSourcePolicy
) -> MarketParseResult:
    documents: dict[str, MarketDocument] = {}
    mentions: dict[str, MarketEntityMention] = {}
    events: dict[str, MarketEvent] = {}
    rejected = 0
    warnings: list[str] = []
    for result in results:
        documents.update({item.document_key: item for item in result.documents})
        mentions.update({item.mention_key: item for item in result.mentions})
        events.update({item.event_key: item for item in result.events})
        rejected += result.rejected_items
        warnings.extend(result.warnings)
    return MarketParseResult(
        source_id=policy.source_id,
        source_policy_version=policy.version,
        documents=tuple(documents.values()),
        mentions=tuple(mentions.values()),
        events=tuple(events.values()),
        rejected_items=rejected,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _sec_form(entry: ET.Element, summary: str, title: str, allowed: tuple[str, ...]) -> str:
    allowed_map = {item.upper(): item for item in allowed}
    candidates = list(_entry_categories(entry, 100))
    explicit = _first_child_text(entry, ("form-type", "formtype", "form"), 100)
    if explicit:
        candidates.append(explicit)
    match = re.search(r"\bForm\s+Type\s*:\s*([A-Z0-9-]+(?:/A)?)\b", summary, flags=re.I)
    if match:
        candidates.append(match.group(1))
    prefix = re.match(r"^\s*([A-Z0-9-]+(?:/A)?)\s*[-:]", title, flags=re.I)
    if prefix:
        candidates.append(prefix.group(1))
    for candidate in candidates:
        normalized = re.sub(r"\s+", "", candidate).upper()
        if normalized in allowed_map:
            return allowed_map[normalized]
    return ""


def _sec_accession(entry_id: str, link: str) -> str:
    for value in (entry_id, link):
        match = _SEC_ACCESSION_RE.search(value)
        if match:
            return match.group(0)
    if "accession-number=" in entry_id:
        return entry_id.rsplit("accession-number=", 1)[-1].strip()
    return entry_id or link


def _sec_company_name(entry: ET.Element, title: str, summary: str, form: str) -> str:
    explicit = _first_child_text(
        entry,
        ("company-name", "companyname", "conformed-name", "issuer-name"),
        500,
    )
    if explicit:
        return explicit
    match = re.search(
        r"\b(?:Company|Issuer|Registrant|Filed by)\s+Name\s*:\s*([^;|<]{2,300})",
        summary,
        flags=re.I,
    )
    if match:
        return _bounded_text(match.group(1), 300)
    value = re.sub(rf"^\s*{re.escape(form)}\s*[-:]\s*", "", title, flags=re.I)
    value = re.sub(r"\s*\((?:CIK\s*)?0*\d{1,10}\)\s*$", "", value, flags=re.I)
    return _bounded_text(value, 300)


def _sec_cik(entry: ET.Element, title: str, summary: str, accession: str) -> str:
    explicit = _first_child_text(entry, ("cik", "cik-number", "ciknumber"), 100)
    match = _SEC_CIK_RE.search(" ".join((explicit, title, summary)))
    if match:
        return match.group(1).zfill(10)
    parenthetical = re.search(r"\((0?\d{7,10})\)\s*$", title)
    if parenthetical:
        return parenthetical.group(1).zfill(10)
    if _SEC_ACCESSION_RE.fullmatch(accession):
        return accession.split("-", 1)[0]
    return ""


def _sec_amounts(summary: str, form: str) -> tuple[MarketAmount, ...]:
    if form not in {"D", "D/A"}:
        return ()
    results: list[MarketAmount] = []
    patterns = (
        (
            "amount_sold_as_of_filing",
            rf"\b(?:Total\s+)?Amount\s+Sold(?:\s+as\s+of\s+(?:the\s+)?filing)?\s*[:=]?\s*{_MONEY_RE}",
        ),
        (
            "total_offering_amount",
            rf"\bTotal\s+Offering\s+Amount\s*[:=]?\s*{_MONEY_RE}",
        ),
    )
    for semantics, pattern in patterns:
        match = re.search(pattern, summary, flags=re.I)
        if not match:
            continue
        value = match.group(1).replace(",", "")
        results.append(MarketAmount(value=value, currency="USD", semantics=semantics))
    return tuple(results)


def _sec_primary_semantics(form: str) -> tuple[str, str, str, EntityRole]:
    if form in {"D", "D/A"}:
        return (
            "private_offering_notice",
            "filed_amendment" if form.endswith("/A") else "filed_notice",
            "Form D amendment notice" if form.endswith("/A") else "Form D notice",
            "issuer",
        )
    if form in {"S-1", "S-1/A", "F-1", "F-1/A"}:
        return (
            "registration_statement",
            "filed_amendment" if form.endswith("/A") else "filed_registration_statement",
            "registration-statement amendment" if form.endswith("/A") else "registration statement",
            "registrant",
        )
    if form in {"8-K", "8-K/A"}:
        return (
            "current_report_filing",
            "filed_amendment" if form.endswith("/A") else "filed_current_report",
            "8-K amendment" if form.endswith("/A") else "8-K current report",
            "filer",
        )
    if form in {"C", "C/A", "C-U", "C-U/A"}:
        return (
            "crowdfunding_filing",
            "filed_amendment" if form.endswith("/A") else "filed_crowdfunding_disclosure",
            f"Form {form} crowdfunding filing",
            "issuer",
        )
    return (
        "regulation_a_filing",
        "filed_amendment" if form.endswith("/A") else "filed_offering_disclosure",
        f"Form {form} Regulation A filing",
        "issuer",
    )


_SEC_ITEM_EVENT_TYPES: Mapping[str, str] = MappingProxyType(
    {
        "1.01": "material_agreement_disclosure",
        "2.01": "acquisition_or_disposition_disclosure",
        "2.02": "results_of_operations_disclosure",
        "3.02": "unregistered_securities_sale_disclosure",
        "5.02": "leadership_change_disclosure",
    }
)


def _sec_items(summary: str) -> tuple[str, ...]:
    items: list[str] = []
    for match in _SEC_ITEM_RE.finditer(summary):
        items.extend(re.findall(r"\d\.\d{2}", match.group(1)))
    return tuple(dict.fromkeys(items))


def parse_sec_atom_feed(
    payload: bytes | str,
    source_id: str,
    *,
    observed_at: datetime | None = None,
    limits: MarketFeedLimits | None = None,
) -> MarketParseResult:
    policy = require_market_source_policy(source_id, capability="store_metadata")
    if policy.parser != "sec_atom":
        raise ValueError(f"{source_id} is not an SEC Atom source")
    limits = _policy_limits(policy, limits)
    observed_at = (observed_at or datetime.now(UTC)).astimezone(UTC)
    root = _safe_xml_root(payload, limits)
    entries, rejected = _feed_entries(root, limits.max_items)
    documents: list[MarketDocument] = []
    mentions: list[MarketEntityMention] = []
    events: list[MarketEvent] = []
    warnings: list[str] = []

    for entry in entries:
        title = _first_child_text(entry, ("title",), limits.max_text_chars)
        summary = _first_child_text(
            entry, ("summary", "description", "content"), limits.max_text_chars
        )
        form = _sec_form(entry, summary, title, policy.allowed_forms)
        if not form:
            rejected += 1
            continue
        link = _entry_link(entry)
        entry_id = _first_child_text(entry, ("id", "guid"), 1_000)
        accession = _sec_accession(entry_id, link)
        if not accession:
            rejected += 1
            warnings.append("SEC entry omitted because it had no accession or stable link")
            continue
        published_at = _parse_datetime(
            _first_child_text(entry, ("filing-date", "published", "pubdate"), 200)
        )
        source_updated_at = _parse_datetime(
            _first_child_text(entry, ("updated", "lastbuilddate"), 200)
        )
        effective_at = published_at or source_updated_at
        company_name = _sec_company_name(entry, title, summary, form)
        cik = _sec_cik(entry, title, summary, accession)
        file_match = _SEC_FILE_NUMBER_RE.search(summary)
        file_number = file_match.group(1).upper() if file_match else ""
        event_type, assertion_mode, filing_label, role = _sec_primary_semantics(form)
        causal_hint = file_number or accession
        causal_form = form.removesuffix("/A")
        metadata = {
            "accession_number": accession,
            "cik": cik,
            "company_name": company_name,
            "file_number": file_number,
            "form": form,
            "is_amendment": form.endswith("/A"),
            "item_numbers": list(_sec_items(summary)) if form in {"8-K", "8-K/A"} else [],
        }
        document = _build_document(
            policy=policy,
            external_id=accession,
            canonical_url=link,
            title=title,
            summary=summary,
            document_kind="sec_filing_metadata",
            assertion_mode=assertion_mode,
            publisher_name="U.S. Securities and Exchange Commission",
            publisher_domain="sec.gov",
            published_at=published_at,
            source_updated_at=source_updated_at,
            observed_at=observed_at,
            metadata=metadata,
            causal_parts=(
                "sec_filing",
                cik or _normalized_name(company_name),
                causal_form,
                causal_hint,
            ),
        )
        documents.append(document)
        subject = _mention(
            document,
            raw_name=company_name or f"SEC filer {cik or accession}",
            role=role,
            identifiers={key: value for key, value in {"cik": cik}.items() if value},
            confidence="high" if cik else "medium",
            metadata={"form": form},
        )
        mentions.append(subject)

        amounts = _sec_amounts(summary, form)
        if form in {"D", "D/A"}:
            safe_summary = (
                f"{subject.raw_name} filed a {filing_label} reporting literal offering facts; "
                "the filing does not establish that the total offering amount was raised."
            )
        elif form in {"S-1", "S-1/A", "F-1", "F-1/A"}:
            safe_summary = (
                f"{subject.raw_name} filed a {filing_label}; the filing does not establish "
                "that an IPO or other offering was completed."
            )
        elif form in {"8-K", "8-K/A"}:
            safe_summary = (
                f"{subject.raw_name} filed an {filing_label}; no funding, acquisition, or "
                "agreement is inferred without an explicit item disclosure."
            )
        else:
            safe_summary = f"{subject.raw_name} filed a {filing_label}."
        events.append(
            _event(
                document,
                event_type=event_type,
                assertion_mode=assertion_mode,
                effective_at=effective_at,
                subject_mention_key=subject.mention_key,
                safe_summary=safe_summary,
                effective_time_basis="filing_date",
                amounts=amounts,
                prohibited_claims=(
                    (
                        "money raised",
                        "total offering amount raised",
                        "SEC approval",
                        "valuation",
                        "runway",
                    )
                    if form in {"D", "D/A"}
                    else (
                        "completed IPO",
                        "completed offering",
                        "capital raised",
                        "SEC approval",
                    )
                    if form in {"S-1", "S-1/A", "F-1", "F-1/A"}
                    else (
                        "funding event",
                        "completed acquisition",
                        "material agreement",
                    )
                    if form in {"8-K", "8-K/A"}
                    else ("capital raised", "SEC approval", "automatic qualification")
                ),
                metadata={"form": form, "is_amendment": form.endswith("/A")},
            )
        )
        if form in {"8-K", "8-K/A"}:
            for item_number in _sec_items(summary):
                item_event_type = _SEC_ITEM_EVENT_TYPES.get(item_number)
                if not item_event_type:
                    continue
                events.append(
                    _event(
                        document,
                        event_type=item_event_type,
                        assertion_mode="filed_item_disclosure",
                        effective_at=effective_at,
                        subject_mention_key=subject.mention_key,
                        safe_summary=(
                            f"{subject.raw_name} filed an 8-K disclosure under Item {item_number}; "
                            "this records the filing category without extending its claims."
                        ),
                        causal_suffix=f"item:{item_number}",
                        effective_time_basis="filing_date",
                        prohibited_claims=(
                            "transaction completion",
                            "transaction value",
                            "budget",
                            "automatic qualification",
                        ),
                        metadata={"form": form, "item_number": item_number},
                    )
                )
    return _finalize_result(
        policy,
        documents,
        mentions,
        events,
        rejected_items=rejected,
        warnings=warnings,
    )


def parse_official_rss_atom(
    payload: bytes | str,
    source_id: str,
    *,
    observed_at: datetime | None = None,
    limits: MarketFeedLimits | None = None,
) -> MarketParseResult:
    policy = require_market_source_policy(source_id, capability="store_metadata")
    if policy.parser != "rss_atom":
        raise ValueError(f"{source_id} is not an enabled RSS/Atom metadata source")
    limits = _policy_limits(policy, limits)
    observed_at = (observed_at or datetime.now(UTC)).astimezone(UTC)
    root = _safe_xml_root(payload, limits)
    entries, rejected = _feed_entries(root, limits.max_items)
    documents: list[MarketDocument] = []
    mentions: list[MarketEntityMention] = []
    events: list[MarketEvent] = []
    for entry in entries:
        title = _first_child_text(entry, ("title",), limits.max_text_chars)
        summary = _first_child_text(
            entry, ("summary", "description", "content"), limits.max_text_chars
        )
        link = _entry_link(entry)
        external_id = _first_child_text(entry, ("id", "guid"), 1_000) or link
        if not external_id or not title:
            rejected += 1
            continue
        published_at = _parse_datetime(
            _first_child_text(entry, ("published", "pubdate", "date"), 200)
        )
        source_updated_at = _parse_datetime(_first_child_text(entry, ("updated",), 200))
        causal_title = _normalized_causal_title(title)
        effective_day = (published_at or source_updated_at or observed_at).date().isoformat()
        document = _build_document(
            policy=policy,
            external_id=external_id,
            canonical_url=link,
            title=title,
            summary=summary,
            document_kind="government_release_metadata",
            assertion_mode="agency_published_release",
            publisher_name=policy.label,
            publisher_domain=policy.official_domains[0],
            published_at=published_at,
            source_updated_at=source_updated_at,
            observed_at=observed_at,
            metadata={"categories": list(_entry_categories(entry, 200))},
            causal_parts=("government_release", causal_title, effective_day),
        )
        documents.append(document)
        regulator = _mention(
            document,
            raw_name=policy.label,
            role="regulator",
            identifiers={"domain": policy.official_domains[0]},
            resolution_state="regulator_only",
            confidence="high",
        )
        mentions.append(regulator)
        events.append(
            _event(
                document,
                event_type="regulatory_release",
                assertion_mode="agency_published_release",
                effective_at=published_at or source_updated_at,
                subject_mention_key="",
                safe_summary=(
                    f"{policy.label} published a release titled “{title}”; the metadata alone "
                    "does not establish any allegation, judgment, settlement, or final order."
                ),
                effective_time_basis="publication_date",
                prohibited_claims=(
                    "allegation",
                    "judgment",
                    "settlement",
                    "final order",
                    "automatic disqualification",
                ),
                metadata={"requires_subject_resolution": True},
            )
        )
    return _finalize_result(policy, documents, mentions, events, rejected_items=rejected)


def _load_json_payload(payload: bytes | str | Mapping[str, Any] | Sequence[Any], limit: int) -> Any:
    if isinstance(payload, (bytes, str)):
        text = _bounded_payload(payload, limit)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise MarketPayloadError("invalid JSON payload") from exc
    return payload


def parse_federal_register_json(
    payload: bytes | str | Mapping[str, Any],
    source_id: str = "federal_register_documents_v1",
    *,
    observed_at: datetime | None = None,
    limits: MarketFeedLimits | None = None,
) -> MarketParseResult:
    policy = require_market_source_policy(source_id, capability="store_metadata")
    if policy.parser != "federal_register_json":
        raise ValueError(f"{source_id} is not a Federal Register source")
    limits = _policy_limits(policy, limits)
    observed_at = (observed_at or datetime.now(UTC)).astimezone(UTC)
    parsed = _load_json_payload(payload, limits.max_response_bytes)
    if not isinstance(parsed, Mapping) or not isinstance(parsed.get("results"), list):
        raise MarketPayloadError("Federal Register payload must contain a results list")
    rows = parsed["results"]
    documents: list[MarketDocument] = []
    mentions: list[MarketEntityMention] = []
    events: list[MarketEvent] = []
    rejected = max(0, len(rows) - limits.max_items)
    for row in rows[: limits.max_items]:
        if not isinstance(row, Mapping):
            rejected += 1
            continue
        external_id = _bounded_text(row.get("document_number"), 200)
        title = _bounded_text(row.get("title"), limits.max_text_chars)
        canonical_url = canonicalize_market_url(
            str(row.get("html_url") or row.get("pdf_url") or "")
        )
        if not external_id or not title or not canonical_url:
            rejected += 1
            continue
        summary = _bounded_text(row.get("abstract"), limits.max_text_chars)
        published_at = _parse_datetime(row.get("publication_date"))
        effective_at = _parse_datetime(row.get("effective_on")) or published_at
        agencies = row.get("agencies") if isinstance(row.get("agencies"), list) else []
        document_type = _bounded_text(row.get("type"), 200)
        docket_ids = [
            _bounded_text(item, 200)
            for item in row.get("docket_ids", [])
            if isinstance(item, str) and item.strip()
        ] if isinstance(row.get("docket_ids"), list) else []
        regulation_ids = [
            _bounded_text(item, 200)
            for item in row.get("regulation_id_numbers", [])
            if isinstance(item, str) and item.strip()
        ] if isinstance(row.get("regulation_id_numbers"), list) else []
        causal_hint = regulation_ids or docket_ids or [external_id]
        metadata = {
            "action": _bounded_text(row.get("action"), limits.max_text_chars),
            "agencies": _json_safe(agencies),
            "docket_ids": docket_ids,
            "document_number": external_id,
            "document_type": document_type,
            "effective_on": _iso(_parse_datetime(row.get("effective_on"))),
            "regulation_id_numbers": regulation_ids,
            "official_legal_status_note": (
                "FederalRegister.gov API metadata is informational; use the official PDF on "
                "GovInfo for legal reliance."
            ),
        }
        document = _build_document(
            policy=policy,
            external_id=external_id,
            canonical_url=canonical_url,
            title=title,
            summary=summary,
            document_kind="federal_register_metadata",
            assertion_mode="published_notice",
            publisher_name="Office of the Federal Register",
            publisher_domain="federalregister.gov",
            published_at=published_at,
            source_updated_at=None,
            observed_at=observed_at,
            metadata=metadata,
            causal_parts=("federal_register", document_type.casefold(), causal_hint),
        )
        documents.append(document)
        for agency in agencies[:20]:
            if not isinstance(agency, Mapping):
                continue
            agency_name = _bounded_text(agency.get("name") or agency.get("raw_name"), 500)
            if not agency_name:
                continue
            identifiers = (
                {"federal_register_agency_id": str(agency["id"])}
                if agency.get("id") is not None
                else {}
            )
            mentions.append(
                _mention(
                    document,
                    raw_name=agency_name,
                    role="regulator",
                    identifiers=identifiers,
                    resolution_state="regulator_only",
                    confidence="high",
                )
            )
        events.append(
            _event(
                document,
                event_type="government_notice",
                assertion_mode="published_notice",
                effective_at=effective_at,
                subject_mention_key="",
                safe_summary=(
                    f"The Federal Register published a {document_type or 'document'} titled "
                    f"“{title}”; no company impact is inferred from metadata alone."
                ),
                effective_time_basis=(
                    "effective_date" if row.get("effective_on") else "publication_date"
                ),
                prohibited_claims=(
                    "company impact",
                    "legal conclusion",
                    "automatic qualification",
                ),
                metadata={"document_number": external_id, "document_type": document_type},
            )
        )
    return _finalize_result(policy, documents, mentions, events, rejected_items=rejected)


def parse_gdelt_metadata_json(
    payload: bytes | str | Mapping[str, Any],
    source_id: str = "gdelt_doc_metadata_v2",
    *,
    observed_at: datetime | None = None,
    limits: MarketFeedLimits | None = None,
) -> MarketParseResult:
    policy = require_market_source_policy(source_id, capability="store_metadata")
    if policy.parser != "gdelt_json":
        raise ValueError(f"{source_id} is not a GDELT metadata source")
    limits = _policy_limits(policy, limits)
    observed_at = (observed_at or datetime.now(UTC)).astimezone(UTC)
    parsed = _load_json_payload(payload, limits.max_response_bytes)
    if not isinstance(parsed, Mapping) or not isinstance(parsed.get("articles"), list):
        raise MarketPayloadError("GDELT payload must contain an articles list")
    rows = parsed["articles"]
    documents: list[MarketDocument] = []
    mentions: list[MarketEntityMention] = []
    events: list[MarketEvent] = []
    rejected = max(0, len(rows) - limits.max_items)
    for row in rows[: limits.max_items]:
        if not isinstance(row, Mapping):
            rejected += 1
            continue
        canonical_url = canonicalize_market_url(str(row.get("url") or ""))
        title = _bounded_text(row.get("title"), limits.max_text_chars)
        if not canonical_url or not title:
            rejected += 1
            continue
        parsed_url = urlparse(canonical_url)
        publisher_domain = (parsed_url.hostname or "").casefold()
        published_at = _parse_datetime(row.get("seendate"))
        effective_day = (published_at or observed_at).date().isoformat()
        external_id = canonical_url
        metadata = {
            "gdelt_domain": _bounded_text(row.get("domain"), 500),
            "language": _bounded_text(row.get("language"), 100),
            "source_country": _bounded_text(row.get("sourcecountry"), 100),
            "social_image": canonicalize_market_url(str(row.get("socialimage") or "")),
            "url_mobile": canonicalize_market_url(str(row.get("url_mobile") or "")),
        }
        document = _build_document(
            policy=policy,
            external_id=external_id,
            canonical_url=canonical_url,
            title=title,
            summary="",
            document_kind="news_article_metadata",
            assertion_mode="indexed_by_gdelt",
            publisher_name=publisher_domain,
            publisher_domain=publisher_domain,
            published_at=published_at,
            source_updated_at=None,
            observed_at=observed_at,
            metadata=metadata,
            causal_parts=("news_attention", _normalized_causal_title(title), effective_day),
        )
        documents.append(document)
        publisher = _mention(
            document,
            raw_name=publisher_domain,
            role="publisher",
            identifiers={"domain": publisher_domain},
            candidate_domain="",
            promotable=False,
            resolution_state="publisher_only",
            confidence="high",
            metadata={"never_promote_as_subject_from_gdelt": True},
        )
        mentions.append(publisher)
        events.append(
            _event(
                document,
                event_type="news_attention_metadata",
                assertion_mode="indexed_by_gdelt",
                effective_at=published_at,
                subject_mention_key="",
                safe_summary=(
                    f"GDELT indexed metadata for a story titled “{title}” from "
                    f"{publisher_domain}; the publisher domain is not a company identity signal."
                ),
                effective_time_basis="source_timestamp",
                confidence="medium",
                prohibited_claims=(
                    "subject company identity",
                    "independent confirmation",
                    "traction",
                    "budget",
                ),
                metadata={"requires_subject_resolution": True},
            )
        )
    return _finalize_result(policy, documents, mentions, events, rejected_items=rejected)


def parse_hacker_news_story_ids(
    payload: bytes | str | Sequence[Any],
    *,
    maximum_bytes: int = 500_000,
    maximum_items: int = 100,
) -> tuple[int, ...]:
    parsed = _load_json_payload(payload, maximum_bytes)
    if not isinstance(parsed, Sequence) or isinstance(parsed, (str, bytes, bytearray)):
        raise MarketPayloadError("Hacker News story index must be a JSON list")
    results: list[int] = []
    for value in parsed:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            results.append(value)
        if len(results) >= maximum_items:
            break
    return tuple(dict.fromkeys(results))


def _safe_hn_candidate_domain(url: str) -> str:
    canonical = canonicalize_market_url(url)
    if not canonical:
        return ""
    parsed = urlparse(canonical)
    host = (parsed.hostname or "").rstrip(".").casefold()
    if not host or host.endswith(_RESERVED_HOST_SUFFIXES):
        return ""
    try:
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        pass
    extracted = _TLD_EXTRACT(host)
    if not extracted.domain or not extracted.suffix:
        return ""
    registrable = f"{extracted.domain}.{extracted.suffix}".casefold()
    if registrable in _BLOCKED_HN_REGISTRABLE_DOMAINS:
        return ""
    return registrable


def parse_hacker_news_items(
    payload: bytes | str | Mapping[str, Any] | Sequence[Any],
    source_id: str,
    *,
    observed_at: datetime | None = None,
    limits: MarketFeedLimits | None = None,
) -> MarketParseResult:
    policy = require_market_source_policy(source_id, capability="store_metadata")
    if policy.parser != "hacker_news_json":
        raise ValueError(f"{source_id} is not a Hacker News source")
    limits = _policy_limits(policy, limits)
    observed_at = (observed_at or datetime.now(UTC)).astimezone(UTC)
    parsed = _load_json_payload(payload, limits.max_response_bytes)
    rows = parsed if isinstance(parsed, list) else [parsed]
    documents: list[MarketDocument] = []
    mentions: list[MarketEntityMention] = []
    events: list[MarketEvent] = []
    rejected = max(0, len(rows) - limits.max_items)
    for row in rows[: limits.max_items]:
        if not isinstance(row, Mapping) or row.get("deleted") or row.get("dead"):
            rejected += 1
            continue
        item_id = row.get("id")
        item_type = str(row.get("type") or "")
        title = _bounded_text(row.get("title"), limits.max_text_chars)
        if (
            not isinstance(item_id, int)
            or isinstance(item_id, bool)
            or item_type != "story"
            or not title
        ):
            rejected += 1
            continue
        published_at = _parse_unix_datetime(row.get("time"))
        hn_url = f"https://news.ycombinator.com/item?id={item_id}"
        outbound_url = canonicalize_market_url(str(row.get("url") or ""))
        is_show_hn = bool(re.match(r"^\s*Show\s+HN\s*:", title, flags=re.I))
        candidate_domain = _safe_hn_candidate_domain(outbound_url) if is_show_hn else ""
        metadata = {
            "by": _bounded_text(row.get("by"), 200),
            "descendants": (
                row.get("descendants") if isinstance(row.get("descendants"), int) else None
            ),
            "hn_item_id": item_id,
            "is_show_hn": is_show_hn,
            "outbound_url": outbound_url,
            "score": row.get("score") if isinstance(row.get("score"), int) else None,
            "text": _bounded_text(row.get("text"), limits.max_text_chars),
        }
        document = _build_document(
            policy=policy,
            external_id=str(item_id),
            canonical_url=hn_url,
            title=title,
            summary=metadata["text"],
            document_kind="hacker_news_item_metadata",
            assertion_mode="community_submission",
            publisher_name="Hacker News",
            publisher_domain="news.ycombinator.com",
            published_at=published_at,
            source_updated_at=None,
            observed_at=observed_at,
            metadata=metadata,
            causal_parts=("hacker_news_item", item_id),
        )
        documents.append(document)
        mentions.append(
            _mention(
                document,
                raw_name="Hacker News",
                role="publisher",
                identifiers={"domain": "news.ycombinator.com"},
                resolution_state="publisher_only",
                confidence="high",
            )
        )
        subject_mention_key = ""
        if candidate_domain:
            subject = _mention(
                document,
                raw_name=candidate_domain,
                role="subject",
                identifiers={"domain": candidate_domain, "hn_item_id": str(item_id)},
                candidate_domain=candidate_domain,
                promotable=True,
                resolution_state="direct_domain_candidate",
                confidence="medium",
                metadata={
                    "identity_basis": "public outbound domain on a Show HN submission",
                    "not_claimed": ["traction", "customers", "revenue", "company_size"],
                },
            )
            mentions.append(subject)
            subject_mention_key = subject.mention_key
        event_type = "show_hn_submission" if is_show_hn else "hacker_news_attention"
        events.append(
            _event(
                document,
                event_type=event_type,
                assertion_mode="community_submission",
                effective_at=published_at,
                subject_mention_key=subject_mention_key,
                safe_summary=(
                    f"A Show HN submission linked to {candidate_domain}."
                    if candidate_domain
                    else f"Hacker News carried a submission titled “{title}”; no company domain "
                    "or business traction is inferred."
                ),
                effective_time_basis="source_timestamp",
                confidence="medium",
                prohibited_claims=(
                    "traction",
                    "customers",
                    "revenue",
                    "company size",
                    "independent confirmation",
                ),
                metadata={
                    "hn_score_is_attention_only": True,
                    "promotable_direct_domain": bool(candidate_domain),
                },
            )
        )
    return _finalize_result(policy, documents, mentions, events, rejected_items=rejected)


def _coerce_fetch_response(
    response: MarketFetchResponse | bytes | str | Mapping[str, Any] | Sequence[Any],
    url: str,
) -> MarketFetchResponse:
    if isinstance(response, MarketFetchResponse):
        return response
    return MarketFetchResponse(url=url, content=response)


async def poll_market_source(
    source_id: str,
    fetch: MarketFetchAdapter,
    *,
    observed_at: datetime | None = None,
    endpoint_urls: Sequence[str] | None = None,
    limits: MarketFeedLimits | None = None,
) -> MarketParseResult:
    policy = require_market_source_policy(source_id, capability="fetch")
    observed_at = (observed_at or datetime.now(UTC)).astimezone(UTC)
    limits = _policy_limits(policy, limits)
    urls = tuple(endpoint_urls or policy.endpoint_urls)
    if not urls:
        raise ValueError(f"No endpoint URL is configured for {source_id}")
    for url in urls:
        canonical = canonicalize_market_url(url)
        host = (urlparse(canonical).hostname or "").casefold()
        if not canonical or not any(
            host == domain or host.endswith(f".{domain}") for domain in policy.official_domains
        ):
            raise MarketSourcePolicyDenied(
                source_id, "fetch", f"endpoint host is not approved: {host}"
            )

    results: list[MarketParseResult] = []
    validators: dict[str, dict[str, str]] = {}
    requests_used = 0
    if policy.parser == "hacker_news_json":
        index_url = urls[0]
        response = _coerce_fetch_response(
            await fetch(index_url, limits.max_response_bytes), index_url
        )
        requests_used += 1
        if not 200 <= response.status_code < 300:
            raise MarketPayloadError(f"source returned HTTP {response.status_code}")
        story_ids = parse_hacker_news_story_ids(
            response.content,
            maximum_bytes=limits.max_response_bytes,
            maximum_items=min(limits.max_items, policy.max_requests - 1),
        )
        validators[index_url] = _response_validators(response.headers)
        for story_id in story_ids:
            if requests_used >= policy.max_requests:
                break
            item_url = f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json"
            item_response = _coerce_fetch_response(
                await fetch(item_url, limits.max_response_bytes), item_url
            )
            requests_used += 1
            if not 200 <= item_response.status_code < 300:
                results.append(
                    MarketParseResult(
                        source_id=source_id,
                        source_policy_version=policy.version,
                        rejected_items=1,
                        warnings=(f"HN item {story_id} returned HTTP {item_response.status_code}",),
                    )
                )
                continue
            validators[item_url] = _response_validators(item_response.headers)
            results.append(
                parse_hacker_news_items(
                    item_response.content,
                    source_id,
                    observed_at=observed_at,
                    limits=replace(limits, max_items=1),
                )
            )
    else:
        for url in urls[: policy.max_requests]:
            response = _coerce_fetch_response(
                await fetch(url, limits.max_response_bytes), url
            )
            requests_used += 1
            if not 200 <= response.status_code < 300:
                raise MarketPayloadError(f"source returned HTTP {response.status_code}")
            validators[url] = _response_validators(response.headers)
            if policy.parser == "sec_atom":
                parsed_result = parse_sec_atom_feed(
                    response.content,
                    source_id,
                    observed_at=observed_at,
                    limits=limits,
                )
            elif policy.parser == "rss_atom":
                parsed_result = parse_official_rss_atom(
                    response.content,
                    source_id,
                    observed_at=observed_at,
                    limits=limits,
                )
            elif policy.parser == "federal_register_json":
                parsed_result = parse_federal_register_json(
                    response.content,
                    source_id,
                    observed_at=observed_at,
                    limits=limits,
                )
            elif policy.parser == "gdelt_json":
                parsed_result = parse_gdelt_metadata_json(
                    response.content,
                    source_id,
                    observed_at=observed_at,
                    limits=limits,
                )
            else:
                raise ValueError(f"No parser adapter is implemented for {source_id}")
            results.append(parsed_result)
    merged = _merge_results(results, policy)
    latest_source_time = max(
        (
            item.published_at or item.source_updated_at
            for item in merged.documents
            if item.published_at or item.source_updated_at
        ),
        default=None,
    )
    first_validator = next((value for value in validators.values() if value), {})
    return replace(
        merged,
        cursor={
            "latest_published_at": _iso(latest_source_time),
            "requests_used": requests_used,
            "response_validators": validators,
        },
        etag=first_validator.get("etag", ""),
        last_modified=first_validator.get("last_modified", ""),
    )


def _response_validators(headers: Mapping[str, str]) -> dict[str, str]:
    lowered = {str(key).casefold(): str(value) for key, value in headers.items()}
    return {
        key: value
        for key, value in {
            "etag": lowered.get("etag", ""),
            "last_modified": lowered.get("last-modified", ""),
        }.items()
        if value
    }
