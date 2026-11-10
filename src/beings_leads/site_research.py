"""Local navigation planning; hints choose retrieval, never establish scoreable facts."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

DEFAULT_SITE_RESEARCH_PAGES = 6

_PURPOSES = (
    ("procurement", r"\b(?:procurement|rfps?|tenders?|supplier brief|request for proposals?)\b"),
    ("hiring", r"\b(?:careers?|jobs?|hiring|open roles?|vacancies|positions?|requisitions?|"
               r"join us|join our team|work with us)\b"),
    ("contact", r"\b(?:contacts?|new business|leadership|team|people|vcard)\b"),
    ("proof", r"\b(?:case stud\w*|portfolio|work|clients?|customer stor\w*|success stor\w*)\b"),
    ("production", r"\b(?:capabilit\w*|production|studio|creative|services?|solutions?)\b"),
    ("release", r"\b(?:launch\w*|releases?|changelog|what s new|product updates?)\b"),
    ("content", r"\b(?:blog|news|articles?|insights?|stories|press|media|webinars?|events?)\b"),
    ("program", r"\b(?:creators?|influencers?|ambassadors?|affiliates?|partners?)\b"),
    ("catalog", r"\b(?:products?|collections?|catalog\w*|shop|pricing|subscriptions?)\b"),
    ("company", r"\b(?:about|company|who we are)\b"),
)
_PATTERNS = tuple((purpose, re.compile(pattern, re.I)) for purpose, pattern in _PURPOSES)
_SKIP_PATH = re.compile(
    r"(?:^|/)(?:login|log-in|logout|sign-in|signin|sign-up|signup|register|account|"
    r"checkout|cart|basket|unsubscribe|search|tag|tags|author|wp-admin|wp-json)(?:/|$)", re.I,
)
_SKIP_SUFFIX = re.compile(
    r"\.(?:png|jpe?g|gif|svg|webp|ico|css|js|map|woff2?|ttf|mp[34]|mov|webm|zip|gz|pdf)$", re.I,
)
_TRACKING = {"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "ref", "source"}
_STRUCTURED = {
    "jobposting": "hiring", "article": "content", "newsarticle": "content",
    "blogposting": "content", "product": "catalog", "service": "production",
    "event": "content",
}


def company_host(url: str) -> str:
    return (urlsplit(url).hostname or "").casefold().rstrip(".").removeprefix("www.")


def research_url(value: str, base: str) -> str:
    """Accept linked public pages of this exact company, with stable tracking aliases."""
    try:
        if not value or value.startswith("#") or len(value) > 2_000:
            return ""
        parsed = urlsplit(urljoin(base, value))
        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or parsed.port not in {None, 80, 443}
                or company_host(urlunsplit(parsed)) != company_host(base)):
            return ""
        path = unquote(parsed.path).casefold()
        if _SKIP_PATH.search(path) or _SKIP_SUFFIX.search(path):
            return ""
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                 if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING]
        if any(key.casefold() in {"action", "add-to-cart", "remove_item", "s", "q", "token"}
               for key, _ in query):
            return ""
        return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path or "/",
                           urlencode(sorted(query)), ""))
    except ValueError:
        return ""


def url_key(url: str) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    return company_host(url), parsed.path.rstrip("/") or "/", parsed.query


def purpose_for(url: str, label: str = "") -> str:
    path = unquote(urlsplit(url).path)
    corpus = re.sub(r"[-_/]+", " ", f"{path} {label}")
    return next((purpose for purpose, pattern in _PATTERNS if pattern.search(corpus)), "")


@dataclass(frozen=True)
class ResearchLink:
    url: str
    purpose: str
    depth: int
    source: str
    order: int
    observed: bool = True


class SiteResearchPlanner:
    """Revisit linked gaps after every page, retaining useful legacy primary selections."""

    def __init__(self, home_url: str):
        self.home_url = home_url
        self.links: dict[tuple[str, str, str], ResearchLink] = {}
        self.attempted = {url_key(home_url)}
        self.covered: set[str] = set()
        self.depths = {url_key(home_url): 0}
        self.primary: list[str] = []
        self.entity_counts: Counter[tuple[str, str]] = Counter()

    def _add(self, value: str, source: str, label: str = "", purpose: str = "",
             *, observed: bool = True) -> None:
        url = research_url(value, source or self.home_url)
        if not url or company_host(url) != company_host(self.home_url):
            return
        key = url_key(url)
        purpose = purpose or purpose_for(url, label)
        old = self.links.get(key)
        if purpose and (old is None or observed and not old.observed):
            self.links[key] = ResearchLink(
                url, purpose, self.depths.get(url_key(source), 0) + 1,
                source, old.order if old is not None else len(self.links), observed,
            )

    def observe(self, url: str, soup: BeautifulSoup) -> None:
        """Read attributable anchor/structured hints from the already-parsed fetched page."""
        page_link = self.links.get(url_key(url))
        page_purpose = page_link.purpose if page_link is not None else purpose_for(url)
        self.covered.update([page_purpose] if page_purpose else [])
        for anchor in soup.find_all("a", href=True):
            if any(anchor.find_parent(tag) for tag in ("template", "noscript")):
                continue
            label = " ".join([
                anchor.get_text(" ", strip=True)[:300], str(anchor.get("aria-label") or ""),
                str(anchor.get("title") or ""),
                " ".join(str(image.get("alt") or "") for image in anchor.find_all("img")),
            ])[:600]
            target = urljoin(url, str(anchor["href"]).strip())
            purpose = purpose_for(target, label)
            if (not purpose and label.strip() and url_key(url) != url_key(self.home_url)
                    and page_purpose in {"hiring", "proof", "content", "procurement", "catalog"}
                    and not anchor.find_parent(["nav", "header", "footer"])):
                # A role title or story headline under a verified hub is a retrieval hint,
                # never evidence of the publisher's hiring, clients or demand by itself.
                purpose = page_purpose
            self._add(str(anchor["href"]).strip(), url, label, purpose)
        for script in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
            raw = script.string or script.get_text(" ", strip=True)
            if not raw or len(raw) > 1_000_000:
                continue
            try:
                self._structured(json.loads(raw), url)
            except (ValueError, TypeError, RecursionError):
                continue

    def deduplicate_entities(self, soup: BeautifulSoup, url: str, web) -> None:
        """Repeated products/media on different pages cannot manufacture operating scale.

        Keep the largest observed within-page multiplicity for anonymous equal objects, so
        first-page compatibility is preserved. All raw page and atomic provenance survives.
        """
        counts: Counter[tuple[str, str]] = Counter()
        fields = {
            "product": "product_objects", "offer": "offer_objects",
            "aggregateoffer": "offer_objects", "organization": "organization_objects",
            "corporation": "organization_objects", "brand": "organization_objects",
            "localbusiness": "location_objects", "store": "location_objects",
            "place": "location_objects", "postaladdress": "location_objects",
        }

        def visit(value, depth=0):
            if depth > 12:
                return
            if isinstance(value, dict):
                types = value.get("@type", [])
                types = ([types] if isinstance(types, str)
                         else types if isinstance(types, list) else [])
                categories = {fields[str(kind).casefold()] for kind in types
                              if str(kind).casefold() in fields}
                if categories:
                    identity = value.get("@id") or value.get("url")
                    material = (urljoin(url, identity) if isinstance(identity, str) else
                                json.dumps(value, sort_keys=True, separators=(",", ":")))
                    fingerprint = hashlib.sha256(material.encode()).hexdigest()
                    for category in categories:
                        counts[category, fingerprint] += 1
                for child in value.values():
                    visit(child, depth + 1)
            elif isinstance(value, list):
                for child in value[:500]:
                    visit(child, depth + 1)

        for script in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
            raw = script.string or script.get_text(" ", strip=True)
            if not raw or len(raw) > 1_000_000:
                continue
            try:
                visit(json.loads(raw))
            except (ValueError, TypeError, RecursionError):
                continue
        for node in soup.find_all(["img", "video", "source", "iframe"]):
            category = ("image_assets" if node.name == "img" else "video_assets"
                        if node.name == "video" or node.find_parent("video") is not None else "")
            value = str(node.get("src") or node.get("data-src") or "").strip()
            if node.name == "iframe" and value:
                host = company_host(urljoin(url, value))
                if any(host == provider or host.endswith("." + provider) for provider in (
                    "youtube.com", "youtu.be", "vimeo.com", "wistia.com", "wistia.net", "mux.com",
                )):
                    category = "embedded_video_players"
            if category:
                identity = urljoin(url, value) if value else str(node)
                counts[category, node.name + ":" + identity] += 1
        totals: Counter[str] = Counter()
        for key, count in counts.items():
            totals[key[0]] += max(0, count - self.entity_counts[key])
            self.entity_counts[key] = max(self.entity_counts[key], count)
        for name in set(fields.values()):
            setattr(web, name, totals[name])
        for name in ("image_assets", "video_assets", "embedded_video_players"):
            setattr(web.sales, name, totals[name])

    def _structured(self, value, source: str, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(value, dict):
            types = value.get("@type", [])
            types = [types] if isinstance(types, str) else types if isinstance(types, list) else []
            purpose = next((_STRUCTURED[str(kind).casefold()] for kind in types
                            if str(kind).casefold() in _STRUCTURED), "")
            target = value.get("url") or value.get("@id")
            if purpose and isinstance(target, str):
                self._add(target, source, str(value.get("name") or ""), purpose)
            for child in value.values():
                self._structured(child, source, depth + 1)
        elif isinstance(value, list):
            for child in value[:500]:
                self._structured(child, source, depth + 1)

    def seed_primary(self, legacy: list[str], observed: list[str]) -> None:
        known = {url_key(link) for link in observed}
        # Preserve actual legacy primary links; guesses yield to observed detail opportunities.
        self.primary = [link for link in legacy if url_key(link) in known]
        for link in legacy:
            self._add(link, self.home_url, observed=False)

    def next_url(self) -> str | None:
        while self.primary:
            target = research_url(self.primary.pop(0), self.home_url)
            if target and url_key(target) not in self.attempted:
                return self._claim(target)
        candidates = [link for key, link in self.links.items() if key not in self.attempted]
        if not candidates:
            return None
        ranks = {purpose: index for index, (purpose, _) in enumerate(_PURPOSES)}
        link = min(candidates, key=lambda item: (
            not item.observed,  # Linked evidence always precedes guessed legacy fallback paths.
            item.purpose in self.covered,  # Preserve broad coverage before repeating a purpose.
            item.depth == 1 and item.purpose in self.covered,  # Then seek linked details.
            ranks[item.purpose], item.order,
        ))
        return self._claim(link.url)

    def _claim(self, url: str) -> str:
        key = url_key(url)
        self.attempted.add(key)
        if key in self.links:
            link = self.links[key]
            self.depths[key] = link.depth
            self.covered.add(link.purpose)
        return url


def has_first_party_job_schema(soup: BeautifulSoup, page_url: str) -> bool:
    """An opaque job document needs an explicit employer-domain binding, not keywords."""
    nodes: list[dict] = []

    def visit(value, depth=0):
        if depth > 12:
            return
        if isinstance(value, dict):
            nodes.append(value)
            for child in value.values():
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:500]:
                visit(child, depth + 1)

    for script in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw or len(raw) > 1_000_000 or script.find_parent("template"):
            continue
        try:
            visit(json.loads(raw))
        except (ValueError, TypeError, RecursionError):
            continue
    identities = {node["@id"]: node for node in nodes if isinstance(node.get("@id"), str)}
    found = False
    for node in nodes:
        types = node.get("@type")
        if "JobPosting" not in (types if isinstance(types, list) else [types]):
            continue
        found = True
        bound = False
        employer = node.get("hiringOrganization")
        if isinstance(employer, dict):
            reference = employer.get("@id")
            if isinstance(reference, str):
                employer = identities.get(reference, employer)
            urls = [employer.get("url"), employer.get("sameAs"), employer.get("@id")]
        else:
            urls = [employer]
        for value in urls:
            for target in value if isinstance(value, list) else [value]:
                if not isinstance(target, str) or not target.startswith(("http://", "https://")):
                    continue
                try:
                    bound = bound or company_host(target) == company_host(page_url)
                except ValueError:
                    continue
        # A mixed or ambiguous job directory cannot lend another employer's roles
        # to this company through the otherwise valid first-party posting beside them.
        if not bound:
            return False
    return found
