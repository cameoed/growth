from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RelationshipLane = Literal["buyer", "referral_partner"]
TargetScope = Literal["direct", "adjacency", "unclassified", "out_of_scope"]


@dataclass(frozen=True)
class SourceHydrationProfile:
    relationship_lane: RelationshipLane
    target_scope: TargetScope
    weight: int


@dataclass(frozen=True)
class PlatformProfile:
    kind: str
    ability_prior: int
    recurring_prior: int
    fit_prior: int
    note: str
    relationship_lane: RelationshipLane = "buyer"
    target_scope: TargetScope = "unclassified"
    hydration_weight: int = 1


PLATFORM_PROFILES: dict[str, PlatformProfile] = {
    "openstreetmap.org": PlatformProfile(
        "agency",
        2,
        3,
        4,
        "OpenStreetMap public agency, studio, or photographer listing",
        "referral_partner",
        "out_of_scope",
        1,
    ),
    "wikidata.org": PlatformProfile(
        "agency",
        3,
        4,
        5,
        "Wikidata advertising, photo, or production company record",
        "referral_partner",
        "out_of_scope",
        1,
    ),
    "demandware.net": PlatformProfile(
        "enterprise_commerce",
        14,
        5,
        5,
        "Salesforce Commerce Cloud footprint",
        "buyer",
        "direct",
        8,
    ),
    "myvtex.com": PlatformProfile(
        "enterprise_commerce",
        13,
        5,
        5,
        "VTEX footprint",
        "buyer",
        "direct",
        7,
    ),
    "vtexcommercestable.com.br": PlatformProfile(
        "enterprise_commerce",
        13,
        5,
        5,
        "VTEX stable storefront footprint",
        "buyer",
        "direct",
        7,
    ),
    "mybigcommerce.com": PlatformProfile(
        "commerce",
        8,
        4,
        5,
        "BigCommerce tenant footprint",
        "buyer",
        "direct",
        5,
    ),
    "shoplineapp.com": PlatformProfile(
        "commerce",
        7,
        4,
        5,
        "SHOPLINE storefront footprint",
        "buyer",
        "direct",
        4,
    ),
    "myshopline.com": PlatformProfile(
        "commerce",
        7,
        4,
        5,
        "SHOPLINE global storefront footprint",
        "buyer",
        "direct",
        4,
    ),
    "3dcartstores.com": PlatformProfile(
        "commerce",
        5,
        3,
        4,
        "Legacy Shift4Shop/3dcart storefront footprint",
        "buyer",
        "direct",
        3,
    ),
    "s4shops.com": PlatformProfile(
        "commerce",
        5,
        3,
        4,
        "Shift4Shop storefront footprint",
        "buyer",
        "direct",
        3,
    ),
    "myshoplaza.com": PlatformProfile(
        "commerce",
        4,
        3,
        3,
        "Shoplazza storefront footprint",
        "buyer",
        "direct",
        2,
    ),
    "company.site": PlatformProfile(
        "commerce",
        3,
        2,
        2,
        "Ecwid Instant Site storefront footprint",
        "buyer",
        "direct",
        1,
    ),
    "clientseoreport.com": PlatformProfile(
        "agency",
        8,
        8,
        6,
        "Agency reporting portal footprint",
        "referral_partner",
        "out_of_scope",
        4,
    ),
    "agencyanalytics.app": PlatformProfile(
        "agency",
        10,
        9,
        7,
        "AgencyAnalytics tenant portal footprint",
        "referral_partner",
        "out_of_scope",
        5,
    ),
    "tapclicks.com": PlatformProfile(
        "agency",
        9,
        8,
        6,
        "Marketing reporting/operations footprint",
        "referral_partner",
        "out_of_scope",
        4,
    ),
    "dashclicks.com": PlatformProfile(
        "agency",
        7,
        7,
        6,
        "Agency platform footprint",
        "referral_partner",
        "out_of_scope",
        3,
    ),
    "reportgarden.com": PlatformProfile(
        "agency",
        7,
        7,
        6,
        "Agency reporting footprint",
        "referral_partner",
        "out_of_scope",
        2,
    ),
    "seoptimer.com": PlatformProfile(
        "agency",
        5,
        5,
        3,
        "White-label audit footprint",
        "referral_partner",
        "out_of_scope",
        1,
    ),
    "websiteauditserver.com": PlatformProfile(
        "agency",
        4,
        4,
        2,
        "SEOptimer white-label report footprint",
        "referral_partner",
        "out_of_scope",
        1,
    ),
    "mykajabi.com": PlatformProfile(
        "creator_education",
        6,
        6,
        6,
        "Commercial creator platform footprint",
        "buyer",
        "out_of_scope",
        1,
    ),
    "uscreen.io": PlatformProfile(
        "media_membership",
        7,
        8,
        9,
        "Paid video membership footprint",
        "buyer",
        "direct",
        3,
    ),
    "thinkific.com": PlatformProfile(
        "creator_education",
        4,
        5,
        5,
        "Course platform footprint",
        "buyer",
        "out_of_scope",
        1,
    ),
    "teachable.com": PlatformProfile(
        "creator_education",
        4,
        5,
        5,
        "Course platform footprint",
        "buyer",
        "out_of_scope",
        1,
    ),
    "circle.so": PlatformProfile(
        "creator_education",
        4,
        6,
        4,
        "Community platform footprint",
        "buyer",
        "out_of_scope",
        1,
    ),
    "mktoweb.com": PlatformProfile(
        "b2b_marketing",
        11,
        6,
        5,
        "Marketo demand-generation footprint",
        "buyer",
        "adjacency",
        2,
    ),
    "hs-sites.com": PlatformProfile(
        "b2b_marketing",
        8,
        5,
        4,
        "HubSpot hosted-site footprint",
        "buyer",
        "adjacency",
        2,
    ),
    "hs-sites-eu1.com": PlatformProfile(
        "b2b_marketing",
        8,
        5,
        4,
        "HubSpot hosted-site footprint",
        "buyer",
        "adjacency",
        2,
    ),
    "hs-sites-na2.com": PlatformProfile(
        "b2b_marketing",
        8,
        5,
        4,
        "HubSpot hosted-site footprint",
        "buyer",
        "adjacency",
        2,
    ),
    "hs-sites-na3.com": PlatformProfile(
        "b2b_marketing",
        8,
        5,
        4,
        "HubSpot hosted-site footprint",
        "buyer",
        "adjacency",
        2,
    ),
    "hs-sites-ap1.com": PlatformProfile(
        "b2b_marketing",
        8,
        5,
        4,
        "HubSpot hosted-site footprint",
        "buyer",
        "adjacency",
        2,
    ),
}


DERIVED_SOURCE_HYDRATION: dict[str, SourceHydrationProfile] = {
    # First-party client references resolve to potential buyers even though agencies
    # produced the relationship edge.
    "agency-portfolio": SourceHydrationProfile("buyer", "direct", 6),
    # A company displayed on another company's customer, testimonial, case-study, or
    # portfolio surface is a direct-buyer discovery path. The assertion itself remains
    # explicitly non-current and qualification still requires first-party evidence.
    "publisher-appearance": SourceHydrationProfile("buyer", "direct", 7),
    "ai-recovery": SourceHydrationProfile("buyer", "unclassified", 3),
    # A verified paid advertiser landing on its first-party domain is a strong direct-buyer
    # discovery prior. Qualification and the capacity-gap signal still require other evidence.
    "meta-ad-library": SourceHydrationProfile("buyer", "direct", 8),
    "tiktok-ad-library": SourceHydrationProfile("buyer", "direct", 6),
}


def source_hydration_profile(source_apex: str) -> SourceHydrationProfile:
    derived = DERIVED_SOURCE_HYDRATION.get(source_apex)
    if derived is not None:
        return derived
    profile = PLATFORM_PROFILES.get(source_apex)
    if profile is None:
        return SourceHydrationProfile("buyer", "unclassified", 1)
    return SourceHydrationProfile(
        profile.relationship_lane,
        profile.target_scope,
        max(1, profile.hydration_weight),
    )


INFRA_LABELS = {
    "api",
    "app",
    "assets",
    "auth",
    "beta",
    "blog",
    "cdn",
    "careers",
    "click",
    "cms",
    "community",
    "console",
    "dashboard",
    "demo",
    "dev",
    "developer",
    "docs",
    "email",
    "files",
    "ftp",
    "help",
    "info",
    "internal",
    "login",
    "mail",
    "m",
    "media",
    "mobile",
    "monitor",
    "mx",
    "origin",
    "partners",
    "preview",
    "prod",
    "qa",
    "sandbox",
    "secure",
    "shop",
    "smtp",
    "staging",
    "static",
    "status",
    "store",
    "support",
    "test",
    "try",
    "updates",
    "vpn",
    "webmail",
    "www",
}


USER_AGENT = "BeingsProspectMonitor/0.1 (+https://beings.org; public business research)"
