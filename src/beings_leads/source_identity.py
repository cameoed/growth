from __future__ import annotations

import hashlib
import ipaddress
import re
import unicodedata
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

SOURCE_CRAWL_KINDS = frozenset(
    {
        "agency_directory",
        "brand_directory",
        "brand_group_portfolio",
        "platform_customer_list",
    }
)
SEARCH_QUERY_FINGERPRINT_VERSION = "search-query:v1"
LEGACY_SOURCE_IDENTITY_VERSION = "legacy:v1"

_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PERCENT_ESCAPE_RE = re.compile(r"%([0-9a-fA-F]{2})")
_INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9a-fA-F]{2})")
_UNRESERVED_BYTES = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_TRACKING_QUERY_KEYS = frozenset(
    {
        "dclid",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "redirected",
        "srsltid",
    }
)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_search_query(query: str) -> str:
    """Return stable matching text for a search query or repeatable-search recipe."""

    return _WHITESPACE_RE.sub(
        " ",
        unicodedata.normalize("NFKC", str(query)).casefold(),
    ).strip()


def search_query_fingerprint(query: str) -> str:
    """Return a versioned SHA-256 fingerprint for normalized search text."""

    normalized = normalize_search_query(query)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{SEARCH_QUERY_FINGERPRINT_VERSION}:{digest}"


def _canonical_dns_host(value: str) -> str | None:
    raw = unicodedata.normalize("NFKC", value).strip().casefold().rstrip(".")
    if not raw or any(character.isspace() for character in raw):
        return None
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        address = None
    if address is not None:
        return address.compressed
    try:
        host = raw.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return None
    labels = host.split(".")
    if len(labels) < 2 or len(host) > 253:
        return None
    if any(not _DNS_LABEL_RE.fullmatch(label) for label in labels):
        return None
    if host.startswith("www.") and host.count(".") >= 2:
        return host[4:]
    return host


def _canonical_apex(value: str | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if "://" in value or any(character in value for character in "/:@?#"):
        return None
    host = _canonical_dns_host(value)
    if host is None:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    return None


def _normalize_percent_encoding(value: str) -> str | None:
    if _INVALID_PERCENT_ESCAPE_RE.search(value):
        return None

    def replace(match: re.Match[str]) -> str:
        byte = int(match.group(1), 16)
        if byte in _UNRESERVED_BYTES:
            return chr(byte)
        return f"%{byte:02X}"

    return _PERCENT_ESCAPE_RE.sub(replace, value)


def _is_tracking_query_key(value: str) -> bool:
    key = value.casefold()
    return key.startswith("utm_") or key in _TRACKING_QUERY_KEYS


def canonical_source_url_identity(url: str) -> str | None:
    """Return a conservative, scheme-independent identity for one HTTP(S) source URL.

    The identity removes presentation and tracking variance while retaining path case,
    subdomains other than ``www``, non-default ports, and every functional query value.
    """

    if not isinstance(url, str) or not url or url != url.strip():
        return None
    if any(character.isspace() for character in url):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    host = _canonical_dns_host(parsed.hostname)
    if host is None:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    authority_host = f"[{host}]" if ":" in host else host
    authority = authority_host if port is None else f"{authority_host}:{port}"

    normalized_path = _normalize_percent_encoding(parsed.path or "/")
    if normalized_path is None:
        return None
    path = re.sub(r"/{2,}", "/", normalized_path).rstrip("/")

    if _INVALID_PERCENT_ESCAPE_RE.search(parsed.query):
        return None
    try:
        parsed_query_pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            errors="strict",
        )
    except UnicodeDecodeError:
        return None
    query_pairs = [
        (key, value) for key, value in parsed_query_pairs if not _is_tracking_query_key(key)
    ]
    query = urlencode(
        sorted(query_pairs),
        doseq=True,
        quote_via=quote,
        safe="~",
    )
    return f"{authority}{path}{f'?{query}' if query else ''}"


def legacy_source_identity_key(stable_token: str | int) -> str:
    """Return an auditable fallback identity for an invalid historical source row."""

    token = unicodedata.normalize("NFKC", str(stable_token)).strip()
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"{LEGACY_SOURCE_IDENTITY_VERSION}:{digest}"


def _fallback_token(
    *,
    source_kind: str,
    apex: str | None,
    url: str,
    discovery_mechanism: str,
) -> str:
    values = (source_kind, apex or "", url, discovery_mechanism)
    normalized = [unicodedata.normalize("NFKC", str(value)) for value in values]
    return "".join(f"{len(value)}:{value}" for value in normalized)


def source_identity_key(
    *,
    source_kind: str,
    apex: str | None,
    url: str,
    discovery_mechanism: str,
    legacy_token: str | int = "",
) -> str:
    """Return one execution-semantic identity for a source suggestion.

    Tenant discovery is keyed by apex, all public-crawl kinds share URL identity, and
    repeatable searches additionally include their normalized recipe fingerprint. Invalid
    legacy records remain distinct through a stable caller-supplied row or source-key token.
    """

    normalized_kind = str(source_kind).strip().casefold()
    if normalized_kind == "tenant_apex":
        if normalized_apex := _canonical_apex(apex):
            return f"tenant:{normalized_apex}"
    elif normalized_kind in SOURCE_CRAWL_KINDS:
        if normalized_url := canonical_source_url_identity(url):
            return f"crawl:{normalized_url}"
    elif normalized_kind == "repeatable_search" and (
        normalized_url := canonical_source_url_identity(url)
    ):
        recipe = search_query_fingerprint(discovery_mechanism)
        return f"search:{normalized_url}:{recipe}"

    token = legacy_token or _fallback_token(
        source_kind=normalized_kind,
        apex=apex,
        url=url,
        discovery_mechanism=discovery_mechanism,
    )
    return legacy_source_identity_key(token)


__all__ = [
    "LEGACY_SOURCE_IDENTITY_VERSION",
    "SEARCH_QUERY_FINGERPRINT_VERSION",
    "SOURCE_CRAWL_KINDS",
    "canonical_source_url_identity",
    "legacy_source_identity_key",
    "normalize_search_query",
    "search_query_fingerprint",
    "source_identity_key",
]
