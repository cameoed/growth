from __future__ import annotations

import json
from collections.abc import Mapping

SCAN_ACTION_PREFIX = "__BEINGS_SCAN_ACTION__"


def encode_scan_action(
    message: str,
    *,
    detail: str = "",
    level: str = "item",
) -> str:
    """Encode one bounded subprocess activity line for the local dashboard parent."""

    payload = {
        "message": " ".join(str(message).split())[:240],
        "detail": " ".join(str(detail).split())[:500],
        "level": str(level or "item")[:24],
    }
    return SCAN_ACTION_PREFIX + json.dumps(payload, separators=(",", ":"))


def decode_scan_action(line: str) -> dict[str, str] | None:
    """Decode a structured activity line without treating ordinary output as telemetry."""

    if not line.startswith(SCAN_ACTION_PREFIX):
        return None
    try:
        raw = json.loads(line.removeprefix(SCAN_ACTION_PREFIX))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(raw, Mapping):
        return None
    message = " ".join(str(raw.get("message") or "").split())[:240]
    if not message:
        return None
    return {
        "message": message,
        "detail": " ".join(str(raw.get("detail") or "").split())[:500],
        "level": " ".join(str(raw.get("level") or "item").split())[:24] or "item",
    }
