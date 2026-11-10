from __future__ import annotations

import json
import re
import statistics
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .inspect import UnsafeTargetError, _bounded_get
from .models import SiteObservation
from .provider_cooldown import (
    GDELT_HOST,
    active_provider_cooldown,
    record_provider_cooldown,
    retry_after_timestamp,
)

FEED_TTL_HOURS = 12
COMMERCE_TTL_HOURS = 24
NEWS_TTL_HOURS = 24
MAX_FEED_ITEMS = 300
MAX_NEWS_ARTICLES = 25
GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"

COMMERCIAL_TRIGGER_WORDS = (
    "acquires",
    "acquisition",
    "appoints",
    "campaign",
    "collaboration",
    "collection",
    "expands",
    "expansion",
    "funding",
    "launch",
    "opens",
    "partnership",
    "raises",
    "rebrand",
    "releases",
    "retail",
)

GENERIC_COMPANY_NAMES = {
    "about",
    "contact",
    "home",
    "official site",
    "shop",
    "store",
    "welcome",
}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse_public_date(value: object) -> datetime | None:
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
            match = re.match(r"(\d{8})T?(\d{6})?", text)
            if not match:
                return None
            try:
                result = datetime.strptime(
                    f"{match.group(1)}{match.group(2) or '000000'}", "%Y%m%d%H%M%S"
                ).replace(tzinfo=UTC)
            except ValueError:
                return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _entry_text(entry: ET.Element, names: tuple[str, ...]) -> str:
    for child in entry.iter():
        if _local_name(child.tag) in names and child.text:
            return child.text.strip()
    return ""


def parse_feed_document(
    url: str, body: bytes, *, now: datetime | None = None
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    xml_repaired = False
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        # Escape only bare ampersands; retain CDATA/comments and strict XML validation.
        parts = re.split(rb"(<!\[CDATA\[.*?\]\]>|<!--.*?-->)", body, flags=re.S)
        repaired = b"".join(
            part if index % 2 else re.sub(
                rb"&(?!amp;|lt;|gt;|apos;|quot;|#[0-9]+;|#x[0-9a-fA-F]+;)",
                b"&amp;", part,
            )
            for index, part in enumerate(parts)
        )
        try:
            if repaired == body:
                raise exc
            root = ET.fromstring(repaired)
            xml_repaired = True
        except ET.ParseError:
            return {"url": url, "valid": False, "error": f"Invalid feed XML: {exc}"}
    entries = [
        node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}
    ][:MAX_FEED_ITEMS]
    if not entries:
        return {"url": url, "valid": False, "error": "Feed contained no items"}
    recent_30d = 0
    recent_90d = 0
    commercial = 0
    latest: datetime | None = None
    titles: list[str] = []
    item_urls: list[str] = []
    for entry in entries:
        title = _entry_text(entry, ("title",))
        if title:
            titles.append(re.sub(r"\s+", " ", title)[:300])
            commercial += int(
                any(word in title.lower() for word in COMMERCIAL_TRIGGER_WORDS)
            )
        published = _parse_public_date(
            _entry_text(entry, ("published", "updated", "pubdate", "date"))
        )
        if published:
            latest = max(latest, published) if latest else published
            age = current - published
            recent_30d += int(timedelta(0) <= age <= timedelta(days=30))
            recent_90d += int(timedelta(0) <= age <= timedelta(days=90))
        link = ""
        for child in entry.iter():
            if _local_name(child.tag) != "link":
                continue
            link = str(child.attrib.get("href") or child.text or "").strip()
            if link:
                break
        if link.startswith(("http://", "https://")):
            item_urls.append(link)
    return {
        "url": url,
        "valid": True,
        "xml_repaired": xml_repaired,
        "items": len(entries),
        "recent_30d": recent_30d,
        "recent_90d": recent_90d,
        "commercial_items": commercial,
        "latest": latest.isoformat() if latest else None,
        "titles": list(dict.fromkeys(titles))[:20],
        "item_urls": list(dict.fromkeys(item_urls))[:10],
    }


def _safe_price(value: object, *, divisor: int = 1) -> float | None:
    try:
        result = float(str(value).replace(",", "")) / divisor
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if not 0 <= result <= 10_000_000:
        return None
    return round(result, 2)


def summarize_woocommerce_products(
    payload: object, *, total_hint: int = 0
) -> dict[str, Any]:
    products = payload if isinstance(payload, list) else []
    prices: list[float] = []
    currency = ""
    variants = 0
    images = 0
    available = 0
    for product in products[:100]:
        if not isinstance(product, dict):
            continue
        price_data = product.get("prices", {})
        if isinstance(price_data, dict):
            minor = max(0, min(int(price_data.get("currency_minor_unit", 2) or 2), 6))
            price = _safe_price(price_data.get("price"), divisor=10**minor)
            if price is not None:
                prices.append(price)
            currency = currency or str(price_data.get("currency_code", ""))[:8]
        variations = product.get("variations", [])
        variants += len(variations) if isinstance(variations, list) else 0
        product_images = product.get("images", [])
        images += len(product_images) if isinstance(product_images, list) else 0
        available += int(
            bool(product.get("is_purchasable", True))
            and bool(product.get("is_in_stock", True))
        )
    return {
        "provider": "WooCommerce",
        "sample_size": len(products[:100]),
        "total_hint": max(total_hint, len(products[:100])),
        "available_products": available,
        "variants": variants,
        "product_images": images,
        "median_price": round(statistics.median(prices), 2) if prices else 0.0,
        "max_price": max(prices, default=0.0),
        "currency": currency,
    }


def summarize_shopify_products(products: list[dict[str, Any]]) -> dict[str, Any]:
    prices: list[float] = []
    variants = 0
    images = 0
    available = 0
    valid_products = [product for product in products if isinstance(product, dict)][:3]
    for product in valid_products:
        product_variants = product.get("variants", [])
        product_variants = product_variants if isinstance(product_variants, list) else []
        variants += len(product_variants)
        for variant in product_variants:
            if not isinstance(variant, dict):
                continue
            price = _safe_price(variant.get("price"), divisor=100)
            if price is not None:
                prices.append(price)
            available += int(bool(variant.get("available", True)))
        product_images = product.get("images", [])
        images += len(product_images) if isinstance(product_images, list) else 0
    return {
        "provider": "Shopify",
        "sample_size": len(valid_products),
        "total_hint": len(valid_products),
        "available_products": available,
        "variants": variants,
        "product_images": images,
        "median_price": round(statistics.median(prices), 2) if prices else 0.0,
        "max_price": max(prices, default=0.0),
        "currency": "",
    }


def _scale(value: int | float, thresholds: tuple[tuple[int | float, int], ...]) -> int:
    for minimum, points in thresholds:
        if value >= minimum:
            return points
    return 0


def refresh_sales_intelligence(observation: SiteObservation) -> None:
    web = observation.web
    sales = web.sales
    creative = _scale(
        sales.image_assets, ((60, 24), (30, 18), (10, 10), (1, 3))
    )
    creative += _scale(
        sales.video_assets + sales.embedded_video_players,
        ((8, 30), (4, 22), (2, 14), (1, 8)),
    )
    creative += min(len(sales.creative_technologies) * 7, 24)
    creative += min(sales.feed_commercial_items * 4, 14)
    creative += min(web.creative_jobs * 5, 15)

    paid = min(len(sales.growth_technologies) * 9, 54)
    paid += min(
        sum(bool(observation.public_ids.get(key)) for key in (
            "google_tag",
            "meta_pixel",
            "tiktok_pixel",
            "linkedin_partner",
        ))
        * 10,
        32,
    )
    paid += min(web.vendors.creative_commerce_score // 5, 20)

    catalog_size = max(sales.catalog_total_hint, web.sitemap_counts.get("products", 0))
    commerce = _scale(
        catalog_size, ((1_000, 40), (250, 32), (75, 24), (20, 14), (1, 5))
    )
    commerce += _scale(
        sales.catalog_sample_size, ((75, 15), (25, 10), (5, 6), (1, 3))
    )
    commerce += _scale(
        sales.catalog_product_images,
        ((300, 20), (100, 14), (30, 9), (5, 4)),
    )
    commerce += _scale(
        web.max_review_count, ((1_000, 20), (250, 14), (50, 8), (10, 4))
    )
    commerce += _scale(
        sales.catalog_median_price, ((500, 10), (150, 7), (50, 4), (1, 2))
    )

    capacity = min(web.vendors.stack_score // 3, 30)
    capacity += min(
        (len(sales.growth_technologies) + len(sales.creative_technologies)) * 4,
        28,
    )
    capacity += _scale(web.open_jobs, ((50, 20), (20, 14), (5, 8), (1, 3)))
    capacity += min(sales.compensated_jobs * 3, 12)
    capacity += min(len(sales.asset_hosts) * 2, 8)
    capacity += 8 if web.vendors.bimi_published else 0
    capacity += 4 if web.vendors.bimi_verified_mark else 0

    timing = _scale(
        sales.feed_recent_30d, ((20, 28), (8, 20), (3, 13), (1, 6))
    )
    timing += min(sales.feed_commercial_items * 5, 25)
    timing += min(sales.news_commercial_triggers * 10, 30)
    timing += min(sales.news_articles * 2, 10)
    timing += _scale(web.sitemap_recent_30d, ((30, 20), (10, 14), (3, 8), (1, 4)))
    timing += min((web.creative_jobs + web.growth_jobs) * 3, 15)

    sales.creative_demand_score = min(creative, 100)
    sales.paid_growth_score = min(paid, 100)
    sales.commerce_scale_score = min(commerce, 100)
    sales.operating_capacity_score = min(capacity, 100)
    sales.timing_score = min(timing, 100)
    sales.overall_score = round(
        sales.creative_demand_score * 0.30
        + sales.paid_growth_score * 0.25
        + sales.commerce_scale_score * 0.20
        + sales.operating_capacity_score * 0.15
        + sales.timing_score * 0.10
    )

    reasons: list[str] = []
    if sales.video_assets or sales.embedded_video_players:
        reasons.append(
            f"{sales.video_assets + sales.embedded_video_players} video assets or players "
            "across inspected pages"
        )
    if sales.image_assets:
        reasons.append(f"{sales.image_assets} image assets across inspected pages")
    if sales.growth_technologies:
        reasons.append(
            "Paid-growth stack: " + ", ".join(sales.growth_technologies[:6])
        )
    if sales.creative_technologies:
        reasons.append(
            "Creative delivery stack: " + ", ".join(sales.creative_technologies[:6])
        )
    if sales.catalog_sample_size:
        price = (
            f", median {sales.catalog_currency + ' ' if sales.catalog_currency else ''}"
            f"{sales.catalog_median_price:,.2f}"
            if sales.catalog_median_price
            else ""
        )
        reasons.append(
            f"{sales.catalog_provider} exposed {sales.catalog_sample_size} public product "
            f"samples with {sales.catalog_product_images} images{price}"
        )
    if sales.feed_recent_30d:
        reasons.append(
            f"First-party feed published {sales.feed_recent_30d} items in 30 days, "
            f"including {sales.feed_commercial_items} commercial triggers"
        )
    if sales.news_commercial_triggers:
        reasons.append(
            f"{sales.news_commercial_triggers} recent commercial news triggers from "
            f"{sales.news_articles} matched articles"
        )
    if web.vendors.bimi_published:
        reasons.append(
            "BIMI brand-email program detected"
            + (" with verified mark evidence" if web.vendors.bimi_verified_mark else "")
        )
    sales.reasons = reasons[:12]
    sales.source_urls = list(
        dict.fromkeys(
            [
                *sales.source_urls,
                *(item for item in (sales.feed_url, web.vendors.bimi_evidence_url) if item),
                *sales.news_urls,
            ]
        )
    )[:30]
    sales.errors = list(dict.fromkeys(sales.errors))[:15]


def company_news_query(observation: SiteObservation) -> str:
    title = re.sub(r"\s+", " ", observation.title).strip()
    for separator in (" | ", " — ", " – ", " :: ", " · "):
        if separator in title:
            title = title.split(separator, 1)[0].strip()
    title = re.sub(r"\b(?:official site|homepage)\b", "", title, flags=re.I).strip(" -|")
    # GDELT wraps the value in an exact-phrase query, so remove characters that
    # can escape or reshape that phrase before it reaches the public endpoint.
    title = re.sub(r'["()\\\x00-\x1f]', " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    if (
        3 <= len(title) <= 80
        and title.lower() not in GENERIC_COMPANY_NAMES
        and len(title.split()) <= 10
    ):
        return title
    host = (observation.canonical_host or observation.hostname).removeprefix("www.")
    label = host.split(".", 1)[0].replace("-", " ").strip()
    return label if len(label) >= 4 else ""


async def fetch_gdelt_news(
    client: httpx.AsyncClient, query: str
) -> dict[str, Any]:
    url = httpx.URL(
        GDELT_DOC_API,
        params={
            "query": f'"{query}"',
            "mode": "ArtList",
            "maxrecords": str(MAX_NEWS_ARTICLES),
            "timespan": "3months",
            "sort": "DateDesc",
            "format": "json",
        },
    )
    try:
        response, body = await _bounded_get(
            client,
            str(url),
            max_bytes=1_500_000,
            accept="application/json,text/plain;q=0.5",
            allowed_content_types=("json", "text/plain"),
        )
    except (httpx.HTTPError, UnsafeTargetError, TimeoutError) as exc:
        return {"query": query, "error": f"GDELT: {type(exc).__name__}: {exc}"}
    if not 200 <= response.status_code < 400 or not body:
        return {
            "query": query, "error": f"GDELT returned HTTP {response.status_code}",
            **({"retry_after": retry_after_timestamp(response.headers)}
               if response.status_code == 429 else {}),
        }
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return {"query": query, "error": f"Invalid GDELT JSON: {exc}"}
    raw_articles = payload.get("articles", []) if isinstance(payload, dict) else []
    articles: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_articles[:MAX_NEWS_ARTICLES]:
        if not isinstance(item, dict):
            continue
        article_url = str(item.get("url", "")).strip()
        title = re.sub(r"\s+", " ", str(item.get("title", ""))).strip()
        if not article_url.startswith(("http://", "https://")) or article_url in seen:
            continue
        seen.add(article_url)
        articles.append(
            {
                "url": article_url,
                "title": title[:500],
                "seen_date": str(item.get("seendate", ""))[:40],
                "domain": str(item.get("domain", ""))[:200],
            }
        )
    trigger_articles = [
        item
        for item in articles
        if any(word in item["title"].lower() for word in COMMERCIAL_TRIGGER_WORDS)
    ]
    dates = [
        parsed
        for parsed in (_parse_public_date(item["seen_date"]) for item in articles)
        if parsed
    ]
    return {
        "query": query,
        "articles": articles,
        "article_count": len(articles),
        "commercial_triggers": len(trigger_articles),
        "latest": max(dates).isoformat() if dates else None,
        "titles": [item["title"] for item in trigger_articles[:10]],
        "urls": [item["url"] for item in trigger_articles[:10] or articles[:5]],
    }


async def enrich_news_intelligence(
    client: httpx.AsyncClient,
    observation: SiteObservation,
    *,
    cache_get,
    cache_set,
) -> dict[str, Any]:
    query = company_news_query(observation)
    if not query:
        return {"checked": False, "fresh": False, "error": "No stable company name"}
    host = (observation.canonical_host or observation.hostname).removeprefix("www.").lower()
    cached = cache_get(host, "gdelt_news")
    fresh = cached is None or cached.get("query") != query
    if fresh:
        retry_after = active_provider_cooldown(cache_get, GDELT_HOST)
        if retry_after:
            return {
                "checked": False, "fresh": False, "deferred": True,
                "retry_after": retry_after,
                "error": f"GDELT requests deferred after HTTP 429 until {retry_after}",
            }
        cached = await fetch_gdelt_news(client, query)
        if cached.get("retry_after"):
            record_provider_cooldown(cache_set, GDELT_HOST, cached["retry_after"])
        cache_set(host, "gdelt_news", cached, NEWS_TTL_HOURS)
    sales = observation.web.sales
    sales.news_query = query
    sales.news_articles = int(cached.get("article_count", 0))
    sales.news_commercial_triggers = int(cached.get("commercial_triggers", 0))
    sales.news_latest = cached.get("latest")
    sales.news_titles = [str(value)[:500] for value in cached.get("titles", [])[:10]]
    sales.news_urls = [str(value)[:2_000] for value in cached.get("urls", [])[:10]]
    if cached.get("error"):
        sales.errors.append(str(cached["error"]))
    refresh_sales_intelligence(observation)
    return {
        "checked": True,
        "fresh": fresh,
        "articles": sales.news_articles,
        "triggers": sales.news_commercial_triggers,
        "error": cached.get("error"),
    }
