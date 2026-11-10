"""Small deterministic recovery policy for optional public-scanner fetches."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite

import httpx


def scanner_retry_delay(error: Exception) -> float | None:
    if getattr(error, "company_research_deferred", False):
        return None
    # These holds/policy refusals never receive an immediate retry.
    if getattr(error, "terminal_status", "") in {
        "failed",
        "robots_blocked",
        "rate_limited",
        "identity_ambiguous",
    }:
        return None
    if (
        "certificate verify failed" in str(error).lower()
        or "certificate_verify_failed" in str(error).lower()
    ):
        return None
    response = error.response if isinstance(error, httpx.HTTPStatusError) else None
    if response is not None:
        if response.status_code not in {408, 425, 500, 502, 503, 504, 520, 521, 522, 523, 524}:
            return None
    elif not isinstance(
        error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, TimeoutError)
    ):
        return None
    delay = 0.25
    if response is not None and (value := response.headers.get("retry-after", "").strip()):
        try:
            hold = float(value)
        except ValueError:
            try:
                until = parsedate_to_datetime(value)
                if until.tzinfo is None:
                    until = until.replace(tzinfo=UTC)
                hold = (until - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return None
        if not isfinite(hold) or hold > 5:
            return None
        delay = max(delay, hold)
    return delay


def scanner_host_hold_seconds(error: Exception, *, minimum: float) -> float:
    """A proven 429 holds sibling requests too; never shorten explicit Retry-After."""
    if not isinstance(error, httpx.HTTPStatusError) or error.response.status_code != 429:
        return 0.0
    value = error.response.headers.get('retry-after', '').strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            until = parsedate_to_datetime(value)
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
            seconds = (until - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            seconds = minimum
    return max(minimum, seconds) if isfinite(seconds) else minimum
