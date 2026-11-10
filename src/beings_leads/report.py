from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote_plus

from .models import RankedProspect
from .naming import concise_company_name


def _safe(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("|", "\\|")
        .replace("\n", " ")
        .strip()
    )


def _company_label(prospect: RankedProspect) -> str:
    return concise_company_name(
        ai_name=prospect.ai.company_name if prospect.ai else "",
        label_hint=prospect.label_hint,
        title=prospect.observation.title,
        canonical_domain=prospect.observation.canonical_host,
        hostname=prospect.hostname,
    )


def _company_url(prospect: RankedProspect) -> str:
    if prospect.ai and prospect.ai.company_url:
        return prospect.ai.company_url
    return prospect.observation.final_url or f"https://{prospect.hostname}"


def _source_reference(prospect: RankedProspect) -> str:
    source = prospect.source_apex
    if prospect.discovery_evidence_urls:
        links = " · ".join(
            f"[record {index + 1}]({url})"
            for index, url in enumerate(prospect.discovery_evidence_urls[:3])
        )
        return f"`{source}` ({links})"
    if source == "openstreetmap.org":
        return "[OpenStreetMap public records](https://www.openstreetmap.org/copyright)"
    if source == "wikidata.org":
        return "[Wikidata public records](https://www.wikidata.org/wiki/Wikidata:Data_access)"
    if source == "agency-portfolio":
        return "a first-party portfolio reference"
    if source == "ai-recovery":
        return "a cited public identity recovery"
    if source == "meta-ad-library":
        return "a verified Meta Ad Library landing-domain discovery"
    if source == "tiktok-ad-library":
        return "a verified TikTok ad-library landing-domain discovery"
    crt_url = f"https://crt.name/v1/search?apex={quote_plus(source)}"
    return f"[`{source}` CT results]({crt_url})"


def _report_priority(prospect: RankedProspect) -> int:
    """Keep strategically review-ready research visible without implying qualification."""
    capacity = prospect.score.creative_capacity
    strategic_research = capacity.total if capacity.review_ready else 0
    return max(prospect.final_score, prospect.score.contact_priority, strategic_research)


def render_markdown(
    metadata: dict,
    prospects: list[RankedProspect],
    *,
    minimum_score: int = 0,
) -> str:
    visible = [
        prospect
        for prospect in prospects
        if _report_priority(prospect) >= minimum_score
    ]
    placement_ready = sum(prospect.score.placement.ready for prospect in visible)
    placement_very_high = sum(
        prospect.score.placement.confidence == "very_high" for prospect in visible
    )
    public_contact = sum(
        bool(
            prospect.observation.contacts.preferred_email
            or prospect.observation.contact_urls
        )
        for prospect in visible
    )
    lines = [
        "# Beings prospect intelligence",
        "",
        f"Snapshot run: **{metadata.get('id', 'unknown')}**  ",
        f"Started: **{metadata.get('started_at', 'unknown')}**  ",
        f"Status: **{metadata.get('status', 'unknown')}**",
        "",
        (
            "**Research only:** scanner and AI results are unverified hypotheses, not outreach "
            "qualification. A curated account may be pursued only after a human verifies all "
            "three principal signals, all six hard gates, an economic buyer, exactly three asks, "
            "and a strategic score of at least 70."
        ),
        "",
        (
            f"Candidates: **{len(visible)}** · placement ready: **{placement_ready}** · "
            f"very high: **{placement_very_high}** · public contact: **{public_contact}**"
        ),
        "",
    ]
    errors = metadata.get("source_errors", [])
    if errors:
        lines.extend(["## Source warnings", ""])
        for error in errors:
            lines.append(f"- `{error['source_apex']}`: {_safe(error['error'])}")
        lines.append("")

    lines.extend(
        [
            "## Ranked links",
            "",
            (
                "| Placement | Priority | Creative-capacity research | Demand | "
                "Capacity to pay | Company | Trigger | Type | Source |"
            ),
            "|---:|---:|---:|---:|---:|---|---|---|---|",
        ]
    )
    for prospect in visible:
        url = _company_url(prospect)
        lines.append(
            f"| **{prospect.score.placement.score}** | {prospect.score.contact_priority} | "
            f"{prospect.score.creative_capacity.total} | {prospect.score.demand_score} | "
            f"{prospect.score.capacity_score} | "
            f"[{_safe(_company_label(prospect))}]({url}) | "
            f"{_safe(prospect.score.latest_trigger or '—')} | {prospect.score.archetype} | "
            f"`{prospect.source_apex}` |"
        )
    lines.append("")

    lines.extend(["## Prospect dossiers", ""])
    for index, prospect in enumerate(visible, 1):
        observation = prospect.observation
        label = _safe(_company_label(prospect))
        url = _company_url(prospect)
        lines.extend(
            [
                (
                    f"### {index}. [{label}]({url}) — priority "
                    f"{prospect.score.contact_priority}/100"
                ),
                "",
                (
                    f"`{prospect.score.archetype}` · confidence `{prospect.score.confidence}` · "
                    f"discovered as [`{prospect.hostname}`]({url}) via "
                    f"{_source_reference(prospect)}"
                ),
                "",
            ]
        )
        placement = prospect.score.placement
        placement_roles = ", ".join(
            role.removeprefix("ai_") for role in placement.specialist_roles
        )
        lines.extend(
            [
                (
                    f"**Placement fit:** {placement.score}/100 · {placement.confidence} · "
                    f"roles {placement_roles or '—'}"
                ),
                "",
            ]
        )
        lines.extend(f"- {_safe(reason)}" for reason in placement.reasons)
        if placement.blockers:
            lines.extend(f"- Hold: {_safe(blocker)}" for blocker in placement.blockers)
        lines.append("")
        if prospect.score.disqualified:
            lines.append(
                "**Qualification hold:** " + "; ".join(prospect.score.disqualifiers)
            )
            if prospect.ai and prospect.ai.company_url:
                lines.append(
                    "  AI web research recovered a cited current company URL; verify it manually."
                )
            lines.append("")
        component_text = (
            f"demand {prospect.score.demand_score} · capacity to pay/operate "
            f"{prospect.score.capacity_score} · timing {prospect.score.timing_score} · "
            "data confidence "
            f"{prospect.score.data_confidence}"
        )
        lines.extend([f"**Legacy research axes:** {component_text}", ""])
        capacity = prospect.score.creative_capacity
        capacity_axes = (
            f"distribution investment {capacity.distribution_investment}/25 · "
            f"recurring creative demand {capacity.recurring_creative_demand}/20 · "
            f"production-capacity gap {capacity.production_capacity_gap}/25 · "
            f"AI/brand readiness {capacity.ai_brand_readiness}/15 · "
            f"timing/access {capacity.timing_access}/15"
        )
        lines.extend(
            [
                (
                    f"**Provisional creative-capacity research:** {capacity.total}/100, "
                    f"unverified · {capacity_axes}"
                ),
                "",
            ]
        )
        principal_labels = {
            "distribution_investment": "distribution investment",
            "recurring_creative_demand": "recurring creative demand",
            "production_capacity_gap": "production-capacity gap",
        }
        principal_text = " · ".join(
            f"{principal_labels[name]} `{assessment.truth_state}` ({assessment.epistemic_status})"
            for name, assessment in capacity.principal_signals
        )
        lines.extend([f"**Principal hypotheses:** {principal_text}", ""])
        if capacity.derived_signals:
            lines.extend(
                [
                    "**Derived hypotheses:** "
                    + " · ".join(item.replace("_", " ") for item in capacity.derived_signals),
                    "",
                ]
            )
        if capacity.observations:
            lines.extend(["**Creative-capacity observations**", ""])
            for claim in capacity.observations:
                citation = f" ([source]({claim.source_url}))" if claim.source_url else ""
                observed = (
                    claim.observed_at.date().isoformat()
                    if claim.observed_at
                    else "unknown date"
                )
                lines.append(f"- {_safe(claim.claim)}{citation} — observed {observed}")
            lines.append("")
        if capacity.inferences:
            lines.extend(["**Creative-capacity inferences (unverified)**", ""])
            lines.extend(f"- {_safe(claim.claim)}" for claim in capacity.inferences)
            lines.append("")
        vendors = observation.web.vendors
        if vendors.vendors:
            vendor_summary = (
                f"stack {vendors.stack_score}/100 · creative-commerce "
                f"{vendors.creative_commerce_score}/100 · {len(vendors.vendors)} vendors · "
                f"{len(vendors.category_counts)} categories · "
                f"{len(vendors.high_confidence_vendors)} high-confidence"
            )
            if vendors.dmarc_policy:
                vendor_summary += f" · DMARC {vendors.dmarc_policy}"
            lines.extend([f"**Vendor intelligence:** {vendor_summary}", ""])
            lines.append(
                "**Observed vendors:** "
                + " · ".join(_safe(vendor) for vendor in vendors.vendors)
            )
            lines.append("")
            lines.append("**Vendor evidence**")
            lines.append("")
            for item in vendors.evidence[:10]:
                detail = item.purpose or item.record or "Public vendor signal"
                lines.append(
                    f"- [{_safe(item.vendor)}]({item.evidence_url}) — "
                    f"{item.source_type.replace('_', ' ')}, {item.confidence}; "
                    f"{_safe(detail)}"
                )
            lines.append("")
        sales = observation.web.sales
        if sales.overall_score:
            lines.extend(
                [
                    (
                        f"**Sales OSINT:** {sales.overall_score}/100 · creative "
                        f"{sales.creative_demand_score} · paid growth "
                        f"{sales.paid_growth_score} · commerce "
                        f"{sales.commerce_scale_score} · capacity "
                        f"{sales.operating_capacity_score} · timing {sales.timing_score}"
                    ),
                    "",
                ]
            )
            lines.extend(f"- {_safe(reason)}" for reason in sales.reasons)
            lines.append("")
            if sales.news_titles:
                news = " · ".join(
                    f"[{_safe(title)}]({sales.news_urls[index]})"
                    for index, title in enumerate(sales.news_titles)
                    if index < len(sales.news_urls)
                )
                if news:
                    lines.extend([f"**Commercial coverage:** {news}", ""])
        if prospect.score.latest_trigger:
            lines.extend([f"**Why now:** {_safe(prospect.score.latest_trigger)}", ""])
        if prospect.score.opportunity_reasons:
            lines.append("**Free-web opportunity evidence**")
            lines.append("")
            lines.extend(f"- {reason}" for reason in prospect.score.opportunity_reasons)
            lines.append("")
        if prospect.score.reasons:
            lines.append("**Why it ranked**")
            lines.append("")
            lines.extend(f"- {reason}" for reason in prospect.score.reasons)
            lines.append("")
        if prospect.score.evidence:
            lines.append("**Observed evidence**")
            lines.append("")
            lines.extend(f"- {evidence}" for evidence in prospect.score.evidence)
            lines.append("")
        if prospect.ai:
            ai_fit = prospect.ai.placement_fit
            ai_score = prospect.ai.placement_fit_score or prospect.ai.budget_fit_score
            lines.extend(
                [
                    (
                        f"**AI review:** {ai_fit} placement fit "
                        f"({ai_score}/100, {prospect.ai.confidence} confidence)"
                    ),
                    "",
                ]
            )
            rationale = prospect.ai.placement_rationale or prospect.ai.rationale
            lines.extend(f"- {item}" for item in rationale)
            if prospect.ai.recommended_offer:
                lines.append(f"- Suggested offer: {prospect.ai.recommended_offer}")
            lines.append("")
            if prospect.ai.public_decision_makers:
                people = " · ".join(
                    f"[{person.name} — {person.role}]({person.source_url})"
                    for person in prospect.ai.public_decision_makers
                )
                lines.extend([f"**Public decision makers:** {people}", ""])
            if prospect.ai.observations:
                lines.extend(["**AI-cited observations**", ""])
                for claim in prospect.ai.observations:
                    citation = f" ([source]({claim.source_url}))" if claim.source_url else ""
                    observed = (
                        claim.observed_at.date().isoformat()
                        if claim.observed_at
                        else "unknown date"
                    )
                    lines.append(f"- {_safe(claim.claim)}{citation} — observed {observed}")
                lines.append("")
            if prospect.ai.inferences:
                lines.extend(["**AI inferences (unverified)**", ""])
                lines.extend(f"- {_safe(claim.claim)}" for claim in prospect.ai.inferences)
                lines.append("")
            if prospect.ai.economic_buyer_role:
                lines.extend(
                    [
                        "**Likely economic-buyer role (unverified):** "
                        + _safe(prospect.ai.economic_buyer_role),
                        "",
                    ]
                )
            if prospect.ai.plausible_first_month_asks:
                lines.extend(["**Plausible first-month asks (unverified)**", ""])
                lines.extend(
                    f"- {_safe(ask)}" for ask in prospect.ai.plausible_first_month_asks
                )
                lines.append("")
            if prospect.ai.rights_ai_risks:
                lines.extend(["**Rights and AI risks to verify**", ""])
                lines.extend(f"- {_safe(risk)}" for risk in prospect.ai.rights_ai_risks)
                lines.append("")
        contact_bits = [
            (
                f"[{contact.address}](mailto:{contact.address}) "
                f"({contact.purpose.replace('_', ' ')}, {contact.confidence}, "
                f"[source]({contact.source_url}))"
            )
            for contact in observation.contacts.emails
            if contact.outreach_eligible and not contact.historical
        ]
        contact_bits.extend(f"[contact page]({url})" for url in observation.contact_urls[:3])
        if contact_bits:
            lines.extend([f"**Public contact routes:** {' · '.join(contact_bits)}", ""])
        evidence_links = list(
            dict.fromkeys(
                [
                    *observation.evidence_urls,
                    *observation.web.source_urls,
                    *(
                        item.evidence_url
                        for item in observation.web.vendors.evidence
                    ),
                    *observation.web.sales.source_urls,
                    *observation.web.sales.news_urls,
                    *(prospect.ai.evidence_urls if prospect.ai else []),
                ]
            )
        )
        if evidence_links:
            lines.append(
                "**Evidence pages:** "
                + " · ".join(f"[source {i + 1}]({item})" for i, item in enumerate(evidence_links))
            )
            lines.append("")
        research_name = quote_plus(prospect.ai.company_name if prospect.ai else label)
        meta_url = (
            "https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
            f"&country=US&q={research_name}&search_type=keyword_unordered"
        )
        tiktok_url = (
            "https://ads.tiktok.com/business/creativecenter/inspiration/topads/pc/en"
            f"?keyword={research_name}"
        )
        lines.append(
            "**Manual ad checks:** "
            f"[Meta Ad Library]({meta_url}) · "
            f"[TikTok Creative Center]({tiktok_url}) · "
            f"[Google search](https://www.google.com/search?q={research_name}+ads)"
        )
        lines.append("")
        material_changes = [
            change for change in prospect.changes if change["kind"] != "new_candidate"
        ]
        if material_changes:
            lines.append("**Changes since the prior snapshot**")
            lines.append("")
            lines.extend(f"- {change['summary']}" for change in material_changes)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_reports(
    metadata: dict,
    prospects: list[RankedProspect],
    *,
    markdown_path: str | Path,
    json_path: str | Path | None = None,
    minimum_score: int = 0,
) -> None:
    markdown_path = Path(markdown_path)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(
        render_markdown(metadata, prospects, minimum_score=minimum_score), encoding="utf-8"
    )
    if json_path:
        json_path = Path(json_path)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run": metadata,
            "prospects": [
                prospect.model_dump(mode="json")
                for prospect in prospects
                if _report_priority(prospect) >= minimum_score
            ],
        }
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
