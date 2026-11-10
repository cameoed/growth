"""Time-safe descriptive learning from real research, discovery and delivery outcomes.

Unknowns and silence are not failures. No current mutable account row is allowed to
leak an update after the reporting cutoff into an older decision or experiment.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .models import LearningMetric, normalize_company_key, utc_now

MIN_OUTCOME_SAMPLE = 20
MIN_RESEARCH_SAMPLE = 5


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def _known(value: Any, cutoff: datetime) -> bool:
    parsed = _time(value)
    return parsed is not None and parsed <= cutoff


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _micro(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        decimal = Decimal(str(value))
        if not decimal.is_finite() or decimal < 0:
            return None
        return int((decimal * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))
    except (ValueError, ArithmeticError):
        return None


def descriptive_metric(
    numerator: int,
    denominator: int,
    minimum_sample: int = MIN_OUTCOME_SAMPLE,
) -> dict[str, Any]:
    if not 0 <= numerator <= denominator:
        raise ValueError("metric numerator must be between zero and denominator")
    enough = denominator >= minimum_sample
    rate = numerator / denominator if enough else None
    low = high = None
    if rate is not None:
        z = 1.96
        divisor = 1 + z * z / denominator
        center = (rate + z * z / (2 * denominator)) / divisor
        radius = (
            z * math.sqrt((rate * (1 - rate) + z * z / (4 * denominator)) / denominator) / divisor
        )
        low, high = max(0, center - radius), min(1, center + radius)
    return LearningMetric(
        numerator=numerator,
        denominator=denominator,
        minimum_sample=minimum_sample,
        observed_rate=rate,
        wilson_low=low,
        wilson_high=high,
        status="descriptive_only" if enough else "insufficient_data",
    ).model_dump(mode="json")


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _account_states(
    connection: sqlite3.Connection,
    cutoff: datetime,
    tables: set[str],
) -> tuple[list[dict[str, Any]], int]:
    states: dict[int, tuple[datetime, dict[str, Any]]] = {}
    excluded = 0
    if "sales_accounts" in tables:
        for row in connection.execute("SELECT * FROM sales_accounts").fetchall():
            account = dict(row)
            stamp = _time(account.get("updated_at"))
            if stamp is not None and stamp <= cutoff:
                states[account["id"]] = (stamp, account)
            else:
                excluded += 1
    if "engine_account_states" in tables:
        for row in connection.execute(
            "SELECT * FROM engine_account_states ORDER BY recorded_at,id"
        ).fetchall():
            stamp = _time(row["recorded_at"])
            if stamp is None or stamp > cutoff:
                continue
            account_id = row["account_id"]
            if account_id not in states or states[account_id][0] <= stamp:
                states[account_id] = (stamp, _object(row["payload_json"]))
    result: list[dict[str, Any]] = []
    for _, account in states.values():
        try:
            account["company_key"] = normalize_company_key(
                account.get("website_host") or account.get("website", "")
            )
        except ValueError:
            account["company_key"] = ""
        result.append(account)
    return result, excluded


def _facts(account: dict[str, Any], cutoff: datetime) -> dict[str, Any]:
    discovery = _object(account.get("discovery_json"))
    sales = _object(account.get("outcome_json"))
    delivery = _object(account.get("delivery_json"))
    discovery_known = discovery.get("completed") is True and _known(
        discovery.get("held_at"), cutoff
    )
    discovery_label = discovery.get("outcome", "unknown") if discovery_known else "unknown"
    won_at = _time(sales.get("won_at"))
    lost_at = _time(sales.get("lost_at"))
    proposal = sales.get("proposal_outcome")
    won = proposal == "won" and won_at is not None and won_at <= cutoff
    lost = proposal == "lost" and lost_at is not None and lost_at <= cutoff
    start = _time(sales.get("month_one_started_at"))
    paid_start = bool(
        won and start is not None and start <= cutoff and start.date() >= won_at.date()
    )
    renewal_at = _time(delivery.get("renewal_decided_at"))
    renewal = delivery.get("renewal_decision")
    renewal_known = bool(
        paid_start
        and renewal_at is not None
        and start <= renewal_at <= cutoff
        and renewal in {"renewed", "not_renewed"}
    )
    revenue = _micro(delivery.get("recognized_revenue")) if paid_start else None
    cost = _micro(delivery.get("direct_delivery_cost")) if paid_start else None
    margin = revenue - cost if revenue is not None and cost is not None else None
    recurring = discovery.get("confirmed_recurring_need") if discovery_known else None
    if type(recurring) is not bool:
        recurring = None
    minutes = discovery.get("research_minutes") if discovery_known else None
    minutes = float(minutes) if isinstance(minutes, (int, float)) and math.isfinite(minutes) else 0
    return {
        "account_id": account.get("id"),
        "candidate_id": account.get("candidate_id"),
        "company_key": account["company_key"],
        "segment": account.get("segment", "unknown"),
        "cohort": account.get("cohort", "unknown"),
        "source": account.get("lead_source", "unknown"),
        "discovery_label": discovery_label,
        "discovery_at": _time(discovery.get("held_at")),
        "discovery_known": discovery_label in {"qualified", "not_fit", "no_budget", "not_now"},
        "recurring_need": recurring,
        "won": won,
        "lost": lost,
        "proposal_at": won_at if won else lost_at if lost else None,
        "paid_start": paid_start,
        "paid_start_at": start if paid_start else None,
        "renewal_known": renewal_known,
        "renewed": renewal_known and renewal == "renewed",
        "renewal_at": renewal_at if renewal_known else None,
        "revenue_microusd": revenue,
        "delivery_cost_microusd": cost,
        "contribution_microusd": margin,
        "discovery_minutes": max(0, minutes),
        "assets_delivered": delivery.get("assets_delivered", 0) if paid_start else 0,
        "assets_used": delivery.get("assets_used", 0) if paid_start else 0,
        "revision_hours": delivery.get("revision_hours", 0) if paid_start else 0,
        "month_two_queue_depth": delivery.get("month_two_queue_depth", 0) if paid_start else 0,
    }


def _metrics(facts: list[dict[str, Any]]) -> dict[str, Any]:
    discoveries = [item for item in facts if item["discovery_known"]]
    qualified = [item for item in discoveries if item["discovery_label"] == "qualified"]
    recurring = [item for item in facts if item["recurring_need"] is not None]
    closed = [item for item in facts if item["won"] or item["lost"]]
    renewal = [item for item in facts if item["renewal_known"]]
    financial = [item for item in facts if item["contribution_microusd"] is not None]
    delivered = sum(item["assets_delivered"] or 0 for item in facts)
    used = sum(item["assets_used"] or 0 for item in facts)
    discovery_minutes = sum(item["discovery_minutes"] for item in facts)
    return {
        "accounts": len(facts),
        "qualified_discovery": descriptive_metric(len(qualified), len(discoveries)),
        "confirmed_recurring_need": descriptive_metric(
            sum(item["recurring_need"] is True for item in recurring), len(recurring)
        ),
        "win_rate_among_closed_proposals": descriptive_metric(
            sum(item["won"] for item in closed), len(closed)
        ),
        "paid_starts": sum(item["paid_start"] for item in facts),
        "renewal_rate_among_decided": descriptive_metric(
            sum(item["renewed"] for item in renewal), len(renewal)
        ),
        "unknown_discovery_accounts": len(facts) - len(discoveries),
        "open_or_undated_proposal_accounts": len(facts) - len(closed),
        "financially_observed_accounts": len(financial),
        "recognized_revenue_microusd": sum(item["revenue_microusd"] for item in financial),
        "direct_delivery_cost_microusd": sum(item["delivery_cost_microusd"] for item in financial),
        "contribution_microusd": sum(item["contribution_microusd"] for item in financial),
        "mean_contribution_microusd": (
            round(sum(item["contribution_microusd"] for item in financial) / len(financial))
            if financial
            else None
        ),
        "assets_delivered": delivered,
        "assets_used": used,
        "observed_asset_use_ratio": used / delivered if delivered else None,
        "revision_hours": sum(item["revision_hours"] or 0 for item in facts),
        "month_two_queue_depth": sum(item["month_two_queue_depth"] or 0 for item in facts),
        "discovery_minutes": discovery_minutes,
        "qualified_discoveries_per_recorded_hour": (
            len(qualified) / (discovery_minutes / 60)
            if discovery_minutes and len(discoveries) >= MIN_RESEARCH_SAMPLE
            else None
        ),
    }


def _research_summary(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    useful = [item for item in reviews if type(item.get("useful")) is bool]
    changed = [item for item in reviews if type(item.get("decision_changed")) is bool]
    total_minutes = sum(item.get("actual_minutes", 0) for item in reviews)
    return {
        "reviews": len(reviews),
        "actual_minutes": total_minutes,
        "average_minutes": total_minutes / len(reviews) if reviews else None,
        "unknown_labels": sum(item.get("label", "unknown") == "unknown" for item in reviews),
        "useful_evidence": descriptive_metric(
            sum(item["useful"] for item in useful), len(useful), MIN_RESEARCH_SAMPLE
        ),
        "decision_changed": descriptive_metric(
            sum(item["decision_changed"] for item in changed), len(changed), MIN_RESEARCH_SAMPLE
        ),
        "no_fit_labels": sum(item.get("label") == "no_fit" for item in reviews),
    }


def _action_outcomes(
    reviews: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Observed results after research; one company once, without claiming causal credit."""
    first_review: dict[str, datetime] = {}
    for review in reviews:
        stamp = _time(review["recorded_at"])
        company = review["company_key"]
        if stamp is not None and (company not in first_review or stamp < first_review[company]):
            first_review[company] = stamp
    linked: list[dict[str, Any]] = []
    for fact in facts:
        if fact["company_key"] not in first_review:
            continue
        first = first_review[fact["company_key"]]
        item = dict(fact)
        if item["discovery_at"] is None or item["discovery_at"] < first:
            item.update(
                discovery_known=False,
                recurring_need=None,
                discovery_label="unknown",
                discovery_minutes=0,
            )
        if item["proposal_at"] is None or item["proposal_at"] < first:
            item.update(
                won=False,
                lost=False,
                paid_start=False,
                renewal_known=False,
                renewed=False,
                revenue_microusd=None,
                delivery_cost_microusd=None,
                contribution_microusd=None,
                assets_delivered=0,
                assets_used=0,
                revision_hours=0,
                month_two_queue_depth=0,
            )
        linked.append(item)
    metrics = _metrics(linked)
    minutes = sum(item["actual_minutes"] for item in reviews)
    enough = metrics["qualified_discovery"]["denominator"] >= MIN_RESEARCH_SAMPLE
    metrics["qualified_discoveries_per_research_hour"] = (
        metrics["qualified_discovery"]["numerator"] / (minutes / 60) if enough and minutes else None
    )
    metrics["contribution_per_research_hour_microusd"] = (
        round(metrics["contribution_microusd"] / (minutes / 60))
        if minutes and metrics["financially_observed_accounts"] >= MIN_RESEARCH_SAMPLE
        else None
    )
    return metrics


def build_learning_report(
    connection: sqlite3.Connection,
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    cutoff = as_of or utc_now()
    if cutoff.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    cutoff = cutoff.astimezone(UTC)
    if cutoff > utc_now():
        raise ValueError("learning cannot use a future cutoff")
    tables = _tables(connection)
    accounts, excluded = _account_states(connection, cutoff, tables)
    facts = [
        _facts(account, cutoff)
        for account in accounts
        if account.get("relationship_type", "buyer") == "buyer"
    ]
    reviews = []
    if "engine_research_reviews" in tables:
        for row in connection.execute("SELECT * FROM engine_research_reviews").fetchall():
            if _known(row["recorded_at"], cutoff) and _known(row["observed_at"], cutoff):
                reviews.append({
                    **_object(row["payload_json"]), "recorded_at": row["recorded_at"],
                    "id": row["id"],
                })
    actions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for review in reviews:
        actions[review["research_action"]].append(review)
    segmented: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sources: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        segmented[fact["segment"]].append(fact)
        sources[fact["source"]].append(fact)
    exposure_by_candidate: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if "intelligence_exposures" in tables:
        for row in connection.execute("SELECT * FROM intelligence_exposures").fetchall():
            if _known(row["occurred_at"], cutoff) and _known(row["created_at"], cutoff):
                exposure_by_candidate[row["candidate_id"]].append(dict(row))
    exposed = [item for item in facts if exposure_by_candidate.get(item["candidate_id"])]
    closed_after_exposure = 0
    for item in exposed:
        earliest = min(
            _time(row["occurred_at"]) for row in exposure_by_candidate[item["candidate_id"]]
        )
        if item["proposal_at"] is not None and item["proposal_at"] >= earliest:
            closed_after_exposure += 1
    decisions: list[dict[str, Any]] = []
    if "engine_decisions" in tables:
        for row in connection.execute("SELECT * FROM engine_decisions").fetchall():
            if _known(row["as_of"], cutoff) and _known(row["recorded_at"], cutoff):
                decisions.append(
                    {
                        **_object(row["payload_json"]),
                        "recorded_at": row["recorded_at"],
                        "experiment_arm": row["experiment_arm"],
                    }
                )
    experiments: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    experiment_assignments = 0
    if "engine_experiments" in tables:
        for row in connection.execute("SELECT * FROM engine_experiments").fetchall():
            if not _known(row["assigned_at"], cutoff):
                continue
            experiment_assignments += 1
            group = experiments[(row["experiment_key"], row["arm"])]
            assigned = _time(row["assigned_at"])
            for fact in facts:
                if fact["company_key"] != row["company_key"]:
                    continue
                eligible_decisions = [
                    item
                    for item in decisions
                    if item["company_key"] == fact["company_key"]
                    and item.get("experiment_key") == row["experiment_key"]
                    and assigned <= _time(item["as_of"])
                ]
                eligible_exposures = [
                    item
                    for item in exposure_by_candidate[fact["candidate_id"]]
                    if any(
                        _time(decision["recorded_at"]) <= _time(item["occurred_at"])
                        for decision in eligible_decisions
                    )
                ]
                if not eligible_exposures:
                    continue
                exposed_at = min(_time(item["occurred_at"]) for item in eligible_exposures)
                # Trials with pre-existing outcomes are not used as prospective evidence.
                if fact["proposal_at"] is not None and fact["proposal_at"] < exposed_at:
                    continue
                prospective = dict(fact)
                if fact["discovery_at"] is not None and fact["discovery_at"] < exposed_at:
                    prospective.update(
                        discovery_known=False,
                        recurring_need=None,
                        discovery_label="unknown",
                        discovery_minutes=0,
                    )
                group.append(prospective)
    latest_labels: dict[str, str] = {}
    for review in sorted(reviews, key=lambda item: (item["recorded_at"], item["id"])):
        key = review["company_key"]
        label = review.get("label", "unknown")
        # An unlabelled research note adds effort/history without silently reopening
        # a rejected company. A later explicit reassessment can change its fit state.
        if label != "unknown" or key not in latest_labels:
            latest_labels[key] = label
    return {
        "version": "sales-engine-learning-v1",
        "as_of": cutoff.isoformat(),
        "calibration_status": "descriptive_only_no_predictive_model",
        "predictive_probabilities_available": False,
        "minimum_outcome_sample": MIN_OUTCOME_SAMPLE,
        "minimum_research_sample": MIN_RESEARCH_SAMPLE,
        "outcomes": _metrics(facts),
        "by_segment": [
            {"segment": key, **_metrics(value)} for key, value in sorted(segmented.items())
        ],
        "by_source": [{"source": key, **_metrics(value)} for key, value in sorted(sources.items())],
        "research": {
            **_research_summary(reviews),
            "unique_companies": len(latest_labels),
            "current_no_fit_companies": sum(value == "no_fit" for value in latest_labels.values()),
        },
        "research_actions": [
            {
                "research_action": key,
                **_research_summary(value),
                "associated_later_outcomes": _action_outcomes(value, facts),
            }
            for key, value in sorted(actions.items())
        ],
        "exposures": {
            "recorded": sum(map(len, exposure_by_candidate.values())),
            "matched_accounts": len(exposed),
            "closed_proposals_after_exposure": closed_after_exposure,
        },
        "experiments": [
            {"experiment_key": key[0], "arm": key[1], **_metrics(value)}
            for key, value in sorted(experiments.items())
        ],
        "experiment_assignments": experiment_assignments,
        "recorded_decisions": len(decisions),
        "mutable_accounts_excluded_after_cutoff": excluded,
        "limitations": [
            "Uncontacted, unanswered, open and undated outcomes remain unknown.",
            "Rates require 20 explicit outcomes; research yields require 5 explicit review labels.",
            "Cohort intervals are descriptive; selection and incomplete follow-up limit inference.",
            "Experiments require assignment and recorded decision before an actual exposure.",
            "Account changes after the cutoff are excluded; saved historical states remain usable.",
            "Review minutes and discovery minutes are separate to avoid double-counting.",
            "Results associated with research actions do not establish causal attribution.",
            "Contribution is recorded revenue minus direct cost, excluding unrecorded expenses.",
        ],
    }
