"""Build bounded company-discovery queries from accepted local mission criteria."""

from __future__ import annotations

import re
from datetime import timedelta
from itertools import zip_longest

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Country locales are the fixed search scope. City hints do not establish company location.
POLICY = "mission-search-v3-explicit-term"
LOCATIONS = {
    "us": ("United States", "New York", "Los Angeles", "Chicago", "Atlanta", "Dallas",
           "Seattle", "Boston", "San Francisco", "Miami"),
    "gb": ("United Kingdom", "London", "Manchester", "Birmingham", "Edinburgh"),
    "ca": ("Canada", "Toronto", "Vancouver", "Calgary", "Montreal", "Ottawa"),
    "au": ("Australia", "Sydney", "Melbourne", "Brisbane", "Perth"),
}

# Adjacent groups are discovery hypotheses only. Qualification remains mission-specific.
ADJACENCIES = (
    (
        re.compile(r"media buy|media plan|airtime|television advertis|tv advertis|tv spot"),
        ("media buying agency", "media planning agency", "television advertising agency",
         "media investment agency", "sports media agency", "broadcast media buying",
         "programmatic TV agency", "connected TV advertising agency",
         "national television media buyer", "independent media agency",
         "advertising agency", "sports marketing agency", "media strategy consultancy",
         "performance media agency", "cross channel media planning",
         "direct response television agency", "media holding company"),
    ),
    (
        re.compile(r"commercial production|video production|film production|production agenc"),
        ("commercial production company", "commercial film production",
         "advertising production company", "branded content studio", "video production agency",
         "creative production agency", "commercial post production", "animation studio",
         "production services company", "integrated creative agency", "content studio",
         "advertising agency", "creative studio", "film production studio"),
    ),
    (
        re.compile(r"photograph|corporate event|event production"),
        ("corporate event production", "corporate event management", "convention management",
         "conference organizer", "corporate communications", "trade show organizer",
         "experiential marketing agency", "professional association conferences",
         "event venue group", "corporate hospitality", "brand experience agency",
         "conference production company", "convention center", "employee events",
         "corporate meetings", "enterprise events"),
    ),
    (
        re.compile(r"heavy machinery|heavy equipment|construction equipment|industrial machinery"),
        ("heavy equipment dealer", "construction equipment supplier", "equipment distributor",
         "equipment rental company", "industrial machinery dealer", "used equipment auction",
         "material handling equipment", "mining equipment supplier", "construction contractor",
         "civil engineering contractor", "industrial equipment group"),
    ),
)
BEINGS = (
    "consumer brand", "performance marketing agency", "subscription commerce", "gaming studio",
    "food and beverage brand", "consumer software company", "beauty brand", "retail group",
    "apparel brand", "ecommerce brand", "fitness brand", "consumer health brand", "creative agency",
    "brand studio", "multi brand operator", "digital commerce agency", "advertising agency",
    "home goods brand", "pet care brand", "consumer electronics brand",
    "direct to consumer brand", "subscription box brand", "meal kit delivery",
    "skin care company", "sports nutrition brand", "outdoor apparel brand",
    "footwear brand", "jewelry brand", "mattress brand", "furniture retailer",
    "baby products brand", "specialty grocery brand", "ready to drink beverages",
    "non alcoholic beverage company", "specialty coffee roaster", "organic snack brand",
    "personal care company", "hair care brand", "fragrance house", "beauty retailer",
    "online fashion retailer", "eyewear brand", "athleisure brand", "sports equipment brand",
    "home fitness company", "supplement brand", "wellness subscription", "healthy meal delivery",
    "pet food brand", "pet supplies retailer", "specialty ecommerce retailer",
    "omnichannel retailer", "consumer marketplace", "consumer fintech app", "mobile game publisher",
    "streaming service", "language learning app", "digital health app", "fitness app",
    "online learning platform", "travel booking platform", "consumer subscription app",
    "growth marketing agency", "social media agency", "paid media agency",
    "direct response agency", "content production company", "influencer marketing agency",
    "ecommerce marketing agency", "commercial production company", "creative production studio",
)
NEGATIVES = " -site:linkedin.com -site:indeed.com -site:glassdoor.com -site:clutch.co"
CAREERS_EXCLUDED_DOMAINS = (
    "workday.com", "myworkdayjobs.com", "greenhouse.io", "lever.co", "ashbyhq.com",
    "workable.com", "smartrecruiters.com", "icims.com", "taleo.net", "dayforcehcm.com",
    "jobvite.com", "ziprecruiter.com", "talent.com", "jooble.org", "wellfound.com", "builtin.com",
)
LOCAL_CAREERS_EXCLUDED_DOMAINS = {
    "us": ("ultipro.com", "monster.com", "careerbuilder.com", "simplyhired.com", "dice.com"),
    "gb": ("glassdoor.co.uk", "totaljobs.com", "reed.co.uk", "cv-library.co.uk",
           "accessacloud.com", "jobs.nhs.uk"),
    "ca": ("glassdoor.ca", "jobbank.gc.ca", "ultipro.ca", "workopolis.com"),
    "au": ("seek.com", "seek.com.au", "jora.com", "pageuppeople.com", "glassdoor.com.au",
           "careerone.com.au"),
}
GENERIC = {
    "business", "company", "firm", "organization", "buyer", "seller", "services", "agency",
    "companies", "businesses", "providers", "industries", "enterprise", "brand", "brands",
}
CAREERS_FRAME = "{} careers"
FRAMES = (
    "{}", "companies that offer {}", "{} for businesses", "{} products and services",
    "{} case studies", "{} official website", "enterprise {}",
    CAREERS_FRAME, "{} new product launches", "{} our locations", "buy {} from suppliers",
    "{} brands and companies",
)
SCALE_FRAMES = (0, 1, 6, 4, 2, 7, 3, 9, 5, 10, 8, 11)
FRESH_FRAME = "{} new product launches"
TERM_BLOCK = 8

EMPLOYEE_COUNT = re.compile(
    r"(?<![\w$])([0-9]{1,6}(?:[,.][0-9]{3})*)(?:\s*[-–]\s*([0-9,]+))?\+?"
    r"\s+(?:full[- ]time\s+)?(?:employees|team members|staff(?: members)?|salariés|"
    r"employés|Mitarbeiter|empleados|colaboradores|dipendenti|medewerkers)\b", re.I,
)
FOOTPRINT = re.compile(
    r"\b(?:investor relations|annual report|global offices|regional headquarters|"
    r"multiple offices|offices across|locations across|publicly traded|nationwide operations|"
    r"rapport annuel|Geschäftsbericht|jaarverslag)\b",
    re.I,
)
TEAM = re.compile(
    r"\b(?:enterprise clients|international clients|leadership team|our offices|"
    r"our locations|multiple locations|open positions|nos bureaux|unsere Standorte|"
    r"nuestras oficinas|nossos escritórios|le nostre sedi|onze vestigingen)\b", re.I,
)


def literal(value):
    """Accept short plain phrases only. The planner owns all query syntax."""
    if not isinstance(value, str) or len(value) > 100:
        return ""
    phrase = " ".join(value.lower().split())
    if not re.fullmatch(r"[a-z0-9]+(?:[ '-][a-z0-9]+)*", phrase):
        return ""
    phrase = phrase.replace("-", " ")
    if phrase in GENERIC or len(phrase.split()) > 7 or not re.search("[a-z]", phrase):
        return ""
    return phrase


class DiscoveryTermsV1(BaseModel):
    """Search-only hypotheses, kept with the cached mission interpretation."""

    model_config = ConfigDict(extra="forbid", strict=True)
    operator_categories: list[str] = Field(max_length=16)
    adjacent_segments: list[str] = Field(max_length=16)
    products_services: list[str] = Field(max_length=16)
    customer_use_cases: list[str] = Field(max_length=16)
    business_models: list[str] = Field(max_length=16)
    public_activity: list[str] = Field(max_length=16)

    @field_validator("*")
    @classmethod
    def clean_phrases(cls, values):
        return list(dict.fromkeys(phrase for value in values if (phrase := literal(value))))


def topic_terms(spec):
    return tuple(dict.fromkeys(
        phrase for topic in spec.get("topics", []) if isinstance(topic, dict)
        for term in topic.get("terms", []) if (phrase := literal(term))
    ))[:32]


def mission_terms(mission_id, spec):
    if mission_id == "beings":
        return BEINGS
    core = topic_terms(spec)
    source = " ".join(core)
    adjacent = tuple(
        term for pattern, terms in ADJACENCIES if pattern.search(source) for term in terms
    )
    bank = spec.get("discovery_terms_v1")
    groups = DiscoveryTermsV1.model_validate(bank).model_dump().values() if bank else ()
    # Short runs should still see several different search angles.
    return tuple(dict.fromkeys(
        term for row in zip_longest(core, *groups, adjacent) for term in row if term
    ))


def ordered_markets(spec):
    geography = " ".join(
        str(term).casefold()
        for group in spec.get("geography", []) if isinstance(group, dict)
        for term in group.get("terms", [])
    )
    aliases = {"us": ("usa", "u.s.", "nyc"), "gb": ("uk", "britain")}
    return tuple(sorted(LOCATIONS, key=lambda country: not any(
        re.search(r"(?<!\w)" + re.escape(place.casefold()) + r"(?!\w)", geography)
        for place in (*LOCATIONS[country], *aliases.get(country, ()))
    )))


def plan(mission_id, spec, anchor, market="all", *, prefer_large=True):
    if market != "all" and market not in LOCATIONS:
        raise ValueError("Unsupported mission market")
    terms = mission_terms(mission_id, spec)
    if not terms:
        return
    markets = ordered_markets(spec) if market == "all" else (market,)
    # Provider locale already gives a national search. Add metros as optional query text.
    place_groups = [[(country, city) for city in ("", *LOCATIONS[country][1:])]
                    for country in markets]
    scopes = [scope for row in zip_longest(*place_groups) for scope in row if scope]
    frames = tuple(FRAMES[i] for i in SCALE_FRAMES) if prefer_large else FRAMES
    week = anchor - timedelta(days=anchor.weekday())
    month = anchor.replace(day=1)
    after = (week - timedelta(days=30)).isoformat()

    # Each pass uses every term once, rotating frames per term and scope in short blocks.
    # Over successive passes each term reaches every scope/frame pair exactly once.
    for offset in range(len(scopes) * len(frames)):
        frame_round, scope_offset = divmod(offset, len(scopes))
        for index, term in enumerate(terms):
            country, city = scopes[(scope_offset + index // TERM_BLOCK) % len(scopes)]
            frame = frames[(frame_round + scope_offset + index) % len(frames)]
            query = frame.format(term)
            if city:
                query += f" {city}"
            fresh = frame == FRESH_FRAME
            if fresh:
                query += f" after:{after}"
            query += NEGATIVES
            if frame == CAREERS_FRAME:
                domains = CAREERS_EXCLUDED_DOMAINS + LOCAL_CAREERS_EXCLUDED_DOMAINS[country]
                query += "".join(f" -site:{domain}" for domain in domains)
            yield "mission", "search", country, week if fresh else month, query, term


def scale_hint(text):
    """Rank search clues, never publish them as employee counts or mission facts."""
    largest = max(
        (int(m[1].replace(",", "").replace(".", "")) for m in EMPLOYEE_COUNT.finditer(text)),
        default=0,
    )
    return (8 if largest >= 1000 else 5 if largest >= 50 else 0) + (
        3 if FOOTPRINT.search(text) else 0
    ) + (1 if TEAM.search(text) else 0)
