"""Small shared value conversions with explicit, unchanged serialization policies."""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime


def aware_datetime(value: object) -> datetime | None:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    if result.tzinfo is None or result.utcoffset() is None:
        return None
    return result.astimezone(UTC)


def canonical_json_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def mapping_or_empty(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def naive_as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def timezone_datetime(value: object) -> datetime | None:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return result.astimezone(UTC) if result.tzinfo else None
    except (ValueError, TypeError):
        return None
