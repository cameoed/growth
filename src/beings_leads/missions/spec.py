"""Free clause-aware mission compilation; provider-free, conservative explicit constraints."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION = "mission-local-v13"
STOP = set(
    [
        "find",
        "finding",
        "companies",
        "company",
        "businesses",
        "business",
        "looking",
        "look",
        "for",
        "of",
        "the",
        "a",
        "an",
        "in",
        "and",
        "or",
        "that",
        "who",
        "with",
        "to",
        "are",
        "is",
        "our",
        "we",
        "want",
        "need",
        "potential",
        "prospects",
        "prospect",
        "leads",
        "lead",
        "me",
        "us",
        "please",
        "based",
        "located",
        "near",
        "serving",
        "sell",
        "sells",
        "selling",
        "seller",
        "sellers",
        "buy",
        "buys",
        "buying",
        "buyer",
        "buyers",
        "purchase",
        "purchasing",
        "dealer",
        "dealers",
        "reseller",
        "resellers",
        "trade",
        "trading",
        "deal",
        "dealing",
        "various",
        "formats",
        "format",
        "such",
        "as",
        "via",
        "through",
        "including",
        "either",
        "directly",
        "specializing",
        "specialise",
        "specialize",
        "specialise",
        "specializes",
        "specialising",
        "auction",
        "auctions",
        "direct",
        "listings",
        "listing",
        "using",
        "offer",
        "offers",
        "provide",
        "provides",
        "firms",
        "firm",
        "customers",
        "businesses",
        "working",
        "operate",
        "operating",
    ]
)
GROUPS = {
    "machinery": [
        "heavy machinery",
        "heavy equipment",
        "construction equipment",
        "construction machinery",
        "earthmoving equipment",
        "industrial equipment",
        "industrial machinery",
        "excavator",
        "bulldozer",
        "crane",
        "forklift",
        "backhoe",
        "wheel loader",
        "mining equipment",
    ],
    "events": [
        "corporate event",
        "corporate events",
        "conference",
        "conferences",
        "convention",
        "corporate meeting",
        "corporate meetings",
        "business event",
    ],
    "creative": ["creative production", "video production", "product photography", "paid social"],
}
PLACES = {
    "nyc": ["nyc", "new york city", "manhattan", "brooklyn", "queens", "bronx", "staten island"],
    "london": ["london"],
    "boston": ["boston"],
    "chicago": ["chicago"],
    "los angeles": ["los angeles"],
    "san francisco": ["san francisco"],
    "texas": ["texas"],
    "florida": ["florida"],
    "california": ["california"],
    "united states": ["united states", "usa", "u.s.a.", "us"],
    "canada": ["canada"],
    "united kingdom": ["united kingdom", "uk", "u.k."],
}
FORMAT_GROUPS = {
    "auction": ["auction", "auctions", "auctioneer", "auctioneers", "bidding", "bid online"],
    "direct_listing": [
        "direct listing",
        "direct listings",
        "classified",
        "classifieds",
        "marketplace",
        "marketplaces",
        "for sale",
        "inventory",
        "buy now",
    ],
}


class MissionSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_role: Literal["auto", "matching_company", "buyer", "seller", "trader"] = "auto"
    ranking_focus: Literal[
        "auto", "fit", "recurring_activity", "timing", "scale", "access",
    ] = "auto"


class MissionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    brief: str = Field(min_length=5, max_length=2000)
    compile_with_ai: bool = True
    defer_ai: bool = False
    compiler_model: Literal["gpt-5.6-sol", "gpt-6-astra", "gpt-6.1-sol"] = "gpt-5.6-sol"
    setup: MissionSetup | None = None

    @model_validator(mode="after")
    def validate_setup(self):
        if self.defer_ai and not self.compile_with_ai:
            raise ValueError("Deferred setup requires AI compilation")
        if self.setup is not None and not self.compile_with_ai:
            raise ValueError("Guided mission setup requires AI compilation")
        return self


class MissionCompile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    compiler_model: Literal["gpt-5.6-sol", "gpt-6-astra", "gpt-6.1-sol"] = "gpt-5.6-sol"
    retry_cache_key: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def _contains(text, term):
    return bool(re.search(r"(?<!\w)" + re.escape(term) + r"(?:s|es)?(?!\w)", text, re.I))


def compile_spec(brief):
    original = brief.casefold()
    text = original
    geography = []
    # A place is constrained only inside a location clause. "In various formats" is
    # not a place, and a bare "us" pronoun does not imply the United States.
    place_pattern = r"\b(?:in|near|based in|located in|serving|across|within)\s+"
    for place, terms in PLACES.items():
        for term in sorted(terms, key=len, reverse=True):
            match = re.search(place_pattern + re.escape(term) + r"(?!\w)", text)
            if match:
                geography.append(
                    {"id": place, "terms": terms, "role": "geography", "required": True}
                )
                text = text[: match.start()] + " " + text[match.end() :]
                break
    # Preserve unlisted proper place names without classifying arbitrary prose as geography.
    named = re.search(place_pattern + r"([A-Z][\w-]*(?:\s+[A-Z][\w-]*)*)", brief)
    if named and not any(_contains(named.group(1), t) for g in geography for t in g["terms"]):
        value = named.group(1).casefold()
        geography.append({"id": value, "terms": [value], "role": "geography", "required": True})
        text = re.sub(place_pattern + re.escape(value), " ", text)
    formats = []
    for key, terms in FORMAT_GROUPS.items():
        if any(_contains(original, term) for term in terms):
            formats.append(
                {"id": key, "terms": terms, "role": "transaction_format", "required": False}
            )
    # "Such as" introduces examples. Those examples never become unrelated core
    # category words. Recognized formats stay as optional corroboration.
    text = re.split(r"\b(?:such as|including|in various formats|via|through)\b", text)[0]
    topics = []
    for key, terms in GROUPS.items():
        if any(_contains(text, term) for term in [key, *terms]):
            topics.append({"id": key, "terms": terms, "role": "fit", "required": True})
            for term in sorted([key, *terms], key=len, reverse=True):
                text = re.sub(r"(?<!\w)" + re.escape(term) + r"(?:s|es)?(?!\w)", " ", text)
    terms = sorted({w for w in re.findall(r"[\w-]+", text) if w not in STOP and len(w) > 2})
    if terms:
        topics.append(
            {"id": "requested_terms", "terms": terms, "role": "fit", "all": True, "required": True}
        )
    if not topics:
        raise ValueError("Describe the company category or activity to find")
    selling = bool(re.search(r"\b(?:sell\w*|seller\w*|dealer\w*|resell\w*)\b", original))
    buying = bool(re.search(r"\b(?:buy|buys|buyer\w*|buying|purchas\w*)\b", original))
    relation = (
        "trader"
        if buying and selling
        else "seller"
        if selling
        else ("buyer" if buying else "matching_company")
    )
    roles = (
        ["sales", "dealer", "owner", "president"]
        if relation in {"trader", "seller"}
        else ["events", "event", "experiential", "marketing", "owner"]
        if any(t["id"] == "events" for t in topics)
        else ["procurement", "purchasing", "owner"]
        if buying
        else ["owner", "director", "sales"]
    )
    return {
        "version": VERSION,
        "topics": topics,
        "geography": geography,
        "formats": formats,
        "relation": relation,
        "contact_terms": roles,
        "brief": brief,
        "ai_adjustment_limit": 20,
        "require_company_activity": True,
        "parts_requested": bool(re.search(r"\b(?:parts|components|accessories)\b", original)),
        "business_role": {
            "id": "business_role",
            "role": "business_role",
            "required": True,
            "terms": [],
            "relation": relation,
        },
    }
