from __future__ import annotations

from .entity_intelligence import (
    CompanyRoleAssessment,
    assess_company_role,
    company_self_corpus,
)
from .models import PlacementFit, SiteObservation, legacy_agency_portfolio_domains

PHOTO_TERMS = (
    "commercial photography",
    "content photography",
    "ecommerce photography",
    "fashion photography",
    "food photography",
    "image production",
    "lifestyle photography",
    "photo production",
    "photo shoot",
    "photography studio",
    "product photography",
    "retouching",
    "stills production",
)
VIDEO_TERMS = (
    "animation studio",
    "cinematography",
    "commercial film",
    "film production",
    "filmmaking",
    "motion design",
    "motion graphics",
    "post production",
    "post-production",
    "product video",
    "social video",
    "video advertising",
    "video editing",
    "video marketing",
    "video production",
    "vfx",
)
PERFORMANCE_CREATIVE_TERMS = (
    "ad creative",
    "creative strategy",
    "creative testing",
    "direct response creative",
    "paid social creative",
    "performance creative",
    "social ad creative",
    "ugc production",
)
PAID_SOCIAL_TERMS = (
    "customer acquisition",
    "direct response",
    "media buying",
    "meta ads",
    "paid media",
    "paid social",
    "performance marketing",
    "tiktok ads",
)
AGENCY_DELIVERY_TERMS = (
    "agency",
    "case studies",
    "client roster",
    "client success",
    "creative studio",
    "our clients",
    "our work",
    "production company",
    "selected work",
)
RECURRING_TERMS = (
    "always-on",
    "monthly content",
    "monthly retainer",
    "ongoing creative",
    "ongoing production",
    "recurring",
    "retained clients",
    "retainer",
    "weekly content",
)
EXTERNAL_CAPACITY_TERMS = (
    "contract role",
    "contractor",
    "creator network",
    "overflow production",
    "production partner",
    "retained production",
    "talent roster",
    "white label",
    "white-label",
)
SOLO_TERMS = (
    "freelance photographer",
    "freelance videographer",
    "independent photographer",
    "independent videographer",
    "one person studio",
    "one-person studio",
    "solo photographer",
    "solo videographer",
    "solopreneur",
)
PARKING_TERMS = (
    "afternic",
    "bodis",
    "buy this domain",
    "dan.com domain",
    "domain is for sale",
    "domain marketplace",
    "hugedomains",
    "make an offer for this domain",
    "parkingcrew",
    "sedo domain parking",
    "this domain may be for sale",
)
UNRELATED_AGENCY_TERMS = (
    "employment agency",
    "government agency",
    "insurance agency",
    "modeling agency",
    "recruitment agency",
    "real estate agency",
    "staffing agency",
    "talent agency",
    "travel agency",
)
SEO_ONLY_TERMS = (
    "backlink building",
    "link building agency",
    "search engine optimization",
    "seo agency",
    "technical seo",
)
UNSAFE_CONTACT_PURPOSES = {"support", "careers", "legal", "privacy", "security", "unknown"}


def _normalized_corpus(observation: SiteObservation) -> str:
    return company_self_corpus(observation, for_delivery=True)


def _matches(corpus: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if term in corpus]


def _safe_contact_count(observation: SiteObservation) -> int:
    return sum(
        contact.outreach_eligible
        and not contact.historical
        and contact.purpose not in UNSAFE_CONTACT_PURPOSES
        and contact.confidence in {"medium", "high"}
        and bool(contact.source_url)
        for contact in observation.contacts.emails
    )


def _capacity_points(capacity_score: int) -> int:
    if capacity_score >= 70:
        return 18
    if capacity_score >= 55:
        return 15
    if capacity_score >= 45:
        return 12
    if capacity_score >= 30:
        return 7
    if capacity_score >= 20:
        return 3
    return 0


def assess_placement_fit(
    observation: SiteObservation,
    *,
    archetype: str,
    demand_score: int,
    capacity_score: int,
    data_confidence: int,
    company_role: CompanyRoleAssessment | None = None,
) -> PlacementFit:
    """Assess evidence for placing an AI photo/video specialist on a working team.

    The score describes public evidence, not a prediction of a guaranteed purchase. Textual
    positioning establishes specificity; assets, hiring, portfolios, and operating signals
    corroborate it. Reachability can improve execution confidence but cannot create readiness.
    """

    assessment = company_role or assess_company_role(observation)
    corpus = _normalized_corpus(observation)
    identity_corpus = company_self_corpus(observation)
    attributed_agency = assessment.role == "production_agency"
    agency_self_delivery = bool(
        attributed_agency
        and any(
            claim.attribution == "company_self" and claim.predicate == "creative.delivery"
            for claim in assessment.claims
        )
    )
    photo_hits = _matches(corpus, PHOTO_TERMS)
    video_hits = _matches(corpus, VIDEO_TERMS)
    performance_hits = _matches(corpus, PERFORMANCE_CREATIVE_TERMS)
    paid_hits = _matches(corpus, PAID_SOCIAL_TERMS)
    agency_hits = _matches(corpus, AGENCY_DELIVERY_TERMS)
    recurring_hits = _matches(corpus, RECURRING_TERMS)
    external_capacity_hits = _matches(corpus, EXTERNAL_CAPACITY_TERMS)

    web = observation.web
    sales = web.sales
    portfolio_count = len(legacy_agency_portfolio_domains(web)) if agency_self_delivery else 0
    case_studies = int(web.sitemap_counts.get("case_studies", 0)) if agency_self_delivery else 0
    recent_case_studies = (
        int(web.sitemap_recent_counts.get("case_studies", 0)) if agency_self_delivery else 0
    )
    campaigns = int(web.sitemap_counts.get("campaigns", 0))
    recent_campaigns = int(web.sitemap_recent_counts.get("campaigns", 0))
    video_assets = sales.video_assets + sales.embedded_video_players
    safe_contacts = _safe_contact_count(observation)

    specialties: list[str] = []
    if photo_hits:
        specialties.append("photography")
    if video_hits:
        specialties.append("video_production")
    motion_terms = ("motion design", "motion graphics", "animation studio", "vfx")
    if any(term in corpus for term in motion_terms):
        specialties.append("motion_animation")
    post_terms = ("post production", "post-production", "video editing", "retouching")
    if any(term in corpus for term in post_terms):
        specialties.append("post_production")
    if performance_hits:
        specialties.append("performance_creative")
    if paid_hits:
        specialties.append("paid_social")
    if any(term in corpus for term in ("ugc", "social content", "content studio")):
        specialties.append("ugc_content")
    ecommerce_terms = ("ecommerce", "e-commerce", "product photography", "product video")
    if any(term in corpus for term in ecommerce_terms):
        specialties.append("ecommerce_content")
    specialties = list(dict.fromkeys(specialties))

    roles: list[str] = []
    creative_context = bool(photo_hits or video_hits or performance_hits)
    if photo_hits or performance_hits or (sales.catalog_product_images >= 20 and creative_context):
        roles.append("ai_photo")
    if video_hits or performance_hits or (video_assets >= 2 and creative_context):
        roles.append("ai_video")

    evidence_families: list[str] = []
    reasons: list[str] = []
    if roles:
        evidence_families.append("creative_specialism")
        formats = "/".join(role.removeprefix("ai_") for role in roles)
        reasons.append(f"Specific {formats} delivery evidence is public")

    agency_delivery = archetype == "agency" and agency_self_delivery
    if agency_delivery:
        evidence_families.append("agency_delivery")
        reasons.append("Agency or studio positioning connects creative work to client delivery")

    recurring_client_work = bool(
        recurring_hits or portfolio_count >= 5 or case_studies >= 5 or recent_case_studies >= 2
    )
    if recurring_client_work:
        evidence_families.append("recurring_client_work")
        reasons.append(
            "Retainer, recurring, or repeated client-work evidence indicates ongoing load"
        )

    portfolio_throughput = bool(
        portfolio_count >= 3
        or case_studies >= 3
        or recent_case_studies >= 1
        or campaigns >= 5
        or recent_campaigns >= 2
    )
    if portfolio_throughput:
        evidence_families.append("portfolio_throughput")
        reasons.append(
            f"Public work footprint includes {portfolio_count} client domains, "
            f"{case_studies} case studies, and {campaigns} campaign URLs"
        )

    if paid_hits:
        evidence_families.append("paid_distribution")
        reasons.append("Paid-distribution work creates continuous creative refresh pressure")

    production_evidence = bool(
        web.photo_video_jobs
        or (web.creative_jobs and roles)
        or (video_assets and roles)
        or (sales.image_assets >= 10 and roles)
        or (sales.creative_technologies and roles)
    )
    if production_evidence:
        evidence_families.append("production_operations")
        reasons.append(
            f"Non-copy evidence includes {web.photo_video_jobs} photo/video roles and "
            f"{video_assets} video assets or players"
        )

    external_capacity_buying = bool(external_capacity_hits)
    if external_capacity_buying:
        evidence_families.append("external_capacity_buying")
        reasons.append(
            "First-party language describes overflow, white-label, partner, contractor, or "
            "roster-based production capacity"
        )

    production_capacity_pressure = bool(
        web.photo_video_jobs >= 2 or (web.creative_jobs >= 3 and roles)
    )
    if production_capacity_pressure:
        evidence_families.append("production_capacity_pressure")
        reasons.append(
            "Current creative hiring indicates pressure on photo/video production capacity"
        )

    if capacity_score >= 45:
        evidence_families.append("operating_capacity")
        reasons.append(
            f"Operating-capacity evidence clears the placement gate ({capacity_score}/100)"
        )

    active_output = bool(
        recent_case_studies
        or recent_campaigns
        or sales.feed_commercial_items >= 2
        or web.sitemap_recent_30d >= 5
        or web.photo_video_jobs
    )
    if active_output:
        evidence_families.append("active_creative_output")
        reasons.append("Recent publishing, work, or hiring evidence shows current demand")

    if safe_contacts:
        evidence_families.append("safe_outreach")
        reasons.append("A current, explicitly published outreach-eligible contact is available")

    creative_points = 0
    if photo_hits and video_hits:
        creative_points = 30
    elif photo_hits or video_hits:
        creative_points = 24
    elif performance_hits:
        creative_points = 22
    if roles:
        creative_points = min(30, creative_points + min(demand_score // 20, 5))

    type_points = 0
    if archetype == "agency":
        type_points = 12 + min(len(agency_hits), 3)
    elif archetype in {"ecommerce_brand", "media_membership"}:
        type_points = 8
    elif archetype == "saas_or_other":
        type_points = 6

    recurrence_points = 0
    if recurring_client_work:
        recurrence_points += 10
    if portfolio_throughput:
        recurrence_points += 8
    if recent_case_studies or recent_campaigns or sales.feed_commercial_items >= 2:
        recurrence_points += 4
    recurrence_points = min(recurrence_points, 22)

    corroboration_points = 0
    if production_evidence:
        corroboration_points += 6
    if active_output:
        corroboration_points += 4
    corroboration_points = min(corroboration_points, 10)
    capacity_signal_points = min(
        10,
        (6 if external_capacity_buying else 0) + (6 if production_capacity_pressure else 0),
    )

    score = min(
        100,
        creative_points
        + type_points
        + recurrence_points
        + _capacity_points(capacity_score)
        + corroboration_points
        + capacity_signal_points
        + (5 if safe_contacts else 0),
    )

    hard_blockers: list[str] = []
    if not observation.live:
        hard_blockers.append("Site is not currently live")
    if observation.status_code in {401, 403}:
        hard_blockers.append("Public evidence is gated")
    if _matches(identity_corpus, PARKING_TERMS):
        hard_blockers.append("Domain is parked or offered for sale")
    if _matches(identity_corpus, SOLO_TERMS):
        hard_blockers.append("Public positioning describes a solo or freelance operator")
    if _matches(identity_corpus, UNRELATED_AGENCY_TERMS):
        hard_blockers.append("Agency terminology refers to a non-creative business")
    if _matches(identity_corpus, SEO_ONLY_TERMS) and not roles:
        hard_blockers.append("Agency evidence is limited to SEO rather than photo/video delivery")

    blockers = list(hard_blockers)
    if not roles:
        blockers.append("No specific photo, video, or performance-creative evidence")
    if archetype == "agency" and not agency_self_delivery:
        blockers.append("Agency delivery is not supported by an attributed company-self claim")
    if archetype == "agency" and not recurring_client_work:
        blockers.append("Agency lacks recurring-client or repeated portfolio evidence")
    if archetype == "agency" and not production_evidence:
        blockers.append(
            "Agency lacks corroborated photo/video production operations beyond service copy"
        )
    if archetype != "agency" and not active_output:
        blockers.append("Company lacks current in-house creative throughput evidence")
    if capacity_score < 45:
        blockers.append("Operating-capacity evidence is below the placement threshold")
    if data_confidence < 45:
        blockers.append("Public evidence coverage is too thin for placement confidence")
    blockers = list(dict.fromkeys(blockers))

    if hard_blockers:
        score = min(score, 35)

    family_count = len(evidence_families)
    ready = bool(
        not hard_blockers
        and roles
        and capacity_score >= 45
        and data_confidence >= 45
        and score >= 70
        and family_count >= 4
        and (
            (
                archetype == "agency"
                and recurring_client_work
                and agency_delivery
                and production_evidence
            )
            or (archetype != "agency" and active_output and production_evidence)
        )
    )

    very_high = bool(
        ready
        and archetype == "agency"
        and roles
        and recurring_client_work
        and capacity_score >= 45
        and data_confidence >= 55
        and safe_contacts
        and family_count >= 4
        and score >= 80
        and (external_capacity_buying or production_capacity_pressure)
        and not hard_blockers
    )
    if very_high:
        confidence = "very_high"
    elif (
        ready
        and data_confidence >= 50
        and family_count >= 4
        and (archetype != "agency" or external_capacity_buying or production_capacity_pressure)
    ):
        confidence = "high"
    elif roles and not hard_blockers and score >= 50 and family_count >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    return PlacementFit(
        score=score,
        confidence=confidence,
        ready=ready,
        specialist_roles=roles,
        agency_specialties=specialties,
        evidence_families=evidence_families,
        reasons=list(dict.fromkeys(reasons))[:10],
        blockers=blockers[:8],
    )
