"""Mock the paid provider boundary while exercising profile lookup and receipt storage."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beings_leads.models import DiscoveredHost
from beings_leads.sales_engine import individual_lookup, outbound
from beings_leads.sales_engine.api import create_router
from beings_leads.store import LeadStore


@pytest.mark.parametrize("mode", ["match", "ambiguous", "wrong_employer"])
def test_profile_web_search_after_initial_miss(tmp_path, monkeypatch, mode):
    database = tmp_path / "contacts.db"
    with LeadStore(database) as store:
        candidate = store.upsert_candidate(
            DiscoveredHost(hostname="fixture-brand.com", source_apex="fixture-brand.com")
        )
        store.connection.commit()
    domain = "fixture-brand.com"
    person = {"name": "Alex Buyer", "title": "VP Marketing"}
    person["prospect_key"] = outbound.person_key(domain, person)
    company = {
        "candidate_id": candidate,
        "company_key": domain,
        "company_name": "Fixture Brand",
        "contact_recommendations": {"people": [person]},
    }
    monkeypatch.setattr(
        "beings_leads.sales_engine.api._contact_summary", lambda *args: company,
    )
    monkeypatch.setattr(individual_lookup, "resolve_treg_credentials", lambda: object())
    monkeypatch.setattr(individual_lookup, "TregPeopleClient", lambda _: object())
    monkeypatch.setenv("MICROLINK_API_KEY", "fixture-key")
    searches = []

    async def initial_lookup(*args, **kwargs):
        return None, 0, "fixture-call"

    async def web_search(client, api_key, recipe):
        assert api_key == "fixture-key"
        searches.append(recipe[-1])
        if mode == "match":
            duplicate = await asyncio.to_thread(browser.post, path + "-web", json=payload)
            assert duplicate.status_code == 422
            assert "already started" in duplicate.json()["detail"]
        title = "Alex Buyer - VP at Fixture Brand | LinkedIn"
        if mode == "wrong_employer":
            title = "Alex Buyer - VP at Fixture Brand-West | LinkedIn"
        hits = [{"url": "https://linkedin.com/in/alex-buyer?trk=test", "title": title}]
        if mode == "ambiguous":
            hits.append({"url": "https://linkedin.com/in/other-alex", "title": title})
        return hits, False

    monkeypatch.setattr(individual_lookup, "_request", initial_lookup)
    monkeypatch.setattr(individual_lookup, "provider_search", web_search)
    app = FastAPI()
    app.include_router(create_router(lambda: database))
    path = f"/api/sales-engine/companies/{candidate}/contacts/find-profile"
    payload = {"person_key": person["prospect_key"]}
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as browser:
        assert browser.post(path + "-web", json=payload).status_code == 422
        assert searches == []
        first = browser.post(path, json=payload)
        assert first.status_code == 200, first.text
        assert first.json()["web_search_available"]
        assert searches == []
        second = browser.post(path + "-web", json=payload)
        assert second.status_code == 200, second.text
        result = second.json()
        assert result["found"] == (mode == "match")
        assert result["requests"] == 1
        if mode == "match":
            contact = result["contact"]
            assert contact["profile_url"] == "https://www.linkedin.com/in/alex-buyer"
            assert contact["provider_contact"]["profile_url"]["provider"] == "Microlink"
        repeat = browser.post(path + "-web", json=payload)
        assert repeat.status_code == 200, repeat.text
        assert repeat.json()["cached"]
        assert repeat.json()["requests"] == 0
        assert searches == ["Alex Buyer linkedin works at Fixture Brand"]
