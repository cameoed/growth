"""Full local source removal: rebuild aggregates, gates and dependent events together."""

from ..models import SiteObservation, WebIntelligence

SOURCES = (
    "website",
    "frontier",
    "advertising",
    "buyer_evidence",
    "contacts",
    "saved_work",
    "creative_annotations",
    "relationships",
)
DIMENSIONS = (
    "fit",
    "need",
    "timing",
    "ability",
    "access",
    "proof",
    "evidence_quality",
    "risk",
    "uncertainty",
    "durable_fit",
    "contact_now",
    "retained_value_proxy",
    "founder_priority",
)


def identical_score_inputs(replay_inputs, baseline_inputs):
    """Exact full-input equality; trace only controls diagnostic replays, not dimensions."""
    return {key: value for key, value in replay_inputs.items() if key != "trace"} == {
        key: value for key, value in baseline_inputs.items() if key != "trace"
    }


def without_source(observation, source, *, for_scoring=False):
    """Remove one source; scoring may share unchanged branches until its private copy.

    The default remains a fully detached observation for ordinary mutable callers.
    Internal scoring preparation reads the result and score_candidate owns its copy.
    """
    obs = observation.model_copy(deep=not for_scoring)
    if source == "website":
        # Retain inspected identity/availability, contacts and independently hydrated ledgers.
        obs = SiteObservation(
            hostname=obs.hostname,
            canonical_host=obs.canonical_host,
            observed_at=obs.observed_at,
            live=obs.live,
            status_code=obs.status_code,
            final_url=obs.final_url,
            language=obs.language,
            contacts=obs.contacts,
            emails=obs.emails,
            contact_urls=obs.contact_urls,
            buyer_evidence=[r for r in obs.buyer_evidence if r.get("provider") != "public_web"],
            paid_creative_asset_keys=obs.paid_creative_asset_keys,
            text_excerpt="Inspected company identity retained for source-removal replay. " * 2,
            web=WebIntelligence(frontier=obs.web.frontier),
        )
    elif source == "frontier":
        if for_scoring:
            obs.web = obs.web.model_copy()
        obs.web.frontier = type(obs.web.frontier)()
        obs.buyer_evidence = [
            r for r in obs.buyer_evidence if r.get("provider") != "cached_first_party_publication"
        ]
    elif source == "buyer_evidence":
        obs.buyer_evidence = []
        obs.pages = [page.model_copy(update={"buyer_evidence": []}) for page in obs.pages]
    elif source == "advertising":
        obs.buyer_evidence = [r for r in obs.buyer_evidence if r.get("kind") != "advertising"]
        obs.pages = [page.model_copy(update={"buyer_evidence": [
            r for r in page.buyer_evidence if r.get("kind") != "advertising"
        ]}) for page in obs.pages]
        obs.paid_creative_asset_keys = []
    elif source == "contacts":
        obs.contacts = type(obs.contacts)()
        obs.emails, obs.contact_urls = [], []
    return obs


def measure_sources(baseline, replay):
    return {
        source: {field: getattr(baseline, field) - getattr(removed, field) for field in DIMENSIONS}
        for source in SOURCES
        for removed in [replay(source).creative_capacity.opportunity]
    }
