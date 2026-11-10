"""Persistent, nonblocking rate-limit gates using existing indexed control receipts."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

GDELT_HOST = "api.gdeltproject.org"
COOLDOWN_KIND = "provider_rate_limit"


def retry_after_timestamp(headers, *, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    requested = now + timedelta(minutes=15)
    value = str(headers.get("retry-after", "")).strip()
    if value:
        try:
            requested = (
                now + timedelta(seconds=int(value))
                if value.isdigit() else parsedate_to_datetime(value).astimezone(UTC)
            )
            requested = max(now + timedelta(minutes=15), requested)
        except (ValueError, TypeError, OverflowError):
            pass
    return requested.isoformat(timespec="microseconds")


def active_provider_cooldown(cache_get, host: str) -> str | None:
    receipt = cache_get(host, COOLDOWN_KIND)
    if not receipt:
        return None
    try:
        until = datetime.fromisoformat(str(receipt.get("retry_after") or ""))
        if until.tzinfo is None:
            return None
        return until.isoformat() if until > datetime.now(UTC) else None
    except (ValueError, TypeError):
        return None


def record_provider_cooldown(cache_set, host: str, retry_after: str) -> None:
    cache_set(host, COOLDOWN_KIND, {
        "error": "Provider returned HTTP 429", "retry_after": retry_after,
    }, 1)
