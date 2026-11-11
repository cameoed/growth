"""Mission-guided scan contracts, using synthetic settings and mocked providers."""
import json
from types import SimpleNamespace

import pytest

from beings_leads import ai_source_queries, free_source_scout, source_discovery_ai, web
from beings_leads.ad_intelligence import AdDiscoveryRequest
from beings_leads.missions import scan_context as context
from beings_leads.missions.store import create_mission, get_mission, initialize
from beings_leads.models import SourceSuggestion
from beings_leads.scan_goal import ScanGoalRequest
from beings_leads.store import LeadStore


def repeatable_source():
    url = "https://source-directory.com/"
    return SourceSuggestion(
        source_kind="repeatable_search",
        url=url,
        name="Repeatable search source",
        category="Ecommerce",
        discovery_mechanism="Enumerate public sources and resolve official domains.",
        expected_6k_fit="high",
        confidence="high",
        evidence_urls=[url],
    )


@pytest.fixture
def mission_store(tmp_path):
    path = tmp_path / "synthetic.db"
    try:
        with LeadStore(path) as store:
            initialize(store.connection)
            mission = create_mission(
                store.connection, "Robotics", "Find robotics companies in Germany")
            spec = get_mission(store.connection, mission["id"])["spec"]
            spec.update(topics=[{"terms": ["industrial robotics", "automation equipment"]}],
                        geography=[{"terms": ["Germany"], "weight": 30}],
                        workload_metrics=[{"id": "employee_count", "minimum": 500, "weight": 20}])
            store.connection.execute("UPDATE engine_missions SET spec_json=? WHERE id=?",
                                     (json.dumps(spec), mission["id"]))
            store.connection.commit()
            yield store, mission["id"]
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)


def test_saved_interpretation_terms_and_metrics_are_preserved_without_compilation(mission_store):
    store, mission = mission_store
    result = context.scan_context(store.connection, mission)
    assert result["mission_id"] == mission
    assert result["revision"] == 1
    assert "industrial robotics" in result["search_terms"]
    assert result["criteria"]["workload_metrics"][0]["minimum"] == 500
    assert context.scan_context(store.connection, "beings") == {}
    store.connection.execute("UPDATE engine_missions SET archived=1 WHERE id=?", (mission,))
    with pytest.raises(LookupError):
        context.scan_context(store.connection, mission)


def test_uncompiled_mission_never_silently_becomes_beings(mission_store):
    store, _ = mission_store
    pending = create_mission(store.connection, "Pending", "A new mission", pending_ai=True)
    with pytest.raises(ValueError, match="setup"):
        context.scan_context(store.connection, pending["id"])


def test_free_queries_use_custom_topics_and_geography(mission_store):
    store, mission = mission_store
    saved = context.scan_context(store.connection, mission)
    queries = free_source_scout.source_queries("synthetic-run", mission_context=saved)
    assert len(queries) == free_source_scout.MAX_QUERIES
    assert all("Germany" in query for query in queries)
    assert all(any(term in query for term in saved["search_terms"]) for query in queries)
    assert not any("skincare" in query for query in queries)
    brief = ai_source_queries.randomized_brief("seed", [], saved["search_terms"], saved)
    assert brief["selected_mission"]["criteria"]["workload_metrics"]


def test_source_order_is_relevant_stable_and_non_destructive(mission_store):
    store, mission = mission_store
    saved = context.scan_context(store.connection, mission)
    unrelated = SimpleNamespace(name="Beauty brands", url="https://beauty.example")
    relevant = SimpleNamespace(name="Industrial robotics members", url="https://robots.example")
    sources = [unrelated, relevant]
    ordered = sorted(sources, key=lambda source: -context.source_preference(source, saved))
    assert ordered == [relevant, unrelated]
    assert sources == [unrelated, relevant]


@pytest.mark.parametrize("model,arguments", [
    (web.SmartScanStartRequest, {}), (web.WebScanEstimateRequest, {}),
    (web.SourceScoutRequest, {}), (web.DiscoveryMethodRunRequest, {"method": "public_graph"}),
    (web.ScanRequest, {}), (AdDiscoveryRequest, {"meta_queries": ["robotics"]}),
    (ScanGoalRequest, {"goal": "Find robotics firms", "confirmed_maximum_microusd": 20000}),
])
def test_mission_identity_round_trips_with_legacy_default(model, arguments):
    assert model(**arguments).mission_id == "beings"
    request = model(**arguments, mission_id="custom-robotics")
    assert model.model_validate_json(request.model_dump_json()).mission_id == "custom-robotics"


def test_source_resolver_transmits_complete_context_and_keeps_its_cap(monkeypatch):
    calls = []
    response = SimpleNamespace(status="completed", output_parsed={"domains": []},
                               model_dump=lambda **_: {"output": []})
    def parse(**kwargs):
        calls.append(kwargs)
        return response
    monkeypatch.setattr(source_discovery_ai, "OpenAI", lambda **_: SimpleNamespace(
        responses=SimpleNamespace(parse=parse)))
    saved = {"mission_id": "robots", "revision": 4, "search_terms": ["industrial robotics"],
             "criteria": {"employee_count": {"minimum": 500, "weight": 20}}}
    source_discovery_ai._resolve_sync(repeatable_source(), model="mock",
                                     max_results=5, max_tool_calls=1, entity_hints=[],
                                     mission_context=saved)
    assert json.loads(calls[0]["input"])["selected_mission"] == saved
    assert calls[0]["max_tool_calls"] == 1
    assert "not verified evidence" in calls[0]["instructions"] or (
        "as verified evidence" in calls[0]["instructions"])


def test_source_scout_replaces_beings_target_without_extra_search_calls(monkeypatch):
    from beings_leads import source_scout, spending
    from beings_leads.models import SourceScoutBatch

    calls = []
    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status="completed", output_parsed=SourceScoutBatch(suggestions=[]), output_text="",
            model_dump=lambda **_: {"output": [{"type": "web_search_call", "action": {
                "type": "search", "sources": [], "queries": ["industrial robotics directory"],
            }}]},
        )
    monkeypatch.setattr(spending, "record_openai_response", lambda *args: None)
    monkeypatch.setattr(source_scout, "OpenAI", lambda **_: SimpleNamespace(
        responses=SimpleNamespace(parse=parse)))
    saved = {"mission_id": "robots", "revision": 2, "brief": "Industrial robotics in Germany",
             "search_terms": ["industrial robotics"], "criteria": {"minimum_employees": 500}}
    source_scout._scout_sync([], [], model="mock", max_suggestions=1, max_tool_calls=1,
                             cycle=4, mission_context=saved)
    assert len(calls) == 1
    body = json.loads(calls[0]["input"])
    assert body["target"] == saved["brief"]
    assert body["selected_mission"]["criteria"] == saved["criteria"]
    assert all("industrial robotics" in query for query in body["query_variants"])
    assert "$5,000" not in calls[0]["input"]
    assert calls[0]["max_tool_calls"] == 1


def test_mission_context_api_is_read_only_and_rejects_unknown(mission_store, monkeypatch):
    from fastapi import HTTPException

    store, mission = mission_store
    monkeypatch.setattr(web, "database_path", lambda: store.path)
    before = store.connection.total_changes
    result = web.selected_scan_mission_context(mission)
    assert result["mission_id"] == mission
    assert store.connection.total_changes == before
    with pytest.raises(HTTPException) as missing:
        web.selected_scan_mission_context("unknown-mission")
    assert missing.value.status_code == 422


async def test_resolution_cache_is_shared_for_same_mission_but_not_other_revisions(monkeypatch):
    import httpx

    from beings_leads import source_discovery

    receipts, calls = {}, []
    class Cache:
        pages_reused = 0
        def get(self, key, kind):
            return receipts.get((key, kind))
        def put(self, key, kind, value):
            receipts[key, kind] = value
        def candidates(self, values):
            return []
    async def resolve(*args, **kwargs):
        calls.append(kwargs["mission_context"])
        return [], 1
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    monkeypatch.setattr(source_discovery, "_ai_candidates", resolve)
    async with httpx.AsyncClient() as client:
        for revision in [1, 1, 2]:
            await source_discovery.discover_source(
                client, repeatable_source(), page_cache=Cache(),
                mission_context={"mission_id": "robots", "revision": revision},
            )
    assert [call["revision"] for call in calls] == [1, 2]
    assert len(receipts) == 2
