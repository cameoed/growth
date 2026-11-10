from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable
from datetime import datetime
from email.utils import getaddresses
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup, Tag

from .models import (
    ContactIntelligence,
    PublicEmailEvidence,
    PublicPersonEvidence,
    PublicPhoneEvidence,
)

EMAIL_CANDIDATE_RE = re.compile(
    r"(?<![\w.+-])([a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[a-zA-Z0-9.-]+\.[a-zA-Z0-9-]{2,63})(?![\w.-])"
)
OBFUSCATED_EMAIL_RE = re.compile(
    r"(?<![\w.+-])(?P<local>[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]{1,64})"
    r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\))\s*"
    r"(?P<domain>[a-zA-Z0-9-]{1,63}(?:\s*"
    r"(?:\[\s*dot\s*\]|\(\s*dot\s*\))\s*[a-zA-Z0-9-]{1,63})+)(?![\w.-])",
    re.I,
)
OBFUSCATED_DOT_RE = re.compile(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\))\s*", re.I)

EXCLUDED_VISIBLE_ANCESTORS = {"script", "style", "svg", "template", "noscript"}
ASSET_TLDS = {
    "avif",
    "css",
    "eot",
    "gif",
    "ico",
    "jpeg",
    "jpg",
    "js",
    "json",
    "map",
    "mp4",
    "pdf",
    "png",
    "svg",
    "ttf",
    "webm",
    "webp",
    "woff",
    "woff2",
}
RESERVED_OR_PLACEHOLDER_DOMAINS = {
    "company.invalid",
    "company.ltd",
    "company.site",
    "domain.com",
    "email.com",
    "example.com",
    "example.net",
    "example.org",
    "sample.com",
    "test.com",
    "yourdomain.com",
}
PLACEHOLDER_LOCAL_PARTS = {
    "email",
    "example",
    "name",
    "sample",
    "someone",
    "test",
    "user",
    "username",
    "your-email",
    "youremail",
    "yourname",
}
VENDOR_EMAIL_DOMAINS = {
    "cloudflare.com",
    "ditinteractive.com",
    "halothemes.com",
    "hubspot.com",
    "omegatheme.com",
    "papathemes.com",
    "sentry.io",
    "shopify.com",
    "shopline.com",
    "shoplineapp.com",
    "squarespace.com",
    "wixpress.com",
    "wordpress.com",
}
FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "hotmail.com",
    "icloud.com",
    "outlook.com",
    "proton.me",
    "protonmail.com",
    "yahoo.com",
}

PURPOSE_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("security", ("abuse", "phishing", "security", "vulnerability", "vulnerabilities")),
    ("privacy", ("data protection", "dataprotection", "dpo", "gdpr", "privacy")),
    ("legal", ("compliance", "counsel", "legal")),
    (
        "careers",
        ("career", "careers", "employment", "human resources", "jobs", "recruit", "talent"),
    ),
    ("support", ("customer service", "customer success", "help", "service", "support")),
    ("press", ("media enquiries", "media inquiry", "newsroom", "press", "public relations")),
    ("partnerships", ("alliance", "partner", "partnership", "sponsor")),
    (
        "new_business",
        (
            "business development",
            "business enquiry",
            "business inquiry",
            "bizdev",
            "founder",
            "managing director",
            "new business",
            "newbusiness",
            "owner",
            "principal",
        ),
    ),
    ("sales", ("sales",)),
    ("marketing", ("brand director", "chief marketing", "head of marketing", "marketing")),
    (
        "production",
        (
            "creative director",
            "creative producer",
            "head of production",
            "photo",
            "production",
            "studio",
            "video",
        ),
    ),
    ("general", ("contact", "enquiries", "inquiries", "general", "hello", "info", "office")),
)
ELIGIBLE_PURPOSES = {
    "new_business",
    "sales",
    "partnerships",
    "marketing",
    "production",
    "general",
}
RESTRICTED_PURPOSES = {"support", "press", "careers", "legal", "privacy", "security"}
EXPLICIT_METHODS = {"mailto", "json_ld", "microdata", "encoded_markup", "vcard"}
CONTACT_PATH_TOKENS = (
    "about",
    "agency",
    "contact",
    "leadership",
    "people",
    "studio",
    "team",
)
METHOD_RANK = {
    "mailto": 7,
    "json_ld": 6,
    "microdata": 5,
    "vcard": 5,
    "encoded_markup": 4,
    "visible_text": 3,
    "common_crawl": 1,
}
PHONE_METHOD_RANK = {"tel": 4, "json_ld": 3, "microdata": 2, "vcard": 3}
PURPOSE_RANK = {
    "new_business": 10,
    "sales": 9,
    "partnerships": 8,
    "marketing": 7,
    "production": 7,
    "general": 6,
    "unknown": 2,
    "support": 1,
    "press": 1,
    "careers": 0,
    "legal": -1,
    "privacy": -2,
    "security": -3,
}
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
LOCAL_PREFERENCE = {
    "newbusiness": 10,
    "new-business": 10,
    "sales": 9,
    "partnerships": 8,
    "partners": 8,
    "marketing": 7,
    "production": 7,
    "studio": 7,
    "hello": 6,
    "contact": 5,
    "enquiries": 5,
    "inquiries": 5,
    "info": 4,
    "office": 3,
}
BUYER_ROLE_PATTERN = re.compile(
    r"\b(?:co[- ]?founder|founder|owner|chief executive officer|ceo|"
    r"chief marketing officer|cmo|chief brand officer|managing director|president|"
    r"vice president|vp|head|director|lead|manager)\b"
    r"(?:\s+(?:of|,|[-–—])?\s*(?:growth|performance marketing|marketing|creative|"
    r"content|production|studio|brand|e[- ]?commerce|social|video|photo|operations|"
    r"procurement|legal|rights))?",
    re.I,
)
SEMANTIC_PERSON_CLASS_RE = re.compile(
    r"(?:^|[-_\s])(bio|executive|leadership|person|profile|staff|team[-_ ]?member)"
    r"(?:$|[-_\s])",
    re.I,
)
NON_PERSON_CONTEXT = (
    "case study",
    "client quote",
    "customer story",
    "meet our clients",
    "testimonial",
)


def _clean_host(value: str) -> str:
    raw = value.strip().lower().rstrip(".")
    if "://" in raw:
        raw = urlparse(raw).hostname or ""
    elif "/" in raw:
        raw = urlparse(f"//{raw}").hostname or raw.split("/", 1)[0]
    return raw.removeprefix("www.").rstrip(".")


def _domain_matches_site(domain: str, site_host: str) -> bool:
    site = _clean_host(site_host)
    domain = _clean_host(domain)
    return bool(
        site
        and domain
        and (site == domain or site.endswith(f".{domain}") or domain.endswith(f".{site}"))
    )


def normalize_email(value: str) -> str | None:
    """Return a conservative ASCII mailbox or None; never infer a missing component."""

    if not isinstance(value, str):
        return None
    candidate = html.unescape(unquote(value)).strip()
    if candidate.lower().startswith("mailto:"):
        candidate = candidate[7:]
    candidate = candidate.split("?", 1)[0].strip()
    candidate = candidate.strip("\t\r\n <>[](){}\"',;:")
    if len(candidate) > 254 or candidate.count("@") != 1:
        return None
    local, domain = candidate.rsplit("@", 1)
    local = local.strip().lower()
    domain = domain.strip().lower().rstrip(".")
    if not local or len(local) > 64 or local.startswith(".") or local.endswith("."):
        return None
    if ".." in local or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local):
        return None
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    labels = domain.split(".")
    if len(labels) < 2 or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not re.fullmatch(r"[a-z0-9-]+", label)
        for label in labels
    ):
        return None
    tld = labels[-1]
    if tld in ASSET_TLDS or not (re.fullmatch(r"[a-z]{2,63}", tld) or tld.startswith("xn--")):
        return None
    if domain in RESERVED_OR_PLACEHOLDER_DOMAINS:
        return None
    if tld in {"example", "invalid", "localhost", "test"}:
        return None
    if local in PLACEHOLDER_LOCAL_PARTS and labels[0] in {
        "company",
        "domain",
        "email",
        "example",
        "sample",
        "test",
        "yourdomain",
    }:
        return None
    return f"{local}@{domain}"


def classify_purpose(address: str, context: str = "") -> str:
    local = address.split("@", 1)[0].lower()
    for haystack in (
        re.sub(r"[._+@/-]+", " ", local),
        re.sub(r"[._+@/-]+", " ", context.lower()),
    ):
        for purpose, tokens in PURPOSE_TOKENS:
            if any(
                re.search(rf"(?<![a-z0-9]){re.escape(token)}s?(?![a-z0-9])", haystack)
                for token in tokens
            ):
                return purpose
    return "unknown"


def classify_phone_purpose(context: str = "") -> str:
    haystack = re.sub(r"[._+@/-]+", " ", context.lower())
    for purpose, tokens in PURPOSE_TOKENS:
        if any(
            re.search(rf"(?<![a-z0-9]){re.escape(token)}s?(?![a-z0-9])", haystack)
            for token in tokens
        ):
            return purpose
    return "unknown"


def normalize_phone(value: str) -> tuple[str, str] | None:
    """Return conservative display and dial values from an explicit phone field."""

    if not isinstance(value, str):
        return None
    candidate = html.unescape(unquote(value)).strip()
    if candidate.lower().startswith("tel:"):
        candidate = candidate[4:]
    candidate = candidate.split("?", 1)[0].strip()
    candidate = re.sub(r";ext(?:ension)?=\d+$", "", candidate, flags=re.I).strip()
    candidate = re.sub(r"\s+", " ", candidate).strip(" \t\r\n<>{}\"',;")
    digits = re.sub(r"\D", "", candidate)
    if not 7 <= len(digits) <= 15:
        return None
    if len(set(digits)) == 1:
        return None
    dial_number = f"+{digits}" if candidate.startswith("+") else digits
    return candidate[:80], dial_number


def make_phone_evidence(
    value: str,
    *,
    source_url: str,
    site_host: str,
    method: str,
    context: str = "",
    historical: bool = False,
    observed_at: datetime | None = None,
) -> PublicPhoneEvidence | None:
    normalized = normalize_phone(value)
    if not normalized:
        return None
    number, dial_number = normalized
    source_host = _clean_host(urlparse(source_url).hostname or "")
    first_party = _domain_matches_site(source_host, site_host)
    combined_context = " ".join(
        part for part in (context, urlparse(source_url).path) if part
    )
    purpose = classify_phone_purpose(combined_context)
    outreach_eligible = bool(
        not historical
        and first_party
        and (purpose in ELIGIBLE_PURPOSES or purpose == "unknown")
    )
    if purpose in RESTRICTED_PURPOSES:
        outreach_eligible = False
    confidence = "low" if historical else "high" if first_party else "medium"
    return PublicPhoneEvidence(
        number=number,
        dial_number=dial_number,
        source_url=source_url,
        method=method,
        purpose=purpose,
        confidence=confidence,
        outreach_eligible=outreach_eligible,
        historical=historical,
        observed_at=observed_at,
    )


def _is_vendor_address(address: str, site_host: str) -> bool:
    domain = address.rsplit("@", 1)[-1]
    vendor = next(
        (
            vendor_domain
            for vendor_domain in VENDOR_EMAIL_DOMAINS
            if domain == vendor_domain or domain.endswith(f".{vendor_domain}")
        ),
        None,
    )
    return bool(vendor and _clean_host(site_host) != vendor)


def make_email_evidence(
    value: str,
    *,
    source_url: str,
    site_host: str,
    method: str,
    context: str = "",
    historical: bool = False,
    observed_at: datetime | None = None,
    person_name: str = "",
    person_role: str = "",
) -> PublicEmailEvidence | None:
    address = normalize_email(value)
    if not address or _is_vendor_address(address, site_host):
        return None
    domain = address.rsplit("@", 1)[-1]
    domain_match = _domain_matches_site(domain, site_host)
    combined_context = " ".join(
        part for part in (context, person_role, urlparse(source_url).path) if part
    )
    purpose = classify_purpose(address, combined_context)
    explicit_context = any(token in combined_context.lower() for token in CONTACT_PATH_TOKENS)
    free_mailbox_on_contact_surface = bool(
        domain in FREE_EMAIL_DOMAINS
        and method in EXPLICIT_METHODS
        and explicit_context
    )
    identity_aligned = domain_match or free_mailbox_on_contact_surface
    outreach_eligible = not historical and identity_aligned and (
        purpose in ELIGIBLE_PURPOSES
        or (purpose == "unknown" and method in EXPLICIT_METHODS and explicit_context)
    )
    if purpose in RESTRICTED_PURPOSES:
        outreach_eligible = False
    if historical:
        confidence = "low"
    elif domain_match and method in EXPLICIT_METHODS:
        confidence = "high"
    elif domain_match or domain in FREE_EMAIL_DOMAINS or method in EXPLICIT_METHODS:
        confidence = "medium"
    else:
        confidence = "low"
    return PublicEmailEvidence(
        address=address,
        source_url=source_url,
        method="common_crawl" if historical else method,
        purpose=purpose,
        confidence=confidence,
        outreach_eligible=outreach_eligible,
        domain_matches_site=domain_match,
        historical=historical,
        observed_at=observed_at,
        person_name=person_name.strip()[:200],
        person_role=person_role.strip()[:300],
    )


def _context(node: Tag, *, limit: int = 500) -> str:
    parent = node.find_parent(["address", "article", "footer", "header", "li", "section"])
    container = node
    if parent:
        mailto_links = [
            link
            for link in parent.find_all("a", href=True)
            if str(link.get("href") or "").lower().startswith("mailto:")
        ]
        if len(mailto_links) <= 1:
            container = parent
    return re.sub(r"\s+", " ", container.get_text(" ", strip=True))[:limit]


def _mailto_values(raw_href: str) -> list[str]:
    href = html.unescape(raw_href).strip()
    if not href.lower().startswith("mailto:"):
        return []
    payload = href[7:]
    path, _, query = payload.partition("?")
    values = [unquote(path).replace(";", ",")]
    if query:
        parameters = parse_qs(query, keep_blank_values=False)
        values.extend(parameters.get("to", []))
    addresses: list[str] = []
    for _, address in getaddresses(values):
        if address:
            addresses.append(address)
    if not addresses:
        addresses.extend(re.split(r"\s*[,;]\s*", unquote(path)))
    return addresses


def _visible_text_chunks(soup: BeautifulSoup) -> list[str]:
    chunks: list[str] = []
    for node in soup.find_all(string=True):
        if any(
            getattr(parent, "name", "") in EXCLUDED_VISIBLE_ANCESTORS for parent in node.parents
        ):
            continue
        value = re.sub(r"\s+", " ", str(node)).strip()
        if value:
            chunks.append(value)
    return chunks


def _json_types(node: dict[str, object]) -> set[str]:
    raw = node.get("@type", [])
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return {re.split(r"[/#]", str(value).lower().rstrip("/#"))[-1] for value in values}


def _value_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, str)]
    return []


def _jsonld_contact_values(
    payload: object, field_name: str
) -> list[tuple[str, str, str, str]]:
    contacts: list[tuple[str, str, str, str]] = []
    accepted_types = {
        "contactpoint",
        "corporation",
        "localbusiness",
        "organization",
        "person",
        "professionalservice",
    }

    def visit(value: object, *, allowed: bool = False, edge: str = "", depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(value, list):
            for child in value[:250]:
                visit(child, allowed=allowed, edge=edge, depth=depth + 1)
            return
        if not isinstance(value, dict):
            return
        types = _json_types(value)
        node_allowed = allowed or bool(types & accepted_types) or edge.lower() == "contactpoint"
        person = "person" in types
        name = str(value.get("name") or "") if person else ""
        role = str(value.get("jobTitle") or value.get("roleName") or "") if person else ""
        context = " ".join(
            str(value.get(key) or "")
            for key in ("contactType", "description", "jobTitle", "roleName")
        )
        if node_allowed:
            for contact in _value_strings(value.get(field_name)):
                contacts.append((contact, context, name, role))
        for key, child in list(value.items())[:250]:
            if key == field_name:
                continue
            visit(
                child,
                allowed=node_allowed and key in {"contactPoint", "employee", "founder"},
                edge=key,
                depth=depth + 1,
            )

    visit(payload)
    return contacts


def _jsonld_contacts(payload: object) -> list[tuple[str, str, str, str]]:
    return _jsonld_contact_values(payload, "email")


def _jsonld_phones(payload: object) -> list[tuple[str, str, str, str]]:
    return _jsonld_contact_values(payload, "telephone")


def _buyer_role(role: str) -> str:
    lowered = role.lower()
    if any(
        token in lowered
        for token in (
            "founder",
            "owner",
            "chief executive",
            "ceo",
            "chief marketing",
            "cmo",
            "chief brand",
            "managing director",
            "president",
        )
    ):
        return "economic_buyer"
    if any(token in lowered for token in ("legal", "procurement", "rights", "operations")):
        return "delivery_risk"
    if any(
        token in lowered
        for token in (
            "growth",
            "performance",
            "marketing",
            "creative",
            "content",
            "production",
            "studio",
            "brand",
            "commerce",
            "social",
            "video",
            "photo",
        )
    ):
        return "champion"
    return "unknown"


def _person_evidence(
    *,
    name: str,
    role: str,
    source_url: str,
    method: str,
    profile_url: str | None = None,
    observed_at: datetime | None = None,
) -> PublicPersonEvidence | None:
    normalized_name = re.sub(r"\s+", " ", html.unescape(name)).strip(" |,;:-")
    normalized_role = re.sub(r"\s+", " ", html.unescape(role)).strip(" |,;:-")
    name_tokens = normalized_name.split()
    if (
        not 2 <= len(name_tokens) <= 6
        or not 3 <= len(normalized_name) <= 120
        or not 2 <= len(normalized_role) <= 200
        or "@" in normalized_name
        or sum(any(character.isalpha() for character in token) for token in name_tokens) < 2
        or normalized_name.lower() in {"our team", "leadership team", "meet the team"}
        or normalized_name.lower().startswith(("our ", "meet ", "the "))
        or BUYER_ROLE_PATTERN.fullmatch(normalized_name) is not None
    ):
        return None
    buyer_role = _buyer_role(normalized_role)
    if buyer_role == "unknown" and method == "semantic_team_card":
        return None
    safe_profile = None
    if profile_url:
        parsed = urlparse(profile_url)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            safe_profile = profile_url[:2_000]
    return PublicPersonEvidence(
        name=normalized_name[:120],
        role=normalized_role[:200],
        source_url=source_url[:2_000],
        method=method,
        confidence="high" if method in {"json_ld", "microdata"} else "medium",
        buyer_role=buyer_role,
        profile_url=safe_profile,
        observed_at=observed_at,
    )


def _jsonld_people(payload: object) -> list[tuple[str, str, str | None]]:
    people: list[tuple[str, str, str | None]] = []

    def visit(value: object, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(value, list):
            for child in value[:250]:
                visit(child, depth + 1)
            return
        if not isinstance(value, dict):
            return
        if "person" in _json_types(value):
            name = str(value.get("name") or "")
            role = str(value.get("jobTitle") or value.get("roleName") or "")
            same_as = value.get("sameAs")
            urls = _value_strings(same_as)
            profile_url = next((url for url in urls if url.startswith("http")), None)
            if name and role:
                people.append((name, role, profile_url))
        for child in list(value.values())[:250]:
            visit(child, depth + 1)

    visit(payload)
    return people


def extract_public_people(
    soup: BeautifulSoup,
    *,
    source_url: str,
    observed_at: datetime | None = None,
) -> list[PublicPersonEvidence]:
    """Extract only explicit first-party name/role pairs; never infer contact details."""

    found: list[PublicPersonEvidence] = []
    for script in soup.find_all("script"):
        if "ld+json" not in str(script.get("type") or "").lower():
            continue
        value = script.string or script.get_text(" ", strip=True)
        if not value or len(value) > 1_000_000:
            continue
        try:
            payload = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue
        for name, role, profile_url in _jsonld_people(payload):
            evidence = _person_evidence(
                name=name,
                role=role,
                source_url=source_url,
                method="json_ld",
                profile_url=profile_url,
                observed_at=observed_at,
            )
            if evidence:
                found.append(evidence)

    for node in soup.find_all(attrs={"itemtype": re.compile(r"Person", re.I)}):
        name_node = node.find(attrs={"itemprop": "name"})
        role_node = node.find(attrs={"itemprop": re.compile(r"jobTitle|roleName", re.I)})
        if not name_node or not role_node:
            continue
        evidence = _person_evidence(
            name=name_node.get_text(" ", strip=True),
            role=role_node.get_text(" ", strip=True),
            source_url=source_url,
            method="microdata",
            observed_at=observed_at,
        )
        if evidence:
            found.append(evidence)

    page_path = urlparse(source_url).path.lower()
    page_is_people_context = any(
        token in page_path for token in ("about", "leadership", "people", "team")
    )
    semantic_cards: list[Tag] = []
    for node in soup.find_all(["article", "li", "section", "div"]):
        classes = " ".join(str(value) for value in node.get("class", []))
        if page_is_people_context or SEMANTIC_PERSON_CLASS_RE.search(classes):
            semantic_cards.append(node)
    for card in semantic_cards[:250]:
        text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
        lowered = text.lower()
        if not text or len(text) > 900 or any(token in lowered for token in NON_PERSON_CONTEXT):
            continue
        role_match = BUYER_ROLE_PATTERN.search(text)
        if not role_match:
            continue
        name_node = card.find(["h2", "h3", "h4", "strong"])
        if not name_node:
            continue
        evidence = _person_evidence(
            name=name_node.get_text(" ", strip=True),
            role=role_match.group(0),
            source_url=source_url,
            method="semantic_team_card",
            observed_at=observed_at,
        )
        if evidence:
            found.append(evidence)

    by_identity: dict[tuple[str, str], PublicPersonEvidence] = {}
    method_rank = {"json_ld": 3, "microdata": 3, "vcard": 2, "semantic_team_card": 1}
    for person in found:
        key = (person.name.casefold(), person.role.casefold())
        current = by_identity.get(key)
        if current is None or method_rank[person.method] > method_rank[current.method]:
            by_identity[key] = person
    return sorted(
        by_identity.values(),
        key=lambda person: (
            {"economic_buyer": 3, "champion": 2, "delivery_risk": 1, "unknown": 0}[
                person.buyer_role
            ],
            method_rank[person.method],
            person.name,
        ),
        reverse=True,
    )[:20]


def _decode_cfemail(value: str) -> str | None:
    raw = value.strip()
    if len(raw) < 4 or len(raw) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", raw):
        return None
    try:
        key = int(raw[:2], 16)
        return "".join(
            chr(int(raw[index : index + 2], 16) ^ key) for index in range(2, len(raw), 2)
        )
    except (ValueError, OverflowError):
        return None


def _evidence_rank(item: PublicEmailEvidence) -> tuple[int, ...]:
    local = item.address.split("@", 1)[0]
    return (
        int(item.outreach_eligible),
        int(not item.historical),
        PURPOSE_RANK[item.purpose],
        LOCAL_PREFERENCE.get(local, 0),
        CONFIDENCE_RANK[item.confidence],
        int(item.domain_matches_site),
        METHOD_RANK[item.method],
        int(bool(item.person_role)),
    )


def _provenance_rank(item: PublicEmailEvidence) -> tuple[int, ...]:
    """Choose the strongest proof for one mailbox independently of mailbox priority."""

    return (
        int(not item.historical),
        int(item.domain_matches_site),
        CONFIDENCE_RANK[item.confidence],
        METHOD_RANK[item.method],
        int(item.outreach_eligible),
        PURPOSE_RANK[item.purpose],
    )


def merge_email_evidence(
    *groups: Iterable[PublicEmailEvidence],
) -> list[PublicEmailEvidence]:
    """Deduplicate by mailbox, keeping the strongest explicitly observed provenance."""

    by_address: dict[str, PublicEmailEvidence] = {}
    for item in (entry for group in groups for entry in group):
        current = by_address.get(item.address)
        if current is None or _provenance_rank(item) > _provenance_rank(current):
            replacement = item.model_copy(deep=True)
            if current:
                replacement.mx_observed = (
                    True
                    if True in {current.mx_observed, replacement.mx_observed}
                    else replacement.mx_observed
                )
                replacement.person_name = replacement.person_name or current.person_name
                replacement.person_role = replacement.person_role or current.person_role
            by_address[item.address] = replacement
        elif current:
            current.mx_observed = (
                True if True in {current.mx_observed, item.mx_observed} else current.mx_observed
            )
            current.person_name = current.person_name or item.person_name
            current.person_role = current.person_role or item.person_role
    return sorted(
        by_address.values(),
        key=lambda item: (_evidence_rank(item), item.address),
        reverse=True,
    )


def preferred_public_email(items: Iterable[PublicEmailEvidence]) -> str | None:
    eligible = [item for item in items if item.outreach_eligible and not item.historical]
    if not eligible:
        return None
    return max(eligible, key=_evidence_rank).address


def _phone_evidence_rank(item: PublicPhoneEvidence) -> tuple[int, ...]:
    return (
        int(item.outreach_eligible),
        int(not item.historical),
        PURPOSE_RANK[item.purpose],
        CONFIDENCE_RANK[item.confidence],
        PHONE_METHOD_RANK[item.method],
    )


def merge_phone_evidence(
    *groups: Iterable[PublicPhoneEvidence],
) -> list[PublicPhoneEvidence]:
    """Deduplicate explicit phone evidence by its normalized dial value."""

    by_number: dict[str, PublicPhoneEvidence] = {}
    for item in (entry for group in groups for entry in group):
        current = by_number.get(item.dial_number)
        if current is None or _phone_evidence_rank(item) > _phone_evidence_rank(current):
            by_number[item.dial_number] = item.model_copy(deep=True)
    return sorted(
        by_number.values(),
        key=lambda item: (_phone_evidence_rank(item), item.dial_number),
        reverse=True,
    )


def preferred_public_phone(items: Iterable[PublicPhoneEvidence]) -> str | None:
    eligible = [item for item in items if item.outreach_eligible and not item.historical]
    if not eligible:
        return None
    return max(eligible, key=_phone_evidence_rank).dial_number


def extract_public_phone_evidence(
    soup: BeautifulSoup,
    *,
    source_url: str,
    site_host: str,
    historical: bool = False,
    observed_at: datetime | None = None,
) -> list[PublicPhoneEvidence]:
    """Extract phone numbers only from explicit semantic contact fields."""

    found: list[PublicPhoneEvidence] = []

    def add(value: str, method: str, context: str = "") -> None:
        evidence = make_phone_evidence(
            value,
            source_url=source_url,
            site_host=site_host,
            method=method,
            context=context,
            historical=historical,
            observed_at=observed_at,
        )
        if evidence:
            found.append(evidence)

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if href.lower().startswith("tel:"):
            add(href, "tel", _context(anchor))

    for node in soup.find_all(True):
        markers = " ".join(
            str(node.get(attribute) or "") for attribute in ("itemprop", "name", "property")
        )
        tokens = {token for token in re.split(r"[^a-z0-9]+", markers.lower()) if token}
        if not {"phone", "telephone"} & tokens:
            continue
        values = [
            str(node.get(attribute) or "")
            for attribute in ("content", "href", "value")
            if node.get(attribute)
        ]
        values.append(node.get_text(" ", strip=True))
        for value in values:
            add(value, "microdata", _context(node))

    for script in soup.find_all("script"):
        if "ld+json" not in str(script.get("type") or "").lower():
            continue
        value = script.string or script.get_text(" ", strip=True)
        if not value or len(value) > 1_000_000:
            continue
        try:
            payload = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue
        for phone, context, _person_name, person_role in _jsonld_phones(payload):
            add(phone, "json_ld", " ".join((context, person_role)))

    return merge_phone_evidence(found)


def extract_public_email_evidence(
    soup: BeautifulSoup,
    raw_html: str,
    *,
    source_url: str,
    site_host: str,
    historical: bool = False,
    observed_at: datetime | None = None,
) -> list[PublicEmailEvidence]:
    """Extract explicit public emails without regex-scanning raw source or scripts.

    ``raw_html`` is accepted for the inspection integration contract, but intentionally is
    never searched wholesale. That prevents JavaScript strings and asset names from becoming
    contacts. If ``soup`` came from another document, callers should pass that document's raw
    HTML as well.
    """

    if not isinstance(raw_html, str):
        raise TypeError("raw_html must be text")
    found: list[PublicEmailEvidence] = []

    def add(
        value: str,
        method: str,
        context: str = "",
        *,
        person_name: str = "",
        person_role: str = "",
    ) -> None:
        evidence = make_email_evidence(
            value,
            source_url=source_url,
            site_host=site_host,
            method=method,
            context=context,
            historical=historical,
            observed_at=observed_at,
            person_name=person_name,
            person_role=person_role,
        )
        if evidence:
            found.append(evidence)

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        for value in _mailto_values(href):
            add(value, "mailto", _context(anchor))

    for node in soup.find_all(attrs={"data-cfemail": True}):
        decoded = _decode_cfemail(str(node.get("data-cfemail") or ""))
        if decoded:
            add(decoded, "encoded_markup", _context(node))
    for node in soup.find_all(attrs={"data-user": True, "data-domain": True}):
        user = html.unescape(str(node.get("data-user") or "")).strip()
        domain = html.unescape(str(node.get("data-domain") or "")).strip()
        if user and domain:
            add(f"{user}@{domain}", "encoded_markup", _context(node))

    microdata_nodes: list[Tag] = []
    for node in soup.find_all(True):
        markers = " ".join(
            str(node.get(attribute) or "") for attribute in ("itemprop", "name", "property")
        )
        tokens = {token for token in re.split(r"[^a-z0-9]+", markers.lower()) if token}
        if "email" in tokens:
            microdata_nodes.append(node)
    for node in microdata_nodes:
        values = [
            str(node.get(attribute) or "")
            for attribute in ("content", "href", "value")
            if node.get(attribute)
        ]
        values.append(node.get_text(" ", strip=True))
        for value in values:
            for match in EMAIL_CANDIDATE_RE.finditer(html.unescape(unquote(value))):
                add(match.group(1), "microdata", _context(node))

    for script in soup.find_all("script"):
        script_type = str(script.get("type") or "").lower()
        if "ld+json" not in script_type:
            continue
        value = script.string or script.get_text(" ", strip=True)
        if not value or len(value) > 1_000_000:
            continue
        try:
            payload = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue
        for address, context, person_name, person_role in _jsonld_contacts(payload):
            add(
                address,
                "json_ld",
                context,
                person_name=person_name,
                person_role=person_role,
            )

    for chunk in _visible_text_chunks(soup):
        for match in EMAIL_CANDIDATE_RE.finditer(chunk):
            start, end = match.span(1)
            add(match.group(1), "visible_text", chunk[max(0, start - 120) : end + 120])
        for match in OBFUSCATED_EMAIL_RE.finditer(chunk):
            domain = OBFUSCATED_DOT_RE.sub(".", match.group("domain"))
            start, end = match.span()
            add(
                f"{match.group('local')}@{domain}",
                "encoded_markup",
                chunk[max(0, start - 120) : end + 120],
            )
    return merge_email_evidence(found)


def extract_vcard_email_evidence(
    vcard_text: str,
    *,
    source_url: str,
    site_host: str,
    historical: bool = False,
    observed_at: datetime | None = None,
) -> list[PublicEmailEvidence]:
    """Parse only explicit EMAIL/FN/TITLE fields from a fetched public vCard."""

    unfolded = re.sub(r"\r?\n[ \t]", "", vcard_text)
    name = ""
    role = ""
    values: list[str] = []
    for line in unfolded.splitlines()[:500]:
        key, separator, value = line.partition(":")
        if not separator:
            continue
        field = key.split(";", 1)[0].upper()
        if field == "FN":
            name = value.strip()
        elif field in {"ROLE", "TITLE"}:
            role = value.strip()
        elif field == "EMAIL":
            values.append(value.strip())
    evidence = [
        item
        for value in values
        if (
            item := make_email_evidence(
                value,
                source_url=source_url,
                site_host=site_host,
                method="vcard",
                context=role,
                historical=historical,
                observed_at=observed_at,
                person_name=name,
                person_role=role,
            )
        )
    ]
    return merge_email_evidence(evidence)


def extract_vcard_phone_evidence(
    vcard_text: str,
    *,
    source_url: str,
    site_host: str,
    historical: bool = False,
    observed_at: datetime | None = None,
) -> list[PublicPhoneEvidence]:
    """Parse only explicit TEL and role fields from a fetched public vCard."""

    unfolded = re.sub(r"\r?\n[ \t]", "", vcard_text)
    role = ""
    values: list[str] = []
    for line in unfolded.splitlines()[:500]:
        key, separator, value = line.partition(":")
        if not separator:
            continue
        field = key.split(";", 1)[0].upper()
        if field in {"ROLE", "TITLE"}:
            role = value.strip()
        elif field == "TEL":
            values.append(value.strip())
    evidence = [
        item
        for value in values
        if (
            item := make_phone_evidence(
                value,
                source_url=source_url,
                site_host=site_host,
                method="vcard",
                context=role,
                historical=historical,
                observed_at=observed_at,
            )
        )
    ]
    return merge_phone_evidence(evidence)


def extract_contact_forms(soup: BeautifulSoup, *, source_url: str) -> list[str]:
    """Return the public page URL for contact-like forms without submitting anything."""

    for form in soup.find_all("form"):
        controls = form.find_all(["input", "select", "textarea"])
        markers = " ".join(
            " ".join(
                str(control.get(attribute) or "")
                for attribute in ("type", "name", "id", "placeholder")
            )
            for control in controls
        ).lower()
        has_email = "email" in markers
        has_message = any(
            token in markers for token in ("message", "project", "brief", "enquiry", "inquiry")
        )
        if has_email and (has_message or form.find("textarea")):
            return [source_url]
    return []


def build_contact_intelligence(
    emails: Iterable[PublicEmailEvidence],
    *,
    phones: Iterable[PublicPhoneEvidence] = (),
    people: Iterable[PublicPersonEvidence] = (),
    contact_pages_checked: Iterable[str] = (),
    contact_forms: Iterable[str] = (),
    errors: Iterable[str] = (),
) -> ContactIntelligence:
    merged = merge_email_evidence(emails)
    merged_phones = merge_phone_evidence(phones)
    merged_people: dict[tuple[str, str], PublicPersonEvidence] = {}
    for person in people:
        key = (person.name.casefold(), person.role.casefold())
        current = merged_people.get(key)
        if current is None or person.confidence == "high" and current.confidence != "high":
            merged_people[key] = person
    return ContactIntelligence(
        emails=merged,
        preferred_email=preferred_public_email(merged),
        phones=merged_phones,
        preferred_phone=preferred_public_phone(merged_phones),
        people=list(merged_people.values())[:20],
        contact_pages_checked=list(dict.fromkeys(contact_pages_checked))[:20],
        contact_forms=list(dict.fromkeys(contact_forms))[:10],
        errors=list(dict.fromkeys(errors))[:15],
    )


def merge_contact_intelligence(*items: ContactIntelligence) -> ContactIntelligence:
    """Merge page-level contact results without losing the strongest email provenance."""

    return build_contact_intelligence(
        (email for item in items for email in item.emails),
        phones=(phone for item in items for phone in item.phones),
        people=(person for item in items for person in item.people),
        contact_pages_checked=(url for item in items for url in item.contact_pages_checked),
        contact_forms=(url for item in items for url in item.contact_forms),
        errors=(error for item in items for error in item.errors),
    )


def extract_contact_intelligence(
    soup: BeautifulSoup,
    raw_html: str,
    *,
    source_url: str,
    site_host: str,
    historical: bool = False,
    observed_at: datetime | None = None,
) -> ContactIntelligence:
    emails = extract_public_email_evidence(
        soup,
        raw_html,
        source_url=source_url,
        site_host=site_host,
        historical=historical,
        observed_at=observed_at,
    )
    phones = extract_public_phone_evidence(
        soup,
        source_url=source_url,
        site_host=site_host,
        historical=historical,
        observed_at=observed_at,
    )
    people = [] if historical else extract_public_people(
        soup,
        source_url=source_url,
        observed_at=observed_at,
    )
    forms = [] if historical else extract_contact_forms(soup, source_url=source_url)
    return build_contact_intelligence(
        emails,
        phones=phones,
        people=people,
        contact_pages_checked=[source_url],
        contact_forms=forms,
    )
