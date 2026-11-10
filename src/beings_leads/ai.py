from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from openai import OpenAI

from .models import AIAnalysis, DiscoveredHost, ScoreCard, SiteObservation
from .provider_rate_limit import ProviderHTTPClient, provider_thread
from .spending import record_openai_response

AI_INSTRUCTIONS = """You produce conservative public research for Beings.org's Founding 10
Production Lane: one dedicated AI creative specialist, one prioritized production queue, and a
$5,000 month-to-month price ($60,000 annually). The direct-buyer beachhead is paid-growth consumer
brands, consumer apps, digital products, gaming companies, visually ambitious subscription
businesses, and evidence-strong production or performance agencies. An agency can be a direct
buyer when first-party evidence shows recurring retained-client photo/video delivery, performance
creative or paid-social testing, ecommerce content, UGC, motion, post-production, overflow, or
white-label production that plausibly fits one dedicated specialist lane. Keep agencies with useful
brand relationships but insufficient direct production evidence in a separate referral-only lane.
Warm SaaS/AI companies are an adjacency only when a recurring launch, event, webinar, podcast, or
founder-content engine is visible.

Assess three separate principal signals: the company visibly invests in distributing visual
content; it has recurring creative demand; and its production capacity may be failing to keep pace.
Do not confuse production-capacity gap with company scale or ability to pay. Identify a likely
economic-buyer role, up to three specific first-month asks tied to actual cited workload
(return fewer or none if unsupported), and rights, authenticity,
likeness, product-fidelity, or AI-policy risks. This is a research hypothesis, not manual
qualification: never mark a claim human-verified, never assert that a hard gate has passed, and
never decide that an account should be pursued.

Use only supplied page evidence and, when enabled, public web-search evidence. Require multiple
independent evidence families and be skeptical of technology or source membership alone. Never
infer exact revenue, ad spend, headcount, or purchase intent; active-ad count is not ad spend, a job
opening is pressure rather than willingness to outsource, and funding is not marketing budget. An
agency's own active ads can support distribution evidence for that agency, but they do not prove
retained client workload, client ad activity, overflow demand, or willingness to buy production.
Missing evidence is unknown, not negative. Never invent people, email addresses, clients, pricing,
or URLs. Put directly supported statements in observations, model conclusions in inferences, and
attach a public source URL and observation date to every observation. When public web search is
enabled, find the current official website if the discovered
hostname is stale, and identify decision makers only when a public source explicitly connects their
name and role to the company. A thin, stale, gated, or ambiguous site should score low unless a
cited current official site resolves the identity.
"""
AI_PROMPT_VERSION = "prospect-v6-evidence-bound-conversion-intelligence-5k"
AI_REQUEST_TIMEOUT_SECONDS = 120
AI_OUTPUT_TOKENS = 4_000
AI_RETRY_OUTPUT_TOKENS = 7_000


class AIOutputIncompleteError(RuntimeError):
    pass


def _encode_evidence(payload: dict[str, Any]) -> str:
    """Lossless schema/string sharing for large hydrated evidence, never sampling."""
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(plain) < 50_000:
        return plain
    strings: list[str] = []
    string_ids: dict[str, int] = {}
    schemas: list[list[str]] = []
    schema_ids: dict[tuple[str, ...], int] = {}

    def encode(value: Any) -> Any:
        if isinstance(value, str):
            if value not in string_ids:
                string_ids[value] = len(strings)
                strings.append(value)
            return {"s": string_ids[value]}
        if isinstance(value, dict):
            keys = tuple(value)
            if keys not in schema_ids:
                schema_ids[keys] = len(schemas)
                schemas.append(list(keys))
            return {"d": schema_ids[keys], "v": [encode(v) for v in value.values()]}
        if isinstance(value, list):
            return [encode(v) for v in value]
        return value

    root = encode(payload)
    packed = json.dumps(
        {"strings": strings, "schemas": schemas, "root": root},
        ensure_ascii=False, separators=(",", ":"),
    )
    if len(packed) >= len(plain):
        return plain
    return (
        "Lossless evidence encoding: {\"s\":N} means strings[N]; "
        "{\"d\":N,\"v\":[...]} means an object pairing schemas[N] keys with "
        "the corresponding v values. Recursively resolve these references; ordinary arrays, "
        "numbers, booleans and null retain their meaning. All evidence is included.\n" + packed
    )


def _analysis_cache_payload(
    candidate: DiscoveredHost,
    observation: SiteObservation,
    score: ScoreCard,
    *,
    model: str,
) -> dict[str, Any]:
    score_payload = score.model_dump(mode="json")
    # Evaluation time is not a new source observation. Keep source-reported dates intact.
    score_payload.get("creative_capacity", {}).get("opportunity", {}).get(
        "freshness_audit", {}
    ).pop("as_of", None)

    def stable_score_value(value: Any) -> Any:
        # A rescan can move evidence timestamps and their derived decay strength even when the
        # public content is byte-for-byte unchanged. Those transport-time fields must not turn
        # the same evidence into a new AI request; substantive counts and conclusions remain.
        volatile = {
            "current_strength",
            "effective_at",
            "evidence_dates",
            "expires_at",
            "observed_at",
            "observed_dates",
        }
        if isinstance(value, dict):
            return {
                key: stable_score_value(child)
                for key, child in value.items()
                if key not in volatile
            }
        if isinstance(value, list):
            return [stable_score_value(child) for child in value]
        return value

    score_payload = stable_score_value(score_payload)
    return {
        "version": AI_PROMPT_VERSION,
        "model": model,
        "source": candidate.source_apex,
        "hostname": candidate.hostname,
        "fetch_outcome": observation.fetch_outcome,
        "canonical_recovery_required": observation.canonical_recovery_required,
        "tenant_label": observation.tenant_label,
        "content_hash": observation.content_hash,
        "signals": observation.signals,
        "public_ids": observation.public_ids,
        "public_contacts": observation.contacts.model_dump(mode="json"),
        "web_intelligence": observation.web.model_dump(mode="json"),
        "score": score_payload,
    }


def analysis_cache_key(
    candidate: DiscoveredHost, observation: SiteObservation, score: ScoreCard, *,
    model: str, web_search: bool,
) -> str:
    payload = _analysis_cache_payload(candidate, observation, score, model=model)
    payload["web_search"] = web_search
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def analysis_cache_keys(
    candidate: DiscoveredHost, observation: SiteObservation, score: ScoreCard, *, model: str,
) -> tuple[str, str]:
    """Return the existing web and standard keys from one evidence serialization."""
    payload = _analysis_cache_payload(candidate, observation, score, model=model)
    payload["web_search"] = False
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    if not encoded.endswith(b', "web_search": false}'):
        # Preserve canonical sorted bytes if a future field sorts after web_search.
        payload["web_search"] = True
        return (hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest(),
                hashlib.sha256(encoded).hexdigest())
    suffix = b"false}"
    shared = hashlib.sha256(memoryview(encoded)[:-len(suffix)])
    standard = shared.copy()
    standard.update(suffix)
    shared.update(b"true}")
    return shared.hexdigest(), standard.hexdigest()


def _collect_urls(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"url", "link"} and isinstance(child, str) and child.startswith("http"):
                found.add(child)
            found.update(_collect_urls(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_urls(child))
    return found


def _parse_analysis(response: Any) -> AIAnalysis:
    parsed = getattr(response, "output_parsed", None)
    if parsed is not None:
        return parsed
    output_text = str(getattr(response, "output_text", "") or "").strip()
    if output_text:
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", output_text, re.S)
        candidate = fenced.group(1) if fenced else output_text
        try:
            return AIAnalysis.model_validate_json(candidate)
        except ValueError:
            pass
    status = getattr(response, "status", "unknown")
    incomplete = getattr(response, "incomplete_details", None)
    error = AIOutputIncompleteError if status == "incomplete" else RuntimeError
    raise error(
        "OpenAI qualification returned no structured output "
        f"(status={status}, incomplete={incomplete or 'none'})"
    )


def _analyze_sync(
    candidate: DiscoveredHost,
    observation: SiteObservation,
    score: ScoreCard,
    *,
    model: str,
    web_search: bool,
) -> AIAnalysis:
    client = OpenAI(
        timeout=AI_REQUEST_TIMEOUT_SECONDS,
        http_client=ProviderHTTPClient(timeout=AI_REQUEST_TIMEOUT_SECONDS),
        max_retries=0,
    )
    payload = {
        "source_apex": candidate.source_apex,
        "discovered_hostname": candidate.hostname,
        "first_seen": candidate.first_seen.isoformat() if candidate.first_seen else None,
        "final_url": observation.final_url,
        "title": observation.title,
        "description": observation.description,
        "fetch_outcome": observation.fetch_outcome,
        "fetch_disposition": observation.fetch_disposition,
        "canonical_recovery_required": observation.canonical_recovery_required,
        "tenant_label": observation.tenant_label,
        "page_evidence": [page.model_dump(mode="json") for page in observation.pages],
        "public_contacts": observation.contacts.model_dump(mode="json"),
        "contact_urls": observation.contact_urls,
        "social_urls": observation.social_urls,
        "detected_technologies": observation.technologies,
        "deterministic_signals": observation.signals,
        "public_web_intelligence": observation.web.model_dump(mode="json"),
        "deterministic_score": score.model_dump(mode="json"),
    }
    kwargs: dict[str, Any] = {
        "model": model,
        "instructions": AI_INSTRUCTIONS,
        "input": (
            "Stress-test and sharpen the deterministic Creative Capacity research hypothesis for "
            "one $5,000/month specialist lane. Separate cited observations from labeled "
            "inferences. Identify independently corroborated distribution, recurring workload, "
            "and production-capacity evidence; counterevidence; the economic-buyer and champion "
            "roles; up to three evidence-tied first-month asks, without padding when support is "
            "absent; and "
            "AI/rights risks. Never turn correlated technology clues into independent "
            "signals. Return unknown when evidence or scan coverage is missing. If web search is "
            "available, use it only to verify the official company identity, recurring creative "
            "workload, public team-pressure evidence, paid distribution, hiring, or recent "
            "campaigns and launches. Do not search for private personal information.\n\n"
            + _encode_evidence(payload)
        ),
        "text_format": AIAnalysis,
        "store": False,
        "max_output_tokens": AI_OUTPUT_TOKENS,
        "reasoning": {"effort": "low"},
        "prompt_cache_key": "beings-prospect-v6-conversion-intelligence-5k",
    }
    if web_search:
        kwargs["tools"] = [{"type": "web_search"}]
        kwargs["include"] = ["web_search_call.action.sources"]
        kwargs["max_tool_calls"] = 2
    response = client.responses.parse(**kwargs)
    record_openai_response(response, model)
    try:
        analysis = _parse_analysis(response)
    except AIOutputIncompleteError:
        kwargs["max_output_tokens"] = AI_RETRY_OUTPUT_TOKENS
        response = client.responses.parse(**kwargs)
        record_openai_response(response, model)
        analysis = _parse_analysis(response)

    allowed_urls = set(observation.evidence_urls)
    allowed_urls.update(page.url for page in observation.pages)
    allowed_urls.update(observation.contact_urls)
    allowed_urls.update(observation.social_urls)
    if observation.final_url:
        allowed_urls.add(observation.final_url)
    if web_search:
        allowed_urls.update(_collect_urls(response.model_dump(mode="json", warnings=False)))
    analysis.evidence_urls = [url for url in analysis.evidence_urls if url in allowed_urls][:10]
    if analysis.company_url not in allowed_urls:
        analysis.company_url = None
    analysis.public_decision_makers = [
        person for person in analysis.public_decision_makers if person.source_url in allowed_urls
    ][:5]
    analysis.observations = [
        claim
        for claim in analysis.observations
        if claim.source_url in allowed_urls and claim.observed_at is not None
    ][:8]
    analysis.inferences = [
        claim
        for claim in analysis.inferences
        if claim.source_url is None or claim.source_url in allowed_urls
    ][:8]
    return analysis


async def analyze_candidate(
    candidate: DiscoveredHost,
    observation: SiteObservation,
    score: ScoreCard,
    *,
    model: str,
    web_search: bool = False,
) -> AIAnalysis:
    return await provider_thread(
        _analyze_sync,
        candidate,
        observation,
        score,
        model=model,
        web_search=web_search,
    )
