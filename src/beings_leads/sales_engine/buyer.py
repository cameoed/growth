"""Evidence-bound buyer qualification for the independent marketing workbench.

This is a research policy, not a purchase probability. Public activity supports a
question; it cannot establish an approved service budget or purchasing authority.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..value_utils import aware_datetime as _date
from .models import DecisionFeature, normalize_company_key

Criterion = Literal[
    "recurring_need",
    "production_gap",
    "monthly_budget",
    "decision_authority",
    "timing",
    "ai_acceptability",
]
Answer = Literal["yes", "no", "unknown"]
FRESH_DAYS = 90
CONFLICT_DAYS = 7
OFFER_MONTHLY_USD = 5_000
CRITERIA = {
    "recurring_need": (
        "Recurring creative need",
        "What creative work must recur over the next 90 days?",
    ),
    "production_gap": (
        "Uncovered production need",
        "Which recurring work is delayed or uncovered by the current process?",
    ),
    "monthly_budget": (
        "Approved $5,000 monthly budget",
        "Is at least $5,000/month approved for this service, and what expense would it replace?",
    ),
    "decision_authority": (
        "Decision authority",
        "Who can approve this recurring service purchase?",
    ),
    "timing": (
        "Current buying window",
        "When does this work need to start, and is there a current buying window?",
    ),
    "ai_acceptability": (
        "AI and brand requirements",
        "Can AI-assisted production meet the brand, rights and fidelity requirements?",
    ),
}


class BuyerFact(BaseModel):
    """An explicit, attributable operator record; no untyped extra facts are accepted."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    idempotency_key: str = Field(min_length=1, max_length=120, pattern=r"^[\w.:-]+$")
    company_key: str = Field(min_length=1, max_length=253)
    criterion: Criterion
    answer: Answer
    note: str = Field(min_length=1, max_length=4000)
    source_kind: Literal["buyer_conversation", "operator_observation", "public_source"]
    source_url: str | None = Field(default=None, max_length=2048)
    recorded_by: str = Field(min_length=1, max_length=160)
    observed_at: datetime
    monthly_budget_usd: float | None = Field(default=None, strict=True, ge=0, le=1_000_000_000)

    _company = field_validator("company_key")(normalize_company_key)

    source = field_validator("source_url")(classmethod(DecisionFeature.feature_source.__func__))

    @field_validator("observed_at")
    @classmethod
    def observation_clock(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        value = value.astimezone(UTC)
        if value > datetime.now(UTC):
            raise ValueError("buyer facts cannot be observed in the future")
        return value

    @model_validator(mode="after")
    def explicit_budget_and_source(self) -> Self:
        if self.source_kind == "public_source" and not self.source_url:
            raise ValueError("public source facts require their actual source URL")
        if self.criterion != "monthly_budget" and self.monthly_budget_usd is not None:
            raise ValueError("monthly_budget_usd belongs only to the monthly_budget criterion")
        if (
            self.criterion == "monthly_budget"
            and self.answer == "yes"
            and self.monthly_budget_usd is None
        ):
            raise ValueError("a positive monthly budget answer requires an explicit USD amount")
        return self


def _available(item: dict, now: datetime) -> bool:
    # Mutable account/claim/opportunity payloads describe their latest state. A
    # source's old observation date cannot make a later edit available in the past.
    dates = [_date(item.get(key)) for key in ("recorded_at", "created_at", "updated_at")]
    return all(stamp is None or stamp <= now for stamp in dates)


def _evidence(
    note: str,
    observed_at: datetime,
    *,
    source_kind: str,
    source_url: str | None = None,
    recorded_by: str = "",
    fact_id: int | str | None = None,
) -> dict:
    evidence = {
        "source_url": source_url,
        "source_kind": source_kind,
        "note": note,
        "observed_at": observed_at.isoformat(),
        "recorded_by": recorded_by,
    }
    if fact_id is not None:
        evidence["fact_id"] = fact_id
    return evidence


def _add(
    statements: dict[str, list[dict]],
    criterion: str,
    answer: str,
    evidence: dict,
    *,
    confirmed_source: bool = False,
    summary: str = "",
    amount: float | None = None,
) -> None:
    if criterion in statements:
        statements[criterion].append(
            {
                "answer": answer,
                "evidence": evidence,
                "confirmed_source": confirmed_source,
                "summary": summary or evidence["note"],
                "amount": amount,
            }
        )


def _fact_statements(packet: dict, facts: list[dict], now: datetime, statements: dict) -> None:
    for raw in facts:
        if not isinstance(raw, dict) or not _available(raw, now):
            continue
        try:
            fact = BuyerFact.model_validate(
                {k: v for k, v in raw.items() if k in BuyerFact.model_fields}
            )
        except ValueError:
            continue
        if fact.company_key != packet["company_key"] or fact.observed_at > now:
            continue
        fact_id = raw.get("id", fact.idempotency_key)
        amount = fact.monthly_budget_usd
        answer = fact.answer
        summary = fact.note
        if fact.criterion == "monthly_budget" and answer == "yes" and amount is not None:
            answer = "yes" if amount >= OFFER_MONTHLY_USD else "no"
            summary = f"Recorded monthly service budget: ${amount:,.2f}. {fact.note}"
        _add(
            statements,
            fact.criterion,
            answer,
            _evidence(
                fact.note,
                fact.observed_at,
                source_kind=fact.source_kind,
                source_url=fact.source_url,
                recorded_by=fact.recorded_by,
                fact_id=fact_id,
            ),
            confirmed_source=fact.source_kind == "buyer_conversation",
            summary=summary,
            amount=amount,
        )


def _account_statements(packet: dict, now: datetime, statements: dict) -> dict | None:
    account = packet.get("account") or {}
    if not _available(account, now):
        return None
    discovery = account.get("discovery") or {}
    held = _date(discovery.get("held_at"))
    completed = bool(
        discovery.get("completed") and held and held <= now and _available(discovery, now)
    )
    negative = None
    if completed:
        assert held is not None

        def evidence(note: str) -> dict:
            return _evidence(note, held, source_kind="buyer_conversation")

        recurring = discovery.get("confirmed_recurring_need")
        if recurring is not None:
            _add(
                statements,
                "recurring_need",
                "yes" if recurring else "no",
                evidence(
                    "Completed discovery recorded "
                    + ("a recurring need." if recurring else "no recurring need.")
                ),
                confirmed_source=True,
            )
        constraint = discovery.get("creative_constraint")
        if constraint in {"primary", "contributing", "not_primary"}:
            _add(
                statements,
                "production_gap",
                "no" if constraint == "not_primary" else "yes",
                evidence(f"Buyer described creative production as {constraint.replace('_', ' ')}."),
                summary=(
                    f"Creative is {constraint.replace('_', ' ')} in the recorded discovery. "
                    "Confirm whether specific work is actually uncovered."
                ),
            )
        approver = discovery.get("approver") or {}
        if approver.get("name") or approver.get("role"):
            identity = ", ".join(filter(None, [approver.get("name"), approver.get("role")]))
            _add(
                statements,
                "decision_authority",
                "yes",
                evidence(f"Recorded work approver: {identity}."),
                summary=(
                    f"Recorded work approver: {identity}. "
                    "Work approval does not establish authority to buy the service."
                ),
            )
        if discovery.get("must_ship_30_days") and now - held <= timedelta(days=30):
            _add(
                statements,
                "timing",
                "yes",
                evidence(
                    "Buyer recorded work due within 30 days: "
                    + "; ".join(discovery["must_ship_30_days"])
                ),
                summary="Near-term work is recorded; confirm the service purchase can start now.",
            )
        if recurring is None and any(
            discovery.get(k)
            for k in (
                "creative_backlog",
                "must_ship_30_days",
                "must_ship_60_days",
                "must_ship_90_days",
            )
        ):
            _add(
                statements,
                "recurring_need",
                "yes",
                evidence(
                    "Discovery records a work backlog; its recurrence still requires confirmation."
                ),
            )
        outcome = discovery.get("outcome")
        mapping = {"not_fit": "production_gap", "no_budget": "monthly_budget", "not_now": "timing"}
        if outcome in mapping:
            reason = (
                discovery.get("outcome_reason")
                or f"Completed discovery: {outcome.replace('_', ' ')}."
            )
            _add(statements, mapping[outcome], "no", evidence(reason), confirmed_source=True)
            negative = {
                "outcome": outcome,
                "criterion": mapping[outcome],
                "observed_at": held,
                "reason": reason,
            }
    claim_criteria = {
        "recurring_work_3_months": "recurring_need",
        "recurring_creative_demand": "recurring_need",
        "production_capacity_gap": "production_gap",
        "economic_buyer": "decision_authority",
        "ai_permissible": "ai_acceptability",
        "annual_capacity_60k": "monthly_budget",
    }
    for claim in account.get("claims", []):
        observed = _date(claim.get("observed_at"))
        verified = _date(claim.get("verified_at"))
        if not observed or observed > now or not _available(claim, now):
            continue
        answer = {"met": "yes", "not_met": "no"}.get(claim.get("truth_state"))
        if not answer or claim.get("epistemic_status") != "observed":
            continue
        trusted = bool(
            claim.get("source_kind") == "discovery_call"
            and claim.get("human_verified")
            and claim.get("verified_by")
            and verified
            and verified <= now
        )
        source_kind = "buyer_conversation" if trusted else "operator_observation"
        if claim.get("source_url") and not trusted:
            source_kind = "public_source"
        for key in {*claim.get("signals", []), *claim.get("gates", [])}:
            criterion = claim_criteria.get(key)
            if criterion:
                _add(
                    statements,
                    criterion,
                    answer,
                    _evidence(
                        claim.get("claim", ""),
                        observed,
                        source_kind=source_kind,
                        source_url=claim.get("source_url"),
                        recorded_by=claim.get("verified_by", ""),
                    ),
                    confirmed_source=trusted and criterion != "monthly_budget",
                )
    return negative


def _opportunity_statements(packet: dict, now: datetime, statements: dict) -> None:
    saved = packet.get("opportunities") or []
    saved_ids = {item.get("id") for item in saved}
    opportunities = [item for item in saved if item.get("status") != "closed"] + [
        item.get("opportunity", {})
        for item in packet.get("suggested_opportunities", [])
        if item.get("opportunity", {}).get("id") not in saved_ids
    ]
    for item in opportunities:
        if item.get("company_key") != packet["company_key"] or not _available(item, now):
            continue
        for citation in item.get("evidence", []):
            observed = _date(citation.get("observed_at"))
            if (
                not observed
                or observed > now
                or citation.get("subject_company_key") != packet["company_key"]
            ):
                continue
            conversational = bool(
                citation.get("evidence_kind") == "human_confirmed"
                and citation.get("source_kind") == "buyer_conversation"
                and citation.get("recorded_by")
            )
            # Legacy "human_confirmed" rows lacking provenance remain visible in their
            # opportunity, but do not establish a buyer fact or a public source quotation.
            if citation.get("evidence_kind") == "human_confirmed" and not conversational:
                continue
            if not conversational and not citation.get("source_url"):
                continue
            evidence = _evidence(
                citation.get("excerpt", ""),
                observed,
                source_kind="buyer_conversation" if conversational else "public_source",
                source_url=citation.get("source_url"),
                recorded_by=citation.get("recorded_by", ""),
            )
            _add(
                statements,
                "production_gap",
                "yes",
                evidence,
                summary=(
                    "Specific work is evidenced; "
                    "verify it remains uncovered by the current process."
                ),
            )
            if conversational and item.get("status") == "confirmed":
                _add(
                    statements,
                    "recurring_need",
                    "yes",
                    evidence,
                    summary="The buyer confirmed this work; verify the recurring 90-day need.",
                )
                if item.get("approval_owner"):
                    _add(
                        statements,
                        "decision_authority",
                        "yes",
                        evidence,
                        summary=(
                            f"Work approver: {item['approval_owner']}. "
                            "Confirm purchasing authority."
                        ),
                    )


def _latest_review(packet: dict, now: datetime) -> dict | None:
    valid = []
    for review in packet.get("reviews") or []:
        observed = _date(review.get("observed_at"))
        if (
            review.get("company_key", packet["company_key"]) == packet["company_key"]
            and review.get("label") not in {None, "unknown"}
            and observed
            and observed <= now
            and _available(review, now)
        ):
            valid.append(review)
    return max(
        valid,
        key=lambda r: (
            _date(r.get("recorded_at")) or _date(r.get("observed_at")),
            int(r.get("id") or 0),
        ),
        default=None,
    )


def _resolve(key: str, statements: list[dict], now: datetime) -> dict:
    label, question = CRITERIA[key]
    statements.sort(key=lambda s: _date(s["evidence"]["observed_at"]), reverse=True)
    known = [s for s in statements if s["answer"] != "unknown"]
    fresh = [
        s for s in known if now - _date(s["evidence"]["observed_at"]) <= timedelta(days=FRESH_DAYS)
    ]
    confirmed = [s for s in fresh if s["confirmed_source"]]
    state = "unknown"
    summary = "No current, explicit buyer confirmation is recorded."
    chosen = (confirmed or fresh or known or statements)[:1]
    if confirmed:
        newest = _date(confirmed[0]["evidence"]["observed_at"])
        contemporaneous = [
            s
            for s in confirmed
            if newest - _date(s["evidence"]["observed_at"]) <= timedelta(days=CONFLICT_DAYS)
        ]
        if len({s["answer"] for s in contemporaneous}) > 1:
            chosen = [
                contemporaneous[0],
                next(s for s in contemporaneous if s["answer"] != contemporaneous[0]["answer"]),
            ]
            state = "conflicted"
            summary = (
                "Recent buyer statements disagree; "
                "reconcile them before treating this condition as met."
            )
            question = f"Resolve the conflicting buyer statements about {label.lower()}."
        else:
            state = "confirmed" if confirmed[0]["answer"] == "yes" else "not_met"
            summary = confirmed[0]["summary"]
    elif fresh:
        state = "supported"
        summary = fresh[0]["summary"]
        if key == "monthly_budget":
            summary += " This does not confirm an approved $5,000/month service budget."
        if key == "decision_authority":
            summary += " Purchasing authority requires confirmation from the buyer."
            if "work approver" in summary.casefold():
                question = (
                    "Does the recorded work approver also approve the $5,000/month purchase, "
                    "or who does?"
                )
    elif known:
        state = "stale"
        summary = "Evidence is older than 90 days; reconfirm the current buyer situation."
    result = {
        "key": key,
        "label": label,
        "state": state,
        "summary": summary,
        "question": question,
        "evidence": [s["evidence"] for s in chosen[:8]],
    }
    if chosen:
        result["observed_at"] = chosen[0]["evidence"]["observed_at"]
    return result


def build_buyer_assessment(packet: dict, facts: list[dict], *, now: datetime | None = None) -> dict:
    """Resolve six conditions with dated evidence, preserving unknown and negative facts."""
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("assessment time must include a timezone")
    reference = reference.astimezone(UTC)
    packet = {**packet, "company_key": normalize_company_key(packet["company_key"])}
    statements = {key: [] for key in CRITERIA}
    _fact_statements(packet, facts, reference, statements)
    negative = _account_statements(packet, reference, statements)
    _opportunity_statements(packet, reference, statements)
    review = _latest_review(packet, reference)
    criteria = [_resolve(key, entries, reference) for key, entries in statements.items()]
    fact_ids = list(
        dict.fromkeys(
            evidence["fact_id"]
            for item in criteria
            for evidence in item["evidence"]
            if "fact_id" in evidence
        )
    )
    states = {item["key"]: item["state"] for item in criteria}
    unresolved = [item for item in criteria if item["state"] != "confirmed"]
    readiness = "research"
    first = next((item for item in unresolved if item["state"] == "conflicted"), None)
    first = first or (unresolved[0] if unresolved else None)
    next_action = {
        "action": "research",
        "question": first["question"]
        if first
        else "Agree the next sales step with the confirmed buyer.",
        "research_action": f"confirm_{first['key']}" if first else "agree_next_sales_step",
    }
    if all(state == "confirmed" for state in states.values()):
        readiness = "conversation_ready"
        next_action["action"] = "contact"
    if states["monthly_budget"] == "not_met" or states["timing"] == "not_met":
        readiness = "nurture"
        failed = next(
            item
            for item in criteria
            if item["key"] in {"monthly_budget", "timing"} and item["state"] == "not_met"
        )
        next_action = {
            "action": "nurture",
            "question": failed["summary"],
            "research_action": f"revisit_{failed['key']}",
        }
    if any(
        states[key] == "not_met" for key in ("recurring_need", "production_gap", "ai_acceptability")
    ):
        readiness = "deprioritize"
        failed = next(
            item
            for item in criteria
            if item["key"] in {"recurring_need", "production_gap", "ai_acceptability"}
            and item["state"] == "not_met"
        )
        next_action = {
            "action": "deprioritize",
            "question": failed["summary"],
            "research_action": "reassess_buyer_fit",
        }
    negative_controls = negative and any(
        item["key"] == negative["criterion"]
        and item["state"] == "not_met"
        and item.get("observed_at") == negative["observed_at"].isoformat()
        for item in criteria
    )
    if negative_controls:
        readiness = "deprioritize" if negative["outcome"] == "not_fit" else "nurture"
        next_action = {
            "action": readiness,
            "question": negative["reason"],
            "research_action": "resolve_discovery_outcome",
        }
    if (review and review.get("label") == "no_fit") or (packet.get("role") or {}).get(
        "role"
    ) == "directory_or_awards":
        readiness = "deprioritize"
        next_action = {
            "action": "deprioritize",
            "question": review.get("notes", "Operator marked no fit.")
            if review and review.get("label") == "no_fit"
            else "This entity is a directory or awards publisher; verify a separate buyer company.",
            "research_action": "reassess_buyer_fit",
        }
    return {
        "criteria": criteria,
        "readiness": readiness,
        "next_action": next_action,
        "unknowns": [item["label"] for item in unresolved if item["state"] != "not_met"],
        "controlling_review_ids": [review["id"]] if review and review.get("id") is not None else [],
        "fact_ids": fact_ids,
        "confirmed_count": sum(item["state"] == "confirmed" for item in criteria),
    }
