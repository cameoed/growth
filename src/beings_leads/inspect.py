from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import httpx
import tldextract
from bs4 import BeautifulSoup, Tag

from .company_research import (
    FAILURE_LIMIT,
    LOCAL_CAPACITY_DEFERRED,
    PUBLIC_FETCH_SECONDS,
    bounded_public_request,
)
from .config import USER_AGENT
from .contact_intelligence import (
    build_contact_intelligence,
    extract_contact_intelligence,
    extract_vcard_email_evidence,
    extract_vcard_phone_evidence,
)
from .domain_policy import require_allowed
from .frontier_intelligence import extract_frontier_from_html, merge_frontier
from .html_document import parse_html, reuse_html
from .language import assess_english_presentation
from .models import (
    CompanyAppearanceEvidence,
    ContactIntelligence,
    DiscoveredHost,
    DiscoveryAssertionEvidence,
    PageEvidence,
    SiteObservation,
    WebIntelligence,
)
from .naming import _compact as _compact_company_identity
from .platform_network_policy import is_platform_tenant, terminal_platform_observation
from .provider_rate_limit import REQUEST_ACTIVITY, notify_request_activity, provider_stream
from .site_research import DEFAULT_SITE_RESEARCH_PAGES, SiteResearchPlanner, company_host, url_key

PRICE_RE = re.compile(
    r"(?:[$€£]\s?\d[\d,.]*(?:\s?(?:/|per)\s?(?:month|mo|year|yr))?|"
    r"\d[\d,.]*\s?(?:USD|EUR|GBP))",
    re.I,
)
SOCIAL_HOSTS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "youtu.be",
}
PRIORITY_PATH_WORDS = (
    "contact",
    "new-business",
    "case-stud",
    "portfolio",
    "work",
    "client",
    "capabilit",
    "production",
    "studio",
    "creative",
    "service",
    "press",
    "media",
    "about",
    "team",
    "pricing",
    "vcard",
    ".vcf",
)
LEGAL_PATH_WORDS = (
    "subprocessor",
    "sub-processor",
    "privacy",
    "data-processing",
    "dpa",
    "trust",
    "legal",
)
PUBLIC_ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "google_tag": re.compile(r"\b(?:GTM-[A-Z0-9]+|G-[A-Z0-9]{6,}|UA-\d+-\d+)\b", re.I),
    "meta_pixel": re.compile(r"fbq\s*\(\s*['\"]init['\"]\s*,\s*['\"](\d{6,})", re.I),
    "tiktok_pixel": re.compile(r"ttq\.load\s*\(\s*['\"]([A-Z0-9]{8,})", re.I),
    "linkedin_partner": re.compile(r"_linkedin_partner_id\s*=\s*['\"]?(\d{4,})", re.I),
}
ATS_PATTERNS: dict[str, re.Pattern[str]] = {
    "greenhouse": re.compile(
        r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)",
        re.I,
    ),
    "lever": re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", re.I),
}
ATS_PROVIDER_HOSTS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("boards.greenhouse.io", "job-boards.greenhouse.io"),
    "lever": ("jobs.lever.co",),
    "ashby": ("jobs.ashbyhq.com",),
}

TECH_MARKERS: dict[str, tuple[str, ...]] = {
    "Shopify": ("cdn.shopify.com", "shopify-payment-button"),
    "WooCommerce": ("woocommerce", "wc-block-components", "/wp-content/plugins/woocommerce"),
    "WordPress": ("/wp-content/", "/wp-includes/"),
    "BigCommerce": ("cdn11.bigcommerce.com", "stencil-utils"),
    "Salesforce Commerce Cloud": ("demandware", "dwcont"),
    "VTEX": ("vtexassets.com", "vtex.io"),
    "HubSpot": ("js.hs-scripts.com", "js.hsforms.net"),
    "Klaviyo": ("static.klaviyo.com", "klaviyo.js", "klaviyo onsite"),
    "Attentive": ("cdn.attn.tv", "attentivemobile.com"),
    "Braze": ("js.appboycdn.com", "braze.min.js"),
    "Mailchimp": ("chimpstatic.com", "list-manage.com"),
    "Gorgias": ("gorgias.chat", "gorgias.io/app"),
    "Intercom": ("widget.intercom.io", "intercomsettings"),
    "Yotpo": ("staticw2.yotpo.com", "yotpo_site_reviews"),
    "Okendo": ("cdn.okendo.io", "okendo-reviews"),
    "Bazaarvoice": ("bazaarvoice.com", "bvapi"),
    "Stamped": ("stamped.io", "stamped-main-widget"),
    "Recharge": ("rechargepayments.com", "rechargecdn.com"),
    "Skio": ("skio.com", "skio-plan-picker"),
    "Loop Subscriptions": ("loopsubscriptions.com", "loop-widget"),
    "Triple Whale": ("triplewhale", "triple-whale"),
    "Northbeam": ("northbeam.io", "northbeam.js"),
    "Elevar": ("getelevar.com", "elevargtm"),
    "Rockerbox": ("rockerbox.com", "rbtrack"),
    "Optimizely": ("cdn.optimizely.com", "optimizelydata"),
    "VWO": ("dev.visualwebsiteoptimizer.com", "_vwo_code"),
    "AB Tasty": ("try.abtasty.com", "abtasty.com/js"),
    "Hotjar": ("static.hotjar.com", "hjsettings"),
    "Cloudinary": ("res.cloudinary.com", "cloudinary-core"),
    "Imgix": (".imgix.net", "images.imgix.net"),
    "Mux": ("stream.mux.com", "mux-player", "mux-video"),
    "Wistia": ("fast.wistia.com", "wistia_embed"),
    "Vimeo": ("player.vimeo.com",),
    "Contentful": ("images.ctfassets.net", "contentful.com"),
    "Sanity": ("cdn.sanity.io", "sanity-image"),
    "Meta Pixel": ("connect.facebook.net", "fbq("),
    "TikTok Pixel": ("analytics.tiktok.com", "ttq."),
    "LinkedIn Insight": ("snap.licdn.com", "_linkedin_partner_id"),
    "Google Ads/Analytics": ("googletagmanager.com", "gtag("),
}

GROWTH_TECHNOLOGIES = {
    "AB Tasty",
    "Attentive",
    "Bazaarvoice",
    "Braze",
    "Elevar",
    "Google Ads/Analytics",
    "Gorgias",
    "Hotjar",
    "HubSpot",
    "Intercom",
    "Klaviyo",
    "LinkedIn Insight",
    "Loop Subscriptions",
    "Mailchimp",
    "Meta Pixel",
    "Northbeam",
    "Okendo",
    "Optimizely",
    "Recharge",
    "Rockerbox",
    "Skio",
    "Stamped",
    "TikTok Pixel",
    "Triple Whale",
    "VWO",
    "Yotpo",
}

CREATIVE_TECHNOLOGIES = {
    "Cloudinary",
    "Contentful",
    "Imgix",
    "Mux",
    "Sanity",
    "Vimeo",
    "Wistia",
}
NON_CLIENT_HOSTS = SOCIAL_HOSTS | {
    "adobe.com",
    "apple.com",
    "calendly.com",
    "cloudflare.com",
    "fonts.googleapis.com",
    "google.com",
    "googleapis.com",
    "gstatic.com",
    "hubspot.com",
    "mailchimp.com",
    "microsoft.com",
    "shopify.com",
    "squarespace.com",
    "typeform.com",
    "vimeo.com",
    "wix.com",
    "wordpress.com",
}

MAX_APPEARANCE_IMAGE_NODES = 500
MAX_APPEARANCE_ASSETS_PER_NODE = 8
MAX_PAGE_COMPANY_APPEARANCES = 80
MAX_SITE_COMPANY_APPEARANCES = 100
ROOT_FETCH_ATTEMPTS = 2
ROOT_FETCH_TOTAL_ATTEMPTS = FAILURE_LIMIT
ROOT_RETRY_AFTER_MAX_SECONDS = 5.0
ROOT_TRANSIENT_STATUS_CODES = frozenset(
    {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 530}
)
MICROLINK_API_URL = "https://pro.microlink.io/"
MICROLINK_RESPONSE_OVERHEAD_BYTES = 250_000
MICROLINK_ELIGIBLE_STATUS_CODES = ROOT_TRANSIENT_STATUS_CODES | {403}

_APPEARANCE_IMAGE_ATTRIBUTES = (
    "src",
    "srcset",
    "data-src",
    "data-srcset",
    "data-lazy-src",
    "data-lazy-srcset",
    "data-original",
    "data-flickity-lazyload",
)
_APPEARANCE_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.I,
)
_APPEARANCE_TLD_EXTRACT = tldextract.TLDExtract(
    suffix_list_urls=(),
    include_psl_private_domains=False,
)
_APPEARANCE_NON_AUTHORITATIVE_APEXES = {
    # Referral/affiliate platforms expose merchant names as tenant subdomains. Those URLs are
    # evidence links, not proof that the tenant owns the platform's DNS domain.
    "everflowclient.io",
    "firstpromoter.com",
    "goaffpro.com",
    "impact.com",
    "impactradius.com",
    "leaddyno.com",
    "partnerstack.com",
    "partnerize.com",
    "refersion.com",
    "rewardful.com",
    "tapfiliate.com",
    # The same ownership rule applies to common multi-tenant site hosts.
    "carrd.co",
    "framer.website",
    "github.io",
    "myshopify.com",
    "netlify.app",
    "notion.site",
    "pages.dev",
    "squarespace.com",
    "vercel.app",
    "webflow.io",
    "wixsite.com",
}
_APPEARANCE_RELATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "testimonial",
        re.compile(r"\b(?:testimonials?|customer quotes?|what (?:our )?customers say)\b", re.I),
    ),
    (
        "case_study",
        re.compile(
            r"\b(?:case stud(?:y|ies)|customer stor(?:y|ies)|success stor(?:y|ies))\b",
            re.I,
        ),
    ),
    (
        "customer",
        re.compile(
            r"\b(?:our|selected|featured|some of our) (?:customers?|clients?|brands?)\b|"
            r"\b(?:customers?|clients?)\b|trusted by|used by|brands? we (?:work|worked) with",
            re.I,
        ),
    ),
    (
        "portfolio",
        re.compile(r"\b(?:portfolio|our work|selected work|client work|campaigns?)\b", re.I),
    ),
    (
        "partner",
        re.compile(
            r"\b(?:our partners?|partner (?:network|directory)|implementation partners?)\b",
            re.I,
        ),
    ),
    (
        "award",
        re.compile(r"\b(?:award recipients?|winners?|finalists?|shortlist(?:ed)?)\b", re.I),
    ),
    ("member", re.compile(r"\b(?:our members?|member directory|members?)\b", re.I)),
    ("sponsor", re.compile(r"\b(?:our sponsors?|sponsored by|sponsors?)\b", re.I)),
    ("exhibitor", re.compile(r"\b(?:our exhibitors?|exhibitors?|exhibiting)\b", re.I)),
)
_APPEARANCE_LOGO_MARKER_RE = re.compile(
    r"\b(?:client|customer|brand|partner|sponsor)[-_ ]?logos?\b|"
    r"\b(?:logos?|wordmarks?|brandmarks?|logo[-_ ]?cloud|ticker)\b",
    re.I,
)
_APPEARANCE_NEGATIVE_CONTEXT_RE = re.compile(
    r"\b(?:as seen in|featured in|in the press|press logos?|media coverage|publications?|"
    r"accepted payments?|payment methods?|download (?:on|from)|app store|google play|"
    r"powered by|built with|technology stack|tools we use|shop by brand|brands we carry|"
    r"certifications?|compliance badges?)\b",
    re.I,
)
_APPEARANCE_VENDOR_CONTEXT_RE = re.compile(
    r"\b(?:customer data integrations?|data integrations?|integration (?:catalog|directory|"
    r"marketplace|partners?)|integrations? (?:catalog|directory|marketplace|partners?)|"
    r"our integrations?|available integrations?|technology partners?|data sources?|"
    r"supported (?:apps?|platforms?|tools?)|connectors?|connect(?:s|ed)? with|compatible with)\b",
    re.I,
)
_APPEARANCE_ASSET_REJECT_RE = re.compile(
    r"(?:^|[-_ ])(?:app[-_ ]?store|google[-_ ]?play|payment|accepted[-_ ]?cards?|"
    r"certification|certified|compliance|badge|seal|favicon|social[-_ ]?icon|"
    r"facebook[-_ ]?icon|instagram[-_ ]?icon|linkedin[-_ ]?icon|twitter[-_ ]?icon)"
    r"(?:[-_ .]|$)",
    re.I,
)
_APPEARANCE_GENERIC_LABELS = {
    "brand",
    "brand logo",
    "brand name",
    "client",
    "client logo",
    "client story",
    "company",
    "company logo",
    "case study",
    "customer",
    "customer logo",
    "customer story",
    "full portfolio",
    "image",
    "learn more",
    "logo",
    "logos",
    "our work",
    "placeholder",
    "portfolio",
    "project",
    "read more",
    "see our portfolio",
    "screenshot",
    "view portfolio",
    "view work",
    "work",
    "your logo",
}
_APPEARANCE_FILENAME_NOISE = {
    "asset",
    "assets",
    "black",
    "brand",
    "brandmark",
    "client",
    "color",
    "colour",
    "copy",
    "cmyk",
    "dark",
    "desktop",
    "final",
    "full",
    "gray",
    "grey",
    "horizontal",
    "icon",
    "image",
    "img",
    "large",
    "light",
    "logo",
    "logos",
    "mark",
    "mobile",
    "new",
    "old",
    "primary",
    "resize",
    "resized",
    "rgb",
    "secondary",
    "small",
    "stacked",
    "transparent",
    "vertical",
    "white",
    "wordmark",
}
_APPEARANCE_GENERIC_FILENAME_RE = re.compile(
    r"^(?:dsc|img|image|pic|photo|screenshot|untitled)(?:[-_ ]?\d.*)?$",
    re.I,
)
_LEGACY_PORTFOLIO_PROJECTION_RELATION = "portfolio"

SIGNAL_PATTERNS: dict[str, tuple[str, ...]] = {
    "agency": (
        "our clients",
        "client success",
        "performance marketing",
        "paid social",
        "creative agency",
        "marketing agency",
        "advertising agency",
        "digital agency",
        "production company",
        "production studio",
        "content studio",
        "media buying",
        "roas",
        "case studies",
    ),
    "ecommerce": (
        "add to cart",
        "shop now",
        "free shipping",
        "product reviews",
        "best sellers",
        "subscribe & save",
        "subscription",
    ),
    "creative_demand": (
        "video production",
        "photo production",
        "photography",
        "videography",
        "post-production",
        "post production",
        "video editing",
        "motion design",
        "motion graphics",
        "animation",
        "3d production",
        "cgi",
        "vfx",
        "social content",
        "content studio",
        "creative production",
        "performance creative",
        "paid social creative",
        "product photography",
        "commercial photography",
        "lifestyle photography",
        "product launch",
        "new collection",
        "campaign",
        "commercial",
        "content creation",
        "creative testing",
        "ugc",
    ),
    "photo_video_services": (
        "video production",
        "photo production",
        "photography",
        "photographer",
        "videography",
        "videographer",
        "post-production",
        "post production",
        "video editing",
        "motion design",
        "motion graphics",
        "animation studio",
        "3d animation",
        "cgi",
        "vfx",
        "product photography",
        "commercial photography",
        "fashion photography",
        "food photography",
        "lifestyle photography",
        "content studio",
        "creative production",
        "performance creative",
        "paid social creative",
        "ugc production",
    ),
    "placement_demand": (
        "freelance",
        "contractor",
        "contract role",
        "talent roster",
        "creator network",
        "production partner",
        "white label",
        "white-label",
        "overflow production",
        "retained production",
        "creative strategist",
        "video editor",
        "motion designer",
        "art director",
    ),
    "paid_distribution": (
        "paid social",
        "paid media",
        "media buying",
        "performance marketing",
        "customer acquisition",
        "conversion rate",
        "roas",
        "google ads",
        "meta ads",
        "tiktok ads",
    ),
    "scale": (
        "global",
        "international",
        "locations",
        "our team",
        "retailers",
        "stockists",
        "wholesale",
        "enterprise",
        "award-winning",
    ),
    "recurring": (
        "monthly",
        "subscription",
        "always-on",
        "ongoing",
        "retainer",
        "weekly",
        "membership",
    ),
    "hiring": ("we're hiring", "we are hiring", "join our team", "open positions", "careers"),
    "creator_education": (
        "course",
        "academy",
        "cohort",
        "enroll",
        "curriculum",
        "community",
        "membership",
    ),
    "parked": (
        "domain is for sale",
        "buy this domain",
        "this domain may be for sale",
        "make an offer for this domain",
        "domain parking",
    ),
}


# These signals are deliberately more conservative than the broad keyword families above.
# Each match is retained with a short first-party page excerpt and its source URL so later
# qualification can distinguish an observed phrase from an inference. The subtype prefix also
# prevents an agency offering overflow capacity from being mistaken for a company admitting its
# own production backlog.
CONTEXTUAL_SIGNAL_RULES: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "production_pressure": (
        (
            "capacity_shortfall",
            re.compile(
                r"\b(?:we(?:'re| are) at capacity|our (?:production|creative|content) "
                r"capacity is (?:full|limited)|not enough (?:production|creative|content) "
                r"capacity|limited (?:production|creative|content) bandwidth|"
                r"(?:production|creative|content) capacity constraints?)\b",
                re.I,
            ),
        ),
        (
            "backlog",
            re.compile(
                r"\b(?:our (?:production|creative|content) backlog|we (?:have|face|carry) "
                r"(?:a )?(?:production|creative|content) backlog|our backlog of "
                r"(?:production|creative|content) (?:work|requests?|projects?)|"
                r"(?:production|creative|content) bottlenecks?)\b",
                re.I,
            ),
        ),
        (
            "throughput_pressure",
            re.compile(
                r"\b(?:struggl(?:e|es|ing) to keep up|cannot keep up|can't keep up|"
                r"demand (?:is )?outpac(?:es|ing) (?:our )?(?:team|production|capacity)|"
                r"more (?:creative|content|assets?) than (?:our )?team can produce)\b",
                re.I,
            ),
        ),
        (
            "outsourcing",
            re.compile(
                r"\b(?:we (?:outsource|outsourced|are outsourcing|have outsourced)|"
                r"we're outsourcing|our outsourced (?:production|creative|content)|our "
                r"(?:production|creative|content) is outsourced|we (?:use|rely on|work with) "
                r"(?:an? )?(?:external|outside) (?:production|creative|content) "
                r"(?:team|studio|partner))\b",
                re.I,
            ),
        ),
        (
            "external_capacity_offer",
            re.compile(
                r"\b(?:overflow production (?:partner|support|capacity)|white[- ]label "
                r"(?:creative |content )?production|on[- ]demand (?:creative |content )?"
                r"production capacity|surge production capacity|extra production capacity|"
                r"external production partner)\b",
                re.I,
            ),
        ),
    ),
    "production_capacity": (
        (
            "in_house_team",
            re.compile(
                r"\b(?:our in[- ]house (?:creative|content|production|photo|photography|video) "
                r"(?:team|studio|department)|our internal (?:creative|content|production|photo|"
                r"photography|video) (?:team|studio|department)|we have (?:an? )?(?:in[- ]house|"
                r"internal) (?:creative|content|production|photo|photography|video) "
                r"(?:team|studio|department)|in[- ]house (?:creative|content|production|photo|"
                r"photography|video) (?:team|studio|department)|our (?:creative|content|"
                r"production) team)\b",
                re.I,
            ),
        ),
        (
            "staffed_production",
            re.compile(
                r"\b(?:our (?:staff|full[- ]time|dedicated) (?:photographers?|videographers?|"
                r"editors?|producers?|animators?)|staff (?:photographers?|videographers?|"
                r"editors?|producers?)|team of (?:photographers?|videographers?|editors?|"
                r"producers?|animators?))\b",
                re.I,
            ),
        ),
        (
            "owned_studio",
            re.compile(
                r"\b(?:our (?:own )?(?:photo|photography|video|content|creative|production) "
                r"studio|we (?:shoot|film|edit|produce|create) (?:it |everything )?in[- ]house|"
                r"full[- ]service (?:photo|photography|video|content|creative|production) "
                r"studio)\b",
                re.I,
            ),
        ),
    ),
    "ai_workflow": (
        (
            "generative_ai",
            re.compile(
                r"\b(?:generative AI|AI[- ]generated (?:images?|imagery|photos?|videos?|"
                r"creative|content)|synthetic (?:media|imagery|content)|AI (?:image|photo|video) "
                r"generation)\b",
                re.I,
            ),
        ),
        (
            "ai_enabled_workflow",
            re.compile(
                r"\b(?:AI[- ](?:powered|enabled|assisted) (?:creative|content|production|"
                r"workflow|platform|studio|editing|retouching|post[- ]production|localization|"
                r"image generation|video generation)|our AI (?:workflow|pipeline|platform)|"
                r"we (?:use|integrate|deploy|build with) (?:generative )?AI|"
                r"human[- ]in[- ]the[- ]loop)\b",
                re.I,
            ),
        ),
        (
            "ai_production_tool",
            re.compile(
                r"\b(?:Adobe Firefly|Midjourney|Stable Diffusion|ComfyUI|RunwayML|"
                r"Runway (?:AI|Gen[- ]?\d|video model)|OpenAI Sora|FLUX\.1|digital twins?|"
                r"virtual production)\b",
                re.I,
            ),
        ),
    ),
    "rights_risk": (
        (
            "usage_rights",
            re.compile(
                r"\b(?:usage rights?|commercial rights?|content licensing|licensed (?:assets?|"
                r"content|imagery)|copyright clearance|intellectual property rights?|"
                r"IP ownership)\b",
                re.I,
            ),
        ),
        (
            "likeness_consent",
            re.compile(
                r"\b(?:model releases?|talent releases?|likeness (?:rights?|consent|"
                r"permission)|consent for (?:a |the )?(?:person's |talent |model )?likeness|"
                r"facial likeness|voice likeness|biometric (?:data|consent)|identity consent|"
                r"right of publicity|publicity rights?)\b",
                re.I,
            ),
        ),
        (
            "ai_restriction",
            re.compile(
                r"\b(?:(?:AI|artificial intelligence|synthetic media|deepfakes?|AI[- ]generated "
                r"content).{0,70}(?:prohibited|restricted|not permitted|not allowed|banned|"
                r"requires? (?:approval|disclosure|consent))|(?:prohibit|restrict|ban|do not "
                r"permit|do not allow|don't permit|don't allow|does not permit|does not allow)"
                r".{0,70}(?:AI|artificial intelligence|synthetic media|deepfakes?|"
                r"AI[- ]generated content))\b",
                re.I,
            ),
        ),
        (
            "authenticity_fidelity",
            re.compile(
                r"\b(?:brand safety|product fidelity|visual fidelity|authenticity standards?|"
                r"AI disclosure|synthetic media disclosure|content provenance|watermark(?:ing)?|"
                r"C2PA)\b",
                re.I,
            ),
        ),
    ),
    "business_model": (
        (
            "subscription_membership",
            re.compile(
                r"\b(?:subscription[- ]based (?:brand|business|service|platform)|"
                r"subscription (?:brand|business|service|platform)|monthly membership|"
                r"paid membership|subscribe (?:and|&) save)\b",
                re.I,
            ),
        ),
        (
            "direct_to_consumer",
            re.compile(
                r"\b(?:direct[- ]to[- ]consumer (?:brand|business|company)|DTC brand|"
                r"e[- ]?commerce (?:brand|store|business)|online retail brand)\b",
                re.I,
            ),
        ),
        (
            "agency_studio",
            re.compile(
                r"\b(?:(?:creative|marketing|advertising|performance|production) agency|"
                r"(?:content|creative|production|photography|video) studio)\b",
                re.I,
            ),
        ),
        (
            "software_platform",
            re.compile(
                r"\b(?:software[- ]as[- ]a[- ]service|SaaS (?:platform|business|company)|"
                r"(?:we are|we're|our) (?:an? )?(?:software platform|marketplace)|"
                r"B2B (?:platform|software|service|company))\b",
                re.I,
            ),
        ),
        (
            "app_gaming",
            re.compile(
                r"\b(?:(?:consumer|mobile) app|(?:game|gaming) studio|mobile game "
                r"(?:developer|publisher))\b",
                re.I,
            ),
        ),
    ),
    "creative_use_cases": (
        (
            "campaign_launch",
            re.compile(
                r"\b(?:product launches?|collection launches?|campaign creative|launch "
                r"creative|seasonal campaigns?|always[- ]on campaigns?)\b",
                re.I,
            ),
        ),
        (
            "performance_marketing",
            re.compile(
                r"\b(?:performance creative|paid social creative|ad creative testing|"
                r"creative testing|conversion creative|direct[- ]response creative)\b",
                re.I,
            ),
        ),
        (
            "commerce_content",
            re.compile(
                r"\b(?:e[- ]?commerce content|product detail page (?:content|imagery)|"
                r"PDP (?:content|imagery)|marketplace content|retail content|email creative|"
                r"landing page creative)\b",
                re.I,
            ),
        ),
        (
            "brand_content",
            re.compile(
                r"\b(?:brand campaigns?|social content|creator content|branded content|"
                r"editorial content|customer testimonial videos?)\b",
                re.I,
            ),
        ),
    ),
    "localization": (
        (
            "localized_content",
            re.compile(
                r"\b(?:locali[sz](?:e|ed|ing|ation) (?:content|creative|campaigns?|assets?|"
                r"imagery|videos?)|transcreation|market[- ]specific (?:content|creative|"
                r"campaigns?|assets?))\b",
                re.I,
            ),
        ),
        (
            "multilingual",
            re.compile(
                r"\b(?:(?:available|published|produced|delivered|supported) in \d{1,3} "
                r"languages?|multi[- ]?lingual (?:content|creative|campaigns?|assets?)|"
                r"multi[- ]language (?:content|creative|campaigns?|assets?))\b",
                re.I,
            ),
        ),
        (
            "multi_market",
            re.compile(
                r"\b(?:multi[- ]market (?:content|creative|campaigns?|production)|"
                r"(?:across|serving|operating in|available in) \d{1,3} (?:markets?|countries|"
                r"regions)|global and local markets?|international markets?|"
                r"regionalized campaigns?)\b",
                re.I,
            ),
        ),
    ),
    "creative_formats": (
        (
            "video",
            re.compile(
                r"\b(?:video production|video content|brand films?|commercial videos?|"
                r"campaign videos?|motion content)\b",
                re.I,
            ),
        ),
        (
            "image_photo",
            re.compile(
                r"\b(?:photo(?:graphy)? production|brand photography|commercial photography|"
                r"campaign photography|still imagery|image production)\b",
                re.I,
            ),
        ),
        (
            "vertical_short_form",
            re.compile(
                r"\b(?:vertical videos?|9\s*:\s*16|short[- ]form videos?|vertical[- ]first "
                r"content)\b",
                re.I,
            ),
        ),
        (
            "social",
            re.compile(
                r"\b(?:social[- ]first (?:content|creative|video)|social videos?|paid social "
                r"creative|Instagram Reels?|TikTok videos?|social stories)\b",
                re.I,
            ),
        ),
        (
            "ugc_creator",
            re.compile(
                r"\b(?:UGC(?: production| creative| videos?| content)?|user[- ]generated "
                r"content|creator[- ]led content|creator content)\b",
                re.I,
            ),
        ),
        (
            "product",
            re.compile(
                r"\b(?:product photography|product videos?|product imagery|packshots?|"
                r"e[- ]?commerce photography|on[- ]white photography)\b",
                re.I,
            ),
        ),
        (
            "lifestyle",
            re.compile(
                r"\b(?:lifestyle photography|lifestyle videos?|lifestyle imagery|on[- ]model "
                r"photography|in[- ]use imagery)\b",
                re.I,
            ),
        ),
    ),
    "buyer_roles": (
        (
            "marketing_leader",
            re.compile(
                r"\b(?:chief marketing officer|CMO|VP (?:of )?marketing|vice president of "
                r"marketing|head of marketing|marketing director|growth director|head of "
                r"growth|performance marketing director)\b",
                re.I,
            ),
        ),
        (
            "brand_creative_leader",
            re.compile(
                r"\b(?:chief creative officer|creative director|VP (?:of )?creative|head of "
                r"creative|brand director|head of brand|content director|head of content)\b",
                re.I,
            ),
        ),
        (
            "production_leader",
            re.compile(
                r"\b(?:head of production|production director|executive producer|studio "
                r"director|creative operations director|head of creative operations)\b",
                re.I,
            ),
        ),
        (
            "commerce_social_leader",
            re.compile(
                r"\b(?:e[- ]?commerce director|head of e[- ]?commerce|social media director|"
                r"head of social|paid social lead|media director)\b",
                re.I,
            ),
        ),
        (
            "owner_executive",
            re.compile(
                r"\b(?:founder and (?:CEO|creative director)|agency (?:founder|owner|principal)|"
                r"managing director|studio owner)\b",
                re.I,
            ),
        ),
    ),
}

_CONTEXT_BLOCK_TAGS = (
    "h1",
    "h2",
    "h3",
    "h4",
    "p",
    "li",
    "dt",
    "dd",
    "figcaption",
    "td",
    "th",
)
_CONTEXT_SKIP_ANCESTORS = {"nav", "footer", "header", "script", "style", "noscript", "template"}
_THIRD_PARTY_CONTEXT_TOKENS = (
    "case-study",
    "case_study",
    "case study",
    "customer-story",
    "customer_story",
    "client-story",
    "success-story",
)
_QUOTED_CONTEXT_TOKENS = ("testimonial", "quote", "review")
_CASE_PAGE_WORDS = (
    "case-stud",
    "customer-stor",
    "success-stor",
    "client-stor",
    "/portfolio/",
    "/work/",
)
_SELF_ATTRIBUTION_RE = re.compile(
    r"\b(?:we(?:'re|'ve|'ll| are| have| use| build| create| produce| deliver| operate| run| "
    r"work| support| scale)?|our (?:team|studio|agency|company|business|brand|platform|workflow|"
    r"process|production|creative|content|photographers?|videographers?|editors?|producers?))\b",
    re.I,
)
_THIRD_PARTY_SUBJECT_RE = re.compile(
    r"\b(?:(?:our|the|a) (?:clients?|customers?|partners?|brands?)(?:'s|s')?|"
    r"(?:client|customer|partner) team|they|their (?:team|studio|company|brand|business))\b",
    re.I,
)
_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|without|cannot|can't|do not|don't|does not|doesn't|"
    r"no longer|former(?:ly)?|avoid(?:s|ed|ing)?|eliminat(?:e|es|ed|ing)|"
    r"prevent(?:s|ed|ing)?)\b",
    re.I,
)
_POST_NEGATION_RE = re.compile(
    r"^.{0,55}\b(?:is|are|was|were|has been|have been)?\s*(?:prohibited|restricted|"
    r"not permitted|not allowed|banned|eliminated|cleared|resolved|no longer used)\b",
    re.I,
)
_HIRING_CONTEXT_RE = re.compile(
    r"\b(?:we(?:'re| are) hiring|apply (?:now|today)|job opening|open role|open position|"
    r"job description|the successful candidate|responsibilities include)\b",
    re.I,
)
_ROLE_OWNER_CONTEXT_RE = re.compile(
    r"\b(?:our|meet (?:our|the)|led by|leadership|management team|talk to|speak with|"
    r"contact (?:our|the))\b",
    re.I,
)


def _context_attributes(node: Tag) -> str:
    values: list[str] = []
    for current in (node, *node.parents):
        if not isinstance(current, Tag):
            continue
        values.append(str(current.get("id") or ""))
        classes = current.get("class") or []
        values.extend(str(value) for value in classes if value)
        for attribute in ("data-testid", "data-component", "aria-label"):
            values.append(str(current.get(attribute) or ""))
        if current.name in {"main", "body", "html"}:
            break
    return " ".join(values).casefold()


def _context_blocks(
    soup: BeautifulSoup,
    *,
    title: str,
    description: str,
) -> list[tuple[str, bool, bool]]:
    """Return bounded visible blocks with third-party and team-context flags."""

    blocks: list[tuple[str, bool, bool]] = []
    seen: set[str] = set()

    def append(text: str, *, third_party: bool, team_context: bool) -> None:
        normalized = re.sub(r"\s+", " ", text).strip()
        if not 4 <= len(normalized) <= 800:
            return
        key = normalized.casefold()
        if key in seen:
            return
        seen.add(key)
        blocks.append((normalized, third_party, team_context))

    for metadata in (title, description):
        append(metadata, third_party=False, team_context=False)

    candidates = list(soup.find_all(_CONTEXT_BLOCK_TAGS))
    candidates.extend(
        node
        for node in soup.find_all(("div", "section", "article", "main", "body"))
        if not node.find(_CONTEXT_BLOCK_TAGS)
    )
    for node in candidates[:1_500]:
        if not isinstance(node, Tag):
            continue
        ancestors = [parent for parent in node.parents if isinstance(parent, Tag)]
        if node.name in _CONTEXT_SKIP_ANCESTORS or any(
            parent.name in _CONTEXT_SKIP_ANCESTORS for parent in ancestors
        ):
            continue
        if node.find_parent(("blockquote", "q")) is not None or node.find(
            ("blockquote", "q")
        ) is not None:
            continue
        if str(node.get("aria-hidden") or "").casefold() == "true":
            continue
        attributes = _context_attributes(node)
        if any(token in attributes for token in _QUOTED_CONTEXT_TOKENS):
            continue
        third_party = any(token in attributes for token in _THIRD_PARTY_CONTEXT_TOKENS)
        team_context = any(
            token in attributes
            for token in ("team", "people", "leadership", "management", "about-us")
        )
        append(
            node.get_text(" ", strip=True),
            third_party=third_party,
            team_context=team_context,
        )
    return blocks[:500]


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|[\r\n]+", text)
    return [part.strip() for part in parts if 4 <= len(part.strip()) <= 600]


def _is_negated(text: str, start: int, end: int) -> bool:
    return bool(
        _NEGATION_RE.search(text[max(0, start - 55) : start])
        or _POST_NEGATION_RE.search(text[end : end + 80])
    )


def _is_third_party_attribution(text: str, start: int) -> bool:
    prefix = text[max(0, start - 220) : start]
    third_party = list(_THIRD_PARTY_SUBJECT_RE.finditer(prefix))
    if not third_party:
        return False
    last_third_party_end = third_party[-1].end()
    return not any(
        match.start() >= last_third_party_end for match in _SELF_ATTRIBUTION_RE.finditer(prefix)
    )


def _contextual_evidence_value(
    subtype: str,
    match: re.Match[str],
    sentence: str,
    url: str,
) -> str:
    phrase = re.sub(r"\s+", " ", match.group(0)).strip().casefold()
    excerpt = re.sub(r"\s+", " ", sentence).strip()
    if len(excerpt) > 280:
        excerpt = excerpt[:277].rstrip() + "..."
    return f"{subtype}: {phrase} | {excerpt} | source={url}"


def _contextual_page_signals(
    soup: BeautifulSoup,
    *,
    title: str,
    description: str,
    url: str,
) -> dict[str, list[str]]:
    """Extract page-scoped, attributed business and creative-capacity evidence."""

    path = urlparse(url).path.casefold()
    headline = " ".join([title, *(node.get_text(" ", strip=True)
                                  for node in soup.find_all("h1", limit=3))])
    case_page = any(word in path for word in _CASE_PAGE_WORDS) or bool(
        re.search(r"/(?:portfolio|work)(?:/|$)", path)
        or re.search(r"\b(?:case stud(?:y|ies)|customer stor(?:y|ies)|client stor(?:y|ies))\b",
                     headline, re.I)
    )
    team_page = any(
        word in path for word in ("/team", "/people", "/leadership", "/management", "/about")
    )
    found: dict[str, list[str]] = defaultdict(list)
    for block, third_party_container, team_container in _context_blocks(
        soup,
        title=title,
        description=description,
    ):
        for sentence in _sentences(block):
            for category, rules in CONTEXTUAL_SIGNAL_RULES.items():
                for subtype, pattern in rules:
                    for match in pattern.finditer(sentence):
                        self_attributed = bool(_SELF_ATTRIBUTION_RE.search(sentence[: match.end()]))
                        if _is_third_party_attribution(sentence, match.start()):
                            continue
                        if (third_party_container or case_page) and not self_attributed:
                            continue
                        negated = _is_negated(sentence, match.start(), match.end())
                        explicit_shortfall = subtype == "capacity_shortfall" and match.group(
                            0
                        ).casefold().startswith("not enough")
                        if category != "rights_risk" and negated and not explicit_shortfall:
                            continue
                        if category == "buyer_roles":
                            if _HIRING_CONTEXT_RE.search(sentence):
                                continue
                            role_owned = bool(
                                team_page
                                or team_container
                                or _ROLE_OWNER_CONTEXT_RE.search(sentence[: match.end()])
                            )
                            if not role_owned:
                                continue
                        value = _contextual_evidence_value(
                            subtype,
                            match,
                            sentence,
                            url,
                        )
                        if value not in found[category]:
                            found[category].append(value)
                        if len(found[category]) >= 12:
                            break
                    if len(found[category]) >= 12:
                        break
    return {category: values for category, values in found.items() if values}


class UnsafeTargetError(RuntimeError):
    pass


async def _ensure_public_host(hostname: str) -> None:
    require_allowed(hostname)
    try:
        records = await asyncio.to_thread(
            socket.getaddrinfo, hostname, None, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise UnsafeTargetError(f"DNS resolution failed: {exc}") from exc
    addresses = {record[4][0] for record in records}
    if not addresses:
        raise UnsafeTargetError("DNS resolution returned no addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise UnsafeTargetError(f"Refusing non-public address: {address}")


def _ensure_public_network_stream(stream: object) -> None:
    get_extra_info = getattr(stream, "get_extra_info", None)
    if not callable(get_extra_info):
        raise UnsafeTargetError("Connected peer address is unavailable")
    try:
        server_address = get_extra_info("server_addr")
    except Exception as exc:
        raise UnsafeTargetError("Connected peer address is unavailable") from exc
    if not isinstance(server_address, (list, tuple)) or not server_address:
        raise UnsafeTargetError("Connected peer address is unavailable")
    raw_address = server_address[0]
    if isinstance(raw_address, bytes):
        try:
            raw_address = raw_address.decode("ascii")
        except UnicodeDecodeError as exc:
            raise UnsafeTargetError("Connected peer address is invalid") from exc
    try:
        address = str(raw_address).split("%", 1)[0]
        ip = ipaddress.ip_address(address)
    except ValueError as exc:
        raise UnsafeTargetError("Connected peer address is invalid") from exc
    if not ip.is_global:
        raise UnsafeTargetError(f"Refusing non-public connected peer: {address}")


async def _trace_public_connection(event: str, info: dict[str, object]) -> None:
    if not event.endswith(".connect_tcp.complete"):
        return
    stream = info.get("return_value")
    try:
        _ensure_public_network_stream(stream)
    except UnsafeTargetError:
        # DNS validation alone has a rebinding race. Close the socket before HTTP bytes
        # are sent when the address actually selected by the transport is not public.
        close = getattr(stream, "aclose", None)
        if callable(close):
            await close()
        raise


def _ensure_response_peer(
    client: httpx.AsyncClient,
    response: httpx.Response,
) -> None:
    stream = response.extensions.get("network_stream")
    if stream is None and isinstance(getattr(client, "_transport", None), httpx.MockTransport):
        # MockTransport never opens a socket and therefore has no peer to validate.
        return
    _ensure_public_network_stream(stream)


@bounded_public_request
async def _bounded_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    max_redirects: int = 5,
    accept: str = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
    allowed_content_types: tuple[str, ...] = ("html", "text/plain"),
    redirect_validator: Callable[[str, str], Awaitable[None]] | None = None,
    request_headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
    before_dispatch: Callable[[], Awaitable[None]] | None = None,
) -> tuple[httpx.Response, bytes]:
    current = url
    for _ in range(max_redirects + 1):
        require_allowed(current)
        parsed = urlparse(current)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise UnsafeTargetError(f"Unsupported URL: {current}")
        if parsed.username or parsed.password:
            raise UnsafeTargetError("Refusing URL containing credentials")
        if parsed.port not in {None, 80, 443}:
            raise UnsafeTargetError(f"Refusing non-web port: {parsed.port}")
        notify_request_activity("dns", current)
        try:
            await _ensure_public_host(parsed.hostname)
        except asyncio.CancelledError:
            notify_request_activity("cancelled", current)
            raise
        finally:
            notify_request_activity("idle", current)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": accept,
            **dict(request_headers or {}),
        }
        request_options: dict[str, object] = {}
        if timeout is not None:
            request_options["timeout"] = timeout
        async with provider_stream(client,
            "GET",
            current,
            headers=headers,
            follow_redirects=False,
            extensions={"trace": _trace_public_connection},
            before_dispatch=before_dispatch,
            **request_options,
        ) as response:
            # This also covers a reused connection, where no connect trace event fires.
            _ensure_response_peer(client, response)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    return response, b""
                redirected = urljoin(str(response.url), location)
            else:
                content_type = response.headers.get("content-type", "").lower()
                if content_type and not any(
                    allowed in content_type for allowed in allowed_content_types
                ):
                    return response, b""
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    remaining = max_bytes - len(chunks)
                    if remaining <= 0:
                        response.extensions["beings_body_truncated"] = True
                        break
                    if len(chunk) > remaining:
                        response.extensions["beings_body_truncated"] = True
                    chunks.extend(chunk[:remaining])
                return response, bytes(chunks)
        # A redirect policy may fetch robots.txt through this same pool.
        if redirect_validator is not None:
            await redirect_validator(current, redirected)
        current = redirected
    raise httpx.TooManyRedirects(f"More than {max_redirects} redirects for {url}")


def _is_certificate_validation_error(error: BaseException | str) -> bool:
    detail = str(error).lower()
    return "certificate_verify_failed" in detail or "certificate verify failed" in detail


def _retry_after_seconds(
    response: httpx.Response,
    *,
    now: datetime | None = None,
) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        return max(0.0, (retry_at - current).total_seconds())


def _root_retry_delay(
    response: httpx.Response | None,
    *,
    hostname: str,
    attempt: int,
) -> float | None:
    retry_after = _retry_after_seconds(response) if response is not None else 0.0
    if retry_after is not None and retry_after > ROOT_RETRY_AFTER_MAX_SECONDS:
        return None
    digest = hashlib.sha256(f"{hostname}:{attempt}".encode()).digest()
    jitter = digest[0] / 2_550
    backoff = min(0.25 * (2**attempt) + jitter, ROOT_RETRY_AFTER_MAX_SECONDS)
    return max(backoff, retry_after or 0.0)


def _retryable_root_exception(exc: BaseException) -> bool:
    if isinstance(exc, (UnsafeTargetError, httpx.PoolTimeout)) or (
        _is_certificate_validation_error(exc)
    ):
        return False
    return isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            TimeoutError,
        ),
    )


def microlink_fallback_eligible(
    response: httpx.Response | None,
    error: BaseException | None,
) -> bool:
    if response is not None:
        return response.status_code in MICROLINK_ELIGIBLE_STATUS_CODES
    return error is not None and _retryable_root_exception(error)


async def fetch_microlink_rendered_html(
    client: httpx.AsyncClient,
    target_url: str,
    *,
    api_key: str,
    max_bytes: int,
) -> tuple[str, bytes, bool]:
    """Fetch one rendered public page through Microlink's managed proxy."""

    parsed_target = urlparse(target_url)
    if (
        parsed_target.scheme not in {"http", "https"}
        or not parsed_target.hostname
        or parsed_target.username
        or parsed_target.password
        or parsed_target.port not in {None, 80, 443}
    ):
        raise UnsafeTargetError("Microlink fallback target is not a public web URL")
    await _ensure_public_host(parsed_target.hostname)
    query = httpx.QueryParams(
        {
            "url": target_url,
            "data.html.attr": "html",
            "data.html.selector": "html",
            "prerender": "true",
            "meta": "false",
            "ttl": "30d",
            "staleTtl": "0",
        }
    )
    response, body = await _bounded_get(
        client,
        f"{MICROLINK_API_URL}?{query}",
        # JSON escaping can use six bytes for one byte of rendered HTML. Bound the
        # envelope separately; truncating it before parsing destroys valid JSON.
        max_bytes=6 * max_bytes + MICROLINK_RESPONSE_OVERHEAD_BYTES,
        accept="application/json",
        allowed_content_types=("application/json", "text/json"),
        request_headers={"x-api-key": api_key},
        timeout=45.0,
    )
    if not 200 <= response.status_code < 300:
        # Retain diagnostic codes without echoing provider URLs, headers or keys.
        diagnostic = ""
        try:
            error_payload = json.loads(body)
            if isinstance(error_payload, dict):
                code = str(error_payload.get("code") or "")
                if re.fullmatch(r"[A-Z][A-Z0-9_]{1,80}", code):
                    diagnostic = f" ({code})"
                description = str(
                    error_payload.get("message") or error_payload.get("description") or ""
                )
                validation_errors = error_payload.get("errors") or error_payload.get("data")
                if isinstance(validation_errors, dict):
                    validation_errors = [
                        {"field": field, "detail": detail}
                        for field, detail in list(validation_errors.items())[:5]
                    ]
                if isinstance(validation_errors, list) and validation_errors:
                    description += " | validation: " + json.dumps(
                        validation_errors[:5], ensure_ascii=True, default=str
                    )
                if description:
                    description = description.replace(api_key, "[redacted]")
                    description = re.sub(r"https?://\S+", "[URL]", description)
                    diagnostic += ": " + " ".join(description.split())[:800]
        except (ValueError, UnicodeDecodeError):
            pass
        raise httpx.HTTPStatusError(
            f"Microlink HTTP {response.status_code}{diagnostic}",
            request=response.request,
            response=response,
        )
    content_type = response.headers.get("content-type", "").lower()
    if content_type and not any(kind in content_type for kind in ("application/json", "text/json")):
        raise ValueError("Microlink returned an unexpected response content type")
    if response.extensions.get("beings_body_truncated"):
        raise ValueError("Microlink JSON response exceeded the bounded envelope limit")
    if not body.strip():
        raise ValueError("Microlink returned an empty JSON response")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Microlink returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") not in {None, "success"}:
        code = (
            str(payload.get("code") or "provider_error")
            if isinstance(payload, dict)
            else "provider_error"
        )
        raise ValueError(f"Microlink {code}"[:200])
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Microlink response did not include data")
    html = data.get("html")
    if not isinstance(html, str) or not html.strip():
        raise ValueError("Microlink response did not include rendered HTML")
    encoded = html.encode("utf-8")
    if len(encoded) > max_bytes:
        encoded = encoded[:max_bytes]
    final_url = str(data.get("url") or target_url).strip()
    parsed_final = urlparse(final_url)
    if (
        parsed_final.scheme not in {"http", "https"}
        or not parsed_final.hostname
        or parsed_final.username
        or parsed_final.password
        or parsed_final.port not in {None, 80, 443}
    ):
        raise UnsafeTargetError("Microlink returned an unsafe final URL")
    await _ensure_public_host(parsed_final.hostname)
    fetch_mode = response.headers.get("x-fetch-mode", "").casefold()
    return final_url, encoded, "proxy" in fetch_mode or "residential" in fetch_mode


def _root_host_variants(candidate: DiscoveredHost) -> tuple[str, ...]:
    hostname = candidate.hostname.strip().lower().rstrip(".")
    if is_platform_tenant(candidate):
        return (hostname,)
    extracted = _APPEARANCE_TLD_EXTRACT(hostname)
    if extracted.subdomain not in {"", "www"} or not extracted.domain or not extracted.suffix:
        return (hostname,)
    registrable = f"{extracted.domain}.{extracted.suffix}"
    if hostname == f"www.{registrable}":
        return (hostname, registrable)
    if hostname != registrable:
        return (hostname,)
    return (hostname, f"www.{hostname}")


def _jsonld_nodes(value: object, *, depth: int = 0):
    if depth > 12:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _jsonld_nodes(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value[:500]:
            yield from _jsonld_nodes(child, depth=depth + 1)


def _positive_int(value: object) -> int:
    try:
        return max(0, int(float(str(value).replace(",", ""))))
    except (TypeError, ValueError):
        return 0


def _ats_boards_from_url_elements(
    soup: BeautifulSoup,
    page_url: str,
) -> dict[str, list[str]]:
    """Extract ATS tenant ids only from URL-bearing elements on the public page.

    Searching serialized HTML would let comments, prose, JSON examples, and escaped tracking
    parameters manufacture an ATS relationship. Restrict discovery to navigational/embed URLs and
    require the URL's actual host to be one of the supported public ATS hosts.
    """

    candidates: list[str] = []
    for node in soup.find_all(["a", "link", "iframe", "script"], limit=1_500):
        attribute = "href" if node.name in {"a", "link"} else "src"
        raw_url = str(node.get(attribute) or "").strip()
        if not raw_url:
            continue
        absolute = urljoin(page_url, raw_url)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        candidates.append(absolute)

    boards: dict[str, list[str]] = {}
    for provider, pattern in ATS_PATTERNS.items():
        provider_hosts = ATS_PROVIDER_HOSTS[provider]
        matches: list[str] = []
        for candidate in candidates:
            host = (urlparse(candidate).hostname or "").casefold().rstrip(".")
            if host not in provider_hosts:
                continue
            match = pattern.search(candidate)
            if match:
                matches.append(match.group(1).casefold())
        if matches:
            boards[provider] = list(dict.fromkeys(matches))[:3]
    return boards


def _clean_identity_label(value: str) -> str:
    label = re.sub(r"\s+", " ", value).strip(" \t\r\n-|\u00b7:;")
    compact = _compact_company_identity(label)
    if (
        not 2 <= len(label) <= 100
        or not compact
        or not any(character.isalpha() for character in label)
        or label.casefold() in _APPEARANCE_GENERIC_LABELS
        or "://" in label
        or label.count("/") > 1
    ):
        return ""
    return label


def _clean_anchor_subject_label(value: str) -> str:
    label = re.sub(
        r"^(?:visit|view|meet|read about|learn about)\s+",
        "",
        value.strip(),
        flags=re.I,
    )
    if re.search(
        r"\b(?:browse|explore|full|our|see|view)\s+(?:our\s+)?(?:full\s+)?"
        r"(?:case studies|clients?|portfolio|work)\b",
        label,
        re.I,
    ):
        return ""
    label = re.sub(
        r"\s+(?:case study|customer story|success story|client story|profile)$",
        "",
        label,
        flags=re.I,
    )
    return _clean_identity_label(label)


def _appearance_domain(value: str) -> str:
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").strip().casefold().rstrip(".")
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username
        or parsed.password
        or port not in {None, 80, 443}
    ):
        return ""
    try:
        host = host.encode("idna").decode("ascii").removeprefix("www.")
        ipaddress.ip_address(host)
    except UnicodeError:
        return ""
    except ValueError:
        pass
    else:
        return ""
    if not _APPEARANCE_HOST_RE.fullmatch(host):
        return ""
    if host.split(".", 1)[0] in {"asset", "assets", "cdn", "image", "images", "img", "static"}:
        return ""
    extracted = _APPEARANCE_TLD_EXTRACT(host)
    registrable = (
        f"{extracted.domain}.{extracted.suffix}".casefold()
        if extracted.domain and extracted.suffix
        else host
    )
    if registrable in _APPEARANCE_NON_AUTHORITATIVE_APEXES:
        return ""
    if any(
        registrable == blocked or registrable.endswith(f".{blocked}")
        for blocked in NON_CLIENT_HOSTS
    ):
        return ""
    # A deep link can live on a company's own marketing subdomain, but the registrable domain is
    # the strongest ownership claim available from public DNS alone. Never promote the arbitrary
    # subdomain itself as a distinct company.
    return registrable


def _canonical_appearance_url(value: str, *, base_url: str) -> str:
    raw = value.strip()
    if not raw or raw.casefold().startswith(("data:", "blob:", "javascript:")):
        return ""
    absolute = urljoin(base_url, raw)
    try:
        parsed = urlparse(absolute)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in {None, 80, 443}
    ):
        return ""
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))[
        :2_000
    ]


def _srcset_urls(value: str) -> list[str]:
    if value.lstrip().casefold().startswith("data:"):
        return []
    urls: list[str] = []
    for item in value.split(",")[: MAX_APPEARANCE_ASSETS_PER_NODE * 2]:
        candidate = item.strip()
        if not candidate or candidate.casefold().startswith("data:"):
            continue
        parts = candidate.rsplit(maxsplit=1)
        if len(parts) == 2 and re.fullmatch(r"(?:\d+(?:\.\d+)?x|\d+w)", parts[1], re.I):
            candidate = parts[0]
        urls.append(candidate)
    return urls


def _iter_image_asset_urls(node: Tag, *, page_url: str) -> tuple[str, ...]:
    """Return bounded public asset references without fetching any image bytes."""

    nodes = [node]
    if node.name == "img":
        picture = node.find_parent("picture")
        if picture is not None:
            nodes.extend(picture.find_all("source", limit=8))
    found: list[str] = []
    for candidate_node in nodes:
        attributes = _APPEARANCE_IMAGE_ATTRIBUTES
        if candidate_node.name == "image":
            attributes = ("href", "xlink:href", "data-src")
        for attribute in attributes:
            raw = str(candidate_node.get(attribute) or "").strip()
            if not raw:
                continue
            values = _srcset_urls(raw) if "srcset" in attribute else [raw]
            for value in values:
                absolute = _canonical_appearance_url(value, base_url=page_url)
                if absolute and absolute not in found:
                    found.append(absolute)
                if len(found) >= MAX_APPEARANCE_ASSETS_PER_NODE:
                    return tuple(found)
    return tuple(found)


def _filename_identity_hint(asset_url: str) -> str:
    try:
        filename = unquote(urlparse(asset_url).path.rsplit("/", 1)[-1])
    except ValueError:
        return ""
    suffix = re.search(r"\.(?:avif|gif|ico|jpe?g|png|svg|webp)$", filename, re.I)
    if suffix is None:
        return ""
    stem = filename[: suffix.start()]
    if not stem or _APPEARANCE_ASSET_REJECT_RE.search(stem):
        return ""
    stem = re.sub(r"^[0-9a-f]{16,64}[-_]+", "", stem, flags=re.I)
    stem = re.sub(
        r"(?:^|[-_])id[-_]?[a-z0-9]{6,}(?:[-_]\d+)?$",
        "",
        stem,
        flags=re.I,
    )
    stem = re.sub(r"(?:@\d+x|[-_]\d{2,5}x\d{2,5}|[-_]\d{2,5}w)$", "", stem, flags=re.I)
    stem = re.sub(r"\(\d+\)$", "", stem).strip()
    if _APPEARANCE_GENERIC_FILENAME_RE.fullmatch(stem.strip("-_ ")):
        return ""
    tokens = re.findall(r"[A-Za-z0-9&+]+", stem)
    retained: list[str] = []
    for token in tokens:
        lowered = token.casefold()
        if (
            lowered in _APPEARANCE_FILENAME_NOISE
            or re.fullmatch(r"v?\d+", lowered)
            or re.fullmatch(r"\d{2,5}x\d{2,5}", lowered)
            or (
                len(token) >= 10
                and any(character.isdigit() for character in token)
                and any(character.isupper() for character in token)
                and any(character.islower() for character in token)
            )
        ):
            continue
        retained.append(token)
    label = " ".join(retained[:8])
    return _clean_identity_label(label)


def _tag_marker_text(node: Tag) -> str:
    classes = node.get("class", [])
    if isinstance(classes, str):
        classes = [classes]
    return " ".join(
        (
            " ".join(str(value) for value in classes),
            str(node.get("id") or ""),
            str(node.get("aria-label") or ""),
            str(node.get("role") or ""),
        )
    )


def _relation_from_markers(value: str) -> str:
    # Product integrations often contain the word "customer" (for example, "Customer data
    # integrations"). Treat the vendor/integration meaning as higher precedence than the generic
    # customer token so those logos cannot become alleged buyers.
    if _APPEARANCE_VENDOR_CONTEXT_RE.search(value):
        return "unknown"
    for relation, pattern in _APPEARANCE_RELATION_PATTERNS:
        if pattern.search(value):
            return relation
    return "unknown"


def _bounded_tag_text(node: Tag, *, max_characters: int = 500) -> str:
    parts: list[str] = []
    size = 0
    for raw_part in node.stripped_strings:
        part = re.sub(r"\s+", " ", str(raw_part)).strip()
        if not part:
            continue
        size += len(part) + int(bool(parts))
        if size > max_characters:
            return ""
        parts.append(part)
    return " ".join(parts)


def _appearance_context(
    node: Tag,
    *,
    soup: BeautifulSoup,
    page_url: str,
) -> tuple[str, str, int]:
    """Classify only the node's semantic neighborhood, never the whole page body."""

    excerpt_parts: list[str] = []
    relation = "unknown"
    strength = 0
    negative_context = False
    vendor_context = False
    current: Tag | None = node
    depth = 0
    while current is not None and depth < 9:
        if current.name in {"header", "nav", "footer", "aside"}:
            return "unknown", "", 0
        if current.name in {"body", "html", "main"}:
            break
        markers = re.sub(r"\s+", " ", _tag_marker_text(current)).strip()
        marker_relation = _relation_from_markers(markers)
        if marker_relation != "unknown" and strength < 1:
            relation, strength = marker_relation, 1
        if _APPEARANCE_NEGATIVE_CONTEXT_RE.search(markers):
            negative_context = True
        if _APPEARANCE_VENDOR_CONTEXT_RE.search(markers):
            vendor_context = True

        text = _bounded_tag_text(current)
        semantic_container = current.name in {
            "article",
            "blockquote",
            "figure",
            "li",
            "section",
        }
        if text and len(text) <= 500 and (depth <= 2 or semantic_container):
            if text not in excerpt_parts:
                excerpt_parts.append(text)
            text_relation = _relation_from_markers(text)
            if text_relation != "unknown" and strength < 2:
                relation, strength = text_relation, 2
            if _APPEARANCE_NEGATIVE_CONTEXT_RE.search(text):
                negative_context = True
            if _APPEARANCE_VENDOR_CONTEXT_RE.search(text):
                vendor_context = True

        if semantic_container:
            previous_heading = node.find_previous(["h1", "h2", "h3", "h4"])
            headings = (
                [previous_heading]
                if previous_heading is not None and current in previous_heading.parents
                else []
            )
            for heading in headings:
                heading_text = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)).strip()
                if not heading_text:
                    continue
                if heading_text not in excerpt_parts:
                    excerpt_parts.insert(0, heading_text)
                heading_relation = _relation_from_markers(heading_text)
                if heading_relation != "unknown":
                    relation, strength = heading_relation, 3
                    break
                if _APPEARANCE_NEGATIVE_CONTEXT_RE.search(heading_text):
                    negative_context = True
                if _APPEARANCE_VENDOR_CONTEXT_RE.search(heading_text):
                    vendor_context = True
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
        depth += 1

    if strength < 2:
        parsed = urlparse(page_url)
        page_marker = " ".join(
            (
                parsed.path.replace("-", " ").replace("_", " "),
                soup.title.get_text(" ", strip=True) if soup.title else "",
            )
        )
        page_relation = _relation_from_markers(page_marker)
        if page_relation != "unknown" and any(
            token in parsed.path.casefold()
            for token in ("case-stud", "customer", "client", "testimonial", "portfolio", "work")
        ):
            relation, strength = page_relation, 2
            if soup.title:
                excerpt_parts.insert(0, soup.title.get_text(" ", strip=True))

    if vendor_context or (negative_context and strength < 3):
        return "unknown", " | ".join(dict.fromkeys(excerpt_parts))[:1_000], 0
    return relation, " | ".join(dict.fromkeys(excerpt_parts))[:1_000], strength


def _publisher_identity_keys(soup: BeautifulSoup, publisher_domain: str) -> set[str]:
    values = [publisher_domain.split(".", 1)[0].replace("-", " ")]
    for meta in soup.find_all("meta", limit=200):
        marker = " ".join(
            str(meta.get(attribute) or "") for attribute in ("property", "name", "itemprop")
        ).casefold()
        if "site_name" in marker or "application-name" in marker:
            values.append(str(meta.get("content") or ""))
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            payload = json.loads(script.string or script.get_text() or "null")
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        for item in _jsonld_nodes(payload):
            raw_type = item.get("@type", "")
            types = [raw_type] if isinstance(raw_type, str) else raw_type
            if not isinstance(types, list) or not any(
                str(value).casefold() in {"organization", "corporation", "localbusiness"}
                for value in types
            ):
                continue
            name = str(item.get("name") or "")
            name_key = _compact_company_identity(name)
            host_key = _compact_company_identity(publisher_domain.split(".", 1)[0])
            identity_values = [
                item.get("url"),
                item.get("@id"),
                item.get("sameAs"),
            ]
            raw_urls: list[str] = []
            for raw_value in identity_values:
                if isinstance(raw_value, str):
                    raw_urls.append(raw_value)
                elif isinstance(raw_value, list):
                    raw_urls.extend(str(value) for value in raw_value[:10])
            matches_publisher = False
            for raw_url in raw_urls:
                canonical = _canonical_appearance_url(
                    raw_url,
                    base_url=f"https://{publisher_domain}/",
                )
                if not canonical:
                    continue
                identity_host = (
                    (urlparse(canonical).hostname or "")
                    .casefold()
                    .removeprefix("www.")
                    .rstrip(".")
                )
                if identity_host == publisher_domain:
                    matches_publisher = True
                    break
            if matches_publisher or (
                host_key
                and name_key
                and min(len(host_key), len(name_key)) >= 4
                and (host_key in name_key or name_key in host_key)
            ):
                values.append(name)
    return {
        key
        for value in values[:20]
        if (key := _compact_company_identity(_clean_identity_label(value)))
    }


def _parse_appearance_date(values: list[str]) -> datetime | None:
    for value in values:
        normalized = value.strip().replace("Z", "+00:00")
        if not normalized:
            continue
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(normalized[:10])
            except ValueError:
                continue
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    return None


def _explicit_appearance_page_date(soup: BeautifulSoup) -> datetime | None:
    """Return publication metadata that explicitly describes the fetched page.

    A generic ``time`` element can be an event, copyright year, pricing countdown, or another
    unrelated date. It must not silently timestamp every company logo on the page.
    """

    values: list[str] = []
    head = soup.head
    if head is None:
        return None
    for node in head.find_all("meta", limit=200):
        marker = " ".join(
            str(node.get(attribute) or "") for attribute in ("property", "name", "itemprop")
        ).casefold()
        if any(token in marker for token in ("published", "datecreated", "datepublished")):
            values.append(str(node.get("content") or ""))
    for script in head.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            payload = json.loads(script.string or script.get_text() or "null")
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        for item in _jsonld_nodes(payload):
            raw_type = item.get("@type", "")
            types = [raw_type] if isinstance(raw_type, str) else raw_type
            if not isinstance(types, list) or not any(
                str(value).casefold()
                in {"article", "blogposting", "case study", "casestudy", "newsarticle"}
                for value in types
            ):
                continue
            values.extend(
                str(item.get(field) or "") for field in ("datePublished", "dateCreated")
            )
    return _parse_appearance_date(values)


def _local_appearance_article_date(node: Tag) -> datetime | None:
    """Accept a ``time`` value only when it belongs to the appearance's own article."""

    article = node.find_parent("article")
    if article is None:
        return None
    explicit_values: list[str] = []
    fallback_values: list[str] = []
    for meta in article.find_all("meta", limit=30):
        marker = " ".join(
            str(meta.get(attribute) or "") for attribute in ("property", "name", "itemprop")
        ).casefold()
        if any(token in marker for token in ("published", "datecreated", "datepublished")):
            explicit_values.append(str(meta.get("content") or ""))
    for time_node in article.find_all("time", limit=20):
        value = str(time_node.get("datetime") or "")
        marker = " ".join(
            (
                _tag_marker_text(time_node),
                str(time_node.get("itemprop") or ""),
                str(time_node.get("property") or ""),
            )
        ).casefold()
        if any(token in marker for token in ("published", "datecreated", "datepublished")):
            explicit_values.append(value)
        else:
            fallback_values.append(value)
    explicit = _parse_appearance_date(explicit_values)
    if explicit is not None:
        return explicit
    parsed_fallbacks = [
        parsed for value in fallback_values if (parsed := _parse_appearance_date([value]))
    ]
    return parsed_fallbacks[0] if len(parsed_fallbacks) == 1 else None


def _identity_hints_compatible(left: str, right: str) -> bool:
    left_key = _compact_company_identity(left)
    right_key = _compact_company_identity(right)
    if not left_key or not right_key:
        return True
    if left_key == right_key or left_key in right_key or right_key in left_key:
        return True
    return SequenceMatcher(None, left_key, right_key).ratio() >= 0.8


def _domain_matches_name(domain: str, name: str) -> bool:
    domain_label = domain.split(".", 1)[0].replace("-", " ")
    domain_key = _compact_company_identity(domain_label)
    name_key = _compact_company_identity(name)
    if not domain_key or not name_key or min(len(domain_key), len(name_key)) < 4:
        return True
    return (
        domain_key in name_key
        or name_key in domain_key
        or SequenceMatcher(None, domain_key, name_key).ratio() >= 0.62
    )


def _logo_marked(node: Tag, asset_urls: tuple[str, ...]) -> bool:
    markers = [_tag_marker_text(node)]
    current = node.parent
    for _ in range(2):
        if not isinstance(current, Tag):
            break
        markers.append(_tag_marker_text(current))
        current = current.parent
    markers.extend(unquote(urlparse(url).path.rsplit("/", 1)[-1]) for url in asset_urls)
    return bool(_APPEARANCE_LOGO_MARKER_RE.search(" ".join(markers)))


def _appearance_assertion(
    *,
    publisher_domain: str,
    relation: str,
    page_url: str,
    context_excerpt: str,
    subject_key: str,
    confidence: str,
    evidence_kind: str,
    asset_url: str,
    published_at: datetime | None,
) -> DiscoveryAssertionEvidence:
    normalized_context = re.sub(r"\s+", " ", context_excerpt.casefold()).strip()
    context_hash = hashlib.sha256(
        f"{publisher_domain}|{relation}|{subject_key}|{normalized_context}".encode()
    ).hexdigest()
    return DiscoveryAssertionEvidence(
        publisher_domain=publisher_domain,
        relation_type=relation,
        source_url=_canonical_appearance_url(page_url, base_url=page_url),
        context_excerpt=context_excerpt,
        context_hash=context_hash,
        published_at=published_at,
        confidence=confidence,
        current_relationship_unverified=True,
        evidence_kind=evidence_kind,
        asset_url=asset_url,
    )


def _extract_company_appearances(
    soup: BeautifulSoup,
    *,
    page_url: str,
    publisher_domain: str,
) -> list[CompanyAppearanceEvidence]:
    """Extract bounded cross-company proof from HTML that has already been fetched."""

    publisher_domain = publisher_domain.casefold().removeprefix("www.").rstrip(".")
    publisher_keys = _publisher_identity_keys(soup, publisher_domain)
    page_published_at = _explicit_appearance_page_date(soup)
    appearances: list[CompanyAppearanceEvidence] = []

    image_nodes = soup.find_all(["img", "image"], limit=MAX_APPEARANCE_IMAGE_NODES + 1)
    for node in image_nodes[:MAX_APPEARANCE_IMAGE_NODES]:
        if node.find_parent(["header", "nav", "footer", "aside"]) is not None:
            continue
        assets = _iter_image_asset_urls(node, page_url=page_url)
        if not assets:
            continue
        relation, excerpt, relation_strength = _appearance_context(
            node,
            soup=soup,
            page_url=page_url,
        )
        if relation == "unknown" or relation_strength < 2:
            continue
        if any(_APPEARANCE_ASSET_REJECT_RE.search(unquote(urlparse(url).path)) for url in assets):
            continue

        anchor = node.find_parent("a", href=True)
        subject_url = ""
        subject_domain = ""
        if anchor is not None:
            subject_url = _canonical_appearance_url(
                str(anchor.get("href") or ""),
                base_url=page_url,
            )
            subject_domain = _appearance_domain(subject_url)
            if subject_domain and (
                subject_domain == publisher_domain
                or subject_domain.endswith(f".{publisher_domain}")
                or publisher_domain.endswith(f".{subject_domain}")
            ):
                subject_domain = ""

        alt_hints = [
            _clean_identity_label(str(node.get(attribute) or ""))
            for attribute in ("alt", "aria-label", "title")
        ]
        if anchor is not None:
            alt_hints.extend(
                (
                    _clean_identity_label(str(anchor.get("aria-label") or "")),
                    _clean_identity_label(str(anchor.get("title") or "")),
                    _clean_anchor_subject_label(anchor.get_text(" ", strip=True)),
                )
            )
        alt_hints = list(dict.fromkeys(hint for hint in alt_hints if hint))
        filename_hints = list(
            dict.fromkeys(
                hint for asset in assets if (hint := _filename_identity_hint(asset))
            )
        )
        all_hints = [*alt_hints, *filename_hints]
        if len(all_hints) >= 2 and any(
            not _identity_hints_compatible(all_hints[0], hint) for hint in all_hints[1:]
        ):
            continue
        subject_name = all_hints[0] if all_hints else ""
        if not subject_name and subject_domain:
            subject_name = subject_domain.split(".", 1)[0].replace("-", " ")
        if not subject_name:
            continue
        subject_key = _compact_company_identity(subject_name)
        if subject_key in publisher_keys or (
            subject_domain and not _domain_matches_name(subject_domain, subject_name)
        ):
            continue

        logo_marked = _logo_marked(node, assets)
        if not logo_marked and not (
            relation_strength >= 3
            and relation in {"case_study", "customer", "partner", "portfolio"}
        ):
            continue
        if not logo_marked and not subject_domain and relation == "testimonial":
            continue

        agreeing_hints = len({_compact_company_identity(hint) for hint in all_hints}) >= 2
        # Substring-compatible alt/filename pairs are still independent identity cues.
        if len(all_hints) >= 2:
            agreeing_hints = True
        if subject_domain and subject_name and _domain_matches_name(subject_domain, subject_name):
            identity_confidence = "high" if all_hints else "medium"
        elif agreeing_hints:
            identity_confidence = "high"
        elif alt_hints:
            identity_confidence = "medium"
        else:
            identity_confidence = "low"

        if subject_domain:
            extraction_method = "linked_logo"
        elif alt_hints:
            extraction_method = "logo_label"
        else:
            extraction_method = "logo_filename"
        confidence = (
            "high"
            if relation_strength >= 3 and identity_confidence == "high"
            else "medium"
            if identity_confidence in {"medium", "high"}
            else "low"
        )
        asset_url = assets[0]
        assertion = _appearance_assertion(
            publisher_domain=publisher_domain,
            relation=relation,
            page_url=page_url,
            context_excerpt=excerpt or subject_name,
            subject_key=subject_domain or subject_key,
            confidence=confidence,
            evidence_kind=extraction_method,
            asset_url=asset_url,
            published_at=page_published_at or _local_appearance_article_date(node),
        )
        appearances.append(
            CompanyAppearanceEvidence(
                subject_name=subject_name,
                subject_domain=subject_domain,
                subject_url=subject_url,
                asset_url=asset_url,
                extraction_method=extraction_method,
                identity_confidence=identity_confidence,
                assertion=assertion,
            )
        )

    for anchor in soup.find_all("a", href=True, limit=1_500):
        if anchor.find(["img", "picture", "svg"]):
            continue
        if anchor.find_parent(["header", "nav", "footer", "aside"]) is not None:
            continue
        relation, excerpt, relation_strength = _appearance_context(
            anchor,
            soup=soup,
            page_url=page_url,
        )
        if relation == "unknown" or relation_strength < 2:
            continue
        subject_url = _canonical_appearance_url(
            str(anchor.get("href") or ""),
            base_url=page_url,
        )
        if not subject_url:
            continue
        subject_domain = _appearance_domain(subject_url)
        page_host = (urlparse(page_url).hostname or "").casefold().removeprefix("www.")
        internal = bool(
            not subject_domain
            or subject_domain == page_host
            or subject_domain.endswith(f".{page_host}")
            or page_host.endswith(f".{subject_domain}")
        )
        subject_name = _clean_anchor_subject_label(
            " ".join(
                (
                    str(anchor.get("aria-label") or ""),
                    str(anchor.get("title") or ""),
                    anchor.get_text(" ", strip=True),
                )
            )
        )
        if internal:
            path = urlparse(subject_url).path.casefold()
            if not subject_name or not any(
                token in path
                for token in ("case-stud", "customer", "client", "testimonial", "portfolio", "work")
            ):
                continue
            subject_domain = ""
            extraction_method = "internal_record"
        else:
            if not subject_name:
                subject_name = subject_domain.split(".", 1)[0].replace("-", " ")
            extraction_method = "anchor"
        subject_key = _compact_company_identity(subject_name)
        if not subject_key or subject_key in publisher_keys:
            continue
        if subject_domain and not _domain_matches_name(subject_domain, subject_name):
            continue
        identity_confidence = "high" if subject_domain else "medium"
        confidence = "high" if relation_strength >= 3 and subject_domain else "medium"
        assertion = _appearance_assertion(
            publisher_domain=publisher_domain,
            relation=relation,
            page_url=page_url,
            context_excerpt=excerpt or subject_name,
            subject_key=subject_domain or subject_key,
            confidence=confidence,
            evidence_kind=extraction_method,
            asset_url="",
            published_at=page_published_at or _local_appearance_article_date(anchor),
        )
        appearances.append(
            CompanyAppearanceEvidence(
                subject_name=subject_name,
                subject_domain=subject_domain,
                subject_url=subject_url,
                extraction_method=extraction_method,
                identity_confidence=identity_confidence,
                assertion=assertion,
            )
        )

    return _merge_company_appearances(appearances, limit=MAX_PAGE_COMPANY_APPEARANCES)


def _appearance_quality(item: CompanyAppearanceEvidence) -> tuple[int, int, int, int]:
    method_rank = {
        "logo_filename": 0,
        "logo_label": 1,
        "internal_record": 2,
        "anchor": 3,
        "linked_logo": 4,
    }
    confidence_rank = {"low": 0, "medium": 1, "high": 2}
    return (
        int(bool(item.subject_domain)),
        confidence_rank[item.identity_confidence],
        method_rank[item.extraction_method],
        len(item.assertion.context_excerpt),
    )


def _merge_company_appearances(
    *groups: list[CompanyAppearanceEvidence],
    limit: int = MAX_SITE_COMPANY_APPEARANCES,
) -> list[CompanyAppearanceEvidence]:
    """Collapse responsive assets, carousel clones, and repeated pages deterministically."""

    items = [item for group in groups for item in group]
    resolved_domains: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for item in items:
        name_key = _compact_company_identity(item.subject_name)
        if not name_key or not item.subject_domain:
            continue
        normalized_context = re.sub(
            r"\s+", " ", item.assertion.context_excerpt.casefold()
        ).strip()
        resolved_domains[
            (
                item.assertion.publisher_domain,
                item.assertion.relation_type,
                normalized_context,
                name_key,
            )
        ].add(item.subject_domain)

    merged: dict[tuple[str, str, str, str], CompanyAppearanceEvidence] = {}
    for item in items:
        publisher = item.assertion.publisher_domain
        relation = item.assertion.relation_type
        name_key = _compact_company_identity(item.subject_name)
        normalized_context = re.sub(
            r"\s+", " ", item.assertion.context_excerpt.casefold()
        ).strip()
        alias_key = (publisher, relation, normalized_context, name_key)
        domain_aliases = resolved_domains.get(alias_key, set())
        # When exactly one domain-backed copy agrees with a name-only carousel clone, both copies
        # share the resolved identity. Multiple domains with the same display name stay separate.
        identity = item.subject_domain or (
            next(iter(domain_aliases)) if len(domain_aliases) == 1 else name_key
        )
        if not identity:
            continue
        context_key = hashlib.sha256(
            f"{publisher}|{relation}|{identity}|{normalized_context}".encode()
        ).hexdigest()
        key = (publisher, relation, context_key, identity)
        current = merged.get(key)
        if current is None or _appearance_quality(item) > _appearance_quality(current):
            merged[key] = item
    return sorted(
        merged.values(),
        key=lambda item: (
            -_appearance_quality(item)[0],
            -_appearance_quality(item)[1],
            item.subject_domain or _compact_company_identity(item.subject_name),
            item.assertion.source_url,
        ),
    )[:limit]


def _embedded_web_intelligence(
    soup: BeautifulSoup, text_body: str, url: str, links: list[str]
) -> WebIntelligence:
    types: list[str] = []
    product_objects = 0
    offer_objects = 0
    organization_objects = 0
    location_objects = 0
    max_review_count = 0
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw or len(raw) > 1_000_000:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _jsonld_nodes(payload):
            raw_types = node.get("@type", [])
            node_types = (
                [raw_types]
                if isinstance(raw_types, str)
                else raw_types
                if isinstance(raw_types, list)
                else []
            )
            normalized = [str(value) for value in node_types if value]
            types.extend(normalized)
            lowered_types = {value.lower() for value in normalized}
            product_objects += int("product" in lowered_types)
            offer_objects += int(bool(lowered_types & {"offer", "aggregateoffer"}))
            organization_objects += int(
                bool(lowered_types & {"organization", "corporation", "brand"})
            )
            location_objects += int(
                bool(
                    lowered_types
                    & {
                        "localbusiness",
                        "store",
                        "place",
                        "postaladdress",
                    }
                )
            )
            review = node.get("aggregateRating")
            if isinstance(review, dict):
                max_review_count = max(
                    max_review_count,
                    _positive_int(review.get("reviewCount") or review.get("ratingCount")),
                )
            same_as = node.get("sameAs", [])
            same_as_values = [same_as] if isinstance(same_as, str) else same_as
            if isinstance(same_as_values, list):
                for raw_link in same_as_values[:25]:
                    if not isinstance(raw_link, str):
                        continue
                    absolute = urljoin(url, raw_link.strip())
                    parsed = urlparse(absolute)
                    if parsed.scheme in {"http", "https"} and parsed.hostname:
                        links.append(absolute.split("#", 1)[0])

    ats_boards = _ats_boards_from_url_elements(soup, url)

    feed_urls: list[str] = []
    for node in soup.find_all("link", href=True):
        rel = " ".join(str(value) for value in node.get("rel", [])).lower()
        media_type = str(node.get("type", "")).lower()
        if "alternate" in rel and any(
            marker in media_type for marker in ("rss", "atom", "xml")
        ):
            feed_urls.append(urljoin(url, str(node.get("href", ""))))

    image_assets = len(soup.find_all("img"))
    video_assets = len(soup.find_all("video")) + sum(
        node.find_parent("video") is not None for node in soup.find_all("source")
    )
    embedded_video_players = 0
    video_platforms: list[str] = []
    asset_hosts: list[str] = []
    video_host_map = {
        "youtube.com": "YouTube",
        "youtu.be": "YouTube",
        "vimeo.com": "Vimeo",
        "wistia.com": "Wistia",
        "wistia.net": "Wistia",
        "mux.com": "Mux",
    }
    for node in soup.find_all(["img", "video", "source", "iframe"]):
        raw_asset = str(node.get("src") or node.get("data-src") or "").strip()
        if not raw_asset:
            continue
        asset_url = urljoin(url, raw_asset)
        asset_host = (urlparse(asset_url).hostname or "").removeprefix("www.").lower()
        if asset_host and asset_host != (urlparse(url).hostname or "").removeprefix("www."):
            asset_hosts.append(asset_host)
        if node.name == "iframe":
            for provider_host, provider in video_host_map.items():
                if asset_host == provider_host or asset_host.endswith(f".{provider_host}"):
                    embedded_video_players += 1
                    video_platforms.append(provider)
                    break

    page_host = (urlparse(url).hostname or "").removeprefix("www.")
    company_appearances = _extract_company_appearances(
        soup,
        page_url=url,
        publisher_domain=page_host,
    )
    portfolio_domains = [
        appearance.subject_domain
        for appearance in company_appearances
        if appearance.subject_domain
        and appearance.extraction_method == "anchor"
        and appearance.assertion.relation_type == _LEGACY_PORTFOLIO_PROJECTION_RELATION
    ]

    web = WebIntelligence(
        jsonld_types=list(dict.fromkeys(types))[:40],
        product_objects=product_objects,
        offer_objects=offer_objects,
        organization_objects=organization_objects,
        location_objects=location_objects,
        max_review_count=max_review_count,
        ats_boards=ats_boards,
        portfolio_domains=list(dict.fromkeys(portfolio_domains))[:40],
        company_appearances=company_appearances,
    )
    web.sales.feed_urls = list(dict.fromkeys(feed_urls))[:5]
    web.sales.image_assets = image_assets
    web.sales.video_assets = video_assets
    web.sales.embedded_video_players = embedded_video_players
    web.sales.video_platforms = list(dict.fromkeys(video_platforms))[:10]
    web.sales.asset_hosts = list(dict.fromkeys(asset_hosts))[:30]
    return web


@reuse_html
def _extract_page(
    url: str, status_code: int, body: bytes, *, research: SiteResearchPlanner | None = None,
    observed_at: datetime | None = None,
) -> tuple[
    PageEvidence,
    dict[str, list[str]],
    list[str],
    ContactIntelligence,
    list[str],
    dict[str, list[str]],
    WebIntelligence,
]:
    text_body = body.decode("utf-8", errors="replace")
    soup = parse_html(text_body, mutable=True)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": re.compile("description", re.I)})
    description = str(meta.get("content", "")).strip() if meta else ""
    html_language = str(soup.html.get("lang") or "").strip() if soup.html else ""
    if not html_language:
        language_meta = soup.find(
            "meta",
            attrs={"http-equiv": re.compile(r"^content-language$", re.I)},
        )
        html_language = (
            str(language_meta.get("content") or "").strip() if language_meta else ""
        )
    canonical_node = soup.find("link", attrs={"rel": re.compile("canonical", re.I)})
    canonical_url = (
        urljoin(url, str(canonical_node.get("href", "")).strip())
        if canonical_node and canonical_node.get("href")
        else ""
    )
    links: list[str] = []
    site_host = (urlparse(url).hostname or "").removeprefix("www.")
    contacts = extract_contact_intelligence(
        soup,
        text_body,
        source_url=url,
        site_host=site_host,
    )
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if href.lower().startswith("mailto:"):
            continue
        absolute = urljoin(url, href)
        parsed = urlparse(absolute)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            links.append(absolute.split("#", 1)[0])

    web = _embedded_web_intelligence(soup, text_body, url, links)
    if research is not None:
        research.deduplicate_entities(soup, url, web)
    try:
        web.frontier = extract_frontier_from_html(
            text_body,
            url,
            company_key=site_host,
            observed_at=observed_at,
        )
    except Exception as exc:
        # Frontier extractors are optional, versioned work. A parser defect must not make the
        # completed core website scan retry and fetch the same company again inside 90 days.
        web.errors.append(f"frontier local extraction: {type(exc).__name__}: {exc}"[:1_000])
    contextual_signals = _contextual_page_signals(
        soup,
        title=title,
        description=description,
        url=url,
    )
    if research is not None:
        research.observe(url, soup)
    for node in soup(["script", "style", "noscript", "svg", "template"]):
        node.decompose()
    visible = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    excerpt = visible[:20_000]
    content_hash = hashlib.sha256(
        re.sub(r"\s+", " ", visible.lower()).encode("utf-8")
    ).hexdigest()

    lowered = f"{title} {description} {visible}".lower()
    signals: dict[str, list[str]] = defaultdict(list)
    for category, patterns in SIGNAL_PATTERNS.items():
        for pattern in patterns:
            if pattern in lowered:
                signals[category].append(pattern)
    for category, values in contextual_signals.items():
        signals[category].extend(values)
    prices = list(dict.fromkeys(match.group(0).strip() for match in PRICE_RE.finditer(visible)))
    if prices:
        signals["pricing"] = prices[:10]

    technologies = []
    raw_lower = text_body.lower()
    for technology, markers in TECH_MARKERS.items():
        if any(marker in raw_lower for marker in markers):
            technologies.append(technology)
    web.sales.growth_technologies = [
        technology for technology in technologies if technology in GROWTH_TECHNOLOGIES
    ]
    web.sales.creative_technologies = [
        technology for technology in technologies if technology in CREATIVE_TECHNOLOGIES
    ]

    public_ids = {
        name: list(dict.fromkeys(match.upper() for match in pattern.findall(text_body)))[:10]
        for name, pattern in PUBLIC_ID_PATTERNS.items()
    }
    public_ids = {name: values for name, values in public_ids.items() if values}

    from .buyer_intelligence import parse_public_html

    buyer_evidence = parse_public_html(
        text_body, url, site_host, observed_at=observed_at or datetime.now(UTC),
    )
    page = PageEvidence(
        buyer_evidence=[r.model_dump(mode="json") for r in buyer_evidence],
        url=url,
        status_code=status_code,
        title=title[:500],
        description=description[:1_000],
        text_excerpt=excerpt,
        content_hash=content_hash,
        canonical_url=canonical_url[:2_000],
        language=assess_english_presentation(
            excerpt,
            declared_language=html_language,
            title=title,
            description=description,
        ),
    )
    return (
        page,
        dict(signals),
        list(dict.fromkeys(links)),
        technologies,
        contacts,
        public_ids,
        web,
    )


def _select_priority_links(base_url: str, links: list[str], limit: int) -> list[str]:
    """Choose purpose-diverse first-party pages instead of several links from one section."""
    if limit <= 0:
        return []
    base_host = urlparse(base_url).hostname
    buckets: tuple[tuple[str, ...], ...] = (
        ("/procurement", "/rfp", "/supplier-brief", "/tenders"),
        (
            "/career",
            "/jobs",
            "/hiring",
            "/open-roles",
            "/open_roles",
            "/vacancies",
            "/join-us",
            "/join_us",
            "/work-with-us",
        ),
        ("contact", "new-business"),
        ("case-stud", "portfolio", "our-work", "/work", "client"),
        ("capabilit", "production", "studio", "creative", "service"),
        ("team", "people", "leadership", "about", "press", "media"),
        ("pricing",),
    )
    ranked: list[tuple[int, str]] = []
    for link in dict.fromkeys(links):
        parsed = urlparse(link)
        if parsed.hostname != base_host or link.rstrip("/") == base_url.rstrip("/"):
            continue
        path = parsed.path.lower()
        rank = next(
            (index for index, words in enumerate(buckets) if any(word in path for word in words)),
            99,
        )
        if rank < 99:
            ranked.append((rank, link))
    selected: list[str] = []
    for bucket in range(len(buckets)):
        match = next((link for rank, link in ranked if rank == bucket), None)
        if match and match not in selected:
            selected.append(match)
            if len(selected) == limit:
                return selected
    for _, link in sorted(ranked):
        if link not in selected:
            selected.append(link)
            if len(selected) == limit:
                break
    fallbacks = ("/contact", "/work", "/services", "/about")
    for path in fallbacks:
        if len(selected) >= limit:
            break
        fallback = urljoin(base_url, path)
        if fallback.rstrip("/") != base_url.rstrip("/") and fallback not in selected:
            selected.append(fallback)
    return selected


def _merge_signals(target: dict[str, list[str]], incoming: dict[str, list[str]]) -> None:
    for key, values in incoming.items():
        target[key] = list(dict.fromkeys([*target.get(key, []), *values]))[:20]


def _merge_contacts(target: ContactIntelligence, incoming: ContactIntelligence) -> None:
    merged = build_contact_intelligence(
        [*target.emails, *incoming.emails],
        phones=[*target.phones, *incoming.phones],
        people=[*target.people, *incoming.people],
        contact_pages_checked=[
            *target.contact_pages_checked,
            *incoming.contact_pages_checked,
        ],
        contact_forms=[*target.contact_forms, *incoming.contact_forms],
        errors=[*target.errors, *incoming.errors],
    )
    target.emails = merged.emails
    target.preferred_email = merged.preferred_email
    target.phones = merged.phones
    target.preferred_phone = merged.preferred_phone
    target.people = merged.people
    target.contact_pages_checked = merged.contact_pages_checked
    target.contact_forms = merged.contact_forms
    target.errors = merged.errors


def _merge_web(target: WebIntelligence, incoming: WebIntelligence) -> None:
    target.jsonld_types = list(
        dict.fromkeys([*target.jsonld_types, *incoming.jsonld_types])
    )[:40]
    for field in (
        "product_objects",
        "offer_objects",
        "organization_objects",
        "location_objects",
    ):
        setattr(target, field, getattr(target, field) + getattr(incoming, field))
    target.max_review_count = max(target.max_review_count, incoming.max_review_count)
    for provider, boards in incoming.ats_boards.items():
        target.ats_boards[provider] = list(
            dict.fromkeys([*target.ats_boards.get(provider, []), *boards])
        )[:3]
    target.company_appearances = _merge_company_appearances(
        target.company_appearances,
        incoming.company_appearances,
    )
    appearance_domains = [
        appearance.subject_domain
        for appearance in target.company_appearances
        if appearance.subject_domain
        and appearance.extraction_method == "anchor"
        and appearance.assertion.relation_type == _LEGACY_PORTFOLIO_PROJECTION_RELATION
    ]
    target.portfolio_domains = list(
        dict.fromkeys(
            [
                *target.portfolio_domains,
                *incoming.portfolio_domains,
                *appearance_domains,
            ]
        )
    )[:40]
    target.frontier = merge_frontier(target.frontier, incoming.frontier)
    target.sales.image_assets += incoming.sales.image_assets
    target.sales.video_assets += incoming.sales.video_assets
    target.sales.embedded_video_players += incoming.sales.embedded_video_players
    for field in (
        "growth_technologies",
        "creative_technologies",
        "video_platforms",
        "asset_hosts",
        "feed_urls",
    ):
        setattr(
            target.sales,
            field,
            list(
                dict.fromkeys(
                    [*getattr(target.sales, field), *getattr(incoming.sales, field)]
                )
            )[:30],
        )


def _unrendered_app_shell(body: bytes) -> bool:
    """Recognize empty JS mounts, not merely short or unfashionable websites."""
    soup = BeautifulSoup(body, "html.parser")
    if not soup.find("script"):
        return False
    mount = soup.select_one("#root, #app, #__next, #__nuxt, app-root, [ng-version]")
    javascript_notice = any(
        re.search(
            r"(?:enable|requires?|needs?)\s+(?:\w+\s+){0,3}javascript",
            node.get_text(" "), re.I,
        )
        for node in soup.find_all("noscript")
    )
    if mount is None and not javascript_notice:
        return False
    for node in soup(["head", "script", "style", "noscript", "template", "svg"]):
        node.decompose()
    visible = soup.get_text(" ", strip=True)
    # Real copy, contacts or visible media are usable evidence even on a sparse page.
    placeholder = not visible or re.fullmatch(
        r"(?:loading|please wait|initializing)(?:\.{1,3}|…)?", visible, re.I,
    ) is not None
    return placeholder and not soup.find(
        ["a", "img", "video", "picture", "iframe", "form"]
    )


async def inspect_host(
    client: httpx.AsyncClient,
    candidate: DiscoveredHost,
    *,
    pages_per_site: int = DEFAULT_SITE_RESEARCH_PAGES,
    max_bytes_per_page: int = 750_000,
    english_only: bool = True,
    microlink_api_key: str = "",
) -> SiteObservation:
    require_allowed(candidate.hostname)
    first_error: str | None = None
    home_response: httpx.Response | None = None
    home_body = b""
    fallback_response: httpx.Response | None = None
    fallback_body = b""
    fetch_attempts = 0
    fallback_attempted = False
    fallback_proxy_used = False
    fallback_error = ""
    fetch_provider = "direct"
    direct_fetch_error = ""
    last_recoverable_error: BaseException | None = None
    stop_variants = False
    platform_tenant = is_platform_tenant(candidate)
    root_hostnames = _root_host_variants(candidate)
    completed_hosts: set[str] = set()
    no_http_hosts: set[str] = set()
    request_phase = ""
    previous_observer = REQUEST_ACTIVITY.get()

    def observe(phase: str, url: str) -> None:
        nonlocal request_phase
        request_phase = phase
        if previous_observer is not None:
            previous_observer(phase, url)

    schemes = ("https",) if platform_tenant else ("https", "http")
    for scheme in schemes:
        for root_hostname in root_hostnames:
            if (
                stop_variants
                or home_response is not None
                or fetch_attempts >= ROOT_FETCH_TOTAL_ATTEMPTS
            ):
                break
            if root_hostname in completed_hosts or (
                scheme == "http" and root_hostname in no_http_hosts
            ):
                continue
            response_received = False
            for attempt in range(ROOT_FETCH_ATTEMPTS):
                if fetch_attempts >= ROOT_FETCH_TOTAL_ATTEMPTS:
                    break
                fetch_attempts += 1
                request_phase = ""
                token = REQUEST_ACTIVITY.set(observe)
                try:
                    async with asyncio.timeout(PUBLIC_FETCH_SECONDS):
                        response, body = await _bounded_get(
                            client,
                            f"{scheme}://{root_hostname}/",
                            max_bytes=max_bytes_per_page,
                        )
                except (httpx.HTTPError, UnsafeTargetError, TimeoutError) as exc:
                    if isinstance(exc, httpx.PoolTimeout) or (
                        isinstance(exc, TimeoutError) and request_phase == "admission"
                    ):
                        return SiteObservation(
                            hostname=candidate.hostname,
                            fetch_outcome="retryable_error",
                            fetch_disposition=LOCAL_CAPACITY_DEFERRED,
                            fetch_attempts=fetch_attempts,
                            direct_fetch_error=first_error or "",
                            error="Website check deferred by local request capacity",
                        )
                    first_error = f"{type(exc).__name__}: {exc}"[:1_000]
                    if _retryable_root_exception(exc):
                        last_recoverable_error = exc
                    terminal = terminal_platform_observation(
                        candidate,
                        error=first_error,
                        fetch_attempts=fetch_attempts,
                    )
                    if terminal is not None and not (
                        microlink_api_key.strip()
                        and microlink_fallback_eligible(None, exc)
                    ):
                        return terminal
                    if isinstance(exc, UnsafeTargetError) or _is_certificate_validation_error(exc):
                        no_http_hosts.add(root_hostname)
                        if "DNS resolution" not in str(exc):
                            stop_variants = isinstance(exc, UnsafeTargetError)
                        break
                    if attempt + 1 < ROOT_FETCH_ATTEMPTS and _retryable_root_exception(exc):
                        delay = _root_retry_delay(
                            None,
                            hostname=root_hostname,
                            attempt=attempt,
                        )
                        if delay is not None:
                            await asyncio.sleep(delay)
                            continue
                    break
                finally:
                    REQUEST_ACTIVITY.reset(token)

                response_received = True
                fallback_response, fallback_body = response, body
                live_response = 200 <= response.status_code < 400 and bool(body)
                if live_response:
                    home_response, home_body = response, body
                    break
                if (
                    response.status_code in ROOT_TRANSIENT_STATUS_CODES
                    and attempt + 1 < ROOT_FETCH_ATTEMPTS
                ):
                    delay = _root_retry_delay(
                        response,
                        hostname=root_hostname,
                        attempt=attempt,
                    )
                    if delay is None:
                        stop_variants = True
                        break
                    await asyncio.sleep(delay)
                    continue
                if response.status_code == 429:
                    stop_variants = True
                completed_hosts.add(root_hostname)
                break
            if response_received and home_response is None:
                completed_hosts.add(root_hostname)
        if stop_variants or home_response is not None:
            break
    app_shell = home_response is not None and _unrendered_app_shell(home_body)
    if microlink_api_key.strip() and (
        app_shell
        or (
            home_response is None
            and microlink_fallback_eligible(fallback_response, last_recoverable_error)
        )
    ):
        fallback_attempted = True
        direct_fetch_error = first_error or (
            "Homepage is an unrendered JavaScript shell" if app_shell else
            f"HTTP {fallback_response.status_code}" if fallback_response is not None else ""
        )
        fetch_attempts += 1
        target_url = (
            str(home_response.url) if app_shell and home_response is not None else
            str(fallback_response.url)
            if fallback_response is not None
            else f"https://{candidate.hostname}/"
        )
        try:
            rendered_url, rendered_body, fallback_proxy_used = (
                await fetch_microlink_rendered_html(
                    client,
                    target_url,
                    api_key=microlink_api_key.strip(),
                    max_bytes=max_bytes_per_page,
                )
            )
            home_response = httpx.Response(
                200,
                request=httpx.Request("GET", rendered_url),
                headers={"content-type": "text/html; charset=utf-8"},
                content=rendered_body,
            )
            home_body = rendered_body
            fetch_provider = "microlink"
        except (httpx.HTTPError, UnsafeTargetError, TimeoutError, ValueError) as exc:
            fallback_error = f"{type(exc).__name__}: {exc}"[:500]
    if home_response is None and fallback_response is not None:
        home_response, home_body = fallback_response, fallback_body
    if home_response is None:
        terminal = terminal_platform_observation(
            candidate,
            error=first_error or "Fetch failed",
            fetch_attempts=fetch_attempts,
        )
        if terminal is not None:
            return terminal.model_copy(
                update={
                    "fetch_attempts": fetch_attempts,
                    "fallback_attempted": fallback_attempted,
                    "fallback_proxy_used": fallback_proxy_used,
                    "fallback_error": fallback_error,
                }
            )
        return SiteObservation(
            hostname=candidate.hostname,
            fetch_outcome="retryable_error",
            fetch_attempts=fetch_attempts,
            fallback_attempted=fallback_attempted,
            fallback_proxy_used=fallback_proxy_used,
            fallback_error=fallback_error,
            error=first_error or "Fetch failed",
        )

    final_url = str(home_response.url)
    app_shell = _unrendered_app_shell(home_body)
    live = 200 <= home_response.status_code < 400 and bool(home_body) and not app_shell
    if not live:
        terminal = terminal_platform_observation(
            candidate,
            status_code=home_response.status_code,
            final_url=final_url,
            fetch_attempts=fetch_attempts,
        )
        if terminal is not None:
            return terminal.model_copy(
                update={
                    "fetch_attempts": fetch_attempts,
                    "fallback_attempted": fallback_attempted,
                    "fallback_proxy_used": fallback_proxy_used,
                    "fallback_error": fallback_error,
                }
            )
        return SiteObservation(
            hostname=candidate.hostname,
            fetch_outcome="retryable_error",
            fetch_attempts=fetch_attempts,
            fetch_provider=fetch_provider,
            fallback_attempted=fallback_attempted,
            fallback_proxy_used=fallback_proxy_used,
            fallback_error=fallback_error,
            live=False,
            final_url=final_url,
            canonical_host=urlparse(final_url).hostname,
            status_code=home_response.status_code,
            error=(
                ("Homepage is an unrendered JavaScript shell"
                 if app_shell else f"HTTP {home_response.status_code}")
                + (f"; rendered-page fallback failed: {fallback_error}"
                   if fallback_error else "")
            ),
        )

    from .page_compute import extract_page

    research = SiteResearchPlanner(final_url)
    (
        home_page,
        signals,
        links,
        technologies,
        contacts,
        public_ids,
        web,
    ) = await extract_page(final_url, home_response.status_code, home_body, research=research)
    pages = [home_page]
    all_links = list(links)
    secondary_page_limit = (
        0
        if english_only and home_page.language.english_presentation is False
        else max(pages_per_site - 1, 0)
    )
    research.seed_primary(
        _select_priority_links(final_url, links, min(3, secondary_page_limit)), links,
    )
    content_hashes = {hashlib.sha256(home_body).hexdigest()}
    secondary_failures = 0

    async def same_company_redirect(_current: str, target: str) -> None:
        try:
            if company_host(target) == company_host(final_url):
                return
        except ValueError:
            pass
        # A first-party careers redirect can nominate a supported public ATS board.
        # Preserve that discovery for the identity-aware ATS adapter; do not borrow
        # a hosted provider's page as this company's own website evidence.
        target_host = (urlparse(target).hostname or "").casefold().rstrip(".")
        for provider, hosts in ATS_PROVIDER_HOSTS.items():
            if target_host not in hosts:
                continue
            match = ATS_PATTERNS[provider].search(target)
            if match:
                web.ats_boards[provider] = list(dict.fromkeys([
                    *web.ats_boards.get(provider, []), match.group(1).casefold(),
                ]))[:3]
                all_links.append(target)
        raise UnsafeTargetError("Secondary-page redirect crossed the company identity boundary")

    for _ in range(secondary_page_limit):
        link = research.next_url()
        if link is None:
            break
        try:
            async with asyncio.timeout(PUBLIC_FETCH_SECONDS):
                response, body = await _bounded_get(
                    client,
                    link,
                    max_bytes=max_bytes_per_page,
                    redirect_validator=same_company_redirect,
                    allowed_content_types=(
                        "html",
                        "text/plain",
                        "text/vcard",
                        "text/x-vcard",
                    ),
                )
            if 200 <= response.status_code < 400 and body:
                content_type = response.headers.get("content-type", "").lower()
                is_vcard = urlparse(str(response.url)).path.lower().endswith(".vcf") or any(
                    value in content_type for value in ("text/vcard", "text/x-vcard")
                )
                if is_vcard:
                    vcard_text = body.decode("utf-8", errors="replace")
                    site_host = urlparse(final_url).hostname or candidate.hostname
                    vcard_contacts = build_contact_intelligence(
                        extract_vcard_email_evidence(
                            vcard_text,
                            source_url=str(response.url),
                            site_host=site_host,
                        ),
                        phones=extract_vcard_phone_evidence(
                            vcard_text,
                            source_url=str(response.url),
                            site_host=site_host,
                        ),
                        contact_pages_checked=[str(response.url)],
                    )
                    _merge_contacts(contacts, vcard_contacts)
                    continue
                (
                    page,
                    page_signals,
                    page_links,
                    page_technologies,
                    page_contacts,
                    page_public_ids,
                    page_web,
                ) = await extract_page(
                    str(response.url), response.status_code, body, research=research,
                )
                research.attempted.add(url_key(str(response.url)))
                pages.append(page)
                _merge_signals(signals, page_signals)
                _merge_signals(public_ids, page_public_ids)
                # Repeated template/soft-404/redirect bodies retain page/contact provenance
                # without multiplying catalogue/media counts or embedded scanner evidence.
                body_hash = hashlib.sha256(body).hexdigest()
                if body_hash not in content_hashes:
                    _merge_web(web, page_web)
                    content_hashes.add(body_hash)
                _merge_contacts(contacts, page_contacts)
                all_links.extend(page_links)
                technologies.extend(page_technologies)
            else:
                secondary_failures += 1
                web.errors.append(f"secondary page {link}: HTTP {response.status_code} "
                                  "or empty/non-document response")
        except (httpx.HTTPError, UnsafeTargetError, TimeoutError) as exc:
            web.errors.append(f"secondary page {link}: {type(exc).__name__}: {exc}"[:1_000])
            secondary_failures += 1
        if secondary_failures >= FAILURE_LIMIT:
            web.errors.append("Remaining linked pages deferred after three failed page attempts")
            break

    combined_text = " ".join(page.text_excerpt for page in pages)
    emails = [
        contact.address
        for contact in contacts.emails
        if contact.outreach_eligible and not contact.historical
    ][:20]
    social_urls = []
    contact_urls = []
    legal_urls = []
    final_host = (urlparse(final_url).hostname or "").removeprefix("www.")
    for link in dict.fromkeys(all_links):
        parsed = urlparse(link)
        host = (parsed.hostname or "").removeprefix("www.")
        if any(host == social or host.endswith(f".{social}") for social in SOCIAL_HOSTS):
            social_urls.append(link)
        if any(word in parsed.path.lower() for word in ("contact", "about", "team")):
            contact_urls.append(link)
        legal_match = any(word in parsed.path.lower() for word in LEGAL_PATH_WORDS)
        explicit_subprocessor = any(
            word in parsed.path.lower() for word in ("subprocessor", "sub-processor")
        )
        if legal_match and (host == final_host or explicit_subprocessor):
            legal_urls.append(link)
    combined_hash = hashlib.sha256(
        "|".join(page.content_hash for page in pages).encode("ascii")
    ).hexdigest()
    canonical_host = (
        urlparse(home_page.canonical_url).hostname
        or urlparse(final_url).hostname
        or ""
    ).removeprefix("www.")
    outbound_domains = sorted(
        {
            (urlparse(link).hostname or "").removeprefix("www.")
            for link in all_links
            if (urlparse(link).hostname or "").removeprefix("www.")
            and (urlparse(link).hostname or "").removeprefix("www.") != canonical_host
            and not any(
                (urlparse(link).hostname or "").removeprefix("www.") == social
                or (urlparse(link).hostname or "").removeprefix("www.").endswith(
                    f".{social}"
                )
                for social in SOCIAL_HOSTS
            )
        }
    )[:40]
    contact_urls.extend(contacts.contact_forms)
    return SiteObservation(
        hostname=candidate.hostname,
        fetch_outcome="live",
        direct_fetch_error=direct_fetch_error,
        fetch_attempts=fetch_attempts,
        recovered_after_retry=fetch_attempts > 1,
        fetch_provider=fetch_provider,
        fallback_attempted=fallback_attempted,
        fallback_proxy_used=fallback_proxy_used,
        fallback_error=fallback_error,
        live=True,
        final_url=final_url,
        canonical_host=canonical_host,
        status_code=home_response.status_code,
        title=home_page.title,
        description=home_page.description,
        text_excerpt=combined_text[:30_000],
        content_hash=combined_hash,
        language=home_page.language,
        pages=pages,
        emails=emails,
        contacts=contacts,
        contact_urls=list(dict.fromkeys(contact_urls))[:10],
        legal_urls=list(dict.fromkeys(legal_urls))[:15],
        social_urls=list(dict.fromkeys(social_urls))[:15],
        evidence_urls=[page.url for page in pages],
        signals=signals,
        technologies=list(dict.fromkeys(technologies)),
        public_ids=public_ids,
        outbound_domains=outbound_domains,
        web=web,
    )
