from __future__ import annotations

import asyncio
import hashlib
import heapq
import ipaddress
import json
import os
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape as html_unescape
from io import BytesIO
from typing import Literal
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import httpx
import tldextract
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field
from pypdf import PdfReader

from . import source_discovery_ai
from .agency_discovery import canonicalize_public_website
from .discovery import DiscoveryError, discover_apex
from .domain_policy import is_disallowed
from .inspect import (
    UnsafeTargetError,
    _bounded_get,
    _extract_company_appearances,
    _filename_identity_hint,
    _iter_image_asset_urls,
    fetch_microlink_rendered_html,
    microlink_fallback_eligible,
)
from .models import DiscoveredHost, DiscoveryAssertionEvidence, SourceSuggestion
from .source_crawl_yield import CrawlYieldMonitor

SourceKind = Literal[
    "tenant_apex",
    "agency_directory",
    "brand_directory",
    "brand_group_portfolio",
    "platform_customer_list",
    "repeatable_search",
]
ExecutionMethod = Literal[
    "certificate_transparency",
    "public_site_crawl",
    "repeatable_web_search",
]

DEFAULT_CT_ENDPOINT = "https://crt.name/v1/search"
DEFAULT_AI_MODEL = "gpt-5-mini"
SOURCE_MICROLINK_MAX_PAGES = 10
GOOGLE_PARTNERS_HOST = "partnersdirectory.withgoogle.com"
GOOGLE_PARTNERS_API_URL = "https://partnersdirectory.withgoogle.com/api/v1/partners/"
SHOPIFY_CASE_STUDY_SEEDS = (
    "https://www.shopify.com/sitemap/case-studies",
    "https://www.shopify.com/case-studies",
)
_TLD_EXTRACT = tldextract.TLDExtract(
    suffix_list_urls=(),
    include_psl_private_domains=True,
)
_ICANN_TLD_EXTRACT = tldextract.TLDExtract(
    suffix_list_urls=(),
    include_psl_private_domains=False,
)
_DNS_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.I,
)
_SOURCE_URL_ANNOTATION_RE = re.compile(r"[\s<>{}\"'`|\\^]")
_EMBEDDED_SOURCE_URL_RE = re.compile(
    r"https?://[^\s<>{}\[\]\"'`|\\^\u2010-\u2015]+",
    re.I,
)
_RESERVED_SOURCE_HOSTS = frozenset({"example.com", "example.net", "example.org"})
_RESERVED_SOURCE_SUFFIXES = (".arpa",)

_VISIBLE_URL_RE = re.compile(
    r"(?:(?:https?://|www\.)[^\s<>{}\[\]\"']+)",
    re.I,
)
_BARE_DOMAIN_RE = re.compile(
    r"(?<![@\w.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,24}(?![\w.-])",
    re.I,
)
_BARE_WEBSITE_INTENT_RE = re.compile(
    r"\b(?:(?:website|web\s*site|domain|url)(?:\s+(?:is|at))?|visit|browse|go\s+to)"
    r"\s*[:=]?\s*$",
    re.I,
)
_ASSET_SUFFIXES = {
    ".7z",
    ".avi",
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".eot",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".map",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".png",
    ".rar",
    ".svg",
    ".tar",
    ".tgz",
    ".ttf",
    ".wav",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}
_ASSET_HOST_LABELS = {
    "asset",
    "assets",
    "cdn",
    "font",
    "fonts",
    "image",
    "images",
    "img",
    "media",
    "static",
}
_BLOCKED_PROVIDER_DOMAINS = {
    "adobe.com",
    "akamai.net",
    "amazonaws.com",
    "apple.com",
    "azureedge.net",
    "azurewebsites.net",
    "bing.com",
    "bit.ly",
    "cloudflare.com",
    "cloudfront.net",
    "doubleclick.net",
    "facebook.com",
    "force.com",
    "github.com",
    "google.com",
    "googleapis.com",
    "googletagmanager.com",
    "gstatic.com",
    "instagram.com",
    "linkedin.com",
    "meta.com",
    "microsoft.com",
    "pinterest.com",
    "salesforce.com",
    "schema.org",
    "shop.app",
    "shopify.com",
    "shopify.dev",
    "shopify.engineering",
    "shopifyacademy.com",
    "shopifycdn.com",
    "shopifycloud.com",
    "shopifyinvestors.com",
    "shopifyplus.com",
    "shopifystatus.com",
    "tiktok.com",
    "twitter.com",
    "w3.org",
    "x.com",
    "youtube.com",
    "youtu.be",
}
_GENERIC_LINK_LABELS = {
    "click here",
    "home",
    "learn more",
    "more",
    "official site",
    "read more",
    "visit",
    "visit site",
    "website",
}
_ENTITY_LINE_BLOCKLIST = {
    "about",
    "agency",
    "award",
    "awards",
    "brand",
    "brands",
    "case study",
    "client",
    "clients",
    "company",
    "contact",
    "customer",
    "customers",
    "directory",
    "finalists",
    "members",
    "partners",
    "portfolio",
    "privacy policy",
    "terms of service",
    "winners",
}
_PLATFORM_CUSTOMER_DETAIL_PATH_RE = re.compile(
    r"/(?:case-stud(?:y|ies)|customer-stor(?:y|ies)|success-stor(?:y|ies)|customers)/"
    r"(?P<slug>[^/?#]+)(?:/|$)",
    re.I,
)
_SUBJECT_IDENTITY_NOISE = {
    "a",
    "an",
    "and",
    "by",
    "case",
    "client",
    "company",
    "customer",
    "customers",
    "detail",
    "details",
    "for",
    "from",
    "how",
    "in",
    "learn",
    "more",
    "of",
    "official",
    "on",
    "profile",
    "record",
    "site",
    "stories",
    "story",
    "studies",
    "study",
    "success",
    "the",
    "to",
    "visit",
    "website",
    "with",
}
_SUBJECT_HEADING_VERBS = (
    "achieved",
    "accelerated",
    "built",
    "created",
    "delivered",
    "drove",
    "expanded",
    "grew",
    "increased",
    "launched",
    "scaled",
    "transformed",
    "uses",
    "used",
)
_KIND_RANK = {
    "agency_directory": 31.0,
    "brand_directory": 34.0,
    "brand_group_portfolio": 37.0,
    "platform_customer_list": 35.0,
    "repeatable_search": 29.0,
    "tenant_apex": 36.0,
}


class SourceDiscoveryLimits(BaseModel):
    max_results: int = Field(default=100, ge=1, le=500)
    max_pages: int = Field(default=20, ge=1, le=100)
    max_depth: int = Field(default=2, ge=0, le=4)
    max_bytes_per_page: int = Field(default=1_000_000, ge=10_000, le=5_000_000)
    max_pdfs: int = Field(default=4, ge=0, le=20)
    max_pdf_bytes: int = Field(default=5_000_000, ge=50_000, le=20_000_000)
    max_pdf_pages: int = Field(default=40, ge=1, le=200)
    max_tool_calls: int = Field(default=4, ge=1, le=10)
    max_microlink_pages: int = Field(
        default=SOURCE_MICROLINK_MAX_PAGES,
        ge=0,
        le=SOURCE_MICROLINK_MAX_PAGES,
    )


class SourceDiscoveryCounts(BaseModel):
    ct_calls: int = Field(default=0, ge=0)
    api_calls: int = Field(default=0, ge=0)
    html_pages: int = Field(default=0, ge=0)
    pdf_files: int = Field(default=0, ge=0)
    ct_candidates: int = Field(default=0, ge=0)
    direct_candidates: int = Field(default=0, ge=0)
    ai_candidates: int = Field(default=0, ge=0)
    total_candidates: int = Field(default=0, ge=0)
    microlink_attempted: int = Field(default=0, ge=0)
    microlink_recovered: int = Field(default=0, ge=0)


class SourceDiscoveryResult(BaseModel):
    source_key: str
    source_kind: SourceKind
    method: ExecutionMethod
    candidates: list[DiscoveredHost] = Field(default_factory=list)
    counts: SourceDiscoveryCounts = Field(default_factory=SourceDiscoveryCounts)
    tool_calls: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    coverage: dict = Field(default_factory=dict)


@dataclass(slots=True)
class PublicCrawlResult:
    candidates: list[DiscoveredHost] = field(default_factory=list)
    entity_hints: list[str] = field(default_factory=list)
    api_calls: int = 0
    html_pages: int = 0
    pdf_files: int = 0
    microlink_attempted: int = 0
    microlink_recovered: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    fetch_attempts: int = 0


def discovery_method_for(suggestion: SourceSuggestion) -> ExecutionMethod:
    if suggestion.source_kind == "tenant_apex":
        return "certificate_transparency"
    if suggestion.source_kind == "repeatable_search":
        return "repeatable_web_search"
    return "public_site_crawl"


def _canonical_public_host(value: str, *, allow_ip: bool) -> str | None:
    hostname = value.strip().lower().rstrip(".")
    if not hostname:
        return None
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    try:
        address = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        address = None
    if address is not None:
        return address.compressed if allow_ip and address.is_global else None
    if not _DNS_HOST_RE.fullmatch(hostname):
        return None
    if any(
        hostname == reserved or hostname.endswith(f".{reserved}")
        for reserved in _RESERVED_SOURCE_HOSTS
    ) or hostname.endswith(_RESERVED_SOURCE_SUFFIXES):
        return None
    extracted = _ICANN_TLD_EXTRACT(hostname)
    if not extracted.suffix or not extracted.domain:
        return None
    return hostname


def _validated_public_source_url(value: str) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value:
        return None, "Source URL is required"
    if value != value.strip() or _SOURCE_URL_ANNOTATION_RE.search(value):
        return None, "Source URL must be one plain URL without whitespace or annotations"
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
    except ValueError:
        return None, "Source URL must include a valid hostname"
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return None, "Source URL must start with http:// or https://"
    if not hostname:
        return None, "Source URL must include a valid hostname"
    if parsed.username is not None or parsed.password is not None:
        return None, "Source URL must not include credentials"
    try:
        port = parsed.port
    except ValueError:
        return None, "Source URL must use port 80 or 443"
    if port not in {None, 80, 443}:
        return None, "Source URL must use port 80 or 443"
    public_host = _canonical_public_host(hostname, allow_ip=True)
    if public_host is None:
        return None, "Source URL must use a public DNS hostname or global IP address"
    netloc_host = f"[{public_host}]" if ":" in public_host else public_host
    default_port = 443 if scheme == "https" else 80
    netloc = netloc_host if port in {None, default_port} else f"{netloc_host}:{port}"
    return (
        urlunparse(
            (
                scheme,
                netloc,
                parsed.path or "/",
                parsed.params,
                parsed.query,
                "",
            )
        ),
        None,
    )


def canonicalize_public_source_url(value: str) -> str | None:
    """Return a canonical public URL, repairing only one unambiguous embedded URL.

    Already-valid values take the strict path unchanged. Recovery is deliberately narrow:
    it never searches for a substitute and succeeds only when every HTTP(S) span in the
    malformed value is valid and identifies the same public URL.
    """

    canonical, _ = _validated_public_source_url(value)
    if canonical is not None:
        return canonical
    if not isinstance(value, str) or not value:
        return None

    candidates = {
        candidate
        for match in _EMBEDDED_SOURCE_URL_RE.finditer(value)
        if (candidate := _trim_embedded_source_url(match.group(0)))
    }
    if not candidates:
        return None

    recovered: set[str] = set()
    for candidate in candidates:
        normalized, _ = _validated_public_source_url(candidate)
        if normalized is None:
            return None
        recovered.add(normalized)
    return next(iter(recovered)) if len(recovered) == 1 else None


def _trim_embedded_source_url(value: str) -> str:
    """Remove prose punctuation around a URL without stripping balanced URL delimiters."""

    candidate = value.rstrip(".,;:!?")
    for opener, closer in (("(", ")"), ("[", "]"), ("{", "}")):
        while candidate.endswith(closer) and candidate.count(closer) > candidate.count(opener):
            candidate = candidate[:-1]
    return candidate


def _validated_source_apex(value: str) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value:
        return None, "Tenant-apex sources require an apex domain"
    if (
        value != value.strip()
        or _SOURCE_URL_ANNOTATION_RE.search(value)
        or "://" in value
        or any(character in value for character in "/:@?#")
    ):
        return (
            None,
            "Source apex must be a plain hostname without a scheme, path, port, or annotations",
        )
    public_host = _canonical_public_host(value, allow_ip=False)
    if public_host is None:
        return None, "Source apex must be a public DNS hostname"
    return public_host, None


def source_execution_blockers(suggestion: SourceSuggestion) -> list[str]:
    """Return deterministic reasons an accepted suggestion cannot safely execute."""

    blockers: list[str] = []
    if is_disallowed(suggestion.url) or (suggestion.apex and is_disallowed(suggestion.apex)):
        blockers.append("Source domain is disallowed")
    if canonicalize_public_source_url(suggestion.url) is None:
        _, url_blocker = _validated_public_source_url(suggestion.url)
        if url_blocker:
            blockers.append(url_blocker)
    if suggestion.source_kind == "tenant_apex" and not suggestion.apex:
        blockers.append("Tenant-apex sources require an apex domain")
    elif suggestion.apex:
        _, apex_blocker = _validated_source_apex(suggestion.apex)
        if apex_blocker:
            blockers.append(apex_blocker)
    return blockers


def _source_key(suggestion: SourceSuggestion) -> str:
    return suggestion.source_key or suggestion.apex or suggestion.url.rstrip("/").lower()


def _source_identifier(suggestion: SourceSuggestion) -> str:
    if suggestion.apex:
        return suggestion.apex.strip().lower().rstrip(".")
    host = (urlparse(suggestion.url).hostname or "").lower().removeprefix("www.")
    return host or "public-source"


def _origin(url: str) -> tuple[str, str, int | None] | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.lower().rstrip("."), effective_port


def _clean_crawl_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.params,
            parsed.query,
            "",
        )
    )


def _is_asset_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(suffix) for suffix in _ASSET_SUFFIXES)


def _registrable_domain(value: str) -> str | None:
    host = canonicalize_public_website(value)
    if host is None:
        return None
    extracted = _TLD_EXTRACT(host)
    if not extracted.suffix or not extracted.domain:
        return None
    return f"{extracted.domain}.{extracted.suffix}".lower()


def _registrable_label(value: str) -> str | None:
    host = canonicalize_public_website(value)
    if host is None:
        return None
    extracted = _TLD_EXTRACT(host)
    if not extracted.suffix or not extracted.domain:
        return None
    return extracted.domain.lower()


def _source_domains(suggestion: SourceSuggestion, page_url: str = "") -> set[str]:
    domains: set[str] = set()
    for value in (suggestion.apex or "", suggestion.url, page_url):
        domain = _registrable_domain(value)
        if domain:
            domains.add(domain)
    return domains


def _source_provider_labels(suggestion: SourceSuggestion) -> set[str]:
    return {
        label
        for value in (suggestion.apex or "", suggestion.url)
        if (label := _registrable_label(value))
    }


def _is_blocked_candidate(
    raw_url: str,
    domain: str,
    source_domains: set[str],
    source_provider_labels: set[str],
) -> bool:
    parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host:
        return True
    first_label = host.split(".", 1)[0]
    if first_label in _ASSET_HOST_LABELS or _is_asset_url(raw_url):
        return True
    domain_label = _registrable_label(domain)
    if domain_label and domain_label in source_provider_labels:
        return True
    if domain in source_domains or domain in _BLOCKED_PROVIDER_DOMAINS:
        return True
    return any(
        domain == blocked or domain.endswith(f".{blocked}") for blocked in _BLOCKED_PROVIDER_DOMAINS
    )


def _clean_label(value: str, fallback: str) -> str:
    label = re.sub(r"\s+", " ", value).strip(" \t\r\n-|·:;")
    if not label or len(label) < 2 or len(label) > 160 or label.casefold() in _GENERIC_LINK_LABELS:
        return fallback
    return label


def _identity_tokens(value: str) -> tuple[str, ...]:
    return tuple(
        token
        for token in re.findall(r"[a-z0-9]+", unquote(value).casefold())
        if token not in _SUBJECT_IDENTITY_NOISE
    )


def _identity_compact(value: str) -> str:
    return "".join(_identity_tokens(value))


def _heading_subject(value: str) -> str:
    """Extract a conservative subject name from a customer-record heading."""

    heading = re.sub(r"\s+", " ", value).strip(" \t\r\n-|:;")
    if not heading:
        return ""
    heading = re.split(r"\s(?:\||—|–|-)\s", heading, maxsplit=1)[0].strip()
    generic_prefix = re.match(
        r"^(?:case stud(?:y|ies)|customer stor(?:y|ies)|success stor(?:y|ies))\s*[:|-]?\s*",
        heading,
        re.I,
    )
    if generic_prefix:
        heading = heading[generic_prefix.end() :].strip()
    if heading.casefold().startswith("how "):
        heading = heading[4:].strip()
    verb_pattern = "|".join(_SUBJECT_HEADING_VERBS)
    verb = re.search(rf"\s+(?:{verb_pattern})\b", heading, re.I)
    if verb:
        heading = heading[: verb.start()].strip()
    if ":" in heading:
        before, after = (part.strip() for part in heading.split(":", 1))
        if _identity_tokens(before):
            heading = before
        elif _identity_tokens(after):
            heading = after
    heading = re.sub(
        r"\s+(?:case stud(?:y|ies)|customer stor(?:y|ies)|success stor(?:y|ies))$",
        "",
        heading,
        flags=re.I,
    ).strip()
    return heading if 2 <= len(heading) <= 100 else ""


def _customer_record_subjects(
    suggestion: SourceSuggestion,
    page_url: str,
    soup: BeautifulSoup,
) -> tuple[str, ...]:
    """Return precise subjects for customer details, including legacy brand-directory rows."""

    if suggestion.source_kind not in {"platform_customer_list", "brand_directory"}:
        return ()
    match = _PLATFORM_CUSTOMER_DETAIL_PATH_RE.search(unquote(urlparse(page_url).path))
    if match is None:
        return ()
    raw_slug = match.group("slug").replace("-", " ").replace("_", " ")
    subjects: list[str] = []
    slug_tokens = _identity_tokens(raw_slug)
    if any(any(character.isalpha() for character in token) for token in slug_tokens):
        subjects.append(raw_slug)
    else:
        headings = [heading.get_text(" ", strip=True) for heading in soup.find_all("h1", limit=3)]
        if soup.title:
            headings.append(soup.title.get_text(" ", strip=True))
        subjects.extend(
            subject
            for value in headings
            if (subject := _heading_subject(value)) and _identity_compact(subject)
        )
    return tuple(dict.fromkeys(subjects))


def _matches_customer_record_subject(
    raw_url: str,
    label: str,
    subjects: tuple[str, ...],
) -> bool:
    """Require a detail-page outbound identity to match its named customer."""

    if not subjects:
        return True
    domain_label = _registrable_label(raw_url) or ""
    domain_compact = _identity_compact(domain_label)
    label_tokens = set(_identity_tokens(label))
    label_compact = "".join(_identity_tokens(label))
    for subject in subjects:
        subject_tokens = set(_identity_tokens(subject))
        subject_compact = "".join(_identity_tokens(subject))
        if not subject_compact:
            continue
        if domain_compact == subject_compact or label_compact == subject_compact:
            return True
        if min(len(domain_compact), len(subject_compact)) >= 4 and (
            domain_compact in subject_compact or subject_compact in domain_compact
        ):
            return True
        shorter_label = min(len(label_compact), len(subject_compact))
        longer_label = max(len(label_compact), len(subject_compact))
        if (
            shorter_label >= 4
            and shorter_label / max(longer_label, 1) >= 0.6
            and (label_compact in subject_compact or subject_compact in label_compact)
        ):
            return True
        shared_label_tokens = label_tokens & subject_tokens
        if (
            shared_label_tokens
            and label_tokens <= subject_tokens
            and any(len(token) >= 4 for token in shared_label_tokens)
        ):
            return True
    return False


def _candidate_from_url(
    raw_url: str,
    *,
    suggestion: SourceSuggestion,
    evidence_url: str,
    label: str = "",
    rank_bonus: float = 0.0,
    assertion: DiscoveryAssertionEvidence | None = None,
) -> DiscoveredHost | None:
    value = unquote(raw_url.strip().strip("<>()[]{}.,;:'\""))
    if value.startswith("www.") or "://" not in value:
        value = f"https://{value}"
    domain = _registrable_domain(value)
    website_host = canonicalize_public_website(value)
    if domain is None or website_host is None:
        return None
    source_domains = _source_domains(suggestion, evidence_url)
    if _is_blocked_candidate(
        value,
        domain,
        source_domains,
        _source_provider_labels(suggestion),
    ):
        return None
    return DiscoveredHost(
        source_apex=_source_identifier(suggestion),
        # An observed business subdomain is stronger identity evidence than its presumed
        # parent. Hydrate that actual site; verified redirects/canonical aliases dedupe later.
        hostname=website_host,
        discovery_rank=round(_KIND_RANK[suggestion.source_kind] + rank_bonus, 3),
        label_hint=_clean_label(label, domain),
        discovery_evidence_urls=[evidence_url],
        discovery_assertions=[assertion] if assertion else [],
    )


def _merge_candidates(candidates: list[DiscoveredHost], limit: int) -> list[DiscoveredHost]:
    merged: dict[str, DiscoveredHost] = {}
    for candidate in candidates:
        current = merged.get(candidate.hostname)
        if current is None:
            merged[candidate.hostname] = candidate
            continue
        winner = candidate if candidate.discovery_rank > current.discovery_rank else current
        merged[candidate.hostname] = winner.model_copy(
            update={
                "discovery_evidence_urls": list(
                    dict.fromkeys(
                        [*current.discovery_evidence_urls, *candidate.discovery_evidence_urls]
                    )
                )[:20],
                "provider_record_ids": list(
                    dict.fromkeys([*current.provider_record_ids, *candidate.provider_record_ids])
                )[:20],
                "discovery_assertions": list(
                    {
                        item.context_hash or f"{item.relation_type}:{item.source_url}": item
                        for item in [
                            *current.discovery_assertions,
                            *candidate.discovery_assertions,
                        ]
                    }.values()
                )[:30],
            }
        )
    return sorted(
        merged.values(),
        key=lambda item: (-item.discovery_rank, item.hostname),
    )[:limit]


def _jsonld_urls(value: object, *, depth: int = 0) -> list[tuple[str, str]]:
    if depth > 12:
        return []
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        name = str(value.get("name") or value.get("headline") or "")
        for key, child in value.items():
            lowered = str(key).casefold()
            if lowered in {"url", "sameas", "website", "mainentityofpage"}:
                values = (
                    [child] if isinstance(child, str) else child if isinstance(child, list) else []
                )
                for raw in values[:25]:
                    if isinstance(raw, str):
                        found.append((raw, name))
            found.extend(_jsonld_urls(child, depth=depth + 1))
    elif isinstance(value, list):
        for child in value[:500]:
            found.extend(_jsonld_urls(child, depth=depth + 1))
    return found


def _visible_urls(text: str, *, include_bare_domains: bool = True) -> list[str]:
    found = [match.group(0) for match in _VISIBLE_URL_RE.finditer(text)]
    if include_bare_domains:
        for match in _BARE_DOMAIN_RE.finditer(text):
            token = match.group(0)
            # These real suffixes also form prose fragments and relative action
            # filenames. Require website intent for bare text, not for explicit URLs.
            if token.rsplit(".", 1)[-1].casefold() in {"here", "to", "as", "do"}:
                line_start = text.rfind("\n", 0, match.start()) + 1
                prefix = text[line_start:match.start()]
                # HTML text extraction often splits a website label and its value
                # across lines. Retain that cue without treating arbitrary prose as one.
                if not prefix.strip() and line_start:
                    previous_start = text.rfind("\n", 0, line_start - 1) + 1
                    prefix = text[previous_start:line_start - 1]
                if not _BARE_WEBSITE_INTENT_RE.search(prefix):
                    continue
            found.append(token)
    return list(dict.fromkeys(found))


def _entity_hints(text: str) -> list[str]:
    hints: list[str] = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" \t\r\n|•·-:;")
        lowered = line.casefold()
        if (
            not 2 <= len(line) <= 100
            or lowered in _ENTITY_LINE_BLOCKLIST
            or line.count(" ") > 10
            or "http" in lowered
            or "www." in lowered
            or not any(char.isalpha() for char in line)
        ):
            continue
        words = [word for word in re.split(r"\s+", line) if word]
        titleish = sum(word[:1].isupper() for word in words) >= max(1, len(words) // 2)
        if titleish or line.isupper():
            hints.append(line)
    return list(dict.fromkeys(hints))[:100]


def _crawl_link_priority(url: str) -> int:
    """Favor source records over navigation when a crawl has a small page budget."""

    path = unquote(urlparse(url).path).casefold().rstrip("/")
    if path.endswith(".xml"):
        return 20
    for marker in (
        "/case-studies",
        "/case-study",
        "/customer-stories",
        "/customer-story",
        "/success-stories",
        "/success-story",
        "/customers",
        "/portfolio",
        "/our-work",
        "/clients",
        "/members",
        "/winners",
    ):
        position = path.find(marker)
        if position < 0:
            continue
        remainder = path[position + len(marker) :].strip("/")
        return 0 if remainder else 10
    if "/sitemap" in path:
        return 20
    return 100


def _sitemap_links(body: bytes, *, page_url: str) -> list[str]:
    text = body.decode("utf-8", errors="replace")
    links: list[str] = []
    for match in re.finditer(r"<loc\b[^>]*>\s*([^<]+?)\s*</loc>", text, re.I):
        try:
            absolute = _clean_crawl_url(urljoin(page_url, html_unescape(match.group(1))))
            parsed = urlparse(absolute)
        except ValueError:
            continue
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and _origin(absolute) == _origin(page_url)
            and (parsed.path.casefold().endswith(".xml") or not _is_asset_url(absolute))
        ):
            links.append(absolute)
    return list(dict.fromkeys(links))


def _crawl_seed_urls(suggestion: SourceSuggestion) -> list[str]:
    seeds: list[str] = []
    if (
        suggestion.source_kind == "tenant_apex"
        and (suggestion.apex or "").strip().casefold().rstrip(".") == "myshopify.com"
    ):
        seeds.extend(SHOPIFY_CASE_STUDY_SEEDS)
    seeds.append(suggestion.url)
    return list(dict.fromkeys(_clean_crawl_url(url) for url in seeds))


_DISCOVERY_RELATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("testimonial", re.compile(r"\b(?:testimonial|customer quote|what .{0,30} say)\b", re.I)),
    ("case_study", re.compile(r"\b(?:case stud(?:y|ies)|customer stor(?:y|ies))\b", re.I)),
    ("customer", re.compile(r"\b(?:customers?|clients?|trusted by|used by)\b", re.I)),
    ("partner", re.compile(r"\b(?:partners?|integrations?|certified)\b", re.I)),
    ("award", re.compile(r"\b(?:awards?|winners?|finalists?)\b", re.I)),
    ("member", re.compile(r"\b(?:members?|membership)\b", re.I)),
    ("sponsor", re.compile(r"\b(?:sponsors?|sponsored by)\b", re.I)),
    ("exhibitor", re.compile(r"\b(?:exhibitors?|exhibiting)\b", re.I)),
    ("portfolio", re.compile(r"\b(?:portfolio|our work|selected work|campaigns?)\b", re.I)),
)


def _explicit_page_date(soup: BeautifulSoup) -> datetime | None:
    values: list[str] = []
    for node in soup.find_all("meta", limit=200):
        marker = " ".join(
            str(node.get(attribute) or "") for attribute in ("property", "name", "itemprop")
        ).casefold()
        if any(token in marker for token in ("published", "datecreated", "datepublished")):
            values.append(str(node.get("content") or ""))
    for node in soup.find_all("time", limit=50):
        values.append(str(node.get("datetime") or ""))
    for value in values:
        normalized = value.strip().replace("Z", "+00:00")
        if not normalized:
            continue
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(normalized[:10])
            except ValueError:
                continue
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    return None


def _anchor_assertion(
    node: Tag,
    *,
    page_url: str,
    page_markers: str,
    published_at: datetime | None,
) -> DiscoveryAssertionEvidence:
    anchor_text, asset_url = _anchor_identity_metadata(node, page_url=page_url)
    container = node.find_parent(
        ["p", "li", "figure", "blockquote", "tr", "div", "section", "article"]
    )
    context = ""
    if container is not None:
        candidate_context = re.sub(r"\s+", " ", container.get_text(" ", strip=True)).strip()
        if len(candidate_context) <= 1_500:
            context = candidate_context
    if not context:
        context = anchor_text
    elif anchor_text and anchor_text.casefold() not in context.casefold():
        context = f"{context} · {anchor_text}"
    local_markers = " ".join(
        (
            " ".join(str(value) for value in node.get("class", [])),
            str(node.get("aria-label") or ""),
            context,
        )
    )

    def relation_from(markers: str) -> str:
        for candidate_type, pattern in _DISCOVERY_RELATION_PATTERNS:
            if pattern.search(markers):
                return candidate_type
        return "unknown"

    # A nearby card, list item, or paragraph describes this link more precisely than the
    # enclosing page. This prevents an integration link on a case-study page from inheriting
    # the page's customer relationship.
    relation_type = relation_from(local_markers)
    if relation_type == "unknown":
        relation_type = relation_from(page_markers)
    excerpt = context[:1_000]
    normalized = re.sub(r"\s+", " ", excerpt.casefold()).strip()
    publisher = (urlparse(page_url).hostname or "").removeprefix("www.")
    return DiscoveryAssertionEvidence(
        publisher_domain=publisher,
        relation_type=relation_type,
        source_url=page_url,
        context_excerpt=excerpt,
        context_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        published_at=published_at,
        confidence="high" if relation_type != "unknown" and len(context) >= 8 else "low",
        current_relationship_unverified=True,
        evidence_kind="linked_logo" if asset_url else "anchor",
        asset_url=asset_url,
    )


def _anchor_identity_metadata(node: Tag, *, page_url: str) -> tuple[str, str]:
    """Recover the identity carried by a linked logo without fetching its asset."""

    visible = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
    image = node.find(["img", "image"])
    if image is None:
        return visible, ""
    assets = _iter_image_asset_urls(image, page_url=page_url)
    asset_url = assets[0] if assets else ""
    if visible:
        return visible, asset_url
    for attribute in ("alt", "aria-label", "title"):
        label = re.sub(r"\s+", " ", str(image.get(attribute) or "")).strip()
        if label:
            return label, asset_url
    return (_filename_identity_hint(asset_url) if asset_url else ""), asset_url


def _html_clues(
    body: bytes,
    *,
    page_url: str,
    suggestion: SourceSuggestion,
) -> tuple[list[DiscoveredHost], list[str], list[str], list[str]]:
    text = body.decode("utf-8", errors="replace")
    soup = BeautifulSoup(text, "html.parser")
    record_subjects = _customer_record_subjects(suggestion, page_url, soup)
    page_markers = " ".join(
        (
            urlparse(page_url).path.replace("-", " ").replace("_", " "),
            soup.title.get_text(" ", strip=True) if soup.title else "",
            " ".join(
                heading.get_text(" ", strip=True)
                for heading in soup.find_all(["h1", "h2"], limit=8)
            ),
        )
    )
    published_at = _explicit_page_date(soup)
    candidates: list[DiscoveredHost] = []
    crawl_links: list[str] = []
    pdf_links: list[str] = []
    priority_hints: list[str] = []
    logo_hints = [
        appearance.subject_name
        for appearance in _extract_company_appearances(
            soup,
            page_url=page_url,
            publisher_domain=(urlparse(page_url).hostname or "").removeprefix("www."),
        )
        if appearance.subject_name
    ]

    for node in soup.find_all("a", href=True):
        if node.find_parent(["header", "nav", "footer", "aside"]) is not None:
            continue
        raw_href = str(node.get("href") or "").strip()
        if not raw_href or raw_href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        try:
            absolute = _clean_crawl_url(urljoin(page_url, raw_href))
            parsed = urlparse(absolute)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        if parsed.path.lower().endswith(".pdf"):
            pdf_links.append(absolute)
        elif _origin(absolute) == _origin(page_url) and not _is_asset_url(absolute):
            crawl_links.append(absolute)
            if _crawl_link_priority(absolute) == 0:
                hint = _clean_label(
                    _anchor_identity_metadata(node, page_url=page_url)[0],
                    "",
                )
                if hint:
                    priority_hints.append(hint)
        elif _origin(absolute) != _origin(page_url):
            label = _anchor_identity_metadata(node, page_url=page_url)[0]
            if _matches_customer_record_subject(absolute, label, record_subjects):
                candidate = _candidate_from_url(
                    absolute,
                    suggestion=suggestion,
                    evidence_url=page_url,
                    label=label,
                    rank_bonus=2.0,
                    assertion=_anchor_assertion(
                        node, page_url=page_url, page_markers=page_markers,
                        published_at=published_at,
                    ),
                )
                if candidate:
                    candidates.append(candidate)
        for embedded in _visible_urls(unquote(raw_href), include_bare_domains=False):
            label = _anchor_identity_metadata(node, page_url=page_url)[0]
            if not _matches_customer_record_subject(embedded, label, record_subjects):
                continue
            candidate = _candidate_from_url(
                embedded,
                suggestion=suggestion,
                evidence_url=page_url,
                label=label,
                rank_bonus=1.0,
                assertion=_anchor_assertion(
                    node, page_url=page_url, page_markers=page_markers, published_at=published_at,
                ),
            )
            if candidate:
                candidates.append(candidate)

    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            payload = json.loads(script.string or script.get_text() or "null")
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        for raw_url, name in _jsonld_urls(payload):
            if not _matches_customer_record_subject(raw_url, name, record_subjects):
                continue
            candidate = _candidate_from_url(
                raw_url,
                suggestion=suggestion,
                evidence_url=page_url,
                label=name,
                rank_bonus=4.0,
            )
            if candidate:
                candidates.append(candidate)

    crawl_links.extend(_sitemap_links(body, page_url=page_url))
    visible_text = soup.get_text("\n", strip=True)
    # Code samples contain filenames and member expressions that look like real
    # domains (setup.py, subscription.id). Keep explicit URLs in those samples,
    # but require prose context for an unqualified bare website identity.
    prose_soup = BeautifulSoup(body, "html.parser")
    for code_node in prose_soup.find_all(["code", "pre", "script", "style"]):
        code_node.decompose()
    visible_urls = _visible_urls(visible_text, include_bare_domains=False)
    visible_urls.extend(_visible_urls(prose_soup.get_text("\n", strip=True)))
    for raw_url in dict.fromkeys(visible_urls):
        if not _matches_customer_record_subject(raw_url, "", record_subjects):
            continue
        candidate = _candidate_from_url(
            raw_url,
            suggestion=suggestion,
            evidence_url=page_url,
        )
        if candidate:
            candidates.append(candidate)
    return (
        candidates,
        sorted(dict.fromkeys(crawl_links), key=_crawl_link_priority),
        list(dict.fromkeys(pdf_links)),
        list(
            dict.fromkeys(
                [*priority_hints, *logo_hints, *_entity_hints(visible_text)]
            )
        )[:100],
    )


def _pdf_annotation_urls(page: object) -> list[str]:
    urls: list[str] = []
    try:
        annotations = page.get("/Annots", [])
    except (AttributeError, TypeError, ValueError):
        return urls
    for reference in annotations or []:
        try:
            annotation = reference.get_object()
            action = annotation.get("/A", {})
            value = action.get("/URI") if action else None
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if value:
            urls.append(str(value))
    return urls


def _extract_pdf_clues(data: bytes, *, max_pages: int) -> tuple[list[str], list[str]]:
    reader = PdfReader(BytesIO(data), strict=False)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ValueError("PDF is encrypted") from exc
    text_parts: list[str] = []
    urls: list[str] = []
    total_characters = 0
    for page in reader.pages[:max_pages]:
        page_text = str(page.extract_text() or "")
        remaining = 300_000 - total_characters
        if remaining <= 0:
            break
        page_text = page_text[:remaining]
        total_characters += len(page_text)
        text_parts.append(page_text)
        urls.extend(_pdf_annotation_urls(page))
    text = "\n".join(text_parts)
    urls.extend(_visible_urls(text))
    return list(dict.fromkeys(urls)), _entity_hints(text)


def _is_google_partners_source(suggestion: SourceSuggestion) -> bool:
    return (urlparse(suggestion.url).hostname or "").strip().casefold().rstrip(
        "."
    ) == GOOGLE_PARTNERS_HOST


def _google_partner_evidence_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        url = urljoin(f"https://{GOOGLE_PARTNERS_HOST}/", value.strip())
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return _clean_crawl_url(url)


async def _cache_receipt(cache, operation, *args):
    # Real receipts use owned off-loop I/O; lightweight legacy/test caches stay compatible.
    asynchronous = getattr(cache, operation + "_async", None)
    if asynchronous is not None:
        return await asynchronous(*args)
    return getattr(cache, operation)(*args)


async def _google_partners_candidates(
    client: httpx.AsyncClient, suggestion: SourceSuggestion, *,
    limits: SourceDiscoveryLimits, page_cache=None,
) -> tuple[list[DiscoveredHost], list[str], list[str]]:
    candidates = []
    offset = 0
    signatures = set()
    calls = 0
    while True:
        page_size = min(100, limits.max_results) if page_cache else limits.max_results
        api_url = f"{GOOGLE_PARTNERS_API_URL}?limit={page_size}&offset={offset}"
        cached = await _cache_receipt(page_cache, "get", api_url, "api") if page_cache else None
        if cached is not None:
            rows = cached["rows"]
            signature = cached["signature"]
        else:
            if calls >= limits.max_pages:
                if page_cache:
                    page_cache.api_pending = True
                break
            try:
                response, body = await _bounded_get(
                    client, api_url, max_bytes=limits.max_bytes_per_page,
                    accept="application/json,*/*;q=0.1", allowed_content_types=("json",),
                )
                calls += 1
                if page_cache:
                    page_cache.api_calls += 1
                if response.status_code >= 400:
                    return candidates, [f"{api_url}: HTTP {response.status_code}"], []
                payload = json.loads(body)
                raw_rows = payload.get("data", []) if isinstance(payload, dict) else None
                if not isinstance(raw_rows, list):
                    raise ValueError("JSON data must be a list")
                rows = [{key: row.get(key) for key in (
                    "company_website_url", "company_profile_url", "company_name",
                    "company_profile_agency_id",
                )} if isinstance(row, dict) else {} for row in raw_rows]
                signature = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
                if page_cache:
                    await _cache_receipt(page_cache, "put", api_url, "api", {
                        "rows": rows, "signature": signature,
                    })
            except (httpx.HTTPError, OSError, UnsafeTargetError, ValueError) as exc:
                return candidates, [], [f"{api_url}: {type(exc).__name__}: {exc}"[:2_000]]
        if not rows:
            if page_cache:
                page_cache.api_complete = True
            break  # Only explicit empty-page exhaustion, never a short page.
        if signature in signatures:
            return candidates, [], ["Google Partners repeated a pagination slice; coverage unknown"]
        signatures.add(signature)
        for row in rows:
            website = row.get("company_website_url")
            evidence_url = _google_partner_evidence_url(row.get("company_profile_url"))
            if not isinstance(website, str) or evidence_url is None:
                continue
            candidate = _candidate_from_url(
                website, suggestion=suggestion, evidence_url=evidence_url,
                label=str(row.get("company_name") or ""), rank_bonus=5.0,
            )
            if candidate is None or (page_cache
                                      and candidate.hostname in page_cache.known_hosts):
                continue
            agency_id = str(row.get("company_profile_agency_id") or "").strip()
            if agency_id:
                candidate = candidate.model_copy(
                    update={"provider_record_ids": [f"google-partners:{agency_id}"]})
            candidates.append(candidate)
        offset += len(rows)
        if not page_cache:
            break  # Backward-compatible bounded validation, no completeness assertion.
        if len(_merge_candidates(candidates, limits.max_results + 1)) >= limits.max_results:
            page_cache.api_pending = True
            break
    return _merge_candidates(candidates, limits.max_results), [], []


async def crawl_public_source(
    client: httpx.AsyncClient,
    suggestion: SourceSuggestion,
    *,
    limits: SourceDiscoveryLimits | None = None,
    microlink_api_key: str = "",
    page_cache=None,
    progress=None,
) -> PublicCrawlResult:
    """Crawl one public source origin with strict request, depth, size, and PDF bounds."""

    normalized_url = canonicalize_public_source_url(suggestion.url)
    if normalized_url is not None and normalized_url != suggestion.url:
        suggestion = suggestion.model_copy(update={"url": normalized_url})
    bounds = limits or SourceDiscoveryLimits()
    result = PublicCrawlResult()
    page_yield = CrawlYieldMonitor()
    yield_stop: str | None = None
    blockers = source_execution_blockers(suggestion)
    if blockers:
        result.errors.extend(blockers)
        return result
    if _is_google_partners_source(suggestion):
        result.api_calls = 1
        api_candidates, api_warnings, api_errors = await _google_partners_candidates(
            client,
            suggestion,
            limits=bounds,
            page_cache=page_cache,
        )
        result.api_calls = page_cache.api_calls if page_cache else 1
        result.candidates.extend(api_candidates)
        result.warnings.extend(api_warnings)
        result.errors.extend(api_errors)
        if page_cache and not api_errors and not api_warnings:
            result.coverage = {
                "scope": "google_partner_api", "complete": page_cache.api_complete,
                "continuation": page_cache.api_pending, "pages_reused": page_cache.pages_reused,
            }
            return result

    queue: list[tuple[int, int, int, str]] = []
    queued: set[str] = set()
    queue_order = 0
    for seed_index, seed_url in enumerate(_crawl_seed_urls(suggestion)):
        queued.add(seed_url)
        heapq.heappush(
            queue,
            (-100 if seed_index == 0 else 50 + seed_index, 0, queue_order, seed_url),
        )
        queue_order += 1
    seen: set[str] = set()
    pdf_queue: deque[str] = deque()
    seen_pdfs: set[str] = set()
    crawl_origin: tuple[str, str, int | None] | None = None
    scope_incomplete = False
    consecutive_timeouts = 0
    timeout_break = False
    rate_limit_retry_after: datetime | None = None

    def report(url):
        if progress is not None:
            progress({"url": url, "fetch_attempts": result.fetch_attempts,
                      "pages_fetched": result.html_pages, "documents_fetched": result.pdf_files,
                      "pages_reused": page_cache.pages_reused if page_cache else 0,
                      "pages_pending": len(queue) + len(pdf_queue)})

    def follow_links(links, pdf_links, depth):
        nonlocal queue_order, scope_incomplete
        for pdf_url in pdf_links:
            if pdf_url not in seen_pdfs and (
                crawl_origin is None or _origin(pdf_url) == crawl_origin
            ):
                if len(pdf_queue) < bounds.max_pdfs:
                    pdf_queue.append(pdf_url)
                    seen_pdfs.add(pdf_url)
                else:
                    scope_incomplete = True
        for link in links:
            if crawl_origin is not None and _origin(link) != crawl_origin:
                continue
            if link in queued:
                continue
            if depth >= bounds.max_depth or len(queued) >= bounds.max_pages * 10:
                scope_incomplete = True
                continue
            queued.add(link)
            heapq.heappush(queue, (_crawl_link_priority(link), depth + 1, queue_order, link))
            queue_order += 1

    async def rendered_html(url: str) -> tuple[str, bytes] | None:
        if (
            not microlink_api_key.strip()
            or result.microlink_attempted >= bounds.max_microlink_pages
        ):
            return None
        result.microlink_attempted += 1
        rendered_url, rendered_body, _proxy_used = await fetch_microlink_rendered_html(
            client,
            url,
            api_key=microlink_api_key.strip(),
            max_bytes=bounds.max_bytes_per_page,
        )
        return rendered_url, rendered_body

    while queue and result.fetch_attempts < bounds.max_pages:
        if consecutive_timeouts >= 3:
            timeout_break = True
            result.errors.append(
                "Source paused after three consecutive direct timeouts; remaining pages stay due"
            )
            break
        _, depth, _, url = heapq.heappop(queue)
        if url in seen:
            continue
        if crawl_origin is not None and _origin(url) != crawl_origin:
            continue
        seen.add(url)
        cached_pdf = await _cache_receipt(page_cache, "get", url, "pdf") if page_cache else None
        if cached_pdf is not None:
            scope_incomplete = True
            eligible = page_cache.candidates(cached_pdf["candidates"])
            page_yield.observe((item.hostname for item in eligible), fresh=False)
            result.candidates.extend(eligible)
            result.entity_hints.extend(cached_pdf.get("entity_hints", []))
            report(url)
            if len(_merge_candidates(
                result.candidates, bounds.max_results + 1
            )) >= bounds.max_results:
                break
            continue
        cached = await _cache_receipt(page_cache, "get", url, "html") if page_cache else None
        if cached is not None:
            if crawl_origin is None:
                crawl_origin = _origin(cached["final_url"])
            eligible = page_cache.candidates(cached["candidates"])
            page_yield.observe((item.hostname for item in eligible), fresh=False)
            result.candidates.extend(eligible)
            result.entity_hints.extend(cached.get("entity_hints", []))
            scope_incomplete = scope_incomplete or cached.get("dynamic_scope", False)
            follow_links(cached["links"], cached["pdf_links"], depth)
            report(url)
            if len(_merge_candidates(
                result.candidates, bounds.max_results + 1
            )) >= bounds.max_results:
                break
            continue
        response: httpx.Response | None = None
        body = b""
        direct_error: BaseException | None = None
        used_rendered_html = False
        result.fetch_attempts += 1
        report(url)
        try:
            response, body = await _bounded_get(
                client,
                url,
                max_bytes=bounds.max_bytes_per_page,
                allowed_content_types=("html", "text/plain", "xml", "pdf"),
            )
            consecutive_timeouts = 0
        except (httpx.HTTPError, OSError, UnsafeTargetError, ValueError) as exc:
            direct_error = exc
            consecutive_timeouts = (
                consecutive_timeouts + 1 if isinstance(exc, httpx.TimeoutException) else 0
            )
            if microlink_fallback_eligible(None, exc):
                try:
                    recovered = await rendered_html(url)
                except (httpx.HTTPError, UnsafeTargetError, TimeoutError, ValueError) as fallback:
                    result.errors.append(
                        f"{url}: {type(exc).__name__}: {exc}; "
                        f"Microlink fallback failed: {type(fallback).__name__}: {fallback}"[:2_000]
                    )
                    continue
                if recovered is not None:
                    consecutive_timeouts = 0
                    final_url, body = recovered
                    used_rendered_html = True
                    result.microlink_recovered += 1
                    result.warnings.append(
                        f"{url}: direct retrieval failed; Microlink recovered rendered HTML"
                    )
                else:
                    result.errors.append(f"{url}: {type(exc).__name__}: {exc}"[:2_000])
                    continue
            else:
                result.errors.append(f"{url}: {type(exc).__name__}: {exc}"[:2_000])
                continue
        if response is not None and response.status_code >= 400:
            direct_status = response.status_code
            if direct_status == 429:
                now = datetime.now(UTC)
                retry_at = now + timedelta(minutes=15)
                retry_header = response.headers.get("retry-after", "").strip()
                if retry_header:
                    try:
                        requested = (now + timedelta(seconds=int(retry_header))
                                     if retry_header.isdigit()
                                     else parsedate_to_datetime(retry_header).astimezone(UTC))
                        retry_at = max(retry_at, requested)
                    except (ValueError, TypeError, OverflowError):
                        pass
                rate_limit_retry_after = max(rate_limit_retry_after or retry_at, retry_at)
            if microlink_fallback_eligible(response, None):
                try:
                    recovered = await rendered_html(str(response.url))
                except (httpx.HTTPError, UnsafeTargetError, TimeoutError, ValueError) as fallback:
                    result.warnings.append(
                        f"{url}: HTTP {direct_status}; Microlink fallback failed: "
                        f"{type(fallback).__name__}: {fallback}"[:2_000]
                    )
                    continue
                if recovered is not None:
                    final_url, body = recovered
                    used_rendered_html = True
                    result.microlink_recovered += 1
                    result.warnings.append(
                        f"{url}: HTTP {direct_status}; Microlink recovered rendered HTML"
                    )
                else:
                    result.warnings.append(f"{url}: HTTP {direct_status}")
                    continue
            else:
                result.warnings.append(f"{url}: HTTP {direct_status}")
                continue
        elif response is not None:
            final_url = _clean_crawl_url(str(response.url))
        if crawl_origin is None:
            crawl_origin = _origin(final_url)
        content_type = (
            "text/html"
            if used_rendered_html
            else response.headers.get("content-type", "").casefold()
            if response is not None and direct_error is None
            else "text/html"
        )
        if "pdf" in content_type or urlparse(final_url).path.lower().endswith(".pdf"):
            if final_url not in seen_pdfs:
                pdf_queue.append(final_url)
            continue
        candidates, links, pdf_links, hints = await asyncio.to_thread(
            _html_clues,
            body,
            page_url=final_url,
            suggestion=suggestion,
        )
        useful_clues = bool(candidates or links or pdf_links or hints)
        if (
            not useful_clues
            and response is not None
            and direct_error is None
            and not used_rendered_html
        ):
            try:
                recovered = await rendered_html(str(response.url))
            except (httpx.HTTPError, UnsafeTargetError, TimeoutError, ValueError) as fallback:
                result.warnings.append(
                    f"{url}: direct HTML had no source clues; Microlink fallback failed: "
                    f"{type(fallback).__name__}: {fallback}"[:2_000]
                )
            else:
                if recovered is not None:
                    rendered_url, rendered_body = recovered
                    rendered_clues = await asyncio.to_thread(
                        _html_clues,
                        rendered_body,
                        page_url=rendered_url,
                        suggestion=suggestion,
                    )
                    if any(rendered_clues):
                        final_url = rendered_url
                        body = rendered_body
                        used_rendered_html = True
                        candidates, links, pdf_links, hints = rendered_clues
                        result.microlink_recovered += 1
                        result.warnings.append(
                            f"{url}: Microlink recovered source clues from rendered HTML"
                        )
                    else:
                        if not body.strip() and rendered_body.strip():
                            body = rendered_body
                        result.warnings.append(
                            f"{url}: rendered HTML contained no additional source clues"
                        )
        if not body.strip():
            result.warnings.append(f"{url}: empty HTML response")
            continue
        result.html_pages += 1
        report(url)
        all_candidates = candidates
        eligible = (
            page_cache.candidates([c.model_dump(mode="json") for c in candidates])
            if page_cache else candidates
        )
        result.candidates.extend(eligible)
        page_yield.observe((item.hostname for item in eligible), fresh=True)
        result.entity_hints.extend(hints)
        dynamic_scope = bool(re.search(
            rb"load\s+more|show\s+more|<select\b|data-(?:page|cursor|offset)=", body, re.I
        ))
        scope_incomplete = scope_incomplete or dynamic_scope
        if page_cache:
            # All extracted entries are cached before the output cap, so omitted companies
            # can be consumed later using existing persisted lineage as the exact cursor.
            await _cache_receipt(page_cache, "put", url, "html", {
                "final_url": final_url,
                "candidates": [c.model_dump(mode="json") for c in all_candidates],
                "links": links, "pdf_links": pdf_links, "dynamic_scope": dynamic_scope,
                "entity_hints": hints,
            })
        follow_links(links, pdf_links, depth)
        if len(_merge_candidates(result.candidates, bounds.max_results + 1)) >= bounds.max_results:
            break
        if queue and page_yield.stop_reason:
            yield_stop = page_yield.stop_reason
            result.warnings.append(
                "Source yielded after diminishing HTML returns; remaining pages stay due"
            )
            break

    while pdf_queue and result.pdf_files < bounds.max_pdfs and not timeout_break:
        url = pdf_queue.popleft()
        if url in seen_pdfs and url in seen:
            continue
        seen_pdfs.add(url)
        scope_incomplete = True  # Bounded PDF extraction is not proof of whole-document coverage.
        cached_pdf = await _cache_receipt(page_cache, "get", url, "pdf") if page_cache else None
        if cached_pdf is not None:
            result.candidates.extend(page_cache.candidates(cached_pdf["candidates"]))
            result.entity_hints.extend(cached_pdf.get("entity_hints", []))
            report(url)
            continue
        pdf_candidates = []
        try:
            response, body = await _bounded_get(
                client,
                url,
                max_bytes=bounds.max_pdf_bytes,
                accept="application/pdf,*/*;q=0.1",
                allowed_content_types=("pdf", "octet-stream"),
            )
        except (httpx.HTTPError, OSError, UnsafeTargetError, ValueError) as exc:
            result.errors.append(f"{url}: {type(exc).__name__}: {exc}"[:2_000])
            continue
        if response.status_code >= 400:
            result.warnings.append(f"{url}: HTTP {response.status_code}")
            continue
        if not body:
            result.warnings.append(f"{url}: empty or unsupported PDF response")
            continue
        result.pdf_files += 1
        report(url)
        try:
            urls, hints = await asyncio.to_thread(
                _extract_pdf_clues,
                body,
                max_pages=bounds.max_pdf_pages,
            )
        except Exception as exc:
            result.warnings.append(f"{url}: invalid PDF ({type(exc).__name__})")
            continue
        result.entity_hints.extend(hints)
        for raw_url in urls:
            candidate = _candidate_from_url(
                raw_url,
                suggestion=suggestion,
                evidence_url=url,
                rank_bonus=3.0,
            )
            if candidate:
                pdf_candidates.append(candidate)
                result.candidates.extend(
                    page_cache.candidates([candidate.model_dump(mode="json")])
                    if page_cache else [candidate]
                )
        if page_cache:
            await _cache_receipt(page_cache, "put", url, "pdf", {
                "candidates": [c.model_dump(mode="json") for c in pdf_candidates],
                "entity_hints": hints})

    candidate_cap = len(_merge_candidates(
        result.candidates, bounds.max_results + 1
    )) >= bounds.max_results
    retrieval_incomplete = bool(result.errors) or any(
        re.search(r"HTTP \d+|empty|invalid PDF|failed", warning)
        and "recovered" not in warning for warning in result.warnings
    )
    result.coverage = {
        "scope": "reachable_public_pages_within_limits", "complete": False,
        "reachable_scope_exhausted": (
            not queue and not pdf_queue and not scope_incomplete and not retrieval_incomplete
        ),
        "continuation": bool(queue or pdf_queue or candidate_cap or retrieval_incomplete),
        "retrieval_incomplete": retrieval_incomplete,
        "fetch_attempts": result.fetch_attempts,
        "timeout_break": timeout_break,
        "bounded_or_dynamic": scope_incomplete,
        "pages_reused": page_cache.pages_reused if page_cache else 0,
        "page_yield": page_yield.summary(),
        "yield_stop": yield_stop,
    }
    if timeout_break:
        result.coverage["retry_after"] = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    if rate_limit_retry_after and any(
        "HTTP 429" in warning and "recovered rendered HTML" not in warning
        for warning in result.warnings
    ):
        result.coverage["retry_after"] = rate_limit_retry_after.isoformat()
    if yield_stop:
        # Allocation backoff is not a new evidence freshness clock. Preserve longer
        # provider cooldowns, and let existing graph receipts replay on the next slice.
        retry_at = datetime.now(UTC) + timedelta(hours=1)
        if result.coverage.get("retry_after"):
            retry_at = max(retry_at, datetime.fromisoformat(result.coverage["retry_after"]))
        result.coverage["retry_after"] = retry_at.isoformat()
    result.candidates = _merge_candidates(result.candidates, bounds.max_results)
    result.entity_hints = list(dict.fromkeys(result.entity_hints))[:100]
    if (
        result.html_pages == 0
        and result.pdf_files == 0
        and result.api_calls == 0
        and not (page_cache and page_cache.pages_reused)
        and not result.candidates
        and not result.errors
    ):
        cause = next((warning for warning in result.warnings
                      if "recovered rendered HTML" not in warning), "")
        result.errors.append(
            "Source crawl produced no readable public pages"
            + (f": {cause}" if cause else "")
        )
    return result


def _ai_configured() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


async def _ai_candidates(
    suggestion: SourceSuggestion,
    *,
    limits: SourceDiscoveryLimits,
    model: str,
    entity_hints: list[str],
) -> tuple[list[DiscoveredHost], int]:
    resolution = await source_discovery_ai.resolve_source_domains(
        suggestion,
        model=model,
        max_results=limits.max_results,
        max_tool_calls=limits.max_tool_calls,
        entity_hints=entity_hints,
    )
    candidates: list[DiscoveredHost] = []
    for item in resolution.domains:
        candidate = _candidate_from_url(
            item.domain,
            suggestion=suggestion,
            evidence_url=item.evidence_urls[0],
            label=item.name,
            rank_bonus=1.0,
        )
        if candidate:
            candidates.append(
                candidate.model_copy(update={"discovery_evidence_urls": item.evidence_urls[:6]})
            )
    return _merge_candidates(candidates, limits.max_results), resolution.tool_calls


async def discover_source(
    client: httpx.AsyncClient,
    suggestion: SourceSuggestion,
    *,
    limits: SourceDiscoveryLimits | None = None,
    ai_model: str | None = None,
    allow_ai: bool = True,
    ct_endpoint: str = DEFAULT_CT_ENDPOINT,
    microlink_api_key: str = "",
    page_cache=None,
    progress=None,
) -> SourceDiscoveryResult:
    """Execute any SourceSuggestion kind and return auditable, bounded candidates."""

    normalized_url = canonicalize_public_source_url(suggestion.url)
    if normalized_url is not None and normalized_url != suggestion.url:
        suggestion = suggestion.model_copy(update={"url": normalized_url})
    bounds = limits or SourceDiscoveryLimits()
    method = discovery_method_for(suggestion)
    blockers = source_execution_blockers(suggestion)
    if blockers:
        return SourceDiscoveryResult(
            source_key=_source_key(suggestion),
            source_kind=suggestion.source_kind,
            method=method,
            errors=blockers,
        )
    warnings: list[str] = []
    errors: list[str] = []
    candidates: list[DiscoveredHost] = []
    direct_candidates: list[DiscoveredHost] = []
    ai_candidates: list[DiscoveredHost] = []
    ct_candidates: list[DiscoveredHost] = []
    ct_calls = 0
    api_calls = 0
    html_pages = 0
    pdf_files = 0
    microlink_attempted = 0
    microlink_recovered = 0
    tool_calls = 0
    entity_hints: list[str] = []
    ct_failed = False
    crawl_errors: list[str] = []
    direct_crawl_failed = False
    coverage = {}

    if method == "certificate_transparency":
        if not suggestion.apex:
            errors.append("Tenant-apex discovery requires an apex")
            ct_failed = True
        else:
            ct_calls = 1
            try:
                ct_candidates = await discover_apex(
                    client,
                    suggestion.apex,
                    endpoint=ct_endpoint,
                    max_hosts=bounds.max_results,
                )
            except (DiscoveryError, httpx.HTTPError, OSError, ValueError) as exc:
                ct_failed = True
                warnings.append(
                    f"Certificate transparency failed: {type(exc).__name__}: {exc}"[:2_000]
                )
        candidates.extend(ct_candidates)

    if method != "repeatable_web_search":
        crawl = await crawl_public_source(
            client,
            suggestion,
            limits=bounds,
            microlink_api_key=microlink_api_key,
            page_cache=page_cache,
            progress=progress,
        )
        coverage = crawl.coverage
        direct_candidates = crawl.candidates
        api_calls = crawl.api_calls
        html_pages = crawl.html_pages
        pdf_files = crawl.pdf_files
        microlink_attempted = crawl.microlink_attempted
        microlink_recovered = crawl.microlink_recovered
        entity_hints = crawl.entity_hints
        warnings.extend(crawl.warnings)
        crawl_errors = list(crawl.errors)
        direct_crawl_failed = bool(crawl_errors) and not direct_candidates
        errors.extend(crawl_errors)
        candidates.extend(direct_candidates)

    wants_ai = method == "repeatable_web_search" or (
        allow_ai
        and (
            (method == "certificate_transparency" and ct_failed)
            or (
                method == "certificate_transparency" and not ct_candidates and not direct_candidates
            )
            or (
                method == "public_site_crawl"
                and (bool(entity_hints) or len(direct_candidates) < bounds.max_results)
            )
        )
    )
    if wants_ai:
        if not allow_ai or not _ai_configured():
            message = "OpenAI fallback unavailable because OPENAI_API_KEY is not configured"
            if method == "repeatable_web_search" and not candidates:
                errors.append(message)
            else:
                warnings.append(message)
        else:
            try:
                resolution_model = (
                    ai_model or os.getenv("BEINGS_LEADS_SOURCE_MODEL")
                    or os.getenv("BEINGS_LEADS_MODEL") or DEFAULT_AI_MODEL
                )
                resolution_key = hashlib.sha256(json.dumps([
                    suggestion.model_dump(mode="json"), resolution_model,
                    bounds.max_results, bounds.max_tool_calls, entity_hints,
                ], sort_keys=True).encode()).hexdigest()
                cached_resolution = (await _cache_receipt(
                    page_cache, "get", resolution_key, "ai_resolution",
                ) if page_cache else None)
                if cached_resolution is not None:
                    ai_candidates = page_cache.candidates(cached_resolution["candidates"])
                else:
                    ai_candidates, tool_calls = await _ai_candidates(
                        suggestion, limits=bounds, model=resolution_model,
                        entity_hints=entity_hints,
                    )
                    if page_cache:
                        await _cache_receipt(page_cache, "put", resolution_key, "ai_resolution", {
                            "candidates": [c.model_dump(mode="json") for c in ai_candidates],
                        })
            except Exception as exc:
                errors.append(f"OpenAI fallback failed: {type(exc).__name__}: {exc}"[:2_000])
            candidates.extend(ai_candidates)
            if ai_candidates and direct_crawl_failed:
                recovered_errors = set(crawl_errors)
                errors = [message for message in errors if message not in recovered_errors]
                warnings.extend(
                    f"Direct crawl failed; AI recovery returned candidates: {message}"
                    for message in crawl_errors
                )

    if page_cache:
        coverage["pages_reused"] = page_cache.pages_reused
    merged = _merge_candidates(candidates, bounds.max_results)
    counts = SourceDiscoveryCounts(
        ct_calls=ct_calls,
        api_calls=api_calls,
        html_pages=html_pages,
        pdf_files=pdf_files,
        ct_candidates=len(ct_candidates),
        direct_candidates=len(direct_candidates),
        ai_candidates=len(ai_candidates),
        total_candidates=len(merged),
        microlink_attempted=microlink_attempted,
        microlink_recovered=microlink_recovered,
    )
    return SourceDiscoveryResult(
        source_key=_source_key(suggestion),
        source_kind=suggestion.source_kind,
        method=method,
        candidates=merged,
        counts=counts,
        tool_calls=tool_calls,
        warnings=list(dict.fromkeys(warnings)),
        errors=list(dict.fromkeys(errors)),
        coverage=coverage,
    )


execute_source_discovery = discover_source
