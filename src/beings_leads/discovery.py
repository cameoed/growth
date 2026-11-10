from __future__ import annotations

import math
import re
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .config import INFRA_LABELS, PLATFORM_PROFILES, USER_AGENT
from .domain_admission import block_discovery_floods
from .domain_policy import is_disallowed
from .models import DiscoveredHost
from .provider_rate_limit import provider_request

HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
HEXISH_RE = re.compile(r"^(?:[a-f0-9]{20,}|[a-z0-9]{28,}|store-[a-f0-9]{4,})$")
STRONG_INFRA_TOKENS = {
    "api",
    "cdn",
    "demo",
    "fallback",
    "grafana",
    "internal",
    "localhost",
    "preview",
    "qa",
    "sandbox",
    "stage",
    "staging",
    "test",
    "testing",
    "vpn",
}
INFRA_PREFIX_RE = re.compile(
    r"^(?:api|cdn|demo|dev|internal|preview|qa|sandbox|stage|staging|stg|test|testing|vpn)[-_]"
)
PLATFORM_TEST_RE = re.compile(r"(?:^|[-_])test(?:shop)?\d*(?:[-_]|$)|shoplinetestshop\d*")
DEMANDWARE_ENVIRONMENT_LABELS = {
    "compact",
    "development",
    "ds",
    "ecommera",
    "pod1",
    "pod2",
    "pod3",
    "pod4",
    "production",
    "staging",
    "web",
}


class DiscoveryError(RuntimeError):
    pass


def normalize_apex(value: str) -> str:
    value = value.strip().lower().rstrip(".")
    value = re.sub(r"^https?://", "", value).split("/", 1)[0].split(":", 1)[0]
    if "." not in value or not HOST_RE.fullmatch(f"x.{value}"):
        raise ValueError(f"Invalid apex domain: {value!r}")
    return value


def load_apex_inputs(values: list[str]) -> list[str]:
    expanded: list[str] = []
    for value in values:
        path = Path(value)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    expanded.append(line)
        else:
            expanded.append(value)
    return list(dict.fromkeys(normalize_apex(value) for value in expanded))


def parse_crt_lines(apex: str, body: str) -> list[DiscoveredHost]:
    apex = normalize_apex(apex)
    if body.lstrip().lower().startswith("invalid apex"):
        raise DiscoveryError(body.strip())
    block_discovery_floods(
        line.strip().split("\t", 1)[0].lower().lstrip("*.").rstrip(".")
        for line in body.splitlines()
        if line.strip().split("\t", 1)[0].lower().rstrip(".").endswith(f".{apex}")
    )
    if is_disallowed(apex):
        return []
    candidates: dict[str, DiscoveredHost] = {}
    for raw_line in body.splitlines():
        parts = raw_line.strip().split("\t", 1)
        hostname = parts[0].lower().lstrip("*.").rstrip(".")
        if not hostname or hostname == apex or not hostname.endswith(f".{apex}"):
            continue
        if not HOST_RE.match(hostname):
            continue
        first_seen = None
        if len(parts) == 2 and parts[1]:
            with suppress(ValueError):
                first_seen = datetime.fromisoformat(parts[1].replace("Z", "+00:00"))
        label = _tenant_label(apex, hostname)
        candidate = DiscoveredHost(
            source_apex=apex,
            hostname=hostname,
            first_seen=first_seen,
            discovery_rank=_discovery_rank(apex, hostname, label, first_seen),
            label_hint=label,
        )
        if is_obvious_infrastructure_candidate(candidate):
            continue
        current = candidates.get(hostname)
        if current is None or (candidate.first_seen and not current.first_seen):
            candidates[hostname] = candidate
    return sorted(candidates.values(), key=lambda item: (-item.discovery_rank, item.hostname))


def _tenant_label(apex: str, hostname: str) -> str:
    relative = hostname[: -(len(apex) + 1)]
    labels = relative.split(".")
    if apex == "demandware.net":
        meaningful = [label for label in labels if label not in DEMANDWARE_ENVIRONMENT_LABELS]
        return meaningful[-1] if meaningful else labels[0]
    if apex == "myvtex.com":
        return labels[-1].rsplit("--", 1)[-1]
    return labels[0]


def is_obvious_infrastructure_candidate(candidate: DiscoveredHost) -> bool:
    """Identify obvious tenant infrastructure in both fresh and persisted inventories.

    Derived public-company candidates do not live beneath their discovery source apex, so
    they deliberately bypass tenant-label screening. The helper is conservative: it parks
    clear environments, tests, and opaque identifiers without deleting candidate history.
    """

    if is_disallowed(candidate.hostname):
        return True
    apex = candidate.source_apex.lower().rstrip(".")
    hostname = candidate.hostname.lower().rstrip(".")
    suffix = f".{apex}"
    if not hostname.endswith(suffix):
        return False

    relative_labels = hostname[: -len(suffix)].split(".")
    label = (candidate.label_hint or _tenant_label(apex, hostname)).strip().lower()
    if not label or len(label) < 2 or HEXISH_RE.fullmatch(label):
        return True

    if apex == "demandware.net":
        # Deployment/environment labels are part of the documented Demandware customer
        # hostname shape. Screen only the resolved customer identifier here.
        return label in INFRA_LABELS or bool(HEXISH_RE.fullmatch(label))

    for relative_label in relative_labels:
        if relative_label in INFRA_LABELS or INFRA_PREFIX_RE.search(relative_label):
            return True
        parts = {part for part in re.split(r"[-_]", relative_label) if part}
        if parts & STRONG_INFRA_TOKENS or PLATFORM_TEST_RE.search(relative_label):
            return True
    return label in INFRA_LABELS or bool(INFRA_PREFIX_RE.search(label))


def _discovery_rank(apex: str, hostname: str, label: str, first_seen: datetime | None) -> float:
    profile = PLATFORM_PROFILES.get(apex)
    score = float(profile.ability_prior + profile.recurring_prior if profile else 0)
    score += min(len(re.sub(r"[^a-z]", "", label)), 18) / 3
    if "-" in label:
        score += 1
    if (
        profile
        and profile.kind == "agency"
        and any(token in label for token in ("agency", "media", "creative", "studio"))
    ):
        score += 6
    if any(token in hostname for token in ("production", "prod.")):
        score += 3
    if re.search(r"\d{5,}", label):
        score -= 5
    if first_seen:
        age_years = max((datetime.now(UTC) - first_seen).days / 365.25, 0)
        score += max(0, 5 - math.log1p(age_years))
    return round(score, 3)


async def discover_apex(
    client: httpx.AsyncClient,
    apex: str,
    *,
    endpoint: str = "https://crt.name/v1/search",
    max_hosts: int = 500,
) -> list[DiscoveredHost]:
    if is_disallowed(apex):
        return []
    response = await provider_request(client, "GET",
        endpoint,
        params={"apex": apex, "dates": "1"},
        headers={"User-Agent": USER_AGENT, "Accept": "text/plain"},
    )
    response.raise_for_status()
    found = parse_crt_lines(apex, response.text)
    return found if max_hosts <= 0 else found[:max_hosts]
