from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
import tldextract
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .provider_rate_limit import provider_request

AdSurface = Literal["meta", "tiktok_library", "tiktok_top"]
EstimateKind = Literal[
    "meta_discovery",
    "meta_hydration",
    "tiktok_library",
    "tiktok_top",
]
ApifyTier = Literal["FREE", "BRONZE", "SILVER", "GOLD", "PLATINUM", "DIAMOND"]
IdentityState = Literal["verified", "probable", "ambiguous", "rejected"]

DEFAULT_MAX_TOTAL_CHARGE_USD = Decimal("100.00")
APIFY_BASE_URL = "https://api.apify.com/v2"
META_ACTOR_ID = "curious_coder~facebook-ads-library-scraper"
TIKTOK_LIBRARY_ACTOR_ID = "lexis-solutions~tiktok-ads-scraper"
TIKTOK_TOP_ACTOR_ID = "lexis-solutions~tiktok-top-ads-scraper"

_ACTOR_ID_RE = re.compile(
    r"^(?:[A-Za-z0-9_-]{4,100}|[A-Za-z0-9_.-]+~[A-Za-z0-9_.-]+)$"
)
_APIFY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,100}$")
_META_PAGE_ID_RE = re.compile(r"^[0-9]{3,40}$")
_SPACE_RE = re.compile(r"\s+")
_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "msclkid",
    "ttclid",
    "twclid",
    "utm_campaign",
    "utm_content",
    "utm_id",
    "utm_medium",
    "utm_source",
    "utm_term",
}
_MARKETPLACE_DOMAINS = frozenset(
    {
        "amazon.com",
        "apps.apple.com",
        "etsy.com",
        "play.google.com",
        "shop.app",
        "walmart.com",
    }
)
_BUSINESS_NAME_WORDS = frozenset(
    {
        "co",
        "company",
        "corp",
        "corporation",
        "inc",
        "incorporated",
        "limited",
        "llc",
        "ltd",
        "official",
        "plc",
    }
)

# The bundled PSL snapshot makes this deterministic and prevents a network fetch at import time.
_TLD_EXTRACT = tldextract.TLDExtract(
    cache_dir=None,
    suffix_list_urls=(),
    include_psl_private_domains=True,
)


class AdDiscoveryRequest(BaseModel):
    """One manually approved ad-discovery/hydration plan."""

    model_config = ConfigDict(extra="forbid")

    mission_id: str = Field(default="beings", min_length=1, max_length=100)
    mode: Literal["discover", "hydrate", "both"] = "discover"
    meta_enabled: bool = True
    meta_queries: list[str] = Field(default_factory=list, max_length=20)
    country: str = "US"
    active_only: bool = True
    meta_ads_per_query: int = Field(default=500, ge=1, le=500)
    max_new_advertisers: int = Field(default=500, ge=0, le=500)
    meta_ads_per_advertiser: int = Field(default=230, ge=1, le=500)
    existing_target_limit: int = Field(default=0, ge=0, le=500)
    tiktok_library_enabled: bool = False
    tiktok_ads_per_advertiser: int = Field(default=120, ge=1, le=120)
    tiktok_top_enabled: bool = False
    tiktok_top_ads_per_advertiser: int = Field(default=100, ge=1, le=100)
    verify_landing_domains: bool = True
    hydrate_websites: bool = True
    website_hydration_limit: int = Field(default=100, ge=0, le=100)
    max_total_charge_usd: Decimal = Field(
        default=DEFAULT_MAX_TOTAL_CHARGE_USD,
        ge=Decimal("0.10"),
        le=Decimal("100.00"),
        decimal_places=2,
    )

    @field_validator("meta_queries", mode="before")
    @classmethod
    def normalize_queries(cls, values: Any) -> Any:
        if not isinstance(values, list):
            return values
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values:
            if not isinstance(raw, str):
                normalized.append(raw)
                continue
            value = _SPACE_RE.sub(" ", raw).strip()
            key = value.casefold()
            if value and key not in seen:
                normalized.append(value)
                seen.add(key)
        return normalized

    @field_validator("country")
    @classmethod
    def normalize_country(cls, value: str) -> str:
        country = value.strip().upper()
        if country != "ALL" and not re.fullmatch(r"[A-Z]{2}", country):
            raise ValueError("country must be ALL or a two-letter ISO country code")
        return country

    @field_validator("max_total_charge_usd")
    @classmethod
    def normalize_charge_cap(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))

    @model_validator(mode="after")
    def validate_plan(self) -> AdDiscoveryRequest:
        if self.mode in {"discover", "both"}:
            if not self.meta_enabled:
                raise ValueError("discover mode requires Meta to be enabled")
            if not self.meta_queries:
                raise ValueError("discover mode requires at least one Meta query")
        if self.mode in {"hydrate", "both"} and self.existing_target_limit < 1:
            raise ValueError("hydrate mode requires at least one existing target")
        if not any(
            (self.meta_enabled, self.tiktok_library_enabled, self.tiktok_top_enabled)
        ):
            raise ValueError("at least one ad surface must be enabled")
        return self

    @property
    def discovery_query_count(self) -> int:
        return len(self.meta_queries) if self.mode in {"discover", "both"} else 0

    @property
    def existing_target_count(self) -> int:
        return self.existing_target_limit if self.mode in {"hydrate", "both"} else 0

    @property
    def broad_meta_sources(self) -> int:
        return self.discovery_query_count + self.existing_target_count

    @property
    def deep_advertiser_upper_bound(self) -> int:
        new = self.max_new_advertisers if self.mode in {"discover", "both"} else 0
        return new + self.existing_target_count

    def normalized_payload(self) -> dict[str, Any]:
        """Return a stable JSON-safe payload suitable for hashing and persistence."""

        return self.model_dump(mode="json")


class AdRateCard(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: AdSurface
    actor_id: str
    tier: ApifyTier = "FREE"
    result_price_usd: Decimal = Field(ge=0)
    actor_start_price_per_gb_usd: Decimal = Field(default=Decimal("0"), ge=0)
    default_memory_mbytes: int = Field(default=1024, ge=128)
    platform_usage_included: bool = True
    source: str = "Apify actor pricing metadata"

    @property
    def actor_start_events(self) -> int:
        return max(1, math.ceil(self.default_memory_mbytes / 1024))

    def start_cost(self, runs: int) -> Decimal:
        return self.actor_start_price_per_gb_usd * self.actor_start_events * runs


class AdCostEstimate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EstimateKind
    surface: AdSurface
    actor_id: str
    actor_runs: int = Field(ge=0)
    source_count: int = Field(ge=0)
    nominal_max_results: int = Field(ge=0)
    conservative_max_results: int = Field(ge=0)
    result_price_usd: Decimal = Field(ge=0)
    result_cost_usd: Decimal = Field(ge=0)
    start_cost_usd: Decimal = Field(ge=0)
    known_cost_usd: Decimal = Field(ge=0)
    variable_platform_usage: bool = False
    warnings: list[str] = Field(default_factory=list)


class AdScanQuote(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    quote_id: str
    request: AdDiscoveryRequest
    apify_tier: ApifyTier
    line_items: list[AdCostEstimate] = Field(default_factory=list)
    estimated_known_cost_usd: Decimal = Field(ge=0)
    maximum_charge_usd: Decimal = Field(ge=0)
    budget_limited: bool = False
    has_unpriced_platform_usage: bool = False
    warnings: list[str] = Field(default_factory=list)


_TOP_ADS_RESULT_PRICES: dict[ApifyTier, Decimal] = {
    "FREE": Decimal("0.00399"),
    "BRONZE": Decimal("0.00349"),
    "SILVER": Decimal("0.00299"),
    "GOLD": Decimal("0.00249"),
    "PLATINUM": Decimal("0.00249"),
    "DIAMOND": Decimal("0.00249"),
}


def default_rate_cards(tier: ApifyTier = "FREE") -> dict[AdSurface, AdRateCard]:
    """Return conservative current fallbacks; callers may supply freshly fetched cards."""

    return {
        "meta": AdRateCard(
            surface="meta",
            actor_id=META_ACTOR_ID,
            tier=tier,
            result_price_usd=Decimal("0.00075"),
            actor_start_price_per_gb_usd=Decimal("0.00005"),
            default_memory_mbytes=512,
        ),
        "tiktok_library": AdRateCard(
            surface="tiktok_library",
            actor_id=TIKTOK_LIBRARY_ACTOR_ID,
            tier=tier,
            result_price_usd=Decimal("0.00049"),
            actor_start_price_per_gb_usd=Decimal("0.00001"),
            default_memory_mbytes=1024,
            platform_usage_included=False,
        ),
        "tiktok_top": AdRateCard(
            surface="tiktok_top",
            actor_id=TIKTOK_TOP_ACTOR_ID,
            tier=tier,
            result_price_usd=_TOP_ADS_RESULT_PRICES[tier],
            actor_start_price_per_gb_usd=Decimal("0.0025"),
            default_memory_mbytes=4096,
        ),
    }


def _estimate_line(
    *,
    kind: EstimateKind,
    card: AdRateCard,
    runs: int,
    sources: int,
    nominal_results: int,
    conservative_results: int,
    warnings: Sequence[str] = (),
) -> AdCostEstimate:
    result_cost = card.result_price_usd * conservative_results
    start_cost = card.start_cost(runs)
    return AdCostEstimate(
        kind=kind,
        surface=card.surface,
        actor_id=card.actor_id,
        actor_runs=runs,
        source_count=sources,
        nominal_max_results=nominal_results,
        conservative_max_results=conservative_results,
        result_price_usd=card.result_price_usd,
        result_cost_usd=result_cost,
        start_cost_usd=start_cost,
        known_cost_usd=result_cost + start_cost,
        variable_platform_usage=not card.platform_usage_included,
        warnings=list(warnings),
    )


def estimate_ad_scan(
    request: AdDiscoveryRequest,
    *,
    tier: ApifyTier = "FREE",
    rate_cards: Mapping[AdSurface, AdRateCard] | None = None,
) -> AdScanQuote:
    """Estimate known charges without calling a provider or starting a paid run."""

    cards = dict(rate_cards or default_rate_cards(tier))
    missing = {"meta", "tiktok_library", "tiktok_top"} - cards.keys()
    if missing:
        raise ValueError(f"missing rate cards: {', '.join(sorted(missing))}")
    lines: list[AdCostEstimate] = []

    broad_meta_sources = request.broad_meta_sources
    if request.meta_enabled and broad_meta_sources:
        nominal = broad_meta_sources * request.meta_ads_per_query
        # The selected actor documents that per-source limits can overshoot by up to 30.
        conservative = broad_meta_sources * (request.meta_ads_per_query + 30)
        lines.append(
            _estimate_line(
                kind="meta_discovery",
                card=cards["meta"],
                runs=1,
                sources=broad_meta_sources,
                nominal_results=nominal,
                conservative_results=conservative,
                warnings=("Meta per-source limits may overshoot by up to 30 ads.",),
            )
        )

    advertiser_count = request.deep_advertiser_upper_bound
    if request.meta_enabled and advertiser_count:
        nominal = advertiser_count * request.meta_ads_per_advertiser
        conservative = advertiser_count * (request.meta_ads_per_advertiser + 30)
        lines.append(
            _estimate_line(
                kind="meta_hydration",
                card=cards["meta"],
                runs=1,
                sources=advertiser_count,
                nominal_results=nominal,
                conservative_results=conservative,
                warnings=("Meta per-source limits may overshoot by up to 30 ads.",),
            )
        )

    if request.tiktok_library_enabled and advertiser_count:
        pages_per_advertiser = math.ceil(request.tiktok_ads_per_advertiser / 12)
        conservative = advertiser_count * pages_per_advertiser * 12
        lines.append(
            _estimate_line(
                kind="tiktok_library",
                card=cards["tiktok_library"],
                runs=advertiser_count,
                sources=advertiser_count,
                nominal_results=advertiser_count * request.tiktok_ads_per_advertiser,
                conservative_results=conservative,
                warnings=(
                    "TikTok Library pagination returns up to 12 ads per page.",
                    "TikTok Library platform usage is billed separately from result events.",
                ),
            )
        )

    if request.tiktok_top_enabled and advertiser_count:
        lines.append(
            _estimate_line(
                kind="tiktok_top",
                card=cards["tiktok_top"],
                runs=advertiser_count,
                sources=advertiser_count,
                nominal_results=advertiser_count * request.tiktok_top_ads_per_advertiser,
                conservative_results=advertiser_count
                * request.tiktok_top_ads_per_advertiser,
                warnings=("TikTok Top Ads is curated positive-only evidence.",),
            )
        )

    known_cost = sum((line.known_cost_usd for line in lines), start=Decimal("0"))
    variable_usage = any(line.variable_platform_usage for line in lines)
    warnings = list(dict.fromkeys(warning for line in lines for warning in line.warnings))
    if variable_usage:
        warnings.append(
            "The estimate excludes variable platform usage; the provider hard cap remains binding."
        )
    budget_limited = known_cost > request.max_total_charge_usd
    if budget_limited:
        warnings.append("The configured hard cap is below the conservative known-cost estimate.")

    hash_payload = {
        "request": request.normalized_payload(),
        "tier": tier,
        "rate_cards": {
            key: cards[key].model_dump(mode="json") for key in sorted(cards)
        },
    }
    quote_id = hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    return AdScanQuote(
        quote_id=quote_id,
        request=request,
        apify_tier=tier,
        line_items=lines,
        estimated_known_cost_usd=known_cost,
        maximum_charge_usd=request.max_total_charge_usd,
        budget_limited=budget_limited,
        has_unpriced_platform_usage=variable_usage,
        warnings=warnings,
    )


def _meta_url(
    *,
    country: str,
    active_only: bool,
    media_type: str,
    search_type: str,
    query: str | None = None,
    page_id: str | None = None,
    publisher_platforms: Sequence[str] = (),
) -> str:
    normalized_country = country.strip().upper()
    if normalized_country != "ALL" and not re.fullmatch(r"[A-Z]{2}", normalized_country):
        raise ValueError("country must be ALL or a two-letter ISO country code")
    if media_type not in {"all", "image", "video", "meme", "none"}:
        raise ValueError("unsupported Meta media type")
    params: list[tuple[str, str]] = [
        ("active_status", "active" if active_only else "all"),
        ("ad_type", "all"),
        ("country", normalized_country),
        ("is_targeted_country", "false"),
        ("media_type", media_type),
        ("search_type", search_type),
    ]
    if query is not None:
        cleaned = _SPACE_RE.sub(" ", query).strip()
        if not cleaned:
            raise ValueError("Meta query cannot be blank")
        params.append(("q", cleaned))
    if page_id is not None:
        cleaned_page_id = page_id.strip()
        if not _META_PAGE_ID_RE.fullmatch(cleaned_page_id):
            raise ValueError("Meta Page ID must be numeric")
        params.append(("view_all_page_id", cleaned_page_id))
    for index, platform in enumerate(dict.fromkeys(p.lower() for p in publisher_platforms)):
        if platform not in {"facebook", "instagram", "messenger", "threads", "whatsapp"}:
            raise ValueError(f"unsupported Meta publisher platform: {platform}")
        params.append((f"publisher_platforms[{index}]", platform))
    return f"https://www.facebook.com/ads/library/?{urlencode(params)}"


def build_meta_search_url(
    query: str,
    *,
    country: str = "US",
    active_only: bool = True,
    media_type: str = "all",
    publisher_platforms: Sequence[str] = ("facebook", "instagram"),
) -> str:
    return _meta_url(
        query=query,
        country=country,
        active_only=active_only,
        media_type=media_type,
        search_type="keyword_unordered",
        publisher_platforms=publisher_platforms,
    )


def build_meta_page_url(
    page_id: str,
    *,
    country: str = "US",
    active_only: bool = True,
    media_type: str = "all",
    publisher_platforms: Sequence[str] = ("facebook", "instagram"),
) -> str:
    return _meta_url(
        page_id=page_id,
        country=country,
        active_only=active_only,
        media_type=media_type,
        search_type="page",
        publisher_platforms=publisher_platforms,
    )


def build_meta_actor_input(
    urls: Sequence[str],
    *,
    limit_per_source: int,
    scrape_ad_details: bool = False,
    run_tag: str = "",
    active_only: bool = True,
    country: str = "US",
) -> dict[str, Any]:
    if not 1 <= limit_per_source <= 500:
        raise ValueError("limit_per_source must be between 1 and 500")
    unique_urls = list(dict.fromkeys(url.strip() for url in urls if url.strip()))
    if not unique_urls:
        raise ValueError("at least one Meta URL is required")
    for url in unique_urls:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "facebook.com",
            "www.facebook.com",
        }:
            raise ValueError("Meta actor inputs must be HTTPS facebook.com URLs")
    normalized_country = country.strip().upper()
    if normalized_country != "ALL" and not re.fullmatch(r"[A-Z]{2}", normalized_country):
        raise ValueError("country must be ALL or a two-letter ISO country code")
    payload: dict[str, Any] = {
        "urls": [{"url": url} for url in unique_urls],
        "scrapeAdDetails": scrape_ad_details,
        "limitPerSource": limit_per_source,
        "count": len(unique_urls) * limit_per_source,
        "scrapePageAds.activeStatus": "active" if active_only else "all",
        "scrapePageAds.countryCode": normalized_country,
    }
    if run_tag.strip():
        payload["runTag"] = run_tag.strip()[:100]
    return payload


def _clean_host(value: str) -> str:
    raw = value.strip().rstrip(".").lower()
    if not raw:
        return ""
    if "://" in raw or raw.startswith("//"):
        raw = urlparse(raw if "://" in raw else f"https:{raw}").hostname or ""
    else:
        raw = raw.split("/", 1)[0].split(":", 1)[0]
    try:
        return raw.encode("idna").decode("ascii").removeprefix("www.")
    except UnicodeError:
        return ""


def registrable_domain(value: str) -> str:
    """Return the PSL registrable domain for a URL or hostname."""

    host = _clean_host(value)
    if not host:
        return ""
    try:
        import ipaddress

        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return ""
    extracted = _TLD_EXTRACT(host)
    if extracted.domain and extracted.suffix:
        return f"{extracted.domain}.{extracted.suffix}".lower()
    return ""


def domains_match(left: str, right: str) -> bool:
    left_domain = registrable_domain(left)
    right_domain = registrable_domain(right)
    return bool(left_domain and left_domain == right_domain)


def normalize_landing_url(value: str) -> str:
    raw = value.strip().strip("<>")
    if not raw:
        return ""
    redirect_hosts = {"l.facebook.com", "lm.facebook.com", "l.instagram.com"}
    for _ in range(4):
        parsed = urlparse(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username or parsed.password:
            return ""
        if parsed.hostname.lower() not in redirect_hosts:
            break
        redirect_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        target = redirect_params.get("u", "").strip().strip("<>")
        if not target or target == raw:
            return ""
        raw = target
    else:
        return ""

    host = _clean_host(parsed.hostname)
    if not host:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    scheme = parsed.scheme.lower()
    if port is not None and (
        (scheme == "http" and port != 80) or (scheme == "https" and port != 443)
    ):
        return ""
    query = urlencode(
        [
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_QUERY_KEYS
        ]
    )
    return urlunparse((scheme, host, parsed.path or "/", "", query, ""))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [item for item in value if isinstance(item, str)]
    return []


def _text(value: Any) -> str:
    if isinstance(value, str):
        return _SPACE_RE.sub(" ", value).strip()
    if isinstance(value, Mapping):
        return _text(value.get("text"))
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    return ""


def _first_text(*values: Any) -> str:
    return next((text for value in values if (text := _text(value))), "")


def _nested(value: Mapping[str, Any], *path: str) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _unique_urls(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, Mapping):
            raw = _first_text(
                value.get("linkUrl"),
                value.get("link_url"),
                value.get("url"),
                value.get("href"),
            )
        else:
            raw = _text(value)
        normalized = normalize_landing_url(raw)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def extract_meta_landing_urls(item: Mapping[str, Any]) -> list[str]:
    snapshot = _mapping(item.get("snapshot"))
    cards = snapshot.get("cards") if isinstance(snapshot.get("cards"), list) else []
    extra_links = (
        snapshot.get("extraLinks") if isinstance(snapshot.get("extraLinks"), list) else []
    )
    return _unique_urls(
        [
            item.get("linkUrl"),
            item.get("link_url"),
            snapshot.get("linkUrl"),
            snapshot.get("link_url"),
            *cards,
            *extra_links,
        ]
    )


def extract_tiktok_landing_urls(item: Mapping[str, Any]) -> list[str]:
    return _unique_urls(
        [
            item.get("adLandingUrl"),
            item.get("adLandingPageUrl"),
            item.get("landingPage"),
            item.get("landing_page"),
        ]
    )


def landing_domains(urls: Sequence[str]) -> list[str]:
    return list(
        dict.fromkeys(domain for url in urls if (domain := registrable_domain(url)))
    )


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        timestamp = float(value)
        if timestamp > 100_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError:
            for date_format in ("%b %d, %Y", "%B %d, %Y"):
                try:
                    return datetime.strptime(cleaned, date_format).replace(tzinfo=UTC)
                except ValueError:
                    continue
            try:
                return _parse_datetime(float(cleaned))
            except ValueError:
                return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _first_datetime(*values: Any) -> datetime | None:
    return next((parsed for value in values if (parsed := _parse_datetime(value))), None)


def _active_state(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = _text(value).casefold().replace("_", "-")
    if normalized in {"active", "running", "true"}:
        return True
    if normalized in {"ended", "inactive", "removed", "unavailable", "false"}:
        return False
    return None


def _normalize_handle(value: str) -> str:
    return value.strip().strip("@/").casefold()


def social_handle_from_url(platform: Literal["meta", "instagram", "tiktok"], url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.lower().removeprefix("www.")
    parts = [part for part in parsed.path.split("/") if part]
    if platform == "meta":
        if host not in {"facebook.com", "fb.com"}:
            return ""
        if parsed.path.rstrip("/") == "/profile.php":
            return _normalize_handle(dict(parse_qsl(parsed.query)).get("id", ""))
        blocked = {"ads", "groups", "marketplace", "pages", "share", "watch"}
        return _normalize_handle(parts[0]) if parts and parts[0].lower() not in blocked else ""
    if platform == "instagram":
        if host != "instagram.com":
            return ""
        blocked = {"accounts", "direct", "explore", "p", "reel", "reels", "stories"}
        return _normalize_handle(parts[0]) if parts and parts[0].lower() not in blocked else ""
    if host not in {"tiktok.com", "m.tiktok.com"}:
        return ""
    return _normalize_handle(parts[0]) if parts and parts[0].startswith("@") else ""


class NormalizedAdvertiser(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: Literal["meta", "tiktok"]
    external_id: str = ""
    name: str = ""
    handle: str = ""
    profile_url: str | None = None
    linked_instagram_handle: str = ""
    categories: list[str] = Field(default_factory=list)


class NormalizedAd(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: Literal["meta", "tiktok"]
    surface: Literal["meta_library", "tiktok_library", "tiktok_top"]
    external_id: str
    advertiser: NormalizedAdvertiser
    is_active: bool | None = None
    status: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    publisher_platforms: list[str] = Field(default_factory=list)
    landing_urls: list[str] = Field(default_factory=list)
    landing_domains: list[str] = Field(default_factory=list)
    body: str = ""
    headline: str = ""
    description: str = ""
    cta: str = ""
    media_type: str = ""
    media_urls: list[str] = Field(default_factory=list)
    collation_id: str = ""
    collation_count: int | None = None
    creative_fingerprint: str
    impressions: str = ""
    spend: str = ""
    contains_digitally_created_media: bool | None = None
    source_payload_hash: str


def _payload_hash(item: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(item, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _stable_media_identity(url: str) -> str:
    parsed = urlparse(url)
    return f"{(parsed.hostname or '').lower()}{parsed.path}".casefold()


def creative_fingerprint(
    *,
    platform: str,
    collation_id: str = "",
    body: str = "",
    headline: str = "",
    description: str = "",
    cta: str = "",
    landing_domains: Sequence[str] = (),
    media_type: str = "",
    media_urls: Sequence[str] = (),
    external_id: str = "",
) -> str:
    if collation_id.strip():
        material = f"{platform.casefold()}|collation|{collation_id.strip()}"
    else:
        values = [
            platform,
            body,
            headline,
            description,
            cta,
            media_type,
            "|".join(sorted(set(domain.casefold() for domain in landing_domains))),
            "|".join(sorted(set(_stable_media_identity(url) for url in media_urls))),
        ]
        normalized_values = [
            _SPACE_RE.sub(" ", value).strip().casefold() for value in values
        ]
        if not any(normalized_values[1:]) and external_id.strip():
            material = f"{platform.casefold()}|ad|{external_id.strip()}"
        else:
            material = "|".join(normalized_values)
    return hashlib.sha256(material.encode()).hexdigest()


def _meta_advertiser(item: Mapping[str, Any], snapshot: Mapping[str, Any]) -> NormalizedAdvertiser:
    page_info = _mapping(
        _nested(item, "ad_details", "advertiser", "ad_library_page_info", "page_info")
    )
    profile_url = _first_text(
        snapshot.get("pageProfileUri"),
        snapshot.get("page_profile_uri"),
        page_info.get("page_profile_uri"),
        _nested(item, "pageInfo", "page", "url"),
    )
    page_id = _first_text(
        item.get("pageID"),
        item.get("pageId"),
        item.get("page_id"),
        snapshot.get("pageId"),
        snapshot.get("page_id"),
        page_info.get("page_id"),
        _nested(item, "pageInfo", "page", "id"),
    )
    name = _first_text(
        item.get("pageName"),
        item.get("page_name"),
        snapshot.get("pageName"),
        snapshot.get("page_name"),
        snapshot.get("currentPageName"),
        snapshot.get("current_page_name"),
        page_info.get("page_name"),
        _nested(item, "pageInfo", "page", "name"),
    )
    category_values = (
        snapshot.get("pageCategories")
        or snapshot.get("page_categories")
        or item.get("pageCategories")
        or item.get("page_categories")
    )
    categories = [value for value in _strings(category_values) if value.strip()]
    return NormalizedAdvertiser(
        platform="meta",
        external_id=page_id,
        name=name,
        handle=social_handle_from_url("meta", profile_url),
        profile_url=profile_url or None,
        linked_instagram_handle=_normalize_handle(_first_text(page_info.get("ig_username"))),
        categories=list(dict.fromkeys(categories)),
    )


def normalize_meta_ad(item: Mapping[str, Any]) -> NormalizedAd | None:
    ad_id = _first_text(
        item.get("adArchiveID"),
        item.get("adArchiveId"),
        item.get("ad_archive_id"),
        item.get("adId"),
        item.get("id"),
    )
    if not ad_id:
        return None
    snapshot = _mapping(item.get("snapshot"))
    advertiser = _meta_advertiser(item, snapshot)
    urls = extract_meta_landing_urls(item)
    domains = landing_domains(urls)
    cards = snapshot.get("cards") if isinstance(snapshot.get("cards"), list) else []
    body = _first_text(
        snapshot.get("body"),
        *(card.get("body") for card in cards if isinstance(card, Mapping)),
    )
    headline = _first_text(
        snapshot.get("title"),
        *(card.get("title") for card in cards if isinstance(card, Mapping)),
    )
    description = _first_text(
        snapshot.get("linkDescription"),
        snapshot.get("link_description"),
        *(
            _first_text(card.get("linkDescription"), card.get("link_description"))
            for card in cards
            if isinstance(card, Mapping)
        ),
    )
    cta = _first_text(
        snapshot.get("ctaText"),
        snapshot.get("cta_text"),
        snapshot.get("ctaType"),
        snapshot.get("cta_type"),
    )
    videos = snapshot.get("videos") if isinstance(snapshot.get("videos"), list) else []
    images = snapshot.get("images") if isinstance(snapshot.get("images"), list) else []
    media_values: list[Any] = []
    for record in [*videos, *images, *cards]:
        if not isinstance(record, Mapping):
            continue
        media_values.extend(
            record.get(key)
            for key in (
                "videoHdUrl",
                "videoSdUrl",
                "videoUrl",
                "originalImageUrl",
                "resizedImageUrl",
                "imageUrl",
                "video_hd_url",
                "video_sd_url",
                "video_url",
                "video_preview_image_url",
                "original_image_url",
                "resized_image_url",
                "image_url",
            )
        )
    media_urls = _unique_urls(media_values)
    media_type = "video" if videos or any("video" in url for url in media_urls) else (
        "image"
        if images or media_urls
        else _first_text(snapshot.get("displayFormat"), snapshot.get("display_format"))
    )
    collation_id = _first_text(item.get("collationId"), item.get("collation_id"))
    raw_active = item.get("isActive", item.get("is_active"))
    active = _active_state(raw_active)
    platforms = _strings(item.get("publisherPlatform") or item.get("publisher_platform"))
    fingerprint = creative_fingerprint(
        platform="meta",
        collation_id=collation_id,
        body=body,
        headline=headline,
        description=description,
        cta=cta,
        landing_domains=domains,
        media_type=media_type,
        media_urls=media_urls,
        external_id=ad_id,
    )
    collation_count = item.get("collationCount", item.get("collation_count"))
    try:
        parsed_collation_count = int(collation_count) if collation_count is not None else None
    except (TypeError, ValueError):
        parsed_collation_count = None
    raw_generated_media = item.get(
        "containsDigitalCreatedMedia",
        item.get("contains_digital_created_media"),
    )
    return NormalizedAd(
        platform="meta",
        surface="meta_library",
        external_id=ad_id,
        advertiser=advertiser,
        is_active=active,
        status="active" if active is True else "inactive" if active is False else "",
        started_at=_first_datetime(
            item.get("startDateFormatted"),
            item.get("startDate"),
            item.get("start_date"),
        ),
        ended_at=_first_datetime(
            item.get("endDateFormatted"),
            item.get("endDate"),
            item.get("end_date"),
        ),
        publisher_platforms=list(dict.fromkeys(value.upper() for value in platforms)),
        landing_urls=urls,
        landing_domains=domains,
        body=body,
        headline=headline,
        description=description,
        cta=cta,
        media_type=media_type,
        media_urls=media_urls,
        collation_id=collation_id,
        collation_count=parsed_collation_count,
        creative_fingerprint=fingerprint,
        impressions=_first_text(
            _nested(item, "impressionsWithIndex", "impressionsText"),
            item.get("impressions"),
        ),
        spend=_first_text(item.get("spend")),
        contains_digitally_created_media=(
            raw_generated_media if isinstance(raw_generated_media, bool) else None
        ),
        source_payload_hash=_payload_hash(item),
    )


def _tiktok_advertiser(item: Mapping[str, Any]) -> NormalizedAdvertiser:
    user = _mapping(item.get("advertiserTtUser") or item.get("advertiserTtUserId"))
    handle = _normalize_handle(_first_text(user.get("username")))
    profile_url = _first_text(user.get("profile_web_link"), user.get("profileUrl"))
    return NormalizedAdvertiser(
        platform="tiktok",
        external_id=_first_text(item.get("advertiserId"), item.get("advertiser_id")),
        name=_first_text(item.get("advertiserName"), item.get("advertiser_name")),
        handle=handle,
        profile_url=profile_url or None,
    )


def normalize_tiktok_ad(item: Mapping[str, Any]) -> NormalizedAd | None:
    ad_id = _first_text(item.get("adId"), item.get("ad_id"), item.get("id"))
    if not ad_id:
        return None
    advertiser = _tiktok_advertiser(item)
    urls = extract_tiktok_landing_urls(item)
    domains = landing_domains(urls)
    status = _first_text(item.get("status"))
    media_urls = _unique_urls(
        [
            item.get("adVideoUrl"),
            item.get("adVideoCover"),
            *_strings(item.get("adImageUrls")),
        ]
    )
    media_type = {"1": "text", "2": "video", "3": "image"}.get(
        _first_text(item.get("adType"), item.get("ad_type")),
        "",
    )
    headline = _first_text(item.get("adTitle"), item.get("title"))
    fingerprint = creative_fingerprint(
        platform="tiktok",
        body="",
        headline=headline,
        landing_domains=domains,
        media_type=media_type,
        media_urls=media_urls,
        external_id=ad_id,
    )
    return NormalizedAd(
        platform="tiktok",
        surface="tiktok_library",
        external_id=ad_id,
        advertiser=advertiser,
        is_active=_active_state(status),
        status=status,
        started_at=_first_datetime(item.get("adStartDate"), item.get("startDate")),
        ended_at=_first_datetime(item.get("adEndDate"), item.get("endDate")),
        publisher_platforms=["TIKTOK"],
        landing_urls=urls,
        landing_domains=domains,
        headline=headline,
        media_type=media_type,
        media_urls=media_urls,
        creative_fingerprint=fingerprint,
        impressions=_first_text(item.get("adImpressions")),
        spend=_first_text(item.get("adSpent")),
        source_payload_hash=_payload_hash(item),
    )


def normalize_tiktok_top_ad(item: Mapping[str, Any]) -> NormalizedAd | None:
    ad_id = _first_text(item.get("id"), item.get("adId"))
    if not ad_id:
        return None
    urls = extract_tiktok_landing_urls(item)
    domains = landing_domains(urls)
    video_urls = _mapping(item.get("videoUrls"))
    media_urls = _unique_urls([item.get("videoCover"), *video_urls.values()])
    brand_name = _first_text(item.get("brandName"))
    title = _first_text(item.get("title"))
    normalized_brand = _normalized_company_name(brand_name) or brand_name.casefold()
    advertiser_material = "|".join(
        [normalized_brand, domains[0] if domains else ""]
    ).strip("|")
    if not advertiser_material:
        advertiser_material = f"ad:{ad_id}"
    advertiser_digest = hashlib.sha256(advertiser_material.encode()).hexdigest()[:24]
    advertiser = NormalizedAdvertiser(
        platform="tiktok",
        external_id=f"top:{advertiser_digest}",
        name=brand_name,
    )
    fingerprint = creative_fingerprint(
        platform="tiktok",
        headline=title,
        landing_domains=domains,
        media_type="video" if media_urls else "",
        media_urls=media_urls,
        external_id=ad_id,
    )
    return NormalizedAd(
        platform="tiktok",
        surface="tiktok_top",
        external_id=ad_id,
        advertiser=advertiser,
        status="curated",
        publisher_platforms=["TIKTOK"],
        landing_urls=urls,
        landing_domains=domains,
        headline=title,
        media_type="video" if media_urls else "",
        media_urls=media_urls,
        creative_fingerprint=fingerprint,
        source_payload_hash=_payload_hash(item),
    )


def _normalized_company_name(value: str) -> str:
    words = re.findall(r"[a-z0-9]+", value.casefold())
    return " ".join(word for word in words if word not in _BUSINESS_NAME_WORDS)


def _expected_handles(
    first_party_handles: Mapping[str, str | Sequence[str]], platform: str
) -> set[str]:
    values = first_party_handles.get(platform, ())
    if isinstance(values, str):
        values = [values]
    return {_normalize_handle(value) for value in values if _normalize_handle(value)}


class IdentityAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: IdentityState
    confidence: int = Field(ge=0, le=100)
    domain_match: bool = False
    handle_match: bool = False
    name_match: bool = False
    reasons: list[str] = Field(default_factory=list)


def assess_advertiser_identity(
    *,
    canonical_domain: str,
    advertiser: NormalizedAdvertiser,
    observed_landing_domains: Sequence[str],
    candidate_name: str = "",
    first_party_handles: Mapping[str, str | Sequence[str]] | None = None,
    conflicting_identity: bool = False,
    marketplace_domains: Sequence[str] = tuple(_MARKETPLACE_DOMAINS),
) -> IdentityAssessment:
    """Apply deterministic, conservative identity rules; name-only never verifies."""

    handles = first_party_handles or {}
    expected = (
        _expected_handles(handles, "meta") | _expected_handles(handles, "facebook")
        if advertiser.platform == "meta"
        else _expected_handles(handles, "tiktok")
    )
    handle = _normalize_handle(advertiser.handle)
    handle_match = bool(handle and handle in expected)
    if advertiser.platform == "meta":
        instagram = _normalize_handle(advertiser.linked_instagram_handle)
        handle_match = handle_match or bool(
            instagram and instagram in _expected_handles(handles, "instagram")
        )
    explicit_handle_conflict = bool(expected and handle and handle not in expected)
    canonical = registrable_domain(canonical_domain)
    observed = {registrable_domain(value) for value in observed_landing_domains}
    observed.discard("")
    domain_match = bool(canonical and canonical in observed)
    name_match = bool(
        candidate_name
        and advertiser.name
        and _normalized_company_name(candidate_name)
        and _normalized_company_name(candidate_name)
        == _normalized_company_name(advertiser.name)
    )
    marketplace = {registrable_domain(value) for value in marketplace_domains}
    marketplace_only = bool(observed) and observed <= marketplace

    if conflicting_identity or explicit_handle_conflict:
        reasons = ["A first-party identity conflicts with the provider advertiser."]
        if domain_match:
            reasons.append("The landing domain agrees, but the handle conflict blocks scoring.")
        return IdentityAssessment(
            state="rejected",
            confidence=0,
            domain_match=domain_match,
            handle_match=False,
            name_match=name_match,
            reasons=reasons,
        )
    if domain_match and handle_match:
        return IdentityAssessment(
            state="verified",
            confidence=100,
            domain_match=True,
            handle_match=True,
            name_match=name_match,
            reasons=["First-party handle and landing domain both agree."],
        )
    if domain_match and name_match:
        return IdentityAssessment(
            state="verified",
            confidence=90,
            domain_match=True,
            handle_match=False,
            name_match=True,
            reasons=["Landing domain and normalized advertiser name agree."],
        )
    if handle_match:
        reason = (
            "First-party handle agrees and the landing destination is an approved marketplace."
            if marketplace_only
            else "First-party handle agrees; landing-domain verification is unavailable."
        )
        return IdentityAssessment(
            state="verified",
            confidence=80,
            domain_match=False,
            handle_match=True,
            name_match=name_match,
            reasons=[reason],
        )
    if domain_match:
        return IdentityAssessment(
            state="probable",
            confidence=75,
            domain_match=True,
            handle_match=False,
            name_match=name_match,
            reasons=[
                "Landing domain agrees, but no first-party handle or exact name corroborates it."
            ],
        )
    if name_match:
        return IdentityAssessment(
            state="probable",
            confidence=55,
            name_match=True,
            reasons=[
                "Only the normalized advertiser name agrees; name-only evidence cannot score."
            ],
        )
    return IdentityAssessment(
        state="ambiguous",
        confidence=0,
        reasons=["No verified first-party handle or landing-domain agreement was found."],
    )


class ApifyRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    status: str
    default_dataset_id: str | None = None
    actor_id: str | None = None
    build_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    usage_total_usd: Decimal | None = None
    status_message: str = ""

    @property
    def terminal(self) -> bool:
        return self.status.upper() in {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}

    @property
    def succeeded(self) -> bool:
        return self.status.upper() == "SUCCEEDED"

    @classmethod
    def from_api_payload(cls, payload: Mapping[str, Any]) -> ApifyRun:
        if not isinstance(payload, Mapping):
            raise ValueError("Apify run response was not an object")
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
        run_id = _first_text(data.get("id"))
        status = _first_text(data.get("status"))
        if not run_id or not status:
            raise ValueError("Apify response omitted run id or status")
        raw_usage = data.get("usageTotalUsd", data.get("usageUsd"))
        try:
            usage = Decimal(str(raw_usage)) if raw_usage is not None else None
        except (ArithmeticError, TypeError, ValueError):
            usage = None
        if usage is not None and not usage.is_finite():
            usage = None
        # PAY_PER_EVENT totals may lag terminal status. Event usage is the
        # provider's monetary receipt, not a local rate-card estimate.
        event_usage = data.get("eventUsage")
        if isinstance(event_usage, Mapping) and event_usage:
            try:
                event_totals = [Decimal(str(event["eventTotalUsd"]))
                                for event in event_usage.values()]
                if all(total.is_finite() and total >= 0 for total in event_totals):
                    usage = max(usage or Decimal(0), sum(event_totals, Decimal(0)))
            except (ArithmeticError, KeyError, TypeError, ValueError):
                pass
        pricing = _mapping(data.get("pricingInfo"))
        counts = data.get("chargedEventCounts")
        events = _mapping(_mapping(pricing.get("pricingPerEvent")).get("actorChargeEvents"))
        if pricing.get("pricingModel") == "PAY_PER_EVENT" and isinstance(counts, Mapping):
            try:
                charged = Decimal(0)
                for event, count in counts.items():
                    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                        raise ValueError("Invalid provider charged event count")
                    price = Decimal(str(events[event]["eventPriceUsd"]))
                    if not price.is_finite() or price < 0:
                        raise ValueError("Invalid provider event price")
                    charged += count * price
                if counts:
                    usage = max(usage or Decimal(0), charged)
            except (ArithmeticError, KeyError, TypeError, ValueError):
                pass
        return cls(
            id=run_id,
            status=status.upper(),
            default_dataset_id=_first_text(data.get("defaultDatasetId")) or None,
            actor_id=_first_text(data.get("actId")) or None,
            build_id=_first_text(data.get("buildId")) or None,
            started_at=_parse_datetime(data.get("startedAt")),
            finished_at=_parse_datetime(data.get("finishedAt")),
            usage_total_usd=usage,
            status_message=_first_text(data.get("statusMessage")),
        )


class ApifyHTTPAdapter:
    """Small async Apify transport. It never retries or launches replacement runs."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = APIFY_BASE_URL,
        timeout_seconds: float = 30.0,
    ) -> None:
        cleaned = token.strip()
        if not cleaned or any(character.isspace() for character in cleaned):
            raise ValueError("Apify token must be non-empty and contain no whitespace")
        self._token = cleaned
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
        )

    async def __aenter__(self) -> ApifyHTTPAdapter:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _provider_request(self, method: str, url: str, **kwargs) -> httpx.Response:
        return await provider_request(self._client, method, url, provider="apify", **kwargs)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    @staticmethod
    def _validate_actor_id(actor_id: str) -> str:
        if not _ACTOR_ID_RE.fullmatch(actor_id):
            raise ValueError("invalid Apify actor id")
        return actor_id

    @staticmethod
    def _validate_resource_id(value: str, label: str) -> str:
        if not _APIFY_ID_RE.fullmatch(value):
            raise ValueError(f"invalid Apify {label}")
        return value

    async def start_actor(
        self,
        actor_id: str,
        actor_input: Mapping[str, Any],
        *,
        max_items: int,
        max_total_charge_usd: Decimal,
        build: str | None = None,
        timeout_seconds: int | None = None,
    ) -> ApifyRun:
        """Start exactly one run. Transient errors are returned to the caller, never retried."""

        actor = self._validate_actor_id(actor_id)
        if max_items < 1:
            raise ValueError("max_items must be positive")
        try:
            charge = Decimal(str(max_total_charge_usd))
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ValueError("max_total_charge_usd must be a finite decimal") from exc
        if not charge.is_finite() or charge <= 0:
            raise ValueError("max_total_charge_usd must be positive")
        params: dict[str, Any] = {
            "maxItems": max_items,
            "maxTotalChargeUsd": format(charge, "f"),
            "restartOnError": "false",
        }
        if build:
            params["build"] = build
        if timeout_seconds is not None:
            if timeout_seconds < 1:
                raise ValueError("timeout_seconds must be positive")
            params["timeout"] = timeout_seconds
        response = await self._provider_request("POST",
            f"/actors/{actor}/runs",
            params=params,
            json=dict(actor_input),
            headers=self._headers,
        )
        response.raise_for_status()
        return ApifyRun.from_api_payload(response.json())

    async def poll_run(self, run_id: str, *, wait_for_finish: int = 0) -> ApifyRun:
        run = self._validate_resource_id(run_id, "run id")
        if not 0 <= wait_for_finish <= 60:
            raise ValueError("wait_for_finish must be between 0 and 60 seconds")
        response = await self._provider_request("GET",
            f"/actor-runs/{run}",
            params={"waitForFinish": wait_for_finish},
            headers=self._headers,
        )
        response.raise_for_status()
        return ApifyRun.from_api_payload(response.json())

    async def wait_for_run(
        self,
        run_id: str,
        *,
        poll_seconds: float = 2.0,
        timeout_seconds: float = 3600.0,
    ) -> ApifyRun:
        if poll_seconds < 0 or timeout_seconds <= 0:
            raise ValueError("poll and timeout values must be positive")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"Apify run {run_id} did not finish in time")
            current = await self.poll_run(
                run_id,
                wait_for_finish=min(60, max(0, int(remaining))),
            )
            if current.terminal:
                return current
            if poll_seconds:
                await asyncio.sleep(min(poll_seconds, remaining))

    async def iter_dataset_items(
        self,
        dataset_id: str,
        *,
        page_size: int = 1000,
        maximum_items: int | None = None,
    ) -> AsyncIterator[Mapping[str, Any]]:
        dataset = self._validate_resource_id(dataset_id, "dataset id")
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size must be between 1 and 1000")
        if maximum_items is not None and maximum_items < 0:
            raise ValueError("maximum_items cannot be negative")
        offset = 0
        emitted = 0
        while maximum_items is None or emitted < maximum_items:
            limit = page_size
            if maximum_items is not None:
                limit = min(limit, maximum_items - emitted)
            if limit <= 0:
                break
            response = await self._provider_request("GET",
                f"/datasets/{dataset}/items",
                params={
                    "clean": "true",
                    "format": "json",
                    "offset": offset,
                    "limit": limit,
                },
                headers=self._headers,
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, Mapping):
                data = _mapping(payload.get("data"))
                raw_items = data.get("items", payload.get("items", []))
            else:
                raw_items = payload
            if not isinstance(raw_items, list):
                raise ValueError("Apify dataset response was not a list")
            items = [item for item in raw_items if isinstance(item, Mapping)]
            for item in items:
                yield item
                emitted += 1
                if maximum_items is not None and emitted >= maximum_items:
                    break
            offset += len(raw_items)
            if len(raw_items) < limit or not raw_items:
                break

    async def fetch_dataset_items(
        self,
        dataset_id: str,
        *,
        page_size: int = 1000,
        maximum_items: int | None = None,
    ) -> list[Mapping[str, Any]]:
        return [
            item
            async for item in self.iter_dataset_items(
                dataset_id,
                page_size=page_size,
                maximum_items=maximum_items,
            )
        ]

    async def abort_run(self, run_id: str) -> ApifyRun:
        run = self._validate_resource_id(run_id, "run id")
        response = await self._provider_request("POST",
            f"/actor-runs/{run}/abort",
            headers=self._headers,
        )
        response.raise_for_status()
        return ApifyRun.from_api_payload(response.json())
