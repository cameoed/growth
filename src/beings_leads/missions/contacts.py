"""Mission contact relevance and access evidence from the shared acceptance rules."""

import hashlib

from ..microlink_hydration import public_contacts
from ..sales_engine.contacts import COMPANY_CONTACT_DISPLAY_LIMIT, recommend_contacts
from ..sales_engine.summaries import _contact_expiry
from .context import matches

ACCESS_FACT = "contact.reachable_role"


def select_contacts(company, public, licensed, observed, now, terms, *, evidence=False):
    def role_selector(title, _archetype):
        if any(matches(title, term) for term in terms):
            return 200, "Stored role matches the mission contact function."
        if evidence:
            return None
        if not terms or any(
            matches(title, term)
            for term in ("chief executive", "ceo", "founder", "president", "owner")
        ):
            return 100, "Stored company leadership contact." if terms else "Stored role"
        return None

    return recommend_contacts(
        {
            "company_key": company,
            "as_of": now.isoformat(),
            "observed_at": observed,
            "public_contacts": public,
            "licensed_professional_contacts": licensed,
        },
        limit=None if evidence else COMPANY_CONTACT_DISPLAY_LIMIT,
        role_selector=role_selector,
    )


def access_evidence(store, company, public, observed, now, expires, terms):
    public = public_contacts(store.connection, company, public, now=now)
    licensed = store.treg_people_intelligence(company, as_of=now)
    expires = _contact_expiry(
        store.connection, company, public, licensed, observed, now, horizon=expires,
    )
    contacts = select_contacts(company, public, licensed, observed, now, terms, evidence=True)
    records = []
    for person in contacts["people"]:
        if person["freshness"] != "current" or not (person["email"] or person["profile_url"]):
            continue
        source = person["source_url"]
        route_source = person["email_source_url"] if not person["profile_url"] else None
        identity = hashlib.sha256(person["prospect_key"].encode()).hexdigest()
        records.append({
            "id": "mission-access:" + identity,
            "root": source,
            "dependencies": [route_source] if route_source and route_source != source else [],
            "source_url": source,
            "observed_at": person["observed_at"],
            "weight": 1,
            "fact_key": ACCESS_FACT,
            "value": True,
            "text": "",
        })
    return records, expires
