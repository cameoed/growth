"""Explicit provider lookups for one named company contact."""

import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta

import httpx

from ..domain_policy import require_allowed
from ..microlink_discovery import provider_search
from ..provider_rate_limit import is_local_rate_deferral
from ..treg_people import (
    DEFAULT_EMAIL_MAX_COST_USD,
    DISCOVERY_UNIT_COST_USD,
    ProviderPerson,
    TregPeopleClient,
    resolve_treg_credentials,
)
from . import outbound
from .contact_lookup import resolve_person
from .contacts import FRESH_DAYS, _email
from .models import BudgetReservationRequest
from .phone_lookup import _linkedin
from .summaries import _identity

PROFILE_LIMIT = 5
MAXIMUMS = {
    "email": int(DEFAULT_EMAIL_MAX_COST_USD * 1_000_000),
    "profile_url": int(DISCOVERY_UNIT_COST_USD * PROFILE_LIMIT * 1_000_000),
}


def _lookup_key(field, company, key, identity):
    return hashlib.sha256(
        json.dumps(["individual-contact-v1", field, company, key, *identity]).encode()
    ).hexdigest()


def _latest_receipt(connection, identity_key):
    return connection.execute(
        "SELECT cache_key,payload_json,observed_at FROM engine_contact_lookups "
        "WHERE identity_key=? ORDER BY observed_at DESC,cache_key DESC LIMIT 1", (identity_key,),
    ).fetchone()


def _matching_profile(results, name, employer):
    person = re.escape(outbound.identity_text(name))
    company = re.escape(outbound.identity_text(employer))
    name_pattern = rf"^{person}(?:$|\s+[-–—·|])"
    employer_pattern = rf"(?:\bat\s+|[-–—·|]\s*){company}(?=$|\s+[-–—·|]|[.,;](?:\s|$))"
    matches = {}
    for result in results:
        if not isinstance(result, dict) or not (url := _linkedin(result.get("url"))):
            continue
        title = str(result.get("title") or "")[:500]
        excerpt = " ".join(
            str(result.get(field) or "")[:500] for field in ("description", "snippet")
        ).strip()
        if not re.search(name_pattern, outbound.identity_text(title)):
            continue
        if not any(re.search(employer_pattern, outbound.identity_text(value))
                   for value in (title, excerpt)):
            continue
        matches.setdefault(url.casefold(), {
            "value": url, "source_url": url, "provider": "Microlink",
            "source_kind": "public_web_search", "source_title": title, "source_excerpt": excerpt,
        })
    return next(iter(matches.values())) if len(matches) == 1 else None


async def find_profile_on_web(repo, packet, key):
    company = packet["company_key"]
    require_allowed(company)
    person = resolve_person(repo.connection, packet, key)
    if person.get("profile_url") or person.get("profile_url_lookup_blocked"):
        return {"found": False, "contact": None, "cached": True, "requests": 0, "failed": False}
    identity = (person["name"], person.get("profile_url"))
    primary_key = _lookup_key("profile_url", company, key, identity)
    primary = _latest_receipt(repo.connection, primary_key)
    now = datetime.now(UTC)
    if (primary is None or json.loads(primary["payload_json"]).get("value")
            or datetime.fromisoformat(primary["observed_at"]) <= now - timedelta(days=FRESH_DAYS)):
        raise ValueError("Run Find profile before searching the web")
    receipt_key = _lookup_key("profile-web", company, key, (*identity, primary["cache_key"]))
    api_key = os.getenv("MICROLINK_API_KEY", "").strip()
    if not api_key:
        raise ValueError("Configure Microlink in Settings first")
    with repo._transaction():
        claimed = repo.connection.execute(
            "INSERT OR IGNORE INTO engine_contact_lookups "
            "(cache_key,candidate_id,payload_json,observed_at,identity_key) VALUES(?,?,?,?,?)",
            (receipt_key, packet["candidate_id"], '{"pending":true}', now.isoformat(), receipt_key),
        ).rowcount
    if claimed:
        employer = " ".join(str(packet.get("company_name") or company).split())[:160] or company
        query = f"{identity[0]} linkedin works at {employer}"
        value = None
        failed = False
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                results, _ = await provider_search(
                    client, api_key, ("profile", "search", "us", "", query),
                )
            value = _matching_profile(results, identity[0], employer)
        except (httpx.HTTPError, OSError, ValueError):
            failed = True
        stamp = datetime.now(UTC).isoformat()
        with repo._transaction():
            repo.connection.execute(
                "UPDATE engine_contact_lookups SET payload_json=?,observed_at=? WHERE cache_key=?",
                (json.dumps({"value": value, "failed": failed}), stamp, receipt_key),
            )
    else:
        cached = _latest_receipt(repo.connection, receipt_key)
        if cached is None:
            raise LookupError("Company no longer exists")
        receipt = json.loads(cached["payload_json"])
        if receipt.get("pending"):
            raise ValueError("This web search already started or its usage is unconfirmed")
        value, failed, stamp = receipt["value"], receipt["failed"], cached["observed_at"]
    contact = (outbound.fill_contact_gaps(
        repo.connection, packet, key, {"profile_url": value},
        observed_at=stamp, expected_identity=identity,
    ) if value else None)
    return {"found": bool(contact and contact["profile_url"] == value["value"]),
            "contact": contact, "cached": not claimed, "requests": claimed, "failed": failed}


async def _request(client, field, company, person, name, reservation):
    if field == "email":
        profile = _linkedin(person.get("profile_url")) or ""
        match = await client.find_work_email(
            domain=company, person=ProviderPerson(*name, person.get("title") or "", profile),
            idempotency_key=reservation,
        )
        if match.pending:
            raise RuntimeError("Email lookup charge is pending")
        value = _email(match.email, company, provider=True) if match.verified else None
        if value and value.split("@")[0] in {"info", "contact", "hello", "sales", "support",
                                             "admin", "office", "team"}:
            value = None
        source_url, provider = profile or None, match.provider or "Treg"
    else:
        match = await client.search_people(
            domain=company, titles=(), name=name, limit=PROFILE_LIMIT,
            idempotency_key=reservation,
        )
        profiles = {
            url for row in match.people
            if row.company_domain_match is True
            and outbound.identity_text(row.full_name) == outbound.identity_text(person["name"])
            and (url := _linkedin(row.linkedin_url))
        }
        # A truncated response cannot rule out another person with the same name.
        unique = len(profiles) == 1 and match.total <= PROFILE_LIMIT
        value = next(iter(profiles)) if unique else None
        source_url, provider = value, "IcyPeas"
    evidence = {"value": value, "source_url": source_url, "provider": provider,
                "source_kind": "licensed_professional_provider"} if value else None
    return evidence, match.charged_microusd, match.call_id


async def find_individual_field(repo, packet, key, field):
    company = packet["company_key"]
    require_allowed(company)
    person = resolve_person(repo.connection, packet, key)
    if person.get(field) or person.get(f"{field}_lookup_blocked"):
        return {"found": False, "contact": None, "cached": True, "cost_microusd": 0}
    name = person["name"].split(maxsplit=1)
    if len(name) != 2:
        raise ValueError("A first and last name are needed for this lookup")
    identity = (person["name"], person.get("profile_url"))
    identity_key = _lookup_key(field, company, key, identity)
    cached = _latest_receipt(repo.connection, identity_key)
    receipt = json.loads(cached["payload_json"]) if cached else {}
    lifetime = timedelta(minutes=5) if receipt.get("failed") else timedelta(days=FRESH_DAYS)
    reused = bool(cached and datetime.fromisoformat(cached["observed_at"])
                  > datetime.now(UTC) - lifetime)
    failed = False
    cost = 0
    if reused:
        value = receipt["value"]
        failed = receipt.get("failed", False)
        stamp = cached["observed_at"]
    else:
        credentials = resolve_treg_credentials()
        if credentials is None:
            raise ValueError("Configure Treg in Settings first")
        cache_key = (hashlib.sha256(f"{identity_key}:{cached['cache_key']}".encode()).hexdigest()
                     if cached else identity_key)
        reservation = f"individual-{field}:{cache_key}"
        reserved = repo.reserve_budget(BudgetReservationRequest(
            idempotency_key=reservation, company_key=company, provider="treg",
            purpose=f"Individual contact {field} lookup", maximum_microusd=MAXIMUMS[field],
        ))
        if not reserved["created"]:
            raise ValueError("A lookup already ran or is awaiting confirmation")
        try:
            value, cost, call_id = await _request(
                TregPeopleClient(credentials), field, company, person, tuple(name), reservation,
            )
        except Exception as exc:
            cost = 0 if is_local_rate_deferral(exc) else getattr(exc, "charged_microusd", None)
            if type(cost) is not int:
                raise
            value, call_id, failed = None, getattr(exc, "call_id", None), True
        repo.settle_budget(reservation, cost)
        stamp = datetime.now(UTC).isoformat()
        with repo._transaction():
            require_allowed(company)
            current = resolve_person(repo.connection, packet, key)
            if (_identity(repo.connection, packet["candidate_id"])["company_key"] != company
                    or (current["name"], current.get("profile_url")) != identity):
                raise ValueError("Contact identity changed; reopen this company")
            repo.connection.execute(
                "INSERT INTO engine_contact_lookups "
                "(cache_key,candidate_id,payload_json,observed_at,identity_key) VALUES(?,?,?,?,?)",
                (cache_key, packet["candidate_id"],
                 json.dumps({"value": value, "call_id": call_id, "failed": failed}),
                 stamp, identity_key),
            )
    contact = (outbound.fill_contact_gaps(repo.connection, packet, key, {field: value},
                                         observed_at=stamp, expected_identity=identity)
               if value else None)
    return {"found": bool(contact and contact[field] == value["value"]), "contact": contact,
            "cached": reused, "cost_microusd": cost, "failed": failed,
            "web_search_available": field == "profile_url" and value is None}
