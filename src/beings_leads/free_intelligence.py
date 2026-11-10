from __future__ import annotations

import asyncio
import html
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import httpx
import tldextract

from .async_work import cancel_and_drain
from .hiring_intelligence import classify_job_role, normalize_job_posting
from .inspect import UnsafeTargetError, _bounded_get
from .models import PublicJobEvidence, SiteObservation, WebIntelligence
from .provider_rate_limit import retry_after_seconds
from .sales_intelligence import (
    COMMERCE_TTL_HOURS,
    FEED_TTL_HOURS,
    _local_name,
    parse_feed_document,
    refresh_sales_intelligence,
    summarize_shopify_products,
    summarize_woocommerce_products,
)
from .vendor_intelligence import enrich_vendor_intelligence

CacheGet = Callable[[str, str], dict[str, Any] | None]
CacheSet = Callable[[str, str, dict[str, Any], int], None]

SITEMAP_TTL_HOURS = 24
ATS_TTL_HOURS = 12
COMMON_CRAWL_TTL_HOURS = 24 * 30
MAX_SITEMAP_URLS = 2_500
MAX_SITEMAP_BYTES = 1_500_000
MAX_ATS_BYTES = 2_000_000
MAX_COMMON_CRAWL_BYTES = 1_500_000
MAX_CAREER_URLS = 40

_TLD_EXTRACT = tldextract.TLDExtract(
    suffix_list_urls=(),
    include_psl_private_domains=True,
)

URL_CATEGORY_PATTERNS: dict[str, tuple[str, ...]] = {
    "products": ("/product/", "/products/", "/shop/", "/p/"),
    "collections": ("/collection/", "/collections/", "/category/", "/catalog/"),
    "case_studies": ("case-stud", "/portfolio/", "/our-work/", "/work/", "/clients/"),
    "campaigns": ("/campaign/", "/campaigns/", "/landing/", "/launch/"),
    "content": ("/blog/", "/news/", "/article/", "/insights/", "/stories/"),
    "careers": ("/career", "/jobs/", "/open-roles", "/vacancies"),
    "locations": ("/location", "/locations/", "/stores/", "/store-locator"),
    "services": ("/service/", "/services/", "/solutions/"),
    "contacts": (
        "/contact",
        "/about",
        "/team",
        "/people",
        "/leadership",
        "/studio",
        "/production",
        "/press",
        "/media",
        "/new-business",
    ),
}

CREATIVE_JOB_WORDS = (
    "creative",
    "content",
    "copywriter",
    "designer",
    "editor",
    "producer",
    "production",
    "social",
    "studio",
    "video",
    "photo",
    "photographer",
    "photography",
    "videographer",
    "motion",
    "animation",
    "animator",
    "3d",
    "cgi",
    "vfx",
    "retouch",
    "post-production",
    "post production",
    "creative technologist",
)
PHOTO_VIDEO_JOB_WORDS = (
    "video",
    "photo",
    "photographer",
    "photography",
    "videographer",
    "editor",
    "producer",
    "production",
    "motion",
    "animation",
    "animator",
    "3d",
    "cgi",
    "vfx",
    "retouch",
    "post-production",
    "post production",
    "creative technologist",
)
GROWTH_JOB_WORDS = (
    "acquisition",
    "brand",
    "commerce",
    "e-commerce",
    "ecommerce",
    "growth",
    "marketing",
    "media",
    "performance",
)
LEADERSHIP_JOB_WORDS = (
    "chief",
    "director",
    "head of",
    "lead ",
    "manager",
    "president",
    "vice president",
    "vp ",
)
PRODUCTION_PRESSURE_TERMS = (
    "always-on",
    "always on",
    "fast-paced",
    "fast paced",
    "high-volume",
    "high volume",
    "multiple concurrent",
    "multiple projects",
    "rapid turnaround",
    "tight deadline",
    "at scale",
    "production pipeline",
    "content pipeline",
    "asset pipeline",
    "creative testing",
    "testing roadmap",
    "ad concepts",
    "creative refresh",
    "creative fatigue",
    "fresh ads",
    "asset variations",
    "versioning",
    "localization",
    "launch calendar",
    "content cadence",
    "performance creative",
    "creative iteration",
    "creative iterations",
)
EXTERNAL_CAPACITY_JOB_TERMS = (
    "agency partners",
    "external agencies",
    "external partners",
    "external production partners",
    "outside agencies",
    "third-party vendors",
    "third party vendors",
    "contractors",
    "outsourced",
    "outsourcing",
    "white-label",
    "white label",
    "overflow",
    "creator network",
    "freelance network",
    "freelancers",
    "production partners",
    "vendor roster",
)
AI_WORKFLOW_JOB_TERMS = (
    "ai-generated",
    "ai generated",
    "creative ai",
    "generative ai",
    "midjourney",
    "stable diffusion",
    "runway",
    "ai video",
    "ai creative tools",
    "generative video",
    "image generation",
    "sora",
    "veo",
    "kling",
    "pika",
    "luma",
    "comfyui",
    "synthetic media",
)
RIGHTS_RISK_JOB_TERMS = (
    "brand safety",
    "copyright",
    "licensing",
    "likeness",
    "model release",
    "rights management",
    "usage rights",
)


async def _public_get(
    client: httpx.AsyncClient,
    url: str,
    **kwargs: Any,
) -> tuple[httpx.Response, bytes]:
    """Retry one transient public GET while preserving the global byte/redirect bounds."""

    for attempt in range(2):
        try:
            response, body = await _bounded_get(client, url, **kwargs)
        except httpx.TransportError:
            if attempt:
                raise
            await asyncio.sleep(0.2)
            continue
        if response.status_code not in {429, 502, 503, 504} or attempt:
            return response, body
        delay = retry_after_seconds(response.headers)
        if response.status_code == 429 or (delay is not None and delay > 1):
            return response, body
        await asyncio.sleep(max(delay or 0, .2))
    raise RuntimeError("Transient GET retry loop ended unexpectedly")


def _host(observation: SiteObservation) -> str:
    return (
        (
            observation.canonical_host
            or urlparse(observation.final_url or "").hostname
            or observation.hostname
        )
        .removeprefix("www.")
        .lower()
    )


def _origin(observation: SiteObservation) -> str:
    if observation.canonical_host and observation.canonical_host.removeprefix(
        "www."
    ) != observation.hostname.removeprefix("www."):
        return f"https://{observation.canonical_host}"
    parsed = urlparse(observation.final_url or f"https://{observation.hostname}/")
    scheme = parsed.scheme if parsed.scheme in {"http", "https"} else "https"
    return f"{scheme}://{parsed.netloc or observation.hostname}"


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(normalized[:10], "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _child_text(node: ET.Element, name: str) -> str:
    for child in node:
        if _local_name(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _url_category(url: str) -> str | None:
    lowered = urlparse(url).path.lower()
    for category, patterns in URL_CATEGORY_PATTERNS.items():
        if any(pattern in lowered for pattern in patterns):
            return category
    return None


def _company_domain(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold().rstrip(".").removeprefix("www.")
    if not host:
        return ""
    parsed = _TLD_EXTRACT(host)
    if parsed.domain and parsed.suffix:
        return f"{parsed.domain}.{parsed.suffix}"
    return host


def _same_company_url(left: str, right: str) -> bool:
    left_domain = _company_domain(left)
    return bool(left_domain and left_domain == _company_domain(right))


def _career_sitemap_priority(url: str) -> tuple[int, str]:
    path = urlparse(url).path.casefold()
    return (
        0
        if any(
            token in path
            for token in (
                "career",
                "hiring",
                "jobs",
                "open-role",
                "vacanc",
            )
        )
        else 1,
        url,
    )


def summarize_sitemap_documents(
    documents: list[tuple[str, bytes]], *, now: datetime | None = None
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    counts: Counter[str] = Counter()
    recent_counts: Counter[str] = Counter()
    total_urls = 0
    recent_30d = 0
    recent_90d = 0
    latest: datetime | None = None
    samples: list[str] = []
    career_url_sources: dict[str, str] = {}
    child_sitemaps: list[str] = []
    errors: list[str] = []
    for source_url, body in documents:
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            errors.append(f"Invalid sitemap XML at {source_url}: {exc}")
            continue
        kind = _local_name(root.tag)
        if kind == "sitemapindex":
            for node in list(root)[:250]:
                location = _child_text(node, "loc")
                if location and not location.lower().endswith(".gz"):
                    child_sitemaps.append(location)
            continue
        if kind != "urlset":
            errors.append(f"Unsupported sitemap root at {source_url}: {kind}")
            continue
        for node in list(root)[:MAX_SITEMAP_URLS]:
            location = _child_text(node, "loc")
            if not location:
                continue
            total_urls += 1
            category = _url_category(location)
            if category:
                counts[category] += 1
                if len(samples) < 20:
                    samples.append(location)
            if (
                category == "careers"
                and len(career_url_sources) < MAX_CAREER_URLS
                and _same_company_url(location, source_url)
            ):
                career_url_sources.setdefault(location, source_url)
            changed = _parse_date(_child_text(node, "lastmod"))
            if changed:
                latest = max(latest, changed) if latest else changed
                age = current - changed
                if timedelta(0) <= age <= timedelta(days=30):
                    recent_30d += 1
                    if category:
                        recent_counts[category] += 1
                if timedelta(0) <= age <= timedelta(days=90):
                    recent_90d += 1
    return {
        "total_urls": total_urls,
        "counts": dict(counts),
        "recent_30d": recent_30d,
        "recent_90d": recent_90d,
        "recent_counts": dict(recent_counts),
        "latest_change": latest.isoformat() if latest else None,
        "samples": samples,
        "career_urls": list(career_url_sources),
        "career_url_sources": career_url_sources,
        "child_sitemaps": list(dict.fromkeys(child_sitemaps)),
        "errors": errors,
    }


def _retryable_component_status(status: int) -> bool:
    return status in {403, 408, 425, 429} or status >= 500


def _component_retry_errors(summary: dict[str, Any]) -> list[str]:
    # Legacy cached errors have no typed flag; don't hide transport/server failures.
    errors = list(summary.get("retryable_errors", []))
    error = str(summary.get("error") or "")
    if summary.get("retryable_error") or (
        "retryable_error" not in summary and error and not re.search(
            r"HTTP (?:401|404|410)|Unsupported ATS|Feed contained no items|UnsafeTargetError",
            error, re.I,
        )
    ):
        errors.append(error)
    return [str(error) for error in errors if error]


async def _fetch_sitemap_summary(
    client: httpx.AsyncClient,
    observation: SiteObservation,
    *,
    max_sitemaps: int,
) -> dict[str, Any]:
    origin = _origin(observation)
    robots_url = f"{origin}/robots.txt"
    candidates: list[str] = []
    errors: list[str] = []
    retryable_errors: list[str] = []
    try:
        response, body = await _public_get(
            client,
            robots_url,
            max_bytes=250_000,
            accept="text/plain,*/*;q=0.1",
            allowed_content_types=("text/plain", "html"),
        )
        if 200 <= response.status_code < 400 and body:
            robots = body.decode("utf-8", errors="replace")
            candidates.extend(
                match.strip()
                for match in re.findall(r"^\s*Sitemap\s*:\s*(\S+)", robots, re.I | re.M)
            )
        elif _retryable_component_status(response.status_code):
            error = f"robots returned HTTP {response.status_code}"
            errors.append(error)
            retryable_errors.append(error)
    except (httpx.HTTPError, UnsafeTargetError, TimeoutError) as exc:
        error = f"robots: {type(exc).__name__}: {exc}"
        errors.append(error)
        if not isinstance(exc, UnsafeTargetError):
            retryable_errors.append(error)
    candidates.append(f"{origin}/sitemap.xml")
    queue = [
        candidate for candidate in dict.fromkeys(candidates) if _same_company_url(candidate, origin)
    ]
    documents: list[tuple[str, bytes]] = []
    checked: list[str] = []
    attempted: list[str] = []
    while queue and len(attempted) < max_sitemaps:
        url = queue.pop(0)
        if url.lower().endswith(".gz"):
            continue
        attempted.append(url)
        try:
            response, body = await _public_get(
                client,
                url,
                max_bytes=MAX_SITEMAP_BYTES,
                accept="application/xml,text/xml,text/plain;q=0.8,*/*;q=0.1",
                allowed_content_types=("xml", "text/plain", "octet-stream"),
            )
        except (httpx.HTTPError, UnsafeTargetError, TimeoutError) as exc:
            error = f"sitemap: {type(exc).__name__}: {exc}"
            errors.append(error)
            if not isinstance(exc, UnsafeTargetError):
                retryable_errors.append(error)
            continue
        if not 200 <= response.status_code < 400 or not body:
            if _retryable_component_status(response.status_code):
                error = f"sitemap {url} returned HTTP {response.status_code}"
                errors.append(error)
                retryable_errors.append(error)
            continue
        checked.append(str(response.url))
        if response.extensions.get("beings_body_truncated"):
            error = (f"Sitemap {response.url} exceeded the bounded read size; "
                     "coverage remains incomplete")
            errors.append(error)
            retryable_errors.append(error)
            continue
        documents.append((str(response.url), body))
        parsed = summarize_sitemap_documents([(str(response.url), body)])
        admitted_children = [
            child
            for child in parsed["child_sitemaps"]
            if _same_company_url(child, str(response.url))
            and child not in checked
            and child not in queue
        ]
        queue.extend(sorted(admitted_children, key=_career_sitemap_priority))
    summary = summarize_sitemap_documents(documents)
    summary["checked"] = checked
    summary["attempted"] = attempted
    summary["retryable_errors"] = [*retryable_errors, *summary["errors"]][:10]
    summary["errors"] = [*errors, *summary["errors"]][:10]
    return summary


def _plain_job_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _job_field(job: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        value: Any = job
        for part in path.split("."):
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(part)
        if value not in (None, "", [], {}):
            return value
    return None


def _job_date(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        # Lever publishes milliseconds since epoch.
        timestamp = float(value) / 1_000 if float(value) > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    return _parse_date(str(value)) if value else None


def _normalize_job_evidence(
    job: dict[str, Any], provider: str, fallback_url: str
) -> PublicJobEvidence | None:
    canonical = normalize_job_posting(job, provider=provider, fallback_url=fallback_url)
    if canonical is None or not canonical.current:
        return None
    rich = canonical.as_dict()
    title = _plain_job_text(_job_field(job, "title", "text"))
    if not title:
        return None
    description = _plain_job_text(
        _job_field(job, "content", "descriptionPlain", "description", "descriptionHtml")
    )
    location_value = _job_field(job, "location.name", "location", "categories.location")
    department_value = _job_field(
        job,
        "departments",
        "department",
        "categories.department",
        "team",
    )
    if isinstance(department_value, list):
        department_value = ", ".join(
            _plain_job_text(item.get("name") if isinstance(item, dict) else item)
            for item in department_value[:4]
        )
    source_url = (
        _plain_job_text(_job_field(job, "absolute_url", "hostedUrl", "jobUrl", "applyUrl"))
        or fallback_url
    )
    corpus = f"{title} {description}".lower()
    classification = classify_job_role(
        title,
        department=_plain_job_text(department_value),
        description=description,
    )
    role_families = list(classification.role_families)
    # Preserve the legacy compatibility projection for an already-creative role whose literal
    # posting body names photo/video work. The canonical frontier taxonomy remains stricter.
    if (
        classification.creative
        and "photo_video_production" not in role_families
        and any(term in corpus for term in PHOTO_VIDEO_JOB_WORDS)
    ):
        role_families.append("photo_video_production")
    compensation = bool(
        _job_field(
            job,
            "compensation",
            "compensationTierSummary",
            "salaryRange",
            "compensationTier",
        )
    )
    posted_at = _job_date(_job_field(job, "updated_at", "createdAt", "publishedAt", "published_at"))
    try:
        provider_name = provider if provider in {"greenhouse", "lever", "ashby"} else "greenhouse"
        return PublicJobEvidence(
            provider=provider_name,
            title=title[:300],
            source_url=(canonical.source_url or source_url)[:2_000],
            location=_plain_job_text(location_value)[:300],
            department=_plain_job_text(department_value)[:300],
            description_excerpt=description[:1_000],
            posted_at=posted_at,
            compensation_published=compensation or canonical.compensation_published,
            stable_id=canonical.stable_id,
            provider_id=canonical.provider_id,
            current=canonical.current,
            state_reason=canonical.state_reason,
            valid_through=canonical.valid_through,
            first_observed_at=canonical.first_observed_at,
            last_observed_at=canonical.last_observed_at,
            compensation_ranges=rich["compensation_ranges"],
            deliverable_expectations=rich["deliverable_expectations"],
            contract_signals=rich["contract_signals"],
            channels=rich["channels"],
            formats=rich["formats"],
            posted_time_basis=(
                "source_updated"
                if job.get("updated_at")
                else "source_explicit"
                if posted_at
                else "unknown"
            ),
            role_families=role_families,
            production_pressure_signals=[
                term for term in PRODUCTION_PRESSURE_TERMS if term in corpus
            ][:8],
            external_capacity_signals=[
                term for term in EXTERNAL_CAPACITY_JOB_TERMS if term in corpus
            ][:8],
            ai_workflow_signals=[term for term in AI_WORKFLOW_JOB_TERMS if term in corpus][:8],
            rights_risk_signals=[term for term in RIGHTS_RISK_JOB_TERMS if term in corpus][:8],
        )
    except ValueError:
        return None


def _job_summary(
    jobs: list[dict[str, Any]], *, provider: str = "greenhouse", fallback_url: str = ""
) -> dict[str, Any]:
    titles = [str(job.get("title") or job.get("text") or "").strip() for job in jobs]
    titles = [title for title in titles if title]
    job_postings = [
        posting
        for job in jobs
        if (posting := _normalize_job_evidence(job, provider, fallback_url)) is not None
    ]
    creative = sum("creative" in posting.role_families for posting in job_postings)
    photo_video = sum("photo_video_production" in posting.role_families for posting in job_postings)
    growth = sum("growth_marketing" in posting.role_families for posting in job_postings)
    leadership = sum("leadership" in posting.role_families for posting in job_postings)
    relevant = [
        posting.title
        for posting in job_postings
        if {"creative", "growth_marketing"} & set(posting.role_families)
    ]
    compensated = sum(posting.compensation_published for posting in job_postings)
    return {
        "open_jobs": len(jobs),
        "creative_jobs": creative,
        "photo_video_jobs": photo_video,
        "growth_jobs": growth,
        "leadership_jobs": leadership,
        "compensated_jobs": compensated,
        "job_titles": list(dict.fromkeys([*relevant, *titles]))[:20],
        "job_postings": [posting.model_dump(mode="json") for posting in job_postings[:30]],
    }


async def _fetch_ats(client: httpx.AsyncClient, provider: str, board: str) -> dict[str, Any]:
    safe_board = quote(board, safe="")
    if provider == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{safe_board}/jobs?content=true"
    elif provider == "lever":
        url = f"https://api.lever.co/v0/postings/{safe_board}?mode=json"
    elif provider == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{safe_board}?includeCompensation=true"
    else:
        return {"error": f"Unsupported ATS provider: {provider}"}
    response, body = await _public_get(
        client,
        url,
        max_bytes=MAX_ATS_BYTES,
        accept="application/json,*/*;q=0.1",
        allowed_content_types=("json", "text/plain"),
    )
    if not 200 <= response.status_code < 400 or not body:
        return {
            "error": f"{provider} returned HTTP {response.status_code}", "url": url,
            "retryable_error": _retryable_component_status(response.status_code) or (
                200 <= response.status_code < 400 and not body
            ),
        }
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return {
            "error": f"Invalid {provider} JSON: {exc}", "url": url,
            "retryable_error": True,
        }
    jobs = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(jobs, list):
        return {
            "error": f"Invalid {provider} jobs payload", "url": url,
            "retryable_error": True,
        }
    return {
        **_job_summary(
            [job for job in jobs if isinstance(job, dict)],
            provider=provider,
            fallback_url=url,
        ),
        "url": url,
    }


def _apply_sitemap(web: WebIntelligence, summary: dict[str, Any]) -> None:
    web.sitemap_total_urls = int(summary.get("total_urls", 0))
    web.sitemap_counts = {str(key): int(value) for key, value in summary.get("counts", {}).items()}
    web.sitemap_recent_30d = int(summary.get("recent_30d", 0))
    web.sitemap_recent_90d = int(summary.get("recent_90d", 0))
    web.sitemap_recent_counts = {
        str(key): int(value) for key, value in summary.get("recent_counts", {}).items()
    }
    web.sitemaps_checked = len(summary.get("checked", [])) if web.sitemap_total_urls > 0 else 0
    web.latest_sitemap_change = summary.get("latest_change")
    web.career_urls = list(
        dict.fromkeys(str(url) for url in summary.get("career_urls", []) if str(url))
    )[:MAX_CAREER_URLS]
    raw_career_sources = summary.get("career_url_sources", {})
    web.career_url_sources = {
        url: str(raw_career_sources[url])
        for url in web.career_urls
        if isinstance(raw_career_sources, dict) and raw_career_sources.get(url)
    }
    web.source_urls = list(
        dict.fromkeys([*web.source_urls, *summary.get("checked", []), *summary.get("samples", [])])
    )[:30]
    web.errors = list(dict.fromkeys([*web.errors, *summary.get("errors", [])]))[:15]


def _apply_ats(web: WebIntelligence, summaries: list[dict[str, Any]]) -> None:
    web.open_jobs = 0
    web.creative_jobs = 0
    web.photo_video_jobs = 0
    web.growth_jobs = 0
    web.leadership_jobs = 0
    web.job_titles = []
    web.job_postings = []
    web.sales.compensated_jobs = 0
    for summary in summaries:
        if summary.get("error"):
            web.errors.append(str(summary["error"]))
            continue
        web.open_jobs += int(summary.get("open_jobs", 0))
        web.creative_jobs += int(summary.get("creative_jobs", 0))
        web.photo_video_jobs += int(summary.get("photo_video_jobs", 0))
        web.growth_jobs += int(summary.get("growth_jobs", 0))
        web.leadership_jobs += int(summary.get("leadership_jobs", 0))
        web.sales.compensated_jobs += int(summary.get("compensated_jobs", 0))
        web.job_titles = list(dict.fromkeys([*web.job_titles, *summary.get("job_titles", [])]))[:20]
        for raw_posting in summary.get("job_postings", []):
            try:
                posting = PublicJobEvidence.model_validate(raw_posting)
            except (TypeError, ValueError):
                continue
            if posting.source_url not in {item.source_url for item in web.job_postings}:
                web.job_postings.append(posting)
        web.job_postings = web.job_postings[:30]
        if summary.get("url"):
            web.source_urls.append(str(summary["url"]))
    web.source_urls = list(dict.fromkeys(web.source_urls))[:30]
    web.errors = list(dict.fromkeys(web.errors))[:15]


async def _fetch_feed_summary(
    client: httpx.AsyncClient, observation: SiteObservation
) -> dict[str, Any]:
    candidates = list(observation.web.sales.feed_urls)
    if not candidates and "WordPress" in observation.technologies:
        candidates.append(f"{_origin(observation)}/feed/")
    if not candidates:
        return {"valid": False, "url": None, "attempted": 0}
    url = candidates[0]
    try:
        response, body = await _public_get(
            client,
            url,
            max_bytes=1_000_000,
            accept="application/rss+xml,application/atom+xml,application/xml,text/xml;q=0.8",
            allowed_content_types=("rss", "atom", "xml", "text/plain"),
        )
    except (httpx.HTTPError, UnsafeTargetError, TimeoutError) as exc:
        return {
            "valid": False,
            "url": url,
            "attempted": 1,
            "error": f"Feed: {type(exc).__name__}: {exc}",
        }
    if not 200 <= response.status_code < 400 or not body:
        return {
            "valid": False,
            "url": str(response.url),
            "attempted": 1,
            "error": (
                "Feed endpoint returned no readable XML payload"
                if 200 <= response.status_code < 400 and not body
                else f"Feed returned HTTP {response.status_code}"
            ),
            "retryable_error": _retryable_component_status(response.status_code) or (
                200 <= response.status_code < 400 and not body
            ),
        }
    if response.extensions.get("beings_body_truncated"):
        return {
            "valid": False,
            "url": str(response.url),
            "attempted": 1,
            "error": "Feed exceeded the bounded read size; coverage remains incomplete",
            "retryable_error": True,
            "truncated": True,
        }
    return {**parse_feed_document(str(response.url), body), "attempted": 1}


def _apply_feed(web: WebIntelligence, summary: dict[str, Any]) -> None:
    if summary.get("valid"):
        web.sales.feed_url = str(summary.get("url"))
        web.sales.feed_items = int(summary.get("items", 0))
        web.sales.feed_recent_30d = int(summary.get("recent_30d", 0))
        web.sales.feed_recent_90d = int(summary.get("recent_90d", 0))
        web.sales.feed_commercial_items = int(summary.get("commercial_items", 0))
        web.sales.feed_latest = summary.get("latest")
        web.sales.source_urls = list(
            dict.fromkeys(
                [
                    *web.sales.source_urls,
                    str(summary.get("url")),
                    *summary.get("item_urls", []),
                ]
            )
        )[:30]
    if summary.get("error"):
        web.sales.errors.append(str(summary["error"]))


def _shopify_product_json_url(url: str) -> str | None:
    parsed = urlparse(url)
    match = re.search(r"(?P<prefix>/[^?#]*?/)?products/(?P<handle>[^/?#]+)", parsed.path)
    if not match:
        return None
    product_path = parsed.path.rstrip("/")
    if not product_path.endswith(".js"):
        product_path += ".js"
    return urlunparse((parsed.scheme, parsed.netloc, product_path, "", "", ""))


async def _fetch_commerce_summary(
    client: httpx.AsyncClient, observation: SiteObservation
) -> dict[str, Any]:
    if "WooCommerce" in observation.technologies:
        url = f"{_origin(observation)}/wp-json/wc/store/v1/products?per_page=100"
        try:
            response, body = await _public_get(
                client,
                url,
                max_bytes=2_000_000,
                accept="application/json,*/*;q=0.1",
                allowed_content_types=("json", "text/plain"),
            )
            if not 200 <= response.status_code < 400 or not body:
                return {
                    "provider": "WooCommerce", "attempted": 1,
                    "error": (
                        "Commerce endpoint returned no readable JSON payload"
                        if 200 <= response.status_code < 400
                        else f"Commerce returned HTTP {response.status_code}"
                    ),
                    "retryable_error": _retryable_component_status(response.status_code) or (
                        200 <= response.status_code < 400 and not body
                    ),
                }
            payload = json.loads(body)
            if not isinstance(payload, list):
                raise ValueError("Invalid WooCommerce products payload")
            total_hint = int(response.headers.get("x-wp-total", 0) or 0)
            return {
                **summarize_woocommerce_products(payload, total_hint=total_hint),
                "url": str(response.url),
                "attempted": 1,
            }
        except (
            httpx.HTTPError,
            UnsafeTargetError,
            TimeoutError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            return {
                "provider": "WooCommerce",
                "attempted": 1,
                "error": f"Commerce: {type(exc).__name__}: {exc}",
            }
    if "Shopify" in observation.technologies:
        urls = list(
            dict.fromkeys(
                product_url
                for source_url in observation.web.source_urls
                if (product_url := _shopify_product_json_url(source_url))
            )
        )[:3]
        products: list[dict[str, Any]] = []
        checked: list[str] = []
        errors: list[str] = []
        retryable_errors: list[str] = []
        for url in urls:
            checked.append(url)
            try:
                response, body = await _public_get(
                    client,
                    url,
                    max_bytes=750_000,
                    accept="application/json,*/*;q=0.1",
                    allowed_content_types=("json", "javascript", "text/plain"),
                )
                if 200 <= response.status_code < 400 and body:
                    payload = json.loads(body)
                    if isinstance(payload, dict):
                        products.append(payload)
                    else:
                        raise ValueError("Invalid Shopify product payload")
                elif _retryable_component_status(response.status_code):
                    error = f"Shopify product returned HTTP {response.status_code}"
                    errors.append(error)
                    retryable_errors.append(error)
            except (
                httpx.HTTPError,
                UnsafeTargetError,
                TimeoutError,
                ValueError,
            ) as exc:
                error = f"Shopify product: {type(exc).__name__}: {exc}"
                errors.append(error)
                if not isinstance(exc, UnsafeTargetError):
                    retryable_errors.append(error)
        return {
            **summarize_shopify_products(products),
            "total_hint": max(len(products), observation.web.sitemap_counts.get("products", 0)),
            "url": checked[0] if checked else None,
            "checked": checked,
            "attempted": len(checked),
            "error": "; ".join(errors[:3]),
            "retryable_error": False,
            "retryable_errors": retryable_errors[:3],
        }
    return {"provider": None, "attempted": 0}


def _apply_commerce(web: WebIntelligence, summary: dict[str, Any]) -> None:
    sales = web.sales
    sales.catalog_sample_size = int(summary.get("sample_size", 0))
    sales.catalog_provider = str(summary.get("provider")) if sales.catalog_sample_size > 0 else None
    sales.catalog_total_hint = int(summary.get("total_hint", 0))
    sales.catalog_available_products = int(summary.get("available_products", 0))
    sales.catalog_variants = int(summary.get("variants", 0))
    sales.catalog_product_images = int(summary.get("product_images", 0))
    sales.catalog_median_price = float(summary.get("median_price", 0.0))
    sales.catalog_max_price = float(summary.get("max_price", 0.0))
    sales.catalog_currency = str(summary.get("currency", ""))[:8]
    urls = [*summary.get("checked", []), summary.get("url")]
    sales.source_urls = list(
        dict.fromkeys([*sales.source_urls, *(str(url) for url in urls if url)])
    )[:30]
    if summary.get("error"):
        sales.errors.append(str(summary["error"]))


async def enrich_site_owned_intelligence(
    client: httpx.AsyncClient,
    observation: SiteObservation,
    *,
    cache_get: CacheGet,
    cache_set: CacheSet,
    max_sitemaps: int = 4,
    retry_components: list[str] | None = None,
) -> dict[str, Any]:
    if not observation.live or not observation.final_url:
        return {"cache_hits": 0, "network_calls": 0}
    host = _host(observation)
    cache_hits = 0
    network_calls = 0
    selected = set(retry_components or ["*"])
    summaries: dict[str, dict[str, Any]] = {}
    def requested(component: str) -> bool:
        return "*" in selected or component in selected

    async def cached(kind, ttl, fetch, count):
        summary = cache_get(host, kind)
        if summary is not None:
            return summary, 1, 0
        summary = await fetch()
        cache_set(host, kind, summary, ttl)
        return summary, 0, count(summary)

    async def ats_work():
        entries = []
        # Preserve board order and cache reuse, including duplicate board keys.
        for provider, boards in observation.web.ats_boards.items():
            for board in boards[:2]:
                kind = f"ats:{provider}:{board}"
                if not requested(kind):
                    retained = observation.core_hydration_ats_receipts.get(kind)
                    if retained is not None:
                        entries.append((kind, retained, 0, 0, False))
                    continue

                async def fetch(provider=provider, board=board):
                    try:
                        return await _fetch_ats(client, provider, board)
                    except (httpx.HTTPError, UnsafeTargetError, TimeoutError) as exc:
                        return {"error": f"{provider}: {type(exc).__name__}: {exc}"}

                summary, hits, calls = await cached(kind, ATS_TTL_HOURS, fetch, lambda _: 1)
                entries.append((kind, summary, hits, calls, True))
        return entries

    tasks = {}
    # Fetchers read stable component inputs. Only this owner merges into the observation.
    if requested("sitemap"):
        tasks["sitemap"] = asyncio.create_task(cached(
            "sitemap", SITEMAP_TTL_HOURS,
            lambda: _fetch_sitemap_summary(client, observation, max_sitemaps=max(1, max_sitemaps)),
            lambda value: 1 + len(value.get("attempted", [])),
        ))
    tasks["ats"] = asyncio.create_task(ats_work())
    if requested("content_feed"):
        tasks["content_feed"] = asyncio.create_task(cached(
            "content_feed", FEED_TTL_HOURS,
            lambda: _fetch_feed_summary(client, observation),
            lambda value: int(value.get("attempted", 0)),
        ))
    vendor_observation = None
    if requested("vendors"):
        vendor_observation = observation.model_copy(deep=True)
        tasks["vendors"] = asyncio.create_task(enrich_vendor_intelligence(
            client, vendor_observation, cache_get=cache_get, cache_set=cache_set,
        ))
    try:
        if "sitemap" in tasks:
            sitemap, hits, calls = await tasks["sitemap"]
            cache_hits += hits
            network_calls += calls
            _apply_sitemap(observation.web, sitemap)
            summaries["sitemap"] = sitemap

        ats_summaries = []
        for kind, summary, hits, calls, fetched in await tasks["ats"]:
            ats_summaries.append(summary)
            cache_hits += hits
            network_calls += calls
            if fetched:
                summaries[kind] = summary
                observation.core_hydration_ats_receipts[kind] = summary
        if any(key.startswith("ats:") for key in summaries):
            _apply_ats(observation.web, ats_summaries)

        if "content_feed" in tasks:
            feed, hits, calls = await tasks["content_feed"]
            cache_hits += hits
            network_calls += calls
            _apply_feed(observation.web, feed)
            summaries["content_feed"] = feed

        # Shopify URL selection consumes the sitemap/ATS/feed merge above.
        if requested("commerce_catalog"):
            commerce, hits, calls = await cached(
                "commerce_catalog", COMMERCE_TTL_HOURS,
                lambda: _fetch_commerce_summary(client, observation),
                lambda value: int(value.get("attempted", 0)),
            )
            cache_hits += hits
            network_calls += calls
            _apply_commerce(observation.web, commerce)
            summaries["commerce_catalog"] = commerce

        if "vendors" in tasks:
            vendor_summary = await tasks["vendors"]
            cache_hits += vendor_summary["cache_hits"]
            network_calls += vendor_summary["network_calls"]
            observation.web.vendors = vendor_observation.web.vendors
            if observation.web.vendors.disclosure_url:
                observation.web.source_urls = list(dict.fromkeys([
                    *observation.web.source_urls, observation.web.vendors.disclosure_url,
                ]))[:30]
    finally:
        await cancel_and_drain(list(tasks.values()))
    observation.core_hydration_retry_components = [
        key for key, summary in summaries.items() if _component_retry_errors(summary)
    ][:20]
    observation.core_hydration_errors = list(dict.fromkeys(
        error
        for component in summaries.values()
        for error in _component_retry_errors(component)
    ))[:20]
    refresh_sales_intelligence(observation)
    return {
        "cache_hits": cache_hits,
        "network_calls": network_calls,
        "vendors": len(observation.web.vendors.vendors),
        "disclosures": int(bool(observation.web.vendors.disclosure_url)),
        "feeds": int(bool(observation.web.sales.feed_url)),
        "catalogs": int(bool(observation.web.sales.catalog_provider)),
        "retryable_errors": observation.core_hydration_errors,
    }


async def latest_common_crawl_index(client: httpx.AsyncClient) -> str:
    response, body = await _public_get(
        client,
        "https://index.commoncrawl.org/collinfo.json",
        max_bytes=500_000,
        accept="application/json,*/*;q=0.1",
        allowed_content_types=("json", "text/plain"),
    )
    if not 200 <= response.status_code < 400 or not body:
        raise httpx.HTTPStatusError(
            f"Common Crawl collection list returned {response.status_code}",
            request=response.request,
            response=response,
        )
    payload = json.loads(body)
    if not isinstance(payload, list) or not payload:
        raise ValueError("Common Crawl collection list was empty")
    return str(payload[0]["id"])


async def enrich_common_crawl(
    client: httpx.AsyncClient,
    observation: SiteObservation,
    *,
    collection: str,
    cache_get: CacheGet,
    cache_set: CacheSet,
) -> bool:
    if not observation.live:
        return False
    host = _host(observation)
    cached = cache_get(host, "common_crawl")
    if cached is None or cached.get("collection") != collection:
        url = httpx.URL(
            f"https://index.commoncrawl.org/{quote(collection, safe='-')}-index",
            params={
                "url": f"{host}/*",
                "output": "json",
                "filter": "status:200",
                "collapse": "urlkey",
                "limit": "200",
            },
        )
        try:
            response, body = await _public_get(
                client,
                str(url),
                max_bytes=MAX_COMMON_CRAWL_BYTES,
                accept="application/x-ndjson,application/json,text/plain,*/*;q=0.1",
                allowed_content_types=("json", "text/plain", "ndjson"),
            )
            rows = []
            if 200 <= response.status_code < 400:
                for line in body.decode("utf-8", errors="replace").splitlines()[:200]:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        rows.append(value)
            timestamps = [str(row.get("timestamp", "")) for row in rows if row.get("timestamp")]
            cached = {
                "pages": len(rows),
                "latest": max(timestamps) if timestamps else None,
                "collection": collection,
                "url": str(url),
            }
        except (httpx.HTTPError, UnsafeTargetError, TimeoutError) as exc:
            cached = {
                "pages": 0,
                "latest": None,
                "collection": collection,
                "error": f"Common Crawl: {type(exc).__name__}: {exc}",
            }
        cache_set(host, "common_crawl", cached, COMMON_CRAWL_TTL_HOURS)
        fresh = True
    else:
        fresh = False
    observation.web.common_crawl_pages = int(cached.get("pages", 0))
    observation.web.common_crawl_latest = cached.get("latest")
    observation.web.common_crawl_collection = cached.get("collection")
    if cached.get("url"):
        observation.web.source_urls = list(
            dict.fromkeys([*observation.web.source_urls, str(cached["url"])])
        )[:30]
    if cached.get("error"):
        observation.web.errors = list(
            dict.fromkeys([*observation.web.errors, str(cached["error"])])
        )[:15]
    return fresh
