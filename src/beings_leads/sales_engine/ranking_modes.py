"""Read-only ranking lenses over resolved evidence; the Balanced policy is untouched."""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Literal

from .priority import _base_score, build_sales_priority

RankingMode = Literal["balanced", "creative_demand", "buying_timing", "reachable_buyers"]
VERSION = "ranking-lenses-v1"
MODES = ("balanced", "creative_demand", "buying_timing", "reachable_buyers")
COLUMNS = dict(zip(MODES[1:], ("demand_score", "timing_score", "access_score"), strict=True))
# Zero-sum weight contrasts emphasize a dimension relative to other saved dimensions.
# Values already incorporate provenance, decay, independent evidence and uncertainty.
WEIGHTS = {
    "creative_demand": {
        "need": 0.22,
        "fit": 0.08,
        "proof": 0.06,
        "timing": -0.20,
        "access": -0.12,
        "ability": -0.04,
    },
    "buying_timing": {"timing": 0.28, "ability": 0.06, "need": -0.18, "proof": -0.10, "fit": -0.06},
    "reachable_buyers": {
        "access": 0.24,
        "evidence_quality": 0.10,
        "need": -0.18,
        "timing": -0.10,
        "proof": -0.06,
    },
}
FOCUS = {"creative_demand": "need", "buying_timing": "timing", "reachable_buyers": "access"}
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS engine_company_lenses(
        company_key TEXT PRIMARY KEY REFERENCES engine_company_ranking(company_key)
            ON DELETE CASCADE,
        version TEXT NOT NULL, policy_version TEXT NOT NULL, evidence_version TEXT NOT NULL,
        base_as_of TEXT NOT NULL, demand_score INTEGER, timing_score INTEGER, access_score INTEGER,
        priorities_json TEXT NOT NULL)""",
    # Coverage must not read 100k wide evidence/priority JSON rows on a mode switch.
    """CREATE INDEX IF NOT EXISTS idx_engine_lens_base_revision
        ON engine_company_ranking(
            policy_version,dirty,valid_until,as_of,company_key,evidence_version)""",
    """CREATE INDEX IF NOT EXISTS idx_engine_lens_revision
        ON engine_company_lenses(company_key,version,policy_version,evidence_version,base_as_of)""",
    *(
        f"CREATE INDEX IF NOT EXISTS idx_engine_lens_{column} "
        f"ON engine_company_lenses(version,{column} DESC,company_key)"
        for column in COLUMNS.values()
    ),
)


def validate_mode(mode):
    if mode not in MODES:
        raise ValueError("Unknown ranking mode")
    return mode


def score_version(mode, base_version):
    validate_mode(mode)
    return base_version if mode == "balanced" else f"{base_version}:{VERSION}:{mode}"


def _number(value):
    if type(value) not in {int, float} or not math.isfinite(value):
        return 0
    return max(0, min(100, value))


def lens_priority(target, assessment, baseline, mode, *, now):
    """Adjust only the public base, then reuse the unchanged buyer override builder."""
    validate_mode(mode)
    if mode == "balanced":
        return baseline
    opportunity = target.get("opportunity") or {}
    base = _base_score(target)
    adjustment = 0
    if base is not None and base > 0 and opportunity:
        known = set(opportunity.get("known_dimensions") or [])
        values = {
            key: _number(opportunity.get(key)) if key in known or key == "evidence_quality" else 0
            for key in WEIGHTS[mode]
        }
        contrast = sum(values[key] * weight for key, weight in WEIGHTS[mode].items())
        # Confidence limits magnitude; unknown focus cannot earn a positive adjustment.
        contrast *= 0.5 + _number(opportunity.get("evidence_quality")) / 200
        if FOCUS[mode] not in known:
            contrast = min(0, contrast)
        bound = min(12, max(1, base * 0.4))
        adjustment = round(max(-bound, min(bound, contrast)))
        adjusted = max(0, min(100, base + adjustment))
        # Preserve the underlying public-evidence ceilings before adding buyer confirmations.
        ceiling = 100
        if "need" not in known:
            ceiling = min(ceiling, 40)
        if "timing" not in known or "access" not in known:
            ceiling = min(ceiling, 55)
        if _number(opportunity.get("risk")) >= 75 or target.get("availability") == "unavailable":
            ceiling = min(ceiling, 15)
        # A lens cannot newly tighten an already-resolved base above a public ceiling.
        adjusted = min(adjusted, max(base, ceiling))
        adjustment = adjusted - base
    result = (
        build_sales_priority({**target, "priority_score": base + adjustment}, assessment, now=now)
        if base is not None
        else dict(baseline)
    )
    factors = [
        {**factor, "value": base} if factor.get("key") == "stored_evidence" else factor
        for factor in result.get("factors", [])
    ]
    return {
        **result,
        "version": score_version(mode, baseline["version"]),
        "mode": mode,
        "factors": [
            *factors,
            {
                "key": "ranking_lens",
                "mode": mode,
                "public_adjustment": adjustment,
                "value": adjustment,
                "final_adjustment": (result["score"] - baseline["score"])
                if result["score"] is not None and baseline["score"] is not None
                else None,
            },
        ],
    }


def publish_lenses(connection, row, target=None, assessment=None, baseline=None):
    target = target if target is not None else json.loads(row["target_json"])
    assessment = assessment if assessment is not None else json.loads(row["assessment_json"])
    baseline = baseline if baseline is not None else json.loads(row["priority_json"])
    write_lenses(connection, prepare_lenses(row, target, assessment, baseline))


def prepare_lenses(row, target, assessment, baseline):
    now = datetime.fromisoformat(row["as_of"])
    priorities = {
        mode: lens_priority(target, assessment, baseline, mode, now=now)
        for mode in MODES[1:]
    }
    return (
        row["company_key"], VERSION, row["policy_version"], row["evidence_version"],
        row["as_of"], *(priorities[mode]["score"] for mode in MODES[1:]),
        json.dumps(priorities, separators=(",", ":"))
        if baseline["score"] is not None else "{}",
    )


def write_lenses(connection, values):
    connection.execute(
        "INSERT INTO engine_company_lenses VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(company_key) DO UPDATE SET version=excluded.version,"
        "policy_version=excluded.policy_version,evidence_version=excluded.evidence_version,"
        "base_as_of=excluded.base_as_of,demand_score=excluded.demand_score,"
        "timing_score=excluded.timing_score,access_score=excluded.access_score,"
        "priorities_json=excluded.priorities_json",
        values,
    )


def match_sql():
    return (
        "l.company_key=r.company_key AND l.version='" + VERSION + "' "
        "AND l.policy_version=r.policy_version AND l.evidence_version=r.evidence_version "
        "AND l.base_as_of=r.as_of"
    )


def projection_from(mode):
    validate_mode(mode)
    if mode == "balanced":
        return "engine_company_ranking r"
    return f"engine_company_ranking r LEFT JOIN engine_company_lenses l ON {match_sql()}"


def missing_lens_count(connection, *, base_version, now):
    from .read_indexes import ready

    if ready(connection):
        return connection.execute(
            "SELECT COUNT(*) FROM engine_company_listing INDEXED BY idx_engine_listing_gaps "
            "WHERE policy_version=? AND dirty=0 AND lens_ready=0 AND valid_until>? AND as_of<=?",
            (base_version, now.isoformat(), now.isoformat()),
        ).fetchone()[0]
    return connection.execute(
        "SELECT COUNT(*) FROM engine_company_ranking r INDEXED BY idx_engine_lens_base_revision "
        "LEFT JOIN engine_company_lenses l INDEXED BY idx_engine_lens_revision ON "
        + match_sql() + " WHERE l.company_key IS NULL AND r.policy_version=? AND r.dirty=0 "
        "AND r.valid_until>? AND r.as_of<=?",
        (base_version, now.isoformat(), now.isoformat()),
    ).fetchone()[0]


def stored_priority(connection, key, mode, baseline, *, now):
    validate_mode(mode)
    if mode == "balanced":
        return baseline
    from .ranking import policy_version

    row = connection.execute(
        "SELECT l.priorities_json FROM "
        + projection_from(mode)
        + " WHERE r.company_key=? AND r.policy_version=? AND r.dirty=0 "
        "AND r.as_of<=? AND r.valid_until>? AND l.company_key IS NOT NULL",
        (key, policy_version(), now.isoformat(), now.isoformat()),
    ).fetchone()
    if row and (priority := json.loads(row[0]).get(mode)):
        return priority
    return {
        "score": None,
        "label": "Unscored",
        "reason": "",
        "factors": [],
        "limits": [],
        "version": score_version(mode, baseline.get("version", policy_version())),
        "mode": mode,
    }


def refresh_lenses(store, *, limit=500):
    """Backfill saved projections only; never invokes the original scorer or a provider."""
    from .persistence import SalesEngineRepository

    with SalesEngineRepository(store.connection)._transaction():
        rows = store.connection.execute(
            "SELECT r.* FROM engine_company_ranking r LEFT JOIN engine_company_lenses l ON "
            + match_sql()
            + " WHERE l.company_key IS NULL ORDER BY r.company_key LIMIT ?",
            (max(1, min(limit, 1000)),),
        ).fetchall()
        for row in rows:
            publish_lenses(store.connection, row)
    return {"refreshed": len(rows), "provider_requests": 0}
