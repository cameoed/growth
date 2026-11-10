from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .async_work import gather_owned
from .inspect import UnsafeTargetError, _bounded_get
from .models import SiteObservation, VendorEvidence, VendorIntelligence
from .provider_rate_limit import retry_after_seconds

DNS_TTL_HOURS = 24 * 7
DISCLOSURE_TTL_HOURS = 24 * 30
DNS_RESOLVER = "https://cloudflare-dns.com/dns-query"
MAX_SPF_LOOKUPS = 10
MAX_SPF_DEPTH = 2
MAX_DISCLOSURE_PAGES = 3
CAREER_HOST_LABELS = ("careers", "jobs", "hiring", "talent")


@dataclass(frozen=True)
class VendorRule:
    name: str
    category: str
    aliases: tuple[str, ...]
    weight: int = 2
    creative_commerce: bool = False


VENDOR_RULES: tuple[VendorRule, ...] = (
    VendorRule(
        "Google Workspace",
        "collaboration_email",
        ("google workspace", "google workspaces", "_spf.google.com", "aspmx.l.google.com"),
        3,
    ),
    VendorRule(
        "Microsoft 365",
        "collaboration_email",
        ("microsoft 365", "office 365", "spf.protection.outlook.com", "onmicrosoft.com"),
        3,
    ),
    VendorRule(
        "Amazon Web Services",
        "cloud_infrastructure",
        ("amazon web services", "aws", "amazonses.com", "amazonaws.com", "awsdns-"),
        4,
    ),
    VendorRule(
        "Google Cloud",
        "cloud_infrastructure",
        ("google cloud platform", "google cloud", "googleapis.com", "gcp"),
        4,
    ),
    VendorRule(
        "Microsoft Azure",
        "cloud_infrastructure",
        ("microsoft azure", "azure", "azure-dns", "azurewebsites.net"),
        4,
    ),
    VendorRule(
        "Cloudflare",
        "cloud_infrastructure",
        ("cloudflare", "cloudflare.com", "cloudflare.net"),
        4,
    ),
    VendorRule("Fastly", "cloud_infrastructure", ("fastly", "fastly.net"), 3),
    VendorRule("Akamai", "cloud_infrastructure", ("akamai", "akamai.net"), 3),
    VendorRule("Stripe", "payments_revenue", ("stripe",), 4),
    VendorRule("Shopify", "marketing_commerce", ("shopify", "shops.shopify.com"), 4, True),
    VendorRule("Klaviyo", "marketing_commerce", ("klaviyo",), 4, True),
    VendorRule("Attentive", "marketing_commerce", ("attentive",), 4, True),
    VendorRule("Braze", "marketing_commerce", ("braze",), 4, True),
    VendorRule("Iterable", "marketing_commerce", ("iterable",), 3, True),
    VendorRule("Mailchimp", "marketing_commerce", ("mailchimp", "servers.mcsv.net"), 3, True),
    VendorRule("HubSpot", "marketing_commerce", ("hubspot", "hubspotemail.net"), 4, True),
    VendorRule("Salesforce", "customer_operations", ("salesforce", "_spf.salesforce.com"), 4),
    VendorRule("Zendesk", "customer_operations", ("zendesk", "mail.zendesk.com"), 3),
    VendorRule("Intercom", "customer_operations", ("intercom",), 3),
    VendorRule("Gorgias", "customer_operations", ("gorgias",), 3, True),
    VendorRule("Twilio", "customer_operations", ("twilio", "sendgrid", "sendgrid.net"), 3),
    VendorRule("Mailgun", "collaboration_email", ("mailgun", "mailgun.org"), 2),
    VendorRule("WorkOS", "identity_security", ("workos",), 4),
    VendorRule("Okta / Auth0", "identity_security", ("okta", "auth0"), 4),
    VendorRule("Persona", "identity_security", ("persona",), 3),
    VendorRule("Yoti", "identity_security", ("yoti",), 3),
    VendorRule("1Password", "identity_security", ("1password",), 3),
    VendorRule("DigiCert", "identity_security", ("digicert",), 2),
    VendorRule("Let's Encrypt", "identity_security", ("letsencrypt", "let's encrypt"), 1),
    VendorRule("Sentry", "data_observability", ("sentry",), 3),
    VendorRule("Segment", "data_observability", ("segment", "segment.io"), 4),
    VendorRule("Datadog", "data_observability", ("datadog",), 4),
    VendorRule("Snowflake", "data_observability", ("snowflake",), 4),
    VendorRule("MongoDB", "data_observability", ("mongodb",), 3),
    VendorRule("TurboPuffer", "data_observability", ("turbopuffer",), 3),
    VendorRule("Atlassian", "collaboration_dev", ("atlassian", "jira"), 3),
    VendorRule("Slack", "collaboration_dev", ("slack",), 3),
    VendorRule("Cursor", "collaboration_dev", ("cursor",), 2),
    VendorRule("JetBrains", "collaboration_dev", ("jetbrains",), 2),
    VendorRule("Brave Search", "ai_media", ("brave search",), 2),
    VendorRule("ElevenLabs", "ai_media", ("elevenlabs", "eleven labs"), 3, True),
    VendorRule("OpenAI", "ai_media", ("openai",), 3, True),
    VendorRule("Anthropic", "ai_media", ("anthropic", "claude"), 3),
    VendorRule(
        "Meta",
        "marketing_commerce",
        ("meta platforms", "facebook-domain-verification"),
        3,
        True,
    ),
    VendorRule("TikTok", "marketing_commerce", ("tiktok",), 3, True),
    VendorRule("Pinterest", "marketing_commerce", ("pinterest",), 2, True),
    VendorRule("Triple Whale", "marketing_commerce", ("triple whale", "triplewhale"), 4, True),
)

RULE_BY_NAME = {rule.name: rule for rule in VENDOR_RULES}
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
CONFIDENCE_FACTOR = {"low": 0.3, "medium": 0.7, "high": 1.0}
DISCLOSURE_SOURCES = {"subprocessor", "privacy_disclosure"}
CATEGORY_WEIGHTS = {
    "cloud_infrastructure": 4,
    "payments_revenue": 4,
    "marketing_commerce": 4,
    "identity_security": 3,
    "data_observability": 3,
    "customer_operations": 3,
    "collaboration_email": 2,
    "collaboration_dev": 2,
    "ai_media": 3,
}


def _contains_alias(text: str, alias: str) -> bool:
    lowered = text.lower()
    alias = alias.lower()
    if alias in {"aws", "gcp", "jira", "slack", "cursor", "stripe", "azure"}:
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", lowered))
    return alias in lowered


def _matching_rules(text: str) -> list[VendorRule]:
    return [
        rule for rule in VENDOR_RULES if any(_contains_alias(text, alias) for alias in rule.aliases)
    ]


def _clean_dns_record(value: object) -> str:
    text = str(value).strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    return text.replace('" "', "").replace("\\\"", '"')[:1_000]


async def _dns_query(
    client: httpx.AsyncClient, name: str, record_type: str
) -> tuple[list[str], str]:
    url = httpx.URL(DNS_RESOLVER, params={"name": name, "type": record_type})
    for attempt in range(2):
        try:
            response, body = await _bounded_get(
                client,
                str(url),
                max_bytes=500_000,
                accept="application/dns-json",
                allowed_content_types=("dns-json", "json", "text/plain"),
            )
        except httpx.TransportError:
            if attempt:
                raise
            await asyncio.sleep(0.2)
            continue
        delay = retry_after_seconds(response.headers)
        if response.status_code in {502, 503, 504} and not attempt and (
            delay is None or delay <= 1
        ):
            await asyncio.sleep(max(delay or 0, .2))
            continue
        if not 200 <= response.status_code < 400 or not body:
            return [], str(url)
        payload = json.loads(body)
        answers = payload.get("Answer", []) if isinstance(payload, dict) else []
        return (
            [_clean_dns_record(answer.get("data", "")) for answer in answers if answer.get("data")],
            str(url),
        )
    return [], str(url)


def _dns_evidence(
    records: dict[str, list[str]], urls: dict[str, str]
) -> list[VendorEvidence]:
    evidence: list[VendorEvidence] = []
    for key, values in records.items():
        record_type = key.split(":", 1)[0].upper()
        for record in values:
            lowered = record.lower()
            if record_type == "TXT" and lowered.startswith("v=spf1"):
                source_type = "spf"
                confidence = "medium"
            elif record_type == "TXT":
                source_type = "txt_verification"
                confidence = "low"
            elif record_type == "MX":
                source_type = "mx"
                confidence = "medium"
            elif record_type == "NS":
                source_type = "ns"
                confidence = "medium"
            elif record_type == "CAA":
                source_type = "caa"
                confidence = "medium"
            else:
                continue
            for rule in _matching_rules(record):
                evidence.append(
                    VendorEvidence(
                        vendor=rule.name,
                        category=rule.category,
                        source_type=source_type,
                        confidence=confidence,
                        evidence_url=urls.get(key, urls.get(record_type, DNS_RESOLVER)),
                        record=record[:400],
                    )
                )
    return evidence


async def collect_dns_vendor_evidence(
    client: httpx.AsyncClient, host: str
) -> dict[str, Any]:
    career_hosts = tuple(f"{label}.{host}" for label in CAREER_HOST_LABELS)
    wildcard_control_host = (
        f"beings-career-probe-{hashlib.sha256(host.encode()).hexdigest()[:12]}.{host}"
    )
    questions = [
        ("TXT", host, "TXT"),
        ("MX", host, "MX"),
        ("NS", host, "NS"),
        ("CAA", host, "CAA"),
        ("DMARC", f"_dmarc.{host}", "TXT"),
        ("BIMI", f"default._bimi.{host}", "TXT"),
        *(
            (f"A:{career_host}", career_host, "A")
            for career_host in career_hosts
        ),
        (f"A:{wildcard_control_host}", wildcard_control_host, "A"),
    ]

    async def one(key: str, name: str, record_type: str):
        values, url = await _dns_query(client, name, record_type)
        return key, values, url

    results = await asyncio.gather(
        *(one(key, name, record_type) for key, name, record_type in questions),
        return_exceptions=True,
    )
    records: dict[str, list[str]] = {}
    urls: dict[str, str] = {}
    errors: list[str] = []
    network_calls = len(questions)
    for result in results:
        if isinstance(result, BaseException):
            errors.append(f"DNS: {type(result).__name__}: {result}")
            continue
        key, values, url = result
        records[key] = values[:25]
        urls[key] = url

    visited: set[str] = set()
    queue: list[tuple[str, int]] = []
    for record in records.get("TXT", []):
        if record.lower().startswith("v=spf1"):
            queue.extend(
                (domain.lower().rstrip("."), 1)
                for domain in re.findall(
                    r"(?:include:|redirect=)([a-z0-9._-]+)", record, re.I
                )
            )
    while queue and len(visited) < MAX_SPF_LOOKUPS:
        domain, depth = queue.pop(0)
        if domain in visited or depth > MAX_SPF_DEPTH:
            continue
        visited.add(domain)
        try:
            values, url = await _dns_query(client, domain, "TXT")
        except (httpx.HTTPError, UnsafeTargetError, TimeoutError, json.JSONDecodeError) as exc:
            errors.append(f"SPF {domain}: {type(exc).__name__}: {exc}")
            continue
        network_calls += 1
        key = f"TXT:{domain}"
        records[key] = values[:15]
        urls[key] = url
        if depth < MAX_SPF_DEPTH:
            for record in values:
                if record.lower().startswith("v=spf1"):
                    queue.extend(
                        (nested.lower().rstrip("."), depth + 1)
                        for nested in re.findall(
                            r"(?:include:|redirect=)([a-z0-9._-]+)", record, re.I
                        )
                    )

    dmarc_policy = ""
    for record in records.get("DMARC", []):
        match = re.search(r"(?:^|;)\s*p=(reject|quarantine|none)\b", record, re.I)
        if match:
            dmarc_policy = match.group(1).lower()
            break
    bimi_record = next(
        (
            record
            for record in records.get("BIMI", [])
            if record.lower().startswith("v=bimi1")
        ),
        "",
    )
    bimi_logo = re.search(r"(?:^|;)\s*l=([^;\s]+)", bimi_record, re.I)
    bimi_mark = re.search(r"(?:^|;)\s*a=([^;\s]+)", bimi_record, re.I)
    wildcard_resolved = bool(records.get(f"A:{wildcard_control_host}"))
    resolved_career_hosts = (
        []
        if wildcard_resolved
        else [career_host for career_host in career_hosts if records.get(f"A:{career_host}")]
    )
    career_host_evidence_urls = {
        career_host: urls[f"A:{career_host}"]
        for career_host in resolved_career_hosts
        if f"A:{career_host}" in urls
    }
    evidence = _dns_evidence(records, urls)
    return {
        "records": records,
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "career_hosts": resolved_career_hosts,
        "career_host_evidence_urls": career_host_evidence_urls,
        "dmarc_policy": dmarc_policy,
        "bimi_published": bool(bimi_record),
        "bimi_verified_mark": bool(bimi_mark and bimi_mark.group(1)),
        "bimi_logo_url": bimi_logo.group(1)[:2_000] if bimi_logo else None,
        "bimi_evidence_url": urls.get("BIMI"),
        "errors": errors[:10],
        "network_calls": network_calls,
    }


def parse_vendor_disclosure(url: str, body: bytes) -> dict[str, Any]:
    soup = BeautifulSoup(body.decode("utf-8", errors="replace"), "html.parser")
    for node in soup(["script", "style", "noscript", "svg", "template"]):
        node.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    headings = " ".join(
        heading.get_text(" ", strip=True) for heading in soup.find_all(["h1", "h2"])[:5]
    )
    visible = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    lowered = f"{url} {title} {visible[:4_000]}".lower()
    explicit_context = f"{url} {title} {headings}".lower()
    explicit = (
        "subprocessor" in explicit_context or "sub-processor" in explicit_context
    )
    privacy_context = any(
        phrase in lowered
        for phrase in ("service providers", "third-party providers", "data processor")
    )
    if not explicit and not privacy_context:
        return {"url": url, "evidence": [], "valid": False}
    source_type = "subprocessor" if explicit else "privacy_disclosure"
    confidence = "high" if explicit else "medium"
    units = []
    for node in soup.find_all(["tr", "li", "p"]):
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if 8 <= len(text) <= 1_000:
            units.append(text)
    if not units:
        units = [visible[index : index + 800] for index in range(0, min(len(visible), 20_000), 800)]
    evidence: list[VendorEvidence] = []
    seen: set[str] = set()
    for unit in units[:600]:
        for rule in _matching_rules(unit):
            if rule.name in seen:
                continue
            seen.add(rule.name)
            evidence.append(
                VendorEvidence(
                    vendor=rule.name,
                    category=rule.category,
                    source_type=source_type,
                    confidence=confidence,
                    evidence_url=url,
                    purpose=unit[:500],
                )
            )
    seen_lower = {name.lower() for name in seen}
    generic_headers = ("vendor", "provider", "subprocessor", "company", "name")
    purpose_headers = ("purpose", "service", "function", "use", "activity")
    rejected_names = {
        "company",
        "name",
        "provider",
        "service provider",
        "subprocessor",
        "sub-processor",
        "vendor",
        "various",
    }
    for table in soup.find_all("table")[:20]:
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = [
            re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip().lower()
            for cell in rows[0].find_all(["th", "td"])
        ]
        vendor_index = next(
            (
                index
                for index, header in enumerate(headers)
                if any(word in header for word in generic_headers)
            ),
            None,
        )
        if vendor_index is None:
            continue
        purpose_index = next(
            (
                index
                for index, header in enumerate(headers)
                if any(word in header for word in purpose_headers)
            ),
            None,
        )
        for row in rows[1:201]:
            cells = row.find_all(["th", "td"])
            if vendor_index >= len(cells):
                continue
            vendor = re.sub(
                r"\s+", " ", cells[vendor_index].get_text(" ", strip=True)
            ).strip(" \t\n–—:;")
            lowered_vendor = vendor.lower()
            if (
                not 2 <= len(vendor) <= 80
                or len(vendor.split()) > 8
                or lowered_vendor in rejected_names
                or lowered_vendor in seen_lower
                or len(re.findall(r"[.!?]", vendor)) > 1
            ):
                continue
            if _matching_rules(vendor):
                continue
            purpose = ""
            if purpose_index is not None and purpose_index < len(cells):
                purpose = re.sub(
                    r"\s+", " ", cells[purpose_index].get_text(" ", strip=True)
                ).strip()[:500]
            seen_lower.add(lowered_vendor)
            evidence.append(
                VendorEvidence(
                    vendor=vendor,
                    category="other_operating",
                    source_type=source_type,
                    confidence=confidence,
                    evidence_url=url,
                    purpose=purpose,
                )
            )
    return {
        "url": url,
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "valid": bool(evidence),
    }


def _disclosure_candidates(observation: SiteObservation) -> list[str]:
    def priority(url: str) -> int:
        lowered = url.lower()
        if "subprocessor" in lowered or "sub-processor" in lowered:
            return 0
        if any(word in lowered for word in ("data-processing", "dpa", "privacy")):
            return 1
        if "trust" in lowered:
            return 2
        return 3

    explicit = sorted(
        observation.legal_urls,
        key=priority,
    )
    text = (
        f"{observation.title} {observation.description} "
        f"{observation.text_excerpt[:8_000]}"
    ).lower()
    b2b_hint = sum(
        phrase in text
        for phrase in (
            "api",
            "saas",
            "software",
            "enterprise",
            "platform",
            "customer data",
            "data processing",
            "security",
        )
    ) >= 2
    if b2b_hint:
        host = observation.canonical_host or observation.hostname
        explicit.extend(
            [
                f"https://{host}/subprocessors",
                f"https://{host}/legal/subprocessors",
                f"https://{host}/trust/subprocessors",
            ]
        )
    return list(dict.fromkeys(explicit))[:MAX_DISCLOSURE_PAGES]


async def collect_disclosure_evidence(
    client: httpx.AsyncClient, observation: SiteObservation
) -> dict[str, Any]:
    errors: list[str] = []
    attempted = 0
    queue = _disclosure_candidates(observation)
    visited: set[str] = set()
    best_disclosure_url: str | None = None
    privacy_fallback: dict[str, Any] | None = None
    while queue and attempted < MAX_DISCLOSURE_PAGES:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        attempted += 1
        try:
            response, body = await _bounded_get(
                client,
                url,
                max_bytes=1_500_000,
                accept="text/html,application/xhtml+xml,*/*;q=0.1",
                allowed_content_types=("html", "text/plain"),
            )
        except (httpx.HTTPError, UnsafeTargetError, TimeoutError) as exc:
            errors.append(f"Disclosure: {type(exc).__name__}: {exc}")
            continue
        if not 200 <= response.status_code < 400 or not body:
            continue
        resolved_url = str(response.url)
        soup = BeautifulSoup(body.decode("utf-8", errors="replace"), "html.parser")
        nested: list[str] = []
        for link in soup.find_all("a", href=True)[:1_000]:
            href = str(link.get("href", "")).strip()
            label = link.get_text(" ", strip=True)
            hint = f"{href} {label}".lower()
            if "subprocessor" not in hint and "sub-processor" not in hint:
                continue
            candidate = urljoin(resolved_url, href)
            parsed_candidate = urlparse(candidate)
            if parsed_candidate.scheme in {"http", "https"} and parsed_candidate.hostname:
                nested.append(candidate)
        nested = list(dict.fromkeys(nested))
        for candidate in reversed(nested):
            if candidate not in visited and candidate not in queue:
                queue.insert(0, candidate)
        if "subprocessor" in resolved_url.lower() or "sub-processor" in resolved_url.lower():
            best_disclosure_url = resolved_url
        parsed = parse_vendor_disclosure(resolved_url, body)
        if parsed["valid"] and any(
            item["source_type"] == "subprocessor" for item in parsed["evidence"]
        ):
            return {**parsed, "errors": errors, "network_calls": attempted}
        if parsed["valid"] and privacy_fallback is None:
            privacy_fallback = parsed
    if privacy_fallback:
        return {**privacy_fallback, "errors": errors, "network_calls": attempted}
    return {
        "url": best_disclosure_url,
        "evidence": [],
        "valid": False,
        "errors": errors,
        "network_calls": attempted,
    }


def build_vendor_intelligence(
    dns: dict[str, Any], disclosure: dict[str, Any]
) -> VendorIntelligence:
    evidence = [
        VendorEvidence.model_validate(item)
        for item in [*dns.get("evidence", []), *disclosure.get("evidence", [])]
    ]
    deduplicated: dict[tuple[str, str, str], VendorEvidence] = {}
    for item in evidence:
        key = (item.vendor, item.source_type, item.record or item.purpose)
        existing = deduplicated.get(key)
        if existing is None or CONFIDENCE_RANK[item.confidence] > CONFIDENCE_RANK[
            existing.confidence
        ]:
            deduplicated[key] = item
    evidence = list(deduplicated.values())[:100]
    vendor_sources: dict[str, set[str]] = defaultdict(set)
    vendor_categories: dict[str, str] = {}
    vendor_confidence: dict[str, str] = {}
    for item in evidence:
        vendor_sources[item.vendor].add(item.source_type)
        vendor_categories[item.vendor] = item.category
        if CONFIDENCE_RANK[item.confidence] > CONFIDENCE_RANK.get(
            vendor_confidence.get(item.vendor, "low"), 0
        ):
            vendor_confidence[item.vendor] = item.confidence
        else:
            vendor_confidence.setdefault(item.vendor, item.confidence)
    vendors = sorted(vendor_sources)
    corroborated = sorted(
        vendor
        for vendor, sources in vendor_sources.items()
        if sources & DISCLOSURE_SOURCES and sources - DISCLOSURE_SOURCES
    )
    category_counts = Counter(vendor_categories.values())
    source_counts = Counter(item.source_type for item in evidence)
    confidence_counts = Counter(item.confidence for item in evidence)
    disclosed = {
        item.vendor for item in evidence if item.source_type in DISCLOSURE_SOURCES
    }
    dns_vendors = {item.vendor for item in evidence if item.source_type not in DISCLOSURE_SOURCES}
    high_confidence = {
        vendor for vendor, confidence in vendor_confidence.items() if confidence == "high"
    }
    category_strength: dict[str, float] = defaultdict(float)
    for vendor, category in vendor_categories.items():
        category_strength[category] = max(
            category_strength[category],
            CONFIDENCE_FACTOR[vendor_confidence[vendor]],
        )
    category_score = sum(
        CATEGORY_WEIGHTS.get(category, 2) * strength * 2
        for category, strength in category_strength.items()
    )
    weighted = sum(
        RULE_BY_NAME[vendor].weight * CONFIDENCE_FACTOR[vendor_confidence[vendor]]
        for vendor in vendors
        if vendor in RULE_BY_NAME
    )
    stack_score = min(round(category_score), 32)
    stack_score += min(len(high_confidence) * 3, 15)
    stack_score += min(round(weighted), 27)
    stack_score += 8 if disclosure.get("valid") else 0
    stack_score += min(len(corroborated) * 3, 9)
    dmarc_policy = str(dns.get("dmarc_policy", ""))
    stack_score += 5 if dmarc_policy in {"reject", "quarantine"} else 0
    creative_score = min(
        100,
        round(
            sum(
                RULE_BY_NAME[vendor].weight
                * CONFIDENCE_FACTOR[vendor_confidence[vendor]]
                * 9
                for vendor in vendors
                if vendor in RULE_BY_NAME and RULE_BY_NAME[vendor].creative_commerce
            )
        ),
    )
    return VendorIntelligence(
        evidence=evidence,
        vendors=vendors,
        category_counts=dict(category_counts),
        source_counts=dict(source_counts),
        confidence_counts=dict(confidence_counts),
        high_confidence_vendors=sorted(high_confidence),
        corroborated_vendors=corroborated,
        dns_vendor_count=len(dns_vendors),
        disclosed_vendor_count=len(disclosed),
        stack_score=min(stack_score, 100),
        creative_commerce_score=creative_score,
        dmarc_policy=dmarc_policy,
        bimi_published=bool(dns.get("bimi_published")),
        bimi_verified_mark=bool(dns.get("bimi_verified_mark")),
        bimi_logo_url=dns.get("bimi_logo_url"),
        bimi_evidence_url=dns.get("bimi_evidence_url"),
        disclosure_url=disclosure.get("url"),
        dns_records={
            key: [str(value)[:500] for value in values[:15]]
            for key, values in dns.get("records", {}).items()
        },
        errors=list(dict.fromkeys([*dns.get("errors", []), *disclosure.get("errors", [])]))[
            :15
        ],
    )


async def enrich_vendor_intelligence(
    client: httpx.AsyncClient,
    observation: SiteObservation,
    *,
    cache_get,
    cache_set,
) -> dict[str, int]:
    if not observation.live:
        return {"network_calls": 0, "cache_hits": 0}
    host = (observation.canonical_host or observation.hostname).removeprefix("www.").lower()
    cache_hits = 0
    network_calls = 0
    async def dns_work():
        dns = cache_get(host, "vendor_dns")
        if dns is not None and all(key in dns for key in (
            "bimi_published", "career_hosts", "career_host_evidence_urls",
        )):
            return dns, 1, 0
        dns = await collect_dns_vendor_evidence(client, host)
        cache_set(host, "vendor_dns", dns, DNS_TTL_HOURS)
        return dns, 0, int(dns.get("network_calls", 0))

    async def disclosure_work():
        disclosure = cache_get(host, "vendor_disclosure")
        if disclosure is not None:
            return disclosure, 1, 0
        disclosure = await collect_disclosure_evidence(client, observation)
        cache_set(host, "vendor_disclosure", disclosure, DISCLOSURE_TTL_HOURS)
        return disclosure, 0, int(disclosure.get("network_calls", 0))

    dns_result, disclosure_result = await gather_owned(dns_work(), disclosure_work())
    dns, disclosure = dns_result[0], disclosure_result[0]
    cache_hits = dns_result[1] + disclosure_result[1]
    network_calls = dns_result[2] + disclosure_result[2]
    subject_key = re.sub(r"[^a-z0-9]", "", host.split(".", 1)[0])
    filtered_disclosure_evidence = [
        item
        for item in disclosure.get("evidence", [])
        if re.sub(r"[^a-z0-9]", "", str(item.get("vendor", "")).lower())
        != subject_key
    ]
    disclosure = {
        **disclosure,
        "evidence": filtered_disclosure_evidence,
        "valid": bool(disclosure.get("valid") and filtered_disclosure_evidence),
    }
    observation.web.vendors = build_vendor_intelligence(dns, disclosure)
    if observation.web.vendors.disclosure_url:
        observation.web.source_urls = list(
            dict.fromkeys(
                [*observation.web.source_urls, observation.web.vendors.disclosure_url]
            )
        )[:30]
    return {"network_calls": network_calls, "cache_hits": cache_hits}
