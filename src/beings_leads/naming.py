from __future__ import annotations

import html
import re
from difflib import SequenceMatcher
from urllib.parse import urlsplit

TITLE_SEPARATOR_RE = re.compile(r"\s*(?:\||[–—•·]|::)\s*|\s+-\s+")
TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)?")
LEADING_BOILERPLATE_RE = re.compile(
    r"^(?:(?:welcome|thank you for coming)\s+to|official\s+(?:website|site)\s+(?:of|for)|"
    r"homepage\s+(?:of|for))\s+",
    re.I,
)
SOURCE_LABEL_RE = re.compile(
    r"(?:first-party work reference|public ai identity recovery|identity recovery|"
    r"certificate transparency|public record)",
    re.I,
)
OPAQUE_HOST_LABEL_RE = re.compile(
    r"^(?:store|shop|cdn|api|preview|test)[-_][a-z0-9]{7,}$",
    re.I,
)

HOSTED_SUFFIXES = (
    "3dcartstores.com",
    "hs-sites.com",
    "hs-sites-na2.com",
    "mybigcommerce.com",
    "myshopline.com",
    "s4shops.com",
    "shoplineapp.com",
)
COMMON_DOMAIN_PARTS = {
    "app",
    "ca",
    "co",
    "com",
    "global",
    "io",
    "ir",
    "net",
    "online",
    "org",
    "shop",
    "store",
    "uk",
    "us",
    "www",
}
BUSINESS_SUFFIXES = (
    "productions",
    "restaurants",
    "consulting",
    "marketing",
    "kitchens",
    "clothing",
    "creative",
    "artisans",
    "designs",
    "studios",
    "studio",
    "agency",
    "cellars",
    "shoppe",
    "films",
    "media",
    "video",
    "beads",
    "tiles",
    "group",
    "steel",
    "pub",
    "corp",
    "llc",
    "ltd",
    "inc",
)
GENERIC_HEADLINE_WORDS = {
    "best",
    "catering",
    "distributor",
    "leading",
    "official",
    "services",
    "solutions",
    "steakhouse",
    "top",
    "welcome",
    "wholesale",
}


def _compact(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _clean_title_fragment(value: str) -> str:
    cleaned = _clean_text(value).strip(" |–—•:-")
    cleaned = LEADING_BOILERPLATE_RE.sub("", cleaned)
    cleaned = re.sub(r"^#\s*1\s+", "", cleaned, flags=re.I)
    cleaned = re.sub(
        r"\s*[([](?:official|homepage|nonprofit|website)[])]\s*$",
        "",
        cleaned,
        flags=re.I,
    )
    return cleaned.strip(" |–—•:-")


def _humanize_slug(value: str) -> str:
    cleaned = _clean_text(value).strip("._- ")
    cleaned = re.sub(r"^www[-_]", "", cleaned, flags=re.I)
    cleaned = re.sub(
        r"[-_](?:com|net|org|co[-_]uk|com[-_]au|co[-_]nz)$",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"(?<=[A-Za-z])\d{3,}$", "", cleaned)
    cleaned = re.sub(r"[-_]+", " ", cleaned)
    if " " not in cleaned and cleaned.islower():
        remaining = cleaned
        suffix_words: list[str] = []
        while True:
            suffix = next(
                (
                    candidate
                    for candidate in BUSINESS_SUFFIXES
                    if remaining.endswith(candidate)
                    and len(remaining) - len(candidate) >= 3
                ),
                "",
            )
            if not suffix:
                break
            suffix_words.insert(0, suffix)
            remaining = remaining[: -len(suffix)]
        cleaned = " ".join([remaining, *suffix_words])
    if cleaned.islower() or cleaned.isupper():
        cleaned = cleaned.title()
    return _clean_text(cleaned)


def _clean_label_hint(value: str) -> str:
    cleaned = _clean_text(value)
    if not cleaned or SOURCE_LABEL_RE.search(cleaned):
        return ""
    cleaned = cleaned.split(" · ", 1)[0].strip()
    cleaned = re.split(r"\s+-\s+", cleaned, maxsplit=1)[0].strip()
    if OPAQUE_HOST_LABEL_RE.fullmatch(cleaned):
        return ""
    cleaned = _humanize_slug(cleaned)
    if _compact(cleaned) in {"home", "website", "unknown", "untitled"}:
        return ""
    return cleaned


def _normalized_host(value: str) -> str:
    raw = _clean_text(value).casefold()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    return (parsed.hostname or raw.split("/", 1)[0]).strip(".")


def _host_stem(value: str) -> str:
    host = _normalized_host(value)
    if not host:
        return ""
    for suffix in HOSTED_SUFFIXES:
        if host == suffix:
            return ""
        if host.endswith(f".{suffix}"):
            return host[: -(len(suffix) + 1)].split(".", 1)[0]
    parts = host.split(".")
    if len(parts) == 1:
        return parts[0]
    if len(parts) >= 3 and parts[-2] in {"co", "com", "net", "org"}:
        return parts[-3]
    return parts[-2]


def _identity_keys(
    *,
    canonical_domain: str,
    hostname: str,
    label_hint: str,
) -> list[str]:
    values = [
        _humanize_slug(_host_stem(canonical_domain)),
        _humanize_slug(_host_stem(hostname)),
    ]
    cleaned_label = _clean_label_hint(label_hint)
    if cleaned_label:
        values.append(cleaned_label)
    keys: list[str] = []
    for value in values:
        key = _compact(value)
        if len(key) >= 3 and key not in COMMON_DOMAIN_PARTS and key not in keys:
            keys.append(key)
    return keys


def _title_fragments(title: str) -> list[str]:
    return [
        cleaned
        for fragment in TITLE_SEPARATOR_RE.split(_clean_text(title))
        if (cleaned := _clean_title_fragment(fragment))
    ]


def _exact_identity_span(fragment: str, identity: str) -> str:
    matches = list(TOKEN_RE.finditer(fragment))
    best: tuple[int, str] | None = None
    for start in range(len(matches)):
        for end in range(start, min(len(matches), start + 8)):
            first = matches[start].start()
            last = matches[end].end()
            candidate = fragment[first:last]
            if _compact(candidate) != identity:
                continue
            tail = fragment[last:]
            legal_suffix = re.match(
                r"^\s*,?\s+(?:Inc\.?|LLC|Ltd\.?|Limited|Corp\.?|Corporation)\b",
                tail,
                flags=re.I,
            )
            if legal_suffix:
                last += legal_suffix.end()
                candidate = fragment[first:last]
            ranked = (len(_compact(candidate)), candidate.strip())
            if best is None or ranked[0] < best[0]:
                best = ranked
    return best[1] if best else ""


def _matched_title_name(title: str, identities: list[str]) -> str:
    fragments = _title_fragments(title)
    if not fragments or not identities:
        return ""
    title_tokens = TOKEN_RE.findall(_clean_text(title))
    title_words = {word.casefold() for word in title_tokens}
    if (
        len(_clean_text(title)) <= 60
        and len(title_tokens) <= 5
        and not TITLE_SEPARATOR_RE.search(_clean_text(title))
        and not GENERIC_HEADLINE_WORDS.intersection(title_words)
        and not _clean_text(title).startswith(("=", "+", "-", "@"))
    ):
        return _clean_title_fragment(title)

    exact: list[tuple[int, str]] = []
    for fragment in fragments:
        fragment_words = [word.casefold() for word in TOKEN_RE.findall(fragment)]
        for identity in identities:
            candidate = _exact_identity_span(fragment, identity)
            if candidate:
                if (
                    len(fragment_words) <= 4
                    and len(fragment) <= 50
                    and _compact(fragment).startswith(identity)
                    and not GENERIC_HEADLINE_WORDS.intersection(fragment_words)
                ):
                    exact.append((len(_compact(fragment)), fragment))
                else:
                    exact.append((len(_compact(candidate)), candidate))
    if exact:
        return min(exact, key=lambda item: item[0])[1]

    fuzzy: list[tuple[float, int, str]] = []
    for fragment in fragments:
        fragment_key = _compact(fragment)
        if not fragment_key or len(TOKEN_RE.findall(fragment)) > 8:
            continue
        score = max(
            SequenceMatcher(None, fragment_key, identity).ratio()
            for identity in identities
        )
        if score >= 0.58:
            fuzzy.append((score, -len(fragment_key), fragment))
    if fuzzy:
        return max(fuzzy)[2]
    return ""


def _concise_title_fallback(title: str) -> str:
    fragments = _title_fragments(title)
    for fragment in fragments:
        words = [word.casefold() for word in TOKEN_RE.findall(fragment)]
        if (
            1 <= len(words) <= 6
            and len(fragment) <= 60
            and not GENERIC_HEADLINE_WORDS.intersection(words)
        ):
            return fragment
    return ""


def concise_company_name(
    *,
    ai_name: str = "",
    label_hint: str = "",
    title: str = "",
    canonical_domain: str = "",
    hostname: str = "",
) -> str:
    """Return a short company identity while retaining page titles as evidence."""

    identities = _identity_keys(
        canonical_domain=canonical_domain,
        hostname=hostname,
        label_hint=label_hint,
    )
    trusted = _clean_text(ai_name)
    if trusted:
        if len(trusted) <= 80 and len(TOKEN_RE.findall(trusted)) <= 10:
            return trusted
        if matched := _matched_title_name(trusted, identities):
            return matched

    if matched := _matched_title_name(title, identities):
        return matched
    if label := _clean_label_hint(label_hint):
        return label
    if stem := _host_stem(canonical_domain):
        return _humanize_slug(stem)
    if stem := _host_stem(hostname):
        return _humanize_slug(stem)
    if concise_title := _concise_title_fallback(title):
        return concise_title
    return "Unknown company"
