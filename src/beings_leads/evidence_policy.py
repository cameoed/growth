"""Local freshness policy for occasional scans; never changes original evidence."""

from datetime import UTC, datetime, timedelta

VERSION = "occasional-scan-v1"
DURABLE_GRACE_DAYS = 90
DURABLE_HALF_LIFE_DAYS = 365
DURABLE_HORIZON_DAYS = 730
DURABLE_FAMILIES = frozenset(
    {"recurring_workload", "ability_proxy", "proof_match", "distribution_investment"}
)


def durable_weight(observed_at, now):
    if not observed_at or observed_at.tzinfo is None:
        return 0.0
    age = (now - observed_at.astimezone(UTC)).total_seconds() / 86400
    if age < 0 or age >= DURABLE_HORIZON_DAYS:
        return 0.0
    return 0.5 ** (max(0.0, age - DURABLE_GRACE_DAYS) / DURABLE_HALF_LIFE_DAYS)


def event_weight(event, effective_at, now):
    if event.details.get("freshness_policy") == VERSION:
        anchor = min(event.observed_at, effective_at) if event.observed_at else effective_at
        return durable_weight(anchor, now)
    age = max(0.0, (now - effective_at).total_seconds() / 86400)
    return 0.5 ** (age / event.half_life_days)


def historical_operating_context(event, now):
    """Expired parser freshness is usable historical context, never a current trigger.

    Only durable frontier operating observations qualify. Current openings, purchases,
    contacts, withdrawals and explicit invalidity have separate contracts and never enter here.
    """
    if (not event.source_kind.startswith("frontier:") and event.source_kind != "ad_library") or (
        event.family not in DURABLE_FAMILIES
    ):
        return event
    if event.details.get("predicate", "").startswith("hiring."):
        return event
    if not event.observed_at:
        return event
    anchor = min(event.observed_at, event.effective_at or event.observed_at)
    if durable_weight(anchor, now) <= 0:
        return event
    boundary = event.expires_at
    if event.source_kind == "ad_library":
        boundary = min(boundary or now, event.observed_at + timedelta(days=90))
    if not boundary or boundary > now:
        return event.model_copy(
            update={
                "details": {**event.details, "freshness_policy": VERSION},
                "refresh_at": boundary,
            }
        )
    return event.model_copy(
        update={
            "event_type": event.event_type + "_historical_context",
            "strength": round(event.strength * 0.85),
            # Preserve confidence in the dated historical observation. Continuation is
            # explicitly unknown and already receives the separate 15% strength discount.
            "confidence": event.confidence,
            "epistemic_status": "inferred",
            "expires_at": anchor + timedelta(days=DURABLE_HORIZON_DAYS),
            "refresh_at": None,
            "summary": "Historical operating evidence; current continuation is unverified.",
            "details": {
                **event.details,
                "freshness_policy": VERSION,
                "original_expires_at": boundary.isoformat(),
                "current_state_known": "false",
            },
        }
    )


def next_dependency_boundary(event, now, threshold=30):
    """Time at which a supporting demand event stops meeting its dependency gate."""
    from .revenue_intelligence import decay_event

    if decay_event(event, now=now).current_strength < threshold:
        return now
    end = min(event.expires_at or now + timedelta(days=3650), now + timedelta(days=3650))
    if decay_event(event, now=end).current_strength >= threshold:
        return end
    low, high = now, end
    for _ in range(40):
        middle = low + (high - low) / 2
        if decay_event(event, now=middle).current_strength >= threshold:
            low = middle
        else:
            high = middle
    return high


def utc_reference(now=None):
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("evidence reference time requires a timezone")
    return now.astimezone(UTC)
