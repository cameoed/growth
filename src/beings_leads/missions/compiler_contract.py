"""Executable compiler teaching examples and actionable mapping diagnostics; no providers."""

import re

from .activity_catalog import ACTIVITY_ALIASES, activity_keys
from .bridge import CATALOG, NUMERIC, bindings
from .context import role_object

FOCUS_FACTS = {
    "recurring_activity": {
        "advertising.new_creatives_30d", "content.recent_30d", "event.recent_count",
        "media.recent_30d", "event.active_months", "content.active_months_180d",
        "work.recurring_kind",
    },
    "timing": {
        "event.upcoming", "event.upcoming_count", "work.kind", "work.recent_kind",
        "market.event_type", "product.release_type",
    },
    "scale": {"company.employee_count", "company.annual_revenue_usd", "production.large_scope"},
}


def focus_matches(criterion, focus):
    if focus == "fit":
        return criterion.operator == "own_activity"
    if criterion.operator != "evidence_fact" or not set(criterion.fact_keys) <= FOCUS_FACTS.get(
        focus, set()
    ):
        return False
    if focus == "recurring_activity":
        if criterion.fact_keys == ["work.recurring_kind"]:
            return criterion.comparison == "present"
        return criterion.comparison == "gte" and criterion.threshold >= 2
    return True


VALUE_DOMAINS = {
    "market.event_type": [
        "private_offering_notice",
        "registration_statement",
        "current_report_filing",
        "crowdfunding_filing",
        "regulation_a_filing",
        "material_agreement_disclosure",
        "acquisition_or_disposition_disclosure",
        "results_of_operations_disclosure",
        "unregistered_securities_sale_disclosure",
        "leadership_change_disclosure",
        "regulatory_release",
        "government_notice",
        "news_attention_metadata",
        "show_hn_submission",
        "hacker_news_attention",
    ],
    "work.kind": [
        "product_launch",
        "campaign_launch",
        "market_expansion",
        "agency_client_win",
        "contractor_request",
        "job_open",
        "localization_request",
    ],
    "creative.format": ["video", "image", "carousel", "unknown"],
    "app.platform": ["ios", "android"],
}
# Explicitly synthetic normalized records consumed by our actual evaluator.
# These teach the data language, not the Beings score or real company assertions.
EXAMPLES = [
    {
        "mission": "Commercial production agencies working with big brands",
        "evidence": [
            {
                "id": "owned",
                "text": "We are a commercial production agency. We produce films.",
                "source_url": "https://agency.example/",
                "company_owned": True,
                "weight": 1,
            },
            {
                "id": "client",
                "fact_key": "portfolio.major_brand",
                "value": True,
                "basis": "published_client_proof_known_brand_identity",
                "weight": 0.75,
            },
        ],
        "mapping": [
            {
                "operator": "own_activity",
                "terms": [
                    "commercial production agency",
                    "film production company",
                    "video production agency",
                ],
                "weight": 60,
                "required": True,
            },
            {
                "operator": "evidence_fact",
                "fact_keys": ["portfolio.major_brand"],
                "comparison": "present",
                "threshold": None,
                "weight": 25,
                "required": False,
            },
            {
                "operator": "evidence_fact",
                "fact_keys": ["production.large_scope"],
                "comparison": "present",
                "threshold": None,
                "weight": 15,
                "required": False,
            },
        ],
        "expected": "Agency ranks on core fit; cited brand proof strengthens it. Missing scale "
        "does not veto it. Logos cannot prove large scope or a current contract.",
    },
    {
        "mission": "NYC corporate photography prospects",
        "evidence": [
            {
                "id": "activity",
                "text": "Our corporate conferences take place in Manhattan.",
                "source_url": "https://events.example/",
                "company_owned": True,
                "weight": 1,
            },
            {
                "id": "event",
                "fact_key": "event.upcoming",
                "value": True,
                "basis": "observed",
                "weight": 1,
            },
        ],
        "mapping": "Core owned demand = conferences/corporate events, not photographers. "
        "Location is an own_location preference, dated events an event.upcoming bonus, "
        "hire photographers is a procurement phrase. Missing budget/intent stays unknown.",
        "expected": "Prospect ranks without a proven photography purchase; a photographer's "
        "site or its clients' events do not qualify as owned buyer demand.",
    },
    {
        "mission": "Machinery sellers with active advertising",
        "evidence": [
            {
                "id": "owned",
                "text": "We sell excavators and construction equipment.",
                "source_url": "https://dealer.example/",
                "company_owned": True,
                "weight": 1,
            },
            {
                "id": "ads",
                "fact_key": "advertising.active_ads",
                "value": 20,
                "measurement_basis": "lower_bound",
                "basis": "observed",
                "weight": 1,
            },
            {
                "id": "launch",
                "fact_key": "work.kind",
                "value": "product_launch",
                "basis": "observed",
                "weight": 0.75,
            },
        ],
        "mapping": "Seller role + excavator/construction equipment core. Numeric ad presence "
        "or an optional gte threshold uses advertising.active_ads, not owned_text. "
        "A cited workload product launch matches work.kind=product_launch (or product launch). "
        "Market filing values such as private_offering_notice are different signals; "
        "a filing does not prove funding or a launch.",
        "expected": "20 observed ads can support >=10, cannot prove <=30/exactly20, spend, "
        "revenue or buying intent. A bakery's ads cannot rescue machinery fit.",
    },
    {
        "mission": "Interpret signals used by a strong Beings prospect for a different mission",
        "evidence": [
            {
                "id": "media",
                "fact_key": "creative.asset_families",
                "value": 12,
                "measurement_basis": "lower_bound",
                "basis": "observed",
                "weight": 1,
            },
            {
                "id": "hiring",
                "fact_key": "demand.photo_video_openings",
                "value": 2,
                "basis": "observed",
                "weight": 0.75,
            },
        ],
        "expected": "A static media footprint and current photo/video openings may be relevant "
        "optional signals. They do not establish output cadence, contractor hiring, "
        "budget or a $5k subscription. Never transfer Beings scores/outcomes into "
        "another mission. Choose weights from the new goal, not the offer.",
    },
]
# Include the complete common evaluator contract, including provenance and freshness.
# Fixed illustrative dates keep compiler cache identity deterministic across saves.
for example in EXAMPLES:
    for record in example["evidence"]:
        record.setdefault("text", "")
        record.setdefault("heading", "")
        record.setdefault("root", record["id"])
        record.setdefault("company_owned", False)
        record.setdefault("confidence_weight", record["weight"])
        record.setdefault("observed_at", "2026-10-10T00:00:00+00:00")
        record.setdefault("source_url", "https://synthetic.example/proof")


def example_criterion(identifier, operator, terms, weight, *, keys=(), required=False):
    return {
        "id": identifier,
        "operator": operator,
        "required": required,
        "weight": weight,
        "groups": [{"alternatives": terms, "activity_fact_keys": []}],
        "group_match": "any",
        "fact_keys": list(keys),
        "comparison": "present",
        "threshold": None,
    }


# A complete strict-schema response, tested by the real normalizer/evaluator below.
# The other worked records illustrate how the same contract transfers across missions.
EXAMPLES[0]["complete_rubric"] = {
    "criteria": [
        example_criterion(
            "agency", "own_activity", EXAMPLES[0]["mapping"][0]["terms"], 60, required=True
        ),
        example_criterion(
            "clients", "evidence_fact", ["major brand"], 25, keys=["portfolio.major_brand"]
        ),
        example_criterion(
            "scope", "evidence_fact", ["large production"], 15, keys=["production.large_scope"]
        ),
    ],
    "exclusions": [],
    "business_role": {
        "relation": "matching_company",
        "buy_verbs": ["buy"],
        "sell_verbs": ["produce"],
        "activity_phrases": ["we produce films", "our production agency"],
    },
    "contact_terms": ["executive producer", "managing director", "head of production"],
    "unsupported_requirements": [],
}


def explicit_requirement(brief, terms=()):
    """Only explicit restriction language creates extra hard gates; defaults are preferences."""
    brief = re.sub(
        r"\b(?:do not|don't|does not|without|no need to|not necessary to)\s+"
        r"(?:requir\w*|need\w*|verify\w*|confirm\w*|prov\w*)[^,;.!?\n]*",
        "",
        brief,
        flags=re.I,
    )
    clauses = re.split(r"[.!?;\n]", brief.casefold())
    for clause in clauses:
        if not re.search(
            r"\b(?:must|only|require\w*|exclusively|verified|confirmed|proven)\b", clause
        ):
            continue
        if not terms or any(t.casefold() in clause for t in terms):
            return True
    return False


def criterion_requirement(brief, criterion):
    """A verified client restriction must not make unrelated location/ad preferences gates."""
    terms = [t for g in criterion.groups for t in g.alternatives]
    aliases = {
        "company.employee_count": ["employees", "headcount", "staff size"],
        "company.annual_revenue_usd": ["revenue"],
        "advertising.active_ads": ["active ads", "active advertising", "ad count"],
        "advertising.unique_creatives": ["distinct creatives", "unique creatives"],
        "portfolio.major_brand": ["major brands", "big brands", "global brands"],
        "production.large_scope": ["large-scale production", "massive commercials"],
    }
    for key in criterion.fact_keys:
        terms.append(key.split(".")[-1].replace("_", " "))
        terms.extend(aliases.get(key, []))
    return explicit_requirement(brief, terms)


def seo_buyer_brief(brief):
    service = r"(?:seo|search engine optimi[sz]ation)"
    return bool(re.search(
        rf"\b{service} buyers?\b"
        rf"|\bbuyers?\s+(?:in need of|seeking|looking for|of)\s+{service}\b"
        rf"|\b(?:companies|businesses|brands)\b[^.!?;]{{0,85}}\bneed for {service}\b",
        brief, re.I,
    ))


def mapping_gaps(rubric, brief, *, target_role=None):
    """Return precise fixable issues, without claims about whole-inventory match counts."""
    issues = []
    core = [c for c in rubric.criteria if c.operator == "own_activity" and c.required]
    if target_role in (None, "auto", "buyer") and seo_buyer_brief(brief):
        provider_terms = (
            r"\b(?:seo|search engine optimi[sz]ation) (?:agenc\w*|consult\w*|services?)\b"
        )
        purchase_terms = r"\b(?:seo|search engine optimi[sz]ation)\b"
        buyer_role = rubric.business_role
        if (target_role != "buyer" and buyer_role.relation != "buyer") or not any(
            re.search(purchase_terms, role_object(term) or "", re.I)
            for term in buyer_role.buy_verbs
        ) or any(
            re.search(provider_terms, term, re.I)
            for criterion in core for group in criterion.groups for term in group.alternatives
        ):
            issues.append({
                "code": "seo_buyer_category_mismatch",
                "blocking": True,
                "fix": "Use buyer relation and owned search-dependent activity as core fit. "
                       "Put a complete phrase such as hire an SEO agency in buy_verbs. "
                       "Keep SEO providers and purchased services out of the core category.",
            })
    if not any(
        not keys or activity_keys(group.alternatives)
        for c in core for group, keys in zip(c.groups, bindings(c.groups), strict=True)
    ):
        issues.append(
            {
                "code": "no_substantive_category",
                "blocking": True,
                "fix": "Provide a required owned business/activity category with meaningful "
                "synonyms. Advertising, client caliber, production scale and other signals "
                "cannot establish the target business by themselves.",
            }
        )
    for c in rubric.criteria:
        mapped = bindings(c.groups)
        for index, (group, keys) in enumerate(zip(c.groups, mapped, strict=True)):
            category = c.operator == "own_activity" and activity_keys(group.alternatives)
            if keys and not category and c.operator not in {"evidence_fact", "unsupported"}:
                issues.append(
                    {
                        "code": "literal_concept_gate",
                        "criterion_id": c.id,
                        "group": index,
                        "use_fact_keys": keys,
                        "fix": "Separate this concept into a typed weighted preference; "
                        "retain substantive category synonyms as core.",
                    }
                )
            if (
                c.operator == "own_activity" and c.required and not category
                and all(len(term.split()) >= 6 for term in group.alternatives)
            ):
                issues.append({
                    "code": "sentence_only_activity",
                    "criterion_id": c.id,
                    "group": index,
                    "fix": "Add concise business or activity labels found on company sites. "
                           "Preserve the target and keep genuine required constraints.",
                })
        if (
            c.operator == "own_activity"
            and c.group_match == "all"
            and len(c.groups) > 1
            and not any(explicit_requirement(brief, g.alternatives) for g in c.groups)
        ):
            issues.append(
                {
                    "code": "conjunctive_category_groups",
                    "criterion_id": c.id,
                    "fix": "Check whether these groups are synonyms or genuinely distinct "
                    "activities. Put synonyms in one OR group; keep AND only for an "
                    "explicitly conjunctive target. Separate qualitative preferences.",
                }
            )
    measurable = sum(c.weight for c in rubric.criteria if c.operator != "unsupported")
    if sum(c.weight for c in core) < 0.4 * measurable:
        issues.append(
            {
                "code": "weak_core_weight",
                "fix": "Ordinary core business fit should dominate optional corroboration "
                "unless the user explicitly prioritizes those other metrics.",
            }
        )
    for c in rubric.criteria:
        terms = [t for g in c.groups for t in g.alternatives]
        if (
            c.operator == "evidence_fact"
            and set(c.fact_keys) & {"work.kind", "work.recent_kind", "work.recurring_kind"}
            and not any(
                t.replace("_", " ") == value.replace("_", " ")
                for t in terms
                for value in VALUE_DOMAINS["work.kind"]
            )
        ):
            issues.append(
                {
                    "code": "unmatchable_enum_values",
                    "criterion_id": c.id,
                    "supported_values": VALUE_DOMAINS["work.kind"],
                    "fix": "Use actual workload enum values as OR alternatives. "
                    "Natural-language labels alone do not match these records.",
                }
            )
        if c.required and c.operator != "own_activity" and not criterion_requirement(brief, c):
            issues.append(
                {
                    "code": "unrequested_hard_gate",
                    "criterion_id": c.id,
                    "fix": "Make this an optional weighted preference. The brief has "
                    "no explicit must-have; unknown cannot veto ordinary prospects.",
                }
            )
        if c.operator == "own_activity" and c.required and len(set(terms)) < 2:
            issues.append(
                {
                    "code": "narrow_category_vocabulary",
                    "criterion_id": c.id,
                    "fix": "Add meaningful business/category synonyms; never generic "
                    "company, seller, buy/sell verbs or unrelated sectors.",
                }
            )
        if (
            c.operator == "evidence_fact"
            and c.comparison != "present"
            and not set(c.fact_keys) <= NUMERIC
        ):
            issues.append(
                {
                    "code": "non_numeric_comparison",
                    "criterion_id": c.id,
                    "fix": "Use a numeric catalog key in its stated units.",
                }
            )
    flat = " ".join(t for c in rubric.criteria for g in c.groups for t in g.alternatives)
    expected = []
    if re.search(r"\b(?:big|major|global|leading) brands\b", brief, re.I):
        expected.append("portfolio.major_brand")
    if re.search(r"\b(?:massive|large[- ]scale) (?:commercial|production)", brief, re.I):
        expected.append("production.large_scope")
    if re.search(r"\b(?:active advertising|active ads|ad activity|running ads)\b", brief, re.I):
        expected.append("advertising.active_ads")
    for key in expected:
        if not any(key in c.fact_keys for c in rubric.criteria):
            issues.append(
                {
                    "code": "missing_evidence_binding",
                    "fact_key": key,
                    "fix": "Map this requested preference to the supported catalog key. "
                    "Do not invent evidence or require exact marketing adjectives.",
                }
            )
    if not flat.strip():
        issues.append({"code": "no_activity_terms", "fix": "Provide target business nouns."})
    return issues


def strict_schema(schema):
    """Responses strict mode requires all properties, including nullable/defaulted fields."""
    if isinstance(schema, dict):
        schema = {k: strict_schema(v) for k, v in schema.items() if k != "default"}
        if schema.get("type") == "object":
            schema["required"] = list(schema.get("properties", {}))
            schema["additionalProperties"] = False
        return schema
    if isinstance(schema, list):
        return [strict_schema(v) for v in schema]
    return schema


def capability_packet():
    return {
        "catalog": CATALOG,
        "owned_activity_bindings": ACTIVITY_ALIASES,
        "coverage_rules": "Owned website language is the general-purpose route. Typed evidence "
        "requires a qualifying saved observation and coverage varies across companies. Use both "
        "routes when they prove the same activity. Missing specialized data is unknown.",
        "numeric_keys": sorted(NUMERIC),
        "string_value_examples": VALUE_DOMAINS,
        "value_rules": "work.kind is a closed enum; other strings are examples, not closed "
        "vocabularies. Market filings/attention cannot become confirmed funding or launches. "
        "creative.format/product are source-validated free text. Numeric keys use counts "
        "except company.annual_revenue_usd, which is annual USD.",
        "examples_are_synthetic": True,
        "worked_examples": EXAMPLES,
    }
