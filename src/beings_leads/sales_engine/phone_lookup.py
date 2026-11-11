"""Bounded prospect phone lookup with first-party and provider provenance."""

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from ..contact_intelligence import normalize_phone
from ..domain_policy import require_allowed
from ..inspect import _bounded_get
from ..provider_rate_limit import is_local_rate_deferral, provider_request
from ..rss_discovery import RSSFetcher, RSSSource
from ..treg_people import TregPeopleClient, resolve_treg_credentials
from . import outbound
from .contact_lookup import resolve_person
from .contacts import FRESH_DAYS, _contact_route, _url
from .models import BudgetReservationRequest
from .summaries import _identity

PROVIDERS = (
    ("leadocean", "GET", "leadocean.people.phone.find", 5_000),
    ("blitzapi", "POST", "blitzapi.people.phone.find", 495),
)
MANUAL_PROVIDER = ("tomba", "GET", "tomba.people.phone.find", 44_500)
EXPECTED_CHAIN_MICROUSD = sum(provider[3] for provider in PROVIDERS)
RETRY_DELAY = timedelta(minutes=5)
VERSION = "treg-public-work-phone-v1"


def _linkedin(value):
    parsed = urlsplit(_url(value) or "")
    if (parsed.hostname in {"linkedin.com", "www.linkedin.com"}
            and re.fullmatch(r"/in/[A-Za-z0-9._%~-]+/?", parsed.path)):
        return "https://www.linkedin.com" + parsed.path.rstrip("/")
    return None


def _dial(value):
    if not isinstance(value, str) or not re.fullmatch(r"\+?[0-9 ().-]{7,40}", value):
        return None
    normalized = normalize_phone(value)
    return normalized[1] if normalized else None


def _same_number(left, right):
    if left.lstrip("+") == right.lstrip("+"):
        return True
    # Accept a published US national form of an explicit +1 candidate.
    return left.startswith("+1") and len(left) == 12 and left[2:] == right


def _records(value):
    if isinstance(value, dict):
        yield value
        yield from _records(value.get("@graph"))
    elif isinstance(value, list):
        for item in value:
            yield from _records(item)


def _published_work_phone(body, person, candidates, source_url):
    name = outbound.identity_text(person["name"])
    title = outbound.identity_text(person.get("title"))
    profile = _linkedin(person.get("profile_url"))
    named_source = (person.get("source_kind") == "public_first_party"
                    and (person.get("source_url") or "").rstrip("/") == source_url.rstrip("/"))
    soup = BeautifulSoup(body, "html.parser")
    published = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            records = list(_records(json.loads(tag.string or "")))
        except (ValueError, RecursionError):
            continue
        for record in records:
            types = record.get("@type", [])
            same_as = record.get("sameAs", [])
            same_as = [same_as] if isinstance(same_as, str) else same_as
            linked = isinstance(same_as, list) and profile and any(
                _linkedin(url) == profile for url in same_as
            )
            same_role = title and outbound.identity_text(record.get("jobTitle")) == title
            if ((types == "Person" or isinstance(types, list) and "Person" in types)
                    and outbound.identity_text(record.get("name")) == name
                    and (named_source or linked or same_role)):
                published.append(record.get("telephone"))
    for card in soup.select(".h-card, .vcard, [itemtype$='/Person']"):
        names = {outbound.identity_text(tag.get("content") or tag.get_text(" ", strip=True))
                 for tag in card.select(".p-name, .fn, [itemprop='name']")}
        linked = profile and any(
            _linkedin(tag["href"]) == profile for tag in card.select("a[href]")
        )
        same_role = title and any(
            outbound.identity_text(tag.get("content") or tag.get_text(" ", strip=True)) == title
            for tag in card.select(".p-job-title, .title, [itemprop='jobTitle']")
        )
        if names == {name} and (named_source or linked or same_role):
            published.extend(tag["href"][4:] for tag in card.select("a[href^='tel:']"))
    numbers = {phone for value in published if (phone := _dial(value))}
    matches = [candidate for candidate in candidates
               if any(_same_number(candidate, number) for number in numbers)]
    return matches[0] if len(matches) == 1 else None


async def _corroborate(client, packet, person, candidates):
    company = packet["company_key"]
    root = f"https://{company}/"
    routes = packet.get("contact_recommendations", {}).get("company_contact") or {}
    urls = dict.fromkeys((person.get("source_url"), routes.get("url"),
                          routes.get("phone_source_url"), root))
    robots = RSSFetcher(client, RSSSource("Company", root))

    async def redirect(_current, target):
        if not _contact_route(target, company):
            raise ValueError("Contact redirect left the public company site")
        await robots._robots(target)

    for url in urls:
        if not _contact_route(url, company):
            continue
        try:
            async with asyncio.timeout(8):
                await robots._robots(url)
                response, body = await _bounded_get(
                    client, url, max_bytes=1_200_000, max_redirects=2,
                    redirect_validator=redirect, timeout=6,
                )
            if (response.status_code == 200 and body
                    and not response.extensions.get("beings_body_truncated")
                    and (phone := _published_work_phone(body, person, candidates,
                                                       str(response.url)))):
                return {"value": phone, "source_url": str(response.url)}
        except (httpx.HTTPError, TimeoutError, ValueError, OSError):
            continue
    return None


def _parse_candidates(payload, provider):
    if not isinstance(payload, dict):
        return None
    data = payload.get("data", payload)
    if provider == "tomba":
        if not isinstance(data, dict):
            return set() if data is None or data == [] else None
        phone = _dial(data.get("e164_format"))
        return {phone} if phone and phone.startswith("+") and data.get("valid") else set()
    if not isinstance(data, dict) or not isinstance(data.get("found"), bool):
        return None
    if not data["found"]:
        return set()
    if provider == "blitzapi":
        phone = _dial(data.get("phone"))
        return {phone} if phone and phone.startswith("+1") else set()
    rows = data.get("phones") or [data.get("best")]
    if not isinstance(rows, list):
        return None
    return {phone for row in rows if isinstance(row, dict)
            and row.get("type") in ("direct", "office")
            and (phone := _dial(row.get("phone"))) and phone.startswith("+")}


async def _candidates(client, provider, method, endpoint, reservation, profile, credentials):
    headers = {"X-Treg-Token": credentials.token, "Idempotency-Key": f"{reservation}:{provider}",
               "X-Treg-Client": "beings-growth",
               "X-Treg-Meta": "workspace=beings-growth,workflow=work-phone-lookup"}
    if credentials.org:
        headers["X-Treg-Org"] = credentials.org
    profile_parameter = "linkedin" if provider == "tomba" else "linkedin_url"
    inputs = ({"params": {profile_parameter: profile}} if method == "GET"
              else {"json": {"person_linkedin_url": profile}})
    try:
        response = await provider_request(
            client, method, f"{credentials.base_url}/call/{endpoint}", headers=headers, **inputs,
        )
    except httpx.HTTPError as exc:
        if is_local_rate_deferral(exc):
            return None, 0, None
        raise
    if response.extensions.get("beings_request_deferred"):
        return None, 0, None
    if response.status_code == 202:
        raise RuntimeError("Phone lookup charge is pending")
    cost = TregPeopleClient._charged(response.headers)
    call_id = response.headers.get("X-Treg-Call-Id")
    if response.status_code == 404 and cost == 0:
        return set(), cost, call_id
    if not response.is_success:
        return None, cost, call_id
    try:
        return _parse_candidates(response.json(), provider), cost, call_id
    except ValueError:
        return None, cost, call_id


async def _provider_fallback(
    client, packet, person, profile, credentials, reservation, legacy_cap, manual_retry,
):
    providers = (MANUAL_PROVIDER,) if manual_retry else PROVIDERS
    reserved_cost = MANUAL_PROVIDER[3] if manual_retry else (
        legacy_cap if legacy_cap is not None else EXPECTED_CHAIN_MICROUSD
    )
    attempts = []
    spent = 0
    phone = None
    complete = True
    for provider, method, endpoint, limit in providers:
        if legacy_cap is not None and spent + limit > legacy_cap:
            continue
        numbers, cost, call_id = await _candidates(
            client, provider, method, endpoint, reservation, profile, credentials,
        )
        spent += cost
        if spent > reserved_cost:
            raise RuntimeError("Phone lookup charge exceeded its budget reservation")
        attempts.append({"provider": provider, "call_id": call_id, "cost_microusd": cost})
        if numbers is None:
            complete = False
        elif numbers:
            phone = await _corroborate(client, packet, person, numbers)
            if phone is None and len(numbers) == 1 and (manual_retry or legacy_cap != 5_000):
                phone = {"value": next(iter(numbers)), "source_url": profile,
                         "source_kind": "licensed_professional_provider"}
            if phone:
                phone["provider"] = provider
                return phone, spent, attempts, True
    return phone, spent, attempts, complete


async def find_phone(repo, packet, key, legacy_cap=None, *, manual_retry=False):
    if manual_retry and legacy_cap is not None:
        raise ValueError("Refresh the dashboard before trying Tomba")
    company = packet["company_key"]
    require_allowed(company)
    person = resolve_person(repo.connection, packet, key)
    profile = _linkedin(person.get("profile_url"))
    if not profile:
        raise ValueError("A LinkedIn profile is needed for this phone lookup")
    if person.get("phone") or person.get("phone_lookup_blocked"):
        return {"contact": None, "found": False, "cached": True, "cost_microusd": 0}
    identity = (person["name"], person.get("profile_url"))
    identity_key = hashlib.sha256(
        json.dumps([VERSION, company, key, *identity]).encode()
    ).hexdigest()
    cached = repo.connection.execute(
        "SELECT cache_key,payload_json,observed_at FROM engine_contact_lookups "
        "WHERE identity_key=? ORDER BY observed_at DESC,cache_key DESC LIMIT 1", (identity_key,),
    ).fetchone()
    now = datetime.now(UTC)
    cached_data = json.loads(cached["payload_json"]) if cached else {}
    complete = cached_data.get("complete", True)
    first_stage_cost = legacy_cap if legacy_cap is not None else EXPECTED_CHAIN_MICROUSD
    reserved_cost = MANUAL_PROVIDER[3] if manual_retry else first_stage_cost
    expanded = cached_data.get("maximum_microusd", 5_000) < first_stage_cost
    spent = 0
    age_limit = timedelta(days=FRESH_DAYS) if complete else RETRY_DELAY
    fresh = bool(cached and datetime.fromisoformat(cached["observed_at"]) > now - age_limit)
    cached_manual = cached_data.get("manual_retry", False)
    retryable_manual = (
        cached_manual and not complete
        and bool(cached_data.get("attempts"))
        and all(attempt.get("cost_microusd") == 0 for attempt in cached_data["attempts"])
    )
    retry_used = fresh and cached_manual and not retryable_manual
    retry_available = retryable_manual or (
        fresh and not cached_manual and not cached_data.get("phone")
        and {attempt["provider"] for attempt in cached_data.get("attempts", [])}
        == {provider[0] for provider in PROVIDERS}
    )
    if manual_retry and not (retry_available or retry_used):
        raise ValueError("Run Find phone before trying Tomba")
    reused = bool(
        (retryable_manual and (fresh or not manual_retry))
        or (fresh and (retry_used or cached_data.get("phone") or (
            not manual_retry and not expanded
        )))
    )
    if reused:
        phone = cached_data["phone"]
        stamp = cached["observed_at"]
    else:
        credentials = resolve_treg_credentials()
        if credentials is None:
            raise ValueError("Configure Treg in Settings first")
        cache_key = (hashlib.sha256(f"{identity_key}:{cached['cache_key']}".encode()).hexdigest()
                     if cached else identity_key)
        reservation = "work-phone:" + cache_key
        reserved = repo.reserve_budget(BudgetReservationRequest(
            idempotency_key=reservation, company_key=company, provider="treg",
            purpose="Public work phone lookup", maximum_microusd=reserved_cost,
        ))
        if not reserved["created"]:
            raise ValueError("A phone lookup already ran or is awaiting confirmation")
        timeout = httpx.Timeout(15, connect=6)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            phone, spent, attempts, complete = await _provider_fallback(
                client, packet, person, profile, credentials, reservation, legacy_cap, manual_retry,
            )
        repo.settle_budget(reservation, spent)
        retryable_manual = manual_retry and not complete and spent == 0
        retry_used = manual_retry and not retryable_manual
        retry_available = retryable_manual or (
            not manual_retry and not phone and len(attempts) == len(PROVIDERS)
        )
        stamp = datetime.now(UTC).isoformat()
        repo.connection.execute("BEGIN IMMEDIATE")
        try:
            require_allowed(company)
            latest = resolve_person(repo.connection, packet, key)
            if (_identity(repo.connection, packet["candidate_id"])["company_key"] != company
                    or (latest["name"], latest.get("profile_url")) != identity):
                raise ValueError("Contact identity changed; reopen this company")
            repo.connection.execute(
                "INSERT INTO engine_contact_lookups "
                "(cache_key,candidate_id,payload_json,observed_at,identity_key) VALUES(?,?,?,?,?)",
                (cache_key, packet["candidate_id"],
                 json.dumps({"phone": phone, "attempts": attempts, "complete": complete,
                             "maximum_microusd": reserved_cost, "manual_retry": manual_retry}),
                 stamp, identity_key),
            )
            repo.connection.commit()
        except BaseException:
            repo.connection.rollback()
            raise
    contact = (outbound.fill_contact_gaps(repo.connection, packet, key, {"phone": phone},
                                          observed_at=stamp, expected_identity=identity)
               if phone else None)
    found = bool(contact and phone and contact["phone"] == phone["value"])
    retry_at = datetime.fromisoformat(stamp) + RETRY_DELAY if retryable_manual else None
    return {"contact": contact, "found": found, "cached": reused, "cost_microusd": spent,
            "complete": complete, "retry_available": retry_available, "retry_used": retry_used,
            "retry_at": retry_at.isoformat() if retry_at and retry_at > now else None}
