from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_UP, Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from .models import AtomicObservation, EvidenceArtifact, ScannerCoverageManifest
from .provider_rate_limit import ProviderRateLimitDeferred, provider_request
from .rehydration import REHYDRATION_DAYS

SCANNER_ID = "treg_people_intelligence_v1"
SCANNER_VERSION = "1.0.0"
EXTRACTOR_VERSION = "treg-icypeas-people-v1"
RULE_VERSION = "beings-buyer-role-v1"
REFRESH_DAYS = REHYDRATION_DAYS
MAX_COHORT_SIZE = 2_500
DEFAULT_PEOPLE_LIMIT = 8
MAX_PEOPLE_LIMIT = 12
DEFAULT_EMAIL_LIMIT = 2
MAX_EMAIL_LIMIT = 2
MIN_GOOD_CONTACTS = 2
DISCOVERY_UNIT_COST_USD = Decimal("0.00038")
DEFAULT_EMAIL_MAX_COST_USD = Decimal("0.025")
MAX_EMAIL_MAX_COST_USD = Decimal("0.08")
BASE_URL = "https://treg.to"
PEOPLE_SEARCH_ENDPOINT = "icypeas.people.search"
EMAIL_FIND_ENDPOINT = "treg.people.email.find"
PEOPLE_SEARCH_SOURCE_URL = "https://treg.to/people-search"
MAX_RESPONSE_BYTES = 2_000_000

ECONOMIC_TITLES: tuple[str, ...] = (
    "Founder",
    "Co-Founder",
    "Owner",
    "Chief Executive Officer",
    "President",
    "Managing Director",
    "Chief Operating Officer",
    "General Manager",
)

CHAMPION_TITLES: tuple[str, ...] = (
    "Chief Marketing Officer",
    "Chief Brand Officer",
    "Chief Creative Officer",
    "Chief Content Officer",
    "Chief Growth Officer",
    "VP Marketing",
    "Head of Marketing",
    "VP Brand",
    "Head of Brand",
    "VP Creative",
    "Head of Creative",
    "VP Content",
    "Head of Content",
    "VP Growth",
    "Head of Growth",
    "VP Production",
    "Head of Production",
    "Executive Producer",
    "Studio Director",
)

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_LEADERSHIP_RE = re.compile(
    r"\b(?:co[ -]?founder|founder|owner|chief executive officer|ceo|"
    r"managing director|chief operating officer|coo|general manager|managing partner|"
    r"(?<!vice )president)\b"
)
_SENIORITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("c_suite", re.compile(r"\b(?:chief|ceo|cmo|cco|cgo|coo)\b")),
    ("founder_owner", re.compile(r"\b(?:co[ -]?founder|founder|owner)\b")),
    ("vp", re.compile(r"\b(?:vice president|vp|svp|evp)\b")),
    ("president_partner", re.compile(r"\b(?:(?<!vice )president|managing partner)\b")),
    ("head", re.compile(r"\bhead\b")),
    ("director", re.compile(r"\b(?:director|executive producer|general manager)\b")),
)
_FUNCTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("marketing", re.compile(r"\b(?:marketing|demand gen(?:eration)?|lifecycle|acquisition)\b")),
    ("brand", re.compile(r"\bbrand\b")),
    ("creative", re.compile(r"\b(?:creative|design|art director)\b")),
    ("content", re.compile(r"\b(?:content|editorial|publishing)\b")),
    ("growth", re.compile(r"\bgrowth\b")),
    ("production", re.compile(r"\b(?:production|producer|studio|creative operations)\b")),
    ("commerce", re.compile(r"\b(?:e[ -]?commerce|commerce|merchandising)\b")),
    ("social_creator", re.compile(r"\b(?:social media|influencer|creator|community)\b")),
    ("communications", re.compile(r"\b(?:communications|comms|public relations|pr)\b")),
)
_UNRELATED_RE = re.compile(
    r"\b(?:engineering|software|developer|information technology|it|security|finance|"
    r"accounting|legal|counsel|people|human resources|hr|talent|recruit|sales|revenue|"
    r"customer success|procurement|supply chain|medical|clinical)\b"
)
_AGENCY_FUNCTION_RE = re.compile(
    r"\b(?:client services|account management|delivery|creative operations|production|"
    r"studio|executive producer|integrated producer)\b"
)


class TregPeopleError(RuntimeError):
    """A sanitized Treg transport or response-contract failure."""

    def __init__(self, message, *, kind="invalid_response", http_status=None,
                 call_id="", charged_microusd=None, request_submitted=None):
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status
        self.call_id = call_id[:200]
        self.charged_microusd = charged_microusd
        self.request_submitted = request_submitted


@dataclass(frozen=True, slots=True)
class TregCredentials:
    token: str = field(repr=False)
    org: str = ""
    base_url: str = BASE_URL


@dataclass(frozen=True, slots=True)
class RoleClassification:
    lane: Literal["economic_buyer", "marketing_creative_champion", "agency_operator"]
    function: str
    seniority: str
    score: int
    reason: str


@dataclass(frozen=True, slots=True)
class ProviderPerson:
    first_name: str
    last_name: str
    title: str
    linkedin_url: str
    company_name: str = ""
    company_website: str = ""
    location: str = ""
    provider: str = "IcyPeas"
    company_domain_match: bool | None = None

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part).strip()


@dataclass(frozen=True, slots=True)
class HydratedPerson:
    first_name: str
    last_name: str
    title: str
    linkedin_url: str
    company_name: str
    location: str
    provider: str
    lane: str
    function: str
    seniority: str
    role_score: int
    role_reason: str
    company_domain_match: bool | None
    work_email: str = ""
    email_status: Literal["not_attempted", "found", "not_found", "unverified"] = "not_attempted"
    email_provider: str = ""
    email_call_id: str = ""

    full_name = ProviderPerson.full_name


@dataclass(frozen=True, slots=True)
class TregSearchResult:
    people: tuple[ProviderPerson, ...]
    total: int
    charged_microusd: int
    call_id: str
    response_hash: str
    response_bytes: int


@dataclass(frozen=True, slots=True)
class TregEmailResult:
    email: str
    provider: str
    charged_microusd: int
    call_id: str
    response_hash: str
    response_bytes: int
    pending: bool = False
    verified: bool = False
    candidate_present: bool = False


class TregUnsettledError(TregPeopleError):
    """An existing call must be reconciled, never resubmitted with a new key."""

    def __init__(self, call_id: str = "", *, metadata: Mapping | None = None) -> None:
        pending = bool(metadata and (metadata.get("pending")
                                     or metadata.get("outcome") == "pending"))
        super().__init__("Treg call is pending or its charge is unknown; do not resubmit",
                         kind="pending" if pending else "unsettled", call_id=call_id)
        self.call_id = call_id[:200]
        self.metadata = dict(metadata or {})


@dataclass(frozen=True, slots=True)
class TregPeopleProjection:
    artifacts: tuple[EvidenceArtifact, ...]
    coverage_manifests: tuple[ScannerCoverageManifest, ...]
    observations: tuple[AtomicObservation, ...]


def resolve_treg_credentials(
    *,
    environ: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> TregCredentials | None:
    env = os.environ if environ is None else environ
    token = str(env.get("TREG_TOKEN") or "").strip()
    org = str(env.get("TREG_ORG") or "").strip()[:120]
    base_url = str(env.get("TREG_BASE_URL") or BASE_URL).strip().rstrip("/")
    path = config_path or Path.home() / ".treg" / "config.json"
    config: Mapping[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping):
                config = loaded
        except (OSError, ValueError):
            config = {}
    token = token or str(config.get("token") or "").strip()
    if not token:
        return None
    org = org or str(config.get("active_org") or "").strip()[:120]
    base_url = str(config.get("base_url") or base_url).strip().rstrip("/") if not env.get(
        "TREG_BASE_URL"
    ) else base_url
    if not base_url.startswith("https://"):
        raise TregPeopleError("Treg base URL must use HTTPS")
    return TregCredentials(token=token, org=org, base_url=base_url)


def classify_role(title: str, *, company_archetype: str | None = None) -> RoleClassification | None:
    normalized = " ".join(str(title or "").lower().replace("&", " and ").split())
    if not normalized:
        return None
    functions = [name for name, pattern in _FUNCTION_PATTERNS if pattern.search(normalized)]
    seniority = next(
        (name for name, pattern in _SENIORITY_PATTERNS if pattern.search(normalized)),
        "",
    )
    leadership = bool(_LEADERSHIP_RE.search(normalized))
    agency = "agency" in str(company_archetype or "").lower()
    agency_operator = agency and bool(_AGENCY_FUNCTION_RE.search(normalized)) and bool(seniority)

    if leadership:
        score = 100 if seniority in {"founder_owner", "c_suite"} else 94
        return RoleClassification(
            lane="economic_buyer",
            function="executive_leadership",
            seniority=seniority or "executive",
            score=score,
            reason="Senior company leadership can own or approve external creative capacity.",
        )
    if not functions and not agency_operator:
        return None
    if not seniority:
        return None
    if _UNRELATED_RE.search(normalized) and not functions and not agency_operator:
        return None
    if agency_operator:
        score = 91 if seniority in {"c_suite", "vp", "head"} else 85
        return RoleClassification(
            lane="agency_operator",
            function="agency_delivery",
            seniority=seniority,
            score=score,
            reason=(
                "Senior agency delivery or production leadership can sponsor "
                "specialist capacity."
            ),
        )
    function = functions[0]
    seniority_score = {
        "c_suite": 98,
        "president_partner": 95,
        "vp": 92,
        "head": 90,
        "director": 82,
        "founder_owner": 96,
    }.get(seniority, 75)
    function_bonus = 2 if function in {"creative", "content", "production", "brand"} else 0
    return RoleClassification(
        lane="marketing_creative_champion",
        function=function,
        seniority=seniority,
        score=min(100, seniority_score + function_bonus),
        reason=(
            "Senior marketing, brand, creative, content, growth, commerce, or production scope "
            "aligns with Beings' recurring creative-capacity offer."
        ),
    )


def _normalized_domain(value: object) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    return (parsed.hostname or "").lower().rstrip(".").removeprefix("www.")


def _text(value: object, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]


def parse_people_search_response(
    payload: object,
    *,
    requested_domain: str,
    charged_microusd: int = 0,
    call_id: str = "",
    raw: bytes | None = None,
) -> TregSearchResult:
    if not isinstance(payload, Mapping):
        raise TregPeopleError("Treg people search returned an invalid response")
    if payload.get("success") is False:
        raise TregPeopleError("Treg people search provider reported failure")
    raw_leads = payload.get("leads", [])
    if not isinstance(raw_leads, Sequence) or isinstance(raw_leads, (str, bytes, bytearray)):
        raise TregPeopleError("Treg people search returned an invalid leads collection")
    domain = _normalized_domain(requested_domain)
    if not domain:
        raise ValueError("A valid company domain is required")
    people: list[ProviderPerson] = []
    for item in raw_leads:
        if not isinstance(item, Mapping):
            continue
        first_name = _text(item.get("firstname") or item.get("firstName"), 100)
        last_name = _text(item.get("lastname") or item.get("lastName"), 100)
        title = _text(item.get("lastJobTitle") or item.get("title") or item.get("headline"), 240)
        profile_url = _text(item.get("profileUrl") or item.get("linkedinUrl"), 1_000)
        if not first_name or not last_name or not title:
            continue
        if profile_url and _normalized_domain(profile_url) not in {
            "linkedin.com",
            "www.linkedin.com",
        }:
            profile_url = ""
        company_website = _text(
            item.get("lastCompanyWebsite") or item.get("currentCompanyWebsite"), 1_000
        )
        returned_domain = _normalized_domain(company_website)
        domain_match = (
            None
            if not returned_domain
            else returned_domain == domain or returned_domain.endswith(f".{domain}")
        )
        if domain_match is False:
            continue
        people.append(
            ProviderPerson(
                first_name=first_name,
                last_name=last_name,
                title=title,
                linkedin_url=profile_url,
                company_name=_text(
                    item.get("lastCompanyName") or item.get("currentCompanyName"), 200
                ),
                company_website=company_website,
                location=_text(item.get("address") or item.get("location"), 240),
                company_domain_match=domain_match,
            )
        )
    try:
        total = max(0, int(payload.get("total") or len(raw_leads)))
    except (TypeError, ValueError):
        total = len(raw_leads)
    body = raw or json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return TregSearchResult(
        people=tuple(people),
        total=total,
        charged_microusd=max(0, int(charged_microusd)),
        call_id=call_id[:200],
        response_hash=hashlib.sha256(body).hexdigest(),
        response_bytes=len(body),
    )


def select_people(
    rows: Sequence[ProviderPerson],
    *,
    company_archetype: str | None = None,
    limit: int = DEFAULT_PEOPLE_LIMIT,
) -> tuple[HydratedPerson, ...]:
    bounded = max(1, min(int(limit), MAX_PEOPLE_LIMIT))
    deduplicated: dict[str, HydratedPerson] = {}
    for row in rows:
        classification = classify_role(row.title, company_archetype=company_archetype)
        if classification is None:
            continue
        identity = row.linkedin_url.lower().rstrip("/") or (
            f"{row.first_name.lower()}|{row.last_name.lower()}|{row.title.lower()}"
        )
        candidate = HydratedPerson(
            first_name=row.first_name,
            last_name=row.last_name,
            title=row.title,
            linkedin_url=row.linkedin_url,
            company_name=row.company_name,
            location=row.location,
            provider=row.provider,
            lane=classification.lane,
            function=classification.function,
            seniority=classification.seniority,
            role_score=classification.score,
            role_reason=classification.reason,
            company_domain_match=row.company_domain_match,
        )
        current = deduplicated.get(identity)
        if current is None or candidate.role_score > current.role_score:
            deduplicated[identity] = candidate
    lane_order = {"economic_buyer": 0, "marketing_creative_champion": 1, "agency_operator": 2}
    ranked = sorted(
        deduplicated.values(),
        key=lambda person: (
            -person.role_score,
            lane_order.get(person.lane, 9),
            person.last_name.lower(),
            person.first_name.lower(),
        ),
    )
    if not ranked:
        return ()
    # Keep both buying lanes visible before filling the remaining slots by score.
    selected: list[HydratedPerson] = []
    for lane in ("economic_buyer", "marketing_creative_champion", "agency_operator"):
        match = next((person for person in ranked if person.lane == lane), None)
        if match is not None and match not in selected:
            selected.append(match)
    selected.extend(person for person in ranked if person not in selected)
    return tuple(selected[:bounded])


def email_candidates(
    people: Sequence[HydratedPerson],
    *,
    limit: int = DEFAULT_EMAIL_LIMIT,
) -> tuple[HydratedPerson, ...]:
    bounded = max(0, min(int(limit), MAX_EMAIL_LIMIT))
    if not bounded:
        return ()
    selected: list[HydratedPerson] = []
    for lanes in (
        {"economic_buyer"},
        {"marketing_creative_champion", "agency_operator"},
    ):
        match = next(
            (
                person
                for person in people
                if person.lane in lanes and person not in selected and person.full_name
            ),
            None,
        )
        if match is not None:
            selected.append(match)
    selected.extend(
        person for person in people if person not in selected and person.full_name
    )
    return tuple(selected[:bounded])


def _find_email(value: object) -> str:
    if isinstance(value, Mapping):
        preferred = (
            "email",
            "work_email",
            "workEmail",
            "email_address",
            "emailAddress",
        )
        for key in preferred:
            candidate = _text(value.get(key), 320)
            if _EMAIL_RE.fullmatch(candidate):
                return candidate.lower()
        for nested in value.values():
            candidate = _find_email(nested)
            if candidate:
                return candidate
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            candidate = _find_email(nested)
            if candidate:
                return candidate
    return ""


def parse_email_response(
    payload: object,
    *,
    charged_microusd: int | None = None,
    call_id: str = "",
    served_by: str = "",
    raw: bytes | None = None,
) -> TregEmailResult:
    if not isinstance(payload, Mapping):
        raise TregPeopleError("Treg email finder returned an invalid response")
    output = payload.get("output")
    route = payload.get("_treg")
    route = route if isinstance(route, Mapping) else {}
    verification = route.get("verification")
    verification = verification if isinstance(verification, Mapping) else {}
    pending = route.get("outcome") == "pending" or verification.get("pending") is True
    verified = (
        verification.get("checked") is True and verification.get("verdict") == "valid"
        if verification else isinstance(output, Mapping) and output.get("verified") is True
    )
    email = _find_email(output) if verified and not pending else ""
    provider = served_by
    if not provider:
        provider = _text(route.get("served_by"), 120)
    if charged_microusd is None:
        try:
            charged_microusd = int(route["charged_micro"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TregUnsettledError(
                call_id or _text(route.get("call_id"), 200), metadata=route
            ) from exc
    body = raw or json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return TregEmailResult(
        email=email,
        provider=provider[:120],
        charged_microusd=max(0, int(charged_microusd)),
        call_id=call_id[:200],
        response_hash=hashlib.sha256(body).hexdigest(),
        response_bytes=len(body),
        pending=pending,
        verified=bool(verified and not pending),
        candidate_present=bool(_find_email(output)),
    )


class TregPeopleClient:
    def __init__(
        self,
        credentials: TregCredentials,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not credentials.token:
            raise ValueError("Treg token is required")
        self.credentials = credentials
        self.client = client

    async def _post(
        self,
        endpoint_id: str,
        payload: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
        idempotency_key: str,
    ) -> tuple[object, bytes, Mapping[str, str]]:
        if endpoint_id not in {PEOPLE_SEARCH_ENDPOINT, EMAIL_FIND_ENDPOINT}:
            raise ValueError("Unsupported Treg people endpoint")
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=httpx.Timeout(45.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
        )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Treg-Token": self.credentials.token,
            "X-Treg-Client": "beings-growth",
            "X-Treg-Meta": "workspace=beings-growth,workflow=people-hydration",
            "Idempotency-Key": idempotency_key[:200],
        }
        if self.credentials.org:
            headers["X-Treg-Org"] = self.credentials.org
        headers.update(extra_headers or {})
        try:
            response = await provider_request(client, "POST",
                f"{self.credentials.base_url}/call/{endpoint_id}",
                headers=headers,
                json=dict(payload),
            )
            raw = bytes(response.content)
            try:
                response_charge = self._charged(response.headers)
            except TregUnsettledError:
                response_charge = None
            response_error = {
                "http_status": response.status_code,
                "call_id": response.headers.get("X-Treg-Call-Id", ""),
                "charged_microusd": response_charge,
                "request_submitted": True,
            }
            if len(raw) > MAX_RESPONSE_BYTES:
                raise TregPeopleError("Treg response exceeded the configured size limit",
                                      **response_error)
            if response.status_code >= 400:
                kind = {401: "access_denied", 403: "access_denied",
                        402: "insufficient_balance", 429: "rate_limited"}.get(
                            response.status_code, "provider_error")
                try:
                    charged = self._charged(response.headers)
                except TregUnsettledError:
                    charged = None
                raise TregPeopleError(
                    f"Treg people request failed with HTTP {response.status_code}",
                    kind=kind, http_status=response.status_code,
                    call_id=response.headers.get("X-Treg-Call-Id", ""),
                    charged_microusd=charged,
                    request_submitted=True,
                )
            try:
                parsed = response.json()
            except ValueError as exc:
                raise TregPeopleError("Treg people request returned invalid JSON",
                                      **response_error) from exc
            route = parsed.get("_treg") if isinstance(parsed, Mapping) else None
            route = route if isinstance(route, Mapping) else {}
            if (
                response.status_code == 202
                or route.get("outcome") == "pending"
            ):
                metadata = dict(route)
                metadata["pending"] = True
                if response.headers.get("X-Treg-Async"):
                    metadata["async_descriptor"] = response.headers["X-Treg-Async"]
                raise TregUnsettledError(
                    response.headers.get("X-Treg-Call-Id", "")
                    or _text(route.get("call_id"), 200), metadata=metadata,
                )
            return parsed, raw, response.headers
        except ProviderRateLimitDeferred as exc:
            raise TregPeopleError("Treg requests are rate-limited; retry deferred",
                                 kind="rate_limited", charged_microusd=0,
                                 request_submitted=False) from exc
        except httpx.TimeoutException as exc:
            raise TregPeopleError("Treg people request timed out", kind="timeout") from exc
        except httpx.RequestError as exc:
            raise TregPeopleError("Treg people request could not be completed",
                                 kind="connection_error") from exc
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    def _charged(headers: Mapping[str, str]) -> int:
        try:
            value = int(headers["X-Treg-Cost-Micro"])
            if value < 0:
                raise ValueError("Negative charge")
            return value
        except (KeyError, TypeError, ValueError) as exc:
            raise TregUnsettledError(headers.get("X-Treg-Call-Id", "")) from exc

    async def search_people(
        self,
        *,
        domain: str,
        titles: Sequence[str],
        limit: int,
        idempotency_key: str,
        name: tuple[str, str] | None = None,
    ) -> TregSearchResult:
        normalized_domain = _normalized_domain(domain)
        bounded = max(1, min(int(limit), MAX_PEOPLE_LIMIT))
        normalized_titles = list(
            dict.fromkeys(_text(title, 160) for title in titles if _text(title, 160))
        )
        if not normalized_domain or not (normalized_titles or name):
            raise ValueError("People search requires a domain and a name or title")
        query = {"currentCompanyWebsite": {"include": [normalized_domain]}}
        if normalized_titles:
            query["currentJobTitle"] = {"include": normalized_titles}
        if name:
            query.update(firstname={"include": [name[0]]}, lastname={"include": [name[1]]})
        payload, raw, headers = await self._post(
            PEOPLE_SEARCH_ENDPOINT,
            {
                "query": query,
                "pagination": {"size": bounded},
            },
            extra_headers={
                "X-Treg-Route-Max-Cost": format(DISCOVERY_UNIT_COST_USD * bounded, "f"),
            },
            idempotency_key=idempotency_key,
        )
        charged = self._charged(headers)
        try:
            return parse_people_search_response(
                payload, requested_domain=normalized_domain, charged_microusd=charged,
                call_id=headers.get("X-Treg-Call-Id", ""), raw=raw,
            )
        except TregPeopleError as exc:
            exc.call_id = headers.get("X-Treg-Call-Id", "")[:200]
            exc.charged_microusd = charged
            raise

    async def find_work_email(
        self,
        *,
        domain: str,
        person: ProviderPerson | HydratedPerson,
        max_cost_usd: Decimal = DEFAULT_EMAIL_MAX_COST_USD,
        idempotency_key: str,
    ) -> TregEmailResult:
        maximum = Decimal(str(max_cost_usd))
        if not maximum.is_finite() or maximum <= 0 or maximum > MAX_EMAIL_MAX_COST_USD:
            raise ValueError("Email route maximum must be between $0 and $0.08")
        body: dict[str, str] = {
            "domain": _normalized_domain(domain),
            "first_name": person.first_name,
            "last_name": person.last_name,
            "full_name": person.full_name,
        }
        if person.linkedin_url:
            body["linkedin_url"] = person.linkedin_url
        payload, raw, headers = await self._post(
            EMAIL_FIND_ENDPOINT,
            body,
            extra_headers={
                "X-Treg-Route-Max-Cost": format(maximum, "f"),
                "X-Treg-Route-Exclude": "leadmagic.x.personal-email-finder",
                "X-Treg-Route-Verify": "true",
            },
            idempotency_key=idempotency_key,
        )
        charged = self._charged(headers)
        try:
            return parse_email_response(
                payload, charged_microusd=charged,
                call_id=headers.get("X-Treg-Call-Id", ""),
                served_by=headers.get("X-Treg-Served-By", ""), raw=raw,
            )
        except TregPeopleError as exc:
            if not isinstance(exc, TregUnsettledError):
                exc.call_id = headers.get("X-Treg-Call-Id", "")[:200]
                exc.charged_microusd = charged
            raise

    async def _archived_call(self, call_id: str) -> tuple[object, int] | None:
        """Read the existing answer without submitting another provider request.

        A missing archive or unknown original charge retains the reservation.
        The archive is not itself a mailbox-verification claim.
        """
        if not re.fullmatch(r"(?:[0-9]+|[a-fA-F0-9]{32})", call_id):
            return None
        headers = {"X-Treg-Token": self.credentials.token}
        if self.credentials.org:
            headers["X-Treg-Org"] = self.credentials.org
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=10, follow_redirects=False, trust_env=False
        )
        try:
            response = await client.get(
                f"{self.credentials.base_url}/calls/{call_id}/result", headers=headers
            )
            if response.status_code != 200 or len(response.content) > MAX_RESPONSE_BYTES:
                return None
            archive = response.json()
            if not isinstance(archive, Mapping) or archive.get("stored") is not True:
                return None
            body = archive.get("response")
            original_headers = body.get("headers", {}) if isinstance(body, Mapping) else {}
            if isinstance(body, Mapping) and "body" in body:
                body = body["body"]
            if isinstance(body, str):
                body = json.loads(body)
            route = body.get("_treg") if isinstance(body, Mapping) else None
            route = route if isinstance(route, Mapping) else {}
            if route.get("outcome") == "pending":
                return None
            headers = (
                httpx.Headers(original_headers) if isinstance(original_headers, Mapping) else {}
            )
            amount = headers.get("X-Treg-Cost-Micro", route.get("charged_micro"))
            if amount is None:
                return None
            charged = int(amount)
            return (body, charged) if charged >= 0 else None
        except (httpx.HTTPError, ValueError, TregPeopleError):
            return None
        finally:
            if owns_client:
                await client.aclose()

    async def recover_work_email(self, call_id: str) -> TregEmailResult | None:
        archived = await self._archived_call(call_id)
        if archived is None:
            return None
        body, charged = archived
        try:
            result = parse_email_response(body, charged_microusd=charged, call_id=call_id)
            return None if result.pending else result
        except TregPeopleError:
            return None

    async def recover_people_search(self, call_id: str, domain: str) -> TregSearchResult | None:
        archived = await self._archived_call(call_id)
        if archived is None:
            return None
        body, charged = archived
        if not isinstance(body, Mapping) or "leads" not in body:
            return None
        try:
            return parse_people_search_response(
                body, requested_domain=domain, charged_microusd=charged, call_id=call_id
            )
        except TregPeopleError:
            return None


def apply_email_result(person: HydratedPerson, result: TregEmailResult) -> HydratedPerson:
    return replace(
        person,
        work_email=result.email if result.verified else "",
        email_status=("found" if result.email and result.verified else
                      "unverified" if result.candidate_present else "not_found"),
        email_provider=result.provider,
        email_call_id=result.call_id,
    )


def estimate_people_hydration(
    *,
    company_count: int,
    people_limit: int = DEFAULT_PEOPLE_LIMIT,
    email_limit: int = 0,
    email_max_cost_usd: Decimal = DEFAULT_EMAIL_MAX_COST_USD,
) -> dict[str, Any]:
    companies = max(0, min(int(company_count), MAX_COHORT_SIZE))
    people = max(1, min(int(people_limit), MAX_PEOPLE_LIMIT))
    emails = max(0, min(int(email_limit), MAX_EMAIL_LIMIT))
    email_maximum = Decimal(str(email_max_cost_usd))
    if not email_maximum.is_finite() or email_maximum < 0 or email_maximum > MAX_EMAIL_MAX_COST_USD:
        raise ValueError("Invalid email route maximum")
    discovery = DISCOVERY_UNIT_COST_USD * companies * people
    email = email_maximum * companies * emails
    total = discovery + email
    money = Decimal("0.000001")
    return {
        "company_count": companies,
        "people_limit": people,
        "email_limit": emails,
        "search_request_count": companies * 2,
        "email_request_count": companies * emails,
        "discovery_max_usd": float(discovery.quantize(money, rounding=ROUND_UP)),
        "email_max_usd": float(email.quantize(money, rounding=ROUND_UP)),
        "maximum_usd": float(total.quantize(money, rounding=ROUND_UP)),
    }


def project_people_result(
    *,
    company_domain: str,
    people: Sequence[HydratedPerson],
    observed_at: datetime | None = None,
    search_call_ids: Sequence[str] = (),
    response_hashes: Sequence[str] = (),
    attempted_searches: int = 2,
    attempted_emails: int = 0,
) -> TregPeopleProjection:
    now = observed_at or datetime.now(UTC)
    company = _normalized_domain(company_domain)
    if not company:
        raise ValueError("A valid company domain is required")
    normalized_people = [
        {
            "name": person.full_name,
            "first_name": person.first_name,
            "last_name": person.last_name,
            "title": person.title,
            "linkedin_url": person.linkedin_url,
            "company_name": person.company_name,
            "location": person.location,
            "lane": person.lane,
            "function": person.function,
            "seniority": person.seniority,
            "role_score": person.role_score,
            "role_reason": person.role_reason,
            "provider": person.provider,
            "company_domain_match": person.company_domain_match,
            "work_email": person.work_email,
            "email_status": person.email_status,
            "email_provider": person.email_provider,
            "email_call_id": person.email_call_id,
            "email_verification_policy": "mailbox-checked-v1",
        }
        for person in people
    ]
    content = {
        "company_domain": company,
        "people": normalized_people,
        "provider": "Treg",
        "source_kind": "licensed_professional_provider",
        "refresh_days": REFRESH_DAYS,
    }
    content_json = json.dumps(content, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(content_json.encode()).hexdigest()
    suffix = content_hash[:24]
    coverage_key = f"coverage:{SCANNER_ID}:{company}:{suffix}"
    artifact_key = f"artifact:{SCANNER_ID}:{company}:{suffix}"
    attempted_units = max(0, int(attempted_searches)) + max(0, int(attempted_emails))
    manifest = ScannerCoverageManifest(
        manifest_key=coverage_key,
        scanner_id=SCANNER_ID,
        scanner_version=SCANNER_VERSION,
        surface="treg_icypeas_ranked_role_search_and_bounded_email_resolution",
        scope=company,
        attempted_units=attempted_units,
        successful_units=attempted_units,
        total_units=attempted_units,
        completeness="complete",
        truncated=False,
        absence_interpretable=True,
        limitations=[
            "Licensed provider coverage can be incomplete or stale",
            "Only a bounded set of senior Beings-relevant role titles was queried",
            "Provider email verification does not establish consent or permission to contact",
            "Results do not establish buyer intent, budget, urgency, or company fit",
        ],
        source_urls=[PEOPLE_SEARCH_SOURCE_URL],
        observed_at=now,
        metadata={
            "provider": "Treg",
            "search_provider": "IcyPeas",
            "people_selected": len(normalized_people),
            "email_attempts": int(attempted_emails),
        },
    )
    artifact = EvidenceArtifact(
        artifact_key=artifact_key,
        company_key=company,
        scanner_id=SCANNER_ID,
        scanner_version=SCANNER_VERSION,
        extractor_version=EXTRACTOR_VERSION,
        artifact_type="licensed_professional_contact_summary",
        canonical_url=PEOPLE_SEARCH_SOURCE_URL,
        original_url=PEOPLE_SEARCH_SOURCE_URL,
        publisher="Treg",
        fetched_at=now,
        status_code=200,
        mime_type="application/json",
        content_hash=content_hash,
        byte_count=len(content_json.encode()),
        robots_state="not_applicable",
        excerpt=(
            f"Treg returned {len(normalized_people)} selected senior professional contact(s) "
            f"for {company} from a bounded role search."
        ),
        locator=f"company:{company} scanner:{SCANNER_ID}",
        retention_policy="normalized_professional_contact_fields_hash_and_bounded_excerpt",
        license_policy="treg_and_child_provider_terms_apply",
        metadata={
            "provider": "Treg",
            "search_provider": "IcyPeas",
            "people_selected": len(normalized_people),
            "full_response_retained": False,
            "call_ids": [item[:200] for item in search_call_ids if item][:10],
            "response_hashes": [item[:128] for item in response_hashes if item][:10],
        },
    )
    observations: list[AtomicObservation] = []
    for index, value in enumerate(normalized_people):
        identity = value["linkedin_url"].lower().rstrip("/") or hashlib.sha256(
            f"{company}|{value['name']}|{value['title']}".lower().encode()
        ).hexdigest()[:24]
        observation_hash = hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        observations.append(
            AtomicObservation(
                observation_key=f"observation:{SCANNER_ID}:{company}:{index}:{observation_hash[:20]}",
                company_key=company,
                scanner_id=SCANNER_ID,
                scanner_version=SCANNER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                rule_id="contact.provider_professional_person",
                rule_version=RULE_VERSION,
                entity_key=f"professional-person:{identity}",
                predicate="contact.provider_professional_person",
                value_type="json",
                value=value,
                qualifiers={
                    "source_kind": "licensed_professional_provider",
                    "provider": "Treg",
                    "search_provider": "IcyPeas",
                    "professional_data": True,
                    "first_party": False,
                    "public_company_site_evidence": False,
                    "permission_to_contact": False,
                    "email_provider_verified": value["email_status"] == "found",
                    "coverage_complete": True,
                },
                summary=f"{value['name']} — {value['title']} ({value['lane'].replace('_', ' ')}).",
                source_url=PEOPLE_SEARCH_SOURCE_URL,
                epistemic_status="observed",
                validity_status="current",
                confidence="high" if value["company_domain_match"] is True else "medium",
                confidence_method="licensed_provider_response_with_local_title_validation",
                first_observed_at=now,
                last_observed_at=now,
                effective_at=now,
                effective_time_basis="scan_time",
                expires_at=now + timedelta(days=REFRESH_DAYS),
                artifact_keys=[artifact_key],
                coverage_keys=[coverage_key],
                score_eligible=False,
                discovery_only=True,
                source_actor_key="provider:treg",
                origin_fingerprint=f"treg:icypeas:{company}:{identity}",
                causal_root_key=f"provider-professional-person:{company}:{identity}",
                metadata={
                    "prohibited_claims": [
                        "permission or consent",
                        "current buyer intent",
                        "available budget",
                        "company fit",
                        "employment guaranteed current",
                    ]
                },
            )
        )
    return TregPeopleProjection(
        artifacts=(artifact,),
        coverage_manifests=(manifest,),
        observations=tuple(observations),
    )


__all__ = [
    "BASE_URL",
    "CHAMPION_TITLES",
    "DEFAULT_EMAIL_LIMIT",
    "DEFAULT_EMAIL_MAX_COST_USD",
    "DEFAULT_PEOPLE_LIMIT",
    "DISCOVERY_UNIT_COST_USD",
    "ECONOMIC_TITLES",
    "EMAIL_FIND_ENDPOINT",
    "EXTRACTOR_VERSION",
    "HydratedPerson",
    "MAX_COHORT_SIZE",
    "MAX_EMAIL_LIMIT",
    "MAX_PEOPLE_LIMIT",
    "PEOPLE_SEARCH_ENDPOINT",
    "REFRESH_DAYS",
    "RoleClassification",
    "SCANNER_ID",
    "SCANNER_VERSION",
    "TregCredentials",
    "TregEmailResult",
    "TregPeopleClient",
    "TregPeopleError",
    "TregPeopleProjection",
    "TregSearchResult",
    "apply_email_result",
    "classify_role",
    "email_candidates",
    "estimate_people_hydration",
    "parse_email_response",
    "parse_people_search_response",
    "project_people_result",
    "resolve_treg_credentials",
    "select_people",
]
