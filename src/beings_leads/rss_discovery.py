"""Free, discovery-only company domains from recent public PR/news articles."""

from __future__ import annotations

import hashlib
import html
import re
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

import httpx
import tldextract
from bs4 import BeautifulSoup

from .agency_discovery import canonicalize_public_website
from .config import USER_AGENT
from .domain_policy import is_disallowed
from .inspect import _bounded_get
from .models import DiscoveredHost, DiscoveryAssertionEvidence
from .provider_rate_limit import (
    DEFAULT_COOLDOWN_SECONDS,
    is_local_rate_deferral,
    retry_after_seconds,
)

if TYPE_CHECKING:
    from .store import LeadStore


@dataclass(frozen=True)
class RSSSource:
    name: str
    url: str


RSS_SOURCES = (
    RSSSource("PR Newswire · all releases", "https://www.prnewswire.com/rss/news-releases-list.rss"),
    RSSSource("PR Newswire · business technology", "https://www.prnewswire.com/rss/"
              "business-technology-latest-news/business-technology-latest-news-list.rss"),
    RSSSource("PR Newswire · consumer products", "https://www.prnewswire.com/rss/"
              "consumer-products-retail-latest-news/consumer-products-retail-latest-news-list.rss"),
    RSSSource("PR Newswire · health", "https://www.prnewswire.com/rss/"
              "health-latest-news/health-latest-news-list.rss"),
    RSSSource("PRWeb · releases", "https://www.prweb.com/rss/news-releases-list.rss"),
    RSSSource("Newswire · releases", "https://www.newswire.com/newsroom/rss"),
    RSSSource("PRLog · releases", "https://www.prlog.org/rss.xml"),
    RSSSource("TechCrunch · startups", "https://techcrunch.com/category/startups/feed/"),
    RSSSource("EU-Startups", "https://www.eu-startups.com/feed/"),
    RSSSource("SiliconANGLE", "https://siliconangle.com/feed/"),
)

MAX_BYTES = 4_000_000
CACHE_KIND = "rss_article_domains_v1:"
_TLD = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)
# Regex locates URLs; URL parsing and the public suffix list establish domain boundaries.
_URL_RE = re.compile(r"(?<![\w@])(?:https?://|//|www\.)[^\s<>\"'`\\]+", re.I)
_BARE_RE = re.compile(
    r"(?<![\w@/.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}(?:/[^\s<>\"'`\\]*)?(?![\w@-]|\.[a-z0-9])", re.I
)
_ASSET_RE = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|ico|css|js|woff2?|mp[34])$", re.I)
_EXCLUDED = {
    "prnewswire.com", "prweb.com", "newswire.com", "prlog.org", "techcrunch.com",
    "eu-startups.com", "siliconangle.com", "businesswire.com", "globenewswire.com",
    "accessnewswire.com", "einpresswire.com", "w3.org", "schema.org", "gravatar.com",
    "google-analytics.com", "googletagmanager.com", "doubleclick.net", "gstatic.com",
    "wp.com", "wordpress.org", "feedburner.com", "feedblitz.com", "t.co", "tinyurl.com",
    "t.ly", "onelink.me", "example.com", "example.org", "example.net",
    "bsky.app", "reddit.com", "whatsapp.com", "sharethis.com", "addthis.com",
    "thecube.net", "thecubeai.com", "thecuberesearch.com",
}


def company_domain(value: str) -> str | None:
    value = html.unescape(value).strip().rstrip(".,;:!?)]}")
    try:
        parsed = urlsplit(value if "://" in value else f"https://{value.lstrip('/')}")
        if parsed.port not in {None, 80, 443}:
            return None
        host = canonicalize_public_website(value)
    except ValueError:
        return None
    if not host or is_disallowed(host):
        return None
    parts = _TLD(host)
    domain = parts.top_domain_under_public_suffix
    if (not domain or domain in _EXCLUDED or is_disallowed(domain)
            or parts.suffix in {"gov", "edu", "mil", "int"}):
        return None
    if host.split(".", 1)[0] in {"cdn", "static", "assets", "images", "img", "media"}:
        return None
    return domain


def article_domains(body: str) -> list[str]:
    """Extract explicit links and printed URLs from article content, excluding page chrome."""
    decoded = html.unescape(body)
    soup = BeautifulSoup(decoded if "<" in decoded else f"<p>{html.escape(decoded)}</p>",
                         "html.parser")
    for element in soup.select("script, style, nav, footer, header, aside, noscript, template"):
        element.decompose()
    content = soup.select_one(
        '[itemprop="articleBody"], .news-release, .entry-content, .article-body, '
        '.release-body, article, main'
    ) or soup
    values = [str(anchor.get("href") or "") for anchor in content.find_all("a", href=True)]
    visible = content.get_text(" ", strip=True)
    # Remove emails before bare-domain matching so an address never manufactures a company.
    visible = re.sub(r"[^\s<>]+@[^\s<>]+", "", visible)
    values.extend(match.group(0) for match in _URL_RE.finditer(visible))
    visible = _URL_RE.sub(" ", visible)
    values.extend(match.group(0) for match in _BARE_RE.finditer(visible))
    domains = set()
    for value in values:
        if value.startswith(("/", "#")) and not value.startswith("//"):
            continue
        try:
            path = urlsplit(value).path
        except ValueError:
            continue
        if _ASSET_RE.search(path):
            continue
        if domain := company_domain(value):
            domains.add(domain)
    return sorted(domains)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _date(value: str) -> datetime | None:
    try:
        result = parsedate_to_datetime(value)
    except (ValueError, TypeError, OverflowError):
        try:
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    # A publication timestamp without an explicit zone cannot establish the seven-day cutoff.
    return result.astimezone(UTC) if result.tzinfo is not None else None


@dataclass(frozen=True)
class RSSEntry:
    url: str
    published_at: datetime
    body: str


def recent_entries(body: bytes, *, window_end: datetime) -> tuple[list[RSSEntry], int]:
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", body.replace(b"\x00", b""), re.I):
        raise ValueError("RSS entity/doctype declarations are unsupported")
    root = ET.fromstring(body)
    if _local_name(root.tag) not in {"rss", "feed", "RDF"}:
        raise ValueError("Response is not RSS or Atom")
    start = window_end - timedelta(days=7)
    entries = {}
    skipped = 0
    for item in root.iter():
        if _local_name(item.tag) not in {"item", "entry"}:
            continue
        children = list(item)
        dates = [child for child in children if _local_name(child.tag) in
                 {"pubDate", "published", "date"}]
        dates.sort(key=lambda child: {"pubDate": 0, "published": 0, "date": 1}[
            _local_name(child.tag)
        ])
        published = next((parsed for child in dates if (parsed := _date(child.text or ""))), None)
        if published is None or not start <= published <= window_end:
            skipped += 1
            continue
        links = [child for child in children if _local_name(child.tag) == "link"
                 and child.get("rel", "alternate") == "alternate"]
        url = next((child.get("href") or child.text or "" for child in links), "").strip()
        parts = []
        for child in children:
            if _local_name(child.tag) in {"description", "summary", "content", "encoded"}:
                parts.append(child.text or "")
                for node in child:
                    # Atom XHTML serializes namespaced anchors as ns0:a; HTML parsers need a.
                    for descendant in node.iter():
                        descendant.tag = _local_name(descendant.tag)
                    parts.append(ET.tostring(node, encoding="unicode"))
        # Repeated feed entries are one article; prefer the longest supplied body.
        entry = RSSEntry(url, published, "\n".join(parts))
        key = (url, published)
        if key not in entries or len(entry.body) > len(entries[key].body):
            entries[key] = entry
    return list(entries.values()), skipped


def _publisher_url(url: str, source: RSSSource) -> bool:
    try:
        parsed = urlsplit(url)
        host = canonicalize_public_website(url)
        source_host = canonicalize_public_website(source.url)
        return bool(host and source_host and not parsed.username and not parsed.password
                    and parsed.port in {None, 80, 443}
                    and _TLD(host).top_domain_under_public_suffix
                    == _TLD(source_host).top_domain_under_public_suffix)
    except ValueError:
        return False


class RSSFetcher:
    """Publisher-scoped, robots-aware wrapper over the shared SSRF/byte/redirect guard."""

    def __init__(self, client: httpx.AsyncClient, source: RSSSource):
        self.client = client
        self.source = source
        self.robots: dict[str, urllib.robotparser.RobotFileParser | bool] = {}

    async def _redirect(self, current: str, target: str) -> None:
        if not _publisher_url(target, self.source):
            raise ValueError("RSS redirect crossed supplier domain")
        await self._robots(target)

    async def _robots_redirect(self, current: str, target: str) -> None:
        if urlsplit(current).hostname != urlsplit(target).hostname:
            raise ValueError("RSS robots redirect crossed host")

    async def _robots(self, url: str) -> None:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.robots:
            response, body = await _bounded_get(
                self.client, origin + "/robots.txt", max_bytes=256_000,
                allowed_content_types=("text/plain", "html"), timeout=20,
                redirect_validator=self._robots_redirect,
            )
            if response.status_code in {404, 410}:
                self.robots[origin] = True
            elif response.status_code == 200 and not response.extensions.get(
                "beings_body_truncated"
            ):
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(body.decode("utf-8", errors="replace").splitlines())
                self.robots[origin] = parser
            else:
                raise ValueError(f"RSS robots unavailable (HTTP {response.status_code})")
        policy = self.robots[origin]
        if policy is False or (policy is not True and not policy.can_fetch(USER_AGENT, url)):
            raise ValueError("RSS robots disallows URL")

    async def __call__(self, url: str) -> bytes:
        if not _publisher_url(url, self.source):
            raise ValueError("RSS article is outside supplier domain")
        await self._robots(url)
        response, body = await _bounded_get(
            self.client, url, max_bytes=MAX_BYTES, max_redirects=3, timeout=30,
            accept="application/rss+xml,application/atom+xml,application/xml,text/xml,text/html",
            allowed_content_types=("rss", "atom", "xml", "html", "text/plain"),
            redirect_validator=self._redirect,
        )
        response.raise_for_status()
        if response.extensions.get("beings_body_truncated") or not body:
            raise ValueError("RSS response is empty, unsupported or exceeds the byte limit")
        return body


@dataclass
class RSSResult:
    candidates: list[DiscoveredHost] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    articles: int = 0
    skipped: int = 0
    cache_hits: int = 0
    deferred_articles: int = 0


@dataclass
class _ArticleCooldown:
    until: float
    cause: str
    articles: int = 0


def save_rss_candidates(store: LeadStore, candidates: list[DiscoveredHost]) -> None:
    """Reuse both exact domains and already-verified canonical aliases, preserving lineage."""
    domains = sorted({candidate.hostname for candidate in candidates})
    known = {}
    for start in range(0, len(domains), 250):
        batch = domains[start:start + 250]
        placeholders = ",".join("?" for _ in batch)
        hosts = [host for domain in batch for host in (domain, "www." + domain)]
        rows = store.connection.execute(
            f"SELECT hostname FROM candidates WHERE hostname IN ({','.join('?' for _ in hosts)})",
            hosts,
        ).fetchall()
        for row in rows:
            host = str(row["hostname"])
            domain = host.removeprefix("www.")
            if domain not in known or host == domain:
                known[domain] = host
        # The canonical-domain and candidate/snapshot indexes bound this to these exact domains.
        rows = store.connection.execute(
            f"""SELECT ti.canonical_domain, c.hostname
                FROM snapshot_target_intelligence ti
                JOIN candidates c ON c.id=ti.candidate_id
                WHERE ti.canonical_domain IN ({placeholders})
                  AND ti.snapshot_id=(SELECT MAX(s.id) FROM snapshots s WHERE s.candidate_id=c.id)
                ORDER BY c.id""",
            batch,
        ).fetchall()
        for row in rows:
            known.setdefault(str(row["canonical_domain"]), str(row["hostname"]))
    store.upsert_derived_candidates([
        candidate.model_copy(update={"hostname": known.get(candidate.hostname, candidate.hostname)})
        for candidate in candidates
    ])


async def collect_rss_source(
    source: RSSSource, *, window_end: datetime,
    fetch: Callable[[str], Awaitable[bytes]],
    cache_get: Callable[[str, str], dict | None],
    cache_set: Callable[[str, str, dict, int], None],
    save: Callable[[list[DiscoveredHost]], None],
    checkpoint: Callable[[], Awaitable[None]],
) -> RSSResult:
    result = RSSResult()
    entries, result.skipped = recent_entries(await fetch(source.url), window_end=window_end)
    publisher = (urlsplit(source.url).hostname or "").removeprefix("www.")
    cooldowns: dict[str, _ArticleCooldown] = {}
    observed_cooldowns: list[_ArticleCooldown] = []
    for entry in entries:
        await checkpoint()
        article_url = urljoin(source.url, entry.url) if entry.url else source.url
        key = CACHE_KIND + hashlib.sha256(
            f"{article_url}\n{entry.published_at.isoformat()}\n{entry.body}".encode()
        ).hexdigest()
        cached = cache_get(publisher, key)
        if cached is not None:
            domains = [domain for domain in cached.get("domains", []) if company_domain(domain)]
            result.cache_hits += 1
        else:
            domains = set(article_domains(entry.body))
            article_failed = False
            if entry.url and _publisher_url(article_url, source):
                host = urlsplit(article_url).hostname or ""
                cooldown = cooldowns.get(host)
                if cooldown is not None and monotonic() < cooldown.until:
                    # Keep inline discoveries, but do not call the fetcher again
                    # or manufacture another per-article error during this hold.
                    cooldown.articles += 1
                    result.deferred_articles += 1
                    article_failed = True
                else:
                    try:
                        article = await fetch(article_url)
                        domains.update(article_domains(article.decode("utf-8", errors="replace")))
                    except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
                        response = getattr(exc, "response", None)
                        cause = f"{article_url}: {type(exc).__name__}: {exc}"
                        if is_local_rate_deferral(exc) or (
                            response is not None and response.status_code == 429
                        ):
                            delay = (
                                retry_after_seconds(response.headers)
                                if response is not None else None
                            )
                            cooldown = _ArticleCooldown(
                                until=monotonic() + (
                                    DEFAULT_COOLDOWN_SECONDS if delay is None else delay
                                ),
                                cause=cause,
                                articles=1,
                            )
                            cooldowns[host] = cooldown
                            observed_cooldowns.append(cooldown)
                            result.deferred_articles += 1
                        else:
                            result.errors.append(cause)
                        article_failed = True
            elif entry.url:
                result.errors.append(f"{article_url}: article is outside supplier domain")
                article_failed = True
            domains = sorted(domains)
            # Failed pages retain inline discoveries but never masquerade as complete cache hits.
            if not article_failed:
                cache_set(publisher, key, {"domains": domains, "checked": True}, 24 * 7)
        candidates = [DiscoveredHost(
            source_apex=publisher, hostname=domain,
            discovery_evidence_urls=[source.url, article_url],
            discovery_assertions=[DiscoveryAssertionEvidence(
                publisher_domain=publisher, source_url=article_url,
                published_at=entry.published_at, confidence="low",
            )],
        ) for domain in domains]
        save(candidates)
        result.candidates.extend(candidates)
        result.articles += 1
    result.errors.extend(
        f"{cooldown.articles:,} article pages deferred during one host cooldown; {cooldown.cause}"
        for cooldown in observed_cooldowns
    )
    return result
