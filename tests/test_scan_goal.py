"""Goal configuration reserves budget but never launches research."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from beings_leads import scan_goal
from beings_leads.sales_engine.persistence import SalesEngineRepository
from beings_leads.store import LeadStore


@pytest.fixture(autouse=True)
def cleanup_databases(tmp_path):
    yield
    for pattern in ("test.db*", "capped.db*"):
        for path in tmp_path.glob(pattern):
            path.unlink(missing_ok=True)


CAPABILITIES = {"openai": True, "seo": True, "people": True, "ads": True, "microlink": True}


def draft():
    return scan_goal.ScanGoalPlan(
        search_terms=["enterprise marketing teams", "global enterprise companies"],
        due_hydration_enabled=True, due_company_limit=10, source_ai_enabled=False,
        microlink_enabled=False, ai_enabled=True, ai_site_limit=5, seo_enabled=False,
        seo_site_limit=10, people_hydration_enabled=True, people_company_limit=10,
        paid_ads_enabled=False, paid_ads_company_limit=5, paid_ads_max_usd=1,
        microlink_hydration=scan_goal.EvidenceOptions(**{
            key: False for key in scan_goal.EvidenceOptions.model_fields
            if key not in {"company_limit", "request_limit"}
        }, company_limit=10, request_limit=25),
    )


def request():
    return scan_goal.ScanGoalRequest(
        goal="Find decision makers at promising agencies", confirmed_maximum_microusd=20_000,
    )


def client(plan=None, failure=None):
    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        if failure:
            raise failure
        return SimpleNamespace(
            id="response-test", status="completed", output_parsed=plan or draft(),
            usage=SimpleNamespace(input_tokens=500, output_tokens=300, input_tokens_details=None),
        )
    return SimpleNamespace(responses=SimpleNamespace(parse=parse)), calls


def test_configure_is_cached_and_spend_is_settled(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    provider, calls = client()
    with LeadStore(tmp_path / "test.db") as store:
        result = scan_goal.configure_goal(
            store.connection, request(), CAPABILITIES, client=provider,
        )
        assert result["settings"]["search_terms"] == draft().search_terms
        assert result["settings"]["due_company_limit"] == 10
        assert result["settings"]["people_company_limit"] == 10
        assert result["cached"] is False
        cached = scan_goal.configure_goal(
            store.connection, request(), CAPABILITIES, client=provider,
        )
        assert cached["cached"] is True
        assert cached["cost_microusd"] == 0
        assert len(calls) == 1
        assert "tools" not in calls[0]
        assert calls[0]["model"] == "gpt-5-nano"
        assert store.connection.execute("SELECT COUNT(*) FROM smart_scan_runs").fetchone()[0] == 0
        budget = store.connection.execute(
            "SELECT status,actual_microusd FROM engine_budget_reservations"
        ).fetchone()
        assert budget["status"] == "settled"
        assert 0 < budget["actual_microusd"] <= 20_000


def test_uncertain_call_cannot_be_automatically_replayed(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    provider, calls = client(failure=TimeoutError("Provider did not answer"))
    with LeadStore(tmp_path / "test.db") as store:
        with pytest.raises(TimeoutError):
            scan_goal.configure_goal(store.connection, request(), CAPABILITIES, client=provider)
        with pytest.raises(ValueError, match="awaiting cost confirmation"):
            scan_goal.configure_goal(store.connection, request(), CAPABILITIES, client=provider)
        assert len(calls) == 1
        assert store.connection.execute(
            "SELECT status FROM engine_budget_reservations"
        ).fetchone()[0] == "reserved"


def test_provider_availability_and_dependencies_are_enforced():
    plan = draft().model_copy(update={
        "due_hydration_enabled": False, "source_ai_enabled": True, "microlink_enabled": True,
        "paid_ads_enabled": True, "seo_enabled": True,
    })
    result = scan_goal.available_plan(plan, {"openai": True})
    assert result["source_ai_enabled"] is False
    assert result["microlink_enabled"] is False
    assert result["paid_ads_enabled"] is False
    assert result["seo_enabled"] is False
    assert result["people_hydration_enabled"] is False
    assert result["ai_enabled"] is True


def test_goal_requires_explicit_cap_and_bounded_quantities():
    with pytest.raises(ValidationError):
        scan_goal.ScanGoalRequest(goal="Find companies", confirmed_maximum_microusd=0)
    with pytest.raises(ValidationError):
        scan_goal.ScanGoalPlan.model_validate({**draft().model_dump(), "ai_site_limit": 100})
    with pytest.raises(ValidationError):
        scan_goal.ScanGoalPlan.model_validate({**draft().model_dump(), "paid_ads_enabled": "true"})


def test_budget_rejection_never_dispatches(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    provider, calls = client()

    def blocked(self, request):
        raise ValueError("monthly research budget would be exceeded")

    monkeypatch.setattr(SalesEngineRepository, "reserve_budget", blocked)
    with LeadStore(tmp_path / "test.db") as store, pytest.raises(ValueError, match="budget"):
        scan_goal.configure_goal(store.connection, request(), CAPABILITIES, client=provider)
    assert calls == []


@pytest.mark.parametrize("prior_submitted", [0, 2])
async def test_conservative_refresh_cap_includes_prior_attempts(
    tmp_path, monkeypatch, prior_submitted,
):
    from beings_leads import web
    from beings_leads.models import DiscoveredHost

    database = tmp_path / "capped.db"
    with LeadStore(database) as store:
        for index in range(6):
            store.upsert_candidate(DiscoveredHost(
                source_apex="example.test", hostname=f"company-{index}.example",
            ))
    monkeypatch.setattr(web, "database_path", lambda: database)
    manager = web.SmartScanManager()
    manager.state.update(
        due_company_limit=3, inventory={},
        counters={"hydration_hosts_submitted": prior_submitted},
    )
    monkeypatch.setattr(manager, "_persist_state", lambda: None)
    submitted = []

    async def fake_scan(request, sources, maximum, **kwargs):
        assert manager.state["counters"]["hydration_hosts_submitted"] == 3
        submitted.extend(request.only_hosts)
        return {"status": "complete"}

    async def unbounded_gaps(*args, **kwargs):
        pytest.fail("Bounded refresh must not start an inventory-wide scanner-gap pass")

    monkeypatch.setattr(manager, "_wait_for_scan", fake_scan)
    monkeypatch.setattr(manager, "_run_scanner_gap_hydration", unbounded_gaps)
    await manager._run_base_hydration()
    assert len(submitted) == 3 - prior_submitted
    assert manager.state["counters"]["hydration_hosts_submitted"] == 3
    assert manager.state["hydration_progress"]["stage"] == "complete"


def test_goal_endpoint_sets_draft_without_starting_scan(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from beings_leads import web

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    monkeypatch.setattr(web, "database_path", lambda: tmp_path / "test.db")
    monkeypatch.setattr("beings_leads.release_mode.requires_user_mission", lambda: False)
    provider, calls = client()
    provider.close = lambda: None
    monkeypatch.setattr(scan_goal, "OpenAI", lambda **kwargs: provider)
    with LeadStore(tmp_path / "test.db"):
        pass
    api = TestClient(web.app, base_url="http://localhost", client=("127.0.0.1", 12345))
    result = api.post("/api/smart-scan/configure", json=request().model_dump())
    assert result.status_code == 200, result.text
    assert result.json()["settings"]["due_company_limit"] == 10
    assert len(calls) == 1
    assert web.smart_scan_manager.state["status"] == "idle"


@pytest.mark.parametrize("terms", [["a" * 101], ["enterprise"] * 6, [""], ["   "], ["one\ntwo"]])
def test_search_terms_are_bounded(terms):
    payload = draft().model_dump()
    payload["search_terms"] = terms
    with pytest.raises(ValidationError):
        scan_goal.ScanGoalPlan.model_validate(payload)
