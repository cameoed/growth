"""Receipt-scoped joins; logical keys alone cannot identify a scanner execution."""

from datetime import UTC, datetime, timedelta


def receipt_time(item):
    for field in ("last_observed_at", "first_observed_at", "observed_at", "fetched_at",
                  "finished_at", "started_at"):
        value = getattr(item, field, None)
        if isinstance(value, datetime) and value.tzinfo:
            return value.astimezone(UTC)
    return None


class ReceiptIndex:
    def __init__(self, items, key_field):
        self.items = {}
        for item in items:
            self.items.setdefault(getattr(item, key_field, ""), []).append(item)

    def get(self, owner, key):
        matches = [item for item in self.items.get(key, ())
                   if item.scanner_id == owner.scanner_id
                   and item.scanner_version == owner.scanner_version]
        execution_id = getattr(owner, "execution_id", None)
        if execution_id is not None:
            matches = [item for item in matches if item.execution_id == execution_id]
        else:
            # Legacy inline inputs have no ledger ID. A unique unbound key remains compatible;
            # repeated keys must match the receipt timestamp, never list position or latest scan.
            matches = [item for item in matches if item.execution_id is None]
            if len(matches) > 1:
                stamp = receipt_time(owner)
                matches = [item for item in matches if stamp and receipt_time(item) == stamp]
        if not matches:
            return None
        first = matches[0]
        # Exact duplicate renderings are harmless; conflicting same-time receipts are unknown.
        return first if all(item == first for item in matches[1:]) else None


def hiring_boundaries(observation, now):
    """Include below-event-threshold counts in time-only ranking/cache invalidation."""
    result = []
    for atomic in observation.web.frontier.observations:
        if atomic.scanner_id != "creative_hiring_demand_v1" or atomic.predicate not in {
            "hiring.workforce_demand_current", "hiring.creative_demand_current"
        }:
            continue
        stamp = receipt_time(atomic)
        if stamp:
            boundary = min(stamp + timedelta(days=14),
                           atomic.expires_at or stamp + timedelta(days=14))
            result.extend(value for value in (stamp, boundary) if value > now)
    return result
