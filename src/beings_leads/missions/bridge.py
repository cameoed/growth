"""Mission-only factual adapters. Original collectors, ledgers and Beings scores are untouched."""

import json
import math
import re
from datetime import timedelta
from urllib.parse import urlsplit

from ..value_utils import timezone_datetime as date
from .context import INCIDENTAL_PATH, matches

# This is a capability contract, not a list of automatic score bonuses.
CATALOG = {
    "portfolio.client": "Published, attributed client/project proof; not a current contract.",
    "portfolio.major_brand": "Client proof naming a recognized global brand; not project scale.",
    "production.commercial_work": "Explicit company-owned commercial production work.",
    "production.large_scope": "Explicit owned global/national/multi-market production scope.",
    "advertising.active_ads": "Verified scoreable active-ad count; not spend or project scale.",
    "advertising.unique_creatives": "Verified distinct creative count; not concept count.",
    "advertising.new_creatives_30d": "Verified scoreable new creatives in 30 days; not revenue.",
    "creative.format": "Confirmed observed format of a same-company creative asset.",
    "creative.product": "Confirmed observed product of a same-company creative asset.",
    "market.event_type": "Eligible resolved company-specific market event; not purchase intent.",
    "event.upcoming": "Explicit future dated eligible owned event; not intent to hire.",
    "demand.creative_openings": "Observed current creative openings; not actual hiring or budget.",
    "demand.photo_video_openings": "Observed current photo/video openings; not contractor intent.",
    "work.kind": "Cited current subject-verified work event type.",
    "work.recent_kind": "Subject-verified work event with a source date in the past 90 days.",
    "work.recurring_kind": "Same work event type independently dated in two calendar months "
    "within the past 90 days.",
    "work.scope": "Cited current subject-verified production scope.",
    "technology.vendor": "Observed first-party disclosed vendor; not demand or spend.",
    "company.employee_count": "Explicit observed same-company employee count, not estimates.",
    "company.annual_revenue_usd": "Observed annual USD revenue, not capital raised or budget.",
    "company.location": "Observed explicit same-company address/location.",
    "company.activity": "Observed company business activity; not its clients' activity.",
    "company.event_program": "Owned published event schedule with a current dated item.",
    "company.editorial_blog": "Company-operated editorial blog with a recent owned item.",
    "company.owned_storefront": "Observed owned shopping and checkout or fulfillment controls.",
    "company.news_publisher": "Observed owned news publisher identity and editorial surface.",
    "search.organic_ranked_results": "DataForSEO estimated US-English ranked organic results.",
}
# Add complete, source-policy-qualified scanner measurements, without inheriting any offer score.
ATOMIC_FIELDS = {
    "creative.asset_concept_family_count": {"": "creative.asset_families"},
    "creative.asset_format_count": {"": "creative.format_count"},
    "creative.aspect_family_count": {"": "creative.aspect_count"},
    "localization.declared_variant_count": {"": "localization.locale_count"},
    "hiring.creative_demand_current": {
        "current_creative_openings": "demand.creative_openings",
        "current_photo_video_openings": "demand.photo_video_openings",
    },
    "company.employee_count": {"": "company.employee_count"},
    "company.annual_revenue_usd": {"": "company.annual_revenue_usd"},
    "company.location": {"": "company.location"},
    "company.activity": {"": "company.activity"},
    "events.upcoming": {"": "event.upcoming"},
    "localization.locale_variant": {"locale": "localization.locale"},
    "measurement.public_funnel_schema": {"funnel_families": "measurement.funnel_family"},
    "content.blog_engine": {
        "recent_30d": "content.recent_30d",
        "active_months_180d": "content.active_months_180d",
        "visual_items": "content.visual_items",
    },
    "events.program_engine": {
        "upcoming_events": "event.upcoming_count",
        "recent_events": "event.recent_count",
        "distinct_months": "event.active_months",
    },
    "enterprise.trust_readiness": {"control_families": "technology.trust_control"},
    "brand.asset_governance": {
        "asset_categories": "brand.asset_category",
        "usage_rule_families": "brand.usage_rule",
    },
    "creator.program": {
        "channels": "creator.channel",
        "content_formats": "creator.format",
        "rights_categories": "creator.right",
    },
    "creator.usage_right": {"": "creator.right"},
    "product.release_current": {"release_type": "product.release_type"},
    "app.domain_association": {"platform": "app.platform"},
    "ai.product_role": {"": "ai.product_role"},
    "media.publishing_pulse": {"recent_30d": "media.recent_30d"},
}
CATALOG.update(
    {
        "creative.asset_families": "Static media URL groups; not semantic concepts or cadence.",
        "creative.format_count": "Observed media format diversity; not validated visual concepts.",
        "creative.aspect_count": "Observed aspect families; not recurring output or capacity.",
        "localization.locale_count": "Declared language variants; not production volume.",
        "localization.locale": "Observed declared locale; not active marketing in that country.",
        "measurement.funnel_family": "Publicly observed funnel types; not conversion performance.",
        "content.recent_30d": "Observed dated editorial items in 30 days; lower bound.",
        "content.active_months_180d": "Distinct months of dated editorial activity in 180 days.",
        "content.visual_items": "Observed visual editorial items; not production or ad spend.",
        "event.upcoming_count": "Observed dated upcoming events; not photography procurement.",
        "event.recent_count": "Observed recent public events; not confirmed future demand.",
        "event.active_months": "Distinct months of dated public programming in a qualified sample.",
        "technology.trust_control": "Disclosed trust controls; not independently certified.",
        "brand.asset_category": "Observed governed brand asset type; not production cadence.",
        "brand.usage_rule": "Published asset usage rules; not a purchasing requirement.",
        "creator.channel": "Published creator program channel; not actual contracted creators.",
        "creator.format": "Published creator program format; not verified delivered output.",
        "creator.right": "Published usage-rights category; not an executed license or contract.",
        "product.release_type": "Observed current product release type; not purchase intent.",
        "app.platform": "Observed domain/app platform association; not downloads or revenue.",
        "ai.product_role": "Observed AI product role; not willingness to buy services.",
        "media.recent_30d": "Observed dated media items in 30 days; bounded-surface lower bound.",
    }
)
NUMERIC = {
    "advertising.active_ads",
    "advertising.unique_creatives",
    "advertising.new_creatives_30d",
    "demand.creative_openings",
    "demand.photo_video_openings",
    "company.employee_count",
    "company.annual_revenue_usd",
    "creative.asset_families",
    "creative.format_count",
    "creative.aspect_count",
    "localization.locale_count",
    "content.recent_30d",
    "content.active_months_180d",
    "content.visual_items",
    "event.upcoming_count",
    "event.recent_count",
    "event.active_months",
    "media.recent_30d",
    "search.organic_ranked_results",
}
COMMERCE_PLATFORM = frozenset({"Shopify", "WooCommerce", "Magento", "BigCommerce"})
SHOP_ACTION = re.compile(r"\b(?:shop (?:all|online|now)|add to (?:cart|bag)|close cart)\b", re.I)
CHECKOUT = re.compile(r"\b(?:subtotal|shipping calculated at checkout)\b", re.I)
FULFILLMENT = re.compile(r"\bshipping (?:&|and) (?:delivery|returns)\b", re.I)
NEWS_SURFACE = re.compile(r"\b(?:top stories|latest news|today['’]s paper|newsletters)\b", re.I)
# Stable identity families only. This does NOT claim current revenue, budget, scale or a contract.
GLOBAL_BRANDS = {
    "apple.com",
    "nike.com",
    "adidas.com",
    "coca-cola.com",
    "pepsi.com",
    "microsoft.com",
    "google.com",
    "amazon.com",
    "samsung.com",
    "toyota.com",
    "bmw.com",
    "mercedes-benz.com",
    "disney.com",
    "netflix.com",
    "mcdonalds.com",
    "ford.com",
    "visa.com",
    "mastercard.com",
}
BRAND_NAMES = {
    "apple",
    "nike",
    "adidas",
    "coca-cola",
    "pepsi",
    "microsoft",
    "google",
    "amazon",
    "samsung",
    "toyota",
    "bmw",
    "mercedes-benz",
    "disney",
    "netflix",
    "mcdonald's",
    "ford",
    "visa",
    "mastercard",
}
SCOPE = re.compile(
    r"\b(?:global|international|national|multi[- ]market|multi[- ]country|broadcast)\s+"
    r"(?:(?:television|tv|video|advertising|commercial|launch|broadcast)\s+){0,3}"
    r"(?:campaigns?|commercials?|productions?|films?|shoots?|advertising|distribution|rollouts?)\b"
    r"|\b(?:campaigns?|commercials?|films?)\s+(?:for|with|in)\s+"
    r"(?:global|national|international|multi[- ]market)\s+(?:distribution|broadcast|release)\b",
    re.I,
)
PRODUCTION = re.compile(
    r"\b(?:commercials?|campaigns?|production|films?|shoots?|advertising)\b", re.I
)
OWN_PRODUCTION = re.compile(
    r"\b(?:we (?:produced|produce|directed|direct|created|create|shot|shoot|delivered)|"
    r"our (?:production|campaign|commercial|film|shoot|work))\b",
    re.I,
)


def own(url, company):
    try:
        host = (urlsplit(url or "").hostname or "").lower().removeprefix("www.")
    except (ValueError, TypeError):
        return False  # Redacted/malformed history cannot establish URL ownership.
    return host == company or host.endswith("." + company)


def initialize_indexes(connection):
    """Add exact lookup indexes only; never rewrite a source or its eligibility policy."""
    from ..sales_engine.evidence_accounting import LEDGERS

    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    identity = {
        "company_key",
        "subject_company_key",
        "object_company_key",
        "website_host",
        "landing_domain",
        "direct_company_key",
        "domain",
        "subject_key",
        "object_key",
        "candidate_id",
        "direct_candidate_id",
        "account_id",
    }
    joins = {
        "market_events": {"mention_id", "document_id", "supersedes_event_id"},
        "market_documents": {"supersedes_document_id"},
        "market_entity_mentions": {"document_id", "supersedes_mention_id"},
        "candidate_ad_observations": {"run_id"},
        "advertiser_domain_links": {"advertiser_id"},
        "ad_creatives": {"advertiser_id"},
    }
    for table in LEDGERS.keys() & tables:
        columns = {r[1] for r in connection.execute(f"PRAGMA table_info({table})")}
        indexed = set()
        for index in connection.execute(f"PRAGMA index_list({table})"):
            # A partial index cannot satisfy the exhaustive accounting lookup.
            if not index[4]:
                parts = list(connection.execute(f"PRAGMA index_info('{index[1]}')"))
                if parts:
                    indexed.add(parts[0][2])
        for column in (identity | joins.get(table, set())) & columns - indexed:
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_mission_bridge_{table}_{column} "
                f"ON {table}({column})"
            )


def bindings(groups):
    """Conservative free interpretation of saved phrase groups; raw AI output is retained."""
    result = []
    for group in groups:
        terms = " ".join(group.alternatives).casefold()
        keys = []
        if re.search(
            r"\b(?:big|major|leading|global|household[- ]name|blue[- ]chip|"
            r"world[- ]renowned) brands",
            terms,
        ):
            keys.append("portfolio.major_brand")
        if re.search(r"\b(?:massive|large[- ]scale|major) (?:commercial|advertising)", terms):
            keys.append("production.large_scope")
        if not keys and re.search(
            r"\b(?:commercials|advertising films|commercial films|advertising video production)\b",
            terms,
        ):
            keys.append("production.commercial_work")
        result.append(keys)
    return result


def accepts(fact, keys, terms, comparison="present", threshold=None):
    if fact.get("fact_key") not in keys:
        return False
    value = fact.get("value")
    if comparison != "present":
        if comparison in {"eq", "lte"} and fact.get("measurement_basis") == "lower_bound":
            return False
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            return False
        return {"gte": value >= threshold, "lte": value <= threshold, "eq": value == threshold}[
            comparison
        ]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    # String-valued fields must match the requested value, not merely exist.
    return isinstance(value, str) and any(
        matches(value.replace("_", " "), t.replace("_", " ")) for t in terms
    )


class Bridge:
    def __init__(self, company, now, emit):
        self.company, self.now, self.emit = company, now, emit
        self.used = set()

    def fact(
        self,
        key,
        value,
        origin,
        url,
        observed,
        *,
        root=None,
        expires=None,
        confidence="high",
        basis="observed",
        eligible=True,
        measurement_basis="exact",
        effective_at=None,
        logical_key=None,
    ):
        if key not in CATALOG:
            return
        try:
            source = urlsplit(url or "")
            cited = source.scheme in {"https", "http"} and bool(source.hostname)
        except (ValueError, TypeError):
            cited = False
        self.used.add(origin.split(":", 1)[0])
        self.emit(
            origin + ":" + key,
            url,
            "",
            observed,
            root=root or url or origin,
            expires=expires,
            logical_key=logical_key,
            eligible=eligible
            and confidence in {"high", "medium"}
            and cited,
            fact={
                "fact_key": key,
                "value": value,
                "basis": basis,
                "confidence_weight": 1 if confidence == "high" else 0.75,
                "measurement_basis": measurement_basis,
                **({"effective_at": effective_at.isoformat()} if effective_at else {}),
            },
        )

    def scope(self, text, origin, url, observed, **kwargs):
        # Read every sentence; unrelated international addresses/footer text is not scope.
        sentences = re.split(r"[.!?;\n]+", text)
        owned = [
            s
            for s in sentences
            if OWN_PRODUCTION.search(s)
            and PRODUCTION.search(s)
            and not re.search(r"\b(?:no|never|not|neither)\b", s, re.I)
        ]
        if owned:
            self.fact("production.commercial_work", True, origin, url, observed, **kwargs)
            if any(SCOPE.search(s) for s in owned):
                self.fact("production.large_scope", True, origin, url, observed, **kwargs)

    def client(self, target, name, origin, url, observed, *, confidence, **kwargs):
        host = str(target).removeprefix("domain:").removeprefix("www.").rstrip(".")
        self.fact(
            "portfolio.client",
            host or name,
            origin,
            url,
            observed,
            confidence=confidence,
            basis="published_client_proof",
            **kwargs,
        )
        if host in GLOBAL_BRANDS or (
            not host and name.casefold().strip() in BRAND_NAMES and confidence == "high"
        ):
            self.fact(
                "portfolio.major_brand",
                True,
                origin,
                url,
                observed,
                confidence=confidence,
                basis="published_client_proof_known_brand_identity",
                **kwargs,
            )

    def snapshot(self, observation, origin):
        stamp = observation.observed_at
        if not observation.live:
            return
        for index, appearance in enumerate(observation.web.company_appearances):
            claim = appearance.assertion
            if (
                claim.relation_type in {"customer", "case_study", "portfolio"}
                and claim.publisher_domain == self.company
                and own(claim.source_url, self.company)
                and appearance.identity_confidence in {"medium", "high"}
                and appearance.extraction_method != "logo_filename"
            ):
                key = f"{origin}:appearance:{index}"
                self.client(
                    appearance.subject_domain,
                    appearance.subject_name,
                    key,
                    claim.source_url,
                    stamp,
                    confidence=claim.confidence,
                )
                self.scope(
                    claim.context_excerpt, key, claim.source_url, stamp, confidence=claim.confidence
                )
        for index, vendor in enumerate(observation.web.vendors.evidence):
            if vendor.source_type in {"subprocessor", "privacy_disclosure"} and own(
                vendor.evidence_url, self.company
            ):
                self.fact(
                    "technology.vendor",
                    vendor.vendor,
                    f"{origin}:vendor:{index}",
                    vendor.evidence_url,
                    stamp,
                    confidence=vendor.confidence,
                )
        for index, page in enumerate(observation.pages):
            if own(page.url, self.company):
                self.scope(page.text_excerpt, f"{origin}:page:{index}", page.url, stamp)
        self.scope(observation.text_excerpt, origin, observation.final_url, stamp)

    def owned_site_activity(self, observation, origin):
        source = observation.final_url
        if not own(source, self.company) or INCIDENTAL_PATH.search(urlsplit(source).path):
            return
        text = " ".join((observation.description, observation.text_excerpt))
        shop = SHOP_ACTION.search(text)
        checkout = shop and CHECKOUT.search(text) and matches(text, "checkout")
        platform = COMMERCE_PLATFORM.intersection(observation.technologies)
        fulfillment = shop and FULFILLMENT.search(text) and matches(text, "order status")
        publisher = "NewsMediaOrganization" in observation.web.jsonld_types
        for key, supported, confidence in (
            (
                "company.owned_storefront",
                (platform and checkout) or fulfillment,
                "high" if platform and checkout else "medium",
            ),
            (
                "company.news_publisher",
                publisher and NEWS_SURFACE.search(text),
                "high",
            ),
        ):
            if supported:
                self.fact(
                    key, True, origin, source, observation.observed_at,
                    confidence=confidence, basis="owned_website_surface",
                )

    def _observed_for_company(self, atomic):
        return (
            atomic.epistemic_status == "observed"
            and atomic.validity_status == "current"
            and atomic.company_key == self.company
            and atomic.qualifiers.get("subject_role")
            not in {"client", "customer", "supplier", "portfolio_client"}
        )

    def _atomic_eligible(self, atomic):
        return (
            atomic.score_eligible and not atomic.discovery_only
            and self._observed_for_company(atomic)
        )

    def atomic(self, atomic):
        base_eligible = self._atomic_eligible(atomic)
        if atomic.predicate in {"seo.search_visibility_estimate", "seo.domain_rank_overview"}:
            self.organic_visibility(atomic, base_eligible)
        if atomic.predicate == "events.program_engine":
            self.event_program(atomic, base_eligible)
        for field, key in ATOMIC_FIELDS.get(atomic.predicate, {}).items():
            eligible = base_eligible
            value = (
                atomic.value.get(field)
                if field and isinstance(atomic.value, dict)
                else atomic.value
            )
            expiry = atomic.expires_at
            if key == "event.upcoming":
                start = date(value.get("start_at")) if isinstance(value, dict) else None
                eligible = bool(
                    eligible
                    and start
                    and start > self.now
                    and atomic.effective_time_basis == "source_explicit"
                )
                expiry = min(expiry, start) if expiry and start else start
                value = True
            if key in NUMERIC and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                eligible = False
            if key == "company.annual_revenue_usd" and atomic.unit not in {
                "USD/year",
                "USD_annual",
            }:
                eligible = False
            values = value if isinstance(value, list) else [value]
            for index, item in enumerate(values):
                self.fact(
                    key,
                    item,
                    "atomic:" + atomic.observation_key + f":{index}",
                    atomic.source_url,
                    atomic.last_observed_at or atomic.first_observed_at,
                    root=atomic.causal_root_key or atomic.source_url,
                    expires=expiry,
                    confidence=atomic.confidence,
                    eligible=eligible,
                    measurement_basis="lower_bound"
                    if key in NUMERIC
                    and (
                        atomic.qualifiers.get("counts_are_lower_bounds") is True
                        or atomic.qualifiers.get("coverage_complete") is False
                        or key.startswith(("creative.", "content.", "media.", "event."))
                    )
                    else "exact",
                )

    def event_program(self, atomic, eligible):
        if (
            atomic.scanner_id != "event_webinar_engine_v1"
            or atomic.company_key != self.company
            or atomic.entity_key != f"event-program:{self.company}"
            or atomic.source_actor_key != f"domain:{self.company}"
            or atomic.qualifiers.get("score_policy") != "recurring_event_program_v1"
        ):
            return
        source = atomic.source_url
        observed = date(atomic.last_observed_at or atomic.first_observed_at)
        effective = date(atomic.effective_at)
        expiry = date(atomic.expires_at)
        value = atomic.value if isinstance(atomic.value, dict) else {}
        # The cited surface contributed the latest dated item. Anchor eligibility to
        # that item so old archives or counts from other contributing surfaces cannot
        # establish current owned activity by themselves.
        supported = False
        if (
            eligible and observed and effective
            and atomic.qualifiers.get("projection_score_gate") is True
            and atomic.effective_time_basis in {"source_explicit", "feed_date"}
            and own(source, self.company) and not INCIDENTAL_PATH.search(urlsplit(source).path)
        ):
            offset = effective - observed
            recent = -timedelta(days=180) <= offset <= timedelta(0)
            upcoming = timedelta(days=3) <= offset <= timedelta(days=45)
            count_key = "recent_events" if recent else "upcoming_events"
            supported = (recent or upcoming) and all(
                type(value.get(field)) is int and value[field] >= minimum
                for field, minimum in (("dated_events", 3), ("distinct_months", 2), (count_key, 1))
            )
            if supported:
                boundary = effective + timedelta(days=180) if recent else effective
                expiry = min(expiry, boundary) if expiry else boundary
        self.fact(
            "company.event_program", supported, "atomic:" + atomic.observation_key,
            source, observed, root=atomic.causal_root_key, effective_at=effective,
            expires=expiry, confidence=atomic.confidence, basis="owned_dated_public_program",
            logical_key="mission-event-program:" + self.company, eligible=supported,
        )

    def editorial_blog(self, observations):
        engines, recent = [], {}
        subject = f"domain:{self.company}"
        for atomic in observations:
            if (
                atomic.scanner_id != "corporate_blog_engine_v1"
                or atomic.company_key != self.company or atomic.source_actor_key != subject
            ):
                continue
            observed = date(atomic.last_observed_at or atomic.first_observed_at)
            if not observed or observed > self.now:
                continue
            execution = atomic.execution_id, observed
            if (
                atomic.predicate == "content.blog_engine"
                and atomic.entity_key == f"owned-publishing:{self.company}"
                and atomic.qualifiers.get("score_policy") == "established_editorial_engine_v1"
            ):
                engines.append((atomic, execution))
            elif atomic.predicate == "content.blog_publication":
                item = atomic.value if isinstance(atomic.value, dict) else {}
                url = item.get("url")
                published = date(atomic.effective_at)
                expiry = date(atomic.expires_at)
                if (
                    published and self.now - timedelta(days=90) < published <= observed
                    and (not expiry or expiry > self.now)
                    and self._observed_for_company(atomic)
                    and atomic.confidence in {"high", "medium"}
                    and atomic.effective_time_basis in {"source_explicit", "feed_date"}
                    and atomic.qualifiers.get("content_class") == item.get("content_class")
                    == "editorial"
                    and isinstance(url, str) and own(url, self.company)
                    and own(atomic.source_url, self.company)
                ):
                    current = recent.get(execution)
                    if not current or published > current[0]:
                        recent[execution] = published, url, expiry, atomic.observation_key

        for engine, execution in engines:
            # Publication rows are discovery-only. An actual dated owned item can
            # corroborate only a qualifying aggregate from its own scan.
            observed = execution[1]
            proof = recent.get(execution)
            supported = bool(
                proof and self._atomic_eligible(engine) and own(engine.source_url, self.company)
            )
            origin = "atomic:" + engine.observation_key
            source = engine.source_url
            expiry = date(engine.expires_at)
            published = None
            if supported:
                published, source, item_expiry, item_key = proof
                origin += ":item:" + item_key
                expiry = min(d for d in (published + timedelta(days=90), expiry, item_expiry) if d)
            self.fact(
                "company.editorial_blog", supported, origin, source, observed,
                root=engine.causal_root_key, expires=expiry, effective_at=published,
                confidence=engine.confidence, basis="owned_dated_editorial_item",
                logical_key="mission-editorial-blog:" + self.company, eligible=supported,
            )

    def organic_visibility(self, atomic, eligible):
        from ..dataforseo_intelligence import LANGUAGE_CODE, LOCATION_CODE, SCANNER_ID

        if atomic.scanner_id != SCANNER_ID or not isinstance(atomic.value, dict):
            return
        value = atomic.value
        if (
            value.get("target") != self.company
            or value.get("location_code") != LOCATION_CODE
            or value.get("language_code") != LANGUAGE_CODE
            or value.get("provider_estimate") is not True
        ):
            return
        organic = value.get("organic")
        count = organic.get("count") if isinstance(organic, dict) else None
        self.fact(
            "search.organic_ranked_results", count, "atomic:" + atomic.observation_key,
            atomic.source_url, atomic.last_observed_at or atomic.first_observed_at,
            root=atomic.causal_root_key, expires=atomic.expires_at, confidence=atomic.confidence,
            logical_key=f"mission-organic:{atomic.predicate}", basis="provider_estimate",
            eligible=(
                eligible and value.get("data_available") is True
                and type(count) is int and count >= 25
            ),
        )

    def saved(self, store, candidate_id, available):
        c = store.connection

        queries = {
            "company_relationships": (
                "SELECT * FROM company_relationships WHERE subject_key=?",
                (self.company,),
            ),
            "engine_relationships": (
                "SELECT * FROM engine_relationships WHERE subject_company_key=?",
                (self.company,),
            ),
            "engine_workload_inputs": (
                "SELECT * FROM engine_workload_inputs WHERE company_key=? AND kind='event'",
                (self.company,),
            ),
            "engine_creative_annotations": (
                """SELECT * FROM engine_creative_annotations a
                WHERE company_key=? AND recorded_at<=?
                AND id=(SELECT max(b.id) FROM engine_creative_annotations b
                    WHERE b.company_key=a.company_key AND b.asset_key=a.asset_key
                    AND b.recorded_at<=?)
                ORDER BY id""",
                (self.company, self.now.isoformat(), self.now.isoformat()),
            ),
        }
        market = self._market_query() if {
            "market_events", "market_documents", "market_entity_mentions",
        } <= available else None
        ads = self._ads_query() if candidate_id and {
            "candidate_ad_observations", "ad_discovery_runs", "advertiser_domain_links",
            "advertiser_entities",
        } <= available else None
        prefetch = getattr(c, "prefetch", None)
        if prefetch is not None:
            prefetch((
                *(query for table, query in queries.items() if table in available),
                *(query for query in (market, ads) if query is not None),
            ))

        def rows(table):
            return c.execute(*queries[table]) if table in available else []

        # Relations are directional: the subject publishes/records work for the object.
        for row in rows("company_relationships"):
            payload = json.loads(row["payload_json"])
            kind = row["relationship_type"]
            if kind in {
                "public_customer_appearance",
                "public_case_study_appearance",
                "public_portfolio_appearance",
                "portfolio_reference",
            }:
                good = (
                    row["status"] == "active"
                    and own(row["source_url"], self.company)
                    and payload.get("publisher_domain", self.company) == self.company
                    and payload.get("identity_confidence", "high") in {"medium", "high"}
                    and payload.get("extraction_method") != "logo_filename"
                )
                target = row["object_key"]
                if target.startswith("unresolved:"):
                    target = payload.get("subject_domain", "")
                self.client(
                    target,
                    row["object_name"],
                    "company_relationships:" + row["object_key"] + ":" + kind,
                    row["source_url"],
                    row["last_observed_at"],
                    confidence=row["confidence"],
                    eligible=good,
                )
                self.scope(
                    payload.get("context_excerpt", ""),
                    "company_relationships:" + row["object_key"] + ":" + kind,
                    row["source_url"],
                    row["last_observed_at"],
                    confidence=row["confidence"],
                    eligible=good,
                )
        for row in rows("engine_relationships"):
            payload = json.loads(row["payload_json"])
            if row["relationship_type"] in {"agency_client", "portfolio_client"}:
                self.client(
                    row["object_company_key"],
                    "",
                    "engine_relationships:" + row["id"],
                    payload.get("source_url", ""),
                    payload.get("observed_at"),
                    confidence="high",
                    eligible=bool(
                        date(row["created_at"])
                        and date(row["updated_at"])
                        and date(row["created_at"]) <= self.now
                        and date(row["updated_at"]) <= self.now
                        and payload.get("subject_company_key") == self.company
                        and own(payload.get("source_url"), self.company)
                    ),
                )
        workload = []
        dated_versions = {}
        for row in rows("engine_workload_inputs"):
            payload = json.loads(row["payload_json"])
            observed = date(payload.get("observed_at"))
            recorded = date(row["recorded_at"])
            if observed is None or recorded is None or recorded > self.now:
                continue
            eligible = (
                payload.get("subject_verified") is True
                and payload.get("current") is True
                and payload.get("company_key") == self.company
            )
            effective = date(payload.get("effective_at"))
            dated = bool(
                eligible and effective and effective <= observed
                and payload.get("time_basis") in {"source_explicit", "feed_date"}
            )
            event_key = payload.get("event_key")
            workload.append((row["id"], payload, observed, effective, eligible, dated))
            if event_key:
                dated_versions.setdefault((event_key, observed), set()).add(
                    (effective, payload.get("kind"), dated)
                )
        for identifier, payload, observed, effective, eligible, dated in workload:
            origin, url = f"engine_workload_inputs:{identifier}", payload.get("source_url", "")
            for key, field in [("work.kind", "kind"), ("work.scope", "work_scope")]:
                self.fact(
                    key, payload.get(field, ""), origin, url, observed,
                    expires=payload.get("expires_at"), eligible=eligible,
                )
            if event_key := payload.get("event_key"):
                expiry = effective + timedelta(days=90) if effective else None
                saved_expiry = date(payload.get("expires_at"))
                if saved_expiry and expiry:
                    expiry = min(expiry, saved_expiry)
                self.fact(
                    "work.recent_kind", payload.get("kind", ""), origin, url, observed,
                    root=event_key, expires=expiry, effective_at=effective,
                    logical_key="work-dated:" + event_key,
                    eligible=dated and len(dated_versions[(event_key, observed)]) == 1,
                )
        for row in rows("engine_creative_annotations"):
            payload = json.loads(row["annotation_json"])
            observed = date(payload.get("observed_at"))
            valid = (
                payload.get("validation_status") == "confirmed"
                and payload.get("asset_key") == row["asset_key"]
                and payload.get("validated_by")
                and date(payload.get("validated_at"))
                and date(payload["validated_at"]) <= self.now
                and date(row["recorded_at"]) <= self.now
                and (
                    own(payload.get("source_url"), self.company)
                    or c.execute(
                        """SELECT 1 FROM ad_creatives a JOIN advertiser_domain_links l
                    ON l.advertiser_id=a.advertiser_id WHERE l.domain=? AND l.state='verified'
                    AND l.confidence>=80 AND COALESCE(l.verified_at,l.first_seen_at)<=?
                    AND a.source_url=? AND a.first_seen_at<=? LIMIT 1""",
                        (
                            self.company,
                            self.now.isoformat(),
                            payload.get("source_url"),
                            self.now.isoformat(),
                        ),
                    ).fetchone()
                )
            )
            if observed:
                for field in ["format", "product"]:
                    self.fact(
                        "creative." + field,
                        payload.get(field, ""),
                        f"engine_creative_annotations:{row['id']}",
                        payload.get("source_url"),
                        observed,
                        eligible=bool(valid),
                        root="annotation:" + payload.get("asset_key", str(row["id"])),
                    )
        if market is not None:
            self.market(c.execute(*market))
        if ads is not None:
            self.ads(c.execute(*ads))

    def _ads_query(self):
        return (
            """WITH ranked AS (
                SELECT o.*,ROW_NUMBER() OVER (PARTITION BY o.platform,o.surface
                    ORDER BY o.observed_at DESC,o.id DESC) AS recency_rank
                FROM candidate_ad_observations o JOIN ad_discovery_runs r ON r.id=o.run_id
                WHERE o.candidate_id IN (
                    SELECT candidate_id FROM engine_mission_members WHERE company_key=?)
                AND o.observed_at<=? AND r.status IN ('complete','partial'))
            SELECT o.* FROM ranked o WHERE recency_rank=1
            AND o.identity_state='verified' AND o.identity_confidence>=80
            AND o.surface<>'tiktok_top' AND o.coverage_type<>'curated_top_ads_positive_only'
            AND EXISTS (SELECT 1 FROM advertiser_domain_links l
                JOIN advertiser_entities a ON a.id=l.advertiser_id
                WHERE l.candidate_id=o.candidate_id AND l.domain=? AND a.platform=o.platform
                AND l.state='verified' AND l.confidence>=80
                AND COALESCE(l.verified_at,l.first_seen_at)<=?) ORDER BY o.id""",
            (self.company, self.now.isoformat(), self.company, self.now.isoformat()),
        )

    def ads(self, rows):
        """Exact latest-surface aggregation, using only fresh, verified scoreable receipts."""
        # Source dates govern these metrics, never a newer curated/ambiguous observation.
        rows = [
            r
            for r in rows
            if date(r["observed_at"]) and date(r["observed_at"]) + timedelta(days=90) > self.now
        ]
        if not rows:
            return
        stamp = min(date(r["observed_at"]) for r in rows)
        groups, unkeyed, active = set(), 0, {}
        for row in rows:
            keys = json.loads(row["creative_group_ids_json"] or "[]")
            groups.update(str(k) for k in keys)
            if not keys:
                unkeyed = max(unkeyed, int(row["total_creative_groups"] or 0))
            active[row["platform"]] = max(
                active.get(row["platform"], 0), int(row["active_ad_count"] or 0)
            )
        source = next(
            (
                u
                for r in rows
                for u in json.loads(r["evidence_urls_json"] or "[]")
                if urlsplit(u).scheme in {"https", "http"}
            ),
            "",
        )
        for key, value in [
            ("advertising.active_ads", sum(active.values())),
            ("advertising.unique_creatives", max(len(groups), unkeyed)),
            ("advertising.new_creatives_30d", max(int(r["new_creatives_30d"] or 0) for r in rows)),
        ]:
            self.fact(
                key,
                value,
                "candidate_ad_observations:" + self.company,
                source,
                stamp,
                root="verified-ad-ledger:" + self.company,
                expires=stamp + timedelta(days=90),
                measurement_basis="lower_bound",
            )

    def _market_query(self):
        return (
            """SELECT e.*,d.canonical_url,d.observed_at AS document_observed,
            d.recorded_at AS document_recorded,m.recorded_at AS mention_recorded,
            m.resolved_at AS mention_resolved,
            m.resolution_status,m.resolution_confidence,m.company_key AS mention_company
            FROM market_events e JOIN market_documents d ON d.id=e.document_id
            LEFT JOIN market_entity_mentions m ON m.id=e.mention_id
            WHERE e.id IN (
                SELECT id FROM market_events WHERE direct_company_key=?
                UNION SELECT id FROM market_events WHERE mention_id IN (
                    SELECT id FROM market_entity_mentions WHERE company_key=?))
            AND NOT EXISTS (SELECT 1 FROM market_events replacement
                WHERE replacement.supersedes_event_id=e.id AND replacement.recorded_at<=?)
            AND NOT EXISTS (SELECT 1 FROM market_documents replacement
                WHERE replacement.supersedes_document_id=d.id AND replacement.recorded_at<=?)
            AND NOT EXISTS (SELECT 1 FROM market_entity_mentions replacement
                WHERE replacement.supersedes_mention_id=m.id AND replacement.recorded_at<=?)
            ORDER BY e.id""",
            (
                self.company,
                self.company,
                self.now.isoformat(),
                self.now.isoformat(),
                self.now.isoformat(),
            ),
        )

    def market(self, rows):
        for row in rows:
            resolved = row["direct_company_key"] == self.company or (
                row["resolution_status"] == "resolved"
                and row["resolution_confidence"] == "high"
                and row["mention_company"] == self.company
                and date(row["mention_recorded"])
                and date(row["mention_recorded"]) <= self.now
                and date(row["mention_resolved"])
                and date(row["mention_resolved"]) <= self.now
            )
            good = (
                resolved
                and row["semantic_eligible"]
                and row["subject_role"] in {"subject", "issuer", "filer", "registrant"}
                and date(row["document_recorded"])
                and date(row["document_recorded"]) <= self.now
                and date(row["recorded_at"])
                and date(row["recorded_at"]) <= self.now
            )
            self.fact(
                "market.event_type",
                row["event_type"],
                f"market_events:{row['id']}",
                row["canonical_url"],
                row["document_observed"],
                confidence=row["confidence"],
                eligible=bool(good),
                root=row["causal_root_key"] or row["canonical_url"],
            )
