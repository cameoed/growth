"""Deterministic, source-addressed company roles and claim attribution.

A website publishing a customer's service, a judge's biography, or a product capability
is not evidence that the publisher sells that service. These deliberately conservative
rules retain ambiguous statements for review and never turn keyword lists into claims.
No retrieval, provider calls, or domain-name classification lists are used here.
"""

from __future__ import annotations

import re
from datetime import datetime
from functools import lru_cache, wraps
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from .config import PLATFORM_PROFILES

if TYPE_CHECKING:
    from .models import DiscoveredHost, SiteObservation

CompanyRole = Literal[
    "consumer_brand",
    "consumer_app_game",
    "production_agency",
    "software_vendor",
    "publisher",
    "directory_or_awards",
    "unknown",
]
ClaimSubject = Literal[
    "company_self",
    "customer",
    "partner",
    "person_quoted",
    "article_subject",
    "unknown",
]
ENTITY_ASSESSMENT_VERSION = "company-role-attribution-v1"


class ClaimAttribution(BaseModel):
    predicate: str = "company.positioning"
    role: CompanyRole = "unknown"
    speaker: str = ""
    subject_company: str | None = None
    attribution: ClaimSubject = "unknown"
    epistemic_status: Literal["observed", "inferred", "unknown"] = "unknown"
    excerpt: str = Field(default="", max_length=1_200)
    source_url: str = ""
    observed_at: datetime | None = None
    confidence: Literal["low", "medium", "high"] = "low"


class CompanyRoleAssessment(BaseModel):
    company_key: str = ""
    role: CompanyRole = "unknown"
    confidence: Literal["low", "medium", "high"] = "low"
    epistemic_status: Literal["observed", "inferred", "unknown"] = "unknown"
    claims: list[ClaimAttribution] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    version: str = ENTITY_ASSESSMENT_VERSION


_SOFTWARE = re.compile(
    r"\b(?:software (?:company|vendor|platform|provider)|our software|enterprise software|"
    r"saas platform|analytics platform|measurement platform|advertising platform|"
    r"marketing platform|marketing reporting platform|creative platform|ai platform|"
    r"ai[- ]powered (?:advertising |creative |marketing )?(?:platform|tool)|"
    r"reporting (?:platform|tool)|customer data platform|attribution platform|"
    r"ad[- ](?:creation|generation) platform|self[- ]service platform)\b",
    re.I,
)
_PUBLISHER = re.compile(
    r"\b(?:research (?:company|firm|provider|publisher)|market research company|"
    r"(?:market|business|marketing) intelligence (?:company|provider|platform)|"
    r"(?:news|digital|media|research) publisher|news publication|"
    r"(?:insights|research),? (?:data,? )?and forecasts|publishes? (?:news|research|reports))\b",
    re.I,
)
_DIRECTORY = re.compile(
    r"\b(?:awards? (?:program(?:me)?|organization|competition)|"
    r"marketing (?:effectiveness )?awards?|"
    r"effectiveness awards?|directory of (?:agencies|companies|studios)|"
    r"(?:agency|business|company|creative) directory|recogniz(?:e|es|ing) (?:the )?(?:most )?"
    r"effective marketing|celebrat(?:e|es|ing) (?:marketing effectiveness|effective marketing))\b",
    re.I,
)
_AGENCY = re.compile(
    r"\b(?:(?:creative|marketing|advertising|digital|production|performance|design|seo|growth) "
    r"(?:(?:and )?(?:production|creative|growth|marketing) )?agency|"
    r"(?:production|seo|marketing services) company|"
    r"(?:growth|marketing) consultancy|creative studio|"
    r"(?:video|film|photography|animation|campaign|content|production) studio)\b",
    re.I,
)
_CONSUMER = re.compile(
    r"\b(?:consumer brand|(?:fashion|apparel|skincare|beauty|footwear|cosmetics|clothing|"
    r"lifestyle|food|beverage|jewelry) brand|online (?:store|shop)|shop (?:our|the) "
    r"(?:collection|products)|add to (?:cart|bag))\b",
    re.I,
)
_APP = re.compile(
    r"\b(?:mobile game(?:s)? (?:studio|developer|publisher)|game (?:developer|publisher)|"
    r"gaming studio|(?:fitness|wellness|dating|meditation|consumer|language learning) app|"
    r"play our games|download our app)\b",
    re.I,
)
_CREATIVE = re.compile(
    r"\b(?:photography|retouching|video|motion|animation|creative (?:production|testing|strategy)|"
    r"ad creative|paid social|campaigns?|production|photo shoots?|"
    r"weekly content|monthly content)\b",
    re.I,
)
_SELF_ACTION = re.compile(
    r"\b(?:we (?:are|produce|create|deliver|offer|provide|make|build|shoot|edit|design|need|"
    r"seek|hire|run|launch|publish|work|speciali[sz]e)|our (?:in[- ]house|studio|team|"
    r"services|work|production|creative|campaigns|monthly|retainer|clients|products|games))\b",
    re.I,
)
_CUSTOMER = re.compile(
    r"\b(?:(?:our|a|the) (?:customers?|clients?) (?:is|are|include|delivers?|produces?|"
    r"creates?|runs?|offers?|speciali[sz]es?|provides?)|customer story|client spotlight)\b",
    re.I,
)
_PERSON = re.compile(
    r"\b(?:meet (?:the |our )?(?:judges?|jury)|(?:judge|juror|jury member|speaker)\b|"
    r"(?:founder|ceo|director|president|chief \w+ officer) (?:of|at)|"
    r"(?:she|he) (?:is|was|leads|runs|founded))",
    re.I,
)
_PARTNER = re.compile(
    r"\b(?:our partner\b|partner spotlight|powered by|in partnership with)\b", re.I
)
_EDITORIAL = re.compile(
    r"\b(?:in this (?:article|report|guide)|learn how|read about|according to|"
    r"top \d+ (?:creative )?agencies|best (?:creative )?agencies)\b",
    re.I,
)
_SELF_NEGATION = re.compile(
    r"\b(?:not|never|no longer) (?:an? )?(?:\w+ ){0,3}(?:agency|studio|production company)\b", re.I
)
_THIRD_PARTY_PATHS = (
    (re.compile(r"/(?:judges?|jury|speakers?|bios?|people)/?", re.I), "person_quoted"),
    (
        re.compile(r"/(?:customers?|customer-stories|case-stud(?:y|ies)|success-stories)/?", re.I),
        "customer",
    ),
    (re.compile(r"/(?:partners?|partner-stories)/?", re.I), "partner"),
    (
        re.compile(r"/(?:blog|articles?|news|insights|resources|guides|directory|winners)/?", re.I),
        "article_subject",
    ),
)


def _memoized_strings(function):
    """Bound repeated pure classification within scoring without retaining large inputs."""
    cached = lru_cache(maxsize=4096)(function)

    @wraps(function)
    def bounded(*args, **kwargs):
        if any(isinstance(value, str) and len(value) > 2048
               for value in (*args, *kwargs.values())):
            return function(*args, **kwargs)
        return cached(*args, **kwargs)

    bounded.cache_info = cached.cache_info
    bounded.cache_clear = cached.cache_clear
    return bounded


@_memoized_strings
def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".").removeprefix("www.")
    except ValueError:
        return ""


def _company_key(observation: SiteObservation) -> str:
    return (
        (observation.canonical_host or _host(observation.final_url or "") or observation.hostname)
        .lower()
        .rstrip(".")
        .removeprefix("www.")
    )


def _same_company(url: str, key: str) -> bool:
    host = _host(url)
    return bool(host and (host == key or host.endswith("." + key)))


def _units(observation: SiteObservation):
    """Keep the original URL/field and sentence; never join unrelated claim subjects."""
    root = observation.final_url or f"https://{_company_key(observation)}/"
    sources = [(root, observation.title, observation.description, observation.text_excerpt)]
    sources.extend((p.url, p.title, p.description, p.text_excerpt) for p in observation.pages)
    seen: set[tuple[str, str]] = set()
    for url, title, description, body in sources:
        for field_name, value in (("title", title), ("description", description), ("body", body)):
            # Bound work on oversized saved pages without sampling fabricated evidence.
            for raw in re.split(r"(?<=[.!?])\s+|[\r\n]+", value[:48_000]):
                excerpt = " ".join(raw.split())[:1_200]
                if not excerpt or (url, excerpt) in seen:
                    continue
                seen.add((url, excerpt))
                yield url, field_name, excerpt


@_memoized_strings
def _attribution(url: str, text: str, company_key: str) -> ClaimSubject:
    if not _same_company(url, company_key):
        return "unknown"
    path = urlparse(url).path
    for pattern, attribution in _THIRD_PARTY_PATHS:
        if pattern.search(path):
            return attribution
    if _CUSTOMER.search(text):
        return "customer"
    if _PERSON.search(text):
        return "person_quoted"
    if _PARTNER.search(text):
        return "partner"
    if _EDITORIAL.search(text):
        return "article_subject"
    return "company_self"


@_memoized_strings
def _role(text: str, *, field: str) -> CompanyRole:
    """Identity nouns must describe the company, not its audience or product user."""
    if _DIRECTORY.search(text):
        return "directory_or_awards"
    if _PUBLISHER.search(text):
        return "publisher"
    if _APP.search(text):
        return "consumer_app_game"
    if _SOFTWARE.search(text):
        return "software_vendor"
    agency = _AGENCY.search(text)
    if agency and not _SELF_NEGATION.search(text):
        prefix = text[: agency.start()].lower()
        # "For creative agencies", "trusted by agencies" and "helps agencies" describe
        # an audience even when the phrase occurs in a homepage title.
        audience = re.search(
            r"\b(?:for|by|help(?:s|ing)?|enabl(?:e|es|ing)|empower(?:s|ing)?|"
            r"connect(?:s|ing)? with|work(?:s|ing)? with|serv(?:e|es|ing))\s+(?:\w+\s+){0,3}$",
            prefix,
        )
        if not audience and (
            field in {"title", "description"}
            or _SELF_ACTION.search(text)
            or re.search(
                r"\b(?:agency|studio|production company|consultancy)\b.{0,80}"
                r"\b(?:clients|services|brands)\b",
                text,
                re.I,
            )
        ):
            return "production_agency"
    if _CONSUMER.search(text):
        return "consumer_brand"
    return "unknown"


@_memoized_strings
def _is_delivery(text: str, role: CompanyRole, field: str) -> bool:
    if not _CREATIVE.search(text):
        return False
    # A platform generating a customer's video is a product capability. A software
    # company's own explicitly described in-house production can still be a buyer lane.
    own_team = bool(
        re.search(
            r"\bour (?:in[- ]house|marketing|creative|production|content) (?:team|studio)\b",
            text,
            re.I,
        )
    )
    if _SOFTWARE.search(text) and not own_team:
        return False
    if role == "production_agency":
        return True
    if re.search(r"\b(?:with|for|help) (?:\w+ ){0,3}(?:agencies|studios|agency)\b", text, re.I):
        return False
    return bool(
        _SELF_ACTION.search(text) or own_team or (field == "title" and role == "consumer_brand")
    )


def assess_company_role(
    observation: SiteObservation,
    *,
    candidate: DiscoveredHost | None = None,
) -> CompanyRoleAssessment:
    """Assess the publisher's operating role from saved, attributed self descriptions.

    The final role remains an inference; each literal claim is observed. A known
    source contributes only a low-confidence prior when no company-self identity
    resolves the role, and never overrides explicit software/publisher/award evidence.
    """
    key = _company_key(observation)
    claims: list[ClaimAttribution] = []
    weighted: dict[str, int] = {}
    for url, field, excerpt in _units(observation):
        attribution = _attribution(url, excerpt, key)
        role = _role(excerpt, field=field)
        delivery = _is_delivery(excerpt, role, field)
        if role == "unknown" and not (_CREATIVE.search(excerpt) or _AGENCY.search(excerpt)):
            continue
        own = attribution == "company_self"
        confidence = (
            "high" if own and (field != "body" or _SELF_ACTION.search(excerpt)) else "medium"
        )
        claims.append(
            ClaimAttribution(
                predicate="creative.delivery" if delivery else "company.positioning",
                role=role,
                speaker=key if _same_company(url, key) else _host(url),
                subject_company=key if own else None,
                attribution=attribution,
                epistemic_status="observed",
                excerpt=excerpt,
                source_url=url,
                observed_at=observation.observed_at,
                confidence=confidence,
            )
        )
        if own and role != "unknown":
            # Repetition of navigation, page templates or the same keyword never adds
            # confidence. Strong, primary identity evidence wins over incidental mentions.
            weight = 5 if field == "title" else 4 if field == "description" else 3
            if _SELF_ACTION.search(excerpt):
                weight = max(weight, 4)
            weighted[role] = max(weighted.get(role, 0), weight)
    protected = ("directory_or_awards", "publisher", "software_vendor", "consumer_app_game")
    role: CompanyRole = "unknown"
    reasons: list[str] = []
    if weighted:
        ranked = sorted(weighted, key=lambda r: (weighted[r], r in protected), reverse=True)
        role = ranked[0]  # type: ignore[assignment]
        # Explicit provider/editorial identity outranks an agency mention of similar
        # strength. Equal contradictory self-identities otherwise remain unresolved.
        if (
            len(ranked) > 1
            and weighted[ranked[1]] == weighted[role]
            and (role in protected) == (ranked[1] in protected)
        ):
            role = "unknown"
            reasons.append("Conflicting company-self identities require review.")
        else:
            reasons.append("Role inferred from the company's own source-addressed positioning.")
    confidence: Literal["low", "medium", "high"] = (
        "high"
        if role != "unknown" and weighted.get(role, 0) >= 4
        else "medium"
        if role != "unknown"
        else "low"
    )
    if role == "unknown" and not weighted and candidate:
        profile = PLATFORM_PROFILES.get(candidate.source_apex)
        source_host = candidate.source_apex.lower().removeprefix("www.")
        # The provider's own host is not one of its tenants. Canonical redirects to an
        # independent company can retain the discovery prior until direct evidence exists.
        if profile and key != source_host:
            priors = {
                "agency": "production_agency",
                "commerce": "consumer_brand",
                "enterprise_commerce": "consumer_brand",
                "media_membership": "publisher",
            }
            role = priors.get(profile.kind, "unknown")  # type: ignore[assignment]
            if role != "unknown":
                reasons.append(
                    "Low-confidence discovery-source prior; "
                    "the company's own operating role remains unverified."
                )
    excluded = sum(c.attribution != "company_self" for c in claims)
    if excluded:
        reasons.append(
            f"Excluded {excluded} contextual customer, partner, person, article or unresolved "
            "claim(s) from company-role evidence."
        )
    unresolved = []
    if role == "unknown" or confidence == "low":
        unresolved.append(
            "Verify the company's own operating model from an about or services page."
        )
    if role in {"software_vendor", "publisher", "directory_or_awards"}:
        unresolved.append(
            "Verify this company's own recurring production queue; "
            "customer and editorial examples do not establish it."
        )
    return CompanyRoleAssessment(
        company_key=key,
        role=role,
        confidence=confidence,
        epistemic_status="inferred" if role != "unknown" else "unknown",
        claims=sorted(
            claims,
            key=lambda claim: (
                claim.attribution == "company_self" and claim.role == role,
                claim.attribution == "company_self" and claim.predicate == "creative.delivery",
                claim.attribution == "company_self" and claim.role != "unknown",
            ),
            reverse=True,
        )[:80],
        reasons=reasons,
        unresolved_questions=unresolved,
    )


def company_self_corpus(observation: SiteObservation, *, for_delivery: bool = False) -> str:
    """Return only owned, non-contextual text; delivery mode excludes tool capabilities.

    This is a derived projection. Raw SiteObservation text/signals are never rewritten.
    Keywords, job titles and URLs are not appended as if they were self statements.
    """
    key = _company_key(observation)
    excerpts: list[str] = []
    for url, field, excerpt in _units(observation):
        if _attribution(url, excerpt, key) != "company_self":
            continue
        role = _role(excerpt, field=field)
        if for_delivery and not _is_delivery(excerpt, role, field):
            # Recurrence/partner capacity can follow an independently established self
            # delivery statement without repeating "we produce" in every sentence.
            if not re.search(
                r"\b(?:our (?:monthly|retainer|clients|work)|we (?:use|work|need|seek)|"
                r"overflow production|white[- ]label|retained production)\b",
                excerpt,
                re.I,
            ):
                continue
            if _SOFTWARE.search(excerpt):
                continue
        excerpts.append(excerpt)
    return " ".join(excerpts).lower()


_PROTECTED_ROLES = {"software_vendor", "publisher", "directory_or_awards"}
_ROLE_SENSITIVE_SIGNALS = {
    "agency",
    "creative_demand",
    "paid_distribution",
    "recurring",
    "business_model",
    "creative_use_cases",
    "creative_formats",
    "localization",
    "ai_workflow",
    "production_pressure",
    "production_capacity",
    "placement_demand",
}


def company_self_signals(
    observation: SiteObservation,
    *,
    company_role: CompanyRoleAssessment | None = None,
) -> dict[str, list[str]]:
    """Copy signal lists, removing unattributed workload claims for contextual publishers.

    Literal tagged signals retain their source URL and excerpt. They are eligible only
    when their phrase occurs in an attributed delivery statement at that same URL.
    Missing raw support is unknown, not proof of absent demand. Hiring and structured
    advertiser observations are independent evidence and do not pass through this filter.
    """
    assessment = company_role or assess_company_role(observation)
    projected = {category: list(values) for category, values in observation.signals.items()}
    if assessment.role not in _PROTECTED_ROLES:
        return projected
    key = assessment.company_key
    delivery_by_source: dict[str, str] = {}
    for url, field, excerpt in _units(observation):
        if _attribution(url, excerpt, key) != "company_self":
            continue
        if _is_delivery(excerpt, _role(excerpt, field=field), field):
            delivery_by_source[url] = delivery_by_source.get(url, "") + " " + excerpt.lower()
    all_delivery = " ".join(delivery_by_source.values())
    for category in _ROLE_SENSITIVE_SIGNALS:
        if category not in projected:
            continue
        retained = []
        for value in projected[category]:
            phrase = value.strip().lower()
            source = re.search(r"(?:^|\|\s*)source=(https?://\S+)\s*$", value, re.I)
            if source:
                parts = value.split("|")
                phrase = parts[0].partition(":")[2].strip().lower()
                excerpt = "|".join(parts[1:-1]).strip()
                url = source.group(1).rstrip(".,;)")
                if not phrase or _attribution(url, excerpt, key) != "company_self":
                    continue
                corpus = delivery_by_source.get(url, "")
            else:
                corpus = all_delivery
            if phrase and re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", corpus):
                retained.append(value)
        projected[category] = retained
    return projected


def project_company_self_observation(
    observation: SiteObservation,
    *,
    company_role: CompanyRoleAssessment | None = None,
) -> SiteObservation:
    """Return a private score projection; preserve every field in the raw observation.

    Provider/customer examples and publisher/award content cannot act as self production
    work. Aggregate assets remain contextual observations, while client case-study counts
    cannot create agency workload. This projection is never persisted as fetched evidence.
    """
    assessment = company_role or assess_company_role(observation)
    if assessment.role not in _PROTECTED_ROLES:
        return observation
    projection = observation.model_copy(deep=True)
    projection.signals = company_self_signals(observation, company_role=assessment)
    projected_text: dict[str, list[str]] = {}
    for url, field, excerpt in _units(observation):
        if _attribution(url, excerpt, assessment.company_key) != "company_self":
            continue
        role = _role(excerpt, field=field)
        if _CREATIVE.search(excerpt) and not _is_delivery(excerpt, role, field):
            continue
        projected_text.setdefault(url, []).append(excerpt)
    root = observation.final_url or f"https://{assessment.company_key}/"
    projection.text_excerpt = " ".join(projected_text.get(root, []))
    # A product-capability title is not self production evidence. The separate role
    # assessment retains original identity metadata and exact source excerpts.
    for field_name in ("title", "description"):
        value = getattr(projection, field_name)
        if _CREATIVE.search(value) and not _is_delivery(
            value, _role(value, field=field_name), field_name
        ):
            setattr(projection, field_name, "")
    # Strip contextual page metadata too:
    # a customer's title such as "Monthly video production" has the same subject problem.
    for page in projection.pages:
        page.text_excerpt = " ".join(projected_text.get(page.url, []))
        page.title = ""
        page.description = ""
    for counts in (projection.web.sitemap_counts, projection.web.sitemap_recent_counts):
        if "case_studies" in counts:
            counts["case_studies"] = 0
    return projection
