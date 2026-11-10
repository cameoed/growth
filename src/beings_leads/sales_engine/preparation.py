"""Evidence-bound sales preparation reconciled with the buyer's actual statements."""

from __future__ import annotations

from datetime import datetime, timedelta


def _dated(value, now: datetime, *, days: int | None = None) -> bool:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return bool(
            stamp.tzinfo and stamp <= now and (days is None or stamp >= now - timedelta(days=days))
        )
    except (ValueError, TypeError):
        return False


def build_sales_preparation(packet: dict) -> dict:
    """Reuse cited scanner material without overriding contrary buyer evidence."""
    assessment = packet["buyer_assessment"]
    source = (packet.get("target") or {}).get("decision_packet") or {}
    now = datetime.fromisoformat(packet["as_of"])
    paused = assessment["readiness"] in {"deprioritize", "nurture"}
    unresolved = [c for c in assessment["criteria"] if c["state"] != "confirmed"]
    questions = list(
        dict.fromkeys(
            [
                assessment["next_action"]["question"],
                *[c["question"] for c in unresolved if c.get("question")],
                *source.get("discovery_questions", []),
            ]
        )
    )[:3]
    proof = [
        p
        for p in source.get("proof_points", [])
        if p.get("source_url") and _dated(p.get("observed_at"), now)
    ][:3]
    fresh_trigger = bool(
        source.get("why_now_source_url")
        and _dated(
            source.get("why_now_observed_at"),
            now,
            days=45,
        )
    )
    cautions = list(
        dict.fromkeys(
            [
                *source.get("do_not_claim", []),
                "Public ads, hiring and payroll do not establish an approved "
                "$5,000 monthly budget.",
                "A production opportunity is a hypothesis until the buyer confirms the work.",
                "Research scores are not purchase probabilities or evidence of consent.",
            ]
        )
    )
    return {
        "outreach_paused": paused,
        "why_now": source.get("why_now", "") if fresh_trigger else "",
        "why_now_source_url": source.get("why_now_source_url") if fresh_trigger else None,
        "why_now_observed_at": source.get("why_now_observed_at") if fresh_trigger else None,
        "trigger_note": "" if fresh_trigger else "No recent cited trigger; verify timing directly.",
        "call_opener": ""
        if paused
        else (
            source.get("call_opener")
            if fresh_trigger and source.get("call_opener")
            else assessment["next_action"]["question"]
        ),
        "email_subject": source.get("email_subject", "") if fresh_trigger and not paused else "",
        "email_angle": source.get("email_angle", "") if fresh_trigger and not paused else "",
        "questions": questions,
        "buyer_roles": source.get("buyer_roles", []),
        "proof_points": proof,
        "likely_objection": assessment["next_action"]["question"]
        if paused
        else source.get("likely_objection", ""),
        "safe_response": "Revisit only when the buyer's circumstances change."
        if paused
        else source.get("safe_response", ""),
        "next_step": assessment["next_action"]["question"],
        "do_not_claim": cautions,
        "provider_requests": 0,
    }
