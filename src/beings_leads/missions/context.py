"""Company-activity evidence gates; a category mention is not a business identity."""

import re
from functools import lru_cache
from urllib.parse import urlsplit

SELLER = re.compile(
    r"\b(?:we (?:sell|supply|auction|distribute|manufacture|offer)|our (?:dealership|inventory|"
    r"auctions?|equipment)|(?:authorized |equipment |machinery )?dealers?|resellers?|"
    r"auctioneers?|auction house|auctions?|marketplace|classifieds?|for sale|"
    r"equipment sales|machinery sales|buy now|sell your|list your|selling)\b",
    re.I,
)
BUYER = re.compile(
    r"\b(?:we (?:buy|purchase|acquire)|buying your|we pay|wanted|seeking suppliers|"
    r"procurement|request for proposal|equipment buyers?|machinery buyers?)\b",
    re.I,
)
ACTIVITY = re.compile(
    r"\b(?:we (?:sell|supply|offer|provide|host|organize|organise|manufacture|specialize|"
    r"specialise)|our (?:services|products|events?|conferences?|meetings?|inventory)|"
    r"dealership|for sale|auctions?|marketplace|organizers?|organisers?|"
    r"manufacturers?|suppliers?|production|specialists?)\b",
    re.I,
)
INCIDENTAL_PATH = re.compile(
    r"/(?:blogs?|news|articles?|careers?|jobs?|case-stud(?:y|ies)|portfolio|testimonials?)(?:/|$)",
    re.I,
)
THIRD_PARTY = re.compile(
    r"\b(?:our (?:customers?|clients?)|(?:customer|client) (?:uses?|sells?|buys?|is|was)|"
    r"case study|portfolio project|job (?:opening|vacancy)|hiring|industry news)\b"
    r"|\b(?:customer|client)\s*:",
    re.I,
)
SERVICE_TO_CATEGORY = re.compile(
    r"\b(?:software|saas|crm|marketing|advertising|websites?|web design|templates?|"
    r"recruiting|recruitment|courses?)\b.{0,100}\b(?:for|helps?|serving|clients?|industry|"
    r"industries|businesses|companies)\b",
    re.I,
)
CLAUSE_BOUNDARY = re.compile(
    r"[.!?;\n]+|,?\s+\b(?:but|whereas|while)\s+"
    r"|,\s+(?=(?:and|or)\s+(?!we\b)|(?:they|our|their|other)\s+)",
    re.I,
)
OPERATION = (
    r"(?:assemble|build|create|design|develop|fabricate|grow|host|maintain|make|manage|"
    r"manufacture|operate|organise|organize|own|produce|publish|run|speciali[sz]e in)"
)
OPERATION_MODIFIERS = (
    r"(?:(?:a|an|the|our|own|custom|bespoke|dedicated|independent|specialized|"
    r"specialised)\s+){0,4}(?:(?:a chain|a network) of\s+)?"
)
OPERATING_ENTITY = re.compile(
    r"\b(?:agenc(?:y|ies)|cent(?:er|re)s?|clinics?|compan(?:y|ies)|facilit(?:y|ies)|gyms?|"
    r"hospitals?|labs?|laborator(?:y|ies)|publishers?|schools?|shops?|stores?|studios?)$",
    re.I,
)


@lru_cache(maxsize=4096)
def _term_pattern(term):
    # Searching the literal first lets the engine skip impossible starts. Check the
    # preceding boundary separately; advancing by one preserves overlapping matches.
    return re.compile(re.escape(term) + r"(?:s|es)?(?!\w)", re.I)


def matches(text, term):
    if text.isascii() and term.isascii() and term.lower() not in text.lower():
        return False
    pattern = _term_pattern(term)
    offset = 0
    while match := pattern.search(text, offset):
        start = match.start()
        if not start or not re.match(r"\w", text[start - 1]):
            return True
        offset = start + 1
        if offset > len(text):
            return False
    return False


@lru_cache(maxsize=4096)
def _identity_pattern(terms):
    target = "(?:" + "|".join(re.escape(t) + r"(?:s|es)?" for t in terms) + r")(?!\w)"
    modifiers = (
        r"(?:(?:a|an|the|global|international|independent|leading|specialist|specialized|"
        r"specialised|full-service|integrated|award-winning|boutique|established)\s+){0,6}"
    )
    return re.compile(
        r"\b(?:we(?:\s+are|['’]re)\s+" + modifiers + target
        + r"(?=$|[,;:]|\s+(?:and|that|which|with|for|in|based|serving|providing)\b)"
        + r"|as\s+" + modifiers + target + r"\s*,?\s+we\b)", re.I,
    )


@lru_cache(maxsize=4096)
def _operation_patterns(terms):
    def target_pattern(values):
        return "(?:" + "|".join(re.escape(t) + r"(?:s|es)?" for t in values) + r")(?!\w)"

    target = target_pattern(terms)
    # Building a clinic or a store does not establish that the builder operates it.
    claims = [r"(?:manage|operate|own|run)\s+" + OPERATION_MODIFIERS + target]
    other_terms = [term for term in terms if not OPERATING_ENTITY.search(term)]
    if other_terms:
        claims.append(OPERATION + r"\s+" + OPERATION_MODIFIERS + target_pattern(other_terms))
    # The category must be the object, including when the operation is described
    # by coordinated verbs. A software product for the target industry is separate.
    owned = re.compile(
        r"\bwe\s+(?:(?:also|currently|directly|independently|actively)\s+){0,2}"
        r"(?:" + OPERATION + r"\s+(?:and|&)\s+)?"
        + "(?:" + "|".join(claims) + ")"
        + r"(?=$|[,;:]|\s+(?:and|or|with|for|in|across|at|from|that|which|where|"
          r"since|every|each|on|serving|worldwide|nationwide)\b)",
        re.I,
    )
    non_owned = re.compile(
        r"\b(?:(?:not|never|no longer|don['’]t|doesn['’]t)\s+"
        r"(?:(?:currently|actually)\s+)?(?:"
        + OPERATION + r"\s+|(?:sell|supply|auction|distribute|offer|provide|buy|purchase|acquire|"
          r"rent|lease)\s+)?"
        r"|(?:look(?:ing)?|search(?:ing)?)\s+for\s+|(?:seek(?:ing)?|need)\s+"
        r"|(?:plan|hope|aim|intend)\s+to\s+" + OPERATION + r"\s+"
        r"|(?:outsourc\w*|subcontract\w*)\s+(?:(?:operation|production|management|"
        r"design|manufacturing)\s+of\s+)?)"
        + OPERATION_MODIFIERS + target
        + r"|" + target + r"\s+(?:is|are)\s+(?:outsourced|subcontracted|"
          r"(?:operated|managed|produced|manufactured)\s+by\s+(?:a\s+third party|"
          r"(?:our\s+)?partners?))\b",
        re.I,
    )
    return owned, non_owned


def role_object(phrase, *, selling=False):
    """Identify a complete verb/object phrase, excluding bare verbs and modifiers."""
    verbs = (
        "sell|supply|auction|trade|distribute|manufacture|offer|provide"
        if selling
        else (
            "buy|purchase|acquire|hire|book|commission|procure|engage|retain|contract|"
            "source|order|rent|lease"
        )
    )
    match = re.fullmatch(
        r"(?:want to |plan to |seek to )?(?:" + verbs + r")\s+(.+)",
        phrase,
        re.I,
    )
    if not match:
        return None
    subject = re.sub(
        r"^(?:(?:new|used|surplus|refurbished|second-hand|quality|your|our|all|a|an)(?:\s+|$))+",
        "",
        match[1],
        flags=re.I,
    ).strip()
    return subject if subject and subject not in {"for", "from", "in", "to"} else None


def _bound_role(window, criterion, relation):
    """Require the requested product to be the object of the trading activity."""
    target = "(?:" + "|".join(re.escape(t) + r"(?:s|es)?" for t in criterion["terms"]) + r")(?!\w)"
    modifiers = r"(?:(?:new|used|surplus|refurbished|second-hand|quality|your|our|all)\s+){0,4}"
    role = criterion.get("business_role", {})
    buy_verbs = "|".join(
        re.escape(v) for v in role.get("buy_verbs", ["buy", "purchase", "acquire"])
    )
    sell_verbs = "|".join(
        re.escape(v)
        for v in role.get(
            "sell_verbs", ["sell", "supply", "auction", "trade", "distribute", "manufacture"]
        )
    )
    buying = bool(
        re.search(
            r"\b(?:we (?:" + buy_verbs + r")|buying your|wanted:)\s+" + modifiers + target,
            window,
            re.I,
        )
    )
    # Compiler verbs may already include their object ("hire photographers").
    # The owned demand category is checked separately; do not append it again.
    if role:
        buying |= any(
            role_object(phrase)
            and (
                (relation == "buyer" and criterion.get("separate_purchase_subject"))
                or any(matches(phrase, term) for term in criterion["terms"])
            )
            and re.search(r"\bwe\s+" + re.escape(phrase) + r"(?!\w)", window, re.I)
            for phrase in role.get("buy_verbs", [])
        )
    selling = bool(
        re.search(
            r"\b(?:we (?:" + sell_verbs + r")|sell your|list your)\s+" + modifiers + target,
            window,
            re.I,
        )
    )
    if role:
        selling |= any(
            role_object(phrase, selling=True)
            and any(matches(phrase, term) for term in criterion["terms"])
            and re.search(r"\bwe\s+" + re.escape(phrase) + r"(?!\w)", window, re.I)
            for phrase in role.get("sell_verbs", [])
        )
    listing = bool(
        re.search(
            target + r"\s+(?:for sale|sales|auctions?|dealers?|dealership|marketplace|inventory)\b",
            window,
            re.I,
        )
        or re.search(
            r"\b(?:marketplace|auctions?|auction house|dealers?|dealership|inventory|listings)\s+"
            r"(?:(?:for|of|in)\s+)?" + modifiers + target,
            window,
            re.I,
        )
    )
    return (
        buying
        if relation == "buyer"
        else selling or listing
        if relation == "seller"
        else (buying or selling or listing)
    )


def _asset_trade(text, window, criterion, relation):
    """Resolve an explicit asset-list complement to the same source's owned sale process.

    Asset marketplaces commonly describe the sale process and its asset categories
    in separate sentences. Both must be first-person claims with the same object;
    a navbar's 'marketplace' or a client's assets cannot supply this relationship.
    """
    if (criterion["id"] != "machinery" and not criterion.get("business_role")) or relation not in {
        "seller",
        "trader",
    }:
        return False
    target = "(?:" + "|".join(re.escape(t) + r"(?:s|es)?" for t in criterion["terms"]) + ")"
    categories = any(
        not THIRD_PARTY.search(segment)
        and re.search(
            r"\bwe (?:handle|sell|auction|manage) (?:surplus )?assets including\s+"
            r"[^.!?;\n]*" + target,
            segment,
            re.I,
        )
        for segment in re.split(r"[.!?;\n]+", text)
    )
    sales = re.search(
        r"\bwe (?:sell|auction) (?:surplus )?assets\b|"
        r"\bwe match assets to[^.!?;\n]*\bbuyers\b[^.!?;\n]*"
        r"\bmanage the selling process\b",
        text,
        re.I,
    )
    return bool(categories and sales)


def _windows(text, criterion):
    # Evaluate every occurrence. This is semantic locality, not a sampled/capped
    # evidence packet; one unrelated sentence cannot lend another sentence a role.
    for segment in CLAUSE_BOUNDARY.split(text):
        if criterion.get("all"):
            if all(matches(segment, t) for t in criterion["terms"]):
                yield segment
            continue
        for term in criterion["terms"]:
            for hit in re.finditer(
                r"(?<!\w)" + re.escape(term) + r"(?:s|es)?(?!\w)", segment, re.I
            ):
                yield segment[max(0, hit.start() - 160) : hit.end() + 180]


def topic_records(spec, criterion, records):
    mentions, own, role = [], [], []
    relation = spec["relation"]
    owned_operation, non_owned = _operation_patterns(tuple(criterion["terms"]))
    for record in records:
        text = record["text"]
        if not (all if criterion.get("all") else any)(matches(text, t) for t in criterion["terms"]):
            continue
        mentions.append(record)
        if record.get("company_owned") is False or INCIDENTAL_PATH.search(
            urlsplit(record.get("source_url", "")).path
        ):
            continue
        for window in _windows(text, criterion):
            if THIRD_PARTY.search(window) or non_owned.search(window):
                continue
            operation = bool(owned_operation.search(window))
            if not operation and SERVICE_TO_CATEGORY.search(window):
                continue
            if criterion.get("suffix_exclusions"):
                target = (
                    "(?:" + "|".join(re.escape(t) + r"(?:s|es)?" for t in criterion["terms"]) + ")"
                )
                suffix = (
                    "(?:" + "|".join(re.escape(t) for t in criterion["suffix_exclusions"]) + ")"
                )
                if re.search(
                    target + r"\s+(?:(?:spare|replacement|oem)\s+)*" + suffix, window, re.I
                ):
                    continue
            if criterion["id"] == "machinery" and not spec.get("parts_requested"):
                category = (
                    "(?:" + "|".join(re.escape(t) + r"(?:s|es)?" for t in criterion["terms"]) + ")"
                )
                if re.search(
                    category + r"\s+(?:(?:spare|replacement|undercarriage|oem)\s+)*"
                    r"(?:parts|components|accessories|repair|maintenance|scales|batteries|"
                    r"attachments|weighing|sensors|tyres|tires)\b",
                    window,
                    re.I,
                ):
                    continue
                if re.search(
                    r"\b(?:parts|components|accessories)\s+for\s+" + category, window, re.I
                ):
                    continue
            if criterion["id"] == "machinery" and re.search(
                "(?:"
                + "|".join(re.escape(t) for t in criterion["terms"])
                + ")"
                + r"(?:\s+[\w-]+){0,3}\s+(?:software|crm|marketing|courses?|training)\b",
                window,
                re.I,
            ):
                continue
            heading = record.get("heading", "")
            heading_match = bool(
                heading
                and not THIRD_PARTY.search(heading)
                and not non_owned.search(heading)
                and (all if criterion.get("all") else any)(
                    matches(heading, t) for t in criterion["terms"]
                )
            )
            commercial = bool(SELLER.search(window))
            purchasing = bool(BUYER.search(window))
            activity = (
                operation or commercial or purchasing or bool(ACTIVITY.search(window))
                or heading_match
                or bool(_identity_pattern(tuple(criterion["terms"])).search(window))
            )
            role_terms = criterion.get("business_role", {}).get("activity_phrases", [])
            activity = activity or any(matches(window, t) for t in role_terms)
            if activity:
                own.append(record)
            valid_role = (
                activity
                if relation == "matching_company"
                else _bound_role(window, criterion, relation)
                or _asset_trade(text, window, criterion, relation)
            )
            if valid_role:
                if not activity:
                    own.append(record)
                role.append(record)
                break
    return mentions, own, role
