from __future__ import annotations

import json
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, Field, field_validator

from .models import SourceSuggestion
from .provider_rate_limit import ProviderHTTPClient, provider_thread

SOURCE_DISCOVERY_AI_TIMEOUT_SECONDS = 300
SOURCE_DISCOVERY_AI_OUTPUT_TOKENS = 8_000

SOURCE_DISCOVERY_AI_INSTRUCTIONS = """You enumerate organizations from one public discovery source.
Return only an organization's official public website domain when a cited public page explicitly
connects that organization to the supplied directory, portfolio, customer list, tenant ecosystem,
or repeatable search. Use web search to reach public index pages, detail pages, PDFs, case studies,
and official organization sites. Do not return the source website, social networks, search engines,
asset/CDN hosts, login pages, or infrastructure providers. Do not infer private information.

This is research-pool discovery only. Never call an organization qualified, a fit, likely to buy,
able to afford the offer, or ready for outreach. Do not make scoring or qualification claims.
The supplied relationship type, source family, and buyer archetypes describe why the operator
accepted the source and which public members to enumerate. They are discovery context only: do not
copy those labels onto an organization as facts or use them to infer demand, dissatisfaction with
an incumbent provider, budget, production pressure, AI permission, or outreach readiness.
Every returned domain must include at least one public evidence URL surfaced by web search. Return
no more than the requested maximum and keep organization names concise.
"""


class CitedOfficialDomain(BaseModel):
    domain: str = Field(min_length=3, max_length=253)
    name: str = Field(default="", max_length=160)
    evidence_urls: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("domain")
    @classmethod
    def clean_domain(cls, value: str) -> str:
        return value.strip().lower().rstrip(".")


class CitedOfficialDomainBatch(BaseModel):
    domains: list[CitedOfficialDomain] = Field(default_factory=list, max_length=100)


class AIResolution(BaseModel):
    domains: list[CitedOfficialDomain] = Field(default_factory=list)
    tool_calls: int = Field(default=0, ge=0)
    allowed_evidence_urls: list[str] = Field(default_factory=list)


def _url_key(value: str) -> str:
    return value.strip().split("#", 1)[0].rstrip("/")


def _web_search_metadata(response: Any) -> tuple[set[str], int]:
    """Return provider-surfaced citation URLs without trusting model-authored output URLs."""

    try:
        payload = response.model_dump(mode="json", warnings=False)
    except (AttributeError, TypeError, ValueError):
        return set(), 0
    if not isinstance(payload, dict):
        return set(), 0

    allowed: set[str] = set()
    tool_calls = 0
    output = payload.get("output", [])
    if not isinstance(output, list):
        return allowed, tool_calls
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        tool_calls += 1
        action = item.get("action", {})
        sources = action.get("sources", []) if isinstance(action, dict) else []
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, dict):
                continue
            url = source.get("url") or source.get("link")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                allowed.add(_url_key(url))
    return allowed, tool_calls


def _parsed_batch(response: Any) -> CitedOfficialDomainBatch:
    parsed = getattr(response, "output_parsed", None)
    if parsed is not None:
        return CitedOfficialDomainBatch.model_validate(parsed)
    output_text = str(getattr(response, "output_text", "") or "").strip()
    if output_text:
        return CitedOfficialDomainBatch.model_validate_json(output_text)
    raise RuntimeError("OpenAI source resolver returned no structured output")


def _resolve_sync(
    suggestion: SourceSuggestion,
    *,
    model: str,
    max_results: int,
    max_tool_calls: int,
    entity_hints: list[str],
) -> AIResolution:
    result_limit = max(1, min(int(max_results), 100))
    tool_limit = max(1, min(int(max_tool_calls), 10))
    request_payload = {
        "source": {
            "source_key": suggestion.source_key,
            "source_kind": suggestion.source_kind,
            "name": suggestion.name,
            "apex": suggestion.apex,
            "url": suggestion.url,
            "discovery_mechanism": suggestion.discovery_mechanism,
            "evidence_urls": suggestion.evidence_urls,
            "relationship_type": suggestion.relationship_type,
            "source_family": suggestion.source_family,
            "buyer_archetypes": suggestion.buyer_archetypes,
        },
        "entity_hints": entity_hints[:100],
        "maximum_domains": result_limit,
    }
    client = OpenAI(
        timeout=SOURCE_DISCOVERY_AI_TIMEOUT_SECONDS,
        http_client=ProviderHTTPClient(timeout=SOURCE_DISCOVERY_AI_TIMEOUT_SECONDS),
        max_retries=0,
    )
    response = client.responses.parse(
        model=model,
        instructions=SOURCE_DISCOVERY_AI_INSTRUCTIONS,
        input=json.dumps(request_payload, ensure_ascii=False),
        tools=[{"type": "web_search"}],
        include=["web_search_call.action.sources"],
        text_format=CitedOfficialDomainBatch,
        store=False,
        max_tool_calls=tool_limit,
        max_output_tokens=SOURCE_DISCOVERY_AI_OUTPUT_TOKENS,
        reasoning={"effort": "low"},
        prompt_cache_key="beings-source-execution-official-domains-v2",
    )
    from .spending import record_openai_response

    record_openai_response(response, model)
    batch = _parsed_batch(response)
    allowed, tool_calls = _web_search_metadata(response)
    resolved: list[CitedOfficialDomain] = []
    seen: set[str] = set()
    for item in batch.domains[:result_limit]:
        evidence = [url for url in dict.fromkeys(item.evidence_urls) if _url_key(url) in allowed][
            :6
        ]
        key = item.domain.strip().lower().rstrip(".")
        if not evidence or not key or key in seen:
            continue
        seen.add(key)
        resolved.append(item.model_copy(update={"domain": key, "evidence_urls": evidence}))
    return AIResolution(
        domains=resolved,
        tool_calls=min(tool_calls, tool_limit),
        allowed_evidence_urls=sorted(allowed),
    )


async def resolve_source_domains(
    suggestion: SourceSuggestion,
    *,
    model: str,
    max_results: int = 50,
    max_tool_calls: int = 4,
    entity_hints: list[str] | None = None,
) -> AIResolution:
    """Resolve cited official domains in one bounded Responses API call."""

    return await provider_thread(
        _resolve_sync,
        suggestion,
        model=model,
        max_results=max_results,
        max_tool_calls=max_tool_calls,
        entity_hints=list(dict.fromkeys(entity_hints or [])),
    )
