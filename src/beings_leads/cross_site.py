from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from urllib.parse import parse_qs, unquote, urlparse

from .models import DiscoveredHost, SiteObservation

FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "hotmail.com",
    "icloud.com",
    "outlook.com",
    "proton.me",
    "yahoo.com",
}

# Shared identifiers are corroborating evidence, not proof that two sites are one company.
# Large account clusters are much more likely to be a platform widget, agency template, or
# centrally managed service than a safe company relationship.
MAX_SHARED_IDENTITY_FANOUT = 20
MAX_SHARED_SERVICE_FANOUT = 8
MAX_CROSS_SITE_SIGNALS = 8

_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$", re.I)
_YOUTUBE_CHANNEL_RE = re.compile(r"^[a-z0-9_-]{3,128}$", re.I)
_GENERATED_SHARED_RE = re.compile(r"^shared .+ across \d+ inspected sites$")
_PLACEHOLDER_WORDS = {
    "demo",
    "example",
    "fake",
    "placeholder",
    "sample",
    "test",
    "testing",
    "todo",
    "undefined",
    "unknown",
    "your",
    "yourid",
    "yourpixelid",
}
_PLACEHOLDER_PREFIXES = ("demo", "example", "fake", "placeholder", "sample", "test", "your")
_PLACEHOLDER_SEQUENCES = {
    "0123456789",
    "123456",
    "1234567",
    "12345678",
    "123456789",
    "1234567890",
}


def _parse_public_url(value: str):
    raw = value.strip()
    if not raw:
        return urlparse("")
    if raw.startswith("//"):
        raw = f"https:{raw}"
    elif "://" not in raw:
        raw = f"https://{raw.lstrip('/')}"
    return urlparse(raw)


def _host_matches(host: str, apex: str) -> bool:
    return host == apex or host.endswith(f".{apex}")


def _segments(path: str) -> list[str]:
    return [unquote(segment).strip() for segment in path.split("/") if segment.strip()]


def _handle(value: str, *, reserved: set[str]) -> str | None:
    normalized = value.strip().removeprefix("@").casefold()
    if (
        normalized in reserved
        or normalized.removesuffix(".php") in reserved
        or not _HANDLE_RE.fullmatch(normalized)
    ):
        return None
    return normalized


def _social_key(url: str) -> str | None:
    """Return a platform-qualified public account identity, never a generic action path."""

    parsed = _parse_public_url(url)
    host = (parsed.hostname or "").removeprefix("www.").lower().rstrip(".")
    parts = _segments(parsed.path)
    lowered = [part.casefold() for part in parts]
    if not host or not parts:
        return None

    generic_actions = {
        "about",
        "account",
        "accounts",
        "ads",
        "business",
        "compose",
        "dialog",
        "explore",
        "help",
        "home",
        "intent",
        "login",
        "logout",
        "privacy",
        "search",
        "share",
        "sharer",
        "signup",
    }

    if _host_matches(host, "linkedin.com"):
        if len(parts) < 2 or lowered[0] not in {"company", "in"}:
            return None
        identifier = _handle(parts[1], reserved=generic_actions | {"company", "in"})
        return f"linkedin.com/{lowered[0]}/{identifier}" if identifier else None

    if _host_matches(host, "youtube.com"):
        if parts[0].startswith("@"):
            identifier = _handle(parts[0], reserved=generic_actions)
            return f"youtube.com/@{identifier}" if identifier else None
        if (
            len(parts) >= 2
            and lowered[0] == "channel"
            and _YOUTUBE_CHANNEL_RE.fullmatch(parts[1])
        ):
            return f"youtube.com/channel/{parts[1]}"
        return None

    if _host_matches(host, "instagram.com"):
        reserved = generic_actions | {
            "developer",
            "directory",
            "p",
            "reel",
            "reels",
            "stories",
            "tv",
        }
        identifier = _handle(parts[0], reserved=reserved)
        return f"instagram.com/{identifier}" if identifier else None

    if _host_matches(host, "tiktok.com"):
        if not parts[0].startswith("@"):
            return None
        identifier = _handle(parts[0], reserved=generic_actions)
        return f"tiktok.com/@{identifier}" if identifier else None

    if _host_matches(host, "facebook.com"):
        reserved = generic_actions | {
            "events",
            "gaming",
            "groups",
            "marketplace",
            "people",
            "photo",
            "photos",
            "plugins",
            "reel",
            "reels",
            "sharer.php",
            "share.php",
            "watch",
        }
        if lowered[0] == "profile.php":
            profile_id = next(iter(parse_qs(parsed.query).get("id", [])), "").strip()
            return f"facebook.com/page/{profile_id}" if profile_id.isdigit() else None
        if lowered[0] in {"pages", "people"}:
            page_id = next((part for part in reversed(parts[1:]) if part.isdigit()), "")
            return f"facebook.com/page/{page_id}" if page_id else None
        if lowered[0] == "pg" and len(parts) >= 2:
            identifier = _handle(parts[1], reserved=reserved)
        else:
            identifier = _handle(parts[0], reserved=reserved)
        return f"facebook.com/{identifier}" if identifier else None

    if _host_matches(host, "twitter.com") or _host_matches(host, "x.com"):
        reserved = generic_actions | {
            "hashtag",
            "i",
            "messages",
            "notifications",
            "settings",
        }
        identifier = _handle(parts[0], reserved=reserved)
        return f"x.com/{identifier}" if identifier else None

    # youtu.be URLs identify individual videos, and unknown social hosts have no safe
    # platform-specific account grammar here.
    return None


def _normalized_host(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    return (parsed.hostname or "").removeprefix("www.").lower().rstrip(".")


def _company_key(observation: SiteObservation) -> str:
    return _normalized_host(observation.canonical_host or observation.hostname)


def _normalized_public_id(kind: str, value: str) -> str | None:
    normalized_kind = kind.strip().casefold()
    raw = str(value).strip()
    if not normalized_kind or not raw:
        return None
    normalized = raw.upper() if normalized_kind in {"google_tag", "tiktok_pixel"} else raw
    words = {
        word.casefold()
        for word in re.split(r"[^a-z0-9]+", normalized, flags=re.I)
        if word
    }
    compact = re.sub(r"[^A-Z0-9]", "", normalized.upper())
    if (
        words & _PLACEHOLDER_WORDS
        or any(word.startswith(_PLACEHOLDER_PREFIXES) for word in words)
        or compact in _PLACEHOLDER_SEQUENCES
        or not compact
    ):
        return None

    payload = normalized.upper()
    for prefix in ("GTM-", "G-", "UA-"):
        if payload.startswith(prefix):
            payload = payload.removeprefix(prefix)
            break
    payload = re.sub(r"[^A-Z0-9]", "", payload)
    if (
        (len(payload) >= 4 and len(set(payload)) == 1)
        or payload in _PLACEHOLDER_SEQUENCES
    ):
        return None

    if normalized_kind == "google_tag" and not re.fullmatch(
        r"(?:GTM-[A-Z0-9]+|G-[A-Z0-9]{6,}|UA-\d+-\d+)", normalized, re.I
    ):
        return None
    if normalized_kind in {"meta_pixel", "linkedin_partner"} and not normalized.isdigit():
        return None
    if normalized_kind == "tiktok_pixel" and not re.fullmatch(r"[A-Z0-9]{8,}", normalized):
        return None
    return normalized


def _is_generated_cross_site_label(label: str) -> bool:
    return label.startswith("links to another inspected company: ") or bool(
        _GENERATED_SHARED_RE.fullmatch(label)
    )


def _clear_generated_cross_site_signals(observation: SiteObservation) -> None:
    existing = observation.signals.get("cross_site", [])
    retained = [label for label in existing if not _is_generated_cross_site_label(label)]
    if retained:
        observation.signals["cross_site"] = retained[:MAX_CROSS_SITE_SIGNALS]
    else:
        observation.signals.pop("cross_site", None)


def _append_cross_site_signal(observation: SiteObservation, label: str) -> bool:
    existing = observation.signals.setdefault("cross_site", [])
    if label in existing or len(existing) >= MAX_CROSS_SITE_SIGNALS:
        return False
    existing.append(label)
    return True


def enrich_cross_site(
    records: Iterable[tuple[DiscoveredHost, SiteObservation]],
    *,
    strict: bool = True,
) -> dict[str, int]:
    items = list(records)
    memberships: dict[tuple[str, str], dict[str, set[int]]] = defaultdict(
        lambda: defaultdict(set)
    )
    canonical_to_indexes: dict[str, set[int]] = defaultdict(set)

    for _, observation in items:
        # Reused snapshots may carry labels produced by an older extractor. Recompute only
        # the in-memory derived signal; the stored historical snapshot remains immutable.
        _clear_generated_cross_site_signals(observation)

    for index, (_, observation) in enumerate(items):
        if not observation.live:
            continue
        company_key = _company_key(observation)
        if not company_key:
            continue
        canonical_to_indexes[company_key].add(index)
        for raw_kind, values in observation.public_ids.items():
            kind = raw_kind.strip().casefold()
            for value in values:
                normalized_id = _normalized_public_id(kind, value)
                if normalized_id:
                    memberships[("service", f"{kind}:{normalized_id}")][company_key].add(index)
        email_domains = {
            contact.address.rsplit("@", 1)[-1].lower().rstrip(".")
            for contact in observation.contacts.emails
            if contact.outreach_eligible
            and contact.domain_matches_site
            and not contact.historical
            and contact.confidence in {"medium", "high"}
        }
        if not strict and not observation.contacts.emails:
            email_domains.update(
                email.rsplit("@", 1)[-1].lower().rstrip(".")
                for email in observation.emails
                if "@" in email
            )
        for domain in email_domains:
            if domain and domain not in FREE_EMAIL_DOMAINS:
                memberships[("email", domain)][company_key].add(index)
        for url in observation.social_urls:
            key = _social_key(url)
            if key:
                memberships[("social", key)][company_key].add(index)

    accepted_memberships: list[tuple[str, str, dict[str, set[int]]]] = []
    for (kind, value), member_companies in memberships.items():
        fanout = len(member_companies)
        limit = MAX_SHARED_SERVICE_FANOUT if kind == "service" else MAX_SHARED_IDENTITY_FANOUT
        if 2 <= fanout <= limit:
            accepted_memberships.append((kind, value, member_companies))

    # Public account/email relationships are more discriminating than generic service IDs.
    # Add them first so an observation's bounded signal list cannot be filled by trackers.
    kind_priority = {"social": 0, "email": 1, "service": 2}
    accepted_memberships.sort(key=lambda item: (kind_priority[item[0]], item[1]))

    signals_added = 0
    for kind, value, member_companies in accepted_memberships:
        fanout = len(member_companies)
        if kind == "social":
            label = f"shared public social account {value} across {fanout} inspected sites"
        elif kind == "email":
            label = f"shared first-party email domain {value} across {fanout} inspected sites"
        else:
            label = f"shared public service/tracker {value} across {fanout} inspected sites"
        indexes = sorted(
            index for company_indexes in member_companies.values() for index in company_indexes
        )
        for index in indexes:
            if _append_cross_site_signal(items[index][1], label):
                signals_added += 1

    linked_pairs: set[tuple[str, str]] = set()
    for _, observation in items:
        if not observation.live:
            continue
        own_host = _company_key(observation)
        if not own_host:
            continue
        for raw_outbound in observation.outbound_domains:
            outbound = _normalized_host(raw_outbound)
            if outbound == own_host or outbound not in canonical_to_indexes:
                continue
            peers = canonical_to_indexes[outbound] - canonical_to_indexes.get(own_host, set())
            if not peers:
                continue
            label = f"links to another inspected company: {outbound}"
            if _append_cross_site_signal(observation, label):
                signals_added += 1
                linked_pairs.add((own_host, outbound))

    return {
        "records": len(items),
        "shared_keys": len(accepted_memberships),
        "linked_pairs": len(linked_pairs),
        "signals_added": signals_added,
    }
