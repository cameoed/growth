from __future__ import annotations

from datetime import UTC, datetime
from math import prod
from typing import Any
from urllib.parse import quote, urlencode

DAILY_MISSION_COUNT = 8

OPERATING_CONTRACT = {
    "primary_cohort": (
        "Paid-growth consumer brands, consumer apps, digital products, gaming, and "
        "subscription businesses"
    ),
    "direct_agency_cohort": (
        "Evidence-strong performance-creative, paid-social, ecommerce-content, UGC, "
        "photo/video, motion, and post-production agencies"
    ),
    "referral_cohort": "Agencies with relevant brand access but no verified direct production need",
    "offer": "$5,000/month ($60,000/year)",
    "principal_signals": [
        "distribution_investment",
        "recurring_creative_demand",
        "production_capacity_gap",
    ],
    "pursuit_rule": (
        "Score at least 70, all three principal signals met, all six hard gates met, "
        "exactly three first-month asks, an economic buyer, and verified observed "
        "evidence for each principal signal"
    ),
    "automation_threshold": "20 completed discovery calls and 3 renewal decisions",
}

ANALYSIS_METHODS: tuple[dict[str, str], ...] = (
    {
        "name": "Open referral geography",
        "signal": (
            "Rotating OpenStreetMap agency, photographer, and video-studio sites for "
            "specialist referrals—not default direct-buyer qualification"
        ),
        "cost": "Free · ODbL",
    },
    {
        "name": "Structured referral graph",
        "signal": (
            "Wikidata advertising, photo, film, and production companies with official "
            "sites as potential referral ecosystems"
        ),
        "cost": "Free · CC0",
    },
    {
        "name": "Portfolio expansion",
        "signal": "First-party client, case-study, production-partner, and work-page domains",
        "cost": "Free",
    },
    {
        "name": "Agency buyer capacity",
        "signal": (
            "Retained-client performance creative, current photo/video throughput, production "
            "operations, overflow, white-label delivery, and one-lane fit"
        ),
        "cost": "Free",
    },
    {
        "name": "Creative-capacity hypothesis",
        "signal": (
            "Observed distribution and recurring demand, with production-gap inferences "
            "kept provisional until human verification"
        ),
        "cost": "Free",
    },
    {
        "name": "Certificate transparency",
        "signal": "Historical tenant subdomains and first-seen timing",
        "cost": "Free",
    },
    {
        "name": "Live identity recovery",
        "signal": "DNS, HTTP status, redirects, canonical company domain, and page identity",
        "cost": "Free",
    },
    {
        "name": "Pricing and offer proof",
        "signal": "Public pricing pages, product offers, catalogs, and structured data",
        "cost": "Free",
    },
    {
        "name": "Creative load",
        "signal": "Image/video density, players, asset hosts, campaigns, and portfolio breadth",
        "cost": "Free",
    },
    {
        "name": "Publishing velocity",
        "signal": "Sitemaps, feeds, launches, case studies, and recent site changes",
        "cost": "Free",
    },
    {
        "name": "Hiring demand",
        "signal": "Public creative, growth, ecommerce, and leadership roles",
        "cost": "Free",
    },
    {
        "name": "Commerce scale",
        "signal": "Catalog size, variants, pricing, reviews, locations, and inventory",
        "cost": "Free",
    },
    {
        "name": "Operating stack",
        "signal": "Public growth, retention, media, and commerce technology evidence",
        "cost": "Free",
    },
    {
        "name": "Commercial triggers",
        "signal": "Bounded news for launches, partnerships, expansion, and appointments",
        "cost": "Free",
    },
    {
        "name": "Historical footprint",
        "signal": "Common Crawl and Internet Archive longevity and page breadth",
        "cost": "Free",
    },
    {
        "name": "Public contact surface",
        "signal": "Provenance-scored mailto, visible, structured, encoded, form, and social routes",
        "cost": "Free",
    },
    {
        "name": "Observed research yield",
        "signal": (
            "Confidence-adjusted live, research-priority, and contactable yield; never "
            "curated pipeline or pursuit state"
        ),
        "cost": "Free",
    },
    {
        "name": "Manual ad-library review",
        "signal": (
            "Operator-observed campaign formats, markets, and refresh patterns retained "
            "as dated claims"
        ),
        "cost": "Operator",
    },
    {
        "name": "Social publishing cadence",
        "signal": "Recent video, product, launch, and campaign publishing frequency",
        "cost": "Free",
    },
    {
        "name": "Review-market presence",
        "signal": "Review count, recency, category position, and customer language",
        "cost": "Free",
    },
    {
        "name": "Retail distribution",
        "signal": "Stockists, store locators, wholesale catalogs, and marketplace coverage",
        "cost": "Free",
    },
    {
        "name": "Team and leadership",
        "signal": "Company-authored team, leadership, department, and location evidence",
        "cost": "Free",
    },
    {
        "name": "Geographic scale",
        "signal": "Public country, language, office, shipping, and market coverage",
        "cost": "Free",
    },
    {
        "name": "Partnership proof",
        "signal": "Certified status, integrations, co-marketing, and implementation work",
        "cost": "Free",
    },
    {
        "name": "Ownership context",
        "signal": "Public parent, portfolio, accelerator, investment, and acquisition evidence",
        "cost": "Free",
    },
    {
        "name": "Event-market activity",
        "signal": "Current exhibitor, sponsor, speaker, and conference participation",
        "cost": "Free",
    },
    {
        "name": "Marketplace footprint",
        "signal": "Public app, partner, seller, supplier, and integration listings",
        "cost": "Free",
    },
    {
        "name": "Public technical references",
        "signal": "URLScan hosts, public code references, docs, and integration examples",
        "cost": "Free",
    },
    {
        "name": "Evidence triangulation",
        "signal": (
            "Agreement across distribution, recurring demand, production gap, timing, "
            "and buyer evidence with observed/inferred/unknown labels"
        ),
        "cost": "Free",
    },
)


def _lens(name: str, intent: str, query: str, evidence: str) -> dict[str, str]:
    return {"name": name, "intent": intent, "query": query, "evidence": evidence}


SEARCH_LENSES: tuple[dict[str, str], ...] = (
    _lens(
        "Tenant platform footprints",
        "Find software assigning public subdomains to paying customers.",
        '("your subdomain" OR "custom subdomain" OR "account URL") '
        "(portal OR storefront OR workspace) software",
        "Documentation proving a repeatable customer-bearing hostname pattern.",
    ),
    _lens(
        "Custom-domain platforms",
        "Find paid platforms whose customer domains point to shared infrastructure.",
        '("connect your domain" OR "custom domain setup") '
        "(customers OR examples OR showcase) platform",
        "Public setup documentation plus live customer examples or DNS evidence.",
    ),
    _lens(
        "Certificate ecosystem gaps",
        "Find overlooked multi-tenant apexes through certificate naming patterns.",
        '("powered by" OR "hosted on") (subdomain OR tenant OR microsite) platform customers',
        "A stable apex with many organization-bearing public certificate names.",
    ),
    _lens(
        "Premium partner directories",
        "Find directories where membership implies client volume or spend.",
        'intitle:(partners OR directory) (attribution OR retention OR ecommerce OR "paid media") '
        "(certified OR premier)",
        "An enumerable roster with public company pages or domains.",
    ),
    _lens(
        "Certification rosters",
        "Find companies investing in difficult commercial certifications.",
        '(certified OR accredited OR specialist) "partner directory" '
        "(marketing OR commerce OR cloud OR media)",
        "A current certification roster tied to paid operational capability.",
    ),
    _lens(
        "Implementation partner locators",
        "Find agencies implementing costly business systems.",
        '("find a partner" OR "solution partner") '
        "(implementation OR migration OR integration) directory",
        "Public profiles with specialties, regions, and company domains.",
    ),
    _lens(
        "Customer-story indexes",
        "Find products whose stories enumerate mature operators.",
        'intitle:("customer stories" OR "customer success") '
        "(commerce OR agency OR marketing) platform",
        "A stable customer index plus evidence the underlying product is paid.",
    ),
    _lens(
        "Case-study libraries",
        "Find libraries revealing repeatable commercial programs.",
        "inurl:(case-studies OR customers) (campaign OR growth OR ecommerce OR transformation)",
        "An enumerable library with named companies and dated outcomes.",
    ),
    _lens(
        "Migration showcases",
        "Find brands recently completing expensive replatforming.",
        '("migration story" OR replatformed OR "migrated from") '
        "(commerce OR CRM OR website) customer",
        "A dated migration with an official company domain and operating context.",
    ),
    _lens(
        "Agency referral award rosters",
        "Find repeat-winning agencies that may introduce qualified paid-growth buyers.",
        "(winners OR shortlist OR finalists) (agency OR campaign) (awards OR effectiveness)",
        "A dated official roster naming the agency and work.",
    ),
    _lens(
        "Brand campaign awards",
        "Find advertisers funding ambitious current creative.",
        "(brand OR advertiser) (campaign winners OR effectiveness awards OR creative awards)",
        "An official entry connecting a brand to a recent campaign.",
    ),
    _lens(
        "Design and packaging awards",
        "Find product businesses investing in premium visual execution.",
        "(packaging OR design OR ecommerce) (award winners OR shortlist) brands",
        "A recent official roster with product or company links.",
    ),
    _lens(
        "Public ad-library indexes",
        "Find active advertisers with recurring creative refresh demand.",
        '("ad library" OR "creative library" OR "ads archive") (brands OR advertisers) public',
        "A stable public index showing recent advertisements by company.",
    ),
    _lens(
        "Campaign archive indexes",
        "Find brands with repeated launches and multi-format campaigns.",
        '("campaign archive" OR "work archive") (brand OR client) video advertising',
        "Dated campaign pages tied to official brand domains.",
    ),
    _lens(
        "Creative-effectiveness rankings",
        "Find marketers measured for commercial creative performance.",
        "(effectiveness OR performance) (rankings OR index OR winners) campaigns brands",
        "A credible recurring ranking with named advertisers or agencies.",
    ),
    _lens(
        "Agency client referral portfolios",
        "Find agencies whose retained-client portfolios may create opted-in introductions.",
        'intitle:(clients OR portfolio OR "our work") '
        '("paid social" OR "performance creative" OR "media buying")',
        "Five or more current client, campaign, or case-study records.",
    ),
    _lens(
        "Production-company rosters",
        "Find production operators serving recurring commercial accounts.",
        "(production OR studio) (directors roster OR client list OR recent work) commercial",
        "A current roster and multiple named commercial projects.",
    ),
    _lens(
        "Creative vendor collectives",
        "Find curated production networks selling to established brands.",
        "(production network OR creative collective OR vendor roster) brands commercial",
        "A curated public roster with brand or agency relationships.",
    ),
    _lens(
        "Growth hiring surfaces",
        "Find companies actively staffing creative and acquisition work.",
        "(site:boards.greenhouse.io OR site:jobs.lever.co OR site:jobs.ashbyhq.com) "
        '("creative director" OR "growth marketing" OR "video producer")',
        "Current company-authored roles tied to acquisition or creative operations.",
    ),
    _lens(
        "Marketing leadership hiring",
        "Find teams building senior demand-generation capacity.",
        '("VP Marketing" OR "Head of Growth" OR "Creative Operations") (hiring OR careers OR jobs)',
        "A current senior role on an official careers or ATS page.",
    ),
    _lens(
        "Agency referral hiring momentum",
        "Find agencies adding delivery capacity and likely to know teams with overflow demand.",
        "(agency OR studio) (hiring OR careers) (producer OR media buyer OR motion designer)",
        "Multiple current delivery roles on company-authored pages.",
    ),
    _lens(
        "Commerce implementation galleries",
        "Find scaled brands using specialist implementers.",
        "(implementation OR launch OR build) showcase (Shopify OR commerce OR storefront) brands",
        "An enumerable gallery with current company domains.",
    ),
    _lens(
        "App marketplace ecosystems",
        "Find mature vendors and firms around commercial software.",
        "(app marketplace OR integrations) (partners OR agencies OR solutions) ecommerce marketing",
        "Public listings with domains, reviews, or install evidence.",
    ),
    _lens(
        "Integration customer examples",
        "Find businesses named in integration and workflow examples.",
        "(integration OR workflow) (customer story OR example OR template) marketing commerce",
        "Official examples naming a current operating company.",
    ),
    _lens(
        "Brand-group portfolios",
        "Find groups operating multiple consumer brands.",
        '("our brands" OR portfolio) (brand group OR holding company OR consumer) ecommerce',
        "A parent-authored portfolio with active brand domains.",
    ),
    _lens(
        "Private-equity portfolios",
        "Find companies with institutional backing and growth mandates.",
        "(portfolio companies OR investments) (consumer OR ecommerce OR software) private equity",
        "A current investor portfolio linked to operating-company domains.",
    ),
    _lens(
        "Accelerator and cohort rosters",
        "Find later-stage cohort companies with traction evidence.",
        "(cohort OR portfolio OR demo day) (commerce OR SaaS OR consumer) accelerator",
        "A dated cohort roster plus independent maturity evidence.",
    ),
    _lens(
        "Wholesale brand catalogs",
        "Find brands with real distribution and product operations.",
        "(brand directory OR vendor catalog OR wholesale marketplace) "
        "(stockists OR retailers OR suppliers)",
        "An enumerable catalog with official brand pages or domains.",
    ),
    _lens(
        "Retail stockist networks",
        "Find product companies distributed across retailers.",
        "(stockists OR store locator OR where to buy) brands directory",
        "Public distribution evidence spanning multiple locations or retailers.",
    ),
    _lens(
        "Marketplace brand indexes",
        "Find established sellers on curated marketplaces.",
        "(brands OR sellers) directory (marketplace OR retailer) premium",
        "A bounded marketplace roster with operating brand identity.",
    ),
    _lens(
        "Trade-show exhibitor lists",
        "Find companies spending to launch, distribute, or sell in person.",
        "(exhibitor list OR exhibitor directory) (retail OR ecommerce OR technology OR marketing)",
        "A current official exhibitor roster with company links.",
    ),
    _lens(
        "Conference sponsor rosters",
        "Find businesses with category-marketing budgets.",
        "(sponsors OR partners) (conference OR summit) (commerce OR marketing OR SaaS)",
        "A dated sponsor roster with tier or company link.",
    ),
    _lens(
        "Professional associations",
        "Find credible member directories in active categories.",
        "(member directory OR members) (association OR council) (retail OR marketing OR software)",
        "A current, public, enumerable member roster.",
    ),
    _lens(
        "Franchise and location directories",
        "Find operators with distributed local creative needs.",
        "(locations OR franchise directory OR find a location) "
        "(fitness OR hospitality OR services OR retail)",
        "A company-authored location network with meaningful breadth.",
    ),
    _lens(
        "Product-launch indexes",
        "Find brands repeatedly shipping products and campaigns.",
        "(new launch OR product launches OR release calendar) (brands OR ecommerce) directory",
        "Dated launches tied to official product-company domains.",
    ),
    _lens(
        "Crowdfunded growth graduates",
        "Find campaign-backed brands now operating at scale.",
        "(funded OR campaign success OR alumni) (Kickstarter OR Indiegogo) retail brand",
        "A funded campaign plus current sales, distribution, or team evidence.",
    ),
    _lens(
        "Retail-tech customer directories",
        "Find scaled merchants through tools they use.",
        "(customers OR merchants OR brands) "
        "(retail technology OR loyalty OR POS OR personalization)",
        "An official customer roster from a paid retail platform.",
    ),
    _lens(
        "CRM and lifecycle ecosystems",
        "Find teams investing in retention and customer communication.",
        "(customer stories OR partners) (CRM OR lifecycle OR email marketing OR retention)",
        "Proof of a paid retention stack and current company operation.",
    ),
    _lens(
        "Affiliate commerce ecosystems",
        "Find brands running structured acquisition partnerships.",
        "(customers OR brands OR advertisers) "
        "(affiliate platform OR creator commerce OR partnership marketing)",
        "An official advertiser roster from a paid platform.",
    ),
    _lens(
        "Regional specialist directories",
        "Find qualified operators outside saturated English lists.",
        "(agency directory OR partner directory OR member directory) "
        "(digital OR commerce OR creative)",
        "A credible country-specific roster with public domains.",
    ),
    _lens(
        "Commercial photography rosters",
        "Find studios delivering recurring product, fashion, food, and lifestyle work.",
        '("commercial photography" OR "product photography") '
        '(studio OR agency) (clients OR portfolio OR "our work")',
        "A current first-party portfolio with multiple commercial clients and contact route.",
    ),
    _lens(
        "Video production networks",
        "Find production companies with continuous brand and agency throughput.",
        '("video production company" OR "commercial production") '
        "(roster OR clients OR case studies OR work)",
        "Recent credited productions, recognizable clients, and an official company domain.",
    ),
    _lens(
        "Post and VFX studios",
        "Find specialists with persistent editing, motion, CGI, and VFX capacity needs.",
        '("post-production" OR VFX OR CGI OR "motion design") '
        "(studio OR company) (reel OR work OR clients)",
        "A current reel/work archive plus team, hiring, or multi-client evidence.",
    ),
    _lens(
        "Performance-creative agency buyers",
        (
            "Find retained-client agencies with recurring production throughput that may buy "
            "one embedded specialist lane."
        ),
        '("performance creative" OR "paid social creative" OR "creative testing") '
        '(agency OR studio) (case studies OR clients OR retainer OR overflow OR "white-label")',
        (
            "First-party performance language, repeat client delivery, current output, and "
            "production-team or overflow evidence."
        ),
    ),
    _lens(
        "Production association members",
        "Find established commercial production and post-production members.",
        "(member directory OR members) (production company OR post-production) "
        "(advertising OR commercial)",
        "A current association roster plus first-party work and capacity evidence.",
    ),
    _lens(
        "Agency specialist referral rosters",
        "Find agencies already coordinating specialists and capable of opted-in referrals.",
        '("talent roster" OR "creator network" OR freelancers OR contractors) '
        "(agency OR production OR creative)",
        "An official roster or hiring page indicating repeat specialist deployment.",
    ),
    _lens(
        "White-label creative delivery",
        "Find agencies buying overflow production for their own clients.",
        '("white-label" OR "overflow production" OR "production partner") '
        "(photo OR video OR creative) agency",
        "Explicit partner/overflow positioning and evidence of recurring client work.",
    ),
    _lens(
        "Agency referral credit trails",
        "Find studios credited on current work and use credits to resolve direct brand buyers.",
        '("production by" OR "photography by" OR "post by" OR "film by") '
        "(campaign OR commercial OR launch)",
        "A first-party project credit resolving to an official specialist company site.",
    ),
    _lens(
        "Creative-subscription customer indexes",
        (
            "Find teams that have publicly used a creative subscription or "
            "creative-as-a-service provider."
        ),
        '("customer stories" OR customers OR portfolio) '
        '("creative subscription" OR "creative-as-a-service" OR "unlimited design" '
        'OR "on-demand creative")',
        (
            "A provider-authored, enumerable customer index; record whether the relationship "
            "is current, historical, or unknown without inferring switching intent."
        ),
    ),
    _lens(
        "Performance-creative system customers",
        "Find advertisers investing in systematic creative testing and analytics.",
        '(customers OR "customer stories" OR case-studies) '
        '("creative analytics" OR "creative intelligence" OR "ad creative testing" '
        'OR "creative performance")',
        (
            "An official customer or case-study index connecting a named company to repeated "
            "creative testing, iteration, or measurement."
        ),
    ),
    _lens(
        "Creative-operations platform customers",
        "Find in-house teams operating a high-throughput creative workflow.",
        '(customers OR "customer stories" OR showcase) '
        '("creative operations" OR DAM OR "creative automation" OR proofing '
        'OR "content operations")',
        (
            "Official workflow evidence naming the customer and a recurring intake, review, "
            "versioning, approval, or asset-distribution problem."
        ),
    ),
    _lens(
        "Creator and UGC platform customers",
        "Find brands and agencies commissioning recurring social-first creator assets.",
        '(brands OR customers OR "success stories") '
        '("creator platform" OR "UGC platform" OR "influencer marketing" '
        'OR "creator content")',
        (
            "A platform-authored customer roster or dated program showing repeated brand or "
            "agency content production, not a creator-only marketplace listing."
        ),
    ),
    _lens(
        "Ad-platform campaign success indexes",
        "Find advertisers with current multi-format paid campaign programs.",
        '("success stories" OR "case studies" OR awards OR campaigns) '
        '(advertisers OR brands) (Meta OR TikTok OR YouTube OR "Amazon Ads" '
        'OR "retail media")',
        (
            "A first-party platform or official award index with a named advertiser, dated "
            "campaign, formats, markets, or measurable creative iteration."
        ),
    ),
    _lens(
        "App and gaming UA customer rosters",
        "Find app and game marketers running continuous user-acquisition creative.",
        '(customers OR "success stories" OR showcase OR awards) '
        '("user acquisition" OR "mobile measurement" OR "app growth" OR "game marketing") '
        "(creative OR campaign OR ads)",
        (
            "An official MMP, attribution, ad-network, app-growth, or UA-award source naming "
            "operators and recurring campaign or creative-testing activity."
        ),
    ),
    _lens(
        "Precise creative-capacity hiring",
        "Find teams explicitly adding the roles that reveal creative throughput pressure.",
        "(site:boards.greenhouse.io OR site:jobs.lever.co OR site:jobs.ashbyhq.com "
        'OR inurl:careers) ("creative operations" OR "performance creative strategist" '
        'OR "creative producer" OR "motion designer" OR "video editor" '
        'OR "generative AI creative") ("manage external agencies" OR "manage freelancers" '
        'OR "high-volume" OR "rapid iteration" OR localization OR versioning '
        'OR "asset versioning")',
        (
            "A current first-party or official ATS role with company-authored responsibility "
            "for external agencies or freelancers, high-volume or rapid creative iteration, "
            "localization, or asset versioning."
        ),
    ),
    _lens(
        "Agency delivery-capacity evidence",
        "Find agencies with recurring client delivery and a plausible outside specialist lane.",
        '(agency OR studio) (retainer OR "always-on" OR embedded OR "dedicated team") '
        '("performance creative" OR UGC OR "paid social" OR video OR motion) '
        '(overflow OR "white-label" OR hiring OR production)',
        (
            "First-party retained-client work and current output plus explicit delivery-team, "
            "hiring, partner, white-label, overflow, or production-capacity evidence."
        ),
    ),
)


BUYER_SOURCE_MISSIONS: tuple[dict[str, Any], ...] = (
    {
        "source_family": "competitor_customer_index",
        "relationship_type": "buyer",
        "mission": "Creative-subscription customer proof",
        "search_query": (
            '(customers OR "customer stories" OR "case studies") '
            '("creative subscription" OR "creative-as-a-service" OR "unlimited design" '
            'OR "on-demand creative")'
        ),
        "ecosystem_examples": [
            "Superside",
            "Design Pickle",
            "Kimp",
            "Penji",
        ],
        "buyer_archetypes": [
            "paid_growth_brand",
            "enterprise_creative_team",
            "agency_delivery_team",
        ],
        "source_pattern": (
            "Public customer, case-study, or portfolio indexes from creative-subscription, "
            "unlimited-design, on-demand-design, and creative-as-a-service providers."
        ),
        "required_evidence": (
            "An enumerable provider-authored index and a named customer's recurring creative "
            "workflow, asset volume, or outside-capacity use."
        ),
        "non_inference": (
            "A listed customer proves neither a current contract nor dissatisfaction, "
            "availability, switching intent, or permission to contact."
        ),
    },
    {
        "source_family": "performance_creative_system",
        "relationship_type": "buyer",
        "mission": "Performance-creative analytics and testing",
        "search_query": (
            '(customers OR "customer stories" OR "case studies") '
            '("creative analytics" OR "creative intelligence" OR "creative testing" '
            'OR "performance creative")'
        ),
        "ecosystem_examples": [
            "Motion",
            "Smartly",
            "Marpipe",
            "VidMob",
            "CreativeX",
        ],
        "buyer_archetypes": [
            "paid_growth_brand",
            "consumer_app_game",
            "agency_delivery_team",
        ],
        "source_pattern": (
            "Official customer and case-study libraries for creative analytics, ad-creative "
            "testing, creative intelligence, catalog/SKU variation, and performance-creative "
            "systems."
        ),
        "required_evidence": (
            "Named companies plus repeated experimentation, refresh cadence, formats, SKUs, "
            "markets, localization, or measured creative outcomes."
        ),
        "non_inference": (
            "Tool adoption is distribution and workflow evidence, not proof of an outside "
            "capacity gap or AI-permissible work."
        ),
    },
    {
        "source_family": "creative_operations_platform",
        "relationship_type": "buyer",
        "mission": "Creative-operations workflow customers",
        "search_query": (
            '(customers OR "customer stories" OR "case studies") '
            '("creative operations" OR DAM OR proofing OR "creative automation" '
            'OR localization OR "content operations")'
        ),
        "ecosystem_examples": [
            "Air",
            "Bynder",
            "Frontify",
            "Celtra",
            "Canto",
            "Adobe Firefly",
        ],
        "buyer_archetypes": [
            "enterprise_creative_team",
            "multi_brand_operator",
            "saas_product_marketing",
        ],
        "source_pattern": (
            "Official DAM, creative automation, proofing, review, localization, brand-system, "
            "and content-operations customer indexes."
        ),
        "required_evidence": (
            "A named team and recurring intake, approval, versioning, localization, asset reuse, "
            "external collaboration, multi-brand governance, or multi-channel production workflow."
        ),
        "non_inference": (
            "A paid creative stack or governed generative-AI workflow proves readiness and "
            "workflow investment, not budget, unmet need, outsourcing intent, or a "
            "one-specialist fit."
        ),
    },
    {
        "source_family": "creator_ugc_platform",
        "relationship_type": "buyer",
        "mission": "Creator and UGC program customers",
        "search_query": (
            '(brands OR customers OR campaigns OR "success stories") '
            '("creator platform" OR "UGC platform" OR "creator content" '
            'OR "influencer marketing")'
        ),
        "ecosystem_examples": [
            "CreatorIQ",
            "Aspire",
            "Insense",
            "GRIN",
            "Billo",
        ],
        "buyer_archetypes": [
            "paid_growth_brand",
            "multi_brand_operator",
            "agency_delivery_team",
        ],
        "source_pattern": (
            "Public customer, campaign, brand, and success-story indexes from creator, UGC, "
            "influencer, and social-content workflow platforms."
        ),
        "required_evidence": (
            "Named brand or agency programs with recurring briefs, asset counts, channels, "
            "formats, markets, or refresh activity."
        ),
        "non_inference": (
            "Creator-program participation does not prove generative-AI rights, specialist "
            "demand, or dissatisfaction with the current workflow."
        ),
    },
    {
        "source_family": "ad_campaign_index",
        "relationship_type": "buyer",
        "mission": "Ad-platform and campaign proof",
        "search_query": (
            '("success stories" OR campaigns OR inspiration OR awards) '
            '(advertisers OR brands) (TikTok OR Meta OR YouTube OR "Amazon Ads" '
            'OR "retail media")'
        ),
        "ecosystem_examples": [
            "TikTok for Business",
            "Meta for Business",
            "Google Ads",
            "Amazon Ads",
            "Snapchat for Business",
            "Pinterest Business",
        ],
        "buyer_archetypes": [
            "paid_growth_brand",
            "consumer_app_game",
            "multi_brand_operator",
        ],
        "source_pattern": (
            "Official ad-platform success stories, public campaign centers, effectiveness "
            "awards, advertiser indexes, and repeatable public ad-library recipes."
        ),
        "required_evidence": (
            "A dated advertiser-to-campaign connection with formats, channels, markets, refresh "
            "patterns, or measured distribution outcomes."
        ),
        "non_inference": (
            "Ad presence or ad count alone does not establish client workload, spend, creative "
            "ownership, or a production-capacity gap."
        ),
    },
    {
        "source_family": "app_gaming_ua",
        "relationship_type": "buyer",
        "mission": "App and gaming user-acquisition ecosystems",
        "search_query": (
            '(customers OR "success stories" OR "case studies") '
            '("user acquisition" OR MMP OR attribution OR "mobile measurement" '
            'OR "app growth") (app OR gaming)'
        ),
        "ecosystem_examples": [
            "AppsFlyer",
            "Adjust",
            "Moloco",
            "Singular",
            "AppLovin",
        ],
        "buyer_archetypes": ["consumer_app_game"],
        "source_pattern": (
            "Public MMP, attribution, mobile-growth, ad-network, monetization, app-marketing, "
            "and UA-award customer or case-study indexes."
        ),
        "required_evidence": (
            "Named apps, publishers, or studios with current acquisition programs, repeated "
            "creative testing, launches, live ops, geographies, or ad formats."
        ),
        "non_inference": (
            "Installs, revenue claims, or platform usage do not prove specialist capacity need, "
            "creative rights, or willingness to outsource."
        ),
    },
    {
        "source_family": "commerce_growth_platform",
        "relationship_type": "buyer",
        "mission": "Commerce, lifecycle, and subscription growth customers",
        "search_query": (
            '(customers OR "customer stories" OR "case studies") '
            "(Klaviyo OR Attentive OR Recharge OR Yotpo OR Gorgias OR Braze) "
            '("email marketing" OR SMS OR lifecycle OR retention OR subscription OR loyalty '
            'OR reviews OR "customer service" OR "customer engagement")'
        ),
        "ecosystem_examples": [
            "Klaviyo",
            "Attentive",
            "Recharge",
            "Yotpo",
            "Gorgias",
            "Braze",
        ],
        "buyer_archetypes": [
            "paid_growth_brand",
            "multi_brand_operator",
            "enterprise_creative_team",
        ],
        "source_pattern": (
            "First-party commerce-marketing, lifecycle-messaging, subscription, loyalty, "
            "reviews, and customer-engagement customer or case-study indexes."
        ),
        "required_evidence": (
            "A provider-authored named-customer page, dated when available, describing recurring "
            "campaigns, lifecycle flows, messages, launches, subscription or retention programs, "
            "segmentation, personalization, channels, or markets; a logo alone is insufficient."
        ),
        "non_inference": (
            "Platform usage or a customer story does not prove creative volume, an outside "
            "capacity gap, dissatisfaction, switching intent, AI-permissible work, a current "
            "contract, or permission to contact."
        ),
    },
    {
        "source_family": "creative_hiring_index",
        "relationship_type": "buyer",
        "mission": "Precise creative-capacity hiring",
        "search_query": (
            "(site:boards.greenhouse.io OR site:jobs.lever.co OR site:jobs.ashbyhq.com "
            'OR inurl:careers) ("creative operations" OR "performance creative strategist" '
            'OR "creative producer" OR "motion designer" OR "video editor" '
            'OR "generative AI creative") ("manage external agencies" OR "manage freelancers" '
            'OR "high-volume" OR "rapid iteration" OR localization OR versioning '
            'OR "asset versioning")'
        ),
        "ecosystem_examples": ["Greenhouse", "Lever", "Ashby"],
        "buyer_archetypes": [
            "paid_growth_brand",
            "consumer_app_game",
            "enterprise_creative_team",
            "agency_delivery_team",
        ],
        "source_pattern": (
            "Current first-party careers or public ATS indexes for creative operations, "
            "performance creative, creative production, motion, video editing, and generative "
            "creative roles."
        ),
        "required_evidence": (
            "An exact current role and company-authored responsibilities tied to managing "
            "external agencies or freelancers, high-volume or rapid creative iteration, "
            "localization or asset versioning, and recurring production or paid distribution."
        ),
        "non_inference": (
            "Generic growth hiring, an expired role, or one job post is only a timing clue; it "
            "does not prove an outsourcing preference or a budget."
        ),
    },
    {
        "source_family": "agency_delivery_capacity",
        "relationship_type": "buyer",
        "mission": "Direct agency delivery capacity",
        "search_query": (
            '("performance creative" OR "paid social creative" OR UGC OR video OR motion) '
            '(agency OR studio) (retainer OR embedded OR overflow OR "white-label" '
            'OR hiring OR "production partner")'
        ),
        "ecosystem_examples": [
            "Common Thread Collective",
            "Power Digital",
            "Structured",
            "Mekanism",
        ],
        "buyer_archetypes": ["agency_delivery_team"],
        "source_pattern": (
            "First-party agency work, client, service, team, hiring, partner, embedded-delivery, "
            "white-label, overflow, production, and current-output indexes."
        ),
        "required_evidence": (
            "Recurring retained-client performance creative plus current photo/video throughput "
            "and an explicit delivery or outside-capacity signal."
        ),
        "non_inference": (
            "An agency's own ads, awards, or client logo wall alone do not prove direct buyer "
            "need; keep brand-access-only ecosystems referral-only."
        ),
    },
)

SEGMENT_PROFILES: tuple[dict[str, str], ...] = (
    {
        "name": "paid-growth consumer brands",
        "category": "Ecommerce",
        "query": '(consumer OR DTC OR ecommerce) ("paid social" OR acquisition OR campaign)',
        "relationship_type": "buyer",
    },
    {
        "name": "consumer apps",
        "category": "Consumer / Digital",
        "query": '("consumer app" OR mobile app) (subscription OR acquisition OR growth)',
        "relationship_type": "buyer",
    },
    {
        "name": "digital products",
        "category": "Consumer / Digital",
        "query": '("digital product" OR marketplace OR platform) (consumer OR creator) growth',
        "relationship_type": "buyer",
    },
    {
        "name": "gaming and interactive entertainment",
        "category": "Consumer / Digital",
        "query": '(gaming OR game studio OR "interactive entertainment") (launch OR campaign)',
        "relationship_type": "buyer",
    },
    {
        "name": "subscription businesses",
        "category": "Consumer / Digital",
        "query": "(subscription OR membership) (consumer OR ecommerce) (acquisition OR retention)",
        "relationship_type": "buyer",
    },
    {
        "name": "multi-brand consumer portfolios",
        "category": "Ecommerce",
        "query": '("our brands" OR portfolio OR holdings) (consumer OR ecommerce)',
        "relationship_type": "buyer",
    },
    {
        "name": "warm SaaS and AI adjacencies",
        "category": "B2B / SaaS",
        "query": '(SaaS OR "AI product") ("paid social" OR launch OR product marketing)',
        "relationship_type": "buyer",
    },
    {
        "name": "in-house creative operations teams",
        "category": "Consumer / Digital",
        "query": (
            '("in-house creative" OR "creative operations" OR "brand studio") '
            '("content velocity" OR "creative requests" OR production OR backlog)'
        ),
        "relationship_type": "buyer",
    },
    {
        "name": "multi-market content operations",
        "category": "Ecommerce",
        "query": (
            '(localization OR transcreation OR "multi-market" OR versioning) '
            "(creative OR campaign OR content) (brand OR ecommerce OR app)"
        ),
        "relationship_type": "buyer",
    },
    {
        "name": "production and performance agencies",
        "category": "Agency",
        "query": (
            '("performance creative" OR "paid social" OR "ecommerce content" OR UGC OR '
            '"video production" OR "product photography" OR "motion design" OR '
            '"post-production") agency '
            '(clients OR "case studies" OR retainer OR overflow OR "white-label")'
        ),
        "relationship_type": "buyer",
    },
    {
        "name": "specialist agency referral partners",
        "category": "Agency",
        "query": 'agency ("paid social" OR "performance creative" OR production) clients',
        "relationship_type": "referral_partner",
    },
)

REGION_VARIATIONS: tuple[dict[str, str], ...] = (
    {"name": "Global English", "query": ""},
    {"name": "United States and Canada", "query": '("United States" OR Canada OR US)'},
    {"name": "United Kingdom and Ireland", "query": '(UK OR "United Kingdom" OR Ireland)'},
    {"name": "DACH", "query": "(Deutschland OR Österreich OR Schweiz OR Agentur)"},
    {"name": "France and Belgium", "query": "(France OR Belgique OR agence OR marques)"},
    {"name": "Benelux", "query": "(Nederland OR België OR Luxembourg OR bureau)"},
    {"name": "Nordics", "query": "(Sweden OR Norway OR Denmark OR Finland OR Sverige)"},
    {"name": "Spain and Portugal", "query": "(España OR Portugal OR agencia OR marcas)"},
    {"name": "Italy", "query": "(Italia OR agenzia OR marchi)"},
    {"name": "Latin America", "query": "(México OR Brasil OR Colombia OR Argentina OR LATAM)"},
    {"name": "Australia and New Zealand", "query": '(Australia OR "New Zealand" OR ANZ)'},
    {"name": "Middle East", "query": '(UAE OR Dubai OR Saudi OR Qatar OR "Middle East")'},
    {
        "name": "India and Southeast Asia",
        "query": "(India OR Singapore OR Indonesia OR Malaysia OR Philippines)",
    },
    {"name": "Japan and South Korea", "query": "(Japan OR 日本 OR Korea OR 한국)"},
)

PROOF_VARIATIONS: tuple[dict[str, str], ...] = (
    {
        "name": "Recurring paid acquisition",
        "query": '("paid social" OR media buying OR acquisition OR ROAS)',
        "evidence": "Evidence of ongoing paid-distribution work.",
    },
    {
        "name": "Current creative output",
        "query": "(campaign OR video OR launch OR creative)",
        "evidence": "Recent company-linked creative or campaign output.",
    },
    {
        "name": "Operating scale",
        "query": '("our team" OR offices OR locations OR enterprise)',
        "evidence": "Public evidence of team, geographic, or customer scale.",
    },
    {
        "name": "Commerce activity",
        "query": "(collection OR catalog OR products OR stockists)",
        "evidence": "An active catalog, launch cadence, or distribution footprint.",
    },
    {
        "name": "Enterprise customers",
        "query": "(enterprise OR clients OR customers OR case studies)",
        "evidence": "Named relationships with established organizations.",
    },
    {
        "name": "Expansion timing",
        "query": "(expansion OR launched OR partnership OR appointed)",
        "evidence": "A dated event suggesting current growth or change.",
    },
    {
        "name": "Institutional backing",
        "query": "(portfolio OR acquired OR investment OR backed)",
        "evidence": "Public ownership context without inferring revenue.",
    },
    {
        "name": "Multi-market presence",
        "query": "(global OR international OR countries OR markets)",
        "evidence": "Company-authored proof of operation across markets.",
    },
    {
        "name": "Retail distribution",
        "query": '(retailers OR wholesale OR stores OR "where to buy")',
        "evidence": "Current multi-channel or physical distribution.",
    },
    {
        "name": "Active hiring",
        "query": '(hiring OR careers OR jobs OR "join our team")',
        "evidence": "Current growth, creative, or operations roles.",
    },
    {
        "name": "Documented outcomes",
        "query": "(results OR case study OR growth OR conversion)",
        "evidence": "Specific public outcomes tied to the company.",
    },
    {
        "name": "Paid operating stack",
        "query": "(certified OR integration OR technology partner OR platform)",
        "evidence": "A paid-tool, certification, or integration commitment.",
    },
    {
        "name": "Recurring creative refresh volume",
        "query": (
            '("creative refresh" OR "content velocity" OR "asset volume" OR '
            '"always-on content" OR "weekly creative")'
        ),
        "evidence": (
            "Observed recurring asset counts, refresh cadence, queued requests, or repeated "
            "campaign production."
        ),
    },
    {
        "name": "Outside creative capacity",
        "query": (
            '(overflow OR "external creative" OR "extended team" OR "production partner" '
            'OR embedded OR "staff augmentation")'
        ),
        "evidence": (
            "First-party evidence that outside specialists, overflow partners, or embedded "
            "creative capacity are part of the delivery model."
        ),
    },
    {
        "name": "Human-directed AI workflow adoption",
        "query": (
            '("AI creative workflow" OR "generative AI" OR "AI-assisted creative" '
            'OR "human-led AI")'
        ),
        "evidence": (
            "Company-authored adoption of AI-assisted creative work with human direction, "
            "review, brand control, or production ownership."
        ),
    },
    {
        "name": "Versioning and localization load",
        "query": (
            '(versioning OR localization OR transcreation OR "multi-format" '
            'OR "multiple aspect ratios" OR "multiple markets")'
        ),
        "evidence": (
            "Observed need to adapt creative across formats, channels, languages, markets, or "
            "product variants."
        ),
    },
)

OPERATOR_VARIATIONS: tuple[dict[str, str], ...] = (
    {"name": "Broad discovery", "query": ""},
    {"name": "Title index", "query": "intitle:(directory OR partners OR customers)"},
    {"name": "URL index", "query": "inurl:(directory OR partners OR customers OR portfolio)"},
    {"name": "PDF roster", "query": "filetype:pdf (directory OR roster OR exhibitors OR winners)"},
    {"name": "Named lists", "query": '("member list" OR "client list" OR "brand directory")'},
    {"name": "Official examples", "query": '(showcase OR examples OR "featured customers")'},
    {"name": "Recent index", "query": "(latest OR recent OR new OR current)"},
    {"name": "Proof pages", "query": "inurl:(case-study OR work OR success OR stories)"},
    {"name": "Commercial roster", "query": "(certified OR premier OR sponsor OR exhibitor)"},
    {"name": "Public enumeration", "query": "(A-Z OR browse OR filter OR categories)"},
)

RECENCY_VARIATIONS: tuple[dict[str, str], ...] = (
    {"name": "Current year", "template": "{year}"},
    {"name": "Last two years", "template": "{previous}..{year}"},
    {"name": "Rolling three years", "template": "after:{older}"},
    {"name": "Recently updated", "template": '(latest OR current OR "updated {year}")'},
    {"name": "Current roster", "template": "(current OR {year})"},
    {"name": "All-time index", "template": ""},
)


def _search_urls(query: str) -> list[dict[str, str]]:
    encoded = quote(query, safe="")
    return [
        {"engine": "DuckDuckGo", "url": f"https://duckduckgo.com/?q={encoded}"},
        {"engine": "Brave", "url": f"https://search.brave.com/search?q={encoded}"},
        {"engine": "Bing", "url": f"https://www.bing.com/search?q={encoded}"},
    ]


def _select(
    pool: tuple[dict[str, str], ...], seed: int, count: int, step: int
) -> list[dict[str, str]]:
    start = seed % len(pool)
    limit = min(count, len(pool))
    selected: list[dict[str, str]] = []
    visited_indexes: set[int] = set()
    index = start
    while len(selected) < limit and index not in visited_indexes:
        visited_indexes.add(index)
        item = pool[index]
        if item not in selected:
            selected.append(item)
        index = (index + step) % len(pool)
    # A step that shares a factor with the pool length visits only one cycle. Fill from a
    # deterministic linear walk rather than waiting forever for an unreachable item.
    for offset in range(len(pool)):
        if len(selected) >= limit:
            break
        item = pool[(start + offset) % len(pool)]
        if item not in selected:
            selected.append(item)
    return selected


def _focus_profile(sources: list[dict[str, Any]], seed: int) -> dict[str, str]:
    direct_profiles = [
        profile
        for profile in SEGMENT_PROFILES
        if profile["relationship_type"] == "buyer"
        and profile["name"] != "warm SaaS and AI adjacencies"
        and profile["name"] != "production and performance agencies"
    ]
    categories = sorted({profile["category"] for profile in direct_profiles})
    counts = {
        category: sum(
            int(source.get("candidate_count") or 0)
            for source in sources
            if source.get("category") == category
        )
        for category in categories
    }
    category = min(counts, key=lambda name: (counts[name], name))
    profiles = [profile for profile in direct_profiles if profile["category"] == category]
    return profiles[seed % len(profiles)]


def _mission(
    lens: dict[str, str],
    *,
    segment: dict[str, str],
    region: dict[str, str],
    proof: dict[str, str],
    operator: dict[str, str],
    recency: dict[str, str],
    year: int,
    known_sources: list[str],
) -> dict[str, Any]:
    exclusions = " ".join(f"-site:{apex}" for apex in known_sources[:4])
    recency_query = recency["template"].format(year=year, previous=year - 1, older=year - 3)
    query = " ".join(
        part
        for part in (
            lens["query"],
            f"({segment['query']})",
            region["query"],
            proof["query"],
            operator["query"],
            recency_query,
            exclusions,
        )
        if part
    )
    if segment["name"] == "production and performance agencies":
        next_step = (
            "Verify first-party retained-client work, current photo/video throughput, production "
            "operations, and a plausible one-lane capacity gap before treating the agency as a "
            "buyer. Add only a bounded source or repeatable index."
        )
    elif segment["relationship_type"] == "referral_partner":
        next_step = (
            "Verify relevant brand relationships and an opted-in introduction path, then keep "
            "the agency in the referral-partner lane unless separate direct production evidence "
            "clears the agency-buyer gate."
        )
    else:
        next_step = (
            "Verify the enumeration pattern, estimate the public pool, then add only the apex "
            "or repeatable index to Sources for a bounded test. Promote buyer candidates to "
            "Accounts only after review."
        )
    return {
        "name": lens["name"],
        "intent": lens["intent"],
        "segment": segment["name"],
        "relationship_type": segment["relationship_type"],
        "region": region["name"],
        "proof_mode": proof["name"],
        "operator_mode": operator["name"],
        "recency": recency["name"],
        "query": query,
        "evidence": f"{lens['evidence']} {proof['evidence']}",
        "next_step": next_step,
        "cost": "Free",
        "urls": _search_urls(query),
    }


def _source_probe(source: dict[str, Any]) -> dict[str, Any]:
    apex = str(source["apex"])
    archive_query = urlencode(
        {
            "url": f"*.{apex}/*",
            "output": "json",
            "fl": "timestamp,original,statuscode,digest",
            "filter": "statuscode:200",
            "collapse": "urlkey",
            "limit": "100",
        }
    )
    tenant_query = f'site:{apex} -www "{apex}"'
    github_query = quote(f'"{apex}"', safe="")
    return {
        "apex": apex,
        "category": source.get("category") or "Other",
        "opportunity_score": int(source.get("opportunity_score") or 0),
        "candidate_count": int(source.get("candidate_count") or 0),
        "historical_count": source.get("indexed_names"),
        "uninspected": max(
            int(source.get("indexed_names") or 0) - int(source.get("candidate_count") or 0),
            0,
        ),
        "links": [
            {"name": "CT names", "url": f"https://crt.sh/?q=%25.{quote(apex, safe='')}"},
            {
                "name": "Archive footprint",
                "url": f"https://web.archive.org/cdx/search/cdx?{archive_query}",
            },
            {"name": "Search tenants", "url": _search_urls(tenant_query)[0]["url"]},
            {
                "name": "URLScan hosts",
                "url": f"https://urlscan.io/search/#domain:{quote(apex, safe='.')}",
            },
            {
                "name": "Public code refs",
                "url": f"https://github.com/search?q={github_query}&type=code",
            },
        ],
    }


def build_research_brief(
    sources: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    seed = current.toordinal()
    focus = _focus_profile(sources, seed)
    enabled = [source for source in sources if source.get("enabled")]
    ranked = sorted(
        enabled or sources,
        key=lambda source: (
            int(source.get("opportunity_score") or 0),
            int(source.get("high_priority_count") or 0),
            int(source.get("candidate_count") or 0),
        ),
        reverse=True,
    )
    # Walk the 56-item permutation in seven rolling, non-overlapping daily blocks.
    # Every seven consecutive days cover the entire pool exactly once.
    lens_seed = seed * DAILY_MISSION_COUNT * 11
    lenses = _select(SEARCH_LENSES, lens_seed, DAILY_MISSION_COUNT, 11)
    agency_buyer = next(
        profile
        for profile in SEGMENT_PROFILES
        if profile["name"] == "production and performance agencies"
    )
    agency_referral = next(
        profile
        for profile in SEGMENT_PROFILES
        if profile["relationship_type"] == "referral_partner"
    )
    remaining_profiles = tuple(
        profile
        for profile in SEGMENT_PROFILES
        if profile != focus and profile != agency_buyer and profile != agency_referral
    )
    profiles = [focus, agency_buyer, agency_referral] + _select(
        remaining_profiles,
        seed * 3,
        DAILY_MISSION_COUNT - 3,
        3,
    )
    regions = _select(REGION_VARIATIONS, seed * 5, DAILY_MISSION_COUNT, 5)
    proofs = _select(PROOF_VARIATIONS, seed * 7, DAILY_MISSION_COUNT, 5)
    operators = _select(OPERATOR_VARIATIONS, seed * 11, DAILY_MISSION_COUNT, 3)
    recencies = _select(RECENCY_VARIATIONS, seed, DAILY_MISSION_COUNT, 5)
    known_sources = [str(source["apex"]) for source in ranked]
    dimensions = {
        "source_lenses": len(SEARCH_LENSES),
        "segments": len(SEGMENT_PROFILES),
        "regions": len(REGION_VARIATIONS),
        "proof_modes": len(PROOF_VARIATIONS),
        "operator_modes": len(OPERATOR_VARIATIONS),
        "recency_modes": len(RECENCY_VARIATIONS),
    }
    return {
        "generated_at": current.isoformat(),
        "cycle": seed % 997 + 1,
        "operating_contract": OPERATING_CONTRACT,
        "focus": {
            "segment": focus["name"],
            "relationship_type": focus["relationship_type"],
            "reason": (
                f"{focus['category']} currently has the smallest inspected direct-buyer "
                "source sample. The first mission prioritizes that gap; the remaining "
                "missions diversify the paid-growth beachhead and reserve separate "
                "agency-buyer and agency-referral missions."
            ),
        },
        "strategy_pool": {
            **dimensions,
            "combinations": prod(dimensions.values()),
            "daily_missions": DAILY_MISSION_COUNT,
        },
        "analysis_methods": list(ANALYSIS_METHODS),
        "public_sources": [
            {
                "name": "OpenStreetMap contributors",
                "url": "https://www.openstreetmap.org/copyright",
                "terms": "ODbL",
            },
            {
                "name": "Wikidata",
                "url": "https://www.wikidata.org/wiki/Wikidata:Data_access",
                "terms": "CC0",
            },
        ],
        "missions": [
            _mission(
                lens,
                segment=profiles[index % len(profiles)],
                region=regions[index % len(regions)],
                proof=proofs[index % len(proofs)],
                operator=operators[index % len(operators)],
                recency=recencies[index % len(recencies)],
                year=current.year,
                known_sources=known_sources,
            )
            for index, lens in enumerate(lenses)
        ],
        "source_probes": [_source_probe(source) for source in ranked[:6]],
        "source_leaders": ranked[:5],
    }
