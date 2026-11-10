"""Lossless JSON storage with exact, small SQLite query projections.

The envelope remains valid JSON for existing JSON1 indexes/queries. Application rows
decode to the original UTF-8 text, including whitespace and ordering. Projections
are storage indexes, never substitutes for complete evidence hydration.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import zlib

KEY = "$beings_storage"
PREFIX = '{"$beings_storage":["zlib-v1",'
PACKET_TAG = b"BSP1\x00"


def _project_text(text, value, projection):
    """Retain original JSON tokens so JSON1 object results keep their escaping too."""
    if not projection:
        return "{}"
    decoder, pieces = json.JSONDecoder(), []
    position = text.index("{") + 1
    while True:
        while text[position].isspace():
            position += 1
        if text[position] == "}":
            break
        key_start = position
        key, key_end = decoder.raw_decode(text, position)
        position = key_end
        while text[position].isspace():
            position += 1
        if text[position] != ":":
            raise ValueError("Invalid JSON object")
        position += 1
        while text[position].isspace():
            position += 1
        start = position
        _, position = decoder.raw_decode(text, position)
        if key in projection:
            token = text[start:position]
            if projection[key] != value[key]:
                token = _project_text(token, value[key], projection[key])
            pieces.append(text[key_start:key_end] + ":" + token)
        while text[position].isspace():
            position += 1
        if text[position] == ",":
            position += 1
        elif text[position] != "}":
            raise ValueError("Invalid JSON object separator")
    return "{" + ",".join(pieces) + "}"


def _projection(value, profile):
    if not isinstance(value, dict):
        return {}
    if profile == "observation":
        result = {k: v for k, v in value.items() if k not in {
            "pages", "text_excerpt", "signals", "buyer_evidence", "paid_creative_asset_keys",
        }}
        if isinstance(result.get("web"), dict):
            result["web"] = {k: v for k, v in result["web"].items() if k != "frontier"}
        return result
    if profile == "score":
        result = {k: v for k, v in value.items() if k != "company_role"}
        if isinstance(result.get("creative_capacity"), dict):
            result["creative_capacity"] = {
                k: v for k, v in result["creative_capacity"].items()
                if k in {"total", "review_ready"} or not isinstance(v, (dict, list))
            }
        return result
    if profile == "target":
        result = {k: value[k] for k in ("company_role", "next_best_scan") if k in value}
        if isinstance(result.get("company_role"), dict):
            result["company_role"] = {k: v for k, v in result["company_role"].items()
                                      if k == "role"}
        return result
    if profile == "audit":
        return {k: value[k] for k in ("criteria", "purchase_signal_contribution") if k in value}
    if profile == "opaque":
        return {}
    raise ValueError(f"Unknown JSON storage profile: {profile}")


def encode_text(text, profile="opaque"):
    """Preserve original bytes and only pack when the entire envelope is smaller."""
    if text is None or not isinstance(text, str) or len(text) < 256:
        return text
    if text.startswith(PREFIX):
        decode_text(text)  # Never accept a corrupt or recursively wrapped envelope.
        return text
    value = json.loads(text)
    if isinstance(value, dict) and KEY in value:
        return text
    raw = text.encode("utf-8")
    envelope = {KEY: ["zlib-v1", base64.b64encode(zlib.compress(raw, 6)).decode("ascii"),
                      hashlib.sha256(raw).hexdigest(), len(raw)]}
    projection = _projection(value, profile)
    projected = _project_text(text, value, projection)[1:-1]
    packed = json.dumps(envelope, separators=(",", ":"))[:-1]
    packed += ("," + projected if projected else "") + "}"
    return packed if len(packed.encode("utf-8")) < len(raw) else text


def decode_text(value):
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    envelope = json.loads(value)
    version, encoded, digest, size = envelope[KEY]
    if version != "zlib-v1":
        raise ValueError("Unknown JSON storage version")
    raw = zlib.decompress(base64.b64decode(encoded, validate=True))
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("JSON storage integrity mismatch")
    return raw.decode("utf-8")


def encode_packet(text):
    raw = text.encode("utf-8")
    return PACKET_TAG + zlib.compress(raw, 6)


def decode_packet(value):
    if not isinstance(value, bytes) or not value.startswith(PACKET_TAG):
        raise ValueError("Unknown accounting packet storage")
    return zlib.decompress(value[len(PACKET_TAG):]).decode("utf-8")


def storage_json(value):
    return decode_packet(value) if isinstance(value, bytes) else decode_text(value)


def decoded_row(cursor, row):
    # Only whole JSON columns use envelopes. Extracted titles, emails and other
    # ordinary strings must stay literal even if they resemble a storage marker.
    return sqlite3.Row(cursor, tuple(
        decode_text(value) if column[0].lower().endswith("_json") else value
        for column, value in zip(cursor.description, row, strict=True)
    ))


def configure(connection, *, rows=True):
    connection.create_function("storage_json", 1, storage_json, deterministic=True)
    if rows:
        connection.row_factory = decoded_row


def usage_snapshot(connection):
    """Exact database allocation at one read, without scanning company evidence."""
    pages, free, size = connection.execute(
        "SELECT p.page_count,f.freelist_count,s.page_size FROM pragma_page_count p,"
        "pragma_freelist_count f,pragma_page_size s"
    ).fetchone()
    return {"page_bytes": pages * size, "free_page_bytes": free * size,
            "live_page_bytes": (pages - free) * size}


# All writes and offline maintenance share this explicit physical contract.
COLUMNS = {
    "snapshots": {"observation_json": "observation", "score_json": "score"},
    "snapshot_target_intelligence": {"hypothesis_json": "target"},
    "intelligence_feature_snapshots": {
        key: "opaque" for key in ("features_json", "missingness_json", "coverage_json",
                                  "scanner_states_json", "evidence_ages_json",
                                  "observation_keys_json")
    },
    "engine_mission_scores": {"audit_json": "audit"},
    "scanner_schedule_decisions": {"metadata_json": "opaque"},
}
