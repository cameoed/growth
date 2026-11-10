"""Validated declarative AI rubrics and exhaustive, provider-free local evaluation."""

import re
from datetime import date
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..microlink_mission import DiscoveryTermsV1
from .activity_catalog import ACTIVITY_ALIASES, activity_keys
from .bridge import CATALOG, NUMERIC, accepts, bindings
from .compiler_contract import (
    criterion_requirement,
    explicit_requirement,
    focus_matches,
    seo_buyer_brief,
)
from .context import (
    INCIDENTAL_PATH,
    THIRD_PARTY,
    _bound_role,
    _windows,
    matches,
    role_object,
    topic_records,
)


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


class Concept(ClosedModel):
    alternatives: list[str] = Field(min_length=1, max_length=20)
    activity_fact_keys: list[str] = Field(default_factory=list, max_length=len(ACTIVITY_ALIASES))

    @model_validator(mode="after")
    def validate_terms(self):
        self.alternatives = _terms(self.alternatives)
        self.activity_fact_keys = list(dict.fromkeys(self.activity_fact_keys))
        if not set(self.activity_fact_keys) <= set(activity_keys(self.alternatives)):
            raise ValueError(
                "Activity fact keys must be supported and paired with an exact catalog category "
                "label in the same group"
            )
        return self


def _terms(values):
    cleaned = list(dict.fromkeys(v.casefold().strip() for v in values))
    if any(not v or len(v) > 120 or not re.search(r"\w", v) for v in cleaned):
        raise ValueError("Terms must be nonempty literal phrases of at most 120 characters")
    return cleaned


class Criterion(ClosedModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    operator: Literal["own_activity", "own_location", "owned_text", "unsupported", "evidence_fact"]
    required: bool
    weight: int = Field(ge=1, le=100, strict=True)
    groups: list[Concept] = Field(min_length=1, max_length=6)
    group_match: Literal["all", "any"]
    fact_keys: list[str] = Field(default_factory=list, max_length=8)
    comparison: Literal["present", "gte", "lte", "eq"] = "present"
    threshold: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_binding(self):
        if self.operator != "own_activity" and any(g.activity_fact_keys for g in self.groups):
            raise ValueError("Activity fact keys require the own_activity operator")
        if any(key not in CATALOG for key in self.fact_keys):
            raise ValueError("Unknown evidence catalog binding")
        if self.operator == "evidence_fact" and not self.fact_keys:
            raise ValueError("Typed evidence criteria need catalog bindings")
        if self.operator != "evidence_fact" and self.fact_keys:
            raise ValueError("Fact bindings require the evidence_fact operator")
        if self.comparison != "present":
            if (
                self.operator != "evidence_fact"
                or self.threshold is None
                or any(key not in NUMERIC for key in self.fact_keys)
            ):
                raise ValueError(
                    "Numeric comparisons require numeric catalog bindings and a threshold"
                )
        elif self.threshold is not None:
            raise ValueError("Presence criteria cannot set a numeric threshold")
        return self


class Exclusion(ClosedModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    scope: Literal["category_suffix", "own_business"]
    terms: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_terms(self):
        self.terms = _terms(self.terms)
        return self


class BusinessRole(ClosedModel):
    relation: Literal["matching_company", "buyer", "seller", "trader"]
    buy_verbs: list[str] = Field(min_length=1, max_length=12)
    sell_verbs: list[str] = Field(min_length=1, max_length=12)
    activity_phrases: list[str] = Field(min_length=1, max_length=15)

    @model_validator(mode="after")
    def validate_terms(self):
        for key in ["buy_verbs", "sell_verbs", "activity_phrases"]:
            setattr(self, key, _terms(getattr(self, key)))
        return self


class Rubric(ClosedModel):
    criteria: list[Criterion] = Field(min_length=1, max_length=12)
    exclusions: list[Exclusion] = Field(max_length=12)
    business_role: BusinessRole
    contact_terms: list[str] = Field(min_length=1, max_length=15)
    unsupported_requirements: list[str] = Field(max_length=12)
    discovery_terms_v1: DiscoveryTermsV1 | None = None

    @model_validator(mode="after")
    def validate_contract(self):
        ids = [c.id for c in self.criteria] + [e.id for e in self.exclusions]
        if len(ids) != len(set(ids)) or "business_role" in ids:
            raise ValueError("Rubric IDs must be unique and business_role is reserved")
        if not any(c.required and c.operator == "own_activity" for c in self.criteria):
            raise ValueError("A required company-activity criterion is mandatory")
        generic = {
            "company",
            "companies",
            "business",
            "businesses",
            "sell",
            "selling",
            "buy",
            "buying",
            "auction",
            "auctions",
            "marketplace",
            "dealer",
            "sellers",
            "buyers",
            "shop",
        }
        if any(
            term in generic
            for c in self.criteria
            if c.operator == "own_activity"
            for g in c.groups
            for term in g.alternatives
        ):
            raise ValueError("Generic trading/instructional words cannot define company activity")
        self.contact_terms = _terms(self.contact_terms)
        if any(len(v) > 240 for v in self.unsupported_requirements):
            raise ValueError("Unsupported requirement exceeds limit")
        return self


def normalize_rubric(rubric):
    """Separate duplicated first-person role phrases from product synonym groups.

    The declarative global trader relation is OR across buy/sell/facilitation.
    Equivalent category gates must not accidentally turn those alternatives into
    AND through redundant role-only groups. Preserve their combined relative weight.
    Original provider rubrics remain in the compilation ledger/version history.
    """
    value = rubric.model_dump()
    role = value["business_role"]
    # Both "buy media" and "we buy media" are valid compiler phrasings.
    # Downstream role matching supplies its own first-person subject.
    for field in ("buy_verbs", "sell_verbs"):
        role[field] = list(dict.fromkeys(re.sub(r"^we\s+", "", term) for term in role[field]))
    role_phrases = {"we " + v for v in [*role["buy_verbs"], *role["sell_verbs"]]}
    role_phrases.update(role["activity_phrases"])
    normalized = []
    for criterion in value["criteria"]:
        if criterion["operator"] == "own_activity":
            kept = [
                g
                for g in criterion["groups"]
                if activity_keys(g["alternatives"])
                or not all(t in role_phrases for t in g["alternatives"])
            ]
            if not kept:
                raise ValueError(
                    "Activity criteria must contain substantive product/category terms"
                )
            stripped = len(kept) != len(criterion["groups"])
            criterion["groups"] = kept
            previous = next(
                (
                    c
                    for c in normalized
                    if stripped
                    and c["operator"] == criterion["operator"]
                    and c["required"] == criterion["required"]
                    and c["groups"] == kept
                    and c["group_match"] == criterion["group_match"]
                    and c["weight"] + criterion["weight"] <= 100
                ),
                None,
            )
            if previous and role["relation"] == "trader":
                previous["weight"] += criterion["weight"]
                previous["id"] = "trading_fit_" + str(normalized.index(previous))
                continue
        normalized.append(criterion)
    value["criteria"] = normalized
    routing = {"contact us", "phone", "email", "request a quote", "contact", "website"}
    value["contact_terms"] = [t for t in value["contact_terms"] if t not in routing]
    if not value["contact_terms"]:
        value["contact_terms"] = [
            "owner",
            "president",
            "sales" if role["relation"] in {"seller", "trader"} else "purchasing",
        ]
    return Rubric.model_validate(value)


def _unknown_kind(text):
    if re.search(r"\bbudget\b|willingness|afford", text, re.I):
        return "budget"
    if re.search(r"upcoming|future|event.{0,30}date|scheduled|current.{0,40}need", text, re.I):
        return "timing"
    if re.search(r"intent|hire|commission|procure|purchas|buying", text, re.I):
        return "purchase"
    return None


def _explicit_constraint(brief, kind):
    """Vague buying goals are prospecting; explicit factual restrictions stay gates."""
    # An explicit opt-out must not become a verified-only gate merely because it
    # names that gate. Other clauses still retain their independently stated limits.
    brief = re.sub(
        r"\b(?:do not|don't|does not|without|no need to|not necessary to)\s+"
        r"(?:requir\w*|need\w*|verify\w*|confirm\w*|prov\w*)[^,;.!?\n]*",
        "",
        brief,
        flags=re.I,
    )
    concepts = {
        "budget": r"budget|afford|spend|pay|revenue",
        "timing": r"event|date|upcoming|scheduled|need|purchase",
        "purchase": r"buy|purchas|hire|commission|procure|intent|photograph",
    }
    concept = concepts[kind]
    strict = r"\b(?:must|only|requir\w*|verified|confirmed|proven|actually|actively|already)\b"
    if re.search(strict + r"[^.!?;\n]{0,100}(?:" + concept + ")", brief, re.I):
        return True
    if re.search(r"(?:" + concept + r")[^.!?;\n]{0,80}" + strict, brief, re.I):
        return True
    if kind == "timing" and re.search(
        r"\b(?:next|this) (?:week|month|year)\b|\b(?:on|by|before|after|within|between)\s+"
        r"(?:the\s+)?(?:next\s+)?\d|\b\d{4}-\d{2}-\d{2}\b",
        brief,
        re.I,
    ):
        return True
    if kind == "budget" and re.search(
        r"\b(?:budget|spend|pay|afford)\b[^.!?;\n]{0,60}(?:\d|at least|minimum|maximum)"
        r"|(?:\d|minimum|maximum)[^.!?;\n]{0,60}\bbudget\b",
        brief,
        re.I,
    ):
        return True
    if kind == "timing" and re.search(
        r"\b(?:today|tomorrow|tonight|january|february|march|april|june|july|august|"
        r"september|october|november|december|monday|tuesday|wednesday|thursday|friday|"
        r"saturday|sunday)\b|\b(?:in|on|by|during|this|next)\s+may\b",
        brief,
        re.I,
    ):
        return True
    if kind == "budget" and re.search(
        r"\b(?:with|have|having|has)\b[^.!?;\n]{0,45}\bbudget\b|budget of|willing to pay",
        brief,
        re.I,
    ):
        return True
    return kind == "purchase" and bool(
        re.search(
            r"\b(?:currently|already|actively)\s+(?:buying|hiring|seeking|purchasing|commissioning)\b"
            r"|\bthat (?:hire|buy|purchase|commission|procure)\b",
            brief,
            re.I,
        )
    )


def _qualification(rubric, brief):
    """Repair saved prospect rubrics without discarding their unknowns or hard facts."""
    value = rubric.model_dump()
    unknown, blocking, changes = [], [], []
    for text in value["unsupported_requirements"]:
        kind = _unknown_kind(text)
        optional = (kind and not _explicit_constraint(brief, kind)) or (
            not kind and not explicit_requirement(brief)
        )
        (unknown if optional else blocking).append(text)
    for criterion in value["criteria"]:
        if criterion["operator"] != "unsupported":
            continue
        text = " ".join(
            [criterion["id"], *[t for g in criterion["groups"] for t in g["alternatives"]]]
        ).replace("_", " ")
        kind = _unknown_kind(text)
        if (kind and not _explicit_constraint(brief, kind)) or (
            not kind and not explicit_requirement(brief)
        ):
            if criterion["required"]:
                changes.append(criterion["id"])
            criterion["required"] = False
    # A supplied complete service-purchase phrase separates the thing being bought
    # from the company's demand activity (e.g. hire photographers / host events).
    role = value["business_role"]
    categories = [
        t
        for c in value["criteria"]
        if c["operator"] == "own_activity"
        for g in c["groups"]
        for t in g["alternatives"]
    ]
    separate_service = any(
        role_object(phrase) and not any(matches(phrase, term) for term in categories)
        for phrase in role["buy_verbs"]
    )
    mode = (
        "prospect"
        if (
            role["relation"] == "buyer"
            and separate_service
            and not _explicit_constraint(brief, "purchase")
        )
        else "purchase"
    )
    return Rubric.model_validate(value), {
        "buyer_mode": mode,
        "separate_purchase_subject": separate_service,
        "unknown_constraints": unknown,
        "blocking_unsupported_requirements": blocking,
        "optionalized_criteria": changes,
    }


SEO_VISIBILITY_TERMS = {
    "search visibility", "organic search visibility", "organic search performance",
    "ranked organic results",
}


def _criterion_bindings(criterion, *, seo_buyer):
    if criterion.operator == "own_location":
        return [["company.location"] for _ in criterion.groups]
    if criterion.operator == "unsupported":
        return [[] for _ in criterion.groups]
    result = []
    for keys, group in zip(bindings(criterion.groups), criterion.groups, strict=True):
        if criterion.operator == "own_activity":
            own_keys = group.activity_fact_keys or activity_keys(group.alternatives)
            if own_keys or not keys:
                keys = ["company.activity", *own_keys]
        if (
            seo_buyer and criterion.operator == "owned_text" and not criterion.required
            and SEO_VISIBILITY_TERMS.intersection(group.alternatives)
        ):
            keys = [*keys, "search.organic_ranked_results"]
        result.append(keys)
    return result


def as_spec(rubric, brief, *, version, model, cache_key, setup=None):
    if setup is not None and setup.target_role != "auto":
        role = rubric.business_role.model_copy(update={"relation": setup.target_role})
        rubric = rubric.model_copy(update={"business_role": role})
    rubric = normalize_rubric(rubric)
    rubric, qualification = _qualification(rubric, brief)
    preferences = {}
    value = rubric.model_dump()
    for criterion in value["criteria"]:
        if criterion["operator"] not in {
            "own_activity",
            "unsupported",
        } and not criterion_requirement(brief, Criterion.model_validate(criterion)):
            criterion["required"] = False
        concepts = [Concept.model_validate(g) for g in criterion["groups"]]
        mapped = [
            [] if activity_keys(group.alternatives) else keys
            for group, keys in zip(concepts, bindings(concepts), strict=True)
        ]
        if (
            criterion["operator"] == "own_activity"
            and criterion["group_match"] == "all"
            and any(mapped)
        ):
            core = [i for i, keys in enumerate(mapped) if not keys]
            optional = [i for i, keys in enumerate(mapped) if keys]
            if core and optional:
                preferences[criterion["id"]] = {
                    "core_groups": core,
                    "hard_groups": [
                        i
                        for i in optional
                        if criterion_requirement(
                            brief,
                            Criterion.model_validate(
                                {
                                    **criterion,
                                    "operator": "evidence_fact",
                                    "fact_keys": mapped[i],
                                    "groups": [criterion["groups"][i]],
                                }
                            ),
                        )
                    ],
                    "weights": [
                        0.6 / len(core) if i in core else 0.4 / len(optional)
                        for i in range(len(mapped))
                    ],
                }
    rubric = Rubric.model_validate(value)
    seo_buyer = rubric.business_role.relation == "buyer" and seo_buyer_brief(brief)
    groups = {"topics": [], "geography": [], "formats": []}
    for c in rubric.criteria:
        field = (
            "topics"
            if c.operator == "own_activity"
            else ("geography" if c.operator == "own_location" else "formats")
        )
        groups[field].append(
            {
                "id": c.id,
                "terms": [t for g in c.groups for t in g.alternatives],
                "required": c.required,
                "role": "fit"
                if field == "topics"
                else ("geography" if field == "geography" else "transaction_format"),
            }
        )
    return {
        **groups,
        **({
            "setup": setup.model_dump(),
            "focused_criteria": [
                c.id for c in rubric.criteria if focus_matches(c, setup.ranking_focus)
            ],
        } if setup is not None else {}),
        "version": version,
        "brief": brief,
        "compiler": "ai-rubric-v1",
        "compiler_model": model,
        "compiler_cache_key": cache_key,
        "compiled_on": date.today().isoformat(),
        "rubric": rubric.model_dump(exclude={"discovery_terms_v1"}),
        **(
            {"discovery_terms_v1": rubric.discovery_terms_v1.model_dump()}
            if rubric.discovery_terms_v1 is not None else {}
        ),
        "qualification": qualification,
        "preference_groups": preferences,
        "evidence_bindings": {
            c.id: _criterion_bindings(c, seo_buyer=seo_buyer) for c in rubric.criteria
        },
        "relation": rubric.business_role.relation,
        "contact_terms": rubric.contact_terms,
        "business_role": {
            "id": "business_role",
            "role": "business_role",
            "required": True,
            "terms": [],
            "relation": rubric.business_role.relation,
        },
        "ai_adjustment_limit": 20,
    }


def assess_rubric(spec, records, *, review=None):
    rubric = Rubric.model_validate(spec["rubric"])
    owned = [r for r in records if r.get("company_owned", False)]
    facts = [r for r in records if r.get("fact_key")]
    if any("work.recurring_kind" in c.fact_keys for c in rubric.criteria):
        events = {}
        for r in facts:
            if r["fact_key"] == "work.recent_kind" and r.get("effective_at"):
                events.setdefault(r["root"], []).append(r)
        by_kind = {}
        for group in events.values():
            if len({(r["effective_at"], r["value"]) for r in group}) == 1:
                by_kind.setdefault(group[0]["value"], []).extend(group)
        facts.extend(
            {**r, "fact_key": "work.recurring_kind"}
            for group in by_kind.values()
            if len({r["effective_at"][:7] for r in group}) >= 2
            for r in group
        )
    prospect_role = spec.get("qualification", {}).get("buyer_mode") == "prospect"
    criteria, role_ids, activity_ids = [], set(), set()
    focused = set(spec.get("focused_criteria", []))
    suffixes = [t for e in rubric.exclusions if e.scope == "category_suffix" for t in e.terms]
    for criterion in rubric.criteria:
        groups = []
        for index, group in enumerate(criterion.groups):
            ids = set()
            keys = (
                []
                if criterion.operator == "unsupported"
                else criterion.fact_keys
                if criterion.operator == "evidence_fact"
                else spec.get("evidence_bindings", {}).get(
                    criterion.id, [[]] * len(criterion.groups)
                )[index]
            )
            fact_ids = {
                r["id"]
                for r in facts
                if r["fact_key"] not in ACTIVITY_ALIASES or r.get("company_owned")
                if accepts(r, keys, group.alternatives, criterion.comparison, criterion.threshold)
            }
            ids.update(fact_ids)
            if criterion.operator == "own_activity" and "company.activity" in keys:
                activity_ids.update(fact_ids)
                if rubric.business_role.relation == "matching_company":
                    role_ids.update(fact_ids)
            if criterion.operator == "own_activity" and not set(keys) & {
                "portfolio.major_brand",
                "production.large_scope",
            }:
                context = {
                    "id": criterion.id,
                    "terms": group.alternatives,
                    "suffix_exclusions": suffixes,
                    "business_role": rubric.business_role.model_dump(),
                    "separate_purchase_subject": spec.get("qualification", {}).get(
                        "separate_purchase_subject", False
                    ),
                }
                _, own, role = topic_records(spec, context, owned)
                ids.update(r["id"] for r in (own if prospect_role else role))
                role_ids.update(r["id"] for r in role)
                activity_ids.update(r["id"] for r in own)
            elif criterion.operator == "own_location":
                ids.update(r["id"] for r in owned if _location_record(r, group.alternatives))
            elif criterion.operator == "owned_text":
                ids.update(
                    r["id"]
                    for r in owned
                    if any(matches(r["text"], term) for term in group.alternatives)
                )
            strength = max((r["weight"] for r in records if r["id"] in ids), default=0)
            groups.append({"index": index, "strength": strength, "record_ids": sorted(ids)})
        preference = spec.get("preference_groups", {}).get(criterion.id)
        strength = (min if criterion.group_match == "all" else max)(g["strength"] for g in groups)
        requirement_strength = strength
        if preference:
            requirement_strength = min(groups[i]["strength"] for i in preference["core_groups"])
            if preference.get("hard_groups"):
                requirement_strength = min(
                    requirement_strength,
                    *(groups[i]["strength"] for i in preference["hard_groups"]),
                )
            strength = sum(
                g["strength"] * w for g, w in zip(groups, preference["weights"], strict=True)
            )
            for group, weight in zip(groups, preference["weights"], strict=True):
                group["relative_weight"] = weight
        criteria.append(
            {
                "id": criterion.id,
                "role": criterion.operator,
                "required": criterion.required,
                "weight": criterion.weight * 1.5 if criterion.id in focused else criterion.weight,
                "strength": strength,
                "requirement_strength": requirement_strength,
                "state": "inferred_match" if strength else "unknown",
                "record_ids": sorted({i for g in groups for i in g["record_ids"]}),
                "groups": groups,
            }
        )
    excluded, exclusion_audit = False, []
    for exclusion in rubric.exclusions:
        if exclusion.scope != "own_business":
            continue  # Suffix exclusions reject a product predicate, not the whole company.
        ids = []
        for record in owned:
            if INCIDENTAL_PATH.search(urlsplit(record.get("source_url", "")).path):
                continue
            if not any(matches(record["text"], t) for t in exclusion.terms):
                continue
            heading = record.get("heading", "")
            terms = "(?:" + "|".join(re.escape(t) for t in exclusion.terms) + r")(?!\w)"
            if any(matches(heading, t) for t in exclusion.terms) or any(
                not THIRD_PARTY.search(window)
                and (
                    _bound_role(window, {"terms": exclusion.terms}, "seller")
                    or re.search(
                        r"\bwe (?:are|operate|run|offer|provide)\s+(?:(?:a|an|professional|"
                        r"commercial|leading|local)\s+){0,3}" + terms,
                        window,
                        re.I,
                    )
                )
                for window in _windows(record["text"], {"terms": exclusion.terms})
            ):
                ids.append(record["id"])
        excluded |= bool(ids)
        exclusion_audit.append({"id": exclusion.id, "record_ids": sorted(set(ids))})
    purchase_known = bool(role_ids)
    role_known = purchase_known or (prospect_role and bool(activity_ids))
    if review:
        for item in review["assessments"]:
            if item["state"] == "unknown":
                continue
            cited = [r for r in owned if r["id"] in item["record_ids"]]
            strength = max((r["weight"] for r in cited), default=0)
            supported = item["state"] == "supported" and bool(strength)
            if item["criterion_id"] == "business_role":
                role_known = supported
            for criterion in criteria:
                if criterion["id"] == item["criterion_id"] and criterion["role"] not in {
                    "unsupported",
                    "evidence_fact",
                }:
                    criterion.update(
                        strength=strength if supported else 0,
                        requirement_strength=strength if supported else 0,
                        state="ai_inferred_match" if supported else "ai_contradicted",
                        record_ids=item["record_ids"],
                    )
    eligible = bool(
        role_known
        and not excluded
        and not spec.get("qualification", {}).get(
            "blocking_unsupported_requirements", rubric.unsupported_requirements
        )
        and all(c.get("requirement_strength", c["strength"]) for c in criteria if c["required"])
    )
    denominator = sum(c["weight"] for c in criteria if c["role"] != "unsupported")
    numerator = sum(c["weight"] * c["strength"] for c in criteria)
    purchase_strength = max((r["weight"] for r in owned if r["id"] in role_ids), default=0)
    purchase_bonus = 10 * purchase_strength if prospect_role and eligible else 0
    fit_budget = 90 if prospect_role else 100
    score = (
        min(95, round(fit_budget * numerator / denominator + purchase_bonus)) if eligible else None
    )
    for criterion in criteria:
        criterion["score_contribution"] = (
            round(fit_budget * criterion["weight"] * criterion["strength"] / denominator, 6)
            if eligible
            else 0
        )
    return score, {
        "criteria": criteria,
        "measurable_weight": denominator,
        "role_known": role_known,
        "role_state": (
            "unknown"
            if not role_known
            else "inferred_purchase_signal"
            if purchase_known and rubric.business_role.relation == "buyer"
            else "inferred_prospect"
            if prospect_role
            else "inferred_match"
        ),
        "purchase_signal_contribution": round(purchase_bonus, 6),
        "role_record_ids": sorted(role_ids),
        "company_activity_record_ids": sorted(activity_ids),
        "required_category_supported": all(
            c["requirement_strength"]
            for c in criteria
            if c["required"] and c["role"] == "own_activity"
        ),
        "exclusions": exclusion_audit,
        "excluded": excluded,
        "unsupported_requirements": rubric.unsupported_requirements,
        "ai_reviewed": review is not None,
        "probability": False,
        "weighted_total": numerator,
        "weight_total": denominator,
    }


def _location_record(record, terms):
    if INCIDENTAL_PATH.search(urlsplit(record.get("source_url", "")).path):
        return False
    for term in terms:
        target = r"(?<!\w)" + re.escape(term) + r"(?!\w)"
        for window in _windows(record["text"], {"terms": [term]}):
            if THIRD_PARTY.search(window):
                continue
            if re.search(
                r"\b(?:in|near|serving|across|within|at|headquarters:|address:)\s+"
                + target
                + "|"
                + target
                + r"\s+(?:office|headquarters|address|location|events)\b",
                window,
                re.I,
            ) or re.search(r"\baddress:\s+[^.!?;\n]{0,100}" + target, window, re.I):
                return True
    return False
