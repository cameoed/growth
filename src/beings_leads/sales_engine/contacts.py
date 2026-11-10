"""Recommend existing professional contact evidence without collection or outreach.

Titles identify useful people to research, not confirmed purchasing authority. An email
is exposed for copying only when its existing evidence permits that route; it never
establishes consent. Public identity and licensed email provenance remain separate.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from ..contact_intelligence import (
    ELIGIBLE_PURPOSES,
    RESTRICTED_PURPOSES,
    classify_purpose,
    normalize_email,
    preferred_public_phone,
)
from ..models import PublicPhoneEvidence
from ..treg_people import PEOPLE_SEARCH_SOURCE_URL, classify_role
from .models import normalize_company_key, validate_source_url

FRESH_DAYS = 90
COMPANY_CONTACT_DISPLAY_LIMIT = 10
PUBLIC = "public_first_party"
LICENSED = "licensed_professional_provider"
REJECTED = {"rejected", "expired", "obsolete", "historical", "disputed", "former", "inactive"}
# The companies link to these separate first-party publications for leadership bios.
# Keep email and fallback contact routes under their existing ownership rules.
PUBLIC_PERSON_SOURCE_ALIASES = {"google.com": "blog.google", "youtube.com": "blog.youtube"}


def _text(value: Any, limit: int = 500) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _date(value: Any) -> datetime | None:
    try:
        date = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return date.astimezone(UTC) if date.tzinfo is not None else None


def _url(value: Any) -> str | None:
    try:
        return validate_source_url(value) if isinstance(value, str) and value else None
    except ValueError:
        return None


def _company_host(value: Any) -> str:
    return (urlsplit(_url(value) or "").hostname or "").lower().removeprefix("www.")


def _own_host(host: str, company: str) -> bool:
    return host == company or host.endswith("." + company)


def _own_url(value: Any, company: str) -> str | None:
    url = _url(value)
    return url if url and _own_host(_company_host(url), company) else None


def _profile(value: Any, company: str) -> str | None:
    url = _url(value)
    host = _company_host(url)
    if url and (_own_host(host, company) or host == "linkedin.com"):
        return url
    return None


def _contact_route(value: Any, company: str) -> str | None:
    url = _own_url(value, company)
    if url and classify_purpose("", urlsplit(url).path) not in RESTRICTED_PURPOSES:
        return url
    return None


def _rows(value: Any, *, complete=False) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [row for row in (value if complete else value[:100]) if isinstance(row, dict)]


def _identity_allowed(row: dict, company: str) -> bool:
    if row.get("historical") or row.get("current") is False or row.get("accepted") is False:
        return False
    if any(
        _text(row.get(key)).lower() in REJECTED
        for key in ("status", "validity_status", "acceptance_status", "employment_status")
    ):
        return False
    if row.get("company_domain_match") is False:
        return False
    if row.get("division_match") is False or row.get("subject_role") in {
        "client",
        "customer",
        "supplier",
        "portfolio_client",
        "former_employee",
    }:
        return False
    for field in ("company_key", "company_domain", "company_website"):
        raw = row.get(field)
        if raw:
            value = str(raw).removeprefix("domain:")
            host = _company_host(value if "://" in value else "https://" + value)
            if not _own_host(host, company):
                return False
    return True


def _freshness(row: dict, fallback: Any, now: datetime) -> tuple[str, datetime | None]:
    observed = _date(row.get("employment_verified_at") or row.get("observed_at") or fallback)
    expires = _date(row.get("expires_at"))
    if expires is not None and expires <= now:
        return "stale", observed
    if observed is not None:
        if observed > now or now - observed > timedelta(days=FRESH_DAYS):
            return "stale", observed
        return "current", observed
    return "unknown", None


def _role(title: str, archetype: str) -> tuple[int, str] | None:
    # Expand the two unambiguous marketing abbreviations for the existing title classifier.
    expanded = re.sub(r"\bcmo\b", "chief marketing officer", title, flags=re.I)
    expanded = re.sub(r"\bcgo\b", "chief growth officer", expanded, flags=re.I)
    # A production title in a factory or software organization is not creative ownership.
    if re.search(
        r"\b(?:manufacturing|factory|plant|industrial|software|engineering)\b", title, re.I
    ) and not re.search(r"\b(?:marketing|creative|brand|content|video|film|design)\b", title, re.I):
        return None
    # Procurement is relevant only with explicit creative/marketing remit. Generic
    # purchasing responsibility is not evidence of buying this service.
    if (
        re.search(r"\b(?:procurement|purchasing|sourcing)\b", title, re.I)
        and re.search(r"\b(?:marketing|creative|advertising|production services)\b", title, re.I)
        and re.search(r"\b(?:chief|vp|vice president|head|director|manager)\b", title, re.I)
    ):
        return 188, "Documented responsibility for creative or marketing procurement."
    classification = classify_role(expanded, company_archetype=archetype)
    if classification is None:
        return None
    if classification.lane == "economic_buyer":
        return classification.score, "Company leader; fallback route to the buying owner."
    return classification.score + 100, "Leads relevant marketing or creative work."


def _lane(title: str) -> str:
    if re.search(r"\b(?:procurement|purchasing|sourcing)\b", title, re.I):
        return "purchasing_contact"
    if re.search(r"\b(?:production|producer|studio|creative operations)\b", title, re.I):
        return "production_owner"
    role = classify_role(title)
    return "company_leader" if role and role.lane == "economic_buyer" else "marketing_creative"


def _email(value: Any, company: str, *, provider: bool = False) -> str | None:
    # Do not silently strip mail headers or repair a provider's malformed address.
    if not isinstance(value, str) or any(char.isspace() for char in value) or "?" in value:
        return None
    address = normalize_email(value)
    if not address or classify_purpose(address) in RESTRICTED_PURPOSES:
        return None
    if provider and not _own_host(address.rsplit("@", 1)[1], company):
        return None
    return address


def _public_emails(
    contacts: dict, company: str, observed: Any, now: datetime, *, complete=False,
) -> list[dict]:
    result = []
    for row in _rows(contacts.get("emails"), complete=complete):
        source = _own_url(row.get("source_url"), company)
        address = _email(row.get("address"), company)
        freshness, stamp = _freshness(row, observed, now)
        if (
            source
            and address
            and _identity_allowed(row, company)
            and row.get("outreach_eligible") is True
            and row.get("purpose") in ELIGIBLE_PURPOSES | {"unknown"}
            and freshness == "current"
        ):
            result.append({**row, "address": address, "source_url": source, "_observed": stamp})
    return result


def _person(
    row: dict,
    *,
    company: str,
    source_kind: str,
    source: str,
    observed: Any,
    now: datetime,
    archetype: str,
    email: dict | None = None,
    source_expires: Any = None,
    role_selector=None,
) -> dict | None:
    name = _text(row.get("name"), 200)
    title = _text(row.get("title") or row.get("role"), 240)
    role = role_selector(title, archetype) if role_selector else _role(title, archetype)
    freshness, stamp = _freshness(row, observed, now)
    if not name or not role or not _identity_allowed(row, company) or freshness == "stale":
        return None
    if re.search(r"\b(?:former|ex[- ]|retired)\b", title, re.I):
        return None
    profile = _profile(row.get("profile_url") or row.get("linkedin_url"), company)
    deadlines = [
        value
        for value in (
            _date(row.get("expires_at")),
            _date(source_expires),
            stamp + timedelta(days=FRESH_DAYS) if stamp else None,
        )
        if value is not None
    ]
    return {
        "name": name,
        "title": title,
        "source_kind": source_kind,
        "source_url": source,
        "profile_url": profile,
        "email": email["address"] if email else None,
        "email_source_kind": email["source_kind"] if email else None,
        "email_source_url": email["source_url"] if email else None,
        "why": role[1],
        "observed_at": stamp.isoformat() if stamp else None,
        "freshness": freshness,
        "expires_at": min(deadlines).isoformat() if deadlines else None,
        "responsibility": _lane(title),
        "authority_confirmed": False,
        "division": _text(row.get("division") or row.get("business_unit"), 200) or None,
        "_quality": {"high": 2, "medium": 1}.get(row.get("confidence"), 0),
        "_rank": role[0],
    }


def _same_person(left: dict, right: dict) -> bool:
    a, b = left["profile_url"], right["profile_url"]
    if a and b:
        return a.lower().rstrip("/") == b.lower().rstrip("/")
    return left["name"].casefold() == right["name"].casefold()


def _company_phone(contacts, company, observed, now):
    phones = []
    for row in _rows(contacts.get("phones"), complete=True):
        if (_freshness(row, observed, now)[0] != "current"
                or row.get("purpose") in RESTRICTED_PURPOSES
                or not _contact_route(row.get("source_url"), company)
                or not re.fullmatch(r"\+?[0-9]{7,15}", _text(row.get("dial_number")))):
            continue
        try:
            phones.append(PublicPhoneEvidence.model_validate(row))
        except ValueError:
            continue
    best = preferred_public_phone(phones)
    return next((phone for phone in phones if best and phone.dial_number == best
                 and phone.outreach_eligible and not phone.historical), None)


def recommend_contacts(packet: dict, limit: int | None = 3, *, role_selector=None) -> dict:
    """Select current stored buyer-role contacts; never fetch or infer contact details."""
    try:
        company = normalize_company_key(packet.get("company_key", ""))
    except (TypeError, ValueError):
        return {"people": [], "company_contact": None}
    now = _date(packet.get("as_of")) or datetime.now(UTC)
    contacts = packet.get("public_contacts") or {}
    licensed = packet.get("licensed_professional_contacts") or {}
    if not isinstance(contacts, dict):
        contacts = {}
    if not isinstance(licensed, dict):
        licensed = {}
    archetype = _text((packet.get("role") or {}).get("role"))
    observed = packet.get("observed_at")
    emails = _public_emails(contacts, company, observed, now, complete=limit is None)
    candidates = []
    public_people = _rows(contacts.get("people"), complete=limit is None)
    licensed_people = _rows(licensed.get("people"), complete=limit is None)
    # Later explicit employment/role changes supersede an older matching identity.
    # A newly collected copy must not revive someone known to have left the company.
    invalidated: list[tuple[dict, datetime]] = []
    for row in [*public_people, *licensed_people]:
        recorded = _date(
            row.get("observed_at")
            or (licensed.get("scanned_at") if row not in public_people else observed)
        )
        if recorded is None or recorded > now:
            continue
        if row.get("company_domain_match") is False:
            continue
        identity = {
            k: v
            for k, v in row.items()
            if k
            not in {
                "historical",
                "current",
                "accepted",
                "status",
                "validity_status",
                "acceptance_status",
                "employment_status",
            }
        }
        if not _identity_allowed(identity, company):
            continue
        changed = row.get("current") is False or any(
            _text(row.get(k)).lower() in REJECTED
            for k in ("status", "validity_status", "employment_status")
        )
        if changed:
            invalidated.append(
                (
                    {
                        "name": _text(row.get("name")),
                        "profile_url": _profile(
                            row.get("profile_url") or row.get("linkedin_url"), company
                        ),
                    },
                    recorded,
                )
            )
    published_names = {_text(row.get("name")).casefold() for row in public_people}
    profiles_by_name: dict[str, set[str]] = {}
    for row in public_people:
        profile = _profile(row.get("profile_url"), company)
        if profile:
            profiles_by_name.setdefault(_text(row.get("name")).casefold(), set()).add(
                profile.lower().rstrip("/")
            )
    # A semantic email/vCard can contain a named role even when no separate team card exists.
    public_people += [
        {
            "name": row.get("person_name"),
            "role": row.get("person_role"),
            "source_url": row["source_url"],
            "observed_at": row["_observed"],
        }
        for row in emails
        if row.get("person_name")
        and row.get("person_role")
        and _text(row.get("person_name")).casefold() not in published_names
    ]
    for row in public_people:
        raw_source = row.get("source_url")
        source = _own_url(raw_source, company)
        if not source and _company_host(raw_source) == PUBLIC_PERSON_SOURCE_ALIASES.get(company):
            source = _url(raw_source)
        if not source:
            continue
        named_email = next(
            (
                email
                for email in emails
                if _text(email.get("person_name")).casefold() == _text(row.get("name")).casefold()
                and len(profiles_by_name.get(_text(row.get("name")).casefold(), set())) <= 1
            ),
            None,
        )
        person = _person(
            row,
            company=company,
            source_kind=PUBLIC,
            source=source,
            observed=observed,
            now=now,
            archetype=archetype,
            role_selector=role_selector,
            email={**named_email, "source_kind": PUBLIC} if named_email else None,
        )
        if person:
            candidates.append(person)
    scanned = _date(licensed.get("scanned_at"))
    expires = _date(licensed.get("expires_at"))
    if (
        licensed.get("status") in {"fresh", "needs_more_contacts"}
        and scanned is not None
        and scanned <= now
        and now - scanned <= timedelta(days=FRESH_DAYS)
        and expires is not None
        and expires > now
        and _identity_allowed(licensed, company)
    ):
        for row in licensed_people:
            if not (
                row.get("company_domain_match") is True
                or any(row.get(k) for k in ("company_key", "company_domain", "company_website"))
            ):
                continue
            address = (
                _email(row.get("work_email"), company, provider=True)
                if row.get("email_status") == "found"
                else None
            )
            source = _url(row.get("source_url")) or PEOPLE_SEARCH_SOURCE_URL
            person = _person(
                row,
                company=company,
                source_kind=LICENSED,
                source=source,
                observed=scanned,
                source_expires=min(expires, scanned + timedelta(days=FRESH_DAYS)),
                now=now,
                archetype=archetype,
                role_selector=role_selector,
                email={"address": address, "source_kind": LICENSED, "source_url": source}
                if address
                else None,
            )
            if person:
                candidates.append(person)
    candidates = [
        row
        for row in candidates
        if not any(
            _same_person(row, invalid)
            and (_date(row["observed_at"]) is None or changed_at >= _date(row["observed_at"]))
            for invalid, changed_at in invalidated
        )
    ]
    candidates.sort(
        key=lambda row: (
            row["freshness"] != "current",
            -row["_rank"],
            -row["_quality"],
            row["source_kind"] != PUBLIC,
            not bool(row["email"] or row["profile_url"]),
            row["name"].casefold(),
        )
    )
    people: list[dict] = []
    for candidate in candidates:
        duplicate = next((person for person in people if _same_person(person, candidate)), None)
        if duplicate is not None:
            if not duplicate["email"] and candidate["email"]:
                for field in ("email", "email_source_kind", "email_source_url"):
                    duplicate[field] = candidate[field]
            if not duplicate["profile_url"]:
                duplicate["profile_url"] = candidate["profile_url"]
            continue
        people.append(dict(candidate))
    if limit is not None:
        people = people[:max(0, min(int(limit), COMPANY_CONTACT_DISPLAY_LIMIT))]
    people = [
        {key: value for key, value in row.items() if not key.startswith("_")}
        for row in people
    ]
    company_contact = None
    phone = _company_phone(contacts, company, observed, now)
    needs_fallback = not people or any(not (person["email"] or person["profile_url"])
                                      for person in people)
    if needs_fallback or phone:
        route_contacts = contacts
        if (source_time := _date(observed)) and source_time > now:
            route_contacts = {}
        generic = (next((row for row in emails if not _text(row.get("person_name"))), None)
                   if needs_fallback else None)
        form = next(
            (
                url
                for raw in (route_contacts.get("contact_forms") or [])[:30]
                if (url := _contact_route(raw, company))
            ),
            None,
        )
        page = next(
            (
                url
                for raw in (route_contacts.get("contact_pages_checked") or [])[:30]
                if (url := _contact_route(raw, company))
            ),
            None,
        )
        route = form or page or f"https://{company}"
        company_contact = {
            "label": "Company contact",
            "source_kind": PUBLIC,
            "source_url": generic["source_url"] if generic else route,
            "email": generic["address"] if generic else None,
            "url": route,
            "why": (
                "Ask for the marketing or creative lead through the company's published route."
                if generic or form or page
                else "Open the company website to find a marketing or creative contact."
            ),
        }
        if phone:
            company_contact["phone"] = phone.dial_number
            company_contact["phone_source_url"] = phone.source_url
    from .outbound import person_key

    for person in people:
        person["prospect_key"] = person_key(company, person)
    return {"people": people, "company_contact": company_contact}
