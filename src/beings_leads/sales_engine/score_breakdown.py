"""Small numeric views of published scores; never recompute company evidence."""

import json
import math

DIMENSIONS = {
    "fit": "Service fit",
    "need": "Creative demand",
    "timing": "Buying timing",
    "access": "Buyer access",
    "ability": "Operating capacity",
    "proof": "Creative proof",
    "risk": "Risk",
    "evidence_quality": "Evidence quality",
    "retained_value_proxy": "Recurring value",
    "uncertainty": "Uncertainty",
}
FACTOR_LABELS = {
    "stored_evidence": "Evidence score",
    "ranking_lens": "Mode adjustment",
    "fit_rejection": "Fit restriction",
    "buyer_hold": "Budget / timing limit",
    "buyer_conflict": "Conflicting evidence limit",
    "buyer_recurring_need": "Confirmed recurring need",
    "buyer_production_gap": "Confirmed production gap",
    "buyer_monthly_budget": "Confirmed monthly budget",
    "buyer_decision_authority": "Confirmed authority",
    "buyer_timing": "Confirmed buying timing",
    "buyer_ai_acceptability": "Confirmed AI acceptability",
    "score_ceiling": "Score limit",
}


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def beings_breakdown(connection, company_key, mode):
    fields = ",".join(
        f"'{key}',json_extract(r.target_json,'$.opportunity.{key}')" for key in DIMENSIONS
    )
    row = connection.execute(
        f"SELECT json_object({fields}),json_extract(r.priority_json,'$.factors'),"
        "json_extract(l.priorities_json,?) "
        "FROM engine_company_ranking r LEFT JOIN engine_company_lenses l "
        "ON l.company_key=r.company_key WHERE r.company_key=?",
        (f"$.{mode}.factors", company_key),
    ).fetchone()
    values = json.loads(row[0])
    factors = json.loads((row[1] if mode == "balanced" else row[2]) or "[]")
    return {
        "dimensions": [
            {"label": label, "value": number(values.get(key))}
            for key, label in DIMENSIONS.items()
        ],
        "adjustments": [
            {
                "label": FACTOR_LABELS.get(factor["key"], "Buyer confirmation"),
                "value": number(factor.get("value")),
                "base": factor["key"] == "stored_evidence",
            }
            for factor in factors
        ],
    }


def mission_breakdown(connection, mission_id, company_key):
    # Project only the small numeric criterion view, not accounting or cited source packets.
    rows = connection.execute(
        "SELECT json_extract(c.value,'$.id'),json_extract(c.value,'$.strength'),"
        "json_extract(c.value,'$.score_contribution') FROM engine_mission_scores s,"
        "json_each(s.audit_json,'$.criteria') c WHERE s.mission_id=? AND s.company_key=?",
        (mission_id, company_key),
    ).fetchall()
    totals = connection.execute(
        "SELECT score,json_extract(audit_json,'$.purchase_signal_contribution') "
        "FROM engine_mission_scores WHERE mission_id=? AND company_key=?",
        (mission_id, company_key),
    ).fetchone()
    adjustments = []
    if rows and all(number(row[2]) is not None for row in rows):
        purchase = number(totals[1]) or 0
        adjustments.append({"label": "Criterion points", "value": sum(row[2] for row in rows),
                            "base": True})
        if purchase:
            adjustments.append({"label": "Purchase signal", "value": purchase, "base": False})
        difference = (totals[0] or 0) - sum(row[2] for row in rows) - purchase
        if abs(difference) > 0.000001:
            adjustments.append({"label": "Limits / rounding", "value": difference, "base": False})
    return {
        "dimensions": [
            {
                "label": str(row[0]).replace("_", " ").replace("-", " ").capitalize(),
                "value": round(row[1] * 100, 1) if number(row[1]) is not None else None,
                "points": number(row[2]),
            }
            for row in rows
        ],
        "adjustments": adjustments,
    }
