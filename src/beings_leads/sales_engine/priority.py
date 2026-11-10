"""One transparent sales-priority index from existing intelligence and buyer facts.

The public-evidence score remains useful without any manual qualification. Freshness,
source correlation and company fit are already handled by that score; this layer
does not run collection, decay the score again, or claim a probability of purchase.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from numbers import Real
from urllib.parse import urlsplit

from ..value_utils import aware_datetime as _observed_at

VERSION = "sales-priority-v2-hydrated"
MAX_REASON_LENGTH = 180
CURRENT_REASON_DAYS = 45
BUYER_POINTS = {
    "recurring_need": (3, "Buyer confirmed recurring need"),
    "production_gap": (3, "Buyer confirmed uncovered production work"),
    "monthly_budget": (3, "Buyer confirmed the $5,000 monthly budget"),
    "decision_authority": (1, "Buyer confirmed purchasing authority"),
    "timing": (1, "Buyer confirmed a current buying window"),
    "ai_acceptability": (1, "Buyer confirmed AI acceptability"),
}
NEGATIVE_FIT = {"recurring_need", "production_gap", "ai_acceptability"}
BUYER_HOLD = {"monthly_budget", "timing"}
CONTACT_SUMMARY = (
    "A current first-party business contact is explicitly published and "
    "marked outreach-eligible by the contact safety rules."
)
CONTACT_ROUTE_SUMMARY = "A current first-party contact route is publicly available."
PLAIN_SUMMARIES = {
    CONTACT_SUMMARY: "A published business contact is available.",
    CONTACT_ROUTE_SUMMARY: "The company publishes a contact route.",
    "First-party language connects recurring work with creative delivery.": (
        "The company's website describes recurring creative work."
    ),
    "First-party pages explicitly reference visual creative work.": (
        "The company's website describes visual creative work."
    ),
    (
        "First-party pages expose paid-distribution language, marketing identifiers, "
        "or a paid-growth technology footprint."
    ): "The website shows signs of paid marketing.",
    (
        "First-party language explicitly describes a production shortfall, backlog, "
        "throughput constraint, or use of outside production; scope and duration remain "
        "unverified."
    ): (
        "The company describes production pressure or outside production; "
        "scope and duration remain unverified."
    ),
}
REASON_FAMILIES = {
    "recurring_workload": 5,
    "production_pressure": 5,
    "commercial_timing": 5,
    "distribution_investment": 4,
    "proof_match": 3,
    "ability_proxy": 2,
    "buyer_access": 0,
    "coverage": 0,
}


def _score(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return int(math.floor(max(0.0, min(100.0, number)) + 0.5))


def _base_score(target: dict) -> int | None:
    opportunity = target.get("opportunity") or {}
    for value in (
        target.get("priority_score"),
        target.get("founder_priority_score"),
        opportunity.get("founder_priority") if isinstance(opportunity, dict) else None,
    ):
        score = _score(value)
        if score is not None:
            return score
    return None


def _sentence(value: object) -> str:
    if not isinstance(value, str):
        return ""
    clean = re.sub(r"\s+", " ", value).strip()
    if not clean:
        return ""
    clean = re.split(r"(?<=[.!?])\s+", clean, maxsplit=1)[0]
    if clean[-1] not in ".!?…":
        clean += "."
    if len(clean) > MAX_REASON_LENGTH:
        clean = clean[: MAX_REASON_LENGTH - 1].rsplit(" ", 1)[0].rstrip(" ,;:.") + "…"
    return clean


def _public_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        return bool(
            parts.scheme in {"http", "https"}
            and parts.hostname
            and not parts.username
            and not parts.password
        )
    except ValueError:
        return False


def _plain_summary(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = re.sub(r"\s+", " ", value).strip()
    if normalized in PLAIN_SUMMARIES:
        return PLAIN_SUMMARIES[normalized]
    themes = re.fullmatch(
        r"The bounded public first-party sample exposes (\d+) creative concept family/families; "
        r"this suggests production complexity, not spend, performance, fatigue, "
        r"or a staffing gap\.",
        normalized,
    )
    if themes:
        return f"Sampled work contains {themes[1]} apparent creative themes."
    hiring = re.fullmatch(
        r"Public careers data includes (\d+) creative, photo, or video role\(s\); "
        r"\d+ structured posting\(s\) add workflow or capacity context, "
        r"but hiring pressure does not prove willingness to outsource\.",
        normalized,
    )
    if hiring:
        return (
            f"The company lists {hiring[1]} creative, photo or video jobs; "
            "outsourcing interest is unconfirmed."
        )
    return normalized


def _reason_rank(statement: str, event: dict, default: int) -> int:
    if statement in {CONTACT_SUMMARY, CONTACT_ROUTE_SUMMARY}:
        return 0
    return REASON_FAMILIES.get(event.get("family"), default)


def _stored_reason(target: dict, base: int | None, now: datetime) -> str:
    packet = target.get("decision_packet") or {}
    events = {
        event["event_key"]: event
        for event in target.get("signal_events") or []
        if isinstance(event, dict) and event.get("event_key")
    }
    candidates = []

    def add_candidate(
        statement: object,
        source_url: object,
        observed: object,
        event: dict,
        *,
        confidence: object = None,
        default_rank: int = 2,
    ) -> None:
        stamp = _observed_at(observed)
        if not _public_url(source_url) or stamp is None or stamp > now:
            return
        if not isinstance(statement, str) or not statement.strip():
            return
        normalized = re.sub(r"\s+", " ", statement).strip()
        strength = _score(event.get("current_strength")) or 0
        candidates.append(
            (
                _reason_rank(normalized, event, default_rank),
                now - stamp <= timedelta(days=CURRENT_REASON_DAYS),
                {"high": 2, "medium": 1}.get(confidence or event.get("confidence"), 0),
                strength,
                stamp,
                normalized,
            )
        )

    if isinstance(packet, dict):
        observed = _observed_at(packet.get("why_now_observed_at"))
        if (
            _public_url(packet.get("why_now_source_url"))
            and observed
            and timedelta() <= now - observed <= timedelta(days=CURRENT_REASON_DAYS)
        ):
            add_candidate(
                packet.get("why_now"),
                packet.get("why_now_source_url"),
                observed,
                events.get(packet.get("why_now_event_key"), {}),
                default_rank=5,
            )
        points = packet.get("proof_points") or []
        for point in points:
            if not isinstance(point, dict):
                continue
            add_candidate(
                point.get("statement"),
                point.get("source_url"),
                point.get("observed_at"),
                events.get(point.get("event_key"), {}),
                confidence=point.get("confidence"),
            )
    # A strong contact event can fill the capped proof list before a more useful
    # demand event. Reuse the already-stored observed signal, never infer a new fact.
    for event in events.values():
        expires = _observed_at(event.get("expires_at"))
        strength = _score(event.get("current_strength"))
        if (
            event.get("epistemic_status") != "observed"
            or event.get("polarity") != "positive"
            or strength is None
            or strength <= 0
            or (expires and expires <= now)
        ):
            continue
        add_candidate(
            event.get("summary"),
            event.get("source_url"),
            event.get("observed_at"),
            event,
        )
    if candidates:
        selected = max(candidates, key=lambda item: item[:5])
        observed, statement = selected[-2:]
        reason = _sentence(_plain_summary(statement))
        if now - observed > timedelta(days=CURRENT_REASON_DAYS):
            reason = _sentence(f"Observed {observed.date().isoformat()}: {reason}")
        return reason
    if base is None:
        return "No stored company-priority score is available yet."
    if base >= 70:
        return "Saved company evidence puts this lead near the front of the research queue."
    if base >= 50:
        return "Saved company evidence supports reviewing this lead for recurring creative work."
    return "Saved company evidence gives this lead lower priority for current outreach."


def _criteria(assessment: dict) -> dict:
    # The resolver already handles dates and contradictory facts. Duplicate or
    # detail-only supported entries must never award points twice.
    result = {}
    for item in assessment.get("criteria") or []:
        if not isinstance(item, dict) or item.get("key") not in BUYER_POINTS:
            continue
        key = item["key"]
        previous = result.get(key)
        if previous is None:
            result[key] = item
        elif previous.get("state") != item.get("state"):
            result[key] = {"key": key, "state": "conflicted"}
    return result


def _confirmed(item: dict) -> bool:
    return item.get("state") == "confirmed" and any(
        isinstance(evidence, dict) and evidence.get("source_kind") == "buyer_conversation"
        for evidence in item.get("evidence") or []
    )


def build_sales_priority(
    target: dict,
    assessment: dict,
    *,
    now: datetime | None = None,
) -> dict:
    """Keep the cold-lead baseline and apply only bounded buyer-evidence changes.

    Both queue and packet use the same target projection and assessment. Suggested
    opportunity counts, contact counts, unknown gates and display-only details are
    intentionally absent from this index.
    """
    target = target if isinstance(target, dict) else {}
    assessment = assessment if isinstance(assessment, dict) else {}
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("priority assessment time must include a timezone")
    reference = reference.astimezone(UTC)
    base = _base_score(target)
    criteria = _criteria(assessment)
    factors = [{"key": "stored_evidence", "label": "Stored company intelligence", "value": base}]
    limits = [
        "This is a sales-priority index, not a probability of purchase.",
        "Missing manual confirmations do not reduce the stored company score.",
        "Public-evidence freshness and overlap are already handled by the underlying score.",
    ]
    score = base
    confirmed = []
    if base is not None:
        for key, (points, label) in BUYER_POINTS.items():
            if _confirmed(criteria.get(key, {})):
                confirmed.append(key)
                factors.append({"key": f"buyer_{key}", "label": label, "value": points})
                score += points
        if score > 100:
            factors.append(
                {"key": "score_ceiling", "label": "100-point ceiling", "value": 100 - score}
            )
            score = 100
    else:
        limits.append("Buyer records alone do not invent a missing company-intelligence score.")
    reason = _stored_reason(target, base, reference)
    label = (
        "Unscored"
        if score is None
        else (
            "High priority"
            if score >= 70
            else "Worth reviewing"
            if score >= 50
            else "Lower priority"
        )
    )
    if confirmed:
        if "monthly_budget" in confirmed:
            reason = "The buyer confirmed a budget for the $5,000/month offer."
        elif "recurring_need" in confirmed and "production_gap" in confirmed:
            reason = "The buyer confirmed recurring work that the current process cannot cover."
        elif "recurring_need" in confirmed:
            reason = "The buyer confirmed recurring demand, strengthening this lead's priority."
        elif "production_gap" in confirmed:
            reason = "The buyer confirmed production work that the current process does not cover."
        else:
            # One point of approval/timing context should not hide the specific
            # public trigger that makes a useful outreach opening.
            reason = _stored_reason(target, base, reference)
    readiness = assessment.get("readiness")
    not_met = {key for key, item in criteria.items() if item.get("state") == "not_met"}
    conflicted = any(item.get("state") == "conflicted" for item in criteria.values())
    action = assessment.get("next_action") or {}
    actual_reason = _sentence(action.get("question")) if isinstance(action, dict) else ""
    role = target.get("company_role") or {}
    outside_scope = (
        target.get("disqualified") is True
        or target.get("target_scope") == "out_of_scope"
        or (isinstance(role, dict) and role.get("role") == "directory_or_awards")
    )
    cap = None
    cap_key = ""
    cap_label = ""
    if readiness == "deprioritize" or not_met & NEGATIVE_FIT or outside_scope:
        cap, cap_key, cap_label = 0, "fit_rejection", "Fit rejection sets current priority to zero"
        label = "Not a fit"
        reason = (
            actual_reason
            if readiness == "deprioritize" and actual_reason
            else (
                "Current buyer evidence rules out the recurring offer."
                if not_met & NEGATIVE_FIT
                else "The stored company assessment places this entity outside the buyer lane."
            )
        )
    elif readiness == "nurture" or not_met & BUYER_HOLD:
        cap, cap_key, cap_label = (
            20,
            "buyer_hold",
            "Current budget or timing hold caps priority at 20",
        )
        label = "Revisit later"
        reason = (
            actual_reason
            if readiness == "nurture" and actual_reason
            else ("The buyer has no current budget or buying window for this offer.")
        )
    elif conflicted:
        cap, cap_key, cap_label = (
            40,
            "buyer_conflict",
            "Conflicting buyer evidence caps priority at 40",
        )
        label = "Resolve conflict"
        reason = (
            "Recent buyer statements conflict, "
            "so this lead needs clarification before further pursuit."
        )
    if cap is not None:
        if score is None:
            # An explicit rejection can establish zero current priority even when
            # no public-evidence score exists; it cannot establish positive priority.
            score = 0 if cap != 40 else None
            factors.append({"key": cap_key, "label": cap_label, "value": 0 if score == 0 else None})
        else:
            adjusted = min(score, cap)
            factors.append({"key": cap_key, "label": cap_label, "value": adjusted - score})
            score = adjusted
    if confirmed:
        limits.append(
            "Current buyer confirmations add at most 12 points under the documented policy."
        )
    return {
        "score": score,
        "label": label,
        "reason": _sentence(reason),
        "factors": factors,
        "limits": limits,
        "version": VERSION,
    }
