from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import USER_AGENT
from .models import DiscoveredHost
from .provider_rate_limit import provider_request

CacheGet = Callable[[str, str], dict[str, Any] | None]
CacheSet = Callable[[str, str, dict[str, Any], int], None]

OSM_ENDPOINT = "https://overpass-api.de/api/interpreter"
WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
AGENCY_DISCOVERY_TTL_HOURS = 24 * 30
MAX_RESPONSE_BYTES = 2_000_000
MAX_SOURCE_RESULTS = 250

HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)

# A rotation intentionally uses metropolitan bounds rather than country-wide Overpass
# requests. It keeps each public query cheap while covering the most agency-dense markets.
METROS: tuple[tuple[str, str, float, float, float, float], ...] = (
    ("new-york", "New York", 40.4774, -74.2591, 40.9176, -73.7004),
    ("los-angeles", "Los Angeles", 33.7037, -118.6682, 34.3373, -118.1553),
    ("london", "London", 51.2868, -0.5104, 51.6919, 0.3340),
    ("toronto", "Toronto", 43.5810, -79.6393, 43.8555, -79.1155),
    ("chicago", "Chicago", 41.6445, -87.9401, 42.0230, -87.5237),
    ("san-francisco", "San Francisco Bay", 37.1894, -122.6500, 38.1550, -121.8000),
    ("miami", "Miami", 25.5516, -80.4800, 26.1004, -80.0300),
    ("austin", "Austin", 30.0987, -97.9384, 30.5169, -97.5615),
    ("atlanta", "Atlanta", 33.4960, -84.5511, 34.0150, -84.1850),
    ("amsterdam", "Amsterdam", 52.2782, 4.7288, 52.4312, 5.0792),
    ("berlin", "Berlin", 52.3383, 13.0884, 52.6755, 13.7611),
    ("paris", "Paris", 48.8156, 2.2241, 48.9022, 2.4699),
    ("sydney", "Sydney", -34.1183, 150.5209, -33.5781, 151.3430),
    ("melbourne", "Melbourne", -38.4339, 144.5937, -37.5113, 145.5125),
    ("singapore", "Singapore", 1.1496, 103.5940, 1.4784, 104.0945),
    ("mexico-city", "Mexico City", 19.0482, -99.3649, 19.5928, -98.9403),
)

WIKIDATA_TYPES: tuple[str, ...] = (
    "Q216931",  # advertising agency
    "Q860572",  # photo agency
    "Q1762059",  # film production company
    "Q11396960",  # production company
)
WIKIDATA_LABELS = {
    "Q216931": "advertising agency",
    "Q860572": "photo agency",
    "Q1762059": "film production company",
    "Q11396960": "production company",
}
WIKIDATA_RANKS = {
    "Q216931": 28.0,
    "Q1762059": 27.0,
    "Q860572": 26.0,
    "Q11396960": 25.0,
}

BLOCKED_HOSTS = {
    # Social profiles are useful evidence, but they are not an official company website.
    "behance.net",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "pinterest.com",
    "tiktok.com",
    "twitter.com",
    "vimeo.com",
    "x.com",
    "youtube.com",
    "youtu.be",
    # Directories and maps should not be mistaken for the listed company's site.
    "aicp.com",
    "clutch.co",
    "designrush.com",
    "goodfirms.co",
    "google.com",
    "mapquest.com",
    "openstreetmap.org",
    "productionhub.com",
    "themanifest.com",
    "wikidata.org",
    "wikipedia.org",
    "yelp.com",
    "yellowpages.com",
    # Link shorteners and generic infrastructure cannot establish company identity.
    "amazonaws.com",
    "azurewebsites.net",
    "bit.ly",
    "cloudfront.net",
    "github.io",
    "linktr.ee",
    "netlify.app",
    "notion.site",
    "pages.dev",
    "sites.google.com",
    "squarespace.com",
    "vercel.app",
    "webflow.io",
    "wixsite.com",
    "wordpress.com",
    "workers.dev",
}


@dataclass(frozen=True, slots=True)
class AgencyDiscoveryResult:
    candidates: list[DiscoveredHost]
    errors: list[str]
    osm_metro: str
    wikidata_page: int
    network_calls: int
    cache_hits: int


def _blocked_host(hostname: str) -> bool:
    return any(hostname == host or hostname.endswith(f".{host}") for host in BLOCKED_HOSTS)


def canonicalize_public_website(value: str) -> str | None:
    """Return a public, non-directory hostname from an explicitly supplied website URL."""

    raw = value.strip().strip("<>")
    if not raw or raw.lower().startswith(("mailto:", "tel:", "javascript:")):
        return None
    if raw.startswith("//"):
        raw = f"https:{raw}"
    elif "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    hostname = (parsed.hostname or "").strip().rstrip(".").lower()
    if parsed.scheme not in {"http", "https"} or not hostname:
        return None
    if parsed.username or parsed.password:
        return None
    try:
        # Accessing .port validates malformed values such as ':not-a-port'.
        _ = parsed.port
    except ValueError:
        return None
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return None
    hostname = hostname.removeprefix("www.")
    if not HOST_RE.fullmatch(hostname):
        return None
    if hostname.rsplit(".", 1)[-1].isdigit():
        return None
    if hostname.endswith((".local", ".localhost", ".test", ".invalid", ".example")):
        return None
    if _blocked_host(hostname):
        return None
    return hostname


def _website_values(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    # Semicolon-separated website tags are conventional in OSM. Avoid splitting on
    # whitespace because valid URLs can contain encoded or unencoded query text.
    return [part.strip() for part in value.split(";") if part.strip()]


def _clean_label(value: Any, fallback: str) -> str:
    label = re.sub(r"\s+", " ", str(value or "")).strip()
    if not label or re.fullmatch(r"Q\d+", label):
        return fallback
    return label[:140]


def _ranked_unique(candidates: list[DiscoveredHost], limit: int) -> list[DiscoveredHost]:
    unique: dict[str, DiscoveredHost] = {}
    for candidate in candidates:
        current = unique.get(candidate.hostname)
        if current is None:
            unique[candidate.hostname] = candidate
            continue
        winner = candidate if candidate.discovery_rank > current.discovery_rank else current
        unique[candidate.hostname] = winner.model_copy(
            update={
                "discovery_evidence_urls": list(
                    dict.fromkeys(
                        [
                            *current.discovery_evidence_urls,
                            *candidate.discovery_evidence_urls,
                        ]
                    )
                )[:12],
                "provider_record_ids": list(
                    dict.fromkeys(
                        [*current.provider_record_ids, *candidate.provider_record_ids]
                    )
                )[:12],
            }
        )
    ranked = sorted(unique.values(), key=lambda item: (-item.discovery_rank, item.hostname))
    return ranked[: max(0, min(limit, MAX_SOURCE_RESULTS))]


def build_overpass_query(rotation: int = 0, *, result_limit: int = 200) -> tuple[str, str]:
    """Build one bounded metro query; identical rotations always select the same metro."""

    if rotation < 0:
        raise ValueError("rotation must be non-negative")
    slug, _label, south, west, north, east = METROS[rotation % len(METROS)]
    limit = max(1, min(result_limit, MAX_SOURCE_RESULTS))
    bounds = f"{south:.4f},{west:.4f},{north:.4f},{east:.4f}"
    query = (
        "[out:json][timeout:20];\n"
        "(\n"
        f'  nwr["office"="advertising_agency"]({bounds});\n'
        f'  nwr["craft"="photographer"]({bounds});\n'
        f'  nwr["amenity"="studio"]["studio"="video"]({bounds});\n'
        ");\n"
        f"out tags center qt {limit};"
    )
    return slug, query


def _osm_category(tags: Mapping[str, Any]) -> tuple[str, float] | None:
    if tags.get("office") == "advertising_agency":
        return "advertising agency", 27.0
    if tags.get("amenity") == "studio" and tags.get("studio") == "video":
        return "video studio", 26.0
    if tags.get("craft") == "photographer":
        return "photographer", 21.0
    return None


def parse_overpass_payload(
    payload: Mapping[str, Any],
    *,
    metro_label: str = "",
    limit: int = MAX_SOURCE_RESULTS,
) -> list[DiscoveredHost]:
    """Parse public OSM features that explicitly include a usable official website."""

    elements = payload.get("elements", [])
    if not isinstance(elements, list):
        return []
    candidates: list[DiscoveredHost] = []
    for element in elements:
        if not isinstance(element, Mapping):
            continue
        tags = element.get("tags", {})
        if not isinstance(tags, Mapping):
            continue
        category = _osm_category(tags)
        if category is None:
            continue
        category_label, base_rank = category
        raw_websites: list[str] = []
        for key in ("website", "contact:website", "url", "contact:url"):
            raw_websites.extend(_website_values(tags.get(key)))
        for raw_website in raw_websites:
            hostname = canonicalize_public_website(raw_website)
            if hostname is None:
                continue
            name = _clean_label(tags.get("name:en") or tags.get("name"), hostname)
            details = [name, category_label]
            if metro_label:
                details.append(metro_label)
            rank = base_rank
            rank += 0.75 if name != hostname else 0.0
            rank += 0.25 if tags.get("email") or tags.get("contact:email") else 0.0
            rank += 0.25 if tags.get("phone") or tags.get("contact:phone") else 0.0
            candidates.append(
                DiscoveredHost(
                    source_apex="openstreetmap.org",
                    hostname=hostname,
                    discovery_rank=round(rank, 3),
                    label_hint=" · ".join(details),
                    discovery_evidence_urls=(
                        [
                            "https://www.openstreetmap.org/"
                            f"{element.get('type')}/{element.get('id')}"
                        ]
                        if element.get("type") in {"node", "way", "relation"}
                        and element.get("id") is not None
                        else []
                    ),
                    provider_record_ids=(
                        [f"osm:{element.get('type')}:{element.get('id')}"]
                        if element.get("type") in {"node", "way", "relation"}
                        and element.get("id") is not None
                        else []
                    ),
                )
            )
    return _ranked_unique(candidates, limit)


def build_wikidata_query(page: int = 0, *, page_size: int = 100) -> str:
    """Build a stable page across the four relevant Wikidata organization classes."""

    if page < 0:
        raise ValueError("page must be non-negative")
    size = max(1, min(page_size, 100))
    offset = page * size
    values = " ".join(f"wd:{item}" for item in WIKIDATA_TYPES)
    return f"""
SELECT DISTINCT ?item ?itemLabel ?website ?type ?typeLabel WHERE {{
  VALUES ?type {{ {values} }}
  ?item wdt:P31 ?type;
        wdt:P856 ?website.
  OPTIONAL {{ ?item rdfs:label ?itemLabel. FILTER(LANG(?itemLabel) = "en") }}
  OPTIONAL {{ ?type rdfs:label ?typeLabel. FILTER(LANG(?typeLabel) = "en") }}
}}
ORDER BY ?item ?website
LIMIT {size}
OFFSET {offset}
""".strip()


def _binding_value(binding: Mapping[str, Any], key: str) -> str:
    value = binding.get(key, {})
    return str(value.get("value", "")) if isinstance(value, Mapping) else ""


def parse_wikidata_payload(
    payload: Mapping[str, Any], *, limit: int = MAX_SOURCE_RESULTS
) -> list[DiscoveredHost]:
    """Parse P856 official websites from the bounded agency/production query."""

    results = payload.get("results", {})
    bindings = results.get("bindings", []) if isinstance(results, Mapping) else []
    if not isinstance(bindings, list):
        return []
    candidates: list[DiscoveredHost] = []
    for binding in bindings:
        if not isinstance(binding, Mapping):
            continue
        hostname = canonicalize_public_website(_binding_value(binding, "website"))
        if hostname is None:
            continue
        type_id = _binding_value(binding, "type").rstrip("/").rsplit("/", 1)[-1]
        if type_id not in WIKIDATA_TYPES:
            continue
        name = _clean_label(_binding_value(binding, "itemLabel"), hostname)
        category = _clean_label(
            _binding_value(binding, "typeLabel"), WIKIDATA_LABELS[type_id]
        )
        rank = WIKIDATA_RANKS[type_id] + (0.75 if name != hostname else 0.0)
        candidates.append(
            DiscoveredHost(
                source_apex="wikidata.org",
                hostname=hostname,
                discovery_rank=round(rank, 3),
                label_hint=f"{name} · {category}",
                discovery_evidence_urls=[_binding_value(binding, "item")],
                provider_record_ids=[
                    _binding_value(binding, "item").rstrip("/").rsplit("/", 1)[-1]
                ],
            )
        )
    return _ranked_unique(candidates, limit)


def _serialize_candidates(candidates: list[DiscoveredHost]) -> dict[str, Any]:
    return {"candidates": [candidate.model_dump(mode="json") for candidate in candidates]}


def _deserialize_candidates(payload: Mapping[str, Any]) -> list[DiscoveredHost] | None:
    raw = payload.get("candidates")
    if not isinstance(raw, list):
        return None
    candidates: list[DiscoveredHost] = []
    for item in raw:
        try:
            candidates.append(DiscoveredHost.model_validate(item))
        except (TypeError, ValueError):
            continue
    return candidates


def _cache_read(
    cache_get: CacheGet | None, host: str, kind: str
) -> list[DiscoveredHost] | None:
    if cache_get is None:
        return None
    try:
        payload = cache_get(host, kind)
    except Exception:
        return None
    return _deserialize_candidates(payload) if payload else None


def _cache_write(
    cache_set: CacheSet | None,
    host: str,
    kind: str,
    candidates: list[DiscoveredHost],
) -> None:
    if cache_set is None:
        return
    try:
        cache_set(
            host,
            kind,
            _serialize_candidates(candidates),
            AGENCY_DISCOVERY_TTL_HOURS,
        )
    except Exception:
        return


def _rate_error(source: str, response: httpx.Response) -> str:
    retry_after = response.headers.get("retry-after", "").strip()
    suffix = f"; retry after {retry_after}" if retry_after else ""
    return f"{source}: HTTP 429{suffix}"


def _response_payload(source: str, response: httpx.Response) -> tuple[dict[str, Any] | None, str]:
    if response.status_code == 429:
        return None, _rate_error(source, response)
    if response.status_code >= 400:
        return None, f"{source}: HTTP {response.status_code}"
    if len(response.content) > MAX_RESPONSE_BYTES:
        return None, f"{source}: response exceeded {MAX_RESPONSE_BYTES} bytes"
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        return None, f"{source}: invalid JSON ({type(exc).__name__})"
    if not isinstance(payload, dict):
        return None, f"{source}: JSON root was not an object"
    return payload, ""


async def discover_agencies(
    client: httpx.AsyncClient,
    *,
    rotation: int = 0,
    max_results: int = MAX_SOURCE_RESULTS,
    wikidata_page_size: int = 100,
    wikidata_max_pages: int = 25,
    timeout_seconds: float = 20.0,
    cache_get: CacheGet | None = None,
    cache_set: CacheSet | None = None,
    osm_endpoint: str = OSM_ENDPOINT,
    wikidata_endpoint: str = WIKIDATA_ENDPOINT,
) -> AgencyDiscoveryResult:
    """Run one OSM metro slice and one Wikidata page, sequentially and without retries.

    Public-service throttles become source errors. The other source is still attempted, and
    callers may pass LeadStore's existing free-signal cache methods without a schema change.
    """

    if rotation < 0:
        raise ValueError("rotation must be non-negative")
    if wikidata_max_pages < 1:
        raise ValueError("wikidata_max_pages must be positive")
    result_limit = max(1, min(max_results, MAX_SOURCE_RESULTS))
    page_size = max(1, min(wikidata_page_size, 100))
    timeout = max(2.0, min(float(timeout_seconds), 30.0))
    metro_slug, osm_query = build_overpass_query(rotation, result_limit=result_limit)
    metro_label = METROS[rotation % len(METROS)][1]
    wikidata_page = rotation % wikidata_max_pages
    osm_kind = f"agency_discovery_osm_{metro_slug}"
    wikidata_kind = f"agency_discovery_wikidata_{wikidata_page}_{page_size}"
    errors: list[str] = []
    candidates: list[DiscoveredHost] = []
    network_calls = 0
    cache_hits = 0

    osm_candidates = _cache_read(cache_get, "openstreetmap.org", osm_kind)
    if osm_candidates is None:
        try:
            response = await provider_request(client, "POST",
                osm_endpoint,
                data={"data": osm_query},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=timeout,
            )
            network_calls += 1
        except httpx.HTTPError as exc:
            errors.append(f"openstreetmap.org: {type(exc).__name__}: {exc}")
        else:
            payload, error = _response_payload("openstreetmap.org", response)
            if error:
                errors.append(error)
            elif payload is not None:
                osm_candidates = parse_overpass_payload(
                    payload, metro_label=metro_label, limit=result_limit
                )
                _cache_write(
                    cache_set, "openstreetmap.org", osm_kind, osm_candidates
                )
    else:
        cache_hits += 1
    candidates.extend(osm_candidates or [])

    wikidata_candidates = _cache_read(cache_get, "wikidata.org", wikidata_kind)
    if wikidata_candidates is None:
        query = build_wikidata_query(wikidata_page, page_size=page_size)
        try:
            response = await provider_request(client, "GET",
                wikidata_endpoint,
                params={"query": query, "format": "json"},
                headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
                timeout=timeout,
            )
            network_calls += 1
        except httpx.HTTPError as exc:
            errors.append(f"wikidata.org: {type(exc).__name__}: {exc}")
        else:
            payload, error = _response_payload("wikidata.org", response)
            if error:
                errors.append(error)
            elif payload is not None:
                wikidata_candidates = parse_wikidata_payload(payload, limit=result_limit)
                _cache_write(
                    cache_set, "wikidata.org", wikidata_kind, wikidata_candidates
                )
    else:
        cache_hits += 1
    candidates.extend(wikidata_candidates or [])

    return AgencyDiscoveryResult(
        candidates=_ranked_unique(candidates, result_limit),
        errors=errors,
        osm_metro=metro_label,
        wikidata_page=wikidata_page,
        network_calls=network_calls,
        cache_hits=cache_hits,
    )
