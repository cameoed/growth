"""Explicit LeadOcean lookup with first-party proof for a named work number."""

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

MAXIMUM_MICROUSD = 5_000
ENDPOINT = "leadocean.people.phone.find"
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
                return {"value": phone, "source_url": str(response.url), "provider": "leadocean"}
        except (httpx.HTTPError, TimeoutError, ValueError, OSError):
            continue
    return None


async def _candidates(client, repo, reservation, profile, credentials):
    headers = {"X-Treg-Token": credentials.token, "Idempotency-Key": reservation,
               "X-Treg-Client": "beings-growth",
               "X-Treg-Meta": "workspace=beings-growth,workflow=work-phone-lookup"}
    if credentials.org:
        headers["X-Treg-Org"] = credentials.org
    try:
        response = await provider_request(
            client, "GET", f"{credentials.base_url}/call/{ENDPOINT}",
            params={"linkedin_url": profile}, headers=headers,
        )
    except Exception as exc:
        if is_local_rate_deferral(exc):
            repo.settle_budget(reservation, 0)
            raise ValueError("Phone lookup is temporarily rate limited") from exc
        raise
    if response.status_code == 202:
        raise RuntimeError("Phone lookup charge is pending")
    cost = TregPeopleClient._charged(response.headers)
    repo.settle_budget(reservation, cost)
    if response.status_code == 404 and cost == 0:
        return set(), response.headers.get("X-Treg-Call-Id")
    response.raise_for_status()
    data = response.json().get("data")
    if not isinstance(data, dict) or not isinstance(data.get("found"), bool):
        raise RuntimeError("Unexpected phone lookup response")
    rows = data.get("phones") or [data.get("best")]
    numbers = {phone for row in rows if isinstance(row, dict)
               and row.get("type") in ("direct", "office")
               and (phone := _dial(row.get("phone"))) and phone.startswith("+")}
    return numbers if data["found"] else set(), response.headers.get("X-Treg-Call-Id")


async def find_phone(repo, packet, key):
    company = packet["company_key"]
    require_allowed(company)
    person = resolve_person(repo.connection, packet, key)
    profile = _linkedin(person.get("profile_url"))
    if not profile:
        raise ValueError("A LinkedIn profile is needed for this phone lookup")
    if person.get("phone") or person.get("phone_lookup_blocked"):
        return {"contact": None, "found": False, "cached": True}
    identity = (person["name"], person.get("profile_url"))
    identity_key = hashlib.sha256(
        json.dumps([VERSION, company, key, *identity]).encode()
    ).hexdigest()
    cached = repo.connection.execute(
        "SELECT cache_key,payload_json,observed_at FROM engine_contact_lookups "
        "WHERE identity_key=? ORDER BY observed_at DESC,cache_key DESC LIMIT 1", (identity_key,),
    ).fetchone()
    now = datetime.now(UTC)
    reused = cached is not None and datetime.fromisoformat(cached["observed_at"]) > (
        now - timedelta(days=FRESH_DAYS))
    if reused:
        phone = json.loads(cached["payload_json"])["phone"]
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
            purpose="Public work phone lookup", maximum_microusd=MAXIMUM_MICROUSD,
        ))
        if not reserved["created"]:
            raise ValueError("A phone lookup already ran or is awaiting confirmation")
        timeout = httpx.Timeout(15, connect=6)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            candidates, call_id = await _candidates(client, repo, reservation, profile, credentials)
            phone = await _corroborate(client, packet, person, candidates) if candidates else None
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
                 json.dumps({"phone": phone, "call_id": call_id}), stamp, identity_key),
            )
            repo.connection.commit()
        except BaseException:
            repo.connection.rollback()
            raise
    contact = (outbound.fill_contact_gaps(repo.connection, packet, key, {"phone": phone},
                                          observed_at=stamp, expected_identity=identity)
               if phone else None)
    found = bool(contact and phone and contact["phone"] == phone["value"])
    return {"contact": contact, "found": found, "cached": reused}
